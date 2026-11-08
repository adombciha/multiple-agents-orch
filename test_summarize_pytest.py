import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.summarize_pytest import main, summarize


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1184 passed, 46 subtests passed in 52.68s", (1184, 0, 0, 0, 0)),
        ("1 failed, 12 passed, 2 warnings in 1.2s", (12, 1, 0, 0, 2)),
        ("=== 2 errors, 3 skipped in 0.12s ===", (0, 0, 2, 3, 0)),
        ("1 error, 1 warning", (0, 0, 1, 0, 1)),
        ("2 errors, 3 warnings", (0, 0, 2, 0, 3)),
        ("46 subtests passed in 0.10s", (0, 0, 0, 0, 0)),
        ("1 passed, 2 subtests failed in 0.10s", (1, 0, 0, 0, 0)),
        ("1 xfailed, 2 xpassed, 3 deselected, 1 rerun", (0, 0, 0, 0, 0)),
        ("=== no tests ran in 0.01s ===", (0, 0, 0, 0, 0)),
        ("  === 4 passed in 3600.00s (1:00:00) ===  \r\n", (4, 0, 0, 0, 0)),
    ],
)
def test_summary_formats(text, expected):
    assert summarize(text) == dict(
        zip(("passed", "failed", "errors", "skipped", "warnings"), expected)
    )


def test_last_valid_summary_wins():
    text = (
        "1 failed in 0.01s\n"
        "=== 12 passed, 46 subtests passed in 1.20s ===\n"
        "FAILED test_example.py::test_example - 99 failed\n"
        "diagnostic: 200 passed in 0.01s\n"
        "1 passed in invalid\n"
        "trailing diagnostic output\n"
    )
    assert summarize(text) == {
        "passed": 12,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "warnings": 0,
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "collected 2 items\n",
        "test_example.py::test_example PASSED\n",
        "FAILED test_example.py::test_example - 1 failed\n",
        "diagnostic: 2 passed in 0.01s",
        "=== short test summary info ===",
        "-1 passed",
        "1 passed in invalid",
        "1 passed, unknown",
        "1 passed trailing text",
        "__import__('os').system('echo unwanted')",
    ],
)
def test_missing_summary(text):
    with pytest.raises(ValueError, match="找不到有效的 pytest 測試摘要"):
        summarize(text)


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        ("2 passed", 0),
        ("1 failed, 12 passed, 2 warnings in 1.2s", 1),
        ("1 error", 1),
        ("2 errors", 1),
        ("3 skipped, 1 warning", 0),
    ],
)
def test_main_output(tmp_path, capsys, json_mode, text, expected_code):
    path = tmp_path / "測試紀錄.txt"
    path.write_text(text, encoding="utf-8")
    original = path.read_bytes()
    arguments = [str(path)] + (["--json"] if json_mode else [])

    assert main(arguments) == expected_code

    captured = capsys.readouterr()
    assert captured.err == ""
    expected = summarize(text)
    if json_mode:
        result = json.loads(captured.out)
        assert result == expected
        assert all(type(value) is int for value in result.values())
    else:
        assert captured.out == (
            f"通過：{expected['passed']}，失敗：{expected['failed']}，"
            f"錯誤：{expected['errors']}，略過：{expected['skipped']}，"
            f"警告：{expected['warnings']}\n"
        )
    assert path.read_bytes() == original


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("missing", "無法讀取檔案"),
        ("directory", "無法讀取檔案"),
        ("invalid_utf8", "UTF-8 解碼失敗"),
        ("no_summary", "找不到有效的 pytest 測試摘要"),
    ],
)
def test_main_errors(tmp_path, capsys, json_mode, scenario, message):
    path = tmp_path / "record.txt"
    if scenario == "directory":
        path = tmp_path
    elif scenario == "invalid_utf8":
        path.write_bytes(b"1 passed\n\xff")
    elif scenario == "no_summary":
        path.write_text("collected 2 items\n", encoding="utf-8")

    assert main([str(path)] + (["--json"] if json_mode else [])) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert str(path) in captured.err
    if scenario == "missing":
        assert not path.exists()


def test_permission_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "record.txt"

    def deny_read(self, *args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "read_text", deny_read)

    assert main([str(path), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "無法讀取檔案" in captured.err
    assert "permission denied" in captured.err
    assert str(path) in captured.err


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize(
    ("content", "expected_code", "message"),
    [
        (b"2 passed in 0.01s\n", 0, ""),
        (b"1 failed, 2 passed in 0.10s\n", 1, ""),
        (b"2 errors in 0.10s\n", 1, ""),
        (b"no summary\n", 2, "找不到有效"),
        (b"1 passed\n\xff", 2, "UTF-8"),
        (None, 2, "無法讀取"),
    ],
)
def test_module_cli(tmp_path, json_mode, content, expected_code, message):
    path = tmp_path / "record.txt"
    if content is not None:
        path.write_bytes(content)
    original = path.read_bytes() if path.exists() else None

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "scripts.summarize_pytest",
            str(path),
        ]
        + (["--json"] if json_mode else []),
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == expected_code
    if expected_code == 2:
        assert result.stdout == ""
        assert message in result.stderr
        assert str(path) in result.stderr
    else:
        assert result.stderr == ""
        expected = summarize(content.decode("utf-8"))
        if json_mode:
            assert json.loads(result.stdout) == expected
        else:
            assert "通過：" in result.stdout
            assert "錯誤：" in result.stdout
    if original is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == original
