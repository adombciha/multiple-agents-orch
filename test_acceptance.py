"""Exercise approval, evidence, revision and delivery for individual requirements."""

from copy import deepcopy

import pytest

from orchestrator.core import acceptance
from orchestrator.core.state import (
    AgentOrchestrator,
    ImplementationApprovalError,
    delivery_snapshot_id,
)
from test_orchestrator import _prepare_gui_delivery_service

TEST_NODE = "tests/test_config.py::test_default_router"


@pytest.fixture
def app(tmp_path):
    app = _prepare_gui_delivery_service(tmp_path).orchestrator
    app.load_config_and_state()
    app.request_path.write_text("Replace the default router and check it on Windows.")
    app.plan_path.write_text("AC-01: verify the default. AC-02: verify Windows.")
    app.state["tasks"] = [
        {"id": "T1", "target_files": ["app.py"], "status": "completed"},
        {"id": "T2", "target_files": ["other.py"], "status": "completed"},
    ]
    app.state["acceptance_criteria"] = acceptance.parse_criteria(
        [
            {
                "requirement": "Use d3:4b by default",
                "kind": "pytest",
                "check": TEST_NODE,
                "task_ids": ["T1", "T2"],
            },
            {
                "requirement": "Make a decision with d3:4b on Windows",
                "kind": "manual",
                "check": "Use the Windows service and record the selected model.",
                "task_ids": ["T1"],
            },
        ],
        app.state["tasks"],
    )
    with pytest.raises(ImplementationApprovalError, match="explicit owner"):
        app.record_implementation_approval("request_and_architect")
    app.record_implementation_approval("approve")
    return app


def run_qa(app, output, code=0):
    acceptance.record_qa(
        app, "pytest -v", code, output, before=acceptance.qa_revision(app)
    )
    app.save_state()


def show(app):
    return app.present_delivery_preview(app.get_delivery_preview())


def test_revision_rechecks_evidence_and_only_then_delivers(app):
    run_qa(app, f"{TEST_NODE} PASSED [100%]")
    first = show(app)
    assert [row["status"] for row in first["acceptance"]] == ["passed", "pending"]
    with pytest.raises(acceptance.AcceptanceError, match="reason"):
        app.confirm_delivery(first)

    acceptance.revise(app, delivery_snapshot_id(first), ["AC-02"], "Verify again")
    assert app.state["state"] == "IMPLEMENTING"
    assert app.state["tasks"][-1]["target_files"] == ["app.py"]
    assert "AC-02" in app.state["tasks"][-1]["description"]
    assert app.state["acceptance_history"]
    assert app.implementation_approval_evidence()["matches_current_file_scope"]
    (app.agent_workspace() / "app.py").write_text("fixed router\n")
    run_qa(app, f"{TEST_NODE} PASSED [100%]")
    app.state.update(state="WAITING_FOR_DELIVERY", delivery_artifacts_prepared=True)
    app.save_state()
    second = show(app)
    acceptance.record_manual(
        app,
        delivery_snapshot_id(second),
        "AC-02",
        "passed",
        "Windows: selected model-a.",
    )
    with pytest.raises(RuntimeError):
        app.confirm_delivery(second)
    latest = show(app)
    assert [row["status"] for row in latest["acceptance"]] == ["passed", "passed"]
    app.confirm_delivery(latest)
    app.step_waiting_for_delivery()
    assert app.state["state"] == "COMPLETED"
    assert app.state["delivery"]["acceptance"] == latest["acceptance"]
    assert (app.workspace / "app.py").read_text() == "fixed router\n"


def test_pending_acknowledgement_persists_without_changing_verdict(app):
    run_qa(app, "2 passed, 1 skipped")
    preview = show(app)
    app.confirm_delivery(preview, acceptance_reason="Windows is unavailable.")
    app.load_config_and_state()
    assert app.validate_delivery_preview(preview)
    assert app.state["acceptance_delivery"] == {
        "criterion_ids": ["AC-01", "AC-02"],
        "reason": "Windows is unavailable.",
        "snapshot_id": delivery_snapshot_id(preview),
    }
    assert [row["status"] for row in acceptance.rows(app)] == ["pending", "pending"]


def test_grounded_failure_blocks_delivery_even_with_reason(app):
    run_qa(app, f"FAILED {TEST_NODE} - AssertionError: wrong default", code=1)
    preview = show(app)
    assert preview["acceptance"][0]["status"] == "failed"
    with pytest.raises(acceptance.AcceptanceError, match="Revise.*AC-01"):
        app.confirm_delivery(preview, acceptance_reason="Accept everything")


def test_named_pass_survives_another_test_failing(app):
    run_qa(
        app, f"{TEST_NODE} PASSED [50%]\nFAILED tests/test_other.py::test_other", code=1
    )
    assert acceptance.rows(app)[0]["status"] == "passed"


def test_revision_uses_original_approved_files_after_task_splitting(app):
    task = app.state["tasks"].pop(0)
    app.state["tasks"].append({**task, "id": "T1-1"})
    app.save_state()
    preview = show(app)
    acceptance.revise(app, delivery_snapshot_id(preview), ["AC-02"], "Verify again")
    assert app.state["tasks"][-1]["target_files"] == ["app.py"]


def test_known_failure_cannot_be_waived_after_revision_or_missing_results(app):
    first = show(app)
    acceptance.record_manual(
        app,
        delivery_snapshot_id(first),
        "AC-02",
        "failed",
        "Windows service rejected d3.",
    )
    second = show(app)
    acceptance.revise(app, delivery_snapshot_id(second), ["AC-01"], "Fix the default.")
    (app.agent_workspace() / "other.py").write_text("updated\n")
    run_qa(app, "1 passed, 1 skipped")
    app.state.update(state="WAITING_FOR_DELIVERY", delivery_artifacts_prepared=True)
    app.save_state()
    preview = show(app)
    assert preview["acceptance"][1]["status"] == "failed"
    assert preview["acceptance"][1]["needs_recheck"]
    with pytest.raises(acceptance.AcceptanceError, match="AC-02"):
        app.confirm_delivery(preview, acceptance_reason="Accept all pending criteria.")


def test_target_change_invalidates_evidence_and_preview(app):
    run_qa(app, f"{TEST_NODE} PASSED [100%]")
    preview = show(app)
    (app.agent_workspace() / "app.py").write_text("another revision\n")
    with pytest.raises(RuntimeError, match="changed"):
        app.confirm_delivery(preview)
    assert acceptance.rows(app)[0]["status"] == "pending"


def test_manual_evidence_is_bound_to_the_displayed_content(app, monkeypatch):
    preview = show(app)
    validate = app.validate_delivery_preview

    def replace_after_validation(*args, **kwargs):
        result = validate(*args, **kwargs)
        (app.agent_workspace() / "app.py").write_text("replaced during confirmation\n")
        return result

    monkeypatch.setattr(app, "validate_delivery_preview", replace_after_validation)
    acceptance.record_manual(
        app,
        delivery_snapshot_id(preview),
        "AC-02",
        "passed",
        "Observed at old revision.",
    )
    assert acceptance.rows(app)[1]["status"] == "pending"


def test_approval_freezes_the_criteria(app):
    frozen = deepcopy(app.state["implementation_approval"]["acceptance_criteria"])
    app.state["acceptance_criteria"][0]["requirement"] = "Different criterion"
    assert app.state["implementation_approval"]["acceptance_criteria"] == frozen
    assert not app.implementation_approval_evidence()["matches_current_file_scope"]
    with pytest.raises(acceptance.AcceptanceError, match="owner approval"):
        acceptance.rows(app)


def test_confirm_without_git_still_binds_target_content(tmp_path):
    app = AgentOrchestrator(tmp_path)
    app.init_project()
    app.request_path.write_text("Create demo.txt.")
    app.plan_path.write_text("Check the demo text.")
    target = tmp_path / "demo.txt"
    target.write_text("Demo\n")
    app.state.update(
        state="WAITING_FOR_DELIVERY",
        workflow_mode="delivery",
        delivery_artifacts_prepared=True,
        tasks=[{"id": "T1", "target_files": ["demo.txt"]}],
    )
    app.state["acceptance_criteria"] = acceptance.parse_criteria(
        [
            {
                "kind": "manual",
                "requirement": "Provide demo text",
                "check": "Read demo.txt",
                "task_ids": ["T1"],
            }
        ],
        app.state["tasks"],
    )
    app.record_implementation_approval("approve")
    preview = show(app)
    assert preview["scheduled_operation"] == "confirm_without_merge"
    target.write_text("Updated demo\n")
    with pytest.raises(RuntimeError, match="changed"):
        app.confirm_delivery(preview, acceptance_reason="Owner will check text later.")
    app.confirm_delivery(show(app), acceptance_reason="Owner will check text later.")
    app.step_waiting_for_delivery()
    assert app.state["state"] == "COMPLETED"
