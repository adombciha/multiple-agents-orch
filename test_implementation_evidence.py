"""Verify persisted authorization precedes controlled writable operations."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from orchestrator.core.state import AgentOrchestrator, ImplementationApprovalError
from orchestrator.main import apply_approve, apply_review_decision
from orchestrator.core import backends
from orchestrator.gui import GuiService
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


@pytest.fixture
def implementation_app(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    app.request_path.write_text("Only modify app.py after owner approval.")
    app.requirements_path.write_text(app.request_path.read_text())
    app.plan_path.write_text("Implement app.py and verify offline.")
    app.state.update(
        state="IMPLEMENTING",
        plan_requires_owner_approval=True,
        tasks=[
            {
                "id": "T1",
                "description": "Implement app.py",
                "status": "pending",
                "target_files": ["app.py"],
                "rd_level": "senior",
                "qa_level": "senior",
            }
        ],
    )
    monkeypatch.setattr(
        app,
        "allocate_workers",
        Mock(return_value=([("rd-senior-1", "senior")], {"T1": "rd-senior-1"})),
    )
    monkeypatch.setattr(app, "get_active_model_for_role", Mock(return_value="offline"))
    monkeypatch.setattr("orchestrator.roles.developer.brain.remember", Mock())
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        Mock(side_effect=AssertionError("Unexpected network")),
    )
    app.save_state()
    return app


def test_start_is_persisted_before_provider_and_response_application(
    implementation_app, monkeypatch
):
    app = implementation_app
    app.record_implementation_approval("approve")
    approval = deepcopy(app.state["implementation_approval"])
    assert "implementation_start" not in approval

    def provider(*args, **kwargs):
        saved = app.read_state_snapshot()["implementation_approval"]
        assert saved["implementation_start"]["approval_id"] == approval["approval_id"]
        assert not (app.workspace / "app.py").exists()
        return "[FILE_START: app.py]\nVALUE = 1\n[FILE_END: app.py]"

    call = Mock(side_effect=provider)
    monkeypatch.setattr(app, "call_agent", call)
    app.step_implementing()
    assert app.state["state"] == "TESTING"
    assert (
        app.implementation_approval_evidence()["controlled_implementation"]
        == "confirmed"
    )
    assert (
        app.state["implementation_approval"]["approved_at"] == approval["approved_at"]
    )
    assert (app.workspace / "app.py").read_text().strip() == "VALUE = 1"


@pytest.mark.parametrize(
    "mutation", ["missing", "request", "plan", "scope", "content", "type"]
)
def test_unapproved_or_changed_baseline_blocks_provider(
    implementation_app, monkeypatch, mutation
):
    app = implementation_app
    app.record_implementation_approval("approve")
    if mutation == "missing":
        app.state.pop("implementation_approval")
    elif mutation in {"request", "plan"}:
        path = app.request_path if mutation == "request" else app.plan_path
        path.write_text(path.read_text() + " Changed.")
    elif mutation == "scope":
        app.state["tasks"][0]["target_files"].append("outside.py")
    elif mutation == "content":
        (app.workspace / "app.py").write_text("EXTERNAL = True\n")
    else:
        (app.workspace / "app.py").symlink_to("missing-target")
    call = Mock(side_effect=AssertionError("Writable provider must not run"))
    monkeypatch.setattr(app, "call_agent", call)
    app.step_implementing()
    call.assert_not_called()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert "implementation_start" not in app.state.get("implementation_approval", {})
    assert "approval" in app.state["human_review_details"].lower()


@pytest.mark.parametrize("phase", ["approval", "start"])
def test_failed_persistence_never_authorizes_a_write(
    implementation_app, monkeypatch, phase
):
    app = implementation_app
    if phase == "start":
        app.record_implementation_approval("approve")
    before = deepcopy(app.state)
    with monkeypatch.context() as fail:
        fail.setattr(app, "save_state", Mock(side_effect=OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            if phase == "approval":
                app.record_implementation_approval("approve")
            else:
                app.prepare_implementation()
    assert app.state == before
    assert "implementation_start" not in app.read_state_snapshot().get(
        "implementation_approval", {}
    )


@pytest.mark.parametrize("started", [False, True])
def test_restart_and_same_scope_retry_preserve_first_records(
    implementation_app, started
):
    app = implementation_app
    app.record_implementation_approval("approve")
    if started:
        app.prepare_implementation()
        (app.workspace / "app.py").write_text("MANAGED_CHANGE = True\n")
    prior = deepcopy(app.state["implementation_approval"])
    restarted = AgentOrchestrator(app.workspace)
    restarted.load_config_and_state()
    restarted.record_implementation_approval("pass")
    assert restarted.state["implementation_approval"] == prior
    restarted.prepare_implementation()
    assert (
        restarted.state["implementation_approval"]["approval_id"]
        == prior["approval_id"]
    )
    if started:
        assert restarted.state["implementation_approval"] == prior


def test_changed_scope_keeps_prior_approval_and_start_as_history(implementation_app):
    app = implementation_app
    app.record_implementation_approval("approve")
    app.prepare_implementation()
    prior = deepcopy(app.state["implementation_approval"])
    app.state["tasks"][0]["target_files"].append("other.py")
    assert (
        app.implementation_approval_evidence()["controlled_implementation"]
        == "mismatch"
    )
    with pytest.raises(ImplementationApprovalError, match="scope"):
        app.prepare_implementation()
    app.record_implementation_approval("pass")
    assert app.state["implementation_approval_history"] == [prior]
    assert app.state["implementation_approval"]["approval_id"] != prior["approval_id"]
    app.prepare_implementation()


def test_approval_history_prompt_is_bounded_without_changing_saved_history(
    implementation_app,
):
    app = implementation_app
    app.record_implementation_approval("approve")
    app.prepare_implementation()
    current = deepcopy(app.state["implementation_approval"])
    files = [f"previous_{number}.py" for number in range(200)]
    history = [
        {
            **deepcopy(current),
            "approval_id": f"previous-{number}",
            "target_files": files,
            "baseline": {
                **deepcopy(current["baseline"]),
                "file_baseline": {
                    path: {"sha256": "a" * 64, "mode": 33188} for path in files
                },
            },
        }
        for number in range(40)
    ]
    app.state["implementation_approval_history"] = history
    app.save_state()
    saved = app.state_path.read_bytes()

    evidence = app.implementation_approval_evidence()
    assert evidence["record"] == current
    assert evidence["approval_history_count"] == 40
    assert [item["approval_id"] for item in evidence["approval_history"]] == [
        "previous-37",
        "previous-38",
        "previous-39",
    ]
    assert all(
        item["target_file_count"] == 200
        and item["baseline_fingerprint"] == current["baseline"]["fingerprint"]
        and item["implementation_started"]
        and item["started_at"] == current["implementation_start"]["started_at"]
        and "file_baseline" not in item
        and "target_files" not in item
        for item in evidence["approval_history"]
    )
    assert len(json.dumps(evidence)) < 8000
    assert len(saved) > 1_000_000
    assert app.state["implementation_approval_history"] == history
    assert app.read_state_snapshot()["implementation_approval_history"] == history
    assert app.state_path.read_bytes() == saved


def test_same_scope_owner_revision_keeps_controlled_start(implementation_app):
    app = implementation_app
    app.record_implementation_approval("approve")
    app.prepare_implementation()
    prior = deepcopy(app.state["implementation_approval"])
    app.state.update(
        state="WAITING_FOR_OWNER",
        human_review_source="Reviewer",
        human_review_kind="revise",
        resume_state="IMPLEMENTING",
        pass_state="IMPLEMENTING",
        human_review_details="app.py: correct VALUE",
    )
    apply_review_decision(app, "revise")
    assert app.state["implementation_approval"] == prior
    app.prepare_implementation()


def test_legacy_record_is_unknown_and_cannot_start_writes(implementation_app):
    app = implementation_app
    app.record_implementation_approval("approve")
    record = app.state["implementation_approval"]
    for key in ("baseline", "approval_id", "target_files"):
        record.pop(key)
    app.save_state()
    assert (
        app.implementation_approval_evidence()["controlled_implementation"] == "unknown"
    )
    with pytest.raises(ImplementationApprovalError):
        app.prepare_implementation()
    assert "implementation_start" not in record


def test_clock_order_is_not_used_as_authorization_proof(implementation_app):
    app = implementation_app
    app.record_implementation_approval("approve")
    app.prepare_implementation()
    app.state["implementation_approval"]["approved_at"] = 500
    app.state["implementation_approval"]["implementation_start"]["started_at"] = 1
    app.save_state()
    assert app.prepare_implementation()["controlled_implementation"] == "confirmed"


def test_tampered_or_unpersisted_start_is_rejected(implementation_app):
    app = implementation_app
    app.record_implementation_approval("approve")
    app.prepare_implementation()
    app.state["implementation_approval"]["implementation_start"][
        "approval_id"
    ] = "another"
    assert (
        app.implementation_approval_evidence()["controlled_implementation"]
        == "mismatch"
    )
    with pytest.raises(ImplementationApprovalError, match="not persisted"):
        app.prepare_implementation()
    app.save_state()
    with pytest.raises(ImplementationApprovalError, match="does not match"):
        app.prepare_implementation()


def test_scope_change_during_provider_response_blocks_application(
    implementation_app, monkeypatch
):
    app = implementation_app
    app.record_implementation_approval("approve")

    def provider(*args, **kwargs):
        app.plan_path.write_text("A different plan")
        return "[FILE_START: app.py]\nVALUE = 1\n[FILE_END: app.py]"

    monkeypatch.setattr(app, "call_agent", provider)
    app.step_implementing()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert not (app.workspace / "app.py").exists()


def test_automatic_plan_authorization_has_no_fabricated_owner_click(implementation_app):
    app = implementation_app
    with pytest.raises(ImplementationApprovalError, match="explicit owner"):
        app.record_implementation_approval("request_and_architect")
    app.state["plan_requires_owner_approval"] = False
    app.record_implementation_approval("request_and_architect")
    record = app.state["implementation_approval"]
    assert record["source"] == "request_and_architect"
    assert "approved_at" not in record
    assert app.prepare_implementation()["controlled_implementation"] == "confirmed"
    app.state["plan_requires_owner_approval"] = True
    with pytest.raises(ImplementationApprovalError, match="scope"):
        app.prepare_implementation()


@pytest.mark.parametrize("entry", ["routes", "direct", "quota_fallback"])
def test_approval_errors_stop_fallback_without_model_penalties(
    implementation_app, monkeypatch, entry
):
    app = implementation_app
    app.record_implementation_approval("approve")
    app.state["active_task_id"] = "T1"
    app.config["backends"]["developer_senior"] = "agy"
    app.config["role_model_routes"] = (
        {"developer_senior": [["agy", "first"], ["agy", "second"]]}
        if entry == "routes"
        else {}
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    call = Mock(side_effect=ImplementationApprovalError("Scope changed"))
    fallback = Mock(side_effect=AssertionError("Must not fall back"))
    monkeypatch.setattr(app, "call_agy", call)
    monkeypatch.setattr(app, "call_agent_ollama_fallback", fallback)
    with pytest.raises(ImplementationApprovalError, match="Scope changed"):
        if entry == "quota_fallback":
            app.call_agy_quota_fallback("developer_senior", "prompt")
        else:
            app.call_agent("developer_senior", "prompt")
    call.assert_called_once()
    fallback.assert_not_called()
    assert not app.state.get("failed_model_routes")
    assert not app.state.get("task_failed_model_routes", {}).get("T1")
    assert not app.state.get("task_contract_failures", {}).get("T1")


@pytest.mark.parametrize("entry", ["cli", "gui"])
def test_cli_and_gui_approval_create_same_evidence_contract(
    implementation_app, monkeypatch, entry
):
    app = implementation_app
    app.state.update(
        state="WAITING_FOR_OWNER",
        human_review_source="Architect",
        resume_state="IMPLEMENTING",
        pass_state="IMPLEMENTING",
        human_review_kind="approve_plan",
    )
    app.save_state()
    if entry == "cli":
        assert apply_approve(app)
    else:
        service = GuiService(app.workspace)
        monkeypatch.setattr(
            service, "_start_worker", lambda prepare: (prepare(), True)[1]
        )
        service.approve()
        app.load_config_and_state()
    record = app.read_state_snapshot()["implementation_approval"]
    assert record["source"] == "owner"
    assert record["target_files"] == ["app.py"]
    assert record["baseline"]["file_baseline"]["app.py"] == {
        "sha256": None,
        "mode": None,
    }
    assert "implementation_start" not in record
    assert app.prepare_implementation()["controlled_implementation"] == "confirmed"


def test_reviewer_receives_confirmed_controlled_scope(implementation_app, monkeypatch):
    app = implementation_app
    app.record_implementation_approval("approve")
    app.prepare_implementation()
    (app.workspace / "app.py").write_text("VALUE = 1\n")
    app.state["state"] = "REVIEWING_CODE"
    app.test_results_path.write_text("Exit Code: 0\nOffline checks passed.")
    call = Mock(
        return_value='{"status":"APPROVED","feedback":[],"process_feedback":[]}'
    )
    monkeypatch.setattr(app, "call_agent", call)
    monkeypatch.setattr(app, "consult_specialists", Mock(return_value=""))
    monkeypatch.setattr("orchestrator.roles.reviewer.scoring.record_event", Mock())
    app.step_reviewing_code()
    prompt = call.call_args.args[1]
    assert '"controlled_implementation": "confirmed"' in prompt
    assert '"matches_current_file_scope": true' in prompt
    assert "do not request another historical first-edit approval" in prompt
    assert "external edits and pre-existing changes are not authenticated" in prompt
    assert app.state["state"] == "WAITING_FOR_DELIVERY"
    assert not app.state["delivery"]["delivered"]


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("workspace_change", ["config", "missing_worktree"])
def test_workspace_change_blocks_response_application(
    implementation_app, started, workspace_change
):
    app = implementation_app
    worktree = app.ai_dir / "worktree"
    worktree.mkdir()
    original = "ROOT_ONLY = True\n"
    (app.workspace / "app.py").write_text(original)
    app.record_implementation_approval("approve")
    if started:
        app.prepare_implementation()
    if workspace_change == "config":
        app.config["use_worktree"] = False
    else:
        worktree.rename(app.ai_dir / "preserved_worktree")
    assert (
        app.implementation_approval_evidence()["controlled_implementation"]
        != "confirmed"
    )
    with pytest.raises(ImplementationApprovalError, match="workspace"):
        app.parse_and_write_files(
            "[FILE_START: app.py]\nVALUE = 2\n[FILE_END: app.py]", ["app.py"]
        )
    assert (app.workspace / "app.py").read_text() == original


@pytest.mark.parametrize("parent", [False, True])
@pytest.mark.parametrize("broken", [False, True])
@pytest.mark.parametrize("started", [False, True])
def test_target_symlinks_cannot_authorize_or_redirect_writes(
    implementation_app, parent, broken, started
):
    app = implementation_app
    target = "alias/app.py" if parent else "app.py"
    app.state["tasks"][0]["target_files"] = [target]
    if started:
        app.record_implementation_approval("approve")
        app.prepare_implementation()
    destination = app.workspace / ("other" if parent else "other.py")
    protected = destination / "app.py" if parent else destination
    original = "PROTECTED = True\n"
    if not broken:
        protected.parent.mkdir(parents=True, exist_ok=True)
        protected.write_text(original)
    link = app.workspace / ("alias" if parent else "app.py")
    link.symlink_to(destination, target_is_directory=parent)
    assert app.existing_changes_snapshot()["files"] == [target]
    if started:
        evidence = app.implementation_approval_evidence()
        assert evidence["controlled_implementation"] == "mismatch"
        assert "symlink" in evidence["file_scope_error"]
    if not started:
        with pytest.raises(ImplementationApprovalError, match="symlink"):
            app.record_implementation_approval("approve")
    with pytest.raises(ImplementationApprovalError, match="symlink"):
        app.parse_and_write_files(
            f"[FILE_START: {target}]\nVALUE = 3\n[FILE_END: {target}]", [target]
        )
    assert link.is_symlink()
    if broken:
        assert not protected.exists()
    else:
        assert protected.read_text() == original
