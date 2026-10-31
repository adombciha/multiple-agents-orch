"""Verify automatic delivery repair using temporary Git repositories and no models."""

from unittest.mock import Mock
from pathlib import Path
import os
import shutil
import sys

import pytest

from orchestrator import gui, main as cli
from orchestrator.core.state import AgentOrchestrator, DeliveryRepairQueued, delivery_snapshot_id
from test_workflow_e2e import git
from test_format_hook import fake_formatter


@pytest.fixture
def delivery_app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        Mock(side_effect=AssertionError("Unexpected network")),
    )
    git(tmp_path, "init", "-b", "master")
    git(tmp_path, "config", "user.name", "Offline Test")
    git(tmp_path, "config", "user.email", "offline@example.com")
    (tmp_path / ".gitignore").write_text(".ai-company*\nbrain.db*\n")
    (tmp_path / "CHANGELOG.md").write_text("# History\n\nHistorical text  \n")
    (tmp_path / "answer.py").write_text("ANSWER = 1\n")
    (tmp_path / "protected.py").write_text("PROTECTED = True\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "baseline")
    app = AgentOrchestrator(tmp_path)
    app.init_project()
    git(
        tmp_path,
        "worktree",
        "add",
        "-b",
        "ai-feature-branch",
        str(app.ai_dir / "worktree"),
    )
    app.request_path.write_text("Only modify answer.py")
    app.requirements_path.write_text("Only modify answer.py")
    app.state.update(
        state="WAITING_FOR_DELIVERY",
        delivery_artifacts_prepared=True,
        changelog_written=True,
        tasks=[{"id": "T1", "target_files": ["answer.py"], "status": "completed"}],
    )
    app.save_state()
    return app


@pytest.mark.parametrize("stage", ["step_waiting_for_delivery", "step_completed"])
def test_generated_changelog_is_clean_and_not_duplicated(
    delivery_app, monkeypatch, stage
):
    app = delivery_app
    path = app.agent_workspace() / "CHANGELOG.md"
    original = path.read_text()
    app.state.update(delivery_artifacts_prepared=False, changelog_written=False)
    monkeypatch.setattr(app.manager, "call_manager", Mock(return_value="summary"))
    monkeypatch.setattr(app.manager, "_review_worker_weights", Mock())
    monkeypatch.setattr(
        app.assistant, "generate_meeting_memory", Mock(return_value="memory")
    )
    call = Mock(return_value="## Generated  \n- Prompt/Output: \n- Details\t\n")
    monkeypatch.setattr(app.assistant, "call_agent", call)
    getattr(app.manager, stage)()
    first = path.read_text()
    getattr(app.manager, stage)()
    assert path.read_text() == first
    assert first.startswith(original)
    assert first.count("## Generated") == 1
    assert all(line == line.rstrip() for line in first[len(original) :].splitlines())
    git(app.agent_workspace(), "diff", "--check", "master")
    call.assert_called_once()
    assert not app.state.get("delivery_confirmed")


def test_content_failure_queues_scoped_repair_and_invalidates_confirmation(
    delivery_app,
):
    app = delivery_app
    path = app.agent_workspace() / "answer.py"
    path.write_text("ANSWER = 2  \n")
    app.state.update(
        delivery_preview={"old": True},
        delivery_preview_presented=True,
        delivery_confirmed=True,
    )
    with pytest.raises(DeliveryRepairQueued):
        app.create_delivery_preview()
    task = app.state["tasks"][-1]
    assert task["id"] == "FIX-DELIVERY-1"
    assert task["target_files"] == ["answer.py"]
    assert "trailing whitespace" in task["description"]
    assert app.state["state"] == "IMPLEMENTING"
    assert app.state["changelog_written"] is True
    assert app.state["delivery_artifacts_prepared"] is False
    assert not app.state.get("delivery_preview")
    assert not app.state.get("delivery_confirmed")
    assert path.read_text() == "ANSWER = 2  \n"
    with pytest.raises(DeliveryRepairQueued):
        app.create_delivery_preview()
    assert len(app.state["tasks"]) == 2


def install_delivery_hook(app, monkeypatch):
    root = app.workspace
    (root / "scripts").mkdir()
    shutil.copy2(
        Path(__file__).parent / "scripts/format_staged.py",
        root / "scripts/format_staged.py",
    )
    hooks = root / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{root}/scripts/format_staged.py"\n'
    )
    hook.chmod(0o755)
    git(root, "config", "core.hooksPath", str(hooks))
    fake_formatter(root, "pyink")
    fake_formatter(root, "prettier")
    monkeypatch.setenv(
        "PATH",
        str(root / ".venv/bin")
        + os.pathsep
        + str(root / "node_modules/.bin")
        + os.pathsep
        + os.environ["PATH"],
    )
    return hook


def test_delivery_formats_before_preview_and_real_hook_merge_succeeds(
    delivery_app, monkeypatch
):
    app = delivery_app
    install_delivery_hook(app, monkeypatch)
    cwd = app.agent_workspace()
    (cwd / "answer.py").write_text("ANSWER = 'ugly'\n")
    with (cwd / "CHANGELOG.md").open("a") as f:
        f.write("\n## ugly release\n")
    index = git(cwd, "diff", "--cached", "--binary")
    preview = app.create_delivery_preview()
    assert git(cwd, "diff", "--cached", "--binary") == index
    assert preview["tracked_files"]["answer.py"] == "ANSWER = 'clean'\n"
    assert "## clean release" in preview["tracked_files"]["CHANGELOG.md"]
    app.present_delivery_preview(preview)
    app.confirm_delivery(preview, "merge")

    app.step_waiting_for_delivery()

    assert app.state["state"] == "COMPLETED"
    assert app.state["delivery"]["delivered"]
    assert not cwd.exists()
    assert (app.workspace / "answer.py").read_text() == "ANSWER = 'clean'\n"
    assert len(app.state["tasks"]) == 1


def test_hook_edit_after_confirmation_requires_new_preview_without_rd(
    delivery_app, monkeypatch
):
    app = delivery_app
    hook = install_delivery_hook(app, monkeypatch)
    cwd = app.agent_workspace()
    (cwd / "answer.py").write_text("ANSWER = 2\n")
    preview = app.present_delivery_preview(app.create_delivery_preview())
    app.confirm_delivery(preview, "merge")
    feature_head = git(cwd, "rev-parse", "HEAD")
    master_head = git(app.workspace, "rev-parse", "HEAD")
    hook.write_text(
        f'#!{sys.executable}\nfrom pathlib import Path\np=Path("answer.py")\ns=p.read_text()\nif "# hook" not in s:\n p.write_text(s+"# hook\\n")\n raise SystemExit(1)\n'
    )

    app.step_waiting_for_delivery()

    assert app.state["state"] == "WAITING_FOR_DELIVERY"
    assert not app.state.get("delivery_confirmed")
    assert not app.state.get("delivery_preview_presented")
    assert app.state["delivery_preview"]["tracked_files"]["answer.py"].endswith(
        "# hook\n"
    )
    assert git(cwd, "rev-parse", "HEAD") == feature_head
    assert git(app.workspace, "rev-parse", "HEAD") == master_head
    assert len(app.state["tasks"]) == 1
    fresh = app.present_delivery_preview(app.get_delivery_preview())
    app.confirm_delivery(fresh, "merge")
    app.step_waiting_for_delivery()
    assert app.state["state"] == "COMPLETED"


@pytest.mark.parametrize("legacy", [False, True])
def test_commit_failure_preserves_diagnostic_without_rd(
    delivery_app, monkeypatch, legacy
):
    app = delivery_app
    hook = install_delivery_hook(app, monkeypatch)
    cwd = app.agent_workspace()
    (cwd / "answer.py").write_text("ANSWER = 2\n")
    preview = app.present_delivery_preview(app.create_delivery_preview())
    app.confirm_delivery(preview, "merge")
    hook.write_text("#!/bin/sh\necho 'commit policy denied' >&2\nexit 1\n")

    if legacy:
        app.manager._merge_delivery(app.state.setdefault("delivery", {}))
    else:
        app.step_waiting_for_delivery()

    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["resume_state"] == "WAITING_FOR_DELIVERY"
    assert "commit policy denied" in app.state["human_review_details"]
    assert app.state["worktree_failure_stage"] == "commit"
    assert len(app.state["tasks"]) == 1
    assert cwd.exists()


def test_preparation_failure_invalidates_confirmation_without_staging(
    delivery_app, monkeypatch
):
    app = delivery_app
    cwd = app.agent_workspace()
    (cwd / "answer.py").write_text("ANSWER = 2\n")
    old = app.present_delivery_preview(app.create_delivery_preview())
    app.confirm_delivery(old, "merge")
    install_delivery_hook(app, monkeypatch)
    fake_formatter(app.workspace, "pyink", fail=True)
    index = git(cwd, "diff", "--cached", "--binary")
    with pytest.raises(RuntimeError, match="Delivery formatting failed"):
        app.create_delivery_preview()
    assert not app.state.get("delivery_confirmed")
    assert not app.state.get("delivery_preview_presented")
    assert git(cwd, "diff", "--cached", "--binary") == index
    assert cwd.exists()


def test_repair_budget_and_scope_remain_enforced(delivery_app):
    app = delivery_app
    (app.agent_workspace() / "protected.py").write_text("PROTECTED = True  \n")
    with pytest.raises(RuntimeError, match="Git delivery check failed"):
        app.create_delivery_preview()
    assert app.state["state"] == "WAITING_FOR_DELIVERY"
    assert len(app.state["tasks"]) == 1
    (app.agent_workspace() / "protected.py").write_text("PROTECTED = True\n")
    (app.agent_workspace() / "answer.py").write_text("ANSWER = 2  \n")
    app.config["max_revisions"] = 1
    with pytest.raises(DeliveryRepairQueued):
        app.create_delivery_preview()
    app.state["tasks"][-1]["status"] = "completed"
    app.state["state"] = "WAITING_FOR_DELIVERY"
    with pytest.raises(RuntimeError, match="Git delivery check failed") as error:
        app.create_delivery_preview()
    assert not isinstance(error.value, DeliveryRepairQueued)
    assert len(app.state["tasks"]) == 2
    assert not app.queue_delivery_content_repair(RuntimeError("Git permission denied"))


def test_run_loop_continues_through_repair_qa_and_review(delivery_app, monkeypatch):
    app = delivery_app
    path = app.agent_workspace() / "answer.py"
    path.write_text("ANSWER = 2  \n")
    visited = []

    def step():
        state = app.state["state"]
        visited.append(state)
        if state == "WAITING_FOR_DELIVERY":
            app.create_delivery_preview()
        elif state == "IMPLEMENTING":
            path.write_text("ANSWER = 2\n")
            app.state["tasks"][-1]["status"] = "completed"
            app.state["state"] = "TESTING"
        elif state == "TESTING":
            app.state["state"] = "REVIEWING_CODE"
        elif state == "REVIEWING_CODE":
            app.state["state"] = "WAITING_FOR_DELIVERY"
        app.save_state()

    monkeypatch.setattr(app, "step", step)
    app.run_to_end()
    assert visited == [
        "WAITING_FOR_DELIVERY",
        "IMPLEMENTING",
        "TESTING",
        "REVIEWING_CODE",
        "WAITING_FOR_DELIVERY",
    ]
    assert app.state["state"] == "WAITING_FOR_DELIVERY"
    assert not app.state.get("delivery_confirmed")


def test_cli_delivery_starts_queued_repair_without_merging(delivery_app, monkeypatch):
    app = delivery_app
    (app.agent_workspace() / "answer.py").write_text("ANSWER = 2  \n")
    run = Mock()
    cleanup = Mock()
    monkeypatch.setattr(app, "run_to_end", run)
    monkeypatch.setattr(app, "cleanup_worktree", cleanup)
    assert cli._deliver_waiting_workflow(app, None) == 1
    run.assert_called_once()
    cleanup.assert_not_called()
    assert app.state["state"] == "IMPLEMENTING"


@pytest.mark.parametrize("confirm", [False, True])
def test_gui_delivery_starts_queued_repair_without_merging(
    delivery_app, monkeypatch, confirm
):
    import threading

    app = delivery_app
    preview = app.present_delivery_preview(app.create_delivery_preview())
    (app.agent_workspace() / "answer.py").write_text("ANSWER = 2  \n")
    done = threading.Event()
    run = Mock(side_effect=done.set)
    cleanup = Mock()
    monkeypatch.setattr(app, "run_to_end", run)
    monkeypatch.setattr(app, "cleanup_worktree", cleanup)
    service = gui.GuiService(app.workspace)
    service.orchestrator = app
    service.delivery(confirm, delivery_snapshot_id(preview))
    assert done.wait(3)
    run.assert_called_once()
    cleanup.assert_not_called()
    assert app.state["state"] == "IMPLEMENTING"
