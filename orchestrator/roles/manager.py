from __future__ import annotations
import sys
import json
import hashlib
import math
from copy import deepcopy
from datetime import datetime, timezone
from orchestrator.roles.base_agent import (
    BaseAgent,
    extract_json_response,
    is_strict_json_response,
    missing_request_files,
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
                    "weight": {"type": "number", "minimum": 0.2, "maximum": 3.0},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 32,
                        "uniqueItems": True,
                    },
                },
                "required": ["role", "level", "weight", "reason", "evidence_ids"],
                "additionalProperties": False,
            },
            "maxItems": 6,
        }
    },
    "required": ["adjustments"],
    "additionalProperties": False,
}


def _finite_number(value) -> bool:
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


def _parse_weight_adjustments(response: str) -> list[dict]:
    data = json.loads(response)
    if not isinstance(data, dict) or set(data) != {"adjustments"}:
        raise ValueError("Expected only an adjustments array")
    adjustments = data["adjustments"]
    if not isinstance(adjustments, list) or len(adjustments) > 6:
        raise ValueError("Expected at most six adjustments")
    seen = set()
    for item in adjustments:
        if not isinstance(item, dict) or set(item) != {
            "role",
            "level",
            "weight",
            "reason",
            "evidence_ids",
        }:
            raise ValueError(
                "Each adjustment requires role, level, weight, reason and evidence_ids"
            )
        role, level = item["role"], item["level"]
        if role not in ("rd", "qa") or level not in ("senior", "middle", "junior"):
            raise ValueError("Unknown staff role or level")
        if (role, level) in seen:
            raise ValueError("Duplicate staff role and level")
        seen.add((role, level))
        if not _finite_number(item["weight"]) or not (
            scoring.WEIGHT_MIN <= item["weight"] <= scoring.WEIGHT_MAX
        ):
            raise ValueError("Weight must be a finite number in the staff range")
        if (
            not isinstance(item["reason"], str)
            or not item["reason"].strip()
            or len(item["reason"]) > 2000
        ):
            raise ValueError("A nonempty reason is required")
        evidence_ids = item["evidence_ids"]
        if (
            not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= 32
            or any(not isinstance(value, str) or not value for value in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)
        ):
            raise ValueError("Unique evidence IDs are required")
    return adjustments


def is_weight_adjustment_response(response: str) -> bool:
    try:
        _parse_weight_adjustments(response)
        return True
    except (TypeError, ValueError):
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
            "The original request below is authoritative: preserve its requested deliverables and file paths. "
            "Saved workflow status, human reports and earlier execution errors are diagnostics, not new requirements. "
            "Do not replace the requested implementation with investigation of a previous workflow failure. "
            "Use only the supplied original request and advisory notes; do not inspect the environment, "
            "run commands, use workspace tools or ask for interactive confirmation in this requirements stage. "
            "Output only one valid JSON object matching this schema:\n"
            f"{json.dumps(REQUIREMENTS_SCHEMA)}\n"
            "Do not output Markdown, code fences, explanations, progress messages, or prose."
        )
        requirements_prompt = f"Original request (authoritative):\n{request}\n\nSales Notes (advisory only):\n{sales_notes or 'No sales consultation required.'}"
        requirements_json = self.call_manager(
            requirements_prompt,
            system_prompt,
            response_validator=is_requirements_json_response,
            response_schema=REQUIREMENTS_SCHEMA,
        )
        requirements_data = json.loads(requirements_json.strip())
        missing = missing_request_files(
            request, json.dumps(requirements_data, ensure_ascii=False)
        )
        if missing:
            self.orchestrator.pause_for_human_review(
                "Requirements",
                f"Generated requirements omitted explicit deliverables: {', '.join(missing)}. "
                "The original request is preserved. Retry PLANNING to regenerate requirements; "
                "do not approve an implementation plan based on this response.\n\n"
                + requirements_json[:2000],
                "PLANNING",
                "PLANNING",
            )
            return
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
            DeliveryRepairQueued,
            log_header,
            log_success,
            log_info,
            log_warning,
            log_error,
        )

        log_header("WAITING FOR DELIVERY CONFIRMATION (Manager)")
        self._restore_weight_review_report()

        if self.orchestrator.state.get("workflow_mode") == "research":
            self.orchestrator.state["state"] = "COMPLETED"
            self.orchestrator.save_state()
            return

        operation = self.orchestrator.delivery_operation()
        wt_path = self.orchestrator.ai_dir / "worktree"
        diff_stat, diff_patch, git_status = self._inspect_delivery_state()
        artifacts_prepared_now = False
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

Git Status:
{git_status}

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
            artifacts_prepared_now = True
            self.orchestrator.save_state()

        if artifacts_prepared_now and isinstance(
            self.orchestrator.state.get("delivery_preview"), dict
        ):
            for key in (
                "presented_delivery_snapshot",
                "delivery_preview",
                "delivery_preview_presented",
                "delivery_confirmed",
            ):
                self.orchestrator.state.pop(key, None)
            self.orchestrator.save_state()

        create_preview = self.orchestrator.create_delivery_preview

        preview = self.orchestrator.state.get("delivery_preview")
        if not self.orchestrator.state.get("delivery_preview_presented", False):
            create_preview(operation)
            preview = self.orchestrator.state.get("delivery_preview")
            log_info("Prepared the persistent delivery snapshot.")
            log_warning(
                "Review the complete Git impact with 'preview' or the GUI before confirming delivery."
            )
            return

        if not isinstance(preview, dict):
            log_warning(
                "The saved Git impact is unavailable. Review it with 'preview' or the GUI before confirming."
            )
            return

        if not self.orchestrator.state.get("delivery_confirmed", False):
            log_warning(
                "Waiting for explicit delivery confirmation of the presented Git snapshot."
            )
            return

        try:
            self.orchestrator.validate_delivery_preview(preview, operation)
        except DeliveryRepairQueued:
            raise
        except RuntimeError as error:
            log_warning(f"Delivery snapshot is no longer valid: {error}")
            create_preview(operation)
            log_warning(
                "The Git impact changed. Review the refreshed snapshot with 'preview' or the GUI before confirming again."
            )
            return

        log_info(
            "Confirmed delivery snapshot is valid; attempting the scheduled Git merge."
        )
        if not self.orchestrator.cleanup_worktree(merge=operation == "merge"):
            self._handle_delivery_failure()
            return

        delivery = self.orchestrator.state.setdefault("delivery", {})
        delivery["delivered"] = True
        delivery["delivered_at"] = datetime.now(timezone.utc).isoformat()
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

    def _handle_delivery_failure(self):
        from orchestrator.core.state import log_warning

        for key in (
            "presented_delivery_snapshot",
            "delivery_preview",
            "delivery_preview_presented",
            "delivery_confirmed",
        ):
            self.orchestrator.state.pop(key, None)
        stage = self.orchestrator.state.get("worktree_failure_stage")
        error = self.orchestrator.state.get(
            "worktree_merge_error", "Git delivery failed before completion."
        )
        if stage == "merge" and self.orchestrator.state.get("worktree_conflict_files"):
            log_warning(
                "Git merge conflict; preserving the worktree and queuing scoped repair."
            )
            self.orchestrator.queue_worktree_repair_task()
        elif (
            stage == "commit"
            and self.orchestrator._checked_git_output(
                ["diff", "--name-only"], self.orchestrator._delivery_workspace()
            ).strip()
        ):
            self.orchestrator.state["state"] = "WAITING_FOR_DELIVERY"
            self.orchestrator.create_delivery_preview(
                self.orchestrator.delivery_operation()
            )
            log_warning(
                "Commit hook changed files. Review the refreshed Git impact and confirm again; no RD repair was queued."
            )
        else:
            self.orchestrator.pause_for_human_review(
                "Orchestrator",
                f"Git delivery failed at {stage or 'setup/cleanup'}; worktree preserved.\n{error}",
                "WAITING_FOR_DELIVERY",
                "WAITING_FOR_DELIVERY",
            )

    def step_completed(self):
        from orchestrator.core.state import (
            log_header,
            log_info,
            log_success,
            log_warning,
        )

        log_header("7. COMPLETED (Manager)")
        self._restore_weight_review_report()

        if self.orchestrator.state.get("workflow_mode") == "research":
            log_success("Research-only workflow finished.")
            return

        delivery = self.orchestrator.state.setdefault(
            "delivery", {"delivered": False, "report_done": False}
        )
        if delivery.get("delivered"):
            log_success("Project already delivered; no further work is needed.")
            return
        require_confirmation = self.orchestrator.config.get(
            "require_delivery_confirmation", True
        )
        if delivery.get("report_done"):
            if require_confirmation:
                self._show_delivery_preview(delivery)
            else:
                self._merge_delivery(delivery)
            return

        with open(self.orchestrator.request_path, "r", encoding="utf-8") as f:
            request = f.read()
        with open(self.orchestrator.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()

        # Get final git diff stat
        wt_path = self.orchestrator.ai_dir / "worktree"
        if (
            self.orchestrator.config.get("use_worktree", True)
            and wt_path.exists()
            and self.orchestrator.has_git
        ):
            _, diff_stat = self.orchestrator.run_command(
                ["git", "diff", "--stat", self.orchestrator.base_branch], cwd=wt_path
            )
            _, diff_patch = self.orchestrator.run_command(
                ["git", "diff", self.orchestrator.base_branch], cwd=wt_path
            )
        elif self.orchestrator.has_git:
            _, diff_stat = self.orchestrator.run_command(
                ["git", "diff", "--stat", self.orchestrator.base_branch]
            )
            _, diff_patch = self.orchestrator.run_command(
                ["git", "diff", self.orchestrator.base_branch]
            )
        else:
            diff_stat = "No git repository."
            diff_patch = "No git repository."

        prompt = f"""We have successfully completed the tasks.\nOriginal Request:\n{request}\n\nRequirements:\n{requirements}\n\nGit Diff Stat:\n{diff_stat}\n\nWrite in the same language as the original request. Use exactly these Markdown sections:\n## Outcome\n## Changes Delivered\n## Verification\n## Scope and Follow-ups\nBe specific: name changed files, cite available test evidence, and distinguish completed work from follow-ups."""

        system_prompt = (
            "You are a Project Manager. Write a beautiful project final report."
        )
        summary = self.call_manager(prompt, system_prompt)
        from orchestrator.core.telemetry import summary_markdown

        usage = summary_markdown(self.orchestrator)
        if usage:
            summary = f"{summary.rstrip()}\n\n{usage}\n"

        if not delivery.get("report_done"):
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

            log_info("Asking Assistant to generate CHANGELOG.md...")
            changelog = self.orchestrator.assistant.generate_changelog(
                summary, diff_patch
            ).strip()
            run_id = self.orchestrator.state.setdefault(
                "run_id",
                hashlib.sha256((request + requirements).encode("utf-8")).hexdigest()[
                    :16
                ],
            )
            marker = f"<!-- ai-company-run: {run_id} -->"
            changelog_workspace = (
                self.orchestrator.ai_dir / "worktree"
                if self.orchestrator.has_git
                and self.orchestrator.config.get("use_worktree", True)
                else self.orchestrator.agent_workspace()
            )
            changelog_path = changelog_workspace / "CHANGELOG.md"
            existing_changelog = (
                changelog_path.read_text(encoding="utf-8")
                if changelog_path.exists()
                else ""
            )
            if changelog and marker not in existing_changelog:
                with open(changelog_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n{marker}\n{changelog}\n")
                log_success("CHANGELOG.md updated successfully!")

            self._review_worker_weights(usage)
            delivery["report_done"] = True
            self.orchestrator.save_state()

        delivery["report_done"] = True
        self.orchestrator.save_state()
        if require_confirmation:
            self._show_delivery_preview(delivery)
        else:
            self._merge_delivery(delivery)

    def _show_delivery_preview(self, delivery: dict) -> None:
        from orchestrator.core.delivery import DeliveryError, delivery_preview, save_preview
        from orchestrator.core.state import log_error, log_info

        try:
            preview_data = delivery_preview(self.orchestrator)
        except (DeliveryError, OSError) as error:
            delivery["preview_error"] = str(error)
            self.orchestrator.save_state()
            log_error(f"Could not prepare safe delivery: {error}")
            return
        preview = save_preview(self.orchestrator, preview_data)
        print(preview)
        log_info(
            "Review the preview, then confirm with: "
            f"python3 orchestrator.py deliver --confirm {preview_data['token']}"
        )

    def _merge_delivery(self, delivery: dict) -> None:
        from orchestrator.core.state import log_success, log_warning

        if self.orchestrator.cleanup_worktree(merge=True) is False:
            self.orchestrator.merge_failure_path.write_text(
                str(
                    self.orchestrator.state.get(
                        "worktree_merge_error", "Git merge failed."
                    )
                ),
                encoding="utf-8",
            )
            delivery["token"] = None
            delivery["preview"] = ""
            self.orchestrator.save_state()
            self._handle_delivery_failure()
            return

        delivery["delivered"] = True
        delivery["delivered_at"] = datetime.now(timezone.utc).isoformat()
        delivery.pop("token", None)
        self.orchestrator.save_state()
        log_success("Multi-agent workflow process has finished successfully!")

    def _write_weight_review_report(self, audit: dict) -> None:
        from orchestrator.core.state import log_warning

        marker = f"<!-- worker-weight-review: {audit['run_id']} -->"
        try:
            path = self.orchestrator.final_report_path
            report = path.read_text(encoding="utf-8") if path.exists() else ""
            if marker in report:
                return
            detail = json.dumps(audit, indent=2, ensure_ascii=False, allow_nan=False)
            scoring._write_atomic(
                path,
                f"{report.rstrip()}\n\n{marker}\n## Staff Execution Reliability Review\n\n"
                "Backend completion is not a contract, test, task or quality verdict.\n\n"
                f"```json\n{detail}\n```\n",
            )
        except OSError as error:
            log_warning(
                f"Weight review is saved in config; report update failed: {error}"
            )

    def _restore_weight_review_report(self) -> bool:
        from orchestrator.core.telemetry import current_run_id

        run_id = current_run_id(self.orchestrator)
        previous = self.orchestrator.config.get("last_worker_weight_review")
        if run_id and isinstance(previous, dict) and previous.get("run_id") == run_id:
            self._write_weight_review_report(previous)
            return True
        return False

    def _review_worker_weights(self, telemetry: str) -> None:
        """Apply one evidence-backed local-staff reliability review per run."""
        from orchestrator.core.state import log_info, log_warning
        from orchestrator.core.telemetry import current_run_id, worker_call_evidence

        run_id = current_run_id(self.orchestrator)
        if run_id is None:
            return
        if self._restore_weight_review_report():
            return
        evidence = worker_call_evidence(self.orchestrator)
        if not evidence:
            return
        max_delta = self.orchestrator.config.get("weight_adjustment_max_delta", 0.3)
        if not _finite_number(max_delta) or max_delta < 0:
            log_warning("Skipping weight review; maximum delta is invalid.")
            return
        try:
            worker_weights = {
                role: {
                    level: self.orchestrator.config.get("worker_weights", {})
                    .get(role, {})
                    .get(level, 1.0)
                    for level in ("senior", "middle", "junior")
                }
                for role in ("rd", "qa")
            }
            if any(
                not _finite_number(value)
                or not scoring.WEIGHT_MIN <= value <= scoring.WEIGHT_MAX
                for weights in worker_weights.values()
                for value in weights.values()
            ):
                raise ValueError("Current staff weight is invalid")
        except (AttributeError, TypeError, ValueError) as error:
            log_warning(f"Skipping weight review; {error}")
            return
        prompt = (
            "Review local staff execution reliability using only the supplied current-run evidence.\n"
            f"Current staff weights: {json.dumps(worker_weights)}\n"
            f"Maximum adjustment: {max_delta}\n"
            f"Current-run backend call evidence: {json.dumps(evidence)}\n"
            "This is a bounded sample of the latest eight eligible calls per staff role/level, "
            "not the complete run or a full performance history. "
            "completed means only that the backend returned, before response validation. "
            "It does not establish valid output, passing tests, task completion or output quality. "
            "Never infer quality from it. Internal backend retries may be aggregated. "
            "Do not attribute quota, discovery, cancellation or infrastructure errors to staff quality. "
            "Adjust only the matching rd/qa level supported by actual ollama evidence, "
            "with a nonempty execution-reliability reason and matching evidence_ids. "
            "Vendor scorecards must remain untouched. "
            "Return an empty adjustments array when evidence is insufficient."
        )
        try:
            response = self.call_manager(
                prompt,
                f"You are a Project Manager reviewing execution reliability only. {JSON_RULES}",
                is_weight_adjustment_response,
                WEIGHT_ADJUSTMENT_SCHEMA,
            )
            adjustments = _parse_weight_adjustments(response)
            by_id = {item["id"]: item for item in evidence}
            config = deepcopy(self.orchestrator.config)
            changes = []
            for item in adjustments:
                role, level = item["role"], item["level"]
                cited = [by_id[value] for value in item["evidence_ids"]]
                if any(
                    record["role"] != role or record["level"] != level
                    for record in cited
                ):
                    raise ValueError("Evidence belongs to another staff role or level")
                current = config.get("worker_weights", {}).get(role, {}).get(level, 1.0)
                if (
                    not _finite_number(current)
                    or not scoring.WEIGHT_MIN <= current <= scoring.WEIGHT_MAX
                ):
                    raise ValueError("Current staff weight is invalid")
                lower = max(scoring.WEIGHT_MIN, current - max_delta)
                upper = min(scoring.WEIGHT_MAX, current + max_delta)
                bounded = scoring.clamp(
                    round(scoring.clamp(item["weight"], lower, upper), 3), lower, upper
                )
                if bounded > current and not any(
                    record["completed"] for record in cited
                ):
                    raise ValueError("An increase requires a completed backend call")
                if bounded < current and not any(
                    not record["completed"] for record in cited
                ):
                    raise ValueError(
                        "A decrease requires an eligible failed backend call"
                    )
                config.setdefault("worker_weights", {}).setdefault(role, {})[
                    level
                ] = bounded
                changes.append(
                    {
                        "role": role,
                        "level": level,
                        "original": current,
                        "new": bounded,
                        "reason": item["reason"].strip(),
                        "evidence": cited,
                    }
                )
            audit = {
                "run_id": run_id,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "adjustments": changes,
            }
            config["last_worker_weight_review"] = audit
            scoring._save_config(self.orchestrator, config)
        except Exception as error:
            log_warning(f"Skipping weight review; no adjustment was committed: {error}")
            return
        self._write_weight_review_report(audit)
        log_info(
            f"Manager execution reliability review saved for {len(changes)} staff entries."
        )
