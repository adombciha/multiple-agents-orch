from __future__ import annotations
import base64
import subprocess
import requests
from pathlib import Path

def get_backend(orchestrator, role: str) -> str:
    backends = orchestrator.config.get("backends", {})
    if role.startswith("developer_"):
        if "developer" in orchestrator.explicit_backends and role not in orchestrator.explicit_backends:
            return backends.get("developer", "ollama")
        return backends.get(role, backends.get("developer", "ollama"))
    if role.startswith("qa_"):
        if "qa" in orchestrator.explicit_backends and role not in orchestrator.explicit_backends:
            return backends.get("qa", "ollama")
        return backends.get(role, backends.get("qa", "ollama"))
    return backends.get(role, "ollama")

def get_active_model_for_role(orchestrator, role: str, backend: str) -> str | None:
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

def call_ollama(orchestrator, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None, image_paths: list[str] | None = None) -> str:
    from orchestrator.core.state import log_info, log_error
    url = f"{orchestrator.config['ollama_url']}/api/chat"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    model = model or orchestrator.get_active_model_for_role(role, "ollama") or orchestrator.config.get("ollama_model", "gemma2:2b")
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
        payload["messages"][-1]["images"] = [base64.b64encode(Path(path).read_bytes()).decode() for path in image_paths]

    try:
        timeout = orchestrator.config.get("ollama_timeout", 1800)
        response = requests.post(url, json=payload, timeout=timeout)
        if payload.get("think") and response.status_code == 400:
            log_info(f"Ollama model {model} does not accept think={think}; retrying without thinking.")
            no_think_models.add(model)
            orchestrator._ollama_no_think_models = no_think_models
            retry_payload = dict(payload)
            retry_payload.pop("think")
            response = requests.post(url, json=retry_payload, timeout=timeout)
        response.raise_for_status()
        return response.json()["message"]["content"]
    except requests.exceptions.RequestException as e:
        log_error(f"Failed to communicate with Ollama at {url}.")
        log_error(f"Error detail: {e}")
        raise RuntimeError(f"Ollama connection failed: {e}")

def call_codex(orchestrator, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None) -> str:
    from orchestrator.core.state import log_info
    full_prompt = ""
    if system_prompt:
        full_prompt += f"System Instructions:\n{system_prompt}\n\n"
    full_prompt += prompt

    temp_prompt_file = orchestrator.ai_dir / "temp_codex_prompt.txt"
    with open(temp_prompt_file, "w", encoding="utf-8") as f:
        f.write(full_prompt)

    cmd = [
        "codex", "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check"
    ]

    model = model or orchestrator.get_active_model_for_role(role, "codex")
    if model:
        cmd.extend(["-m", model])
    effort = orchestrator.config.get("reasoning_effort", {}).get(role)
    if effort:
        cmd.extend(["-c", f"model_reasoning_effort={effort!r}"])
    cmd.append("-")

    log_info(f"Running Codex: {' '.join(cmd)}")
    try:
        with open(temp_prompt_file, "r", encoding="utf-8") as pf:
            result = subprocess.run(
                cmd,
                cwd=orchestrator.workspace,
                stdin=pf,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
                check=False
            )

        if temp_prompt_file.exists():
            temp_prompt_file.unlink()

        if result.returncode != 0:
            raise RuntimeError(f"Codex failed with code {result.returncode}:\n{result.stderr}")
        return result.stdout
    except Exception as e:
        if temp_prompt_file.exists():
            temp_prompt_file.unlink()
        raise e

def call_claude(orchestrator, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
    from orchestrator.core.state import log_info
    full_prompt = ""
    if system_prompt:
        full_prompt += f"{system_prompt}\n\n"
    full_prompt += prompt

    cmd = ["claude", "--print", "--dangerously-skip-permissions"]
    model = orchestrator.get_active_model_for_role(role, "claude")
    if model:
        cmd.extend(["--model", model])
    cmd.append(full_prompt)

    log_info(f"Running Claude Code: {' '.join(cmd[:-1])} ...")
    result = subprocess.run(
        cmd,
        cwd=orchestrator.workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1800,
        check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI failed with code {result.returncode}:\n{result.stderr}")
    return result.stdout

def call_agy(orchestrator, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None) -> str:
    from orchestrator.core.state import log_info
    full_prompt = ""
    if system_prompt:
        full_prompt += f"System Instructions:\n{system_prompt}\n\n"
    full_prompt += prompt

    cmd = ["agy"]
    model = model or orchestrator.get_active_model_for_role(role, "agy")
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["--print", full_prompt])

    log_info(f"Running agy: {' '.join(cmd[:-1])} ...")
    result = subprocess.run(
        cmd,
        cwd=orchestrator.workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1800,
        check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"agy CLI failed with code {result.returncode}:\n{result.stderr}")
    return result.stdout

def call_grok(orchestrator, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None, response_schema: dict | None = None) -> str:
    from orchestrator.core import grok
    return grok.call(orchestrator, prompt, system_prompt, role, model, response_schema)

def token_fallback_model(orchestrator, role: str, error: Exception) -> str | None:
    from orchestrator.core.state import log_warning
    message = str(error).lower()
    if not quota_exhausted(error) and not any(marker in message for marker in ("token limit", "context length", "maximum context", "too many tokens")):
        return None
    tiers = orchestrator.config.get("role_model_tiers", {}).get(role, [])
    if len(tiers) < 2:
        return None
    log_warning(f"[!] Retrying {role} with {tiers[1]} after capacity failure.")
    return tiers[1]

def quota_exhausted(error: Exception) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in (
        "quota", "rate limit", "rate_limit", "too many requests", "429",
        "insufficient credit", "insufficient funds", "usage limit", "token budget",
        "spending-limit", "personal-team-blocked",
    ))

def backend_available(orchestrator, backend: str) -> bool:
    exhausted = orchestrator.state.setdefault("quota_exhausted_backends", {})
    return backend not in exhausted

def mark_backend_quota_exhausted(orchestrator, backend: str) -> None:
    from orchestrator.core.state import log_warning
    orchestrator.state.setdefault("quota_exhausted_backends", {})[backend] = True
    orchestrator.save_state()
    log_warning(f"{backend} quota exhausted; skipping it for this workflow.")

def escalate_developer_backend(orchestrator) -> None:
    from orchestrator.core.state import log_warning
    role = orchestrator.state.get("last_developer_role", "developer_senior")
    task_id = orchestrator.state.get("active_task_id")
    if task_id:
        promotions = orchestrator.state.setdefault("task_developer_promotions", {}).setdefault(task_id, {})
    else:
        promotions = orchestrator.state.setdefault("developer_promotions", {})
    effective_role = promotions.get(role, role)
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
