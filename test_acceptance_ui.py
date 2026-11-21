import json
import pytest

from orchestrator import main as cli
from orchestrator.gui import render_page
from test_gui import GuiPage, make_git_delivery_service, post_action

CRITERION = {
    "id": "AC-01",
    "requirement": "Show <review> status",
    "kind": "manual",
    "check": "Open the dashboard and inspect the status",
    "task_ids": ["T1"],
}


def test_acceptance_controls_reflect_the_presented_rows():
    status = {
        "state": "WAITING_FOR_DELIVERY",
        "delivery_artifacts_prepared": True,
        "delivery_preview_presented": True,
        "delivery_snapshot_id": "current-snapshot",
    }
    for result in ("pending", "failed", "passed"):
        page = render_page(
            {
                **status,
                "git": {
                    "acceptance": [
                        {**CRITERION, "status": result, "evidence": "<b>sample</b>"}
                    ]
                },
            }
        )
        assert "Show &lt;review&gt; status" in page
        assert "&lt;b&gt;sample&lt;/b&gt;" in page
        controls = {
            button["attrs"]["data-action"]: button["attrs"]
            for button in GuiPage(page).buttons
        }
        assert (
            controls["acceptance-AC-01-passed"]["data-snapshot-id"]
            == "current-snapshot"
        )
        assert controls["acceptance-revise"]["data-snapshot-id"] == "current-snapshot"
        assert ("disabled" in controls["confirm-delivery"]) == (result == "failed")
        assert ("promptAcceptance" in controls["confirm-delivery"]["onclick"]) == (
            result == "pending"
        )


def test_gui_manual_result_invalidates_old_snapshot_and_reaches_cli(
    tmp_path, monkeypatch, capsys
):
    service = make_git_delivery_service(tmp_path)
    app = service.orchestrator
    app.request_path.write_text("Change app.py.", encoding="utf-8")
    app.plan_path.write_text("Implement the app.py change.", encoding="utf-8")
    app.state.update(
        acceptance_criteria=[CRITERION],
        tasks=[{"id": "T1", "status": "completed", "target_files": ["app.py"]}],
    )
    app.record_implementation_approval("approve")
    code, pending = post_action(service, "/api/deliver", {"confirm": False})
    assert code == 200
    snapshot_id = pending["delivery_snapshot_id"]
    assert pending["git"]["acceptance"][0]["status"] == "pending"
    manual = {
        "action": "verify",
        "snapshot_id": snapshot_id,
        "criterion_ids": ["AC-01"],
        "status": "passed",
        "reason": "The status displayed in the local dashboard.",
    }
    assert post_action(service, "/api/acceptance", manual)[0] == 200
    assert post_action(service, "/api/acceptance", manual)[0] == 409

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "AgentOrchestrator", lambda workspace: app)
    monkeypatch.setattr(
        "sys.argv", ["orchestrator.py", "deliver", "--confirm", snapshot_id]
    )
    assert cli.main() == 1
    capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "deliver"])
    assert cli.main() == 0
    output = capsys.readouterr().out
    preview, _ = json.JSONDecoder().raw_decode(output[output.index("{\n") :])
    assert preview["acceptance"][0]["status"] == "passed"
    assert preview["acceptance"][0]["evidence"] == manual["reason"]


@pytest.mark.parametrize(
    ("kind", "resume_state"),
    [("approve_plan", "IMPLEMENTING"), ("review", "DEVELOPING_PLAN")],
)
def test_cli_previews_proposed_criteria_and_records_plan_feedback(
    tmp_path, monkeypatch, capsys, kind, resume_state
):
    service = make_git_delivery_service(tmp_path)
    app = service.orchestrator
    app.state.update(
        state="WAITING_FOR_OWNER",
        human_review_source="Architect",
        human_review_kind=kind,
        resume_state=resume_state,
        pass_state="IMPLEMENTING",
        plan_requires_owner_approval=True,
        acceptance_criteria=[CRITERION],
    )
    app.save_state()
    page = render_page(service.status())
    assert "提議的驗收項目" in page
    assert "review-plan-feedback" in page

    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "AgentOrchestrator", lambda workspace: app)
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "review", "preview-plan"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == [CRITERION]
    assert app.state["state"] == "WAITING_FOR_OWNER"
    feedback = "AC-01 should cover both status messages."
    monkeypatch.setattr(
        "sys.argv", ["orchestrator.py", "review", "revise", "--reason", feedback]
    )
    cli.main()
    assert app.state["state"] == "DEVELOPING_PLAN"
    assert feedback in app.state["pending_plan_feedback"]
