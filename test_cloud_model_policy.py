"""Enforce the owner's model exclusions without making model calls."""

from unittest.mock import Mock

import pytest

from orchestrator.core import backends, cloud_routing
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6-astra",
        "gpt-9-astra-preview",
        "Gemini Argon (High)",
        "claude-fabel-5-5-high",
        "Claude (Fabel)",
        "GPT-6-ASTRA",
        None,
        "auto",
    ],
)
def test_prohibited_families_and_unresolved_defaults_are_not_allowed(model):
    assert not cloud_routing.cloud_model_allowed(model)


@pytest.mark.parametrize(
    "role", ["architect", "reviewer", "manager", "developer_senior"]
)
def test_codex_filters_before_scoring_and_discards_prohibited_cache(
    initialized_orchestrator, monkeypatch, role
):
    app = initialized_orchestrator
    monkeypatch.setattr(app, "_get_compact_task_summary", Mock(return_value="task"))
    app._cloud_route_choices = {("codex", role, "task"): ("gpt-6-astra", "high")}
    monkeypatch.setattr(
        cloud_routing,
        "discover_codex_models",
        Mock(
            return_value={
                "gpt-6-astra": {},
                "gpt-6-luna": {},
                "gpt-6.1-sol": {},
            }
        ),
    )
    rank = Mock(return_value=["gpt-6-astra", "gpt-6-luna"])
    monkeypatch.setattr(backends, "route_ollama_model_with_tev1", rank)
    assert cloud_routing.select_codex_model(app, role, "task")[0] == "gpt-6-luna"
    assert "gpt-6-astra" not in rank.call_args.args[2]
    if role.startswith("developer"):
        assert rank.call_args.args[2] == ["gpt-6-luna"]


@pytest.mark.parametrize("catalog", [{}, {"gpt-6-astra": {}}])
def test_empty_allowed_codex_catalog_never_launches_implicit_default(
    initialized_orchestrator, monkeypatch, catalog
):
    monkeypatch.setattr(
        cloud_routing, "discover_codex_models", Mock(return_value=catalog)
    )
    run = Mock()
    monkeypatch.setattr(backends.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="No allowed Codex models"):
        backends.call_codex(
            initialized_orchestrator, "task", role="architect", model="auto"
        )
    run.assert_not_called()


@pytest.mark.parametrize(
    "backend,model",
    [
        ("codex", "gpt-6-astra"),
        ("agy", "gemini-argon-high"),
        ("agy", "claude-fabel-5-5-high"),
    ],
)
def test_explicit_prohibited_models_cannot_launch(
    initialized_orchestrator, monkeypatch, backend, model
):
    run = Mock()
    monkeypatch.setattr(backends.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="prohibited"):
        getattr(backends, f"call_{backend}")(
            initialized_orchestrator,
            "task",
            role="architect",
            model=model,
        )
    run.assert_not_called()


def test_agy_alias_cannot_bypass_exclusion(monkeypatch):
    monkeypatch.setitem(backends.AGY_MODEL_ALIASES, "test-alias", "Claude Fabel (High)")
    with pytest.raises(RuntimeError, match="prohibited"):
        backends.resolve_agy_model("test-alias")


def test_agy_cache_and_ranked_choices_must_stay_in_allowed_candidates(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    selector = "auto:gemini-flash-medium"
    monkeypatch.setattr(app, "_get_compact_task_summary", Mock(return_value="task"))
    app._cloud_route_choices = {
        ("agy", "architect", "task", selector): "gemini-argon-high"
    }
    monkeypatch.setattr(
        backends,
        "resolve_agy_model",
        Mock(
            side_effect=[
                "gemini-argon-high",
                "gemini-3.8-flash-medium",
                "claude-fabel-5-5-high",
            ]
        ),
    )
    rank = Mock()
    monkeypatch.setattr(backends, "route_ollama_model_with_tev1", rank)
    assert (
        cloud_routing.select_agy_model(app, "architect", "task", selector)
        == "gemini-3.8-flash-medium"
    )
    rank.assert_not_called()


@pytest.mark.parametrize(
    "selector", ["auto:gemini-argon-high", "auto:claude-fabel-high"]
)
def test_prohibited_agy_families_never_reach_catalog_or_scoring(
    initialized_orchestrator, monkeypatch, selector
):
    resolve = Mock()
    monkeypatch.setattr(backends, "resolve_agy_model", resolve)
    with pytest.raises(RuntimeError, match="prohibited"):
        cloud_routing.select_agy_model(
            initialized_orchestrator, "architect", "task", selector
        )
    resolve.assert_not_called()


def test_capacity_fallback_skips_all_prohibited_families(initialized_orchestrator):
    app = initialized_orchestrator
    app.config["role_model_tiers"]["architect"] = [
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gemini-argon-high",
        "claude-fabel-5-5-high",
        "gpt-6-luna",
    ]
    assert (
        backends.token_fallback_model(app, "architect", RuntimeError("token limit"))
        == "gpt-6-luna"
    )
    app.config["role_model_tiers"]["architect"].pop()
    assert (
        backends.token_fallback_model(app, "architect", RuntimeError("token limit"))
        is None
    )
