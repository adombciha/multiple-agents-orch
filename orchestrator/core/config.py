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
        "manager": "grok",
        "architect": "grok",
        "developer": "codex",
        "reviewer": "grok",
        "qa": "ollama",
        "developer_senior": "codex",
        "developer_middle": "ollama",
        "developer_junior": "ollama",
        "qa_senior": "ollama",
        "qa_middle": "ollama",
        "qa_junior": "ollama",
        "assistant": "ollama",
        "ra": "grok",
        "security": "ollama",
        "devops": "ollama",
        "uiux": "ollama",
        "uiux_visual_review": "ollama",
        "fae": "ollama",
        "integration": "ollama",
        "sales": "grok",
        "sre": "agy",
    },
    "use_ponytail": False,  # Enforces minimalist senior developer/reviewer principles (YAGNI)
    "use_worktree": True,  # Enforces isolated git worktrees for agent roles
    "backend_escalation_path": ["ollama", "agy", "codex"],
    "vendor_quota_check_enabled": False,  # Probe vendor CLIs' interactive /usage screen (via tmux) before hiring; off by default (slow, needs tmux)
    "vendor_quota_check_threshold_pct": 95,
    "model_tiers": {
        "ollama": ["gemma4:latest", "gemma2:2b", "gemma2:9b"],
        "agy": ["Gemini 3.5 Flash (Medium)", "Gemini 3.1 Pro (Low)"],
        "codex": ["gpt-5.6-luna"],
        "claude": ["claude-3-5-haiku", "claude-3-7-sonnet"],
        "grok": ["grok-4.5"],
    },
    "role_models": {
        "manager": "grok-4.5",
        "reviewer": "grok-4.5",
        "developer_senior": "gpt-5.6-luna",
        "developer_middle": "granite4.1:8b",
        "developer_junior": "gemma4:latest",
        "qa_senior": "gemma4:latest",
        "qa_middle": "gemma4:latest",
        "qa_junior": "gemma4:latest",
        "assistant": "gemma4:latest",
        "architect": "grok-4.5",
        "ra": "grok-4.5",
        "security": "deepseek-coder:6.7b",
        "devops": "qwen2.5-coder:14b",
        "uiux": "qwen3:8b",
        "uiux_visual_review": "llama3.2-vision:latest",
        "fae": "qwen3:8b",
        "integration": "deepseek-coder:6.7b",
        "sales": "grok-4.5",
        "sre": "Gemini 3.1 Pro (Low)",
    },
    "role_model_backends": {
        "manager": "grok",
        "reviewer": "grok",
        "developer_senior": "codex",
        "developer_middle": "ollama",
        "developer_junior": "ollama",
        "qa_senior": "ollama",
        "qa_middle": "ollama",
        "qa_junior": "ollama",
        "assistant": "ollama",
        "architect": "grok",
        "ra": "grok",
        "security": "ollama",
        "devops": "ollama",
        "uiux": "ollama",
        "uiux_visual_review": "ollama",
        "fae": "ollama",
        "integration": "ollama",
        "sales": "grok",
        "sre": "agy",
    },
    "role_model_tiers": {
        "manager": ["grok-4.5", "gpt-5.6-luna"],
        "reviewer": ["grok-4.5", "gpt-5.6-luna"],
        "qa_senior": ["deepseek-r1:7b"],
        "qa_middle": ["qwen2.5-coder:14b"],
        "architect": ["grok-4.5", "gpt-5.6-luna"],
        "developer_senior": ["gpt-5.6-luna", "gpt-oss:20b", "granite4.1:8b"],
        "developer_middle": ["granite4.1:8b", "codegemma:7b", "qwen2.5-coder:7b"],
        "developer_junior": ["gemma4:latest", "qwen2.5-coder:7b", "codegemma:7b"],
        "ra": ["grok-4.5"],
        "sales": ["grok-4.5"],
    },
    "role_model_routes": {
        "manager": [
            ["grok", "grok-4.5"],
            ["codex", "gpt-5.6-luna"],
            ["ollama", "qwen3:8b"],
        ],
        "architect": [
            ["grok", "grok-4.5"],
            ["codex", "gpt-5.6-luna"],
            ["ollama", "gemma4:latest"],
        ],
        "developer_senior": [
            ["codex", "gpt-5.6-luna"],
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
            ["grok", "grok-4.5"],
            ["codex", "gpt-5.6-luna"],
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
            ["grok", "grok-4.5"],
            ["ollama", "qwen3:8b"],
            ["ollama", "gemma4:latest"],
        ],
        "sales": [
            ["grok", "grok-4.5"],
            ["ollama", "qwen3:8b"],
            ["ollama", "gemma4:latest"],
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
    # Internal staff trust weights (ollama-backed roles only). Vendors (claude/codex/agy/grok)
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
