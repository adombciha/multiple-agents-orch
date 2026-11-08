"""Offline CLI tests using synthetic telemetry and automatically cleaned fixtures."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.summarize_llm_usage import main


class SummarizeLLMUsageTests(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "呼叫紀錄.jsonl"

    def record(self, **changes):
        record = {
            "run_id": "run-a",
            "role": "developer_senior",
            "backend": "ollama",
            "model": "model-a",
            "elapsed_ms": 10.0,
            "success": True,
            "error_category": None,
        }
        record.update(changes)
        return record

    def write(self, records):
        self.path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False) + "\n" for record in records
            ),
            encoding="utf-8",
        )

    def invoke(self, arguments=None):
        if arguments is None:
            arguments = [str(self.path), "--run-id", "run-a", "--json"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def result(self):
        code, output, error = self.invoke()
        self.assertEqual(code, 0, error)
        self.assertEqual(error, "")
        result = json.loads(output)
        self.assertEqual(result["run_id"], "run-a")
        self.assertEqual(result["duration_unit"], "ms")
        return result["groups"]

    def assert_error(self, message, line=None, json_mode=True):
        arguments = [str(self.path), "--run-id", "run-a"]
        if json_mode:
            arguments.append("--json")
        code, output, error = self.invoke(arguments)
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("錯誤：", error)
        self.assertIn(message, error)
        if line is not None:
            self.assertIn(f"第 {line} 行", error)
        self.assertNotIn("SECRET_RECORD", error)
        self.assertNotIn("Traceback", error)
        return error

    def test_group_statistics_and_recorded_categories(self):
        categories = [
            "discovery",
            "quota",
            "cancelled",
            "timeout",
            "contract",
            "RuntimeError",
        ]
        records = [self.record(elapsed_ms=0), self.record(elapsed_ms=2.5)]
        records.extend(
            self.record(success=False, error_category=category, elapsed_ms=1.25)
            for category in categories
        )
        records.append(
            self.record(success=False, error_category="timeout", elapsed_ms=10)
        )
        self.write(records)
        original = self.path.read_bytes()

        self.assertEqual(
            self.result(),
            [
                {
                    "role": "developer_senior",
                    "backend": "ollama",
                    "model": "model-a",
                    "calls": 9,
                    "successful": 2,
                    "failed": 7,
                    "total_elapsed_ms": 20.0,
                    "average_elapsed_ms": 20.0 / 9,
                    "error_categories": {
                        "RuntimeError": 1,
                        "cancelled": 1,
                        "contract": 1,
                        "discovery": 1,
                        "quota": 1,
                        "timeout": 2,
                    },
                }
            ],
        )
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_other_runs_do_not_require_valid_fields(self):
        self.write(
            [
                self.record(run_id="run-b", elapsed_ms=float("nan"), success="bad"),
                {"run_id": "run-b", "role": {"SECRET_RECORD": True}},
                self.record(run_id=None, elapsed_ms=False),
                {"unrelated": "SECRET_RECORD"},
                None,
                [],
                42,
                self.record(elapsed_ms=7),
            ]
        )
        groups = self.result()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["calls"], 1)
        self.assertEqual(groups[0]["total_elapsed_ms"], 7)

    def test_sort_uses_total_then_role_backend_model(self):
        specifications = [
            ("b", "a", "a", 5),
            ("a", "b", "a", 5),
            ("a", "a", "b", 5),
            ("a", "a", "a", 2),
            ("a", "a", "a", 3),
            ("z", "z", "z", 6),
        ]
        self.write(
            [
                self.record(role=role, backend=backend, model=model, elapsed_ms=elapsed)
                for role, backend, model, elapsed in specifications
            ]
        )
        first = self.result()
        self.assertEqual(
            [(group["role"], group["backend"], group["model"]) for group in first],
            [
                ("z", "z", "z"),
                ("a", "a", "a"),
                ("a", "a", "b"),
                ("a", "b", "a"),
                ("b", "a", "a"),
            ],
        )
        self.assertEqual(first[1]["calls"], 2)
        self.assertEqual(first, self.result())

    def test_missing_null_and_blank_model_are_unknown(self):
        missing = self.record()
        del missing["model"]
        self.write(
            [
                missing,
                self.record(model=None),
                self.record(model=""),
                self.record(model=" \t"),
            ]
        )
        groups = self.result()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["model"], "未知")
        self.assertEqual(groups[0]["calls"], 4)

    def test_null_failure_category_is_unknown(self):
        self.write([self.record(success=False, error_category=None)])
        self.assertEqual(self.result()[0]["error_categories"], {"未知": 1})

    def test_blank_lines_and_milliseconds(self):
        self.path.write_text(
            "\n \t\n" + json.dumps(self.record(elapsed_ms=1250)) + "\n\n",
            encoding="utf-8",
        )
        group = self.result()[0]
        self.assertEqual(group["total_elapsed_ms"], 1250)
        self.assertEqual(group["average_elapsed_ms"], 1250)

    def test_all_json_is_parsed_before_target_fields(self):
        self.path.write_text(
            '{"run_id":"run-a"}\n\n' '{"run_id":"run-b","SECRET_RECORD": broken}\n',
            encoding="utf-8",
        )
        for json_mode in (False, True):
            with self.subTest(json_mode=json_mode):
                error = self.assert_error("JSON 格式無效", 3, json_mode)
                self.assertNotIn("必要欄位", error)

    def test_invalid_json_after_valid_record_never_prints_statistics(self):
        for tail in ('{"SECRET_RECORD":', '{"run_id":"run-b"} trailing', "NaN extra"):
            for json_mode in (False, True):
                with self.subTest(tail=tail, json_mode=json_mode):
                    self.path.write_text(
                        json.dumps(self.record()) + "\n\n" + tail + "\n",
                        encoding="utf-8",
                    )
                    self.assert_error("JSON 格式無效", 3, json_mode)

    def test_missing_required_fields(self):
        for field in ("role", "backend", "success", "elapsed_ms", "error_category"):
            with self.subTest(field=field):
                record = self.record()
                del record[field]
                self.write([self.record(), record])
                self.assert_error(f"缺少必要欄位：{field}", 2)

    def test_invalid_field_types_do_not_leak_record_values(self):
        cases = [
            ("role", None),
            ("role", 3),
            ("role", ""),
            ("role", " \t"),
            ("backend", []),
            ("backend", False),
            ("backend", ""),
            ("success", 1),
            ("success", 0),
            ("success", "SECRET_RECORD"),
            ("success", None),
            ("model", 3),
            ("model", ["SECRET_RECORD"]),
            ("error_category", {}),
            ("error_category", True),
            ("error_category", 1),
            ("error_category", " \t"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.write([self.record(), self.record(**{field: value})])
                self.assert_error(field, 2)

    def test_invalid_elapsed_values(self):
        values = [
            True,
            False,
            -1,
            -0.1,
            float("nan"),
            float("inf"),
            -float("inf"),
            "SECRET_RECORD",
            None,
            [],
            {},
            10**400,
        ]
        for value in values:
            for json_mode in (False, True):
                with self.subTest(value=value, json_mode=json_mode):
                    self.write([self.record(), self.record(elapsed_ms=value)])
                    self.assert_error("elapsed_ms", 2, json_mode)

    def test_total_overflow_does_not_emit_nonfinite_json(self):
        self.write([self.record(elapsed_ms=1e308), self.record(elapsed_ms=1e308)])
        self.assert_error("累計耗時", 2)

    def test_target_field_error_precedes_not_found(self):
        self.write([self.record(run_id="run-b"), {"run_id": "run-a"}])
        self.assert_error("必要欄位", 2)

    def test_no_matching_run_returns_one(self):
        for records in ([], [self.record(run_id="run-b")], [{"run_id": None}]):
            for json_mode in (False, True):
                with self.subTest(records=records, json_mode=json_mode):
                    self.write(records)
                    arguments = [str(self.path), "--run-id", "run-a"]
                    if json_mode:
                        arguments.append("--json")
                    code, output, error = self.invoke(arguments)
                    self.assertEqual(code, 1)
                    self.assertEqual(output, "")
                    self.assertIn("找不到指定執行識別碼", error)

    def test_file_errors(self):
        self.assert_error("無法讀取")
        self.assertFalse(self.path.exists())
        self.path.mkdir()
        self.assert_error("無法讀取")
        self.path.rmdir()
        self.path.write_bytes(b"\xff")
        self.assert_error("無法讀取")

    def test_permission_and_mid_read_errors_are_private(self):
        for exception in (PermissionError("SECRET_RECORD"), OSError("SECRET_RECORD")):
            with self.subTest(exception=type(exception).__name__):
                with patch.object(Path, "open", side_effect=exception):
                    self.assert_error("無法讀取")
        stream = io.StringIO(json.dumps(self.record()) + "\n")

        class BrokenReader:

            def __enter__(self):
                return self

            def __exit__(self, *args):
                stream.close()

            def __iter__(self):
                yield stream.readline()
                raise OSError("SECRET_RECORD")

        with patch.object(Path, "open", return_value=BrokenReader()):
            self.assert_error("無法讀取")

    def test_parameter_errors_are_chinese_and_private(self):
        cases = [
            [],
            [str(self.path)],
            ["--run-id", "run-a"],
            [str(self.path), "--run-id"],
            [str(self.path), "--run-id", ""],
            [str(self.path), "--run-id", " \t"],
            ["", "--run-id", "run-a"],
            ["bad\0path", "--run-id", "run-a"],
            [str(self.path), "--run-id", "run-a", "--SECRET_RECORD"],
            [str(self.path), "--run", "run-a"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                code, output, error = self.invoke(arguments)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("參數錯誤", error)
                self.assertNotIn("SECRET_RECORD", error)
                self.assertNotIn("usage:", error)

    def test_human_output_explains_limits_in_chinese(self):
        self.write([self.record(success=False, error_category="timeout", model=None)])
        code, output, error = self.invoke([str(self.path), "--run-id", "run-a"])
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        for text in (
            "單位：毫秒",
            "角色／後端／模型",
            "未知",
            "呼叫次數：1",
            "成功：0",
            "失敗：1",
            "累計耗時：10.000",
            "平均耗時：10.000",
            "錯誤分類次數",
            '"timeout": 1',
            "不代表 QA 或 Reviewer 通過",
            "不是流程實際經過時間",
            "不推斷重試、GPU 使用量或模型品質",
        ):
            self.assertIn(text, output)

    def test_module_cli_exit_codes_and_json(self):
        self.write([self.record()])
        original = self.path.read_bytes()
        cases = [
            ([str(self.path), "--run-id", "run-a", "--json"], 0, ""),
            ([str(self.path), "--run-id", "run-a"], 0, ""),
            ([str(self.path), "--run-id", "run-b", "--json"], 1, "找不到"),
            ([str(self.directory / "missing"), "--run-id", "run-a"], 2, "無法讀取"),
            ([], 2, "參數錯誤"),
            (["--help"], 0, ""),
        ]
        for arguments, expected_code, message in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "scripts.summarize_llm_usage",
                        *arguments,
                    ],
                    cwd=Path(__file__).resolve().parent,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_code, result.stderr)
                if expected_code:
                    self.assertEqual(result.stdout, "")
                    self.assertIn(message, result.stderr)
                else:
                    self.assertEqual(result.stderr, "")
                    if "--json" in arguments:
                        self.assertEqual(
                            json.loads(result.stdout)["groups"][0]["calls"], 1
                        )
                    elif "--help" in arguments:
                        self.assertIn("用法：", result.stdout)
                        self.assertIn("選項", result.stdout)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
