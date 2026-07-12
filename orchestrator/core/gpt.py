from __future__ import annotations

import json
import time
from copy import deepcopy

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

SECTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
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
    "required": ["sections"],
    "additionalProperties": False,
}


def is_gpt_oss(model: str | None) -> bool:
    return bool(model and model.split(":", 1)[0] == "gpt-oss")


def _file_blocks(payload: str, allowed_paths: list[str] | None = None) -> str:
    try:
        data = json.loads(payload)
        files = data["files"]
        if not isinstance(files, list) or not files:
            raise ValueError("files must be a non-empty array")
        if allowed_paths and any(item.get("path") not in allowed_paths for item in files if isinstance(item, dict)):
            raise ValueError(f"files contain paths outside task contract: {allowed_paths}")
        if not all(isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("content"), str) for item in files):
            raise ValueError("every file item must contain string path and content")
        return "\n".join(
            f"[FILE_START: {item['path']}]\n{item['content']}\n[FILE_END: {item['path']}]"
            for item in files
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"gpt-oss returned invalid JSON file payload: {error}") from error


def _section_blocks(payload: str, allowed_paths: list[str] | None = None) -> str:
    try:
        data = json.loads(payload)
        sections = data["sections"]
        if not isinstance(sections, list) or not sections:
            raise ValueError("sections must be a non-empty array")
        if allowed_paths and any(item.get("path") not in allowed_paths for item in sections if isinstance(item, dict)):
            raise ValueError(f"sections contain paths outside task contract: {allowed_paths}")
        if not all(isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("content"), str) for item in sections):
            raise ValueError("every section item must contain string path and content")
        return "\n".join(
            f"[SECTION_EDIT_START: {item['path']}]\n[HEADING]\n\n[CONTENT]\n{item['content']}\n[SECTION_EDIT_END: {item['path']}]"
            for item in sections
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"gpt-oss returned invalid JSON section payload: {error}") from error


def _target_files_from_prompt(prompt: str) -> list[str]:
    marker = "Machine contract:"
    if marker not in prompt:
        return []
    try:
        contract, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1].lstrip())
        return [path for path in contract.get("target_files", []) if isinstance(path, str)]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _schema_for_paths(schema: dict, key: str, paths: list[str]) -> dict:
    result = deepcopy(schema)
    if paths:
        result["properties"][key]["items"]["properties"]["path"]["enum"] = paths
    return result


def call(orchestrator, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str = "gpt-oss:20b", image_paths: list[str] | None = None) -> str:
    from orchestrator.core.state import log_error, log_info
    from pathlib import Path
    import base64

    section_mode = "[SECTION_EDIT_START:" in prompt or "[SECTION_EDIT_START:" in (system_prompt or "")
    allowed_paths = _target_files_from_prompt(prompt)
    response_schema = _schema_for_paths(
        SECTION_RESPONSE_SCHEMA if section_mode else FILE_RESPONSE_SCHEMA,
        "sections" if section_mode else "files",
        allowed_paths,
    )
    messages = []
    system = system_prompt or ""
    output_kind = "sections array with path and replacement content" if section_mode else "files array with path and complete file content"
    paths_hint = f" Allowed paths are exactly: {', '.join(allowed_paths)}." if allowed_paths else ""
    system += f" Return only one JSON object with a non-empty {output_kind}. Do not include thinking or explanatory text outside the JSON object.{paths_hint}"
    messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": prompt + "\n\nGPT-OSS output override: ignore any file-marker or section-marker example in the task prompt and return only the JSON object required by the system instruction.",
    })
    if image_paths:
        messages[-1]["images"] = [base64.b64encode(Path(path).read_bytes()).decode() for path in image_paths]

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": response_schema,
        "keep_alive": orchestrator.config.get("ollama_keep_alive", 0),
    }
    think = orchestrator.config.get("ollama_think", "high")
    no_think_models = getattr(orchestrator, "_ollama_no_think_models", set())
    if think and model not in no_think_models:
        payload["think"] = think

    url = f"{orchestrator.config['ollama_url']}/api/chat"
    timeout = orchestrator.config.get("ollama_timeout", 1800)
    started = time.monotonic()
    mode = "json_sections" if section_mode else "json_files"
    log_info(f"GPT Ollama calling model: {model} mode={mode}")
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
        content = response.json()["message"]["content"]
        result = _section_blocks(content, allowed_paths) if section_mode else _file_blocks(content, allowed_paths)
        log_info(f"GPT Ollama completed model={model} elapsed={time.monotonic() - started:.1f}s")
        return result
    except requests.exceptions.RequestException as error:
        log_error(f"Failed to communicate with GPT Ollama at {url}.")
        log_error(f"Error detail: {error}")
        raise RuntimeError(f"GPT Ollama connection failed: {error}") from error
