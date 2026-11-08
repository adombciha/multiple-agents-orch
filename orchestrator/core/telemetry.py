from __future__ import annotations

import json
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path


def _token_count(model: str | None, messages: list[dict]) -> tuple[int | None, str]:
    try:
        from litellm import token_counter
    except ImportError:
        return None, "litellm_unavailable"

    try:
        return int(token_counter(model=model or "unknown", messages=messages)), "exact"
    except Exception:
        return None, "unsupported_model"


def _error_category(error: Exception | None) -> str | None:
    if error is None:
        return None
    from orchestrator.core.cloud_routing import ModelDiscoveryError

    if isinstance(error, ModelDiscoveryError):
        return "discovery"
    text = str(error).lower()
    if any(
        marker in text
        for marker in ("quota", "rate limit", "429", "spending-limit", "token budget")
    ):
        return "quota"
    if "cancel" in type(error).__name__.lower() or "cancel" in text:
        return "cancelled"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "contract" in text or "json" in text or "file block" in text:
        return "contract"
    return type(error).__name__


def _usage_path(orchestrator) -> Path:
    return orchestrator.ai_dir / orchestrator.config.get(
        "llm_usage_log", "llm_usage.jsonl"
    )


def current_run_id(orchestrator) -> str | None:
    value = orchestrator.state.get("run_id")
    if not isinstance(value, str):
        return None
    try:
        identity = uuid.UUID(value)
    except ValueError:
        return None
    return value if identity.version == 4 and identity.hex == value else None


def worker_call_evidence(orchestrator) -> list[dict]:
    """Return current-run local call completions, not output-quality verdicts."""
    run_id = current_run_id(orchestrator)
    if run_id is None:
        return []
    roles = {
        f"{prefix}_{level}": (role, level)
        for prefix, role in (("developer", "rd"), ("qa", "qa"))
        for level in ("senior", "middle", "junior")
    }
    recent = defaultdict(lambda: deque(maxlen=8))
    try:
        with _usage_path(orchestrator).open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    return []
                source_role = record.get("role")
                category = record.get("error_category")
                if (
                    record.get("run_id") != run_id
                    or record.get("backend") != "ollama"
                    or not isinstance(source_role, str)
                    or source_role not in roles
                    or type(record.get("success")) is not bool
                    or (category is not None and not isinstance(category, str))
                ):
                    continue
                if record["success"] and category is not None:
                    continue
                if not record["success"] and category not in {"timeout", "contract"}:
                    # Quota, cancellation and unknown infrastructure failures are not staff evidence.
                    continue
                role, level = roles[source_role]
                recent[(role, level)].append(
                    {
                        "id": f"call:{number}",
                        "run_id": run_id,
                        "source": "backend_call",
                        "source_role": source_role,
                        "role": role,
                        "level": level,
                        "backend": "ollama",
                        "completed": record["success"],
                        "error_category": category,
                    }
                )
    except (OSError, UnicodeError, ValueError):
        return []
    return [item for key in sorted(recent) for item in recent[key]]


def record_call(
    orchestrator,
    *,
    role: str,
    backend: str,
    model: str | None,
    prompt: str,
    system_prompt: str | None,
    output: str | None,
    elapsed_seconds: float,
    image_count: int = 0,
    error: Exception | None = None,
    provider_usage: dict | None = None,
) -> None:
    if not orchestrator.config.get("telemetry_enabled", True):
        return

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    input_tokens, token_status = _token_count(model, messages)
    output_tokens = None
    if output is not None:
        output_tokens, output_status = _token_count(
            model, [{"role": "assistant", "content": output}]
        )
        if token_status == "exact" and output_status != "exact":
            token_status = output_status

    if isinstance(provider_usage, dict):
        input_tokens = provider_usage.get(
            "prompt_tokens", provider_usage.get("input_tokens", input_tokens)
        )
        output_tokens = provider_usage.get(
            "completion_tokens", provider_usage.get("output_tokens", output_tokens)
        )
        token_status = "provider"

    total_tokens = None
    if input_tokens is not None or output_tokens is not None:
        total_tokens = (input_tokens or 0) + (output_tokens or 0)

    record = {
        "run_id": current_run_id(orchestrator),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "backend": backend,
        "model": model,
        "success": error is None,
        "error_category": _error_category(error),
        "elapsed_ms": round(elapsed_seconds * 1000, 1),
        "input_characters": len(prompt) + len(system_prompt or ""),
        "output_characters": len(output or ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "token_status": token_status,
        "image_count": image_count,
    }
    path = _usage_path(orchestrator)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
    except OSError:
        # Telemetry must never change workflow behavior.
        return


def summary_markdown(orchestrator) -> str:
    path = _usage_path(orchestrator)
    if not path.exists():
        return ""

    records = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return ""
    if not records:
        return ""

    by_model = defaultdict(
        lambda: {"calls": 0, "failed": 0, "input": 0, "output": 0, "total": 0}
    )
    for record in records:
        item = by_model[record.get("model") or "unknown"]
        item["calls"] += 1
        item["failed"] += int(not record.get("success", False))
        item["input"] += record.get("input_tokens") or 0
        item["output"] += record.get("output_tokens") or 0
        item["total"] += record.get("total_tokens") or 0

    lines = [
        "## LiteLLM Token Usage",
        "",
        f"- Calls: {len(records)}",
        "- Prompt/output content is not stored; only metadata and token counts are recorded.",
        "",
        "| Model | Calls | Failed | Input tokens | Output tokens | Total tokens |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for model, item in sorted(by_model.items()):
        lines.append(
            f"| `{model}` | {item['calls']} | {item['failed']} | {item['input']} | {item['output']} | {item['total']} |"
        )
    return "\n".join(lines)
