from __future__ import annotations
import json

WEIGHT_MIN = 0.2
WEIGHT_MAX = 3.0
ALPHA = 0.3
EVENT_SCALE = 0.1


def clamp(value: float, lo: float = WEIGHT_MIN, hi: float = WEIGHT_MAX) -> float:
    return max(lo, min(hi, value))


def _ewma_update(current: float, score: float) -> float:
    target = current + score * EVENT_SCALE
    return clamp((1 - ALPHA) * current + ALPHA * target)


def _save_config(orchestrator) -> None:
    orchestrator.config_path.write_text(
        json.dumps(orchestrator.config, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_weight(config: dict, role: str, level: str) -> float:
    """Internal staff trust weight for a role/level (e.g. role='rd'|'qa')."""
    return float(config.get("worker_weights", {}).get(role, {}).get(level, 1.0))


def record_staff_event(orchestrator, role: str, level: str, score: float) -> float:
    """Update an internal staff member's trust weight (ollama-backed roles only)."""
    weights = orchestrator.config.setdefault("worker_weights", {}).setdefault(role, {})
    updated = round(_ewma_update(float(weights.get(level, 1.0)), score), 3)
    weights[level] = updated
    _save_config(orchestrator)
    return updated


def get_vendor_rating(config: dict, backend: str, model: str | None) -> float:
    key = f"{backend}/{model or 'unknown'}"
    return float(config.get("vendor_scores", {}).get(key, {}).get("rating", 1.0))


def record_vendor_event(orchestrator, backend: str, model: str | None, score: float) -> float:
    """Update a paid vendor's scorecard rating; internal staff (ollama) never lands here."""
    key = f"{backend}/{model or 'unknown'}"
    vendors = orchestrator.config.setdefault("vendor_scores", {})
    entry = vendors.setdefault(key, {"rating": 1.0, "jobs": 0, "failures": 0})
    entry["rating"] = round(_ewma_update(float(entry.get("rating", 1.0)), score), 3)
    entry["jobs"] = int(entry.get("jobs", 0)) + 1
    if score < 0:
        entry["failures"] = int(entry.get("failures", 0)) + 1
    _save_config(orchestrator)
    return entry["rating"]


def record_event(orchestrator, role: str, level: str, backend: str, model: str | None, score: float) -> float:
    """Route a performance event to the internal staff weight or the vendor scorecard.

    Whether a (role, level) counts as staff or vendor for this event depends on which
    physical backend actually delivered it, not the role's default configuration.
    """
    if backend == "ollama":
        return record_staff_event(orchestrator, role, level, score)
    return record_vendor_event(orchestrator, backend, model, score)
