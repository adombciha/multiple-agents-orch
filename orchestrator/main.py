import os
import sys
import argparse
import json
from urllib.error import URLError
from urllib.request import urlopen
from pathlib import Path
from orchestrator.core.state import AgentOrchestrator, Colors, log_success, log_info, log_error, log_header


def ollama_available(url: str) -> bool:
    try:
        with urlopen(f"{url.rstrip('/')}/api/tags", timeout=2):
            return True
    except (OSError, URLError):
        return False

def main():
    parser = argparse.ArgumentParser(description="Multi-Agent Orchestrator CLI")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    subparsers.add_parser("init", help="Initialize the .ai-company folder and configuration")

    start_parser = subparsers.add_parser("start", help="Start a new multi-agent task")
    start_parser.add_argument("prompt", type=str, help="The development task prompt or request")
    start_parser.add_argument("--image", action="append", default=[], help="Screenshot or mockup path for UI/UX visual review; repeat for multiple images")

    subparsers.add_parser("step", help="Run the next step in the state machine")
    subparsers.add_parser("run", help="Run the agent loop to completion")
    subparsers.add_parser("status", help="Get current orchestrator status and logs info")
    approve_parser = subparsers.add_parser("approve", help="Approve a workflow paused for owner review")
    approve_parser.add_argument("--run", action="store_true", help="Continue the workflow after approval")
    review_parser = subparsers.add_parser("review", help="Decide a paused human review")
    review_parser.add_argument("decision", choices=["pass", "revise", "reject"])
    review_parser.add_argument("--run", action="store_true", help="Continue the workflow after the decision")

    reset_parser = subparsers.add_parser("reset", help="Reset the orchestrator state")
    reset_parser.add_argument("--state", type=str, default="PLANNING", help="Reset state to specific value (default: PLANNING)")

    backend_parser = subparsers.add_parser("set-backend", help="Set the agent backend for a role")
    backend_parser.add_argument("role", choices=["manager", "architect", "developer", "reviewer", "qa", "assistant", "ra", "security", "sales", "sre", "devops", "uiux", "uiux_visual_review", "fae", "integration"], help="The agent role")
    backend_parser.add_argument("backend", choices=["ollama", "codex", "claude", "gemini", "agy"], help="The backend to use")

    ollama_url_parser = subparsers.add_parser("set-ollama-url", help="Set the Ollama API URL")
    ollama_url_parser.add_argument("url", help="Ollama API base URL, for example http://172.17.144.1:11434")

    args = parser.parse_args()

    workspace = Path(os.getcwd())
    orchestrator = AgentOrchestrator(workspace)

    if args.command == "init":
        orchestrator.init_project()
        orchestrator.load_config_and_state()
        local_url = orchestrator.config["ollama_url"]
        if ollama_available(local_url):
            log_success(f"Ollama is available at {local_url}")
        else:
            windows_url = f"http://{orchestrator.get_windows_host_ip()}:11434"
            if ollama_available(windows_url):
                log_info(f"Windows Ollama detected at {windows_url}")
                if sys.stdin.isatty() and input("Use this Windows Ollama endpoint? [Y/n] ").strip().lower() not in {"n", "no"}:
                    orchestrator.config["ollama_url"] = windows_url
                    orchestrator.config_path.write_text(json.dumps(orchestrator.config, indent=2), encoding="utf-8")
                    log_success(f"Ollama API URL set to {windows_url}")
            else:
                log_error("No Ollama endpoint detected. Start Ollama locally or on the Windows host before running a task.")
    elif args.command == "start":
        orchestrator.init_project()
        orchestrator.load_config_and_state()
        allowed_image_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
        max_image_bytes = orchestrator.config.get("max_visual_image_bytes", 10 * 1024 * 1024)
        image_paths = []
        for path in map(Path, args.image):
            if not path.is_file() or path.suffix.lower() not in allowed_image_suffixes or path.stat().st_size > max_image_bytes:
                log_error(f"Each --image must be a PNG, JPEG, or WebP file no larger than {max_image_bytes // 1024 // 1024}MB.")
                return
            image_paths.append(str(path.resolve()))

        # Save request prompt
        with open(orchestrator.request_path, "w", encoding="utf-8") as f:
            f.write(args.prompt)
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
            "quota_exhausted_backends": {},
            "failed_model_routes": [],
            "visual_image_paths": image_paths,
        }
        orchestrator.save_state()
        log_success("Orchestrator initialized and ready to run. Run 'python3 orchestrator.py run' to execute.")
    elif args.command == "step":
        orchestrator.step()
    elif args.command == "run":
        orchestrator.run_to_end()
    elif args.command == "status":
        try:
            orchestrator.load_config_and_state()
            log_header("ORCHESTRATOR STATUS")
            print(f"{'Current State:':<20}{Colors.BOLD}{orchestrator.state['state']}{Colors.ENDC}")
            print(f"{'Plan Revisions:':<20}{orchestrator.state['plan_revisions']}/{orchestrator.config['max_revisions']}")
            print(f"{'Code Revisions:':<20}{orchestrator.state['code_revisions']}/{orchestrator.config['max_revisions']}")
            print(f"Ollama Model:       {orchestrator.config['ollama_model']}")
            print(f"{'Developer Backend:':<20}{orchestrator.config['backends']['developer']}")
            print(f"{'Reviewer Backend:':<20}{orchestrator.config['backends']['reviewer']}")
            print(f"{'QA Backend:':<20}{orchestrator.config['backends'].get('qa', 'ollama')}")
            print(f"{'Test Command:':<20}{orchestrator.config['test_command']}")

            tasks = orchestrator.state.get("tasks", [])
            if tasks:
                print(f"\n{'Action Items'} ({len(tasks)} total):")
                for t in tasks:
                    status_color = Colors.GREEN if t['status'] == 'completed' else Colors.WARNING
                    print(f" - [{status_color}{t['status']}{Colors.ENDC}] {t['id']}: {t['description']}")
            else:
                print(f"\n{'No tasks parsed yet.'}")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    elif args.command == "approve":
        try:
            orchestrator.load_config_and_state()
            if orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                log_error(f"Cannot approve from state: {orchestrator.state.get('state')}")
                return
            resume_state = orchestrator.state.get("pass_state", orchestrator.state.get("resume_state", "REVIEWING_CODE"))
            orchestrator.state["state"] = resume_state
            orchestrator.state.pop("human_review_source", None)
            orchestrator.state.pop("resume_state", None)
            orchestrator.save_state()
            log_success(f"Owner approval recorded; workflow resumed at {resume_state}.")
            if args.run:
                orchestrator.run_to_end()
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    elif args.command == "review":
        try:
            orchestrator.load_config_and_state()
            if orchestrator.state.get("state") != "WAITING_FOR_OWNER":
                log_error(f"Cannot review from state: {orchestrator.state.get('state')}")
                return
            decision = args.decision
            if decision == "pass":
                next_state = orchestrator.state.get("pass_state", "REVIEWING_CODE")
            elif decision == "revise":
                next_state = "IMPLEMENTING"
                n = orchestrator.state.get("code_revisions", 0) + 1
                orchestrator.state["code_revisions"] = n
                orchestrator.state.setdefault("tasks", []).append({
                    "id": f"HUMAN-REVIEW-{n}",
                    "description": f"Implement human review revisions:\n{orchestrator.state.get('human_review_details', '')[:2000]}",
                    "status": "pending",
                    **orchestrator.fix_task_levels(),
                })
            else:
                next_state = "FAILED"
            orchestrator.state["state"] = next_state
            for key in ("human_review_source", "resume_state", "pass_state", "human_review_details"):
                orchestrator.state.pop(key, None)
            orchestrator.save_state()
            log_success(f"Human review '{decision}' recorded; workflow moved to {next_state}.")
            if args.run and decision != "reject":
                orchestrator.run_to_end()
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    elif args.command == "reset":
        try:
            orchestrator.load_config_and_state()
            orchestrator.state["state"] = args.state
            orchestrator.state["plan_revisions"] = 0
            orchestrator.state["code_revisions"] = 0
            orchestrator.state["model_tier_indices"] = {
                "developer": 0,
                "reviewer": 0,
                "manager": 0,
                "qa": 0
            }
            orchestrator.state["specialists"] = []
            orchestrator.state["staffing"] = {}
            orchestrator.state["worker_assignments"] = {}
            orchestrator.state["last_developer_role"] = "developer_senior"
            orchestrator.state["last_qa_level"] = "senior"
            orchestrator.state["developer_promotions"] = {}
            orchestrator.state["quota_exhausted_backends"] = {}
            orchestrator.cleanup_worktree(merge=False)
            orchestrator.save_state()
            log_success(f"State reset to {args.state}")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
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
            log_success(f"Successfully configured '{args.role}' backend to '{args.backend}'")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    elif args.command == "set-ollama-url":
        orchestrator.init_project()
        orchestrator.load_config_and_state()
        orchestrator.config["ollama_url"] = args.url.rstrip("/")
        with open(orchestrator.config_path, "w", encoding="utf-8") as f:
            json.dump(orchestrator.config, f, indent=2)
        log_success(f"Ollama API URL set to {orchestrator.config['ollama_url']}")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
