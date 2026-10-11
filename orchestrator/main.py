import os
import re
import sys
import argparse
import json
import shutil
import time

import html
import hmac
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import urlopen

from http.cookies import SimpleCookie
from pathlib import Path
from orchestrator.core.state import (
    AgentOrchestrator,
    Colors,
    log_success,
    log_info,
    log_warning,
    log_error,
    log_header,
)
from orchestrator.core import scoring


REFERENCE_BLOCK_START = (
    "<!-- REFERENCE-ONLY:START (context, not a file to create or edit) -->"
)
REFERENCE_BLOCK_END = "<!-- REFERENCE-ONLY:END -->"


def inline_referenced_markdown(prompt: str, workspace: Path) -> str:
    """Some backends in manager/planning calls only ever see the
    prompt text, not the filesystem -- a path mentioned in the prompt is just
    words to them. If the prompt references an existing .md file by relative
    path, append that file's actual content so every backend sees it, not
    just the ones with their own file-reading tools.

    The appended content is wrapped in REFERENCE_BLOCK_START/END markers.
    References are masked outside those markers so DEVELOPING_PLAN does not
    mistake background documents for edit targets. Paths on an explicit
    "Target files:" line remain visible to the planner."""
    pattern = re.compile(r"[A-Za-z0-9_.\-/]+\.md(?![A-Za-z0-9_])")
    referenced = []
    for name in pattern.findall(prompt):
        path = Path(name)
        if path.is_absolute():
            continue
        resolved = (workspace / path).resolve()
        if workspace != resolved and workspace not in resolved.parents:
            continue
        if resolved.is_file() and resolved not in referenced:
            referenced.append(resolved)
    if not referenced:
        return prompt

    def _mask(match: re.Match) -> str:
        line_start = prompt.rfind("\n", 0, match.start()) + 1
        if re.match(r"\s*Target files\s*:", prompt[line_start : match.start()], re.I):
            return match.group(0)
        path = Path(match.group(0))
        resolved = (workspace / path).resolve()
        return (
            f"'{resolved.stem}' (see reference material below)"
            if resolved in referenced
            else match.group(0)
        )

    masked_prompt = pattern.sub(_mask, prompt)
    appended = f"\n\n{REFERENCE_BLOCK_START}\nReferenced file contents (inlined automatically so every backend can see them; this block is context, and edit targets are identified outside it):\n"
    for path in referenced:
        appended += f"\n## {path.relative_to(workspace)}\n\n{path.read_text(encoding='utf-8')}\n"
    appended += f"\n{REFERENCE_BLOCK_END}\n"
    return masked_prompt + appended


def clear_run_state(orchestrator: AgentOrchestrator, target_state: str) -> None:
    """Shared by `reset` and `reject`: wipe revision counters, staffing, and
    the worktree so a run can start over from `target_state`. Does not touch
    the saved request prompt -- callers decide separately whether to keep it
    (`reject`, so the same ask gets re-planned) or clear it too (`reset`,
    so a fresh `start` is required)."""
    if orchestrator.cleanup_worktree(merge=False) is False:
        raise RuntimeError("Git worktree cleanup failed; run state was preserved.")
    orchestrator.state["state"] = target_state
    orchestrator.state["plan_revisions"] = 0
    orchestrator.state["code_revisions"] = 0
    orchestrator.state["model_tier_indices"] = {
        "developer": 0,
        "reviewer": 0,
        "manager": 0,
        "qa": 0,
    }
    orchestrator.state["specialists"] = []
    orchestrator.state["staffing"] = {}
    orchestrator.state["worker_assignments"] = {}
    orchestrator.state["last_developer_role"] = "developer_senior"
    orchestrator.state["last_qa_level"] = "senior"
    orchestrator.state["developer_promotions"] = {}
    orchestrator.state["task_developer_promotions"] = {}
    orchestrator.state["task_backend_overrides"] = {}
    orchestrator.state["quota_exhausted_backends"] = {}
    orchestrator.state["failed_model_routes"] = []
    orchestrator.state["task_failed_model_routes"] = {}
    orchestrator.state["tasks"] = []
    orchestrator.state["active_task_id"] = None


def _all_available_developer_routes_failed_contract(
    orchestrator, task_id: str
) -> bool:
    from orchestrator.core import backends

    task = next(
        (task for task in orchestrator.state.get("tasks", []) if task.get("id") == task_id),
        {},
    )
    assignment = (
        orchestrator.state.get("worker_assignments", {}).get("rd", {}).get(task_id, "")
    )
    level = (
        assignment.split("-")[1]
        if assignment.count("-") >= 2
        else task.get("rd_level", task.get("assignee_level", "senior"))
    )
    role = f"developer_{level}"
    role = orchestrator.state.get("task_developer_promotions", {}).get(
        task_id, {}
    ).get(role, role)
    configured = list(orchestrator.config.get("role_model_routes", {}).get(role, []))
    override = orchestrator.state.get("task_backend_overrides", {}).get(task_id)
    if override:
        configured.sort(key=lambda route: route[0] != override)

    routes = []
    ollama_added = False
    for backend, model in configured:
        if not backends.backend_available(orchestrator, backend):
            continue
        if backend == "ollama":
            if ollama_added:
                continue
            ollama_added = True
            candidates = sorted(backends.discover_ollama_chat_models(orchestrator))
            ranked = (
                backends.route_ollama_model_with_tev1(
                    orchestrator,
                    role,
                    candidates,
                    task.get("implementation_scope") or task.get("description", ""),
                )
                if orchestrator.config.get("ollama_router_enabled", True)
                else None
            )
            try:
                limit = max(
                    0, int(orchestrator.config.get("max_ollama_routes_per_call", 2))
                )
            except (TypeError, ValueError):
                limit = 2
            routes.extend([["ollama", item] for item in (ranked or candidates)[:limit]])
        else:
            routes.append([backend, model])
    available = [
        f"{backend}/{model}"
        for backend, model in routes
        if backends.backend_available(orchestrator, backend)
    ]
    failed = orchestrator.state.get("task_contract_failures", {}).get(task_id, {})
    return bool(available) and all(route in failed for route in available)


def clear_task_route_failures(
    orchestrator, task_id: str, clear_contract: bool
) -> None:
    old_failures = orchestrator.state.setdefault("task_failed_model_routes", {}).pop(
        task_id, []
    )
    if old_failures:
        log_info(
            f"Cleared {len(old_failures)} cached route failures for {task_id} before retry."
        )
    if clear_contract:
        contract_failures = orchestrator.state.setdefault(
            "task_contract_failures", {}
        ).pop(task_id, {})
        if contract_failures:
            log_info(
                f"Cleared {len(contract_failures)} contract-failed routes for {task_id}."
            )


def apply_approve(
    orchestrator: AgentOrchestrator, clear_route_failures: bool = False
) -> bool:
    """Resume a pause without retrying contract-failed routes unless requested."""
    resume_state = orchestrator.state.get(
        "pass_state", orchestrator.state.get("resume_state", "REVIEWING_CODE")
    )
    if resume_state == "FAILED":
        resume_state = "IMPLEMENTING"
    active_task_id = orchestrator.state.get("active_task_id")
    if active_task_id:
        source = orchestrator.state.get("human_review_source")
        if (
            source in {"Developer", "Orchestrator"}
            and not clear_route_failures
            and _all_available_developer_routes_failed_contract(
                orchestrator, active_task_id
            )
        ):
            log_error(
                "Every available Developer route for this task has failed its output contract. "
                "Use 'reject' to re-plan, or approve/continue with --clear-route-failures to retry them."
            )
            return False
        clear_task_route_failures(
            orchestrator, active_task_id, clear_contract=clear_route_failures
        )
        if source in {"Developer", "Orchestrator"}:
            for task in orchestrator.state.get("tasks", []):
                if task.get("id") == active_task_id:
                    task["revisions"] = 0
                    break
            orchestrator.state.setdefault("task_developer_calls", {})[
                active_task_id
            ] = 0
            orchestrator.state.setdefault("task_error_signatures", {}).pop(
                active_task_id, None
            )
    orchestrator.clear_pause_request()
    orchestrator.state["state"] = resume_state
    orchestrator.state.pop("human_review_source", None)
    orchestrator.state.pop("resume_state", None)
    orchestrator.save_state()
    log_success(f"Owner approval recorded; workflow resumed at {resume_state}.")
    return True


def apply_review_decision(
    orchestrator: AgentOrchestrator, decision: str, reset_revisions: bool = False
) -> None:
    """Apply a pass/revise/reject decision to a WAITING_FOR_OWNER pause.
    Pass and revise resume the workflow by clearing any pending pause request.
    `reset_revisions` (revise only, non-Architect/Manager source): clear
    code_revisions first so the auto-revise loop gets a fresh budget instead
    of immediately re-pausing if it was already at max_revisions."""
    if decision == "pass":
        next_state = orchestrator.state.get("pass_state", "REVIEWING_CODE")
    elif decision == "revise":
        if orchestrator.state.get("human_review_source") in {"Architect", "Manager"}:
            next_state = "DEVELOPING_PLAN"
            orchestrator.state["plan_revisions"] = 0
        else:
            next_state = "IMPLEMENTING"
            if reset_revisions:
                orchestrator.state["code_revisions"] = 0
            n = orchestrator.state.get("code_revisions", 0) + 1
            orchestrator.state["code_revisions"] = n
            orchestrator.state.setdefault("tasks", []).append(
                {
                    "id": f"HUMAN-REVIEW-{n}",
                    "description": f"Implement human review revisions:\n{orchestrator.state.get('human_review_details', '')[:2000]}",
                    "target_files": orchestrator.relevant_target_files(
                        orchestrator.state.get("human_review_details", "")
                    ),
                    "status": "pending",
                    **orchestrator.fix_task_levels(),
                }
            )
    else:
        next_state = "FAILED"
    if decision == "pass" and orchestrator.state.get("human_review_source") == "QA":
        for qa_level in orchestrator.state.get("qa_dissenting_levels", []):
            qa_role = f"qa_{qa_level}"
            qa_backend = orchestrator.get_backend(qa_role)
            qa_model = orchestrator.get_active_model_for_role(qa_role, qa_backend)
            scoring.record_event(
                orchestrator, "qa", qa_level, qa_backend, qa_model, -0.5
            )
            log_info(
                f"Recorded QA false-positive for qa_{qa_level} after owner override."
            )
    if decision in {"pass", "revise"}:
        orchestrator.clear_pause_request()
    orchestrator.state["state"] = next_state
    for key in (
        "human_review_source",
        "resume_state",
        "pass_state",
        "human_review_details",
        "qa_dissenting_levels",
    ):
        orchestrator.state.pop(key, None)
    orchestrator.save_state()
    log_success(f"Human review '{decision}' recorded; workflow moved to {next_state}.")


def pick_continue_decision(orchestrator: AgentOrchestrator) -> tuple[str, bool]:
    """Decide what `continue` should do with a WAITING_FOR_OWNER pause: an AI
    verdict that rejected something (Architect/Manager/Reviewer/QA) should be
    sent back for another attempt (revise); a Developer (or unrecognized)
    pause just means retries ran out on the current task, so resume it
    (approve). For revise, auto-clear the revision counter if it's already
    at the ceiling so the auto-loop gets a genuine attempt instead of
    immediately re-pausing."""
    source = orchestrator.state.get("human_review_source")
    if source not in {"Architect", "Manager", "Reviewer", "QA"}:
        return "approve", False
    max_rev = orchestrator.config.get("max_revisions", 2)
    at_ceiling = orchestrator.state.get("code_revisions", 0) >= max_rev
    return "revise", at_ceiling


def ollama_available(url: str) -> bool:
    try:
        with urlopen(f"{url.rstrip('/')}/api/tags", timeout=2):
            return True
    except (OSError, URLError):
        return False


def configure_ollama(orchestrator: AgentOrchestrator):
    local_url = orchestrator.config["ollama_url"]
    if ollama_available(local_url):
        log_success(f"Ollama is available at {local_url}")
        orchestrator.state["ollama_reachable"] = True
        orchestrator.save_state()
        return
    windows_url = f"http://{orchestrator.get_windows_host_ip()}:11434"
    if ollama_available(windows_url):
        log_info(f"Windows Ollama detected at {windows_url}")
        if not sys.stdin.isatty() or input(
            "Use this Windows Ollama endpoint? [Y/n] "
        ).strip().lower() not in {"n", "no"}:
            orchestrator.config["ollama_url"] = windows_url
            orchestrator.config_path.write_text(
                json.dumps(orchestrator.config, indent=2), encoding="utf-8"
            )
            log_success(f"Ollama API URL set to {windows_url}")
            orchestrator.state["ollama_reachable"] = True
            orchestrator.save_state()
    else:
        log_error(
            "No Ollama endpoint detected. Start Ollama locally or on the Windows host before running a task."
        )
        orchestrator.state["ollama_reachable"] = False
        orchestrator.save_state()


def archive_previous_run(orchestrator: AgentOrchestrator) -> bool:
    if not orchestrator.state_path.exists():
        return True

    try:
        previous_state = json.loads(
            orchestrator.state_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        log_error("Cannot read workflow state; existing artifacts were preserved.")
        return False

    state = previous_state.get("state")
    if state == "COMPLETED":
        delivery = previous_state.get("delivery")
        if (isinstance(delivery, dict) and not delivery.get("delivered", False)) or (
            orchestrator.ai_dir / "worktree"
        ).exists():
            log_error(
                "The completed workflow has not been delivered; its artifacts were preserved."
            )
            return False
    if state == "PLANNING" and not orchestrator.request_path.exists():
        return True
    if state not in {"COMPLETED", "FAILED"}:
        log_error(
            f"Cannot start a new task while workflow is in progress ({state}). "
            "Use 'stop' to request a safe pause, then 'reject' or 'reset'."
        )
        return False

    sources = [
        orchestrator.state_path,
        orchestrator.final_report_path,
        orchestrator.human_report_path,
        orchestrator.qa_report_path,
        orchestrator.reviewer_output_path,
        orchestrator.developer_output_path,
        orchestrator.specialist_review_path,
        orchestrator.meeting_memory_path,
        orchestrator.test_results_path,
    ]
    existing = [path for path in sources if path.exists()]
    if not existing:
        return True

    archive_dir = (
        orchestrator.workspace / ".ai-company-archive" / str(time.time_ns())
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    for source in existing:
        shutil.copy2(source, archive_dir / source.name)
    log_info(f"Archived previous workflow reports and state to {archive_dir}")
    return True


def request_stop(orchestrator: AgentOrchestrator) -> None:
    if not orchestrator.config_path.exists():
        log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
        return
    orchestrator.request_pause()
    log_success(
        "Stop requested. The workflow will pause safely at the next step boundary."
    )


WEB_PAGE = """<!doctype html>
<html lang="zh-Hant">
<head><meta charset="utf-8"><title>AI Company Orchestrator</title>
<style>
body{font-family:sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}
button{margin:.25rem;padding:.5rem 1rem} pre{white-space:pre-wrap;background:#f4f4f4;padding:1rem}
progress{width:100%}.danger{color:#a00}
</style></head>
<body>
<h1>任務執行</h1>
<form id="start"><textarea name="prompt" rows="5" cols="80" required></textarea><br>
<button>提交任務</button></form>
<div><button id="continue">繼續</button><button id="stop">停止</button>
<button id="deliver">確認交付</button></div>
<p id="summary">載入中…</p><progress id="progress" max="100" value="0"></progress>
<h2>審核與暫停</h2><pre id="review"></pre>
<h2>測試與報告</h2><pre id="reports"></pre>
<script>
let csrf="";
async function api(path, options={}) {
  options.headers=Object.assign({"Content-Type":"application/json","X-CSRF-Token":csrf}, options.headers||{});
  const r=await fetch(path, options), data=await r.json();
  if(!r.ok) throw Error(data.error||"操作失敗");
  return data;
}
async function refresh(){
  try {
    const d=await api("/api/status");
    csrf=d.csrf; document.cookie="orchestrator_csrf="+encodeURIComponent(csrf)+"; SameSite=Strict";
    document.getElementById("summary").textContent=`狀態：${d.state}｜進度：${d.completed}/${d.total}｜繼續動作：${d.next_action}`;
    document.getElementById("progress").value=d.progress;
    document.getElementById("review").textContent=d.review_reason||d.pause_status||"無";
    document.getElementById("reports").textContent=d.reports||"無";
    window.deliverySnapshot=d.delivery_snapshot;
  } catch(e){document.getElementById("summary").textContent=e.message}
}
async function act(path, body){
  try { await api(path,{method:"POST",body:JSON.stringify(body||{})}); await refresh(); }
  catch(e){alert(e.message)}
}
document.getElementById("start").onsubmit=e=>{e.preventDefault();act("/api/start",{prompt:e.target.prompt.value})};
document.getElementById("continue").onclick=()=>act("/api/continue");
document.getElementById("stop").onclick=()=>act("/api/stop");
document.getElementById("deliver").onclick=()=>act("/api/deliver",{snapshot:window.deliverySnapshot});
refresh(); setInterval(refresh,2000);
</script></body></html>"""


def _web_report(orchestrator: AgentOrchestrator) -> str:
    paths = (
        ("測試", orchestrator.test_results_path),
        ("最終報告", orchestrator.final_report_path),
        ("人工報告", orchestrator.human_report_path),
        ("審核輸出", orchestrator.reviewer_output_path),
    )
    chunks = []
    for label, path in paths:
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")[-12000:]
            except OSError:
                content = "無法讀取報告"
            chunks.append(f"{label}:\n{html.escape(content)}")
    return "\n\n".join(chunks)


def _web_snapshot(orchestrator: AgentOrchestrator) -> dict:
    diff_stat, _, git_status = orchestrator.manager._inspect_delivery_state()
    return {"diff_stat": diff_stat, "git_status": git_status}


def _web_status(orchestrator: AgentOrchestrator) -> dict:
    state = orchestrator.state.get("state", "UNKNOWN")
    tasks = orchestrator.state.get("tasks", [])
    completed = sum(task.get("status") == "completed" for task in tasks)
    total = len(tasks)
    reason = orchestrator.state.get("human_review_details", "")
    pause = "已要求停止，將在下一個安全邊界暫停。" if orchestrator.state.get("pause_requested") else ""
    if state == "WAITING_FOR_OWNER":
        next_action = "審核通過、修訂或拒絕"
    elif state == "WAITING_FOR_DELIVERY":
        next_action = "檢視 Git 影響後確認交付"
    elif state in {"COMPLETED", "FAILED"}:
        next_action = "無"
    else:
        next_action = "繼續執行至下一決策點"
    snapshot = _web_snapshot(orchestrator)
    return {
        "state": state,
        "completed": completed,
        "total": total,
        "progress": int(completed * 100 / total) if total else (100 if state == "COMPLETED" else 0),
        "review_reason": html.escape(str(reason)),
        "pause_status": html.escape(pause),
        "reports": _web_report(orchestrator),
        "next_action": next_action,
        "delivery_snapshot": snapshot,
        "csrf": orchestrator.web_csrf_token,
    }


def _web_start_task(orchestrator: AgentOrchestrator, prompt: str) -> None:
    if not prompt.strip():
        raise ValueError("prompt is required")
    if not archive_previous_run(orchestrator):
        raise RuntimeError("Cannot start a task while another workflow is active.")
    orchestrator.clear_run_artifacts()
    orchestrator.init_project()
    orchestrator.load_config_and_state()
    orchestrator.request_path.write_text(prompt, encoding="utf-8")
    orchestrator.state = {
        "state": "PLANNING",
        "plan_revisions": 0,
        "code_revisions": 0,
        "model_tier_indices": {"developer": 0, "reviewer": 0, "manager": 0, "qa": 0},
        "tasks": [], "specialists": [], "staffing": {}, "worker_assignments": {},
        "last_developer_role": "developer_senior", "last_qa_level": "senior",
        "developer_promotions": {}, "task_developer_promotions": {},
        "task_backend_overrides": {}, "quota_exhausted_backends": {},
        "failed_model_routes": [], "task_failed_model_routes": {},
        "visual_image_paths": [], "workflow_mode": "delivery",
        "research_roles": [], "ollama_reachable": orchestrator.state.get("ollama_reachable", True),
    }
    orchestrator.save_state()
    orchestrator.run_to_end()


def serve_web(orchestrator: AgentOrchestrator, port: int) -> None:
    class WebHandler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _allowed(self, write: bool = False) -> bool:
            host = self.headers.get("Host", "").split(":", 1)[0]
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            if host != "127.0.0.1" or (origin and origin != expected_origin):
                return False
            if not write:
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            supplied = self.headers.get("X-CSRF-Token", "")
            return hmac.compare_digest(
                supplied, self.server.csrf_token
            ) and hmac.compare_digest(
                cookie.get("orchestrator_csrf", "").value if cookie.get("orchestrator_csrf") else "",
                self.server.csrf_token,
            )

        def _body(self) -> dict:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self) -> None:
            if not self._allowed():
                self._json(403, {"error": "forbidden"})
                return
            if self.path == "/":
                body = WEB_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", f"orchestrator_csrf={self.server.csrf_token}; HttpOnly; SameSite=Strict")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                with orchestrator.execution_lock():
                    orchestrator.load_config_and_state()
                    self._json(200, _web_status(orchestrator))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._allowed(True):
                self._json(403, {"error": "forbidden"})
                return
            try:
                body = self._body()
                with orchestrator.execution_lock():
                    orchestrator.load_config_and_state()
                    if self.path == "/api/start":
                        _web_start_task(orchestrator, str(body.get("prompt", "")))
                    elif self.path == "/api/continue":
                        state = orchestrator.state.get("state")
                        if state == "WAITING_FOR_OWNER":
                            decision, reset = pick_continue_decision(orchestrator)
                            if decision == "approve":
                                if not apply_approve(orchestrator):
                                    raise RuntimeError(
                                        "Developer route contract failures must be cleared before retry."
                                    )
                            else:
                                apply_review_decision(orchestrator, "revise", reset)
                        elif state == "WAITING_FOR_DELIVERY":
                            raise RuntimeError("delivery confirmation required")
                        orchestrator.run_to_end()
                    elif self.path == "/api/stop":
                        request_stop(orchestrator)
                    elif self.path == "/api/deliver":
                        current = _web_snapshot(orchestrator)
                        presented = body.get("snapshot")
                        if presented != current:
                            raise RuntimeError("Git impact changed; refresh and confirm again")
                        if orchestrator.state.get("state") != "WAITING_FOR_DELIVERY":
                            raise RuntimeError("delivery is not available")
                        orchestrator.state["delivery_confirmed"] = True
                        orchestrator.save_state()
                        orchestrator.step()
                    else:
                        self._json(404, {"error": "not found"})
                        return
                    self._json(200, _web_status(orchestrator))
            except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
                self._json(409, {"error": str(error)})

    orchestrator.init_project()
    orchestrator.load_config_and_state()
    orchestrator.web_csrf_token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", port), WebHandler)
    server.csrf_token = orchestrator.web_csrf_token
    try:
        log_success(f"Web UI listening on http://127.0.0.1:{server.server_port}/")
        server.serve_forever()
    finally:
        server.server_close()


def preview_delivery(orchestrator: AgentOrchestrator) -> None:
    with orchestrator.execution_lock():
        orchestrator.load_config_and_state()
        state = orchestrator.state.get("state")
        if state != "WAITING_FOR_DELIVERY":
            log_error(f"Cannot preview delivery from state: {state}")
            return

        preview = orchestrator.create_delivery_preview()
        orchestrator.present_delivery_preview(preview)
        print(f"Current State: {state}")
        print(
            "Complete Git delivery snapshot:\n"
            + json.dumps(preview, ensure_ascii=False, indent=2)
        )
        log_warning(
            "Delivery confirmation required. Re-run with 'confirm' or "
            "'deliver --confirm' after reviewing the Git impact."
        )


def confirm_delivery(orchestrator: AgentOrchestrator) -> None:
    with orchestrator.execution_lock():
        orchestrator.load_config_and_state()
        state = orchestrator.state.get("state")
        if state != "WAITING_FOR_DELIVERY":
            log_error(f"Cannot confirm delivery from state: {state}")
            return

        preview = orchestrator.get_delivery_preview()
        orchestrator.confirm_delivery(preview)
        orchestrator.step()


def deliver_task(orchestrator: AgentOrchestrator, confirm: bool) -> None:
    if confirm:
        confirm_delivery(orchestrator)
    else:
        preview_delivery(orchestrator)


def main():
    parser = argparse.ArgumentParser(description="Multi-Agent Orchestrator CLI")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    subparsers.add_parser(
        "init", help="Initialize the .ai-company folder and configuration"
    )

    start_parser = subparsers.add_parser("start", help="Start a new multi-agent task")
    start_parser.add_argument(
        "prompt", type=str, help="The development task prompt or request"
    )
    start_parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Screenshot or mockup path for UI/UX visual review; repeat for multiple images",
    )
    start_parser.add_argument(
        "--research",
        action="store_true",
        help="Run Sales and RA research only; skip code delivery roles",
    )

    subparsers.add_parser("step", help="Run the next step in the state machine")
    subparsers.add_parser("run", help="Run the agent loop to completion")
    gui_parser = subparsers.add_parser(
        "gui",
        aliases=["web"],
        help="Serve the local single-task web GUI",
    )
    gui_parser.add_argument("--port", type=int, default=8765)
    subparsers.add_parser(
        "preview", help="Create and show a persistent delivery preview"
    )
    subparsers.add_parser(
        "confirm", help="Confirm the persisted delivery preview and deliver"
    )
    continue_parser = subparsers.add_parser(
        "continue",
        help="Auto-resume from wherever the project is: picks approve or revise "
        "for a paused review (auto-clearing the revision counter if needed) and "
        "keeps running. Use when you don't want to think about which command "
        "applies to the current pause.",
    )
    continue_parser.add_argument(
        "--clear-route-failures",
        action="store_true",
        help="Retry Developer routes previously rejected by the output contract",
    )
    subparsers.add_parser(
        "stop",
        aliases=["pause"],
        help="Request a safe stop at the next workflow step boundary",
    )
    deliver_parser = subparsers.add_parser(
        "deliver", help="Show Git impact and explicitly confirm delivery"
    )
    deliver_parser.add_argument(
        "--confirm",
        "--yes",
        action="store_true",
        dest="confirm",
        help="Confirm the displayed current Git impact and deliver the work",
    )
    subparsers.add_parser(
        "status", help="Get current orchestrator status and logs info"
    )
    approve_parser = subparsers.add_parser(
        "approve", help="Approve a workflow paused for owner review"
    )
    approve_parser.add_argument(
        "--run", action="store_true", help="Continue the workflow after approval"
    )
    approve_parser.add_argument(
        "--clear-route-failures",
        action="store_true",
        help="Retry Developer routes previously rejected by the output contract",
    )
    review_parser = subparsers.add_parser("review", help="Decide a paused human review")
    review_parser.add_argument("decision", choices=["pass", "revise", "reject"])
    review_parser.add_argument(
        "--run", action="store_true", help="Continue the workflow after the decision"
    )
    review_parser.add_argument(
        "--reset-revisions",
        action="store_true",
        help="With 'revise': clear the code/plan revision counter first, giving the "
        "auto-revise loop a fresh budget instead of immediately re-pausing if it "
        "was paused for hitting max_revisions. Keeps the current worktree and "
        "tasks -- unlike 'reset', nothing already implemented is discarded.",
    )

    reject_parser = subparsers.add_parser(
        "reject",
        help="Send the whole project back to PLANNING, keeping the original "
        "request prompt so it gets re-planned from the same ask. Unlike "
        "'reset', nothing has to be re-typed.",
    )
    reject_parser.add_argument(
        "--run", action="store_true", help="Continue the workflow after rejecting"
    )

    reset_parser = subparsers.add_parser(
        "reset",
        help="Full restart: clears the saved request prompt too, so 'start' "
        "must be run again before 'run'. Use 'reject' instead to keep the "
        "same prompt and just re-plan.",
    )
    reset_parser.add_argument(
        "--state",
        type=str,
        default="PLANNING",
        help="Reset state to specific value (default: PLANNING)",
    )

    backend_parser = subparsers.add_parser(
        "set-backend", help="Set the agent backend for a role"
    )
    backend_parser.add_argument(
        "role",
        choices=[
            "manager",
            "architect",
            "developer",
            "reviewer",
            "qa",
            "assistant",
            "ra",
            "security",
            "sales",
            "sre",
            "devops",
            "uiux",
            "uiux_visual_review",
            "fae",
            "integration",
        ],
        help="The agent role",
    )
    backend_parser.add_argument(
        "backend",
        choices=["ollama", "codex", "agy"],
        help="The backend to use",
    )

    ollama_url_parser = subparsers.add_parser(
        "set-ollama-url", help="Set the Ollama API URL"
    )
    ollama_url_parser.add_argument(
        "url", help="Ollama API base URL, for example http://172.17.144.1:11434"
    )

    weights_parser = subparsers.add_parser(
        "weights", help="Show or adjust internal staff trust weights"
    )
    weights_sub = weights_parser.add_subparsers(dest="weights_command")
    weights_set_parser = weights_sub.add_parser(
        "set", help="Manually override a staff weight"
    )
    weights_set_parser.add_argument("role", choices=["rd", "qa"])
    weights_set_parser.add_argument("level", choices=["senior", "middle", "junior"])
    weights_set_parser.add_argument("value", type=float)

    subparsers.add_parser(
        "roster", help="Show today's available RD/QA roles (staff and vendors)"
    )
    subparsers.add_parser("vendors", help="Show the paid-vendor scorecard")

    brain_parser = subparsers.add_parser(
        "brain", help="Inspect or prune the internal company memory"
    )
    brain_sub = brain_parser.add_subparsers(dest="brain_command")
    brain_search_parser = brain_sub.add_parser("search", help="Search company memory")
    brain_search_parser.add_argument("query")
    brain_prune_parser = brain_sub.add_parser("prune", help="Prune old memories")
    brain_prune_parser.add_argument(
        "--keep",
        type=int,
        default=200,
        help="Number of most recent memories to keep (default: 200)",
    )

    args = parser.parse_args()

    workspace = Path(os.getcwd())
    orchestrator = AgentOrchestrator(workspace)

    if args.command == "init":
        try:
            with orchestrator.execution_lock():
                orchestrator.init_project()
                orchestrator.load_config_and_state()
                configure_ollama(orchestrator)
        except (RuntimeError, OSError) as error:
            log_error(str(error))
    elif args.command == "start":
        try:
            with orchestrator.execution_lock():
                if not archive_previous_run(orchestrator):
                    return
                orchestrator.clear_run_artifacts()
                orchestrator.init_project()
                orchestrator.load_config_and_state()
                configure_ollama(orchestrator)
                ollama_reachable = orchestrator.state.get("ollama_reachable", True)
                allowed_image_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
                max_image_bytes = orchestrator.config.get(
                    "max_visual_image_bytes", 10 * 1024 * 1024
                )
                image_paths = []
                for path in map(Path, args.image):
                    if (
                        not path.is_file()
                        or path.suffix.lower() not in allowed_image_suffixes
                        or path.stat().st_size > max_image_bytes
                    ):
                        log_error(
                            f"Each --image must be a PNG, JPEG, or WebP file no larger than {max_image_bytes // 1024 // 1024}MB."
                        )
                        return
                    image_paths.append(str(path.resolve()))

                # Save request prompt, auto-inlining any referenced .md files
                full_prompt = inline_referenced_markdown(
                    args.prompt, orchestrator.workspace
                )
                with open(orchestrator.request_path, "w", encoding="utf-8") as f:
                    f.write(full_prompt)
                log_success(f"Saved request to {orchestrator.request_path}")

                # Reset state to PLANNING
                orchestrator.state = {
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
                    "visual_image_paths": image_paths,
                    "workflow_mode": "research" if args.research else "delivery",
                    "research_roles": ["sales", "ra"] if args.research else [],
                    "ollama_reachable": ollama_reachable,
                }
                orchestrator.save_state()
                log_success(
                    "Orchestrator initialized and ready to run. Run 'python3 orchestrator.py run' to execute."
                )
        except (RuntimeError, OSError) as error:
            log_error(str(error))
            return
    elif args.command == "step":
        try:
            with orchestrator.execution_lock():
                orchestrator.step()
        except RuntimeError as error:
            log_error(str(error))
    elif args.command == "run":
        try:
            with orchestrator.execution_lock():
                orchestrator.run_to_end()
        except RuntimeError as error:
            log_error(str(error))
    elif args.command in {"gui", "web"}:
        try:
            from orchestrator.gui import serve

            serve(workspace, args.port)
        except (OSError, RuntimeError, FileNotFoundError) as error:
            log_error(str(error))
    elif args.command in {"stop", "pause"}:
        request_stop(orchestrator)
    elif args.command in {"preview", "confirm"}:
        try:
            if args.command == "preview":
                preview_delivery(orchestrator)
            else:
                confirm_delivery(orchestrator)
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "deliver":
        try:
            deliver_task(orchestrator, args.confirm)
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "status":
        try:
            config = json.loads(
                orchestrator.config_path.read_text(encoding="utf-8")
            )
            state = orchestrator.read_state_snapshot()
            if (
                state.get("state") == "COMPLETED"
                and orchestrator._completed_run_needs_preservation(state)
            ):
                state = dict(state)
                state["state"] = "WAITING_FOR_DELIVERY"
                state.setdefault("resume_state", "WAITING_FOR_DELIVERY")
            log_header("ORCHESTRATOR STATUS")
            print(
                f"{'Current State:':<20}{Colors.BOLD}{state.get('state', 'UNKNOWN')}{Colors.ENDC}"
            )
            print(
                f"{'Plan Revisions:':<20}{state.get('plan_revisions', 0)}/{config.get('max_revisions', 2)}"
            )
            print(
                f"{'Code Revisions:':<20}{state.get('code_revisions', 0)}/{config.get('max_revisions', 2)}"
            )
            print(f"Ollama Model:       {config.get('ollama_model', '')}")
            print(
                f"{'Developer Backend:':<20}{config.get('backends', {}).get('developer', '')}"
            )
            print(
                f"{'Reviewer Backend:':<20}{config.get('backends', {}).get('reviewer', '')}"
            )
            print(
                f"{'QA Backend:':<20}{config.get('backends', {}).get('qa', 'ollama')}"
            )
            print(f"{'Test Command:':<20}{config.get('test_command', '')}")

            tasks = state.get("tasks", [])
            if tasks:
                print(f"\n{'Action Items'} ({len(tasks)} total):")
                for t in tasks:
                    status_color = (
                        Colors.GREEN if t["status"] == "completed" else Colors.WARNING
                    )
                    print(
                        f" - [{status_color}{t['status']}{Colors.ENDC}] {t['id']}: {t['description']}"
                    )
            else:
                print(f"\n{'No tasks parsed yet.'}")
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "approve":
        try:
            should_run = False
            with orchestrator.execution_lock():
                orchestrator.load_config_and_state()
                if orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                    log_error(
                        f"Cannot approve from state: {orchestrator.state.get('state')}"
                    )
                    return
                if not apply_approve(orchestrator, args.clear_route_failures):
                    return
                should_run = args.run
                if should_run:
                    orchestrator.run_to_end()
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "review":
        try:
            should_run = False
            with orchestrator.execution_lock():
                orchestrator.load_config_and_state()
                if orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                    log_error(
                        f"Cannot review from state: {orchestrator.state.get('state')}"
                    )
                    return
                apply_review_decision(
                    orchestrator, args.decision, args.reset_revisions
                )
                should_run = args.run and args.decision != "reject"
                if should_run:
                    orchestrator.run_to_end()
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "continue":
        try:
            with orchestrator.execution_lock():
                orchestrator.load_config_and_state()
                state = orchestrator.state.get("state")
                if state == "COMPLETED":
                    log_success("Project already completed; nothing to continue.")
                    return
                if state == "FAILED":
                    log_error(
                        "Project failed; nothing to continue. Use 'reject' (keep the "
                        "prompt, re-plan) or 'reset' (start over) instead."
                    )
                    return
                if state == "WAITING_FOR_DELIVERY":
                    log_warning(
                        "Delivery confirmation required; use 'deliver --confirm'."
                    )
                    return
                if state == "WAITING_FOR_OWNER":
                    decision, reset_revisions = pick_continue_decision(orchestrator)
                    if decision == "approve":
                        if not apply_approve(
                            orchestrator, args.clear_route_failures
                        ):
                            return
                    else:
                        apply_review_decision(orchestrator, "revise", reset_revisions)
                else:
                    orchestrator.clear_pause_request()
                orchestrator.run_to_end()
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "reject":
        try:
            with orchestrator.execution_lock():
                orchestrator.load_config_and_state()
                clear_run_state(orchestrator, "PLANNING")
                orchestrator.save_state()
                log_success(
                    "Project sent back to PLANNING; the original request prompt was kept."
                )
                if args.run:
                    orchestrator.run_to_end()
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "reset":
        try:
            with orchestrator.execution_lock():
                orchestrator.load_config_and_state()
                clear_run_state(orchestrator, args.state)
                if orchestrator.request_path.exists():
                    orchestrator.request_path.unlink()
                orchestrator.save_state()
            log_success(
                f"State reset to {args.state}; the request prompt was cleared -- "
                "run 'start' again before 'run'."
            )
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "set-backend":
        try:
            orchestrator.load_config_and_state()
            roles = (
                ["developer_senior", "developer_middle", "developer_junior"]
                if args.role == "developer"
                else ["qa_senior", "qa_middle", "qa_junior"]
                if args.role == "qa"
                else [args.role]
            )
            for role in roles:
                orchestrator.config["backends"][role] = args.backend
            with open(orchestrator.config_path, "w", encoding="utf-8") as f:
                json.dump(orchestrator.config, f, indent=2)
            log_success(
                f"Successfully configured '{args.role}' backend to '{args.backend}'"
            )
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "set-ollama-url":
        orchestrator.init_project()
        orchestrator.load_config_and_state()
        orchestrator.config["ollama_url"] = args.url.rstrip("/")
        with open(orchestrator.config_path, "w", encoding="utf-8") as f:
            json.dump(orchestrator.config, f, indent=2)
        log_success(f"Ollama API URL set to {orchestrator.config['ollama_url']}")
    elif args.command == "weights":
        try:
            orchestrator.load_config_and_state()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
            return
        if args.weights_command == "set":
            if not (scoring.WEIGHT_MIN <= args.value <= scoring.WEIGHT_MAX):
                log_error(
                    f"Weight must be between {scoring.WEIGHT_MIN} and {scoring.WEIGHT_MAX}."
                )
                return
            orchestrator.config.setdefault("worker_weights", {}).setdefault(
                args.role, {}
            )[args.level] = args.value
            with open(orchestrator.config_path, "w", encoding="utf-8") as f:
                json.dump(orchestrator.config, f, indent=2)
            log_success(f"Set {args.role}/{args.level} trust weight to {args.value}")
        else:
            log_header("INTERNAL STAFF TRUST WEIGHTS")
            for role, levels in orchestrator.config.get("worker_weights", {}).items():
                for level, weight in levels.items():
                    print(f" - {role}/{level}: {weight}")
    elif args.command == "roster":
        try:
            orchestrator.load_config_and_state()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
            return
        from orchestrator.core import roster

        data = roster.snapshot(orchestrator)
        log_header("TODAY'S ROSTER")
        for role, info in data["available_roles"].items():
            status_color = (
                Colors.GREEN if info["status"] == "available" else Colors.FAIL
            )
            score = info.get("weight", info.get("rating"))
            print(
                f" - {role} ({info['type']}, backend={info['backend']}): {status_color}{info['status']}{Colors.ENDC} score={score}"
            )
        if data["unavailable_backends"]:
            print(f"\nUnavailable backends: {', '.join(data['unavailable_backends'])}")
    elif args.command == "vendors":
        try:
            orchestrator.load_config_and_state()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
            return
        log_header("VENDOR SCORECARD")
        vendor_scores = orchestrator.config.get("vendor_scores", {})
        if not vendor_scores:
            print("No vendor activity recorded yet.")
        for key, info in vendor_scores.items():
            print(
                f" - {key}: rating={info.get('rating')} jobs={info.get('jobs')} failures={info.get('failures')}"
            )
    elif args.command == "brain":
        try:
            orchestrator.load_config_and_state()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
            return
        from orchestrator.core import brain

        if args.brain_command == "search":
            results = brain.recall(orchestrator, args.query, limit=10)
            if not results:
                print("No matching memories.")
            for item in results:
                print(f"- {item}\n")
        elif args.brain_command == "prune":
            deleted = brain.prune(orchestrator, keep_last=args.keep)
            log_success(
                f"Pruned {deleted} old memories; kept the most recent {args.keep}."
            )
        else:
            log_error("Specify a brain subcommand: search or prune.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
