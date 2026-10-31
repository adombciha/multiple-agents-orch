import json
import threading
from unittest.mock import Mock

import pytest

from orchestrator import gui
from orchestrator.core.state import AgentOrchestrator
from orchestrator.main import apply_review_decision, clear_run_state
from orchestrator.roles.qa import qa_verdict
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config
from test_gui import make_handler, make_service, start_worker_inline


@pytest.mark.parametrize(
    "report",
    [
        "Project overview",
        "QA_STATUS: FAILED",
        "QA_STATUS: PASSED\nQA_STATUS: FAILED",
        "QA_STATUS: FAILED\nDEPENDENCY_INSTALL_NEEDED: pip install pytest",
    ],
)
def test_invalid_qa_has_no_verdict(report):
    assert qa_verdict(report) is None


@pytest.mark.parametrize("exit_code", [0, 1])
def test_invalid_qa_pauses_without_fix_tasks_or_developer_penalties(
    initialized_orchestrator, monkeypatch, exit_code
):
    app = initialized_orchestrator
    app.state["tasks"] = [
        {
            "id": "T1",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
            "target_files": ["app.py"],
        }
    ]
    monkeypatch.setattr(
        app, "run_command", Mock(return_value=(exit_code, "tests finished"))
    )
    monkeypatch.setattr(
        app,
        "allocate_workers",
        Mock(return_value=([("qa-1", "senior")], {"T1": "qa-1"})),
    )
    call = Mock(return_value="This platform manages AI projects.")
    monkeypatch.setattr(app, "call_agent", call)
    penalty = Mock()
    monkeypatch.setattr("orchestrator.roles.qa.scoring.record_event", penalty)
    app.step_testing()
    assert call.call_count == 2
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["human_review_source"] == "QA_RESPONSE"
    assert app.state["pass_state"] == "TESTING"
    assert len(app.state["tasks"]) == 1
    penalty.assert_not_called()


def test_qa_one_correction_can_recover(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    app.state["tasks"] = [
        {"id": "T1", "status": "completed", "target_files": ["app.py"]}
    ]
    monkeypatch.setattr(app, "run_command", Mock(return_value=(0, "tests passed")))
    monkeypatch.setattr(
        app,
        "allocate_workers",
        Mock(return_value=([("qa-1", "senior")], {"T1": "qa-1"})),
    )
    monkeypatch.setattr(
        app,
        "call_agent",
        Mock(
            side_effect=[
                "overview",
                "QA_STATUS: PASSED\nVerified app.py against tests.",
            ]
        ),
    )
    app.step_testing()
    assert app.state["state"] == "REVIEWING_CODE"


def test_owner_plan_revision_keeps_feedback_until_success(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.requirements_path.write_text("Implement app.py", encoding="utf-8")
    app.request_path.write_text("Implement app.py", encoding="utf-8")
    app.state.update(
        state="WAITING_FOR_OWNER",
        human_review_source="Architect",
        human_review_details="Keep both files in one atomic task.",
    )
    apply_review_decision(app, "revise")
    app.load_config_and_state()
    assert app.state["plan_revisions"] == 0
    monkeypatch.setattr(app, "call_manager", Mock(return_value="not a plan"))
    app.step_developing_plan()
    assert app.state["human_review_source"] == "Manager"
    assert "Keep both files" in app.state["pending_plan_feedback"]
    apply_review_decision(app, "revise")
    manager = Mock(
        return_value='{"tasks": [{"id": "T1", "description": "Implement app", "target_files": ["app.py"], "rd_level": "middle", "qa_level": "middle"}], "staffing": {"rd": {"middle": 1}, "qa": {"middle": 1}}}'
    )
    monkeypatch.setattr(app, "call_manager", manager)
    app.step_developing_plan()
    assert "Keep both files in one atomic task." in manager.call_args.args[0]
    assert "pending_plan_feedback" not in app.state
    app.state["pending_plan_feedback"] = "old run"
    monkeypatch.setattr(app, "cleanup_worktree", Mock(return_value=True))
    clear_run_state(app, "PLANNING")
    assert "pending_plan_feedback" not in app.state


def test_gui_acknowledges_lock_before_slow_prepare_and_persists_error(tmp_path):
    service = gui.GuiService(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def prepare():
        entered.set()
        assert release.wait(5)
        raise RuntimeError("backend preparation failed")

    try:
        assert service._start_worker(prepare)
        assert entered.wait(2)
        assert service.status()["worker_status"]["phase"] == "preparing"
        assert service._start_worker(prepare) is False
        with pytest.raises(RuntimeError, match="already in progress"):
            with AgentOrchestrator(tmp_path).execution_lock():
                pass
        worker = service.worker
    finally:
        release.set()
        if service.worker:
            service.worker.join(3)
    assert not worker.is_alive()
    restored = gui.GuiService(tmp_path).status()
    assert restored["worker_status"] == {
        "phase": "failed",
        "error": "backend preparation failed",
    }


def test_gui_background_execution_error_is_visible(tmp_path):
    service = gui.GuiService(tmp_path)
    service.orchestrator.run_to_end = Mock(side_effect=RuntimeError("execution failed"))
    service._run_to_decision()
    assert service.status()["worker_status"]["error"] == "execution failed"


def test_gui_explicit_route_retry_uses_shared_approval(monkeypatch):
    service = make_service("WAITING_FOR_OWNER")
    service.orchestrator.state["human_review_source"] = "Developer"
    start_worker_inline(service)
    approve = Mock(return_value=True)
    monkeypatch.setattr(gui, "apply_approve", approve)
    service.continue_task(clear_route_failures=True)
    approve.assert_called_once_with(service.orchestrator, clear_route_failures=True)


def test_gui_worker_handles_persistence_failure(tmp_path, monkeypatch):
    service = gui.GuiService(tmp_path)
    monkeypatch.setattr(gui.os, "replace", Mock(side_effect=OSError("disk full")))
    service._write_worker_status("failed", "original execution failure")
    assert "original execution failure" in service.worker_status["error"]
    assert "disk full" in service.worker_status["error"]


def test_disconnected_http_client_does_not_raise():
    handler = make_handler("POST", {})
    handler.wfile = Mock()
    handler.wfile.write.side_effect = BrokenPipeError()
    handler._send_json({"error": "original failure"})


@pytest.mark.parametrize(
    "source,label",
    [
        ("Architect", "依審核意見修訂"),
        ("QA", "依審核意見修訂"),
        ("Developer", "清除失敗路由並重試"),
        ("QA_RESPONSE", "重試 QA 驗證"),
    ],
)
def test_gui_buttons_match_review_source(source, label):
    page = gui.render_page({"state": "WAITING_FOR_OWNER", "review": {"source": source}})
    assert label in page
    assert "確認二次交付</button>" not in page
    if source == "QA_RESPONSE":
        assert "接受目前結果並繼續</button>" not in page


def test_gui_running_only_offers_stop():
    page = gui.render_page({"state": "IMPLEMENTING", "worker_running": True})
    assert "目前步驟完成後停止</button>" in page
    assert "重置任務</button>" not in page
    assert "繼續執行</button>" not in page
