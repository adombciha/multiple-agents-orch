from __future__ import annotations
import sys
import json
from orchestrator.roles.base_agent import BaseAgent, extract_json_response, is_json_response

JSON_RULES = "Output only one valid JSON object. Do not use Markdown fences, comments, explanation, headings, or prose. The first character must be { and the last character must be }."
REQUIREMENT_HEADINGS = (
    "Project Overview & Context",
    "Specific Functional Requirements",
    "Technical Specifications & Stack constraints",
    "Scope boundaries",
)
REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "project_overview": {"type": "string"},
        "functional_requirements": {"type": "array", "items": {"type": "string"}},
        "technical_specifications": {"type": "array", "items": {"type": "string"}},
        "scope_boundaries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["project_overview", "functional_requirements", "technical_specifications", "scope_boundaries"],
    "additionalProperties": False,
}
SALES_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {"use_sales": {"type": "boolean"}},
    "required": ["use_sales"],
    "additionalProperties": False,
}
RESEARCH_TRACKS_SCHEMA = {
    "type": "object",
    "properties": {
        "tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": ["sales", "ra"]},
                    "focus": {"type": "string"},
                },
                "required": ["role", "focus"],
                "additionalProperties": False,
            },
            "maxItems": 3,
        }
    },
    "required": ["tracks"],
    "additionalProperties": False,
}

def is_requirements_json_response(response: str) -> bool:
    try:
        data = json.loads(extract_json_response(response))
        return all(key in data for key in REQUIREMENTS_SCHEMA["required"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False

def render_requirements(data: dict) -> str:
    def render(value):
        if isinstance(value, list):
            return "\n".join(f"- {item}" for item in value)
        return str(value)

    return (
        "# Requirements\n\n"
        f"## {REQUIREMENT_HEADINGS[0]}\n{render(data['project_overview'])}\n\n"
        f"## {REQUIREMENT_HEADINGS[1]}\n{render(data['functional_requirements'])}\n\n"
        f"## {REQUIREMENT_HEADINGS[2]}\n{render(data['technical_specifications'])}\n\n"
        f"## {REQUIREMENT_HEADINGS[3]}\n{render(data['scope_boundaries'])}\n"
    )

class ManagerAgent(BaseAgent):
    def step_planning(self):
        from orchestrator.core.state import log_header, log_error, log_success, log_info

        log_header("1. PLANNING (Ollama Manager)")

        if self.orchestrator.state.get("workflow_mode") != "research":
            self.orchestrator.setup_worktree()

        if not self.orchestrator.request_path.exists():
            log_error(f"No request file found at {self.orchestrator.request_path}. Please run 'start' command first.")
            sys.exit(1)

        with open(self.orchestrator.request_path, "r", encoding="utf-8") as f:
            request = f.read()

        sales_selection = self.call_manager(
            f"""Decide whether this request needs a Sales specialist before requirements are written:\n\n{request}\n\nRespond only with {{"use_sales": true}} when business scope, users, value, or acceptance criteria are unclear; otherwise respond with {{"use_sales": false}}.""",
            f"You are a Project Manager. {JSON_RULES}",
            is_json_response,
            SALES_SELECTION_SCHEMA,
        )
        try:
            use_sales = bool(json.loads(extract_json_response(sales_selection)).get("use_sales"))
        except (json.JSONDecodeError, AttributeError):
            use_sales = False
        sales_notes = ""
        if use_sales:
            sales_notes = self.call_agent(
                "sales",
                f"""Clarify the business scope for this request:\n\n{request}\n\nList users, desired outcome, scope boundaries, and acceptance criteria.""",
                "You are the project's Sales specialist.",
            )

        system_prompt = """You are the Project Manager of an AI software company. Convert the request into a structured requirements object. Output only one valid JSON object matching the supplied schema. Do not output Markdown, code fences, explanations, progress messages, or prose."""
        requirements_prompt = f"{request}\n\nSales Notes:\n{sales_notes or 'No sales consultation required.'}"
        requirements_json = self.call_manager(
            requirements_prompt,
            system_prompt,
            response_validator=is_requirements_json_response,
            response_schema=REQUIREMENTS_SCHEMA,
        )
        requirements_data = json.loads(extract_json_response(requirements_json))
        self.orchestrator.requirements_json_path.write_text(
            json.dumps(requirements_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        requirements = render_requirements(requirements_data)

        # Save requirements
        with open(self.orchestrator.requirements_path, "w", encoding="utf-8") as f:
            f.write(requirements)

        log_success(f"Requirements generated and saved to {self.orchestrator.requirements_path}")
        if self.orchestrator.state.get("workflow_mode") == "research":
            tracks = self.call_manager(
                f"""Split this research request into 1-3 independent Sales or RA research tracks.\n\n{request}\n\nRespond only with JSON: {{\"tracks\":[{{\"role\":\"sales\" or \"ra\",\"focus\":\"specific research question\"}}]}}.""",
                f"You are a research program manager. {JSON_RULES}",
                is_json_response,
                RESEARCH_TRACKS_SCHEMA,
            )
            try:
                selected = json.loads(extract_json_response(tracks)).get("tracks", [])
                self.orchestrator.state["specialists"] = [{"role": item["role"], "reason": item["focus"]} for item in selected if item.get("role") in {"sales", "ra"}][:3]
            except (json.JSONDecodeError, KeyError, TypeError):
                self.orchestrator.state["specialists"] = []
            if not self.orchestrator.state["specialists"]:
                self.orchestrator.state["specialists"] = [{"role": role, "reason": "Research-only workflow"} for role in self.orchestrator.state["research_roles"]]
            self.orchestrator.state["state"] = "RESEARCHING"
        else:
            self.orchestrator.state["state"] = "DEVELOPING_PLAN"
        self.orchestrator.save_state()

    def step_completed(self):
        from orchestrator.core.state import log_header, log_success, log_info

        log_header("7. GENERATING SUMMARY (Ollama Manager)")

        if self.orchestrator.state.get("workflow_mode") == "research":
            log_success("Research-only workflow finished.")
            return

        with open(self.orchestrator.request_path, "r", encoding="utf-8") as f:
            request = f.read()
        with open(self.orchestrator.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()

        # Get final git diff stat
        wt_path = self.orchestrator.ai_dir / "worktree"
        if self.orchestrator.config.get("use_worktree", True) and wt_path.exists() and self.orchestrator.has_git:
            _, diff_stat = self.orchestrator.run_command(["git", "diff", "--stat", self.orchestrator.base_branch], cwd=wt_path)
            _, diff_patch = self.orchestrator.run_command(["git", "diff", self.orchestrator.base_branch], cwd=wt_path)
        elif self.orchestrator.has_git:
            _, diff_stat = self.orchestrator.run_command(["git", "diff", "--stat", self.orchestrator.base_branch])
            _, diff_patch = self.orchestrator.run_command(["git", "diff", self.orchestrator.base_branch])
        else:
            diff_stat = "No git repository."
            diff_patch = "No git repository."

        prompt = f"""We have successfully completed the tasks.\nOriginal Request:\n{request}\n\nRequirements:\n{requirements}\n\nGit Diff Stat:\n{diff_stat}\n\nWrite in the same language as the original request. Use exactly these Markdown sections:\n## Outcome\n## Changes Delivered\n## Verification\n## Scope and Follow-ups\nBe specific: name changed files, cite available test evidence, and distinguish completed work from follow-ups."""

        system_prompt = "You are a Project Manager. Write a beautiful project final report."
        summary = self.call_manager(prompt, system_prompt)

        with open(self.orchestrator.final_report_path, "w", encoding="utf-8") as f:
            f.write(summary)

        log_success(f"Final project report generated at {self.orchestrator.final_report_path}")

        qa_report = self.orchestrator.qa_report_path.read_text(encoding="utf-8") if self.orchestrator.qa_report_path.exists() else "No QA report available."
        review = self.orchestrator.reviewer_output_path.read_text(encoding="utf-8") if self.orchestrator.reviewer_output_path.exists() else "No code review available."
        meeting_memory = self.orchestrator.assistant.generate_meeting_memory(
            request, summary, self.orchestrator.state.get("tasks", []), qa_report, review, diff_stat,
        )
        self.orchestrator.meeting_memory_path.write_text(meeting_memory, encoding="utf-8")
        log_success(f"Meeting memory saved at {self.orchestrator.meeting_memory_path}")

        # Assistant generates CHANGELOG
        log_info("Asking Assistant to generate CHANGELOG.md...")
        changelog = self.orchestrator.assistant.generate_changelog(summary, diff_patch)

        with open(self.orchestrator.workspace / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write("\n\n" + changelog)

        log_success("CHANGELOG.md updated successfully!")

        # Merge and clean up isolated worktree
        self.orchestrator.cleanup_worktree(merge=True)

        log_success("Multi-agent workflow process has finished successfully!")
