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
def test_manager_combined_python_task_is_split_before_coverage_validation(
    initialized_orchestrator, monkeypatch, extra_targets, task_id
):
    app = initialized_orchestrator
    app.request_path.write_text("Only add live_answer.py and test_live_answer.py.")
    app.requirements_path.write_text(
        "Only allowed to add live_answer.py and test_live_answer.py."
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
        assert [task["id"] for task in tasks] == ["T1-1", "T1-2"]
        assert [task["target_files"] for task in tasks] == [
            ["live_answer.py"],
            ["test_live_answer.py"],
        ]
        assert all(task["status"] == "pending" for task in tasks)
        assert all(
            task["rd_level"] == "senior" and task["qa_level"] == "senior"
            for task in tasks
        )
        assert all(task["output_contract"]["format"] == "file_blocks" for task in tasks)
