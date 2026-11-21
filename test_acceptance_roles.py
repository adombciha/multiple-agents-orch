import json
from unittest.mock import Mock

import pytest

from orchestrator.core import acceptance
from orchestrator.core.config import DEFAULT_CONFIG
from orchestrator.core.state import AgentOrchestrator

TEST_NODE = "test_answer.py::test_valid_answer"


@pytest.fixture
def app(tmp_path):
    orchestrator = AgentOrchestrator(tmp_path)
    orchestrator.init_project()
    orchestrator.load_config_and_state()
    return orchestrator


def test_planner_pauses_for_missing_criteria(app, monkeypatch):
    app.requirements_path.write_text("Add an answer.", encoding="utf-8")
    manager_response = {
        "tasks": [
            {"id": "T1", "description": "Add answer", "target_files": ["answer.py"]}
        ]
    }
    monkeypatch.setattr(
        app, "call_agent", Mock(return_value=json.dumps(manager_response))
    )

    app.step_developing_plan()

    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["resume_state"] == "DEVELOPING_PLAN"
    assert not app.plan_path.exists()


def test_planner_rejects_overlapping_whole_file_tasks(app, monkeypatch):
    request = "Only add guide.md. Include installation and usage."
    app.request_path.write_text(request, encoding="utf-8")
    app.requirements_path.write_text(request, encoding="utf-8")
    response = {
        "tasks": [
            {
                "id": topic,
                "description": f"Document {topic}",
                "target_files": ["guide.md"],
            }
            for topic in ("installation", "usage")
        ],
        "acceptance_criteria": [
            {
                "requirement": "Document installation and usage",
                "kind": "manual",
                "check": "Open the rendered guide and read both sections.",
                "task_ids": ["installation", "usage"],
            }
        ],
    }
    monkeypatch.setattr(app, "call_agent", Mock(return_value=json.dumps(response)))

    app.step_developing_plan()

    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert "Use one task for guide.md" in app.state["human_review_details"]
    assert not app.plan_path.exists()


@pytest.mark.parametrize(
    ("test_command", "actual_command", "output", "status"),
    [
        (
            DEFAULT_CONFIG["test_command"],
            "python3 -m pytest -v",
            f"{TEST_NODE} PASSED [100%]",
            "passed",
        ),
        ("pytest -q", "pytest -q", "1 passed", "pending"),
    ],
)
def test_approved_plan_through_qa_and_review(
    app, monkeypatch, test_command, actual_command, output, status
):
    app.config.update(test_command=test_command, require_delivery_confirmation=False)
    app.request_path.write_text(
        "Add an answer and inspect it on Windows.", encoding="utf-8"
    )
    app.requirements_path.write_text("Add an answer and verify it.", encoding="utf-8")
    responses = {
        "manager": {
            "tasks": [
                {"id": "T1", "description": "Add answer", "target_files": ["answer.py"]}
            ],
            "acceptance_criteria": [
                {
                    "requirement": "Return the expected answer",
                    "kind": "pytest",
                    "check": TEST_NODE,
                    "task_ids": ["T1"],
                },
                {
                    "requirement": "Answer works on Windows",
                    "kind": "manual",
                    "check": "In Windows Python, import answer and compare the result to 42.",
                    "task_ids": ["T1"],
                },
            ],
        },
        "architect": {"status": "APPROVED", "feedback": []},
        "reviewer": {"status": "APPROVED", "feedback": [], "process_feedback": []},
    }

    def model(role, prompt, *args, **kwargs):
        if role.startswith("qa_"):
            return "QA_STATUS: PASSED\nNo defects found."
        return json.dumps(responses[role])

    call_agent = Mock(side_effect=model)
    run_command = Mock(return_value=(0, output))
    monkeypatch.setattr(app, "call_agent", call_agent)
    monkeypatch.setattr(app, "run_command", run_command)

    app.step_developing_plan()
    assert app.state["state"] == "REVIEWING_PLAN"
    plan = app.plan_path.read_text(encoding="utf-8")
    assert "### AC-01" in plan and TEST_NODE in plan
    assert "### AC-02" in plan and "Windows Python" in plan
    app.step_reviewing_plan()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["human_review_kind"] == "approve_plan"
    assert "implementation_approval" not in app.state

    app.record_implementation_approval("approve")
    app.state["tasks"][0]["status"] = "completed"
    app.state["state"] = "TESTING"
    run_command.reset_mock()
    app.step_testing()
    run_command.assert_called_once_with(
        ["bash", "-c", actual_command],
        timeout=600,
        capture=True,
        progress_path=app.test_results_path,
    )
    assert [row["status"] for row in acceptance.rows(app)] == [status, "pending"]
    assert app.state["state"] == "REVIEWING_CODE"
    app.step_reviewing_code()
    review_prompt = call_agent.call_args.args[1]
    assert "Recorded Per-Requirement Acceptance Evidence" in review_prompt
    assert f'"status": "{status}"' in review_prompt
    assert app.state["state"] == "WAITING_FOR_DELIVERY"
