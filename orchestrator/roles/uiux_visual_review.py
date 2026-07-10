from orchestrator.roles.base_agent import BaseAgent


class UIUXVisualReviewAgent(BaseAgent):
    role = "uiux_visual_review"
    system_prompt = "You are a UI/UX visual reviewer. Review supplied screenshot or mockup descriptions for visual hierarchy, accessibility, and state clarity."

    def review(self, requirements: str, plan: str, context: str = "") -> str:
        return self.call_agent(self.role, f"Review the available UI/UX visual context.\n\nRequirements:\n{requirements}\n\nPlan:\n{plan}\n\nVisual Context:\n{context or 'No screenshot supplied.'}\n\nReturn visual usability and accessibility acceptance criteria.", self.system_prompt)
