import os
import sys
import argparse
import json
from pathlib import Path
from orchestrator.core.state import AgentOrchestrator, Colors, log_success, log_info, log_error, log_header

def main():
    parser = argparse.ArgumentParser(description="Multi-Agent Orchestrator CLI")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    subparsers.add_parser("init", help="Initialize the .ai-company folder and configuration")

    start_parser = subparsers.add_parser("start", help="Start a new multi-agent task")
    start_parser.add_argument("prompt", type=str, help="The development task prompt or request")

    subparsers.add_parser("step", help="Run the next step in the state machine")
    subparsers.add_parser("run", help="Run the agent loop to completion")
    subparsers.add_parser("status", help="Get current orchestrator status and logs info")

    reset_parser = subparsers.add_parser("reset", help="Reset the orchestrator state")
    reset_parser.add_argument("--state", type=str, default="PLANNING", help="Reset state to specific value (default: PLANNING)")

    backend_parser = subparsers.add_parser("set-backend", help="Set the agent backend for a role")
    backend_parser.add_argument("role", choices=["manager", "architect", "developer", "reviewer", "qa", "assistant", "ra", "security", "sales", "sre"], help="The agent role")
    backend_parser.add_argument("backend", choices=["ollama", "codex", "claude", "gemini", "agy"], help="The backend to use")

    args = parser.parse_args()

    workspace = Path(os.getcwd())
    orchestrator = AgentOrchestrator(workspace)

    if args.command == "init":
        orchestrator.init_project()
    elif args.command == "start":
        orchestrator.init_project()
        orchestrator.load_config_and_state()

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
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
