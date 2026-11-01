"""Offline model responses with real workflow stages, Git worktrees and delivery."""

import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from orchestrator import main as cli
from orchestrator.core.state import AgentOrchestrator, delivery_snapshot_id


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def offline_models(app, monkeypatch):
    def manager(prompt, system=None, response_validator=None, response_schema=None):
        properties = (response_schema or {}).get("properties", {})
        if "use_sales" in properties:
            return '{"use_sales": false}'
        if "project_overview" in properties:
            return json.dumps(
                {
                    "project_overview": "Implement smoke.py",
                    "functional_requirements": ["answer() returns 42"],
                    "technical_specifications": ["Python standard library"],
                    "scope_boundaries": ["Only smoke.py"],
                }
            )
        if "adjustments" in properties:
            return '{"adjustments": []}'
        if "tasks" in prompt or "action items" in prompt:
            return json.dumps(
                {
                    "tasks": [
                        {
                            "id": "SMOKE",
                            "description": "Implement answer() returning 42",
                            "target_files": ["smoke.py"],
                            "complexity": "routine",
                            "rd_level": "senior",
                            "qa_level": "senior",
                        }
                    ],
                    "staffing": {"rd": {"senior": 1}, "qa": {"senior": 1}},
                }
            )
        return "## Outcome\nOffline smoke implementation and verification complete."

    def agent(role, prompt, *args, **kwargs):
        if role.startswith("developer"):
            return "[FILE_START: smoke.py]\ndef answer():\n    return 42\n[FILE_END: smoke.py]"
        if role.startswith("qa_"):
            return "QA_STATUS: PASSED\nsmoke.py returned 42 in the configured test command."
        if role == "reviewer":
            return '{"status": "APPROVED", "feedback": [], "process_feedback": []}'
        if role == "architect":
            return '{"status": "APPROVED", "feedback": []}'
        if role == "assistant":
            return "## Offline smoke delivery\nAdded and verified smoke.py."
        raise AssertionError(f"Unexpected model role: {role}")

    monkeypatch.setattr(app, "call_manager", manager)
    monkeypatch.setattr(app, "call_agent", agent)
    monkeypatch.setattr(app, "consult_specialists", Mock(return_value=""))
    monkeypatch.setattr(
        app, "get_active_model_for_role", Mock(return_value="offline-model")
    )
    monkeypatch.setattr(
        app,
        "allocate_workers",
        lambda role, tasks: (
            [(f"{role}-senior-1", "senior")],
            {task["id"]: f"{role}-senior-1" for task in tasks},
        ),
    )


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("owner_gate", [False, True])
def test_start_pause_restart_test_review_and_real_delivery(
    tmp_path, monkeypatch, conflict, owner_gate
):
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        Mock(side_effect=AssertionError("Unexpected network call")),
    )
    git(tmp_path, "init", "-b", "master")
    git(tmp_path, "config", "user.name", "Offline Test")
    git(tmp_path, "config", "user.email", "offline@example.com")
    (tmp_path / ".gitignore").write_text(
        ".ai-company*\nbrain.db*\n__pycache__/\n", encoding="utf-8"
    )
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-m", "base")
    app = AgentOrchestrator(tmp_path)
    app.init_project()
    app.config.update(
        test_command=f'{sys.executable} -c "from smoke import answer; assert answer() == 42"',
        telemetry_enabled=False,
    )
    app.config_path.write_text(json.dumps(app.config), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "configure_ollama", Mock())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orchestrator.py",
            "start",
            "Implement smoke.py with answer() returning 42."
            + (" Wait for owner approval before implementation." if owner_gate else ""),
        ],
    )
    cli.main()
    app.load_config_and_state()
    offline_models(app, monkeypatch)
    for expected in (
        "DEVELOPING_PLAN",
        "REVIEWING_PLAN",
        "WAITING_FOR_OWNER" if owner_gate else "IMPLEMENTING",
    ):
        app.step()
        assert app.state["state"] == expected
    if owner_gate:
        from orchestrator.gui import GuiService
        from orchestrator.core.status import next_actions

        app = AgentOrchestrator(tmp_path)
        app.load_config_and_state()
        offline_models(app, monkeypatch)
        assert app.state["human_review_kind"] == "approve_plan"
        gui_decision = GuiService(tmp_path).status()["workflow_decision"]
        assert gui_decision["destinations"]["approve"] == "IMPLEMENTING"
        assert next_actions(app.state)[0]["command"] == "approve"
        with monkeypatch.context() as approval:
            approval.setattr(sys, "argv", ["orchestrator.py", "continue"])
            approval.setattr(cli, "run_workflow", Mock())
            cli._run_cli(app)
        assert app.state["state"] == "IMPLEMENTING"
        assert app.implementation_approval_evidence()[
            "matches_current_request_and_plan"
        ]
    app.request_pause()
    app.run_to_end()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    app = AgentOrchestrator(tmp_path)
    app.load_config_and_state()
    offline_models(app, monkeypatch)
    assert app.pause_requested()
    assert cli.apply_approve(app)
    app.run_to_end()
    assert app.state["state"] == "WAITING_FOR_DELIVERY", app.state.get(
        "human_review_details"
    )
    worktree = app.ai_dir / "worktree"
    assert (worktree / "smoke.py").is_file()
    assert "Exit Code: 0" in app.test_results_path.read_text()
    assert not (tmp_path / "smoke.py").exists()
    assert cli._deliver_waiting_workflow(app, None) == 0
    preview = app.get_delivery_preview()
    if conflict:
        (tmp_path / "smoke.py").write_text("target branch content\n", encoding="utf-8")
        git(tmp_path, "add", "smoke.py")
        git(tmp_path, "commit", "-m", "target change")
        assert cli._deliver_waiting_workflow(app, delivery_snapshot_id(preview)) == 1
        assert cli._deliver_waiting_workflow(app, None) == 0
        preview = app.get_delivery_preview()
    cli._deliver_waiting_workflow(app, delivery_snapshot_id(preview))
    if conflict:
        assert app.state["state"] != "COMPLETED"
        assert worktree.is_dir()
        assert (tmp_path / "smoke.py").read_text() == "target branch content\n"
        assert any(task["id"].startswith("FIX-MERGE") for task in app.state["tasks"])
    else:
        assert app.state["state"] == "COMPLETED"
        assert app.state["delivery"]["delivered"] is True
        assert git(tmp_path, "show", "HEAD:smoke.py") == "def answer():\n    return 42"
        assert "Offline smoke delivery" in git(tmp_path, "show", "HEAD:CHANGELOG.md")
