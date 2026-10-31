import json
from unittest.mock import Mock

import pytest

from orchestrator.roles.base_agent import declared_request_files, missing_request_files
from orchestrator import gui
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config

REQUEST = """## Context
Test the existing workflow, not its implementation.
## Scope
Only add:
- live_answer.py
- test_live_answer.py
Do not modify orchestrator.py.
## Goal
Implement integer addition and pytest coverage.
"""


def requirements(overview):
    return json.dumps(
        {
            "project_overview": overview,
            "functional_requirements": [overview],
            "technical_specifications": ["Python and pytest"],
            "scope_boundaries": ["No unrelated edits"],
        }
    )


@pytest.mark.parametrize(
    "task_request",
    [REQUEST, "只新增 live_answer.py 與 test_live_answer.py。不要修改其他檔案。"],
)
def test_explicit_outputs_exclude_forbidden_and_reference_files(task_request):
    task_request += "\n<!-- REFERENCE-ONLY:START -->\n## Scope\n- docs/spec.md\n<!-- REFERENCE-ONLY:END -->"
    assert declared_request_files(task_request) == [
        "live_answer.py",
        "test_live_answer.py",
    ]
    assert missing_request_files(task_request, "test_live_answer.py") == [
        "live_answer.py"
    ]


def test_planning_rejects_requirements_about_previous_worktree_error(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.request_path.write_text(REQUEST, encoding="utf-8")
    app.human_report_path.write_text(
        "WAITING_FOR_OWNER: Git worktree setup failed", encoding="utf-8"
    )
    manager = Mock(
        side_effect=[
            '{"use_sales": false}',
            requirements("Investigate Git worktree setup failed. Do not modify files."),
        ]
    )
    monkeypatch.setattr(app, "call_manager", manager)
    app.step_planning()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["human_review_source"] == "Requirements"
    assert app.state["pass_state"] == "PLANNING"
    assert not app.requirements_json_path.exists()
    assert app.request_path.read_text() == REQUEST
    assert "live_answer.py" in app.state["human_review_details"]
    assert REQUEST in manager.call_args.args[0]
    assert "diagnostics, not new requirements" in manager.call_args.args[1]


def test_planning_accepts_requirements_covering_original_outputs(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.request_path.write_text(REQUEST, encoding="utf-8")
    monkeypatch.setattr(
        app,
        "call_manager",
        Mock(
            side_effect=[
                '{"use_sales": false}',
                requirements("Implement live_answer.py and test_live_answer.py"),
            ]
        ),
    )
    app.step_planning()
    assert app.state["state"] == "DEVELOPING_PLAN"
    assert "test_live_answer.py" in app.requirements_path.read_text()


def test_developing_plan_detects_saved_requirements_drift_without_model_call(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    app.request_path.write_text(REQUEST, encoding="utf-8")
    app.requirements_path.write_text(
        "Investigate the previous Git error; no coding request exists.",
        encoding="utf-8",
    )
    manager = Mock()
    monkeypatch.setattr(app, "call_manager", manager)
    app.step_developing_plan()
    manager.assert_not_called()
    assert app.state["human_review_source"] == "Requirements"
    assert app.state["resume_state"] == "PLANNING"
    assert app.state["tasks"] == []
    page = gui.render_page(
        {"state": "WAITING_FOR_OWNER", "review": {"source": "Requirements"}}
    )
    assert "重新產生需求</button>" in page
    assert "接受目前結果並繼續</button>" not in page
