from __future__ import annotations
import sys
import json
from orchestrator.roles.base_agent import (
    BaseAgent,
    extract_json_response,
    is_strict_json_response,
)
from orchestrator.core import scoring, brain

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
    "required": [
        "project_overview",
        "functional_requirements",
        "technical_specifications",
        "scope_boundaries",
    ],
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

WEIGHT_ADJUSTMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "adjustments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": ["rd", "qa"]},
                    "level": {"type": "string", "enum": ["senior", "middle", "junior"]},
                    "weight": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["role", "level", "weight"],
                "additionalProperties": False,
            },
            "maxItems": 6,
        }
    },
    "required": ["adjustments"],
    "additionalProperties": False,
}


def is_weight_adjustment_response(response: str) -> bool:
    try:
        data = json.loads(response.strip())
        return isinstance(data.get("adjustments"), list)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def is_requirements_json_response(response: str) -> bool:
    try:
        data = json.loads(response.strip())
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
            log_error(
                f"No request file found at {self.orchestrator.request_path}. Please run 'start' command first."
            )
            sys.exit(1)

        with open(self.orchestrator.request_path, "r", encoding="utf-8") as f:
            request = f.read()

        sales_selection = self.call_manager(
            f"""Decide whether this request needs a Sales specialist before requirements are written:\n\n{request}\n\nRespond only with {{"use_sales": true}} when business scope, users, value, or acceptance criteria are unclear; otherwise respond with {{"use_sales": false}}.""",
            f"You are a Project Manager. {JSON_RULES}",
            is_strict_json_response,
            SALES_SELECTION_SCHEMA,
        )
        try:
            use_sales = bool(
                json.loads(extract_json_response(sales_selection)).get("use_sales")
            )
        except (json.JSONDecodeError, AttributeError):
            use_sales = False
        sales_notes = ""
        if use_sales:
            sales_notes = self.call_agent(
                "sales",
                f"""Clarify the business scope for this request:\n\n{request}\n\nList users, desired outcome, scope boundaries, and acceptance criteria.""",
                "You are the project's Sales specialist.",
            )

        system_prompt = (
            "You are the Project Manager of an AI software company. "
            "Convert the request into a structured requirements object. "
            "Output only one valid JSON object matching this schema:\n"
            f"{json.dumps(REQUIREMENTS_SCHEMA)}\n"
            "Do not output Markdown, code fences, explanations, progress messages, or prose."
        )
        requirements_prompt = f"{request}\n\nSales Notes:\n{sales_notes or 'No sales consultation required.'}"
        requirements_json = self.call_manager(
            requirements_prompt,
            system_prompt,
            response_validator=is_requirements_json_response,
            response_schema=REQUIREMENTS_SCHEMA,
        )
        requirements_data = json.loads(requirements_json.strip())
        self.orchestrator.requirements_json_path.write_text(
            json.dumps(requirements_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        requirements = render_requirements(requirements_data)

        # Save requirements
        with open(self.orchestrator.requirements_path, "w", encoding="utf-8") as f:
            f.write(requirements)

        log_success(
            f"Requirements generated and saved to {self.orchestrator.requirements_path}"
        )
        if self.orchestrator.state.get("workflow_mode") == "research":
            tracks = self.call_manager(
                f"""Split this research request into 1-3 independent Sales or RA research tracks.\n\n{request}\n\nRespond only with JSON: {{\"tracks\":[{{\"role\":\"sales\" or \"ra\",\"focus\":\"specific research question\"}}]}}.""",
                f"You are a research program manager. {JSON_RULES}",
                is_strict_json_response,
                RESEARCH_TRACKS_SCHEMA,
            )
            try:
                selected = json.loads(extract_json_response(tracks)).get("tracks", [])
                self.orchestrator.state["specialists"] = [
                    {"role": item["role"], "reason": item["focus"]}
                    for item in selected
                    if item.get("role") in {"sales", "ra"}
                ][:3]
            except (json.JSONDecodeError, KeyError, TypeError):
                self.orchestrator.state["specialists"] = []
            if not self.orchestrator.state["specialists"]:
                self.orchestrator.state["specialists"] = [
                    {"role": role, "reason": "Research-only workflow"}
                    for role in self.orchestrator.state["research_roles"]
                ]
            self.orchestrator.state["state"] = "RESEARCHING"
        else:
            self.orchestrator.state["state"] = "DEVELOPING_PLAN"
        self.orchestrator.save_state()

    def _inspect_delivery_state(self) -> tuple[str, str, str]:
        wt_path = self.orchestrator.ai_dir / "worktree"
        target_cwd = (
            wt_path
            if self.orchestrator.config.get("use_worktree", True)
            and wt_path.exists()
            and self.orchestrator.has_git
            else (self.orchestrator.workspace if self.orchestrator.has_git else None)
        )
        if target_cwd is not None:
            _, diff_stat = self.orchestrator.run_command(
                ["git", "diff", "--stat", self.orchestrator.base_branch], cwd=target_cwd
            )
            _, diff_patch = self.orchestrator.run_command(
                ["git", "diff", self.orchestrator.base_branch], cwd=target_cwd
            )
            _, git_status = self.orchestrator.run_command(
                ["git", "status", "--short"], cwd=target_cwd
            )
        else:
            diff_stat = "No git repository."
            diff_patch = "No git repository."
            git_status = ""
        return diff_stat, diff_patch, git_status

    def step_waiting_for_delivery(self):
        from orchestrator.core.state import (
            log_header,
            log_success,
            log_info,
            log_warning,
        )

        log_header("WAITING FOR DELIVERY CONFIRMATION (Manager)")

        if self.orchestrator.state.get("workflow_mode") == "research":
            self.orchestrator.state["state"] = "COMPLETED"
            self.orchestrator.save_state()
            return

        wt_path = self.orchestrator.ai_dir / "worktree"
        has_pending_worktree = (
            self.orchestrator.config.get("use_worktree", True)
            and wt_path.exists()
            and self.orchestrator.has_git
        )
        if (
            self.orchestrator.state.get("changelog_written")
            and self.orchestrator.state.get("delivery_confirmed")
            and not has_pending_worktree
        ):
            self.orchestrator.state["state"] = "COMPLETED"
            for key in (
                "presented_delivery_snapshot",
                "delivery_preview",
                "delivery_preview_presented",
                "delivery_confirmed",
            ):
                self.orchestrator.state.pop(key, None)
            self.orchestrator.save_state()
            log_success("Delivery was already completed; skipping duplicate finalization.")
            return

        diff_stat, diff_patch, _ = self._inspect_delivery_state()
        if not self.orchestrator.state.get("delivery_artifacts_prepared", False):
            with open(self.orchestrator.request_path, "r", encoding="utf-8") as f:
                request = f.read()
            with open(self.orchestrator.requirements_path, "r", encoding="utf-8") as f:
                requirements = f.read()

            prompt = f"""We have successfully completed the tasks.
Original Request:
{request}

Requirements:
{requirements}

Git Diff Stat:
{diff_stat}

Write in the same language as the original request. Use exactly these Markdown sections:
## Outcome
## Changes Delivered
## Verification
## Scope and Follow-ups
Be specific: name changed files, cite available test evidence, and distinguish completed work from follow-ups."""

            summary = self.call_manager(
                prompt,
                "You are a Project Manager. Write a beautiful project final report.",
            )
            from orchestrator.core.telemetry import summary_markdown

            usage = summary_markdown(self.orchestrator)
            if usage:
                summary = f"{summary.rstrip()}\n\n{usage}\n"

            self.orchestrator.final_report_path.write_text(summary, encoding="utf-8")
            log_success(
                f"Final project report generated at {self.orchestrator.final_report_path}"
            )

            qa_report = (
                self.orchestrator.qa_report_path.read_text(encoding="utf-8")
                if self.orchestrator.qa_report_path.exists()
                else "No QA report available."
            )
            review = (
                self.orchestrator.reviewer_output_path.read_text(encoding="utf-8")
                if self.orchestrator.reviewer_output_path.exists()
                else "No code review available."
            )
            meeting_memory = self.orchestrator.assistant.generate_meeting_memory(
                request,
                summary,
                self.orchestrator.state.get("tasks", []),
                qa_report,
                review,
                diff_stat,
            )
            self.orchestrator.meeting_memory_path.write_text(
                meeting_memory, encoding="utf-8"
            )
            log_success(
                f"Meeting memory saved at {self.orchestrator.meeting_memory_path}"
            )
            brain.remember(
                self.orchestrator,
                "meeting",
                f"Request: {request[:500]}\n\n{meeting_memory}",
            )

            if not self.orchestrator.state.get("changelog_written", False):
                log_info("Asking Assistant to generate CHANGELOG.md...")
                changelog = self.orchestrator.assistant.generate_changelog(
                    summary, diff_patch
                ).strip()
                delivery_workspace = (
                    wt_path
                    if self.orchestrator.config.get("use_worktree", True)
                    and wt_path.exists()
                    and self.orchestrator.has_git
                    else self.orchestrator.workspace
                )
                changelog_path = delivery_workspace / "CHANGELOG.md"
                existing = (
                    changelog_path.read_text(encoding="utf-8")
                    if changelog_path.exists()
                    else ""
                )
                if changelog and changelog not in existing:
                    with open(changelog_path, "a", encoding="utf-8") as f:
                        f.write("\n\n" + changelog + "\n")
                self.orchestrator.state["changelog_written"] = True

            self._review_worker_weights(usage)
            self.orchestrator.state["delivery_artifacts_prepared"] = True
            self.orchestrator.save_state()

        create_preview = self.orchestrator.create_delivery_preview

        preview = self.orchestrator.state.get("delivery_preview")
        if not isinstance(preview, dict):
            create_preview("merge")
            log_info("Prepared the persistent delivery snapshot.")
            log_warning(
                "Review the complete Git impact with 'preview' or the GUI before confirming delivery."
            )
            return

        if not self.orchestrator.state.get("delivery_preview_presented", False):
            log_warning(
                "The saved Git impact has not been presented. Review it with 'preview' or the GUI before confirming."
            )
            return

        if not self.orchestrator.state.get("delivery_confirmed", False):
            log_warning(
                "Waiting for explicit delivery confirmation of the presented Git snapshot."
            )
            return

        try:
            self.orchestrator.validate_delivery_preview(preview, "merge")
        except RuntimeError as error:
            log_warning(f"Delivery snapshot is no longer valid: {error}")
            create_preview("merge")
            log_warning(
                "The Git impact changed. Review the refreshed snapshot with 'preview' or the GUI before confirming again."
            )
            return

        log_info("Confirmed delivery snapshot is valid; attempting the scheduled Git merge.")
        if not self.orchestrator.cleanup_worktree(merge=True):
            log_warning(
                "Delivery failed during Git merge. Preserving the feature worktree and queuing repair."
            )
            self.orchestrator.queue_worktree_repair_task()
            return

        self.orchestrator.state["state"] = "COMPLETED"
        for key in (
            "presented_delivery_snapshot",
            "delivery_preview",
            "delivery_preview_presented",
            "delivery_confirmed",
        ):
            self.orchestrator.state.pop(key, None)
        self.orchestrator.save_state()
        log_success("Final artifacts delivered and merged successfully.")

    def step_completed(self):
        from orchestrator.core.state import (
            log_header,
            log_success,
            log_warning,
        )

        log_header("7. COMPLETED (Manager)")

        if self.orchestrator.state.get("workflow_mode") == "research":
            log_success("Research-only workflow finished.")
            return

        wt_path = self.orchestrator.ai_dir / "worktree"
        if (
            self.orchestrator.config.get("use_worktree", True)
            and wt_path.exists()
            and self.orchestrator.has_git
        ):
            log_warning(
                "Found undelivered worktree in COMPLETED state. Returning to WAITING_FOR_DELIVERY."
            )
            self.orchestrator.state["state"] = "WAITING_FOR_DELIVERY"
            self.orchestrator.save_state()
            return

        log_success("Workflow is already completed.")

    def _review_worker_weights(self, telemetry: str) -> None:
        """Manager performance review: propose small, clamped nudges to internal staff
        trust weights based on how this run went. Vendor scorecards are informational
        and out of scope here — vendors are billed per job, not managed by trust."""
        from orchestrator.core.state import log_info, log_warning

        worker_weights = self.orchestrator.config.get("worker_weights", {})
        vendor_scores = self.orchestrator.config.get("vendor_scores", {})
        prompt = (
            "You are closing the books on this run as the team's manager.\n\n"
            f"Current internal staff trust weights (0.2-3.0, higher = more trusted):\n{json.dumps(worker_weights)}\n\n"
            f"Current vendor scorecard (paid backends, informational only, do not touch):\n{json.dumps(vendor_scores)}\n\n"
            f"Run telemetry:\n{telemetry or 'No telemetry recorded.'}\n\n"
            "Propose small performance-review nudges to internal staff weights only "
            "(role 'rd' or 'qa', level 'senior', 'middle', or 'junior'). Keep changes conservative. "
            "Respond only with JSON matching the schema; use an empty 'adjustments' array if no change is warranted."
        )
        try:
            response = self.call_manager(
                prompt,
                f"You are a Project Manager doing a staff performance review. {JSON_RULES}",
                is_weight_adjustment_response,
                WEIGHT_ADJUSTMENT_SCHEMA,
            )
            data = json.loads(extract_json_response(response))
        except Exception as e:
            log_warning(f"Skipping weight review; Manager response was unusable: {e}")
            return

        max_delta = self.orchestrator.config.get("weight_adjustment_max_delta", 0.3)
        changed = False
        for item in data.get("adjustments", []):
            if not isinstance(item, dict):
                continue
            role, level = item.get("role"), item.get("level")
            if role not in {"rd", "qa"} or level not in {"senior", "middle", "junior"}:
                continue
            try:
                proposed = float(item.get("weight"))
            except (TypeError, ValueError):
                continue
            current = scoring.get_weight(self.orchestrator.config, role, level)
            bounded = scoring.clamp(
                scoring.clamp(proposed, current - max_delta, current + max_delta)
            )
            self.orchestrator.config.setdefault("worker_weights", {}).setdefault(
                role, {}
            )[level] = round(bounded, 3)
            changed = True
            log_info(
                f"Manager weight review: {role}/{level} -> {bounded} ({item.get('reason', 'no reason given')})"
            )

        if changed:
            self.orchestrator.config_path.write_text(
                json.dumps(self.orchestrator.config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
