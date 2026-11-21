from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import tempfile
import uuid
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from orchestrator.core import acceptance
from orchestrator.core.state import AgentOrchestrator, DeliveryRepairQueued, delivery_snapshot_id
from orchestrator.main import (
    apply_approve,
    apply_continue,
    apply_review_decision,
    archive_previous_run,
    clear_run_state,
    inline_referenced_markdown,
)

from orchestrator.core.runlock import RunLock
from orchestrator.core.status import read_snapshot, workflow_decision, next_actions, STAGE_LABELS

HOST = "127.0.0.1"

HTML = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-Agent Orchestrator</title>
<style>
body{font:16px system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#202124}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f5f7;padding:1rem;border-radius:8px}
button{margin:.25rem;padding:.6rem 1rem;cursor:pointer} .muted{color:#5f6368}
</style>
<h1>Multi-Agent Orchestrator</h1>
<p id="state">Loading status…</p><p id="progress"></p><div id="actions"></div>
<h2>Tasks</h2><div id="tasks"></div>
<h2>Human review</h2><pre id="review"></pre>
<h2>Delivery preview</h2><pre id="delivery"></pre>
<h2>Tests</h2><pre id="tests"></pre>
<h2>Final report</h2><pre id="final-report"></pre>
<h2>Recent log</h2><pre id="log"></pre>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
const stateNode = document.getElementById('state');
const progressNode = document.getElementById('progress');
const actionNode = document.getElementById('actions');
const taskNode = document.getElementById('tasks');
const reviewNode = document.getElementById('review');
const deliveryNode = document.getElementById('delivery');
const testsNode = document.getElementById('tests');
const finalReportNode = document.getElementById('final-report');
const logNode = document.getElementById('log');
function button(label, action, confirmText='') {
  const node = document.createElement('button');
  node.textContent = label;
  node.onclick = async () => {
    if (confirmText && !window.confirm(confirmText)) return;
    let payload = {action};
    if (action.startsWith('deliver --confirm ') || action.startsWith('review accept-existing --snapshot ')) {
      if (!window.confirm(`${deliveryNode.textContent}\n${action}\nConfirm this operation?`)) return;
    }
    if (action === 'deliver') {
      const snapshot = await getStatus();
      const delivery = snapshot.snapshot?.delivery || {};
      if (delivery.token && delivery.preview) {
        if (!window.confirm(`${delivery.preview}\nConfirm delivery with token ${delivery.token}?`)) return;
        payload.confirm = delivery.token;
      }
    }
    await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json','X-Orchestrator-Token':token}, body:JSON.stringify(payload)});
    await refresh();
  };
  actionNode.append(node);
}
async function getStatus() {
  const response = await fetch('/api/status', {headers:{'X-Orchestrator-Token':token}, cache:'no-store'});
  return response.ok ? response.json() : {};
}
function addText(parent, value) {
  const node = document.createElement('pre');
  node.textContent = value || '';
  parent.append(node);
}
async function refresh() {
  const data = await getStatus();
  const snapshot = data.snapshot || {};
  stateNode.textContent = `${snapshot.state || 'Unknown'}${data.lock ? ` — ${data.lock.command} (PID ${data.lock.pid})` : ''}${data.pause_requested ? ' — pause pending' : ''}`;
  actionNode.replaceChildren();
  for (const action of snapshot.next_actions || []) {
    const command = action.command === 'review pass' ? 'review_pass' : action.command;
    button(action.label, command, ['reset', 'reject', 'review reject', 'review_pass'].includes(command) ? 'Confirm this owner decision?' : '');
  }
  if (data.lock && !data.pause_requested) button('Pause after this step', 'pause');
  if (data.pause_requested) button('Cancel pending pause', 'pause_cancel');
  taskNode.replaceChildren();
  const tasks = snapshot.tasks || [];
  const done = tasks.filter(task => task.status === 'completed').length;
  const active = snapshot.active_task_id ? ` — active task ${snapshot.active_task_id}` : '';
  progressNode.textContent = `Tasks ${done}/${tasks.length} complete${active} · plan revisions ${snapshot.plan_revisions || 0}/${snapshot.max_revisions || 0} · code revisions ${snapshot.code_revisions || 0}/${snapshot.max_revisions || 0}`;
  for (const task of tasks) addText(taskNode, `[${task.status}] ${task.id}: ${task.description}`);
  reviewNode.textContent = data.human_report || snapshot.human_review_details || '';
  deliveryNode.textContent = data.delivery_preview || snapshot.delivery?.preview || '';
  if (snapshot.owner_acceptance_preview) deliveryNode.textContent += '\n' + JSON.stringify(snapshot.owner_acceptance_preview, null, 2);
  testsNode.textContent = data.test_results || '';
  finalReportNode.textContent = data.final_report || '';
  logNode.textContent = (data.run_log || []).join('\n');
}
refresh();
setInterval(refresh, 2000);
</script>
</html>"""


_delivery_snapshot_id = delivery_snapshot_id


class GuiService:

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.orchestrator = AgentOrchestrator(self.workspace)
        self.csrf_token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.worker_lock = threading.Lock()
        self.worker: threading.Thread | None = None

    def _load(self) -> None:
        self.orchestrator.load_config_and_state()

    def _state(self) -> dict:
        state_path = getattr(self.orchestrator, "state_path", None)
        if state_path is None:
            state = getattr(self.orchestrator, "state", {})
            return dict(state) if isinstance(state, dict) else {}
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = getattr(self.orchestrator, "state", {})
            return dict(state) if isinstance(state, dict) else {}
        if not isinstance(state, dict):
            return {}
        preserve_completed = getattr(
            self.orchestrator, "_completed_run_needs_preservation", None
        )
        if (
            state.get("state") == "COMPLETED"
            and callable(preserve_completed)
            and preserve_completed(state)
        ):
            state = dict(state)
            state["state"] = "WAITING_FOR_DELIVERY"
            state.setdefault("resume_state", "WAITING_FOR_DELIVERY")
        return state

    def _run_to_decision(
        self,
        prepare=None,
        started: threading.Event | None = None,
        startup_error: list[Exception] | None = None,
    ) -> None:
        try:
            with self.orchestrator.execution_lock():
                self._write_worker_status("preparing")
                if started:
                    started.set()
                if prepare:
                    try:
                        prepare()
                    except DeliveryRepairQueued:
                        pass
                self._write_worker_status("running")
                if self.orchestrator.state.get("state") != "COMPLETED":
                    self.orchestrator.run_to_end()
                self._write_worker_status("idle")
        except Exception as error:
            if started and not started.is_set() and startup_error is not None:
                startup_error.append(error)
            self._write_worker_status("failed", str(error))
            if started:
                started.set()
        finally:
            with self.worker_lock:
                self.worker = None

    def _start_worker(self, prepare=None) -> bool:
        started = threading.Event()
        startup_error: list[Exception] = []
        with self.worker_lock:
            if self.worker and self.worker.is_alive():
                return False
            self.worker = threading.Thread(
                target=self._run_to_decision,
                args=(prepare, started, startup_error),
                name="orchestrator-gui-worker",
                daemon=True,
            )
            self.worker.start()
        if not started.wait(10):
            return True
        if startup_error:
            raise RuntimeError(f"Could not start workflow: {startup_error[0]}")
        return True

    def _write_worker_status(self, phase: str, error: str = "") -> None:
        self.worker_status = {"phase": phase, "error": error}
        ai_dir = getattr(self.orchestrator, "ai_dir", None)
        if ai_dir is None:
            return
        temporary = None
        try:
            ai_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=ai_dir, delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(self.worker_status, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, ai_dir / "gui_worker.json")
        except OSError as failure:
            self.worker_status["error"] = (
                f"{error}\nCould not persist GUI worker status: {failure}".strip()
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _reports(self) -> dict[str, str]:
        files = {
            "review": self.orchestrator.human_report_path,
            "tests": self.orchestrator.test_results_path,
            "qa": self.orchestrator.qa_report_path,
            "final": self.orchestrator.final_report_path,
            "plan": self.orchestrator.plan_path,
        }
        return {
            name: path.read_text(encoding="utf-8", errors="replace")
            if path.exists()
            else ""
            for name, path in files.items()
        }

    def _worker_is_running(self) -> bool:
        with self.worker_lock:
            return bool(self.worker and self.worker.is_alive())

    def status(self) -> dict:
        state = self._state()
        tasks = state.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        reports = self._reports()
        completed_tasks = sum(
            isinstance(task, dict)
            and str(task.get("status", "")).upper() in {"DONE", "COMPLETED"}
            for task in tasks
        )
        worker_running = self._worker_is_running()
        external_lock = None
        if not worker_running and getattr(self.orchestrator, "ai_dir", None):
            external_lock = RunLock.read_holder(self.orchestrator.ai_dir)
            worker_running = bool(external_lock)
        test_progress = getattr(self.orchestrator, "_test_progress", None)
        local_activity = getattr(self.orchestrator, "_local_model_activity", None)
        if local_activity is None and external_lock:
            try:
                local_activity = json.loads(
                    (self.orchestrator.ai_dir / "local_model_activity.json").read_text(
                        encoding="utf-8"
                    )
                )
                if local_activity.get("pid") != external_lock.get("pid"):
                    local_activity = None
            except (OSError, ValueError, AttributeError):
                local_activity = None
        try:
            if not (
                isinstance(local_activity, dict)
                and local_activity.get("phase") in {"waiting", "running"}
                and isinstance(local_activity.get("role"), str)
                and isinstance(local_activity.get("model"), str)
                and type(local_activity.get("started")) in {int, float}
                and math.isfinite(local_activity["started"])
                and type(local_activity.get("wait_seconds")) in {int, float}
                and math.isfinite(local_activity["wait_seconds"])
                and 0 <= local_activity["wait_seconds"] <= 300
            ):
                local_activity = None
        except (TypeError, ValueError, OverflowError):
            local_activity = None
        runtime = dict(getattr(self, "worker_status", {}))
        if not runtime:
            try:
                runtime = json.loads(
                    (self.orchestrator.ai_dir / "gui_worker.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (AttributeError, OSError, ValueError):
                runtime = {}
        if not worker_running and runtime.get("phase") in {"preparing", "running"}:
            runtime = {
                "phase": "failed",
                "error": "Previous GUI worker stopped; inspect the saved state before continuing.",
            }
        pause_requested = self.orchestrator.pause_requested()
        pause_status = (
            "目前步驟完成後暫停"
            if pause_requested and worker_running
            else "已暫停，等待擁有者決策"
            if state.get("state") == "WAITING_FOR_OWNER"
            else "未請求"
        )
        preview = (
            state.get("delivery_preview", {})
            if state.get("delivery_preview_presented")
            else state.get("presented_delivery_snapshot", {})
        )
        can_submit = not worker_running and (
            state.get("state") in {"COMPLETED", "FAILED"}
            or (
                state.get("state", "PLANNING") == "PLANNING"
                and not self.orchestrator.request_path.exists()
            )
        )
        decision = workflow_decision(
            state,
            can_submit=can_submit,
            busy=worker_running,
            can_cancel_model=bool(
                getattr(self.orchestrator, "_active_model_call", False)
            ),
            pause_requested=pause_requested,
            external_lock=bool(external_lock),
            git=preview,
            snapshot_id=_delivery_snapshot_id(preview)
            if isinstance(preview, dict) and preview
            else "",
        )
        next_action = decision["explanation"]
        delivery = state.get("delivery", {})
        if not isinstance(delivery, dict):
            delivery = {}
        return {
            "state": state.get("state", "PLANNING"),
            "stage": state.get("stage", state.get("state", "PLANNING")),
            "project_name": Path(
                getattr(
                    self,
                    "workspace",
                    getattr(self.orchestrator, "workspace", Path.cwd()),
                )
            ).name,
            "plan_requires_owner_approval": state.get("plan_requires_owner_approval"),
            "plan_acceptance": (
                acceptance.plan_rows(state)
                if acceptance.awaiting_plan_review(state)
                else []
            ),
            "resume_state": state.get("resume_state"),
            "pass_state": state.get("pass_state"),
            "owner_acceptance_pending": AgentOrchestrator.owner_acceptance_required(
                state
            ),
            "owner_acceptance_preview": state.get("owner_acceptance_preview", {}),
            "delivery_artifacts_prepared": state.get("delivery_artifacts_prepared"),
            "workflow_mode": state.get("workflow_mode"),
            "delivery": {
                "delivered": delivery.get("delivered"),
                "acceptance": delivery.get("acceptance", []),
                "acceptance_reason": state.get("acceptance_delivery", {}).get(
                    "reason", ""
                ),
            },
            "progress": {
                "plan_revisions": state.get("plan_revisions", 0),
                "code_revisions": state.get("code_revisions", 0),
                "tasks": tasks,
                "active_task_id": state.get("active_task_id"),
                "completed_tasks": completed_tasks,
                "total_tasks": len(tasks),
            },
            "review": {
                "source": state.get("human_review_source", ""),
                "reason": state.get("human_review_details", ""),
            },
            "pause_requested": pause_requested,
            "pause_status": pause_status,
            "delivery_confirmed": bool(state.get("delivery_confirmed")),
            "delivery_preview_presented": bool(state.get("delivery_preview_presented")),
            "presented_delivery_snapshot": state.get("presented_delivery_snapshot", {}),
            "delivery_snapshot_id": (
                _delivery_snapshot_id(preview)
                if isinstance(preview, dict) and preview
                else ""
            ),
            "git": preview,
            "tests": reports.get("tests", ""),
            "next_action": next_action,
            "workflow_decision": decision,
            "human_review_kind": state.get("human_review_kind"),
            "reports": reports,
            "can_cancel_model": bool(
                getattr(self.orchestrator, "_active_model_call", False)
            ),
            "worker_running": worker_running,
            "external_lock": external_lock,
            "local_model_activity": (
                {
                    **local_activity,
                    "elapsed_seconds": max(
                        0, int(time.monotonic() - local_activity["started"])
                    ),
                }
                if worker_running and local_activity
                else {}
            ),
            "test_progress": (
                {
                    **test_progress,
                    "elapsed_seconds": max(
                        0, int(time.monotonic() - test_progress["started"])
                    ),
                }
                if worker_running and state.get("state") == "TESTING" and test_progress
                else {}
            ),
            "worker_status": runtime,
            "can_submit": can_submit,
        }

    @contextmanager
    def _operation(self):
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError("Another GUI action is already in progress.")
        try:
            yield
        finally:
            self.operation_lock.release()

    def _new_task(self, prompt: str) -> dict:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Task prompt is required.")
        if len(prompt) > 100_000:
            raise ValueError("Task prompt is too large.")

        def prepare():
            state = self._state()
            if state.get("state") not in {"PLANNING", "COMPLETED", "FAILED"} or (
                state.get("state") == "PLANNING"
                and self.orchestrator.request_path.exists()
            ):
                raise RuntimeError(
                    "A task is already running or waiting for a decision."
                )
            if self.orchestrator.state_path.exists() and not archive_previous_run(
                self.orchestrator
            ):
                raise RuntimeError("The previous workflow could not be archived.")
            if self.orchestrator.ai_dir.exists():
                self.orchestrator.clear_run_artifacts()
            self.orchestrator.init_project()
            self.orchestrator.load_config_and_state()
            full_prompt = inline_referenced_markdown(prompt, self.workspace)
            self.orchestrator.request_path.write_text(full_prompt, encoding="utf-8")
            self.orchestrator.state = {
                "state": "PLANNING",
                "plan_revisions": 0,
                "code_revisions": 0,
                "model_tier_indices": {
                    "developer": 0,
                    "reviewer": 0,
                    "manager": 0,
                    "qa": 0,
                },
                "tasks": [],
                "specialists": [],
                "staffing": {},
                "worker_assignments": {},
                "last_developer_role": "developer_senior",
                "last_qa_level": "senior",
                "developer_promotions": {},
                "task_developer_promotions": {},
                "task_backend_overrides": {},
                "quota_exhausted_backends": {},
                "failed_model_routes": [],
                "task_failed_model_routes": {},
                "task_contract_failures": {},
                "task_developer_calls": {},
                "task_error_signatures": {},
                "workflow_mode": "delivery",
                "research_roles": [],
                "run_id": uuid.uuid4().hex,
            }
            self.orchestrator.save_state()

        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            if not self._start_worker(prepare):
                raise RuntimeError("A workflow worker is already running.")
        return self.status()

    def stop(self) -> dict:
        state = self._state().get("state")
        if state in {
            "COMPLETED",
            "FAILED",
            "WAITING_FOR_OWNER",
            "WAITING_FOR_DELIVERY",
        }:
            raise RuntimeError("Workflow is not running.")
        if self.orchestrator.pause_requested():
            raise RuntimeError("A pause has already been requested.")
        self.orchestrator.request_pause()
        return self.status()

    def cancel_model(self) -> dict:
        if not self._worker_is_running() or not getattr(
            self.orchestrator, "_active_model_call", False
        ):
            raise RuntimeError("No cancellable model call is running in this GUI.")
        self.orchestrator.request_pause(cancel_model=True)
        return self.status()

    def continue_task(self, clear_route_failures: bool = False) -> dict:
        def prepare():
            self._load()
            if not apply_continue(self.orchestrator, clear_route_failures):
                raise RuntimeError(
                    "Developer route contract failures must be cleared before retry."
                )

        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            if not self._start_worker(prepare):
                raise RuntimeError("A workflow worker is already running.")
        return self.status()

    def approve(self) -> dict:
        def prepare():
            self._load()
            if self.orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                raise RuntimeError("Workflow is not waiting for owner approval.")
            if not apply_approve(self.orchestrator):
                raise RuntimeError(
                    "Developer route contract failures must be cleared before retry."
                )

        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            if not self._start_worker(prepare):
                raise RuntimeError("A workflow worker is already running.")
        return self.status()

    def review(
        self, decision: str, snapshot_id: str = "", *, plan_feedback: str | None = None
    ) -> dict:
        if decision not in {
            "pass",
            "revise",
            "reject",
            "preview-existing",
            "accept-existing",
        }:
            raise ValueError("Invalid review decision.")
        if plan_feedback is not None and decision != "revise":
            raise acceptance.AcceptanceError(
                "Plan feedback requires the revise decision."
            )

        def prepare():
            self._load()
            if self.orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                raise RuntimeError("Workflow is not waiting for owner review.")
            if decision == "accept-existing":
                apply_review_decision(
                    self.orchestrator, decision, snapshot_id=snapshot_id
                )
            else:
                apply_review_decision(
                    self.orchestrator,
                    decision,
                    decision == "revise"
                    and self.orchestrator.state.get("code_revisions", 0)
                    >= self.orchestrator.config.get("max_revisions", 2),
                    **(
                        {"plan_feedback": plan_feedback}
                        if plan_feedback is not None
                        else {}
                    ),
                )

        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            if decision in {"reject", "preview-existing"}:
                with self.orchestrator.execution_lock():
                    prepare()
            elif not self._start_worker(prepare):
                raise RuntimeError("A workflow worker is already running.")
        return self.status()

    def reset(self, *, keep_request: bool = False) -> dict:
        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            with self.orchestrator.execution_lock():
                self._load()
                if keep_request and self.orchestrator.state.get("state") != "FAILED":
                    raise RuntimeError(
                        "Only a failed workflow can be sent back to planning here."
                    )
                clear_run_state(
                    self.orchestrator,
                    "PLANNING",
                    **({"discard_worktree": True} if not keep_request else {}),
                )
                if not keep_request and self.orchestrator.request_path.exists():
                    self.orchestrator.request_path.unlink()
                self.orchestrator.save_state()
        return self.status()

    def acceptance_action(
        self,
        action: str,
        snapshot_id: str,
        *,
        criterion_ids: list[str],
        status: str,
        reason: str,
    ) -> dict:
        def prepare():
            self._load()
            if self.orchestrator.state.get("state") != "WAITING_FOR_DELIVERY":
                raise acceptance.AcceptanceError(
                    "Workflow is not waiting for delivery."
                )
            if action == "verify":
                if len(criterion_ids) != 1:
                    raise acceptance.AcceptanceError(
                        "Select exactly one manual criterion."
                    )
                acceptance.record_manual(
                    self.orchestrator, snapshot_id, criterion_ids[0], status, reason
                )
            else:
                acceptance.revise(self.orchestrator, snapshot_id, criterion_ids, reason)

        if action not in {"verify", "revise"}:
            raise ValueError("Invalid acceptance action.")
        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            if action == "verify":
                with self.orchestrator.execution_lock():
                    prepare()
            elif not self._start_worker(prepare):
                raise RuntimeError("A workflow worker is already running.")
        return self.status()

    def delivery(
        self,
        confirm: bool,
        snapshot_id: str | None = None,
        *,
        acceptance_reason: str = "",
    ) -> dict:
        try:
            return self._delivery(confirm, snapshot_id, acceptance_reason)
        except DeliveryRepairQueued:
            with self._operation():
                if not self._start_worker(self._load):
                    raise RuntimeError("A workflow worker is already running.")
            return self.status()

    @contextmanager
    def _delivery_preview_lock(self):
        execution_lock = self.orchestrator.execution_lock
        lock_override = vars(self.orchestrator).get("execution_lock")
        owner = (os.getpid(), threading.get_ident())

        @contextmanager
        def nested_lock():
            if owner == (os.getpid(), threading.get_ident()):
                yield
            else:
                with execution_lock():
                    yield

        with execution_lock():
            # Nested saves share this lock; only the outer exit releases it.
            self.orchestrator.execution_lock = nested_lock
            try:
                yield
            finally:
                owner = None
                if lock_override is None:
                    del self.orchestrator.execution_lock
                else:
                    self.orchestrator.execution_lock = lock_override

    def _delivery(
        self, confirm: bool, snapshot_id: str | None = None, acceptance_reason: str = ""
    ) -> dict:
        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            if not confirm:
                with self._delivery_preview_lock():
                    self._load()
                    if self.orchestrator.state.get("state") != "WAITING_FOR_DELIVERY":
                        raise RuntimeError("Workflow is not waiting for delivery.")
                    operation = self.orchestrator.delivery_operation()
                    if not self.orchestrator.state.get(
                        "delivery_artifacts_prepared", False
                    ):
                        step = getattr(
                            self.orchestrator, "step_waiting_for_delivery", None
                        )
                        if callable(step):
                            step()
                    if isinstance(
                        self.orchestrator.state.get("delivery_preview"), dict
                    ):
                        preview = self.orchestrator.get_delivery_preview()
                    else:
                        preview = self.orchestrator.create_delivery_preview(operation)
                    stored = self.orchestrator.state.get("delivery_preview")
                    if isinstance(stored, dict):
                        preview = dict(stored)
                    preview = self.orchestrator.present_delivery_preview(preview)
                    self.orchestrator.state["presented_delivery_snapshot"] = dict(
                        preview
                    )
                    self.orchestrator.save_state()
                result = self.status()
                result["delivery_confirmation_required"] = True
                return result

            def prepare():
                self._load()
                if self.orchestrator.state.get("state") != "WAITING_FOR_DELIVERY":
                    raise RuntimeError("Workflow is not waiting for delivery.")
                operation = self.orchestrator.delivery_operation()
                preview = self.orchestrator.get_delivery_preview()
                if not self.orchestrator.state.get(
                    "delivery_preview_presented"
                ) or snapshot_id != _delivery_snapshot_id(preview):
                    raise RuntimeError(
                        "Git impact changed; the confirmed snapshot does not match the displayed snapshot. Review it again."
                    )
                try:
                    self.orchestrator.confirm_delivery(
                        preview,
                        operation,
                        **(
                            {"acceptance_reason": acceptance_reason}
                            if acceptance_reason
                            else {}
                        ),
                    )
                except (DeliveryRepairQueued, acceptance.AcceptanceError):
                    raise
                except RuntimeError as error:
                    self.orchestrator.create_delivery_preview(operation)
                    self.orchestrator.state.pop("presented_delivery_snapshot", None)
                    self.orchestrator.save_state()
                    raise RuntimeError(
                        "Git impact changed or the saved preview is invalid; review the new snapshot and confirm again."
                    ) from error
                self.orchestrator.step_waiting_for_delivery()

            if not self._start_worker(prepare):
                raise RuntimeError("A workflow worker is already running.")
        return self.status()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _safe_text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _report_html(value: str) -> str:
    return html.escape(value, quote=False).replace("\n", "<br>")


_GUI_SCRIPT = r"""
let csrf = document.body.dataset.csrf;
let deliverySnapshotId = document.getElementById('git').dataset.snapshotId || '';
let actionPending = false;
let refreshing = null;
let actionVersion = 0;
let statusCurrent = true;
let observedDeliveryError = document.getElementById('git').dataset.validationError || '';
let deliveryInvalid = !!observedDeliveryError;
let previewPending = false;
let requestedPreview = null;
let latestControls = new Map();
let latestStyles = new Map();
const pendingReadings = new Map();
const readingMarkup = new Map();
for (const id of ['tasks', 'reports', 'current-review']) {
  const node = document.getElementById(id);
  readingMarkup.set(node, node.innerHTML);
}
let pendingGit = null;
let pendingWorkflow = null;
let decisionTitle = document.getElementById('decision-copy').querySelector('h3').textContent;
let decisionExplanation = document.getElementById('decision-copy').querySelector('p').textContent;
const readingNotice = '資料已更新，目前保留閱讀內容';
function controls(root=document) {
  return Array.from(root.querySelectorAll('button[data-action]'));
}
function rememberControls(root) {
  latestControls = new Map(controls(root).map(node =>
    [node.dataset.action, node.dataset.enabled === 'true']));
  latestStyles = new Map(controls(root).map(node => [node.dataset.action, node.className]));
}
rememberControls(document);
function message(id, text) {
  const node = document.getElementById(id);
  if (node && node.textContent !== text) node.textContent = text;
}
function selectedWithin(node) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return false;
  if (node.contains(selection.anchorNode) || node.contains(selection.focusNode)) return true;
  return Array.from({length:selection.rangeCount || 0}, (_, i) => selection.getRangeAt(i))
    .some(range => range.intersectsNode(node));
}
function preserveDeliverySelection(event) {
  const preview = document.getElementById('git').querySelector('[data-preview]');
  if (event.button === 0 && selectedWithin(preview)) event.preventDefault();
}
function protectedNode(node) {
  return selectedWithin(node) || node.contains(document.activeElement);
}
function readingProtected(node) {
  return protectedNode(node) || node.open === true ||
    Array.from(node.querySelectorAll('details[data-detail]')).some(detail => detail.open);
}
function patchReading(current, incoming, feedbackId, key='') {
  const markup = incoming.innerHTML;
  if (markup !== readingMarkup.get(current)) {
    pendingReadings.set(current, {incoming, feedbackId, key});
    if (readingProtected(current)) {
      message(feedbackId, readingNotice);
      return false;
    }
    if (key) patchList(current, incoming, key);
    else patchRegion(current, incoming);
    readingMarkup.set(current, markup);
  }
  pendingReadings.delete(current);
  message(feedbackId, '');
  return true;
}
function patchRegion(current, incoming, preserve=true) {
  if (current.innerHTML !== incoming.innerHTML) {
    if (preserve && protectedNode(current)) return false;
    const top = current.scrollTop, left = current.scrollLeft;
    const open = new Map(Array.from(current.querySelectorAll('details[data-detail]'))
      .map(node => [node.dataset.detail, node.open]));
    const scrolls = Array.from(current.querySelectorAll('pre')).map(node => [node.scrollTop, node.scrollLeft]);
    current.innerHTML = incoming.innerHTML;
    current.querySelectorAll('pre').forEach((node, index) => {
      if (scrolls[index]) [node.scrollTop, node.scrollLeft] = scrolls[index];
    });
    current.querySelectorAll('details[data-detail]').forEach(node => {
      if (open.has(node.dataset.detail)) node.open = open.get(node.dataset.detail);
    });
    current.scrollTop = top;
    current.scrollLeft = left;
  }
  return true;
}
function patchList(current, incoming, key) {
  const existing = new Map(Array.from(current.children).map(node => [node.dataset[key], node]));
  const wanted = new Set();
  let previous = null;
  for (const next of Array.from(incoming.children)) {
    const id = next.dataset[key];
    wanted.add(id);
    let node = existing.get(id);
    if (node) {
      if (key === 'report') {
        patchRegion(node.querySelector('pre'), next.querySelector('pre'));
        patchRegion(node.querySelector('summary'), next.querySelector('summary'));
      } else {
        patchRegion(node, next);
        if (key === 'action') {
          node.className = next.className;
          node.dataset.enabled = next.dataset.enabled;
          if (next.dataset.snapshotId) node.dataset.snapshotId = next.dataset.snapshotId;
        }
      }
    } else {
      node = next;
      current.appendChild(node);
    }
    // Moving a selected or focused subtree can destroy a native Range.
    const desired = previous ? previous.nextElementSibling : current.firstElementChild;
    if (node !== desired && !protectedNode(current)) current.insertBefore(node, desired);
    previous = node;
  }
  for (const node of Array.from(current.children)) {
    if (!wanted.has(node.dataset[key]) && !protectedNode(node) &&
        !(key === 'report' && node.open)) node.remove();
  }
}
function applyWorkflowUpdate() {
  if (!pendingWorkflow) return;
  const current = document.getElementById('workflow-status');
  // The container survives replacement; selected or focused descendants must remain.
  const preserve = current !== document.activeElement || selectedWithin(current);
  const applied = patchRegion(current, pendingWorkflow, preserve);
  message('workflow-reading-status', applied ? '' :
    readingNotice + '。最新任務概況：' + pendingWorkflow.textContent.trim().replace(/\s+/g, ' '));
  if (applied) pendingWorkflow = null;
}
function applyGitUpdate(reveal=false) {
  if (!pendingGit) return;
  const {nextGit, completed, waitingForDelivery} = pendingGit;
  const git = document.getElementById('git');
  const currentPreview = git.querySelector('[data-preview]');
  const nextPreview = nextGit.querySelector('[data-preview]');
  const confirming = !!git.querySelector('[data-action="confirm-delivery"]');
  if (!reveal && (requestedPreview || (confirming && waitingForDelivery && !completed))) return;
  const currentText = currentPreview.querySelector('pre'), nextText = nextPreview.querySelector('pre');
  const sameContent = git.dataset.snapshotId === nextGit.dataset.snapshotId &&
    (currentText ? currentText.innerHTML : '') === (nextText ? nextText.innerHTML : '');
  if (!reveal && (!sameContent || (completed && confirming)) && readingProtected(currentPreview)) {
    message('git-reading-status', readingNotice);
    return;
  }
  if (sameContent)
    patchList(document.getElementById('delivery-actions'), nextGit.querySelector('#delivery-actions'), 'action');
  else if (reveal)
    currentPreview.innerHTML = nextPreview.innerHTML;
  else
    patchRegion(currentPreview, nextPreview);
  deliverySnapshotId = completed ? '' : nextGit.dataset.snapshotId;
  git.dataset.snapshotId = deliverySnapshotId;
  git.dataset.presented = nextGit.dataset.presented;
  git.dataset.validationError = nextGit.dataset.validationError;
  git.dataset.delivered = nextGit.dataset.delivered;
  if (waitingForDelivery && nextGit.dataset.presented === 'true' && deliverySnapshotId &&
      (reveal || !confirming || !sameContent)) {
    // Newly applied confirmation controls must be visible with their snapshot.
    currentPreview.querySelectorAll('details[data-detail="git-impact"]').forEach(node => {
      node.open = true;
    });
  }
  if (!waitingForDelivery) {
    deliveryInvalid = false;
    if (!completed) message('delivery-feedback', '');
  }
  pendingGit = null;
  message('git-reading-status', '');
}
function revealDeliveryPreview() {
  if (actionPending || !statusCurrent || !requestedPreview || !pendingGit) return;
  applyGitUpdate(true);
  requestedPreview = null;
  deliveryInvalid = false;
  message('action-feedback', `已展示本次快照 ${deliverySnapshotId}。`);
  message('delivery-feedback', '請檢視 Git 影響與驗收結果，再另行確認交付。');
  const heading = document.getElementById('git').querySelector('details[data-detail="git-impact"] summary');
  heading.focus({preventScroll:true});
  heading.scrollIntoView({block:'start'});
  updateAvailability();
}
function flushReadingUpdates() {
  if (actionPending) return;
  const x = window.scrollX, y = window.scrollY;
  for (const [current, {incoming, feedbackId, key}] of Array.from(pendingReadings))
    patchReading(current, incoming, feedbackId, key);
  applyWorkflowUpdate();
  applyGitUpdate();
  updateAvailability();
  window.scrollTo(x, y);
}
function updateDecisionCopy() {
  let title = decisionTitle, explanation = decisionExplanation;
  const waitingForPreview = pendingGit && pendingGit.waitingForDelivery && pendingGit.changed &&
    pendingGit.nextGit.dataset.presented === 'true';
  const hasConfirmation = !!document.getElementById('git').querySelector('[data-action="confirm-delivery"]');
  if (previewPending) {
    title = '正在展示 Git 影響';
    explanation = '請等候本次快照。';
  } else if (latestControls.get('deliver{}') === true &&
      ((deliveryInvalid && hasConfirmation) || requestedPreview || waitingForPreview)) {
    if (requestedPreview) {
      title = '新 Git 展示尚待套用';
      explanation = '目前保留閱讀內容；請使用「查看本次快照」開啟新展示，再另行確認交付。';
    } else if (!deliveryInvalid && waitingForPreview) {
      title = '新 Git 展示尚待套用';
      explanation = '目前保留閱讀內容；請收合 Git 區塊並結束選取或焦點閱讀，套用新展示後才能另行確認交付。';
    } else {
      title = 'Git 影響已變更，請重新展示';
      explanation = '舊確認已停用。請使用「展示 Git 影響」重新展示並檢視，再另行確認交付。';
    }
  }
  const current = document.getElementById('decision-copy');
  const heading = current.querySelector('h3'), content = current.querySelector('p');
  if ((heading.textContent !== title || content.textContent !== explanation) && protectedNode(current)) {
    message('decision-status', title + '。' + explanation);
    return;
  }
  if (heading.textContent !== title) heading.textContent = title;
  if (content.textContent !== explanation) content.textContent = explanation;
  message('decision-status', '');
}
function updateAvailability() {
  updateDecisionCopy();
  const view = document.getElementById('delivery-preview-view');
  view.hidden = !requestedPreview;
  view.disabled = view.hidden || actionPending || !statusCurrent;
  controls().forEach(node => {
    if (node === view) return;
    const confirm = node.dataset.action === 'confirm-delivery';
    const bound = !!node.dataset.snapshotId;
    node.className = latestStyles.get(node.dataset.action) || 'secondary';
    if (confirm && deliveryInvalid) node.className = 'secondary';
    if (node.dataset.action === 'deliver{}' && deliveryInvalid &&
        latestControls.get(node.dataset.action) === true) node.className = 'primary';
    node.disabled = actionPending || !statusCurrent ||
      latestControls.get(node.dataset.action) !== true ||
      (bound && (deliveryInvalid || node.dataset.snapshotId !== deliverySnapshotId));
  });
}
function invalidateDelivery() {
  deliveryInvalid = true;
  if (!requestedPreview)
    message('delivery-feedback', 'Git 展示已變更或驗證未通過，請重新展示並檢視 Git 影響；舊確認已停用。');
  updateAvailability();
}
function rejectDeliveryPreview(reason) {
  const {id} = requestedPreview;
  requestedPreview = null;
  invalidateDelivery();
  message('action-feedback', `本次快照 ${id} 無法展示：${reason}。請重新展示 Git 影響。`);
}
async function refresh(afterAction=false) {
  if (actionPending && !afterAction) return false;
  if (refreshing) {
    if (!afterAction) return false;
    await refreshing;
  }
  refreshing = refreshPage(afterAction);
  try { return await refreshing; } finally { refreshing = null; }
}
async function refreshPage(afterAction) {
  const version = actionVersion;
  try {
    // Keep workflow decisions in the server-rendered controls.
    const response = await fetch('/', {credentials:'same-origin', cache:'no-store'});
    if (!response.ok) throw new Error('Status request failed');
    const incoming = new DOMParser().parseFromString(await response.text(), 'text/html');
    const ids = ['workflow-status', 'tasks', 'task-form', 'actions', 'git', 'reports', 'reset-actions', 'delivery-actions', 'display-actions', 'decision-copy', 'current-review', 'evidence-summary', 'plan-acceptance'];
    if (!ids.every(id => incoming.getElementById(id)) || !incoming.body.dataset.csrf ||
        !incoming.getElementById('state') ||
        !incoming.getElementById('task-form').querySelector('textarea'))
      throw new Error('Incomplete status page');
    for (const [id, key] of [['reports', 'report'], ['tasks', 'task']]) {
      const nodes = Array.from(incoming.getElementById(id).children);
      if (nodes.some(node => !node.dataset[key]) ||
          new Set(nodes.map(node => node.dataset[key])).size !== nodes.length ||
          (key === 'report' && nodes.some(node => !node.querySelector('pre') || !node.querySelector('summary'))))
        throw new Error('Invalid status items');
    }
    if (!incoming.getElementById('git').querySelector('[data-preview]'))
      throw new Error('Missing delivery content');
    const nextControls = controls(incoming);
    if (nextControls.some(node => !['true', 'false'].includes(node.dataset.enabled)) ||
        new Set(nextControls.map(node => node.dataset.action)).size !== nextControls.length)
      throw new Error('Invalid controls');
    if ((actionPending && !afterAction) || version !== actionVersion) return false;
    const x = window.scrollX, y = window.scrollY;
    rememberControls(incoming);
    const git = document.getElementById('git'), nextGit = incoming.getElementById('git');
    const currentPreview = git.querySelector('[data-preview]');
    const nextPreview = nextGit.querySelector('[data-preview]');
    const confirmingDelivery = !!git.querySelector('[data-action="confirm-delivery"]');
    const nextState = incoming.getElementById('state').textContent;
    const waitingForDelivery = nextState === 'WAITING_FOR_DELIVERY';
    const deliveryCompleted = nextState === 'COMPLETED' &&
      nextGit.dataset.delivered === 'true';
    const validationError = nextGit.dataset.validationError || '';
    if (validationError && validationError !== observedDeliveryError) invalidateDelivery();
    observedDeliveryError = validationError;
    if (requestedPreview) {
      // The worker may still report the error that prompted this successful preview.
      const newError = validationError !== requestedPreview.error && validationError;
      if (newError || !waitingForDelivery || nextGit.dataset.presented !== 'true' ||
          nextGit.dataset.snapshotId !== requestedPreview.id ||
          !nextPreview.querySelector('details[data-detail="git-impact"] summary'))
        rejectDeliveryPreview(newError || '最新交付狀態與回應不符');
    }
    const currentText = currentPreview.querySelector('pre'), nextText = nextPreview.querySelector('pre');
    const changed = git.dataset.snapshotId !== nextGit.dataset.snapshotId ||
      (currentText ? currentText.innerHTML : '') !== (nextText ? nextText.innerHTML : '') ||
      (git.dataset.presented === 'true' && nextGit.dataset.presented !== 'true');
    if (confirmingDelivery && changed && !requestedPreview && !deliveryCompleted) invalidateDelivery();
    pendingWorkflow = incoming.getElementById('workflow-status');
    applyWorkflowUpdate();
    patchRegion(document.getElementById('evidence-summary'), incoming.getElementById('evidence-summary'));
    patchRegion(document.getElementById('plan-acceptance'), incoming.getElementById('plan-acceptance'));
    const nextDecision = incoming.getElementById('decision-copy');
    decisionTitle = nextDecision.querySelector('h3').textContent;
    decisionExplanation = nextDecision.querySelector('p').textContent;
    patchReading(document.getElementById('current-review'), incoming.getElementById('current-review'), 'review-reading-status');
    patchReading(document.getElementById('tasks'), incoming.getElementById('tasks'), 'tasks-reading-status', 'task');
    const form = document.getElementById('task-form');
    if (!form.contains(document.activeElement) && !form.querySelector('textarea').value)
      form.hidden = incoming.getElementById('task-form').hidden;
    patchList(document.getElementById('actions'), incoming.getElementById('actions'), 'action');
    patchList(document.getElementById('reset-actions'), incoming.getElementById('reset-actions'), 'action');
    patchList(document.getElementById('display-actions'), incoming.getElementById('display-actions'), 'action');
    if (deliveryCompleted) {
      deliveryInvalid = false;
      deliverySnapshotId = '';
      // Disable confirmation now; applyGitUpdate removes it after reading ends.
      updateAvailability();
      message('delivery-feedback', '交付已完成，詳細結果請查看最終報告。');
    }
    // Keep confirmation controls inside the snapshot they actually describe.
    pendingGit = {nextGit, completed:deliveryCompleted, waitingForDelivery, changed};
    message('git-reading-status', changed ? readingNotice : '');
    applyGitUpdate();
    patchReading(document.getElementById('reports'), incoming.getElementById('reports'), 'reports-reading-status', 'report');
    csrf = incoming.body.dataset.csrf;
    statusCurrent = true;
    updateAvailability();
    if (deliveryInvalid) invalidateDelivery();
    window.scrollTo(x, y);
    message('last-refresh', '最後成功更新：' + new Date().toLocaleString());
    message('connection-status', '連線正常');
    message('refresh-error', '');
    return true;
  } catch(error) {
    statusCurrent = false;
    updateAvailability();
    message('connection-status', '連線異常，操作暫停');
    message('refresh-error', '更新失敗，正在顯示上次資料');
    return false;
  }
}
async function promptAcceptance(kind, criterionId='', status='') {
  const promptText = {
    plan: '請輸入要調整的驗收項目與修訂內容。',
    confirm: '驗收仍有待驗證項目。請輸入接受這些待驗證項目並交付的理由。',
    verify: `請輸入 ${criterionId} 的實際觀察結果及驗證環境。`,
    revise: '請輸入需要修訂的原因及預期結果。'
  };
  const criterionIds = kind === 'revise' ? Array.from(
    document.querySelectorAll('input[name="acceptance-criterion"]:checked'), input => input.value
  ) : [criterionId];
  if (kind === 'revise' && !criterionIds.length) {
    message('delivery-feedback', '請選取至少一個要修訂的驗收項目。');
    return;
  }
  const reason = window.prompt(promptText[kind]);
  if (reason === null) return;
  if (!reason.trim() || reason.length > 2000) {
    message('action-feedback', '請輸入 1 至 2000 字。');
    return;
  }
  if (kind === 'plan') {
    await post('/api/review', {decision:'revise', reason});
  } else if (kind === 'confirm') {
    await post('/api/deliver', {confirm:true, snapshot_id:deliverySnapshotId, reason});
  } else {
    await post('/api/acceptance', {action:kind, snapshot_id:deliverySnapshotId,
      criterion_ids:criterionIds, status, reason});
  }
}
async function post(url, body={}) {
  if (actionPending || !statusCurrent) return;
  const key = url === '/api/task' ? 'task{}' :
    url === '/api/deliver' && body.confirm ? 'confirm-delivery' :
    url === '/api/acceptance' ? (body.action === 'verify' ?
      `acceptance-${body.criterion_ids[0]}-${body.status}` : 'acceptance-revise') :
    url === '/api/review' && body.reason ? 'review-plan-feedback' :
    url.slice('/api/'.length) + JSON.stringify(body, Object.keys(body).sort());
  const control = controls().find(node => node.dataset.action === key);
  if (!control || control.disabled || latestControls.get(key) !== true) return;
  if ((body.confirm || url === '/api/acceptance') && (deliveryInvalid || !deliverySnapshotId ||
      body.snapshot_id !== deliverySnapshotId || control.dataset.snapshotId !== deliverySnapshotId)) {
    invalidateDelivery();
    return;
  }
  actionPending = true;
  actionVersion += 1;
  const previewing = url === '/api/deliver' && !body.confirm;
  if (previewing) {
    previewPending = true;
    requestedPreview = null;
    deliveryInvalid = true;
    message('delivery-feedback', '');
  }
  updateAvailability();
  message('action-feedback', url === '/api/deliver' ?
    (body.confirm ? '正在驗證並處理交付，請等待結果。' : '正在準備交付資料並展示 Git 影響，請等待結果。') :
    '操作處理中，請稍候。');
  try {
    const response = await fetch(url, {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) {
      if (url === '/api/deliver' || url === '/api/acceptance') invalidateDelivery();
      message('action-feedback', '操作未完成：' + (data.error || '伺服器拒絕操作，請檢查最新狀態。') +
        (previewing ? ' 請重新展示 Git 影響。' : ''));
      alert(data.error || '操作失敗');
    } else if (previewing) {
      const id = data.delivery_snapshot_id;
      if (data.delivery_preview_presented === true && typeof id === 'string' && id) {
        requestedPreview = {id, error:data.worker_status?.phase === 'failed' ? data.worker_status.error : ''};
        message('action-feedback', `已取得本次快照 ${id}，正在更新交付預覽。`);
      } else {
        invalidateDelivery();
        message('action-feedback', '展示未完成：伺服器未回傳有效快照。請重新展示 Git 影響。');
      }
    } else {
      if (url === '/api/task') {
        const input = document.querySelector('#task-form textarea');
        if (input.value === body.prompt) input.value = '';
      }
      if (url === '/api/acceptance' && body.action === 'verify') {
        invalidateDelivery();
        message('action-feedback', '已記錄人工驗證。請重新展示並檢視交付快照。');
      } else {
        message('action-feedback', '操作已受理，請依最新狀態查看結果。');
      }
    }
  } catch(error) {
    if (url === '/api/deliver' || url === '/api/acceptance') invalidateDelivery();
    message('action-feedback', '連線或回應異常，操作結果尚未確認；' +
      (previewing ? '請在狀態更新後重新展示 Git 影響。' : '請先等待狀態更新。'));
  } finally {
    const current = await refresh(true);
    actionPending = false;
    previewPending = false;
    if (previewing && requestedPreview) {
      if (!current) {
        rejectDeliveryPreview('無法確認最新交付狀態');
      } else if (selectedWithin(document.getElementById('git').querySelector('[data-preview]'))) {
        message('action-feedback', `本次快照 ${requestedPreview.id} 已準備好。請選擇「查看本次快照」以開啟。`);
        message('delivery-feedback', '新展示尚未套用，目前保留閱讀內容。');
      } else {
        revealDeliveryPreview();
      }
    }
    updateAvailability();
  }
}
document.getElementById('task-form').onsubmit = async (event) => {
  event.preventDefault();
  const prompt = new FormData(event.target).get('prompt');
  // Prompt text is not part of the stable submit-control identity.
  if (actionPending || !statusCurrent || latestControls.get('task{}') !== true) return;
  await post('/api/task', {prompt});
};
async function refreshWhenIdle() {
  await refresh();
  setTimeout(refreshWhenIdle, 3000);
}
setTimeout(refreshWhenIdle, 3000);
if (document.addEventListener) {
  document.addEventListener('toggle', flushReadingUpdates, true);
  document.addEventListener('focusout', () => setTimeout(flushReadingUpdates, 0));
  document.addEventListener('selectionchange', flushReadingUpdates);
}
if (deliveryInvalid) invalidateDelivery();
updateAvailability();
"""


def render_page(status: dict) -> str:
    reports = status.get("reports", {})
    progress = status.get("progress", {})
    tasks = [task for task in progress.get("tasks", []) if isinstance(task, dict)]
    review = status.get("review", {})
    current = status.get("state", "PLANNING")
    delivered = (
        isinstance(status.get("delivery"), dict)
        and status["delivery"].get("delivered") is True
    )
    busy = bool(status.get("worker_running"))
    runtime = status.get("worker_status", {})
    reason = str(review.get("reason", "")).strip()
    source = review.get("source", "")
    git = status.get("git") or {}
    snapshot_id = status.get("delivery_snapshot_id", "")
    presented = bool(status.get("delivery_preview_presented") and snapshot_id and git)
    plan_acceptance = status.get("plan_acceptance", [])
    criteria = (
        status["delivery"].get("acceptance", [])
        if delivered
        else git.get("acceptance", [])
    )
    has_pending = any(row["status"] == "pending" for row in criteria)
    has_failed = any(row["status"] == "failed" for row in criteria)
    can_submit = (
        not busy
        and current in {"PLANNING", "COMPLETED", "FAILED"}
        and bool(
            status.get(
                "can_submit",
                current in {"COMPLETED", "FAILED"}
                or current == "PLANNING"
                and not tasks,
            )
        )
    )
    phase = {
        "preparing": "準備中",
        "running": "執行中",
        "failed": "執行失敗",
        "idle": "已停止於決策點",
    }.get(runtime.get("phase"), "等待操作")
    local_activity = status.get("local_model_activity", {})
    if busy and local_activity:
        model = f"{local_activity['role']} / {local_activity['model']}"
        elapsed = local_activity["elapsed_seconds"]
        if local_activity["phase"] == "waiting":
            phase += f"；等待本機推論資源：{model}，已等待 {elapsed} 秒（上限 {local_activity['wait_seconds']} 秒）"
        else:
            phase += f"；本機模型推論中：{model}，已執行 {elapsed} 秒"
    test_progress = status.get("test_progress", {})
    if busy and current == "TESTING" and test_progress:
        phase += f"；測試已執行 {test_progress['elapsed_seconds']} 秒（上限 {test_progress['timeout']} 秒）"
        if test_progress.get("output_tail"):
            phase += (
                "；最近輸出：" + test_progress["output_tail"].splitlines()[-1][-180:]
            )
        else:
            phase += "；等待測試輸出"
    stage_labels = STAGE_LABELS
    stage = status.get("stage", current)
    buttons: list[str] = []
    delivery_buttons: list[str] = []
    display_buttons: list[str] = []
    reset_buttons: list[str] = []

    def button(
        label,
        action,
        body=None,
        confirmation=None,
        *,
        primary=False,
        enabled=True,
        target=None,
        identity=None,
        prompt=None,
    ):
        payload = body or {}
        key = identity or action + json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
        script = f"post('/api/{action}', {json.dumps(payload)})"
        if identity == "confirm-delivery":
            script = (
                "promptAcceptance('confirm')"
                if has_pending
                else "post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})"
            )
            enabled = enabled and not has_failed
            primary = primary and enabled
        if prompt:
            script = (
                "promptAcceptance("
                + ", ".join(json.dumps(part) for part in prompt)
                + ")"
            )
        if confirmation:
            script = f"if(window.confirm({json.dumps(confirmation, ensure_ascii=False)})) {script}"
        attributes = (
            f' data-snapshot-id="{_safe_text(snapshot_id)}"'
            if identity == "confirm-delivery" or action == "acceptance"
            else ""
        )
        if action == "deliver" and target is display_buttons:
            attributes += ' onmousedown="preserveDeliverySelection(event)"'
        (buttons if target is None else target).append(
            f'<button type="button" data-action="{_safe_text(key)}"'
            f' data-enabled="{str(enabled).lower()}"'
            f' class="{"primary" if primary else "secondary"}"{attributes}'
            f' {"" if enabled else "disabled"} onclick="{_safe_text(script)}">'
            f"{_safe_text(label)}</button>"
        )

    decision = status.get("workflow_decision") or workflow_decision(
        {**status, "human_review_source": source, "human_review_details": reason},
        busy=busy,
        can_cancel_model=bool(status.get("can_cancel_model")),
        pause_requested=bool(status.get("pause_requested")),
        external_lock=bool(status.get("external_lock")),
        can_submit=can_submit,
        git=git,
        snapshot_id=snapshot_id,
    )
    title, explanation = decision["title"], decision["explanation"]
    for key, target in (
        ("actions", buttons),
        ("display_actions", display_buttons),
        ("delivery_actions", delivery_buttons),
        ("reset_actions", reset_buttons),
    ):
        for action in decision[key]:
            button(**action, target=target)

    def acceptance_details(row):
        kind = "人工驗證" if row["kind"] == "manual" else "pytest"
        return (
            f'<strong>{_safe_text(row["id"])} · {_safe_text(row["requirement"])}</strong>'
            f'<p>驗證方式（{kind}）<br>{_report_html(row["check"])}</p>'
            f'<p class="muted">對應任務 {_safe_text(", ".join(row["task_ids"]))}</p>'
        )

    plan_html = ""
    if plan_acceptance:
        rows_html = "".join(
            f"<li>{acceptance_details(row)}</li>" for row in plan_acceptance
        )
        plan_html = (
            f'<h3>提議的驗收項目</h3><ul class="acceptance-list">{rows_html}</ul>'
        )
        if not busy and not status.get("external_lock"):
            button(
                "要求調整驗收項目",
                "review",
                target=buttons,
                identity="review-plan-feedback",
                prompt=("plan",),
            )

    stages = ["需求", "計畫", "核准", "實作", "測試", "審查", "交付"]
    delivery_complete = current == "COMPLETED" and delivered
    stage_index = {
        "PLANNING": 0,
        "DEVELOPING_PLAN": 1,
        "REVIEWING_PLAN": 1,
        "IMPLEMENTING": 3,
        "TESTING": 4,
        "REVIEWING_CODE": 5,
        "WAITING_FOR_DELIVERY": 6,
        "COMPLETED": 6,
    }.get(status.get("resume_state") if current == "WAITING_FOR_OWNER" else current)
    if decision.get("kind") == "approve_plan":
        stage_index = 2
    if current == "RESEARCHING":
        stages, stage_index = ["研究"], 0
    steps = []
    for index, label in enumerate(stages):
        done = delivery_complete or stage_index is not None and index < stage_index
        active = not delivery_complete and index == stage_index
        steps.append(
            f'<li class="step {"done" if done else "active" if active else "upcoming"}"'
            f'{" aria-current=\"step\"" if active else ""}>'
            f'<span class="step-marker" aria-hidden="true">{"✓" if done else index + 1}</span>'
            f'<span>{label}</span><span class="sr-only">'
            f'{"（已經過）" if done else "（目前階段）" if active else "（尚未到達）"}</span></li>'
        )
    state_label = {
        "WAITING_FOR_DELIVERY": "待交付確認",
        "WAITING_FOR_OWNER": "等待你的決策",
        "COMPLETED": "已完成交付" if delivered else "待確認交付狀態",
    }.get(current, stage_labels.get(current, current))
    state_tone = (
        "complete"
        if delivery_complete
        else "failed"
        if current == "FAILED"
        else "pending"
        if current in {"WAITING_FOR_OWNER", "WAITING_FOR_DELIVERY", "COMPLETED"}
        else "running"
    )

    task_items = []
    task_occurrences: dict[str, int] = {}
    for task in tasks:
        task_id = str(task.get("id", ""))
        task_status = str(task.get("status", "")).upper()
        if task_status in {"DONE", "COMPLETED"}:
            icon, label = "✓", "已完成"
        elif task_status in {"RUNNING", "IN_PROGRESS", "IMPLEMENTING"} or (
            task_id == progress.get("active_task_id") and busy
        ):
            icon, label = "▶", "執行中"
        elif task_status in {"FAILED", "ERROR"}:
            icon, label = "!", "失敗"
        else:
            icon, label = "○", "待處理"
        description = str(task.get("description", ""))
        target_files = task.get("target_files", [])
        if not isinstance(target_files, list):
            target_files = []
        canonical = json.dumps(
            {
                "id": task_id,
                "description": description,
                "target_files": sorted(set(target_files))
                if isinstance(target_files, list)
                else [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        task_occurrences[fingerprint] = task_occurrences.get(fingerprint, 0) + 1
        key = f"task-{fingerprint}-{task_occurrences[fingerprint]}"
        task_items.append(
            f'<li data-task="{_safe_text(key)}">'
            f'<div class="task-heading"><strong>{_safe_text(task_id)}</strong>'
            f'<span class="badge"><span aria-hidden="true">{icon}</span> {label}</span></div>'
            f'<details data-detail="{_safe_text(key)}"><summary>'
            f'{_safe_text(description.splitlines()[0][:120] if description else "查看任務描述")}'
            '</summary><pre tabindex="0">'
            + _report_html(description)
            + "</pre></details>"
            + '<div class="file-chips">'
            + "".join(
                f"<code>{_safe_text(path)}</code>"
                for path in target_files
                if isinstance(path, str)
            )
            + "</div></li>"
        )
    report_names = {
        "review": "人工審核報告",
        "tests": "測試結果",
        "qa": "QA 報告",
        "final": "最終報告",
        "plan": "實作計畫",
    }
    report_html = "".join(
        f'<details name="workflow-reports" data-report="{_safe_text(name)}" data-detail="report-{_safe_text(name)}">'
        f"<summary>{_safe_text(report_names.get(name, name))}</summary>"
        f'<pre id="report-{_safe_text(name)}" tabindex="0">{_report_html(value)}</pre></details>'
        for name, value in reports.items()
        if value
    )
    evidence_html = "".join(
        f'<li><div><strong>{label}</strong><span class="muted">'
        f'{"可檢視報告" if reports.get(name) else "尚無紀錄"}</span></div>'
        + (
            f'<a href="#report-{name}">查看證據<span class="sr-only">：{label}</span></a>'
            if reports.get(name)
            else ""
        )
        + "</li>"
        for name, label in (
            ("tests", "測試結果"),
            ("qa", "QA 報告"),
            ("review", "審核報告"),
        )
    )
    acceptance_html = ""
    if criteria:
        row_items = []
        waiting = current == "WAITING_FOR_DELIVERY"
        ready = bool(
            presented and waiting and not busy and not status.get("external_lock")
        )
        labels = {"passed": "通過", "failed": "未通過", "pending": "待驗證"}
        for row in criteria:
            row_buttons = []
            criterion_id = row["id"]
            if waiting and row["kind"] == "manual":
                for result in ("passed", "failed"):
                    button(
                        f"{criterion_id} 記錄{labels[result]}",
                        "acceptance",
                        target=row_buttons,
                        identity=f"acceptance-{criterion_id}-{result}",
                        prompt=("verify", criterion_id, result),
                        enabled=ready,
                    )
            choice = (
                '<label><input type="checkbox" name="acceptance-criterion" '
                f'value="{_safe_text(criterion_id)}"> 選取 {_safe_text(criterion_id)} 修訂</label>'
                if waiting
                else ""
            )
            row_items.append(
                f'<li>{acceptance_details(row)}<p>結果 <strong>{labels[row["status"]]}</strong></p>'
                f'<p>{_report_html(row["evidence"])}</p>'
                + (
                    "<p>上次未通過，需重新驗證。</p>"
                    if row.get("needs_recheck")
                    else ""
                )
                + choice
                + f'<div class="actions">{"".join(row_buttons)}</div></li>'
            )
        notice = ""
        if delivered:
            exception_reason = status["delivery"].get("acceptance_reason", "")
            if exception_reason:
                notice = f"接受待驗證項目的理由：{exception_reason}"
        elif has_failed:
            notice = "請修訂未通過的項目後再交付。"
        elif has_pending:
            notice = "確認交付時須說明接受待驗證項目的理由。"
        acceptance_html = (
            "<h3>逐項驗收結果</h3>"
            f'<ul class="acceptance-list">{"".join(row_items)}</ul>'
            + (f'<p class="muted">{_safe_text(notice)}</p>' if notice else "")
        )
        if waiting:
            button(
                "提交所選項目的修訂",
                "acceptance",
                target=delivery_buttons,
                identity="acceptance-revise",
                prompt=("revise",),
                enabled=ready,
            )
    elif presented and current == "WAITING_FOR_DELIVERY":
        acceptance_html = '<p class="muted">此任務未建立驗收條件</p>'
    git_html = (
        '<details data-detail="git-impact" open><summary>Git 影響（Git impact）</summary>'
        '<pre tabindex="0">'
        + _report_html(
            json.dumps(
                {key: value for key, value in git.items() if key != "acceptance"},
                ensure_ascii=False,
                indent=2,
            )
        )
        + "</pre>"
        + f'<p class="muted">快照 ID：{_safe_text(snapshot_id or "尚無展示")}</p>'
        + acceptance_html
        + f'<div id="delivery-actions" class="actions">{"".join(delivery_buttons)}</div>'
        + "</details>"
        if git
        else acceptance_html + '<div id="delivery-actions" class="actions"></div>'
        if delivered
        else '<div class="empty-state"><strong>尚未展示 Git 影響。</strong>'
        '<p class="muted">到達交付階段後，先展示並檢視變更，再另行確認交付。</p></div>'
        '<div id="delivery-actions" class="actions"></div>'
    )
    error = str(runtime.get("error", ""))
    validation_error = (
        error
        if runtime.get("phase") == "failed"
        and any(
            word in error.lower()
            for word in ("git impact", "snapshot", "saved preview")
        )
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>工作流程控制台 · Orchestrator</title>
<style>
:root{{color-scheme:light;--bg:#F7F9FC;--card:#FFF;--text:#172338;--muted:#52637B;--border:#DAE2ED;--primary:#245BE5;--error:#B42318;--success:#087655;--amber:#985508}}
*{{box-sizing:border-box}} html{{scroll-padding-top:24px}} body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 Inter,"Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;display:grid;grid-template-columns:220px minmax(0,1fr)}}
a{{color:#1D4ED8;text-underline-offset:4px}} a:hover{{color:#173EAD}} .sr-only,.skip-link:not(:focus){{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}} .skip-link:focus{{position:fixed;top:12px;left:12px;z-index:10;background:white;padding:12px}}
.app-sidebar{{position:sticky;top:0;height:100vh;background:white;border-right:1px solid var(--border);padding:28px 20px;display:flex;flex-direction:column;gap:32px}} .brand{{font-weight:750;font-size:19px;display:flex;gap:10px;align-items:center}}.brand-mark{{display:grid;place-items:center;background:var(--primary);color:white;border-radius:10px;width:36px;height:36px;font:700 18px monospace;flex-shrink:0}}
.app-sidebar nav{{display:grid;gap:8px}}.app-sidebar a{{display:flex;align-items:center;min-height:44px;padding:10px 14px;border-radius:9px;text-decoration:none;color:var(--muted);font-weight:600}}.app-sidebar a:hover{{background:#EFF4FF;color:#1D4ED8}}.app-sidebar .management-link{{margin-top:auto;border-top:1px solid var(--border);border-radius:0;padding-top:20px;font-size:14px}}
main{{width:100%;max-width:1440px;margin:auto;padding:28px 36px;min-width:0}}.topbar{{display:flex;align-items:center;justify-content:space-between;gap:20px;border-bottom:1px solid var(--border);padding-bottom:22px;margin-bottom:28px}}.topbar h1{{font-size:22px;margin:0}}.project-name{{font-size:14px;color:var(--muted);overflow-wrap:anywhere}}.connection-details{{font-size:14px;max-width:320px}}.connection-details summary{{color:var(--muted)}}
h2{{font-size:20px;margin:0 0 18px;letter-spacing:-.02em}}h3{{font-size:22px;line-height:1.4;margin:0 0 12px}}p{{margin:8px 0 16px}}.muted{{color:var(--muted);font-size:14px;overflow-wrap:anywhere}}.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:22px;min-width:0;box-shadow:0 1px 3px #17233806}}
.status-heading{{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}}.status-heading h2{{margin:0}}.state-badge,.badge{{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:650;border-radius:999px;padding:5px 12px;background:#EEF3FC;color:#294A86}}.state-badge.pending{{background:#FFF4DB;color:var(--amber)}}.state-badge.complete{{background:#E8F8F0;color:var(--success)}}.state-badge.failed{{background:#FDEDEB;color:var(--error)}}
.workflow-steps{{list-style:none;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px;padding:0;margin:22px 0 12px}}.workflow-steps .step{{display:flex;gap:8px;justify-content:center;align-items:center;border:1px solid var(--border);border-radius:8px;min-height:50px;font-size:14px;color:var(--muted)}}.workflow-steps .done{{background:#EFFAF5;border-color:#B6E4D0;color:var(--success)}}.workflow-steps .active{{background:#FFF7E6;border-color:#D89A29;color:var(--amber);font-weight:700}}.step-marker{{font:600 14px monospace}}.status-meta{{display:flex;gap:12px 24px;flex-wrap:wrap;align-items:center;color:var(--muted);font-size:14px}}.status-meta p{{margin:0}}.runtime-details{{font-size:14px}}.runtime-details summary{{min-height:44px;align-content:center}}
.next-card{{border-color:#E9C574;background:#FFFCF4}}.next-card h2{{font-size:14px;color:var(--amber);letter-spacing:.04em}}.next-card h3{{font-size:26px}}.next-card #decision-copy p{{max-width:76ch;color:#634826}}.actions{{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}}.actions:empty{{display:none}}.action-row{{display:flex;flex-wrap:wrap;align-items:center;gap:12px}}.action-row .actions{{margin:4px 0}}.delivery-link{{font-size:14px;min-height:44px;display:inline-flex;align-items:center}}
button{{font:inherit;min-height:44px;padding:10px 18px;border:1px solid var(--border);border-radius:8px;cursor:pointer}}button:disabled{{opacity:.55;cursor:not-allowed}}.primary{{background:var(--primary);color:white;border-color:var(--primary);font-weight:650}}.secondary{{background:white;color:var(--text)}}button:hover:enabled{{box-shadow:0 0 0 2px #245BE530}}button.primary:hover:enabled{{background:#1D4ED8}}:focus-visible{{outline:3px solid #245BE5;outline-offset:3px}}
.work-grid{{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(280px,1fr);gap:22px;align-items:start}}.task-list{{list-style:none;margin:0;padding:0}}.task-list>li{{padding:18px 0;border-top:1px solid var(--border)}}.task-list>li:first-child{{border:0;padding-top:0}}.task-heading{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}}.task-list summary{{list-style-position:inside;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;min-height:44px;align-content:center}}.file-chips{{display:flex;gap:6px;flex-wrap:wrap}}.file-chips code{{font-size:13px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);overflow-wrap:anywhere}}
.evidence-list{{list-style:none;margin:0;padding:0}}.evidence-list li{{display:flex;justify-content:space-between;align-items:center;gap:14px;padding:14px 0;border-bottom:1px solid var(--border)}}.evidence-list li:last-child{{border:0}}.evidence-list strong,.evidence-list span{{display:block}}.evidence-list a{{min-height:44px;align-content:center;font-size:14px;white-space:nowrap}}.evidence-note{{margin:14px 0 0}}
.acceptance-list{{list-style:none;padding:0;overflow-wrap:anywhere}}.acceptance-list li{{padding:12px 0;border-top:1px solid var(--border)}}.acceptance-list p{{margin:6px 0}}.acceptance-list label{{display:inline-flex;align-items:center;gap:8px;min-height:44px}}
pre{{font:14px/1.65 ui-monospace,"SFMono-Regular",Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:16px;border:1px solid var(--border);border-radius:8px;max-height:32rem;overflow:auto}}details{{margin:10px 0}}summary{{cursor:pointer;overflow-wrap:anywhere;min-height:44px;align-content:center}}#reports details{{border:1px solid var(--border);border-radius:8px;margin:10px 0}}#reports summary{{padding:10px 16px;font-weight:600}}#reports details[open] summary{{background:#EFF4FF;color:#1D4ED8}}#reports pre{{margin:12px;border:0}}.empty-state{{text-align:center;background:var(--bg);border:1px dashed var(--border);border-radius:8px;padding:28px 20px}}.empty-state p{{margin:8px 0 0}}
textarea{{display:block;width:100%;min-height:130px;resize:vertical;font:inherit;border:1px solid var(--border);border-radius:8px;padding:12px;margin:10px 0}}label{{font-weight:650}}#task-form{{border-top:1px solid #E9DAB6;padding-top:20px;margin-top:20px}}.feedback{{color:var(--muted);overflow-wrap:anywhere;font-size:14px}}.feedback:empty,.error:empty{{display:none}}.error{{color:var(--error);overflow-wrap:anywhere}}#task-management>summary{{font-weight:650}}#reset-area .secondary{{color:var(--error);border-color:#E8C3BD}}[hidden]{{display:none!important}}
@media(max-width:1100px){{body{{grid-template-columns:180px minmax(0,1fr)}}main{{padding:24px}}.brand{{font-size:16px}}.work-grid{{grid-template-columns:1fr}}}}
@media(max-width:760px){{body{{display:block}}.app-sidebar{{position:static;height:auto;padding:14px 16px;gap:12px;border-right:0;border-bottom:1px solid var(--border)}}.app-sidebar nav{{display:flex;gap:4px;flex-wrap:wrap}}.app-sidebar a{{padding:8px 12px}}.app-sidebar .management-link{{margin:0;border:0;padding:8px 12px}}.brand{{font-size:18px}}main{{padding:20px 16px}}.topbar{{align-items:start;gap:12px;flex-wrap:wrap;margin-bottom:20px}}.topbar h1{{font-size:20px}}.card{{padding:18px}}.workflow-steps{{grid-template-columns:repeat(4,minmax(0,1fr))}}.workflow-steps .step{{font-size:13px;gap:5px}}.next-card h3{{font-size:23px}}.work-grid{{gap:0}}.connection-details{{width:100%;max-width:none}}.actions button{{max-width:100%}}.evidence-list li{{gap:8px}}}}
</style>
</head>
<body data-csrf="{_safe_text(status.get('csrf', ''))}" data-busy-message="{_safe_text(explanation if busy else '')}">
<a class="skip-link" href="#main-content">跳至主要內容</a>
<aside class="app-sidebar" aria-label="工作流程導覽">
<div class="brand"><span class="brand-mark" aria-hidden="true">&gt;_</span>工作流程控制台</div>
<nav aria-label="頁面區段">
<a href="#overview-title">總覽</a><a href="#tasks-title">任務清單</a>
<a href="#delivery-title">交付變更</a><a href="#reports-title">報告</a>
</nav>
<a class="management-link" href="#task-management" onclick="document.getElementById('task-management').open=true">任務管理 · 重設與重新規劃</a>
</aside>
<main id="main-content">
<header class="topbar">
<h1>工作流程控制台</h1>
<details class="connection-details"><summary id="connection-status">已載入，等待背景更新</summary>
<p id="last-refresh" role="status"></p><p id="refresh-error" class="error" role="status"></p></details>
</header>
<section class="card" aria-labelledby="overview-title">
<div id="workflow-status">
<div class="status-heading"><h2 id="overview-title">任務概況</h2><span class="state-badge {state_tone}">{_safe_text(state_label)}</span></div>
<p class="project-name">{_safe_text(status.get("project_name", "目前專案"))} · 本機工作區</p>
<ol class="workflow-steps" aria-label="工作流程階段">{''.join(steps)}</ol>
<div class="status-meta"><p>任務完成數：{_safe_text(progress.get("completed_tasks", 0))} / {_safe_text(progress.get("total_tasks", len(tasks)))}</p>
<p>執行狀態：{_safe_text(phase)}</p></div>
<p class="error" role="alert">{_safe_text(error)}</p>
<details class="runtime-details"><summary>進階狀態</summary><p>目前階段：{_safe_text(stage_labels.get(stage, stage))}</p><p>狀態代碼：<code id="state">{_safe_text(current)}</code></p></details>
</div>
<p id="workflow-reading-status" class="feedback" role="status" aria-atomic="true"></p>
</section>
<section class="card next-card" aria-labelledby="next-title">
<h2 id="next-title">下一步</h2>
<div id="decision-copy"><h3>{_safe_text(title)}</h3><p>{_safe_text(explanation)}</p></div>
<div id="plan-acceptance">{plan_html}</div>
<p id="decision-status" class="feedback" role="status"></p>
<div class="action-row"><div id="actions" class="actions">{''.join(buttons)}</div>
<div id="display-actions" class="actions">{''.join(display_buttons)}</div>
<button type="button" id="delivery-preview-view" data-action="view-delivery-preview" data-enabled="false" class="secondary" aria-controls="git" onclick="revealDeliveryPreview()" hidden disabled>查看本次快照</button>
<a class="delivery-link" href="#delivery-title">查看交付變更與確認 ↓</a></div>
<form id="task-form" {'' if can_submit else 'hidden'}>
<label for="task-prompt">新任務需求</label>
<textarea id="task-prompt" name="prompt" maxlength="100000" required placeholder="描述你希望完成的任務" aria-describedby="prompt-help"></textarea>
<p id="prompt-help" class="muted">一次處理一個任務，請描述預期結果與限制。</p>
<button type="submit" data-action="task{{}}" data-enabled="{str(can_submit).lower()}" class="{'primary' if can_submit else 'secondary'}" {'' if can_submit else 'disabled'}>提交任務</button>
</form>
<p id="action-feedback" class="feedback" role="status" aria-atomic="true"></p>
</section>
<div class="work-grid">
<section class="card" aria-labelledby="tasks-title">
<h2 id="tasks-title">任務清單</h2>
<p id="tasks-reading-status" class="feedback" role="status"></p>
<ul id="tasks" class="task-list">{''.join(task_items)}</ul>
</section>
<aside class="card" aria-labelledby="evidence-title">
<h2 id="evidence-title">驗收證據</h2>
<div id="evidence-summary"><ul class="evidence-list">{evidence_html}</ul></div>
<p class="muted evidence-note">報告存在不代表驗收通過。交付仍需檢視變更及二次確認。</p>
</aside>
</div>
<section class="card" aria-labelledby="delivery-title">
<h2 id="delivery-title">交付展示與確認</h2>
<div id="git" data-snapshot-id="{_safe_text(snapshot_id)}" data-presented="{str(presented).lower()}" data-validation-error="{_safe_text(validation_error)}" data-delivered="{str(delivered).lower()}">
<p id="git-reading-status" class="feedback" role="status"></p>
<div data-preview>{git_html}</div>
</div>
<p id="delivery-feedback" class="feedback" role="status" aria-atomic="true"></p>
</section>
<section class="card" aria-labelledby="reports-title">
<h2 id="reports-title">報告與紀錄</h2><p class="muted">選擇報告查看內容。報告可能屬於先前階段，請以目前狀態及原因判斷下一步。</p>
<p id="review-reading-status" class="feedback" role="status"></p>
<details id="current-review" data-detail="current-review"><summary>目前審核原因：{_safe_text(source or "尚無來源")}</summary>
<pre id="review-reason" tabindex="0">{_report_html(reason)}</pre></details>
<p id="reports-reading-status" class="feedback" role="status"></p>
<div id="reports">{report_html}</div>
</section>
<details id="task-management" class="card"><summary>任務管理</summary>
<section id="reset-area" aria-labelledby="reset-title">
<h2 id="reset-title" class="sr-only">任務管理</h2>
<p class="muted">以下操作會影響目前任務。請先確認備份與保留範圍。</p>
<div id="reset-actions" class="actions">{''.join(reset_buttons)}</div>
</section></details>
</main>
<script>{_GUI_SCRIPT}</script>
</body>
</html>"""


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "OrchestratorGUI/1.0"

    @property
    def service(self) -> GuiService:
        return self.server.service

    def _origin_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        expected_host = f"127.0.0.1:{self.server.server_port}"
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        if host != expected_host:
            return False
        if fetch_site and fetch_site not in {"same-origin", "same-site", "none"}:
            return False
        if self.command == "POST":
            return origin == expected_origin
        return not origin or origin == expected_origin

    def _csrf_allowed(self) -> bool:
        if self.command != "POST":
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get("gui_csrf")
        return bool(
            session
            and secrets.compare_digest(session.value, self.service.csrf_token)
            and secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), self.service.csrf_token
            )
        )

    def _security_check(self) -> bool:
        if self.command not in {"GET", "HEAD", "POST"} or not self._origin_allowed():
            self._send_json({"error": "Request rejected."}, HTTPStatus.FORBIDDEN)
            return False
        if not self._csrf_allowed():
            self._send_json({"error": "CSRF validation failed."}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = _json_bytes(value)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request length.") from error
        if length < 0 or length > 256 * 1024:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ValueError("JSON object required.")
        return value

    def _result(self, value: dict) -> dict:
        result = dict(value)
        result["csrf"] = self.service.csrf_token
        return result

    def do_GET(self) -> None:
        if not self._security_check():
            return
        path = urlsplit(self.path).path
        if path == "/":
            status = self._result(self.service.status())
            body = render_page(status).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", self._cookie())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return
        if path == "/api/status":
            self._send_json(self._result(self.service.status()))
            return
        self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        if not self._security_check():
            return
        path = urlsplit(self.path).path
        if path not in {
            "/api/task",
            "/api/stop",
            "/api/cancel-model",
            "/api/continue",
            "/api/approve",
            "/api/review",
            "/api/acceptance",
            "/api/reset",
            "/api/reject",
            "/api/deliver",
        }:
            self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._read_json()
            if path == "/api/task":
                result = self.service._new_task(str(body.get("prompt", "")))
            elif path == "/api/stop":
                result = self.service.stop()
            elif path == "/api/cancel-model":
                result = self.service.cancel_model()
            elif path == "/api/continue":
                clear = body.get("clear_route_failures", False)
                if not isinstance(clear, bool):
                    raise ValueError("clear_route_failures must be a boolean.")
                result = (
                    self.service.continue_task(clear_route_failures=True)
                    if clear
                    else self.service.continue_task()
                )
            elif path == "/api/approve":
                result = self.service.approve()
            elif path == "/api/review":
                decision = str(body.get("decision", ""))
                if "reason" in body:
                    if not isinstance(body["reason"], str):
                        raise ValueError("Plan feedback must be text.")
                    result = self.service.review(decision, plan_feedback=body["reason"])
                elif decision == "accept-existing":
                    result = self.service.review(
                        decision, str(body.get("snapshot_id", ""))
                    )
                else:
                    result = self.service.review(decision)
            elif path == "/api/acceptance":
                action, snapshot_id, status, reason = (
                    body.get(key, "")
                    for key in ("action", "snapshot_id", "status", "reason")
                )
                criterion_ids = body.get("criterion_ids")
                if (
                    any(
                        not isinstance(value, str)
                        for value in (action, snapshot_id, status, reason)
                    )
                    or not isinstance(criterion_ids, list)
                    or len(criterion_ids) > 30
                    or any(
                        not isinstance(criterion_id, str)
                        for criterion_id in criterion_ids
                    )
                ):
                    raise ValueError(
                        "Acceptance actions require text and a list of criterion IDs."
                    )
                result = self.service.acceptance_action(
                    action,
                    snapshot_id,
                    criterion_ids=criterion_ids,
                    status=status,
                    reason=reason,
                )
            elif path == "/api/reset":
                result = self.service.reset()
            elif path == "/api/reject":
                result = self.service.reset(keep_request=True)
            else:
                confirm = body.get("confirm", False)
                reason = body.get("reason", "")
                if (
                    not isinstance(confirm, bool)
                    or not isinstance(reason, str)
                    or (reason and not confirm)
                ):
                    raise ValueError(
                        "A delivery reason requires explicit confirmation."
                    )
                result = self.service.delivery(
                    confirm,
                    body.get("snapshot_id"),
                    **({"acceptance_reason": reason} if reason else {}),
                )
            self._send_json(self._result(result))
        except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.CONFLICT)

    def _cookie(self) -> str:
        return (
            f"gui_csrf={self.service.csrf_token}; Path=/; HttpOnly; " "SameSite=Strict"
        )

    def log_message(self, format, *args):
        return


def _read_text(path, limit=1_000_000):
    try:
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
    except OSError:
        return ""
    if len(content) > limit:
        content = content[-limit:]
    return content.decode("utf-8", errors="replace")


def make_handler(orchestrator, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "OrchestratorGUI"

        def _json(self, status: int, value: dict):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, require_origin=False):
            host = self.headers.get("Host", "").lower()
            if host not in allowed_hosts:
                self._json(403, {"error": "Invalid Host header."})
                return False
            if not hmac.compare_digest(
                self.headers.get("X-Orchestrator-Token", ""), token
            ):
                self._json(403, {"error": "Invalid access token."})
                return False
            if require_origin and self.headers.get("Origin") not in allowed_origins:
                self._json(403, {"error": "Invalid Origin header."})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path)
            host = self.headers.get("Host", "").lower()
            if host not in allowed_hosts:
                self._json(403, {"error": "Invalid Host header."})
                return
            if path.path == "/":
                query = dict(
                    item.split("=", 1) for item in path.query.split("&") if "=" in item
                )
                if not hmac.compare_digest(query.get("token", ""), token):
                    self._json(403, {"error": "Invalid access token."})
                    return
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                )
                self.end_headers()
                self.wfile.write(body)
                return
            if path.path != "/api/status" or not self._authorized():
                if path.path != "/api/status":
                    self._json(404, {"error": "Not found."})
                return
            try:
                snapshot = read_snapshot(orchestrator.status_path)
            except (OSError, json.JSONDecodeError):
                self._json(503, {"error": "Status snapshot is unavailable."})
                return
            holder = RunLock.read_holder(orchestrator.ai_dir)
            snapshot["next_actions"] = next_actions(
                snapshot,
                busy=bool(holder),
                external_lock=bool(holder),
                pause_requested=orchestrator.pause_request_path.exists(),
            )
            self._json(
                200,
                {
                    "snapshot": snapshot,
                    "lock": holder,
                    "pause_requested": orchestrator.pause_request_path.exists(),
                    "run_log": _read_text(orchestrator.run_log_path).splitlines()[
                        -200:
                    ],
                    "human_report": _read_text(orchestrator.human_report_path),
                    "test_results": _read_text(orchestrator.test_results_path),
                    "delivery_preview": _read_text(orchestrator.delivery_preview_path),
                    "final_report": _read_text(orchestrator.final_report_path),
                },
            )

        def do_POST(self):
            if not self._authorized(require_origin=True):
                return
            if urlsplit(self.path).path != "/api/action":
                self._json(404, {"error": "Not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(400, {"error": "Invalid Content-Length."})
                return
            if length < 1 or length > 16_384:
                self._json(413, {"error": "Request body is too large or empty."})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "Request body must be JSON."})
                return
            if not isinstance(payload, dict):
                self._json(400, {"error": "Request body must be an object."})
                return
            action = payload.get("action")
            commands = {
                "run": ["run"],
                "deliver": ["deliver"],
                "continue": ["continue"],
                "pause": ["pause", "--source", "gui"],
                "pause_cancel": ["pause", "--cancel"],
                "review_pass": ["review", "pass"],
                "approve": ["approve", "--run"],
                "review revise": ["review", "revise", "--run"],
                "review reject": ["review", "reject"],
                "review preview-existing": ["review", "preview-existing"],
                "continue --clear-route-failures": [
                    "continue",
                    "--clear-route-failures",
                ],
                "reject": ["reject"],
                "reset": ["reset"],
            }
            bound_command = re.fullmatch(
                r"(deliver --confirm |review accept-existing --snapshot )([0-9a-f]{64})",
                action if isinstance(action, str) else "",
            )
            if bound_command:
                commands[action] = (
                    ["deliver", "--confirm", bound_command[2]]
                    if bound_command[1].startswith("deliver")
                    else [
                        "review",
                        "accept-existing",
                        "--snapshot",
                        bound_command[2],
                        "--run",
                    ]
                )
            if action == "deliver" and payload.get("confirm") is not None:
                confirmation = payload.get("confirm")
                if not isinstance(confirmation, str) or not re.fullmatch(
                    r"[0-9a-f]{12}", confirmation
                ):
                    self._json(
                        400,
                        {"error": "A valid delivery confirmation token is required."},
                    )
                    return
                commands[action] = ["deliver", "--confirm", confirmation]
            elif action not in commands:
                self._json(400, {"error": "Unknown action."})
                return
            if action not in {"pause", "pause_cancel"}:
                holder = RunLock.read_holder(orchestrator.ai_dir)
                if holder:
                    self._json(
                        409,
                        {
                            "error": "Another command is already running.",
                            "lock": holder,
                        },
                    )
                    return
            log = orchestrator.run_log_path
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as output:
                process = subprocess.Popen(
                    [sys.executable, "orchestrator.py", *commands[action]],
                    cwd=orchestrator.workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
            self._json(202, {"started": True, "pid": process.pid})

        def log_message(self, format, *args):
            return

    return Handler


def serve(workspace, port: int = 8765):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    service = GuiService(Path(workspace))
    server = ThreadingHTTPServer((HOST, port), RequestHandler)
    server.service = service
    print(f"Orchestrator GUI listening on http://{HOST}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local orchestrator web GUI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    serve(args.workspace, args.port)
