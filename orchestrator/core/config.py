from copy import deepcopy

DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    "ollama_model": "gemma4:latest",
    "ollama_keep_alive": 0,
    "ollama_timeout": 1800,
    "ollama_num_predict": 8192,
    "ollama_think": "high",
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
        "ollama": ["gemma4:latest", "gemma2:2b", "gemma2:9b"],
        "agy": ["auto:gemini-flash-medium"],
        "codex": ["auto"],
    },
    "role_models": {
        "manager": "auto",
        "reviewer": "auto",
        "developer_senior": "auto",
        "developer_middle": "granite4.1:8b",
        "developer_junior": "gemma4:latest",
        "qa_senior": "gemma4:latest",
        "qa_middle": "gemma4:latest",
        "qa_junior": "gemma4:latest",
        "assistant": "gemma4:latest",
        "architect": "auto",
        "ra": "auto:gemini-flash-medium",
        "security": "deepseek-coder:6.7b",
        "devops": "qwen2.5-coder:14b",
        "uiux": "qwen3:8b",
        "uiux_visual_review": "llama3.2-vision:latest",
        "fae": "qwen3:8b",
        "integration": "deepseek-coder:6.7b",
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
        "qa_senior": ["deepseek-r1:7b"],
        "qa_middle": ["qwen2.5-coder:14b"],
        "architect": ["auto", "auto:gemini-flash-medium"],
        "developer_senior": ["auto", "gpt-oss:20b", "granite4.1:8b"],
        "developer_middle": ["granite4.1:8b", "codegemma:7b", "qwen2.5-coder:7b"],
        "developer_junior": ["gemma4:latest", "qwen2.5-coder:7b", "codegemma:7b"],
        "ra": ["auto:gemini-flash-medium"],
        "sales": ["auto:gemini-flash-medium"],
    },
    "role_model_routes": {
        "manager": [
            ["codex", "auto"],
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "qwen3:8b"],
        ],
        "architect": [
            ["codex", "auto"],
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "gemma4:latest"],
        ],
        "developer_senior": [
            ["codex", "auto"],
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "gpt-oss:20b"],
            ["ollama", "granite4.1:8b"],
        ],
        "developer_middle": [
            ["ollama", "granite4.1:8b"],
            ["ollama", "codegemma:7b"],
            ["ollama", "qwen2.5-coder:7b"],
        ],
        "developer_junior": [
            ["ollama", "gemma4:latest"],
            ["ollama", "qwen2.5-coder:7b"],
            ["ollama", "codegemma:7b"],
        ],
        "qa_senior": [["ollama", "gemma4:latest"], ["ollama", "qwen2.5-coder:7b"]],
        "qa_middle": [["ollama", "gemma4:latest"], ["ollama", "qwen2.5-coder:7b"]],
        "qa_junior": [
            ["ollama", "gemma4:latest"],
            ["ollama", "codegemma:7b"],
            ["ollama", "qwen2.5-coder:7b"],
        ],
        "reviewer": [
            ["codex", "auto"],
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "gemma4:latest"],
        ],
        "security": [
            ["ollama", "deepseek-coder:6.7b"],
            ["ollama", "gemma4:latest"],
            ["ollama", "qwen2.5-coder:7b"],
        ],
        "devops": [
            ["ollama", "qwen2.5-coder:14b"],
            ["ollama", "deepseek-coder-v2:latest"],
            ["ollama", "qwen2.5-coder:7b"],
        ],
        "uiux": [["ollama", "qwen3:8b"], ["ollama", "gemma4:latest"]],
        "uiux_visual_review": [
            ["ollama", "llama3.2-vision:latest"],
            ["ollama", "gemma4:latest"],
        ],
        "fae": [["ollama", "qwen3:8b"], ["ollama", "gemma4:latest"]],
        "integration": [
            ["ollama", "deepseek-coder:6.7b"],
            ["ollama", "qwen3:8b"],
            ["ollama", "gemma4:latest"],
        ],
        "ra": [
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "qwen3:8b"],
            ["ollama", "gemma4:latest"],
        ],
        "sales": [
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "qwen3:8b"],
            ["ollama", "gemma4:latest"],
        ],
        "sre": [
            ["agy", "auto:gemini-flash-medium"],
            ["ollama", "qwen3:8b"],
        ],
        "assistant": [
            ["ollama", "gemma4:latest"],
            ["ollama", "qwen3:8b"],
            ["ollama", "qwen2.5-coder:7b"],
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
    "qa_ollama_fallback_model": "qwen2.5-coder:7b",
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
            migrated["role_models"][role] = DEFAULT_CONFIG["role_models"].get(role, model)

    for role, models in migrated.get("role_model_tiers", {}).items():
        migrated["role_model_tiers"][role] = [
            "auto:gemini-flash-medium" if model.startswith("Gemini") and model in old_models
            else "auto" if model in old_models else model
            for model in models
        ]

    for role, routes in migrated.get("role_model_routes", {}).items():
        routes = [route for route in routes if route[0] not in removed]
        routes = [
            [backend, DEFAULT_CONFIG["model_tiers"].get(backend, [model])[0] if model in old_models else model]
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
            "auto:gemini-flash-medium" if model == "Gemini 3.5 Flash (Medium)"
            else "auto" if model == "gpt-5.6-luna" else model
            for model in models
        ]
    if "backend_escalation_path" in migrated:
        migrated["backend_escalation_path"] = [
            backend for backend in migrated["backend_escalation_path"] if backend not in removed
        ]
    return migrated
