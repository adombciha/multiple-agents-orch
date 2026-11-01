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
        "plan_requires_owner_approval": True,
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
    app.pause_for_human_review(
        "Architect",
        APPROVAL,
        "IMPLEMENTING",
        "IMPLEMENTING",
        decision_kind="approve_plan",
    )
    assert app.read_state_snapshot()["human_review_kind"] == "approve_plan"
    snapshot = app.status_path.read_text()
    assert '"command": "approve"' in snapshot
    app.state["human_review_details"] = "Translated wording"
    assert continue_decision(app.state) == ("approve", False)


def test_busy_terminal_state_does_not_offer_mutating_commands():
    assert next_actions({"state": "FAILED"}, busy=True) == []


@pytest.fixture
def policy_app(tmp_path, monkeypatch):
    app = AgentOrchestrator(tmp_path)
    monkeypatch.setattr(app, "get_windows_host_ip", lambda: None)
    app.init_project()
    app.load_config_and_state()
    app.request_path.write_text("Implement app.py after owner approval.")
    app.plan_path.write_text("Implement only app.py, then run offline checks.")
    return app


@pytest.mark.parametrize(
    "stage",
    [
        "PLANNING",
        "DEVELOPING_PLAN",
        "REVIEWING_PLAN",
        "IMPLEMENTING",
        "TESTING",
        "REVIEWING_CODE",
        "RESEARCHING",
        "WAITING_FOR_DELIVERY",
        "COMPLETED",
        "FAILED",
    ],
)
@pytest.mark.parametrize("operation", ["approve", "pass", "revise", "reject"])
def test_owner_commands_outside_decision_point_have_no_side_effects(
    policy_app, stage, operation
):
    import copy
    from orchestrator.main import apply_approve, apply_review_decision

    app = policy_app
    app.state["state"] = stage
    app.save_state()
    app.request_pause()
    before = copy.deepcopy(app.state)
    saved = app.state_path.read_bytes()
    with pytest.raises(RuntimeError, match="not waiting"):
        apply_approve(app) if operation == "approve" else apply_review_decision(
            app, operation
        )
    assert app.state == before
    assert app.state_path.read_bytes() == saved
    assert app.pause_requested()
    assert not app.implementation_approval_evidence()[
        "matches_current_request_and_plan"
    ]


@pytest.mark.parametrize("source", ["QA_RESPONSE", "QA"])
@pytest.mark.parametrize("operation", ["pass", "revise"])
def test_test_retry_cannot_be_overridden_as_a_pass_or_code_repair(
    policy_app, source, operation
):
    from orchestrator.main import apply_review_decision

    app = policy_app
    app.pause_for_human_review(
        source,
        "Retry only",
        "TESTING",
        "TESTING",
        decision_kind="resume" if source == "QA_RESPONSE" else "retry_testing",
    )
    before = app.state_path.read_bytes()
    with pytest.raises(RuntimeError, match="not available"):
        apply_review_decision(app, operation)
    assert app.state_path.read_bytes() == before
    assert not app.state.get("tasks")
    decision = workflow_decision(app.state)
    assert not any(
        (item["body"] or {}).get("decision") in {"pass", "revise"}
        for item in decision["actions"]
    )


def test_new_pause_requires_explicit_architect_verdict_and_survives_restart(policy_app):
    from orchestrator.main import apply_continue

    app = policy_app
    app.state["plan_requires_owner_approval"] = True
    app.pause_for_human_review("Architect", APPROVAL, "IMPLEMENTING", "IMPLEMENTING")
    assert owner_review_kind(app.state) == "review"
    app.pause_for_human_review(
        "Architect",
        "核准的文字可以改變",
        "IMPLEMENTING",
        "IMPLEMENTING",
        decision_kind="approve_plan",
    )
    restarted = AgentOrchestrator(app.workspace)
    restarted.load_config_and_state()
    assert owner_review_kind(restarted.state) == "approve_plan"
    report = restarted.human_report_path.read_text()
    assert workflow_decision(restarted.state)["title"] in report
    assert "驗證未通過" not in report
    assert apply_continue(restarted)
    assert restarted.state["state"] == "IMPLEMENTING"
    assert not restarted.state.get("tasks")
    assert restarted.implementation_approval_evidence()[
        "matches_current_request_and_plan"
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"human_review_source": "Reviewer"},
        {"pass_state": "COMPLETED"},
        {"resume_state": "DEVELOPING_PLAN"},
        {"plan_requires_owner_approval": False},
        {"human_review_kind": "unexpected"},
    ],
)
def test_inconsistent_saved_approval_fails_closed(policy_app, change):
    from orchestrator.main import apply_continue

    app = policy_app
    app.state["plan_requires_owner_approval"] = True
    app.pause_for_human_review(
        "Architect",
        "Approved",
        "IMPLEMENTING",
        "IMPLEMENTING",
        decision_kind="approve_plan",
    )
    app.state.update(change)
    app.save_state()
    app.request_pause()
    before = app.state_path.read_bytes()
    with pytest.raises(RuntimeError):
        apply_continue(app)
    assert app.state_path.read_bytes() == before
    assert app.pause_requested()
    assert not app.implementation_approval_evidence()[
        "matches_current_request_and_plan"
    ]
    assert all(
        item["action"] == "review" and item["body"] == {"decision": "reject"}
        for item in workflow_decision(app.state)["actions"]
    )


def test_process_blocker_cannot_generate_a_code_repair_task(policy_app):
    from orchestrator.main import apply_review_decision

    app = policy_app
    app.state["owner_acceptance_pending"] = True
    app.pause_for_human_review(
        "Orchestrator",
        "Owner must decide",
        "REVIEWING_CODE",
        "REVIEWING_CODE",
        decision_kind="accept_existing",
    )
    before = app.state_path.read_bytes()
    with pytest.raises(RuntimeError, match="not available"):
        apply_review_decision(app, "revise")
    assert app.state_path.read_bytes() == before
    assert not app.state.get("tasks")


def test_gui_revalidates_a_stale_approval_before_clearing_pause(monkeypatch):
    from test_gui import make_service, start_worker_inline

    service = make_service("WAITING_FOR_OWNER")
    service.orchestrator.state.update(
        human_review_kind="approve_plan",
        human_review_source="Architect",
        plan_requires_owner_approval=True,
        resume_state="IMPLEMENTING",
        pass_state="COMPLETED",
    )
    start_worker_inline(service)
    with pytest.raises(RuntimeError):
        service.approve()
    service.orchestrator.clear_pause_request.assert_not_called()
    service.orchestrator.save_state.assert_not_called()


@pytest.mark.parametrize(
    "source,target",
    [
        ("Owner", "WAITING_FOR_DELIVERY"),
        ("QA_RESPONSE", "REVIEWING_CODE"),
        ("Developer", "COMPLETED"),
    ],
)
def test_continue_never_grants_delivery_or_bypasses_invalid_qa(
    policy_app, source, target
):
    from orchestrator.main import apply_continue

    app = policy_app
    app.state.update(
        state="WAITING_FOR_OWNER",
        human_review_source=source,
        resume_state=target,
        pass_state=target,
    )
    if source == "Owner":
        assert apply_continue(app)
        assert app.state["state"] == "WAITING_FOR_DELIVERY"
        assert not app.state.get("delivery_confirmed")
    else:
        with pytest.raises(RuntimeError):
            apply_continue(app)
        assert app.state["state"] == "WAITING_FOR_OWNER"


@pytest.mark.parametrize(
    "stage,prepared",
    [
        ("IMPLEMENTING", True),
        ("TESTING", True),
        ("REVIEWING_CODE", True),
        ("WAITING_FOR_OWNER", True),
        ("COMPLETED", True),
        ("WAITING_FOR_DELIVERY", False),
    ],
)
def test_delivery_confirmation_checks_stage_before_git_or_state_mutation(
    policy_app, stage, prepared, monkeypatch
):
    from unittest.mock import Mock

    app = policy_app
    app.state.update(
        state=stage,
        delivery_artifacts_prepared=prepared,
        delivery_preview_presented=True,
        delivery_preview={"scheduled_operation": "merge"},
    )
    app.save_state()
    before = app.state_path.read_bytes()
    build = Mock(
        side_effect=AssertionError("Invalid confirmation must not inspect Git")
    )
    monkeypatch.setattr(app, "_build_delivery_preview", build)
    with pytest.raises(RuntimeError):
        app.confirm_delivery(app.state["delivery_preview"], "merge")
    assert app.state_path.read_bytes() == before
    assert not app.state.get("delivery_confirmed")
    build.assert_not_called()


def test_failed_gui_replan_uses_real_endpoint_and_preserves_request(
    policy_app, monkeypatch
):
    from unittest.mock import Mock
    from orchestrator.gui import GuiService
    from test_gui import post_action

    app = policy_app
    app.state.update(state="FAILED", tasks=[{"id": "T1", "status": "pending"}])
    app.save_state()
    request = app.request_path.read_bytes()
    service = GuiService(app.workspace)
    monkeypatch.setattr(
        service.orchestrator, "cleanup_worktree", Mock(return_value=True)
    )
    status = service.status()
    assert any(
        item["action"] == "reject" for item in status["workflow_decision"]["actions"]
    )
    code, response = post_action(service, "/api/reject")
    assert code == 200
    assert response["state"] == "PLANNING"
    assert service.orchestrator.state["tasks"] == []
    assert app.request_path.read_bytes() == request
    service.orchestrator.state["state"] = "WAITING_FOR_DELIVERY"
    service.orchestrator.save_state()
    before = app.state_path.read_bytes()
    code, response = post_action(service, "/api/reject")
    assert code == 409
    assert app.state_path.read_bytes() == before
