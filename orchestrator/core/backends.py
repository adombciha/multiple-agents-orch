from __future__ import annotations
import base64
import json
import math
import re
import shutil
import subprocess
import time
import uuid
import requests
from pathlib import Path

AGY_MODEL_ALIASES = {
    "gemini-3.5-flash": "auto:gemini-flash-medium",
    "gemini-3.1-pro": "Gemini 3.1 Pro (Low)",
    "gpt-oss-120b": "GPT-OSS 120B (Medium)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
}

_agy_models_cache = (0.0, ())


def resolve_agy_model(model: str) -> str:
    if not model.startswith("auto:"):
        return AGY_MODEL_ALIASES.get(model, model)

    global _agy_models_cache
    if not _agy_models_cache[1] or time.monotonic() - _agy_models_cache[0] >= 300:
        result = subprocess.run(
            ["agy", "models"], capture_output=True, text=True, timeout=60, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"Cannot list AGY models: {result.stderr.strip()}")
        _agy_models_cache = (
            time.monotonic(),
            tuple(
                line.split("\t", 1)[0]
                for line in result.stdout.splitlines()
                if "\t" in line
            ),
        )

    family, effort = model.removeprefix("auto:").rsplit("-", 1)
    prefix, _, variant = family.partition("-")
    pattern = (
        re.compile(
            rf"^{re.escape(prefix)}-(\d+(?:\.\d+)*)-{re.escape(variant)}-{re.escape(effort)}$"
        )
        if prefix == "gemini"
        else re.compile(
            rf"^{re.escape(prefix)}-{re.escape(variant)}-(\d+(?:-\d+)*)-{re.escape(effort)}$"
        )
    )
    matches = (
        (tuple(map(int, re.split(r"[.-]", match.group(1)))), name)
        for name in _agy_models_cache[1]
        if (match := pattern.fullmatch(name))
    )
    try:
        return max(matches)[1]
    except ValueError as exc:
        raise RuntimeError(f"No AGY model available for {model}") from exc


def get_backend(orchestrator, role: str) -> str:
    backends = orchestrator.config.get("backends", {})
    if role.startswith("developer_"):
        task_id = orchestrator.state.get("active_task_id")
        override = (
            orchestrator.state.get("task_backend_overrides", {}).get(task_id)
            if task_id
            else None
        )
        if override:
            return override
        if (
            "developer" in orchestrator.explicit_backends
            and role not in orchestrator.explicit_backends
        ):
            return backends.get("developer", "ollama")
        return backends.get(role, backends.get("developer", "ollama"))
    if role.startswith("qa_"):
        if (
            "qa" in orchestrator.explicit_backends
            and role not in orchestrator.explicit_backends
        ):
            return backends.get("qa", "ollama")
        return backends.get(role, backends.get("qa", "ollama"))
    return backends.get(role, "ollama")


def get_active_model_for_role(orchestrator, role: str, backend: str) -> str | None:
    if backend == "ollama":
        candidates = sorted(
            discover_ollama_chat_models(orchestrator, force_refresh=True)
        )
        if not candidates:
            return None
        ranked = route_ollama_model_with_tev1(
            orchestrator, role, candidates, f"Execution for role {role}"
        )
        return ranked[0] if ranked else candidates[0]

    expected_backend = orchestrator.config.get("role_model_backends", {}).get(role)
    if backend == expected_backend:
        role_tiers = orchestrator.config.get("role_model_tiers", {}).get(role, [])
        if role_tiers:
            return role_tiers[0]
        role_model = orchestrator.config.get("role_models", {}).get(role)
        if role_model:
            return role_model

    indices = orchestrator.state.setdefault("model_tier_indices", {})
    if role == "assistant" and "assistant" not in indices:
        idx = indices.setdefault(role, 1)
    else:
        idx = indices.setdefault(role, 0)

    tiers = orchestrator.config.get("model_tiers", {}).get(backend, [])
    if not tiers:
        return None

    if idx < len(tiers):
        return tiers[idx]
    return tiers[-1]


def _ollama_model_matches(candidate: str, installed: set[str]) -> bool:
    if candidate in installed:
        return True
    for model in installed:
        if model.startswith(f"{candidate}:") or candidate.startswith(f"{model}:"):
            return True
        if candidate.endswith(":latest") and model == candidate[:-7]:
            return True
        if model.endswith(":latest") and candidate == model[:-7]:
            return True
    return False


def discover_installed_ollama_models(
    orchestrator, force_refresh: bool = False
) -> set[str]:
    """Discover installed models from Ollama /api/tags endpoint without pulling or deleting."""
    from orchestrator.core.state import log_info

    cached = getattr(orchestrator, "_installed_ollama_models_cache", None)
    if cached and not force_refresh and time.monotonic() - cached[0] < 60:
        return cached[1]

    url = f"{orchestrator.config.get('ollama_url', 'http://localhost:11434').rstrip('/')}/api/tags"
    try:
        response = requests.get(
            url, timeout=orchestrator.config.get("ollama_router_timeout", 5)
        )
        if response.status_code == 200:
            data = response.json()
            models = {m.get("name") for m in data.get("models", []) if m.get("name")}
            orchestrator._installed_ollama_models_cache = (time.monotonic(), models)
            return models
    except Exception as exc:
        log_info(f"Could not discover Ollama installed models at {url}: {exc}")

    if cached and not force_refresh:
        return cached[1]
    return set()


def discover_ollama_chat_models(
    orchestrator, force_refresh: bool = False
) -> dict[str, dict[str, object]]:
    """Return installed models that Ollama reports as supporting completion."""
    from orchestrator.core.state import log_info

    installed = discover_installed_ollama_models(orchestrator, force_refresh)
    if not installed:
        return {}

    cached = getattr(orchestrator, "_ollama_chat_models_cache", None)
    if (
        cached
        and cached[1] == frozenset(installed)
        and time.monotonic() - cached[0] < 60
    ):
        return cached[2]

    router_model = orchestrator.config.get("ollama_router_model", "tev1:4b")
    url = f"{orchestrator.config.get('ollama_url', 'http://localhost:11434').rstrip('/')}/api/show"
    chat_models = {}
    for model in sorted(installed):
        if _ollama_model_matches(router_model, {model}):
            continue
        try:
            response = requests.post(
                url,
                json={"model": model},
                timeout=orchestrator.config.get("ollama_router_timeout", 5),
            )
            if response.status_code != 200:
                continue
            data = response.json()
            capabilities = data.get("capabilities", [])
            if "completion" not in capabilities:
                continue
            details = data.get("details", {})
            model_info = data.get("model_info", {})
            context_length = next(
                (
                    value
                    for key, value in model_info.items()
                    if key.endswith(".context_length")
                ),
                None,
            )
            chat_models[model] = {
                key: value
                for key, value in {
                    "family": details.get("family"),
                    "parameter_size": details.get("parameter_size"),
                    "quantization_level": details.get("quantization_level"),
                    "context_length": context_length,
                    "capabilities": sorted(capabilities),
                }.items()
                if value is not None
            }
        except Exception as exc:
            log_info(f"Could not inspect Ollama model '{model}': {exc}")

    orchestrator._ollama_chat_models_cache = (
        time.monotonic(),
        frozenset(installed),
        chat_models,
    )
    return chat_models


def route_ollama_model_with_tev1(
    orchestrator,
    role: str,
    candidates: list[str],
    task_summary: str | None = None,
    *,
    criteria: dict[str, str | None] | None = None,
    require_installed: bool = True,
) -> list[str] | None:
    """Rank runtime candidates using Tev1 choice probabilities."""
    from orchestrator.core.state import log_info, log_warning

    if not orchestrator.config.get("ollama_router_enabled", True):
        return None

    chat_model_cache = getattr(orchestrator, "_ollama_chat_models_cache", None)
    installed_models = (
        set(chat_model_cache[1])
        if chat_model_cache
        else discover_installed_ollama_models(orchestrator)
    )
    if not installed_models:
        return None

    router_model = orchestrator.config.get("ollama_router_model", "tev1:4b")

    if not _ollama_model_matches(router_model, installed_models):
        log_info(
            f"Tev1 router '{router_model}' is not installed at Ollama endpoint; falling back."
        )
        return None

    available_candidates = list(
        dict.fromkeys(
            candidate
            for candidate in candidates
            if (
                not require_installed
                or _ollama_model_matches(candidate, installed_models)
            )
            and not _ollama_model_matches(candidate, {router_model})
        )
    )
    if not available_candidates:
        return None
    if len(available_candidates) == 1:
        return available_candidates
    if len(available_candidates) > 26:
        log_warning("Tev1 supports at most 26 choices; using static local model order.")
        return None

    summary = (task_summary or f"Execution for role {role}").strip()[:120]
    state_input = f"Role: {role}. Task: {summary}"

    url = f"{orchestrator.config.get('ollama_url', 'http://localhost:11434').rstrip('/')}/v1/systemone"
    payload = {
        "model": router_model,
        "state": state_input,
        "questions": {
            "selected_model": {
                "type": "choice",
                "instructions": f"Select the best model for the role '{role}'.",
                "criteria": {
                    candidate: criteria.get(candidate) if criteria else None
                    for candidate in available_candidates
                },
            }
        },
    }

    try:
        timeout = orchestrator.config.get("ollama_router_timeout", 10)
        response = requests.post(url, json=payload, timeout=timeout)
        if response.status_code != 200:
            detail = getattr(response, "text", "")
            detail = " ".join(detail.split())[:300] if isinstance(detail, str) else ""
            detail_text = f" Response: {detail}" if detail else ""
            log_info(
                f"Ollama /v1/systemone returned status {response.status_code}.{detail_text} "
                "Falling back to available local candidates."
            )
            return None
        data = response.json()
        ans = data.get("answers", {}).get("selected_model", {})
        choice = ans.get("choice")
        probabilities = ans.get("probabilities")
        confidence = ans.get("confidence")
        min_conf = float(orchestrator.config.get("ollama_router_min_confidence", 0.5))

        def valid_probability(value):
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and 0 <= value <= 1
            )

        if (
            choice not in available_candidates
            or not isinstance(probabilities, dict)
            or set(probabilities) != set(available_candidates)
            or not all(valid_probability(value) for value in probabilities.values())
            or not valid_probability(confidence)
            or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.01)
            or probabilities[choice] < max(probabilities.values()) - 0.001
        ):
            log_warning(
                "Tev1 returned an invalid model probability distribution; using static local model order."
            )
            return None

        ranked = sorted(
            available_candidates,
            key=lambda candidate: probabilities[candidate],
            reverse=True,
        )
        scores = ", ".join(
            f"{candidate}={probabilities[candidate]:.3f}" for candidate in ranked
        )
        qualifier = "low confidence; " if confidence < min_conf else ""
        log_info(
            f"Tev1 ranked {role} options ({qualifier}confidence {confidence:.2f}): {scores}"
        )
        return ranked
    except Exception as exc:
        log_info(f"Failed to query Tev1 router at {url}: {exc}; falling back.")
        return None


def call_ollama(
    orchestrator,
    prompt: str,
    system_prompt: str | None = None,
    role: str = "developer",
    model: str | None = None,
    image_paths: list[str] | None = None,
) -> str:
    from orchestrator.core.state import log_info, log_error

    url = f"{orchestrator.config['ollama_url']}/api/chat"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    model = model or orchestrator.get_active_model_for_role(role, "ollama")
    if not model:
        raise RuntimeError("No installed Ollama chat-capable model is available.")
    if "tev1" in model.lower():
        raise RuntimeError(
            f"Model '{model}' is restricted to decision routing via /v1/systemone and cannot be used as task executor."
        )
    from orchestrator.core import gpt

    if role.startswith("developer") and gpt.is_gpt_oss(model):
        return gpt.call(orchestrator, prompt, system_prompt, role, model, image_paths)
    log_info(f"Ollama calling model: {model}")
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": orchestrator.config.get("ollama_keep_alive", 0),
    }
    think = orchestrator.config.get("ollama_think", "high")
    no_think_models = getattr(orchestrator, "_ollama_no_think_models", set())
    if think and model not in no_think_models:
        payload["think"] = think
    if image_paths:
        payload["messages"][-1]["images"] = [
            base64.b64encode(Path(path).read_bytes()).decode() for path in image_paths
        ]

    try:
        timeout = orchestrator.config.get("ollama_timeout", 1800)
        response = requests.post(url, json=payload, timeout=timeout)
        if payload.get("think") and response.status_code == 400:
            log_info(
                f"Ollama returned HTTP 400 with think={think} for {model}; retrying without thinking."
            )
            no_think_models.add(model)
            orchestrator._ollama_no_think_models = no_think_models
            retry_payload = dict(payload)
            retry_payload.pop("think")
            response = requests.post(url, json=retry_payload, timeout=timeout)
        response.raise_for_status()
        return response.json()["message"]["content"]
    except requests.exceptions.RequestException as e:
        error_response = getattr(e, "response", None)
        detail = ""
        if error_response is not None and error_response.status_code == 400:
            detail = f": {error_response.text.strip()[:500]}"
        log_error(f"Failed to communicate with Ollama at {url}.")
        log_error(f"Error detail: {e}{detail}")
        raise RuntimeError(f"Ollama connection failed: {e}{detail}") from e


def call_codex(
    orchestrator,
    prompt: str,
    system_prompt: str | None = None,
    role: str = "developer",
    model: str | None = None,
) -> str:
    from orchestrator.core.state import log_info

    full_prompt = ""
    if system_prompt:
        full_prompt += f"System Instructions:\n{system_prompt}\n\n"
    full_prompt += prompt

    temp_prompt_file = orchestrator.ai_dir / "temp_codex_prompt.txt"
    with open(temp_prompt_file, "w", encoding="utf-8") as f:
        f.write(full_prompt)

    cmd = [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
    ]

    model = model or orchestrator.get_active_model_for_role(role, "codex")
    if role.startswith("developer") and model != "auto" and not model.endswith("-luna"):
        raise RuntimeError("Developer Codex route requires a Luna model")
    effort = orchestrator.config.get("reasoning_effort", {}).get(role)
    if model == "auto":
        from orchestrator.core.cloud_routing import select_codex_model

        model, effort = select_codex_model(orchestrator, role, prompt)
    orchestrator._resolved_cloud_model = model
    if model and model != "auto":
        cmd.extend(["-m", model])
    if effort:
        cmd.extend(["-c", f"model_reasoning_effort={effort!r}"])
    cmd.append("-")

    log_info(f"Running Codex: {' '.join(cmd)}")
    try:
        with open(temp_prompt_file, "r", encoding="utf-8") as pf:
            result = subprocess.run(
                cmd,
                cwd=orchestrator.agent_workspace(),
                stdin=pf,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
                check=False,
            )

        if temp_prompt_file.exists():
            temp_prompt_file.unlink()

        if result.returncode != 0:
            raise RuntimeError(
                f"Codex failed with code {result.returncode}:\n{result.stderr}"
            )
        return result.stdout
    except Exception as e:
        if temp_prompt_file.exists():
            temp_prompt_file.unlink()
        raise e


def call_agy(
    orchestrator,
    prompt: str,
    system_prompt: str | None = None,
    role: str = "developer",
    model: str | None = None,
) -> str:
    from orchestrator.core.state import log_info

    full_prompt = ""
    if system_prompt:
        full_prompt += f"System Instructions:\n{system_prompt}\n\n"
    full_prompt += prompt

    mode = (
        "accept-edits"
        if orchestrator.state.get("state") == "IMPLEMENTING"
        and role.startswith("developer")
        else "plan"
    )
    if mode == "accept-edits":
        full_prompt += (
            "\n\nDo not edit files directly or use workspace tools. "
            "Return only the machine-readable file blocks required by the task."
        )

    cmd = ["agy", "--sandbox", "--mode", mode]
    model = model or orchestrator.get_active_model_for_role(role, "agy")
    if model and model.startswith("auto:"):
        from orchestrator.core.cloud_routing import select_agy_model

        model = select_agy_model(orchestrator, role, prompt, model)
    if model:
        model = resolve_agy_model(model)
    orchestrator._resolved_cloud_model = model
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["--input-format", "stream-json", "--output-format", "stream-json"])

    log_info(f"Running agy: {' '.join(cmd)} ...")
    result = subprocess.run(
        cmd,
        cwd=orchestrator.agent_workspace(),
        input=json.dumps(
            {"event": "user", "message": {"content": full_prompt}},
            ensure_ascii=False,
        )
        + "\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agy CLI failed with code {result.returncode}:\n{result.stderr}"
        )
    for line in reversed(result.stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            final = event.get("result", {})
            if (
                isinstance(final, dict)
                and final.get("status") == "SUCCESS"
                and isinstance(final.get("response"), str)
            ):
                return final["response"]
            raise RuntimeError(f"agy CLI returned unsuccessful result: {final}")
    raise RuntimeError("agy CLI returned no result event")


def token_fallback_model(orchestrator, role: str, error: Exception) -> str | None:
    from orchestrator.core.state import log_warning

    message = str(error).lower()
    if not quota_exhausted(error) and not any(
        marker in message
        for marker in (
            "token limit",
            "context length",
            "maximum context",
            "too many tokens",
        )
    ):
        return None
    tiers = orchestrator.config.get("role_model_tiers", {}).get(role, [])
    if len(tiers) < 2:
        return None
    log_warning(f"[!] Retrying {role} with {tiers[1]} after capacity failure.")
    return tiers[1]


def quota_exhausted(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
            "429",
            "insufficient credit",
            "insufficient funds",
            "usage limit",
            "token budget",
            "spending-limit",
            "personal-team-blocked",
        )
    )


def backend_available(orchestrator, backend: str) -> bool:
    exhausted = orchestrator.state.setdefault("quota_exhausted_backends", {})
    until = exhausted.get(backend)
    if until is None:
        return True
    if (
        isinstance(until, (int, float))
        and not isinstance(until, bool)
        and until > time.time()
    ):
        return False
    exhausted.pop(backend, None)
    prefix = f"{backend}/"
    orchestrator.state["failed_model_routes"] = [
        route
        for route in orchestrator.state.get("failed_model_routes", [])
        if not route.startswith(prefix)
    ]
    for routes in orchestrator.state.get("task_failed_model_routes", {}).values():
        routes[:] = [route for route in routes if not route.startswith(prefix)]
    orchestrator.save_state()
    return True


def mark_backend_quota_exhausted(orchestrator, backend: str) -> None:
    from orchestrator.core.state import log_warning

    orchestrator.state.setdefault("quota_exhausted_backends", {})[backend] = (
        time.time() + 300
    )
    orchestrator.save_state()
    log_warning(f"{backend} quota exhausted; retrying after 5 minutes.")


QUOTA_EXHAUSTED_THRESHOLD_PCT = 95

# Interactive-mode-only quota/usage commands, discovered per-CLI since none of
# these expose a headless flag for it.
VENDOR_QUOTA_PROBES = {
    "codex": {"command": "/status"},
    "agy": {"command": "/credits"},
}


def _tmux_pane(session: str) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            check=True,
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return result.stdout
    except Exception:
        return None


def _tmux_wait_stable(
    session: str, timeout: float = 15.0, interval: float = 0.6
) -> str | None:
    """Poll the pane until its content stops changing for two consecutive polls
    (the CLI finished rendering/loading), or `timeout` elapses. Generic across
    vendors, so we don't need a hand-tuned fixed sleep per CLI."""
    last, stable = None, 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        pane = _tmux_pane(session)
        if pane is None:
            return None
        if pane == last:
            stable += 1
            if stable >= 2:
                return pane
        else:
            stable = 0
        last = pane
        time.sleep(interval)
    return last


def _probe_vendor_quota_pane(
    binary: str,
    command: str,
    trust_prompt: str | None = None,
    dismiss: tuple[str, str] | None = None,
) -> str | None:
    """Launch `binary` in a scratch tmux session, drive it to the quota/usage
    screen via `command`, and return the captured pane text (or None if the
    session/tmux itself failed at any step). Best-effort only: a launch that
    times out, a CLI that isn't installed, or missing tmux all fail safe."""
    if shutil.which("tmux") is None:
        return None
    session = f"quota_check_{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session,
                "-x",
                "200",
                "-y",
                "50",
                binary,
            ],
            check=True,
            timeout=10,
        )
        pane = _tmux_wait_stable(session)
        if pane is None:
            return None
        if trust_prompt and re.search(trust_prompt, pane):
            subprocess.run(
                ["tmux", "send-keys", "-t", session, "y"], check=True, timeout=5
            )
            pane = _tmux_wait_stable(session)
            if pane is None:
                return None
        if dismiss and re.search(dismiss[0], pane):
            subprocess.run(
                ["tmux", "send-keys", "-t", session, dismiss[1]], check=True, timeout=5
            )
            pane = _tmux_wait_stable(session)
            if pane is None:
                return None
        subprocess.run(
            ["tmux", "send-keys", "-t", session, command], check=True, timeout=5
        )
        time.sleep(
            0.5
        )  # let the CLI register the slash-command before Enter, or it can land as a no-op
        subprocess.run(
            ["tmux", "send-keys", "-t", session, "Enter"], check=True, timeout=5
        )
        time.sleep(
            1.5
        )  # skip past the blank screen-clear frame some CLIs render before the usage screen
        return _tmux_wait_stable(session, timeout=8.0)
    except Exception:
        return None
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


def parse_vendor_quota_pct(backend: str, pane: str) -> int | None:
    """Extract a 0-100 'how exhausted is this vendor' number from a captured
    quota-screen pane. Returns None when the format isn't recognized (unknown,
    not exhausted) rather than guessing."""
    if backend == "codex":
        m = re.search(r"Current week[^\n]*[\s\S]{0,80}?(\d{1,3})%\s*used", pane)
        if m:
            return int(m.group(1))
        m = re.search(r"weekly[^\n]{0,40}?(\d{1,3})%", pane, re.IGNORECASE)
        return int(m.group(1)) if m else None
    if backend == "agy":
        if re.search(r"not enabled", pane, re.IGNORECASE):
            return None
        if re.search(r"insufficient|no credits|exhausted", pane, re.IGNORECASE):
            return 100
        m = re.search(r"Remaining AI Credits:\s*([\d,]+)", pane)
        if m:
            return 100 if int(m.group(1).replace(",", "")) == 0 else 0
        return None
    return None


def check_vendor_quota(orchestrator, backend: str) -> bool | None:
    """Proactively probe `backend`'s CLI for remaining quota via its
    interactive-only /usage-style command, run inside a scratch tmux session.

    Returns False if the vendor looks exhausted (skip hiring it), True if it
    looks fine, or None if unknown (probe failed, format not recognized, or
    the check is disabled) -- unknown is treated as available, since the
    reactive quota_exhausted() detection during the real call is the actual
    safety net; this is only a best-effort head start."""
    from orchestrator.core.state import log_info, log_warning

    if not orchestrator.config.get("vendor_quota_check_enabled", False):
        return None
    probe = VENDOR_QUOTA_PROBES.get(backend)
    if probe is None:
        return None
    binary = shutil.which(backend)
    if binary is None:
        return None

    pane = _probe_vendor_quota_pane(
        binary,
        probe["command"],
        trust_prompt=probe.get("trust_prompt"),
        dismiss=probe.get("dismiss"),
    )
    if pane is None:
        log_info(
            f"Could not check {backend}'s live quota (probe failed); assuming available."
        )
        return None
    pct = parse_vendor_quota_pct(backend, pane)
    if pct is None:
        log_info(f"Could not parse {backend}'s quota screen; assuming available.")
        return None

    threshold = orchestrator.config.get(
        "vendor_quota_check_threshold_pct", QUOTA_EXHAUSTED_THRESHOLD_PCT
    )
    available = pct < threshold
    if not available:
        log_warning(
            f"[!] {backend} is at {pct}% of its usage limit; treating as exhausted for hiring."
        )
    return available


def _effective_developer_role(orchestrator, role: str) -> str:
    task_id = orchestrator.state.get("active_task_id")
    if task_id:
        promotions = orchestrator.state.get("task_developer_promotions", {}).get(
            task_id, {}
        )
    else:
        promotions = orchestrator.state.get("developer_promotions", {})
    return promotions.get(role, role)


def escalate_developer_backend(orchestrator) -> None:
    from orchestrator.core.state import log_warning
    from orchestrator.core import scoring

    role = orchestrator.state.get("last_developer_role", "developer_senior")
    task_id = orchestrator.state.get("active_task_id")
    if task_id:
        promotions = orchestrator.state.setdefault(
            "task_developer_promotions", {}
        ).setdefault(task_id, {})
    else:
        promotions = orchestrator.state.setdefault("developer_promotions", {})
    effective_role = _effective_developer_role(orchestrator, role)
    effective_level = effective_role.rsplit("_", 1)[-1]
    effective_backend = get_backend(orchestrator, effective_role)
    effective_model = get_active_model_for_role(
        orchestrator, effective_role, effective_backend
    )
    scoring.record_event(
        orchestrator, "rd", effective_level, effective_backend, effective_model, -1.5
    )
    promoted_role = {
        "developer_junior": "developer_middle",
        "developer_middle": "developer_senior",
    }.get(effective_role)
    if not promoted_role:
        log_warning("[!] Already at the highest Developer model tier.")
        return

    promotions[role] = promoted_role
    orchestrator.save_state()
    scope = f"task {task_id}" if task_id else "this run"
    log_warning(f"[!] Promoted {role} to {promoted_role} for {scope}.")


def maybe_hire_vendor(orchestrator, task: dict) -> str | None:
    """Deterministic firefighting: when internal staff is struggling on a task,
    hand it to the best-rated available vendor from backend_escalation_path.

    Trigger: one retry left before human review, staff trust weight has dropped
    below 0.5, or Ollama itself is unreachable. Returns the hired backend name,
    or None if nothing fired (staff keeps the task).
    """
    from orchestrator.core.state import log_warning
    from orchestrator.core import scoring

    role = orchestrator.state.get("last_developer_role", "developer_senior")
    effective_role = _effective_developer_role(orchestrator, role)
    if get_backend(orchestrator, effective_role) != "ollama":
        return None

    level = effective_role.rsplit("_", 1)[-1]
    max_revisions = orchestrator.config.get("max_revisions", 2)
    revisions = int(task.get("revisions", 0))
    weight = scoring.get_weight(orchestrator.config, "rd", level)
    ollama_down = not orchestrator.state.get("ollama_reachable", True)
    if not (revisions >= max(0, max_revisions - 1) or weight < 0.5 or ollama_down):
        return None

    task_id = task.get("id")
    failed_routes = orchestrator.state.get("task_failed_model_routes", {}).get(
        task_id, []
    )
    candidates = []
    for candidate_backend in orchestrator.config.get("backend_escalation_path", []):
        if candidate_backend == "ollama" or not backend_available(
            orchestrator, candidate_backend
        ):
            continue
        model = get_active_model_for_role(
            orchestrator, effective_role, candidate_backend
        )
        if f"{candidate_backend}/{model}" in failed_routes:
            continue
        candidates.append(
            (
                scoring.get_vendor_rating(
                    orchestrator.config, candidate_backend, model
                ),
                candidate_backend,
            )
        )
    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    for hired_rating, hired_backend in candidates:
        if check_vendor_quota(orchestrator, hired_backend) is False:
            mark_backend_quota_exhausted(orchestrator, hired_backend)
            continue
        orchestrator.state.setdefault("task_backend_overrides", {})[
            task_id
        ] = hired_backend
        orchestrator.save_state()
        log_warning(
            f"[!] Task {task_id} is struggling on internal staff ({effective_role}); hiring vendor '{hired_backend}' (rating {hired_rating}) to firefight."
        )
        return hired_backend
    return None
