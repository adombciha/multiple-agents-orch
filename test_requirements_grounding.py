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


@pytest.mark.parametrize(
    "targets",
    [
        ["scripts/check_ollama_models.py", "test_check_ollama_models.py"],
        ["scripts/check_ollama_models.py"],
        [
            "scripts/check_ollama_models.py",
            "test_check_ollama_models.py",
            "orchestrator/core/backends.py",
        ],
    ],
)
def test_plan_coverage_uses_declared_outputs_not_read_only_references(
    initialized_orchestrator, monkeypatch, targets
):
    app = initialized_orchestrator
    request = """## Scope
僅新增：
- scripts/check_ollama_models.py
- test_check_ollama_models.py

既有程式與設定只能唯讀。重用 orchestrator/core/backends.py 的能力探索及請求參數計算。
## Goal
新增本機模型診斷工具與離線測試。
"""
    files = ["scripts/check_ollama_models.py", "test_check_ollama_models.py"]
    app.request_path.write_text(request)
    app.requirements_path.write_text(
        "僅允許新增 scripts/check_ollama_models.py 與 test_check_ollama_models.py。\n"
        "重用 orchestrator/core/backends.py，不修改現有程式。"
    )
    task = {
        "id": "task-1",
        "description": "Inspect the reference, then implement the diagnostic tool and offline tests",
        "target_files": targets,
        "rd_level": "middle",
        "qa_level": "middle",
    }
    call = Mock(
        return_value=json.dumps(
            {
                "tasks": [task],
                "staffing": {"rd": {"middle": 1}, "qa": {"middle": 1}},
                "specialists": [],
            }
        )
    )
    monkeypatch.setattr(app, "call_manager", call)

    app.step_developing_plan()

    call.assert_called_once()
    rule = (
        call.call_args.args[0]
        .split("Cover every requested file and no files outside this list: ", 1)[1]
        .split(". Related", 1)[0]
    )
    assert json.loads(rule) == files
    assert "orchestrator/core/backends.py" in call.call_args.args[0]
    if targets == files:
        assert app.state["state"] == "REVIEWING_PLAN"
        assert app.state["tasks"][0]["target_files"] == files
        assert app.state["tasks"][0]["description"] == task["description"]
        assert (
            "orchestrator/core/backends.py"
            not in app.plan_path.read_text()
            .split("## Target Files\n", 1)[1]
            .split("\n##", 1)[0]
        )
    else:
        assert app.state["state"] == "WAITING_FOR_OWNER"
        assert app.state["human_review_source"] == "Manager"
        assert app.state["tasks"] == []
