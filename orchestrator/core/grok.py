from __future__ import annotations

import json
import subprocess
import time


def extract_schema_payload(output: str, schema: dict) -> str:
    """Extract the schema-shaped assistant payload from Grok CLI's JSON envelope."""
    try:
        envelope = json.loads(output)
    except json.JSONDecodeError:
        return output

    required = set(schema.get("required", []))

    def find(value):
        if isinstance(value, dict):
            if required.issubset(value):
                return value
            for child in value.values():
                match = find(child)
                if match is not None:
                    return match
        elif isinstance(value, list):
            for child in value:
                match = find(child)
                if match is not None:
                    return match
        elif isinstance(value, str):
            try:
                return find(json.loads(value))
            except json.JSONDecodeError:
                return None
        return None

    payload = find(envelope)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else output


def call(orchestrator, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None, response_schema: dict | None = None) -> str:
    from orchestrator.core.state import log_info

    structured = response_schema is not None
    coding = role.startswith("developer")
    full_prompt = prompt if structured or coding else (f"{system_prompt}\n\n{prompt}" if system_prompt else prompt)
    model = model or orchestrator.get_active_model_for_role(role, "grok") or "grok-4.5"
    effort = orchestrator.config.get("reasoning_effort", {}).get(role)
    cmd = ["grok", "-p", full_prompt, "-m", model]
    if effort:
        cmd.extend(["--effort", effort])
    if structured:
        cmd.extend([
            "--json-schema", json.dumps(response_schema, ensure_ascii=False, separators=(",", ":")),
            "--output-format", "json",
            "--system-prompt-override",
            "Return exactly one JSON object matching the supplied schema. Do not inspect files, use tools, plan, call subagents, explain, or output Markdown.",
            "--max-turns", "1", "--no-plan", "--no-subagents", "--no-memory",
            "--disable-web-search", "--verbatim",
        ])
    elif coding:
        cmd.extend([
            "--system-prompt-override",
            "Apply the requested edit mentally and output only the exact file or section blocks requested by the prompt. Do not inspect files, use tools, plan, call subagents, explain, or output prose.",
            "--max-turns", "1", "--no-plan", "--no-subagents", "--no-memory",
            "--disable-web-search", "--verbatim",
        ])

    mode = "structured" if structured else "coding" if coding else "default"
    started = time.monotonic()
    log_info(f"Running Grok Build: grok -p ... -m {model} --effort {effort or 'default'} mode={mode}")
    result = subprocess.run(
        cmd, cwd=orchestrator.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=1800, check=False,
    )
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        log_info(f"Grok failed role={role} model={model} elapsed={elapsed:.1f}s")
        raise RuntimeError(f"Grok Build failed with code {result.returncode}:\n{result.stderr}")
    output = extract_schema_payload(result.stdout, response_schema) if structured else result.stdout
    log_info(f"Grok completed role={role} model={model} elapsed={elapsed:.1f}s output_chars={len(result.stdout)} payload_chars={len(output)}")
    return output
