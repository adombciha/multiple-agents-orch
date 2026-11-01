"""Keep approval evidence and owner-only decisions out of code repair loops."""

import json
from unittest.mock import Mock

import pytest

from orchestrator.main import apply_approve, apply_review_decision
from orchestrator.roles.reviewer import REVIEW_SCHEMA, is_code_review
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config

REQUEST = (
    "Only modify app.py. Run full offline regression using isolated temporary fixtures. "
    "Do not run real model performance tests."
)
APPROVED = {"status": "APPROVED", "feedback": [], "process_feedback": []}


@pytest.fixture
def review_app(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    app.request_path.write_text(REQUEST)
    app.requirements_path.write_text("Implement app.py with offline tests.")
    app.plan_path.write_text("Implement app.py after owner approval.")
    app.test_results_path.write_text("Exit code: 0\n749 passed")
    app.qa_report_path.write_text("QA_STATUS: PASSED\nOffline regression passed.")
    app.state.update(
        state="REVIEWING_CODE",
        tasks=[{"id": "T1", "target_files": ["app.py"], "status": "completed"}],
    )
    monkeypatch.setattr(app, "consult_specialists", Mock(return_value=""))
    monkeypatch.setattr(app, "call_agent", Mock(return_value=json.dumps(APPROVED)))
    monkeypatch.setattr(app, "escalate_developer_backend", Mock())
    monkeypatch.setattr(app, "get_active_model_for_role", Mock(return_value="offline"))
    monkeypatch.setattr("orchestrator.roles.reviewer.scoring.record_event", Mock())
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        Mock(side_effect=AssertionError("Unexpected network")),
    )
    return app


@pytest.mark.parametrize("changed", [None, "request", "plan"])
def test_review_and_specialists_receive_actual_approval_and_original_request(
    review_app, changed
):
    app = review_app
    app.record_implementation_approval("approve")
    record = dict(app.state["implementation_approval"])
    if changed:
        path = app.request_path if changed == "request" else app.plan_path
        path.write_text(path.read_text() + " Updated scope.")
    # A persisted machine context must not replace the authoritative request.
    app.agent_context_path.write_text(json.dumps({"request": "Stale request"}))

    app.step_reviewing_code()

    evidence = app.implementation_approval_evidence()
    assert evidence["matches_current_request_and_plan"] is (changed is None)
    assert evidence["record"] == record
    prompt = app.call_agent.call_args.args[1]
    context = app.consult_specialists.call_args.kwargs["context"]
    for text in (prompt, context):
        assert app.request_path.read_text() in text
        assert app.requirements_path.read_text() in text
        assert app.plan_path.read_text() in text
        assert json.dumps(evidence, ensure_ascii=False) in text
        assert "not that approval preceded the first edit" in text
        assert "not proof of an unauthorized edit" in text
        assert "isolated temporary test fixtures" in text
        assert "only offline validation was requested" in text
        assert "not additional owner requirements or approval evidence" in text
    assert "749 passed" in prompt
    assert app.call_agent.call_args.args[4] is is_code_review
    assert app.call_agent.call_args.args[5] == REVIEW_SCHEMA
    assert "process_feedback" in prompt
    assert app.state["state"] == "WAITING_FOR_DELIVERY"
    assert app.state["delivery"]["delivered"] is False


def test_review_does_not_invent_approval_for_a_legacy_run(review_app):
    app = review_app
    app.step_reviewing_code()
    assert '"record": {}' in app.call_agent.call_args.args[1]
    assert (
        '"matches_current_request_and_plan": false' in app.call_agent.call_args.args[1]
    )
    assert "implementation_approval" not in app.state


@pytest.mark.parametrize("matching_approval", [False, True])
@pytest.mark.parametrize("code_findings", [[], ["app.py: replace the unsafe call."]])
def test_process_blocker_pauses_without_rd_tasks_penalties_or_revision_cost(
    review_app, matching_approval, code_findings
):
    app = review_app
    if matching_approval:
        app.record_implementation_approval("approve")
    app.state["code_revisions"] = app.config["max_revisions"]
    app.state["plan_escalations"] = app.config["max_plan_escalations"]
    state_before = json.loads(json.dumps(app.state))
    blocker = "Owner must resolve the missing historical first-edit approval record."
    app.call_agent.return_value = json.dumps(
        {"status": "REJECTED", "feedback": code_findings, "process_feedback": [blocker]}
    )

    app.step_reviewing_code()

    app.load_config_and_state()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["human_review_source"] == "Orchestrator"
    assert app.state["resume_state"] == app.state["pass_state"] == "REVIEWING_CODE"
    assert blocker in app.state["human_review_details"]
    for finding in code_findings:
        assert finding in app.reviewer_output_path.read_text()
    assert app.state["tasks"] == state_before["tasks"]
    assert app.state["code_revisions"] == state_before["code_revisions"]
    assert app.state["plan_escalations"] == state_before["plan_escalations"]
    assert app.state.get("implementation_approval") == state_before.get(
        "implementation_approval"
    )
    app.escalate_developer_backend.assert_not_called()
    from orchestrator.roles.reviewer import scoring

    scoring.record_event.assert_not_called()
    saved = json.loads(app.reviewer_output_json_path.read_text())
    assert saved["process_feedback"] == [blocker]
    assert (
        "Process blockers (owner decision required)"
        in app.human_report_path.read_text()
    )


@pytest.mark.parametrize("decision", ["approve", "pass"])
def test_process_decision_retries_review_instead_of_implementation_or_delivery(
    review_app, decision
):
    app = review_app
    app.call_agent.return_value = json.dumps(
        {
            "status": "REJECTED",
            "feedback": [],
            "process_feedback": ["Clarify approval."],
        }
    )
    app.step_reviewing_code()
    if decision == "approve":
        assert apply_approve(app)
    else:
        apply_review_decision(app, "pass")
    assert app.state["state"] == "REVIEWING_CODE"
    assert "implementation_approval" not in app.state
    assert len(app.state["tasks"]) == 1

    app.call_agent.return_value = json.dumps(APPROVED)
    app.step_reviewing_code()
    assert app.state["state"] == "WAITING_FOR_DELIVERY"
    assert not app.state["delivery"]["delivered"]


def test_matching_approval_does_not_bypass_actual_out_of_scope_code_rejection(
    review_app,
):
    app = review_app
    app.record_implementation_approval("approve")
    finding = "outside.py: remove this change; the owner only authorized app.py."
    app.call_agent.return_value = json.dumps(
        {"status": "REJECTED", "feedback": [finding], "process_feedback": []}
    )
    app.step_reviewing_code()
    assert app.state["state"] == "IMPLEMENTING"
    assert app.state["code_revisions"] == 1
    assert finding in app.state["tasks"][-1]["description"]
    app.escalate_developer_backend.assert_called_once()


@pytest.mark.parametrize(
    "response,valid",
    [
        (APPROVED, True),
        (
            {
                "status": "REJECTED",
                "feedback": ["app.py defect"],
                "process_feedback": [],
            },
            True,
        ),
        (
            {
                "status": "REJECTED",
                "feedback": [],
                "process_feedback": ["Owner decision"],
            },
            True,
        ),
        ({"status": "APPROVED", "feedback": []}, False),
        ({**APPROVED, "process_feedback": ["Missing approval"]}, False),
        ({**APPROVED, "status": "REJECTED"}, False),
        ({**APPROVED, "feedback": [None]}, False),
        ({**APPROVED, "process_feedback": {}}, False),
        ({**APPROVED, "process_feedback": [" "]}, False),
        ({**APPROVED, "status": []}, False),
        ({**APPROVED, "extra": True}, False),
        ([], False),
    ],
)
def test_review_contract_separates_code_and_process_findings(response, valid):
    assert is_code_review(json.dumps(response)) is valid


def test_invalid_review_cannot_generate_repairs_or_allow_delivery(review_app):
    app = review_app
    app.call_agent.return_value = '{"status":"REJECTED","feedback":["Get approval"]}'
    app.step_reviewing_code()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["resume_state"] == app.state["pass_state"] == "REVIEWING_CODE"
    assert len(app.state["tasks"]) == 1
    app.escalate_developer_backend.assert_not_called()


@pytest.mark.parametrize("changed", [False, True])
def test_review_receives_explicit_acceptance_bound_to_actual_contents(
    review_app, changed
):
    app = review_app
    (app.workspace / "app.py").write_text("VALUE = 1\n")
    app.call_agent.return_value = json.dumps(
        {
            "status": "REJECTED",
            "feedback": [],
            "process_feedback": ["Resolve first-edit approval timing."],
        }
    )
    app.step_reviewing_code()
    preview = app.present_existing_changes()
    apply_review_decision(app, "accept-existing", snapshot_id=preview["id"])
    record = dict(app.state["existing_changes_acceptance"])
    if changed:
        (app.workspace / "app.py").write_text("VALUE = 2\n")
    app.call_agent.return_value = json.dumps(APPROVED)

    app.step_reviewing_code()

    evidence = app.existing_changes_acceptance_evidence()
    assert evidence["matches_current_changes"] is (not changed)
    assert evidence["record"] == record
    for text in (
        app.call_agent.call_args.args[1],
        app.consult_specialists.call_args.kwargs["context"],
    ):
        assert json.dumps(evidence, ensure_ascii=False) in text
        assert "Do not request the same historical proof again" in text
        assert "does not establish prior approval" in text
        assert "A mismatched acceptance must not be used" in text
    assert app.state["state"] == "WAITING_FOR_DELIVERY"
    assert not app.state["delivery"]["delivered"]


def test_accepted_content_changing_during_review_cannot_reach_delivery(review_app):
    app = review_app
    path = app.workspace / "app.py"
    path.write_text("VALUE = 1\n")
    app.call_agent.return_value = json.dumps(
        {
            "status": "REJECTED",
            "feedback": [],
            "process_feedback": ["Resolve approval timing."],
        }
    )
    app.step_reviewing_code()
    preview = app.present_existing_changes()
    apply_review_decision(app, "accept-existing", snapshot_id=preview["id"])

    def review(*args, **kwargs):
        path.write_text("VALUE = 2\n")
        return json.dumps(APPROVED)

    app.call_agent.side_effect = review
    app.step_reviewing_code()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert "changed during review" in app.state["human_review_details"]
    assert app.state["resume_state"] == "REVIEWING_CODE"
    assert len(app.state["tasks"]) == 1
