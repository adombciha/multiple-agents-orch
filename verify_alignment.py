"""Check shared README examples and links without requiring line-by-line translation."""

import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

READMES = (
    "README.md",
    "README_en.md",
    "README_ja.md",
    "README_ru.md",
    "README_zh-CN.md",
)


def inventory(text):
    examples, headings = Counter(), []
    fence = None
    lines = []
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if marker:
            delimiter, language = marker.groups()
            if fence is None:
                fence = (delimiter, language.strip())
                lines = []
            elif (
                delimiter[0] == fence[0][0]
                and len(delimiter) >= len(fence[0])
                and not language.strip()
            ):
                if fence[1] not in {"", "text"}:
                    examples[(fence[1], "\n".join(lines))] += 1
                fence = None
            else:
                lines.append(line)
        elif fence is not None:
            lines.append(line)
        elif re.match(r"^#{1,6}\s+", line):
            headings.append(line)
    links = Counter(re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", text))
    errors = ["Unclosed code fence"] if fence else []
    if sum(line.startswith("# ") for line in headings) != 1:
        errors.append("Expected one document title")
    return examples, links, errors


def check_readmes(root):
    root = Path(root)
    errors, inventories = [], {}
    commands = {}
    for name in READMES:
        try:
            text = (root / name).read_text(encoding="utf-8")
            inventories[name] = inventory(text)
            commands[name] = set(
                re.findall(r"python3 orchestrator\.py ([a-z][\w-]*)", text)
            )
        except (OSError, UnicodeError) as error:
            errors.append(f"{name}: {error}")
            continue
        examples, links, issues = inventories[name]
        errors.extend(f"{name}: {issue}" for issue in issues)
        for link in links:
            parsed = urlsplit(link)
            if parsed.scheme in {"http", "https"}:
                if not parsed.netloc:
                    errors.append(f"{name}: invalid URL {link}")
            elif not parsed.scheme and parsed.path:
                if not (root / unquote(parsed.path)).exists():
                    errors.append(f"{name}: missing local link target {link}")
    if READMES[0] in inventories:
        reference_examples, reference_links, _ = inventories[READMES[0]]
        for name, (examples, links, _) in inventories.items():
            for language, example in (reference_examples - examples).elements():
                errors.append(
                    f"{name}: missing/changed {language} example: {example[:120]}"
                )
            for link in (reference_links - links).elements():
                errors.append(f"{name}: missing shared link: {link}")
            for command in commands[READMES[0]] - commands[name]:
                errors.append(f"{name}: missing CLI command: {command}")
    return errors


def main():
    errors = check_readmes(Path(__file__).resolve().parent)
    for error in errors:
        print(error)
    if not errors:
        print(
            "README examples, code fences and shared links checked. Translation meaning and remote URLs require separate review."
        )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
