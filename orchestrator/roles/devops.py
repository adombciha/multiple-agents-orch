from orchestrator.roles.base_agent import BaseAgent


class DevOpsAgent(BaseAgent):
    role = "devops"
    system_prompt = "You are a DevOps Engineer. Focus on CI/CD, containers, release gates, rollback, and deployment safety."

    def review(self, requirements: str, plan: str, context: str = "") -> str:
        return self.call_agent(self.role, f"Review this project for CI/CD and deployment risks.\n\nRequirements:\n{requirements}\n\nPlan:\n{plan}\n\nContext:\n{context or 'None'}\n\nReturn risks, pipeline acceptance criteria, and rollback requirements.", self.system_prompt)
