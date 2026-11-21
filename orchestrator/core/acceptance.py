"""Owner-approved acceptance criteria and evidence for one workflow."""

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import time


class AcceptanceError(RuntimeError):
    """An acceptance action is invalid for the current workflow."""


def parse_criteria(raw, tasks: list[dict]) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not 1 <= len(raw) <= 30:
        raise ValueError("Provide 1 to 30 acceptance criteria.")
    task_ids = [task.get("id") if isinstance(task, dict) else None for task in tasks]
    if any(not isinstance(task, str) or not task.strip() for task in task_ids):
        raise ValueError(
            "Acceptance criteria require nonempty implementation task IDs."
        )
    known = set(task_ids)
    if len(known) != len(task_ids):
        raise ValueError("Acceptance criteria require unique implementation task IDs.")
    criteria = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError("Each acceptance criterion must be an object.")
        requirement, kind, check, ids = (
            item.get(key) for key in ("requirement", "kind", "check", "task_ids")
        )
        if (
            not isinstance(requirement, str)
            or not requirement.strip()
            or len(requirement) > 500
            or not isinstance(check, str)
            or not check.strip()
            or len(check) > 500
            or kind not in ("pytest", "manual")
            or not isinstance(ids, list)
            or not ids
            or any(not isinstance(task, str) or task not in known for task in ids)
        ):
            raise ValueError(
                "Each criterion needs a requirement, check, kind and known task IDs."
            )
        if kind == "pytest":
            path, separator, test = check.strip().partition("::")
            if (
                not separator
                or not test
                or not path.endswith(".py")
                or Path(path).is_absolute()
                or ".." in Path(path).parts
                or "\n" in check
                or "\r" in check
            ):
                raise ValueError(
                    "A pytest criterion must name an exact relative pytest node ID."
                )
        criteria.append(
            {
                "id": f"AC-{index:02d}",
                "requirement": requirement.strip(),
                "kind": kind,
                "check": check.strip(),
                "task_ids": list(dict.fromkeys(ids)),
            }
        )
    if {task for item in criteria for task in item["task_ids"]} != known:
        raise ValueError("Every implementation task must have an acceptance criterion.")
    return criteria


def plan_rows(state: dict) -> list[dict]:
    return deepcopy(state.get("acceptance_criteria", []))


def awaiting_plan_review(state: dict) -> bool:
    from orchestrator.core.status import owner_review_kind

    return (
        state.get("state") == "WAITING_FOR_OWNER"
        and state.get("human_review_source") in {"Architect", "Manager"}
        and state.get("pass_state") == "IMPLEMENTING"
        and owner_review_kind(state) in {"approve_plan", "review", "revise"}
    )


def criterion_targets(criteria: list[dict], tasks: list[dict]) -> dict[str, list[str]]:
    by_task = {task["id"]: task["target_files"] for task in tasks}
    return {
        item["id"]: sorted(
            {
                str(Path(path))
                for key in item["task_ids"]
                for path in by_task.get(key, [])
            }
        )
        for item in criteria
    }


def _approved(app) -> list[dict]:
    record = app.state.get("implementation_approval", {})
    criteria = record.get("acceptance_criteria", [])
    if not criteria and not plan_rows(app.state):
        return []
    if (
        not criteria
        or record.get("source") != "owner"
        or criteria != plan_rows(app.state)
        or not app.implementation_approval_evidence()["matches_current_file_scope"]
    ):
        raise AcceptanceError(
            "Acceptance criteria changed or lack current owner approval."
        )
    return criteria


def _revision(app) -> str:
    from orchestrator.core.state import delivery_snapshot_id

    root = app.agent_workspace()
    files = {}
    try:
        for relative in app.writable_implementation_file_scope():
            path = root / relative
            if path.exists():
                files[relative] = [
                    path.stat().st_mode,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ]
            else:
                files[relative] = None
    except OSError as error:
        raise AcceptanceError(
            f"Could not read acceptance target files: {error}"
        ) from error
    return delivery_snapshot_id(
        {
            "approval": app.state["implementation_approval"]["approval_id"],
            "files": files,
        }
    )


def preview_rows(app) -> tuple[str | None, list[dict]]:
    criteria = _approved(app)
    if not criteria:
        return None, []
    verification = _verification(app)
    revision = _revision(app)
    current = verification.get("revision") == revision
    records = _current_records(verification, revision)
    missing = (
        "Target files changed since verification."
        if verification and not current
        else "No matching verification evidence."
    )
    return revision, [
        {**item, **records.get(item["id"], {"status": "pending", "evidence": missing})}
        for item in criteria
    ]


def rows(app) -> list[dict]:
    return preview_rows(app)[1]


def qa_revision(app) -> str | None:
    return _revision(app) if _approved(app) else None


def _verification(app) -> dict:
    verification = app.state.get("acceptance_verification", {})
    if (
        verification.get("approval_id")
        == app.state["implementation_approval"]["approval_id"]
    ):
        return verification
    return {}


def _failed_records(records: dict) -> dict:
    return {
        key: {**record, "needs_recheck": True}
        for key, record in records.items()
        if record["status"] == "failed"
    }


def _current_records(verification: dict, revision: str) -> dict:
    records = verification.get("records", {})
    return (
        dict(records)
        if verification.get("revision") == revision
        else _failed_records(records)
    )


def _record(app, revision: str, records: dict) -> None:
    prior = app.state.get("acceptance_verification")
    if prior:
        app.state.setdefault("acceptance_history", []).append(prior)
    app.state["acceptance_verification"] = {
        "approval_id": app.state["implementation_approval"]["approval_id"],
        "revision": revision,
        "records": records,
    }


def record_qa(
    app, command: str, code: int, output: str, *, before: str | None = None
) -> None:
    criteria = _approved(app)
    if not criteria:
        return
    revision = _revision(app)
    records = _current_records(_verification(app), revision)
    if before is None or before != revision:
        _record(app, revision, _failed_records(records))
        return
    # Pytest's verbose output names passing tests. A suite exit code alone proves no item.
    lines = [
        re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in output.splitlines()
    ]
    for item in criteria:
        if item["kind"] != "pytest":
            continue
        check = re.escape(item["check"])
        failed = re.compile(
            rf"^(?:{check}\s+(?:FAILED|ERROR)\b|(?:FAILED|ERROR)\s+{check}(?:\s+-|$))"
        )
        passed = re.compile(rf"^{check}\s+PASSED(?:\s+\[\s*\d+%\])?$")
        failure = next((line for line in lines if failed.match(line)), None)
        success = next((line for line in lines if passed.fullmatch(line)), None)
        if code not in (126, 127) and (failure or (code in (0, 1) and success)):
            records[item["id"]] = {
                "status": "failed" if failure else "passed",
                "evidence": f"{command[:500]} (exit {code})\n{(failure or success)[:500]}",
            }
        elif records.get(item["id"], {}).get("status") == "failed":
            records[item["id"]] = {**records[item["id"]], "needs_recheck": True}
        else:
            records.pop(item["id"], None)
    _record(app, revision, records)


def _validate_snapshot(app, snapshot_id: str) -> dict:
    from orchestrator.core.state import delivery_snapshot_id

    preview = app.state.get("delivery_preview")
    if (
        not snapshot_id
        or not isinstance(preview, dict)
        or not app.state.get("delivery_preview_presented")
        or snapshot_id != delivery_snapshot_id(preview)
    ):
        raise AcceptanceError(
            "Show the current delivery preview before taking this action."
        )
    app.validate_delivery_preview(preview)
    return preview


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise AcceptanceError(f"Provide {label} using 1 to 2000 characters.")
    return value.strip()


def record_manual(
    app, snapshot_id: str, criterion_id: str, status: str, evidence: str
) -> None:
    preview = _validate_snapshot(app, snapshot_id)
    item = next(
        (row for row in preview.get("acceptance", []) if row["id"] == criterion_id),
        None,
    )
    if not item or item["kind"] != "manual" or status not in ("passed", "failed"):
        raise AcceptanceError("Select a manual criterion and record a pass or failure.")
    evidence = _text(evidence, "the observed result and environment")
    revision = preview["acceptance_revision"]
    records = _current_records(_verification(app), revision)
    records[criterion_id] = {"status": status, "evidence": evidence}
    _record(app, revision, records)
    app.state.pop("acceptance_delivery", None)
    app.state["delivery_preview_presented"] = False
    app.state["delivery_confirmed"] = False
    app.state.pop("presented_delivery_snapshot", None)
    app.save_state()


def update_plan_feedback(app, reason: str) -> None:
    if not awaiting_plan_review(app.state) or not plan_rows(app.state):
        raise AcceptanceError(
            "The workflow is not waiting for acceptance criteria approval."
        )
    reason = _text(reason, "plan revision feedback")
    prior = app.state.get("pending_plan_feedback", "")
    app.state["pending_plan_feedback"] = "\n\n".join(
        part
        for part in (prior, f"Owner acceptance criteria feedback: {reason}")
        if part
    )


def revise(app, snapshot_id: str, ids: list[str], reason: str) -> None:
    preview = _validate_snapshot(app, snapshot_id)
    reason = _text(reason, "a revision reason")
    by_id = {item["id"]: item for item in preview.get("acceptance", [])}
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(key, str) or key not in by_id for key in ids)
    ):
        raise AcceptanceError("Select one or more criteria from the current preview.")
    selected = [by_id[key] for key in dict.fromkeys(ids)]
    targets = app.state["implementation_approval"]["acceptance_target_files"]
    tasks = app.state["tasks"]
    paths = sorted({path for item in selected for path in targets[item["id"]]})
    if not paths or not set(paths) <= set(app.writable_implementation_file_scope()):
        raise AcceptanceError("No approved implementation files match those criteria.")
    task_id = f"HUMAN-REVIEW-AC-{time.time_ns()}"
    tasks.append(
        {
            "id": task_id,
            "description": f"Owner requested acceptance revisions: {reason}\n"
            + "\n".join(
                f"{item['id']} {item['requirement']}\nCheck: {item['check']}\nEvidence: {item['evidence']}"
                for item in selected
            ),
            "target_files": paths,
            "status": "pending",
            **app.fix_task_levels(),
        }
    )
    _record(app, _revision(app), _failed_records(_verification(app).get("records", {})))
    for key in (
        "acceptance_delivery",
        "delivery_preview",
        "delivery_preview_presented",
        "presented_delivery_snapshot",
        "delivery_confirmed",
    ):
        app.state.pop(key, None)
    app.state["delivery_artifacts_prepared"] = False
    app.state.setdefault("delivery", {})["report_done"] = False
    app.state["state"] = "IMPLEMENTING"
    app.save_state()


def delivery_decision(preview: dict, reason: str) -> dict | None:
    criteria = preview.get("acceptance", [])
    failures = [item["id"] for item in criteria if item["status"] == "failed"]
    if failures:
        raise AcceptanceError(
            f"Revise failed acceptance criteria first: {', '.join(failures)}."
        )
    pending = [item["id"] for item in criteria if item["status"] == "pending"]
    if not pending:
        return None
    return {
        "criterion_ids": pending,
        "reason": _text(reason, "a reason to accept pending criteria"),
    }
