"""Resolve cloud CLI auto routes from their live catalogs with Tev1 scoring."""

import json
import re
import subprocess
import time

_codex_catalog_cache = None
_catalog_failures = {}
CATALOG_TTL = 300
DISCOVERY_TIMEOUT = 60
DISCOVERY_ATTEMPTS = 2
DISCOVERY_COOLDOWN = 15


class ModelDiscoveryError(RuntimeError):
    """A catalog failure before any model execution, safe to retry later."""

    def __init__(self, provider: str, kind: str, detail: str):
        self.provider = provider
        self.kind = kind
        super().__init__(f"{provider} model discovery [{kind}]: {detail}")


def discover_cli_catalog(provider: str, parse):
    """Bound discovery work across roles and summaries without caching failures as lists."""
    failure = _catalog_failures.get(provider)
    if failure and time.monotonic() < failure[0]:
        raise failure[1]
    command = ["codex", "debug", "models"] if provider == "codex" else ["agy", "models"]
    error = None
    for _ in range(DISCOVERY_ATTEMPTS):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=DISCOVERY_TIMEOUT,
                check=False,
            )
            if result.returncode != 0:
                error = ModelDiscoveryError(
                    provider, "exit", f"command exited with code {result.returncode}"
                )
                continue
            catalog = parse(result.stdout)
        except subprocess.TimeoutExpired:
            error = ModelDiscoveryError(
                provider, "timeout", f"command exceeded {DISCOVERY_TIMEOUT}s"
            )
            break
        except OSError as exc:
            error = ModelDiscoveryError(
                provider, "command", f"cannot execute command ({type(exc).__name__})"
            )
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            # Catalogs and CLI diagnostics may contain credentials or system instructions.
            error = ModelDiscoveryError(
                provider, "format", f"invalid catalog format ({type(exc).__name__})"
            )
        else:
            _catalog_failures.pop(provider, None)
            return catalog
    _catalog_failures[provider] = (time.monotonic() + DISCOVERY_COOLDOWN, error)
    raise error


def _parse_codex_catalog(output: str) -> dict[str, dict]:
    data = json.loads(output)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("models must be an array")
    models = {}
    for entry in data["models"]:
        if not isinstance(entry, dict):
            raise ValueError("model entry must be an object")
        if entry.get("visibility") != "list" or not entry.get("supported_in_api"):
            continue
        slug = entry.get("slug")
        levels = entry.get("supported_reasoning_levels", [])
        if (
            not isinstance(slug, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", slug)
            or not isinstance(levels, list)
            or any(
                not isinstance(level, dict) or not isinstance(level.get("effort"), str)
                for level in levels
            )
            or (
                entry.get("default_reasoning_level") is not None
                and not isinstance(entry["default_reasoning_level"], str)
            )
        ):
            raise ValueError("invalid model slug or reasoning levels")
        models[slug] = entry
    return models


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
        and time.monotonic() - _codex_catalog_cache[0] < CATALOG_TTL
    ):
        return _codex_catalog_cache[1]
    models = discover_cli_catalog("codex", _parse_codex_catalog)
    _codex_catalog_cache = (time.monotonic(), models)
    return models


def select_codex_model(orchestrator, role: str, prompt: str) -> tuple[str, str | None]:
    from orchestrator.core import backends
    from orchestrator.core.state import log_info

    fallback_effort = orchestrator.config.get("reasoning_effort", {}).get(role)
    summary = orchestrator._get_compact_task_summary(role, prompt)
    catalog = discover_codex_models()
    models = {
        slug: entry
        for slug, entry in catalog.items()
        if codex_model_allowed_for_role(role, slug)
    }
    if role.startswith("developer"):
        if not models:
            raise ModelDiscoveryError(
                "codex",
                "policy_excluded" if catalog else "empty",
                "No available allowed Developer model (Luna, or Sol for senior/middle) for Codex route",
            )
    if not models:
        raise ModelDiscoveryError(
            "codex",
            "policy_excluded" if catalog else "empty",
            "No allowed Codex models available; refusing an implicit CLI default",
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
    ranked_efforts = [level for level in (ranked_efforts or []) if level in efforts]
    if not ranked_efforts:
        preferred = fallback_effort if fallback_effort in efforts else default_effort
        ranked_efforts = [preferred] if preferred in efforts else list(efforts)
    effort = ranked_efforts[0] if ranked_efforts else None
    log_info(f"Runtime Codex selection for {role}: {model} ({effort}).")
    return model, effort


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
    candidates = []
    for effort in ("low", "medium", "high"):
        try:
            model = backends.resolve_agy_model(f"auto:{family}-{effort}")
        except ModelDiscoveryError as error:
            if error.kind != "no_match":
                raise
            continue
        except RuntimeError:
            continue
        if cloud_model_allowed(model) and model not in candidates:
            candidates.append(model)
    if len(candidates) < 2:
        if not candidates:
            raise ModelDiscoveryError(
                "agy", "no_match", f"No allowed AGY model available for {selector}"
            )
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
    return ranked[0]
