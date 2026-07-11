from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest

import orchestrator
from orchestrator import DEFAULT_CONFIG, AgentOrchestrator
from orchestrator.core.backends import quota_exhausted


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


def test_constructor_non_git_output_is_safe_default(tmp_path, monkeypatch):
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        Mock(return_value=Mock(returncode=0, stdout="false\n")),
    )

    app = AgentOrchestrator(tmp_path)

    assert app.has_git is False
    assert app.base_branch == "master"


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
        },
        timeout=1800,
    )
    response.raise_for_status.assert_called_once_with()


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


def test_staffing_is_capped_by_configured_capacity(initialized_orchestrator):
    initialized_orchestrator.state["staffing"] = {"qa": {"senior": 4, "junior": -1}}

    assert initialized_orchestrator.staffing("qa") == {"senior": 1, "middle": 0, "junior": 0}


def test_role_models_select_the_configured_seniority_model(initialized_orchestrator):
    assert initialized_orchestrator.get_active_model_for_role("developer_senior", "grok") == "grok-4.5"
    assert initialized_orchestrator.get_active_model_for_role("developer_middle", "grok") == "grok-4.5"
    assert initialized_orchestrator.get_active_model_for_role("developer_junior", "ollama") == "codegemma:7b"
    assert initialized_orchestrator.get_active_model_for_role("architect", "ollama") == "gemma4:latest"
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
    assert app.get_active_model_for_role("reviewer", "codex") == "gpt-5.6-sol"
    assert app.config["ollama_keep_alive"] == 0


def test_role_model_is_not_used_for_an_ollama_fallback(initialized_orchestrator):
    assert initialized_orchestrator.get_active_model_for_role("manager", "ollama") == "gemma4:latest"


def test_developer_plan_does_not_require_file_blocks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.state["state"] = "DEVELOPING_PLAN"
    initialized_orchestrator.config["role_model_routes"]["developer_senior"] = [["grok", "grok-4.5"]]
    monkeypatch.setattr(initialized_orchestrator, "call_grok", Mock(return_value="# Plan"))

    assert initialized_orchestrator.call_agent("developer_senior", "prompt") == "# Plan"


def test_implementing_prompt_requires_only_file_blocks(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "write hello", "status": "pending", "rd_level": "junior", "qa_level": "junior"}]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    call_agent = Mock(return_value="[FILE_START: hello.py]\nprint('hello')\n[FILE_END: hello.py]")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", call_agent)

    initialized_orchestrator.step_implementing()

    assert "respond only with [file_start" in call_agent.call_args.args[2].lower()


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


def test_implementing_pauses_when_all_model_routes_fail(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.state["tasks"] = [{"id": "T-1", "description": "write hello", "target_files": ["hello.py"], "status": "pending", "rd_level": "junior", "qa_level": "junior"}]
    initialized_orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(side_effect=RuntimeError("all routes failed")))

    initialized_orchestrator.step_implementing()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "Developer"


def test_run_to_end_pauses_instead_of_raising_on_unhandled_error(initialized_orchestrator, monkeypatch):
    monkeypatch.setattr(initialized_orchestrator, "step", Mock(side_effect=RuntimeError("unexpected failure")))

    initialized_orchestrator.run_to_end()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"
    assert initialized_orchestrator.state["human_review_source"] == "Orchestrator"


def test_manager_retries_terra_then_ollama_after_token_failure(initialized_orchestrator, monkeypatch):
    codex = Mock(side_effect=RuntimeError("maximum context length"))
    agy = Mock(return_value="fallback")
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)
    monkeypatch.setattr(initialized_orchestrator, "call_agy", agy)

    assert initialized_orchestrator.call_manager("prompt") == "fallback"
    codex.assert_called_once_with("prompt", None, role="manager", model="gpt-5.6-sol")
    agy.assert_called_once_with("prompt", None, role="manager", model="gemini-3.5-flash")


def test_reviewer_retries_terra_then_ollama_after_token_failure(initialized_orchestrator, monkeypatch):
    codex = Mock(side_effect=RuntimeError("maximum context length"))
    ollama = Mock(return_value="fallback")
    monkeypatch.setattr(initialized_orchestrator, "call_codex", codex)
    monkeypatch.setattr(initialized_orchestrator, "call_agent_ollama_fallback", ollama)

    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "fallback"
    codex.assert_called_once_with("prompt", None, role="reviewer", model="gpt-5.6-sol")
    ollama.assert_called_once_with("reviewer", "prompt", None, model="deepseek-coder:6.7b")


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
    assert initialized_orchestrator.token_fallback_model("manager", RuntimeError("maximum context length")) == "gpt-5.6-terra"
    assert initialized_orchestrator.get_active_model_for_role("manager", "codex") == "gpt-5.6-sol"
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
        ["codex", "gpt-5.6-sol"], ["agy", "gpt-oss-120b"]
    ]

    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "agy fallback"
    assert initialized_orchestrator.call_agent("reviewer", "prompt") == "agy fallback"

    codex.assert_called_once_with("prompt", None, role="reviewer", model="gpt-5.6-sol")
    assert agy.call_args_list == [call("prompt", None, role="reviewer", model="gpt-oss-120b")] * 2


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
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="[APPROVED]\nok"))

    initialized_orchestrator.step_reviewing_plan()

    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"


def test_step_reviewing_plan_rejected_revises_until_max(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="REJECTED\nfix it"))
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
    monkeypatch.setattr(initialized_orchestrator, "run_command", Mock(return_value=(1, "tests failed")))
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="FAILED\nfix it"))
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["code_revisions"] == 1
    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["tasks"][-1]["id"] == "FIX-QA-1"
    assert initialized_orchestrator.state["tasks"][-1]["rd_level"] == "senior"
    assert initialized_orchestrator.state["tasks"][-1]["qa_level"] == "senior"

    initialized_orchestrator.state["code_revisions"] = initialized_orchestrator.config["max_revisions"]
    initialized_orchestrator.step_testing()

    assert initialized_orchestrator.state["state"] == "WAITING_FOR_OWNER"


def test_step_reviewing_code_approved_moves_to_completed(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text("tests", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="APPROVED\nok"))

    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["state"] == "COMPLETED"


def test_step_reviewing_code_rejected_revises_until_max(initialized_orchestrator, monkeypatch):
    initialized_orchestrator.requirements_path.write_text("requirements", encoding="utf-8")
    initialized_orchestrator.plan_path.write_text("plan", encoding="utf-8")
    initialized_orchestrator.test_results_path.write_text("tests", encoding="utf-8")
    monkeypatch.setattr(initialized_orchestrator, "call_agent", Mock(return_value="REJECTED\nfix it"))
    monkeypatch.setattr(initialized_orchestrator, "escalate_developer_backend", Mock())

    initialized_orchestrator.step_reviewing_code()

    assert initialized_orchestrator.state["code_revisions"] == 1
    assert initialized_orchestrator.state["state"] == "IMPLEMENTING"
    assert initialized_orchestrator.state["tasks"][-1]["id"] == "FIX-REV-1"
    assert initialized_orchestrator.state["tasks"][-1]["rd_level"] == "senior"
    assert initialized_orchestrator.state["tasks"][-1]["qa_level"] == "senior"

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
    ])
    monkeypatch.setattr(initialized_orchestrator, "run_command", run)

    initialized_orchestrator.cleanup_worktree(merge=True)

    assert run.call_args_list == [
        call(["git", "show-ref", "--verify", "--quiet", "refs/heads/ai-feature-branch"], cwd=initialized_orchestrator.workspace),
        call(["git", "add", "."], cwd=worktree),
        call(["git", "commit", "-m", "AI Auto-commit before merge"], cwd=worktree),
        call(["git", "merge", "ai-feature-branch"], cwd=initialized_orchestrator.workspace),
    ]
