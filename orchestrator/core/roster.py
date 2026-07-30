from __future__ import annotations
from orchestrator.core import backends, scoring

LEVELS = ("senior", "middle", "junior")
LEVEL_FALLBACK = {
    "senior": ("middle", "junior"),
    "middle": ("senior", "junior"),
    "junior": ("middle", "senior"),
}


def snapshot(orchestrator) -> dict:
    """Today's roll call: which RD/QA levels are staffed (ollama) or hireable (vendor).

    Ollama reachability is checked once at `init`/`start` (see main.configure_ollama)
    and cached in state["ollama_reachable"]; planning stays a pure, testable read.
    """
    config = orchestrator.config
    ollama_up = orchestrator.state.get("ollama_reachable", True) and "ollama" not in orchestrator.state.get("quota_exhausted_backends", {})
    available_roles = {}
    for prefix, weight_role in (("developer", "rd"), ("qa", "qa")):
        for level in LEVELS:
            role = f"{prefix}_{level}"
            backend = orchestrator.get_backend(role)
            if backend == "ollama":
                status = "available" if ollama_up else "offline"
                available_roles[role] = {
                    "backend": backend, "type": "staff", "status": status,
                    "weight": scoring.get_weight(config, weight_role, level), "paid": False,
                }
            else:
                status = "available" if backends.backend_available(orchestrator, backend) else "offline"
                model = orchestrator.get_active_model_for_role(role, backend)
                available_roles[role] = {
                    "backend": backend, "type": "vendor", "status": status,
                    "rating": scoring.get_vendor_rating(config, backend, model), "paid": True,
                }

    unavailable_backends = sorted(
        {info["backend"] for info in available_roles.values() if info["status"] == "offline"}
    )
    return {"available_roles": available_roles, "unavailable_backends": unavailable_backends}


def reassign_unavailable_levels(tasks: list[dict], roster_data: dict, role_key: str, role_prefix: str) -> None:
    """Move a task off an offline level onto the nearest staffed/hireable one."""
    available_roles = roster_data.get("available_roles", {})
    for task in tasks:
        level = task.get(role_key)
        if level not in LEVELS:
            continue
        if available_roles.get(f"{role_prefix}_{level}", {}).get("status") == "available":
            continue
        for candidate in LEVEL_FALLBACK.get(level, ()):
            if available_roles.get(f"{role_prefix}_{candidate}", {}).get("status") == "available":
                task[role_key] = candidate
                break
