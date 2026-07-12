from __future__ import annotations

PONYTAIL_PROMPT = (
    "\n\n[PONYTAIL RULE ACTIVE]\n"
    "Enforce the laziest solution that actually works, simplest, shortest, most minimal. "
    "Channel a senior developer who has seen everything:\n"
    "- Climb the ladder:\n"
    "  1. Does this need to exist at all? (YAGNI)\n"
    "  2. Already in this codebase? Reuse it. Look before you write.\n"
    "  3. Stdlib does it? Use it.\n"
    "  4. Native platform feature covers it? Use it.\n"
    "  5. Already-installed dependency solves it? Use it.\n"
    "  6. Can it be one line? One line.\n"
    "  7. Only then: the minimum code that works.\n"
    "- No unrequested abstractions: no interface with one implementation, no factory for one product, no config for a value that never changes.\n"
    "- Deletion over addition. Shortest working diff wins.\n"
    "- Non-trivial logic must leave one runnable check behind."
)

def inject_ponytail_prompt(system_prompt: str | None, use_ponytail: bool, role: str | None = None) -> str | None:
    """Injects PONYTAIL_PROMPT if active and role is eligible (manager, developer, reviewer)."""
    if not use_ponytail:
        return system_prompt
    is_eligible = True
    if role is not None:
        is_eligible = (role == "manager" or role.startswith("developer") or role == "reviewer")
    if is_eligible:
        if system_prompt:
            return system_prompt + PONYTAIL_PROMPT
        else:
            return PONYTAIL_PROMPT.strip()
    return system_prompt

class BaseAgent:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def call_agent(self, role: str, prompt: str, system_prompt: str | None = None, image_paths: list[str] | None = None, response_validator=None) -> str:
        return self.orchestrator.call_agent(role, prompt, system_prompt, image_paths, response_validator)

    def call_agent_ollama_fallback(self, role: str, prompt: str, system_prompt: str | None = None) -> str:
        return self.orchestrator.call_agent_ollama_fallback(role, prompt, system_prompt)

    def call_manager(self, prompt: str, system_prompt: str | None = None) -> str:
        return self.orchestrator.call_manager(prompt, system_prompt)
