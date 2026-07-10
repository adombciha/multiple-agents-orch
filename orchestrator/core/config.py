from copy import deepcopy

DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    "ollama_model": "gemma4:latest",
    "test_command": "python3 -m pytest -q",
    "max_revisions": 2,
    "backends": {
        "manager": "codex",
        "architect": "agy",
        "developer": "codex",
        "reviewer": "codex",
        "qa": "ollama",
        "developer_senior": "codex",
        "developer_middle": "codex",
        "developer_junior": "agy",
        "qa_senior": "ollama",
        "qa_middle": "ollama",
        "qa_junior": "ollama",
        "assistant": "ollama",
        "ra": "grok",
        "security": "ollama",
        "sales": "grok",
        "sre": "agy"
    },
    "use_ponytail": False,  # Enforces minimalist senior developer/reviewer principles (YAGNI)
    "use_worktree": True,   # Enforces isolated git worktrees for agent roles
    "backend_escalation_path": ["ollama", "agy", "codex"],
    "model_tiers": {
        "ollama": ["gemma4:latest", "gemma2:2b", "gemma2:9b"],
        "agy": ["gemini-3.5-flash", "gemini-3.1-pro"],
        "codex": ["gpt-5.6-sol"],
        "claude": ["claude-3-5-haiku", "claude-3-7-sonnet"],
        "grok": ["grok-4.5"],
    },
    "role_models": {
        "manager": "gpt-5.6-sol",
        "reviewer": "gpt-5.6-sol",
        "developer_senior": "gpt-5.6-terra",
        "developer_middle": "gpt-5.6-luna",
        "developer_junior": "gemini-3.5-flash",
        "qa_senior": "qwen2.5-coder:14b",
        "qa_middle": "deepseek-coder-v2:latest",
        "qa_junior": "gemma4:latest",
        "assistant": "gemma4:latest",
        "architect": "gemini-3.1-pro",
        "ra": "grok-4.5",
        "security": "deepseek-r1:latest",
        "sales": "grok-4.5",
        "sre": "gemini-3.1-pro"
    },
    "role_model_backends": {
        "manager": "codex",
        "reviewer": "codex",
        "developer_senior": "codex",
        "developer_middle": "codex",
        "developer_junior": "agy",
        "qa_senior": "ollama",
        "qa_middle": "ollama",
        "qa_junior": "ollama",
        "assistant": "ollama",
        "architect": "agy",
        "ra": "grok",
        "security": "ollama",
        "sales": "grok",
        "sre": "agy"
    },
    "role_model_tiers": {
        "manager": ["gpt-5.6-sol", "gpt-5.6-terra"],
        "reviewer": ["gpt-5.6-sol", "gpt-5.6-terra"],
        "qa_senior": ["qwen2.5-coder:14b"],
        "qa_middle": ["deepseek-coder-v2:latest"],
        "ra": ["grok-4.5"],
        "sales": ["grok-4.5"]
    },
    "staffing_limits": {
        "rd": {"senior": 1, "middle": 2, "junior": 3},
        "qa": {"senior": 1, "middle": 2, "junior": 3}
    },
    "qa_ollama_fallback_model": "qwen2.5-coder:7b",
}


def merge_defaults(defaults: dict, values: dict) -> dict:
    merged = deepcopy(defaults)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged
