from __future__ import annotations
import json
import re
from pathlib import Path
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


_MISSING_DEPENDENCY_SIGNALS = re.compile(
    r"modulenotfounderror|no module named|importerror|command not found|"
    r"not installed|not found on path|not found in path|executable not found|"
    r"missing dependency|cannot find (?:the )?(?:executable|binary|module)",
    re.IGNORECASE,
)

QA_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["PASSED", "FAILED"]},
        "summary": {"type": "string", "minLength": 1},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["code", "process", "environment"],
                    },
                    **{
                        key: {"type": "string"}
                        for key in ("file", "requirement", "evidence", "fix")
                    },
                },
                "required": ["kind", "file", "requirement", "evidence", "fix"],
                "additionalProperties": False,
            },
        },
        "dependency_install_needed": {"type": ["string", "null"]},
        "test_command_suggestion": {"type": ["string", "null"]},
    },
    "required": [
        "status",
        "summary",
        "findings",
        "dependency_install_needed",
        "test_command_suggestion",
    ],
    "additionalProperties": False,
}


def render_qa_json(response: str) -> str:
    """Validate structured output before rendering the existing report format."""
    data = json.loads(response)
    if not isinstance(data, dict) or set(data) != set(QA_RESPONSE_SCHEMA["required"]):
        raise ValueError("QA JSON fields do not match the schema")
    if (
        data["status"] not in {"PASSED", "FAILED"}
        or not isinstance(data["summary"], str)
        or not data["summary"].strip()
        or not isinstance(data["findings"], list)
    ):
        raise ValueError("Invalid QA status, summary or findings")
    for item in data["findings"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"kind", "file", "requirement", "evidence", "fix"}
            or not all(isinstance(value, str) for value in item.values())
            or item["kind"] not in {"code", "process", "environment"}
        ):
            raise ValueError("Invalid QA finding")
    for key in ("dependency_install_needed", "test_command_suggestion"):
        if data[key] is not None and (
            not isinstance(data[key], str) or not data[key].strip() or "\n" in data[key]
        ):
            raise ValueError("Invalid QA diagnostic field")
    if data["status"] == "PASSED" and (
        data["findings"] or data["dependency_install_needed"]
    ):
        raise ValueError(
            "PASSED conflicts with blocking findings or missing dependencies"
        )
    report = f"QA_STATUS: {data['status']}\n" + " ".join(data["summary"].splitlines())
    report += "\nQA_FINDINGS: " + json.dumps(data["findings"], ensure_ascii=False)
    for key, marker in (
        ("dependency_install_needed", "DEPENDENCY_INSTALL_NEEDED"),
        ("test_command_suggestion", "TEST_COMMAND_SUGGESTION"),
    ):
        if data[key]:
            report += f"\n{marker}: {data[key]}"
    return report


def qa_verdict(report: str) -> str | None:
    if not isinstance(report, str):
        return None
    lines = [line.strip() for line in report.splitlines() if line.strip()]
    if not lines or lines[0] not in {"QA_STATUS: PASSED", "QA_STATUS: FAILED"}:
        return None
    if any(line.startswith("QA_STATUS:") for line in lines[1:]):
        return None
    if lines[0].endswith("FAILED") and not any(
        line
        and not line.startswith(
            ("DEPENDENCY_INSTALL_NEEDED:", "TEST_COMMAND_SUGGESTION:")
        )
        for line in lines[1:]
    ):
        return None
    return lines[0].split(": ", 1)[1]


def qa_code_findings(
    report: str, targets: list[str], evidence: str, request: str
) -> list[dict]:
    """Only grounded, in-scope code findings may become Developer instructions."""
    lines = [
        line.strip()[len("QA_FINDINGS:") :].strip()
        for line in report.splitlines()
        if line.strip().startswith("QA_FINDINGS:")
    ]
    if len(lines) != 1:
        return []
    try:
        findings = json.loads(lines[0])
    except (TypeError, ValueError):
        return []
    if not isinstance(findings, list):
        return []
    return [
        item
        for item in findings
        if isinstance(item, dict)
        and item.get("kind") == "code"
        and item.get("file") in targets
        and all(
            isinstance(item.get(key), str) and item[key].strip()
            for key in ("evidence", "requirement", "fix")
        )
        and item["evidence"] in evidence
        and item["requirement"] in request
    ]


class QAAgent(BaseAgent):

    def step_testing(self):
        from orchestrator.core.state import (
            log_header,
            log_success,
            log_warning,
            log_info,
        )

        log_header("5. TESTING & VERIFICATION (QA Agent)")
        target_files = list(
            dict.fromkeys(
                path
                for task in self.orchestrator.state.get("tasks", [])
                for path in task.get("target_files", [])
            )
        )
        docs_only = bool(target_files) and all(
            Path(path).suffix.lower() == ".md" for path in target_files
        )
        document_contents = []
        if docs_only:
            test_cmd = "Document-scope Markdown review"
            errors = []
            workspace = self.orchestrator.agent_workspace().resolve()
            for target in target_files:
                path = (workspace / target).resolve()
                if not path.is_relative_to(workspace) or not path.is_file():
                    errors.append(f"Missing or invalid Markdown target: {target}")
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    errors.append(f"Cannot read Markdown target {target}: {exc}")
                    continue
                if not content.strip():
                    errors.append(f"Empty Markdown target: {target}")
                document_contents.append(f"### {target}\n{content}")
            code = 1 if errors else 0
            output = (
                "\n".join(errors)
                if errors
                else f"Read {len(target_files)} Markdown target(s)."
            )
        else:
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
        request_path = (
            self.orchestrator.request_path
            if self.orchestrator.request_path.exists()
            else self.orchestrator.requirements_path
        )
        request = (
            request_path.read_text(encoding="utf-8") if request_path.exists() else ""
        )
        approval = self.orchestrator.implementation_approval_evidence()

        if docs_only:
            git_diff = "\n\n".join(document_contents)
        elif self.orchestrator.has_git:
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
        invalid_workers = []
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
                        "target_files",
                    )
                }
                for task in tasks
                if qa_assignments.get(task["id"]) == worker_id
            ]
            if not assigned_tasks:
                continue
            review_intro = (
                "Review the complete Markdown targets for accuracy, structure, links, and code fences. No project test command was run."
                if docs_only
                else "Analyze the test execution results for your assigned changes."
            )
            evidence_label = "Markdown Target Contents" if docs_only else "Git Diff"
            diagnostic_instruction = (
                ""
                if docs_only
                else "Check whether the actual test output hides relevant diagnostics. If the command needs improvement, put the exact command and reason in test_command_suggestion; otherwise use null."
            )
            qa_prompt = (
                f"{review_intro}\n\nMachine Context:\n{agent_context}\n\nAssigned Tasks:\n{json.dumps(assigned_tasks, ensure_ascii=False)}"
                f"\n\n{evidence_label}:\n{git_diff}\n\nRaw Test Output:\n{output}\nTest Exit Code: {code}\n\n"
                "Assess correctness and safety using the supplied evidence. Use status PASSED only when no blocking defects remain; otherwise use FAILED and list concrete findings. "
                f"{diagnostic_instruction}\n"
                "Only when the raw output establishes a missing environment dependency, put the required human install command in dependency_install_needed; otherwise use null. Environment repairs are not Developer code tasks."
            )
            qa_prompt += (
                f"\n\nOriginal Request (authoritative over derived tasks/reviews):\n{request}"
                f"\nActual Test Command: {test_cmd}\nOwner Approval Evidence: {json.dumps(approval, ensure_ascii=False)}\n"
                "Missing or stale approval evidence means unknown, not proof of unauthorized editing. "
                "Distinguish restrictions on the delivered tool at runtime from isolated test fixtures; do not extend a runtime no-write rule to all tests without explicit request evidence. "
                "Do not demand excluded real-model performance tests or treat unavailable measurements as code defects. "
                "Evaluate the actual command and exit code; do not claim full regression was absent just because no separate narrative says it ran. "
                "For FAILED, populate the findings array. Each code finding must have kind='code', file (an assigned target), "
                "requirement (an exact quote from Original Request), evidence (an exact excerpt from raw output or the supplied diff), and fix (the concrete in-scope code change). "
                "Use kind='process' or 'environment' for non-code issues; never ask RD to encode approval or delivery status into product source. "
                "If tests pass, a code rejection requires grounded code findings; uncertainty alone does not establish a defect."
            )
            system_prompt = (
                "You are a Senior Quality Assurance Engineer. Review complex/design/high-risk tasks and their regressions."
                if level == "senior"
                else "You are a Middle Quality Assurance Engineer. Review moderate feature work and integration regressions."
                if level == "middle"
                else "You are a Junior Quality Assurance Engineer. Verify routine task behavior and obvious regressions; flag design risks for senior QA."
            )

            def valid_report(value):
                verdict = qa_verdict(value)
                return verdict is not None and (
                    verdict != "FAILED"
                    or code != 0
                    or bool(
                        qa_code_findings(
                            value, target_files, f"{output}\n{git_diff}", request
                        )
                    )
                    or bool(
                        _MISSING_DEPENDENCY_SIGNALS.search(output)
                        and _extract_marker(
                            [(worker_id, level, value)], "DEPENDENCY_INSTALL_NEEDED"
                        )
                    )
                )

            def valid_json(value):
                try:
                    rendered = render_qa_json(value)
                    return valid_report(rendered) and not (
                        code != 0 and qa_verdict(rendered) == "PASSED"
                    )
                except (TypeError, ValueError):
                    return False

            qa_prompt += (
                "\nOUTPUT CONTRACT: Return exactly one JSON object matching the following schema; no Markdown, headings or marker lines. "
                "Put code/process/environment findings in findings, install commands in dependency_install_needed, and command suggestions in test_command_suggestion. "
                "PASSED requires an empty findings array and null dependency_install_needed. FAILED needs actual blocking evidence; unsupported concerns are not defects.\n"
                + json.dumps(QA_RESPONSE_SCHEMA)
            )
            system_prompt += " Return only the supplied JSON schema, without Markdown."
            try:
                report = self.call_agent(
                    f"qa_{level}",
                    qa_prompt,
                    system_prompt,
                    response_validator=valid_json,
                    response_schema=QA_RESPONSE_SCHEMA,
                )
            except RuntimeError as error:
                invalid_workers.append(worker_id)
                qa_reports.append(
                    (
                        worker_id,
                        level,
                        f"QA routes exhausted without a valid structured response: {error}",
                    )
                )
                continue
            if report.lstrip().startswith("{"):
                try:
                    report = render_qa_json(report)
                except (TypeError, ValueError):
                    invalid_workers.append(worker_id)
                    qa_reports.append(
                        (worker_id, level, "Invalid QA JSON; no verdict accepted.")
                    )
                    continue

            if not valid_report(report):
                log_warning(
                    f"Invalid QA response from {worker_id}; requesting one correction."
                )
                report = self.call_agent(
                    f"qa_{level}",
                    qa_prompt
                    + "\n\nYour previous response did not satisfy the QA contract. "
                    + f"Previous QA response (untrusted; verify against the original request and actual test evidence):\n{report[:4000]}\n"
                    + "Return only JSON matching the supplied schema. "
                    "For FAILED, cite the observed failure, affected file/test and required fix. "
                    "If tests passed, provide grounded QA_FINDINGS code evidence or correct your unsupported verdict. "
                    "Do not summarize the project or invent defects.",
                    system_prompt,
                    response_validator=valid_json,
                    response_schema=QA_RESPONSE_SCHEMA,
                )
                if report.lstrip().startswith("{"):
                    try:
                        report = render_qa_json(report)
                    except (TypeError, ValueError):
                        report = "Invalid QA JSON; no verdict accepted."
            if not valid_report(report):
                invalid_workers.append(worker_id)
            qa_reports.append((worker_id, level, report))
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

        if invalid_workers or not qa_reports:
            self.orchestrator.state.pop("qa_dissenting_levels", None)
            self.orchestrator.pause_for_human_review(
                "QA_RESPONSE",
                f"QA output contract failed after bounded correction/fallback: {', '.join(invalid_workers) or 'no assigned QA workers'}. "
                f"No code defect was established by these responses. Test exit code: {code}. "
                "Retry TESTING after checking the QA backend.\n\n" + qa_report[:2000],
                "TESTING",
                "TESTING",
            )
            return

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
        if dependency_install_needed and not _MISSING_DEPENDENCY_SIGNALS.search(output):
            # QA can claim a missing dependency from general knowledge of
            # the project (e.g. "playwright is in requirements.txt") even
            # when this run's actual raw output shows nothing of the sort
            # -- observed live once the QA backend switched to a stronger
            # model that produces well-formed but stale/hallucinated
            # suggestions instead of obviously-malformed ones. Only honor
            # the marker when the raw output itself corroborates it.
            log_warning(
                f"[!] QA claimed a missing dependency but this run's raw output shows no such error; ignoring: {dependency_install_needed}"
            )
            dependency_install_needed = None
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
            return qa_verdict(report) == "PASSED"

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
            findings = [
                item
                for _, _, report in qa_reports
                for item in qa_code_findings(
                    report, target_files, f"{output}\n{git_diff}", request
                )
            ]
            repair_feedback = (
                "Grounded code findings:\n" + json.dumps(findings, ensure_ascii=False)
                if findings
                else "Repair the failures shown in the raw test output. QA prose established no additional code defect."
            )
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
                        "description": f"Fix QA verification issues.\nTest exit code: {code}\nTest output:\n{output[:2000]}\n{repair_feedback}",
                        "target_files": self.orchestrator.relevant_target_files(
                            f"{output}\n{repair_feedback}", primary=output
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
                    "QA", repair_feedback, "IMPLEMENTING", "REVIEWING_CODE"
                )
