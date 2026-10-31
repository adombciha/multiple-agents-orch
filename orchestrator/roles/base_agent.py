from __future__ import annotations
import json
import re


def declared_request_files(request: str) -> list[str]:
    """Extract explicit Python/Markdown deliverables, excluding reference-only context."""
    request = re.sub(
        r"<!-- REFERENCE-ONLY:START.*?-->.*?<!-- REFERENCE-ONLY:END -->",
        "",
        request,
        flags=re.DOTALL,
    )
    files = []
    in_scope = False
    listing = False
    for line in request.replace("。", "\n").replace("；", "\n").splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.+)", line)
        if heading:
            in_scope = heading.group(1).strip().lower() in {
                "scope",
                "files",
                "target files",
                "範圍",
                "目標檔案",
            }
            listing = False
            continue
        declaration = re.search(
            r"只(?:允許|允许)?(?:新增|修改)|僅(?:允許)?(?:新增|修改)|"
            r"only (?:add|modify|create)|target files\s*:",
            line,
            re.IGNORECASE,
        )
        bullet = bool(re.match(r"\s*[-*]\s+", line))
        if not line.strip():
            continue
        if not bullet and not declaration:
            listing = False
        if declaration:
            listing = True
        if not (declaration or bullet and (in_scope or listing)):
            continue
        if re.search(
            r"不得|不要|禁止|do not|must not|never|excluded", line, re.IGNORECASE
        ):
            continue
        for path in re.findall(r"[A-Za-z0-9_.\-/]+\.(?:md|py)(?![A-Za-z0-9_])", line):
            if not path.startswith("/") and ".." not in path.split("/"):
                files.append(path.removeprefix("./"))
    return list(dict.fromkeys(files))


def missing_request_files(request: str, requirements: str) -> list[str]:
    mentioned = set(
        re.findall(r"[A-Za-z0-9_.\-/]+\.(?:md|py)(?![A-Za-z0-9_])", requirements)
    )
    return [path for path in declared_request_files(request) if path not in mentioned]


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


def extract_json_response(response: str) -> str:
    """Return the first valid JSON value, tolerating a Markdown code fence."""
    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
        ).strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return json.dumps(value, ensure_ascii=False)
        except json.JSONDecodeError:
            continue
    raise ValueError("response does not contain valid JSON")


def is_json_response(response: str) -> bool:
    try:
        extract_json_response(response)
        return True
    except (TypeError, ValueError):
        return False


def is_strict_json_response(response: str) -> bool:
    try:
        json.loads(response.strip())
        return True
    except (TypeError, json.JSONDecodeError):
        return False


def inject_ponytail_prompt(
    system_prompt: str | None, use_ponytail: bool, role: str | None = None
) -> str | None:
    """Injects PONYTAIL_PROMPT if active and role is eligible (manager, developer, reviewer)."""
    if not use_ponytail:
        return system_prompt
    is_eligible = True
    if role is not None:
        is_eligible = (
            role == "manager" or role.startswith("developer") or role == "reviewer"
        )
    if is_eligible:
        if system_prompt:
            return system_prompt + PONYTAIL_PROMPT
        else:
            return PONYTAIL_PROMPT.strip()
    return system_prompt


class BaseAgent:

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def call_agent(
        self,
        role: str,
        prompt: str,
        system_prompt: str | None = None,
        image_paths: list[str] | None = None,
        response_validator=None,
        response_schema: dict | None = None,
    ) -> str:
        return self.orchestrator.call_agent(
            role,
            prompt,
            system_prompt,
            image_paths,
            response_validator,
            response_schema,
        )

    def call_agent_ollama_fallback(
        self, role: str, prompt: str, system_prompt: str | None = None
    ) -> str:
        return self.orchestrator.call_agent_ollama_fallback(role, prompt, system_prompt)

    def call_manager(
        self,
        prompt: str,
        system_prompt: str | None = None,
        response_validator=None,
        response_schema: dict | None = None,
    ) -> str:
        return self.orchestrator.call_manager(
            prompt, system_prompt, response_validator, response_schema
        )
