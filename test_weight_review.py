"""Verify evidence-backed staff reviews without invoking providers."""

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock
import uuid

import pytest

from orchestrator.core import backends, scoring, telemetry
from orchestrator.core.cloud_routing import ModelDiscoveryError
from orchestrator.gui import GuiService
from orchestrator.roles.manager import is_weight_adjustment_response
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


@pytest.fixture
def review_app(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    app.state["run_id"] = uuid.uuid4().hex
    monkeypatch.setattr(telemetry, "_token_count", lambda *args: (None, "offline"))
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        Mock(side_effect=AssertionError("Unexpected network")),
    )
    app.final_report_path.write_text("# Existing final report\n", encoding="utf-8")
    return app


def record(app, *, role="developer_junior", backend="ollama", error=None):
    telemetry.record_call(
        app,
        role=role,
        backend=backend,
        model="offline-model",
        prompt="private prompt",
        system_prompt=None,
        output="private output",
        elapsed_seconds=0.01,
        error=error,
    )


def adjustment(**changes):
    return {
        "role": "rd",
        "level": "junior",
        "weight": 2.9,
        "reason": "The backend completed the recorded call.",
        "evidence_ids": ["call:1"],
        **changes,
    }


def respond(app, monkeypatch, entries):
    call = Mock(return_value=json.dumps({"adjustments": entries}))
    monkeypatch.setattr(app.manager, "call_manager", call)
    return call


def test_review_commits_bounded_weights_audit_and_report(review_app, monkeypatch):
    app = review_app
    app.config["worker_weights"]["rd"]["junior"] = 1.0
    original = deepcopy(app.config)
    record(app)
    call = respond(app, monkeypatch, [adjustment()])

    app.manager._review_worker_weights("Untrusted historical summary")

    persisted = json.loads(app.config_path.read_text())
    assert persisted == app.config
    assert app.config["worker_weights"]["rd"]["junior"] == 1.3
    audit = persisted.pop("last_worker_weight_review")
    original["worker_weights"]["rd"]["junior"] = 1.3
    assert persisted == original
    assert audit["run_id"] == app.state["run_id"]
    change = audit["adjustments"][0]
    assert (change["original"], change["new"]) == (1.0, 1.3)
    assert change["reason"] == adjustment()["reason"]
    assert change["evidence"][0]["completed"] is True
    report = app.final_report_path.read_text()
    assert "# Existing final report" in report
    assert "Staff Execution Reliability Review" in report
    assert "quality verdict" in report
    prompt = call.call_args.args[0]
    assert "before response validation" in prompt
    assert "Untrusted historical summary" not in prompt
    assert "private prompt" not in prompt


@pytest.mark.parametrize(
    "entry",
    [
        {key: value for key, value in adjustment().items() if key != "reason"},
        adjustment(reason=" \n "),
        adjustment(reason=False),
        adjustment(evidence_ids=[]),
        adjustment(evidence_ids=["call:1", "call:1"]),
        adjustment(evidence_ids=[False]),
        adjustment(evidence_ids=["call:999"]),
        adjustment(weight=float("nan")),
        adjustment(weight=float("inf")),
        adjustment(weight=-float("inf")),
        adjustment(weight=True),
        adjustment(weight="1.2"),
        adjustment(weight=None),
        adjustment(weight=0.1),
        adjustment(weight=3.1),
        adjustment(role="vendor"),
        adjustment(role=[]),
        adjustment(level="principal"),
        adjustment(extra="field"),
        adjustment(role="qa"),
        adjustment(level="senior"),
    ],
)
def test_invalid_adjustment_rejects_entire_batch(review_app, monkeypatch, entry):
    app = review_app
    record(app)
    record(app, role="qa_senior")
    before = deepcopy(app.config)
    disk = app.config_path.read_bytes()
    respond(
        app,
        monkeypatch,
        [
            adjustment(role="qa", level="senior", weight=1.6, evidence_ids=["call:2"]),
            entry,
        ],
    )

    app.manager._review_worker_weights("summary")

    assert app.config == before
    assert app.config_path.read_bytes() == disk
    assert "last_worker_weight_review" not in app.config


@pytest.mark.parametrize(
    "response", ["[]", "null", "{}", "false", '{"adjustments": {}}', "not json"]
)
def test_parser_rejects_invalid_top_level(response):
    assert is_weight_adjustment_response(response) is False


def test_parser_rejects_duplicates_even_with_legal_values():
    assert not is_weight_adjustment_response(
        json.dumps({"adjustments": [adjustment(), adjustment()]})
    )
    assert is_weight_adjustment_response('{"adjustments": []}')


@pytest.mark.parametrize("run_id", [None, "legacy-hash-1234", uuid.uuid1().hex])
def test_missing_or_legacy_identity_cannot_authorize_review(
    review_app, monkeypatch, run_id
):
    app = review_app
    app.state["run_id"] = run_id
    record(app)
    call = respond(app, monkeypatch, [adjustment()])
    app.manager._review_worker_weights("summary")
    assert telemetry.current_run_id(app) is None
    assert telemetry.worker_call_evidence(app) == []
    call.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        "old_run",
        "vendor",
        "unknown_role",
        "quota",
        "discovery",
        "cancelled",
        "disabled",
        "corrupt",
    ],
)
def test_no_eligible_evidence_skips_model_and_config(review_app, monkeypatch, case):
    app = review_app
    if case == "old_run":
        record(app)
        app.state["run_id"] = uuid.uuid4().hex
    elif case == "vendor":
        record(app, backend="codex")
    elif case == "unknown_role":
        record(app, role="developer")
    elif case == "quota":
        record(app, error=RuntimeError("quota timeout"))
    elif case == "discovery":
        record(app, error=ModelDiscoveryError("codex", "timeout", "timed out"))
    elif case == "cancelled":
        record(app, error=RuntimeError("cancelled"))
    elif case == "disabled":
        app.config["telemetry_enabled"] = False
        record(app)
    else:
        record(app)
        with (app.ai_dir / "llm_usage.jsonl").open("a") as handle:
            handle.write("not json\n")
    before = deepcopy(app.config)
    disk = app.config_path.read_bytes()
    call = respond(app, monkeypatch, [adjustment()])
    app.manager._review_worker_weights("summary")
    call.assert_not_called()
    assert app.config == before
    assert app.config_path.read_bytes() == disk


@pytest.mark.parametrize(
    "field,value",
    [("success", 1), ("role", []), ("error_category", []), ("backend", "agy")],
)
def test_malformed_evidence_is_not_eligible(review_app, field, value):
    app = review_app
    record(app)
    path = app.ai_dir / "llm_usage.jsonl"
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data) + "\n")
    assert telemetry.worker_call_evidence(app) == []


def test_failed_timeout_can_support_decrease_but_not_increase(review_app, monkeypatch):
    app = review_app
    app.config["worker_weights"]["rd"]["junior"] = 1.0
    record(app, error=TimeoutError("timed out"))
    respond(app, monkeypatch, [adjustment()])
    before = deepcopy(app.config)
    app.manager._review_worker_weights("")
    assert app.config == before
    respond(app, monkeypatch, [adjustment(weight=0.2, reason="Backend timed out.")])
    app.manager._review_worker_weights("")
    assert app.config["worker_weights"]["rd"]["junior"] == 0.7


def test_completed_call_cannot_support_decrease(review_app, monkeypatch):
    app = review_app
    record(app)
    before = deepcopy(app.config)
    respond(app, monkeypatch, [adjustment(weight=0.2)])
    app.manager._review_worker_weights("")
    assert app.config == before


@pytest.mark.parametrize("delta", [True, -0.1, "0.3", float("nan"), float("inf")])
def test_invalid_delta_skips_model(review_app, monkeypatch, delta):
    app = review_app
    app.config["weight_adjustment_max_delta"] = delta
    record(app)
    call = respond(app, monkeypatch, [adjustment()])
    app.manager._review_worker_weights("")
    call.assert_not_called()


@pytest.mark.parametrize("current", [True, "1.0", float("nan"), float("inf"), 3.1])
def test_invalid_current_weight_rejects_review(review_app, monkeypatch, current):
    app = review_app
    app.config["worker_weights"]["rd"]["junior"] = current
    record(app)
    disk = app.config_path.read_bytes()
    respond(app, monkeypatch, [adjustment()])
    app.manager._review_worker_weights("")
    assert app.config_path.read_bytes() == disk
    assert "last_worker_weight_review" not in app.config


@pytest.mark.parametrize(
    "current,delta,proposed,expected",
    [
        (2.9, 0.3, 3.0, 3.0),
        (1.23456, 0.00001, 3.0, 1.23457),
        (1.23456, 0, 3.0, 1.23456),
    ],
)
def test_rounding_keeps_delta_and_range_bounds(
    review_app, monkeypatch, current, delta, proposed, expected
):
    app = review_app
    app.config["worker_weights"]["rd"]["junior"] = current
    app.config["weight_adjustment_max_delta"] = delta
    record(app)
    respond(app, monkeypatch, [adjustment(weight=proposed)])
    app.manager._review_worker_weights("")
    actual = app.config["worker_weights"]["rd"]["junior"]
    assert actual == pytest.approx(expected)
    assert abs(actual - current) <= delta + 1e-12


@pytest.mark.parametrize("stage", ["create", "write", "fsync", "replace", "serialize"])
def test_atomic_failure_preserves_disk_and_memory(review_app, monkeypatch, stage):
    app = review_app
    record(app)
    respond(app, monkeypatch, [adjustment()])
    if stage == "serialize":
        app.config["unrelated"] = float("nan")
    before = deepcopy(app.config)
    disk = app.config_path.read_bytes()
    failure = Mock(side_effect=OSError("disk failure"))
    if stage == "create":
        monkeypatch.setattr(scoring.tempfile, "NamedTemporaryFile", failure)
    elif stage == "write":
        original = scoring.tempfile.NamedTemporaryFile

        def broken_file(*args, **kwargs):
            handle = original(*args, **kwargs)
            handle.write = failure
            return handle

        monkeypatch.setattr(scoring.tempfile, "NamedTemporaryFile", broken_file)
    elif stage == "fsync":
        monkeypatch.setattr(scoring.os, "fsync", failure)
    elif stage == "replace":
        monkeypatch.setattr(scoring.os, "replace", failure)
    app.manager._review_worker_weights("")
    assert app.config == before
    assert app.config_path.read_bytes() == disk
    assert "last_worker_weight_review" not in app.config
    assert not list(app.ai_dir.glob("tmp*"))


@pytest.mark.parametrize("stage", ["create", "partial_write", "flush", "replace"])
def test_saved_audit_retries_report_without_second_model_or_adjustment(
    review_app, monkeypatch, stage
):
    app = review_app
    record(app)
    call = respond(app, monkeypatch, [adjustment()])
    original_report = app.final_report_path.read_bytes()
    original_writer = app.manager._write_weight_review_report
    monkeypatch.setattr(app.manager, "_write_weight_review_report", Mock())
    app.manager._review_worker_weights("")
    committed = deepcopy(app.config)
    app.save_state()
    app.load_config_and_state()
    monkeypatch.setattr(app.manager, "_write_weight_review_report", original_writer)
    with monkeypatch.context() as patch:
        failure = Mock(side_effect=OSError("report temporarily unavailable"))
        if stage == "create":
            patch.setattr(scoring.tempfile, "NamedTemporaryFile", failure)
        elif stage == "replace":
            patch.setattr(scoring.os, "replace", failure)
        else:
            original_file = scoring.tempfile.NamedTemporaryFile

            def broken_file(*args, **kwargs):
                handle = original_file(*args, **kwargs)
                if stage == "partial_write":
                    write = handle.write

                    def partial_write(text):
                        write(text[: len(text) // 2])
                        raise OSError("partial report write")

                    handle.write = partial_write
                else:
                    handle.flush = failure
                return handle

            patch.setattr(scoring.tempfile, "NamedTemporaryFile", broken_file)
        app.manager._review_worker_weights("")
        assert app.final_report_path.read_bytes() == original_report
        assert app.config == committed
    app.manager._review_worker_weights("")
    app.manager._review_worker_weights("")
    assert app.config == committed
    call.assert_called_once()
    report = app.final_report_path.read_text()
    assert report.count("## Staff Execution Reliability Review") == 1
    assert report.rstrip().endswith("```")


def test_cleanup_failure_does_not_hide_committed_config(review_app, monkeypatch):
    app = review_app
    staged = deepcopy(app.config)
    staged["worker_weights"]["rd"]["junior"] = 1.2
    monkeypatch.setattr(
        Path, "unlink", Mock(side_effect=OSError("cleanup unavailable"))
    )
    scoring._save_config(app, staged)
    assert app.config == json.loads(app.config_path.read_text()) == staged


def test_audit_retains_only_latest_run(review_app, monkeypatch):
    app = review_app
    first = app.state["run_id"]
    record(app)
    respond(app, monkeypatch, [adjustment()])
    app.manager._review_worker_weights("")
    app.state["run_id"] = uuid.uuid4().hex
    record(app)
    respond(app, monkeypatch, [adjustment(evidence_ids=["call:2"])])
    app.manager._review_worker_weights("")
    audit = app.config["last_worker_weight_review"]
    assert audit["run_id"] != first
    assert len(audit["adjustments"]) == 1
    assert "last_worker_weight_review" not in json.dumps(audit)


@pytest.mark.parametrize("kind", ["staff", "vendor"])
def test_existing_scoring_events_preserve_memory_on_replace_failure(
    review_app, monkeypatch, kind
):
    app = review_app
    original = deepcopy(app.config)
    disk = app.config_path.read_bytes()
    monkeypatch.setattr(
        scoring.os, "replace", Mock(side_effect=OSError("disk failure"))
    )
    with pytest.raises(OSError):
        if kind == "staff":
            scoring.record_staff_event(app, "rd", "junior", 1.0)
        else:
            scoring.record_vendor_event(app, "codex", "offline-model", -1.0)
    assert app.config == original
    assert app.config_path.read_bytes() == disk


def test_call_completion_is_recorded_before_response_validator(review_app, monkeypatch):
    app = review_app
    app.config["role_model_routes"]["developer_junior"] = [["ollama", "offline-model"]]
    app.config["ollama_router_enabled"] = False
    monkeypatch.setattr(
        backends, "discover_ollama_chat_models", Mock(return_value={"offline-model"})
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    monkeypatch.setattr(
        app,
        "call_agent_ollama_fallback",
        lambda role, prompt, system_prompt, **kwargs: app._call_with_usage(
            role,
            "ollama",
            "offline-model",
            prompt,
            system_prompt,
            lambda: "invalid output",
        ),
    )
    with pytest.raises(RuntimeError, match="output contract"):
        app.call_role_model_routes(
            "developer_junior", "prompt", response_validator=lambda _: False
        )
    evidence = telemetry.worker_call_evidence(app)
    assert len(evidence) == 1
    assert evidence[0]["completed"] is True


def test_gui_new_runs_get_unique_persisted_uuid(review_app, monkeypatch):
    app = review_app
    service = GuiService(app.workspace)
    service.orchestrator = app
    monkeypatch.setattr(app, "get_windows_host_ip", Mock(return_value="127.0.0.1"))
    monkeypatch.setattr(service, "status", Mock(return_value={"state": "PLANNING"}))
    monkeypatch.setattr(service, "_start_worker", lambda prepare: (prepare() or True))
    service._new_task("offline task")
    first = telemetry.current_run_id(app)
    assert first
    assert json.loads(app.state_path.read_text())["run_id"] == first
    app.state["state"] = "FAILED"
    app.save_state()
    service._new_task("same offline task")
    assert telemetry.current_run_id(app) != first


def test_large_history_uses_bounded_current_run_sample(review_app, monkeypatch):
    app = review_app
    record(app)
    path = app.ai_dir / "llm_usage.jsonl"
    template = json.loads(path.read_text())
    rows = [
        {**template, "role": f"{prefix}_{level}"}
        for _ in range(100)
        for prefix in ("developer", "qa")
        for level in ("senior", "middle", "junior")
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    evidence = telemetry.worker_call_evidence(app)
    assert len(evidence) == 48
    assert len({item["id"] for item in evidence}) == 48
    assert all(int(item["id"].split(":")[1]) > 552 for item in evidence)
    cited = next(
        item for item in evidence if item["role"] == "rd" and item["level"] == "junior"
    )
    call = respond(app, monkeypatch, [adjustment(evidence_ids=[cited["id"]])])
    app.manager._review_worker_weights("")
    assert len(call.call_args.args[0]) < 18000
    assert "bounded sample" in call.call_args.args[0]
    assert len(path.read_text().splitlines()) == len(rows)
    assert app.config["last_worker_weight_review"]["adjustments"][0]["evidence"] == [
        cited
    ]


def test_empty_review_is_saved_once_without_weight_changes(review_app, monkeypatch):
    app = review_app
    record(app)
    before = deepcopy(app.config["worker_weights"])
    call = respond(app, monkeypatch, [])
    app.manager._review_worker_weights("")
    app.manager._review_worker_weights("")
    assert app.config["worker_weights"] == before
    assert app.config["last_worker_weight_review"]["adjustments"] == []
    assert '"adjustments": []' in app.final_report_path.read_text()
    call.assert_called_once()


def test_completed_delivery_can_restore_report_without_rerunning_work(
    review_app, monkeypatch
):
    app = review_app
    record(app)
    call = respond(app, monkeypatch, [adjustment()])
    app.manager._review_worker_weights("")
    committed = deepcopy(app.config)
    app.final_report_path.write_text("# Regenerated report\n")
    app.state["delivery"] = {"delivered": True, "report_done": True}
    app.manager.step_completed()
    assert app.config == committed
    assert "Staff Execution Reliability Review" in app.final_report_path.read_text()
    call.assert_called_once()


def test_contract_retry_records_each_backend_completion(review_app, monkeypatch):
    app = review_app
    app.state.update(state="IMPLEMENTING", active_task_id="T1")
    app.config["role_model_routes"]["developer_junior"] = [["ollama", "offline-model"]]
    app.config["ollama_router_enabled"] = False
    monkeypatch.setattr(
        backends, "discover_ollama_chat_models", Mock(return_value={"offline-model"})
    )
    monkeypatch.setattr(backends, "backend_available", Mock(return_value=True))
    outputs = iter(
        ["invalid output", "[FILE_START: app.py]\nVALUE = 1\n[FILE_END: app.py]"]
    )
    monkeypatch.setattr(
        app,
        "call_agent_ollama_fallback",
        lambda role, prompt, system_prompt, **kwargs: app._call_with_usage(
            role,
            "ollama",
            "offline-model",
            prompt,
            system_prompt,
            lambda: next(outputs),
        ),
    )
    response = app.call_role_model_routes(
        "developer_junior",
        "prompt",
        response_validator=lambda text: text.startswith("[FILE_START:"),
    )
    assert "VALUE = 1" in response
    evidence = telemetry.worker_call_evidence(app)
    assert len(evidence) == 2
    assert all(item["completed"] for item in evidence)
    assert app.state["task_developer_calls"]["T1"] == 2


@pytest.mark.parametrize("detail", ["timed out", "quota exhausted"])
def test_discovery_error_category_precedes_message_heuristics(detail):
    assert (
        telemetry._error_category(ModelDiscoveryError("codex", "timeout", detail))
        == "discovery"
    )
