from __future__ import annotations
from orchestrator.roles.base_agent import BaseAgent

class AssistantAgent(BaseAgent):
    def generate_changelog(self, summary: str, diff_patch: str) -> str:
        changelog_prompt = f"""Please generate a CHANGELOG entry for the following completed task.\n\nSummary:\n{summary}\n\nDiff:\n{diff_patch[:5000]}"""
        changelog_system = "You are the project Assistant. You write concise, professional markdown CHANGELOG entries."
        return self.call_agent("assistant", changelog_prompt, changelog_system)

    def generate_meeting_memory(self, request: str, summary: str, tasks: list[dict], qa_report: str, review: str, diff_stat: str) -> str:
        prompt = f"""Create durable meeting memory for the next human or agent taking over this project. Write in the same language as the original request.

Original Request:
{request}

Final Report:
{summary}

Completed Tasks:
{tasks}

QA Result:
{qa_report}

Code Review Result:
{review}

Changed Files:
{diff_stat}

Use exactly these Markdown sections:
## Outcome
## Decisions and Rationale
## Verification Evidence
## Remaining Risks or Follow-ups
## Next-session Handoff

State only facts supported by the supplied material. Keep it concise but specific."""
        return self.call_agent("assistant", prompt, "You are the project Assistant. Preserve concise, factual project memory for humans.")
