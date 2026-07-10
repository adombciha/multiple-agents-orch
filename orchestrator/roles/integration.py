from orchestrator.roles.base_agent import BaseAgent


class IntegrationAgent(BaseAgent):
    role = "integration"
    system_prompt = "You are an Integration Engineer. Focus on API contracts, third-party systems, protocols, failure handling, and integration tests."

    def review(self, requirements: str, plan: str, context: str = "") -> str:
        return self.call_agent(self.role, f"Review this project for cross-system integration risks.\n\nRequirements:\n{requirements}\n\nPlan:\n{plan}\n\nContext:\n{context or 'None'}\n\nReturn interface contracts, failure cases, and integration acceptance criteria.", self.system_prompt)
