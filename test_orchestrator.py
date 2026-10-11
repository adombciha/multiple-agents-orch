from __future__ import annotations

import copy
import json
import re
import sys
import types
from pathlib import Path
from unittest.mock import Mock, call

import pytest

import orchestrator
import orchestrator.main as orchestrator_main
from orchestrator import DEFAULT_CONFIG, AgentOrchestrator
from orchestrator.core import backends, gpt, scoring, roster, brain
from orchestrator.core.telemetry import record_call, summary_markdown
from orchestrator.core.backends import quota_exhausted
from orchestrator.roles.base_agent import is_json_response
from orchestrator.roles.developer import is_task_plan_response
from orchestrator.roles.manager import (
    is_requirements_json_response,
    render_requirements,
)


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
        "staffing": {
            "rd": {"senior": 1, "middle": 0, "junior": 0},
            "qa": {"senior": 1, "middle": 0, "junior": 0},
        },
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


def test_initialized_orchestrator_fixture_loads_temp_config_and_state(
    initialized_orchestrator,
):
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

    record = json.loads(
        (initialized_orchestrator.ai_dir / "llm_usage.jsonl")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert record["input_tokens"] == 7
    assert record["output_tokens"] == 7
    assert record["total_tokens"] == 14
    assert record["token_status"] == "exact"


def test_usage_summary_markdown_groups_calls_by_model(
    initialized_orchestrator, monkeypatch
):
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.token_counter = Mock(return_value=3)
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    record_call(
        initialized_orchestrator,
        role="manager",
        backend="codex",
        model="gpt-6.1-sol",
        prompt="prompt",
        system_prompt=None,
        output="output",
        elapsed_seconds=0.1,
    )

    summary = summary_markdown(initialized_orchestrator)
    assert "## LiteLLM Token Usage" in summary
    assert "`gpt-6.1-sol`" in summary
    assert "| 6 |" in summary


def test_call_with_usage_records_success_and_failure(initialized_orchestrator):
    assert (
        initialized_orchestrator._call_with_usage(
            "manager",
            "codex",
            "gpt-6.1-sol",
            "prompt",
            None,
            lambda: "response",
        )
        == "response"
    )

    with pytest.raises(RuntimeError, match="failed"):
        initialized_orchestrator._call_with_usage(
            "manager",
            "codex",
            "gpt-6.1-sol",
            "prompt",
            None,
            lambda: (_ for _ in ()).throw(RuntimeError("failed")),
        )

    records = [
        json.loads(line)
        for line in (initialized_orchestrator.ai_dir / "llm_usage.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
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


def test_agy_auto_model_uses_latest_available_family_and_keeps_explicit(monkeypatch):
    monkeypatch.setattr(backends, "_agy_models_cache", (0.0, ()))
    run = Mock(return_value=Mock(
        returncode=0,
        stdout=(
            "gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)\n"
            "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
            "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
            "gemini-3.1-pro-low\tGemini 3.1 Pro (Low)\n"
            "claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)\n"
        ),
    ))
    monkeypatch.setattr(backends.subprocess, "run", run)

    assert backends.resolve_agy_model("auto:gemini-flash-medium") == "gemini-3.8-flash-medium"
    assert backends.resolve_agy_model("auto:gemini-flash-low") == "gemini-3.8-flash-low"
    assert backends.resolve_agy_model("auto:gemini-pro-low") == "gemini-3.1-pro-low"
    assert backends.resolve_agy_model("auto:claude-sonnet-medium") == "claude-sonnet-5-5-medium"
    assert backends.resolve_agy_model("gemini-3.8-flash-low") == "gemini-3.8-flash-low"
    run.assert_called_once()


def test_legacy_config_migrates_removed_backends_but_keeps_manual_agy_model(tmp_path, no_git):
    config, _ = write_ai_company(tmp_path)
    config["backends"]["ra"] = "grok"
    config["role_model_backends"]["ra"] = "grok"
    config["role_models"]["ra"] = "grok-4.5"
    config["role_model_routes"]["ra"] = [["grok", "grok-4.5"], ["ollama", "qwen3:8b"]]
    config["role_models"]["sales"] = "claude-sonnet-5-5-medium"
    (tmp_path / ".ai-company" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    app = AgentOrchestrator(tmp_path)
    app.load_config_and_state()

    assert app.config["backends"]["ra"] == "agy"
    assert app.config["role_model_routes"]["ra"][0] == ["agy", "auto:gemini-flash-medium"]
    assert app.config["role_models"]["sales"] == "claude-sonnet-5-5-medium"
    assert "grok" not in (tmp_path / ".ai-company" / "config.json").read_text(encoding="utf-8")


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
    expected_state.setdefault("task_backend_overrides", {})
    assert app.state == expected_state


def test_load_config_and_state_missing_config_keeps_default_config(tmp_path, no_git):
    ai_dir = tmp_path / ".ai-company"
    ai_dir.mkdir()
    (ai_dir / "state.json").write_text(
        json.dumps({"state": "PLANNING"}), encoding="utf-8"
    )
    app = AgentOrchestrator(tmp_path)

    with pytest.raises(FileNotFoundError):
        app.load_config_and_state()

    assert app.config == DEFAULT_CONFIG


def test_constructor_detects_git_and_current_branch(tmp_path, monkeypatch):
    run = Mock(
        side_effect=[
            Mock(returncode=0, stdout="true\n"),
            Mock(returncode=0, stdout="main\n"),
        ]
    )
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
    initialized_orchestrator.config_path.write_text(
        json.dumps(initialized_orchestrator.config), encoding="utf-8"
    )
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

    assert (
        json.loads(initialized_orchestrator.state_path.read_text(encoding="utf-8"))
        == initialized_orchestrator.state
    )


def test_save_state_interrupted_write_preserves_previous_state(
    initialized_orchestrator, monkeypatch
):
    original = initialized_orchestrator.state_path.read_bytes()
    original_files = set(initialized_orchestrator.ai_dir.iterdir())

    def interrupted_dump(_state, stream, indent):
        stream.write("{")
        raise OSError("interrupted")

    monkeypatch.setattr("orchestrator.core.state.json.dump", interrupted_dump)
    initialized_orchestrator.state["state"] = "TESTING"

    with pytest.raises(OSError, match="interrupted"):
        initialized_orchestrator.save_state()

    assert initialized_orchestrator.state_path.read_bytes() == original
    assert set(initialized_orchestrator.ai_dir.iterdir()) == original_files


def test_init_rejects_corrupt_state_without_clearing_artifacts(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state_path.write_text("{", encoding="utf-8")
    initialized_orchestrator.request_path.write_text("keep request", encoding="utf-8")
    clear = Mock()
    monkeypatch.setattr(initialized_orchestrator, "clear_run_artifacts", clear)

    with pytest.raises(RuntimeError, match="artifacts were preserved"):
        initialized_orchestrator.init_project()

    clear.assert_not_called()
    assert initialized_orchestrator.request_path.read_text(encoding="utf-8") == "keep request"
    assert initialized_orchestrator.state_path.read_text(encoding="utf-8") == "{"


def test_run_command_success_returns_stdout_and_stderr(
    initialized_orchestrator, monkeypatch
):
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


def test_run_command_explicit_cwd_is_passed_through(
    initialized_orchestrator, tmp_path, monkeypatch
):
    cwd = tmp_path / "elsewhere"
    run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(orchestrator.subprocess, "run", run)

    initialized_orchestrator.run_command(["pwd"], cwd=cwd)

    assert run.call_args.kwargs["cwd"] == cwd


@pytest.mark.parametrize("state", ["IMPLEMENTING", "TESTING", "REVIEWING_CODE"])
def test_run_command_uses_worktree_cwd_for_agent_states(
    initialized_orchestrator, state, monkeypatch
):
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

    backends.call_agy(
        initialized_orchestrator, "prompt", role="developer_junior", model="test-model"
    )

    assert run.call_args.kwargs["cwd"] == worktree


def test_codex_exec_is_read_only_and_uses_file_block_output(
    initialized_orchestrator, monkeypatch
):
    run = Mock(
        return_value=Mock(
            returncode=0,
            stdout="[FILE_START: app.py]\nok\n[FILE_END: app.py]",
            stderr="",
        )
    )
    monkeypatch.setattr(backends.subprocess, "run", run)

    result = backends.call_codex(initialized_orchestrator, "prompt", model="auto")

    assert result.startswith("[FILE_START: app.py]")
    assert run.call_args.args[0][:5] == [
        "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"
    ]
    assert "-m" not in run.call_args.args[0]


def test_agy_maps_legacy_model_ids_to_supported_gemini_names(
    initialized_orchestrator, monkeypatch
):
    run = Mock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(backends.subprocess, "run", run)

    backends.call_agy(initialized_orchestrator, "prompt", model="gemini-3.1-pro")

    assert run.call_args.args[0][0:6] == [
        "agy", "--sandbox", "--mode", "plan", "--model", "Gemini 3.1 Pro (Low)"
    ]


def test_run_command_use_worktree_false_runs_in_workspace(
    initialized_orchestrator, monkeypatch
):
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


def test_run_command_subprocess_exception_returns_error_tuple(
    initialized_orchestrator, monkeypatch
):
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        Mock(side_effect=OSError("boom")),
    )

    returncode, output = initialized_orchestrator.run_command(["bad"])

    assert returncode == -1
    assert "Error running command: boom" in output


def test_call_ollama_posts_chat_payload_and_returns_message_content(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["model_tier_indices"]["reviewer"] = 1
    response = Mock()
    response.json.return_value = {"message": {"content": "agent answer"}}
    post = Mock(return_value=response)
    monkeypatch.setattr(orchestrator.requests, "post", post)

    result = initialized_orchestrator.call_ollama(
        "do it", system_prompt="be brief", role="reviewer"
    )

    assert result == "agent answer"
    post.assert_called_once_with(
        f"{initialized_orchestrator.config['ollama_url']}/api/chat",
        json={
            "model": "qwen3.8:latest",
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


def test_call_ollama_retries_without_thinking_when_model_rejects_it(
    initialized_orchestrator, monkeypatch
):
    rejected = Mock(status_code=400)
    accepted = Mock(status_code=200)
    accepted.json.return_value = {"message": {"content": "answer"}}
    post = Mock(side_effect=[rejected, accepted])
    monkeypatch.setattr(orchestrator.requests, "post", post)

    assert (
        initialized_orchestrator.call_ollama("do it", model="plain-model") == "answer"
    )
    assert post.call_args_list[0].kwargs["json"]["think"] == "high"
    assert "think" not in post.call_args_list[1].kwargs["json"]
    assert "plain-model" in initialized_orchestrator._ollama_no_think_models


def test_gpt_oss_developer_uses_json_files_and_returns_file_blocks(
    initialized_orchestrator, monkeypatch
):
    response = Mock(status_code=200)
    response.json.return_value = {
        "message": {
            "content": json.dumps(
                {"files": [{"path": "app.py", "content": "print('ok')"}]}
            )
        }
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(gpt.requests, "post", post)

    result = backends.call_ollama(
        initialized_orchestrator,
        "write app",
        role="developer_senior",
        model="gpt-oss:20b",
    )

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
    accepted.json.return_value = {
        "message": {
            "content": json.dumps({"files": [{"path": "app.py", "content": "ok"}]})
        }
    }
    post = Mock(side_effect=[rejected, accepted])
    monkeypatch.setattr(gpt.requests, "post", post)

    backends.call_ollama(
        initialized_orchestrator,
        "write app",
        role="developer_senior",
        model="gpt-oss:20b",
    )

    assert "think" in post.call_args_list[0].kwargs["json"]
    assert "think" not in post.call_args_list[1].kwargs["json"]
    assert "format" in post.call_args_list[1].kwargs["json"]


def test_gpt_oss_section_mode_returns_section_blocks(
    initialized_orchestrator, monkeypatch
):
    response = Mock(status_code=200)
    response.json.return_value = {
        "message": {
            "content": json.dumps(
                {"sections": [{"path": "README.md", "content": "Updated"}]}
            )
        }
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(gpt.requests, "post", post)

    result = backends.call_ollama(
        initialized_orchestrator,
        "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Install",
        role="developer_senior",
        model="gpt-oss:20b",
    )

    assert (
        result
        == "[SECTION_EDIT_START: README.md]\n[HEADING]\n\n[CONTENT]\nUpdated\n[SECTION_EDIT_END: README.md]"
    )
    assert post.call_args.kwargs["json"]["format"] == gpt.SECTION_RESPONSE_SCHEMA


def test_gpt_oss_rejects_path_outside_task_contract(
    initialized_orchestrator, monkeypatch
):
    response = Mock(status_code=200)
    response.json.return_value = {
        "message": {
            "content": json.dumps(
                {"sections": [{"path": "README_EN.md", "content": "wrong"}]}
            )
        }
    }
    monkeypatch.setattr(gpt.requests, "post", Mock(return_value=response))

    with pytest.raises(RuntimeError, match="outside task contract"):
        backends.call_ollama(
            initialized_orchestrator,
            'Machine contract: {"target_files":["README_en.md"]}\n[SECTION_EDIT_START: README_en.md]',
            role="developer_senior",
            model="gpt-oss:20b",
        )


def test_gpt_oss_retries_truncated_json_with_larger_output_limit(
    initialized_orchestrator, monkeypatch
):
    truncated = Mock(status_code=200)
    truncated.json.return_value = {
        "message": {
            "content": '{"sections":[{"path":"README_ja.md","content":"unterminated'
        }
    }
    complete = Mock(status_code=200)
    complete.json.return_value = {
        "message": {
            "content": json.dumps(
                {"sections": [{"path": "README_ja.md", "content": "fixed"}]}
            )
        }
    }
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


def test_call_ollama_falls_back_to_configured_model(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["ollama_model"] = "fallback-model"
    monkeypatch.setattr(
        initialized_orchestrator, "get_active_model_for_role", Mock(return_value=None)
    )
    response = Mock()
    response.json.return_value = {"message": {"content": "fallback answer"}}
    post = Mock(return_value=response)
    monkeypatch.setattr(orchestrator.requests, "post", post)

    result = initialized_orchestrator.call_ollama("do it")

    assert result == "fallback answer"
    assert post.call_args.kwargs["json"]["model"] == "fallback-model"


def test_configure_ollama_uses_detected_windows_endpoint(
    initialized_orchestrator, monkeypatch
):
    monkeypatch.setattr(
        orchestrator_main, "ollama_available", Mock(side_effect=[False, True])
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "get_windows_host_ip",
        Mock(return_value="172.17.144.1"),
    )
    monkeypatch.setattr(orchestrator_main.sys.stdin, "isatty", Mock(return_value=False))

    orchestrator_main.configure_ollama(initialized_orchestrator)

    assert initialized_orchestrator.config["ollama_url"] == "http://172.17.144.1:11434"


def test_review_revise_reset_revisions_clears_counter_before_incrementing(
    tmp_path, no_git, monkeypatch
):
    # A prior run already hit max_revisions (code_revisions == 2): without
    # --reset-revisions, a plain `revise` would push it to 3, past max_rev,
    # so the very next automatic reject immediately re-pauses with no real
    # retry. --reset-revisions clears it first so the auto-revise loop gets
    # a genuine fresh budget, without touching the worktree or tasks the
    # way `reset` would.
    write_ai_company(
        tmp_path,
        state_overrides={
            "state": "WAITING_FOR_OWNER",
            "code_revisions": 2,
            "human_review_source": "Reviewer",
            "human_review_details": "Fix the two flagged bugs.",
            "pass_state": "COMPLETED",
            "resume_state": "IMPLEMENTING",
            "tasks": [{"id": "T-1", "target_files": ["app.py"], "status": "completed"}],
        },
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["orchestrator.py", "review", "revise", "--reset-revisions"]
    )

    orchestrator_main.main()

    state = json.loads((tmp_path / ".ai-company" / "state.json").read_text())
    assert state["state"] == "IMPLEMENTING"
    assert state["code_revisions"] == 1
    human_review_task = next(t for t in state["tasks"] if t["id"] == "HUMAN-REVIEW-1")
    assert human_review_task["target_files"] == ["app.py"]
    assert any(t["id"] == "HUMAN-REVIEW-1" for t in state["tasks"])


def test_review_revise_without_reset_revisions_keeps_incrementing(
    tmp_path, no_git, monkeypatch
):
    write_ai_company(
        tmp_path,
        state_overrides={
            "state": "WAITING_FOR_OWNER",
            "code_revisions": 2,
            "human_review_source": "Reviewer",
            "human_review_details": "Fix the two flagged bugs.",
            "pass_state": "COMPLETED",
            "resume_state": "IMPLEMENTING",
        },
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "review", "revise"])

    orchestrator_main.main()

    state = json.loads((tmp_path / ".ai-company" / "state.json").read_text())
    assert state["code_revisions"] == 3


def test_reset_clears_saved_request_prompt(tmp_path, no_git, monkeypatch):
    write_ai_company(tmp_path)
    (tmp_path / ".ai-company" / "request.md").write_text(
        "Build the thing.", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "reset"])

    orchestrator_main.main()

    assert not (tmp_path / ".ai-company" / "request.md").exists()


def test_reject_keeps_request_prompt_and_resets_to_planning(
    tmp_path, no_git, monkeypatch
):
    write_ai_company(
        tmp_path,
        state_overrides={
            "state": "IMPLEMENTING",
            "code_revisions": 2,
            "tasks": [{"id": "T-1", "status": "completed"}],
        },
    )
    (tmp_path / ".ai-company" / "request.md").write_text(
        "Build the thing.", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "reject"])

    orchestrator_main.main()

    assert (tmp_path / ".ai-company" / "request.md").read_text(
        encoding="utf-8"
    ) == "Build the thing."
    state = json.loads((tmp_path / ".ai-company" / "state.json").read_text())
    assert state["state"] == "PLANNING"
    assert state["code_revisions"] == 0
    assert state["tasks"] == []


def test_continue_approves_when_paused_by_developer(tmp_path, no_git, monkeypatch):
    write_ai_company(
        tmp_path,
        state_overrides={
            "state": "WAITING_FOR_OWNER",
            "human_review_source": "Developer",
            "pass_state": "IMPLEMENTING",
            "resume_state": "IMPLEMENTING",
        },
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "continue"])
    run_to_end = Mock()
    monkeypatch.setattr(AgentOrchestrator, "run_to_end", run_to_end)

    orchestrator_main.main()

    state = json.loads((tmp_path / ".ai-company" / "state.json").read_text())
    assert state["state"] == "IMPLEMENTING"
    assert "human_review_source" not in state
    run_to_end.assert_called_once()


def test_continue_revises_and_resets_ceiling_when_paused_by_reviewer(
    tmp_path, no_git, monkeypatch
):
    write_ai_company(
        tmp_path,
        state_overrides={
            "state": "WAITING_FOR_OWNER",
            "human_review_source": "Reviewer",
            "human_review_details": "Fix the bugs.",
            "code_revisions": 2,
            "pass_state": "COMPLETED",
            "resume_state": "IMPLEMENTING",
            "tasks": [{"id": "T-1", "target_files": ["app.py"], "status": "completed"}],
        },
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "continue"])
    run_to_end = Mock()
    monkeypatch.setattr(AgentOrchestrator, "run_to_end", run_to_end)

    orchestrator_main.main()

    state = json.loads((tmp_path / ".ai-company" / "state.json").read_text())
    assert state["state"] == "IMPLEMENTING"
    assert state["code_revisions"] == 1
    run_to_end.assert_called_once()


def test_continue_revises_without_reset_when_budget_remains(
    tmp_path, no_git, monkeypatch
):
    write_ai_company(
        tmp_path,
        state_overrides={
            "state": "WAITING_FOR_OWNER",
            "human_review_source": "QA",
            "human_review_details": "Fix the bugs.",
            "code_revisions": 0,
            "pass_state": "COMPLETED",
            "resume_state": "IMPLEMENTING",
        },
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "continue"])
    monkeypatch.setattr(AgentOrchestrator, "run_to_end", Mock())

    orchestrator_main.main()

    state = json.loads((tmp_path / ".ai-company" / "state.json").read_text())
    assert state["code_revisions"] == 1


def test_continue_runs_directly_when_not_paused(tmp_path, no_git, monkeypatch):
    write_ai_company(tmp_path, state_overrides={"state": "IMPLEMENTING"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "continue"])
    run_to_end = Mock()
    monkeypatch.setattr(AgentOrchestrator, "run_to_end", run_to_end)

    orchestrator_main.main()

    run_to_end.assert_called_once()


def test_continue_noop_when_already_completed(tmp_path, no_git, monkeypatch):
    write_ai_company(tmp_path, state_overrides={"state": "COMPLETED"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "continue"])
    run_to_end = Mock()
    monkeypatch.setattr(AgentOrchestrator, "run_to_end", run_to_end)

    orchestrator_main.main()

    run_to_end.assert_not_called()


def test_continue_reports_and_does_not_run_when_failed(tmp_path, no_git, monkeypatch):
    write_ai_company(tmp_path, state_overrides={"state": "FAILED"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "continue"])
    run_to_end = Mock()
    monkeypatch.setattr(AgentOrchestrator, "run_to_end", run_to_end)

    orchestrator_main.main()

    run_to_end.assert_not_called()


def test_inline_referenced_markdown_appends_existing_file_content(tmp_path):
    workspace = tmp_path
    (workspace / "docs").mkdir()
    plan_path = workspace / "docs" / "plan.md"
    plan_path.write_text("# The Plan\n\nDo the thing.", encoding="utf-8")

    result = orchestrator_main.inline_referenced_markdown(
        "Implement docs/plan.md exactly as written.", workspace
    )

    assert "'plan' (see reference material below)" in result
    assert "# The Plan" in result
    assert "Do the thing." in result


def test_inline_referenced_markdown_masks_path_mention_outside_reference_block(
    tmp_path,
):
    # The path mention in the human-written prompt text must not survive as
    # a bare "path.md" substring anywhere outside the reference block --
    # otherwise DEVELOPING_PLAN's markdown-file-to-edit detection (which
    # scans the whole saved request, not just the appended block) still
    # catches it, exactly like the appended block itself is designed to
    # avoid.
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("content", encoding="utf-8")

    result = orchestrator_main.inline_referenced_markdown(
        "Follow plan.md exactly.", tmp_path
    )

    prefix = result.split(orchestrator_main.REFERENCE_BLOCK_START)[0]
    assert not re.search(r"[A-Za-z0-9_.\-/]+\.md(?![A-Za-z0-9_])", prefix)


def test_inline_referenced_markdown_preserves_explicit_target_files(tmp_path):
    (tmp_path / "README_en.md").write_text("# Translation\n", encoding="utf-8")
    (tmp_path / "spec.md").write_text("# Context\n", encoding="utf-8")

    result = orchestrator_main.inline_referenced_markdown(
        "Target files: README_en.md\nFollow spec.md for context.", tmp_path
    )

    prefix = result.split(orchestrator_main.REFERENCE_BLOCK_START)[0]
    assert "Target files: README_en.md" in prefix
    assert "spec.md" not in prefix


def test_inline_referenced_markdown_ignores_missing_file(tmp_path):
    result = orchestrator_main.inline_referenced_markdown(
        "See docs/does_not_exist.md for details.", tmp_path
    )

    assert result == "See docs/does_not_exist.md for details."


def test_inline_referenced_markdown_ignores_path_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = orchestrator_main.inline_referenced_markdown(
        "See ../outside.md for details.", workspace
    )

    assert "secret" not in result


def test_inline_referenced_markdown_noop_when_no_md_referenced(tmp_path):
    result = orchestrator_main.inline_referenced_markdown(
        "Add a new feature with no file references.", tmp_path
    )

    assert result == "Add a new feature with no file references."


def test_inline_referenced_markdown_dedupes_repeated_reference(tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("content", encoding="utf-8")

    result = orchestrator_main.inline_referenced_markdown(
        "See plan.md and also plan.md again.", tmp_path
    )

    assert result.count("## plan.md") == 1


def test_call_ollama_encodes_local_image(
    initialized_orchestrator, monkeypatch, tmp_path
):
    image = tmp_path / "screen.png"
    image.write_bytes(b"image")
    response = Mock()
    response.json.return_value = {"message": {"content": "review"}}
    post = Mock(return_value=response)
    monkeypatch.setattr(orchestrator.requests, "post", post)

    initialized_orchestrator.call_ollama("review", image_paths=[str(image)])

    assert post.call_args.kwargs["json"]["messages"][-1]["images"] == ["aW1hZ2U="]


def test_call_ollama_request_exception_raises_runtime_error(
    initialized_orchestrator, monkeypatch
):
    monkeypatch.setattr(
        orchestrator.requests,
        "post",
        Mock(side_effect=orchestrator.requests.exceptions.RequestException("down")),
    )

    with pytest.raises(RuntimeError, match="Ollama connection failed: down"):
        initialized_orchestrator.call_ollama("hello")


def test_task_plan_validator_requires_tasks_array():
    assert not is_task_plan_response('{"requirements": "not a task plan"}')
    assert is_task_plan_response('{"tasks": [], "staffing": {}, "specialists": []}')


def test_requirements_validator_requires_structured_json():
    assert not is_requirements_json_response(
        "I'll inspect the Python implementation first."
    )
    data = {
        "project_overview": "Build it",
        "functional_requirements": ["It works"],
        "technical_specifications": ["Python"],
        "scope_boundaries": ["No redesign"],
    }
    assert is_requirements_json_response(json.dumps(data))
    assert "## Project Overview & Context" in render_requirements(data)


def test_manager_planning_supplies_requirements_schema(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.request_path.write_text("Update translated README files", encoding="utf-8")
    requirements = {
        "project_overview": "Update translations",
        "functional_requirements": ["Synchronize README content"],
        "technical_specifications": [],
        "scope_boundaries": ["README files only"],
    }
    call_manager = Mock(side_effect=['{"use_sales": false}', json.dumps(requirements)])
    monkeypatch.setattr(app.manager, "call_manager", call_manager)

    app.manager.step_planning()

    requirements_call = call_manager.call_args_list[1]
    assert (
        json.dumps(requirements_call.kwargs["response_schema"])
        in requirements_call.args[1]
    )
    assert app.state["state"] == "DEVELOPING_PLAN"


def test_staffing_is_capped_by_configured_capacity(initialized_orchestrator):
    initialized_orchestrator.state["staffing"] = {"qa": {"senior": 4, "junior": -1}}

    assert initialized_orchestrator.staffing("qa") == {
        "senior": 1,
        "middle": 0,
        "junior": 0,
    }


def test_role_models_select_the_configured_seniority_model(initialized_orchestrator):
    assert (
        initialized_orchestrator.get_active_model_for_role("developer_senior", "codex")
        == "auto"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("developer_middle", "ollama")
        == "granite4.2:8b"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("developer_junior", "ollama")
        == "gemma4:latest"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("architect", "codex")
        == "auto"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("ra", "agy") == "auto:gemini-flash-medium"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("sales", "agy")
        == "auto:gemini-flash-medium"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("qa_senior", "ollama")
        == "deepseek-r1:7b"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("qa_middle", "ollama")
        == "ornith:9b"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("qa_junior", "ollama")
        == "gemma4:latest"
    )


def test_load_config_migrates_new_role_defaults(tmp_path, no_git):
    ai_dir = tmp_path / ".ai-company"
    ai_dir.mkdir()
    (ai_dir / "config.json").write_text(
        json.dumps({"backends": {"developer": "codex"}}), encoding="utf-8"
    )
    (ai_dir / "state.json").write_text(
        json.dumps({"state": "PLANNING"}), encoding="utf-8"
    )

    app = AgentOrchestrator(tmp_path)
    app.load_config_and_state()

    assert app.get_backend("developer_junior") == "codex"
    assert app.get_active_model_for_role("reviewer", "codex") == "auto"
    assert app.config["ollama_keep_alive"] == 0


def test_role_model_is_not_used_for_an_ollama_fallback(initialized_orchestrator):
    assert (
        initialized_orchestrator.get_active_model_for_role("manager", "ollama")
        == "gemma4:latest"
    )


def test_developer_plan_does_not_require_file_blocks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["state"] = "DEVELOPING_PLAN"
    initialized_orchestrator.config["role_model_routes"]["developer_senior"] = [
        ["agy", "gemini-3.8-flash-medium"]
    ]
    monkeypatch.setattr(
        initialized_orchestrator, "call_agy", Mock(return_value="# Plan")
    )

    assert initialized_orchestrator.call_agent("developer_senior", "prompt") == "# Plan"


def test_invalid_architect_status_falls_back_to_next_route(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["state"] = "REVIEWING_PLAN"
    initialized_orchestrator.config["role_model_routes"]["architect"] = [
        ["agy", "gemini-3.8-flash-medium"],
        ["ollama", "qwen3:8b"],
    ]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(initialized_orchestrator, "call_agy", Mock(return_value="..."))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent_ollama_fallback",
        Mock(return_value="PLAN_STATUS: APPROVED\nok"),
    )

    result = initialized_orchestrator.call_agent("architect", "prompt")

    assert result.startswith("PLAN_STATUS: APPROVED")
    assert initialized_orchestrator.state["failed_model_routes"] == []


def test_output_contract_failure_falls_back_to_next_route(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    initialized_orchestrator.config["role_model_routes"]["developer_junior"] = [
        ["agy", "gemini-3.8-flash-medium"],
        ["ollama", "qwen3:8b"],
    ]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agy",
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


def test_ollama_developer_retries_invalid_contract_once(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    initialized_orchestrator.config["role_model_routes"]["developer_junior"] = [
        ["ollama", "gemma4:latest"]
    ]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    call = Mock(
        side_effect=["I changed it.", "[FILE_START: app.py]\nok\n[FILE_END: app.py]"]
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent_ollama_fallback", call)

    result = initialized_orchestrator.call_agent(
        "developer_junior",
        "prompt",
        response_validator=lambda response: "app.py" in response,
    )

    assert "app.py" in result
    assert call.call_count == 2


def test_implementing_route_failures_are_scoped_to_active_task(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    initialized_orchestrator.state["active_task_id"] = "T-1"
    initialized_orchestrator.config["role_model_routes"]["developer_junior"] = [
        ["agy", "gemini-3.8-flash-medium"]
    ]
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(
        initialized_orchestrator, "call_agy", Mock(side_effect=RuntimeError("invalid"))
    )

    with pytest.raises(RuntimeError):
        initialized_orchestrator.call_agent("developer_junior", "prompt")

    assert initialized_orchestrator.state["task_failed_model_routes"]["T-1"] == [
        "agy/gemini-3.8-flash-medium"
    ]
    assert initialized_orchestrator.state["failed_model_routes"] == []
    initialized_orchestrator.state["active_task_id"] = "T-2"
    assert (
        initialized_orchestrator.state["task_failed_model_routes"].get("T-2", []) == []
    )


def test_developer_route_accepts_markdown_edit_blocks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["state"] = "IMPLEMENTING"
    initialized_orchestrator.config["role_model_routes"]["developer_junior"] = [
        ["agy", "gemini-3.8-flash-medium"]
    ]
    response = "[FILE_EDIT_START: README.md]\n[OLD]\nold\n[NEW]\nnew\n[FILE_EDIT_END: README.md]"
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(
        initialized_orchestrator, "call_agy", Mock(return_value=response)
    )

    assert initialized_orchestrator.call_agent("developer_junior", "prompt") == response


def test_implementing_prompt_requires_only_file_blocks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "write hello",
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    call_agent = Mock(
        return_value="[FILE_START: hello.py]\nprint('hello')\n[FILE_END: hello.py]"
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert "respond only with [file_start" in call_agent.call_args.args[2].lower()


def test_implementing_prompt_includes_only_current_target_files(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "update README",
            "target_files": ["README.md"],
            "section_heading": "## Quick Start",
            "output_contract": {"format": "markdown_section_replacements"},
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    (initialized_orchestrator.workspace / "README.md").write_text(
        "# Project\n\n## Quick Start\n\nKeep me\n\n## Other\n\nDo not include this section\n",
        encoding="utf-8",
    )
    (initialized_orchestrator.workspace / "README_en.md").write_text(
        "# Do not include me\n", encoding="utf-8"
    )
    call_agent = Mock(
        return_value="[SECTION_EDIT_START: README.md]\n[HEADING]\n## Quick Start\n[CONTENT]\nUpdated\n[SECTION_EDIT_END: README.md]"
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    prompt = call_agent.call_args.args[1]
    assert "[CURRENT_FILE: README.md]\n## Quick Start" in prompt
    assert "[SECTION_EDIT_START: README.md]" in prompt
    assert "Do not include this section" not in prompt
    assert "Do not include me" not in prompt


def test_implementing_missing_section_heading_falls_back_to_whole_file(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.config["backends"]["developer_junior"] = "agy"
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    target = initialized_orchestrator.workspace / "docs" / "guide.md"
    target.parent.mkdir()
    target.write_text("# Project\n\nExisting\n", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "add installation guide",
            "target_files": ["docs/guide.md"],
            "section_heading": "## Installation",
            "output_contract": {"format": "markdown_section_replacements"},
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    call_agent = Mock(
        return_value="[FILE_START: docs/guide.md]\n# Project\n\nExisting\n\n## Installation\n\nRun it\n[FILE_END: docs/guide.md]"
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert "## Installation" in target.read_text(encoding="utf-8")
    assert "[FILE_START:" in call_agent.call_args.args[1]


def test_split_scoped_fix_tasks_keep_section_contract(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.config["backends"]["developer_senior"] = "ollama"
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    translation_files = [f"README_{index}.md" for index in range(2)]
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "FIX-REV-2",
            "description": "repair damaged README sections",
            "target_files": translation_files,
            "section_heading": "## Source section",
            "output_contract": {"format": "file_blocks"},
            "status": "pending",
            "rd_level": "senior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"junior": 1},
    }
    (initialized_orchestrator.workspace / translation_files[0]).write_text(
        "# Project\n\n## Intro\n\nIntro\n\n## Source section\n\nOld\n\n## Other\n\nKeep\n",
        encoding="utf-8",
    )
    (initialized_orchestrator.workspace / translation_files[1]).write_text(
        "# Project\n\n## Intro\n\nIntro\n\n## Target section\n\nOld\n\n## Other\n\nKeep\n",
        encoding="utf-8",
    )
    call_agent = Mock(
        side_effect=[
            f"[SECTION_EDIT_START: {translation_files[0]}]\n[HEADING]\n## Source section\n[CONTENT]\nUpdated first\n[SECTION_EDIT_END: {translation_files[0]}]",
            f"[SECTION_EDIT_START: {translation_files[1]}]\n[HEADING]\n## Target section\n[CONTENT]\nUpdated second\n[SECTION_EDIT_END: {translation_files[1]}]",
        ]
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert all(
        "[SECTION_EDIT_START:" in call.args[1] for call in call_agent.call_args_list
    )
    assert "Updated first" in (
        initialized_orchestrator.workspace / translation_files[0]
    ).read_text(encoding="utf-8")
    assert "Updated second" in (
        initialized_orchestrator.workspace / translation_files[1]
    ).read_text(encoding="utf-8")


def test_translation_task_receives_read_only_primary_readme(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "sync translation README",
            "target_files": ["README_en.md"],
            "output_contract": {"format": "file_blocks"},
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    (initialized_orchestrator.workspace / "README.md").write_text(
        "# Canonical\n", encoding="utf-8"
    )
    (initialized_orchestrator.workspace / "README_en.md").write_text(
        "# Translation\n", encoding="utf-8"
    )
    call_agent = Mock(
        return_value="[FILE_START: README_en.md]\n# Translation\n[FILE_END: README_en.md]"
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    prompt = call_agent.call_args.args[1]
    assert "[READ_ONLY_REFERENCE: README.md]" in prompt
    assert "# Canonical" in prompt
    assert "must never be modified" in prompt


def test_fix_task_on_existing_markdown_downgrades_to_targeted_edits(
    initialized_orchestrator, monkeypatch
):
    # A whole-file rewrite has to reproduce every heading verbatim, which no
    # route managed for a revision spanning several unrelated sections.
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.config["backends"]["developer_senior"] = "ollama"
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "FIX-QA-2-13",
            "description": "Fix QA verification issues.",
            "target_files": ["README.md"],
            "output_contract": {"format": "file_blocks"},
            "status": "pending",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"senior": 1},
    }
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text(
        "# Project\n\n## Storage\n\nUses S3.\n\n## Verify\n\nRun pytest.\n",
        encoding="utf-8",
    )
    call_agent = Mock(
        return_value=(
            "[FILE_EDIT_START: README.md]\n[OLD]\nUses S3.\n[NEW]\nUses a NAS pointer.\n[FILE_EDIT_END: README.md]"
        )
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    task = initialized_orchestrator.state["tasks"][0]
    assert task["output_contract"]["format"] == "file_edits"
    assert "[FILE_EDIT_START: README.md]" in call_agent.call_args.args[1]
    assert readme.read_text(encoding="utf-8") == (
        "# Project\n\n## Storage\n\nUses a NAS pointer.\n\n## Verify\n\nRun pytest.\n"
    )


def test_whole_markdown_task_keeps_declared_file_block_contract(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "sync README",
            "target_files": ["README.md"],
            "output_contract": {"format": "file_blocks"},
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n", encoding="utf-8")
    call_agent = Mock(
        return_value="[FILE_START: README.md]\n# Project\n\n## Install\n\nNew\n[FILE_END: README.md]"
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    prompt = call_agent.call_args.args[1]
    assert "[FILE_START: path/to/file.ext]" in prompt
    assert "[SECTION_EDIT_START:" not in prompt
    assert "New" in readme.read_text(encoding="utf-8")


def test_file_blocks_can_replace_headings_for_explicit_full_rewrite(
    initialized_orchestrator,
):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Install\n\n## Workflow\n\nOriginal\n", encoding="utf-8")
    output = (
        "[FILE_START: README.md]\n# Replacement\n\nNew content\n[FILE_END: README.md]"
    )

    written = initialized_orchestrator.parse_and_write_files(
        output,
        ["README.md"],
        allow_markdown_heading_changes=True,
    )

    assert written == ["README.md"]
    assert readme.read_text(encoding="utf-8") == "# Replacement\n\nNew content"


def test_direct_developer_task_stays_pending_when_no_file_changes(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["backends"]["developer_senior"] = "codex"
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "update app.py",
            "target_files": ["app.py"],
            "status": "pending",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"senior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="No edits made")
    )

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"
    assert initialized_orchestrator.state["tasks"][0]["revisions"] == 1
    assert callable(initialized_orchestrator.call_agent.call_args.args[4])


@pytest.mark.parametrize(
    "output",
    [
        "[FILE_START: one.py]\nprint(1)\n[FILE_END: one.py]",
        "[FILE_START: one.py]\nprint(1)\n[FILE_END: one.py]"
        "[FILE_START: two.py]\nprint(2)\n[FILE_END: two.py]"
        "[FILE_START: extra.py]\nprint(3)\n[FILE_END: extra.py]",
    ],
)
def test_incomplete_or_extra_file_response_does_not_write_or_complete(
    initialized_orchestrator, monkeypatch, output
):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{
        "id": "T-1", "description": "update two files",
        "target_files": ["one.py", "two.py"], "status": "pending",
        "rd_level": "senior", "qa_level": "senior",
    }]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1}, "qa": {"senior": 1},
    }
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value=output))

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"
    assert not (initialized_orchestrator.workspace / "one.py").exists()
    assert not (initialized_orchestrator.workspace / "two.py").exists()
    assert not (initialized_orchestrator.workspace / "extra.py").exists()


def test_implementing_skips_read_only_inventory_tasks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "Inventory current routes",
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    call_agent = Mock()
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "completed"
    call_agent.assert_not_called()


def test_legacy_fix_task_inherits_original_target_files(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "change app",
            "target_files": ["app.py"],
            "status": "completed",
            "rd_level": "junior",
            "qa_level": "junior",
        },
        {
            "id": "FIX-REV-1",
            "description": "fix app",
            "status": "pending",
            "rd_level": "middle",
            "qa_level": "junior",
        },
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"middle": 1, "junior": 1},
        "qa": {"junior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value="[FILE_START: app.py]\nfixed\n[FILE_END: app.py]"),
    )

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["tasks"][-1]["target_files"] == ["app.py"]
    assert (initialized_orchestrator.workspace / "app.py").read_text(
        encoding="utf-8"
    ) == "fixed"


def test_human_review_task_inherits_original_target_files(
    initialized_orchestrator, monkeypatch
):
    # Regression test: HUMAN-REVIEW-N tasks need target files inherited
    # from the original task so file-block responses can be validated.
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "change app",
            "target_files": ["app.py"],
            "status": "completed",
            "rd_level": "junior",
            "qa_level": "junior",
        },
        {
            "id": "HUMAN-REVIEW-1",
            "description": "fix app",
            "status": "pending",
            "rd_level": "middle",
            "qa_level": "junior",
        },
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"middle": 1, "junior": 1},
        "qa": {"junior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value="[FILE_START: app.py]\nfixed\n[FILE_END: app.py]"),
    )

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["tasks"][-1]["target_files"] == ["app.py"]
    assert (initialized_orchestrator.workspace / "app.py").read_text(
        encoding="utf-8"
    ) == "fixed"


def test_multi_file_fix_task_splits_and_keeps_route_state(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "FIX-REV-1",
            "description": "fix translations",
            "target_files": ["README_en.md", "README_ja.md"],
            "status": "pending",
            "rd_level": "middle",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"middle": 1},
        "qa": {"junior": 1},
    }
    initialized_orchestrator.state["task_developer_promotions"] = {
        "FIX-REV-1": {"developer_middle": "developer_senior"}
    }
    initialized_orchestrator.state["task_failed_model_routes"] = {
        "FIX-REV-1": ["codex/gpt-5.6-luna"]
    }
    initialized_orchestrator.config["backends"]["developer_senior"] = "ollama"
    call_agent = Mock(
        side_effect=[
            "[FILE_START: README_en.md]\nEnglish\n[FILE_END: README_en.md]",
            "[FILE_START: README_ja.md]\nJapanese\n[FILE_END: README_ja.md]",
        ]
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    split_tasks = initialized_orchestrator.state["tasks"]
    assert [task["target_files"] for task in split_tasks] == [
        ["README_en.md"],
        ["README_ja.md"],
    ]
    assert all(
        initialized_orchestrator.state["task_developer_promotions"][task["id"]][
            "developer_middle"
        ]
        == "developer_senior"
        for task in split_tasks
    )
    assert all(
        initialized_orchestrator.state["task_failed_model_routes"][task["id"]]
        == ["codex/gpt-5.6-luna"]
        for task in split_tasks
    )
    assert call_agent.call_count == 2


def test_implementing_pauses_when_all_model_routes_fail(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "write hello",
            "target_files": ["hello.py"],
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(side_effect=RuntimeError("all routes failed")),
    )

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["code_revisions"] == 0
    assert initialized_orchestrator.state["tasks"][0]["revisions"] == 1


def test_file_blocks_cannot_write_outside_task_contract(initialized_orchestrator):
    output = "[FILE_START: allowed.py]\nok\n[FILE_END: allowed.py]\n[FILE_START: test_orchestrator.py]\nwrong\n[FILE_END: test_orchestrator.py]"

    written = initialized_orchestrator.parse_and_write_files(output, ["allowed.py"])

    assert written == []
    assert not (initialized_orchestrator.workspace / "allowed.py").exists()
    assert not (initialized_orchestrator.workspace / "test_orchestrator.py").exists()


def test_file_blocks_cannot_remove_existing_markdown_headings(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Install\n\n## Workflow\n\nOriginal\n", encoding="utf-8")
    output = "[FILE_START: README.md]\n# New\n\nReplacement\n[FILE_END: README.md]"

    written = initialized_orchestrator.parse_and_write_files(output, ["README.md"])

    assert written == []
    assert (
        readme.read_text(encoding="utf-8") == "# Install\n\n## Workflow\n\nOriginal\n"
    )


def test_file_blocks_reject_python_that_does_not_parse(initialized_orchestrator):
    # The [FILE_START:] protocol is bracket-based, so models leak Markdown
    # escapes like \[ into the payload; one such write breaks collection
    # for the entire suite.
    output = "[FILE_START: app.py]\nvalue = 1\n\\[\n\\]\n[FILE_END: app.py]"

    assert initialized_orchestrator.parse_and_write_files(output, ["app.py"]) == []
    assert not (initialized_orchestrator.workspace / "app.py").exists()


def test_relevant_target_files_prefers_files_named_by_test_output(
    initialized_orchestrator,
):
    initialized_orchestrator.state["tasks"] = [
        {"target_files": ["tests/test_smoke.py", "README.md", "backend/api.py"]}
    ]
    test_output = 'ERROR collecting tests/test_smoke.py\nFile "tests/test_smoke.py", line 90'
    qa_report = "| README.md | PASSED |\n| backend/api.py | PASSED |\n"

    assert initialized_orchestrator.relevant_target_files(
        f"{test_output}\n{qa_report}", primary=test_output
    ) == ["tests/test_smoke.py"]


def test_relevant_target_files_falls_back_when_output_names_nothing(
    initialized_orchestrator,
):
    initialized_orchestrator.state["tasks"] = [
        {"target_files": ["README.md", "backend/api.py"]}
    ]

    assert initialized_orchestrator.relevant_target_files(
        "review says backend/api.py leaks a handle", primary="exit code 1"
    ) == ["backend/api.py"]


def test_markdown_edit_blocks_replace_only_exact_text(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text(
        "# Install\n\nOld command\n\n## Workflow\n\nKeep this\n", encoding="utf-8"
    )
    output = "[FILE_EDIT_START: README.md]\n[OLD]\nOld command\n[NEW]\nNew command\n[FILE_EDIT_END: README.md]"

    written = initialized_orchestrator.parse_and_write_files(output, ["README.md"])

    assert written == ["README.md"]
    assert (
        readme.read_text(encoding="utf-8")
        == "# Install\n\nNew command\n\n## Workflow\n\nKeep this\n"
    )


def test_markdown_edit_blocks_require_unique_old_text(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Install\n\nSame\nSame\n", encoding="utf-8")
    output = "[FILE_EDIT_START: README.md]\n[OLD]\nSame\n[NEW]\nChanged\n[FILE_EDIT_END: README.md]"

    assert initialized_orchestrator.parse_and_write_files(output, ["README.md"]) == []
    assert readme.read_text(encoding="utf-8") == "# Install\n\nSame\nSame\n"


def test_markdown_section_blocks_replace_only_named_section(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text(
        "# Project\n\nIntro\n\n## Install\n\nOld command\n\n## Workflow\n\nKeep this\n",
        encoding="utf-8",
    )
    output = "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Install\n[CONTENT]\nNew command\n[SECTION_EDIT_END: README.md]"

    written = initialized_orchestrator.parse_and_write_files(
        output, ["README.md"], allowed_heading="## Install"
    )

    assert written == ["README.md"]
    assert (
        readme.read_text(encoding="utf-8")
        == "# Project\n\nIntro\n\n## Install\n\nNew command\n\n## Workflow\n\nKeep this\n"
    )


def test_markdown_section_blocks_reject_wrong_task_heading(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text(
        "# Project\n\n## Install\n\nOld\n\n## Workflow\n\nKeep\n", encoding="utf-8"
    )
    output = "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Workflow\n[CONTENT]\nChanged\n[SECTION_EDIT_END: README.md]"

    assert (
        initialized_orchestrator.parse_and_write_files(
            output, ["README.md"], allowed_heading="## Install"
        )
        == []
    )
    assert "Keep" in readme.read_text(encoding="utf-8")


def test_file_level_task_rejects_section_blocks(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n", encoding="utf-8")
    output = "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Install\n[CONTENT]\nChanged\n[SECTION_EDIT_END: README.md]"

    assert initialized_orchestrator.parse_and_write_files(output, ["README.md"]) == []
    assert "Old" in readme.read_text(encoding="utf-8")


def test_single_markdown_response_is_wrapped_only_when_complete(
    initialized_orchestrator,
):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n", encoding="utf-8")
    output = "# Project\n\n## Install\n\nChanged\n"

    assert initialized_orchestrator.parse_and_write_files(output, ["README.md"]) == [
        "README.md"
    ]
    assert "Changed" in readme.read_text(encoding="utf-8")


def test_markdown_section_blocks_reject_unchanged_content(initialized_orchestrator):
    readme = initialized_orchestrator.workspace / "README.md"
    readme.write_text("# Project\n\n## Install\n\nOld\n", encoding="utf-8")
    output = "[SECTION_EDIT_START: README.md]\n[HEADING]\n## Install\n[CONTENT]\nOld\n[SECTION_EDIT_END: README.md]"

    assert (
        initialized_orchestrator.parse_and_write_files(
            output, ["README.md"], allowed_heading="## Install"
        )
        == []
    )
    assert "Old" in readme.read_text(encoding="utf-8")


def test_implementing_pauses_when_file_contract_writes_nothing(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "update README",
            "target_files": ["README.md"],
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            return_value="[FILE_START: README.md]\n# Replacement\n[FILE_END: README.md]"
        ),
    )
    (initialized_orchestrator.workspace / "README.md").write_text(
        "# Existing\n\n## Keep\n", encoding="utf-8"
    )

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["code_revisions"] == 0
    assert initialized_orchestrator.state["tasks"][0]["revisions"] == 1
    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"


def test_manager_invalid_json_falls_back_to_next_route(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["role_model_routes"]["manager"] = [
        ["agy", "auto:gemini-flash-medium"],
        ["codex", "auto"],
    ]
    agy = Mock(return_value="Here is the JSON you requested: not valid")
    codex = Mock(return_value='{"use_sales": false}')
    monkeypatch.setattr(initialized_orchestrator, "call_agy", agy)
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)

    result = initialized_orchestrator.call_agent(
        "manager", "prompt", response_validator=is_json_response
    )

    assert result == '{"use_sales": false}'
    assert initialized_orchestrator.state["failed_model_routes"] == []


def test_run_to_end_pauses_instead_of_raising_on_unhandled_error(
    initialized_orchestrator, monkeypatch
):
    monkeypatch.setattr(
        initialized_orchestrator,
        "step",
        Mock(side_effect=RuntimeError("unexpected failure")),
    )

    initialized_orchestrator.run_to_end()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "Orchestrator"


def test_manager_retries_agy_then_ollama_after_token_failure(
    initialized_orchestrator, monkeypatch
):
    agy = Mock(side_effect=RuntimeError("maximum context length"))
    codex = Mock(side_effect=RuntimeError("maximum context length"))
    ollama = Mock(return_value="fallback")
    monkeypatch.setattr(initialized_orchestrator, "call_agy", agy)
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)
    monkeypatch.setattr(initialized_orchestrator, "call_agent_ollama_fallback", ollama)

    assert initialized_orchestrator.call_manager("prompt") == "fallback"
    codex.assert_called_once_with("prompt", None, role="manager", model="auto")
    agy.assert_called_once_with("prompt", None, role="manager", model="auto:gemini-flash-medium")
    ollama.assert_called_once_with("manager", "prompt", None, model="qwen3.8:latest")


def test_reviewer_retries_agy_then_ollama_after_token_failure(
    initialized_orchestrator, monkeypatch
):
    agy = Mock(side_effect=RuntimeError("maximum context length"))
    codex = Mock(side_effect=RuntimeError("maximum context length"))
    ollama = Mock(return_value="fallback")
    monkeypatch.setattr(initialized_orchestrator, "call_agy", agy)
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)
    monkeypatch.setattr(initialized_orchestrator, "call_agent_ollama_fallback", ollama)

    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "fallback"
    codex.assert_called_once_with("prompt", None, role="reviewer", model="auto")
    agy.assert_called_once_with("prompt", None, role="reviewer", model="auto:gemini-flash-medium")
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
    assert assignments == {
        "T-1": "rd-junior-1",
        "T-2": "rd-junior-2",
        "T-3": "rd-junior-1",
    }


def test_token_fallback_promotes_manager_only_for_token_errors(
    initialized_orchestrator,
):
    assert (
        initialized_orchestrator.token_fallback_model(
            "manager", RuntimeError("maximum context length")
        )
        == "auto:gemini-flash-medium"
    )
    assert (
        initialized_orchestrator.get_active_model_for_role("manager", "codex")
        == "auto"
    )
    assert (
        initialized_orchestrator.token_fallback_model(
            "reviewer", RuntimeError("connection failed")
        )
        is None
    )


def test_qa_senior_uses_local_qwen(initialized_orchestrator, monkeypatch):
    ollama = Mock(return_value="qwen response")
    monkeypatch.setattr(initialized_orchestrator, "call_ollama", ollama)

    assert initialized_orchestrator.call_agent("qa_senior", "prompt") == "qwen response"
    ollama.assert_called_once_with(
        "prompt", None, role="qa_senior", model="gemma4:latest"
    )


def test_qa_ollama_falls_back_to_gemma(initialized_orchestrator, monkeypatch):
    ollama = Mock(side_effect=[RuntimeError("model unavailable"), "gemma fallback"])
    monkeypatch.setattr(initialized_orchestrator, "call_ollama", ollama)

    assert (
        initialized_orchestrator.call_agent_ollama_fallback("qa_junior", "prompt")
        == "gemma fallback"
    )
    assert ollama.call_args_list == [
        call("prompt", None, role="qa_junior"),
        call("prompt", None, role="qa_junior", model="gemma4:latest"),
    ]


def test_quota_exhausted_backend_is_skipped_for_the_rest_of_the_day(
    initialized_orchestrator, monkeypatch
):
    codex = Mock(side_effect=RuntimeError("429 quota exceeded"))
    agy = Mock(return_value="agy fallback")
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)
    monkeypatch.setattr(initialized_orchestrator, "call_agy", agy)
    initialized_orchestrator.config["role_model_routes"]["reviewer"] = [
        ["codex", "gpt-5.6-sol"],
        ["agy", "Gemini 3.5 Flash (Medium)"],
    ]

    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "agy fallback"
    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "agy fallback"

    codex.assert_called_once_with("prompt", None, role="reviewer", model="gpt-5.6-sol")
    assert (
        agy.call_args_list
        == [call("prompt", None, role="reviewer", model="Gemini 3.5 Flash (Medium)")]
        * 2
    )


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

    assert (
        initialized_orchestrator.state["developer_promotions"]["developer_junior"]
        == "developer_middle"
    )
    assert (
        initialized_orchestrator.config["role_models"]["developer_junior"]
        == original_model
    )
    assert initialized_orchestrator.fix_task_levels() == {
        "rd_level": "middle",
        "qa_level": "junior",
    }
    assert initialized_orchestrator.state["staffing"]["rd"]["middle"] == 1


def test_developer_promotion_is_isolated_to_active_task(initialized_orchestrator):
    initialized_orchestrator.state["active_task_id"] = "T-1"
    initialized_orchestrator.state["last_developer_role"] = "developer_junior"

    initialized_orchestrator.escalate_developer_backend()

    assert (
        initialized_orchestrator.state["task_developer_promotions"]["T-1"][
            "developer_junior"
        ]
        == "developer_middle"
    )
    assert initialized_orchestrator.state["developer_promotions"] == {}
    assert (
        initialized_orchestrator.state["task_developer_promotions"].get("T-2", {}) == {}
    )


def test_developing_plan_saves_manager_staffing(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="plan")
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "T-1", "description": "implement", "target_files": ["app.py"], "status": "pending", "complexity": "routine", "assignee_level": "junior"}], "staffing": {"rd": {"senior": 1, "junior": 2}, "qa": {"senior": 1, "junior": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["staffing"]["rd"] == {
        "senior": 1,
        "junior": 2,
    }
    assert initialized_orchestrator.state["staffing"]["qa"] == {
        "senior": 1,
        "junior": 1,
    }
    assert initialized_orchestrator.state["tasks"][0]["assignee_level"] == "junior"


def test_developing_plan_uses_manager_tasks_without_developer_planner(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "Add a setting", encoding="utf-8"
    )
    developer_call = Mock()
    monkeypatch.setattr(initialized_orchestrator, "call_agent", developer_call)
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "T-1", "description": "add setting", "target_files": ["app.py"], "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    developer_call.assert_not_called()
    assert "`app.py`" in initialized_orchestrator.plan_path.read_text(encoding="utf-8")


def test_developing_plan_keeps_section_level_readme_tasks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "只允許修改 docs/guide.md", encoding="utf-8"
    )
    initialized_orchestrator.request_path.write_text(
        "只允許修改 docs/guide.md", encoding="utf-8"
    )
    guide = initialized_orchestrator.workspace / "docs" / "guide.md"
    guide.parent.mkdir()
    guide.write_text("# Project\n\n## Quick Start\n\nOld\n", encoding="utf-8")
    manager = Mock(
        return_value='{"tasks": [{"id": "DOC-QUICK", "description": "update Quick Start", "target_files": ["docs/guide.md"], "section_heading": "## Quick Start", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'
    )
    monkeypatch.setattr(initialized_orchestrator, "call_manager", manager)

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["id"] == "DOC-QUICK"
    assert (
        initialized_orchestrator.state["tasks"][0]["section_heading"]
        == "## Quick Start"
    )
    assert "## Quick Start" in manager.call_args.args[0]


def test_developing_plan_ignores_markdown_paths_inside_reference_block(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "Build a small backend app.", encoding="utf-8"
    )
    request = (
        "Build the app as specified.\n\n"
        "<!-- REFERENCE-ONLY:START (context, not a file to create or edit) -->\n"
        "## docs/spec.md\n\n# Spec\n\n## Some Heading\n\nDetails.\n"
        "<!-- REFERENCE-ONLY:END -->\n"
    )
    initialized_orchestrator.request_path.write_text(request, encoding="utf-8")
    spec = initialized_orchestrator.workspace / "docs" / "spec.md"
    spec.parent.mkdir()
    spec.write_text("# Spec\n\n## Some Heading\n\nDetails.\n", encoding="utf-8")
    manager = Mock(
        return_value='{"tasks": [{"id": "T1", "description": "implement backend", "target_files": ["backend/app.py"], "rd_level": "middle", "qa_level": "middle"}], "staffing": {"rd": {"middle": 1}, "qa": {"middle": 1}}}'
    )
    monkeypatch.setattr(initialized_orchestrator, "call_manager", manager)

    initialized_orchestrator.step_developing_plan()

    prompt_sent_to_manager = manager.call_args.args[0]
    assert "docs/spec.md" not in prompt_sent_to_manager
    assert initialized_orchestrator.state["tasks"][0]["id"] == "T1"


def test_developing_plan_ignores_markdown_path_referenced_in_original_request_text(
    initialized_orchestrator, monkeypatch
):
    # Regression test for the full `start` -> `inline_referenced_markdown`
    # -> DEVELOPING_PLAN path: the human-written prompt sentence itself
    # names the referenced file by path (that's how inline_referenced_
    # markdown knows what to inline), and that mention must not survive
    # unmasked outside the reference block, or DEVELOPING_PLAN misreads it
    # as a file to edit section-by-section -- this happened live even after
    # the reference block wrapping was added, because only the appended
    # copy was protected, not the original mention.
    initialized_orchestrator.requirements_path.write_text(
        "Build a small backend app.", encoding="utf-8"
    )
    spec = initialized_orchestrator.workspace / "docs" / "spec.md"
    spec.parent.mkdir()
    spec.write_text("# Spec\n\n## Some Heading\n\nDetails.\n", encoding="utf-8")
    request = orchestrator_main.inline_referenced_markdown(
        "Build the app described in docs/spec.md.", initialized_orchestrator.workspace
    )
    initialized_orchestrator.request_path.write_text(request, encoding="utf-8")
    manager = Mock(
        return_value='{"tasks": [{"id": "T1", "description": "implement backend", "target_files": ["backend/app.py"], "rd_level": "middle", "qa_level": "middle"}], "staffing": {"rd": {"middle": 1}, "qa": {"middle": 1}}}'
    )
    monkeypatch.setattr(initialized_orchestrator, "call_manager", manager)

    initialized_orchestrator.step_developing_plan()

    prompt_sent_to_manager = manager.call_args.args[0]
    assert "docs/spec.md" not in prompt_sent_to_manager
    assert initialized_orchestrator.state["tasks"][0]["id"] == "T1"


def test_developing_plan_uses_whole_file_task_for_new_markdown(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "只允許新增 docs/litellm-telemetry.md", encoding="utf-8"
    )
    initialized_orchestrator.request_path.write_text(
        "建立 docs/litellm-telemetry.md", encoding="utf-8"
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "DOC-NEW", "description": "write telemetry guide", "target_files": ["docs/litellm-telemetry.md"], "section_heading": "## Installation and enablement", "rd_level": "middle", "qa_level": "middle"}], "staffing": {"rd": {"middle": 1}, "qa": {"middle": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    task = initialized_orchestrator.state["tasks"][0]
    assert "section_heading" not in task
    assert task["output_contract"]["format"] == "file_blocks"
    plan = initialized_orchestrator.plan_path.read_text(encoding="utf-8")
    assert "Check Markdown structure, links, and code fences" in plan
    assert "Run the configured test command" not in plan
    assert "Run `git diff --check`" in plan


def test_developing_plan_uses_whole_file_tasks_for_readme_language_swap(
    initialized_orchestrator, monkeypatch
):
    request = "將 README.md 與 README_en.md 互換語言角色，並同步更新多國語系。"
    initialized_orchestrator.requirements_path.write_text(request, encoding="utf-8")
    initialized_orchestrator.request_path.write_text(request, encoding="utf-8")
    (initialized_orchestrator.workspace / "README.md").write_text(
        "# Multi-Agent Orchestrator\n", encoding="utf-8"
    )
    (initialized_orchestrator.workspace / "README_en.md").write_text(
        "# Multi-Agent Orchestrator\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "README-TW", "description": "rewrite README.md", "target_files": ["README.md"], "section_heading": "# Multi-Agent Orchestrator", "rd_level": "senior", "qa_level": "middle"}, {"id": "README-EN", "description": "rewrite README_en.md", "target_files": ["README_en.md"], "section_heading": "# Multi-Agent Orchestrator", "rd_level": "senior", "qa_level": "middle"}], "staffing": {"rd": {"senior": 1}, "qa": {"middle": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert len(initialized_orchestrator.state["tasks"]) == 2
    assert all(
        "section_heading" not in task
        for task in initialized_orchestrator.state["tasks"]
    )
    assert all(
        task["output_contract"]["format"] == "file_blocks"
        for task in initialized_orchestrator.state["tasks"]
    )
    assert all(
        task["output_contract"]["allow_markdown_heading_changes"]
        for task in initialized_orchestrator.state["tasks"]
    )


def test_developing_plan_allows_heading_changes_for_sole_readme_authoring_task(
    initialized_orchestrator, monkeypatch
):
    # Regression test: a task whose only target is a single existing Markdown
    # file, submitted whole-file (file_blocks), used to have its heading-
    # preservation guard left off by default unless it matched the narrow
    # swap/translation markers. That silently rejected an ordinary "write the
    # README" task (every route "failed the task output contract" because the
    # new content didn't repeat the old scaffold's headings) even though the
    # task's own scope makes it the sole owner of that file's content.
    initialized_orchestrator.requirements_path.write_text(
        "Build a small backend app.", encoding="utf-8"
    )
    initialized_orchestrator.request_path.write_text(
        "Build a small backend app.", encoding="utf-8"
    )
    (initialized_orchestrator.workspace / "README.md").write_text(
        "# Scaffold\n\n## TODO\n\nFill this in.\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "T14", "description": "Write README.md documenting purpose, install, and usage.", "target_files": ["README.md"], "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    task = initialized_orchestrator.state["tasks"][0]
    assert task["output_contract"]["format"] == "file_blocks"
    assert task["output_contract"]["allow_markdown_heading_changes"] is True


def test_developing_plan_uses_file_edits_for_navigation_only_task(
    initialized_orchestrator, monkeypatch
):
    request = "互換 README.md 與 README_en.md，並更新 README_zh-CN.md 的語言導覽；保留 body content。"
    initialized_orchestrator.requirements_path.write_text(request, encoding="utf-8")
    initialized_orchestrator.request_path.write_text(request, encoding="utf-8")
    for name in ("README.md", "README_en.md", "README_zh-CN.md"):
        (initialized_orchestrator.workspace / name).write_text(
            "# Multi-Agent Orchestrator\n\nNavigation\n\n## Body\n\nKeep\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "README-TW", "description": "swap README.md", "target_files": ["README.md"], "rd_level": "middle", "qa_level": "middle"}, {"id": "README-EN", "description": "swap README_en.md", "target_files": ["README_en.md"], "rd_level": "middle", "qa_level": "middle"}, {"id": "README-ZH-NAV", "description": "Update language navigation in README_zh-CN.md; leave body content otherwise unchanged.", "target_files": ["README_zh-CN.md"], "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"middle": 1, "junior": 1}, "qa": {"middle": 1, "junior": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    nav_task = next(
        task
        for task in initialized_orchestrator.state["tasks"]
        if task["id"] == "README-ZH-NAV"
    )
    assert nav_task["output_contract"] == {
        "format": "file_edits",
        "response_must_start_with": "[FILE_EDIT_START:",
        "allow_prose": False,
    }


def test_implementing_navigation_task_requests_file_edit_blocks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "NAV-1",
            "description": "Update language navigation; leave body content otherwise unchanged.",
            "target_files": ["README_zh-CN.md"],
            "output_contract": {
                "format": "file_edits",
                "response_must_start_with": "[FILE_EDIT_START:",
                "allow_prose": False,
            },
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    target = initialized_orchestrator.workspace / "README_zh-CN.md"
    target.write_text(
        "# Project\n\nOld navigation\n\n## Body\n\nKeep\n", encoding="utf-8"
    )
    call_agent = Mock(
        return_value="[FILE_EDIT_START: README_zh-CN.md]\n[OLD]\nOld navigation\n[NEW]\nNew navigation\n[FILE_EDIT_END: README_zh-CN.md]"
    )
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert "[FILE_EDIT_START: README_zh-CN.md]" in call_agent.call_args.args[1]
    assert (
        target.read_text(encoding="utf-8")
        == "# Project\n\nNew navigation\n\n## Body\n\nKeep\n"
    )


def test_developing_plan_covers_non_markdown_requested_file(
    initialized_orchestrator, monkeypatch
):
    request = "將 README.md 與 README_en.md 完整內容互換，並更新 verify_alignment.py。只允許修改這三個檔案。"
    initialized_orchestrator.requirements_path.write_text(request, encoding="utf-8")
    initialized_orchestrator.request_path.write_text(request, encoding="utf-8")
    for name in ("README.md", "README_en.md"):
        (initialized_orchestrator.workspace / name).write_text(
            "# Multi-Agent Orchestrator\n", encoding="utf-8"
        )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "README-TW", "description": "rewrite README.md", "target_files": ["README.md"], "rd_level": "senior", "qa_level": "middle"}, {"id": "README-EN", "description": "rewrite README_en.md", "target_files": ["README_en.md"], "rd_level": "senior", "qa_level": "middle"}, {"id": "ALIGNMENT", "description": "update verify_alignment.py", "target_files": ["verify_alignment.py"], "rd_level": "middle", "qa_level": "middle"}], "staffing": {"rd": {"senior": 1, "middle": 1}, "qa": {"middle": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert {
        task["target_files"][0] for task in initialized_orchestrator.state["tasks"]
    } == {
        "README.md",
        "README_en.md",
        "verify_alignment.py",
    }
    assert "- `verify_alignment.py`" in initialized_orchestrator.plan_path.read_text(
        encoding="utf-8"
    )
    assert "Run the configured test command" in initialized_orchestrator.plan_path.read_text(
        encoding="utf-8"
    )


def test_developing_plan_normalizes_task_status_to_pending(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="plan")
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "T-1", "description": "implement", "target_files": ["app.py"], "status": "completed", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"


def test_developing_plan_does_not_treat_unrelated_change_scope_as_readme_only(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "Update README and config; 不得修改無關功能", encoding="utf-8"
    )
    initialized_orchestrator.request_path.write_text(
        "Update README.md and orchestrator/core/config.py", encoding="utf-8"
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="plan")
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "CONFIG-1", "description": "update config", "target_files": ["orchestrator/core/config.py"], "rd_level": "middle", "qa_level": "junior"}], "staffing": {"rd": {"middle": 1}, "qa": {"junior": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["id"] == "CONFIG-1"


def test_developing_plan_pauses_when_manager_returns_only_planning_tasks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="plan")
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "T-1", "description": "Gather requirements", "rd_level": "senior", "qa_level": "junior"}]}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "Manager"


def test_developing_plan_reopens_changed_completed_task(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "old scope",
            "target_files": ["app.py"],
            "status": "completed",
            "complexity": "routine",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="plan")
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "T-1", "description": "new scope", "target_files": ["app.py"], "complexity": "routine", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["status"] == "pending"


def test_developing_plan_tells_manager_to_preserve_completed_tasks_on_replan(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "old scope",
            "target_files": ["app.py"],
            "status": "completed",
            "complexity": "routine",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    manager = Mock(
        return_value='{"tasks": [{"id": "T-1", "description": "old scope", "target_files": ["app.py"], "complexity": "routine", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'
    )
    monkeypatch.setattr(initialized_orchestrator, "call_manager", manager)

    initialized_orchestrator.step_developing_plan()

    prompt = manager.call_args.args[0]
    assert "plan revision, not a fresh start" in prompt
    assert '"id": "T-1"' in prompt
    assert '"status": "completed"' in prompt
    assert initialized_orchestrator.state["tasks"][0]["status"] == "completed"


def test_developing_plan_omits_existing_tasks_rule_on_first_pass(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    manager = Mock(
        return_value='{"tasks": [{"id": "T-1", "description": "implement", "target_files": ["app.py"], "complexity": "routine", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 1}, "qa": {"junior": 1}}}'
    )
    monkeypatch.setattr(initialized_orchestrator, "call_manager", manager)

    initialized_orchestrator.step_developing_plan()

    assert "plan revision, not a fresh start" not in manager.call_args.args[0]


def test_developing_plan_rejects_staffing_that_cannot_cover_tasks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="plan")
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value='{"tasks": [{"id": "T-1", "description": "implement", "rd_level": "junior", "qa_level": "junior"}], "staffing": {"rd": {"junior": 0}, "qa": {"junior": 0}}}'
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "Manager"


def test_consult_specialists_only_calls_manager_selected_roles(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["specialists"] = [
        {"role": "security", "reason": "handles credentials"},
        {"role": "unknown", "reason": "must be ignored"},
    ]
    call_agent = Mock(return_value="review")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    notes = initialized_orchestrator.consult_specialists("requirements", "plan")

    assert "## Security" in notes
    assert call_agent.call_args.args[0] == "security"


def test_consult_specialists_skips_readme_only_tasks(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["specialists"] = [
        {"role": "devops", "reason": "mentioned in docs"}
    ]
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
    initialized_orchestrator.state["tasks"] = [
        {"id": "T-1", "description": "add it", "status": "pending", "unused": "omit"}
    ]

    initialized_orchestrator.write_agent_context()

    assert json.loads(
        initialized_orchestrator.agent_context_path.read_text(encoding="utf-8")
    ) == {
        "request": "Add a setting",
        "contract": {
            "stage": "PLANNING",
            "allowed_actions": ["plan_or_review"],
            "output_contract": {"format": "stage_prompt", "allow_file_blocks": False},
        },
        "tasks": [{"id": "T-1", "description": "add it", "status": "pending"}],
        "specialists": [],
    }


def test_assistant_meeting_memory_uses_handoff_sections(
    initialized_orchestrator, monkeypatch
):
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="## Outcome\ndone")
    )

    memory = initialized_orchestrator.assistant.generate_meeting_memory(
        "請修正", "完成", [], "PASSED", "APPROVED", "a.py"
    )

    assert memory == "## Outcome\ndone"
    prompt = initialized_orchestrator.call_agent.call_args.args[1]
    assert "## Next-session Handoff" in prompt


def test_consult_specialists_dispatches_new_role_modules(
    initialized_orchestrator, monkeypatch
):
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


def test_visual_review_receives_start_image_paths(
    initialized_orchestrator, monkeypatch, tmp_path
):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"image")
    initialized_orchestrator.state["specialists"] = [
        {"role": "uiux_visual_review", "reason": "mockup"}
    ]
    initialized_orchestrator.state["visual_image_paths"] = [str(screenshot)]
    review = Mock(return_value="review")
    monkeypatch.setattr(initialized_orchestrator.uiux_visual_review, "review", review)

    initialized_orchestrator.consult_specialists("requirements", "plan")

    assert f"[IMAGE: {screenshot}]" in review.call_args.args[2]


def test_visual_images_force_visual_review_specialist(initialized_orchestrator):
    initialized_orchestrator.state["visual_image_paths"] = ["/tmp/screen.png"]

    initialized_orchestrator.ensure_visual_review_specialist()
    initialized_orchestrator.ensure_visual_review_specialist()

    assert initialized_orchestrator.state["specialists"] == [
        {"role": "uiux_visual_review", "reason": "Visual inputs provided"}
    ]


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
def test_step_dispatches_known_states(
    initialized_orchestrator, monkeypatch, state, method_name
):
    initialized_orchestrator.state["state"] = state
    initialized_orchestrator.save_state()
    called = Mock()
    monkeypatch.setattr(initialized_orchestrator, method_name, called)

    initialized_orchestrator.step()

    called.assert_called_once_with()


def test_step_reviewing_plan_approved_moves_to_implementing(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value='{"status":"APPROVED","feedback":["ok"]}'),
    )

    initialized_orchestrator.step_reviewing_plan()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert (
        json.loads(initialized_orchestrator.reviewer_output_json_path.read_text())[
            "status"
        ]
        == "APPROVED"
    )
    assert initialized_orchestrator.reviewer_output_path.read_text().startswith(
        "PLAN_STATUS: APPROVED"
    )


def test_step_reviewing_plan_rejected_revises_until_max(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value='{"status":"REJECTED","feedback":["fix it"]}'),
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_reviewing_plan()

    assert initialized_orchestrator.state["plan_revisions"] == 1
    assert initialized_orchestrator.state["state"] == "DEVELOPING_PLAN"

    initialized_orchestrator.state["plan_revisions"] = initialized_orchestrator.config[
        "max_revisions"
    ]
    initialized_orchestrator.step_reviewing_plan()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["pass_state"] == "IMPLEMENTING"


def test_step_testing_passed_moves_to_reviewing_code(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(0, "tests ok"))
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value="QA_STATUS: PASSED\nok"),
    )

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "REVIEWING_CODE"


def test_step_testing_captures_qa_test_command_suggestion(
    initialized_orchestrator, monkeypatch
):
    # QA is required to flag it when the test command itself hides
    # diagnostic info it needs (e.g. pytest -q silently swallowing skip
    # reasons) -- this is human-owned config, so QA can only suggest a
    # change here, never apply it itself.
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(0, "tests ok"))
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            return_value="QA_STATUS: PASSED\nok\nTEST_COMMAND_SUGGESTION: add -rs so skip reasons are visible"
        ),
    )

    initialized_orchestrator.step_testing()

    assert (
        initialized_orchestrator.state["test_command_suggestion"]
        == "add -rs so skip reasons are visible"
    )


def test_step_testing_pauses_for_human_when_qa_reports_missing_dependency(
    initialized_orchestrator, monkeypatch
):
    # A missing OS-level dependency (e.g. playwright not installed) can
    # never be fixed by a code-only FIX task -- Developer just burns
    # revision/escalation budget rewording skip messages instead of
    # actually resolving anything. QA must be able to say "a human needs
    # to run this install command," and the orchestrator must pause
    # immediately for that instead of looping through code revisions.
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    monkeypatch.setattr(
        initialized_orchestrator,
        "run_command",
        Mock(
            return_value=(
                0,
                "SKIPPED [3] tests/test_gui.py: ModuleNotFoundError: No module named 'playwright'",
            )
        ),
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            return_value="QA_STATUS: FAILED\nskipped tests\nDEPENDENCY_INSTALL_NEEDED: pip install playwright && playwright install chromium"
        ),
    )
    escalate = Mock()
    monkeypatch.setattr(
        initialized_orchestrator, "escalate_developer_backend", escalate
    )

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "QA"
    assert (
        "pip install playwright && playwright install chromium"
        in initialized_orchestrator.state["human_review_details"]
    )
    assert initialized_orchestrator.state["code_revisions"] == 0
    assert all(
        not t["id"].startswith("FIX-QA")
        for t in initialized_orchestrator.state["tasks"]
    )
    escalate.assert_not_called()


def test_step_testing_ignores_dependency_marker_when_qa_says_none_needed(
    initialized_orchestrator, monkeypatch
):
    # Regression test: observed live -- a weak local QA model doesn't
    # reliably follow "only add this line if it applies." It added
    # `DEPENDENCY_INSTALL_NEEDED: None of the failures appear to be due to
    # missing system dependencies...` even though nothing needed
    # installing, which used to be treated as a real, actionable signal
    # and paused the run with a nonsense "install command."
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"senior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(1, "1 failed"))
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            return_value="QA_STATUS: FAILED\nselector timeout\nDEPENDENCY_INSTALL_NEEDED: None of the failures appear to be due to missing system dependencies"
        ),
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] != "WAITING_FOR_OWNER"
    assert "test_command_suggestion" not in initialized_orchestrator.state


def test_step_testing_ignores_dependency_marker_wrapped_in_punctuation(
    initialized_orchestrator, monkeypatch
):
    # Regression test: observed live again -- this time the "not needed"
    # filler was wrapped in a leading paren, e.g.
    # `DEPENDENCY_INSTALL_NEEDED: (None immediately required, but confirm
    # ...)`, which slipped past a plain `.startswith("none")` check since
    # the value doesn't literally start with the word "none".
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"senior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(1, "1 failed"))
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            return_value="QA_STATUS: FAILED\nselector timeout\nDEPENDENCY_INSTALL_NEEDED: (None immediately required, but confirm all mocked binaries can be found on PATH.)"
        ),
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] != "WAITING_FOR_OWNER"


def test_step_testing_ignores_dependency_marker_truncated_to_garbage(
    initialized_orchestrator, monkeypatch
):
    # Regression test: observed live a third time -- the weak model's
    # output got truncated mid-line to a bare "\", which isn't a "none
    # needed" filler (nothing in the denylist matches it) but is equally
    # useless as an "install command." Require real content, not just any
    # non-empty string, before treating the marker as actionable.
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"senior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(1, "1 failed"))
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            return_value="QA_STATUS: FAILED\nselector timeout\nDEPENDENCY_INSTALL_NEEDED: \\"
        ),
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] != "WAITING_FOR_OWNER"


def test_step_testing_ignores_dependency_marker_uncorroborated_by_raw_output(
    initialized_orchestrator, monkeypatch
):
    # Regression test: observed live after switching QA to a stronger
    # backend (codex/gpt-5.6-luna) -- it stopped producing malformed
    # marker values, but it still claimed
    # `DEPENDENCY_INSTALL_NEEDED: pip install playwright && playwright
    # install chromium` from general knowledge of the project (playwright
    # is in requirements.txt) even though this run's actual raw test
    # output had zero mention of playwright or any import/module error --
    # it was a single unrelated mock-assertion failure. A well-formed
    # value isn't enough; it must be corroborated by the raw output.
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"senior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator,
        "run_command",
        Mock(return_value=(1, "1 failed, 74 passed -- AssertionError: mock mismatch")),
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            return_value="QA_STATUS: FAILED\nmock assertion mismatch\nDEPENDENCY_INSTALL_NEEDED: pip install -r requirements.txt && playwright install chromium"
        ),
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] != "WAITING_FOR_OWNER"


def test_pause_for_human_review_includes_test_command_suggestion(
    initialized_orchestrator,
):
    initialized_orchestrator.state["test_command_suggestion"] = (
        "add -rs so skip reasons are visible"
    )

    initialized_orchestrator.pause_for_human_review(
        "QA", "details", "TESTING", "TESTING"
    )

    report = initialized_orchestrator.human_report_path.read_text(encoding="utf-8")
    assert "add -rs so skip reasons are visible" in report


def test_step_testing_failed_command_cannot_pass_on_qa_response(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(1, "tests failed"))
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="PASSED\nok")
    )

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert (
        "Test exit code: 1"
        in initialized_orchestrator.state["tasks"][-1]["description"]
    )
    assert "tests failed" in initialized_orchestrator.state["tasks"][-1]["description"]


def test_step_testing_command_not_found_pauses_without_fix_task(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "implement",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    monkeypatch.setattr(
        initialized_orchestrator,
        "run_command",
        Mock(return_value=(127, "bash: .venv/bin/python: No such file or directory")),
    )
    qa_agent = Mock()
    monkeypatch.setattr(initialized_orchestrator, "call_agent", qa_agent)
    escalate = Mock()
    monkeypatch.setattr(
        initialized_orchestrator, "escalate_developer_backend", escalate
    )

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["resume_state"] == "TESTING"
    assert all(
        not t["id"].startswith("FIX-QA")
        for t in initialized_orchestrator.state["tasks"]
    )
    qa_agent.assert_not_called()
    escalate.assert_not_called()


def test_step_testing_failed_revises_until_max(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "change app",
            "target_files": ["app.py"],
            "status": "completed",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(1, "tests failed"))
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="FAILED\nfix it")
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["code_revisions"] == 1
    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["tasks"][-1]["id"] == "FIX-QA-1"
    assert initialized_orchestrator.state["tasks"][-1]["rd_level"] == "senior"
    assert initialized_orchestrator.state["tasks"][-1]["qa_level"] == "junior"
    assert initialized_orchestrator.state["tasks"][-1]["target_files"] == ["app.py"]


def test_step_testing_failed_narrows_target_files_to_ones_the_report_names(
    initialized_orchestrator, monkeypatch
):
    # Regression test: target_files used to be the union of every file any
    # task had ever targeted, so a file touched once early on (e.g.
    # README.md) stayed a permanent target forever. Per-file splitting then
    # handed out a subtask for it on every later failure even when it had
    # nothing to do with that failure, and the subtask could never succeed
    # since there was nothing relevant left to fix. It should now narrow to
    # just the files the QA report/output actually names, when any match.
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "change app",
            "target_files": ["app.py"],
            "status": "completed",
            "rd_level": "junior",
            "qa_level": "junior",
        },
        {
            "id": "T-2",
            "description": "write docs",
            "target_files": ["README.md"],
            "status": "completed",
            "rd_level": "junior",
            "qa_level": "junior",
        },
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    monkeypatch.setattr(
        initialized_orchestrator,
        "run_command",
        Mock(return_value=(1, "FAILED tests/test_app.py - app.py is broken")),
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="QA_STATUS: FAILED")
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["tasks"][-1]["target_files"] == ["app.py"]


def test_step_testing_failed_at_max_revisions_escalates_then_pauses(
    initialized_orchestrator, monkeypatch
):
    # First time hitting max_revisions: gets one bounded chance to replan
    # (Manager reconsiders the task/file breakdown) instead of immediately
    # giving up on automation.
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "target_files": ["app.py"],
            "status": "completed",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    initialized_orchestrator.state["code_revisions"] = initialized_orchestrator.config[
        "max_revisions"
    ]
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(1, "tests failed"))
    )
    monkeypatch.setattr(
        initialized_orchestrator, "call_agent", Mock(return_value="FAILED\nfix it")
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "DEVELOPING_PLAN"
    assert initialized_orchestrator.state["plan_escalations"] == 1
    assert initialized_orchestrator.state["code_revisions"] == 0
    assert (
        "Repeated failure during implementation"
        in initialized_orchestrator.requirements_path.read_text(encoding="utf-8")
    )

    # Second time hitting max_revisions after the escalation was already
    # used: no more automatic options, pause for a human.
    initialized_orchestrator.state["code_revisions"] = initialized_orchestrator.config[
        "max_revisions"
    ]
    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"


def test_step_reviewing_code_approved_moves_to_completed(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text("tests", encoding="utf-8")
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value='{"status":"APPROVED","feedback":["ok"]}'),
    )

    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["state"] == "COMPLETED"
    assert (
        json.loads(initialized_orchestrator.reviewer_output_json_path.read_text())[
            "status"
        ]
        == "APPROVED"
    )
    assert initialized_orchestrator.reviewer_output_path.read_text().startswith(
        "APPROVED"
    )


def test_reviewer_receives_untracked_file_status(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text(
        "tests passed", encoding="utf-8"
    )
    initialized_orchestrator.has_git = True
    monkeypatch.setattr(
        initialized_orchestrator,
        "run_command",
        Mock(
            side_effect=[
                (0, "diff"),
                (0, "stdout:\n?? docs/new.md\nstderr:\n"),
                (1, "new file contents"),
            ]
        ),
    )
    call_agent = Mock(return_value='{"status":"APPROVED","feedback":[]}')
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_reviewing_code()

    assert (
        "Git Status (includes untracked files):\nstdout:\n?? docs/new.md"
        in call_agent.call_args.args[1]
    )
    assert "Untracked File Diffs:\nnew file contents" in call_agent.call_args.args[1]


def test_step_reviewing_code_rejected_revises_until_max(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text("tests", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "change app",
            "target_files": ["app.py"],
            "status": "completed",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value='{"status":"REJECTED","feedback":["fix it"]}'),
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["code_revisions"] == 1
    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["tasks"][-1]["id"] == "FIX-REV-1"
    assert initialized_orchestrator.state["tasks"][-1]["rd_level"] == "senior"
    assert initialized_orchestrator.state["tasks"][-1]["qa_level"] == "senior"
    assert initialized_orchestrator.state["tasks"][-1]["target_files"] == ["app.py"]


def test_step_reviewing_code_rejected_at_max_revisions_escalates_then_pauses(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text("tests", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "target_files": ["app.py"],
            "status": "completed",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["code_revisions"] = initialized_orchestrator.config[
        "max_revisions"
    ]
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value='{"status":"REJECTED","feedback":["fix it"]}'),
    )
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["state"] == "DEVELOPING_PLAN"
    assert initialized_orchestrator.state["plan_escalations"] == 1
    assert initialized_orchestrator.state["code_revisions"] == 0

    initialized_orchestrator.state["code_revisions"] = initialized_orchestrator.config[
        "max_revisions"
    ]
    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"


def test_setup_worktree_use_worktree_false_returns_without_action(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.has_git = True
    cleanup = Mock()
    run = Mock()
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", cleanup)
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.setup_worktree()

    cleanup.assert_not_called()
    run.assert_not_called()


def test_setup_worktree_no_git_returns_without_action(
    initialized_orchestrator, monkeypatch
):
    cleanup = Mock()
    run = Mock()
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", cleanup)
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.setup_worktree()

    cleanup.assert_not_called()
    run.assert_not_called()


def test_setup_worktree_cleans_up_before_git_worktree_add(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    events = []
    monkeypatch.setattr(
        initialized_orchestrator, "cleanup_worktree", lambda: events.append("cleanup") or True
    )
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


def test_setup_worktree_failing_add_raises_runtime_error(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.has_git = True
    cleanup = Mock()
    run = Mock(return_value=(1, "nope"))
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", cleanup)
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    with pytest.raises(RuntimeError, match="Git worktree setup failed"):
        initialized_orchestrator.setup_worktree()

    cleanup.assert_called_once_with()


def test_cleanup_worktree_use_worktree_false_returns_without_action(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["use_worktree"] = False
    initialized_orchestrator.has_git = True
    run = Mock()
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree()

    run.assert_not_called()


def test_cleanup_worktree_no_git_returns_without_action(
    initialized_orchestrator, monkeypatch
):
    run = Mock()
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree()

    run.assert_not_called()


def test_cleanup_worktree_merge_false_removes_listed_worktree_and_deletes_branch(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    run = Mock(return_value=(0, f"stdout:\nworktree {worktree}\nbranch refs/heads/ai-feature-branch\n\nstderr:\n"))
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree(merge=False)

    assert run.call_args_list == [
        call(["git", "worktree", "list", "--porcelain"], cwd=initialized_orchestrator.workspace),
        call(
            ["git", "worktree", "remove", str(worktree)],
            cwd=initialized_orchestrator.workspace,
        ),
        call(
            ["git", "branch", "-d", "ai-feature-branch"],
            cwd=initialized_orchestrator.workspace,
        ),
    ]


def test_cleanup_worktree_skips_remove_when_worktree_list_has_no_worktree(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.has_git = True
    run = Mock(return_value=(0, "stdout:\nworktree /repo\nbranch refs/heads/master\n\nstderr:\n"))
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree()

    assert run.call_args_list == [
        call(["git", "worktree", "list", "--porcelain"], cwd=initialized_orchestrator.workspace),
    ]


def test_cleanup_worktree_preserves_path_owned_by_another_branch(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    worktree.mkdir()
    sentinel = worktree / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    run = Mock(return_value=(
        0, f"stdout:\nworktree {worktree}\nbranch refs/heads/user-branch\n\nstderr:\n"
    ))
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    assert initialized_orchestrator.cleanup_worktree() is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    run.assert_called_once_with(
        ["git", "worktree", "list", "--porcelain"],
        cwd=initialized_orchestrator.workspace,
    )


def test_cleanup_worktree_stops_if_remove_fails(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    run = Mock(side_effect=[
        (0, f"stdout:\nworktree {worktree}\nbranch refs/heads/ai-feature-branch\n\nstderr:\n"),
        (1, "dirty worktree"),
    ])
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    assert initialized_orchestrator.cleanup_worktree() is False
    assert run.call_count == 2
    assert run.call_args_list[-1] == call(
        ["git", "worktree", "remove", str(worktree)],
        cwd=initialized_orchestrator.workspace,
    )


def test_reset_keeps_state_when_worktree_cleanup_fails(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["tasks"] = [{"id": "T-1"}]
    before = copy.deepcopy(initialized_orchestrator.state)
    monkeypatch.setattr(initialized_orchestrator, "cleanup_worktree", Mock(return_value=False))

    with pytest.raises(RuntimeError, match="run state was preserved"):
        orchestrator_main.clear_run_state(initialized_orchestrator, "PLANNING")

    assert initialized_orchestrator.state == before


def test_cleanup_worktree_merge_success_runs_merge_then_cleanup(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    run = Mock(
        side_effect=[
            (0, f"stdout:\nworktree {worktree}\nbranch refs/heads/ai-feature-branch\n\nstderr:\n"),
            (0, ""),
            (0, "stdout:\n M app.py\nstderr:\n"),
            (0, ""),
            (0, ""),
            (0, "merged"),
            (0, ""),
            (0, ""),
        ]
    )
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree(merge=True)

    assert run.call_args_list == [
        call(["git", "worktree", "list", "--porcelain"], cwd=initialized_orchestrator.workspace),
        call(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/ai-feature-branch"],
            cwd=initialized_orchestrator.workspace,
        ),
        call(["git", "status", "--porcelain"], cwd=worktree),
        call(["git", "add", "."], cwd=worktree),
        call(["git", "commit", "-m", "AI Auto-commit before merge"], cwd=worktree),
        call(
            ["git", "merge", "ai-feature-branch"],
            cwd=initialized_orchestrator.workspace,
        ),
        call(
            ["git", "worktree", "remove", str(worktree)],
            cwd=initialized_orchestrator.workspace,
        ),
        call(
            ["git", "branch", "-d", "ai-feature-branch"],
            cwd=initialized_orchestrator.workspace,
        ),
    ]


def test_cleanup_worktree_merge_failure_stops_before_cleanup(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    run = Mock(
        side_effect=[
            (0, f"stdout:\nworktree {worktree}\nbranch refs/heads/ai-feature-branch\n\nstderr:\n"),
            (0, ""),
            (0, "stdout:\n M app.py\nstderr:\n"),
            (0, ""),
            (0, ""),
            (1, "conflict"),
            (0, ""),
            (0, ""),
        ]
    )
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree(merge=True)

    assert run.call_args_list == [
        call(["git", "worktree", "list", "--porcelain"], cwd=initialized_orchestrator.workspace),
        call(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/ai-feature-branch"],
            cwd=initialized_orchestrator.workspace,
        ),
        call(["git", "status", "--porcelain"], cwd=worktree),
        call(["git", "add", "."], cwd=worktree),
        call(["git", "commit", "-m", "AI Auto-commit before merge"], cwd=worktree),
        call(
            ["git", "merge", "ai-feature-branch"],
            cwd=initialized_orchestrator.workspace,
        ),
        call(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=initialized_orchestrator.workspace,
        ),
        call(["git", "merge", "--abort"], cwd=initialized_orchestrator.workspace),
    ]


def test_cleanup_worktree_merge_failure_aborts_and_records_conflict(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.has_git = True
    worktree = initialized_orchestrator.ai_dir / "worktree"
    merge_output = "stdout:\nCONFLICT (add/add): Merge conflict in docs/litellm-telemetry.md\nstderr:\n"
    run = Mock(
        side_effect=[
            (0, f"stdout:\nworktree {worktree}\nbranch refs/heads/ai-feature-branch\n\nstderr:\n"),
            (0, ""),
            (0, "stdout:\n M app.py\nstderr:\n"),
            (0, ""),
            (0, ""),
            (1, merge_output),
            (0, "stdout:\ndocs/litellm-telemetry.md\nstderr:\n"),
            (0, ""),
        ]
    )
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    assert initialized_orchestrator.cleanup_worktree(merge=True) is False

    assert initialized_orchestrator.state["worktree_conflict_files"] == [
        "docs/litellm-telemetry.md"
    ]
    assert initialized_orchestrator.state["worktree_merge_error"] == merge_output
    assert run.call_args_list[-1] == call(
        ["git", "merge", "--abort"], cwd=initialized_orchestrator.workspace
    )


def test_manager_queues_rd_task_after_worktree_merge_failure(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.request_path.write_text("request", encoding="utf-8")
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.has_git = False
    initialized_orchestrator.state["worktree_conflict_files"] = [
        "docs/litellm-telemetry.md"
    ]
    initialized_orchestrator.state["worktree_merge_error"] = "merge conflict"
    monkeypatch.setattr(
        initialized_orchestrator.manager, "call_manager", Mock(return_value="summary")
    )
    monkeypatch.setattr(
        initialized_orchestrator.assistant,
        "generate_meeting_memory",
        Mock(return_value="memory"),
    )
    monkeypatch.setattr(
        initialized_orchestrator.assistant, "generate_changelog", Mock(return_value="")
    )
    monkeypatch.setattr(
        initialized_orchestrator, "cleanup_worktree", Mock(return_value=False)
    )

    initialized_orchestrator.step_completed()

    task = initialized_orchestrator.state["tasks"][-1]
    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert task["id"] == "FIX-MERGE-1"
    assert task["status"] == "pending"
    assert task["target_files"] == ["docs/litellm-telemetry.md"]
    assert "merge conflict" in task["description"]


# --- Auto-weighting: scoring.py ---


def test_scoring_clamp_bounds():
    assert scoring.clamp(10) == scoring.WEIGHT_MAX
    assert scoring.clamp(-5) == scoring.WEIGHT_MIN
    assert scoring.clamp(1.5) == 1.5


def test_record_staff_event_moves_weight_and_persists(initialized_orchestrator):
    initialized_orchestrator.config["worker_weights"] = {
        "rd": {"senior": 1.0, "middle": 1.0, "junior": 1.0}
    }

    updated = scoring.record_staff_event(initialized_orchestrator, "rd", "junior", 1.0)

    assert updated > 1.0
    assert initialized_orchestrator.config["worker_weights"]["rd"]["junior"] == updated
    persisted = json.loads(
        initialized_orchestrator.config_path.read_text(encoding="utf-8")
    )
    assert persisted["worker_weights"]["rd"]["junior"] == updated


def test_record_vendor_event_tracks_jobs_and_failures(initialized_orchestrator):
    scoring.record_vendor_event(initialized_orchestrator, "codex", "gpt-5.6-luna", 1.0)
    scoring.record_vendor_event(initialized_orchestrator, "codex", "gpt-5.6-luna", -1.0)

    entry = initialized_orchestrator.config["vendor_scores"]["codex/gpt-5.6-luna"]
    assert entry["jobs"] == 2
    assert entry["failures"] == 1


def test_record_event_routes_ollama_to_staff_and_others_to_vendor(
    initialized_orchestrator,
):
    scoring.record_event(
        initialized_orchestrator, "rd", "senior", "ollama", "gemma4:latest", 1.0
    )
    scoring.record_event(
        initialized_orchestrator, "rd", "senior", "codex", "gpt-5.6-luna", -1.0
    )

    assert "senior" in initialized_orchestrator.config["worker_weights"]["rd"]
    assert "codex/gpt-5.6-luna" in initialized_orchestrator.config["vendor_scores"]


# --- Auto-weighting: QA weighted voting ---


def test_qa_weighted_vote_overrides_single_junior_dissent(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "x",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "qa": {"senior": 1, "middle": 1, "junior": 1}
    }
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(0, "tests ok"))
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            side_effect=[
                "QA_STATUS: PASSED\nok",
                "QA_STATUS: PASSED\nok",
                "QA_STATUS: FAILED\nnitpick",
            ]
        ),
    )

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "REVIEWING_CODE"


def test_qa_weighted_vote_senior_dissent_blocks_even_with_majority_pass(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "x",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "qa": {"senior": 1, "middle": 1, "junior": 1}
    }
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(0, "tests ok"))
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(
            side_effect=[
                "QA_STATUS: FAILED\nrisk",
                "QA_STATUS: PASSED\nok",
                "QA_STATUS: PASSED\nok",
            ]
        ),
    )

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["qa_dissenting_levels"] == ["senior"]


def test_qa_hard_fails_on_nonzero_exit_even_if_all_workers_pass(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "x",
            "status": "completed",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {"qa": {"senior": 1}}
    monkeypatch.setattr(
        initialized_orchestrator, "run_command", Mock(return_value=(1, "boom"))
    )
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent",
        Mock(return_value="QA_STATUS: PASSED\nok"),
    )

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"


# --- Auto-weighting: roster ---


def test_roster_snapshot_marks_ollama_staff_offline_when_unreachable(
    initialized_orchestrator,
):
    initialized_orchestrator.state["ollama_reachable"] = False

    data = roster.snapshot(initialized_orchestrator)

    assert data["available_roles"]["developer_junior"]["status"] == "offline"
    assert data["available_roles"]["developer_junior"]["type"] == "staff"
    assert "ollama" in data["unavailable_backends"]


def test_roster_snapshot_marks_vendor_offline_when_quota_exhausted(
    initialized_orchestrator,
):
    initialized_orchestrator.state["quota_exhausted_backends"] = {"codex": True}

    data = roster.snapshot(initialized_orchestrator)

    assert data["available_roles"]["developer_senior"]["status"] == "offline"
    assert data["available_roles"]["developer_senior"]["type"] == "vendor"


def test_reassign_unavailable_levels_falls_back_to_available_level():
    tasks = [{"id": "T-1", "rd_level": "junior"}]
    roster_data = {
        "available_roles": {
            "developer_junior": {"status": "offline"},
            "developer_middle": {"status": "offline"},
            "developer_senior": {"status": "available"},
        }
    }

    roster.reassign_unavailable_levels(tasks, roster_data, "rd_level", "developer")

    assert tasks[0]["rd_level"] == "senior"


def test_developing_plan_reassigns_task_off_offline_rd_level(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.state["ollama_reachable"] = False
    monkeypatch.setattr(
        initialized_orchestrator,
        "call_manager",
        Mock(
            return_value=json.dumps(
                {
                    "tasks": [
                        {
                            "id": "T-1",
                            "description": "implement",
                            "target_files": ["app.py"],
                            "rd_level": "junior",
                            "qa_level": "junior",
                        }
                    ],
                    "staffing": {
                        "rd": {"junior": 1, "senior": 1},
                        "qa": {"junior": 1, "senior": 1},
                    },
                    "specialists": [],
                }
            )
        ),
    )

    initialized_orchestrator.step_developing_plan()

    assert initialized_orchestrator.state["tasks"][0]["rd_level"] == "senior"


# --- Auto-weighting: vendor firefighting ---


def test_maybe_hire_vendor_fires_when_struggling_and_sets_override(
    initialized_orchestrator,
):
    initialized_orchestrator.state["last_developer_role"] = "developer_junior"
    initialized_orchestrator.state["active_task_id"] = "T-1"
    task = {
        "id": "T-1",
        "revisions": initialized_orchestrator.config["max_revisions"] - 1,
    }

    hired = initialized_orchestrator.maybe_hire_vendor(task)

    assert hired in {"agy", "codex"}
    assert initialized_orchestrator.state["task_backend_overrides"]["T-1"] == hired


def test_maybe_hire_vendor_noop_for_vendor_backed_role(initialized_orchestrator):
    initialized_orchestrator.state["last_developer_role"] = "developer_senior"
    task = {"id": "T-1", "revisions": 5}

    assert initialized_orchestrator.maybe_hire_vendor(task) is None
    assert "T-1" not in initialized_orchestrator.state.get("task_backend_overrides", {})


# --- Auto-weighting: proactive vendor quota probe ---

AGY_CREDITS_DISABLED_SAMPLE = """
Model AI Credits

Remaining AI Credits: AI Credits not enabled (enable in /settings)
"""

AGY_CREDITS_EXHAUSTED_SAMPLE = """
Model AI Credits

Remaining AI Credits: 0
"""


def test_parse_vendor_quota_pct_agy_credits_not_enabled_is_unknown():
    assert backends.parse_vendor_quota_pct("agy", AGY_CREDITS_DISABLED_SAMPLE) is None


def test_parse_vendor_quota_pct_agy_zero_credits_is_exhausted():
    assert backends.parse_vendor_quota_pct("agy", AGY_CREDITS_EXHAUSTED_SAMPLE) == 100


def test_parse_vendor_quota_pct_unrecognized_text_is_unknown():
    assert backends.parse_vendor_quota_pct("codex", "no idea what this is") is None


def test_check_vendor_quota_is_a_noop_when_disabled(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["vendor_quota_check_enabled"] = False
    probe = Mock()
    monkeypatch.setattr(backends, "_probe_vendor_quota_pane", probe)

    assert backends.check_vendor_quota(initialized_orchestrator, "codex") is None
    probe.assert_not_called()


def test_check_vendor_quota_flags_exhausted_vendor(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["vendor_quota_check_enabled"] = True
    monkeypatch.setattr(backends.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        backends,
        "_probe_vendor_quota_pane",
        Mock(return_value=AGY_CREDITS_EXHAUSTED_SAMPLE),
    )

    assert backends.check_vendor_quota(initialized_orchestrator, "agy") is False


def test_maybe_hire_vendor_skips_exhausted_vendor_and_hires_next(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.state["last_developer_role"] = "developer_junior"
    initialized_orchestrator.state["active_task_id"] = "T-1"
    task = {
        "id": "T-1",
        "revisions": initialized_orchestrator.config["max_revisions"] - 1,
    }
    monkeypatch.setattr(
        scoring,
        "get_vendor_rating",
        lambda config, backend, model: {"codex": 2.0, "agy": 1.0}.get(backend, 0.0),
    )
    monkeypatch.setattr(
        backends, "check_vendor_quota", lambda orch, backend: backend != "codex"
    )

    hired = initialized_orchestrator.maybe_hire_vendor(task)

    assert hired == "agy"
    assert initialized_orchestrator.state["quota_exhausted_backends"]["codex"] is True
    assert initialized_orchestrator.state["task_backend_overrides"]["T-1"] == "agy"


def test_task_backend_override_wins_for_developer_role(initialized_orchestrator):
    initialized_orchestrator.state["active_task_id"] = "T-1"
    initialized_orchestrator.state["task_backend_overrides"] = {"T-1": "agy"}

    assert initialized_orchestrator.get_backend("developer_junior") == "agy"


# --- Auto-weighting: manager weight review ---


def test_manager_review_worker_weights_applies_clamped_adjustment(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["worker_weights"] = {
        "rd": {"senior": 1.0, "middle": 1.0, "junior": 1.0},
        "qa": {"senior": 1.5, "middle": 1.0, "junior": 0.6},
    }
    monkeypatch.setattr(
        initialized_orchestrator.manager,
        "call_manager",
        Mock(
            return_value=json.dumps(
                {
                    "adjustments": [
                        {
                            "role": "rd",
                            "level": "junior",
                            "weight": 2.9,
                            "reason": "great run",
                        }
                    ]
                }
            )
        ),
    )

    initialized_orchestrator.manager._review_worker_weights("telemetry")

    assert initialized_orchestrator.config["worker_weights"]["rd"]["junior"] == 1.3


def test_manager_review_worker_weights_skips_on_invalid_response(
    initialized_orchestrator, monkeypatch
):
    original = copy.deepcopy(initialized_orchestrator.config["worker_weights"])
    monkeypatch.setattr(
        initialized_orchestrator.manager, "call_manager", Mock(return_value="not json")
    )

    initialized_orchestrator.manager._review_worker_weights("telemetry")

    assert initialized_orchestrator.config["worker_weights"] == original


# --- Auto-weighting: brain (SQLite+FTS5 company memory) ---


def test_brain_remember_and_recall_roundtrip(initialized_orchestrator):
    brain.remember(
        initialized_orchestrator,
        "lesson",
        "Task T-1 failed due to missing import handling",
    )

    results = brain.recall(initialized_orchestrator, "missing import handling")

    assert any("missing import handling" in r for r in results)


def test_brain_recall_returns_nothing_for_unrelated_query(initialized_orchestrator):
    brain.remember(
        initialized_orchestrator, "lesson", "Task T-1 failed due to a timeout"
    )

    assert brain.recall(initialized_orchestrator, "unrelated banana pancake") == []


def test_brain_prune_keeps_only_most_recent(initialized_orchestrator):
    for i in range(5):
        brain.remember(initialized_orchestrator, "lesson", f"lesson number {i}")

    deleted = brain.prune(initialized_orchestrator, keep_last=2)

    assert deleted == 3
    assert len(brain.recall(initialized_orchestrator, "lesson", limit=10)) == 2


def test_brain_injected_for_ollama_backend_prompt(
    initialized_orchestrator, monkeypatch
):
    brain.remember(initialized_orchestrator, "lesson", "Remember to check CSV encoding")
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.config["backends"]["developer_junior"] = "ollama"
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "check CSV encoding",
            "target_files": ["app.py"],
            "status": "pending",
            "rd_level": "junior",
            "qa_level": "junior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"junior": 1},
        "qa": {"junior": 1},
    }
    call_agent = Mock(return_value="[FILE_START: app.py]\nok\n[FILE_END: app.py]")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    prompt = call_agent.call_args.args[1]
    assert "[COMPANY_MEMORY]" in prompt
    assert "CSV encoding" in prompt


def test_brain_not_injected_for_vendor_backend_prompt(
    initialized_orchestrator, monkeypatch
):
    brain.remember(initialized_orchestrator, "lesson", "Remember to check CSV encoding")
    initialized_orchestrator.requirements_path.write_text(
        "requirements", encoding="utf-8"
    )
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.config["backends"]["developer_senior"] = "codex"
    initialized_orchestrator.state["tasks"] = [
        {
            "id": "T-1",
            "description": "check CSV encoding",
            "target_files": ["app.py"],
            "status": "pending",
            "rd_level": "senior",
            "qa_level": "senior",
        }
    ]
    initialized_orchestrator.state["staffing"] = {
        "rd": {"senior": 1},
        "qa": {"senior": 1},
    }
    call_agent = Mock(return_value="No edits needed")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    prompt = call_agent.call_args.args[1]
    assert "[COMPANY_MEMORY]" not in prompt


def test_discover_installed_ollama_models_success_and_cache(
    initialized_orchestrator, monkeypatch
):
    get_mock = Mock(
        return_value=Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "models": [
                        {"name": "tev1:4b"},
                        {"name": "granite4.2:8b"},
                        {"name": "gemma4:latest"},
                    ]
                }
            ),
        )
    )
    monkeypatch.setattr(backends.requests, "get", get_mock)

    models = backends.discover_installed_ollama_models(initialized_orchestrator)
    assert models == {"tev1:4b", "granite4.2:8b", "gemma4:latest"}
    assert get_mock.call_count == 1

    # Second call uses cache and does not repeat request
    models_cached = backends.discover_installed_ollama_models(initialized_orchestrator)
    assert models_cached == {"tev1:4b", "granite4.2:8b", "gemma4:latest"}
    assert get_mock.call_count == 1


def test_discover_installed_ollama_models_error_handling(
    initialized_orchestrator, monkeypatch
):
    get_mock = Mock(side_effect=backends.requests.RequestException("connection refused"))
    monkeypatch.setattr(backends.requests, "get", get_mock)

    models = backends.discover_installed_ollama_models(initialized_orchestrator)
    assert models == set()


def test_route_ollama_model_with_tev1_selects_candidate(
    initialized_orchestrator, monkeypatch
):
    monkeypatch.setattr(
        backends,
        "discover_installed_ollama_models",
        Mock(return_value={"tev1:4b", "granite4.2:8b", "qwen3.5:9b"}),
    )

    post_mock = Mock(
        return_value=Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "answers": {
                        "selected_model": {
                            "choice": "qwen3.5:9b",
                            "confidence": 0.88,
                        }
                    }
                }
            ),
        )
    )
    monkeypatch.setattr(backends.requests, "post", post_mock)

    chosen = backends.route_ollama_model_with_tev1(
        initialized_orchestrator,
        role="developer_middle",
        candidates=["granite4.2:8b", "qwen3.5:9b"],
        task_summary="Refactor parser module",
    )
    assert chosen == "qwen3.5:9b"

    payload = post_mock.call_args.kwargs["json"]
    assert payload["model"] == "tev1:4b"
    assert "Refactor parser module" in payload["state"]
    assert payload["questions"]["selected_model"]["criteria"] == [
        "granite4.2:8b",
        "qwen3.5:9b",
    ]


def test_route_ollama_model_with_tev1_fallback_conditions(
    initialized_orchestrator, monkeypatch
):
    monkeypatch.setattr(
        backends,
        "discover_installed_ollama_models",
        Mock(return_value={"tev1:4b", "granite4.2:8b", "qwen3.5:9b"}),
    )

    # 1. Low confidence (< 0.5)
    post_low_conf = Mock(
        return_value=Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "answers": {
                        "selected_model": {
                            "choice": "qwen3.5:9b",
                            "confidence": 0.35,
                        }
                    }
                }
            ),
        )
    )
    monkeypatch.setattr(backends.requests, "post", post_low_conf)
    assert (
        backends.route_ollama_model_with_tev1(
            initialized_orchestrator,
            role="developer_middle",
            candidates=["granite4.2:8b", "qwen3.5:9b"],
        )
        is None
    )

    # 2. Choice not in available candidates
    post_invalid_choice = Mock(
        return_value=Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "answers": {
                        "selected_model": {
                            "choice": "unauthorized-model:latest",
                            "confidence": 0.99,
                        }
                    }
                }
            ),
        )
    )
    monkeypatch.setattr(backends.requests, "post", post_invalid_choice)
    assert (
        backends.route_ollama_model_with_tev1(
            initialized_orchestrator,
            role="developer_middle",
            candidates=["granite4.2:8b", "qwen3.5:9b"],
        )
        is None
    )

    # 3. HTTP 404 / 500 endpoint error
    post_error = Mock(
        return_value=Mock(status_code=404, json=Mock(return_value={}))
    )
    monkeypatch.setattr(backends.requests, "post", post_error)
    assert (
        backends.route_ollama_model_with_tev1(
            initialized_orchestrator,
            role="developer_middle",
            candidates=["granite4.2:8b", "qwen3.5:9b"],
        )
        is None
    )


def test_call_ollama_strictly_rejects_tev1_as_executor(initialized_orchestrator):
    with pytest.raises(RuntimeError, match="restricted to decision routing"):
        backends.call_ollama(
            initialized_orchestrator,
            prompt="write code",
            role="developer",
            model="tev1:4b",
        )


def test_call_role_model_routes_prioritizes_tev1_decision(
    initialized_orchestrator, monkeypatch
):
    initialized_orchestrator.config["role_model_routes"]["developer_middle"] = [
        ["ollama", "granite4.2:8b"],
        ["ollama", "qwen3.5:9b"],
        ["ollama", "llama3.1:8b"],
    ]

    monkeypatch.setattr(
        backends,
        "route_ollama_model_with_tev1",
        Mock(return_value="llama3.1:8b"),
    )

    called_models = []

    def mock_ollama_fallback(role, prompt, system_prompt=None, model=None, **kwargs):
        called_models.append(model)
        return "response from " + str(model)

    monkeypatch.setattr(
        initialized_orchestrator,
        "call_agent_ollama_fallback",
        mock_ollama_fallback,
    )

    res = initialized_orchestrator.call_role_model_routes(
        "developer_middle", "A long full prompt with implementation instructions"
    )
    assert res == "response from llama3.1:8b"
    assert called_models[0] == "llama3.1:8b"
