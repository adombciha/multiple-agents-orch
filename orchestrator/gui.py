from __future__ import annotations

import hmac
import json
import re
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from orchestrator.core.runlock import RunLock
from orchestrator.core.status import read_snapshot

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


def serve(orchestrator, port: int = 8765):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("GUI port must be between 1 and 65535.")
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(orchestrator, token, port)
    )
    server.daemon_threads = True
    print(f"Orchestrator GUI: http://127.0.0.1:{port}/?token={token}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
