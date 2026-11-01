from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import tempfile
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from orchestrator.core.state import AgentOrchestrator, DeliveryRepairQueued, delivery_snapshot_id
from orchestrator.main import (
    apply_approve,
    apply_review_decision,
    archive_previous_run,
    clear_run_state,
    inline_referenced_markdown,
    pick_continue_decision,
)

from orchestrator.core.runlock import RunLock
from orchestrator.core.status import read_snapshot

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
    if (action === 'deliver') {
      const snapshot = await getStatus();
      const delivery = snapshot.snapshot?.delivery || {};
      if (!delivery.token || !delivery.preview) return;
      if (!window.confirm(`${delivery.preview}\nConfirm delivery with token ${delivery.token}?`)) return;
      payload.confirm = delivery.token;
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
    button(action.label, command, command === 'reject' ? 'Reject this run? Existing worktree changes may be discarded.' : '');
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
        next_action = {
            "PLANNING": "Review the implementation plan.",
            "WAITING_FOR_OWNER": "Review the reported reason and choose an approval decision.",
            "WAITING_FOR_DELIVERY": (
                "Confirm the presented Git impact."
                if state.get("delivery_preview_presented")
                else "Show the Git impact before confirming delivery."
            ),
            "COMPLETED": "No further action is required.",
            "FAILED": "Review the failure report before submitting a new task.",
        }.get(state.get("state"), "Wait for the worker to advance the workflow.")
        if pause_requested and worker_running:
            next_action = "The current step will finish, then the workflow will pause."
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
            "resume_state": state.get("resume_state"),
            "pass_state": state.get("pass_state"),
            "delivery_artifacts_prepared": state.get("delivery_artifacts_prepared"),
            "workflow_mode": state.get("workflow_mode"),
            "delivery": {"delivered": delivery.get("delivered")},
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
            "reports": reports,
            "worker_running": worker_running,
            "external_lock": external_lock,
            "worker_status": runtime,
            "can_submit": not worker_running
            and (
                state.get("state") in {"COMPLETED", "FAILED"}
                or (
                    state.get("state", "PLANNING") == "PLANNING"
                    and not self.orchestrator.request_path.exists()
                )
            ),
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

    def continue_task(self, clear_route_failures: bool = False) -> dict:
        def prepare():
            self._load()
            state = self.orchestrator.state.get("state")
            if state in {"COMPLETED", "FAILED"}:
                raise RuntimeError(
                    "Workflow cannot be continued from its terminal state."
                )
            if state == "WAITING_FOR_DELIVERY":
                raise RuntimeError("Delivery confirmation is required.")
            if state == "WAITING_FOR_OWNER":
                decision, reset_revisions = pick_continue_decision(self.orchestrator)
                if decision == "revise":
                    apply_review_decision(
                        self.orchestrator,
                        "revise",
                        reset_revisions,
                    )
                elif not apply_approve(
                    self.orchestrator, clear_route_failures=clear_route_failures
                ):
                    raise RuntimeError(
                        "Developer route contract failures must be cleared before retry."
                    )
            else:
                self.orchestrator.clear_pause_request()

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
            self.orchestrator.clear_pause_request()
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

    def review(self, decision: str) -> dict:
        if decision not in {"pass", "revise", "reject"}:
            raise ValueError("Invalid review decision.")

        def prepare():
            self._load()
            if self.orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                raise RuntimeError("Workflow is not waiting for owner review.")
            apply_review_decision(
                self.orchestrator,
                decision,
                decision == "revise"
                and self.orchestrator.state.get("code_revisions", 0)
                >= self.orchestrator.config.get("max_revisions", 2),
            )

        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            if decision == "reject":
                with self.orchestrator.execution_lock():
                    prepare()
            elif not self._start_worker(prepare):
                raise RuntimeError("A workflow worker is already running.")
        return self.status()

    def reset(self) -> dict:
        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            with self.orchestrator.execution_lock():
                self._load()
                clear_run_state(self.orchestrator, "PLANNING")
                if self.orchestrator.request_path.exists():
                    self.orchestrator.request_path.unlink()
                self.orchestrator.save_state()
        return self.status()

    def delivery(self, confirm: bool, snapshot_id: str | None = None) -> dict:
        try:
            return self._delivery(confirm, snapshot_id)
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

    def _delivery(self, confirm: bool, snapshot_id: str | None = None) -> dict:
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
                    self.orchestrator.confirm_delivery(preview, operation)
                except DeliveryRepairQueued:
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
let deliveryRefreshRequested = false;
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
function applyGitUpdate() {
  if (!pendingGit) return;
  const {nextGit, completed, waitingForDelivery} = pendingGit;
  const git = document.getElementById('git');
  const currentPreview = git.querySelector('[data-preview]');
  const nextPreview = nextGit.querySelector('[data-preview]');
  const confirming = !!git.querySelector('[data-action="confirm-delivery"]');
  if (confirming && waitingForDelivery && !deliveryRefreshRequested && !completed) return;
  const currentText = currentPreview.querySelector('pre'), nextText = nextPreview.querySelector('pre');
  const sameContent = git.dataset.snapshotId === nextGit.dataset.snapshotId &&
    (currentText ? currentText.innerHTML : '') === (nextText ? nextText.innerHTML : '');
  if ((!sameContent || (completed && confirming)) && readingProtected(currentPreview)) {
    message('git-reading-status', readingNotice);
    return;
  }
  if (sameContent)
    patchList(document.getElementById('delivery-actions'), nextGit.querySelector('#delivery-actions'), 'action');
  else
    patchRegion(currentPreview, nextPreview);
  deliverySnapshotId = completed ? '' : nextGit.dataset.snapshotId;
  git.dataset.snapshotId = deliverySnapshotId;
  git.dataset.presented = nextGit.dataset.presented;
  git.dataset.validationError = nextGit.dataset.validationError;
  git.dataset.delivered = nextGit.dataset.delivered;
  if (waitingForDelivery && nextGit.dataset.presented === 'true' && deliverySnapshotId &&
      (deliveryRefreshRequested || !confirming || !sameContent)) {
    // Newly applied confirmation controls must be visible with their snapshot.
    currentPreview.querySelectorAll('details[data-detail="git-impact"]').forEach(node => {
      node.open = true;
    });
  }
  if (deliveryRefreshRequested && nextGit.dataset.presented === 'true' && deliverySnapshotId) {
    deliveryInvalid = false;
    deliveryRefreshRequested = false;
    message('delivery-feedback', 'Git 影響已重新展示，請檢視後另行確認。');
  }
  if (!waitingForDelivery) {
    deliveryInvalid = false;
    deliveryRefreshRequested = false;
    if (!completed) message('delivery-feedback', '');
  }
  pendingGit = null;
  message('git-reading-status', '');
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
  if (latestControls.get('deliver{}') === true &&
      ((deliveryInvalid && hasConfirmation) || deliveryRefreshRequested || waitingForPreview)) {
    if (deliveryRefreshRequested || (!deliveryInvalid && waitingForPreview)) {
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
  controls().forEach(node => {
    const confirm = node.dataset.action === 'confirm-delivery';
    node.className = latestStyles.get(node.dataset.action) || 'secondary';
    if (confirm && deliveryInvalid) node.className = 'secondary';
    if (node.dataset.action === 'deliver{}' && deliveryInvalid &&
        latestControls.get(node.dataset.action) === true) node.className = 'primary';
    node.disabled = actionPending || !statusCurrent ||
      latestControls.get(node.dataset.action) !== true ||
      (confirm && (deliveryInvalid || node.dataset.snapshotId !== deliverySnapshotId));
  });
}
function invalidateDelivery() {
  deliveryInvalid = true;
  message('delivery-feedback', 'Git 展示已變更或驗證未通過，請重新展示並檢視 Git 影響；舊確認已停用。');
  updateAvailability();
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
    const ids = ['workflow-status', 'tasks', 'task-form', 'actions', 'git', 'reports', 'reset-actions', 'delivery-actions', 'display-actions', 'decision-copy', 'current-review'];
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
    const currentText = currentPreview.querySelector('pre'), nextText = nextPreview.querySelector('pre');
    const changed = git.dataset.snapshotId !== nextGit.dataset.snapshotId ||
      (currentText ? currentText.innerHTML : '') !== (nextText ? nextText.innerHTML : '') ||
      (git.dataset.presented === 'true' && nextGit.dataset.presented !== 'true');
    if (confirmingDelivery && changed && !deliveryRefreshRequested && !deliveryCompleted) invalidateDelivery();
    pendingWorkflow = incoming.getElementById('workflow-status');
    applyWorkflowUpdate();
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
      deliveryRefreshRequested = false;
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
async function post(url, body={}) {
  if (actionPending || !statusCurrent) return;
  const key = url === '/api/task' ? 'task{}' :
    url === '/api/deliver' && body.confirm ? 'confirm-delivery' :
    url.slice('/api/'.length) + JSON.stringify(body, Object.keys(body).sort());
  const control = controls().find(node => node.dataset.action === key);
  if (!control || control.disabled || latestControls.get(key) !== true) return;
  if (body.confirm && (deliveryInvalid || !deliverySnapshotId ||
      body.snapshot_id !== deliverySnapshotId || control.dataset.snapshotId !== deliverySnapshotId)) {
    invalidateDelivery();
    return;
  }
  actionPending = true;
  actionVersion += 1;
  updateAvailability();
  message('action-feedback', url === '/api/deliver' ?
    (body.confirm ? '正在驗證並處理交付，請等待結果。' : '正在準備交付資料並展示 Git 影響，請等待結果。') :
    '操作處理中，請稍候。');
  let displayed = false;
  try {
    const response = await fetch(url, {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) {
      if (url === '/api/deliver') invalidateDelivery();
      message('action-feedback', '操作未完成：' + (data.error || '伺服器拒絕操作，請檢查最新狀態。'));
      alert(data.error || '操作失敗');
    } else {
      if (url === '/api/task') {
        const input = document.querySelector('#task-form textarea');
        if (input.value === body.prompt) input.value = '';
      }
      displayed = url === '/api/deliver' && !body.confirm &&
        data.delivery_preview_presented === true && !!data.delivery_snapshot_id;
      if (displayed) {
        deliveryRefreshRequested = true;
        invalidateDelivery();
      }
      message('action-feedback', '操作已受理，請依最新狀態查看結果。');
    }
  } catch(error) {
    if (url === '/api/deliver') invalidateDelivery();
    message('action-feedback', '連線或回應異常，操作結果尚未確認；請先等待狀態更新。');
  } finally {
    await refresh(true);
    actionPending = false;
    updateAvailability();
    if (displayed && deliveryRefreshRequested)
      message('delivery-feedback', '新展示尚未套用，目前保留閱讀內容；收合此區並結束閱讀後會更新，確認仍暫停。');
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
    resume = status.get("resume_state")
    pass_state = status.get("pass_state")
    prepared = status.get("delivery_artifacts_prepared") is True
    git = status.get("git", {})
    snapshot_id = status.get("delivery_snapshot_id", "")
    presented = bool(status.get("delivery_preview_presented") and snapshot_id and git)
    valid_preview = presented and prepared
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
    stage_labels = {
        "PLANNING": "需求規劃",
        "DEVELOPING_PLAN": "撰寫實作計畫",
        "REVIEWING_PLAN": "審核實作計畫",
        "IMPLEMENTING": "實作中",
        "TESTING": "測試驗證",
        "REVIEWING_CODE": "程式審核",
        "WAITING_FOR_OWNER": "等待擁有者決策",
        "WAITING_FOR_DELIVERY": "交付準備與確認",
        "COMPLETED": "已完成",
        "FAILED": "流程失敗",
    }
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
    ):
        payload = body or {}
        key = identity or action + json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
        script = f"post('/api/{action}', {json.dumps(payload)})"
        if identity == "confirm-delivery":
            script = (
                "post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})"
            )
        if confirmation:
            script = f"if(window.confirm({json.dumps(confirmation, ensure_ascii=False)})) {script}"
        attributes = (
            f' data-snapshot-id="{_safe_text(snapshot_id)}"'
            if identity == "confirm-delivery"
            else ""
        )
        (buttons if target is None else target).append(
            f'<button type="button" data-action="{_safe_text(key)}"'
            f' data-enabled="{str(enabled).lower()}"'
            f' class="{"primary" if primary else "secondary"}"{attributes}'
            f' {"" if enabled else "disabled"} onclick="{_safe_text(script)}">'
            f"{_safe_text(label)}</button>"
        )

    title = "等待下一步"
    explanation = "請查看目前階段及報告，再決定是否繼續。"
    if busy:
        if status.get("external_lock"):
            title = "其他執行程序持有工作流程鎖"
            explanation = (
                "請等待其他執行程序釋放工作流程鎖；目前不可核准、展示或確認交付。"
            )
        elif current == "WAITING_FOR_DELIVERY":
            title = "正在驗證並處理交付" if prepared else "正在準備交付資料"
            explanation = (
                "背景工作尚未結束，完成後會更新結果；目前請等待，交付確認暫停使用。"
            )
        elif status.get("pause_requested"):
            title = "目前步驟完成後暫停"
            explanation = "已收到停止請求，正在等待目前步驟安全完成，不會中斷正在進行的模型或測試。"
        else:
            title = "執行中"
            explanation = f"目前正在處理「{stage_labels.get(stage, stage)}」，背景工作結束後會更新狀態。"
        if not status.get("pause_requested") and current not in {
            "WAITING_FOR_OWNER",
            "WAITING_FOR_DELIVERY",
            "COMPLETED",
            "FAILED",
        }:
            button("目前步驟完成後停止", "stop", primary=True)
    elif current == "WAITING_FOR_OWNER":
        # Only the current verdict prefix and its recorded transition are evidence.
        approval_prefix = (
            "Architect approved the plan. The original request requires "
            "owner approval before implementation."
        )
        first_line = reason.splitlines()[0] if reason else ""
        approved = (
            source == "Architect"
            and status.get("plan_requires_owner_approval") is True
            and first_line == approval_prefix
            and resume == pass_state == "IMPLEMENTING"
        )
        rejected = (
            (
                source in {"Architect", "Manager"}
                and first_line == "PLAN_STATUS: REJECTED"
                and resume == "DEVELOPING_PLAN"
                and pass_state == "IMPLEMENTING"
            )
            or (
                source == "Reviewer"
                and first_line == "REJECTED"
                and resume == "IMPLEMENTING"
                and pass_state == "IMPLEMENTING"
            )
            or (
                source == "QA"
                and resume == "IMPLEMENTING"
                and pass_state == "REVIEWING_CODE"
                and (
                    first_line == "QA_STATUS: FAILED"
                    or re.match(
                        r"\A## [^\n]+ \([^\n()]+\)\nQA_STATUS: FAILED(?:\n|$)", reason
                    )
                )
            )
        )
        qa_environment = (
            source == "QA"
            and resume == pass_state == "TESTING"
            and (
                re.match(
                    r"\ATest command failed to launch \(exit (?:126|127)\), so no tests were executed\.",
                    reason,
                )
                or reason.startswith(
                    "QA reports that test failures/skips are caused by a missing "
                    "environment dependency, not a code bug."
                )
            )
        )
        if approved:
            title = "計畫已通過，等待擁有者核准實作"
            explanation = (
                "Architect 已通過目前計畫。核准後將進入實作，不會重新修訂計畫。"
            )
            button("核准實作", "approve", primary=True)
        elif rejected:
            title = "審核未通過，等待修訂"
            explanation = (
                "提交修訂後會重新撰寫計畫，再進行審核。"
                if source in {"Architect", "Manager"}
                else "提交修訂後會建立既有人工修訂任務，回到實作流程。"
            )
            button("提交修訂", "review", {"decision": "revise"}, primary=True)
        elif qa_environment:
            title = "測試環境待處理，尚未完成驗證"
            explanation = "請先處理報告中的測試啟動或依賴問題，再重試 QA 驗證；此操作重跑 TESTING，不建立修復任務。"
            button("重試 QA 驗證", "approve", primary=True)
        elif source in {"Architect", "Manager", "Reviewer", "QA"}:
            title = "等待擁有者檢視，目前資料不足以判定審核結果"
            explanation = (
                "請先閱讀目前原因與完整報告。下列次要操作的效果不同，請明確選擇。"
            )
        else:
            title = "已暫停，等待擁有者決策"
            label = (
                "重新產生需求"
                if source == "Requirements"
                else "重試 QA 驗證"
                if source == "QA_RESPONSE"
                else "繼續目前任務"
            )
            explanation = (
                "將重新產生需求並回到規劃階段。"
                if source == "Requirements"
                else (
                    "QA 回覆未符合契約，重試將回到 TESTING，不代表測試通過。"
                    if source == "QA_RESPONSE"
                    else "繼續後會依已保存的恢復階段執行，既有路由限制仍然適用。"
                )
            )
            button(label, "continue", primary=True)
            if source in {"Developer", "Orchestrator"}:
                button(
                    "清除失敗路由並重試",
                    "continue",
                    {"clear_route_failures": True},
                    "將重新嘗試已因回覆格式失敗的模型，可能再次消耗額度。確定重試？",
                )
        if source in {"Architect", "Manager", "Reviewer", "QA"}:
            if not rejected and not qa_environment:
                button("依審核意見修訂", "review", {"decision": "revise"})
            if not approved and not qa_environment:
                button(
                    "接受目前結果並繼續",
                    "review",
                    {"decision": "pass"},
                    "此操作會覆核本次否決並沿通過路徑繼續，確定接受？",
                )
            if not approved and not rejected and not qa_environment:
                button(
                    "依既有來源規則繼續（將修訂）",
                    "continue",
                    confirmation="此來源的繼續操作會提交修訂，並不代表核准。確定繼續？",
                )
        button(
            "拒絕本次結果",
            "review",
            {"decision": "reject"},
            "拒絕會將本次流程標記為失敗，確定拒絕？",
        )
    elif current == "WAITING_FOR_DELIVERY":
        if not prepared:
            title = "準備交付資料中"
            explanation = (
                "展示前會先完成最終報告、CHANGELOG 與既有交付成果；準備完成後才可確認。"
            )
        elif valid_preview:
            title = "已展示 Git 影響，等待二次確認"
            explanation = "請檢視下方展示內容與快照 ID。明確確認後，系統仍會重新驗證，再執行既有 Git 交付。"
        else:
            title = "等待展示 Git 影響"
            explanation = "請先展示並檢視目前 Git 影響，再另行確認交付。"
    elif current == "COMPLETED":
        if status.get("workflow_mode") == "research":
            title = "研究已完成"
            explanation = "研究結果可由下方報告查看，此流程不需要 Git 交付確認。"
        elif delivered:
            title = "已完成交付"
            explanation = "既有交付流程已完成，詳細結果請查看最終報告。"
        else:
            title = "流程已完成，交付結果待檢視"
            explanation = "目前資料不足以確認 Git 已交付，請查看最終報告與交付紀錄。"
    elif current == "FAILED":
        title = "流程失敗"
        explanation = "請先查看執行錯誤與報告，再提交新任務或明確重置。"
    elif not can_submit:
        button("繼續執行", "continue", primary=True)

    if can_submit:
        title = title if current in {"COMPLETED", "FAILED"} else "提交新任務"
        explanation += " 可在任務輸入區提交新需求，開始新的單一任務流程。"

    if current == "WAITING_FOR_DELIVERY":
        button(
            "準備並展示 Git 影響" if not prepared else "展示 Git 影響",
            "deliver",
            primary=not busy and not valid_preview,
            enabled=not busy,
            target=display_buttons,
        )
    if presented and current == "WAITING_FOR_DELIVERY":
        button(
            "確認二次交付",
            "deliver",
            primary=not busy and current == "WAITING_FOR_DELIVERY" and valid_preview,
            enabled=not busy and current == "WAITING_FOR_DELIVERY" and valid_preview,
            target=delivery_buttons,
            identity="confirm-delivery",
        )
    if not busy:
        button(
            "重置任務",
            "reset",
            confirmation="重置會清除已存任務內容並清理目前 worktree，且需要重新提交任務。確定重置？",
            target=reset_buttons,
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
            "</summary><pre>" + _report_html(description) + "</pre></details></li>"
        )
    report_names = {
        "review": "人工審核報告",
        "tests": "測試結果",
        "qa": "QA 報告",
        "final": "最終報告",
        "plan": "實作計畫",
    }
    report_html = "".join(
        f'<details data-report="{_safe_text(name)}" data-detail="report-{_safe_text(name)}">'
        f"<summary>{_safe_text(report_names.get(name, name))}</summary>"
        f"<pre>{_report_html(value)}</pre></details>"
        for name, value in reports.items()
        if value
    )
    git_html = (
        '<details data-detail="git-impact" open><summary>Git 影響（Git impact）</summary>'
        "<pre>"
        + _report_html(json.dumps(git, ensure_ascii=False, indent=2))
        + "</pre>"
        + f'<p class="muted">快照 ID：{_safe_text(snapshot_id or "尚無展示")}</p>'
        + f'<div id="delivery-actions" class="actions">{"".join(delivery_buttons)}</div>'
        + "</details>"
        if git
        else '<p class="muted">尚未展示 Git 影響。</p><div id="delivery-actions" class="actions"></div>'
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
<title>AI Company Orchestrator</title>
<style>
:root{{color-scheme:light;--bg:#F8FAFC;--card:#FFFFFF;--text:#0F172A;--muted:#475569;--border:#CBD5E1;--primary:#1E3A5F;--error:#DC2626}}
*{{box-sizing:border-box}}
body{{font:16px/1.5 system-ui,sans-serif;color:var(--text);background:var(--bg);max-width:1200px;margin:32px auto;padding:0 24px}}
h1{{font-size:28px;margin:0 0 24px}} h2{{font-size:20px;margin:0 0 16px}} h3{{font-size:18px}}
p{{margin:8px 0 16px}} .muted{{color:var(--muted);font-size:14px;overflow-wrap:anywhere}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:24px;margin-bottom:24px;min-width:0}}
.overview{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:24px}}
dl{{margin:0}} dt{{font-size:14px;color:var(--muted)}} dd{{margin:4px 0 16px;overflow-wrap:anywhere}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:16px;border-radius:8px;max-height:32rem;overflow:auto;font-size:14px}}
button{{font:inherit;min-height:44px;padding:8px 16px;border:1px solid var(--border);border-radius:8px;cursor:pointer}}
.primary{{background:var(--primary);color:white;border-color:var(--primary);font-weight:600}}
.secondary{{background:var(--card);color:var(--text)}}
button:disabled{{opacity:.55;cursor:not-allowed}}
button:hover:enabled{{box-shadow:0 0 0 1px var(--primary)}}
:focus-visible{{outline:3px solid #2563EB;outline-offset:3px}}
textarea{{display:block;width:100%;min-height:160px;font:inherit;line-height:1.5;border:1px solid var(--border);border-radius:8px;padding:12px;margin:8px 0 16px}}
label{{font-weight:600}} .actions{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}}
.badge{{font-weight:600}} ul{{padding:0;list-style:none}} li{{border-top:1px solid var(--border);padding:16px 0}}
.task-heading{{display:flex;flex-wrap:wrap;gap:16px}} details{{margin:12px 0}} summary{{cursor:pointer;overflow-wrap:anywhere}}
.feedback{{color:var(--muted);overflow-wrap:anywhere}} .error{{color:var(--error)}}
#reset-area{{border-top:1px solid var(--border);padding-top:24px}} [hidden]{{display:none!important}}
@media(max-width:767px){{body{{margin:16px auto;padding:0 16px}} .overview{{grid-template-columns:1fr;gap:0}} .card{{padding:16px}} button{{max-width:100%}}}}
</style>
</head>
<body data-csrf="{_safe_text(status.get('csrf', ''))}" data-busy-message="{_safe_text(explanation if busy else '')}">
<header>
<h1>AI Company Orchestrator</h1>
</header>
<main>
<div class="overview">
<section class="card" aria-labelledby="overview-title">
<h2 id="overview-title">任務概況</h2>
<div id="workflow-status">
<dl>
<dt>專案</dt><dd>{_safe_text(status.get("project_name", "目前專案"))}</dd>
<dt>目前階段</dt><dd>{_safe_text(stage_labels.get(stage, stage))} <small>({_safe_text(stage)})</small></dd>
<dt>任務完成數</dt><dd>{_safe_text(progress.get("completed_tasks", 0))} / {_safe_text(progress.get("total_tasks", len(tasks)))}</dd>
</dl>
<p>狀態：<span id="state" class="badge">{_safe_text(current)}</span></p>
<p class="muted">執行狀態：{_safe_text(phase)}</p>
<p class="error" role="alert">{_safe_text(error)}</p>
</div>
<p id="workflow-reading-status" class="feedback" role="status" aria-atomic="true"></p>
<dl><dt>連線狀態</dt><dd id="connection-status">已載入，等待背景更新</dd>
<dt>最後成功更新時間</dt><dd><p id="last-refresh" role="status"></p></dd></dl>
<p id="refresh-error" class="error" role="status"></p>
</section>
<section class="card" aria-labelledby="next-title">
<h2 id="next-title">下一步</h2>
<div id="decision-copy"><h3>{_safe_text(title)}</h3><p>{_safe_text(explanation)}</p></div>
<p id="decision-status" class="feedback" role="status"></p>
<div id="actions" class="actions">{''.join(buttons)}</div>
<form id="task-form" {'' if can_submit else 'hidden'}>
<label for="task-prompt">新任務需求</label>
<textarea id="task-prompt" name="prompt" maxlength="100000" required placeholder="輸入新任務" aria-describedby="prompt-help"></textarea>
<p id="prompt-help" class="muted">一次處理一個任務，請描述預期結果與限制。</p>
<button type="submit" data-action="task{{}}" data-enabled="{str(can_submit).lower()}" class="{'primary' if can_submit else 'secondary'}" {'' if can_submit else 'disabled'}>提交任務</button>
</form>
<p id="action-feedback" class="feedback" role="status" aria-atomic="true"></p>
</section>
</div>
<section class="card" aria-labelledby="tasks-title">
<h2 id="tasks-title">任務清單</h2>
<p id="tasks-reading-status" class="feedback" role="status"></p>
<ul id="tasks">{''.join(task_items)}</ul>
</section>
<section class="card" aria-labelledby="delivery-title">
<h2 id="delivery-title">交付展示與確認</h2>
<div id="display-actions" class="actions">{''.join(display_buttons)}</div>
<div id="git" data-snapshot-id="{_safe_text(snapshot_id)}" data-presented="{str(presented).lower()}" data-validation-error="{_safe_text(validation_error)}" data-delivered="{str(delivered).lower()}">
<p id="git-reading-status" class="feedback" role="status"></p>
<div data-preview>{git_html}</div>
</div>
<p id="delivery-feedback" class="feedback" role="status" aria-atomic="true"></p>
</section>
<section class="card" aria-labelledby="reports-title">
<h2 id="reports-title">報告</h2><p class="muted">展開查看完整內容；報告可能屬於先前階段，請以目前狀態及原因判斷下一步。</p>
<p id="review-reading-status" class="feedback" role="status"></p>
<details id="current-review" data-detail="current-review"><summary>目前審核原因：{_safe_text(source or "尚無來源")}</summary>
<pre id="review-reason">{_report_html(reason)}</pre></details>
<p id="reports-reading-status" class="feedback" role="status"></p>
<div id="reports">{report_html}</div>
</section>
<section id="reset-area" aria-labelledby="reset-title">
<h2 id="reset-title">任務管理</h2>
<div id="reset-actions" class="actions">{''.join(reset_buttons)}</div>
</section>
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
            "/api/continue",
            "/api/approve",
            "/api/review",
            "/api/reset",
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
                result = self.service.review(str(body.get("decision", "")))
            elif path == "/api/reset":
                result = self.service.reset()
            else:
                result = self.service.delivery(
                    bool(body.get("confirm", False)), body.get("snapshot_id")
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
                "continue": ["continue"],
                "pause": ["pause", "--source", "gui"],
                "pause_cancel": ["pause", "--cancel"],
                "review_pass": ["review", "pass"],
                "reject": ["reject"],
            }
            if action == "deliver":
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
