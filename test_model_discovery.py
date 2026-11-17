"""Exercise catalog recovery without contacting model providers or live workflows."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from orchestrator.core import backends, cloud_routing
from orchestrator.core.state import ImplementationApprovalError
from orchestrator.main import apply_continue
from test_implementation_evidence import implementation_app
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


@pytest.fixture(autouse=True)
def isolated_catalogs(monkeypatch):
    monkeypatch.setattr(cloud_routing, "_codex_catalog_cache", None)
    monkeypatch.setattr(cloud_routing, "_catalog_failures", {})
    monkeypatch.setattr(backends, "_agy_models_cache", None)
    monkeypatch.setattr(backends, "_tev1_cooldowns", {})


@pytest.fixture
def clock(monkeypatch):
    value = Mock(return_value=0.0)
    monkeypatch.setattr(cloud_routing.time, "monotonic", value)
    return value


def catalog_output(provider, model):
    if provider == "agy":
        return f"{model}\tDisplay name\n"
    return json.dumps(
        {"models": [{"slug": model, "visibility": "list", "supported_in_api": True}]}
    )


def discover(provider):
    if provider == "codex":
        return cloud_routing.discover_codex_models()
    return backends.resolve_agy_model("auto:gemini-flash-medium")


@pytest.mark.parametrize("provider", ["codex", "agy"])
@pytest.mark.parametrize("kind", ["timeout", "exit", "format", "command"])
def test_discovery_failure_is_bounded_safe_and_recovers(
    provider, kind, clock, monkeypatch
):
    failure = {
        "timeout": subprocess.TimeoutExpired(
            "private-command", 60, output="secret instructions", stderr="secret token"
        ),
        "command": OSError("secret credential"),
        "exit": Mock(
            returncode=7, stdout="private instructions", stderr="secret quota token"
        ),
        "format": Mock(
            returncode=0, stdout="secret invalid catalog", stderr="secret credential"
        ),
    }[kind]
    run = (
        Mock(side_effect=failure)
        if isinstance(failure, Exception)
        else Mock(return_value=failure)
    )
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    for _ in range(3):
        with pytest.raises(cloud_routing.ModelDiscoveryError) as captured:
            discover(provider)
        assert captured.value.kind == kind
        assert "secret" not in str(captured.value)
        assert "instructions" not in str(captured.value)
        assert len(str(captured.value)) < 200
    assert run.call_count == (
        1 if kind == "timeout" else cloud_routing.DISCOVERY_ATTEMPTS
    )
    clock.return_value = cloud_routing.DISCOVERY_COOLDOWN
    model = "gpt-6.1-sol" if provider == "codex" else "gemini-3.8-flash-medium"
    run.side_effect = None
    run.return_value = Mock(returncode=0, stdout=catalog_output(provider, model))
    result = discover(provider)
    assert model in result
    assert provider not in cloud_routing._catalog_failures


@pytest.mark.parametrize("provider", ["codex", "agy"])
def test_expired_failure_keeps_last_success_but_never_uses_it(
    provider, clock, monkeypatch
):
    old_model = "gpt-6.1-sol" if provider == "codex" else "gemini-3.8-flash-medium"
    new_model = "gpt-9-sol" if provider == "codex" else "gemini-4.1-flash-medium"
    run = Mock(
        return_value=Mock(returncode=0, stdout=catalog_output(provider, old_model))
    )
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    discover(provider)
    original = (
        cloud_routing._codex_catalog_cache
        if provider == "codex"
        else backends._agy_models_cache
    )
    clock.return_value = cloud_routing.CATALOG_TTL - 1
    assert old_model in discover(provider)
    run.assert_called_once()
    clock.return_value = cloud_routing.CATALOG_TTL
    run.side_effect = subprocess.TimeoutExpired("models", 60)
    with pytest.raises(cloud_routing.ModelDiscoveryError, match="timeout"):
        discover(provider)
    saved = (
        cloud_routing._codex_catalog_cache
        if provider == "codex"
        else backends._agy_models_cache
    )
    assert saved is original
    with pytest.raises(cloud_routing.ModelDiscoveryError):
        discover(provider)
    assert run.call_count == 2
    clock.return_value += cloud_routing.DISCOVERY_COOLDOWN
    run.side_effect = None
    run.return_value = Mock(returncode=0, stdout=catalog_output(provider, new_model))
    refreshed = discover(provider)
    assert new_model in refreshed
    assert old_model not in refreshed


@pytest.mark.parametrize("provider", ["codex", "agy"])
def test_valid_empty_catalog_has_success_ttl(provider, clock, monkeypatch):
    output = '{"models": []}' if provider == "codex" else "\n"
    run = Mock(return_value=Mock(returncode=0, stdout=output))
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    for _ in range(2):
        if provider == "codex":
            assert discover(provider) == {}
        else:
            with pytest.raises(cloud_routing.ModelDiscoveryError) as captured:
                discover(provider)
            assert captured.value.kind == "empty"
    run.assert_called_once()
    assert not cloud_routing._catalog_failures
    clock.return_value = cloud_routing.CATALOG_TTL
    if provider == "codex":
        discover(provider)
    else:
        with pytest.raises(cloud_routing.ModelDiscoveryError):
            discover(provider)
    assert run.call_count == 2


@pytest.mark.parametrize(
    "provider,model", [("codex", "gpt-6-astra"), ("agy", "gemini-argon-high")]
)
def test_policy_exclusion_is_distinct_from_failed_discovery(
    provider, model, initialized_orchestrator, monkeypatch
):
    run = Mock(return_value=Mock(returncode=0, stdout=catalog_output(provider, model)))
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    with pytest.raises(cloud_routing.ModelDiscoveryError) as captured:
        if provider == "codex":
            cloud_routing.select_codex_model(
                initialized_orchestrator, "architect", "task"
            )
        else:
            discover(provider)
    assert captured.value.kind == "policy_excluded"
    assert not cloud_routing._catalog_failures
    run.assert_called_once()


@pytest.mark.parametrize(
    "output",
    [
        "null",
        "{}",
        '{"models": {}}',
        '{"models": [false]}',
        '{"models": [{"slug": [], "visibility": "list", "supported_in_api": true}]}',
    ],
)
def test_codex_rejects_invalid_schema_without_exposing_output(output, monkeypatch):
    monkeypatch.setattr(
        cloud_routing.subprocess,
        "run",
        Mock(return_value=Mock(returncode=0, stdout=output)),
    )
    with pytest.raises(cloud_routing.ModelDiscoveryError) as captured:
        cloud_routing.discover_codex_models()
    assert captured.value.kind == "format"
    assert cloud_routing._codex_catalog_cache is None


def test_agy_selector_propagates_discovery_failure_once(
    initialized_orchestrator, monkeypatch
):
    error = cloud_routing.ModelDiscoveryError("agy", "timeout", "command exceeded 60s")
    resolve = Mock(side_effect=error)
    monkeypatch.setattr(backends, "resolve_agy_model", resolve)
    with pytest.raises(cloud_routing.ModelDiscoveryError) as captured:
        cloud_routing.select_agy_model(
            initialized_orchestrator, "architect", "task", "auto:gemini-flash-medium"
        )
    assert captured.value is error
    resolve.assert_called_once()


def test_agy_no_matching_family_is_recoverable_and_lists_only_once(
    initialized_orchestrator, monkeypatch
):
    run = Mock(
        return_value=Mock(
            returncode=0, stdout=catalog_output("agy", "gemini-3.8-pro-low")
        )
    )
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    with pytest.raises(cloud_routing.ModelDiscoveryError) as captured:
        cloud_routing.select_agy_model(
            initialized_orchestrator, "architect", "task", "auto:gemini-flash-medium"
        )
    assert captured.value.kind == "no_match"
    run.assert_called_once()


def router_response(candidates):
    return Mock(
        status_code=200,
        json=Mock(
            return_value={
                "answers": {
                    "selected_model": {
                        "choice": candidates[0],
                        "confidence": 0.8,
                        "probabilities": {
                            model: 0.8
                            if model == candidates[0]
                            else 0.2 / (len(candidates) - 1)
                            for model in candidates
                        },
                    }
                }
            }
        ),
    )


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired("unused", 1),
        backends.requests.Timeout("secret token"),
        Mock(status_code=503, text="secret token"),
    ],
)
def test_tev1_cooldown_crosses_candidates_and_recovers_same_choice(
    failure, initialized_orchestrator, clock, monkeypatch
):
    app = initialized_orchestrator
    candidates = ["a", "b"]
    monkeypatch.setattr(
        backends,
        "discover_installed_ollama_models",
        Mock(return_value={"d3:4b", *candidates, "c"}),
    )
    post = (
        Mock(side_effect=failure)
        if isinstance(failure, Exception)
        else Mock(return_value=failure)
    )
    monkeypatch.setattr(backends.requests, "post", post)
    assert (
        backends.route_ollama_model_with_tev1(app, "architect", candidates, "summary")
        is None
    )
    assert (
        backends.route_ollama_model_with_tev1(app, "reviewer", ["b", "c"], "other")
        is None
    )
    post.assert_called_once()
    clock.return_value = backends.TEV1_COOLDOWN
    post.side_effect = None
    post.return_value = router_response(candidates)
    assert (
        backends.route_ollama_model_with_tev1(app, "architect", candidates, "summary")
        == candidates
    )
    assert post.call_count == 2


def test_tev1_endpoint_changes_do_not_reuse_cooldown_or_success(
    initialized_orchestrator, clock, monkeypatch
):
    app = initialized_orchestrator
    candidates = ["a", "b"]
    monkeypatch.setattr(
        backends,
        "discover_installed_ollama_models",
        Mock(return_value={"d3:4b", *candidates}),
    )
    post = Mock(
        side_effect=[
            backends.requests.Timeout(),
            router_response(candidates),
            router_response(candidates),
        ]
    )
    monkeypatch.setattr(backends.requests, "post", post)
    assert backends.route_ollama_model_with_tev1(app, "architect", candidates) is None
    app.config["ollama_url"] = "http://other-endpoint:11434"
    assert (
        backends.route_ollama_model_with_tev1(app, "architect", candidates)
        == candidates
    )
    app.config["ollama_url"] = "http://third-endpoint:11434"
    assert (
        backends.route_ollama_model_with_tev1(app, "architect", candidates)
        == candidates
    )
    assert post.call_count == 3


@pytest.mark.parametrize("kind", ["timeout", "empty", "policy_excluded", "no_match"])
def test_discovery_does_not_poison_routes_quota_or_model_budget(
    kind, initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.state.update(state="IMPLEMENTING", active_task_id="T-001")
    app.state["task_developer_calls"] = {"T-001": 2}
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["developer_middle"] = [["codex", "auto"]]
    error = cloud_routing.ModelDiscoveryError("codex", kind, "quota token limit 429")
    call = Mock(side_effect=error)
    monkeypatch.setattr(app, "call_codex", call)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="All configured routes failed"):
            app.call_role_model_routes("developer_middle", "task")
    assert app.state["task_developer_calls"]["T-001"] == 2
    assert not app.state["task_failed_model_routes"]["T-001"]
    assert not app.state["task_contract_failures"]["T-001"]
    assert not app.state["quota_exhausted_backends"]
    assert not backends.quota_exhausted(error)
    assert backends.token_fallback_model(app, "developer_middle", error) is None
    call.side_effect = None
    call.return_value = "[FILE_START: utility.py]\npass\n[FILE_END: utility.py]"
    assert app.call_role_model_routes("developer_middle", "task") == call.return_value
    assert app.state["task_developer_calls"]["T-001"] == 3
    assert call.call_count == 4


def test_real_model_failure_still_consumes_budget(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.state.update(state="IMPLEMENTING", active_task_id="T-001")
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["developer_middle"] = [["codex", "auto"]]
    monkeypatch.setattr(
        app, "call_codex", Mock(side_effect=RuntimeError("model execution failed"))
    )
    with pytest.raises(RuntimeError):
        app.call_role_model_routes("developer_middle", "task")
    assert app.state["task_developer_calls"]["T-001"] == 1
    assert app.state["task_failed_model_routes"]["T-001"] == ["codex/auto"]


def test_approval_guard_still_stops_routing(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["architect"] = [
        ["codex", "auto"],
        ["agy", "explicit"],
    ]
    monkeypatch.setattr(
        app,
        "call_codex",
        Mock(side_effect=ImplementationApprovalError("guard refused")),
    )
    fallback = Mock()
    monkeypatch.setattr(app, "call_agy", fallback)
    with pytest.raises(ImplementationApprovalError):
        app.call_role_model_routes("architect", "task")
    fallback.assert_not_called()
    assert not app.state["failed_model_routes"]


def test_codex_revalidates_candidates_and_reasoning(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    catalogs = [
        {"gpt-1-sol": {"supported_reasoning_levels": [{"effort": "low"}]}},
        {"gpt-2-sol": {"supported_reasoning_levels": [{"effort": "high"}]}},
    ]
    monkeypatch.setattr(
        cloud_routing, "discover_codex_models", Mock(side_effect=catalogs)
    )
    rank = Mock(return_value=None)
    monkeypatch.setattr(backends, "route_ollama_model_with_tev1", rank)
    assert cloud_routing.select_codex_model(app, "architect", "task") == (
        "gpt-1-sol",
        "low",
    )
    assert cloud_routing.select_codex_model(app, "architect", "task") == (
        "gpt-2-sol",
        "high",
    )
    assert [call.args[2] for call in rank.call_args_list] == [
        ["gpt-1-sol"],
        ["gpt-2-sol"],
    ]


def test_codex_same_model_revalidates_efforts(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    models = {"gpt-1-sol": {"supported_reasoning_levels": [{"effort": "low"}]}}
    monkeypatch.setattr(
        cloud_routing, "discover_codex_models", Mock(return_value=models)
    )
    monkeypatch.setattr(
        backends, "route_ollama_model_with_tev1", Mock(return_value=None)
    )
    assert cloud_routing.select_codex_model(app, "architect", "task")[1] == "low"
    models["gpt-1-sol"]["supported_reasoning_levels"] = [{"effort": "high"}]
    assert cloud_routing.select_codex_model(app, "architect", "task")[1] == "high"


def test_agy_revalidates_family_version(initialized_orchestrator, clock, monkeypatch):
    app = initialized_orchestrator
    run = Mock(
        return_value=Mock(
            returncode=0, stdout=catalog_output("agy", "gemini-3.8-flash-medium")
        )
    )
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    assert (
        cloud_routing.select_agy_model(
            app, "architect", "task", "auto:gemini-flash-medium"
        )
        == "gemini-3.8-flash-medium"
    )
    clock.return_value = cloud_routing.CATALOG_TTL
    run.return_value = Mock(
        returncode=0, stdout=catalog_output("agy", "gemini-4.1-flash-medium")
    )
    assert (
        cloud_routing.select_agy_model(
            app, "architect", "task", "auto:gemini-flash-medium"
        )
        == "gemini-4.1-flash-medium"
    )
    assert run.call_count == 2


def test_discovery_retries_a_transient_exit_without_cooling_down(clock, monkeypatch):
    run = Mock(
        side_effect=[
            Mock(returncode=1),
            Mock(returncode=0, stdout=catalog_output("codex", "gpt-1-sol")),
        ]
    )
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    assert "gpt-1-sol" in cloud_routing.discover_codex_models()
    assert run.call_count == 2
    assert not cloud_routing._catalog_failures


def test_provider_failure_does_not_block_other_catalog(clock, monkeypatch):
    run = Mock(
        side_effect=[
            subprocess.TimeoutExpired("codex", 60),
            Mock(returncode=0, stdout=catalog_output("agy", "gemini-3.8-flash-medium")),
        ]
    )
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    with pytest.raises(cloud_routing.ModelDiscoveryError):
        discover("codex")
    assert discover("agy") == "gemini-3.8-flash-medium"
    assert run.call_count == 2


def test_codex_fallback_selection_can_be_reranked_after_router_recovers(
    initialized_orchestrator, clock, monkeypatch
):
    app = initialized_orchestrator
    models = {"gpt-1-sol": {}, "gpt-2-sol": {}}
    monkeypatch.setattr(
        cloud_routing, "discover_codex_models", Mock(return_value=models)
    )
    monkeypatch.setattr(
        backends, "discover_installed_ollama_models", Mock(return_value={"d3:4b"})
    )
    post = Mock(side_effect=backends.requests.Timeout("secret token"))
    monkeypatch.setattr(backends.requests, "post", post)
    assert cloud_routing.select_codex_model(app, "architect", "task")[0] == "gpt-1-sol"
    assert cloud_routing.select_codex_model(app, "architect", "task")[0] == "gpt-1-sol"
    post.assert_called_once()
    clock.return_value = backends.TEV1_COOLDOWN
    post.side_effect = None
    post.return_value = router_response(list(reversed(models)))
    assert cloud_routing.select_codex_model(app, "architect", "task")[0] == "gpt-2-sol"
    assert cloud_routing.select_codex_model(app, "architect", "task")[0] == "gpt-2-sol"
    assert post.call_count == 2


def test_failed_discovery_refunds_only_its_own_reservation(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.state.update(state="IMPLEMENTING", active_task_id="T-001")
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["developer_middle"] = [["codex", "auto"]]

    def independently_recorded_call(*args, **kwargs):
        app.state["task_developer_calls"]["T-001"] += 1
        raise cloud_routing.ModelDiscoveryError("codex", "timeout", "not executed")

    monkeypatch.setattr(app, "call_codex", independently_recorded_call)
    with pytest.raises(RuntimeError):
        app.call_role_model_routes("developer_middle", "task")
    assert app.state["task_developer_calls"]["T-001"] == 2


@pytest.mark.parametrize(
    "metadata",
    [
        {"supported_reasoning_levels": {}},
        {"supported_reasoning_levels": [None]},
        {"supported_reasoning_levels": [{"effort": []}]},
        {"default_reasoning_level": {}},
    ],
)
def test_invalid_reasoning_metadata_is_a_discovery_format_error(metadata, monkeypatch):
    entry = {
        "slug": "gpt-1-sol",
        "visibility": "list",
        "supported_in_api": True,
        **metadata,
    }
    run = Mock(return_value=Mock(returncode=0, stdout=json.dumps({"models": [entry]})))
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    with pytest.raises(cloud_routing.ModelDiscoveryError) as captured:
        cloud_routing.discover_codex_models()
    assert captured.value.kind == "format"


def test_discovery_pause_preserves_task_across_owner_continues_and_recovers(
    implementation_app, clock, monkeypatch
):
    app = implementation_app
    app.config["backends"]["developer_senior"] = "codex"
    app.config["role_model_routes"]["developer_senior"] = [["codex", "auto"]]
    app.config["ollama_router_enabled"] = False
    app.record_implementation_approval("approve")
    requirements = app.requirements_path.read_bytes()
    plan = app.plan_path.read_bytes()
    run = Mock(side_effect=subprocess.TimeoutExpired("models", 60))
    monkeypatch.setattr(cloud_routing.subprocess, "run", run)
    model_call = Mock(
        return_value=Mock(
            returncode=0,
            stdout="[FILE_START: app.py]\nVALUE = 1\n[FILE_END: app.py]",
            stderr="",
        )
    )
    monkeypatch.setattr(backends, "run_model_cli", model_call)
    replan = Mock(side_effect=AssertionError("Discovery must not trigger replanning"))
    monkeypatch.setattr(app, "escalate_to_replan_or_pause", replan)
    remember = Mock()
    monkeypatch.setattr("orchestrator.roles.developer.brain.remember", remember)
    for now in (
        0,
        0,
        cloud_routing.DISCOVERY_COOLDOWN,
        cloud_routing.DISCOVERY_COOLDOWN * 2,
    ):
        clock.return_value = now
        app.step_implementing()
        assert app.state["state"] == "WAITING_FOR_OWNER"
        assert app.state["human_review_source"] == "Orchestrator"
        assert app.state["resume_state"] == "IMPLEMENTING"
        assert app.state["pass_state"] == "IMPLEMENTING"
        assert "discovery" in app.state["human_review_details"]
        assert app.state["tasks"][0].get("revisions", 0) == 0
        assert app.state["tasks"][0]["status"] == "pending"
        assert app.state["task_developer_calls"]["T1"] == 0
        assert app.requirements_path.read_bytes() == requirements
        assert app.plan_path.read_bytes() == plan
        assert not (app.workspace / "app.py").exists()
        assert apply_continue(app)
        assert app.state["state"] == "IMPLEMENTING"
    assert run.call_count == 3
    remember.assert_not_called()
    replan.assert_not_called()
    model_call.assert_not_called()
    clock.return_value += cloud_routing.DISCOVERY_COOLDOWN
    run.side_effect = None
    run.return_value = Mock(returncode=0, stdout=catalog_output("codex", "gpt-1-sol"))
    app.step_implementing()
    assert app.state["state"] == "TESTING"
    assert app.state["tasks"][0]["status"] == "completed"
    assert app.state["task_developer_calls"]["T1"] == 1
    assert (app.workspace / "app.py").read_text().strip() == "VALUE = 1"
    model_call.assert_called_once()
    assert app.requirements_path.read_bytes() == requirements
    assert app.plan_path.read_bytes() == plan


def test_mixed_discovery_and_model_failure_keeps_normal_failure_semantics(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.state.update(state="IMPLEMENTING", active_task_id="T-001")
    app.config["ollama_router_enabled"] = False
    app.config["role_model_routes"]["developer_middle"] = [
        ["codex", "auto"],
        ["agy", "explicit"],
    ]
    monkeypatch.setattr(
        app,
        "call_codex",
        Mock(
            side_effect=cloud_routing.ModelDiscoveryError(
                "codex", "timeout", "not executed"
            )
        ),
    )
    monkeypatch.setattr(
        app, "call_agy", Mock(side_effect=RuntimeError("model execution failed"))
    )
    with pytest.raises(RuntimeError) as captured:
        app.call_role_model_routes("developer_middle", "task")
    assert not isinstance(captured.value, cloud_routing.ModelDiscoveryError)
    assert app.state["task_developer_calls"]["T-001"] == 1
    assert app.state["task_failed_model_routes"]["T-001"] == ["agy/explicit"]
