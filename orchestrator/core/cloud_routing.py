"""Resolve cloud CLI auto routes from their live catalogs with Tev1 scoring."""

import json
import re
import subprocess
import time

_codex_catalog_cache = None


def cloud_model_allowed(model: str | None) -> bool:
    return bool(model and model != "auto" and not model.startswith("auto:")) and not {
        "fabel",
        "argon",
        "astra",
    }.intersection(re.findall(r"[a-z0-9]+", model.casefold()))


def codex_model_allowed_for_role(role: str, model: str | None) -> bool:
    if not cloud_model_allowed(model):
        return False
    if not role.startswith("developer"):
        return True
    families = (
        ("-luna", "-sol")
        if role in {"developer_senior", "developer_middle"}
        else ("-luna",)
    )
    return model.endswith(families)


def discover_codex_models() -> dict[str, dict]:
    global _codex_catalog_cache
    if (
        _codex_catalog_cache is not None
        and time.monotonic() - _codex_catalog_cache[0] < 300
    ):
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
    if key in cache and codex_model_allowed_for_role(role, cache[key][0]):
        return cache[key]
    cache.pop(key, None)
    models = {
        slug: entry
        for slug, entry in discover_codex_models().items()
        if codex_model_allowed_for_role(role, slug)
    }
    if role.startswith("developer"):
        if not models:
            raise RuntimeError(
                "No available allowed Developer model (Luna, or Sol for senior/middle) for Codex route"
            )
    if not models:
        raise RuntimeError(
            "No allowed Codex models available; refusing an implicit CLI default"
        )
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
    ranked = [slug for slug in (ranked or []) if slug in models] or list(models)
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

    if not cloud_model_allowed(selector.removeprefix("auto:")):
        raise RuntimeError(f"AGY model is prohibited or unresolved: {selector!r}")
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
    if key in cache and cloud_model_allowed(cache[key]):
        return cache[key]
    cache.pop(key, None)
    candidates = []
    for effort in ("low", "medium", "high"):
        try:
            model = backends.resolve_agy_model(f"auto:{family}-{effort}")
        except RuntimeError:
            continue
        if cloud_model_allowed(model) and model not in candidates:
            candidates.append(model)
    if len(candidates) < 2:
        if not candidates:
            raise RuntimeError(f"No allowed AGY model available for {selector}")
        return candidates[0]
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
    ranked = [model for model in (ranked or []) if model in candidates] or candidates
    log_info(f"Runtime AGY selection for {role}: {ranked[0]}.")
    cache[key] = ranked[0]
    return cache[key]
