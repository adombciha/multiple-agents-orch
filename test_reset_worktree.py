"""Explicit reset backs up dirty work before removing only its owned worktree."""

import copy
import json
import tarfile
from unittest.mock import Mock

import pytest

from orchestrator.core.state import AgentOrchestrator
from orchestrator.main import archive_previous_run, clear_run_state
from test_workflow_e2e import git


@pytest.fixture
def dirty_app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        Mock(side_effect=AssertionError("No network")),
    )
    monkeypatch.setattr(AgentOrchestrator, "get_windows_host_ip", lambda self: None)
    git(tmp_path, "init", "-b", "master")
    git(tmp_path, "config", "user.name", "Offline Test")
    git(tmp_path, "config", "user.email", "offline@example.com")
    (tmp_path / ".gitignore").write_text(".ai-company*\nignored.bin\n")
    (tmp_path / "tracked.txt").write_text("baseline\n")
    (tmp_path / "deleted.txt").write_text("delete me\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "baseline")
    app = AgentOrchestrator(tmp_path)
    app.init_project()
    app.state.update(state="FAILED", tasks=[{"id": "T1", "status": "pending"}])
    app.request_path.write_text("Old request")
    app.test_results_path.write_text("Old test evidence")
    app.save_state()
    app.setup_worktree()
    wt = app.ai_dir / "worktree"
    (wt / "feature.txt").write_text("committed feature\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "unmerged feature")
    (wt / "tracked.txt").write_text("staged version\n")
    git(wt, "add", "tracked.txt")
    (wt / "tracked.txt").write_text("working version\n")
    (wt / "deleted.txt").unlink()
    (wt / "new.bin").write_bytes(b"\x00\xffuntracked")
    (wt / "new.bin").chmod(0o755)
    (wt / "ignored.bin").write_bytes(b"ignored data")
    (wt / "link").symlink_to("new.bin")
    return app


def test_explicit_reset_backups_can_restore_commit_index_and_working_files(
    dirty_app, tmp_path
):
    app = dirty_app
    wt = app.ai_dir / "worktree"
    head = git(wt, "rev-parse", "HEAD")
    clear_run_state(app, "PLANNING", discard_worktree=True)
    assert app.state["state"] == "PLANNING" and app.state["tasks"] == []
    assert not wt.exists()
    assert "ai-feature-branch" not in git(tmp_path, "branch", "--list")
    (archive,) = (tmp_path / ".ai-company-archive").glob("reset-*")
    assert json.loads((archive / "manifest.json").read_text())["head"] == head.strip()
    assert (archive / "run" / "request.md").read_text() == "Old request"
    assert (archive / "run" / "test_results.txt").read_text() == "Old test evidence"
    restore = tmp_path / "restored"
    git(
        tmp_path,
        "clone",
        "-b",
        "ai-feature-branch",
        str(archive / "branch.bundle"),
        str(restore),
    )
    git(restore, "apply", "--index", str(archive / "index.patch"))
    git(restore, "apply", str(archive / "working.patch"))
    assert (restore / "feature.txt").read_text() == "committed feature\n"
    assert (restore / "tracked.txt").read_text() == "working version\n"
    assert git(restore, "show", ":tracked.txt") == "staged version"
    assert not (restore / "deleted.txt").exists()
    with tarfile.open(archive / "worktree.tar") as contents:
        assert contents.extractfile("worktree/new.bin").read() == b"\x00\xffuntracked"
        assert contents.getmember("worktree/new.bin").mode == 0o755
        assert contents.getmember("worktree/link").issym()
        assert contents.extractfile("worktree/ignored.bin").read() == b"ignored data"
        assert "worktree/.git" not in contents.getnames()
    assert (tmp_path / "tracked.txt").read_text() == "baseline\n"


def test_start_refuses_dirty_or_unmerged_worktree_before_archiving(dirty_app):
    app = dirty_app
    before = app.state_path.read_bytes()
    assert not archive_previous_run(app)
    assert not (app.workspace / ".ai-company-archive").exists()
    assert app.state_path.read_bytes() == before
    assert (app.ai_dir / "worktree" / "new.bin").exists()


def test_backup_failure_never_removes_worktree_or_changes_state(dirty_app, monkeypatch):
    app = dirty_app
    before = copy.deepcopy(app.state)
    run = app.run_command
    calls = []

    def fail_bundle(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "bundle", "create"]:
            return 1, "backup failed"
        return run(cmd, **kwargs)

    monkeypatch.setattr(app, "run_command", fail_bundle)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        clear_run_state(app, "PLANNING", discard_worktree=True)
    assert app.state == before
    assert not any(cmd[:3] == ["git", "worktree", "remove"] for cmd in calls)
    assert (app.ai_dir / "worktree" / "new.bin").exists()


def test_remove_failure_reuses_backup_and_preserves_state(dirty_app, monkeypatch):
    app = dirty_app
    before = copy.deepcopy(app.state)
    run = app.run_command

    def fail_remove(cmd, **kwargs):
        return (
            (1, "remove denied")
            if cmd[:3] == ["git", "worktree", "remove"]
            else run(cmd, **kwargs)
        )

    monkeypatch.setattr(app, "run_command", fail_remove)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="cleanup failed"):
            clear_run_state(app, "PLANNING", discard_worktree=True)
    assert app.state == before
    assert len(list((app.workspace / ".ai-company-archive").glob("reset-*"))) == 1
    assert (app.ai_dir / "worktree" / "new.bin").exists()


def test_ordinary_cleanup_never_forces_dirty_worktree(dirty_app):
    app = dirty_app
    assert app.cleanup_worktree() is False
    assert (app.ai_dir / "worktree" / "new.bin").exists()
    assert not (app.workspace / ".ai-company-archive").exists()


def test_repeated_report_archive_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(AgentOrchestrator, "get_windows_host_ip", lambda self: None)
    app = AgentOrchestrator(tmp_path)
    app.init_project()
    app.state["state"] = "FAILED"
    app.save_state()
    app.request_path.write_text("Old request")
    assert archive_previous_run(app)
    assert archive_previous_run(app)
    archives = list((tmp_path / ".ai-company-archive").glob("run-*"))
    assert len(archives) == 1
    assert (archives[0] / "request.md").read_text() == "Old request"


def test_force_reset_refuses_worktree_owned_by_another_branch(dirty_app):
    app = dirty_app
    wt = app.ai_dir / "worktree"
    git(wt, "checkout", "-b", "user-branch")
    assert app.cleanup_worktree(discard=True) is False
    assert (wt / "new.bin").exists()
    assert "user-branch" in git(app.workspace, "branch", "--list")
    assert not (app.workspace / ".ai-company-archive").exists()


def test_start_refuses_clean_unmerged_commits_without_archiving(dirty_app):
    app = dirty_app
    wt = app.ai_dir / "worktree"
    git(wt, "add", ".")
    git(wt, "commit", "-m", "all changes")
    assert not archive_previous_run(app)
    assert wt.exists()
    assert not (app.workspace / ".ai-company-archive").exists()


@pytest.mark.parametrize("entry", ["gui", "cli"])
def test_reset_entry_removes_dirty_worktree_only_after_backup(
    dirty_app, monkeypatch, entry
):
    app = dirty_app
    if entry == "gui":
        from orchestrator.gui import GuiService

        service = GuiService(app.workspace)
        result = service.reset()
        assert result["state"] == "PLANNING"
    else:
        import sys
        from orchestrator import main as cli

        monkeypatch.chdir(app.workspace)
        monkeypatch.setattr(sys, "argv", ["orchestrator.py", "reset"])
        cli.main()
    assert not (app.ai_dir / "worktree").exists()
    assert not app.request_path.exists()
    assert len(list((app.workspace / ".ai-company-archive").glob("reset-*"))) == 1
    assert json.loads(app.state_path.read_text())["state"] == "PLANNING"


def test_worktree_changed_during_backup_is_preserved(dirty_app, monkeypatch):
    app = dirty_app
    run = app.run_command

    def mutate_during_backup(cmd, **kwargs):
        result = run(cmd, **kwargs)
        if cmd[:3] == ["git", "bundle", "verify"]:
            (app.ai_dir / "worktree" / "tracked.txt").write_text(
                "changed during backup\n"
            )
        return result

    monkeypatch.setattr(app, "run_command", mutate_during_backup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        clear_run_state(app, "PLANNING", discard_worktree=True)
    assert (
        app.ai_dir / "worktree" / "tracked.txt"
    ).read_text() == "changed during backup\n"
    assert app.state["state"] == "FAILED"
    assert "ai-feature-branch" in git(app.workspace, "branch", "--list")
