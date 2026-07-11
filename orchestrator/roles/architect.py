from __future__ import annotations
from orchestrator.roles.base_agent import BaseAgent

class ArchitectAgent(BaseAgent):
    def step_reviewing_plan(self):
        from orchestrator.core.state import log_header, log_success, log_warning, log_info

        log_header("3. REVIEWING PLAN (Architect)")
        with open(self.orchestrator.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.orchestrator.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()
        specialist_notes = self.orchestrator.consult_specialists(requirements, plan)

        prompt = f"""Review the implementation plan against the requirements.\n\nRequirements:\n{requirements}\n\nImplementation Plan:\n{plan}\n\nSpecialist Reviews:\n{specialist_notes or 'None selected for this project.'}\n\nCheck for architectural issues, gaps in requirements, and safety.\n\nThe first non-empty line must be exactly one of:\nPLAN_STATUS: APPROVED\nPLAN_STATUS: REJECTED\nThen provide concise feedback."""

        system_prompt = "You are a Senior Software Architect. Review the implementation plan."
        review = self.call_agent("architect", prompt, system_prompt)

        with open(self.orchestrator.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)
        log_info(f"Architect response saved. Preview:\n{review[:200]}...")

        first_line = next((line.strip().upper().strip("[]*") for line in review.splitlines() if line.strip()), "")
        is_approved = first_line in {"PLAN_STATUS: APPROVED", "APPROVED"}

        if is_approved:
            log_success("Implementation plan APPROVED by Architect!")
            self.orchestrator.state["state"] = "IMPLEMENTING"
            self.orchestrator.save_state()
        else:
            log_warning("Implementation plan REJECTED by Architect.")
            self.orchestrator.escalate_developer_backend()
            max_rev = self.orchestrator.config.get("max_revisions", 2)
            if self.orchestrator.state["plan_revisions"] < max_rev:
                self.orchestrator.state["plan_revisions"] += 1
                self.orchestrator.state["state"] = "DEVELOPING_PLAN"
                self.orchestrator.save_state()
                log_info(f"Revising plan (Revision {self.orchestrator.state['plan_revisions']}/{max_rev})...")
            else:
                log_warning("Reached max plan revisions. Pausing for human review.")
                self.orchestrator.pause_for_human_review("Architect", review, "DEVELOPING_PLAN", "IMPLEMENTING")
