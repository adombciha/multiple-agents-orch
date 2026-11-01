"""Cancel only the current CLI model call, preserving resumable workflow state."""

import json
import sys
import threading
from unittest.mock import Mock

import pytest

from orchestrator.core import backends
from orchestrator.core.state import ModelCallCancelled
from orchestrator.core.status import workflow_decision
from orchestrator.main import apply_approve
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


@pytest.mark.parametrize("backend", ["codex", "agy"])
def test_cancel_unwinds_fallback_preserves_task_and_can_resume(
    initialized_orchestrator, monkeypatch, backend
):
    app = initialized_orchestrator
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["developer_senior"] = [
        [backend, "model"],
        ["ollama", "fallback"],
    ]
    app.config["role_model_routes"]["developer_senior"][1] = [
        "agy" if backend == "codex" else "codex",
        "fallback",
    ]
    app.config_path.write_text(json.dumps(app.config))
    app.state.update(
        state="IMPLEMENTING",
        tasks=[
            {
                "id": "T1",
                "target_files": ["answer.py"],
                "status": "pending",
            }
        ],
        active_task_id="T1",
        task_backend_overrides={"T1": backend},
    )
    app.save_state()
    ready = threading.Event()
    original_check = app.check_model_cancellation

    def observe():
        if getattr(app, "_active_model_call", False):
            ready.set()
        original_check()

    monkeypatch.setattr(app, "check_model_cancellation", observe)
    monkeypatch.setattr(backends, "backend_available", lambda *args: True)
    fallback = Mock(
        side_effect=AssertionError("Cancellation must not try another route")
    )
    run = backends.run_agy_stream if backend == "agy" else backends.run_model_cli

    def cli(*args, **kwargs):
        return run(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys,time; sys.stdin.read(); print('partial', flush=True); time.sleep(60)",
            ],
            input="request\n",
            cwd=app.workspace,
            timeout=10,
            orchestrator=app,
        ).stdout

    monkeypatch.setattr(app, "call_codex" if backend == "codex" else "call_agy", cli)
    monkeypatch.setattr(
        app, "call_agy" if backend == "codex" else "call_codex", fallback
    )
    applied = Mock()

    def implement():
        app.call_role_model_routes("developer_senior", "Implement T1")
        applied()

    monkeypatch.setattr(app, "step_implementing", implement)
    worker = threading.Thread(target=app.step)
    worker.start()
    try:
        assert ready.wait(5)
        # A safe stop remains distinct from cancellation.
        app.request_pause()
        assert app.pause_requested()
        app.request_pause(cancel_model=True)
    finally:
        app.request_pause(cancel_model=True)
        worker.join(8)
    assert not worker.is_alive()
    applied.assert_not_called()
    fallback.assert_not_called()
    assert not app._active_model_call
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["resume_state"] == "IMPLEMENTING"
    assert app.state["tasks"][0]["status"] == "pending"
    assert not (app.workspace / "answer.py").exists()
    assert not app.state.get("task_failed_model_routes", {}).get("T1")
    assert not app.state.get("task_contract_failures", {}).get("T1")
    assert app.pause_request_path.read_text().strip() == "cancel-model"
    assert apply_approve(app)
    assert not app.pause_requested()
    assert app.state["state"] == "IMPLEMENTING"


def test_pending_cancellation_never_launches_cli(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    app.request_pause(cancel_model=True)
    popen = Mock(side_effect=AssertionError("Do not launch"))
    monkeypatch.setattr(backends.subprocess, "Popen", popen)
    with pytest.raises(ModelCallCancelled):
        backends.run_model_cli(["unused"], cwd=app.workspace, orchestrator=app)
    popen.assert_not_called()


def test_safe_pause_does_not_abort_cli(initialized_orchestrator):
    app = initialized_orchestrator
    app.request_pause()
    result = backends.run_model_cli(
        [sys.executable, "-c", "print('complete')"], cwd=app.workspace, orchestrator=app
    )
    assert result.returncode == 0 and result.stdout.strip() == "complete"
    assert app.pause_requested()
    assert not app._active_model_call


def test_cancel_at_response_completion_discards_response(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    original = app.check_model_cancellation
    calls = 0

    def cancel_after_completion():
        nonlocal calls
        calls += 1
        if calls == 3:
            app.request_pause(cancel_model=True)
        original()

    monkeypatch.setattr(app, "check_model_cancellation", cancel_after_completion)
    with pytest.raises(ModelCallCancelled):
        backends.run_model_cli(
            [sys.executable, "-c", "print('must not apply')"],
            cwd=app.workspace,
            orchestrator=app,
        )
    assert not app._active_model_call


def test_cancel_action_requires_active_cli_and_is_available_after_safe_stop():
    state = {"state": "IMPLEMENTING"}
    decision = workflow_decision(
        state, busy=True, pause_requested=True, can_cancel_model=True
    )
    assert any(
        item["action"] == "cancel-model" and item["confirmation"]
        for item in decision["actions"]
    )
    for decision in (
        workflow_decision(state, busy=True),
        workflow_decision(state, busy=True, can_cancel_model=True, external_lock=True),
        workflow_decision(state),
    ):
        assert not any(item["action"] == "cancel-model" for item in decision["actions"])


def test_codex_cancellation_cleans_prompt_file(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    monkeypatch.setattr(
        backends, "run_model_cli", Mock(side_effect=ModelCallCancelled())
    )
    with pytest.raises(ModelCallCancelled):
        backends.call_codex(app, "Task", role="developer_senior", model="gpt-6-luna")
    assert not (app.ai_dir / "temp_codex_prompt.txt").exists()
