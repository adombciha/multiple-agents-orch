"""The same saved decision must drive CLI suggestions and GUI controls."""

from types import SimpleNamespace

import pytest

from orchestrator.core.state import AgentOrchestrator, delivery_snapshot_id
from orchestrator.core.status import continue_decision, next_actions, owner_review_kind, workflow_decision
from orchestrator.gui import render_page
from orchestrator.main import pick_continue_decision

APPROVAL = "Architect approved the plan. The original request requires owner approval before implementation."


@pytest.mark.parametrize(
    "source,reason,resume,passed,kind,command",
    [
        (
            "Architect",
            APPROVAL,
            "IMPLEMENTING",
            "IMPLEMENTING",
            "approve_plan",
            "approve",
        ),
        (
            "Architect",
            "PLAN_STATUS: REJECTED",
            "DEVELOPING_PLAN",
            "IMPLEMENTING",
            "revise",
            "review revise",
        ),
        (
            "Reviewer",
            "REJECTED",
            "IMPLEMENTING",
            "IMPLEMENTING",
            "revise",
            "review revise",
        ),
        (
            "QA",
            "Test command failed to launch (exit 127), so no tests were executed.",
            "TESTING",
            "TESTING",
            "retry_testing",
            "approve",
        ),
        ("QA_RESPONSE", "Invalid contract", "TESTING", "TESTING", "resume", "continue"),
        (
            "Developer",
            "Invalid edits",
            "IMPLEMENTING",
            "IMPLEMENTING",
            "resume",
            "continue",
        ),
        (
            "Orchestrator",
            "Reviewer requires an owner decision.",
            "REVIEWING_CODE",
            "REVIEWING_CODE",
            "accept_existing",
            "review preview-existing",
        ),
    ],
)
def test_decision_matches_gui_cli_and_legacy_state(
    source, reason, resume, passed, kind, command
):
    state = {
        "state": "WAITING_FOR_OWNER",
        "human_review_source": source,
        "human_review_details": reason,
        "resume_state": resume,
        "pass_state": passed,
        "plan_requires_owner_approval": True,
    }
    before = dict(state)
    decision = workflow_decision(state)
    assert decision["kind"] == kind
    assert next_actions(state)[0]["command"] == command
    view = {**state, "review": {"source": source, "reason": reason}}
    page = render_page(view)
    primary = next(item for item in decision["actions"] if item["primary"])
    assert primary["label"] in page
    assert decision["title"] in page
    app = SimpleNamespace(state=state, config={"max_revisions": 2})
    assert pick_continue_decision(app) == continue_decision(state)
    assert state == before


def test_explicit_decision_survives_report_wording_changes():
    state = {
        "state": "WAITING_FOR_OWNER",
        "human_review_kind": "approve_plan",
        "human_review_source": "Architect",
        "resume_state": "IMPLEMENTING",
        "pass_state": "IMPLEMENTING",
        "human_review_details": "計畫已通過",
    }
    assert workflow_decision(state)["kind"] == "approve_plan"
    assert continue_decision(state) == ("approve", False)
    assert owner_review_kind({**state, "state": "IMPLEMENTING"}) == ""


def test_delivery_running_and_confirmation_are_distinct():
    preview = {"diff": "content", "operation": "merge"}
    state = {
        "state": "WAITING_FOR_DELIVERY",
        "delivery_preview": preview,
        "delivery_preview_presented": True,
        "delivery_artifacts_prepared": True,
    }
    decision = workflow_decision(
        state, busy=True, git=preview, snapshot_id=delivery_snapshot_id(preview)
    )
    assert "處理交付" in decision["title"]
    assert all(
        not item["enabled"]
        for item in decision["display_actions"] + decision["delivery_actions"]
    )
    assert next_actions(state, busy=True) == []
    assert next_actions(state)[-1][
        "command"
    ] == "deliver --confirm " + delivery_snapshot_id(preview)
    assert all(
        "confirm" not in item["command"]
        for item in next_actions({**state, "delivery_artifacts_prepared": False})
    )


def test_pause_persists_decision_kind_and_snapshot(tmp_path, monkeypatch):
    app = AgentOrchestrator(tmp_path)
    monkeypatch.setattr(app, "get_windows_host_ip", lambda: None)
    app.init_project()
    app.load_config_and_state()
    app.state["plan_requires_owner_approval"] = True
    app.pause_for_human_review("Architect", APPROVAL, "IMPLEMENTING", "IMPLEMENTING")
    assert app.read_state_snapshot()["human_review_kind"] == "approve_plan"
    snapshot = app.status_path.read_text()
    assert '"command": "approve"' in snapshot
    app.state["human_review_details"] = "Translated wording"
    assert continue_decision(app.state) == ("approve", False)


def test_busy_terminal_state_does_not_offer_mutating_commands():
    assert next_actions({"state": "FAILED"}, busy=True) == []
