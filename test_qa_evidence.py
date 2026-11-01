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


@pytest.mark.parametrize("text", ["x" * 100000, "測試失敗🙂" * 20000])
def test_qa_excerpt_bounds_utf8_and_marks_omissions(text):
    from orchestrator.roles.qa import qa_excerpt

    excerpt = qa_excerpt(text, 600)
    assert len(excerpt.encode("utf-8")) <= 600
    assert "TRUNCATED" in excerpt
    assert "�" not in excerpt
    assert qa_excerpt("short evidence", 600) == "short evidence"


def test_qa_test_excerpt_retains_root_error_and_failure_summary():
    from orchestrator.roles.qa import qa_test_excerpt

    output = (
        "." * 5000
        + "\n"
        + (
            "Traceback\nE   OfflineViolation: subprocess guard rejected git init\n"
            * 9000
        )
        + "FAILED test_workflow_acceptance.py::test_scope\n"
        + "145 failed, 725 passed, 48 errors\n"
    )
    excerpt = qa_test_excerpt(output)
    assert len(excerpt.encode("utf-8")) <= 4500
    assert "E   OfflineViolation: subprocess guard rejected git init" in excerpt
    assert "FAILED test_workflow_acceptance.py::test_scope" in excerpt
    assert "145 failed, 725 passed, 48 errors" in excerpt
    assert "test_results.txt" in excerpt


def test_large_qa_evidence_fits_local_model_and_reaches_rd(qa_app, monkeypatch):
    from orchestrator.core.backends import ollama_request_options

    app = qa_app
    output = (
        "." * 5000
        + "\n"
        + "E   OfflineViolation: subprocess guard rejected git init\n" * 9000
        + "FAILED app.py::test_scope\n145 failed, 725 passed, 48 errors\n"
    )
    app.has_git = True
    app.request_path.write_text(REQUEST + "\n" + "需求" * 100000)
    app.state["tasks"][0]["description"] = "驗證" * 100000
    monkeypatch.setattr(
        app, "read_agent_context", Mock(return_value={"notes": "背景" * 100000})
    )
    monkeypatch.setattr(
        app, "run_command", Mock(side_effect=[(1, output), (0, "+change\n" * 100000)])
    )
    app.config["ollama_num_ctx"] = 8192
    app.state["code_revisions"] = 0
    calls = []

    def respond(role, prompt, system, images, validator, schema):
        calls.append(prompt)
        options, _ = ollama_request_options(
            app,
            "local",
            [
                {"content": system},
                {"content": prompt},
            ],
        )
        assert options["num_predict"] >= 1500
        assert "Test Exit Code: 1" in prompt
        assert "OfflineViolation" in prompt
        assert "145 failed, 725 passed, 48 errors" in prompt
        assert "TRUNCATED" in prompt
        false_pass = json.dumps(
            {
                "status": "PASSED",
                "summary": "Everything passed",
                "findings": [],
                "dependency_install_needed": None,
                "test_command_suggestion": None,
            }
        )
        with pytest.raises(RuntimeError, match="contradicts test exit code"):
            validator(false_pass)
        return "QA_STATUS: FAILED\nObserved subprocess guard failures."

    monkeypatch.setattr(app, "call_agent", respond)
    app.step_testing()
    assert len(calls) == 1
    assert output in app.test_results_path.read_text()
    assert app.state["state"] == "IMPLEMENTING"
    repair = app.state["tasks"][-1]
    assert "OfflineViolation" in repair["description"]
    assert "145 failed" in repair["description"]
    assert repair["target_files"] == ["app.py"]


def test_qa_correction_is_bounded_and_does_not_turn_missing_evidence_into_pass(
    qa_app, monkeypatch
):
    from orchestrator.core.backends import ollama_request_options

    call = Mock(return_value="無效回覆" * 100000)
    monkeypatch.setattr(qa_app, "call_agent", call)
    qa_app.config["ollama_num_ctx"] = 8192
    qa_app.step_testing()
    assert call.call_count == 2
    for invocation in call.call_args_list:
        _, prompt, system, *_ = invocation.args
        options, _ = ollama_request_options(
            qa_app,
            "local",
            [
                {"content": system},
                {"content": prompt},
            ],
        )
        assert options["num_predict"] >= 1500
    assert "TRUNCATED" in call.call_args_list[1].args[1]
    assert qa_app.state["state"] == "WAITING_FOR_OWNER"
    assert qa_app.state["human_review_source"] == "QA_RESPONSE"


ISOLATED_CHILD_FAILURE = """___ test_offline_child ___
result = subprocess.run([sys.executable, '-I', '-S', '-c', program])
E       AssertionError: Traceback (most recent call last):
E         File "<string>", line 1, in <module>
E       ModuleNotFoundError: No module named 'pytest'
E       CompletedProcess(args=['/usr/bin/python3', '-I', '-S', '-c', program])
FAILED app.py::test_offline_child
1 failed, 719 passed
"""


@pytest.mark.parametrize(
    "output,expected",
    [
        (ISOLATED_CHILD_FAILURE, False),
        (ISOLATED_CHILD_FAILURE.replace("'-I', '-S', ", ""), False),
        (ISOLATED_CHILD_FAILURE.replace("'pytest'", "'requests'"), False),
        ("/usr/bin/python3: No module named pytest", True),
        (
            "ImportError while importing test module app.py\nModuleNotFoundError: No module named 'requests'",
            True,
        ),
        ("SKIPPED [3] app.py: ModuleNotFoundError: No module named 'playwright'", True),
        ("bash: pytest: command not found", True),
        ("719 passed", False),
        (
            ISOLATED_CHILD_FAILURE
            + "\n___ test_other_dependency ___\nModuleNotFoundError: No module named 'playwright'",
            True,
        ),
    ],
)
def test_environment_dependency_distinguishes_isolated_child_failures(output, expected):
    from orchestrator.roles.qa import qa_environment_dependency

    assert qa_environment_dependency(output) is expected


def test_isolated_child_import_failure_returns_to_rd_without_install_pause(
    qa_app, monkeypatch
):
    app = qa_app
    monkeypatch.setattr(
        app, "run_command", Mock(return_value=(1, ISOLATED_CHILD_FAILURE))
    )
    call = Mock(
        return_value=json.dumps(
            {
                "status": "FAILED",
                "summary": "The isolated child cannot import pytest.",
                "findings": [
                    {
                        "kind": "environment",
                        "file": "n/a",
                        "requirement": "",
                        "evidence": "No module named 'pytest'",
                        "fix": "Install pytest",
                    }
                ],
                "dependency_install_needed": "pip install pytest",
                "test_command_suggestion": None,
            }
        )
    )
    monkeypatch.setattr(app, "call_agent", call)
    app.step_testing()
    assert app.state["state"] == "IMPLEMENTING"
    assert app.state["code_revisions"] == 1
    repair = app.state["tasks"][-1]
    assert "-S" in repair["description"]
    assert "No module named 'pytest'" in repair["description"]
    assert "pip install pytest" not in repair["description"]
    assert repair["target_files"] == ["app.py"]
    assert (
        "Environment dependency supported by runtime evidence: False"
        in call.call_args.args[1]
    )
    assert ISOLATED_CHILD_FAILURE in app.test_results_path.read_text()


def test_real_environment_import_failure_still_pauses_for_owner(qa_app, monkeypatch):
    app = qa_app
    monkeypatch.setattr(
        app,
        "run_command",
        Mock(
            return_value=(
                1,
                "ImportError while importing test module app.py\nModuleNotFoundError: No module named 'requests'",
            )
        ),
    )
    monkeypatch.setattr(
        app,
        "call_agent",
        Mock(
            return_value=json.dumps(
                {
                    "status": "FAILED",
                    "summary": "Required test dependency is missing.",
                    "findings": [],
                    "dependency_install_needed": "python -m pip install requests",
                    "test_command_suggestion": None,
                }
            )
        ),
    )
    app.step_testing()
    assert app.state["state"] == "WAITING_FOR_OWNER"
    assert app.state["human_review_source"] == "QA"
    assert "pip install requests" in app.state["human_review_details"]
    assert app.state["code_revisions"] == 0
    assert len(app.state["tasks"]) == 1
    app.escalate_developer_backend.assert_not_called()
