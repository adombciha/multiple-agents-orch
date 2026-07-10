from __future__ import annotations
from orchestrator.roles.base_agent import BaseAgent

class AssistantAgent(BaseAgent):
    def generate_changelog(self, summary: str, diff_patch: str) -> str:
        changelog_prompt = f"""Please generate a CHANGELOG entry for the following completed task.\n\nSummary:\n{summary}\n\nDiff:\n{diff_patch[:5000]}"""
        changelog_system = "You are the project Assistant. You write concise, professional markdown CHANGELOG entries."
        return self.call_agent("assistant", changelog_prompt, changelog_system)
