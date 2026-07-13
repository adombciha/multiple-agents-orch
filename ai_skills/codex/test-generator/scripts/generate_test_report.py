#!/usr/bin/env python3
"""Generate stable test and RD action reports from extracted test evidence."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GAP_STATUSES = {
    "blocked",
    "partial",
    "pending",
    "unimplemented",
    "environment-dependent",
    "failed",
}
ALLOWED_STATUSES = GAP_STATUSES | {"passed", "skipped", "pass", "fail", "skip"}
REQUIRED_REPORT_KEYS = {
    "schema_version",
    "generated_at",
    "source",
    "summary",
    "cases",
    "commands",
    "artifacts",
    "environment",
    "coverage_boundary",
    "assumptions",
    "unresolved_gaps",
    "rd_actions",
}
REQUIRED_TEST_REPORT_HEADINGS = [
    "# Automated Test Report",
    "## Source",
    "## Summary",
    "## Case Results",
    "## Commands",
    "## Artifacts",
    "## Coverage Boundary",
    "## Assumptions",
    "## Unresolved Gaps",
]


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalize_status(value: Any) -> str:
    return text(value, "pending").strip().lower() or "pending"


def validate_results(results: dict[str, Any], path: Path) -> None:
    cases = as_list(results.get("cases")) or as_list(results.get("tests"))
    if not cases:
        raise ValueError(f"{path} must contain a non-empty 'cases' or 'tests' list")

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"{path} case #{index} must be an object")
        status = normalize_status(case.get("status"))
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"{path} case #{index} has unsupported status: {status}")

    for key in ("artifacts", "assumptions", "unresolved_gaps", "rd_actions", "gaps"):
        if key in results and not isinstance(results[key], list):
            raise ValueError(f"{path} field '{key}' must be a list")
    if "environment" in results and not isinstance(results["environment"], dict):
        raise ValueError(f"{path} field 'environment' must be an object")


def infer_cases(spec: dict[str, Any], results: dict[str, Any]) -> list[dict[str, str]]:
    result_cases = as_list(results.get("cases")) or as_list(results.get("tests"))
    if result_cases:
        cases = []
        for index, case in enumerate(result_cases, start=1):
            if not isinstance(case, dict):
                continue
            case_id = text(
                case.get("case_id") or case.get("id") or case.get("name"),
                f"CASE-{index:03d}",
            )
            cases.append(
                {
                    "case_id": case_id,
                    "status": normalize_status(case.get("status")),
                    "title": text(case.get("title") or case.get("name") or case_id),
                    "source": text(case.get("source") or case.get("traceability")),
                    "expected": text(case.get("expected")),
                    "actual": text(case.get("actual")),
                    "failure_reason": text(
                        case.get("failure_reason") or case.get("reason")
                    ),
                }
            )
        return cases

    cases = []
    for index, case in enumerate(as_list(spec.get("inferred_cases")), start=1):
        if not isinstance(case, dict):
            continue
        case_id = text(case.get("case_id"), f"CASE-{index:03d}")
        cases.append(
            {
                "case_id": case_id,
                "status": "pending",
                "title": text(case.get("text") or case.get("data") or case_id),
                "source": text(case.get("source"), "markdown-test-spec"),
                "expected": "",
                "actual": "",
                "failure_reason": "No executable test result was provided.",
            }
        )
    return cases


def summarize(cases: list[dict[str, str]]) -> dict[str, int]:
    counts = {
        "total": len(cases),
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "blocked": 0,
        "partial": 0,
        "pending": 0,
        "unimplemented": 0,
        "environment_dependent": 0,
    }
    aliases = {
        "pass": "passed",
        "fail": "failed",
        "skip": "skipped",
        "environment-dependent": "environment_dependent",
    }
    for case in cases:
        status = aliases.get(
            normalize_status(case.get("status")),
            normalize_status(case.get("status")).replace("-", "_"),
        )
        if status in counts:
            counts[status] += 1
    return counts


def collect_rd_actions(
    cases: list[dict[str, str]], results: dict[str, Any]
) -> list[dict[str, str]]:
    actions = []
    for item in as_list(results.get("rd_actions")) + as_list(results.get("gaps")):
        if not isinstance(item, dict):
            continue
        actions.append(
            {
                "priority": text(item.get("priority"), "P1"),
                "owner": text(item.get("owner"), "RD"),
                "topic": text(
                    item.get("topic") or item.get("title") or item.get("gap"),
                    "Unresolved test gap",
                ),
                "rationale": text(item.get("rationale") or item.get("reason")),
                "required_input": text(
                    item.get("required_input") or item.get("required_artifact")
                ),
                "affected_cases": text(item.get("affected_cases") or item.get("cases")),
                "unlocked_coverage": text(item.get("unlocked_coverage")),
                "next_step": text(
                    item.get("next_step")
                    or item.get("next_command")
                    or item.get("handoff")
                ),
            }
        )

    for case in cases:
        status = normalize_status(case.get("status"))
        if status not in GAP_STATUSES:
            continue
        actions.append(
            {
                "priority": "P0" if status in {"blocked", "failed"} else "P1",
                "owner": "RD",
                "topic": f"Resolve {status} test case {case['case_id']}",
                "rationale": case["failure_reason"] or "Test evidence is incomplete.",
                "required_input": "",
                "affected_cases": case["case_id"],
                "unlocked_coverage": "Executable regression or release-evidence coverage for the affected case.",
                "next_step": "Fix the implementation, fixture, expected output, or environment, then rerun tests and regenerate reports.",
            }
        )
    return actions


def build_report(spec: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    source = spec.get("source", {}) if isinstance(spec.get("source"), dict) else {}
    cases = infer_cases(spec, results)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": text(source.get("path")),
            "sha256": text(source.get("sha256")),
            "line_count": source.get("line_count", 0),
        },
        "summary": summarize(cases),
        "cases": cases,
        "commands": [
            text(item.get("command"))
            for item in as_list(spec.get("commands"))
            if isinstance(item, dict)
        ],
        "artifacts": [
            text(item)
            for item in (
                as_list(results.get("artifacts"))
                or as_list(results.get("generated_files"))
            )
        ],
        "environment": results.get("environment", {}),
        "coverage_boundary": text(
            results.get("coverage_boundary") or results.get("boundary")
        ),
        "assumptions": [text(item) for item in as_list(results.get("assumptions"))],
        "unresolved_gaps": [
            text(item) for item in as_list(results.get("unresolved_gaps"))
        ],
        "rd_actions": collect_rd_actions(cases, results),
    }


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(cell.replace("|", "\\|").replace("\n", "<br>") for cell in row)
            + " |"
        )
    return "\n".join(lines)


def bullet_list(items: list[Any], empty: str = "None") -> str:
    values = [text(item) for item in items if text(item)]
    return "\n".join(f"- {item}" for item in values) or f"- {empty}"


def render_test_report(report: dict[str, Any]) -> str:
    source = report["source"]
    summary = report["summary"]
    case_rows = [
        [
            case["case_id"],
            case["status"],
            case["title"],
            case["expected"],
            case["actual"],
            case["failure_reason"],
        ]
        for case in report["cases"]
    ]
    return "\n".join(
        [
            "# Automated Test Report",
            "",
            "## Source",
            "",
            f"- Path: `{source['path'] or 'Not provided'}`",
            f"- SHA-256: `{source['sha256'] or 'Not provided'}`",
            f"- Generated At: `{report['generated_at']}`",
            "",
            "## Summary",
            "",
            markdown_table(
                [
                    "Total",
                    "Passed",
                    "Failed",
                    "Skipped",
                    "Blocked",
                    "Partial",
                    "Pending",
                    "Unimplemented",
                    "Environment Dependent",
                ],
                [
                    [
                        text(summary[key])
                        for key in (
                            "total",
                            "passed",
                            "failed",
                            "skipped",
                            "blocked",
                            "partial",
                            "pending",
                            "unimplemented",
                            "environment_dependent",
                        )
                    ]
                ],
            ),
            "",
            "## Case Results",
            "",
            (
                markdown_table(
                    [
                        "Case ID",
                        "Status",
                        "Title",
                        "Expected",
                        "Actual",
                        "Failure Reason",
                    ],
                    case_rows,
                )
                if case_rows
                else "No cases were provided."
            ),
            "",
            "## Commands",
            "",
            bullet_list(
                [f"`{command}`" for command in report["commands"]], "Not provided"
            ),
            "",
            "## Artifacts",
            "",
            bullet_list(
                [f"`{artifact}`" for artifact in report["artifacts"]], "Not provided"
            ),
            "",
            "## Coverage Boundary",
            "",
            report["coverage_boundary"] or "Not provided",
            "",
            "## Assumptions",
            "",
            bullet_list(report["assumptions"]),
            "",
            "## Unresolved Gaps",
            "",
            bullet_list(report["unresolved_gaps"]),
            "",
        ]
    )


def render_rd_report(report: dict[str, Any]) -> str:
    actions = report["rd_actions"]
    if not actions:
        return "# RD Action Report\n\nNo RD actions required.\n"
    rows = [
        [
            action["priority"],
            action["owner"],
            action["topic"],
            action["rationale"],
            action["required_input"],
            action["affected_cases"],
            action["unlocked_coverage"],
            action["next_step"],
        ]
        for action in actions
    ]
    return (
        "# RD Action Report\n\n## Actions\n\n"
        + markdown_table(
            [
                "Priority",
                "Owner",
                "Topic",
                "Rationale",
                "Required Input / Artifact",
                "Affected Cases",
                "Unlocked Coverage",
                "Next Step",
            ],
            rows,
        )
        + "\n"
    )


def validate_outputs(out_dir: Path) -> None:
    required_files = [
        "test-report.json",
        "test-report.md",
        "rd-action-report.json",
        "rd-action-report.md",
    ]
    for name in required_files:
        path = out_dir / name
        if not path.exists():
            raise RuntimeError(f"Expected report was not generated: {path}")

    report = load_json(out_dir / "test-report.json")
    missing_keys = sorted(REQUIRED_REPORT_KEYS - set(report))
    if missing_keys:
        raise RuntimeError(
            f"test-report.json missing required keys: {', '.join(missing_keys)}"
        )

    markdown = (out_dir / "test-report.md").read_text(encoding="utf-8")
    missing_headings = [
        heading for heading in REQUIRED_TEST_REPORT_HEADINGS if heading not in markdown
    ]
    if missing_headings:
        raise RuntimeError(
            f"test-report.md missing required headings: {', '.join(missing_headings)}"
        )

    rd_actions = json.loads(
        (out_dir / "rd-action-report.json").read_text(encoding="utf-8")
    )
    if not isinstance(rd_actions, list):
        raise RuntimeError("rd-action-report.json root must be a list")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec", type=Path, required=True, help="Path to markdown-test-spec.json"
    )
    parser.add_argument("--results", type=Path, help="Optional test result JSON")
    parser.add_argument(
        "--out-dir", type=Path, required=True, help="Report output directory"
    )
    args = parser.parse_args()

    results = load_json(args.results) if args.results else {}
    if args.results:
        validate_results(results, args.results)
    report = build_report(load_json(args.spec), results)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "test-report.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        "test-report.md": render_test_report(report),
        "rd-action-report.json": json.dumps(
            report["rd_actions"], ensure_ascii=False, indent=2
        )
        + "\n",
        "rd-action-report.md": render_rd_report(report),
    }
    for name, content in outputs.items():
        path = args.out_dir / name
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path}")
    validate_outputs(args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
