from orchestrator.roles.base_agent import BaseAgent


class FAEAgent(BaseAgent):
    role = "fae"
    system_prompt = "You are a Field Application Engineer. Focus on customer environments, SDK/device compatibility, validation, and support handoff."

    def review(self, requirements: str, plan: str, context: str = "") -> str:
        return self.call_agent(self.role, f"Review this project for customer-environment and hardware/SDK integration risks.\n\nRequirements:\n{requirements}\n\nPlan:\n{plan}\n\nContext:\n{context or 'None'}\n\nReturn compatibility assumptions, validation steps, and items requiring real-world confirmation.", self.system_prompt)
