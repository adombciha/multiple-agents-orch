from __future__ import annotations
import json
import re
from orchestrator.roles.base_agent import BaseAgent
from orchestrator.core import scoring, brain


def _extract_marker(qa_reports, marker: str) -> str | None:
    """Pull the value after a `MARKER:` line QA was told to add only when
    it applies. Weak local models don't reliably follow "only add this
    line if X" -- they often add it anyway with a "None"/"not needed"
    value instead of omitting it, so a naive extraction would treat that
    as a real, actionable signal. Filter those out here."""
    no_op_starts = (
        "none",
        "n/a",
        "na ",
        "no ",
        "not needed",
        "not required",
        "not applicable",
        "nothing",
    )
    for _, _, report in qa_reports:
        for line in report.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith(f"{marker}:"):
                value = stripped.split(":", 1)[1].strip()
                # Weak models wrap the "not needed" filler in punctuation
                # ("(None immediately required, but...)") that a plain
                # startswith check misses -- strip leading non-alphanumerics
                # before comparing.
                bare = re.sub(r"^[^a-zA-Z0-9]+", "", value).lower()
                # Require enough real content to be an actionable value at
                # all -- weak models sometimes truncate mid-line into
                # near-nothing (e.g. a bare "\"), which isn't a "none
                # needed" filler but is equally useless to act on.
                if len(bare) >= 5 and not bare.startswith(no_op_starts):
                    return value
    return None


class QAAgent(BaseAgent):
    def step_testing(self):
        from orchestrator.core.state import (
            log_header,
            log_success,
            log_warning,
            log_info,
        )

        log_header("5. TESTING & VERIFICATION (QA Agent)")
        test_cmd = self.orchestrator.config.get("test_command", "git diff --stat")
        log_info(f"Running test command: {test_cmd}")

        code, output = self.orchestrator.run_command(
            ["bash", "-c", test_cmd], timeout=600, capture=True
        )

        with open(self.orchestrator.test_results_path, "w", encoding="utf-8") as f:
            f.write(f"Command: {test_cmd}\nExit Code: {code}\nOutput:\n{output}")

        log_info(f"Test exit code: {code}")

        if code in (126, 127):
            log_warning(
                f"[!] Test command could not run (exit {code}): environment problem, not a code failure. Pausing for human review."
            )
            self.orchestrator.pause_for_human_review(
                "QA",
                f"Test command failed to launch (exit {code}), so no tests were executed.\nCommand: {test_cmd}\nOutput:\n{output}\nFix test_command in config.json, then approve to re-run TESTING.",
                "TESTING",
                "TESTING",
            )
            return

        agent_context = json.dumps(
            self.orchestrator.read_agent_context(), ensure_ascii=False
        )

        if self.orchestrator.has_git:
            _, git_diff = self.orchestrator.run_command(
                ["git", "diff", self.orchestrator.base_branch], capture=True
            )
            if not git_diff.strip():
                _, git_diff = self.orchestrator.run_command(
                    ["git", "diff"], capture=True
                )
        else:
            git_diff = "No git repository, changes are directly in workspace."

        tasks = self.orchestrator.state.get("tasks", [])
        qa_workers, qa_assignments = self.orchestrator.allocate_workers("qa", tasks)
        qa_reports = []
        for worker_id, level in qa_workers:
            assigned_tasks = [
                {
                    key: task.get(key)
                    for key in (
                        "id",
                        "description",
                        "complexity",
                        "rd_level",
                        "qa_level",
                    )
                }
                for task in tasks
                if qa_assignments.get(task["id"]) == worker_id
            ]
            if not assigned_tasks:
                continue
            qa_prompt = f"""Analyze the test execution results for your assigned changes.\n\nMachine Context:\n{agent_context}\n\nAssigned Tasks:\n{json.dumps(assigned_tasks, ensure_ascii=False)}\n\nGit Diff:\n{git_diff}\n\nRaw Test Output:\n{output}\nTest Exit Code: {code}\n\nGenerate a detailed QA report in Markdown. Its first non-empty line MUST be exactly `QA_STATUS: PASSED` or `QA_STATUS: FAILED`. Use PASSED only when tests pass and the implementation is correct and safe; otherwise use FAILED and list fixes.\n\nMandatory check every time: does this raw test output hide any diagnostic information you would need (e.g. skipped tests with no visible reason, silenced warnings, truncated tracebacks)? If the test command itself is inadequate to verify this project, you MUST add a line `TEST_COMMAND_SUGGESTION: <exact recommended test_command and why>` -- do not silently accept an opaque test command.\n\nAlso mandatory: if any failure or skip is caused by a missing OS-level/environment dependency (an interpreter, package, browser binary, service, etc. that must be installed on the machine, not something a code change can fix), you MUST add a line `DEPENDENCY_INSTALL_NEEDED: <exact shell command(s) a human should run>`. Do not mark this PASSED and do not describe it as something the Developer should work around in code -- a human has to install it."""
            system_prompt = (
                "You are a Senior Quality Assurance Engineer. Review complex/design/high-risk tasks and their regressions."
                if level == "senior"
                else "You are a Middle Quality Assurance Engineer. Review moderate feature work and integration regressions."
                if level == "middle"
                else "You are a Junior Quality Assurance Engineer. Verify routine task behavior and obvious regressions; flag design risks for senior QA."
            )
            qa_reports.append(
                (
                    worker_id,
                    level,
                    self.call_agent(f"qa_{level}", qa_prompt, system_prompt),
                )
            )
        qa_report = "\n\n".join(
            f"## {worker_id} ({level})\n{report}"
            for worker_id, level, report in qa_reports
        )
        if qa_reports:
            self.orchestrator.state["last_qa_level"] = max(
                (level for _, level, _ in qa_reports),
                key=("junior", "middle", "senior").index,
            )

        with open(self.orchestrator.qa_report_path, "w", encoding="utf-8") as f:
            f.write(qa_report)
        log_success(
            f"QA report generated and saved to {self.orchestrator.qa_report_path}"
        )

        test_command_suggestion = _extract_marker(qa_reports, "TEST_COMMAND_SUGGESTION")
        if test_command_suggestion:
            self.orchestrator.state["test_command_suggestion"] = test_command_suggestion
            log_warning(
                f"[!] QA suggests updating test_command: {test_command_suggestion}"
            )
        else:
            self.orchestrator.state.pop("test_command_suggestion", None)

        dependency_install_needed = _extract_marker(
            qa_reports, "DEPENDENCY_INSTALL_NEEDED"
        )
        if dependency_install_needed:
            log_warning(
                f"[!] QA reports a missing environment dependency, not a code issue. Pausing for human review: {dependency_install_needed}"
            )
            self.orchestrator.pause_for_human_review(
                "QA",
                f"QA reports that test failures/skips are caused by a missing environment dependency, not a code bug. A human needs to install it, then approve to re-run TESTING.\n\nRun this:\n```bash\n{dependency_install_needed}\n```\n\nQA report:\n{qa_report[:2000]}",
                "TESTING",
                "TESTING",
            )
            return

        def passed(report):
            lines = [line.strip() for line in report.splitlines() if line.strip()]
            status = next(
                (
                    line
                    for line in lines[:3] + lines[-3:]
                    if re.fullmatch(
                        r"QA_STATUS:\s*(PASSED|FAILED)", line, re.IGNORECASE
                    )
                ),
                "",
            )
            return status.upper() == "QA_STATUS: PASSED"

        qa_weights = self.orchestrator.config.get("worker_weights", {}).get("qa", {})
        weighted_votes = sum(
            float(qa_weights.get(level, 1.0)) * (1 if passed(report) else -1)
            for _, level, report in qa_reports
        )
        total_weight = sum(
            float(qa_weights.get(level, 1.0)) for _, level, _ in qa_reports
        )
        vote_ratio = weighted_votes / total_weight if total_weight else 0.0
        vote_threshold = self.orchestrator.config.get("qa_vote_threshold", 0.6)
        is_passed = code == 0 and bool(qa_reports) and vote_ratio >= vote_threshold

        if is_passed:
            log_success("QA verification PASSED!")
            self.orchestrator.state["state"] = "REVIEWING_CODE"
            self.orchestrator.state.pop("qa_dissenting_levels", None)
            self.orchestrator.save_state()
        else:
            log_warning("QA verification FAILED!")
            self.orchestrator.state["qa_dissenting_levels"] = sorted(
                {level for _, level, report in qa_reports if not passed(report)}
            )
            brain.remember(
                self.orchestrator,
                "lesson",
                f"QA verification failed (exit code {code}). Test output:\n{output[:500]}",
            )
            self.orchestrator.escalate_developer_backend()
            for rd_level in {
                t.get("rd_level") for t in tasks if t.get("status") == "completed"
            } - {None}:
                rd_role = f"developer_{rd_level}"
                rd_backend = self.orchestrator.get_backend(rd_role)
                rd_model = self.orchestrator.get_active_model_for_role(
                    rd_role, rd_backend
                )
                scoring.record_event(
                    self.orchestrator, "rd", rd_level, rd_backend, rd_model, -0.5
                )
            max_rev = self.orchestrator.config.get("max_revisions", 2)
            if self.orchestrator.state["code_revisions"] < max_rev:
                self.orchestrator.state["code_revisions"] += 1
                self.orchestrator.state["state"] = "IMPLEMENTING"
                fix_task_id = f"FIX-QA-{self.orchestrator.state['code_revisions']}"
                self.orchestrator.state["tasks"].append(
                    {
                        "id": fix_task_id,
                        "description": f"Fix QA verification issues.\nTest exit code: {code}\nTest output:\n{output[:2000]}\nQA feedback:\n{qa_report[:2000]}",
                        "target_files": self.orchestrator.relevant_target_files(
                            f"{output}\n{qa_report}"
                        ),
                        "output_contract": {
                            "format": "file_blocks",
                            "response_must_start_with": "[FILE_START:",
                            "allow_prose": False,
                        },
                        "status": "pending",
                        **self.orchestrator.fix_task_levels(),
                    }
                )
                self.orchestrator.save_state()
                log_info(
                    f"Revising code based on QA report (Revision {self.orchestrator.state['code_revisions']}/{max_rev})..."
                )
            else:
                self.orchestrator.escalate_to_replan_or_pause(
                    "QA", qa_report, "IMPLEMENTING", "REVIEWING_CODE"
                )
