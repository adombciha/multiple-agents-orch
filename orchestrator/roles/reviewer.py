from __future__ import annotations
import json
from orchestrator.roles.base_agent import BaseAgent

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
        return data.get("status") in {"APPROVED", "REJECTED"} and isinstance(data.get("feedback"), list)
    except (AttributeError, json.JSONDecodeError):
        return False

def render_code_review(data: dict) -> str:
    feedback = "\n".join(f"- {item}" for item in data["feedback"]) or "- No issues found."
    return f"{data['status']}\n\n{feedback}\n"

class ReviewerAgent(BaseAgent):
    def step_reviewing_code(self):
        from orchestrator.core.state import log_header, log_success, log_warning, log_info

        log_header("6. REVIEWING CODE (Architect / Reviewer)")
        agent_context = json.dumps(self.orchestrator.read_agent_context(), ensure_ascii=False)
        requirements = self.orchestrator.requirements_path.read_text(encoding="utf-8")
        plan = self.orchestrator.plan_path.read_text(encoding="utf-8")
        with open(self.orchestrator.test_results_path, "r", encoding="utf-8") as f:
            test_results = f.read()

        # Get git diff
        if self.orchestrator.has_git:
            _, git_diff = self.orchestrator.run_command(["git", "diff", self.orchestrator.base_branch], capture=True)
            if not git_diff.strip():
                _, git_diff = self.orchestrator.run_command(["git", "diff"], capture=True)
            _, git_status = self.orchestrator.run_command(["git", "status", "--short"], capture=True)
            status_lines = git_status.partition("stdout:\n")[2].partition("\nstderr:")[0].splitlines()
            untracked_files = [line[3:] for line in status_lines if line.startswith("?? ")]
            untracked_diffs = []
            for path in untracked_files:
                _, diff = self.orchestrator.run_command(["git", "diff", "--no-index", "--", "/dev/null", path], capture=True)
                untracked_diffs.append(diff)
            git_evidence = f"Git Diff:\n{git_diff}\n\nGit Status (includes untracked files):\n{git_status}\n\nUntracked File Diffs:\n{'\n'.join(untracked_diffs) or 'None'}"
        else:
            git_evidence = "No git repository, changes are directly in workspace."

        specialist_notes = self.orchestrator.consult_specialists(
            requirements,
            plan,
            context=git_evidence,
            roles={"security", "ra", "sre", "devops", "uiux", "uiux_visual_review", "fae", "integration"},
        )

        prompt = f"""Review the code changes made.\n\nMachine Context:\n{agent_context}\n\nTest Results:\n{test_results}\n\nSpecialist Reviews:\n{specialist_notes or 'None selected for this project.'}\n\n{git_evidence}\n\nVerify that the implementation matches assigned tasks, tests pass, and no bugs, scope violations, or safety issues remain. Return the verdict and concise feedback using the supplied JSON schema."""

        review_json = self.call_agent(
            "reviewer", prompt,
            "You are a Senior Code Reviewer. Return only JSON with status (APPROVED or REJECTED) and a feedback string array.",
            response_validator=is_code_review, response_schema=REVIEW_SCHEMA,
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

        if is_approved:
            log_success("Code changes APPROVED by Reviewer!")
            self.orchestrator.state["state"] = "COMPLETED"
            self.orchestrator.save_state()
        else:
            log_warning("Code changes REJECTED by Reviewer.")
            self.orchestrator.escalate_developer_backend()
            max_rev = self.orchestrator.config.get("max_revisions", 2)
            if self.orchestrator.state["code_revisions"] < max_rev:
                self.orchestrator.state["code_revisions"] += 1
                self.orchestrator.state["state"] = "IMPLEMENTING"
                fix_task_id = f"FIX-REV-{self.orchestrator.state['code_revisions']}"
                self.orchestrator.state["tasks"].append({
                    "id": fix_task_id,
                    "description": f"Fix Code Review Issues. Feedback from Reviewer:\n{review[:2000]}",
                    "status": "pending",
                    **self.orchestrator.fix_task_levels(),
                })
                self.orchestrator.save_state()
                log_info(f"Revising implementation (Revision {self.orchestrator.state['code_revisions']}/{max_rev})...")
            else:
                log_warning("Reached max code review revisions. Pausing for human review.")
                self.orchestrator.pause_for_human_review("Reviewer", review, "IMPLEMENTING", "COMPLETED")
