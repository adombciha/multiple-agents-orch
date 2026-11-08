"""Check local QA resource and model accounting without contacting providers."""

import json
import os
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
import requests

from orchestrator.core import backends, telemetry
from orchestrator import gui
from orchestrator.core.local_resources import LocalResourceBusy
from orchestrator.roles.qa import QA_RESPONSE_SCHEMA
from test_qa_evidence import qa_app
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


@pytest.fixture(autouse=True)
def offline_token_count(monkeypatch):
    monkeypatch.setattr(
        telemetry, "_token_count", lambda *_args: (None, "unsupported_model")
    )


@pytest.mark.parametrize("timeout", [False, True])
def test_local_qa_usage_keeps_request_model_after_cloud_call(
    initialized_orchestrator, monkeypatch, timeout
):
    app = initialized_orchestrator

    def call_cloud(app, prompt, system_prompt, role, model):
        app._resolved_cloud_model = "gpt-6.1-sol"
        return "cloud response"

    monkeypatch.setattr(backends, "call_codex", call_cloud)
    assert app.call_codex("Plan", role="architect", model="auto") == "cloud response"
    resolver = Mock(side_effect=["halo:9b", "wrong-second-resolution:7b"])
    monkeypatch.setattr(app, "get_active_model_for_role", resolver)
    post = Mock(
        side_effect=requests.exceptions.ReadTimeout("Read timed out")
        if timeout
        else None,
        return_value=Mock(json=lambda: {"message": {"content": "local response"}}),
    )
    monkeypatch.setattr(backends.requests, "post", post)

    if timeout:
        with pytest.raises(RuntimeError, match="Read timed out"):
            app.call_ollama("Check evidence", role="qa_middle")
    else:
        assert app.call_ollama("Check evidence", role="qa_middle") == "local response"

    resolver.assert_called_once_with("qa_middle", "ollama")
    assert post.call_args.kwargs["json"]["model"] == "halo:9b"
    records = [
        json.loads(line)
        for line in (app.ai_dir / "llm_usage.jsonl").read_text().splitlines()
    ]
    assert records[0]["model"] == "gpt-6.1-sol"
    assert records[1]["backend"] == "ollama"
    assert records[1]["model"] == "halo:9b"
    assert records[1]["success"] is not timeout
    assert records[1]["error_category"] == ("timeout" if timeout else None)


@pytest.mark.parametrize("role", ["qa", "qa_senior", "qa_middle", "qa_junior"])
def test_local_only_qa_ignores_cloud_routes_and_backend(
    initialized_orchestrator, monkeypatch, role
):
    app = initialized_orchestrator
    app.config["qa_local_only"] = True
    app.config["backends"][role] = "codex"
    app.config["role_model_routes"][role] = [["codex", "auto"], ["agy", "auto"]]
    app.config["ollama_router_enabled"] = False
    monkeypatch.setattr(
        backends, "discover_ollama_chat_models", Mock(return_value={"halo:9b": {}})
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    local = Mock(return_value="local QA")
    cloud = Mock(side_effect=AssertionError("Cloud QA must not run"))
    monkeypatch.setattr(app, "call_agent_ollama_fallback", local)
    monkeypatch.setattr(app, "call_codex", cloud)
    monkeypatch.setattr(app, "call_agy", cloud)

    assert app.get_backend(role) == "ollama"
    assert app.call_agent(role, "Check evidence") == "local QA"
    local.assert_called_once_with(role, "Check evidence", None, model="halo:9b")
    cloud.assert_not_called()


@pytest.mark.parametrize("installed", [True, False], ids=["timeout", "no-models"])
def test_local_only_qa_failure_never_falls_back_to_cloud(
    initialized_orchestrator, monkeypatch, installed
):
    app = initialized_orchestrator
    app.config["qa_local_only"] = True
    app.config["ollama_router_enabled"] = False
    app.config["backends"]["qa_middle"] = "codex"
    app.config["role_model_routes"]["qa_middle"] = [
        ["codex", "auto"],
        ["agy", "auto"],
    ]
    monkeypatch.setattr(
        backends,
        "discover_ollama_chat_models",
        Mock(return_value={"halo:9b": {}} if installed else {}),
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    post = Mock(side_effect=requests.exceptions.ReadTimeout("Read timed out"))
    monkeypatch.setattr(backends.requests, "post", post)
    cloud = Mock(side_effect=AssertionError("Cloud QA must not run"))
    monkeypatch.setattr(app, "call_codex", cloud)
    monkeypatch.setattr(app, "call_agy", cloud)

    expected = "Read timed out" if installed else "No installed Ollama"
    with pytest.raises(RuntimeError, match=expected):
        app.call_agent("qa_middle", "Check evidence")

    cloud.assert_not_called()
    assert post.call_count == int(installed)
    if installed:
        assert post.call_args.kwargs["json"]["model"] == "halo:9b"


@pytest.mark.parametrize("backend", ["codex", "agy"])
def test_local_only_qa_rejects_direct_cloud_calls(
    initialized_orchestrator, monkeypatch, backend
):
    app = initialized_orchestrator
    app.config["qa_local_only"] = True
    cloud = Mock(side_effect=AssertionError("Cloud QA must not run"))
    monkeypatch.setattr(backends, f"call_{backend}", cloud)
    with pytest.raises(RuntimeError, match="restricted to local"):
        getattr(app, f"call_{backend}")(
            "Check evidence", role="qa_middle", model="auto"
        )
    cloud.assert_not_called()


def test_local_qa_policy_disabled_keeps_configured_cloud_routes(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    assert app.config["qa_local_only"] is False
    app.config["backends"]["qa_middle"] = "codex"
    app.config["role_model_routes"]["qa_middle"] = [["codex", "auto"]]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    cloud = Mock(return_value="configured cloud QA")
    monkeypatch.setattr(app, "call_codex", cloud)

    assert app.get_backend("qa_middle") == "codex"
    assert app.call_agent("qa_middle", "Check evidence") == "configured cloud QA"
    cloud.assert_called_once_with(
        "Check evidence", None, role="qa_middle", model="auto"
    )


@pytest.mark.parametrize("role", ["developer_middle", "architect"])
def test_local_qa_policy_preserves_other_cloud_routes(
    initialized_orchestrator, monkeypatch, role
):
    app = initialized_orchestrator
    app.config["qa_local_only"] = True
    app.config["role_model_routes"][role] = [["codex", "auto"]]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    cloud = Mock(return_value="cloud response")
    monkeypatch.setattr(app, "call_codex", cloud)

    assert app.get_backend(role) == "codex"
    assert app.call_agent(role, "Work") == "cloud response"
    cloud.assert_called_once_with("Work", None, role=role, model="auto")


@pytest.mark.parametrize("executor", ["chat", "router", "gpt"])
def test_local_inference_posts_hold_foreground_slot_and_publish_activity(
    initialized_orchestrator, monkeypatch, executor
):
    app = initialized_orchestrator
    app.config["ollama_resource_dir"] = "/unused/coordination"
    lease_events = []
    activity_path = app.ai_dir / "local_model_activity.json"

    @contextmanager
    def slot(directory, *, timeout, on_wait):
        assert directory == "/unused/coordination"
        assert timeout == 60
        on_wait()
        assert app._local_model_activity["phase"] == "waiting"
        assert json.loads(activity_path.read_text())["phase"] == "waiting"
        lease_events.append("acquired")
        try:
            yield
        finally:
            lease_events.append("released")

    monkeypatch.setattr(backends, "foreground_slot", slot)

    def post(url, *, json, timeout):
        assert lease_events == ["acquired"]
        assert app._local_model_activity["phase"] == "running"
        assert app._local_model_activity["model"] == json["model"]
        assert activity_path.exists()
        if executor == "router":
            return Mock(
                status_code=200,
                json=lambda: {
                    "answers": {
                        "selected_model": {
                            "choice": "halo:9b",
                            "confidence": 0.9,
                            "probabilities": {"halo:9b": 0.9, "other:7b": 0.1},
                        }
                    }
                },
            )
        content = (
            '{"files":[{"path":"app.py","content":"print(1)"}]}'
            if executor == "gpt"
            else "local response"
        )
        return Mock(status_code=200, json=lambda: {"message": {"content": content}})

    monkeypatch.setattr(backends.requests, "post", post)
    if executor == "router":
        monkeypatch.setattr(
            backends,
            "discover_installed_ollama_models",
            Mock(return_value={"tev1:4b", "halo:9b", "other:7b"}),
        )
        assert backends.route_ollama_model_with_tev1(
            app, "qa_middle", ["halo:9b", "other:7b"], "Check"
        ) == ["halo:9b", "other:7b"]
    else:
        response = app.call_ollama(
            "Implement" if executor == "gpt" else "Check",
            role="developer_middle" if executor == "gpt" else "qa_middle",
            model="gpt-oss:20b" if executor == "gpt" else "halo:9b",
        )
        assert (
            "app.py" in response if executor == "gpt" else response == "local response"
        )
    assert lease_events == ["acquired", "released"]
    assert app._local_model_activity is None
    assert not activity_path.exists()


def test_busy_local_resource_never_sends_request_or_changes_routes(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.config.update(
        qa_local_only=True,
        ollama_resource_dir="/unused/coordination",
        ollama_router_enabled=False,
    )
    app.config["role_model_routes"]["qa_middle"] = [["codex", "auto"]]
    routes = dict(app.config["role_model_routes"])
    monkeypatch.setattr(
        backends,
        "discover_ollama_chat_models",
        Mock(return_value={"halo:9b": {}, "other:7b": {}}),
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    slots = []

    @contextmanager
    def slot(*_args, **kwargs):
        slots.append("wait")
        kwargs["on_wait"]()
        raise LocalResourceBusy("Local inference resource remained busy for 60s")
        yield

    monkeypatch.setattr(backends, "foreground_slot", slot)
    post = Mock(side_effect=AssertionError("A busy lease must not send HTTP"))
    monkeypatch.setattr(backends.requests, "post", post)
    cloud = Mock(side_effect=AssertionError("A busy lease must not switch backend"))
    monkeypatch.setattr(app, "call_codex", cloud)
    monkeypatch.setattr(app, "call_agy", cloud)
    quota = Mock()
    monkeypatch.setattr(backends, "mark_backend_quota_exhausted", quota)

    with pytest.raises(LocalResourceBusy):
        app.call_agent("qa_middle", "Check", response_schema=QA_RESPONSE_SCHEMA)

    assert slots == ["wait"]
    post.assert_not_called()
    cloud.assert_not_called()
    quota.assert_not_called()
    assert app.config["role_model_routes"] == routes
    assert app.state["qa_failed_model_routes"] == []
    assert app.state["failed_model_routes"] == []
    assert not (app.ai_dir / "llm_usage.jsonl").exists()
    assert app._local_model_activity is None
    assert not (app.ai_dir / "local_model_activity.json").exists()


def test_busy_router_propagates_without_cooldown_or_static_fallback(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    monkeypatch.setattr(
        backends,
        "discover_installed_ollama_models",
        Mock(return_value={"tev1:4b", "halo:9b", "other:7b"}),
    )
    post = Mock(side_effect=LocalResourceBusy("Local inference resource remained busy"))
    monkeypatch.setattr(backends, "ollama_post", post)
    with pytest.raises(LocalResourceBusy):
        backends.route_ollama_model_with_tev1(
            app, "qa_middle", ["halo:9b", "other:7b"], "Check"
        )
    post.assert_called_once()
    assert backends._tev1_cooldowns == {}


def test_busy_local_fallback_does_not_switch_models(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.config["ollama_router_enabled"] = False
    monkeypatch.setattr(
        backends,
        "discover_ollama_chat_models",
        Mock(return_value={"halo:9b": {}, "other:7b": {}}),
    )
    call = Mock(side_effect=LocalResourceBusy("Local inference resource remained busy"))
    monkeypatch.setattr(app, "call_ollama", call)
    with pytest.raises(LocalResourceBusy):
        app.call_agent_ollama_fallback("qa_middle", "Check")
    call.assert_called_once()


def test_busy_resource_refunds_unexecuted_developer_call(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.state.update(state="IMPLEMENTING", active_task_id="T1")
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["developer_middle"] = [["ollama", "auto"]]
    monkeypatch.setattr(
        backends, "discover_ollama_chat_models", Mock(return_value={"halo:9b": {}})
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(
        app,
        "call_agent_ollama_fallback",
        Mock(side_effect=LocalResourceBusy("Local inference resource remained busy")),
    )
    with pytest.raises(LocalResourceBusy):
        app.call_role_model_routes("developer_middle", "Implement")
    assert app.state["task_developer_calls"]["T1"] == 0
    assert app.state["task_failed_model_routes"]["T1"] == []
    assert app.state["task_contract_failures"]["T1"] == {}


def test_busy_qa_resource_pauses_once_and_preserves_tasks(qa_app, monkeypatch):
    app = qa_app
    tasks = list(app.state["tasks"])
    monkeypatch.setattr(
        app,
        "allocate_workers",
        Mock(return_value=([("qa-1", "senior"), ("qa-2", "senior")], {"T1": "qa-1"})),
    )
    call = Mock(
        side_effect=LocalResourceBusy("Local inference resource remained busy for 60s")
    )
    monkeypatch.setattr(app, "call_agent", call)
    app.step_testing()
    call.assert_called_once()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["human_review_source"] == "QA_RESPONSE"
    assert app.state["resume_state"] == app.state["pass_state"] == "TESTING"
    assert "Local inference resource busy" in app.state["human_review_details"]
    assert app.state["tasks"] == tasks
    assert app.state["code_revisions"] == 0
    app.escalate_developer_backend.assert_not_called()


@pytest.mark.parametrize(
    "wait", [None, True, -1, 301, float("nan"), float("inf"), "60"]
)
def test_local_resource_wait_budget_validation(
    initialized_orchestrator, monkeypatch, wait
):
    app = initialized_orchestrator
    app.config["ollama_resource_wait_seconds"] = wait
    post = Mock()
    monkeypatch.setattr(backends.requests, "post", post)
    with pytest.raises(ValueError, match="ollama_resource_wait_seconds"):
        app.call_ollama("Check", role="qa_middle", model="halo:9b")
    post.assert_not_called()


def test_gpt_retry_busy_keeps_executed_call_telemetry_and_budget(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.state.update(state="IMPLEMENTING", active_task_id="T1")
    app.config.update(
        ollama_resource_dir="/unused/coordination", ollama_router_enabled=False
    )
    app.config["role_model_routes"]["developer_middle"] = [["ollama", "auto"]]
    monkeypatch.setattr(
        backends,
        "discover_ollama_chat_models",
        Mock(return_value={"gpt-oss:20b": {}, "other:7b": {}}),
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    acquisitions = []

    @contextmanager
    def slot(*_args, **kwargs):
        acquisitions.append("attempt")
        if len(acquisitions) == 2:
            kwargs["on_wait"]()
            raise LocalResourceBusy("Local inference resource remained busy")
        yield

    monkeypatch.setattr(backends, "foreground_slot", slot)
    post = Mock(
        return_value=Mock(status_code=200, json=lambda: {"message": {"content": "{"}})
    )
    monkeypatch.setattr(backends.requests, "post", post)
    with pytest.raises(LocalResourceBusy) as error:
        app.call_role_model_routes("developer_middle", "Implement")

    assert error.value.inference_attempts == 1
    assert len(acquisitions) == 2
    post.assert_called_once()
    assert app.state["task_developer_calls"]["T1"] == 1
    assert app.state["task_failed_model_routes"]["T1"] == []
    assert app.state["task_contract_failures"]["T1"] == {}
    records = [
        json.loads(line)
        for line in (app.ai_dir / "llm_usage.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["model"] == "gpt-oss:20b"
    assert records[0]["error_category"] == "LocalResourceBusy"
    assert app._local_model_activity is None


@pytest.mark.parametrize("phase", ["waiting", "running"])
def test_gui_reports_local_activity_and_escapes_model(tmp_path, monkeypatch, phase):
    service = gui.GuiService(tmp_path)
    app = service.orchestrator
    app.init_project()
    app.state["state"] = "TESTING"
    app.save_state()
    backends._set_local_model_activity(
        app,
        {
            "phase": phase,
            "role": "qa_middle",
            "model": "<script>model</script>",
            "started": gui.time.monotonic() - 12,
            "wait_seconds": 60,
            "pid": os.getpid(),
        },
    )
    monkeypatch.setattr(service, "_worker_is_running", lambda: True)
    status = service.status()
    assert status["local_model_activity"]["elapsed_seconds"] >= 12
    page = gui.render_page(status)
    expected = "等待本機推論資源" if phase == "waiting" else "本機模型推論中"
    assert expected in page
    assert "&lt;script&gt;model&lt;/script&gt;" in page
    assert "<script>model</script>" not in page
    monkeypatch.setattr(service, "_worker_is_running", lambda: False)
    monkeypatch.setattr(gui.RunLock, "read_holder", lambda *_args: None)
    assert service.status()["local_model_activity"] == {}


@pytest.mark.parametrize(
    "activity",
    [
        {"pid": 1234},
        {
            "pid": 1234,
            "phase": "waiting",
            "role": "qa",
            "model": "halo",
            "started": "old",
            "wait_seconds": 60,
        },
        {
            "pid": 1234,
            "phase": "waiting",
            "role": "qa",
            "model": "halo",
            "started": float("nan"),
            "wait_seconds": 60,
        },
        {
            "pid": 1234,
            "phase": "waiting",
            "role": "qa",
            "model": "halo",
            "started": 12,
            "wait_seconds": None,
        },
        [],
    ],
)
def test_gui_ignores_malformed_persisted_local_activity(
    tmp_path, monkeypatch, activity
):
    service = gui.GuiService(tmp_path)
    service.orchestrator.init_project()
    path = service.orchestrator.ai_dir / "local_model_activity.json"
    path.write_text(json.dumps(activity))
    monkeypatch.setattr(service, "_worker_is_running", lambda: False)
    monkeypatch.setattr(gui.RunLock, "read_holder", lambda *_args: {"pid": 1234})
    assert service.status()["local_model_activity"] == {}


def test_gui_reads_external_local_activity_only_for_current_lock_holder(
    tmp_path, monkeypatch
):
    service = gui.GuiService(tmp_path)
    service.orchestrator.init_project()
    path = service.orchestrator.ai_dir / "local_model_activity.json"
    path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "phase": "waiting",
                "role": "qa_middle",
                "model": "halo:9b",
                "started": gui.time.monotonic() - 10,
                "wait_seconds": 60,
            }
        )
    )
    monkeypatch.setattr(service, "_worker_is_running", lambda: False)
    monkeypatch.setattr(gui.RunLock, "read_holder", lambda *_args: {"pid": 1234})
    assert service.status()["local_model_activity"]["phase"] == "waiting"
    monkeypatch.setattr(gui.RunLock, "read_holder", lambda *_args: {"pid": 5678})
    assert service.status()["local_model_activity"] == {}
