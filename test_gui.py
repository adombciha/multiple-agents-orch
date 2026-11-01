import io
import json
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator import gui
from orchestrator.core.state import AgentOrchestrator


def make_service(state="PLANNING"):
    lock_temp = tempfile.TemporaryDirectory()
    lock_owner = AgentOrchestrator(Path(lock_temp.name))
    service = gui.GuiService.__new__(gui.GuiService)
    service.worker = None
    service.worker_lock = threading.Lock()
    service.operation_lock = threading.Lock()
    service._lock_temp = lock_temp
    service._lock_owner = lock_owner
    service.csrf_token = "csrf-token"
    service.orchestrator = SimpleNamespace(
        state={"state": state},
        state_path=SimpleNamespace(
            read_text=Mock(return_value=json.dumps({"state": state}))
        ),
        config={"max_revisions": 2},
        execution_lock=lock_owner.execution_lock,
        pause_requested=Mock(return_value=False),
        request_pause=Mock(),
        clear_pause_request=Mock(),
        save_state=Mock(),
        step=Mock(),
        load_config_and_state=Mock(),
    )
    service.status = Mock(
        return_value={
            "state": state,
            "progress": {"tasks": [], "plan_revisions": 0, "code_revisions": 0},
            "reports": {},
            "worker_running": False,
        }
    )
    return service


def start_worker_inline(service):
    def start(prepare=None):
        with service.orchestrator.execution_lock():
            if prepare:
                prepare()
        return True

    service._start_worker = Mock(side_effect=start)
    return service._start_worker


def finish_worker(service):
    worker = service.worker
    if worker:
        worker.join(timeout=5)
        assert not worker.is_alive()
    return service.status()


def make_git_delivery_service(workspace):
    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "master")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (workspace / ".gitignore").write_text(".ai-company-*\n", encoding="utf-8")
    (workspace / "app.py").write_text("base\n", encoding="utf-8")
    git("add", ".gitignore", "app.py")
    git("commit", "-m", "initial")

    service = gui.GuiService(workspace)
    service.orchestrator.init_project()
    service.orchestrator.config["use_worktree"] = False
    service.orchestrator.config_path.write_text(
        json.dumps(service.orchestrator.config, indent=2), encoding="utf-8"
    )
    service.orchestrator.state.update(
        {
            "state": "WAITING_FOR_DELIVERY",
            "workflow_mode": "delivery",
            "delivery_artifacts_prepared": True,
            "changelog_written": True,
        }
    )
    service.orchestrator.final_report_path.write_text(
        "final report\n", encoding="utf-8"
    )
    service.orchestrator.save_state()
    (workspace / "app.py").write_text("feature\n", encoding="utf-8")
    service.orchestrator.run_to_end = Mock()
    return service


def make_worktree_delivery_service(workspace):
    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "master")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (workspace / ".gitignore").write_text(".ai-company-*\n", encoding="utf-8")
    (workspace / "app.py").write_text("base\n", encoding="utf-8")
    git("add", ".gitignore", "app.py")
    git("commit", "-m", "initial")

    service = gui.GuiService(workspace)
    orchestrator = service.orchestrator
    orchestrator.init_project()
    orchestrator.setup_worktree()
    orchestrator.state.update(
        {
            "state": "WAITING_FOR_DELIVERY",
            "workflow_mode": "delivery",
            "delivery_artifacts_prepared": True,
            "changelog_written": True,
        }
    )
    orchestrator.final_report_path.write_text("final report\n", encoding="utf-8")
    (orchestrator.ai_dir / "worktree" / "app.py").write_text(
        "feature\n", encoding="utf-8"
    )
    orchestrator.save_state()
    service._start_worker = start_worker_inline(service)
    return service


def start_test_server(service):
    server = gui.ThreadingHTTPServer((gui.HOST, 0), gui.RequestHandler)
    server.service = service
    server.csrf_token = service.csrf_token
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def http_request(server, method, path, body=None):
    port = server.server_port
    headers = {
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
    }
    data = None
    if body is not None:
        headers.update(
            {
                "Cookie": f"gui_csrf={server.csrf_token}",
                "X-CSRF-Token": server.csrf_token,
                "Content-Type": "application/json",
            }
        )
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def make_handler(command, headers, path="/api/status", service=None):
    handler = gui.RequestHandler.__new__(gui.RequestHandler)
    handler.command = command
    handler.path = path
    handler.headers = headers
    handler.server = SimpleNamespace(
        server_port=8765,
        service=service or SimpleNamespace(csrf_token="csrf-token"),
    )
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = io.BytesIO()
    return handler


def post_json(handler, value):
    payload = json.dumps(value).encode("utf-8")
    handler.headers["Content-Length"] = str(len(payload))
    handler.rfile = io.BytesIO(payload)
    gui.RequestHandler.do_POST(handler)
    return json.loads(handler.wfile.getvalue())


def test_submit_starts_worker_with_initial_workflow_state(tmp_path, monkeypatch):
    lock_owner = AgentOrchestrator(tmp_path)
    service = gui.GuiService.__new__(gui.GuiService)
    service.workspace = tmp_path
    service.operation_lock = threading.Lock()
    service.worker_lock = threading.Lock()
    service.worker = None
    service.status = Mock(return_value={"state": "PLANNING"})
    service._state = Mock(return_value={"state": "PLANNING"})
    service._lock_owner = lock_owner

    orchestrator = SimpleNamespace(
        state_path=SimpleNamespace(exists=Mock(return_value=False)),
        ai_dir=tmp_path / "missing-ai",
        request_path=tmp_path / "request.md",
        state={},
        execution_lock=lock_owner.execution_lock,
        init_project=Mock(),
        load_config_and_state=Mock(),
        save_state=Mock(),
    )
    service.orchestrator = orchestrator

    def start_worker(prepare=None):
        with orchestrator.execution_lock():
            if prepare:
                prepare()
        return True

    service._start_worker = Mock(side_effect=start_worker)
    monkeypatch.setattr(
        gui, "inline_referenced_markdown", lambda prompt, workspace: prompt
    )

    result = service._new_task("run the offline GUI workflow")

    assert result["state"] == "PLANNING"
    assert orchestrator.request_path.read_text(encoding="utf-8") == (
        "run the offline GUI workflow"
    )
    assert orchestrator.state["workflow_mode"] == "delivery"
    service._start_worker.assert_called_once()


def test_new_task_is_rejected_while_another_task_is_running():
    service = make_service("RUNNING")
    service._state = Mock(return_value={"state": "RUNNING"})

    service._new_task("second task")
    worker = service.worker
    if worker:
        worker.join(timeout=2)
    assert "already running" in service.worker_status["error"]


def test_worker_runs_to_decision_and_clears_worker_reference():
    service = make_service()
    service.orchestrator.run_to_end = Mock(
        side_effect=lambda: service.orchestrator.state.update(
            {"state": "WAITING_FOR_OWNER"}
        )
    )
    service.worker = object()

    service._run_to_decision()

    assert service.orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert service.worker is None


def test_duplicate_submit_click_does_not_start_second_worker():
    service = make_service()
    service.worker = SimpleNamespace(is_alive=lambda: True)

    assert service._start_worker() is False


def test_stop_sets_pause_request_while_workflow_is_running():
    service = make_service("RUNNING")
    service._load = Mock()

    result = service.stop()

    service.orchestrator.request_pause.assert_called_once_with()
    assert result["state"] == "RUNNING"


def test_review_failure_can_request_revision(monkeypatch):
    service = make_service("WAITING_FOR_OWNER")
    service._load = Mock()
    apply_review = Mock()
    monkeypatch.setattr(gui, "apply_review_decision", apply_review)
    start_worker = start_worker_inline(service)

    service.review("revise")

    apply_review.assert_called_once_with(service.orchestrator, "revise", False)
    start_worker.assert_called_once()


def test_delivery_first_click_presents_git_impact_and_second_click_confirms(tmp_path):
    service = make_git_delivery_service(tmp_path)

    presented = service.delivery(False)

    assert presented["delivery_confirmation_required"] is True
    assert presented["git"]["tracked_files"] == {"app.py": "feature\n"}
    assert presented["git"]["source_commit"]
    assert presented["git"]["target_commit"]
    assert presented["git"]["scheduled_operation"] == "confirm_without_merge"
    assert service.orchestrator.state["delivery_confirmed"] is False

    result = service.delivery(True, presented["delivery_snapshot_id"])
    result = finish_worker(service)
    assert result["state"] == "COMPLETED"
    service.orchestrator.run_to_end.assert_not_called()


def test_delivery_confirmation_requires_the_presented_snapshot_id(tmp_path):
    service = make_git_delivery_service(tmp_path)
    service.delivery(False)

    service.delivery(True)
    result = finish_worker(service)
    assert "does not match the displayed snapshot" in result["worker_status"]["error"]

    assert service.orchestrator.state["delivery_confirmed"] is False


def test_delivery_requires_new_confirmation_when_git_impact_changes(tmp_path):
    service = make_git_delivery_service(tmp_path)
    first = service.delivery(False)
    (tmp_path / "app.py").write_text("changed feature\n", encoding="utf-8")

    service.delivery(True, first["delivery_snapshot_id"])
    result = finish_worker(service)
    assert "Git impact changed" in result["worker_status"]["error"]

    assert service.orchestrator.state["delivery_confirmed"] is False
    assert service.orchestrator.state["delivery_preview_presented"] is False
    updated = service.orchestrator.get_delivery_preview()
    assert updated != first["git"]
    assert updated["tracked_files"] == {"app.py": "changed feature\n"}


def test_gui_delivery_confirmation_merges_worktree_and_reaches_completed(tmp_path):
    service = make_worktree_delivery_service(tmp_path)
    run_to_end = Mock()
    service.orchestrator.run_to_end = run_to_end

    presented = service.delivery(False)
    result = service.delivery(True, presented["delivery_snapshot_id"])

    assert result["state"] == "COMPLETED"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "feature\n"
    assert service.orchestrator.state["delivery"]["delivered"] is True
    run_to_end.assert_not_called()


def test_gui_delivery_prepares_final_artifacts_before_presenting_snapshot(
    tmp_path, monkeypatch
):
    service = make_worktree_delivery_service(tmp_path)
    orchestrator = service.orchestrator
    orchestrator.state.update(
        {
            "delivery_artifacts_prepared": False,
            "changelog_written": False,
        }
    )
    orchestrator.request_path.write_text("request", encoding="utf-8")
    orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    orchestrator.final_report_path.unlink()
    orchestrator.save_state()
    orchestrator.manager.call_manager = Mock(return_value="summary")
    orchestrator.assistant.generate_meeting_memory = Mock(return_value="memory")
    orchestrator.assistant.generate_changelog = Mock(return_value="change")
    orchestrator.manager._review_worker_weights = Mock()
    monkeypatch.setattr("orchestrator.roles.manager.brain.remember", Mock())

    present = orchestrator.present_delivery_preview

    def assert_prepared(preview):
        assert orchestrator.state["delivery_artifacts_prepared"] is True
        assert orchestrator.state["changelog_written"] is True
        assert orchestrator.final_report_path.read_text(encoding="utf-8").startswith(
            "summary"
        )
        assert "change" in (
            orchestrator.ai_dir / "worktree" / "CHANGELOG.md"
        ).read_text(encoding="utf-8")
        return present(preview)

    monkeypatch.setattr(orchestrator, "present_delivery_preview", assert_prepared)

    result = service.delivery(False)

    assert result["delivery_confirmation_required"] is True
    assert orchestrator.state["delivery_preview_presented"] is True


@pytest.mark.parametrize(
    "mutation",
    ["source_branch", "target_branch", "index"],
)
def test_gui_delivery_snapshot_rejects_branch_and_index_changes(tmp_path, mutation):
    service = make_worktree_delivery_service(tmp_path)
    presented = service.delivery(False)
    worktree = service.orchestrator.ai_dir / "worktree"

    if mutation == "source_branch":
        subprocess.run(
            ["git", "branch", "-m", "delivery-source"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
    elif mutation == "target_branch":
        subprocess.run(
            ["git", "checkout", "-b", "delivery-target"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            ["git", "add", "app.py"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )

    assert service.orchestrator.get_delivery_preview() != presented["git"]

    with pytest.raises(RuntimeError, match="does not match the displayed snapshot"):
        service.delivery(True, presented["delivery_snapshot_id"])


def test_delivery_file_content_records_symlink_without_following_target(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "linked.txt"
    link.symlink_to(outside)

    try:
        orchestrator = AgentOrchestrator(tmp_path)
        content = orchestrator._delivery_file_content(link)
        assert content == f"symlink:{outside}"
        assert content != "secret"
    finally:
        outside.unlink()


def test_delivery_file_contents_rejects_symlinked_parent_outside_worktree(tmp_path):
    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (worktree / "linked-dir").symlink_to(outside, target_is_directory=True)
    orchestrator = AgentOrchestrator(tmp_path)

    with pytest.raises(RuntimeError, match="escapes the source worktree"):
        orchestrator._delivery_file_contents(worktree, ["linked-dir/secret.txt"])


def test_http_task_runs_async_and_status_is_readable_during_execution(tmp_path):
    service = gui.GuiService(tmp_path)
    started = threading.Event()
    release = threading.Event()
    orchestrator = service.orchestrator

    def run_worker():
        orchestrator.state["state"] = "IMPLEMENTING"
        orchestrator.save_state()
        started.set()
        release.wait(timeout=5)
        if orchestrator.pause_requested():
            orchestrator.state.update(
                {
                    "state": "WAITING_FOR_OWNER",
                    "resume_state": "IMPLEMENTING",
                }
            )
        else:
            orchestrator.state["state"] = "COMPLETED"
        orchestrator.save_state()

    orchestrator.run_to_end = run_worker
    server, server_thread = start_test_server(service)

    try:
        code, _ = http_request(server, "POST", "/api/task", {"prompt": "offline task"})
        assert code == gui.HTTPStatus.OK
        assert started.wait(timeout=2)

        contender = AgentOrchestrator(tmp_path)
        with pytest.raises(RuntimeError, match="already in progress"):
            with contender.execution_lock():
                pass

        code, current = http_request(server, "GET", "/api/status")
        assert code == gui.HTTPStatus.OK
        assert current["state"] == "IMPLEMENTING"
        assert current["worker_running"] is True

        code, duplicate = http_request(
            server, "POST", "/api/task", {"prompt": "second task"}
        )
        assert code == gui.HTTPStatus.CONFLICT
        assert "already running" in duplicate["error"]

        code, stopped = http_request(server, "POST", "/api/stop", {})
        assert code == gui.HTTPStatus.OK
        assert stopped["pause_status"] == "目前步驟完成後暫停"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        if service.worker:
            service.worker.join(timeout=2)

    assert orchestrator.state["state"] == "WAITING_FOR_OWNER"


def test_http_stop_and_continue_use正式_action_routes():
    service = make_service("IMPLEMENTING")
    service._load = Mock()

    stop_handler = make_handler(
        "POST",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=csrf-token",
            "X-CSRF-Token": "csrf-token",
        },
        "/api/stop",
        service,
    )
    stopped = post_json(stop_handler, {})

    assert stopped["state"] == "IMPLEMENTING"
    service.orchestrator.request_pause.assert_called_once_with()
    assert stop_handler.send_response.call_args.args[0] == gui.HTTPStatus.OK

    service.orchestrator.pause_requested.return_value = True
    start_worker = start_worker_inline(service)
    continue_handler = make_handler(
        "POST",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=csrf-token",
            "X-CSRF-Token": "csrf-token",
        },
        "/api/continue",
        service,
    )
    continued = post_json(continue_handler, {})

    assert continued["state"] == "IMPLEMENTING"
    service.orchestrator.clear_pause_request.assert_called_once_with()
    start_worker.assert_called_once()
    assert continue_handler.send_response.call_args.args[0] == gui.HTTPStatus.OK


def test_http_duplicate_operations_are_rejected():
    service = SimpleNamespace(
        csrf_token="csrf-token",
        _new_task=Mock(
            side_effect=RuntimeError(
                "A task is already running or waiting for a decision."
            )
        ),
    )
    task_handler = make_handler(
        "POST",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=csrf-token",
            "X-CSRF-Token": "csrf-token",
        },
        "/api/task",
        service,
    )

    task_result = post_json(task_handler, {"prompt": "second task"})

    assert task_handler.send_response.call_args.args[0] == gui.HTTPStatus.CONFLICT
    assert "already running" in task_result["error"]
    service._new_task.assert_called_once_with("second task")

    running_service = make_service("RUNNING")
    running_service._load = Mock()
    running_service.orchestrator.pause_requested.return_value = True
    stop_handler = make_handler(
        "POST",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=csrf-token",
            "X-CSRF-Token": "csrf-token",
        },
        "/api/stop",
        running_service,
    )

    stop_result = post_json(stop_handler, {})

    assert stop_handler.send_response.call_args.args[0] == gui.HTTPStatus.CONFLICT
    assert "already been requested" in stop_result["error"]
    running_service.orchestrator.request_pause.assert_not_called()


def test_http_delivery_persists_git_impact_for_status_and_requires_confirmation(
    tmp_path,
):
    service = make_git_delivery_service(tmp_path)
    headers = {
        "Host": "127.0.0.1:8765",
        "Origin": "http://127.0.0.1:8765",
        "Cookie": f"gui_csrf={service.csrf_token}",
        "X-CSRF-Token": service.csrf_token,
    }

    present_handler = make_handler("POST", dict(headers), "/api/deliver", service)
    presented = post_json(present_handler, {"confirm": False})

    assert presented["delivery_confirmation_required"] is True
    assert presented["git"]["tracked_files"] == {"app.py": "feature\n"}
    assert presented["git"]["source_commit"]
    assert presented["git"]["target_commit"]

    status_handler = make_handler(
        "GET",
        {"Host": "127.0.0.1:8765"},
        "/api/status",
        service,
    )
    gui.RequestHandler.do_GET(status_handler)
    current = json.loads(status_handler.wfile.getvalue())

    assert current["git"] == presented["git"]
    assert current["delivery_confirmed"] is False

    confirm_handler = make_handler("POST", dict(headers), "/api/deliver", service)
    confirmed = post_json(
        confirm_handler,
        {"confirm": True, "snapshot_id": presented["delivery_snapshot_id"]},
    )

    assert finish_worker(service)["state"] == "COMPLETED"
    service.orchestrator.run_to_end.assert_not_called()
    assert confirm_handler.send_response.call_args.args[0] == gui.HTTPStatus.OK


def test_render_page_displays_stage_tasks_reports_and_git_impact():
    page = gui.render_page(
        {
            "state": "WAITING_FOR_DELIVERY",
            "delivery_preview_presented": True,
            "delivery_snapshot_id": "snapshot",
            "worker_running": False,
            "pause_requested": False,
            "review": {"reason": "Reviewer requested revision"},
            "progress": {
                "tasks": [
                    {
                        "id": "T6",
                        "status": "DONE",
                        "description": "Offline GUI mock tests",
                    }
                ]
            },
            "reports": {"qa": "All checks passed"},
            "git": {"diff_stat": "1 file changed", "git_status": " M test_gui.py"},
            "csrf": "csrf-token",
        }
    )

    assert "WAITING_FOR_DELIVERY" in page
    assert "T6" in page
    assert "Offline GUI mock tests" in page
    assert "All checks passed" in page
    assert "Git impact" in page
    assert "1 file changed" in page
    assert "snapshot_id:deliverySnapshotId" in page
    assert "location.reload" not in page
    assert "patchRegion" in page
    assert 'id="reports"' in page
    assert "setTimeout(refreshWhenIdle, 3000)" in page
    assert "重置任務</button>" in page
    assert "重置會清除已存任務內容並清理目前 worktree" in page


def test_render_page_escapes_script_injection_from_tasks_reports_and_review():
    page = gui.render_page(
        {
            "state": "<script>alert(1)</script>",
            "review": {"reason": "<img src=x onerror=alert(1)>"},
            "progress": {
                "tasks": [
                    {
                        "id": "<script>alert(2)</script>",
                        "status": "DONE",
                        "description": "<svg onload=alert(3)>",
                    }
                ]
            },
            "reports": {"qa": "<script>alert(4)</script>"},
            "git": {
                "diff_stat": "<script>alert(5)</script>",
                "git_status": "clean",
            },
        }
    )

    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;svg onload=alert(3)&gt;" in page


def test_http_root_escapes_injected_status_text():
    service = SimpleNamespace(
        csrf_token="csrf-token",
        status=Mock(
            return_value={
                "state": "<script>alert(1)</script>",
                "review": {"reason": "<img src=x onerror=alert(2)>"},
                "progress": {
                    "tasks": [
                        {
                            "id": "<script>alert(3)</script>",
                            "status": "DONE",
                            "description": "<svg onload=alert(4)>",
                        }
                    ]
                },
                "reports": {"qa": "<script>alert(5)</script>"},
                "git": {
                    "diff_stat": "<script>alert(6)</script>",
                    "git_status": "clean",
                },
            }
        ),
    )
    handler = make_handler(
        "GET",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
        },
        "/",
        service,
    )

    gui.RequestHandler.do_GET(handler)
    page = handler.wfile.getvalue().decode("utf-8")

    assert handler.send_response.call_args.args[0] == gui.HTTPStatus.OK
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x onerror=alert(2)>" not in page
    assert "<svg onload=alert(4)>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_post_submit_route_works_with_an_offline_mock_handler():
    service = SimpleNamespace(
        csrf_token="csrf-token",
        _new_task=Mock(return_value={"state": "PLANNING"}),
    )
    headers = {
        "Host": "127.0.0.1:8765",
        "Origin": "http://127.0.0.1:8765",
        "Cookie": "gui_csrf=csrf-token",
        "X-CSRF-Token": "csrf-token",
        "Content-Length": "22",
    }
    handler = make_handler("POST", headers, "/api/task", service)
    handler.rfile = io.BytesIO(b'{"prompt":"offline"}')

    gui.RequestHandler.do_POST(handler)

    service._new_task.assert_called_once_with("offline")
    assert json.loads(handler.wfile.getvalue())["state"] == "PLANNING"


def test_post_reset_route_calls_existing_reset_service():
    service = SimpleNamespace(
        csrf_token="csrf-token",
        reset=Mock(return_value={"state": "PLANNING"}),
    )
    handler = make_handler(
        "POST",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=csrf-token",
            "X-CSRF-Token": "csrf-token",
        },
        "/api/reset",
        service,
    )

    response = post_json(handler, {})

    service.reset.assert_called_once_with()
    assert response["state"] == "PLANNING"


def test_gui_reset_reuses_reset_logic_and_clears_saved_prompt(monkeypatch):
    service = make_service("FAILED")
    service.worker = None
    service.worker_lock = threading.Lock()
    service.operation_lock = threading.Lock()
    service.status = Mock(return_value={"state": "PLANNING"})
    service.orchestrator.request_path = SimpleNamespace(
        exists=Mock(return_value=True), unlink=Mock()
    )
    clear_run_state = Mock()
    monkeypatch.setattr(gui, "clear_run_state", clear_run_state)

    result = service.reset()

    clear_run_state.assert_called_once_with(service.orchestrator, "PLANNING")
    service.orchestrator.request_path.unlink.assert_called_once_with()
    service.orchestrator.save_state.assert_called_once_with()
    assert result["state"] == "PLANNING"


def test_gui_reset_rejects_while_worker_is_running():
    service = make_service("IMPLEMENTING")
    service.worker = Mock()
    service.worker.is_alive.return_value = True
    service.worker_lock = threading.Lock()
    service.operation_lock = threading.Lock()

    with pytest.raises(RuntimeError, match="worker is already running"):
        service.reset()

    service.orchestrator.load_config_and_state.assert_not_called()


def test_gui_reset_preserves_prompt_when_worktree_cleanup_fails(monkeypatch):
    service = make_service("FAILED")
    service.worker = None
    service.worker_lock = threading.Lock()
    service.operation_lock = threading.Lock()
    service.orchestrator.request_path = SimpleNamespace(
        exists=Mock(return_value=True), unlink=Mock()
    )
    monkeypatch.setattr(
        gui,
        "clear_run_state",
        Mock(side_effect=RuntimeError("Git worktree cleanup failed")),
    )

    with pytest.raises(RuntimeError, match="worktree cleanup failed"):
        service.reset()

    service.orchestrator.request_path.unlink.assert_not_called()
    service.orchestrator.save_state.assert_not_called()


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("evil.example", "http://127.0.0.1:8765"),
        ("127.0.0.1:8765", "https://evil.example"),
    ],
)
def test_host_and_origin_validation_rejects_cross_site_requests(host, origin):
    handler = make_handler(
        "POST",
        {
            "Host": host,
            "Origin": origin,
            "Cookie": "gui_csrf=csrf-token",
            "X-CSRF-Token": "csrf-token",
        },
    )

    assert handler._security_check() is False

    handler.send_response.assert_called_once_with(gui.HTTPStatus.FORBIDDEN)


def test_http_post_rejects_cross_site_request_before_dispatch():
    service = SimpleNamespace(
        csrf_token="csrf-token",
        _new_task=Mock(),
    )
    handler = make_handler(
        "POST",
        {
            "Host": "evil.example",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=csrf-token",
            "X-CSRF-Token": "csrf-token",
        },
        "/api/task",
        service,
    )

    result = post_json(handler, {"prompt": "blocked"})

    assert handler.send_response.call_args.args[0] == gui.HTTPStatus.FORBIDDEN
    assert result["error"] == "Request rejected."
    service._new_task.assert_not_called()


def test_csrf_validation_rejects_missing_or_wrong_token():
    handler = make_handler(
        "POST",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=wrong-token",
            "X-CSRF-Token": "wrong-token",
        },
    )

    assert handler._security_check() is False
    assert handler.send_response.call_args.args[0] == gui.HTTPStatus.FORBIDDEN


def test_http_post_rejects_invalid_csrf_before_dispatch():
    service = SimpleNamespace(
        csrf_token="csrf-token",
        _new_task=Mock(),
    )
    handler = make_handler(
        "POST",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=wrong-token",
            "X-CSRF-Token": "wrong-token",
        },
        "/api/task",
        service,
    )

    result = post_json(handler, {"prompt": "blocked"})

    assert handler.send_response.call_args.args[0] == gui.HTTPStatus.FORBIDDEN
    assert result["error"] == "CSRF validation failed."
    service._new_task.assert_not_called()


@pytest.mark.parametrize("port", [0, -1, 65536, True, "8765"])
def test_local_gui_rejects_invalid_port_before_binding(tmp_path, monkeypatch, port):
    server_factory = Mock()
    service_factory = Mock()
    monkeypatch.setattr(gui, "ThreadingHTTPServer", server_factory)
    monkeypatch.setattr(gui, "GuiService", service_factory)

    with pytest.raises(ValueError, match="port must be between 1 and 65535"):
        gui.serve(tmp_path, port)

    server_factory.assert_not_called()
    service_factory.assert_not_called()


def test_local_gui_creates_server_for_valid_port_and_closes_it(tmp_path, monkeypatch):
    created = []
    service = object()

    class FakeServer:

        def __init__(self, address, handler):
            self.address = address
            self.handler = handler
            self.service = None
            self.served = False
            self.closed = False
            created.append(self)

        def serve_forever(self):
            self.served = True

        def server_close(self):
            self.closed = True

    monkeypatch.setattr(gui, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(gui, "GuiService", lambda workspace: service)

    gui.serve(tmp_path, 8765)

    assert len(created) == 1
    assert created[0].address == (gui.HOST, 8765)
    assert created[0].handler is gui.RequestHandler
    assert created[0].service is service
    assert created[0].served is True
    assert created[0].closed is True


def test_gui_cli_entrypoint_uses_formal_web_server(monkeypatch, tmp_path):
    serve = Mock()
    monkeypatch.setattr(gui, "serve", serve)
    monkeypatch.setattr(
        "sys.argv",
        [
            "orchestrator-gui",
            "--workspace",
            str(tmp_path),
            "--port",
            "8765",
        ],
    )

    gui.main()

    serve.assert_called_once_with(tmp_path, 8765)


def test_local_gui_checks_host_origin_token_and_action_lock():
    service = SimpleNamespace(
        csrf_token="csrf-token",
        stop=Mock(),
    )
    handler = make_handler(
        "POST",
        {
            "Host": "evil.example",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": "gui_csrf=csrf-token",
            "X-CSRF-Token": "csrf-token",
        },
        "/api/stop",
        service,
    )

    result = post_json(handler, {})

    assert handler.send_response.call_args.args[0] == gui.HTTPStatus.FORBIDDEN
    assert result["error"] == "Request rejected."
    service.stop.assert_not_called()


def test_gui_status_reads_persistent_snapshot_without_overwriting_worker_state():
    service = make_service("RUNNING")
    service.orchestrator.state_path.read_text.return_value = json.dumps(
        {"state": "WAITING_FOR_OWNER", "tasks": []}
    )
    service._reports = Mock(return_value={})
    service.status = gui.GuiService.status.__get__(service, gui.GuiService)

    result = service.status()

    assert result["state"] == "WAITING_FOR_OWNER"
    assert service.orchestrator.state["state"] == "RUNNING"


def test_gui_stop_writes_pause_request_without_execution_lock():
    service = make_service("IMPLEMENTING")
    service._state = Mock(return_value={"state": "IMPLEMENTING"})
    service.orchestrator.execution_lock = Mock(
        side_effect=AssertionError("stop must not take execution_lock")
    )

    result = service.stop()

    assert result["state"] == "IMPLEMENTING"
    service.orchestrator.request_pause.assert_called_once_with()
    service.orchestrator.execution_lock.assert_not_called()


def test_completed_gui_run_keeps_delivery_gate_across_reload():
    service = make_service("COMPLETED")
    service.orchestrator.state_path.read_text.return_value = json.dumps(
        {
            "state": "COMPLETED",
            "delivery_confirmed": False,
            "presented_delivery_snapshot": {
                "diff_stat": "1 file changed",
                "git_status": " M app.py",
            },
        }
    )
    service.orchestrator._completed_run_needs_preservation = Mock(return_value=True)
    service._reports = Mock(return_value={})
    service.status = gui.GuiService.status.__get__(service, gui.GuiService)

    result = service.status()

    assert result["state"] == "WAITING_FOR_DELIVERY"
    assert result["delivery_confirmed"] is False
    assert result["git"] == {
        "diff_stat": "1 file changed",
        "git_status": " M app.py",
    }


def test_gui_approve_clears_pause_request_before_resuming(monkeypatch):
    service = make_service("WAITING_FOR_OWNER")
    service._load = Mock()
    start_worker = start_worker_inline(service)
    apply_approve = Mock(side_effect=lambda app: app.clear_pause_request() or True)
    monkeypatch.setattr(gui, "apply_approve", apply_approve)

    result = service.approve()

    assert result["state"] == "WAITING_FOR_OWNER"
    service.orchestrator.clear_pause_request.assert_called_once_with()
    apply_approve.assert_called_once_with(service.orchestrator)
    start_worker.assert_called_once()


def test_gui_continue_owner_review_clears_pause_request_once(monkeypatch):
    service = make_service("WAITING_FOR_OWNER")
    service.orchestrator.state["human_review_source"] = "QA"
    service._load = Mock()
    start_worker = start_worker_inline(service)
    apply_review_decision = Mock(
        side_effect=lambda orchestrator, *_args: orchestrator.clear_pause_request()
    )
    monkeypatch.setattr(
        "orchestrator.main.apply_review_decision", apply_review_decision
    )

    result = service.continue_task()

    assert result["state"] == "WAITING_FOR_OWNER"
    service.orchestrator.clear_pause_request.assert_called_once_with()
    apply_review_decision.assert_called_once_with(service.orchestrator, "revise", False)
    start_worker.assert_called_once()


def test_archive_previous_run_preserves_undelivered_worktree_then_archives(tmp_path):
    orchestrator = AgentOrchestrator(tmp_path)
    orchestrator.ai_dir.mkdir(parents=True, exist_ok=True)
    orchestrator.state_path.write_text(
        json.dumps({"state": "COMPLETED", "delivery": {"delivered": False}}),
        encoding="utf-8",
    )
    (orchestrator.ai_dir / "worktree").mkdir()

    assert gui.archive_previous_run(orchestrator) is False
    assert not (tmp_path / ".ai-company-archive").exists()

    (orchestrator.ai_dir / "worktree").rmdir()
    orchestrator.state_path.write_text(
        json.dumps({"state": "COMPLETED", "delivery": {"delivered": True}}),
        encoding="utf-8",
    )
    orchestrator.request_path.write_text("old request", encoding="utf-8")
    orchestrator.plan_path.write_text("old plan", encoding="utf-8")
    orchestrator.final_report_path.write_text("done", encoding="utf-8")

    assert gui.archive_previous_run(orchestrator) is True
    archive = next((tmp_path / ".ai-company-archive").iterdir())
    assert (archive / "state.json").exists()
    assert (archive / "request.md").read_text(encoding="utf-8") == "old request"
    assert (archive / "implementation_plan.md").read_text(
        encoding="utf-8"
    ) == "old plan"
    assert (archive / "final_report.md").read_text(encoding="utf-8") == "done"


def test_orchestrator_cli_gui_entrypoint_uses_the_new_service(monkeypatch, tmp_path):
    from orchestrator import main as orchestrator_main

    serve = Mock()
    monkeypatch.setattr(gui, "serve", serve)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "gui", "--port", "8765"])

    orchestrator_main.main()

    serve.assert_called_once_with(tmp_path, 8765)


def test_cli_delivery_requires_displayed_snapshot_and_merges_only_fresh_preview(
    tmp_path, monkeypatch, capsys
):
    from orchestrator import main as orchestrator_main
    from orchestrator.core.state import delivery_snapshot_id

    service = make_worktree_delivery_service(tmp_path)
    orchestrator = service.orchestrator
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        orchestrator_main, "AgentOrchestrator", lambda _workspace: orchestrator
    )
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "deliver"])

    assert orchestrator_main.main() == 0
    first_preview = dict(orchestrator.state["delivery_preview"])
    first_id = delivery_snapshot_id(first_preview)
    assert orchestrator.state["delivery_preview_presented"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "base\n"
    assert first_id in capsys.readouterr().out

    worktree = orchestrator.ai_dir / "worktree"
    (worktree / "app.py").write_text("updated feature\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["orchestrator.py", "deliver", "--confirm", first_id]
    )

    assert orchestrator_main.main() == 1
    updated_preview = dict(orchestrator.state["delivery_preview"])
    updated_id = delivery_snapshot_id(updated_preview)
    assert updated_id != first_id
    assert orchestrator.state["delivery_preview_presented"] is True
    assert orchestrator.state.get("delivery_confirmed", False) is False
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "base\n"
    assert updated_id in capsys.readouterr().out

    monkeypatch.setattr(
        "sys.argv", ["orchestrator.py", "deliver", "--confirm", updated_id]
    )
    assert orchestrator_main.main() == 0

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "updated feature\n"
    assert orchestrator.state["state"] == "COMPLETED"
    assert orchestrator.state["delivery"]["delivered"] is True


def test_cli_start_archives_completed_run_before_clearing_reports(
    tmp_path, monkeypatch
):
    from orchestrator import main as orchestrator_main

    orchestrator = AgentOrchestrator(tmp_path)
    orchestrator.init_project()
    orchestrator.state = {
        "state": "COMPLETED",
        "delivery": {"delivered": True},
    }
    orchestrator.save_state()
    orchestrator.final_report_path.write_text("previous result", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        orchestrator_main,
        "AgentOrchestrator",
        lambda _workspace: orchestrator,
    )
    monkeypatch.setattr(orchestrator_main, "configure_ollama", lambda _app: None)
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "start", "new test task"])

    orchestrator_main.main()

    archive = next((tmp_path / ".ai-company-archive").iterdir())
    assert (archive / "final_report.md").read_text(
        encoding="utf-8"
    ) == "previous result"
    assert orchestrator.state["state"] == "PLANNING"
    assert orchestrator.request_path.read_text(encoding="utf-8") == "new test task"


ARCHITECT_APPROVAL = (
    "Architect approved the plan. The original request requires "
    "owner approval before implementation."
)
APPROVED_TITLE = "計畫已通過，等待擁有者核准實作"
REJECTED_TITLE = "審核未通過，等待修訂"
UNCERTAIN_TITLE = "等待擁有者檢視，目前資料不足以判定審核結果"
ENVIRONMENT_TITLE = "測試環境待處理，尚未完成驗證"
DELIVERY_PREVIEW = {"diff_stat": "1 file changed", "git_status": " M app.py"}


class GuiPage(HTMLParser):
    """Read rendered controls and their regions without a browser dependency."""

    def __init__(self, page):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.handle_data("\n")
        node = {
            "tag": tag,
            "attrs": dict(attrs),
            "ancestors": tuple(parent["attrs"].get("id") for parent in self.stack),
            "parent": self.stack[-1] if self.stack else None,
            "text": "",
        }
        self.nodes.append(node)
        if tag not in {"meta", "link", "input", "br", "hr", "img"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data

    def by_id(self, identity):
        matches = [node for node in self.nodes if node["attrs"].get("id") == identity]
        assert len(matches) == 1, identity
        return matches[0]

    @property
    def buttons(self):
        return [node for node in self.nodes if node["tag"] == "button"]


def assert_gui_decision(page, title, label=None, action=None):
    document = GuiPage(page)
    headings = [
        node["text"]
        for node in document.nodes
        if node["tag"] == "h3" and "decision-copy" in node["ancestors"]
    ]
    assert headings == [title]
    assert '<html lang="zh-Hant">' in page
    assert "<h1>AI Company Orchestrator</h1>" in page
    assert document.by_id("overview-title")["text"] == "任務概況"
    assert document.by_id("next-title")["text"] == "下一步"
    assert document.by_id("delivery-title")["text"] == "交付展示與確認"
    primary = [
        node
        for node in document.buttons
        if "primary" in node["attrs"].get("class", "").split()
        and "disabled" not in node["attrs"]
    ]
    assert len(primary) == (0 if label is None else 1)
    for node in document.buttons:
        attrs = node["attrs"]
        assert attrs["data-enabled"] in {"true", "false"}
        assert ("disabled" not in attrs) == (attrs["data-enabled"] == "true")
    identities = [node["attrs"]["data-action"] for node in document.buttons]
    assert len(identities) == len(set(identities))
    if primary:
        control = primary[0]
        assert control["text"] == label
        assert control["attrs"]["data-action"] == action
        if action == "task{}":
            assert control["attrs"]["type"] == "submit"
            assert "task-form" in control["ancestors"]
            assert "await post('/api/task', {prompt})" in page
        elif action == "confirm-delivery":
            assert control["attrs"]["onclick"] == (
                "post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})"
            )
            snapshot_id = control["attrs"]["data-snapshot-id"]
            assert snapshot_id
            region = document.by_id("git")
            assert snapshot_id == region["attrs"]["data-snapshot-id"]
            assert "git" in control["ancestors"]
            actions = document.by_id("delivery-actions")
            assert control["parent"] is actions
            impact = actions["parent"]
            assert impact["tag"] == "details"
            assert impact["attrs"]["data-detail"] == "git-impact"
            assert "data-preview" in impact["parent"]["attrs"]
            contents = [
                node
                for node in document.nodes
                if node["tag"] == "pre" and node["parent"] is impact
            ]
            assert len(contents) == 1
            assert snapshot_id in impact["text"]
            node = control
            while node is not None:
                assert "hidden" not in node["attrs"]
                assert node["attrs"].get("aria-hidden") != "true"
                if node["tag"] == "details":
                    assert "open" in node["attrs"]
                node = node["parent"]
        else:
            endpoint, payload = action.split("{", 1)
            expected = (
                f"post('/api/{endpoint}', {json.dumps(json.loads('{' + payload))})"
            )
            assert control["attrs"]["onclick"] == expected
    return document


def owner_status(source, reason, resume_state, pass_state, **fields):
    return {
        "state": "WAITING_FOR_OWNER",
        "plan_requires_owner_approval": True,
        "review": {"source": source, "reason": reason},
        "resume_state": resume_state,
        "pass_state": pass_state,
        **fields,
    }


OWNER_REVIEW_CASES = [
    pytest.param(
        "Architect",
        ARCHITECT_APPROVAL + "\n\nPLAN_STATUS: REJECTED\nHistorical example.",
        "IMPLEMENTING",
        "IMPLEMENTING",
        APPROVED_TITLE,
        "核准實作",
        "approve{}",
        id="architect-approved-with-embedded-rejection",
    ),
    pytest.param(
        "Architect",
        "PLAN_STATUS: REJECTED\nRevise app.py.",
        "DEVELOPING_PLAN",
        "IMPLEMENTING",
        REJECTED_TITLE,
        "提交修訂",
        'review{"decision":"revise"}',
        id="architect-rejected",
    ),
    pytest.param(
        "Manager",
        "PLAN_STATUS: REJECTED\nRevise app.py.",
        "DEVELOPING_PLAN",
        "IMPLEMENTING",
        REJECTED_TITLE,
        "提交修訂",
        'review{"decision":"revise"}',
        id="manager-rejected",
    ),
    pytest.param(
        "Reviewer",
        "REJECTED\nFix app.py.",
        "IMPLEMENTING",
        "IMPLEMENTING",
        REJECTED_TITLE,
        "提交修訂",
        'review{"decision":"revise"}',
        id="reviewer-rejected",
    ),
    pytest.param(
        "QA",
        "QA_STATUS: FAILED\nFix app.py.",
        "IMPLEMENTING",
        "REVIEWING_CODE",
        REJECTED_TITLE,
        "提交修訂",
        'review{"decision":"revise"}',
        id="qa-failed",
    ),
    pytest.param(
        "QA",
        "## QA-1 (senior)\nQA_STATUS: FAILED\nFix app.py.",
        "IMPLEMENTING",
        "REVIEWING_CODE",
        REJECTED_TITLE,
        "提交修訂",
        'review{"decision":"revise"}',
        id="qa-worker-report-failed",
    ),
    *[
        pytest.param(
            "QA",
            f"Test command failed to launch (exit {exit_code}), so no tests were executed.\n"
            "Command: unavailable-test-runner\nFix test_command, then approve.",
            "TESTING",
            "TESTING",
            ENVIRONMENT_TITLE,
            "重試 QA 驗證",
            "approve{}",
            id=f"qa-launch-{exit_code}",
        )
        for exit_code in (126, 127)
    ],
    pytest.param(
        "QA",
        "QA reports that test failures/skips are caused by a missing "
        "environment dependency, not a code bug.\nDEPENDENCY_INSTALL_NEEDED: install runner",
        "TESTING",
        "TESTING",
        ENVIRONMENT_TITLE,
        "重試 QA 驗證",
        "approve{}",
        id="qa-missing-dependency",
    ),
    pytest.param(
        "QA_RESPONSE",
        "QA output contract failed after one correction: QA-1. "
        "No code defect was established by these responses.",
        "TESTING",
        "TESTING",
        "已暫停，等待擁有者決策",
        "重試 QA 驗證",
        "continue{}",
        id="qa-response-contract",
    ),
    pytest.param(
        "Architect",
        "Review evidence is incomplete.",
        "IMPLEMENTING",
        "IMPLEMENTING",
        UNCERTAIN_TITLE,
        None,
        None,
        id="insufficient-evidence",
    ),
    pytest.param(
        "Architect",
        ARCHITECT_APPROVAL,
        "DEVELOPING_PLAN",
        "IMPLEMENTING",
        UNCERTAIN_TITLE,
        None,
        None,
        id="contradictory-transition",
    ),
]


@pytest.mark.parametrize(
    "source,reason,resume_state,pass_state,title,label,action", OWNER_REVIEW_CASES
)
def test_offline_owner_decision_matrix(
    source, reason, resume_state, pass_state, title, label, action
):
    page = gui.render_page(owner_status(source, reason, resume_state, pass_state))
    document = assert_gui_decision(page, title, label, action)
    assert "hidden" in document.by_id("task-form")["attrs"]
    assert document.by_id("review-reason")["text"] == reason
    if title == UNCERTAIN_TITLE:
        controls = {node["text"]: node["attrs"] for node in document.buttons}
        assert "window.confirm" in controls["接受目前結果並繼續"]["onclick"]
        assert "window.confirm" in controls["依既有來源規則繼續（將修訂）"]["onclick"]
    if title == ENVIRONMENT_TITLE:
        assert all(
            node["attrs"]["data-action"] != 'review{"decision":"revise"}'
            for node in document.buttons
        )


@pytest.mark.parametrize(
    "fields,title,label,action",
    [
        pytest.param(
            {"state": "PLANNING", "can_submit": True},
            "提交新任務",
            "提交任務",
            "task{}",
            id="initial-can-submit",
        ),
        pytest.param(
            {"state": "PLANNING", "can_submit": False},
            "等待下一步",
            "繼續執行",
            "continue{}",
            id="saved-request-cannot-submit",
        ),
        pytest.param(
            {"state": "IMPLEMENTING", "worker_running": True},
            "執行中",
            "目前步驟完成後停止",
            "stop{}",
            id="running",
        ),
        pytest.param(
            {"state": "IMPLEMENTING", "worker_running": True, "pause_requested": True},
            "目前步驟完成後暫停",
            None,
            None,
            id="stop-pending",
        ),
        pytest.param(
            owner_status(
                "Orchestrator",
                "Paused at step boundary.",
                "IMPLEMENTING",
                "IMPLEMENTING",
            ),
            "已暫停，等待擁有者決策",
            "繼續目前任務",
            "continue{}",
            id="safely-paused",
        ),
        pytest.param(
            {"state": "WAITING_FOR_OWNER", "worker_running": True},
            "執行中",
            None,
            None,
            id="owner-decision-still-running",
        ),
        pytest.param(
            {"state": "WAITING_FOR_DELIVERY", "delivery_artifacts_prepared": False},
            "準備交付資料中",
            "準備並展示 Git 影響",
            "deliver{}",
            id="delivery-preparation",
        ),
        pytest.param(
            {"state": "WAITING_FOR_DELIVERY", "worker_running": True},
            "正在準備交付資料",
            None,
            None,
            id="preparation-running",
        ),
        pytest.param(
            {"state": "WAITING_FOR_DELIVERY", "delivery_artifacts_prepared": True},
            "等待展示 Git 影響",
            "展示 Git 影響",
            "deliver{}",
            id="awaiting-display",
        ),
        pytest.param(
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": True,
                "delivery_preview_presented": True,
                "delivery_snapshot_id": "snapshot",
                "git": DELIVERY_PREVIEW,
            },
            "已展示 Git 影響，等待二次確認",
            "確認二次交付",
            "confirm-delivery",
            id="valid-display",
        ),
        pytest.param(
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": False,
                "delivery_preview_presented": True,
                "delivery_snapshot_id": "snapshot",
                "git": DELIVERY_PREVIEW,
            },
            "準備交付資料中",
            "準備並展示 Git 影響",
            "deliver{}",
            id="artifacts-invalidated",
        ),
        pytest.param(
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": True,
                "delivery_preview_presented": False,
                "delivery_snapshot_id": "stale",
                "git": DELIVERY_PREVIEW,
                "worker_status": {"phase": "failed", "error": "Git impact changed"},
            },
            "等待展示 Git 影響",
            "展示 Git 影響",
            "deliver{}",
            id="display-invalidated",
        ),
        pytest.param(
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": True,
                "delivery_preview_presented": True,
                "delivery_snapshot_id": "snapshot",
                "git": DELIVERY_PREVIEW,
                "worker_running": True,
            },
            "正在驗證並處理交付",
            None,
            None,
            id="confirmation-running",
        ),
        pytest.param(
            {"state": "COMPLETED", "delivery": {"delivered": True}, "can_submit": True},
            "已完成交付",
            "提交任務",
            "task{}",
            id="actually-delivered",
        ),
        pytest.param(
            {
                "state": "COMPLETED",
                "delivery": {"delivered": False},
                "can_submit": False,
            },
            "流程已完成，交付結果待檢視",
            None,
            None,
            id="completion-is-not-delivery",
        ),
        pytest.param(
            {"state": "FAILED", "can_submit": True},
            "流程失敗",
            "提交任務",
            "task{}",
            id="failed",
        ),
    ],
)
def test_offline_lifecycle_decision_matrix(fields, title, label, action):
    document = assert_gui_decision(gui.render_page(fields), title, label, action)
    assert ("hidden" not in document.by_id("task-form")["attrs"]) == (
        action == "task{}"
    )
    delivery_controls = [
        node for node in document.buttons if "delivery-actions" in node["ancestors"]
    ]
    if fields.get("worker_running"):
        assert all("disabled" in node["attrs"] for node in delivery_controls)
    if fields.get("state") == "WAITING_FOR_DELIVERY":
        assert all("git" in node["ancestors"] for node in delivery_controls)
        confirmations = [
            node
            for node in delivery_controls
            if node["attrs"]["data-action"] == "confirm-delivery"
        ]
        if not fields.get("delivery_preview_presented"):
            assert confirmations == []
        for node in confirmations:
            assert ("disabled" not in node["attrs"]) == (
                fields.get("delivery_artifacts_prepared") is True
                and not fields.get("worker_running")
            )


@pytest.mark.parametrize(
    "change",
    [
        {"review": {"source": "Architect", "reason": ""}},
        {
            "review": {
                "source": "Architect",
                "reason": "Architect verification requires human review.",
            }
        },
        {
            "review": {
                "source": "Architect",
                "reason": "Plan example:\nPLAN_STATUS: REJECTED",
            }
        },
        {
            "review": {
                "source": "Architect",
                "reason": "PLAN_STATUS: APPROVED\n\nPLAN_STATUS: REJECTED",
            }
        },
        {"review": {"source": "Manager", "reason": ARCHITECT_APPROVAL}},
        {"review": {"source": "Reviewer", "reason": ARCHITECT_APPROVAL}},
        {"review": {"source": "Architect rejected", "reason": "Review pending."}},
        {
            "review": {
                "source": "QA",
                "reason": "QA_STATUS: PASSED\n\nQA_STATUS: FAILED",
            }
        },
        {"resume_state": "DEVELOPING_PLAN"},
        {"pass_state": "TESTING"},
        {"plan_requires_owner_approval": False},
        {"plan_requires_owner_approval": None},
        {"plan_requires_owner_approval": "true"},
    ],
)
def test_source_names_history_and_conflicting_evidence_do_not_invent_verdicts(change):
    status = owner_status(
        "Architect", ARCHITECT_APPROVAL, "IMPLEMENTING", "IMPLEMENTING"
    )
    status.update(change)
    status["reports"] = {
        "review": "PLAN_STATUS: REJECTED\nArchitect 驗證未通過，需要人工確認。",
        "qa": "QA_STATUS: FAILED",
        "plan": ARCHITECT_APPROVAL + "\nPLAN_STATUS: REJECTED",
        "final": "APPROVED\nPrevious run completed.",
    }
    document = GuiPage(gui.render_page(status))
    decision = document.by_id("decision-copy")["text"]
    assert APPROVED_TITLE not in decision
    assert REJECTED_TITLE not in decision
    expected = (
        UNCERTAIN_TITLE
        if status["review"]["source"] in {"Architect", "Manager", "Reviewer", "QA"}
        else "已暫停，等待擁有者決策"
    )
    assert expected in decision
    assert all(
        node["attrs"]["data-action"] not in {"approve{}", 'review{"decision":"revise"}'}
        for node in document.buttons
        if node["attrs"].get("class") == "primary"
    )
    assert status["reports"]["review"] in document.by_id("reports")["text"]


@pytest.mark.parametrize(
    "missing", ["delivery_snapshot_id", "git", "delivery_preview_presented"]
)
def test_delivery_confirmation_requires_complete_display_evidence(missing):
    status = {
        "state": "WAITING_FOR_DELIVERY",
        "delivery_artifacts_prepared": True,
        "delivery_preview_presented": True,
        "delivery_snapshot_id": "snapshot",
        "git": DELIVERY_PREVIEW,
    }
    status.pop(missing)
    document = assert_gui_decision(
        gui.render_page(status), "等待展示 Git 影響", "展示 Git 影響", "deliver{}"
    )
    assert not any(
        node["attrs"]["data-action"] == "confirm-delivery" for node in document.buttons
    )


def test_full_task_review_and_reports_remain_expandable_and_escaped():
    description = "完整任務描述 " + "長" * 150 + "\n最後一行 <script>alert(1)</script>"
    reason = ARCHITECT_APPROVAL + "\n\n" + description
    reports = {
        "review": "# 人工審核報告\nArchitect 驗證未通過，需要人工確認。",
        "tests": "測試結果\n" + description,
        "qa": "QA 報告\n" + description,
        "plan": "實作計畫\n" + description,
        "final": "最終報告\n" + description,
    }
    page = gui.render_page(
        owner_status(
            "Architect",
            reason,
            "IMPLEMENTING",
            "IMPLEMENTING",
            progress={
                "tasks": [{"id": "T1", "status": "DONE", "description": description}]
            },
            reports=reports,
        )
    )
    document = assert_gui_decision(page, APPROVED_TITLE, "核准實作", "approve{}")
    assert "<script>alert(1)</script>" not in page
    assert document.by_id("review-reason")["text"] == reason
    assert document.by_id("current-review")["tag"] == "details"
    items = [node for node in document.nodes if "data-task" in node["attrs"]]
    assert len(items) == 1
    task = next(
        node
        for node in document.nodes
        if node["attrs"].get("data-detail") == items[0]["attrs"]["data-task"]
    )
    assert task["tag"] == "details"
    assert task["parent"] is items[0]
    assert description in task["text"]
    panels = [
        node
        for node in document.nodes
        if node["tag"] == "details" and "data-report" in node["attrs"]
    ]
    assert {node["attrs"]["data-report"] for node in panels} == set(reports)
    for panel in panels:
        assert "open" not in panel["attrs"]
        assert reports[panel["attrs"]["data-report"]] in panel["text"]
    assert "已完成" in document.by_id("tasks")["text"]


@pytest.fixture
def owner_review_service(tmp_path):
    def create(
        source="Architect",
        reason=ARCHITECT_APPROVAL,
        resume_state="IMPLEMENTING",
        pass_state="IMPLEMENTING",
    ):
        service = gui.GuiService(tmp_path)
        orchestrator = service.orchestrator
        orchestrator.ai_dir.mkdir(parents=True, exist_ok=True)
        orchestrator.config["max_revisions"] = 2
        orchestrator.config_path.write_text(
            json.dumps(orchestrator.config), encoding="utf-8"
        )
        orchestrator.request_path.write_text(
            "請核准後再實作 app.py。", encoding="utf-8"
        )
        orchestrator.state.update(
            {
                "plan_requires_owner_approval": True,
                "tasks": [
                    {"id": "T1", "status": "completed", "target_files": ["app.py"]}
                ],
            }
        )
        orchestrator.pause_for_human_review(source, reason, resume_state, pass_state)
        # Exercise saved runs from before explicit decision kinds existed.
        orchestrator.state.pop("human_review_kind", None)
        orchestrator.save_state()
        orchestrator.request_pause()
        orchestrator.run_to_end = Mock(
            side_effect=AssertionError("Offline contract tests must not run agents")
        )
        start_worker_inline(service)
        return service

    return create


def post_action(service, route, body=None):
    handler = make_handler(
        "POST",
        {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Cookie": f"gui_csrf={service.csrf_token}",
            "X-CSRF-Token": service.csrf_token,
        },
        route,
        service,
    )
    result = post_json(handler, {} if body is None else body)
    return handler.send_response.call_args.args[0], result


@pytest.mark.parametrize(
    "source,reason,resume_state,pass_state,title,label,action", OWNER_REVIEW_CASES
)
def test_persisted_pause_evidence_reaches_status_and_http_page(
    owner_review_service, source, reason, resume_state, pass_state, title, label, action
):
    service = owner_review_service(source, reason, resume_state, pass_state)
    status = service.status()
    assert status["review"] == {"source": source, "reason": reason}
    assert status["resume_state"] == resume_state
    assert status["pass_state"] == pass_state
    assert status["plan_requires_owner_approval"] is True
    assert status["pause_requested"] is True
    assert status["can_submit"] is False
    assert status["reports"][
        "review"
    ] == service.orchestrator.human_report_path.read_text(encoding="utf-8")
    assert_gui_decision(gui.render_page(status), title, label, action)

    handler = make_handler("GET", {"Host": "127.0.0.1:8765"}, "/", service)
    gui.RequestHandler.do_GET(handler)
    assert handler.send_response.call_args.args[0] == gui.HTTPStatus.OK
    assert_gui_decision(handler.wfile.getvalue().decode("utf-8"), title, label, action)
    handler = make_handler("GET", {"Host": "127.0.0.1:8765"}, "/api/status", service)
    gui.RequestHandler.do_GET(handler)
    persisted = json.loads(handler.wfile.getvalue())
    assert persisted["review"] == status["review"]
    assert persisted["pass_state"] == pass_state
    assert persisted["reports"]["review"] == status["reports"]["review"]
    assert persisted["csrf"] == service.csrf_token


def test_http_approve_uses_recorded_implementing_path_and_rejects_duplicate(
    owner_review_service,
):
    service = owner_review_service(
        reason=ARCHITECT_APPROVAL + "\n\nPLAN_STATUS: REJECTED"
    )
    orchestrator = service.orchestrator
    before_tasks = list(orchestrator.state["tasks"])
    code, status = post_action(service, "/api/approve")
    assert code == gui.HTTPStatus.OK
    assert status["state"] == "IMPLEMENTING"
    assert status["pause_requested"] is False
    assert status["progress"]["tasks"] == before_tasks
    assert orchestrator.read_state_snapshot()["state"] == "IMPLEMENTING"
    assert "human_review_source" not in orchestrator.state
    before = orchestrator.state_path.read_bytes()
    code, result = post_action(service, "/api/approve")
    assert code == gui.HTTPStatus.CONFLICT
    assert "not waiting for owner approval" in result["error"]
    assert orchestrator.state_path.read_bytes() == before
    orchestrator.run_to_end.assert_not_called()


@pytest.mark.parametrize("source", ["Architect", "Manager", "Reviewer", "QA"])
@pytest.mark.parametrize("revisions", [0, 1, 2, 3])
def test_http_revise_preserves_existing_source_routes_and_revision_budget(
    owner_review_service, source, revisions
):
    reason = (
        "PLAN_STATUS: REJECTED\nFix app.py."
        if source in {"Architect", "Manager"}
        else (
            "REJECTED\nFix app.py."
            if source == "Reviewer"
            else "QA_STATUS: FAILED\nFix app.py."
        )
    )
    service = owner_review_service(
        source,
        reason,
        "DEVELOPING_PLAN" if source in {"Architect", "Manager"} else "IMPLEMENTING",
        "REVIEWING_CODE" if source == "QA" else "IMPLEMENTING",
    )
    orchestrator = service.orchestrator
    orchestrator.state.update(
        {
            "code_revisions": revisions,
            "plan_revisions": 2,
            "pending_plan_feedback": "Previous plan feedback.",
        }
    )
    orchestrator.save_state()
    before_tasks = list(orchestrator.state["tasks"])
    code, status = post_action(service, "/api/review", {"decision": "revise"})
    assert code == gui.HTTPStatus.OK
    expected_state = (
        "DEVELOPING_PLAN" if source in {"Architect", "Manager"} else "IMPLEMENTING"
    )
    assert status["state"] == expected_state
    assert status["pause_requested"] is False
    if source in {"Architect", "Manager"}:
        assert orchestrator.state["tasks"] == before_tasks
        assert orchestrator.state["plan_revisions"] == 0
        assert orchestrator.state["code_revisions"] == revisions
        assert (
            orchestrator.state["pending_plan_feedback"]
            == "Previous plan feedback.\n\n" + reason
        )
    else:
        expected_revisions = 1 if revisions >= 2 else revisions + 1
        assert orchestrator.state["code_revisions"] == expected_revisions
        assert orchestrator.state["plan_revisions"] == 2
        assert orchestrator.state["tasks"][:-1] == before_tasks
        task = orchestrator.state["tasks"][-1]
        assert task["id"] == f"HUMAN-REVIEW-{expected_revisions}"
        assert task["status"] == "pending"
        assert task["target_files"] == ["app.py"]
        assert reason in task["description"]
    assert orchestrator.read_state_snapshot()["state"] == expected_state
    before = orchestrator.state_path.read_bytes()
    code, result = post_action(service, "/api/review", {"decision": "revise"})
    assert code == gui.HTTPStatus.CONFLICT
    assert "not waiting for owner review" in result["error"]
    assert orchestrator.state_path.read_bytes() == before
    orchestrator.run_to_end.assert_not_called()


@pytest.mark.parametrize(
    "source,reason,route",
    [
        (
            "QA",
            f"Test command failed to launch (exit {code}), so no tests were executed.",
            "/api/approve",
        )
        for code in (126, 127)
    ]
    + [
        (
            "QA",
            "QA reports that test failures/skips are caused by a missing "
            "environment dependency, not a code bug.",
            "/api/approve",
        ),
        (
            "QA_RESPONSE",
            "QA output contract failed after one correction.",
            "/api/continue",
        ),
    ],
)
def test_http_qa_retry_returns_to_testing_without_creating_fix_tasks(
    owner_review_service, source, reason, route
):
    service = owner_review_service(source, reason, "TESTING", "TESTING")
    orchestrator = service.orchestrator
    orchestrator.state["code_revisions"] = 2
    orchestrator.save_state()
    before_tasks = list(orchestrator.state["tasks"])
    code, status = post_action(service, route)
    assert code == gui.HTTPStatus.OK
    assert status["state"] == "TESTING"
    assert status["pause_requested"] is False
    assert status["progress"]["tasks"] == before_tasks
    assert status["progress"]["code_revisions"] == 2
    assert orchestrator.read_state_snapshot()["state"] == "TESTING"
    orchestrator.run_to_end.assert_not_called()


@pytest.mark.parametrize("route", ["/api/approve", "/api/review"])
@pytest.mark.parametrize(
    "blocked",
    [
        "worker",
        "operation",
        "IMPLEMENTING",
        "FAILED",
        "COMPLETED",
        "WAITING_FOR_DELIVERY",
    ],
)
def test_http_rejects_busy_or_invalid_owner_decisions_without_mutation(
    owner_review_service, route, blocked
):
    service = owner_review_service()
    orchestrator = service.orchestrator
    if blocked == "worker":
        service.worker = SimpleNamespace(is_alive=lambda: True)
    elif blocked == "operation":
        service.operation_lock.acquire()
    else:
        orchestrator.state["state"] = blocked
        orchestrator.save_state()
    before = orchestrator.state_path.read_bytes()
    pause = orchestrator.pause_request_path.read_bytes()
    try:
        code, result = post_action(service, route, {"decision": "revise"})
        assert code == gui.HTTPStatus.CONFLICT
        assert result["error"]
        assert orchestrator.state_path.read_bytes() == before
        assert orchestrator.pause_request_path.read_bytes() == pause
        assert orchestrator.pause_requested() is True
        orchestrator.run_to_end.assert_not_called()
        if blocked in {"worker", "operation"}:
            service._start_worker.assert_not_called()
    finally:
        if blocked == "operation":
            service.operation_lock.release()


@pytest.mark.parametrize("route", ["/api/approve", "/api/review"])
def test_http_owner_decision_respects_a_lock_held_by_another_process(
    owner_review_service, route
):
    service = owner_review_service()
    orchestrator = service.orchestrator
    del service._start_worker
    before = orchestrator.state_path.read_bytes()
    pause = orchestrator.pause_request_path.read_bytes()
    child_code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from orchestrator.core.state import AgentOrchestrator\n"
        "app = AgentOrchestrator(Path(sys.argv[1]))\n"
        "with app.execution_lock():\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.read(1)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-u", "-c", child_code, str(service.workspace)],
        cwd=Path(gui.__file__).resolve().parent.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = threading.Event()
    output = []

    def read_ready():
        output.append(holder.stdout.readline().strip())
        ready.set()

    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    try:
        assert ready.wait(timeout=5), "Child did not acquire the execution lock"
        assert output == ["locked"]
        assert service.status()["worker_running"] is True
        code, result = post_action(service, route, {"decision": "revise"})
        assert code == gui.HTTPStatus.CONFLICT
        assert "already in progress" in result["error"]
        finish_worker(service)
        assert orchestrator.state_path.read_bytes() == before
        assert orchestrator.pause_request_path.read_bytes() == pause
        orchestrator.run_to_end.assert_not_called()
    finally:
        holder.stdin.close()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=5)
        reader.join(timeout=2)
        holder.stdout.close()
        holder.stderr.close()
    assert holder.returncode == 0
    start_worker_inline(service)
    code, status = post_action(service, route, {"decision": "revise"})
    assert code == gui.HTTPStatus.OK
    assert status["state"] == (
        "IMPLEMENTING" if route == "/api/approve" else "DEVELOPING_PLAN"
    )


def test_status_can_submit_follows_saved_request_and_running_worker(tmp_path):
    service = gui.GuiService(tmp_path)
    assert service.status()["can_submit"] is True
    orchestrator = service.orchestrator
    orchestrator.ai_dir.mkdir(parents=True, exist_ok=True)
    orchestrator.request_path.write_text("Saved request", encoding="utf-8")
    orchestrator.save_state()
    assert service.status()["can_submit"] is False
    orchestrator.state.update({"state": "COMPLETED", "delivery": {"delivered": True}})
    orchestrator.save_state()
    assert service.status()["can_submit"] is True
    service.worker = SimpleNamespace(is_alive=lambda: True)
    assert service.status()["can_submit"] is False


def test_http_stop_waits_for_real_safe_boundary_and_continue_uses_saved_state(
    owner_review_service,
):
    service = owner_review_service()
    orchestrator = service.orchestrator
    orchestrator.clear_pause_request()
    orchestrator.state["state"] = "IMPLEMENTING"
    orchestrator.save_state()
    service.worker = SimpleNamespace(is_alive=lambda: True)
    code, status = post_action(service, "/api/stop")
    assert code == gui.HTTPStatus.OK
    assert status["state"] == "IMPLEMENTING"
    assert status["pause_status"] == "目前步驟完成後暫停"
    assert_gui_decision(gui.render_page(status), "目前步驟完成後暫停")
    code, result = post_action(service, "/api/stop")
    assert code == gui.HTTPStatus.CONFLICT
    assert "already been requested" in result["error"]
    with orchestrator.execution_lock():
        assert orchestrator.check_pause_at_step_boundary() is True
    service.worker = None
    status = service.status()
    assert status["state"] == "WAITING_FOR_OWNER"
    assert status["resume_state"] == "IMPLEMENTING"
    assert status["pass_state"] == "IMPLEMENTING"
    assert status["pause_requested"] is True
    assert_gui_decision(
        gui.render_page(status), "已暫停，等待擁有者決策", "繼續目前任務", "continue{}"
    )
    code, status = post_action(service, "/api/continue")
    assert code == gui.HTTPStatus.OK
    assert status["state"] == "IMPLEMENTING"
    assert status["pause_requested"] is False


@pytest.mark.parametrize("external_holder", [False, True])
def test_delivery_preview_samples_status_after_releasing_its_own_lock(
    tmp_path, monkeypatch, external_holder
):
    service = make_worktree_delivery_service(tmp_path)
    orchestrator = service.orchestrator
    contender = AgentOrchestrator(tmp_path)
    own_lock_released = threading.Event()
    acquired = threading.Event()
    release = threading.Event()
    errors = []
    samples = []
    execution_lock = orchestrator.execution_lock
    status = service.status

    def hold_external_lock():
        try:
            assert own_lock_released.wait(timeout=10)
            with contender.execution_lock():
                acquired.set()
                assert release.wait(timeout=10)
        except Exception as error:
            errors.append(error)

    lock_depth = 0

    @contextmanager
    def observed_execution_lock():
        nonlocal lock_depth
        lock_depth += 1
        try:
            with execution_lock():
                yield
        finally:
            lock_depth -= 1
            if lock_depth == 0:
                own_lock_released.set()
        if external_holder and lock_depth == 0:
            assert acquired.wait(timeout=10)

    def sample_status():
        assert own_lock_released.is_set(), "Status sampled inside the display lock"
        result = status()
        samples.append(result["worker_running"])
        return result

    monkeypatch.setattr(orchestrator, "execution_lock", observed_execution_lock)
    monkeypatch.setattr(service, "status", sample_status)
    holder = threading.Thread(target=hold_external_lock, daemon=True)
    if external_holder:
        holder.start()
    try:
        code, presented = post_action(service, "/api/deliver", {"confirm": False})
        assert code == gui.HTTPStatus.OK
        assert presented["delivery_confirmation_required"] is True
        assert samples == [external_holder]
        assert bool(presented["external_lock"]) is external_holder
        assert presented["delivery_snapshot_id"]
        if external_holder:
            document = assert_gui_decision(
                gui.render_page(presented), "其他執行程序持有工作流程鎖"
            )
            assert all("disabled" in node["attrs"] for node in document.buttons)
        else:
            assert_gui_decision(
                gui.render_page(presented),
                "已展示 Git 影響，等待二次確認",
                "確認二次交付",
                "confirm-delivery",
            )
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "base\n"
        assert orchestrator.state["delivery_confirmed"] is False
        service._start_worker.assert_not_called()
        if external_holder:
            before = orchestrator.state_path.read_bytes()
            code, blocked = post_action(
                service,
                "/api/deliver",
                {"confirm": True, "snapshot_id": presented["delivery_snapshot_id"]},
            )
            assert code == gui.HTTPStatus.CONFLICT
            assert "already in progress" in blocked["error"]
            assert orchestrator.state_path.read_bytes() == before
            assert (tmp_path / "app.py").read_text(encoding="utf-8") == "base\n"
    finally:
        release.set()
        if external_holder:
            holder.join(timeout=5)
            assert not holder.is_alive()
    assert not errors, errors
    monkeypatch.setattr(service, "status", status)
    recovered = service.status()
    assert recovered["worker_running"] is False
    assert recovered["external_lock"] is None
    assert_gui_decision(
        gui.render_page(recovered),
        "已展示 Git 影響，等待二次確認",
        "確認二次交付",
        "confirm-delivery",
    )


@pytest.mark.parametrize("phase", ["preparing", "confirming"])
def test_http_delivery_blocks_operations_during_real_worker_phases(
    tmp_path, monkeypatch, phase
):
    service = make_worktree_delivery_service(tmp_path)
    orchestrator = service.orchestrator
    del service._start_worker
    started = threading.Event()
    release = threading.Event()

    def wait_for_release():
        started.set()
        assert release.wait(timeout=10), "Delivery phase was not released"

    if phase == "preparing":
        orchestrator.state.update(
            {"delivery_artifacts_prepared": False, "changelog_written": False}
        )
        orchestrator.request_path.write_text("request", encoding="utf-8")
        orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
        orchestrator.final_report_path.unlink()
        orchestrator.save_state()

        def prepare_summary(*_args):
            wait_for_release()
            return "summary"

        orchestrator.manager.call_manager = Mock(side_effect=prepare_summary)
        orchestrator.assistant.generate_meeting_memory = Mock(return_value="memory")
        orchestrator.assistant.generate_changelog = Mock(return_value="change")
        orchestrator.manager._review_worker_weights = Mock()
        monkeypatch.setattr("orchestrator.roles.manager.brain.remember", Mock())
        orchestrator.run_to_end = orchestrator.step_waiting_for_delivery
        title = "正在準備交付資料"
    else:
        presented = service.delivery(False)
        confirm_delivery = orchestrator.confirm_delivery

        def confirm_after_release(preview, operation):
            wait_for_release()
            return confirm_delivery(preview, operation)

        monkeypatch.setattr(orchestrator, "confirm_delivery", confirm_after_release)
        title = "正在驗證並處理交付"

    try:
        if phase == "preparing":
            assert service._start_worker() is True
        else:
            code, accepted = post_action(
                service,
                "/api/deliver",
                {"confirm": True, "snapshot_id": presented["delivery_snapshot_id"]},
            )
            assert code == gui.HTTPStatus.OK
            assert accepted["worker_running"] is True
            assert_gui_decision(gui.render_page(accepted), title)
        assert started.wait(timeout=5)
        assert service.worker is not None and service.worker.is_alive()
        code, busy = post_action(service, "/api/deliver", {"confirm": False})
        assert code == gui.HTTPStatus.CONFLICT
        assert "worker is already running" in busy["error"]
        current = service.status()
        assert current["worker_running"] is True
        document = assert_gui_decision(gui.render_page(current), title)
        assert all("disabled" in node["attrs"] for node in document.buttons)
        with pytest.raises(RuntimeError, match="already in progress"):
            with AgentOrchestrator(tmp_path).execution_lock():
                pass
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "base\n"
        assert orchestrator.state.get("delivery_confirmed", False) is False
    finally:
        release.set()
        completed = finish_worker(service)

    assert completed["worker_running"] is False
    assert completed["external_lock"] is None
    assert completed["worker_status"]["phase"] == "idle"
    if phase == "preparing":
        assert completed["delivery_artifacts_prepared"] is True
        assert completed["delivery_preview_presented"] is False
        assert_gui_decision(
            gui.render_page(completed),
            "等待展示 Git 影響",
            "展示 Git 影響",
            "deliver{}",
        )
        presented = service.delivery(False)
        assert presented["worker_running"] is False
        assert_gui_decision(
            gui.render_page(presented),
            "已展示 Git 影響，等待二次確認",
            "確認二次交付",
            "confirm-delivery",
        )
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "base\n"
    else:
        assert completed["state"] == "COMPLETED"
        assert completed["delivery"]["delivered"] is True
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "feature\n"


def test_http_worktree_delivery_revalidates_then_merges_and_rejects_duplicate(tmp_path):
    service = make_worktree_delivery_service(tmp_path)
    server, thread = start_test_server(service)
    try:
        code, presented = http_request(
            server, "POST", "/api/deliver", {"confirm": False}
        )
        assert code == gui.HTTPStatus.OK
        assert presented["delivery_confirmation_required"] is True
        assert presented["worker_running"] is False
        assert presented["external_lock"] is None
        assert presented["delivery_snapshot_id"]
        assert_gui_decision(
            gui.render_page(presented),
            "已展示 Git 影響，等待二次確認",
            "確認二次交付",
            "confirm-delivery",
        )
        code, current = http_request(server, "GET", "/api/status")
        assert code == gui.HTTPStatus.OK
        assert current["worker_running"] is False
        assert current["delivery_snapshot_id"] == presented["delivery_snapshot_id"]
        document = assert_gui_decision(
            gui.render_page(current),
            "已展示 Git 影響，等待二次確認",
            "確認二次交付",
            "confirm-delivery",
        )
        confirmation = next(
            node
            for node in document.buttons
            if node["attrs"]["data-action"] == "confirm-delivery"
        )
        assert "git" in confirmation["ancestors"]
        assert "delivery-actions" in confirmation["ancestors"]
        assert (
            confirmation["attrs"]["data-snapshot-id"]
            == presented["delivery_snapshot_id"]
        )
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "base\n"
        code, result = http_request(
            server, "POST", "/api/deliver", {"confirm": True, "snapshot_id": "wrong"}
        )
        assert code == gui.HTTPStatus.CONFLICT
        assert "does not match the displayed snapshot" in result["error"]
        assert service.orchestrator.state.get("delivery_confirmed", False) is False
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "base\n"
        code, completed = http_request(
            server,
            "POST",
            "/api/deliver",
            {"confirm": True, "snapshot_id": presented["delivery_snapshot_id"]},
        )
        assert code == gui.HTTPStatus.OK
        assert completed["state"] == "COMPLETED"
        assert completed["delivery"]["delivered"] is True
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "feature\n"
        committed = subprocess.run(
            ["git", "show", "HEAD:app.py"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert committed.stdout == "feature\n"
        assert_gui_decision(
            gui.render_page(completed), "已完成交付", "提交任務", "task{}"
        )
        code, duplicate = http_request(
            server,
            "POST",
            "/api/deliver",
            {"confirm": True, "snapshot_id": presented["delivery_snapshot_id"]},
        )
        assert code == gui.HTTPStatus.CONFLICT
        assert "not waiting for delivery" in duplicate["error"]
        assert (
            service.orchestrator.read_state_snapshot()["delivery"]["delivered"] is True
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# FIX-REV-3-3 verification (read-only workspace, edits applied in memory):
# Initial pytest FD capture failed because no writable temporary directory exists.
# Recovery: python3 -B, --capture=sys, -p no:cacheprovider; no files were edited.
# Selected baseline: 174 passed, 1 failed (obsolete raw task-ID selector).
# Selected revised GUI/Node regression: 175 passed, 101 deselected; no skips.
# Collection only: 276 targeted cases and 689 full-suite cases.
# Independent senior QA: static review and HTML visibility mutations passed.
# Not run: writable fixture/Git/socket/lock tests and full-suite execution.
# Risk: new lock cases need writable integration QA; browser layout is unverified.
#
# plan_notes (task-2, read-only validation):
# In-memory pytest collection found 145 test_gui.py cases.
# Selected test_gui.py checks: 56 passed, 89 deselected; no pytest skips.
# Existing test_gui_refresh.py: 4 passed, 18 failed against the current GUI.
# Failures: flow-* (9), status-deferred, preserve, delivery, selection,
# report-selection, report-membership, failure, race, submit.
# The refresh DOM double lacks the new required regions/control contracts;
# task-3 must address those failures without weakening the production checks.
# Targeted rerun: python3 -m pytest -q -rs test_gui.py test_gui_refresh.py
# Full rerun: python3 -m pytest -q -rs
# Skipped here: writable temporary fixtures, socket/Git/lock tests, full regression.
# Risk: service/integration cases remain unexecuted; no real-browser layout checks.


def test_gui_shows_live_test_runtime_and_escapes_recent_output(tmp_path, monkeypatch):
    service = gui.GuiService(tmp_path)
    app = service.orchestrator
    app.init_project()
    app.state["state"] = "TESTING"
    app.save_state()
    app._test_progress = {
        "started": gui.time.monotonic() - 12,
        "timeout": 600,
        "output_tail": "<script>alert(1)</script>",
    }
    monkeypatch.setattr(service, "_worker_is_running", lambda: True)
    status = service.status()
    assert status["test_progress"]["elapsed_seconds"] >= 12
    page = gui.render_page(status)
    assert "測試已執行" in page and "上限 600 秒" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page
    app.state["state"] = "WAITING_FOR_OWNER"
    app.save_state()
    assert service.status()["test_progress"] == {}
    app.state["state"] = "TESTING"
    app.save_state()
    monkeypatch.setattr(service, "_worker_is_running", lambda: False)
    assert service.status()["test_progress"] == {}
