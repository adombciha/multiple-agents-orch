"""Read a test record and print a Traditional Chinese summary."""

import argparse
from pathlib import Path
import re
import sys

_COUNT = r"\d+ (?:subtests )?(?:passed|failed|skipped|deselected|xfailed|xpassed|errors?|warnings?|reruns?)"
_STATISTICS = re.compile(
    rf"(?:{_COUNT}(?:, {_COUNT})*|no tests ran)"
    r"(?: in \d+(?:\.\d+)?s(?: \([\d:]+\))?)?"
)


def summarize(text):
    """Return the command, recorded exit code, and last pytest statistics."""
    fields = {}
    lines = text.splitlines()
    for line in lines:
        if line.strip() == "Output:":
            break
        name, separator, value = line.partition(":")
        if separator and name.strip() in ("Command", "Exit Code"):
            fields[name.strip()] = value.strip()

    for name in ("Command", "Exit Code"):
        if not fields.get(name):
            raise ValueError(f"缺少必要欄位或欄位為空：{name}")

    raw_code = fields["Exit Code"]
    if not re.fullmatch(r"[+-]?[0-9]+", raw_code):
        raise ValueError(f"Exit Code 必須是整數：{raw_code!r}")
    try:
        exit_code = int(raw_code)
    except ValueError:
        raise ValueError(f"Exit Code 整數無法解析：{raw_code!r}") from None

    statistics = "未知"
    for line in lines:
        candidate = line.strip().strip("=").strip()
        if _STATISTICS.fullmatch(candidate):
            statistics = line.strip()

    return fields["Command"], exit_code, statistics


def main(argv=None):
    parser = argparse.ArgumentParser(description="唯讀摘要測試紀錄。")
    parser.add_argument("--file", required=True, type=Path, help="測試紀錄檔案路徑")
    args = parser.parse_args(argv)

    try:
        text = args.file.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        print(f"錯誤：檔案不存在：{args.file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as exc:
        print(f"錯誤：無法讀取檔案：{args.file}：{exc}", file=sys.stderr)
        return 1

    try:
        command, exit_code, statistics = summarize(text)
    except ValueError as exc:
        print(f"錯誤：{args.file}：{exc}", file=sys.stderr)
        return 1

    print(f"測試命令（Command）：{command}")
    print(f"原始測試退出碼（Exit Code）：{exit_code}")
    print(f"pytest 統計：{statistics}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
