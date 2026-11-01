"""Keep ungrounded QA and process instructions out of code repair tasks."""

import json
from unittest.mock import Mock

import pytest

from orchestrator.main import apply_approve, apply_review_decision, clear_run_state
from orchestrator.roles.qa import qa_code_findings
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config

REQUEST = "Only modify app.py. Do not run real model performance tests."


@pytest.fixture
def qa_app(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    app.request_path.write_text(REQUEST)
    app.requirements_path.write_text(REQUEST)
    app.plan_path.write_text("Implement app.py and isolated offline tests.")
    app.state.update(
        state="TESTING",
        tasks=[
            {
                "id": "T1",
                "status": "completed",
                "target_files": ["app.py"],
                "rd_level": "senior",
                "qa_level": "senior",
            }
        ],
    )
    monkeypatch.setattr(
        app,
        "allocate_workers",
        Mock(return_value=([("qa-1", "senior")], {"T1": "qa-1"})),
    )
    monkeypatch.setattr(
        app, "run_command", Mock(return_value=(0, "719 passed\n46 subtests passed"))
    )
    monkeypatch.setattr(app, "escalate_developer_backend", Mock())
    monkeypatch.setattr(
        app, "get_active_model_for_role", Mock(return_value="offline-model")
    )
    monkeypatch.setattr("orchestrator.roles.qa.brain.remember", Mock())
    monkeypatch.setattr("orchestrator.roles.qa.scoring.record_event", Mock())
    return app


@pytest.mark.parametrize("decision", ["approve", "pass"])
@pytest.mark.parametrize("changed", [None, "request", "plan"])
def test_owner_approval_is_bound_to_actual_request_and_plan(qa_app, decision, changed):
    app = qa_app
    assert not app.implementation_approval_evidence()[
        "matches_current_request_and_plan"
    ]
    app.state.update(
        state="WAITING_FOR_OWNER",
        human_review_source="Architect",
        resume_state="IMPLEMENTING",
        pass_state="IMPLEMENTING",
    )
    if decision == "approve":
        assert apply_approve(app)
    else:
        apply_review_decision(app, "pass")
    app.load_config_and_state()
    assert app.implementation_approval_evidence()["matches_current_request_and_plan"]
    if changed:
        path = app.request_path if changed == "request" else app.plan_path
        path.write_text(path.read_text() + " changed")
        assert not app.implementation_approval_evidence()[
            "matches_current_request_and_plan"
        ]


@pytest.mark.parametrize(
    "claim",
    [
        "Files exist so initial approval must have been violated.",
        "719 passed does not prove full regression ran. Add approval notes to source.",
        "Measure real model speed, quality and memory before passing.",
        "Runtime no-write rules mean test fixtures may not create temporary worktrees.",
    ],
)
def test_unsupported_rejection_gets_one_correction_without_rd_work(
    qa_app, monkeypatch, claim
):
    app = qa_app
    call = Mock(return_value="QA_STATUS: FAILED\n" + claim)
    monkeypatch.setattr(app, "call_agent", call)
    app.step_testing()
    assert call.call_count == 2
    assert claim in call.call_args_list[1].args[1]
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["human_review_source"] == "QA_RESPONSE"
    assert app.state["resume_state"] == "TESTING"
    assert len(app.state["tasks"]) == 1
    assert app.state["code_revisions"] == 0
    app.escalate_developer_backend.assert_not_called()
    prompt = call.call_args_list[0].args[1]
    assert REQUEST in prompt and "Actual Test Command: python3 -m pytest -q" in prompt
    assert "719 passed" in prompt and "Owner Approval Evidence:" in prompt


def test_false_rejection_can_correct_automatically(qa_app, monkeypatch):
    app = qa_app
    monkeypatch.setattr(
        app,
        "call_agent",
        Mock(
            side_effect=[
                "QA_STATUS: FAILED\nObtain approval in source code",
                "QA_STATUS: PASSED\nThe requested offline tests passed.",
            ]
        ),
    )
    app.step_testing()
    assert app.state["state"] == "REVIEWING_CODE"
    assert len(app.state["tasks"]) == 1


def test_real_test_failure_cannot_pass_and_does_not_forward_process_prose(
    qa_app, monkeypatch
):
    app = qa_app
    monkeypatch.setattr(
        app,
        "run_command",
        Mock(
            return_value=(
                1,
                "FAILED test_app.py::test_value - app.py assertion mismatch",
            )
        ),
    )
    monkeypatch.setattr(
        app,
        "call_agent",
        Mock(
            return_value="QA_STATUS: FAILED\nWrite approval status into source. Measure real model speed."
        ),
    )
    app.step_testing()
    assert app.state["state"] == "IMPLEMENTING"
    task = app.state["tasks"][-1]
    assert "assertion mismatch" in task["description"]
    assert "Write approval status" not in task["description"]
    assert "Measure real model" not in task["description"]
    assert task["target_files"] == ["app.py"]


@pytest.mark.parametrize(
    "kind,file,evidence,requirement,accepted",
    [
        ("code", "app.py", "app.py: unsafe()", "Only modify app.py", True),
        ("process", "app.py", "app.py: unsafe()", "Only modify app.py", False),
        ("environment", "app.py", "app.py: unsafe()", "Only modify app.py", False),
        ("code", "outside.py", "app.py: unsafe()", "Only modify app.py", False),
        ("code", "app.py", "invented evidence", "Only modify app.py", False),
        ("code", "app.py", "app.py: unsafe()", "invented requirement", False),
    ],
)
def test_findings_require_in_scope_file_and_verbatim_evidence(
    kind, file, evidence, requirement, accepted
):
    report = "QA_STATUS: FAILED\nQA_FINDINGS: " + json.dumps(
        [
            {
                "kind": kind,
                "file": file,
                "evidence": evidence,
                "requirement": requirement,
                "fix": "Remove the unsafe call",
            }
        ]
    )
    assert (
        bool(qa_code_findings(report, ["app.py"], "app.py: unsafe()", REQUEST))
        is accepted
    )


def test_grounded_defect_still_blocks_successful_tests(qa_app, monkeypatch):
    app = qa_app
    monkeypatch.setattr(
        app, "run_command", Mock(return_value=(0, "tests passed\napp.py: unsafe()"))
    )
    finding = {
        "kind": "code",
        "file": "app.py",
        "requirement": "Only modify app.py",
        "evidence": "app.py: unsafe()",
        "fix": "Remove the unsafe call",
    }
    monkeypatch.setattr(
        app,
        "call_agent",
        Mock(
            return_value="QA_STATUS: FAILED\nConcrete defect\nQA_FINDINGS: "
            + json.dumps([finding])
        ),
    )
    app.step_testing()
    assert app.state["state"] == "IMPLEMENTING"
    assert "Remove the unsafe call" in app.state["tasks"][-1]["description"]


def test_developer_gets_current_approval_instead_of_stale_task_demand(
    qa_app, monkeypatch
):
    app = qa_app
    app.record_implementation_approval("approve")
    app.state["state"] = "IMPLEMENTING"
    app.state["tasks"][0].update(
        status="pending",
        description="Implement the fix; historical QA claims owner approval is absent.",
    )
    monkeypatch.setattr(
        app,
        "allocate_workers",
        Mock(return_value=([("rd-senior-1", "senior")], {"T1": "rd-senior-1"})),
    )
    call = Mock(return_value="[FILE_START: app.py]\nVALUE = 1\n[FILE_END: app.py]")
    monkeypatch.setattr(app, "call_agent", call)
    app.step_implementing()
    prompt = call.call_args.args[1]
    assert REQUEST in prompt
    assert '"matches_current_request_and_plan": true' in prompt
    assert "Never encode approval history" in prompt
    assert app.state["state"] == "TESTING"


@pytest.mark.parametrize("action", ["revise", "reset"])
def test_revising_or_resetting_revokes_previous_approval(qa_app, monkeypatch, action):
    app = qa_app
    app.record_implementation_approval("approve")
    app.state.update(state="WAITING_FOR_OWNER", human_review_source="Architect")
    if action == "revise":
        apply_review_decision(app, "revise")
    else:
        monkeypatch.setattr(app, "cleanup_worktree", Mock(return_value=True))
        clear_run_state(app, "PLANNING")
    assert not app.implementation_approval_evidence()[
        "matches_current_request_and_plan"
    ]
