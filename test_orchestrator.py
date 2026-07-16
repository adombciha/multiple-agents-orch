from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock, call

import pytest

import orchestrator
import orchestrator.main as orchestrator_main
from orchestrator import DEFAULT_CONFIG, AgentOrchestrator
from orchestrator.core import backends, grok, gpt
from orchestrator.core.telemetry import record_call, summary_markdown
from orchestrator.core.backends import quota_exhausted
from orchestrator.core.grok import extract_schema_payload
from orchestrator.roles.base_agent import is_json_response
from orchestrator.roles.developer import is_task_plan_response
from orchestrator.roles.manager import is_requirements_json_response, render_requirements


def write_ai_company(
    workspace: Path,
    *,
    config_overrides: dict | None = None,
    state_overrides: dict | None = None,
) -> tuple[dict, dict]:
    def merge(base: dict, overrides: dict) -> dict:
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value
        return base

    ai_dir = workspace / ".ai-company"
    ai_dir.mkdir()

    config = copy.deepcopy(DEFAULT_CONFIG)
    if config_overrides:
        merge(config, config_overrides)

    state = {
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
        "staffing": {"rd": {"senior": 1, "middle": 0, "junior": 0}, "qa": {"senior": 1, "middle": 0, "junior": 0}},
    }
    if state_overrides:
        state.update(state_overrides)

    (ai_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (ai_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return config, state


@pytest.fixture(autouse=True)
def restore_default_config():
    """Ensure DEFAULT_CONFIG is not mutated by other tests."""
    original = copy.deepcopy(DEFAULT_CONFIG)
    yield
    DEFAULT_CONFIG.clear()
    DEFAULT_CONFIG.update(original)


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        Mock(return_value=Mock(returncode=1, stdout="false\n", stderr="")),
    )


@pytest.fixture
def initialized_orchestrator(tmp_path, no_git):
    write_ai_company(tmp_path)
    app = AgentOrchestrator(tmp_path)
    app.load_config_and_state()
    return app


def test_initialized_orchestrator_fixture_loads_temp_config_and_state(initialized_orchestrator):
    assert initialized_orchestrator.config == DEFAULT_CONFIG
    assert initialized_orchestrator.state["state"] == "PLANNING"
    assert initialized_orchestrator.has_git is False


def test_record_call_writes_metadata_without_prompt_or_output(initialized_orchestrator):
    record_call(
        initialized_orchestrator,
        role="developer_junior",
        backend="ollama",
        model="test-model",
        prompt="secret prompt",
        system_prompt="private system prompt",
        output="secret output",
        elapsed_seconds=0.25,
    )

    usage_path = initialized_orchestrator.ai_dir / "llm_usage.jsonl"
    record = json.loads(usage_path.read_text(encoding="utf-8").strip())
    assert record["role"] == "developer_junior"
    assert record["backend"] == "ollama"
    assert record["model"] == "test-model"
    assert record["input_characters"] > 0
    assert record["output_characters"] > 0
    assert "secret prompt" not in usage_path.read_text(encoding="utf-8")
    assert "secret output" not in usage_path.read_text(encoding="utf-8")


def test_record_call_uses_litellm_token_counter(initialized_orchestrator, monkeypatch):
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.token_counter = Mock(return_value=7)
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    record_call(
        initialized_orchestrator,
        role="qa_junior",
        backend="ollama",
        model="gemma4:latest",
        prompt="prompt",
        system_prompt=None,
        output="output",
        elapsed_seconds=0.1,
    )

    record = json.loads((initialized_orchestrator.ai_dir / "llm_usage.jsonl").read_text(encoding="utf-8").strip())
    assert record["input_tokens"] == 7
    assert record["output_tokens"] == 7
    assert record["total_tokens"] == 14
    assert record["token_status"] == "exact"


def test_usage_summary_markdown_groups_calls_by_model(initialized_orchestrator, monkeypatch):
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.token_counter = Mock(return_value=3)
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    record_call(
        initialized_orchestrator,
        role="manager",
        backend="grok",
        model="grok-4.5",
        prompt="prompt",
        system_prompt=None,
        output="output",
        elapsed_seconds=0.1,
    )

    summary = summary_markdown(initialized_orchestrator)
    assert "## LiteLLM Token Usage" in summary
    assert "`grok-4.5`" in summary
    assert "| 6 |" in summary


def test_call_with_usage_records_success_and_failure(initialized_orchestrator):
    assert initialized_orchestrator._call_with_usage(
        "manager", "grok", "grok-4.5", "prompt", None, lambda: "response",
    ) == "response"

    with pytest.raises(RuntimeError, match="failed"):
        initialized_orchestrator._call_with_usage(
            "manager", "grok", "grok-4.5", "prompt", None,
            lambda: (_ for _ in ()).throw(RuntimeError("failed")),
        )

    records = [
        json.loads(line)
        for line in (initialized_orchestrator.ai_dir / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["success"] for record in records] == [True, False]


def test_write_ai_company_overrides_do_not_mutate_default_config(tmp_path):
    config, state = write_ai_company(
        tmp_path,
        config_overrides={"backends": {"developer": "ollama"}, "use_worktree": False},
        state_overrides={"state": "TESTING"},
    )

    assert config["backends"]["developer"] == "ollama"
    assert config["backends"]["manager"] == DEFAULT_CONFIG["backends"]["manager"]
    assert DEFAULT_CONFIG["backends"]["developer"] == "codex"
    assert config["use_worktree"] is False
    assert DEFAULT_CONFIG["use_worktree"] is True
    assert state["state"] == "TESTING"


def test_load_config_and_state_loads_config_and_state_from_files(tmp_path, no_git):
    config, state = write_ai_company(
        tmp_path,
        config_overrides={"ollama_model": "test-model", "use_worktree": False},
        state_overrides={"state": "IMPLEMENTING", "tasks": [{"id": "T-1"}]},
    )

    app = AgentOrchestrator(tmp_path)
    app.load_config_and_state()

    assert app.config == config
    expected_state = copy.deepcopy(state)
    expected_state.setdefault("specialists", [])
    expected_state.setdefault("staffing", {})
    expected_state.setdefault("worker_assignments", {})
    expected_state.setdefault("last_developer_role", "developer_senior")
    expected_state.setdefault("developer_promotions", {})
    expected_state.setdefault("failed_model_routes", [])
    expected_state.setdefault("task_failed_model_routes", {})
    expected_state.setdefault("task_developer_promotions", {})
    assert app.state == expected_state


def test_load_config_and_state_missing_config_keeps_default_config(tmp_path, no_git):
    ai_dir = tmp_path / ".ai-company"
    ai_dir.mkdir()
    (ai_dir / "state.json").write_text(json.dumps({"state": "PLANNING"}), encoding="utf-8")
    app = AgentOrchestrator(tmp_path)

    with pytest.raises(FileNotFoundError):
        app.load_config_and_state()

    assert app.config == DEFAULT_CONFIG


def test_constructor_detects_git_and_current_branch(tmp_path, monkeypatch):
    run = Mock(side_effect=[
        Mock(returncode=0, stdout="true\n"),
        Mock(returncode=0, stdout="main\n"),
    ])
    monkeypatch.setattr(orchestrator.subprocess, "run", run)

    app = AgentOrchestrator(tmp_path)

    assert app.has_git is True
    assert app.base_branch == "main"
    assert app.ai_dir.name == f".ai-company-{tmp_path.name}-main"


def test_constructor_non_git_output_is_safe_default(tmp_path, monkeypatch):
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        Mock(return_value=Mock(returncode=0, stdout="false\n")),
    )

    app = AgentOrchestrator(tmp_path)

    assert app.has_git is False
    assert app.base_branch == "master"
    assert app.ai_dir.name == ".ai-company"


def test_init_clears_completed_run_but_keeps_config(initialized_orchestrator):
    initialized_orchestrator.config["ollama_url"] = "http://windows:11434"
    initialized_orchestrator.config_path.write_text(json.dumps(initialized_orchestrator.config), encoding="utf-8")
    initialized_orchestrator.state["state"] = "COMPLETED"
    initialized_orchestrator.save_state()
    initialized_orchestrator.request_path.write_text("old request", encoding="utf-8")

    initialized_orchestrator.init_project()
    initialized_orchestrator.load_config_and_state()

    assert initialized_orchestrator.state["state"] == "PLANNING"
    assert initialized_orchestrator.config["ollama_url"] == "http://windows:11434"
    assert not initialized_orchestrator.request_path.exists()


def test_save_state_writes_current_state_to_state_file(initialized_orchestrator):
    initialized_orchestrator.state = {
        "state": "TESTING",
        "plan_revisions": 1,
        "code_revisions": 2,
        "tasks": [{"id": "T-1", "status": "pending"}],
    }

    initialized_orchestrator.save_state()

    assert json.loads(initialized_orchestrator.state_path.read_text(encoding="utf-8")) == initialized_orchestrator.state


def test_run_command_success_returns_stdout_and_stderr(initialized_orchestrator, monkeypatch):
    run = Mock(return_value=Mock(returncode=0, stdout="ok\n", stderr="warn\n"))
    monkeypatch.setattr(orchestrator.subprocess, "run", run)

    returncode, output = initialized_orchestrator.run_command(["test", "cmd"])

    assert returncode == 0
    assert output == "stdout:\nok\n\nstderr:\nwarn\n"
    run.assert_called_once_with(
        ["test", "cmd"],
        cwd=initialized_orchestrator.workspace,
        stdout=orchestrator.subprocess.PIPE,
        stderr=orchestrator.subprocess.PIPE,
        text=True,
        timeout=1800,
        check=False,
    )


def test_run_command_explicit_cwd_is_passed_through(initialized_orchestrator, tmp_path, monkeypatch):
    cwd = tmp_path / "elsewhere"
    run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(orchestrator.subprocess, "run", run)

    initialized_orchestrator.run_command(["pwd"], cwd=cwd)

    assert run.call_args.kwargs["cwd"] == cwd


@pytest.mark.parametrize("state", ["IMPLEMENTING", "TESTING", "REVIEWING_CODE"])
def test_run_command_uses_worktree_cwd_for_agent_states(initialized_orchestrator, state, monkeypatch):
    worktree = initialized_orchestrator.ai_dir / "worktree"
    worktree.mkdir()
    initialized_orchestrator.state["state"] = state
    run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(orchestrator.subprocess, "run", run)

    initialized_orchestrator.run_command(["pwd"])

    assert run.call_args.kwargs["cwd"] == worktree


def test_direct_cli_backends_use_agent_worktree(initialized_orchestrator, monkeypatch):
    worktree = initialized_orchestrator.ai_dir / "worktree"
    worktree.mkdir()
    run = Mock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(backends.subprocess, "run", run)

    backends.call_agy(initialized_orchestrator, "prompt", role="developer_junior", model="test-model")

    assert run.call_args.kwargs["cwd"] == worktree


def test_agy_maps_legacy_model_ids_to_supported_gemini_names(initialized_orchestrator, monkeypatch):
    run = Mock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(backends.subprocess, "run", run)

    backends.call_agy(initialized_orchestrator, "prompt", model="gemini-3.1-pro")

    assert run.call_args.args[0][0:3] == ["agy", "--model", "Gemini 3.1 Pro (Low)"]


def test_grok_uses_agent_worktree(initialized_orchestrator, monkeypatch):
    worktree = initialized_orchestrator.ai_dir / "worktree"
    worktree.mkdir()
    run = Mock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(grok.subprocess, "run", run)

    grok.call(initialized_orchestrator, "prompt", role="developer_junior", model="test-model")

    assert run.call_args.kwargs["cwd"] == worktree


def test_run_command_use_worktree_false_runs_in_workspace(initialized_orchestrator, monkeypatch):
    (initialized_orchestrator.ai_dir / "worktree").mkdir()
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(orchestrator.subprocess, "run", run)

    initialized_orchestrator.run_command(["pwd"])

    assert run.call_args.kwargs["cwd"] == initialized_orchestrator.workspace


def test_run_command_timeout_returns_error_tuple(initialized_orchestrator, monkeypatch):
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        Mock(side_effect=orchestrator.subprocess.TimeoutExpired(["slow"], 1)),
    )

    returncode, output = initialized_orchestrator.run_command(["slow"], timeout=1)

    assert returncode == -1
    assert "Timeout expired" in output


def test_run_command_subprocess_exception_returns_error_tuple(initialized_orchestrator, monkeypatch):
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        Mock(side_effect=OSError("boom")),
    )

    returncode, output = initialized_orchestrator.run_command(["bad"])

    assert returncode == -1
    assert "Error running command: boom" in output


def test_call_ollama_posts_chat_payload_and_returns_message_content(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["model_tier_indices"]["reviewer"] = 1
    response = Mock()
    response.json.return_value = {"message": {"content": "agent answer"}}
    post = Mock(return_value=response)
    monkeypatch.setattr(orchestrator.requests, "post", post)

    result = initialized_orchestrator.call_ollama("do it", system_prompt="be brief", role="reviewer")

    assert result == "agent answer"
    post.assert_called_once_with(
        f"{initialized_orchestrator.config['ollama_url']}/api/chat",
        json={
            "model": "gemma2:2b",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "do it"},
            ],
            "stream": False,
            "keep_alive": 0,
            "think": "high",
        },
        timeout=1800,
    )
    response.raise_for_status.assert_called_once_with()


def test_call_ollama_retries_without_thinking_when_model_rejects_it(initialized_orchestrator, monkeypatch):
    rejected = Mock(status_code=400)
    accepted = Mock(status_code=200)
    accepted.json.return_value = {"message": {"content": "answer"}}
    post = Mock(side_effect=[rejected, accepted])
    monkeypatch.setattr(orchestrator.requests, "post", post)

    assert initialized_orchestrator.call_ollama("do it", model="plain-model") == "answer"
    assert post.call_args_list[0].kwargs["json"]["think"] == "high"
    assert "think" not in post.call_args_list[1].kwargs["json"]
    assert "plain-model" in initialized_orchestrator._ollama_no_think_models


def test_gpt_oss_developer_uses_json_files_and_returns_file_blocks(initialized_orchestrator, monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"message": {"content": json.dumps({"files": [{"path": "app.py", "content": "print('ok')"}]})}}
    post = Mock(return_value=response)
    monkeypatch.setattr(gpt.requests, "post", post)

    result = backends.call_ollama(initialized_orchestrator, "write app", role="developer_senior", model="gpt-oss:20b")

    assert result == "[FILE_START: app.py]\nprint('ok')\n[FILE_END: app.py]"
    payload = post.call_args.kwargs["json"]
    assert payload["format"] == gpt.FILE_RESPONSE_SCHEMA
    assert payload["think"] == "high"
    assert payload["options"]["num_predict"] == 8192
    assert "complete file content" in payload["messages"][0]["content"]
    assert "GPT-OSS output override" in payload["messages"][1]["content"]


def test_gpt_oss_retries_json_without_thinking(initialized_orchestrator, monkeypatch):
    rejected = Mock(status_code=400)
    accepted = Mock(status_code=200)
    accepted.json.return_value = {"message": {"content": json.dumps({"files": [{"path": "app.py", "content": "ok"}]})}}
    post = Mock(side_effect=[rejected, accepted])
    monkeypatch.setattr(gpt.requests, "post", post)

    backends.call_ollama(initialized_orchestrator, "write app", role="developer_senior", model="gpt-oss:20b")

    assert "think" in post.call_args_list[0].kwargs["json"]
    assert "think" not in post.call_args_list[1].kwargs["json"]
    assert "format" in post.call_args_list[1].kwargs["json"]


def test_gpt_oss_section_mode_returns_section_blocks(initialized_orchestrator, monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"message": {"content": json.dumps({"sections": [{"path": "README.md", "content": "Updated"}]})}}
    post = Mock(return_value=response)
    monkeypatch.setattr(gpt.requests, "post", post)

    result = backends.call_ollama(
        initialized_orchestrator,
        "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Install",
        role="developer_senior",
        model="gpt-oss:20b",
    )

    assert result == "[SECTION_EDIT_START: README.md]\n[HEADING]\n\n[CONTENT]\nUpdated\n[SECTION_EDIT_END: README.md]"
    assert post.call_args.kwargs["json"]["format"] == gpt.SECTION_RESPONSE_SCHEMA


def test_gpt_oss_rejects_path_outside_task_contract(initialized_orchestrator, monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"message": {"content": json.dumps({"sections": [{"path": "README_EN.md", "content": "wrong"}]})}}
    monkeypatch.setattr(gpt.requests, "post", Mock(return_value=response))

    with pytest.raises(RuntimeError, match="outside task contract"):
        backends.call_ollama(
            initialized_orchestrator,
            'Machine contract: {"target_files":["README_en.md"]}\n[SECTION_EDIT_START: README_en.md]',
            role="developer_senior",
            model="gpt-oss:20b",
        )


def test_gpt_oss_retries_truncated_json_with_larger_output_limit(initialized_orchestrator, monkeypatch):
    truncated = Mock(status_code=200)
    truncated.json.return_value = {"message": {"content": '{"sections":[{"path":"README_ja.md","content":"unterminated'}}
    complete = Mock(status_code=200)
    complete.json.return_value = {"message": {"content": json.dumps({"sections": [{"path": "README_ja.md", "content": "fixed"}]})}}
    post = Mock(side_effect=[truncated, complete])
    monkeypatch.setattr(gpt.requests, "post", post)

    result = backends.call_ollama(
        initialized_orchestrator,
        'Machine contract: {"target_files":["README_ja.md"]}\n[SECTION_EDIT_START: README_ja.md]',
        role="developer_senior",
        model="gpt-oss:20b",
    )

    assert "fixed" in result
    assert post.call_args_list[1].kwargs["json"]["options"]["num_predict"] == 16384

def test_call_ollama_falls_back_to_configured_model(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.config["ollama_model"] = "fallback-model"
    monkeypatch.setattr(initialized_orchestrator, "get_active_model_for_role", Mock(return_value=None))
    response = Mock()
    response.json.return_value = {"message": {"content": "fallback answer"}}
    post = Mock(return_value=response)
    monkeypatch.setattr(orchestrator.requests, "post", post)

    result = initialized_orchestrator.call_ollama("do it")

    assert result == "fallback answer"
    assert post.call_args.kwargs["json"]["model"] == "fallback-model"


def test_configure_ollama_uses_detected_windows_endpoint(initialized_orchestrator, monkeypatch):
    monkeypatch.setattr(orchestrator_main, "ollama_available", Mock(side_effect=[False, True]))
    monkeypatch.setattr(initialized_orchestrator, "get_windows_host_ip", Mock(return_value="172.17.144.1"))
    monkeypatch.setattr(orchestrator_main.sys.stdin, "isatty", Mock(return_value=False))

    orchestrator_main.configure_ollama(initialized_orchestrator)

    assert initialized_orchestrator.config["ollama_url"] == "http://172.17.144.1:11434"


def test_call_ollama_encodes_local_image(initialized_orchestrator, monkeypatch, tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(b"image")
    response = Mock()
    response.json.return_value = {"message": {"content": "review"}}
    post = Mock(return_value=response)
    monkeypatch.setattr(orchestrator.requests, "post", post)

    initialized_orchestrator.call_ollama("review", image_paths=[str(image)])

    assert post.call_args.kwargs["json"]["messages"][-1]["images"] == ["aW1hZ2U="]


def test_call_ollama_request_exception_raises_runtime_error(initialized_orchestrator, monkeypatch):
    monkeypatch.setattr(
        orchestrator.requests,
        "post",
        Mock(side_effect=orchestrator.requests.exceptions.RequestException("down")),
    )

    with pytest.raises(RuntimeError, match="Ollama connection failed: down"):
        initialized_orchestrator.call_ollama("hello")


def test_grok_structured_call_uses_schema_and_logs_capable_command(initialized_orchestrator, monkeypatch):
    completed = Mock(returncode=0, stdout='{"ok":true}', stderr="")
    runner = Mock(return_value=completed)
    monkeypatch.setattr(grok.subprocess, "run", runner)

    assert backends.call_grok(
        initialized_orchestrator,
        "prompt",
        role="manager",
        model="grok-4.5",
        response_schema={"type": "object"},
    ) == '{"ok":true}'

    command = runner.call_args.args[0]
    assert command[0:2] == ["grok", "-p"]
    assert "--effort" in command
    assert "medium" in command
    assert "--json-schema" in command


def test_grok_json_envelope_extracts_schema_payload():
    payload = {
        "project_overview": "Sync docs",
        "functional_requirements": [],
        "technical_specifications": [],
        "scope_boundaries": [],
    }
    envelope = json.dumps({"session_id": "123", "result": {"content": json.dumps(payload)}})
    schema = {"required": list(payload)}

    assert json.loads(extract_schema_payload(envelope, schema)) == payload


def test_grok_developer_uses_single_turn_coding_mode(initialized_orchestrator, monkeypatch):
    completed = Mock(
        returncode=0,
        stdout="[FILE_START: hello.py]\nprint('ok')\n[FILE_END: hello.py]",
        stderr="",
    )
    runner = Mock(return_value=completed)
    monkeypatch.setattr(grok.subprocess, "run", runner)

    backends.call_grok(
        initialized_orchestrator,
        "edit prompt",
        role="developer_middle",
        model="grok-4.5",
    )

    command = runner.call_args.args[0]
    assert "--json-schema" not in command
    assert "--max-turns" in command
    assert "--no-plan" in command
    assert "--no-subagents" in command
    assert "--no-memory" in command
    assert "--disable-web-search" in command
    assert "--verbatim" in command


def test_task_plan_validator_requires_tasks_array():
    assert not is_task_plan_response('{"requirements": "not a task plan"}')
    assert is_task_plan_response('{"tasks": [], "staffing": {}, "specialists": []}')


def test_requirements_validator_requires_structured_json():
    assert not is_requirements_json_response("I'll inspect the Python implementation first.")
    data = {
        "project_overview": "Build it",
        "functional_requirements": ["It works"],
        "technical_specifications": ["Python"],
        "scope_boundaries": ["No redesign"],
    }
    assert is_requirements_json_response(json.dumps(data))
    assert "## Project Overview & Context" in render_requirements(data)


def test_staffing_is_capped_by_configured_capacity(initialized_orchestrator):
    initialized_orchestrator.state["staffing"] = {"qa": {"senior": 4, "junior": -1}}

    assert initialized_orchestrator.staffing("qa") == {"senior": 1, "middle": 0, "junior": 0}


def test_role_models_select_the_configured_seniority_model(initialized_orchestrator):
    assert initialized_orchestrator.get_active_model_for_role("developer_senior", "codex") == "gpt-5.6-luna"
    assert initialized_orchestrator.get_active_model_for_role("developer_middle", "ollama") == "granite4.1:8b"
    assert initialized_orchestrator.get_active_model_for_role("developer_junior", "ollama") == "gemma4:latest"
    assert initialized_orchestrator.get_active_model_for_role("architect", "grok") == "grok-4.5"
    assert initialized_orchestrator.get_active_model_for_role("ra", "grok") == "grok-4.5"
    assert initialized_orchestrator.get_active_model_for_role("sales", "grok") == "grok-4.5"
    assert initialized_orchestrator.get_active_model_for_role("qa_senior", "ollama") == "deepseek-r1:7b"
    assert initialized_orchestrator.get_active_model_for_role("qa_middle", "ollama") == "qwen2.5-coder:14b"
    assert initialized_orchestrator.get_active_model_for_role("qa_junior", "ollama") == "gemma4:latest"


def test_load_config_migrates_new_role_defaults(tmp_path, no_git):
    ai_dir = tmp_path / ".ai-company"
    ai_dir.mkdir()
    (ai_dir / "config.json").write_text(json.dumps({"backends": {"developer": "codex"}}), encoding="utf-8")
    (ai_dir / "state.json").write_text(json.dumps({"state": "PLANNING"}), encoding="utf-8")

    app = AgentOrchestrator(tmp_path)
    app.load_config_and_state()

    assert app.get_backend("developer_junior") == "codex"
    assert app.get_active_model_for_role("reviewer", "codex") == "gpt-5.6-luna"
    assert app.config["ollama_keep_alive"] == 0


def test_role_model_is_not_used_for_an_ollama_fallback(initialized_orchestrator):
    assert initialized_orchestrator.get_active_model_for_role("manager", "ollama") == "gemma4:latest"


def test_developer_plan_does_not_require_file_blocks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["state"] = "DEVELOPING_PLAN"
    initialized_orchestrator.config["role_model_routes"]["developer_senior"] = [["grok", "grok-4.5"]]
    monkeypatch.setattr(initialized_orchestrator, "call_grok", Mock(return_value="# Plan"))

    assert initialized_orchestrator.call_agent("developer_senior", "prompt") == "# Plan"


def test_invalid_architect_status_falls_back_to_next_route(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["state"] = "REVIEWING_PLAN"
    initialized_orchestrator.config["role_model_routes"]["architect"] = [["grok", "grok-4.5"], ["ollama", "qwen3:8b"]]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(initialized_orchestrator, "call_grok", Mock(return_value="..."))
    monkeypatch.setattr(initialized_orchestrator, "call_agent_ollama_fallback", Mock(return_value="PLAN_STATUS: APPROVED\nok"))

    result = initialized_orchestrator.call_agent("architect", "prompt")

    assert result.startswith("PLAN_STATUS: APPROVED")
    assert initialized_orchestrator.state["failed_model_routes"] == []


def test_output_contract_failure_falls_back_to_next_route(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    initialized_orchestrator.config["role_model_routes"]["developer_junior"] = [
        ["grok", "grok-4.5"],
        ["ollama", "qwen3:8b"],
    ]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_grok",
        Mock(return_value="[FILE_START: bad.py]\ninvalid\n[FILE_END: bad.py]"),
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent_ollama_fallback",
        Mock(return_value="[FILE_START: good.py]\nvalid\n[FILE_END: good.py]"),
    )

    result = initialized_orchestrator.call_agent(
        "developer_junior",
        "prompt",
        response_validator=lambda response: "good.py" in response,
    )

    assert "good.py" in result
    assert initialized_orchestrator.state["failed_model_routes"] == []


def test_ollama_developer_retries_invalid_contract_once(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    initialized_orchestrator.config["role_model_routes"]["developer_junior"] = [["ollama", "gemma4:latest"]]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    call = Mock(side_effect=["I changed it.", "[FILE_START: app.py]\nok\n[FILE_END: app.py]"])
    monkeypatch.setattr(initialized_orchestrator, "call_agent_ollama_fallback", call)

    result = initialized_orchestrator.call_agent(
        "developer_junior", "prompt", response_validator=lambda response: "app.py" in response
    )

    assert "app.py" in result
    assert call.call_count == 2


def test_implementing_route_failures_are_scoped_to_active_task(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    initialized_orchestrator.state["active_task_id"] = "T-1"
    initialized_orchestrator.config["role_model_routes"]["developer_junior"] = [["grok", "grok-4.5"]]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(initialized_orchestrator, "call_grok", Mock(return_value="invalid"))

    with pytest.raises(RuntimeError):
        initialized_orchestrator.call_agent("developer_junior", "prompt")

    assert initialized_orchestrator.state["task_failed_model_routes"]["T-1"] == []
    assert initialized_orchestrator.state["failed_model_routes"] == []
    initialized_orchestrator.state["active_task_id"] = "T-2"
    assert initialized_orchestrator.state["task_failed_model_routes"].get("T-2", []) == []


def test_developer_route_accepts_markdown_edit_blocks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    initialized_orchestrator.config["role_model_routes"]["developer_junior"] = [["grok", "grok-4.5"]]
    response = "[FILE_EDIT_START: README.md]\n[OLD]\nold\n[NEW]\nnew\n[FILE_EDIT_END: README.md]"
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(initialized_orchestrator, "call_grok", Mock(return_value=response))

    assert initialized_orchestrator.call_agent("developer_junior", "prompt") == response


def test_implementing_prompt_requires_only_file_blocks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "write hello", "status": "pending", "rd_level": "junior", "qa_level": "junior"}]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    call_agent = Mock(return_value="[FILE_START: hello.py]\nprint('hello')\n[FILE_END: hello.py]")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert "respond only with [file_start" in call_agent.call_args.args[2].lower()


def test_implementing_prompt_includes_only_current_target_files(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "update README", "target_files": ["README.md"], "section_heading": "## Quick Start", "output_contract": {"format": "markdown_section_replacements"}, "status": "pending", "rd_level": "junior", "qa_level": "junior"}]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    (initialized_orchestrator.workspace / "README.md").write_text("# Project\n\n## Quick Start\n\nKeep me\n\n## Other\n\nDo not include this section\n", encoding="utf-8")
    (initialized_orchestrator.workspace / "README_en.md").write_text("# Do not include me\n", encoding="utf-8")
    call_agent = Mock(return_value="[SECTION_EDIT_START: README.md]\n[HEADING]\n## Quick Start\n[CONTENT]\nUpdated\n[SECTION_EDIT_END: README.md]")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    prompt = call_agent.call_args.args[1]
    assert "[CURRENT_FILE: README.md]\n## Quick Start" in prompt
    assert "[SECTION_EDIT_START: README.md]" in prompt
    assert "Do not include this section" not in prompt
    assert "Do not include me" not in prompt


def test_implementing_missing_section_heading_falls_back_to_whole_file(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.config["backends"]["developer_junior"] = "grok"
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    target = initialized_orchestrator.workspace / "docs" / "guide.md"
    target.parent.mkdir()
    target.write_text("# Project\n\nExisting\n", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
        "id": "T-1",
        "description": "add installation guide",
        "target_files": ["docs/guide.md"],
        "section_heading": "## Installation",
        "output_contract": {"format": "markdown_section_replacements"},
        "status": "pending",
        "rd_level": "junior",
        "qa_level": "junior",
    }]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    call_agent = Mock(return_value="[FILE_START: docs/guide.md]\n# Project\n\nExisting\n\n## Installation\n\nRun it\n[FILE_END: docs/guide.md]")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert "## Installation" in target.read_text(encoding="utf-8")
    assert "[FILE_START:" in call_agent.call_args.args[1]


def test_split_scoped_fix_tasks_keep_section_contract(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.config["backends"]["developer_senior"] = "ollama"
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    translation_files = [f"README_{index}.md" for index in range(2)]
    initialized_orchestrator.state["tasks"] = [{
        "id": "FIX-REV-2",
        "description": "repair damaged README sections",
        "target_files": translation_files,
        "section_heading": "## Source section",
        "output_contract": {"format": "file_blocks"},
        "status": "pending",
        "rd_level": "senior",
        "qa_level": "junior",
    }]
    initialized_orchestrator.state["staffing"] = {"rd": {"senior": 1}, "qa": {"junior": 1}}
    (initialized_orchestrator.workspace / translation_files[0]).write_text(
        "# Project\n\n## Intro\n\nIntro\n\n## Source section\n\nOld\n\n## Other\n\nKeep\n",
        encoding="utf-8",
    )
    (initialized_orchestrator.workspace / translation_files[1]).write_text(
        "# Project\n\n## Intro\n\nIntro\n\n## Target section\n\nOld\n\n## Other\n\nKeep\n",
        encoding="utf-8",
    )
    call_agent = Mock(side_effect=[
        f"[SECTION_EDIT_START: {translation_files[0]}]\n[HEADING]\n## Source section\n[CONTENT]\nUpdated first\n[SECTION_EDIT_END: {translation_files[0]}]",
        f"[SECTION_EDIT_START: {translation_files[1]}]\n[HEADING]\n## Target section\n[CONTENT]\nUpdated second\n[SECTION_EDIT_END: {translation_files[1]}]",
    ])
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert all("[SECTION_EDIT_START:" in call.args[1] for call in call_agent.call_args_list)
    assert "Updated first" in (initialized_orchestrator.workspace / translation_files[0]).read_text(encoding="utf-8")
    assert "Updated second" in (initialized_orchestrator.workspace / translation_files[1]).read_text(encoding="utf-8")


def test_translation_task_receives_read_only_primary_readme(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
        "id": "T-1", "description": "sync translation README", "target_files": ["README_en.md"],
        "output_contract": {"format": "file_blocks"}, "status": "pending",
        "rd_level": "junior", "qa_level": "junior",
    }]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    (initialized_orchestrator.workspace / "README.md").write_text("# Canonical\n", encoding="utf-8")
    (initialized_orchestrator.workspace / "README_en.md").write_text("# Translation\n", encoding="utf-8")
    call_agent = Mock(return_value="[FILE_START: README_en.md]\n# Translation\n[FILE_END: README_en.md]")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    prompt = call_agent.call_args.args[1]
    assert "[READ_ONLY_REFERENCE: README.md]" in prompt
    assert "# Canonical" in prompt
    assert "must never be modified" in prompt


def test_whole_markdown_task_keeps_declared_file_block_contract(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
        "id": "T-1", "description": "sync README", "target_files": ["README.md"],
        "output_contract": {"format": "file_blocks"}, "status": "pending",
        "rd_level": "junior", "qa_level": "junior",
    }]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n", encoding="utf-8")
    call_agent = Mock(return_value="[FILE_START: README.md]\n# Project\n\n## Install\n\nNew\n[FILE_END: README.md]")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    prompt = call_agent.call_args.args[1]
    assert "[FILE_START: path/to/file.ext]" in prompt
    assert "[SECTION_EDIT_START:" not in prompt
    assert "New" in readme.read_text(encoding="utf-8")


def test_file_blocks_can_replace_headings_for_explicit_full_rewrite(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Install\n\n## Workflow\n\nOriginal\n", encoding="utf-8")
    output = "[FILE_START: README.md]\n# Replacement\n\nNew content\n[FILE_END: README.md]"

    written = initialized_orchestrator.parse_and_write_files(
        output,
        ["README.md"],
        allow_markdown_heading_changes=True,
    )

    assert written == ["README.md"]
    assert readme.read_text(encoding="utf-8") == "# Replacement\n\nNew content"


def test_direct_developer_task_stays_pending_when_no_file_changes(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.config["backends"]["developer_senior"] = "codex"
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
        "id": "T-1",
        "description": "update app.py",
        "target_files": ["app.py"],
        "status": "pending",
        "rd_level": "senior",
        "qa_level": "senior",
    }]
    initialized_orchestrator.state["staffing"] = {"rd": {"senior": 1}, "qa": {"senior": 1}}
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="No edits made"))

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"
    assert initialized_orchestrator.state["tasks"][0]["revisions"] == 1
    assert initialized_orchestrator.call_agent.call_args.args[4] is None


def test_implementing_skips_read_only_inventory_tasks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "Inventory current routes", "status": "pending", "rd_level": "junior", "qa_level": "junior"}]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    call_agent = Mock()
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "completed"
    call_agent.assert_not_called()


def test_legacy_fix_task_inherits_original_target_files(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {"id": "T-1", "description": "change app", "target_files": ["app.py"], "status": "completed", "rd_level": "junior", "qa_level": "junior"},
        {"id": "FIX-REV-1", "description": "fix app", "status": "pending", "rd_level": "middle", "qa_level": "junior"},
    ]
    initialized_orchestrator.state["staffing"] = {"rd": {"middle": 1, "junior": 1}, "qa": {"junior": 1}}
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="[FILE_START: app.py]\nfixed\n[FILE_END: app.py]"))

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["tasks"][-1]["target_files"] == ["app.py"]
    assert (initialized_orchestrator.workspace / "app.py").read_text(encoding="utf-8") == "fixed"


def test_multi_file_fix_task_splits_and_keeps_route_state(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
        "id": "FIX-REV-1", "description": "fix translations",
        "target_files": ["README_en.md", "README_ja.md"], "status": "pending",
        "rd_level": "middle", "qa_level": "junior",
    }]
    initialized_orchestrator.state["staffing"] = {"rd": {"middle": 1}, "qa": {"junior": 1}}
    initialized_orchestrator.state["task_developer_promotions"] = {"FIX-REV-1": {"developer_middle": "developer_senior"}}
    initialized_orchestrator.state["task_failed_model_routes"] = {"FIX-REV-1": ["codex/gpt-5.6-luna"]}
    initialized_orchestrator.config["backends"]["developer_senior"] = "ollama"
    call_agent = Mock(side_effect=[
        "[FILE_START: README_en.md]\nEnglish\n[FILE_END: README_en.md]",
        "[FILE_START: README_ja.md]\nJapanese\n[FILE_END: README_ja.md]",
    ])
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    split_tasks = initialized_orchestrator.state["tasks"]
    assert [task["target_files"] for task in split_tasks] == [["README_en.md"], ["README_ja.md"]]
    assert all(initialized_orchestrator.state["task_developer_promotions"][task["id"]]["developer_middle"] == "developer_senior" for task in split_tasks)
    assert all(initialized_orchestrator.state["task_failed_model_routes"][task["id"]] == ["codex/gpt-5.6-luna"] for task in split_tasks)
    assert call_agent.call_count == 2


def test_implementing_pauses_when_all_model_routes_fail(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "write hello", "target_files": ["hello.py"], "status": "pending", "rd_level": "junior", "qa_level": "junior"}]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(side_effect=RuntimeError("all routes failed")))

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["code_revisions"] == 0
    assert initialized_orchestrator.state["tasks"][0]["revisions"] == 1


def test_file_blocks_cannot_write_outside_task_contract(initialized_orchestrator):
    output = "[FILE_START: allowed.py]\nok\n[FILE_END: allowed.py]\n[FILE_START: test_orchestrator.py]\nwrong\n[FILE_END: test_orchestrator.py]"

    written = initialized_orchestrator.parse_and_write_files(output, ["allowed.py"])

    assert written == ["allowed.py"]
    assert (initialized_orchestrator.workspace / "allowed.py").read_text(encoding="utf-8") == "ok"
    assert not (initialized_orchestrator.workspace / "test_orchestrator.py").exists()


def test_file_blocks_cannot_remove_existing_markdown_headings(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Install\n\n## Workflow\n\nOriginal\n", encoding="utf-8")
    output = "[FILE_START: README.md]\n# New\n\nReplacement\n[FILE_END: README.md]"

    written = initialized_orchestrator.parse_and_write_files(output, ["README.md"])

    assert written == []
    assert readme.read_text(encoding="utf-8") == "# Install\n\n## Workflow\n\nOriginal\n"


def test_markdown_edit_blocks_replace_only_exact_text(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Install\n\nOld command\n\n## Workflow\n\nKeep this\n", encoding="utf-8")
    output = "[FILE_EDIT_START: README.md]\n[OLD]\nOld command\n[NEW]\nNew command\n[FILE_EDIT_END: README.md]"

    written = initialized_orchestrator.parse_and_write_files(output, ["README.md"])

    assert written == ["README.md"]
    assert readme.read_text(encoding="utf-8") == "# Install\n\nNew command\n\n## Workflow\n\nKeep this\n"


def test_markdown_edit_blocks_require_unique_old_text(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Install\n\nSame\nSame\n", encoding="utf-8")
    output = "[FILE_EDIT_START: README.md]\n[OLD]\nSame\n[NEW]\nChanged\n[FILE_EDIT_END: README.md]"

    assert initialized_orchestrator.parse_and_write_files(output, ["README.md"]) == []
    assert readme.read_text(encoding="utf-8") == "# Install\n\nSame\nSame\n"


def test_markdown_section_blocks_replace_only_named_section(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\nIntro\n\n## Install\n\nOld command\n\n## Workflow\n\nKeep this\n", encoding="utf-8")
    output = "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Install\n[CONTENT]\nNew command\n[SECTION_EDIT_END: README.md]"

    written = initialized_orchestrator.parse_and_write_files(output, ["README.md"], allowed_heading="## Install")

    assert written == ["README.md"]
    assert readme.read_text(encoding="utf-8") == "# Project\n\nIntro\n\n## Install\n\nNew command\n\n## Workflow\n\nKeep this\n"


def test_markdown_section_blocks_reject_wrong_task_heading(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n\n## Workflow\n\nKeep\n", encoding="utf-8")
    output = "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Workflow\n[CONTENT]\nChanged\n[SECTION_EDIT_END: README.md]"

    assert initialized_orchestrator.parse_and_write_files(output, ["README.md"], allowed_heading="## Install") == []
    assert "Keep" in readme.read_text(encoding="utf-8")


def test_file_level_task_rejects_section_blocks(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n", encoding="utf-8")
    output = "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Install\n[CONTENT]\nChanged\n[SECTION_EDIT_END: README.md]"

    assert initialized_orchestrator.parse_and_write_files(output, ["README.md"]) == []
    assert "Old" in readme.read_text(encoding="utf-8")


def test_single_markdown_response_is_wrapped_only_when_complete(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n", encoding="utf-8")
    output = "# Project\n\n## Install\n\nChanged\n"

    assert initialized_orchestrator.parse_and_write_files(output, ["README.md"]) == ["README.md"]
    assert "Changed" in readme.read_text(encoding="utf-8")


def test_markdown_section_blocks_reject_unchanged_content(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n", encoding="utf-8")
    output = "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Install\n[CONTENT]\nOld\n[SECTION_EDIT_END: README.md]"

    assert initialized_orchestrator.parse_and_write_files(output, ["README.md"], allowed_heading="## Install") == []
    assert "Old" in readme.read_text(encoding="utf-8")


def test_implementing_pauses_when_file_contract_writes_nothing(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "update README", "target_files": ["README.md"], "status": "pending", "rd_level": "junior", "qa_level": "junior"}]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="[FILE_START: README.md]\n# Replacement\n[FILE_END: README.md]"))
    (initialized_orchestrator.workspace / "README.md").write_text("# Existing\n\n## Keep\n", encoding="utf-8")

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["code_revisions"] == 0
    assert initialized_orchestrator.state["tasks"][0]["revisions"] == 1
    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"


def test_manager_invalid_json_falls_back_to_next_route(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.config["role_model_routes"]["manager"] = [
        ["grok", "grok-4.5"], ["codex", "gpt-5.6-luna"]
    ]
    grok = Mock(return_value="Here is the JSON you requested: not valid")
    codex = Mock(return_value='{"use_sales": false}')
    monkeypatch.setattr(initialized_orchestrator, "call_grok", grok)
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)

    result = initialized_orchestrator.call_agent(
        "manager", "prompt", response_validator=is_json_response
    )

    assert result == '{"use_sales": false}'
    assert initialized_orchestrator.state["failed_model_routes"] == []


def test_run_to_end_pauses_instead_of_raising_on_unhandled_error(initialized_orchestrator, monkeypatch):
    monkeypatch.setattr(initialized_orchestrator, "step", Mock(side_effect=RuntimeError("unexpected failure")))

    initialized_orchestrator.run_to_end()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "Orchestrator"


def test_manager_retries_luna_then_ollama_after_token_failure(initialized_orchestrator, monkeypatch):
    grok = Mock(side_effect=RuntimeError("maximum context length"))
    codex = Mock(side_effect=RuntimeError("maximum context length"))
    ollama = Mock(return_value="fallback")
    monkeypatch.setattr(initialized_orchestrator, "call_grok", grok)
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)
    monkeypatch.setattr(initialized_orchestrator, "call_agent_ollama_fallback", ollama)

    assert initialized_orchestrator.call_manager("prompt") == "fallback"
    grok.assert_called_once_with("prompt", None, role="manager", model="grok-4.5")
    codex.assert_called_once_with("prompt", None, role="manager", model="gpt-5.6-luna")
    ollama.assert_called_once_with("manager", "prompt", None, model="qwen3:8b")


def test_reviewer_retries_luna_then_ollama_after_token_failure(initialized_orchestrator, monkeypatch):
    grok = Mock(side_effect=RuntimeError("maximum context length"))
    codex = Mock(side_effect=RuntimeError("maximum context length"))
    ollama = Mock(return_value="fallback")
    monkeypatch.setattr(initialized_orchestrator, "call_grok", grok)
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)
    monkeypatch.setattr(initialized_orchestrator, "call_agent_ollama_fallback", ollama)

    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "fallback"
    grok.assert_called_once_with("prompt", None, role="reviewer", model="grok-4.5")
    codex.assert_called_once_with("prompt", None, role="reviewer", model="gpt-5.6-luna")
    ollama.assert_called_once_with("reviewer", "prompt", None, model="gemma4:latest")


def test_allocate_workers_persists_round_robin_assignments(initialized_orchestrator):
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 0, "middle": 0, "junior": 2},
        "qa": {"senior": 0, "middle": 0, "junior": 1},
    }
    tasks = [
        {"id": "T-1", "assignee_level": "junior"},
        {"id": "T-2", "assignee_level": "junior"},
        {"id": "T-3", "assignee_level": "junior"},
    ]

    workers, assignments = initialized_orchestrator.allocate_workers("rd", tasks)

    assert workers == [("rd-junior-1", "junior"), ("rd-junior-2", "junior")]
    assert assignments == {"T-1": "rd-junior-1", "T-2": "rd-junior-2", "T-3": "rd-junior-1"}


def test_token_fallback_promotes_manager_only_for_token_errors(initialized_orchestrator):
    assert initialized_orchestrator.token_fallback_model("manager", RuntimeError("maximum context length")) == "gpt-5.6-luna"
    assert initialized_orchestrator.get_active_model_for_role("manager", "codex") == "gpt-5.6-luna"
    assert initialized_orchestrator.token_fallback_model("reviewer", RuntimeError("connection failed")) is None


def test_qa_senior_uses_local_qwen(initialized_orchestrator, monkeypatch):
    ollama = Mock(return_value="qwen response")
    monkeypatch.setattr(initialized_orchestrator, "call_ollama", ollama)

    assert initialized_orchestrator.call_agent("qa_senior", "prompt") == "qwen response"
    ollama.assert_called_once_with("prompt", None, role="qa_senior", model="gemma4:latest")


def test_qa_ollama_falls_back_to_qwen(initialized_orchestrator, monkeypatch):
    ollama = Mock(side_effect=[RuntimeError("gemma unavailable"), "qwen fallback"])
    monkeypatch.setattr(initialized_orchestrator, "call_ollama", ollama)

    assert initialized_orchestrator.call_agent_ollama_fallback("qa_junior", "prompt") == "qwen fallback"
    assert ollama.call_args_list == [
        call("prompt", None, role="qa_junior"),
        call("prompt", None, role="qa_junior", model="qwen2.5-coder:7b"),
    ]


def test_quota_exhausted_backend_is_skipped_for_the_rest_of_the_day(initialized_orchestrator, monkeypatch):
    codex = Mock(side_effect=RuntimeError("429 quota exceeded"))
    agy = Mock(return_value="agy fallback")
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)
    monkeypatch.setattr(initialized_orchestrator, "call_agy", agy)
    initialized_orchestrator.config["role_model_routes"]["reviewer"] = [
        ["codex", "gpt-5.6-sol"], ["agy", "Gemini 3.5 Flash (Medium)"]
    ]

    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "agy fallback"
    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "agy fallback"

    codex.assert_called_once_with("prompt", None, role="reviewer", model="gpt-5.6-sol")
    assert agy.call_args_list == [call("prompt", None, role="reviewer", model="Gemini 3.5 Flash (Medium)")] * 2


def test_spending_limit_is_treated_as_quota_exhausted():
    assert quota_exhausted(RuntimeError("403 personal-team-blocked:spending-limit"))


def test_rd_and_qa_can_use_different_levels(initialized_orchestrator):
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"junior": 1},
    }
    tasks = [{"id": "T-1", "rd_level": "senior", "qa_level": "junior"}]

    _, rd_assignments = initialized_orchestrator.allocate_workers("rd", tasks)
    _, qa_assignments = initialized_orchestrator.allocate_workers("qa", tasks)

    assert rd_assignments == {"T-1": "rd-senior-1"}
    assert qa_assignments == {"T-1": "qa-junior-1"}


def test_developer_promotion_is_state_only(initialized_orchestrator):
    original_model = initialized_orchestrator.config["role_models"]["developer_junior"]
    initialized_orchestrator.state["last_developer_role"] = "developer_junior"
    initialized_orchestrator.state["last_qa_level"] = "junior"

    initialized_orchestrator.escalate_developer_backend()

    assert initialized_orchestrator.state["developer_promotions"]["developer_junior"] == "developer_middle"
    assert initialized_orchestrator.config["role_models"]["developer_junior"] == original_model
    assert initialized_orchestrator.fix_task_levels() == {"rd_level": "middle", "qa_level": "junior"}
    assert initialized_orchestrator.state["staffing"]["rd"]["middle"] == 1


def test_developer_promotion_is_isolated_to_active_task(initialized_orchestrator):
    initialized_orchestrator.state["active_task_id"] = "T-1"
    initialized_orchestrator.state["last_developer_role"] = "developer_junior"

    initialized_orchestrator.escalate_developer_backend()

    assert initialized_orchestrator.state["task_developer_promotions"]["T-1"]["developer_junior"] == "developer_middle"
    assert initialized_orchestrator.state["developer_promotions"] == {}
    assert initialized_orchestrator.state["task_developer_promotions"].get("T-2", {}) == {}


def test_developing_plan_saves_manager_staffing(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="plan"))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "T-1", "description": "implement", "target_files": ["app.py"], "status": "pending", "complexity": "routine", "assignee_level": "junior"}], "staffing": {"rd": {"senior": 1, "junior": 2}, "qa": {"senior": 1, "junior": 1}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["staffing"]["rd"] == {"senior": 1, "junior": 2}
    assert initialized_orchestrator.state["staffing"]["qa"] == {"senior": 1, "junior": 1}
    assert initialized_orchestrator.state["tasks"][0]["assignee_level"] == "junior"


def test_developing_plan_uses_manager_tasks_without_developer_planner(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("Add a setting", encoding="utf-8")
    developer_call = Mock()
    monkeypatch.setattr(initialized_orchestrator, "call_agent", developer_call)
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "T-1", "description": "add setting", "target_files": ["app.py"], "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    developer_call.assert_not_called()
    assert "`app.py`" in initialized_orchestrator.plan_path.read_text(encoding="utf-8")


def test_developing_plan_keeps_section_level_readme_tasks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("只允許修改 docs/guide.md", encoding="utf-8")
    initialized_orchestrator.request_path.write_text("只允許修改 docs/guide.md", encoding="utf-8")
    guide = initialized_orchestrator.workspace / "docs" / "guide.md"
    guide.parent.mkdir()
    guide.write_text("# Project\n\n## Quick Start\n\nOld\n", encoding="utf-8")
    manager = Mock(return_value='{"tasks": [{"id": "DOC-QUICK", "description": "update Quick Start", "target_files": ["docs/guide.md"], "section_heading": "## Quick Start", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}')
    monkeypatch.setattr(initialized_orchestrator, "call_manager", manager)

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["id"] == "DOC-QUICK"
    assert initialized_orchestrator.state["tasks"][0]["section_heading"] == "## Quick Start"
    assert "## Quick Start" in manager.call_args.args[0]


def test_developing_plan_uses_whole_file_task_for_new_markdown(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("只允許新增 docs/litellm-telemetry.md", encoding="utf-8")
    initialized_orchestrator.request_path.write_text("建立 docs/litellm-telemetry.md", encoding="utf-8")
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "DOC-NEW", "description": "write telemetry guide", "target_files": ["docs/litellm-telemetry.md"], "section_heading": "## Installation and enablement", "rd_level": "middle", "qa_level": "middle"}], "staffing": {"rd": {"middle": 1}, "qa": {"middle": 1}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    task = initialized_orchestrator.state["tasks"][0]
    assert "section_heading" not in task
    assert task["output_contract"]["format"] == "file_blocks"


def test_developing_plan_uses_whole_file_tasks_for_readme_language_swap(initialized_orchestrator, monkeypatch):
    request = "將 README.md 與 README_en.md 互換語言角色，並同步更新多國語系。"
    initialized_orchestrator.requirements_path.write_text(request, encoding="utf-8")
    initialized_orchestrator.request_path.write_text(request, encoding="utf-8")
    (initialized_orchestrator.workspace / "README.md").write_text("# Multi-Agent Orchestrator\n", encoding="utf-8")
    (initialized_orchestrator.workspace / "README_en.md").write_text("# Multi-Agent Orchestrator\n", encoding="utf-8")
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "README-TW", "description": "rewrite README.md", "target_files": ["README.md"], "section_heading": "# Multi-Agent Orchestrator", "rd_level": "senior", "qa_level": "middle"}, {"id": "README-EN", "description": "rewrite README_en.md", "target_files": ["README_en.md"], "section_heading": "# Multi-Agent Orchestrator", "rd_level": "senior", "qa_level": "middle"}], "staffing": {"rd": {"senior": 1}, "qa": {"middle": 1}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    assert len(initialized_orchestrator.state["tasks"]) == 2
    assert all("section_heading" not in task for task in initialized_orchestrator.state["tasks"])
    assert all(task["output_contract"]["format"] == "file_blocks" for task in initialized_orchestrator.state["tasks"])
    assert all(task["output_contract"]["allow_markdown_heading_changes"] for task in initialized_orchestrator.state["tasks"])


def test_developing_plan_covers_non_markdown_requested_file(initialized_orchestrator, monkeypatch):
    request = "將 README.md 與 README_en.md 完整內容互換，並更新 verify_alignment.py。只允許修改這三個檔案。"
    initialized_orchestrator.requirements_path.write_text(request, encoding="utf-8")
    initialized_orchestrator.request_path.write_text(request, encoding="utf-8")
    for name in ("README.md", "README_en.md"):
        (initialized_orchestrator.workspace / name).write_text("# Multi-Agent Orchestrator\n", encoding="utf-8")
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "README-TW", "description": "rewrite README.md", "target_files": ["README.md"], "rd_level": "senior", "qa_level": "middle"}, {"id": "README-EN", "description": "rewrite README_en.md", "target_files": ["README_en.md"], "rd_level": "senior", "qa_level": "middle"}, {"id": "ALIGNMENT", "description": "update verify_alignment.py", "target_files": ["verify_alignment.py"], "rd_level": "middle", "qa_level": "middle"}], "staffing": {"rd": {"senior": 1, "middle": 1}, "qa": {"middle": 1}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    assert {task["target_files"][0] for task in initialized_orchestrator.state["tasks"]} == {
        "README.md",
        "README_en.md",
        "verify_alignment.py",
    }
    assert "- `verify_alignment.py`" in initialized_orchestrator.plan_path.read_text(encoding="utf-8")


def test_developing_plan_normalizes_task_status_to_pending(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="plan"))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "T-1", "description": "implement", "target_files": ["app.py"], "status": "completed", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"


def test_developing_plan_does_not_treat_unrelated_change_scope_as_readme_only(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("Update README and config; 不得修改無關功能", encoding="utf-8")
    initialized_orchestrator.request_path.write_text("Update README.md and orchestrator/core/config.py", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="plan"))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "CONFIG-1", "description": "update config", "target_files": ["orchestrator/core/config.py"], "rd_level": "middle", "qa_level": "junior"}], "staffing": {"rd": {"middle": 1}, "qa": {"junior": 1}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["id"] == "CONFIG-1"


def test_developing_plan_pauses_when_manager_returns_only_planning_tasks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="plan"))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "T-1", "description": "Gather requirements", "rd_level": "senior", "qa_level": "junior"}]}'),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "Manager"


def test_developing_plan_reopens_changed_completed_task(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
            "id": "T-1", "description": "old scope", "target_files": ["app.py"], "status": "completed",
        "complexity": "routine", "rd_level": "junior", "qa_level": "junior",
    }]
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="plan"))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "T-1", "description": "new scope", "target_files": ["app.py"], "complexity": "routine", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"


def test_developing_plan_rejects_staffing_that_cannot_cover_tasks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="plan"))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(return_value='{"tasks": [{"id": "T-1", "description": "implement", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 0}, "qa": {"junior": 0}}}'),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "Manager"


def test_consult_specialists_only_calls_manager_selected_roles(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["specialists"] = [
        {"role": "security", "reason": "handles credentials"},
        {"role": "unknown", "reason": "must be ignored"},
    ]
    call_agent = Mock(return_value="review")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    notes = initialized_orchestrator.consult_specialists("requirements", "plan")

    assert "## Security" in notes
    assert call_agent.call_args.args[0] == "security"


def test_consult_specialists_skips_readme_only_tasks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["specialists"] = [{"role": "devops", "reason": "mentioned in docs"}]
    call_agent = Mock()
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    notes = initialized_orchestrator.consult_specialists(
        "Only modify README translations; must not modify source code.",
        "Update README_en.md and README_ja.md.",
    )

    assert notes == ""
    call_agent.assert_not_called()


def test_agent_context_is_compact_machine_data(initialized_orchestrator):
    initialized_orchestrator.request_path.write_text("Add a setting", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "add it", "status": "pending", "unused": "omit"}]

    initialized_orchestrator.write_agent_context()

    assert json.loads(initialized_orchestrator.agent_context_path.read_text(encoding="utf-8")) == {
        "request": "Add a setting", "contract": {"stage": "PLANNING", "allowed_actions": ["plan_or_review"], "output_contract": {"format": "stage_prompt", "allow_file_blocks": False}}, "tasks": [{"id": "T-1", "description": "add it", "status": "pending"}], "specialists": [],
    }


def test_assistant_meeting_memory_uses_handoff_sections(initialized_orchestrator, monkeypatch):
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="## Outcome\ndone"))

    memory = initialized_orchestrator.assistant.generate_meeting_memory("請修正", "完成", [], "PASSED", "APPROVED", "a.py")

    assert memory == "## Outcome\ndone"
    prompt = initialized_orchestrator.call_agent.call_args.args[1]
    assert "## Next-session Handoff" in prompt


def test_consult_specialists_dispatches_new_role_modules(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["specialists"] = [
        {"role": role, "reason": "needed"}
        for role in ("devops", "uiux", "uiux_visual_review", "fae", "integration")
    ]
    call_agent = Mock(return_value="review")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    notes = initialized_orchestrator.consult_specialists("requirements", "plan")

    expected_roles = {"devops", "uiux", "uiux_visual_review", "fae", "integration"}
    assert {call.args[0] for call in call_agent.call_args_list} == expected_roles
    assert all(f"## {role.title()}" in notes for role in expected_roles)


def test_visual_review_receives_start_image_paths(initialized_orchestrator, monkeypatch, tmp_path):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"image")
    initialized_orchestrator.state["specialists"] = [{"role": "uiux_visual_review", "reason": "mockup"}]
    initialized_orchestrator.state["visual_image_paths"] = [str(screenshot)]
    review = Mock(return_value="review")
    monkeypatch.setattr(initialized_orchestrator.uiux_visual_review, "review", review)

    initialized_orchestrator.consult_specialists("requirements", "plan")

    assert f"[IMAGE: {screenshot}]" in review.call_args.args[2]


def test_visual_images_force_visual_review_specialist(initialized_orchestrator):
    initialized_orchestrator.state["visual_image_paths"] = ["/tmp/screen.png"]

    initialized_orchestrator.ensure_visual_review_specialist()
    initialized_orchestrator.ensure_visual_review_specialist()

    assert initialized_orchestrator.state["specialists"] == [{"role": "uiux_visual_review", "reason": "Visual inputs provided"}]


@pytest.mark.parametrize(
    ("state", "method_name"),
    [
        ("PLANNING", "step_planning"),
        ("DEVELOPING_PLAN", "step_developing_plan"),
        ("REVIEWING_PLAN", "step_reviewing_plan"),
        ("IMPLEMENTING", "step_implementing"),
        ("TESTING", "step_testing"),
        ("REVIEWING_CODE", "step_reviewing_code"),
        ("COMPLETED", "step_completed"),
    ],
)
def test_step_dispatches_known_states(initialized_orchestrator, monkeypatch, state, method_name):
    initialized_orchestrator.state["state"] = state
    initialized_orchestrator.save_state()
    called = Mock()
    monkeypatch.setattr(initialized_orchestrator, method_name, called)

    initialized_orchestrator.step()

    called.assert_called_once_with()


def test_step_reviewing_plan_approved_moves_to_implementing(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value='{"status":"APPROVED","feedback":["ok"]}'))

    initialized_orchestrator.step_reviewing_plan()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert json.loads(initialized_orchestrator.reviewer_output_json_path.read_text())["status"] == "APPROVED"
    assert initialized_orchestrator.reviewer_output_path.read_text().startswith("PLAN_STATUS: APPROVED")


def test_step_reviewing_plan_rejected_revises_until_max(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value='{"status":"REJECTED","feedback":["fix it"]}'))
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_reviewing_plan()

    assert initialized_orchestrator.state["plan_revisions"] == 1
    assert initialized_orchestrator.state["state"] == "DEVELOPING_PLAN"

    initialized_orchestrator.state["plan_revisions"] = initialized_orchestrator.config["max_revisions"]
    initialized_orchestrator.step_reviewing_plan()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["pass_state"] == "IMPLEMENTING"


def test_step_testing_passed_moves_to_reviewing_code(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "implement", "status": "completed", "rd_level": "senior", "qa_level": "senior"}]
    monkeypatch.setattr(initialized_orchestrator, "run_command", Mock(return_value=(0, "tests ok")))
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="QA_STATUS: PASSED\nok"))

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "REVIEWING_CODE"


def test_step_testing_failed_command_cannot_pass_on_qa_response(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "implement", "status": "completed", "rd_level": "senior", "qa_level": "senior"}]
    monkeypatch.setattr(initialized_orchestrator, "run_command", Mock(return_value=(1, "tests failed")))
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="PASSED\nok"))

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert "Test exit code: 1" in initialized_orchestrator.state["tasks"][-1]["description"]
    assert "tests failed" in initialized_orchestrator.state["tasks"][-1]["description"]


def test_step_testing_failed_revises_until_max(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
        "id": "T-1", "description": "change app", "target_files": ["app.py"],
        "status": "completed", "rd_level": "junior", "qa_level": "junior",
    }]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    monkeypatch.setattr(initialized_orchestrator, "run_command", Mock(return_value=(1, "tests failed")))
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="FAILED\nfix it"))
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["code_revisions"] == 1
    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["tasks"][-1]["id"] == "FIX-QA-1"
    assert initialized_orchestrator.state["tasks"][-1]["rd_level"] == "senior"
    assert initialized_orchestrator.state["tasks"][-1]["qa_level"] == "junior"
    assert initialized_orchestrator.state["tasks"][-1]["target_files"] == ["app.py"]

    initialized_orchestrator.state["code_revisions"] = initialized_orchestrator.config["max_revisions"]
    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"


def test_step_reviewing_code_approved_moves_to_completed(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text("tests", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value='{"status":"APPROVED","feedback":["ok"]}'))

    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["state"] == "COMPLETED"
    assert json.loads(initialized_orchestrator.reviewer_output_json_path.read_text())["status"] == "APPROVED"
    assert initialized_orchestrator.reviewer_output_path.read_text().startswith("APPROVED")


def test_reviewer_receives_untracked_file_status(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text("tests passed", encoding="utf-8")
    initialized_orchestrator.has_git = True
    monkeypatch.setattr(initialized_orchestrator, "run_command", Mock(side_effect=[(0, "diff"), (0, "stdout:\n?? docs/new.md\nstderr:\n"), (1, "new file contents")]))
    call_agent = Mock(return_value='{"status":"APPROVED","feedback":[]}')
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_reviewing_code()

    assert "Git Status (includes untracked files):\nstdout:\n?? docs/new.md" in call_agent.call_args.args[1]
    assert "Untracked File Diffs:\nnew file contents" in call_agent.call_args.args[1]


def test_step_reviewing_code_rejected_revises_until_max(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text("tests", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
        "id": "T-1", "description": "change app", "target_files": ["app.py"],
        "status": "completed", "rd_level": "junior", "qa_level": "junior",
    }]
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value='{"status":"REJECTED","feedback":["fix it"]}'))
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["code_revisions"] == 1
    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["tasks"][-1]["id"] == "FIX-REV-1"
    assert initialized_orchestrator.state["tasks"][-1]["rd_level"] == "senior"
    assert initialized_orchestrator.state["tasks"][-1]["qa_level"] == "senior"
    assert initialized_orchestrator.state["tasks"][-1]["target_files"] == ["app.py"]

    initialized_orchestrator.state["code_revisions"] = initialized_orchestrator.config["max_revisions"]
    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"


def test_setup_worktree_use_worktree_false_returns_without_action(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.has_git = True
    cleanup = Mock()
    run = Mock()
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", cleanup)
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.setup_worktree()

    cleanup.assert_not_called()
    run.assert_not_called()


def test_setup_worktree_no_git_returns_without_action(initialized_orchestrator, monkeypatch):
    cleanup = Mock()
    run = Mock()
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", cleanup)
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.setup_worktree()

    cleanup.assert_not_called()
    run.assert_not_called()


def test_setup_worktree_cleans_up_before_git_worktree_add(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    events = []
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", lambda: events.append("cleanup"))
    monkeypatch.setattr(
        initialized_orchestrator,
        "run_command",
        lambda cmd, cwd=None, **kwargs: events.append(("run", cmd, cwd)) or (0, "ok"),
    )

    initialized_orchestrator.setup_worktree()

    assert events == [
        "cleanup",
        (
            "run",
            ["git", "worktree", "add", "-b", "ai-feature-branch", str(worktree)],
            initialized_orchestrator.workspace,
        ),
    ]


def test_setup_worktree_failing_add_raises_runtime_error(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.has_git = True
    cleanup = Mock()
    run = Mock(return_value=(1, "nope"))
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", cleanup)
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    with pytest.raises(RuntimeError, match="Git worktree setup failed"):
        initialized_orchestrator.setup_worktree()

    cleanup.assert_called_once_with()


def test_cleanup_worktree_use_worktree_false_returns_without_action(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.has_git = True
    run = Mock()
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree()

    run.assert_not_called()


def test_cleanup_worktree_no_git_returns_without_action(initialized_orchestrator, monkeypatch):
    run = Mock()
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree()

    run.assert_not_called()


def test_cleanup_worktree_merge_false_removes_listed_worktree_and_deletes_branch(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    run = Mock(return_value=(0, f"{worktree} ai-feature-branch\n"))
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree(merge=False)

    assert run.call_args_list == [
        call(["git", "worktree", "list"], capture=True),
        call(["git", "worktree", "remove", "--force", str(worktree)], cwd=initialized_orchestrator.workspace),
        call(["git", "branch", "-D", "ai-feature-branch"], cwd=initialized_orchestrator.workspace),
    ]


def test_cleanup_worktree_skips_remove_when_worktree_list_has_no_worktree(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.has_git = True
    run = Mock(return_value=(0, "/repo master\n"))
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree()

    assert run.call_args_list == [
        call(["git", "worktree", "list"], capture=True),
        call(["git", "branch", "-D", "ai-feature-branch"], cwd=initialized_orchestrator.workspace),
    ]


def test_cleanup_worktree_merge_success_runs_merge_then_cleanup(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    run = Mock(side_effect=[
        (0, ""),
        (0, ""),
        (0, ""),
        (0, "merged"),
        (0, f"{worktree} ai-feature-branch\n"),
        (0, ""),
        (0, ""),
    ])
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree(merge=True)

    assert run.call_args_list == [
        call(["git", "show-ref", "--verify", "--quiet", "refs/heads/ai-feature-branch"], cwd=initialized_orchestrator.workspace),
        call(["git", "add", "."], cwd=worktree),
        call(["git", "commit", "-m", "AI Auto-commit before merge"], cwd=worktree),
        call(["git", "merge", "ai-feature-branch"], cwd=initialized_orchestrator.workspace),
        call(["git", "worktree", "list"], capture=True),
        call(["git", "worktree", "remove", "--force", str(worktree)], cwd=initialized_orchestrator.workspace),
        call(["git", "branch", "-D", "ai-feature-branch"], cwd=initialized_orchestrator.workspace),
    ]


def test_cleanup_worktree_merge_failure_stops_before_cleanup(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    run = Mock(side_effect=[
        (0, ""),
        (0, ""),
        (0, ""),
        (1, "conflict"),
        (0, ""),
        (0, ""),
    ])
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree(merge=True)

    assert run.call_args_list == [
        call(["git", "show-ref", "--verify", "--quiet", "refs/heads/ai-feature-branch"], cwd=initialized_orchestrator.workspace),
        call(["git", "add", "."], cwd=worktree),
        call(["git", "commit", "-m", "AI Auto-commit before merge"], cwd=worktree),
        call(["git", "merge", "ai-feature-branch"], cwd=initialized_orchestrator.workspace),
        call(["git", "diff", "--name-only", "--diff-filter=U"], cwd=initialized_orchestrator.workspace),
        call(["git", "merge", "--abort"], cwd=initialized_orchestrator.workspace),
    ]


def test_cleanup_worktree_merge_failure_aborts_and_records_conflict(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    merge_output = "stdout:\nCONFLICT (add/add): Merge conflict in docs/litellm-telemetry.md\nstderr:\n"
    run = Mock(side_effect=[
        (0, ""),
        (0, ""),
        (0, ""),
        (1, merge_output),
        (0, "stdout:\ndocs/litellm-telemetry.md\nstderr:\n"),
        (0, ""),
    ])
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    assert initialized_orchestrator.cleanup_worktree(merge=True) is False

    assert initialized_orchestrator.state["worktree_conflict_files"] == ["docs/litellm-telemetry.md"]
    assert initialized_orchestrator.state["worktree_merge_error"] == merge_output
    assert run.call_args_list[-1] == call(["git", "merge", "--abort"], cwd=initialized_orchestrator.workspace)


def test_manager_queues_rd_task_after_worktree_merge_failure(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.request_path.write_text("request", encoding="utf-8")
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.has_git = False
    initialized_orchestrator.state["worktree_conflict_files"] = ["docs/litellm-telemetry.md"]
    initialized_orchestrator.state["worktree_merge_error"] = "merge conflict"
    monkeypatch.setattr(initialized_orchestrator.manager, "call_manager", Mock(return_value="summary"))
    monkeypatch.setattr(initialized_orchestrator.assistant, "generate_meeting_memory", Mock(return_value="memory"))
    monkeypatch.setattr(initialized_orchestrator.assistant, "generate_changelog", Mock(return_value=""))
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", Mock(return_value=False))

    initialized_orchestrator.step_completed()

    task = initialized_orchestrator.state["tasks"][-1]
    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert task["id"] == "FIX-MERGE-1"
    assert task["status"] == "pending"
    assert task["target_files"] == ["docs/litellm-telemetry.md"]
    assert "merge conflict" in task["description"]
