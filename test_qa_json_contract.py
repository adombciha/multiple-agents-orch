"""Exercise native JSON enforcement and bounded QA route fallback offline."""

import json
from unittest.mock import Mock

import pytest

from orchestrator.core import backends
from orchestrator.roles.qa import QA_RESPONSE_SCHEMA, render_qa_json
from test_qa_evidence import qa_app
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


def result(**changes):
    data = {
        "status": "PASSED",
        "summary": "All requested offline tests passed.",
        "findings": [],
        "dependency_install_needed": None,
        "test_command_suggestion": None,
    }
    data.update(changes)
    return json.dumps(data)


@pytest.mark.parametrize(
    "bad",
    [
        "### QA Report\n#### QA_STATUS: FAILED\nAll tests passed; no issues were found.",
        "{}",
        result(status="UNKNOWN"),
        result(
            status="PASSED",
            findings=[
                {
                    "kind": "process",
                    "file": "",
                    "requirement": "",
                    "evidence": "",
                    "fix": "Obtain approval",
                }
            ],
        ),
        result(dependency_install_needed="pip install imaginary"),
        result(findings=["not a finding"]),
    ],
)
def test_json_rejects_malformed_or_inconsistent_results(bad):
    with pytest.raises((TypeError, ValueError)):
        render_qa_json(bad)


def configure_routes(app, monkeypatch, responses):
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["qa_senior"] = [["ollama", "auto"]]
    models = {name: {} for name in ["first:v1", "second:v1", "third:v1"]}
    monkeypatch.setattr(
        backends, "discover_ollama_chat_models", Mock(return_value=models)
    )
    monkeypatch.setattr(
        backends, "discover_installed_ollama_models", Mock(return_value=set(models))
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    post = Mock(
        side_effect=[
            Mock(
                status_code=200,
                json=lambda content=content: {"message": {"content": content}},
            )
            for content in responses
        ]
    )
    monkeypatch.setattr(backends.requests, "post", post)
    return post


@pytest.mark.parametrize(
    "first",
    [
        "### QA Report\n#### QA_STATUS: FAILED\nAll tests passed; no defects exist.",
        result(status="FAILED", summary="All tests passed, no defects exist."),
        result(
            findings=[
                {
                    "kind": "code",
                    "file": "app.py",
                    "requirement": "Only modify app.py",
                    "evidence": "invented",
                    "fix": "Change code",
                }
            ]
        ),
    ],
)
def test_qa_uses_native_schema_and_next_local_model(qa_app, monkeypatch, first):
    app = qa_app
    post = configure_routes(app, monkeypatch, [first, result()])
    app.step_testing()
    assert app.state["state"] == "REVIEWING_CODE"
    assert len(app.state["tasks"]) == 1
    assert [call.kwargs["json"]["model"] for call in post.call_args_list] == [
        "first:v1",
        "second:v1",
    ]
    assert all(
        call.kwargs["json"]["format"] == QA_RESPONSE_SCHEMA
        for call in post.call_args_list
    )
    assert app.qa_report_path.read_text().count("QA_STATUS: PASSED") == 1


def test_qa_route_exhaustion_is_bounded_and_does_not_queue_rd(qa_app, monkeypatch):
    app = qa_app
    post = configure_routes(app, monkeypatch, [result(status="FAILED"), "not JSON"])
    app.step_testing()
    assert post.call_count == 2
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["human_review_source"] == "QA_RESPONSE"
    assert app.state["resume_state"] == "TESTING"
    assert app.state["code_revisions"] == 0
    assert len(app.state["tasks"]) == 1
    app.escalate_developer_backend.assert_not_called()


def test_missing_route_configuration_still_uses_validated_qa_path(qa_app, monkeypatch):
    app = qa_app
    post = configure_routes(app, monkeypatch, ["bad", result()])
    app.config["role_model_routes"].pop("qa_senior")
    app.step_testing()
    assert post.call_count == 2
    assert app.state["state"] == "REVIEWING_CODE"


def test_direct_local_fallback_preserves_native_schema(qa_app, monkeypatch):
    app = qa_app
    post = configure_routes(app, monkeypatch, [result()])
    response = app.call_agent_ollama_fallback(
        "qa_senior", "Check offline evidence", response_schema=QA_RESPONSE_SCHEMA
    )
    assert json.loads(response)["status"] == "PASSED"
    assert post.call_args.kwargs["json"]["format"] == QA_RESPONSE_SCHEMA


def test_real_failure_cannot_be_overridden_by_json_pass(qa_app, monkeypatch):
    app = qa_app
    monkeypatch.setattr(
        app,
        "run_command",
        Mock(return_value=(1, "FAILED test_app.py - app.py assertion mismatch")),
    )
    post = configure_routes(
        app,
        monkeypatch,
        [result(), result(status="FAILED", summary="The raw test command failed.")],
    )
    app.step_testing()
    assert post.call_count == 2
    assert app.state["state"] == "IMPLEMENTING"
    assert "assertion mismatch" in app.state["tasks"][-1]["description"]


def test_json_defect_requires_real_requirement_and_evidence(qa_app, monkeypatch):
    app = qa_app
    monkeypatch.setattr(
        app, "run_command", Mock(return_value=(0, "719 passed\napp.py: unsafe()"))
    )
    finding = {
        "kind": "code",
        "file": "app.py",
        "requirement": "Only modify app.py",
        "evidence": "app.py: unsafe()",
        "fix": "Remove the unsafe call",
    }
    post = configure_routes(
        app, monkeypatch, [result(status="FAILED", findings=[finding])]
    )
    app.step_testing()
    assert post.call_count == 1
    assert app.state["state"] == "IMPLEMENTING"
    assert "Remove the unsafe call" in app.state["tasks"][-1]["description"]


def test_context_overflow_does_not_disable_routes_for_smaller_requests(
    qa_app, monkeypatch
):
    app = qa_app
    post = configure_routes(app, monkeypatch, [result()])
    app.config["ollama_num_ctx"] = 8192
    with pytest.raises(RuntimeError, match="exceeds context budget"):
        app.call_role_model_routes(
            "qa_senior", "x" * 100000, response_schema=QA_RESPONSE_SCHEMA
        )
    post.assert_not_called()
    assert app.state.get("qa_failed_model_routes", []) == []
    assert app.state.get("failed_model_routes", []) == []
    response = app.call_role_model_routes(
        "qa_senior", "Short evidence", response_schema=QA_RESPONSE_SCHEMA
    )
    assert json.loads(response)["status"] == "PASSED"
    assert post.call_count == 1


@pytest.mark.parametrize("entry", ["approve", "continue", "gui"])
def test_owner_qa_retry_recovers_legacy_and_qa_routes_after_restart(
    qa_app, monkeypatch, entry
):
    from orchestrator.main import apply_approve, apply_continue

    app = qa_app
    post = configure_routes(app, monkeypatch, [result()])
    legacy = ["ollama/first:v1", "ollama/second:v1", "agy/other"]
    app.state.update(
        failed_model_routes=legacy[:],
        qa_failed_model_routes=["ollama/first:v1", "ollama/second:v1"],
        quota_exhausted_backends={"agy": 12345},
        task_failed_model_routes={"T1": ["codex/auto"]},
    )
    app.pause_for_human_review(
        "QA_RESPONSE", "No QA response", "TESTING", "TESTING", decision_kind="resume"
    )
    app.load_config_and_state()
    if entry == "gui":
        from orchestrator.gui import GuiService

        service = GuiService(app.workspace)
        service.orchestrator = app
        monkeypatch.setattr(service, "_worker_is_running", lambda: False)
        monkeypatch.setattr(
            service, "_start_worker", lambda prepare: (prepare(), True)[1]
        )
        service.approve()
    else:
        callback = apply_approve if entry == "approve" else apply_continue
        assert callback(app)
    assert app.state["state"] == "TESTING"
    assert app.state["qa_failed_model_routes"] == []
    assert app.state["failed_model_routes"] == legacy
    assert app.state["quota_exhausted_backends"] == {"agy": 12345}
    assert app.state["task_failed_model_routes"] == {"T1": ["codex/auto"]}
    post.assert_not_called()
    app.step_testing()
    assert post.call_count == 1
    assert app.state["state"] == "REVIEWING_CODE"


def test_qa_transport_failure_is_scoped_and_retry_does_not_bypass_backend_gate(
    qa_app, monkeypatch
):
    from orchestrator.main import apply_approve

    app = qa_app
    post = configure_routes(app, monkeypatch, [])
    post.side_effect = backends.requests.ConnectionError("offline")
    app.step_testing()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert len(app.state["qa_failed_model_routes"]) == 2
    assert app.state.get("failed_model_routes", []) == []
    gate = Mock(return_value=False)
    monkeypatch.setattr(backends, "backend_available", gate)
    assert apply_approve(app)
    post.reset_mock()
    app.step_testing()
    gate.assert_called()
    post.assert_not_called()
    assert app.state["human_review_source"] == "QA_RESPONSE"
