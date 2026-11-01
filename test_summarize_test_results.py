from pathlib import Path
import subprocess
import sys

import pytest

from scripts.summarize_test_results import main


def write_record(tmp_path, text):
    path = tmp_path / "test_results.txt"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("code", "statistics"),
    [
        (0, "================ 2 passed in 0.12s ================"),
        (1, "================ 1 failed, 1 passed in 0.12s ================"),
        (2, "================ 1 error in 0.12s ================"),
        (5, "no tests ran in 0.01s"),
        (-9, "未知"),
    ],
)
def test_success_preserves_test_exit_code(tmp_path, capsys, code, statistics):
    path = write_record(
        tmp_path,
        f"Command: python -m pytest -q\nExit Code: {code}\n"
        f"Output:\n{statistics if statistics != '未知' else 'process terminated'}\n",
    )
    original = path.read_bytes()

    assert main(["--file", str(path)]) == 0

    captured = capsys.readouterr()
    assert captured.out == (
        "測試命令（Command）：python -m pytest -q\n"
        f"原始測試退出碼（Exit Code）：{code}\n"
        f"pytest 統計：{statistics}\n"
    )
    assert captured.err == ""
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "last",
    [
        "== 1 failed, 3 passed, 1 skipped, 2 warnings in 1.23s ==",
        "1007 passed, 46 subtests passed in 50.93s",
    ],
)
def test_last_statistics_line_wins(tmp_path, capsys, last):
    path = write_record(
        tmp_path,
        "Command: pytest\nExit Code: 1\nOutput:\n"
        "2 passed in 0.01s\n"
        "================ short test summary info ================\n"
        "FAILED test_example.py::test_failure - AssertionError\n"
        f"{last}\ntrailing diagnostic output\n",
    )

    assert main(["--file", str(path)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"pytest 統計：{last}"


@pytest.mark.parametrize(
    "output",
    [
        "",
        "collected 2 items\n",
        "test_passed.py::test_passed PASSED\n",
        "FAILED test_example.py::test_failed - 1 failed\n",
        "diagnostic: 2 passed in 0.01s\n",
        "================ short test summary info ================\n",
    ],
)
def test_missing_statistics(tmp_path, capsys, output):
    path = write_record(tmp_path, f"Command: pytest\nExit Code: 0\nOutput:\n{output}")

    assert main(["--file", str(path)]) == 0
    assert capsys.readouterr().out.endswith("pytest 統計：未知\n")


@pytest.mark.parametrize(
    "statistics",
    [
        "1 passed",
        "1007 passed, 46 subtests passed in 50.93s",
        "1 failed, 2 passed, 3 skipped, 4 deselected in 0.10s",
        "1 xfailed, 2 xpassed, 1 warning in 0.10s",
        "1 error, 2 errors in 0.10s",
        "1 rerun, 2 reruns, 1 passed in 0.10s",
        "1 passed in 3600.00s (1:00:00)",
        "================ no tests ran in 0.00s ================",
    ],
)
def test_statistics_variants(tmp_path, capsys, statistics):
    path = write_record(
        tmp_path, f"Command: pytest\nExit Code: 0\nOutput:\n{statistics}"
    )

    assert main(["--file", str(path)]) == 0
    assert capsys.readouterr().out.endswith(f"pytest 統計：{statistics}\n")


def test_bom_crlf_whitespace_and_colon_in_command(tmp_path, capsys):
    path = write_record(
        tmp_path,
        "\ufeff  Command: python -m pytest C:/tests/test_example.py  \r\n"
        "  Exit Code: +001  \r\nOutput:\r\n1 failed in 0.01s",
    )

    assert main(["--file", str(path)]) == 0
    captured = capsys.readouterr()
    assert (
        "測試命令（Command）：python -m pytest C:/tests/test_example.py\n"
        in captured.out
    )
    assert "原始測試退出碼（Exit Code）：1\n" in captured.out


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("", "Command"),
        ("Exit Code: 0\n", "Command"),
        ("Command: pytest\n", "Exit Code"),
        ("Command:   \nExit Code: 0\n", "Command"),
        ("Command: pytest\nExit Code:   \n", "Exit Code"),
        ("Output:\nCommand: pytest\nExit Code: 0\n", "Command"),
    ],
)
def test_missing_or_empty_required_fields(tmp_path, capsys, text, field):
    path = write_record(tmp_path, text)

    assert main(["--file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"缺少必要欄位或欄位為空：{field}" in captured.err
    assert str(path) in captured.err


@pytest.mark.parametrize("code", ["abc", "1.0", "1_0", "0 extra", "+", "0x01"])
def test_invalid_exit_code(tmp_path, capsys, code):
    path = write_record(tmp_path, f"Command: pytest\nExit Code: {code}\n")

    assert main(["--file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Exit Code 必須是整數" in captured.err
    assert repr(code) in captured.err


def test_output_does_not_override_recorded_fields(tmp_path, capsys):
    path = write_record(
        tmp_path,
        "Command: pytest\nExit Code: 1\nOutput:\n"
        "Command: misleading command\nExit Code: 0\n1 failed in 0.01s\n",
    )

    assert main(["--file", str(path)]) == 0
    captured = capsys.readouterr()
    assert "測試命令（Command）：pytest\n" in captured.out
    assert "原始測試退出碼（Exit Code）：1\n" in captured.out


def test_missing_file(tmp_path, capsys):
    path = tmp_path / "missing.txt"

    assert main(["--file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "檔案不存在" in captured.err
    assert str(path) in captured.err
    assert not path.exists()


def test_directory_is_unreadable(tmp_path, capsys):
    assert main(["--file", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "無法讀取檔案" in captured.err
    assert str(tmp_path) in captured.err


def test_permission_error(tmp_path, monkeypatch, capsys):
    path = write_record(tmp_path, "Command: pytest\nExit Code: 0\n")

    def deny_read(self, *args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "read_text", deny_read)

    assert main(["--file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "無法讀取檔案" in captured.err
    assert "permission denied" in captured.err


def test_invalid_utf8(tmp_path, capsys):
    path = tmp_path / "invalid.txt"
    path.write_bytes(b"Command: pytest\nExit Code: 0\nOutput:\n\xff")

    assert main(["--file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "無法讀取檔案" in captured.err


@pytest.mark.parametrize(
    ("text", "expected_code", "expected_output"),
    [
        (
            "Command: pytest\nExit Code: 0\nOutput:\n1 passed in 0.01s\n",
            0,
            "原始測試退出碼（Exit Code）：0",
        ),
        (
            "Command: pytest\nExit Code: 1\nOutput:\n1 failed in 0.01s\n",
            0,
            "原始測試退出碼（Exit Code）：1",
        ),
        ("Command: pytest\nExit Code: invalid\n", 1, "Exit Code 必須是整數"),
        (None, 1, "檔案不存在"),
    ],
)
def test_module_cli(tmp_path, text, expected_code, expected_output):
    path = tmp_path / "test_results.txt"
    if text is not None:
        path.write_text(text, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "scripts.summarize_test_results",
            "--file",
            str(path),
        ],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == expected_code
    if expected_code == 0:
        assert expected_output in result.stdout
        assert "測試命令（Command）：pytest" in result.stdout
        assert "pytest 統計：" in result.stdout
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert expected_output in result.stderr
