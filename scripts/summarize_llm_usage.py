"""Summarize recorded LLM call durations without importing the orchestrator.

Telemetry stores elapsed_ms in milliseconds, success as a boolean, and
error_category as a nullable string. Categories are counted verbatim; this
reader does not classify exceptions or infer relationships between calls.
"""

import argparse
import json
import math
from pathlib import Path
import sys


class _ArgumentParser(argparse.ArgumentParser):

    def error(self, message):
        raise ValueError("參數錯誤：請提供 PATH 與 --run-id RUN_ID，並檢查選項。")

    def format_help(self):
        return super().format_help().replace("usage: ", "用法：", 1)


def summarize(path, run_id):
    """Parse all JSON before validating and aggregating matching records."""
    records = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                raise ValueError(f"第 {number} 行：JSON 格式無效。") from None
            if isinstance(record, dict) and record.get("run_id") == run_id:
                records.append((number, record))

    groups = {}
    for number, record in records:

        def invalid(message):
            return ValueError(f"第 {number} 行：{message}")

        for field in ("role", "backend", "success", "elapsed_ms", "error_category"):
            if field not in record:
                raise invalid(f"缺少必要欄位：{field}。")
        for field in ("role", "backend"):
            if not isinstance(record[field], str) or not record[field].strip():
                raise invalid(f"{field} 必須是非空白字串。")
        if type(record["success"]) is not bool:
            raise invalid("success 必須是布林值。")

        model = record.get("model")
        if model is not None and not isinstance(model, str):
            raise invalid("model 必須是字串或 null。")
        model = model if model and model.strip() else "未知"

        category = record["error_category"]
        if category is not None and (
            not isinstance(category, str) or not category.strip()
        ):
            raise invalid("error_category 必須是非空白字串或 null。")

        elapsed = record["elapsed_ms"]
        if type(elapsed) not in (int, float):
            raise invalid("elapsed_ms 必須是有限非負數，且不可為布林值。")
        try:
            elapsed = float(elapsed)
        except OverflowError:
            raise invalid("elapsed_ms 必須是有限非負數。") from None
        if not math.isfinite(elapsed) or elapsed < 0:
            raise invalid("elapsed_ms 必須是有限非負數。")

        key = (record["role"], record["backend"], model)
        if key not in groups:
            groups[key] = {
                "role": key[0],
                "backend": key[1],
                "model": key[2],
                "calls": 0,
                "successful": 0,
                "failed": 0,
                "total_elapsed_ms": 0.0,
                "average_elapsed_ms": 0.0,
                "error_categories": {},
            }
        group = groups[key]
        total = group["total_elapsed_ms"] + elapsed
        if not math.isfinite(total):
            raise invalid("累計耗時超出可表示的有限數值範圍。")
        group["calls"] += 1
        group["successful"] += int(record["success"])
        group["failed"] += int(not record["success"])
        group["total_elapsed_ms"] = total
        if not record["success"]:
            label = category if category is not None else "未知"
            counts = group["error_categories"]
            counts[label] = counts.get(label, 0) + 1

    for group in groups.values():
        group["average_elapsed_ms"] = group["total_elapsed_ms"] / group["calls"]
        group["error_categories"] = dict(sorted(group["error_categories"].items()))
    ordered = sorted(
        groups.values(),
        key=lambda group: (
            -group["total_elapsed_ms"],
            group["role"],
            group["backend"],
            group["model"],
        ),
    )
    return {"run_id": run_id, "duration_unit": "ms", "groups": ordered}


def main(argv=None):
    parser = _ArgumentParser(
        description="唯讀統計指定執行識別碼的模型呼叫耗時。",
        usage="%(prog)s PATH --run-id RUN_ID [--json]",
        add_help=False,
        allow_abbrev=False,
    )
    parser._positionals.title = "位置參數"
    parser._optionals.title = "選項"
    parser.add_argument("-h", "--help", action="help", help="顯示說明並結束")
    parser.add_argument("path", metavar="PATH", help="UTF-8 JSONL 紀錄路徑")
    parser.add_argument("--run-id", required=True, metavar="RUN_ID", help="執行識別碼")
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出摘要")
    try:
        args = parser.parse_args(argv)
        if "\0" in args.path:
            raise ValueError("參數錯誤：PATH 包含無效字元。")
        if not args.run_id.strip() or not args.path.strip():
            raise ValueError("參數錯誤：PATH 與 RUN_ID 不可為空白。")
    except ValueError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    try:
        result = summarize(args.path, args.run_id)
    except (OSError, UnicodeError):
        print("錯誤：無法讀取 UTF-8 紀錄檔案。", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    if not result["groups"]:
        print("找不到指定執行識別碼的紀錄。", file=sys.stderr)
        return 1

    if args.json:
        output = json.dumps(result, ensure_ascii=False, allow_nan=False)
    else:
        lines = [
            "模型呼叫耗時摘要（單位：毫秒）",
            "成功僅表示紀錄中的呼叫成功，不代表 QA 或 Reviewer 通過。",
            "累計呼叫耗時不是流程實際經過時間；不推斷重試、GPU 使用量或模型品質。",
        ]
        for group in result["groups"]:
            names = [group["role"], group["backend"], group["model"]]
            lines.extend(
                [
                    "",
                    "角色／後端／模型：" + json.dumps(names, ensure_ascii=False),
                    f"呼叫次數：{group['calls']}，成功：{group['successful']}，失敗：{group['failed']}",
                    f"累計耗時：{group['total_elapsed_ms']:.3f} 毫秒，平均耗時：{group['average_elapsed_ms']:.3f} 毫秒",
                    "錯誤分類次數："
                    + json.dumps(group["error_categories"], ensure_ascii=False),
                ]
            )
        output = "\n".join(lines)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
