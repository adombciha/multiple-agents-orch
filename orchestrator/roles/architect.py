from __future__ import annotations
import json
from orchestrator.roles.base_agent import BaseAgent

ARCHITECT_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["APPROVED", "REJECTED"]},
        "feedback": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "feedback"],
    "additionalProperties": False,
}

def is_architect_review(response: str) -> bool:
    try:
        data = json.loads(response.strip())
        return data.get("status") in {"APPROVED", "REJECTED"} and isinstance(data.get("feedback"), list)
    except (AttributeError, json.JSONDecodeError):
        return False

def render_architect_review(data: dict) -> str:
    feedback = "\n".join(f"- {item}" for item in data["feedback"]) or "- No issues found."
    return f"PLAN_STATUS: {data['status']}\n\n{feedback}\n"

class ArchitectAgent(BaseAgent):
    def step_reviewing_plan(self):
        from orchestrator.core.state import log_header, log_success, log_warning, log_info

        log_header("3. REVIEWING PLAN (Architect)")
        with open(self.orchestrator.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.orchestrator.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()
        specialist_notes = self.orchestrator.consult_specialists(requirements, plan)

        prompt = f"""Review the implementation plan against the requirements.\n\nRequirements:\n{requirements}\n\nImplementation Plan:\n{plan}\n\nSpecialist Reviews:\n{specialist_notes or 'None selected for this project.'}\n\nCheck for architectural issues, requirement gaps, scope violations, and safety. Return the verdict and concise feedback using the supplied JSON schema."""

        review_json = self.call_agent(
            "architect", prompt,
            "You are a Senior Software Architect. Return only JSON with status (APPROVED or REJECTED) and a feedback string array.",
            response_validator=is_architect_review, response_schema=ARCHITECT_REVIEW_SCHEMA,
        )
        review_data = json.loads(review_json)
        review = render_architect_review(review_data)
        self.orchestrator.reviewer_output_json_path.write_text(
            json.dumps(review_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        with open(self.orchestrator.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)
        log_info(f"Architect response saved. Preview:\n{review[:200]}...")

        is_approved = review_data["status"] == "APPROVED"

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
