from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STAGE_LABELS = {
    "PLANNING": "需求規劃",
    "DEVELOPING_PLAN": "撰寫實作計畫",
    "REVIEWING_PLAN": "審核實作計畫",
    "IMPLEMENTING": "實作中",
    "TESTING": "測試驗證",
    "REVIEWING_CODE": "程式審核",
    "WAITING_FOR_OWNER": "等待擁有者決策",
    "WAITING_FOR_DELIVERY": "交付準備與確認",
    "COMPLETED": "已完成",
    "FAILED": "流程失敗",
    "RESEARCHING": "研究中",
}


def owner_review_kind(state: dict) -> str:
    """Read explicit decisions, with a compatibility path for older saved runs."""
    if state.get("state") != "WAITING_FOR_OWNER":
        return ""
    source = state.get("human_review_source", "")
    resume = state.get("resume_state")
    pass_state = state.get("pass_state")
    reason = str(state.get("human_review_details", "")).strip()
    recorded = state.get("human_review_kind")
    if recorded is not None:
        expected = classify_owner_pause(state)
        compatible = (
            recorded == expected
            or recorded == "review"
            and source in {"Architect", "Manager", "Reviewer", "QA"}
        )
        return recorded if compatible else "invalid"
    if (
        source == "Orchestrator"
        and resume == pass_state == "REVIEWING_CODE"
        and (
            state.get("owner_acceptance_pending") is True
            or reason.startswith("Reviewer requires an owner decision.")
        )
    ):
        return "accept_existing"
    # Only the current verdict prefix and its recorded transition are evidence.
    approval_prefix = (
        "Architect approved the plan. The original request requires "
        "owner approval before implementation."
    )
    first_line = reason.splitlines()[0] if reason else ""
    approved = (
        source == "Architect"
        and state.get("plan_requires_owner_approval") is True
        and first_line == approval_prefix
        and resume == pass_state == "IMPLEMENTING"
    )
    rejected = (
        (
            source in {"Architect", "Manager"}
            and first_line == "PLAN_STATUS: REJECTED"
            and resume == "DEVELOPING_PLAN"
            and pass_state == "IMPLEMENTING"
        )
        or (
            source == "Reviewer"
            and first_line == "REJECTED"
            and resume == "IMPLEMENTING"
            and pass_state == "IMPLEMENTING"
        )
        or (
            source == "QA"
            and resume == "IMPLEMENTING"
            and pass_state == "REVIEWING_CODE"
            and (
                first_line == "QA_STATUS: FAILED"
                or re.match(
                    r"\A## [^\n]+ \([^\n()]+\)\nQA_STATUS: FAILED(?:\n|$)", reason
                )
            )
        )
    )
    qa_environment = (
        source == "QA"
        and resume == pass_state == "TESTING"
        and (
            re.match(
                r"\ATest command failed to launch \(exit (?:126|127)\), so no tests were executed\.",
                reason,
            )
            or reason.startswith(
                "QA reports that test failures/skips are caused by a missing "
                "environment dependency, not a code bug."
            )
        )
    )
    if approved:
        return "approve_plan"
    if rejected:
        return "revise"
    if qa_environment:
        return "retry_testing"
    return (
        "review" if source in {"Architect", "Manager", "Reviewer", "QA"} else "resume"
    )


def classify_owner_pause(state: dict) -> str:
    """Validate recorded decision kinds against structured transitions."""
    source = state.get("human_review_source")
    resume, passed = state.get("resume_state"), state.get("pass_state")
    if (
        source == "Orchestrator"
        and resume == passed == "REVIEWING_CODE"
        and state.get("owner_acceptance_pending") is True
    ):
        return "accept_existing"
    if source in {"Architect", "Manager"}:
        if (
            resume == passed == "IMPLEMENTING"
            and state.get("plan_requires_owner_approval") is True
        ):
            return "approve_plan"
        if resume == "DEVELOPING_PLAN" and passed == "IMPLEMENTING":
            return "revise"
    if source == "Reviewer" and resume == passed == "IMPLEMENTING":
        return "revise"
    if source == "QA":
        if resume == passed == "TESTING":
            return "retry_testing"
        if resume == "IMPLEMENTING" and passed == "REVIEWING_CODE":
            return "revise"
    return (
        "review" if source in {"Architect", "Manager", "Reviewer", "QA"} else "resume"
    )


def owner_action_target(state: dict, action: str) -> str:
    """Validate an owner operation before changing state or granting approval."""
    if state.get("state") != "WAITING_FOR_OWNER":
        raise RuntimeError("Workflow is not waiting for owner review.")
    kind = owner_review_kind(state)
    allowed = {
        "approve_plan": {"approve", "pass", "revise", "reject", "continue"},
        "revise": {"approve", "pass", "revise", "reject", "continue"},
        "review": {"approve", "pass", "revise", "reject", "continue"},
        "retry_testing": {"approve", "continue", "reject"},
        "resume": {"approve", "continue", "reject"},
        "accept_existing": {
            "approve",
            "pass",
            "continue",
            "preview-existing",
            "accept-existing",
            "reject",
        },
        "invalid": {"reject"},
    }
    if action not in allowed[kind]:
        raise RuntimeError(
            f"Owner action {action!r} is not available for decision {kind!r}."
        )
    if action == "continue":
        action = continue_decision(state)[0]
    if action == "reject":
        return "FAILED"
    if action == "preview-existing":
        return "WAITING_FOR_OWNER"
    if action == "accept-existing":
        return "REVIEWING_CODE"
    if action == "revise":
        return (
            "DEVELOPING_PLAN"
            if state.get("human_review_source") in {"Architect", "Manager"}
            else "IMPLEMENTING"
        )
    default = {"QA_RESPONSE": "TESTING", "Requirements": "PLANNING"}.get(
        state.get("human_review_source"), "REVIEWING_CODE"
    )
    target = state.get("pass_state", state.get("resume_state", default))
    if target == "FAILED" and state.get("human_review_source") in {
        "Developer",
        "Orchestrator",
    }:
        target = "IMPLEMENTING"
    if target not in STAGE_LABELS or target in {"WAITING_FOR_OWNER", "FAILED"}:
        raise RuntimeError(
            "Saved owner transition is invalid; inspect the workflow state."
        )
    if kind == "accept_existing" and target != "REVIEWING_CODE":
        raise RuntimeError("Existing changes require code review before delivery.")
    if state.get("human_review_source") == "QA_RESPONSE" and target != "TESTING":
        raise RuntimeError("Invalid QA output must retry TESTING.")
    if target == "COMPLETED" and not (
        state.get("workflow_mode") == "research"
        or state.get("delivery", {}).get("delivered")
    ):
        raise RuntimeError(
            "Undelivered changes cannot be marked completed by an owner review."
        )
    return target


def continue_decision(state: dict, max_revisions: int = 2) -> tuple[str, bool]:
    """Keep explicit approvals separate from repair and environment retries."""
    kind = owner_review_kind(state)
    if kind in {"revise", "review"}:
        return "revise", state.get("code_revisions", 0) >= max_revisions
    return "approve", False


def workflow_decision(
    state: dict,
    *,
    busy: bool = False,
    pause_requested: bool = False,
    external_lock: bool = False,
    can_submit: bool = False,
    git: dict | None = None,
    snapshot_id: str = "",
) -> dict:
    """Describe the next owner decision for every interface; never mutate state."""
    current = state.get("state", "PLANNING")
    source = state.get("human_review_source", "")
    prepared = state.get("delivery_artifacts_prepared") is True
    delivered = (
        isinstance(state.get("delivery"), dict)
        and state["delivery"].get("delivered") is True
    )
    presented = bool(state.get("delivery_preview_presented") and snapshot_id and git)
    valid_preview = presented and prepared
    status = {
        **state,
        "pause_requested": pause_requested,
        "external_lock": external_lock,
    }
    stage = state.get("stage", current)
    stage_labels = STAGE_LABELS
    buttons, delivery_buttons, display_buttons, reset_buttons = [], [], [], []
    destinations = {}

    def button(
        label,
        action,
        body=None,
        confirmation=None,
        *,
        primary=False,
        enabled=True,
        target=None,
        identity=None,
    ):
        if current == "WAITING_FOR_OWNER" and action in {
            "approve",
            "review",
            "continue",
        }:
            operation = (body or {}).get("decision", action)
            try:
                destinations[operation] = owner_action_target(state, operation)
            except RuntimeError:
                return
        (buttons if target is None else target).append(
            {
                "label": label,
                "action": action,
                "body": body,
                "confirmation": confirmation,
                "primary": primary,
                "enabled": enabled,
                "identity": identity,
            }
        )

    title = "等待下一步"
    explanation = "請查看目前階段及報告，再決定是否繼續。"
    if busy:
        if status.get("external_lock"):
            title = "其他執行程序持有工作流程鎖"
            explanation = (
                "請等待其他執行程序釋放工作流程鎖；目前不可核准、展示或確認交付。"
            )
        elif current == "WAITING_FOR_DELIVERY":
            title = "正在驗證並處理交付" if prepared else "正在準備交付資料"
            explanation = (
                "背景工作尚未結束，完成後會更新結果；目前請等待，交付確認暫停使用。"
            )
        elif status.get("pause_requested"):
            title = "目前步驟完成後暫停"
            explanation = "已收到停止請求，正在等待目前步驟安全完成，不會中斷正在進行的模型或測試。"
        else:
            title = "執行中"
            explanation = f"目前正在處理「{stage_labels.get(stage, stage)}」，背景工作結束後會更新狀態。"
        if not status.get("pause_requested") and current not in {
            "WAITING_FOR_OWNER",
            "WAITING_FOR_DELIVERY",
            "COMPLETED",
            "FAILED",
        }:
            button("目前步驟完成後停止", "stop", primary=True)
    elif current == "WAITING_FOR_OWNER":
        kind = owner_review_kind(state)
        approved = kind == "approve_plan"
        rejected = kind == "revise"
        qa_environment = kind == "retry_testing"
        if kind == "invalid":
            title = "決策資料不一致，已停止執行"
            explanation = "儲存的決策類型與恢復階段不一致。請檢查狀態資料；目前只能拒絕本次結果或明確重置。"
        elif kind == "accept_existing":
            title = "等待擁有者處置現有變更"
            explanation = "請先展示接受範圍。接受會記錄你對現有變更及首次核准時序疑義的處置，然後重新審查程式；交付仍需另行確認。"
            acceptance_preview = status.get("owner_acceptance_preview", {})
            button(
                "展示接受範圍",
                "review",
                {"decision": "preview-existing"},
                primary=not acceptance_preview,
            )
            if acceptance_preview:
                scope = ", ".join(acceptance_preview["snapshot"]["files"])
                explanation += (
                    f" 接受檔案：{scope}。範圍版本：{acceptance_preview['id']}。"
                )
                button(
                    "接受現有變更並重新審查",
                    "review",
                    {
                        "decision": "accept-existing",
                        "snapshot_id": acceptance_preview["id"],
                    },
                    f"接受以下現有變更及首次核准時序疑義：{scope}\n範圍版本：{acceptance_preview['id']}\n接受後將重新審查，並不代表測試通過或確認交付。確定接受？",
                    primary=True,
                )
            button("重新審查（未接受現有變更）", "approve")
        elif approved:
            title = "計畫已通過，等待擁有者核准實作"
            explanation = (
                "Architect 已通過目前計畫。核准後將進入實作，不會重新修訂計畫。"
            )
            button("核准實作", "approve", primary=True)
        elif rejected:
            title = "審核未通過，等待修訂"
            explanation = (
                "提交修訂後會重新撰寫計畫，再進行審核。"
                if source in {"Architect", "Manager"}
                else "提交修訂後會建立既有人工修訂任務，回到實作流程。"
            )
            button("提交修訂", "review", {"decision": "revise"}, primary=True)
        elif qa_environment:
            title = "測試環境待處理，尚未完成驗證"
            explanation = "請先處理報告中的測試啟動或依賴問題，再重試 QA 驗證；此操作重跑 TESTING，不建立修復任務。"
            button("重試 QA 驗證", "approve", primary=True)
        elif source in {"Architect", "Manager", "Reviewer", "QA"}:
            title = "等待擁有者檢視，目前資料不足以判定審核結果"
            explanation = (
                "請先閱讀目前原因與完整報告。下列次要操作的效果不同，請明確選擇。"
            )
        else:
            title = "已暫停，等待擁有者決策"
            label = (
                "重新產生需求"
                if source == "Requirements"
                else "重試 QA 驗證"
                if source == "QA_RESPONSE"
                else "繼續目前任務"
            )
            explanation = (
                "將重新產生需求並回到規劃階段。"
                if source == "Requirements"
                else (
                    "QA 回覆未符合契約，重試將回到 TESTING，不代表測試通過。"
                    if source == "QA_RESPONSE"
                    else "繼續後會依已保存的恢復階段執行，既有路由限制仍然適用。"
                )
            )
            button(label, "continue", primary=True)
            if source in {"Developer", "Orchestrator"}:
                button(
                    "清除失敗路由並重試",
                    "continue",
                    {"clear_route_failures": True},
                    "將重新嘗試已因回覆格式失敗的模型，可能再次消耗額度。確定重試？",
                )
        if source in {"Architect", "Manager", "Reviewer", "QA"}:
            if not rejected and not qa_environment:
                button("依審核意見修訂", "review", {"decision": "revise"})
            if not approved and not qa_environment:
                button(
                    "接受目前結果並繼續",
                    "review",
                    {"decision": "pass"},
                    "此操作會覆核本次否決並沿通過路徑繼續，確定接受？",
                )
            if not approved and not rejected and not qa_environment:
                button(
                    "依既有來源規則繼續（將修訂）",
                    "continue",
                    confirmation="此來源的繼續操作會提交修訂，並不代表核准。確定繼續？",
                )
        button(
            "拒絕本次結果",
            "review",
            {"decision": "reject"},
            "拒絕會將本次流程標記為失敗，確定拒絕？",
        )
    elif current == "WAITING_FOR_DELIVERY":
        if not prepared:
            title = "準備交付資料中"
            explanation = (
                "展示前會先完成最終報告、CHANGELOG 與既有交付成果；準備完成後才可確認。"
            )
        elif valid_preview:
            title = "已展示 Git 影響，等待二次確認"
            explanation = "請檢視下方展示內容與快照 ID。明確確認後，系統仍會重新驗證，再執行既有 Git 交付。"
        else:
            title = "等待展示 Git 影響"
            explanation = "請先展示並檢視目前 Git 影響，再另行確認交付。"
    elif current == "COMPLETED":
        if status.get("workflow_mode") == "research":
            title = "研究已完成"
            explanation = "研究結果可由下方報告查看，此流程不需要 Git 交付確認。"
        elif delivered:
            title = "已完成交付"
            explanation = "既有交付流程已完成，詳細結果請查看最終報告。"
        else:
            title = "流程已完成，交付結果待檢視"
            explanation = "目前資料不足以確認 Git 已交付，請查看最終報告與交付紀錄。"
    elif current == "FAILED":
        title = "流程失敗"
        explanation = "請先查看執行錯誤與報告，再提交新任務或明確重置。"
        button(
            "重新規劃",
            "reject",
            confirmation="將清理本次 worktree 並保留需求重新規劃。確定繼續？",
            primary=not can_submit,
        )
    elif current not in STAGE_LABELS:
        title = "未知的流程階段"
        explanation = "請先檢查儲存的狀態資料；目前無法繼續執行。"
    elif not can_submit:
        button("繼續執行", "continue", primary=True)

    if can_submit:
        title = title if current in {"COMPLETED", "FAILED"} else "提交新任務"
        explanation += " 可在任務輸入區提交新需求，開始新的單一任務流程。"

    if current == "WAITING_FOR_DELIVERY":
        button(
            "準備並展示 Git 影響" if not prepared else "展示 Git 影響",
            "deliver",
            primary=not busy and not valid_preview,
            enabled=not busy,
            target=display_buttons,
        )
    if presented and current == "WAITING_FOR_DELIVERY":
        button(
            "確認二次交付",
            "deliver",
            primary=not busy and current == "WAITING_FOR_DELIVERY" and valid_preview,
            enabled=not busy and current == "WAITING_FOR_DELIVERY" and valid_preview,
            target=delivery_buttons,
            identity="confirm-delivery",
        )
    if not busy:
        button(
            "重置任務",
            "reset",
            confirmation="重置會清除已存任務內容並清理目前 worktree，且需要重新提交任務。確定重置？",
            target=reset_buttons,
        )

    return {
        "title": title,
        "explanation": explanation,
        "actions": buttons,
        "display_actions": display_buttons,
        "delivery_actions": delivery_buttons,
        "reset_actions": reset_buttons,
        "kind": owner_review_kind(state),
        "destinations": destinations,
    }


def next_actions(state: dict, delivery_pending: bool = False, **runtime) -> list[dict]:
    if state.get("state") == "COMPLETED" and delivery_pending:
        state = {**state, "state": "WAITING_FOR_DELIVERY"}
    preview = state.get("delivery_preview") or state.get(
        "presented_delivery_snapshot", {}
    )
    # Confirmation commands must refer to the preview actually presented.
    from orchestrator.core.state import delivery_snapshot_id

    snapshot_id = delivery_snapshot_id(preview) if preview else ""
    decision = workflow_decision(state, git=preview, snapshot_id=snapshot_id, **runtime)
    actions = []
    for item in (
        decision["actions"]
        + decision["display_actions"]
        + decision["delivery_actions"]
        + (decision["reset_actions"] if state.get("state") == "FAILED" else [])
    ):
        if not item["enabled"]:
            continue
        command = item["action"]
        body = item["body"] or {}
        if item["identity"] == "confirm-delivery":
            command = "deliver --confirm " + snapshot_id
        elif command == "review":
            command += " " + body["decision"]
            if body.get("snapshot_id"):
                command += " --snapshot " + body["snapshot_id"]
        elif command == "continue" and state.get("state") != "WAITING_FOR_OWNER":
            command = "run"
        elif body.get("clear_route_failures"):
            command += " --clear-route-failures"
        if command == "stop":
            command = "pause"
        actions.append(
            {
                "command": command,
                "label": item["label"],
                "description": decision["explanation"],
            }
        )
    return actions


def write_snapshot(orchestrator) -> None:
    state = orchestrator.state
    delivery = state.get("delivery", {})
    snapshot = {
        **{
            key: state.get(key)
            for key in (
                "resume_state",
                "pass_state",
                "human_review_kind",
                "plan_requires_owner_approval",
                "owner_acceptance_pending",
                "owner_acceptance_preview",
                "delivery_artifacts_prepared",
                "delivery_preview_presented",
                "delivery_preview",
                "presented_delivery_snapshot",
                "workflow_mode",
            )
        },
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
