from copy import deepcopy

DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    "ollama_model": "gemma4:latest",
    "ollama_keep_alive": 0,
    "ollama_timeout": 1800,
    "ollama_num_predict": 8192,
    "ollama_think": "high",
    "ollama_router_model": "tev1:4b",
    "ollama_router_enabled": True,
    "ollama_router_min_confidence": 0.5,
    "ollama_router_timeout": 10,
    "telemetry_enabled": True,
    "llm_usage_log": "llm_usage.jsonl",
    "max_visual_image_bytes": 10 * 1024 * 1024,
    "test_command": "python3 -m pytest -q",
    "max_revisions": 2,
    "max_plan_escalations": 1,  # After max_revisions narrow fixes fail a stage, give the Manager this many chances to replan before pausing for a human
    "backends": {
        "manager": "codex",
        "architect": "codex",
        "developer": "codex",
        "reviewer": "codex",
        "qa": "ollama",
        "developer_senior": "codex",
        "developer_middle": "ollama",
        "developer_junior": "ollama",
        "qa_senior": "ollama",
        "qa_middle": "ollama",
        "qa_junior": "ollama",
        "assistant": "ollama",
        "ra": "agy",
        "security": "ollama",
        "devops": "ollama",
        "uiux": "ollama",
        "uiux_visual_review": "ollama",
        "fae": "ollama",
        "integration": "ollama",
        "sales": "agy",
        "sre": "agy",
    },
    "use_ponytail": False,  # Enforces minimalist senior developer/reviewer principles (YAGNI)
    "use_worktree": True,  # Enforces isolated git worktrees for agent roles
    "backend_escalation_path": ["ollama", "agy", "codex"],
    "vendor_quota_check_enabled": False,  # Probe vendor CLIs' interactive /usage screen (via tmux) before hiring; off by default (slow, needs tmux)
    "vendor_quota_check_threshold_pct": 95,
    "model_tiers": {
        "ollama": ["gemma4:latest", "qwen3.8:latest", "deepseek-r1:7b"],
        "agy": ["auto:gemini-flash-medium"],
        "codex": ["auto"],
    },
    "role_models": {
        "manager": "auto",
        "reviewer": "auto",
        "developer_senior": "auto",
        "developer_middle": "granite4.2:8b",
        "developer_junior": "gemma4:latest",
        "qa_senior": "deepseek-r1:7b",
        "qa_middle": "ornith:9b",
        "qa_junior": "gemma4:latest",
        "assistant": "gemma4:latest",
        "architect": "auto",
        "ra": "auto:gemini-flash-medium",
        "security": "deepseek-r1:7b",
        "devops": "qwen3.8:latest",
        "uiux": "qwen3.5:9b",
        "uiux_visual_review": "gemma4:latest",
        "fae": "qwen3.5:9b",
        "integration": "granite4.2:8b",
        "sales": "auto:gemini-flash-medium",
        "sre": "auto:gemini-flash-medium",
    },
    "role_model_backends": {
        "manager": "codex",
        "reviewer": "codex",
        "developer_senior": "codex",
        "developer_middle": "ollama",
        "developer_junior": "ollama",
        "qa_senior": "ollama",
        "qa_middle": "ollama",
        "qa_junior": "ollama",
        "assistant": "ollama",
        "architect": "codex",
        "ra": "agy",
        "security": "ollama",
        "devops": "ollama",
        "uiux": "ollama",
        "uiux_visual_review": "ollama",
        "fae": "ollama",
        "integration": "ollama",
        "sales": "agy",
        "sre": "agy",
    },
    "role_model_tiers": {
        "manager": ["auto", "auto:gemini-flash-medium"],
        "reviewer": ["auto", "auto:gemini-flash-medium"],
        "qa_senior": ["deepseek-r1:7b", "gemma4:latest"],
        "qa_middle": ["ornith:9b", "gemma4:latest"],
        "architect": ["auto", "auto:gemini-flash-medium"],
        "developer_senior": ["auto"],
        "developer_middle": [],
        "developer_junior": [],
        "ra": ["auto:gemini-flash-medium"],
        "sales": ["auto:gemini-flash-medium"],
    },
    "role_model_routes": {
        "manager": [
            ["codex", "auto"],
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "auto"],
        ],
        "architect": [
            ["codex", "auto"],
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "auto"],
        ],
        "developer_senior": [
            ["codex", "auto"],
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "auto"],
        ],
        "developer_middle": [
            ["ollama", "auto"],
        ],
        "developer_junior": [
            ["ollama", "auto"],
        ],
        "qa_senior": [
            ["ollama", "auto"],
        ],
        "qa_middle": [
            ["ollama", "auto"],
        ],
        "qa_junior": [
            ["ollama", "auto"],
        ],
        "reviewer": [
            ["codex", "auto"],
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "auto"],
        ],
        "security": [
            ["ollama", "auto"],
        ],
        "devops": [
            ["ollama", "auto"],
        ],
        "uiux": [["ollama", "auto"]],
        "uiux_visual_review": [
            ["ollama", "auto"],
        ],
        "fae": [["ollama", "auto"]],
        "integration": [
            ["ollama", "auto"],
        ],
        "ra": [
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "auto"],
        ],
        "sales": [
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "auto"],
        ],
        "sre": [
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "auto"],
        ],
        "assistant": [
            ["ollama", "auto"],
        ],
    },
    "reasoning_effort": {
        "manager": "medium",
        "architect": "medium",
        "reviewer": "medium",
        "developer_senior": "low",
    },
    "staffing_limits": {
        "rd": {"senior": 1, "middle": 2, "junior": 3},
        "qa": {"senior": 1, "middle": 2, "junior": 3},
    },
    # Internal staff trust weights (ollama-backed roles only). Vendors (codex/agy)
    # are billed per token and use vendor_scores below instead of a headcount weight.
    "worker_weights": {
        "rd": {"senior": 1.0, "middle": 1.0, "junior": 1.0},
        "qa": {"senior": 1.5, "middle": 1.0, "junior": 0.6},
    },
    "qa_vote_threshold": 0.6,
    "weight_adjustment_max_delta": 0.3,
    "vendor_scores": {},
    "vendor_hire_min_jobs": 5,
    "brain_db": "brain.db",
}


def merge_defaults(defaults: dict, values: dict) -> dict:
    merged = deepcopy(defaults)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def migrate_legacy_config(values: dict) -> dict:
    migrated = deepcopy(values)
    removed = {"claude", "grok"}
    old_models = {"grok-4.5", "gpt-5.6-luna", "claude-3-5-haiku", "claude-3-7-sonnet"}
    old_models.add("Gemini 3.5 Flash (Medium)")

    for field in ("backends", "role_model_backends"):
        for role, backend in migrated.get(field, {}).items():
            if backend in removed:
                migrated[field][role] = DEFAULT_CONFIG[field].get(role, "ollama")

    for role, model in migrated.get("role_models", {}).items():
        if model in old_models:
            migrated["role_models"][role] = DEFAULT_CONFIG["role_models"].get(
                role, model
            )

    for role, models in migrated.get("role_model_tiers", {}).items():
        migrated["role_model_tiers"][role] = [
            "auto:gemini-flash-medium"
            if model.startswith("Gemini") and model in old_models
            else "auto"
            if model in old_models
            else model
            for model in models
        ]

    for role, routes in migrated.get("role_model_routes", {}).items():
        routes = [route for route in routes if route[0] not in removed]
        routes = [
            [
                backend,
                DEFAULT_CONFIG["model_tiers"].get(backend, [model])[0]
                if model in old_models
                else model,
            ]
            for backend, model in routes
        ]
        preferred = DEFAULT_CONFIG["role_model_routes"].get(role, [None])[0]
        if preferred and (not routes or routes[0][0] != preferred[0]):
            routes.insert(0, deepcopy(preferred))
        migrated["role_model_routes"][role] = routes

    for backend in removed:
        migrated.get("model_tiers", {}).pop(backend, None)
    for backend, models in migrated.get("model_tiers", {}).items():
        migrated["model_tiers"][backend] = [
            "auto:gemini-flash-medium"
            if model == "Gemini 3.5 Flash (Medium)"
            else "auto"
            if model == "gpt-5.6-luna"
            else model
            for model in models
        ]
    if "backend_escalation_path" in migrated:
        migrated["backend_escalation_path"] = [
            backend
            for backend in migrated["backend_escalation_path"]
            if backend not in removed
        ]
    return migrated
