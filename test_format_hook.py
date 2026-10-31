"""Run the formatter hook against a temporary Git index, never the project index."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import format_staged


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "master")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def fake_formatter(root, name, fail=False):
    executable = root / (
        ".venv/bin/pyink" if name == "pyink" else "node_modules/.bin/prettier"
    )
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        + (
            "sys.exit(2)\n"
            if fail
            else "for arg in sys.argv[1:]:\n    if arg.startswith('./'):\n        p = Path(arg)\n        p.write_bytes(p.read_bytes().replace(b'ugly', b'clean'))\n"
        )
    )
    executable.chmod(0o755)


def test_formats_supported_staged_files_without_updating_index(repo):
    fake_formatter(repo, "pyink")
    fake_formatter(repo, "prettier")
    for name in ("code with spaces.py", "README.md", "untouched.txt"):
        (repo / name).write_text("ugly\n")
    git(repo, "add", ".")
    assert format_staged.main() == 1
    assert (repo / "code with spaces.py").read_text() == "clean\n"
    assert (repo / "README.md").read_text() == "clean\n"
    assert (repo / "untouched.txt").read_text() == "ugly\n"
    assert git(repo, "show", ":README.md") == b"ugly\n"
    git(repo, "add", "code with spaces.py", "README.md")
    assert format_staged.main() == 0


def test_partial_staging_is_rejected_before_any_formatting(repo):
    fake_formatter(repo, "pyink")
    for name in ("partial.py", "other.py"):
        (repo / name).write_text("ugly\n")
    git(repo, "add", "partial.py", "other.py")
    (repo / "partial.py").write_text("unstaged work\n")
    assert format_staged.main() == 1
    assert (repo / "partial.py").read_text() == "unstaged work\n"
    assert (repo / "other.py").read_text() == "ugly\n"
    assert git(repo, "show", ":partial.py") == b"ugly\n"


def test_missing_formatter_fails_without_touching_files(repo, monkeypatch):
    (repo / "app.py").write_text("ugly\n")
    git(repo, "add", "app.py")
    monkeypatch.setattr(format_staged.shutil, "which", lambda _: None)
    assert format_staged.main() == 1
    assert (repo / "app.py").read_text() == "ugly\n"


def test_symlink_is_not_followed(repo):
    target = repo / "outside.txt"
    target.write_text("ugly\n")
    (repo / "link.py").symlink_to(target)
    git(repo, "add", "link.py")
    assert format_staged.main() == 0
    assert target.read_text() == "ugly\n"


def test_formatter_failure_preserves_index(repo):
    fake_formatter(repo, "pyink", fail=True)
    (repo / "app.py").write_text("ugly\n")
    git(repo, "add", "app.py")
    assert format_staged.main() == 1
    assert git(repo, "show", ":app.py") == b"ugly\n"


def test_git_commit_invokes_hook_and_requires_restaging(repo):
    source = Path(__file__).resolve().parent
    (repo / ".githooks").mkdir()
    (repo / "scripts").mkdir()
    shutil.copy2(source / ".githooks" / "pre-commit", repo / ".githooks" / "pre-commit")
    shutil.copy2(
        source / "scripts" / "format_staged.py", repo / "scripts" / "format_staged.py"
    )
    git(repo, "config", "core.hooksPath", ".githooks")
    fake_formatter(repo, "pyink")
    (repo / "app.py").write_text("ugly\n")
    git(repo, "add", "app.py")
    command = [
        "git",
        "-c",
        "user.name=Hook Test",
        "-c",
        "user.email=hook@example.invalid",
        "commit",
        "-m",
        "hook test",
    ]
    first = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert first.returncode != 0
    assert "stage the formatted files" in first.stderr
    assert (repo / "app.py").read_text() == "clean\n"
    assert git(repo, "show", ":app.py") == b"ugly\n"
    git(repo, "add", "app.py")
    second = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert git(repo, "show", "HEAD:app.py") == b"clean\n"
