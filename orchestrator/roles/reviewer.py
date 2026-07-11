from __future__ import annotations
import json
from orchestrator.roles.base_agent import BaseAgent

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
        else:
            git_diff = "No git repository, changes are directly in workspace."

        specialist_notes = self.orchestrator.consult_specialists(
            requirements,
            plan,
            context=f"Git Diff:\n{git_diff}",
            roles={"security", "ra", "sre", "devops", "uiux", "uiux_visual_review", "fae", "integration"},
        )

        prompt = f"""Review the code changes made. Here is the context:\n\nMachine Context:\n{agent_context}\n\nTest Results:\n{test_results}\n\nSpecialist Reviews:\n{specialist_notes or 'None selected for this project.'}\n\nGit Diff:\n{git_diff}\n\nVerify if the implementation matches the assigned tasks and if the tests pass.\nIf acceptable, start your response with 'APPROVED'.\nIf there are bugs, logic errors, style issues, or failures, start your response with 'REJECTED' followed by detailed feedback.\n\nFormat:\n[APPROVED or REJECTED]\n[Feedback details]"""

        system_prompt = "You are a Senior Code Reviewer. Review the git diff and test results."
        review = self.call_agent("reviewer", prompt, system_prompt)

        with open(self.orchestrator.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)

        log_info(f"Code Review response saved. Preview:\n{review[:200]}...")

        is_approved = review.strip().upper().replace("*", "").startswith("APPROVED")

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
