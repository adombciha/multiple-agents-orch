import os
import re
import sys
import argparse
import json
import uuid
import tempfile
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen

from http.cookies import SimpleCookie
from pathlib import Path
from orchestrator.core.state import (
    AgentOrchestrator,
    Colors,
    log_success,
    log_info,
    log_warning,
    log_error,
    log_header,
)
from orchestrator.core import scoring

REFERENCE_BLOCK_START = (
    "<!-- REFERENCE-ONLY:START (context, not a file to create or edit) -->"
)
REFERENCE_BLOCK_END = "<!-- REFERENCE-ONLY:END -->"


def inline_referenced_markdown(prompt: str, workspace: Path) -> str:
    """Some backends in manager/planning calls only ever see the
    prompt text, not the filesystem -- a path mentioned in the prompt is just
    words to them. If the prompt references an existing .md file by relative
    path, append that file's actual content so every backend sees it, not
    just the ones with their own file-reading tools.

    The appended content is wrapped in REFERENCE_BLOCK_START/END markers.
    References are masked outside those markers so DEVELOPING_PLAN does not
    mistake background documents for edit targets. Paths on an explicit
    "Target files:" line remain visible to the planner."""
    pattern = re.compile(r"[A-Za-z0-9_.\-/]+\.md(?![A-Za-z0-9_])")
    referenced = []
    for name in pattern.findall(prompt):
        path = Path(name)
        if path.is_absolute():
            continue
        resolved = (workspace / path).resolve()
        if workspace != resolved and workspace not in resolved.parents:
            continue
        if resolved.is_file() and resolved not in referenced:
            referenced.append(resolved)
    if not referenced:
        return prompt

    def _mask(match: re.Match) -> str:
        line_start = prompt.rfind("\n", 0, match.start()) + 1
        if re.match(r"\s*Target files\s*:", prompt[line_start : match.start()], re.I):
            return match.group(0)
        path = Path(match.group(0))
        resolved = (workspace / path).resolve()
        return (
            f"'{resolved.stem}' (see reference material below)"
            if resolved in referenced
            else match.group(0)
        )

    masked_prompt = pattern.sub(_mask, prompt)
    appended = f"\n\n{REFERENCE_BLOCK_START}\nReferenced file contents (inlined automatically so every backend can see them; this block is context, and edit targets are identified outside it):\n"
    for path in referenced:
        appended += f"\n## {path.relative_to(workspace)}\n\n{path.read_text(encoding='utf-8')}\n"
    appended += f"\n{REFERENCE_BLOCK_END}\n"
    return masked_prompt + appended


def clear_run_state(orchestrator: AgentOrchestrator, target_state: str) -> None:
    """Shared by `reset` and `reject`: wipe revision counters, staffing, and
    the worktree so a run can start over from `target_state`. Does not touch
    the saved request prompt -- callers decide separately whether to keep it
    (`reject`, so the same ask gets re-planned) or clear it too (`reset`,
    so a fresh `start` is required)."""
    if orchestrator.cleanup_worktree(merge=False) is False:
        raise RuntimeError("Git worktree cleanup failed; run state was preserved.")
    orchestrator.state["state"] = target_state
    orchestrator.state["plan_revisions"] = 0
    orchestrator.state["code_revisions"] = 0
    orchestrator.state["model_tier_indices"] = {
        "developer": 0,
        "reviewer": 0,
        "manager": 0,
        "qa": 0,
    }
    orchestrator.state["specialists"] = []
    orchestrator.state["staffing"] = {}
    orchestrator.state["worker_assignments"] = {}
    orchestrator.state["last_developer_role"] = "developer_senior"
    orchestrator.state["last_qa_level"] = "senior"
    orchestrator.state["developer_promotions"] = {}
    orchestrator.state["task_developer_promotions"] = {}
    orchestrator.state["task_backend_overrides"] = {}
    orchestrator.state["quota_exhausted_backends"] = {}
    orchestrator.state["failed_model_routes"] = []
    orchestrator.state["task_failed_model_routes"] = {}
    orchestrator.state["task_contract_failures"] = {}
    orchestrator.state["task_developer_calls"] = {}
    orchestrator.state["task_error_signatures"] = {}
    orchestrator.state["tasks"] = []
    orchestrator.state["active_task_id"] = None


def _all_available_developer_routes_failed_contract(orchestrator, task_id: str) -> bool:
    from orchestrator.core import backends

    task = next(
        (
            task
            for task in orchestrator.state.get("tasks", [])
            if task.get("id") == task_id
        ),
        {},
    )
    assignment = (
        orchestrator.state.get("worker_assignments", {}).get("rd", {}).get(task_id, "")
    )
    level = (
        assignment.split("-")[1]
        if assignment.count("-") >= 2
        else task.get("rd_level", task.get("assignee_level", "senior"))
    )
    role = f"developer_{level}"
    role = (
        orchestrator.state.get("task_developer_promotions", {})
        .get(task_id, {})
        .get(role, role)
    )
    configured = list(orchestrator.config.get("role_model_routes", {}).get(role, []))
    override = orchestrator.state.get("task_backend_overrides", {}).get(task_id)
    if override:
        configured.sort(key=lambda route: route[0] != override)

    routes = []
    ollama_added = False
    for backend, model in configured:
        if not backends.backend_available(orchestrator, backend):
            continue
        if backend == "ollama":
            if ollama_added:
                continue
            ollama_added = True
            candidates = sorted(backends.discover_ollama_chat_models(orchestrator))
            if orchestrator.config.get("ollama_router_enabled", True):
                ranked = backends.route_ollama_model_with_tev1(
                    orchestrator,
                    role,
                    candidates,
                    task.get("implementation_scope") or task.get("description", ""),
                )
            else:
                ranked = None
            try:
                limit = max(
                    0, int(orchestrator.config.get("max_ollama_routes_per_call", 2))
                )
            except (TypeError, ValueError):
                limit = 2
            routes.extend(["ollama", item] for item in (ranked or candidates)[:limit])
        else:
            routes.append([backend, model])
    available = [
        f"{backend}/{model}"
        for backend, model in routes
        if backends.backend_available(orchestrator, backend)
    ]
    failed = orchestrator.state.get("task_contract_failures", {}).get(task_id, {})
    return bool(available) and all(route in failed for route in available)


def clear_task_route_failures(orchestrator, task_id: str, clear_contract: bool) -> None:
    old_failures = orchestrator.state.setdefault("task_failed_model_routes", {}).pop(
        task_id, []
    )
    if old_failures:
        log_info(
            f"Cleared {len(old_failures)} cached route failures for {task_id} before retry."
        )
    if clear_contract:
        contract_failures = orchestrator.state.setdefault(
            "task_contract_failures", {}
        ).pop(task_id, {})
        if contract_failures:
            log_info(
                f"Cleared {len(contract_failures)} contract-failed routes for {task_id}."
            )


def apply_approve(
    orchestrator: AgentOrchestrator, clear_route_failures: bool = False
) -> bool:
    """Resume a WAITING_FOR_OWNER pause at its recorded pass_state (falling
    back to resume_state), resetting the Developer budget after a Developer
    pause while retaining contract-failed routes unless explicitly cleared."""
    resume_state = orchestrator.state.get(
        "pass_state", orchestrator.state.get("resume_state", "REVIEWING_CODE")
    )
    if resume_state == "FAILED":
        resume_state = "IMPLEMENTING"
    active_task_id = orchestrator.state.get("active_task_id")
    if active_task_id:
        source = orchestrator.state.get("human_review_source")
        if (
            source in {"Developer", "Orchestrator"}
            and not clear_route_failures
            and _all_available_developer_routes_failed_contract(
                orchestrator, active_task_id
            )
        ):
            log_error(
                "Every available Developer route for this task has failed its output contract. "
                "Use 'reject' to re-plan, or approve/continue with --clear-route-failures to retry them."
            )
            return False
        clear_task_route_failures(
            orchestrator, active_task_id, clear_contract=clear_route_failures
        )
        if source in {"Developer", "Orchestrator"}:
            for task in orchestrator.state.get("tasks", []):
                if task.get("id") == active_task_id:
                    task["revisions"] = 0
                    break
            orchestrator.state.setdefault("task_developer_calls", {})[
                active_task_id
            ] = 0
            orchestrator.state.setdefault("task_error_signatures", {}).pop(
                active_task_id, None
            )
        ):
            log_error(
                "Every available Developer route for this task has failed its output contract. "
                "Use 'reject' to re-plan, or approve/continue with --clear-route-failures to retry them."
            )
            return False
        clear_task_route_failures(
            orchestrator, active_task_id, clear_contract=clear_route_failures
        )
        if source in {"Developer", "Orchestrator"}:
            for task in orchestrator.state.get("tasks", []):
                if task.get("id") == active_task_id:
                    task["revisions"] = 0
                    break
            orchestrator.state.setdefault("task_developer_calls", {})[
                active_task_id
            ] = 0
            orchestrator.state.setdefault("task_error_signatures", {}).pop(
                active_task_id, None
            )
    orchestrator.clear_pause_request()
    orchestrator.state["state"] = resume_state
    orchestrator.state.pop("human_review_source", None)
    orchestrator.state.pop("resume_state", None)
    orchestrator.save_state()
    log_success(f"Owner approval recorded; workflow resumed at {resume_state}.")
    return True


def apply_review_decision(
    orchestrator: AgentOrchestrator, decision: str, reset_revisions: bool = False
) -> None:
    """Apply a pass/revise/reject decision to a WAITING_FOR_OWNER pause.
    Pass and revise resume the workflow by clearing any pending pause request.
    `reset_revisions` (revise only, non-Architect/Manager source): clear
    code_revisions first so the auto-revise loop gets a fresh budget instead
    of immediately re-pausing if it was already at max_revisions."""
    if decision == "pass":
        next_state = orchestrator.state.get("pass_state", "REVIEWING_CODE")
    elif decision == "revise":
        if orchestrator.state.get("human_review_source") in {"Architect", "Manager"}:
            next_state = "DEVELOPING_PLAN"
            orchestrator.state["plan_revisions"] = 0
        else:
            next_state = "IMPLEMENTING"
            if reset_revisions:
                orchestrator.state["code_revisions"] = 0
            n = orchestrator.state.get("code_revisions", 0) + 1
            orchestrator.state["code_revisions"] = n
            orchestrator.state.setdefault("tasks", []).append(
                {
                    "id": f"HUMAN-REVIEW-{n}",
                    "description": f"Implement human review revisions:\n{orchestrator.state.get('human_review_details', '')[:2000]}",
                    "target_files": orchestrator.relevant_target_files(
                        orchestrator.state.get("human_review_details", "")
                    ),
                    "status": "pending",
                    **orchestrator.fix_task_levels(),
                }
            )
    else:
        next_state = "FAILED"
    if decision == "pass" and orchestrator.state.get("human_review_source") == "QA":
        for qa_level in orchestrator.state.get("qa_dissenting_levels", []):
            qa_role = f"qa_{qa_level}"
            qa_backend = orchestrator.get_backend(qa_role)
            qa_model = orchestrator.get_active_model_for_role(qa_role, qa_backend)
            scoring.record_event(
                orchestrator, "qa", qa_level, qa_backend, qa_model, -0.5
            )
            log_info(
                f"Recorded QA false-positive for qa_{qa_level} after owner override."
            )
    if decision in {"pass", "revise"}:
        orchestrator.clear_pause_request()
    orchestrator.state["state"] = next_state
    for key in (
        "human_review_source",
        "resume_state",
        "pass_state",
        "human_review_details",
        "qa_dissenting_levels",
    ):
        orchestrator.state.pop(key, None)
    orchestrator.save_state()
    log_success(f"Human review '{decision}' recorded; workflow moved to {next_state}.")


def pick_continue_decision(orchestrator: AgentOrchestrator) -> tuple[str, bool]:
    """Decide what `continue` should do with a WAITING_FOR_OWNER pause: an AI
    verdict that rejected something (Architect/Manager/Reviewer/QA) should be
    sent back for another attempt (revise); a Developer (or unrecognized)
    pause just means retries ran out on the current task, so resume it
    (approve). For revise, auto-clear the revision counter if it's already
    at the ceiling so the auto-loop gets a genuine attempt instead of
    immediately re-pausing."""
    source = orchestrator.state.get("human_review_source")
    if source not in {"Architect", "Manager", "Reviewer", "QA"}:
        return "approve", False
    max_rev = orchestrator.config.get("max_revisions", 2)
    at_ceiling = orchestrator.state.get("code_revisions", 0) >= max_rev
    return "revise", at_ceiling


def ollama_available(url: str) -> bool:
    try:
        with urlopen(f"{url.rstrip('/')}/api/tags", timeout=2):
            return True
    except (OSError, URLError):
        return False


def configure_ollama(orchestrator: AgentOrchestrator):
    local_url = orchestrator.config["ollama_url"]
    if ollama_available(local_url):
        log_success(f"Ollama is available at {local_url}")
        orchestrator.state["ollama_reachable"] = True
        orchestrator.save_state()
        return
    windows_url = f"http://{orchestrator.get_windows_host_ip()}:11434"
    if ollama_available(windows_url):
        log_info(f"Windows Ollama detected at {windows_url}")
        if not sys.stdin.isatty() or input(
            "Use this Windows Ollama endpoint? [Y/n] "
        ).strip().lower() not in {"n", "no"}:
            orchestrator.config["ollama_url"] = windows_url
            orchestrator.config_path.write_text(
                json.dumps(orchestrator.config, indent=2), encoding="utf-8"
            )
            log_success(f"Ollama API URL set to {windows_url}")
            orchestrator.state["ollama_reachable"] = True
            orchestrator.save_state()
    else:
        log_error(
            "No Ollama endpoint detected. Start Ollama locally or on the Windows host before running a task."
        )
        orchestrator.state["ollama_reachable"] = False
        orchestrator.save_state()


def set_pause_request(
    orchestrator: AgentOrchestrator, cancel: bool = False, source: str = "cli"
) -> None:
    path = orchestrator.pause_request_path
    if cancel:
        if path.exists():
            path.unlink()
            log_success("Pending pause request cancelled.")
        else:
            log_info("No pending pause request to cancel.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(
                {
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                    "source": source,
                },
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)
    log_success("Pause requested; the workflow will stop after its current step.")


def _run_cli(orchestrator: AgentOrchestrator | None = None):
    parser = argparse.ArgumentParser(description="Multi-Agent Orchestrator CLI")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    subparsers.add_parser(
        "init", help="Initialize the .ai-company folder and configuration"
    )

    start_parser = subparsers.add_parser("start", help="Start a new multi-agent task")
    start_parser.add_argument(
        "prompt", type=str, help="The development task prompt or request"
    )
    start_parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Screenshot or mockup path for UI/UX visual review; repeat for multiple images",
    )
    start_parser.add_argument(
        "--research",
        action="store_true",
        help="Run Sales and RA research only; skip code delivery roles",
    )

    subparsers.add_parser("step", help="Run the next step in the state machine")
    subparsers.add_parser("run", help="Run the agent loop to completion")
    pause_parser = subparsers.add_parser(
        "pause",
        help="Pause after the current step; use --cancel to cancel a pending pause",
    )
    pause_parser.add_argument("--cancel", action="store_true")
    pause_parser.add_argument(
        "--source", choices=("cli", "gui"), default="cli", help=argparse.SUPPRESS
    )
    deliver_parser = subparsers.add_parser(
        "deliver", help="Preview or confirm delivery of a completed workflow"
    )
    deliver_parser.add_argument("--confirm", help="Previously displayed delivery token")
    gui_parser = subparsers.add_parser("gui", help="Open the local workflow dashboard")
    gui_parser.add_argument("--port", type=int, help="HTTP port (defaults to config)")
    continue_parser = subparsers.add_parser(
        "continue",
        help="Auto-resume from wherever the project is: picks approve or revise "
        "for a paused review (auto-clearing the revision counter if needed) and "
        "keeps running. Use when you don't want to think about which command "
        "applies to the current pause.",
    )
    continue_parser.add_argument(
        "--clear-route-failures",
        action="store_true",
        help="Clear cached temporary and contract route failures for the active task before continuing.",
    )
    subparsers.add_parser(
        "status", help="Get current orchestrator status and logs info"
    )
    approve_parser = subparsers.add_parser(
        "approve", help="Approve a workflow paused for owner review"
    )
    approve_parser.add_argument(
        "--run", action="store_true", help="Continue the workflow after approval"
    )
    approve_parser.add_argument(
        "--clear-route-failures",
        action="store_true",
        help="Clear cached temporary and contract route failures for the active task before approval.",
    )
    review_parser = subparsers.add_parser("review", help="Decide a paused human review")
    review_parser.add_argument("decision", choices=["pass", "revise", "reject"])
    review_parser.add_argument(
        "--run", action="store_true", help="Continue the workflow after the decision"
    )
    review_parser.add_argument(
        "--reset-revisions",
        action="store_true",
        help="With 'revise': clear the code/plan revision counter first, giving the "
        "auto-revise loop a fresh budget instead of immediately re-pausing if it "
        "was paused for hitting max_revisions. Keeps the current worktree and "
        "tasks -- unlike 'reset', nothing already implemented is discarded.",
    )

    reject_parser = subparsers.add_parser(
        "reject",
        help="Send the whole project back to PLANNING, keeping the original "
        "request prompt so it gets re-planned from the same ask. Unlike "
        "'reset', nothing has to be re-typed.",
    )
    reject_parser.add_argument(
        "--run", action="store_true", help="Continue the workflow after rejecting"
    )

    reset_parser = subparsers.add_parser(
        "reset",
        help="Full restart: clears the saved request prompt too, so 'start' "
        "must be run again before 'run'. Use 'reject' instead to keep the "
        "same prompt and just re-plan.",
    )
    reset_parser.add_argument(
        "--state",
        type=str,
        default="PLANNING",
        help="Reset state to specific value (default: PLANNING)",
    )

    backend_parser = subparsers.add_parser(
        "set-backend", help="Set the agent backend for a role"
    )
    backend_parser.add_argument(
        "role",
        choices=[
            "manager",
            "architect",
            "developer",
            "reviewer",
            "qa",
            "assistant",
            "ra",
            "security",
            "sales",
            "sre",
            "devops",
            "uiux",
            "uiux_visual_review",
            "fae",
            "integration",
        ],
        help="The agent role",
    )
    backend_parser.add_argument(
        "backend",
        choices=["ollama", "codex", "agy"],
        help="The backend to use",
    )

    ollama_url_parser = subparsers.add_parser(
        "set-ollama-url", help="Set the Ollama API URL"
    )
    ollama_url_parser.add_argument(
        "url", help="Ollama API base URL, for example http://172.17.144.1:11434"
    )

    weights_parser = subparsers.add_parser(
        "weights", help="Show or adjust internal staff trust weights"
    )
    weights_sub = weights_parser.add_subparsers(dest="weights_command")
    weights_set_parser = weights_sub.add_parser(
        "set", help="Manually override a staff weight"
    )
    weights_set_parser.add_argument("role", choices=["rd", "qa"])
    weights_set_parser.add_argument("level", choices=["senior", "middle", "junior"])
    weights_set_parser.add_argument("value", type=float)

    subparsers.add_parser(
        "roster", help="Show today's available RD/QA roles (staff and vendors)"
    )
    subparsers.add_parser("vendors", help="Show the paid-vendor scorecard")

    brain_parser = subparsers.add_parser(
        "brain", help="Inspect or prune the internal company memory"
    )
    brain_sub = brain_parser.add_subparsers(dest="brain_command")
    brain_search_parser = brain_sub.add_parser("search", help="Search company memory")
    brain_search_parser.add_argument("query")
    brain_prune_parser = brain_sub.add_parser("prune", help="Prune old memories")
    brain_prune_parser.add_argument(
        "--keep",
        type=int,
        default=200,
        help="Number of most recent memories to keep (default: 200)",
    )

    args = parser.parse_args()

    workspace = Path(os.getcwd())
    orchestrator = orchestrator or AgentOrchestrator(workspace)

    if args.command == "init":
        try:
            orchestrator.init_project()
        except RuntimeError as error:
            log_error(str(error))
            return 1
        orchestrator.load_config_and_state()
        configure_ollama(orchestrator)
    elif args.command == "start":
        if orchestrator.state_path.exists():
            try:
                orchestrator.load_config_and_state()
            except (FileNotFoundError, json.JSONDecodeError) as error:
                log_error(f"Cannot safely start a new run: {error}")
                return 1
            state = orchestrator.state.get("state")
            delivery = orchestrator.state.get("delivery", {})
            if (
                state not in {"COMPLETED", "FAILED"}
                and orchestrator.request_path.exists()
            ):
                log_error(
                    f"A workflow is already active in state {state}; continue or review it before starting another task."
                )
                return 1
            if state == "COMPLETED" and not delivery.get("delivered", False):
                log_error(
                    "The completed workflow still has undelivered work; deliver or reject it before starting another task."
                )
                return 1
        try:
            orchestrator.clear_run_artifacts()
            orchestrator.init_project()
        except RuntimeError as error:
            log_error(str(error))
            return 1
        orchestrator.load_config_and_state()
        configure_ollama(orchestrator)
        ollama_reachable = orchestrator.state.get("ollama_reachable", True)
        allowed_image_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
        max_image_bytes = orchestrator.config.get(
            "max_visual_image_bytes", 10 * 1024 * 1024
        )
        image_paths = []
        for path in map(Path, args.image):
            if (
                not path.is_file()
                or path.suffix.lower() not in allowed_image_suffixes
                or path.stat().st_size > max_image_bytes
            ):
                log_error(
                    f"Each --image must be a PNG, JPEG, or WebP file no larger than {max_image_bytes // 1024 // 1024}MB."
                )
                image_paths = []
                for path in map(Path, args.image):
                    if (
                        not path.is_file()
                        or path.suffix.lower() not in allowed_image_suffixes
                        or path.stat().st_size > max_image_bytes
                    ):
                        log_error(
                            f"Each --image must be a PNG, JPEG, or WebP file no larger than {max_image_bytes // 1024 // 1024}MB."
                        )
                        return
                    image_paths.append(str(path.resolve()))

                # Save request prompt, auto-inlining any referenced .md files
                full_prompt = inline_referenced_markdown(
                    args.prompt, orchestrator.workspace
                )
                with open(orchestrator.request_path, "w", encoding="utf-8") as f:
                    f.write(full_prompt)
                log_success(f"Saved request to {orchestrator.request_path}")

        # Reset state to PLANNING
        orchestrator.state = {
            "state": "PLANNING",
            "plan_revisions": 0,
            "code_revisions": 0,
            "model_tier_indices": {
                "developer": 0,
                "reviewer": 0,
                "manager": 0,
                "qa": 0,
            },
            "tasks": [],
            "specialists": [],
            "staffing": {},
            "worker_assignments": {},
            "last_developer_role": "developer_senior",
            "last_qa_level": "senior",
            "developer_promotions": {},
            "task_developer_promotions": {},
            "task_backend_overrides": {},
            "quota_exhausted_backends": {},
            "failed_model_routes": [],
            "task_failed_model_routes": {},
            "task_contract_failures": {},
            "task_developer_calls": {},
            "task_error_signatures": {},
            "visual_image_paths": image_paths,
            "workflow_mode": "research" if args.research else "delivery",
            "research_roles": ["sales", "ra"] if args.research else [],
            "ollama_reachable": ollama_reachable,
            "run_id": uuid.uuid4().hex,
        }
        orchestrator.save_state()
        log_success(
            "Orchestrator initialized and ready to run. Run 'python3 orchestrator.py run' to execute."
        )
    elif args.command == "step":
        try:
            with orchestrator.execution_lock():
                orchestrator.step()
        except RuntimeError as error:
            log_error(str(error))
    elif args.command == "run":
        orchestrator.run_to_end()
    elif args.command == "pause":
        set_pause_request(orchestrator, args.cancel, args.source)
    elif args.command == "deliver":
        try:
            orchestrator.load_config_and_state()
            from orchestrator.core.delivery import deliver

            if args.confirm is not None and not deliver(orchestrator, args.confirm):
                return 1
        except (FileNotFoundError, json.JSONDecodeError) as error:
            log_error(f"Project is not ready for delivery: {error}")
            return 1
    elif args.command == "gui":
        port = args.port
        if port is None and orchestrator.config_path.exists():
            try:
                config = json.loads(
                    orchestrator.config_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                log_error(f"Cannot read GUI configuration: {error}")
                return 1
            port = config.get("gui_port", 8765)
        from orchestrator.gui import serve

        try:
            serve(orchestrator, 8765 if port is None else port)
        except ValueError as error:
            log_error(str(error))
            return 1
    elif args.command == "status":
        try:
            from orchestrator.core.runlock import RunLock
            from orchestrator.core.status import read_snapshot

            snapshot = read_snapshot(orchestrator.status_path)
            log_header("ORCHESTRATOR STATUS")
            print(
                f"{'Current State:':<20}{Colors.BOLD}{snapshot.get('state', 'unknown')}{Colors.ENDC}"
            )
            print(
                f"{'Plan Revisions:':<20}{snapshot.get('plan_revisions', 0)}/{snapshot.get('max_revisions', 0)}"
            )
            print(
                f"{'Code Revisions:':<20}{snapshot.get('code_revisions', 0)}/{snapshot.get('max_revisions', 0)}"
            )
            tasks = snapshot.get("tasks", [])
            if tasks:
                print(f"\n{'Action Items'} ({len(tasks)} total):")
                for t in tasks:
                    status_color = (
                        Colors.GREEN
                        if t.get("status") == "completed"
                        else Colors.WARNING
                    )
                    print(
                        f" - [{status_color}{t.get('status')}{Colors.ENDC}] {t.get('id')}: {t.get('description', '')}"
                    )
            else:
                print(f"\n{'No tasks parsed yet.'}")
            holder = RunLock.read_holder(orchestrator.ai_dir)
            print(
                f"\nRunning command: {holder.get('command')} (PID {holder.get('pid')})"
                if holder
                else "\nRunning command: none"
            )
            pause_pending = orchestrator.pause_request_path.exists()
            print(f"Pause request: {'pending' if pause_pending else 'none'}")
            print("Next actions:")
            for action in snapshot.get("next_actions", []):
                print(f" - {action['command']}: {action['description']}")
            delivery_preview = snapshot.get("delivery", {}).get("preview")
            if delivery_preview:
                print(f"\n{delivery_preview}")
        except (FileNotFoundError, json.JSONDecodeError):
            log_error(
                "No valid status snapshot. Run 'python3 orchestrator.py init' first."
            )
    elif args.command == "approve":
        try:
            orchestrator.load_config_and_state()
            if orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                log_error(
                    f"Cannot approve from state: {orchestrator.state.get('state')}"
                )
                return
            if not apply_approve(orchestrator, args.clear_route_failures):
                return
            if args.run:
                orchestrator.run_to_end()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "review":
        try:
            should_run = False
            with orchestrator.execution_lock():
                orchestrator.load_config_and_state()
                if orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                    log_error(
                        f"Cannot review from state: {orchestrator.state.get('state')}"
                    )
                    return
                apply_review_decision(
                    orchestrator, args.decision, args.reset_revisions
                )
                should_run = args.run and args.decision != "reject"
                if should_run:
                    orchestrator.run_to_end()
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "continue":
        try:
            orchestrator.load_config_and_state()
            state = orchestrator.state.get("state")
            if state == "COMPLETED":
                if orchestrator.state.get("delivery", {}).get("delivered"):
                    log_success("Project already delivered; nothing to continue.")
                else:
                    orchestrator.step()
                return
            if state == "FAILED":
                log_error(
                    "Project failed; nothing to continue. Use 'reject' (keep the "
                    "prompt, re-plan) or 'reset' (start over) instead."
                )
                return
            if state == "WAITING_FOR_OWNER":
                decision, reset_revisions = pick_continue_decision(orchestrator)
                if decision == "approve":
                    if not apply_approve(orchestrator, args.clear_route_failures):
                        return
                else:
                    if args.clear_route_failures:
                        active_task_id = orchestrator.state.get("active_task_id")
                        if active_task_id:
                            clear_task_route_failures(
                                orchestrator, active_task_id, clear_contract=True
                            )
                    apply_review_decision(orchestrator, "revise", reset_revisions)
            orchestrator.run_to_end()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "reject":
        try:
            with orchestrator.execution_lock():
                orchestrator.load_config_and_state()
                clear_run_state(orchestrator, "PLANNING")
                orchestrator.save_state()
                log_success(
                    "Project sent back to PLANNING; the original request prompt was kept."
                )
                if args.run:
                    orchestrator.run_to_end()
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "reset":
        try:
            with orchestrator.execution_lock():
                orchestrator.load_config_and_state()
                clear_run_state(orchestrator, args.state)
                if orchestrator.request_path.exists():
                    orchestrator.request_path.unlink()
                orchestrator.save_state()
            log_success(
                f"State reset to {args.state}; the request prompt was cleared -- "
                "run 'start' again before 'run'."
            )
        except RuntimeError as error:
            log_error(str(error))
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "set-backend":
        try:
            orchestrator.load_config_and_state()
            roles = (
                ["developer_senior", "developer_middle", "developer_junior"]
                if args.role == "developer"
                else ["qa_senior", "qa_middle", "qa_junior"]
                if args.role == "qa"
                else [args.role]
            )
            for role in roles:
                orchestrator.config["backends"][role] = args.backend
            with open(orchestrator.config_path, "w", encoding="utf-8") as f:
                json.dump(orchestrator.config, f, indent=2)
            log_success(
                f"Successfully configured '{args.role}' backend to '{args.backend}'"
            )
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
    elif args.command == "set-ollama-url":
        orchestrator.init_project()
        orchestrator.load_config_and_state()
        orchestrator.config["ollama_url"] = args.url.rstrip("/")
        with open(orchestrator.config_path, "w", encoding="utf-8") as f:
            json.dump(orchestrator.config, f, indent=2)
        log_success(f"Ollama API URL set to {orchestrator.config['ollama_url']}")
    elif args.command == "weights":
        try:
            orchestrator.load_config_and_state()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
            return
        if args.weights_command == "set":
            if not (scoring.WEIGHT_MIN <= args.value <= scoring.WEIGHT_MAX):
                log_error(
                    f"Weight must be between {scoring.WEIGHT_MIN} and {scoring.WEIGHT_MAX}."
                )
                return
            orchestrator.config.setdefault("worker_weights", {}).setdefault(
                args.role, {}
            )[args.level] = args.value
            with open(orchestrator.config_path, "w", encoding="utf-8") as f:
                json.dump(orchestrator.config, f, indent=2)
            log_success(f"Set {args.role}/{args.level} trust weight to {args.value}")
        else:
            log_header("INTERNAL STAFF TRUST WEIGHTS")
            for role, levels in orchestrator.config.get("worker_weights", {}).items():
                for level, weight in levels.items():
                    print(f" - {role}/{level}: {weight}")
    elif args.command == "roster":
        try:
            orchestrator.load_config_and_state()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
            return
        from orchestrator.core import roster

        data = roster.snapshot(orchestrator)
        log_header("TODAY'S ROSTER")
        for role, info in data["available_roles"].items():
            status_color = (
                Colors.GREEN if info["status"] == "available" else Colors.FAIL
            )
            score = info.get("weight", info.get("rating"))
            print(
                f" - {role} ({info['type']}, backend={info['backend']}): {status_color}{info['status']}{Colors.ENDC} score={score}"
            )
        if data["unavailable_backends"]:
            print(f"\nUnavailable backends: {', '.join(data['unavailable_backends'])}")
    elif args.command == "vendors":
        try:
            orchestrator.load_config_and_state()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
            return
        log_header("VENDOR SCORECARD")
        vendor_scores = orchestrator.config.get("vendor_scores", {})
        if not vendor_scores:
            print("No vendor activity recorded yet.")
        for key, info in vendor_scores.items():
            print(
                f" - {key}: rating={info.get('rating')} jobs={info.get('jobs')} failures={info.get('failures')}"
            )
    elif args.command == "brain":
        try:
            orchestrator.load_config_and_state()
        except FileNotFoundError:
            log_error(
                "Project not initialized. Please run 'python3 orchestrator.py init' first."
            )
            return
        from orchestrator.core import brain

        if args.brain_command == "search":
            results = brain.recall(orchestrator, args.query, limit=10)
            if not results:
                print("No matching memories.")
            for item in results:
                print(f"- {item}\n")
        elif args.brain_command == "prune":
            deleted = brain.prune(orchestrator, keep_last=args.keep)
            log_success(
                f"Pruned {deleted} old memories; kept the most recent {args.keep}."
            )
        else:
            log_error("Specify a brain subcommand: search or prune.")
    else:
        parser.print_help()


_LOCKED_COMMANDS = {
    "init",
    "start",
    "step",
    "run",
    "continue",
    "approve",
    "review",
    "reject",
    "reset",
    "deliver",
    "set-backend",
    "set-ollama-url",
}


def main():
    from orchestrator.core.runlock import RunLock, RunLockBusy

    command = sys.argv[1] if len(sys.argv) > 1 else None
    locked = (
        command in _LOCKED_COMMANDS
        or (command == "weights" and len(sys.argv) > 2 and sys.argv[2] == "set")
        or (command == "brain" and len(sys.argv) > 2 and sys.argv[2] == "prune")
    )
    if not locked:
        return _run_cli()

    orchestrator = AgentOrchestrator(Path(os.getcwd()))
    try:
        with RunLock(orchestrator.ai_dir, command):
            return _run_cli(orchestrator)
    except RunLockBusy as error:
        holder = error.holder_info
        pid = holder.get("pid", "?")
        started = holder.get("started_at", "unknown time")
        active_command = holder.get("command", "unknown command")
        log_error(
            f"Another command is running: {active_command} (PID {pid}, started {started})."
        )
        return 1


if __name__ == "__main__":
    main()
