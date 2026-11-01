"""Verify AGY's native JSON contract without making paid model calls."""

import json
from unittest.mock import Mock

import pytest

from orchestrator.core import backends
from orchestrator.roles.base_agent import is_strict_json_response
from orchestrator.roles.manager import SALES_SELECTION_SCHEMA
from orchestrator.roles.developer import TASK_PLAN_SCHEMA, is_task_plan_response
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


def agy_result(response, **fields):
    return Mock(
        returncode=0,
        stdout=json.dumps(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": response, **fields},
            }
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
    monkeypatch.setattr(backends, "run_agy_stream", run)
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
    assert "finish tool is permitted and must be used" in prompt
    assert "Do not use other tools" in prompt
    assert "invoke tools" not in prompt
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
    monkeypatch.setattr(backends, "run_agy_stream", run)
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


@pytest.mark.parametrize("encoded", [False, True])
def test_native_structured_output_takes_precedence(
    initialized_orchestrator, monkeypatch, encoded
):
    app = initialized_orchestrator
    configure_routes(app)
    data = {
        "tasks": [{"id": "T1", "target_files": ["answer.py"]}],
        "staffing": {},
        "specialists": [],
    }
    run = Mock(
        return_value=agy_result(
            "Summary text that is not JSON.",
            structured_output=json.dumps(data) if encoded else data,
        )
    )
    monkeypatch.setattr(backends, "run_agy_stream", run)
    response = app.call_role_model_routes(
        "manager",
        "Task",
        response_validator=is_task_plan_response,
        response_schema=TASK_PLAN_SCHEMA,
    )
    assert json.loads(response) == data
    run.assert_called_once()
    assert not (app.ai_dir / "diagnostics").exists()


def test_identical_planner_and_finish_json_are_accepted(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    configure_routes(app)
    data = {"tasks": [{"id": "T1"}], "staffing": {}, "specialists": []}
    response = json.dumps(data, indent=2) + "\n\n" + json.dumps(data, sort_keys=True)
    run = Mock(return_value=agy_result(response))
    monkeypatch.setattr(backends, "run_agy_stream", run)
    assert (
        json.loads(
            app.call_role_model_routes(
                "manager",
                "Task",
                response_validator=is_task_plan_response,
                response_schema=TASK_PLAN_SCHEMA,
            )
        )
        == data
    )
    run.assert_called_once()


@pytest.mark.parametrize(
    "response,fields",
    [
        ('{"use_sales": false}\n{"use_sales": true}', {}),
        ('prefix {"use_sales": false}', {}),
        ('{"use_sales": false} trailing explanation', {}),
        ('```json\n{"use_sales": false}\n```', {}),
        ('{"use_sales": false}', {"structured_output": "not JSON"}),
        ('{"use_sales": false}', {"structured_output": {"tasks": "not an array"}}),
    ],
)
def test_invalid_structured_result_stops_and_saves_private_diagnostic(
    initialized_orchestrator, monkeypatch, response, fields
):
    app = initialized_orchestrator
    configure_routes(app)
    run = Mock(return_value=agy_result(response, **fields))
    monkeypatch.setattr(backends, "run_agy_stream", run)
    fallback = Mock()
    monkeypatch.setattr(app, "call_codex", fallback)
    validator = (
        is_task_plan_response
        if fields.get("structured_output") == {"tasks": "not an array"}
        else is_strict_json_response
    )
    with pytest.raises(
        RuntimeError, match="Strict JSON contract failed.*Diagnostic saved to"
    ):
        app.call_role_model_routes(
            "manager",
            "Task",
            response_validator=validator,
            response_schema=TASK_PLAN_SCHEMA
            if validator is is_task_plan_response
            else SALES_SELECTION_SCHEMA,
        )
    run.assert_called_once()
    fallback.assert_not_called()
    diagnostics = list((app.ai_dir / "diagnostics").glob("agy-contract-*.json"))
    assert len(diagnostics) == 1
    assert diagnostics[0].stat().st_mode & 0o777 == 0o600
    diagnostic = json.loads(diagnostics[0].read_text())
    assert diagnostic["error"]
    assert diagnostic["output"]


def test_file_blocks_ignore_structured_output(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    response = "[FILE_START: answer.py]\nvalue = 1\n[FILE_END: answer.py]"
    run = Mock(return_value=agy_result(response, structured_output={"unrelated": True}))
    monkeypatch.setattr(backends, "run_agy_stream", run)
    assert app.call_agy("Task", model="test-model") == response


def test_invalid_nonempty_json_still_stops_without_correction(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    configure_routes(app)
    run = Mock(return_value=agy_result("Here is an explanation, not JSON."))
    monkeypatch.setattr(backends, "run_agy_stream", run)
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
    monkeypatch.setattr(backends, "run_agy_stream", run)
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
    monkeypatch.setattr(backends, "run_agy_stream", run)
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


def test_stream_result_survives_cli_shutdown_hang(tmp_path):
    import sys
    import time

    wire = agy_result("完整 response").stdout
    started = time.monotonic()
    result = backends.run_agy_stream(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.read(); "
            f"print({wire!r}, end='', flush=True); time.sleep(60)",
        ],
        cwd=tmp_path,
        input="x" * 200_000,
        timeout=8,
        exit_grace=0,
    )
    assert time.monotonic() - started < 6
    assert result.returncode == 0
    assert result.stdout == wire


@pytest.mark.parametrize(
    "output",
    [
        '{"event":"assistant","message":"working"}\n',
        '{"event":"result","result":{"status":"SUCCESS"}}',
        '{"event":"result",\n',
    ],
)
def test_stream_without_complete_result_times_out(tmp_path, output):
    import subprocess
    import sys

    with pytest.raises(subprocess.TimeoutExpired):
        backends.run_agy_stream(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.stdin.read(); "
                f"print({output!r}, end='', flush=True); time.sleep(60)",
            ],
            cwd=tmp_path,
            input="request",
            timeout=0.2,
            exit_grace=0,
        )


def test_stream_preserves_natural_exit_failure(tmp_path):
    import sys

    result = backends.run_agy_stream(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); "
            "print('error', file=sys.stderr); sys.exit(7)",
        ],
        cwd=tmp_path,
        input="request",
        timeout=5,
    )
    assert result.returncode == 7
    assert result.stderr.strip() == "error"


@pytest.mark.parametrize(
    "final",
    [
        {"status": "ERROR", "response": "failed"},
        {"status": "SUCCESS", "response": ""},
    ],
)
def test_hanging_cli_result_still_requires_valid_contract(
    initialized_orchestrator, monkeypatch, tmp_path, final
):
    import sys

    wire = json.dumps({"event": "result", "result": final}) + "\n"
    result = backends.run_agy_stream(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.read(); "
            f"print({wire!r}, end='', flush=True); time.sleep(60)",
        ],
        cwd=tmp_path,
        input="request",
        timeout=8,
        exit_grace=0,
    )
    monkeypatch.setattr(backends, "run_agy_stream", Mock(return_value=result))
    with pytest.raises(RuntimeError):
        backends.call_agy(initialized_orchestrator, "Task", model="test-model")


@pytest.mark.skipif(backends.os.name != "posix", reason="POSIX process groups")
def test_stream_reaps_cli_when_child_keeps_pipes_open(tmp_path, monkeypatch):
    import signal
    import sys

    spawned = []
    popen = backends.subprocess.Popen

    def record_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(backends.subprocess, "Popen", record_process)
    killpg = Mock(wraps=backends.os.killpg)
    monkeypatch.setattr(backends.os, "killpg", killpg)
    wire = agy_result("done").stdout
    script = (
        "import subprocess,sys; sys.stdin.read(); "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "sys.stderr.write('x' * 200000); sys.stderr.flush(); "
        f"print({wire!r}, end='', flush=True)"
    )
    result = backends.run_agy_stream(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        input="request",
        timeout=8,
        exit_grace=0,
    )
    assert result.returncode == 0
    assert result.stdout == wire
    assert result.stderr == "x" * 200000
    assert spawned[0].poll() is not None
    assert spawned[0].stdout.closed
    killpg.assert_any_call(spawned[0].pid, signal.SIGKILL)
