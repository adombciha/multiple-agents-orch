from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class DeliveryError(RuntimeError):
    pass


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DeliveryError(f"Git check failed ({' '.join(args)}): {detail}")
    return result.stdout


def _worktree_is_owned(orchestrator, worktree: Path) -> bool:
    listing = _git(orchestrator.workspace, "worktree", "list", "--porcelain")
    expected_path = worktree.resolve()
    for record in listing.split("\n\n"):
        lines = record.splitlines()
        path = next((line[9:] for line in lines if line.startswith("worktree ")), None)
        branch = "branch refs/heads/ai-feature-branch" in lines
        if path and Path(path).resolve() == expected_path and branch:
            return True
    return False


def _untracked_hashes(worktree: Path) -> list[dict]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise DeliveryError(
            f"Could not enumerate untracked files: {result.stderr.decode(errors='replace')}"
        )
    root = worktree.resolve()
    files = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = os.fsdecode(raw_path)
        candidate = worktree / relative
        try:
            candidate.resolve().relative_to(root)
        except ValueError as error:
            raise DeliveryError(
                f"Untracked path escapes the worktree: {relative}"
            ) from error
        content = (
            os.fsencode(os.readlink(candidate))
            if candidate.is_symlink()
            else candidate.read_bytes()
        )
        files.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    return files


def _workspace_changes(orchestrator) -> list[str]:
    output = _git(
        orchestrator.workspace,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    try:
        ai_dir = orchestrator.ai_dir.relative_to(orchestrator.workspace).as_posix()
    except ValueError:
        ai_dir = ""
    changes = []
    for line in output.splitlines():
        path = line[3:].strip().strip('"')
        if ai_dir and (path == ai_dir or path.startswith(f"{ai_dir}/")):
            continue
        changes.append(line)
    return changes


def _changelog_entry(orchestrator) -> str:
    run_id = orchestrator.state.get("run_id")
    if not run_id:
        return ""
    path = orchestrator.agent_workspace() / "CHANGELOG.md"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = f"<!-- ai-company-run: {run_id} -->"
    start = content.find(marker)
    if start < 0:
        return ""
    next_marker = content.find("<!-- ai-company-run:", start + len(marker))
    entry = content[start : next_marker if next_marker >= 0 else None]
    return entry.strip()


def delivery_preview(orchestrator) -> dict:
    if not orchestrator.has_git or not orchestrator.config.get("use_worktree", True):
        payload = {
            "target": "No Git merge",
            "base_head": None,
            "feature_head": None,
            "files": [],
            "diff_stat": "No Git impact to preview.",
            "workspace_changes": [],
            "untracked": [],
            "changelog_entry": _changelog_entry(orchestrator),
            "operation": "Confirm delivery without a Git merge.",
        }
        return _add_token(payload)

    worktree = orchestrator.ai_dir / "worktree"
    if not worktree.is_dir() or not _worktree_is_owned(orchestrator, worktree):
        raise DeliveryError("The expected feature worktree is not owned by this run.")

    current_branch = _git(orchestrator.workspace, "branch", "--show-current").strip()
    if (
        orchestrator.base_branch != "HEAD"
        and current_branch != orchestrator.base_branch
    ):
        raise DeliveryError(
            f"The workspace branch changed from {orchestrator.base_branch} to {current_branch or 'detached HEAD'}."
        )
    base_head = _git(orchestrator.workspace, "rev-parse", "HEAD").strip()
    feature_head = _git(worktree, "rev-parse", "HEAD").strip()
    status = _git(
        worktree,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    diff_stat = _git(worktree, "diff", "--stat", base_head).strip()
    diff = _git(worktree, "diff", "--binary", base_head)
    changed = _git(worktree, "diff", "--name-status", base_head)
    files = [line for line in changed.splitlines() if line]
    untracked = _untracked_hashes(worktree)
    files.extend(f"?? {item['path']}" for item in untracked)
    payload = {
        "target": f"{orchestrator.base_branch} @ {base_head}",
        "target_branch": current_branch or "HEAD",
        "base_head": base_head,
        "feature_head": feature_head,
        "status": status,
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "files": files,
        "diff_stat": diff_stat or "No tracked-file changes.",
        "workspace_changes": _workspace_changes(orchestrator),
        "untracked": untracked,
        "changelog_entry": _changelog_entry(orchestrator),
        "operation": "Auto-commit remaining worktree changes, then merge ai-feature-branch.",
    }
    return _add_token(payload)


def _add_token(payload: dict) -> dict:
    value = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    payload["token"] = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return payload


def render_preview(payload: dict) -> str:
    files = payload.get("files") or ["(none)"]
    workspace_changes = payload.get("workspace_changes", [])
    workspace_status = (
        "clean"
        if not workspace_changes
        else "has uncommitted changes (delivery blocked)"
    )
    lines = [
        "Git delivery preview",
        f"Target: {payload['target']}",
        f"Feature commit: {payload.get('feature_head') or '(none)'}",
        f"Working tree: {workspace_status}",
        "",
        "Files:",
        *files,
        "",
        "Diff stat:",
        payload.get("diff_stat", ""),
        "",
        "CHANGELOG entry:",
        payload.get("changelog_entry") or "(none)",
        "",
        f"Planned operation: {payload['operation']}",
        f"Confirmation token: {payload['token']}",
    ]
    if workspace_changes:
        lines.extend(["", "Workspace changes:", *workspace_changes])
    return "\n".join(lines) + "\n"


def save_preview(orchestrator, payload: dict) -> str:
    preview = render_preview(payload)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=orchestrator.ai_dir, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(preview)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, orchestrator.delivery_preview_path)
    finally:
        temporary.unlink(missing_ok=True)
    delivery = orchestrator.state.setdefault("delivery", {})
    delivery["preview"] = preview
    delivery["token"] = payload["token"]
    orchestrator.save_state()
    return preview


def deliver(orchestrator, confirm_token: str | None = None) -> bool:
    from orchestrator.core.state import log_error, log_info, log_success, log_warning

    delivery = orchestrator.state.setdefault("delivery", {})
    if orchestrator.state.get("state") != "COMPLETED" or delivery.get("delivered"):
        log_error("Delivery is only available for a completed, undelivered workflow.")
        return False
    try:
        current = delivery_preview(orchestrator)
    except (DeliveryError, OSError) as error:
        log_error(f"Cannot create a safe delivery preview: {error}")
        return False

    preview = render_preview(current)
    if confirm_token is None:
        print(save_preview(orchestrator, current))
        log_info(
            f"Review the preview, then confirm with: python3 orchestrator.py deliver --confirm {current['token']}"
        )
        return False

    displayed_token = delivery.get("token")
    if not re.fullmatch(r"[0-9a-f]{12}", confirm_token):
        log_error("Confirmation token must be 12 lowercase hexadecimal characters.")
        return False
    if displayed_token != confirm_token or current["token"] != displayed_token:
        print(save_preview(orchestrator, current))
        log_warning(
            "Delivery contents changed or were not previously displayed; review this preview and confirm again."
        )
        return False
    if current.get("workspace_changes"):
        log_error("Delivery blocked: the main workspace has uncommitted changes.")
        print(preview)
        return False

    if not orchestrator.has_git or not orchestrator.config.get("use_worktree", True):
        delivery["delivered"] = True
        delivery["delivered_at"] = datetime.now(timezone.utc).isoformat()
        delivery.pop("token", None)
        orchestrator.save_state()
        log_success("Delivery confirmed; no Git merge was required.")
        return True

    if orchestrator.cleanup_worktree(merge=True) is False:
        details = orchestrator.state.get("worktree_merge_error", "Git merge failed.")
        orchestrator.merge_failure_path.write_text(str(details), encoding="utf-8")
        delivery["token"] = None
        delivery["preview"] = ""
        orchestrator.save_state()
        task = orchestrator.queue_worktree_repair_task()
        log_warning(
            f"Merge failed; worktree preserved and repair task {task['id']} queued. Run the workflow to continue."
        )
        return False

    delivery["delivered"] = True
    delivery["delivered_at"] = datetime.now(timezone.utc).isoformat()
    delivery["merged_head"] = _git(orchestrator.workspace, "rev-parse", "HEAD").strip()
    delivery.pop("token", None)
    orchestrator.save_state()
    log_success("Delivery confirmed and feature branch merged successfully.")
    return True
