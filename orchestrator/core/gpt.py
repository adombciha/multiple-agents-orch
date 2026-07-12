from __future__ import annotations

import json
import time

import requests


FILE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            "minItems": 1,
        }
    },
    "required": ["files"],
    "additionalProperties": False,
}


def is_gpt_oss(model: str | None) -> bool:
    return bool(model and model.split(":", 1)[0] == "gpt-oss")


def _file_blocks(payload: str) -> str:
    try:
        data = json.loads(payload)
        files = data["files"]
        if not isinstance(files, list) or not files:
            raise ValueError("files must be a non-empty array")
        return "\n".join(
            f"[FILE_START: {item['path']}]\n{item['content']}\n[FILE_END: {item['path']}]"
            for item in files
            if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("content"), str)
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"gpt-oss returned invalid JSON file payload: {error}") from error


def call(orchestrator, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str = "gpt-oss:20b", image_paths: list[str] | None = None) -> str:
    from orchestrator.core.state import log_error, log_info
    from pathlib import Path
    import base64

    messages = []
    system = system_prompt or ""
    system += " Return only one JSON object with a non-empty files array; each item must contain path and complete file content. Do not include thinking or explanatory text outside the JSON object."
    messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": prompt + "\n\nGPT-OSS output override: ignore any file-marker example in the task prompt and return only the JSON object required by the system instruction.",
    })
    if image_paths:
        messages[-1]["images"] = [base64.b64encode(Path(path).read_bytes()).decode() for path in image_paths]

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": FILE_RESPONSE_SCHEMA,
        "keep_alive": orchestrator.config.get("ollama_keep_alive", 0),
    }
    think = orchestrator.config.get("ollama_think", "high")
    no_think_models = getattr(orchestrator, "_ollama_no_think_models", set())
    if think and model not in no_think_models:
        payload["think"] = think

    url = f"{orchestrator.config['ollama_url']}/api/chat"
    timeout = orchestrator.config.get("ollama_timeout", 1800)
    started = time.monotonic()
    log_info(f"GPT Ollama calling model: {model} mode=json_files")
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        if payload.get("think") and response.status_code == 400:
            log_info(f"GPT model {model} does not accept think={think}; retrying without thinking.")
            no_think_models.add(model)
            orchestrator._ollama_no_think_models = no_think_models
            retry_payload = dict(payload)
            retry_payload.pop("think")
            response = requests.post(url, json=retry_payload, timeout=timeout)
        response.raise_for_status()
        result = _file_blocks(response.json()["message"]["content"])
        log_info(f"GPT Ollama completed model={model} elapsed={time.monotonic() - started:.1f}s")
        return result
    except requests.exceptions.RequestException as error:
        log_error(f"Failed to communicate with GPT Ollama at {url}.")
        log_error(f"Error detail: {error}")
        raise RuntimeError(f"GPT Ollama connection failed: {error}") from error
