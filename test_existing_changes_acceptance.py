"""Exercise explicit owner acceptance without running models or delivering changes."""

import json
import sys
from unittest.mock import Mock

import pytest

from orchestrator.core.state import AgentOrchestrator
from orchestrator.gui import GuiService, render_page
from orchestrator.main import apply_approve, apply_review_decision, clear_run_state
from test_workflow_e2e import git


@pytest.fixture
def acceptance_app(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "master")
    git(tmp_path, "config", "user.name", "Offline Test")
    git(tmp_path, "config", "user.email", "offline@example.com")
    (tmp_path / ".gitignore").write_text(".ai-company*\nbrain.db*\n")
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    (tmp_path / "helper.py").write_text("VALUE = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "baseline")
    app = AgentOrchestrator(tmp_path)
    app.init_project()
    app.config["use_worktree"] = False
    app.config_path.write_text(json.dumps(app.config))
    app.request_path.write_text("Only modify app.py and run offline regression.")
    app.requirements_path.write_text(app.request_path.read_text())
    app.plan_path.write_text("Implement app.py after owner approval.")
    app.test_results_path.write_text("Exit Code: 0\nAll tests passed.")
    (tmp_path / "app.py").write_text("VALUE = 2\n")
    app.state.update(
        state="WAITING_FOR_OWNER",
        human_review_source="Orchestrator",
        resume_state="REVIEWING_CODE",
        pass_state="REVIEWING_CODE",
        human_review_details="Reviewer requires an owner decision.\nAccept the existing changes despite missing first-edit history.",
        tasks=[{"id": "T1", "target_files": ["app.py"], "status": "completed"}],
    )
    app.record_implementation_approval("pass")
    app.save_state()
    monkeypatch.setattr(
        app, "call_agent", Mock(side_effect=AssertionError("Unexpected model"))
    )
    return app


def test_acceptance_requires_display_and_exact_snapshot_id(acceptance_app):
    app = acceptance_app
    with pytest.raises(RuntimeError, match="Show the acceptance scope"):
        apply_review_decision(app, "accept-existing")
    assert "existing_changes_acceptance" not in app.state
    preview = app.present_existing_changes()
    with pytest.raises(RuntimeError, match="snapshot ID"):
        apply_review_decision(app, "accept-existing", snapshot_id="wrong")
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["owner_acceptance_preview"] == preview
    app.call_agent.assert_not_called()


def test_acceptance_survives_restart_and_does_not_rewrite_prior_approval(
    acceptance_app,
):
    app = acceptance_app
    prior = dict(app.state["implementation_approval"])
    preview = app.present_existing_changes()
    restarted = AgentOrchestrator(app.workspace)
    restarted.load_config_and_state()
    apply_review_decision(restarted, "accept-existing", snapshot_id=preview["id"])
    restarted.load_config_and_state()
    assert restarted.state["state"] == "REVIEWING_CODE"
    record = restarted.state["existing_changes_acceptance"]
    assert record["decision"] == "accept_existing_changes"
    assert record["accepted_at"] >= prior["approved_at"]
    assert restarted.state["implementation_approval"] == prior
    assert restarted.existing_changes_acceptance_evidence()["matches_current_changes"]
    assert len(restarted.state["tasks"]) == 1
    assert "owner_acceptance_preview" not in restarted.state
    assert "delivery_confirmed" not in restarted.state


@pytest.mark.parametrize(
    "mutation",
    [
        "request",
        "plan",
        "task",
        "tracked",
        "untracked",
        "new-file",
        "deleted",
        "index",
        "commit",
        "mode",
        "untracked-mode",
        "reason",
    ],
)
def test_changed_scope_rejects_old_confirmation_and_invalidates_acceptance(
    acceptance_app, mutation
):
    app = acceptance_app
    loose = app.workspace / "new.py"
    loose.write_text("VALUE = 1\n")
    preview = app.present_existing_changes()
    if mutation == "request":
        app.request_path.write_text("Only modify helper.py.")
    elif mutation == "plan":
        app.plan_path.write_text("Changed plan")
    elif mutation in {"task", "tracked", "untracked"}:
        path = {"task": "app.py", "tracked": "helper.py", "untracked": "new.py"}[
            mutation
        ]
        (app.workspace / path).write_text("VALUE = 3\n")
    elif mutation == "new-file":
        (app.workspace / "another.py").write_text("VALUE = 4\n")
    elif mutation == "deleted":
        loose.unlink()
    elif mutation == "index":
        git(app.workspace, "add", "app.py")
    elif mutation == "commit":
        git(app.workspace, "commit", "--allow-empty", "-m", "new head")
    elif mutation == "mode":
        (app.workspace / "app.py").chmod(0o755)
    elif mutation == "untracked-mode":
        loose.chmod(0o755)
    else:
        app.state["human_review_details"] += " New owner-only blocker."
    with pytest.raises(RuntimeError, match="scope changed"):
        apply_review_decision(app, "accept-existing", snapshot_id=preview["id"])
    assert "existing_changes_acceptance" not in app.state
    assert app.state["state"] == "WAITING_FOR_OWNER"
    # Previously accepted bytes must also cease to be current after content changes.
    app.state["existing_changes_acceptance"] = {
        "decision": "accept_existing_changes",
        **preview,
    }
    if mutation != "reason":
        assert not app.existing_changes_acceptance_evidence()["matches_current_changes"]
    refreshed = app.present_existing_changes()
    assert refreshed["id"] != preview["id"]


def test_symlink_retarget_and_escaping_task_paths_are_rejected(acceptance_app):
    app = acceptance_app
    link = app.workspace / "linked.py"
    link.symlink_to("app.py")
    preview = app.present_existing_changes()
    link.unlink()
    link.symlink_to("helper.py")
    with pytest.raises(RuntimeError, match="scope changed"):
        apply_review_decision(app, "accept-existing", snapshot_id=preview["id"])
    app.state["tasks"][0]["target_files"] = ["../outside.py"]
    with pytest.raises(RuntimeError, match="escapes"):
        app.present_existing_changes()


@pytest.mark.parametrize("decision", ["approve", "pass"])
def test_generic_resume_never_records_explicit_acceptance(acceptance_app, decision):
    app = acceptance_app
    app.present_existing_changes()
    if decision == "approve":
        assert apply_approve(app)
    else:
        apply_review_decision(app, "pass")
    assert "existing_changes_acceptance" not in app.state
    assert app.state["state"] == "REVIEWING_CODE"


@pytest.mark.parametrize(
    "state", ["REVIEWING_CODE", "IMPLEMENTING", "WAITING_FOR_DELIVERY", "COMPLETED"]
)
def test_acceptance_is_unavailable_outside_process_decision(acceptance_app, state):
    app = acceptance_app
    app.state["state"] = state
    with pytest.raises(RuntimeError, match="not waiting"):
        app.present_existing_changes()
    assert "existing_changes_acceptance" not in app.state


def test_git_error_preserves_state_without_fabricating_acceptance(
    acceptance_app, monkeypatch
):
    app = acceptance_app
    preview = app.present_existing_changes()
    monkeypatch.setattr(
        app, "_checked_git_output", Mock(side_effect=RuntimeError("Git failed"))
    )
    with pytest.raises(RuntimeError, match="Git failed"):
        apply_review_decision(app, "accept-existing", snapshot_id=preview["id"])
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["owner_acceptance_preview"] == preview
    assert "existing_changes_acceptance" not in app.state


def test_gui_displays_scope_then_records_acceptance_under_shared_lock(
    acceptance_app, monkeypatch
):
    from test_gui import finish_worker, start_test_server, http_request

    app = acceptance_app
    service = GuiService(app.workspace)
    service.orchestrator = app
    initial = render_page(service.status())
    assert "展示接受範圍" in initial
    assert "接受現有變更並重新審查" not in initial
    assert "清除失敗路由並重試" not in initial
    locked = []

    def run():
        contender = AgentOrchestrator(app.workspace)
        with pytest.raises(RuntimeError, match="already (locked|in progress)"):
            with contender.execution_lock():
                pass
        locked.append(app.state["state"])

    monkeypatch.setattr(app, "run_to_end", run)
    server, thread = start_test_server(service)
    try:
        status, data = http_request(
            server, "POST", "/api/review", {"decision": "preview-existing"}
        )
        assert status == 200
        preview = data["owner_acceptance_preview"]
        page = render_page(data)
        assert "接受現有變更並重新審查" in page
        assert "app.py" in page and preview["id"] in page
        assert "不代表測試通過或確認交付" in page
        app.call_agent.assert_not_called()
        status, _ = http_request(
            server,
            "POST",
            "/api/review",
            {"decision": "accept-existing", "snapshot_id": preview["id"]},
        )
        assert status == 200
        finish_worker(service)
        assert locked == ["REVIEWING_CODE"]
        assert app.state["existing_changes_acceptance"]["id"] == preview["id"]
        assert app.state["state"] == "REVIEWING_CODE"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_previews_then_confirms_without_automatic_delivery(
    acceptance_app, monkeypatch, capsys
):
    import orchestrator.main as cli

    app = acceptance_app
    monkeypatch.chdir(app.workspace)
    monkeypatch.setattr(cli, "AgentOrchestrator", lambda _: app)
    run = Mock()
    monkeypatch.setattr(cli, "run_workflow", run)
    monkeypatch.setattr(
        sys, "argv", ["orchestrator.py", "review", "preview-existing", "--run"]
    )
    cli.main()
    run.assert_not_called()
    preview = app.state["owner_acceptance_preview"]
    assert preview["id"] in capsys.readouterr().out
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orchestrator.py",
            "review",
            "accept-existing",
            "--snapshot",
            preview["id"],
            "--run",
        ],
    )
    cli.main()
    run.assert_called_once_with(app)
    assert app.state["state"] == "REVIEWING_CODE"
    assert "delivery_confirmed" not in app.state


def test_gui_preview_fails_while_another_process_holds_execution_lock(acceptance_app):
    app = acceptance_app
    service = GuiService(app.workspace)
    contender = AgentOrchestrator(app.workspace)
    with contender.execution_lock():
        with pytest.raises(RuntimeError, match="already (locked|in progress)"):
            service.review("preview-existing")
    assert "owner_acceptance_preview" not in app.read_state_snapshot()


def test_reset_revokes_acceptance_and_preview(acceptance_app, monkeypatch):
    app = acceptance_app
    preview = app.present_existing_changes()
    app.accept_existing_changes(preview["id"])
    monkeypatch.setattr(app, "cleanup_worktree", Mock(return_value=True))
    clear_run_state(app, "PLANNING")
    assert "existing_changes_acceptance" not in app.state
    assert "owner_acceptance_preview" not in app.state
    assert "owner_acceptance_pending" not in app.state


def test_acceptance_scope_excludes_later_target_branch_changes(acceptance_app):
    app = acceptance_app
    worktree = app.ai_dir / "worktree"
    git(app.workspace, "worktree", "add", "-b", "ai-feature-branch", str(worktree))
    app.config["use_worktree"] = True
    (app.workspace / "later_guard.py").write_text("POLICY = True\n")
    git(app.workspace, "add", "later_guard.py")
    git(app.workspace, "commit", "-m", "later target policy")
    (worktree / "app.py").write_text("VALUE = 2\n")
    (worktree / "new.py").write_text("VALUE = 1\n")
    preview = app.present_existing_changes()
    assert preview["snapshot"]["files"] == ["app.py", "new.py"]
    assert preview["snapshot"]["workspace"] == str(worktree)
    (worktree / "new.py").write_text("VALUE = 2\n")
    with pytest.raises(RuntimeError, match="scope changed"):
        apply_review_decision(app, "accept-existing", snapshot_id=preview["id"])


def test_acceptance_preview_rejects_symlinked_workspace(acceptance_app):
    app = acceptance_app
    app.config["use_worktree"] = True
    (app.ai_dir / "worktree").symlink_to(app.workspace, target_is_directory=True)
    with pytest.raises(RuntimeError, match="workspace escapes"):
        app.present_existing_changes()


def test_binary_and_text_content_cannot_share_an_acceptance_fingerprint(acceptance_app):
    app = acceptance_app
    path = app.workspace / "raw.dat"
    path.write_bytes(b"\xff")
    preview = app.present_existing_changes()
    path.write_bytes(b"hex:ff")
    with pytest.raises(RuntimeError, match="scope changed"):
        apply_review_decision(app, "accept-existing", snapshot_id=preview["id"])
