from __future__ import annotations
import json
from orchestrator.roles.base_agent import BaseAgent
from orchestrator.core import scoring

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["APPROVED", "REJECTED"]},
        "feedback": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "feedback"],
    "additionalProperties": False,
}


def is_code_review(response: str) -> bool:
    try:
        data = json.loads(response.strip())
        return data.get("status") in {"APPROVED", "REJECTED"} and isinstance(
            data.get("feedback"), list
        )
    except (AttributeError, json.JSONDecodeError):
        return False


def render_code_review(data: dict) -> str:
    feedback = (
        "\n".join(f"- {item}" for item in data["feedback"]) or "- No issues found."
    )
    return f"{data['status']}\n\n{feedback}\n"


class ReviewerAgent(BaseAgent):

    def step_reviewing_code(self):
        from orchestrator.core.state import (
            log_header,
            log_success,
            log_warning,
            log_info,
        )

        log_header("6. REVIEWING CODE (Architect / Reviewer)")
        agent_context = json.dumps(
            self.orchestrator.read_agent_context(), ensure_ascii=False
        )
        requirements = self.orchestrator.requirements_path.read_text(encoding="utf-8")
        plan = self.orchestrator.plan_path.read_text(encoding="utf-8")
        with open(self.orchestrator.test_results_path, "r", encoding="utf-8") as f:
            test_results = f.read()
        qa_report = (
            self.orchestrator.qa_report_path.read_text(encoding="utf-8")
            if self.orchestrator.qa_report_path.exists()
            else ""
        )

        # Separate task changes from commits added to the target branch later.
        if self.orchestrator.has_git:
            try:
                workspace = self.orchestrator.agent_workspace()
                baseline = self.orchestrator._checked_git_output(
                    ["merge-base", "HEAD", self.orchestrator.base_branch], workspace
                ).strip()
                if not baseline:
                    raise RuntimeError("Git returned an empty shared baseline")
                git_diff = self.orchestrator._checked_git_output(
                    ["diff", baseline], workspace
                )
                target_drift = self.orchestrator._checked_git_output(
                    ["diff", "--stat", baseline, self.orchestrator.base_branch],
                    workspace,
                )
                git_status = self.orchestrator._checked_git_output(
                    ["status", "--short", "--untracked-files=all"], workspace
                )
                untracked_files = [
                    line[3:]
                    for line in git_status.splitlines()
                    if line.startswith("?? ")
                ]
                untracked_diffs = []
                for path in untracked_files:
                    code, diff = self.orchestrator.run_command(
                        ["git", "diff", "--no-index", "--", "/dev/null", path],
                        capture=True,
                    )
                    if code not in {0, 1}:
                        raise RuntimeError(
                            f"Cannot read untracked diff for {path}: {diff}"
                        )
                    untracked_diffs.append(self.orchestrator._git_stdout(diff))
            except RuntimeError as error:
                self.orchestrator.pause_for_human_review(
                    "Orchestrator",
                    f"Git review evidence failed: {error}",
                    "REVIEWING_CODE",
                    "REVIEWING_CODE",
                )
                return
            git_evidence = (
                f"Shared Git baseline: {baseline}\n\nTask Git Diff (since shared baseline):\n{git_diff}\n\n"
                f"Git Status (includes untracked files):\n{git_status}\n\n"
                f"Untracked File Diffs:\n{'\n'.join(untracked_diffs) or 'None'}\n\n"
                f"Target branch changes since shared baseline ({self.orchestrator.base_branch}; context only):\n"
                f"{target_drift or 'None'}\n\n"
                "Target branch changes are not task-authored edits or deletions. "
                "Tests ran in the task workspace; they do not verify integration with later target commits. "
                "Git delivery preview and merge checks remain required before delivery."
            )
        else:
            git_evidence = "No git repository, changes are directly in workspace."

        specialist_notes = self.orchestrator.consult_specialists(
            requirements,
            plan,
            context=git_evidence,
            roles={
                "security",
                "ra",
                "sre",
                "devops",
                "uiux",
                "uiux_visual_review",
                "fae",
                "integration",
            },
        )

        prompt = f"""Review the code changes made.\n\nMachine Context:\n{agent_context}\n\nTest Results:\n{test_results}\n\nQA Reports (includes any dissenting per-level votes; a weighted majority may have overridden a minority QA FAILED vote — weigh it yourself):\n{qa_report or "No QA report available."}\n\nSpecialist Reviews:\n{specialist_notes or "None selected for this project."}\n\n{git_evidence}\n\nVerify that the implementation matches assigned tasks, tests pass, and no bugs, scope violations, or safety issues remain. Return the verdict and concise feedback using the supplied JSON schema."""

        review_json = self.call_agent(
            "reviewer",
            prompt,
            "You are a Senior Code Reviewer. Return only JSON with status (APPROVED or REJECTED) and a feedback string array.",
            response_validator=is_code_review,
            response_schema=REVIEW_SCHEMA,
        )
        review_data = json.loads(review_json)
        review = render_code_review(review_data)
        self.orchestrator.reviewer_output_json_path.write_text(
            json.dumps(review_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        with open(self.orchestrator.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)

        log_info(f"Code Review response saved. Preview:\n{review[:200]}...")

        is_approved = review_data["status"] == "APPROVED"
        workflow_mode = self.orchestrator.state.get("workflow_mode")

        if is_approved:
            log_success("Code changes APPROVED by Reviewer!")
            requires_delivery = (
                self.orchestrator.config.get("require_delivery_confirmation", True)
                and self.orchestrator.state.get("workflow_mode") != "research"
            )
            self.orchestrator.state["state"] = (
                "WAITING_FOR_DELIVERY" if requires_delivery else "COMPLETED"
            )
            if requires_delivery:
                self.orchestrator.state["delivery"] = {
                    "delivered": False,
                    "report_done": False,
                }
            self.orchestrator.save_state()
        else:
            log_warning("Code changes REJECTED by Reviewer.")
            self.orchestrator.escalate_developer_backend()
            rd_role = self.orchestrator.state.get(
                "last_developer_role", "developer_senior"
            )
            rd_level = rd_role.rsplit("_", 1)[-1]
            rd_backend = self.orchestrator.get_backend(rd_role)
            rd_model = self.orchestrator.get_active_model_for_role(rd_role, rd_backend)
            scoring.record_event(
                self.orchestrator, "rd", rd_level, rd_backend, rd_model, -0.5
            )
            max_rev = self.orchestrator.config.get("max_revisions", 2)
            if self.orchestrator.state["code_revisions"] < max_rev:
                self.orchestrator.state["code_revisions"] += 1
                self.orchestrator.state["state"] = "IMPLEMENTING"
                fix_task_id = f"FIX-REV-{self.orchestrator.state['code_revisions']}"
                self.orchestrator.state["tasks"].append(
                    {
                        "id": fix_task_id,
                        "description": f"Fix Code Review Issues. Feedback from Reviewer:\n{review[:2000]}",
                        "target_files": self.orchestrator.relevant_target_files(review),
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
                    f"Revising implementation (Revision {self.orchestrator.state['code_revisions']}/{max_rev})..."
                )
            else:
                self.orchestrator.escalate_to_replan_or_pause(
                    "Reviewer", review, "IMPLEMENTING", "IMPLEMENTING"
                )
