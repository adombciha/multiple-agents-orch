"""Resolve cloud CLI auto routes from their live catalogs with Tev1 scoring."""

import json
import subprocess
import time

_codex_catalog_cache = (0.0, {})


def discover_codex_models() -> dict[str, dict]:
    global _codex_catalog_cache
    if time.monotonic() - _codex_catalog_cache[0] < 300:
        return _codex_catalog_cache[1]
    try:
        result = subprocess.run(
            ["codex", "debug", "models"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            return {}
        models = {
            entry["slug"]: entry
            for entry in json.loads(result.stdout).get("models", [])
            if entry.get("visibility") == "list"
            and entry.get("supported_in_api")
            and entry.get("slug")
        }
        _codex_catalog_cache = (time.monotonic(), models)
        return models
    except (OSError, TypeError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return {}


def select_codex_model(orchestrator, role: str, prompt: str) -> tuple[str, str | None]:
    from orchestrator.core import backends
    from orchestrator.core.state import log_info

    fallback_effort = orchestrator.config.get("reasoning_effort", {}).get(role)
    summary = orchestrator._get_compact_task_summary(role, prompt)
    cache = getattr(orchestrator, "_cloud_route_choices", None)
    if cache is None:
        cache = orchestrator._cloud_route_choices = {}
    key = ("codex", role, summary)
    if key in cache:
        return cache[key]
    models = discover_codex_models()
    if role.startswith("developer"):
        models = {
            slug: entry for slug, entry in models.items() if slug.endswith("-luna")
        }
        if not models:
            raise RuntimeError("No available Luna model for Developer Codex route")
    if not models:
        return "auto", fallback_effort
    descriptions = {
        slug: entry.get("description") or entry.get("display_name")
        for slug, entry in models.items()
    }
    ranked = backends.route_ollama_model_with_tev1(
        orchestrator,
        role,
        list(models),
        summary,
        criteria=descriptions,
        require_installed=False,
    )
    if not ranked:
        if role.startswith("developer"):
            ranked = list(models)
        else:
            return "auto", fallback_effort
    model = ranked[0]
    efforts = {
        level["effort"]: level.get("description")
        for level in models[model].get("supported_reasoning_levels", [])
        if level.get("effort") in {"low", "medium", "high"}
    }
    ranked_efforts = (
        backends.route_ollama_model_with_tev1(
            orchestrator,
            role,
            list(efforts),
            summary,
            criteria=efforts,
            require_installed=False,
        )
        if len(efforts) > 1
        else list(efforts)
    )
    default_effort = models[model].get("default_reasoning_level")
    effort = (
        ranked_efforts
        or [fallback_effort if fallback_effort in efforts else default_effort]
    )[0]
    log_info(f"Runtime Codex selection for {role}: {model} ({effort}).")
    cache[key] = (model, effort)
    return cache[key]


def select_agy_model(orchestrator, role: str, prompt: str, selector: str) -> str:
    from orchestrator.core import backends
    from orchestrator.core.state import log_info

    if not selector.startswith("auto:"):
        return selector
    family, _, fallback_effort = selector.removeprefix("auto:").rpartition("-")
    if not family or fallback_effort not in {"low", "medium", "high"}:
        return selector
    summary = orchestrator._get_compact_task_summary(role, prompt)
    cache = getattr(orchestrator, "_cloud_route_choices", None)
    if cache is None:
        cache = orchestrator._cloud_route_choices = {}
    key = ("agy", role, summary, selector)
    if key in cache:
        return cache[key]
    candidates = []
    for effort in ("low", "medium", "high"):
        try:
            model = backends.resolve_agy_model(f"auto:{family}-{effort}")
        except RuntimeError:
            continue
        if model not in candidates:
            candidates.append(model)
    if len(candidates) < 2:
        return candidates[0] if candidates else selector
    ranked = backends.route_ollama_model_with_tev1(
        orchestrator,
        role,
        candidates,
        summary,
        criteria={
            model: f"{family} with {model.rsplit('-', 1)[-1]} reasoning"
            for model in candidates
        },
        require_installed=False,
    )
    if not ranked:
        return selector
    log_info(f"Runtime AGY selection for {role}: {ranked[0]}.")
    cache[key] = ranked[0]
    return cache[key]
