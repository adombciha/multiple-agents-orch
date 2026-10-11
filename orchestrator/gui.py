from __future__ import annotations

import argparse
import hashlib
import html
import json
import secrets
import threading
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from orchestrator.core.state import AgentOrchestrator
from orchestrator.main import (
    apply_approve,
    apply_review_decision,
    archive_previous_run,
    clear_run_state,
    inline_referenced_markdown,
)


HOST = "127.0.0.1"
MAX_BODY_BYTES = 256 * 1024
ALLOWED_ACTIONS = {
    "/api/task",
    "/api/stop",
    "/api/continue",
    "/api/approve",
    "/api/review",
    "/api/deliver",
}
READ_ROUTES = {"/", "/api/status"}
STATE_CHANGING_METHODS = {"POST"}
SAFE_METHODS = {"GET", "HEAD", "POST"}


def _delivery_snapshot_id(snapshot: dict) -> str:
    payload = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
                self.orchestrator.run_to_end()
        except Exception as error:
            if prepared:
                raise
            if startup_error is not None:
                startup_error.append(error)
            if started:
                started.set()
        finally:
            with self.worker_lock:
                self.worker = None

    def _start_worker(self, prepare=None) -> bool:
        started = threading.Event()
        startup_error = []
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
        stage = state.get("stage", state.get("state", "PLANNING"))
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
            "stage": stage,
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
            "delivery_preview_presented": bool(
                state.get("delivery_preview_presented")
            ),
            "presented_delivery_snapshot": state.get(
                "presented_delivery_snapshot", {}
            ),
            "delivery_snapshot_id": (
                _delivery_snapshot_id(preview) if isinstance(preview, dict) and preview else ""
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

    def _worker_is_running(self) -> bool:
        with self.worker_lock:
            return bool(self.worker and self.worker.is_alive())

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
                decision = (
                    "revise"
                    if source in {"Architect", "Manager", "Reviewer", "QA"}
                    else "approve"
                )
                if decision == "approve":
                    if not apply_approve(self.orchestrator):
                        raise RuntimeError(
                            "Developer route contract failures must be cleared before retry."
                        )
                else:
                    max_revisions = self.orchestrator.config.get(
                        "max_revisions", 2
                    )
                    apply_review_decision(
                        self.orchestrator,
                        "revise",
                        self.orchestrator.state.get("code_revisions", 0)
                        >= max_revisions,
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
                    preview = self.orchestrator.create_delivery_preview("merge")
                    preview = self.orchestrator.present_delivery_preview(preview)
                    self.orchestrator.state["presented_delivery_snapshot"] = {
                        "diff_stat": preview.get("diff_stat", ""),
                        "git_status": preview.get("git_status", ""),
                        "source_commit": preview.get("source_commit"),
                        "target_commit": preview.get("target_commit"),
                        "scheduled_operation": preview.get("scheduled_operation"),
                    }
                    self.orchestrator.save_state()
                    result = self.status()
                    result["delivery_confirmation_required"] = True
                    result["git"] = preview
                    return result

            def prepare():
                self._load()
                if self.orchestrator.state.get("state") != "WAITING_FOR_DELIVERY":
                    raise RuntimeError("Workflow is not waiting for delivery.")
                preview = self.orchestrator.get_delivery_preview()
                if (
                    not self.orchestrator.state.get("delivery_preview_presented")
                    or snapshot_id != _delivery_snapshot_id(preview)
                ):
                    raise RuntimeError(
                        "The confirmed Git impact does not match the displayed snapshot; review it again."
                    )
                try:
                    self.orchestrator.confirm_delivery(preview, "merge")
                except RuntimeError as error:
                    self.orchestrator.create_delivery_preview("merge")
                    self.orchestrator.state.pop("presented_delivery_snapshot", None)
                    self.orchestrator.save_state()
                    raise RuntimeError(
                        "Git impact changed or the saved preview is invalid; review the new snapshot and confirm again."
                    ) from error

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
    )
    report_html = "".join(
        f"<details><summary>{_safe_text(name)}</summary>"
        f"<pre>{_report_html(value)}</pre></details>"
        for name, value in reports.items()
        if value
    )
    stage = _safe_text(status.get("stage", status.get("state", "")))
    progress = status.get("progress", {})
    progress_text = (
        _safe_text(progress.get("completed_tasks", 0))
        + "/"
        + _safe_text(progress.get("total_tasks", len(tasks)))
        + " tasks; plan revisions "
        + _safe_text(progress.get("plan_revisions", 0))
        + "; code revisions "
        + _safe_text(progress.get("code_revisions", 0))
    )
    review = status.get("review", {})
    review_source = _safe_text(review.get("source", ""))
    reason = _safe_text(review.get("reason", ""))
    next_action = _safe_text(
        status.get("next_action", "Wait for the worker to advance the workflow.")
    )
    tests = _report_html(str(status.get("tests", "") or "尚無測試結果"))
    git = status.get("git", {})
    git_html = (
        "<h3>Git impact</h3><pre>"
        + _report_html(json.dumps(git, ensure_ascii=False, indent=2))
        + "</pre>"
        if git
        else ""
    )
    csrf = _safe_text(status.get("csrf", ""))
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
<p>階段：{stage}</p>
<p>狀態：<span id="state" class="badge">{_safe_text(status.get("state", ""))}</span></p>
<p>進度：{progress_text}</p>
<p>審核來源：{review_source}</p>
<p>審核原因：{reason}</p>
<p>執行中：{_safe_text(status.get("worker_running", False))}　
暫停狀態：{_safe_text(status.get("pause_status", "未請求"))}</p>
<h2>測試</h2><pre>{tests}</pre>
<p>下一動作：{next_action}</p>
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
<h2>任務與進度</h2><ul id="tasks">{task_html}</ul>
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
setTimeout(() => location.reload(), 3000);
</script>
</body>
</html>"""


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "OrchestratorGUI/1.0"

    @property
    def service(self) -> GuiService:
        return self.server.service  # type: ignore[attr-defined]

    def _origin_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        expected_host = f"{HOST}:{self.server.server_port}"
        expected_origin = f"http://{HOST}:{self.server.server_port}"
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
        if self.command not in SAFE_METHODS or not self._origin_allowed():
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
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ValueError("JSON object required.")
        return value

    def _result(self, value: dict) -> dict:
        value = dict(value)
        value["csrf"] = self.service.csrf_token
        return value

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
        if path not in ALLOWED_ACTIONS:
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
            f"gui_csrf={self.service.csrf_token}; Path=/; HttpOnly; "
            "SameSite=Strict"
        )

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(workspace: Path, port: int) -> None:
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise ValueError("port must be between 1 and 65535")
    service = GuiService(workspace)
    server = ThreadingHTTPServer((HOST, port), RequestHandler)
    server.service = service  # type: ignore[attr-defined]
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


if __name__ == "__main__":
    main()
