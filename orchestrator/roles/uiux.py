from orchestrator.roles.base_agent import BaseAgent


class UIUXAgent(BaseAgent):
    role = "uiux"
    system_prompt = "You are a UI/UX specialist. Focus on user flows, accessibility, clear states, and acceptance criteria."

    def review(self, requirements: str, plan: str, context: str = "") -> str:
        return self.call_agent(self.role, f"Review this project for UI/UX risks.\n\nRequirements:\n{requirements}\n\nPlan:\n{plan}\n\nContext:\n{context or 'None'}\n\nReturn user-flow, accessibility, and usability acceptance criteria.", self.system_prompt)
