from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def next_actions(state: dict, delivery_pending: bool = False) -> list[dict]:
    current = state.get("state")
    if current == "COMPLETED":
        if delivery_pending:
            return [
                {
                    "command": "deliver",
                    "label": "Deliver",
                    "description": "Review the Git preview, then confirm delivery.",
                },
                {
                    "command": "reject",
                    "label": "Reject",
                    "description": "Discard this run and return it to planning.",
                },
            ]
        return []
    if current == "FAILED":
        return [
            {
                "command": "reject",
                "label": "Re-plan",
                "description": "Keep the request and plan again.",
            },
            {
                "command": "reset",
                "label": "Reset",
                "description": "Clear this run and start over.",
            },
        ]
    if current == "WAITING_FOR_OWNER":
        actions = [
            {
                "command": "continue",
                "label": "Continue",
                "description": "Apply the existing approve/revise decision and resume.",
            },
            {
                "command": "reject",
                "label": "Reject",
                "description": "Reject this run and return it to planning.",
            },
        ]
        if state.get("human_review_source") in {
            "Architect",
            "Manager",
            "Reviewer",
            "QA",
        }:
            actions.insert(
                1,
                {
                    "command": "review pass",
                    "label": "Override: pass",
                    "description": "Accept the review and resume.",
                },
            )
        return actions
    return [
        {
            "command": "run",
            "label": "Run",
            "description": "Run the workflow to its next decision point.",
        }
    ]


def write_snapshot(orchestrator) -> None:
    state = orchestrator.state
    delivery = state.get("delivery", {})
    snapshot = {
        "state": state.get("state", "PLANNING"),
        "active_task_id": state.get("active_task_id"),
        "tasks": [
            {
                "id": task.get("id"),
                "status": task.get("status"),
                "description": str(task.get("description", ""))[:120],
            }
            for task in state.get("tasks", [])
        ],
        "plan_revisions": state.get("plan_revisions", 0),
        "code_revisions": state.get("code_revisions", 0),
        "max_revisions": orchestrator.config.get("max_revisions", 0),
        "human_review_source": state.get("human_review_source"),
        "human_review_details": state.get("human_review_details", ""),
        "delivery": {
            "preview": delivery.get("preview", ""),
            "token": delivery.get("token"),
            "delivered": bool(delivery.get("delivered")),
        },
        "pause_requested": orchestrator.pause_request_path.exists(),
        "next_actions": next_actions(
            state, bool(delivery) and not delivery.get("delivered", False)
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = orchestrator.status_path
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
