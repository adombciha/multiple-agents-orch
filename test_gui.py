import io
import json
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
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

    with pytest.raises(RuntimeError, match="already running"):
        service._new_task("second task")


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

    assert result["state"] == "COMPLETED"
    service.orchestrator.run_to_end.assert_not_called()


def test_delivery_confirmation_requires_the_presented_snapshot_id(tmp_path):
    service = make_git_delivery_service(tmp_path)
    service.delivery(False)

    with pytest.raises(RuntimeError, match="does not match the displayed snapshot"):
        service.delivery(True)

    assert service.orchestrator.state["delivery_confirmed"] is False


def test_delivery_requires_new_confirmation_when_git_impact_changes(tmp_path):
    service = make_git_delivery_service(tmp_path)
    first = service.delivery(False)
    (tmp_path / "app.py").write_text("changed feature\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Git impact changed"):
        service.delivery(True, first["delivery_snapshot_id"])

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
    service = make_service("RUNNING")
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

    assert stopped["state"] == "RUNNING"
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

    assert continued["state"] == "RUNNING"
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

    assert confirmed["state"] == "COMPLETED"
    service.orchestrator.run_to_end.assert_not_called()
    assert confirm_handler.send_response.call_args.args[0] == gui.HTTPStatus.OK


def test_render_page_displays_stage_tasks_reports_and_git_impact():
    page = gui.render_page(
        {
            "state": "WAITING_FOR_DELIVERY",
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
    assert "document.activeElement === promptInput || promptInput.value" in page
    assert "setTimeout(refreshWhenIdle, 3000)" in page


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
    apply_approve = Mock(return_value=True)
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
    monkeypatch.setattr(gui, "apply_review_decision", apply_review_decision)

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
