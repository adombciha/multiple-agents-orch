from copy import deepcopy

DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    "ollama_model": "gemma4:latest",
    "ollama_keep_alive": 0,
    "ollama_timeout": 1800,
    "max_visual_image_bytes": 10 * 1024 * 1024,
    "test_command": "python3 -m pytest -q",
    "max_revisions": 2,
    "backends": {
        "manager": "codex",
        "architect": "ollama",
        "developer": "codex",
        "reviewer": "codex",
        "qa": "ollama",
        "developer_senior": "codex",
        "developer_middle": "codex",
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
        "developer_junior": "deepseek-r1:7b",
        "qa_senior": "deepseek-r1:7b",
        "qa_middle": "qwen2.5-coder:14b",
        "qa_junior": "gemma4:latest",
        "assistant": "gemma4:latest",
        "architect": "deepseek-r1:7b",
        "ra": "grok-4.5",
        "security": "deepseek-r1:7b",
        "devops": "qwen2.5-coder:14b",
        "uiux": "qwen3:8b",
        "uiux_visual_review": "llama3.2-vision:latest",
        "fae": "qwen3:8b",
        "integration": "deepseek-r1:7b",
        "sales": "grok-4.5",
        "sre": "gemini-3.1-pro"
    },
    "role_model_backends": {
        "manager": "codex",
        "reviewer": "codex",
        "developer_senior": "codex",
        "developer_middle": "codex",
        "developer_junior": "ollama",
        "qa_senior": "ollama",
        "qa_middle": "ollama",
        "qa_junior": "ollama",
        "assistant": "ollama",
        "architect": "ollama",
        "ra": "grok",
        "security": "ollama",
        "devops": "ollama",
        "uiux": "ollama",
        "uiux_visual_review": "ollama",
        "fae": "ollama",
        "integration": "ollama",
        "sales": "grok",
        "sre": "agy"
    },
    "role_model_tiers": {
        "manager": ["gpt-5.6-sol", "gpt-5.6-terra"],
        "reviewer": ["gpt-5.6-sol", "gpt-5.6-terra"],
        "qa_senior": ["deepseek-r1:7b"],
        "qa_middle": ["qwen2.5-coder:14b"],
        "architect": ["deepseek-r1:7b"],
        "developer_junior": ["deepseek-r1:7b"],
        "ra": ["grok-4.5"],
        "sales": ["grok-4.5"]
    },
    "role_model_routes": {
        "manager": [["codex", "gpt-5.6-sol"], ["agy", "gemini-3.5-flash"], ["ollama", "qwen3:8b"]],
        "architect": [["ollama", "deepseek-r1:7b"], ["ollama", "qwen3:8b"], ["ollama", "qwen2.5-coder:14b"]],
        "developer_senior": [["codex", "gpt-5.6-terra"], ["ollama", "qwen2.5-coder:14b"], ["ollama", "deepseek-coder-v2:latest"]],
        "developer_middle": [["codex", "gpt-5.6-luna"], ["ollama", "qwen2.5-coder:14b"], ["ollama", "codegemma:7b"]],
        "developer_junior": [["ollama", "deepseek-r1:7b"], ["ollama", "codegemma:7b"], ["ollama", "qwen2.5-coder:7b"]],
        "qa_senior": [["ollama", "deepseek-r1:7b"], ["ollama", "qwen2.5-coder:14b"], ["ollama", "qwen2.5-coder:7b"]],
        "qa_middle": [["ollama", "qwen2.5-coder:14b"], ["ollama", "deepseek-coder-v2:latest"], ["ollama", "gemma4:latest"]],
        "qa_junior": [["ollama", "gemma4:latest"], ["ollama", "codegemma:7b"], ["ollama", "qwen2.5-coder:7b"]],
        "reviewer": [["codex", "gpt-5.6-sol"], ["ollama", "deepseek-r1:7b"], ["ollama", "qwen2.5-coder:14b"]],
        "security": [["ollama", "deepseek-r1:7b"], ["ollama", "phi4-reasoning:14b"], ["ollama", "qwen2.5-coder:14b"]],
        "devops": [["ollama", "qwen2.5-coder:14b"], ["ollama", "deepseek-coder-v2:latest"], ["ollama", "qwen2.5-coder:7b"]],
        "uiux": [["ollama", "qwen3:8b"], ["ollama", "gemma4:latest"]],
        "uiux_visual_review": [["ollama", "llama3.2-vision:latest"], ["ollama", "gemma4:latest"]],
        "fae": [["ollama", "qwen3:8b"], ["ollama", "gemma4:latest"]],
        "integration": [["ollama", "deepseek-r1:7b"], ["ollama", "qwen2.5-coder:14b"], ["ollama", "deepseek-coder-v2:latest"]],
        "ra": [["grok", "grok-4.5"], ["ollama", "qwen3:8b"], ["ollama", "gemma4:latest"]],
        "sales": [["grok", "grok-4.5"], ["ollama", "qwen3:8b"], ["ollama", "gemma4:latest"]],
        "assistant": [["ollama", "gemma4:latest"], ["ollama", "qwen3:8b"], ["ollama", "qwen2.5-coder:7b"]],
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
