from __future__ import annotations

import argparse
import hmac
import html
import json
import re
import secrets
import subprocess
import sys
import threading
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from orchestrator.core.state import AgentOrchestrator, delivery_snapshot_id
from orchestrator.main import (
    apply_approve,
    apply_review_decision,
    archive_previous_run,
    inline_referenced_markdown,
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
        prepared = False
        try:
            with self.orchestrator.execution_lock():
                if prepare:
                    prepare()
                prepared = True
                if started:
                    started.set()
                if self.orchestrator.state.get("state") != "COMPLETED":
                    self.orchestrator.run_to_end()
        except Exception as error:
            if not prepared and startup_error is not None:
                startup_error.append(error)
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
            raise RuntimeError("Workflow worker did not acquire the execution lock.")
        if startup_error:
            raise RuntimeError(f"Could not start workflow: {startup_error[0]}")
        return True

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
        return {
            "state": state.get("state", "PLANNING"),
            "stage": state.get("stage", state.get("state", "PLANNING")),
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
            if state.get("state") not in {"PLANNING", "COMPLETED", "FAILED"}:
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

    def continue_task(self) -> dict:
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
                source = self.orchestrator.state.get("human_review_source")
                if source in {"Architect", "Manager", "Reviewer", "QA"}:
                    max_revisions = self.orchestrator.config.get("max_revisions", 2)
                    apply_review_decision(
                        self.orchestrator,
                        "revise",
                        self.orchestrator.state.get("code_revisions", 0)
                        >= max_revisions,
                    )
                elif not apply_approve(self.orchestrator):
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

    def delivery(self, confirm: bool, snapshot_id: str | None = None) -> dict:
        with self._operation():
            if self._worker_is_running():
                raise RuntimeError("A workflow worker is already running.")
            if not confirm:
                with self.orchestrator.execution_lock():
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
                    result["git"] = preview
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


def render_page(status: dict) -> str:
    reports = status.get("reports", {})
    tasks = status.get("progress", {}).get("tasks", [])
    task_html = "".join(
        "<li><strong>"
        + _safe_text(task.get("id", ""))
        + "</strong> ["
        + _safe_text(task.get("status", ""))
        + "] "
        + _safe_text(task.get("description", ""))
        + "</li>"
        for task in tasks
        if isinstance(task, dict)
    )
    report_html = "".join(
        f"<details><summary>{_safe_text(name)}</summary>"
        f"<pre>{_report_html(value)}</pre></details>"
        for name, value in reports.items()
        if value
    )
    progress = status.get("progress", {})
    review = status.get("review", {})
    git = status.get("git", {})
    git_html = (
        "<h3>Git impact</h3><pre>"
        + _report_html(json.dumps(git, ensure_ascii=False, indent=2))
        + "</pre>"
        if git
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Company Orchestrator</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
pre{{white-space:pre-wrap;background:#f4f4f4;padding:1rem;overflow:auto}}
button{{margin:.25rem;padding:.5rem 1rem}} textarea{{width:100%;min-height:8rem}}
.badge{{font-weight:bold}} details{{margin:1rem 0}}
</style>
</head>
<body>
<h1>AI Company Orchestrator</h1>
<p>階段：{_safe_text(status.get("stage", status.get("state", "")))}</p>
<p>狀態：<span id="state" class="badge">{_safe_text(status.get("state", ""))}</span></p>
<p>進度：{_safe_text(progress.get("completed_tasks", 0))}/{_safe_text(progress.get("total_tasks", len(tasks)))} tasks</p>
<p>審核原因：{_safe_text(review.get("reason", ""))}</p>
<p>下一動作：{_safe_text(status.get("next_action", ""))}</p>
<h2>Tasks</h2><ul>{task_html}</ul>
<form id="task-form">
<textarea name="prompt" placeholder="輸入新任務"></textarea><br>
<button>提交任務</button>
</form>
<button onclick="post('/api/stop')">停止</button>
<button onclick="post('/api/continue')">繼續</button>
<button onclick="post('/api/approve')">通過審核</button>
<button onclick="post('/api/review',{{decision:'revise'}})">審核未通過，優先修訂</button>
<button onclick="post('/api/deliver')">展示 Git 影響</button>
<button onclick="post('/api/deliver',{{confirm:true,snapshot_id:deliverySnapshotId}})">確認二次交付</button>
<div id="git">{git_html}</div>
<h2>報告</h2>{report_html}
<script>
const csrf = {json.dumps(status.get("csrf", ""))};
const deliverySnapshotId = {json.dumps(status.get("delivery_snapshot_id", ""))};
async function post(url, body={{}}) {{
  const response = await fetch(url, {{
    method:'POST', credentials:'same-origin',
    headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},
    body:JSON.stringify(body)
  }});
  const data = await response.json();
  if (!response.ok) alert(data.error || '操作失敗');
  location.reload();
}}
document.getElementById('task-form').onsubmit = async (event) => {{
  event.preventDefault();
  const prompt = new FormData(event.target).get('prompt');
  await post('/api/task', {{prompt}});
}};
function refreshWhenIdle() {{
  const promptInput = document.querySelector('#task-form textarea');
  if (document.activeElement === promptInput || promptInput.value) {{
    setTimeout(refreshWhenIdle, 3000);
    return;
  }}
  location.reload();
}}
setTimeout(refreshWhenIdle, 3000);
</script>
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

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
                self.wfile.write(body)
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
                result = self.service.continue_task()
            elif path == "/api/approve":
                result = self.service.approve()
            elif path == "/api/review":
                result = self.service.review(str(body.get("decision", "")))
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
