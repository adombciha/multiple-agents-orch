#!/usr/bin/env python3
"""Extract a structured test-generation spec from a Markdown document."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

KEYWORD_RE = re.compile(
    r"\b(expected|actual|pass|fail|failure|tolerance|judgement|schema|report|"
    r"boundary|golden|regression|acceptance|criteria|input|output|case[_ -]?id)\b",
    re.IGNORECASE,
)
PATH_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+"
)
COMMAND_RE = re.compile(
    r"^\s*(?:python3?|pytest|qmake|make|cmake|ctest|npm|yarn|pnpm|cargo|go test|"
    r"mvn|gradle|java|./)[^\n]*$"
)


def parse_fences(lines: list[str]) -> tuple[list[dict[str, Any]], set[int]]:
    blocks: list[dict[str, Any]] = []
    fenced_lines: set[int] = set()
    in_block = False
    start_line = 0
    language = ""
    body: list[str] = []

    for index, line in enumerate(lines, start=1):
        stripped = line.rstrip("\n")
        if stripped.startswith("```"):
            fenced_lines.add(index)
            if not in_block:
                in_block = True
                start_line = index
                language = stripped[3:].strip()
                body = []
            else:
                blocks.append(
                    {
                        "start_line": start_line,
                        "end_line": index,
                        "language": language,
                        "content": "\n".join(body),
                    }
                )
                in_block = False
                start_line = 0
                language = ""
                body = []
            continue
        if in_block:
            fenced_lines.add(index)
            body.append(stripped)

    if in_block:
        blocks.append(
            {
                "start_line": start_line,
                "end_line": len(lines),
                "language": language,
                "content": "\n".join(body),
                "unterminated": True,
            }
        )
    return blocks, fenced_lines


def parse_headings(lines: list[str], fenced_lines: set[int]) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if index in fenced_lines:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append(
                {
                    "line": index,
                    "level": len(match.group(1)),
                    "title": match.group(2).strip(),
                }
            )
    return headings


def split_table_row(line: str) -> list[str]:
    row = line.strip().strip("|")
    return [cell.strip() for cell in row.split("|")]


def is_separator_row(line: str) -> bool:
    cells = split_table_row(line)
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells
    )


def parse_tables(lines: list[str], fenced_lines: set[int]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    index = 0
    while index < len(lines) - 1:
        line_no = index + 1
        if line_no in fenced_lines:
            index += 1
            continue
        current = lines[index].rstrip("\n")
        nxt = lines[index + 1].rstrip("\n")
        if "|" in current and "|" in nxt and is_separator_row(nxt):
            headers = split_table_row(current)
            rows: list[dict[str, str]] = []
            raw_rows: list[list[str]] = []
            end = index + 2
            while end < len(lines):
                end_line_no = end + 1
                candidate = lines[end].rstrip("\n")
                if (
                    end_line_no in fenced_lines
                    or "|" not in candidate
                    or not candidate.strip()
                ):
                    break
                cells = split_table_row(candidate)
                raw_rows.append(cells)
                rows.append(
                    {
                        (
                            headers[column]
                            if column < len(headers)
                            else f"column_{column + 1}"
                        ): (cells[column] if column < len(cells) else "")
                        for column in range(max(len(headers), len(cells)))
                    }
                )
                end += 1
            tables.append(
                {
                    "start_line": line_no,
                    "end_line": end,
                    "headers": headers,
                    "rows": rows,
                    "raw_rows": raw_rows,
                }
            )
            index = end
            continue
        index += 1
    return tables


def extract_keywords(lines: list[str], fenced_lines: set[int]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if index in fenced_lines:
            continue
        found = sorted({match.group(0).lower() for match in KEYWORD_RE.finditer(line)})
        if found:
            matches.append({"line": index, "keywords": found, "text": line.strip()})
    return matches


def extract_paths(text: str) -> list[str]:
    return sorted(set(PATH_RE.findall(text)))


def extract_commands(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for block in blocks:
        language = str(block.get("language", "")).lower()
        content = str(block.get("content", ""))
        if language in {"bash", "sh", "shell", "console", "text", ""}:
            for offset, line in enumerate(content.splitlines()):
                if COMMAND_RE.match(line):
                    commands.append(
                        {
                            "line": int(block["start_line"]) + offset + 1,
                            "command": line.strip(),
                            "block_start_line": block["start_line"],
                        }
                    )
    return commands


def infer_cases(
    tables: list[dict[str, Any]], keyword_lines: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    case_like = re.compile(
        r"(case|id|well|sample|expected|actual|status|judgement|target)", re.IGNORECASE
    )
    for table in tables:
        headers = table.get("headers", [])
        if any(case_like.search(header) for header in headers):
            for row_index, row in enumerate(table.get("rows", []), start=1):
                case_id = ""
                for key, value in row.items():
                    if re.search(r"case|id", key, re.IGNORECASE) and value:
                        case_id = value
                        break
                cases.append(
                    {
                        "source": "table",
                        "table_start_line": table["start_line"],
                        "row_number": row_index,
                        "case_id": case_id
                        or f"table_{table['start_line']}_row_{row_index}",
                        "data": row,
                    }
                )

    for item in keyword_lines:
        text = item["text"]
        match = re.search(r"\b([A-Z][A-Z0-9]+-\d{2,}|[A-Z]+_\d{2,})\b", text)
        if match:
            cases.append(
                {
                    "source": "keyword_line",
                    "line": item["line"],
                    "case_id": match.group(1),
                    "text": text,
                }
            )
    return cases


def build_spec(markdown_path: Path) -> dict[str, Any]:
    text = markdown_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    blocks, fenced_lines = parse_fences([line + "\n" for line in lines])
    headings = parse_headings(lines, fenced_lines)
    tables = parse_tables(lines, fenced_lines)
    keyword_lines = extract_keywords(lines, fenced_lines)
    commands = extract_commands(blocks)

    return {
        "source": {
            "path": str(markdown_path),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "line_count": len(lines),
        },
        "headings": headings,
        "tables": tables,
        "code_blocks": blocks,
        "commands": commands,
        "keyword_lines": keyword_lines,
        "referenced_paths": extract_paths(text),
        "inferred_cases": infer_cases(tables, keyword_lines),
        "summary": {
            "heading_count": len(headings),
            "table_count": len(tables),
            "code_block_count": len(blocks),
            "command_count": len(commands),
            "keyword_line_count": len(keyword_lines),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", type=Path, help="Markdown file to parse")
    parser.add_argument("--out", type=Path, required=True, help="JSON output path")
    args = parser.parse_args()

    if not args.markdown.exists():
        parser.error(f"Markdown file does not exist: {args.markdown}")
    if not args.markdown.is_file():
        parser.error(f"Markdown path is not a file: {args.markdown}")

    spec = build_spec(args.markdown)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {args.out}")
    print(json.dumps(spec["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
