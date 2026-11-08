"""Read a pytest log and summarize its last valid statistics line."""

import argparse
import json
from pathlib import Path
import re
import sys

_KEYS = ("passed", "failed", "errors", "skipped", "warnings")
_COUNT = (
    r"[0-9]+ (?:subtests )?"
    r"(?:passed|failed|errors?|skipped|warnings?|deselected|xfailed|xpassed|reruns?)"
)
_STATISTICS = re.compile(
    rf"(?P<counts>{_COUNT}(?:, {_COUNT})*|no tests ran)"
    r"(?: in [0-9]+(?:\.[0-9]+)?s(?: \([0-9:]+\))?)?"
)
_ALIASES = {"error": "errors", "warning": "warnings"}


def summarize(text):
    """Return five counts from the last complete pytest statistics line."""
    for line in reversed(text.splitlines()):
        candidate = line.strip().strip("=").strip()
        match = _STATISTICS.fullmatch(candidate)
        if match is None:
            continue

        result = dict.fromkeys(_KEYS, 0)
        counts = match.group("counts")
        if counts != "no tests ran":
            for item in counts.split(", "):
                number, label = item.split(" ", 1)
                label = _ALIASES.get(label, label)
                if label in result:
                    result[label] += int(number)
        return result

    raise ValueError("找不到有效的 pytest 測試摘要")


def main(argv=None):
    parser = argparse.ArgumentParser(description="唯讀摘要 UTF-8 pytest 測試紀錄。")
    parser.add_argument("log_path", type=Path, help="pytest 紀錄檔案路徑")
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出五項計數")
    args = parser.parse_args(argv)

    try:
        text = args.log_path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        print(
            f"錯誤：UTF-8 解碼失敗：{args.log_path}：{exc}",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(
            f"錯誤：無法讀取檔案：{args.log_path}：{exc}",
            file=sys.stderr,
        )
        return 2

    try:
        counts = summarize(text)
    except ValueError as exc:
        print(f"錯誤：{args.log_path}：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(counts))
    else:
        print(
            f"通過：{counts['passed']}，失敗：{counts['failed']}，"
            f"錯誤：{counts['errors']}，略過：{counts['skipped']}，"
            f"警告：{counts['warnings']}"
        )
    return 1 if counts["failed"] or counts["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
