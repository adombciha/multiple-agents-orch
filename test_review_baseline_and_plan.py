"""Check review provenance and plan coverage without calling real models."""

import json
from unittest.mock import Mock

import pytest

from orchestrator.core.state import AgentOrchestrator
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config
from test_workflow_e2e import git


@pytest.mark.parametrize("committed", [False, True])
def test_review_separates_real_task_changes_from_later_target_commits(
    tmp_path, monkeypatch, committed
):
    git(tmp_path, "init", "-b", "master")
    git(tmp_path, "config", "user.name", "Offline Test")
    git(tmp_path, "config", "user.email", "offline@example.com")
    (tmp_path / ".gitignore").write_text(".ai-company*\nbrain.db*\n")
    (tmp_path / "guard.py").write_text("POLICY = 'old'\n")
    (tmp_path / "answer.py").write_text("ANSWER = 40\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "shared baseline")
    baseline = git(tmp_path, "rev-parse", "HEAD")
    app = AgentOrchestrator(tmp_path)
    app.init_project()
    worktree = app.ai_dir / "worktree"
    git(tmp_path, "worktree", "add", "-b", "ai-feature-branch", str(worktree))
    (tmp_path / "guard.py").write_text("POLICY = 'prohibit premium models'\n")
    (tmp_path / "test_policy.py").write_text("assert True\n")
    git(tmp_path, "add", "guard.py", "test_policy.py")
    git(tmp_path, "commit", "-m", "later target safeguards")
    (worktree / "answer.py").write_text("ANSWER = 42\n")
    git(worktree, "add", "answer.py")
    if committed:
        git(worktree, "commit", "-m", "task change")
    (worktree / "test_answer.py").write_text("assert 20 + 22 == 42\n")
    heads = (git(tmp_path, "rev-parse", "HEAD"), git(worktree, "rev-parse", "HEAD"))
    status = git(worktree, "status", "--short")
    app.requirements_path.write_text("Update answer.py and add test_answer.py only")
    app.plan_path.write_text("Implement the requested files")
    app.test_results_path.write_text("Tests passed in feature worktree")
    monkeypatch.setattr(app, "consult_specialists", Mock(return_value=""))
    call = Mock(return_value='{"status":"APPROVED","feedback":[]}')
    monkeypatch.setattr(app, "call_agent", call)
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        Mock(side_effect=AssertionError("Unexpected network")),
    )

    app.step_reviewing_code()

    prompt = call.call_args.args[1]
    assert f"Shared Git baseline: {baseline}" in prompt
    task_diff = prompt.split("Task Git Diff (since shared baseline):\n", 1)[1].split(
        "Git Status", 1
    )[0]
    assert "+ANSWER = 42" in task_diff
    assert "guard.py" not in task_diff
    assert "test_policy.py" not in task_diff
    assert "assert 20 + 22 == 42" in prompt
    drift = prompt.split("Target branch changes since shared baseline", 1)[1]
    assert "guard.py" in drift and "test_policy.py" in drift
    assert "not task-authored edits or deletions" in drift
    assert "do not verify integration" in drift
    assert app.state["state"] == "WAITING_FOR_DELIVERY"
    assert heads == (
        git(tmp_path, "rev-parse", "HEAD"),
        git(worktree, "rev-parse", "HEAD"),
    )
    assert status == git(worktree, "status", "--short")


@pytest.mark.parametrize("failed_step", ["merge-base", "diff", "untracked"])
def test_git_evidence_failure_pauses_without_requesting_review(
    initialized_orchestrator, monkeypatch, failed_step
):
    app = initialized_orchestrator
    app.has_git = True
    app.requirements_path.write_text("requirements")
    app.plan_path.write_text("plan")
    app.test_results_path.write_text("tests passed")

    def command(args, **kwargs):
        if args[1] == "merge-base":
            return (1, "Git failed") if failed_step == "merge-base" else (0, "baseline")
        if args[1] == "status":
            return 0, "stdout:\n?? new.py\nstderr:\n"
        if "--no-index" in args:
            return (
                (2, "Git failed") if failed_step == "untracked" else (1, "new content")
            )
        return (1, "Git failed") if failed_step == "diff" else (0, "diff")

    monkeypatch.setattr(app, "run_command", command)
    call = Mock()
    monkeypatch.setattr(app, "call_agent", call)
    app.step_reviewing_code()
    call.assert_not_called()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["resume_state"] == "REVIEWING_CODE"
    assert "Git review evidence failed" in app.state["human_review_details"]


@pytest.mark.parametrize("extra_targets", [[], ["outside.py"]])
@pytest.mark.parametrize("task_id", ["T1", ""])
def test_manager_combined_python_task_preserves_group_and_validates_coverage(
    initialized_orchestrator, monkeypatch, extra_targets, task_id
):
    app = initialized_orchestrator
    app.request_path.write_text("Only add live_answer.py and test_live_answer.py.")
    app.requirements_path.write_text(
        "Only allowed to add live_answer.py and test_live_answer.py. "
        "Revision feedback: combine both files into a single implementation task."
    )
    response = {
        "tasks": [
            {
                "id": task_id,
                "description": "Implement addition and its tests",
                "target_files": [
                    "live_answer.py",
                    "test_live_answer.py",
                    *extra_targets,
                ],
                "rd_level": "senior",
                "qa_level": "senior",
                "complexity": "routine",
            }
        ],
        "staffing": {"rd": {"senior": 1}, "qa": {"senior": 1}},
        "specialists": [],
    }
    call = Mock(return_value=json.dumps(response))
    monkeypatch.setattr(app, "call_manager", call)
    app.step_developing_plan()
    call.assert_called_once()
    if extra_targets or not task_id:
        assert app.state["state"] == "WAITING_FOR_OWNER"
        assert app.state["tasks"] == []
    else:
        assert app.state["state"] == "REVIEWING_PLAN"
        tasks = app.state["tasks"]
        assert [task["id"] for task in tasks] == ["T1"]
        assert [task["target_files"] for task in tasks] == [
            ["live_answer.py", "test_live_answer.py"],
        ]
        assert all(task["status"] == "pending" for task in tasks)
        assert all(
            task["rd_level"] == "senior" and task["qa_level"] == "senior"
            for task in tasks
        )
        assert all(task["output_contract"]["format"] == "file_blocks" for task in tasks)
        assert tasks[0]["description"] == response["tasks"][0]["description"]
        assert (
            "Related non-Markdown files may share one implementation task"
            in call.call_args.args[0]
        )
        assert "Implement changes only in" not in app.plan_path.read_text()


def test_combined_code_task_survives_replan_and_implementation(
    initialized_orchestrator, monkeypatch
):
    app = initialized_orchestrator
    files = ["live_answer.py", "test_live_answer.py"]
    description = (
        "Inspect existing implementation first, then implement both files and tests"
    )
    app.request_path.write_text("Only add live_answer.py and test_live_answer.py.")
    app.requirements_path.write_text(
        "Only allowed to add live_answer.py and test_live_answer.py. Combine both into one task."
    )
    app.state["plan_revisions"] = 1
    app.state["pending_plan_feedback"] = (
        "Combine both files into one implementation task"
    )
    monkeypatch.setattr(
        app,
        "call_manager",
        Mock(
            return_value=json.dumps(
                {
                    "tasks": [
                        {
                            "id": "COMBINED",
                            "description": description,
                            "target_files": files,
                            "rd_level": "senior",
                            "qa_level": "senior",
                        }
                    ],
                    "staffing": {"rd": {"senior": 1}, "qa": {"senior": 1}},
                    "specialists": [],
                }
            )
        ),
    )
    app.step_developing_plan()
    assert app.state["state"] == "REVIEWING_PLAN"
    assert app.state["tasks"][0]["description"] == description
    app.state["state"] = "IMPLEMENTING"
    call = Mock(
        return_value=(
            "[FILE_START: live_answer.py]\ndef add(a, b):\n    return a + b\n[FILE_END: live_answer.py]\n"
            "[FILE_START: test_live_answer.py]\nfrom live_answer import add\ndef test_add():\n    assert add(20, 22) == 42\n[FILE_END: test_live_answer.py]"
        )
    )
    monkeypatch.setattr(app, "call_agent", call)
    app.step_implementing()
    call.assert_called_once()
    assert app.state["state"] == "TESTING"
    assert len(app.state["tasks"]) == 1
    assert app.state["tasks"][0]["id"] == "COMBINED"
    assert app.state["tasks"][0]["status"] == "completed"
    assert all((app.workspace / path).is_file() for path in files)


@pytest.mark.parametrize(
    "prefix",
    [
        "Inspect existing implementation first",
        "Review current behavior",
        "檢查既有實作",
        "驗證現況",
    ],
)
def test_plan_preserves_inspection_led_implementation_and_review_notes(
    initialized_orchestrator, monkeypatch, prefix
):
    app = initialized_orchestrator
    request = (
        "只允許修改 orchestrator/gui.py 與 test_gui_refresh.py。\n"
        "開始修改前列出影響、風險，經 owner 核准後實作。\n"
        "更新失敗時保留上次時間與交付確認；模擬 DOM 不代表真實瀏覽器測試。"
    )
    app.request_path.write_text(request)
    app.requirements_path.write_text(request)
    app.state["pending_plan_feedback"] = (
        "Include invalid-response, HTTP failure and recovery cases."
    )
    notes = {
        "current_state": [
            "Inspection is pending; inspect existing refresh before editing."
        ],
        "impact": ["Only GUI refresh and its offline regression tests."],
        "expected_changes": [
            "Timestamp updates only after validated state is applied."
        ],
        "risks": ["Keep task input, focus, reports and delivery confirmation intact."],
        "verification": [
            ".venv/bin/python -m pytest -q test_gui_refresh.py",
            ".venv/bin/python -m pytest -q -p no:cacheprovider",
            "Cover network/HTTP/invalid-response errors, initial state and recovery; simulated DOM only.",
        ],
    }
    task = {
        "id": "task-1",
        "description": prefix + "; enhance refresh and add regression tests.",
        "target_files": ["orchestrator/gui.py", "test_gui_refresh.py"],
        "rd_level": "middle",
        "qa_level": "middle",
        "complexity": "moderate",
    }
    manager = Mock(
        return_value=json.dumps(
            {
                "tasks": [task],
                "staffing": {"rd": {"middle": 1}, "qa": {"middle": 1}},
                "specialists": [],
                "plan_notes": notes,
            }
        )
    )
    monkeypatch.setattr(app, "call_manager", manager)

    app.step_developing_plan()

    assert app.state["state"] == "REVIEWING_PLAN"
    assert app.state["tasks"][0]["description"] == task["description"]
    plan = app.plan_path.read_text()
    assert request in plan
    for items in notes.values():
        assert all(item in plan for item in items)
    assert "wait for explicit owner approval" in plan
    assert "HTTP failure and recovery" in manager.call_args.args[0]
    assert "plan_notes" in manager.call_args.args[0]
    assert app.state["plan_requires_owner_approval"] is True
    monkeypatch.setattr(app, "consult_specialists", Mock(return_value=""))
    monkeypatch.setattr(
        app, "call_agent", Mock(return_value='{"status":"APPROVED","feedback":[]}')
    )

    app.step_reviewing_plan()

    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["resume_state"] == "IMPLEMENTING"
    assert app.state["pass_state"] == "IMPLEMENTING"
    assert all(t["status"] == "pending" for t in app.state["tasks"])
    from orchestrator.main import apply_approve

    assert apply_approve(app)
    assert app.state["state"] == "IMPLEMENTING"


@pytest.mark.parametrize("targets", [None, [], ["../outside.py"], ["/tmp/outside.py"]])
def test_plan_rejects_unscoped_tasks_without_description_heuristics(
    initialized_orchestrator, monkeypatch, targets
):
    app = initialized_orchestrator
    app.requirements_path.write_text("Implement the requested feature.")
    task = {"id": "T1", "description": "Inspect requirements and implement feature"}
    if targets is not None:
        task["target_files"] = targets
    monkeypatch.setattr(
        app, "call_manager", Mock(return_value=json.dumps({"tasks": [task]}))
    )

    app.step_developing_plan()

    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert not app.state["tasks"]
