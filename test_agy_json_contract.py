"""Verify AGY's native JSON contract without making paid model calls."""

import json
from unittest.mock import Mock

import pytest

from orchestrator.core import backends
from orchestrator.roles.base_agent import is_strict_json_response
from orchestrator.roles.manager import SALES_SELECTION_SCHEMA
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


def agy_result(response):
    return Mock(
        returncode=0,
        stdout=json.dumps(
            {"event": "result", "result": {"status": "SUCCESS", "response": response}}
        )
        + "\n",
        stderr="",
    )


def configure_routes(app):
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["manager"] = [
        ["agy", "test-model"],
        ["codex", "unused-model"],
    ]


def test_schema_reaches_native_agy_cli_through_routing(
    initialized_orchestrator, monkeypatch, capsys
):
    app = initialized_orchestrator
    configure_routes(app)
    run = Mock(return_value=agy_result('{"use_sales": false}'))
    monkeypatch.setattr(backends.subprocess, "run", run)
    result = app.call_role_model_routes(
        "manager",
        "Use the supplied requirements",
        response_validator=is_strict_json_response,
        response_schema=SALES_SELECTION_SCHEMA,
    )
    assert json.loads(result) == {"use_sales": False}
    command = run.call_args.args[0]
    assert (
        json.loads(command[command.index("--json-schema") + 1])
        == SALES_SELECTION_SCHEMA
    )
    assert command[-4:] == [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    prompt = json.loads(run.call_args.kwargs["input"])["message"]["content"]
    assert "Do not run commands" in prompt
    assert "request interactive confirmation" in prompt
    log = capsys.readouterr().out
    assert "--json-schema <schema>" in log
    assert '"additionalProperties"' not in log


@pytest.mark.parametrize("response", ["", "  \n"])
def test_empty_agy_result_stops_without_fallback_and_records_failure(
    initialized_orchestrator, monkeypatch, response
):
    app = initialized_orchestrator
    configure_routes(app)
    run = Mock(return_value=agy_result(response))
    monkeypatch.setattr(backends.subprocess, "run", run)
    fallback = Mock()
    monkeypatch.setattr(app, "call_codex", fallback)
    with pytest.raises(
        RuntimeError, match="Strict JSON contract failed.*empty final response"
    ):
        app.call_role_model_routes(
            "manager",
            "Task",
            response_validator=is_strict_json_response,
            response_schema=SALES_SELECTION_SCHEMA,
        )
    run.assert_called_once()
    fallback.assert_not_called()
    records = [
        json.loads(line)
        for line in (app.ai_dir / "llm_usage.jsonl").read_text().splitlines()
    ]
    assert records[-1]["success"] is False
    assert records[-1]["error_category"] == "contract"


def test_invalid_nonempty_json_still_stops_without_correction(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    configure_routes(app)
    run = Mock(return_value=agy_result("Here is an explanation, not JSON."))
    monkeypatch.setattr(backends.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Strict JSON contract failed"):
        app.call_role_model_routes(
            "manager",
            "Task",
            response_validator=is_strict_json_response,
            response_schema=SALES_SELECTION_SCHEMA,
        )
    run.assert_called_once()


def test_file_block_calls_do_not_receive_json_schema(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.state["state"] = "IMPLEMENTING"
    response = "[FILE_START: app.py]\nvalue = 1\n[FILE_END: app.py]"
    run = Mock(return_value=agy_result(response))
    monkeypatch.setattr(backends.subprocess, "run", run)
    assert app.call_agy("Task", role="developer_senior", model="test-model") == response
    assert "--json-schema" not in run.call_args.args[0]
    assert "structured-response stage" not in run.call_args.kwargs["input"]


def test_direct_agy_route_passes_schema_and_does_not_fallback_on_empty(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.config["backends"]["manager"] = "agy"
    monkeypatch.setattr(app, "call_role_model_routes", Mock(return_value=None))
    monkeypatch.setattr(
        app, "get_active_model_for_role", Mock(return_value="test-model")
    )
    run = Mock(return_value=agy_result(""))
    monkeypatch.setattr(backends.subprocess, "run", run)
    fallback = Mock()
    monkeypatch.setattr(app, "call_agent_ollama_fallback", fallback)
    with pytest.raises(
        RuntimeError, match="Strict JSON contract failed.*empty final response"
    ):
        app.call_agent("manager", "Task", response_schema=SALES_SELECTION_SCHEMA)
    command = run.call_args.args[0]
    assert (
        json.loads(command[command.index("--json-schema") + 1])
        == SALES_SELECTION_SCHEMA
    )
    run.assert_called_once()
    fallback.assert_not_called()
