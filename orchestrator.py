#!/usr/bin/env python3
"""
AI Multi-Agent Orchestrator
Coordinates Ollama (Manager/Orchestrator), Codex (Developer), and Claude Code (Reviewer/Architect)
to manage software development workflows inside WSL.
"""

from __future__ import annotations
import os
import sys
import json
import argparse
import subprocess
from pathlib import Path
import requests

# Default Configuration
DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    "ollama_model": "gemma4:latest",
    "test_command": "git diff --stat",  # Runs a simple check if no test suite exists
    "max_revisions": 2,
    "backends": {
        "manager": "codex",
        "architect": "agy",
        "developer": "codex",
        "reviewer": "codex",
        "qa": "codex",
        "developer_senior": "codex",
        "developer_middle": "codex",
        "developer_junior": "agy",
        "qa_senior": "codex",
        "qa_middle": "codex",
        "qa_junior": "agy",
        "assistant": "ollama",
        "ra": "agy",
        "security": "ollama",
        "sales": "ollama",
        "sre": "agy"
    },
    "use_ponytail": False,  # Enforces minimalist senior developer/reviewer principles (YAGNI)
    "use_worktree": True,   # Enforces isolated git worktrees for agent roles
    "backend_escalation_path": ["ollama", "agy", "codex"],
    "model_tiers": {
        "ollama": ["gemma4:latest", "gemma2:2b", "gemma2:9b"],
        "agy": ["gemini-3.5-flash", "gemini-3.1-pro"],
        "codex": ["gpt-5.6-sol"],
        "claude": ["claude-3-5-haiku", "claude-3-7-sonnet"]
    },
    "role_models": {
        "developer_senior": "gpt-5.6-terra",
        "developer_middle": "gpt-5.6-luna",
        "developer_junior": "gemini-3.5-flash",
        "qa_senior": "gpt-5.6-terra",
        "qa_middle": "gpt-5.6-luna",
        "qa_junior": "gemini-3.5-flash",
        "assistant": "gemma4:latest",
        "architect": "gemini-3.1-pro",
        "ra": "gemini-3.1-pro",
        "security": "deepseek-r1:latest",
        "sales": "qwen2.5:latest",
        "sre": "gemini-3.1-pro"
    },
    "staffing_limits": {
        "rd": {"senior": 1, "middle": 2, "junior": 3},
        "qa": {"senior": 1, "middle": 2, "junior": 3}
    }
}


PONYTAIL_PROMPT = (
    "\n\n[PONYTAIL RULE ACTIVE]\n"
    "Enforce the laziest solution that actually works, simplest, shortest, most minimal. "
    "Channel a senior developer who has seen everything:\n"
    "- Climb the ladder:\n"
    "  1. Does this need to exist at all? (YAGNI)\n"
    "  2. Already in this codebase? Reuse it. Look before you write.\n"
    "  3. Stdlib does it? Use it.\n"
    "  4. Native platform feature covers it? Use it.\n"
    "  5. Already-installed dependency solves it? Use it.\n"
    "  6. Can it be one line? One line.\n"
    "  7. Only then: the minimum code that works.\n"
    "- No unrequested abstractions: no interface with one implementation, no factory for one product, no config for a value that never changes.\n"
    "- Deletion over addition. Shortest working diff wins.\n"
    "- Non-trivial logic must leave one runnable check behind."
)

# ANSI Escape Colors for prettier logging
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def log_info(msg: str):
    print(f"{Colors.BLUE}[*] {msg}{Colors.ENDC}")

def log_success(msg: str):
    print(f"{Colors.GREEN}[+] {msg}{Colors.ENDC}")

def log_warning(msg: str):
    print(f"{Colors.WARNING}[!] {msg}{Colors.ENDC}")

def log_error(msg: str):
    print(f"{Colors.FAIL}[-] {msg}{Colors.ENDC}")

def log_header(msg: str):
    print(f"\n{Colors.HEADER}{Colors.BOLD}=== {msg} ==={Colors.ENDC}")

class AgentOrchestrator:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.ai_dir = workspace / ".ai-company"
        
        # Check if we are in a git repository
        import subprocess
        try:
            result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.workspace, capture_output=True, text=True)
            self.has_git = (result.returncode == 0 and result.stdout.strip() == "true")
        except Exception:
            self.has_git = False
            
        self.base_branch = "master"
        if self.has_git:
            try:
                result = subprocess.run(["git", "branch", "--show-current"], cwd=self.workspace, capture_output=True, text=True)
                if result.returncode == 0 and result.stdout.strip():
                    self.base_branch = result.stdout.strip()
                else:
                    self.base_branch = "HEAD"
            except Exception:
                self.base_branch = "HEAD"
        self.config_path = self.ai_dir / "config.json"
        self.state_path = self.ai_dir / "state.json"
        
        # Files for data sharing
        self.request_path = self.ai_dir / "request.md"
        self.requirements_path = self.ai_dir / "requirements.md"
        self.plan_path = self.ai_dir / "implementation_plan.md"
        self.action_items_path = self.ai_dir / "action_items.json"
        self.reviewer_output_path = self.ai_dir / "reviewer_output.md"
        self.developer_output_path = self.ai_dir / "developer_output.md"
        self.test_results_path = self.ai_dir / "test_results.txt"
        self.qa_report_path = self.ai_dir / "qa_report.md"
        self.specialist_review_path = self.ai_dir / "specialist_reviews.md"
        self.final_report_path = self.ai_dir / "final_report.md"

        self.config = DEFAULT_CONFIG
        self.state = {
            "state": "PLANNING",
            "plan_revisions": 0,
            "code_revisions": 0,
            "model_tier_indices": {
                "developer": 0,
                "reviewer": 0,
                "manager": 0,
                "qa": 0
            },
            "tasks": [],
            "specialists": [],
            "staffing": {"rd": {"senior": 1, "middle": 0, "junior": 0}, "qa": {"senior": 1, "middle": 0, "junior": 0}}
        }

    def init_project(self):
        """Initializes the .ai-company folder and configuration files."""
        self.ai_dir.mkdir(exist_ok=True)
        
        # Write config if not exists
        if not self.config_path.exists():
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            log_success(f"Created config: {self.config_path}")
        else:
            log_info(f"Config already exists: {self.config_path}")

        # Write initial state if not exists
        if not self.state_path.exists():
            self.save_state()
            log_success(f"Created state file: {self.state_path}")
        else:
            log_info(f"State file already exists: {self.state_path}")

        # Suggest Windows Host IP for Ollama setup
        host_ip = self.get_windows_host_ip()
        log_info(f"Suggested Windows Host IP: {host_ip}")
        log_info(f"If Ollama is running on Windows, set 'ollama_url' in config.json to 'http://{host_ip}:11434'")

    def load_config_and_state(self):
        """Loads configuration and state files from disk."""
        if not self.config_path.exists():
            raise FileNotFoundError("Project not initialized. Please run 'init' first.")
            
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
            
        with open(self.state_path, "r", encoding="utf-8") as f:
            self.state = json.load(f)

    def save_state(self):
        """Saves current state to state.json."""
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    def get_windows_host_ip(self) -> str:
        """Finds the default route gateway (usually the Windows Host IP in WSL)."""
        try:
            result = subprocess.run(
                ["ip", "route", "show"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            for line in result.stdout.splitlines():
                if "default via" in line:
                    parts = line.split()
                    if len(parts) > 2:
                        return parts[2]
        except Exception:
            pass
        return "127.0.0.1"

    def run_command(self, cmd: list[str], timeout: int = 1800, capture: bool = True, cwd: Path | None = None) -> tuple[int, str]:
        """Runs a subprocess command safety and returns (returncode, output)."""
        exec_cwd = cwd or self.workspace
        
        # If use_worktree is active and we are executing in implementing/testing states,
        # redirect default cwd to the worktree path
        if self.config.get("use_worktree", True) and not cwd:
            current_state = self.state.get("state", "PLANNING")
            if current_state in ["IMPLEMENTING", "TESTING", "REVIEWING_CODE"]:
                wt_path = self.ai_dir / "worktree"
                if wt_path.exists():
                    exec_cwd = wt_path

        try:
            result = subprocess.run(
                cmd,
                cwd=exec_cwd,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                text=True,
                timeout=timeout,
                check=False
            )
            output = ""
            if capture:
                output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            return result.returncode, output
        except subprocess.TimeoutExpired as e:
            return -1, f"Timeout expired: {e}"
        except Exception as e:
            return -1, f"Error running command: {e}"

    # Agent backends callers
    def call_ollama(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        url = f"{self.config['ollama_url']}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        model = self.get_active_model_for_role(role, "ollama") or self.config.get("ollama_model", "gemma2:2b")
        log_info(f"Ollama calling model: {model}")
        payload = {
            "model": model,
            "messages": messages,
            "stream": False
        }
        
        try:
            response = requests.post(url, json=payload, timeout=600)
            response.raise_for_status()
            return response.json()["message"]["content"]
        except requests.exceptions.RequestException as e:
            log_error(f"Failed to communicate with Ollama at {url}.")
            log_error(f"Error detail: {e}")
            raise RuntimeError(f"Ollama connection failed: {e}")

    def call_codex(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        # Prepend system prompt to the user prompt if present
        full_prompt = ""
        if system_prompt:
            full_prompt += f"System Instructions:\n{system_prompt}\n\n"
        full_prompt += prompt

        # Write prompt to a temp file to avoid shell argument length limits
        temp_prompt_file = self.ai_dir / "temp_codex_prompt.txt"
        with open(temp_prompt_file, "w", encoding="utf-8") as f:
            f.write(full_prompt)

        cmd = [
            "codex", "exec", 
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check"
        ]
        
        model = self.get_active_model_for_role(role, "codex")
        if model:
            cmd.extend(["-m", model])
        cmd.append("-")
        
        log_info(f"Running Codex: {' '.join(cmd)}")
        try:
            # Send prompt via stdin
            with open(temp_prompt_file, "r", encoding="utf-8") as pf:
                result = subprocess.run(
                    cmd,
                    cwd=self.workspace,
                    stdin=pf,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=1800,
                    check=False
                )
            
            # Clean up temp file
            if temp_prompt_file.exists():
                temp_prompt_file.unlink()

            if result.returncode != 0:
                raise RuntimeError(f"Codex failed with code {result.returncode}:\n{result.stderr}")
            return result.stdout
        except Exception as e:
            if temp_prompt_file.exists():
                temp_prompt_file.unlink()
            raise e

    def call_claude(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        full_prompt = ""
        if system_prompt:
            full_prompt += f"{system_prompt}\n\n"
        full_prompt += prompt

        cmd = ["claude", "--print", "--dangerously-skip-permissions"]
        model = self.get_active_model_for_role(role, "claude")
        if model:
            cmd.extend(["--model", model])
        cmd.append(full_prompt)
        
        log_info(f"Running Claude Code: {' '.join(cmd[:-1])} ...")
        result = subprocess.run(
            cmd,
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
            check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI failed with code {result.returncode}:\n{result.stderr}")
        return result.stdout


    def call_agy(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        full_prompt = ""
        if system_prompt:
            full_prompt += f"System Instructions:\n{system_prompt}\n\n"
        full_prompt += prompt

        cmd = ["agy"]
        model = self.get_active_model_for_role(role, "agy")
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["--print", full_prompt])
        
        log_info(f"Running agy: {' '.join(cmd[:-1])} ...")
        result = subprocess.run(
            cmd,
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
            check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"agy CLI failed with code {result.returncode}:\n{result.stderr}")
        return result.stdout

    def call_manager(self, prompt: str, system_prompt: str | None = None) -> str:
        backend = self.config["backends"].get("manager", "ollama")
        log_info(f"Requesting Agent 'manager' (Backend: {backend})...")
        
        # Inject ponytail prompt if enabled
        if self.config.get("use_ponytail", False):
            if system_prompt:
                system_prompt += PONYTAIL_PROMPT
            else:
                system_prompt = PONYTAIL_PROMPT.strip()

        if backend == "codex":
            try:
                return self.call_codex(prompt, system_prompt, role="manager")
            except Exception as e:
                log_warning(f"Codex manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt, role="manager")
        elif backend == "claude":
            try:
                return self.call_claude(prompt, system_prompt, role="manager")
            except Exception as e:
                log_warning(f"Claude manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt, role="manager")
        elif backend == "agy":
            try:
                return self.call_agy(prompt, system_prompt, role="manager")
            except Exception as e:
                log_warning(f"agy manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt, role="manager")
        else:
            return self.call_ollama(prompt, system_prompt, role="manager")

    def call_agent_ollama_fallback(self, role: str, prompt: str, system_prompt: str | None = None) -> str:
        """Helper to call Ollama and append format instructions if missing for the developer role."""
        if role.startswith("developer") and "[FILE_START:" not in prompt:
            prompt += (
                "\n\nCRITICAL: You do not have access to terminal tools. "
                "To modify files, you must output your changes using this exact block format:\n"
                "[FILE_START: path/to/file.ext]\n"
                "full file content here\n"
                "[FILE_END: path/to/file.ext]\n\n"
                "Any modifications outside this format will be ignored. Write only complete file contents inside the blocks."
            )
        return self.call_ollama(prompt, system_prompt, role=role)

    def call_agent(self, role: str, prompt: str, system_prompt: str | None = None) -> str:
        backend = self.config["backends"].get(role, "ollama")
        log_info(f"Requesting Agent '{role}' (Backend: {backend})...")
        
        # Inject ponytail prompt if enabled and role is a developer or reviewer.
        if self.config.get("use_ponytail", False) and (role.startswith("developer") or role == "reviewer"):
            if system_prompt:
                system_prompt += PONYTAIL_PROMPT
            else:
                system_prompt = PONYTAIL_PROMPT.strip()

        if backend == "claude":
            try:
                return self.call_claude(prompt, system_prompt, role=role)
            except Exception as e:
                log_warning(f"Claude backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        elif backend == "codex":
            try:
                return self.call_codex(prompt, system_prompt, role=role)
            except Exception as e:
                log_warning(f"Codex backend failed: {e}")
                log_warning("Falling open to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        elif backend == "agy":
            try:
                return self.call_agy(prompt, system_prompt, role=role)
            except Exception as e:
                log_warning(f"agy backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        else:
            return self.call_agent_ollama_fallback(role, prompt, system_prompt)

    # Workflow Steps
    def setup_worktree(self):
        """Creates an isolated git worktree for development and QA testing."""
        if not self.config.get("use_worktree", True) or not self.has_git:
            return
            
        wt_path = self.ai_dir / "worktree"
        
        # 1. Clean up any existing worktree/branch first
        self.cleanup_worktree()
        
        # 2. Add worktree
        log_info(f"Setting up isolated Git worktree at {wt_path} on branch 'ai-feature-branch'...")
        wt_path.parent.mkdir(exist_ok=True, parents=True)
        
        # Run git worktree add
        code, output = self.run_command(["git", "worktree", "add", "-b", "ai-feature-branch", str(wt_path)], cwd=self.workspace)
        if code != 0:
            log_error(f"Failed to create git worktree: {output}")
            raise RuntimeError("Git worktree setup failed.")
        log_success("Git worktree successfully established.")

    def cleanup_worktree(self, merge=False):
        """Cleans up the git worktree, optionally merging the changes back first."""
        if not self.config.get("use_worktree", True) or not self.has_git:
            return
            
        wt_path = self.ai_dir / "worktree"
        
        # If merge is requested, merge the branch in root
        if merge:
            code, _ = self.run_command(["git", "show-ref", "--verify", "--quiet", "refs/heads/ai-feature-branch"], cwd=self.workspace)
            branch_exists = (code == 0)
            if branch_exists:
                log_info(f"Merging 'ai-feature-branch' back into {self.base_branch}...")
                
                # Commit any changes inside the worktree first if they are not committed
                # We want to add and commit
                self.run_command(["git", "add", "."], cwd=wt_path)
                self.run_command(["git", "commit", "-m", "AI Auto-commit before merge"], cwd=wt_path)
                
                code, output = self.run_command(["git", "merge", "ai-feature-branch"], cwd=self.workspace)
                if code != 0:
                    log_error(f"Failed to merge feature branch due to conflict: {output}")
                    log_warning("ABORTING WORKTREE CLEANUP! Please resolve the git merge conflict manually in your root workspace.")
                    log_warning("The 'ai-feature-branch' and your worktree are preserved.")
                    return
                else:
                    log_success(f"Successfully merged changes to {self.base_branch}!")
                
        # Remove worktree
        wt_list = self.run_command(["git", "worktree", "list"], capture=True)[1]
        if "worktree" in wt_list:
            log_info("Removing Git worktree...")
            self.run_command(["git", "worktree", "remove", "--force", str(wt_path)], cwd=self.workspace)
            
        # Delete branch
        self.run_command(["git", "branch", "-D", "ai-feature-branch"], cwd=self.workspace)

    def step_planning(self):
        log_header("1. PLANNING (Ollama Manager)")
        
        # Setup clean worktree
        self.setup_worktree()
        
        if not self.request_path.exists():
            log_error(f"No request file found at {self.request_path}. Please run 'start' command first.")
            sys.exit(1)
            
        with open(self.request_path, "r", encoding="utf-8") as f:
            request = f.read()

        system_prompt = """You are the Project Manager of an AI software company. Your job is to analyze the user's request and write a detailed, clear requirements document in Markdown format.\nRequirements must contain:\n1. Project Overview & Context\n2. Specific Functional Requirements\n3. Technical Specifications & Stack constraints\n4. Scope boundaries (what is NOT included)\n\nOutput ONLY the Markdown content for requirements.md. Do not add any greeting, preamble, or conversational introduction."""
        
        requirements = self.call_manager(request, system_prompt)
        
        # Save requirements
        with open(self.requirements_path, "w", encoding="utf-8") as f:
            f.write(requirements)
            
        log_success(f"Requirements generated and saved to {self.requirements_path}")
        self.state["state"] = "DEVELOPING_PLAN"
        self.save_state()

    def step_developing_plan(self):
        log_header("2. DEVELOPING PLAN (Developer)")
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()

        prompt = (
            f"Please read the following project requirements:\n\n"
            f"{requirements}\n\n"
            f"Draft a step-by-step implementation plan in Markdown. The plan should include:\n"
            f"1. Target files to create or modify.\n"
            f"2. Specific changes or logic details for each file.\n"
            f"3. Sequence of work (action items).\n"
            f"4. Testing strategy.\n\n"
            f"Write ONLY the Markdown implementation plan. Do not include any conversational preamble or postscript."
        )

        if self.state["plan_revisions"] > 0 and self.reviewer_output_path.exists():
            with open(self.reviewer_output_path, "r", encoding="utf-8") as f:
                feedback = f.read()
            prompt = (
                f"Your previous implementation plan was REJECTED by the reviewer with feedback:\n\n"
                f"{feedback}\n\n"
                f"Please revise the implementation plan to address all reviewer comments.\n"
                f"Write the complete updated implementation plan in Markdown. Only output the plan content."
            )

        system_prompt = "You are a Lead Software Developer. Generate a clear step-by-step implementation plan."
        plan = self.call_agent("developer", prompt, system_prompt)

        with open(self.plan_path, "w", encoding="utf-8") as f:
            f.write(plan)
        log_success(f"Implementation plan generated and saved to {self.plan_path}")

        # Manager assigns tasks and scales the RD/QA team within configured capacity.
        log_info("Parsing implementation plan into structured action items...")
        capacity = self.config.get("staffing_limits", {})
        capabilities = {
            "backends": self.config.get("backends", {}),
            "model_tiers": self.config.get("model_tiers", {}),
        }
        parse_prompt = f"""Read this implementation plan:\n\n{plan}\n\nCreate a JSON object with:\n- 'tasks': a flat array of coding tasks. Each has 'id', 'description', 'status': 'pending', 'complexity' ('routine', 'moderate', or 'complex'), and 'assignee_level' ('junior', 'middle', or 'senior'). Assign isolated repetitive changes to junior; ordinary feature work with known patterns to middle; architecture, cross-module, security, data migration, ambiguity, or design work to senior.\n- 'staffing': an allocation based on task count/scope, available capacity, and capabilities below. Include a senior for complex work, middle agents for moderate work, and juniors only for safely separable routine work.\n- 'specialists': only include roles relevant to this project: 'sales' for unclear business requirements, 'security' for auth/secrets/payment/PII/attack surface, 'ra' for laws/regulations/healthcare/financial compliance, and 'sre' for deployment/CI/CD/containers/monitoring. Each item has 'role' and a short 'reason'. Do not include a role when it is unnecessary.\n\nAvailable capacity:\n{json.dumps(capacity)}\n\nCapabilities:\n{json.dumps(capabilities)}\n\nThe staffing object must contain rd and qa, each with integer senior, middle, and junior counts. Respond ONLY with valid JSON."""
        
        parsed_items_raw = self.call_manager(parse_prompt, "You are a Project Manager. Output only raw JSON.")
        
        # Clean potential markdown wrapping
        clean_json = parsed_items_raw.strip()
        if clean_json.startswith("```"):
            lines = clean_json.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_json = "\n".join(lines).strip()

        try:
            parsed = json.loads(clean_json)
            tasks = parsed if isinstance(parsed, list) else parsed["tasks"]
            if not isinstance(tasks, list):
                raise ValueError("tasks must be a JSON array")
            if isinstance(parsed, dict):
                self.state["staffing"] = parsed.get("staffing", self.state.get("staffing", {}))
                specialists = parsed.get("specialists", [])
                self.state["specialists"] = [
                    item for item in specialists
                    if isinstance(item, dict) and item.get("role") in {"sales", "security", "ra", "sre"}
                ]
            for task in tasks:
                if task.get("complexity") not in {"routine", "moderate", "complex"}:
                    task["complexity"] = "complex"
                if task.get("assignee_level") not in {"junior", "middle", "senior"}:
                    task["assignee_level"] = "senior"
            # Retain previously completed tasks if we are revising
            existing_completed = {t["id"] for t in self.state["tasks"] if t.get("status") == "completed"}
            for t in tasks:
                if t["id"] in existing_completed:
                    t["status"] = "completed"
            self.state["tasks"] = tasks
            self.save_state()
            with open(self.action_items_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2)
            log_success(f"Saved {len(tasks)} tasks to {self.action_items_path}")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log_warning(f"Could not parse tasks as JSON. Saving raw output. Error: {e}")
            log_warning(f"Raw manager output was: {parsed_items_raw}")
            # Write a fallback task
            fallback_tasks = [{"id": "TASK-001", "description": "Implement overall implementation plan", "status": "pending"}]
            self.state["tasks"] = fallback_tasks
            self.save_state()

        self.state["state"] = "REVIEWING_PLAN"
        self.save_state()

    def get_active_model_for_role(self, role: str, backend: str) -> str | None:
        """Returns the specific active model name for a role/backend based on the current scaling tier index."""
        role_model = self.config.get("role_models", {}).get(role)
        if role_model:
            return role_model

        # Ensure model_tier_indices exist in state
        indices = self.state.setdefault("model_tier_indices", {})
        if role == "assistant" and "assistant" not in indices:
            idx = indices.setdefault(role, 1)  # Default to index 1 (gemma2:2b) for assistant
        else:
            idx = indices.setdefault(role, 0)
        
        # Look up model tiers for this backend
        tiers = self.config.get("model_tiers", {}).get(backend, [])
        if not tiers:
            return None
            
        # Bound index to tiers list
        if idx < len(tiers):
            return tiers[idx]
        return tiers[-1]

    def staffing(self, role: str) -> dict[str, int]:
        """Return a validated staffing allocation within configured capacity."""
        limits = self.config.get("staffing_limits", {}).get(role, {})
        selected = self.state.get("staffing", {}).get(role, {})
        def bounded(level: str) -> int:
            try:
                return max(0, min(int(selected.get(level, 0)), int(limits.get(level, 0))))
            except (TypeError, ValueError):
                return 0

        return {level: bounded(level) for level in ("senior", "middle", "junior")}

    def consult_specialists(self, requirements: str, plan: str) -> str:
        """Collect only the specialist reviews selected by the manager."""
        reports = []
        for specialist in self.state.get("specialists", []):
            role = specialist["role"]
            if role not in {"sales", "security", "ra", "sre"}:
                continue
            prompt = f"""Review this project only for your specialty.\n\nRequirements:\n{requirements}\n\nPlan:\n{plan}\n\nReturn concise risks, missing requirements, and concrete acceptance criteria."""
            report = self.call_agent(role, prompt, f"You are the project's {role.title()} specialist.")
            reports.append(f"## {role.title()}\n{report}")
        notes = "\n\n".join(reports)
        if notes:
            self.specialist_review_path.write_text(notes, encoding="utf-8")
        return notes

    def escalate_developer_backend(self):
        """Escalates the developer dynamically (vertical model upgrade first, then horizontal backend upgrade)."""
        current_dev = self.config["backends"].get("developer", "codex")
        
        # Ensure tracking variables exist
        indices = self.state.setdefault("model_tier_indices", {})
        idx = indices.get("developer", 0)
        
        # Check if we can upgrade the model within the current backend (vertical scaling)
        tiers = self.config.get("model_tiers", {}).get(current_dev, [])
        if idx + 1 < len(tiers):
            # Upgrade model tier
            indices["developer"] = idx + 1
            new_model = tiers[idx + 1]
            log_warning(f"[!] Dynamic Vertical Scale: Upgraded developer model on '{current_dev}' to '{new_model}' (Tier {idx + 1})")
            self.save_state()
            return
            
        # If we cannot upgrade model anymore, perform horizontal escalation (switch backend)
        escalation_path = self.config.get("backend_escalation_path", ["ollama", "agy", "codex"])
        if current_dev in escalation_path:
            curr_idx = escalation_path.index(current_dev)
            if curr_idx + 1 < len(escalation_path):
                new_dev = escalation_path[curr_idx + 1]
                log_warning(f"[!] Dynamic Horizontal Scale: Escalated Developer backend from '{current_dev}' to '{new_dev}'")
                self.config["backends"]["developer"] = new_dev
                # Reset model tier for the new backend
                indices["developer"] = 0
                
                # Save updated config and state
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=2)
                self.save_state()
                return

        log_warning("[!] Already at highest Developer model and backend escalation tier.")

    def step_reviewing_plan(self):
        log_header("3. REVIEWING PLAN (Architect)")
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()
        specialist_notes = self.consult_specialists(requirements, plan)

        prompt = f"""Review the implementation plan against the requirements.\n\nRequirements:\n{requirements}\n\nImplementation Plan:\n{plan}\n\nSpecialist Reviews:\n{specialist_notes or 'None selected for this project.'}\n\nCheck for architectural issues, gaps in requirements, and safety.\nIf acceptable, start your response with 'APPROVED'.\nIf issues exist, start your response with 'REJECTED' followed by detailed feedback.\n\nFormat:\n[APPROVED or REJECTED]\n[Feedback details]"""

        system_prompt = "You are a Senior Software Architect. Review the implementation plan."
        review = self.call_agent("architect", prompt, system_prompt)

        with open(self.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)
        log_info(f"Architect response saved. Preview:\n{review[:200]}...")

        is_approved = review.strip().upper().replace("*", "").startswith("APPROVED")
        
        if is_approved:
            log_success("Implementation plan APPROVED by Architect!")
            self.state["state"] = "IMPLEMENTING"
            self.save_state()
        else:
            log_warning("Implementation plan REJECTED by Architect.")
            self.escalate_developer_backend()
            max_rev = self.config.get("max_revisions", 2)
            if self.state["plan_revisions"] < max_rev:
                self.state["plan_revisions"] += 1
                self.state["state"] = "DEVELOPING_PLAN"
                self.save_state()
                log_info(f"Revising plan (Revision {self.state['plan_revisions']}/{max_rev})...")
            else:
                log_warning("Reached max plan revisions. Proceeding to implementation anyway (Owner override).")
                self.state["state"] = "IMPLEMENTING"
                self.save_state()

    def parse_and_write_files(self, text: str) -> list[str]:
        import re
        pattern = re.compile(r'\[FILE_START:\s*(.*?)\](.*?)\[FILE_END:\s*\1\]', re.DOTALL)
        matches = pattern.findall(text)
        
        written_files = []
        for filepath_str, content in matches:
            filepath_str = filepath_str.strip()
            content = content.strip()
            
            # Strip potential leading/trailing markdown code block wrappers
            if content.startswith("```"):
                lines = content.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                content = "\n".join(lines).strip()
                
            # Determine base directory
            base_dir = self.workspace
            if self.config.get("use_worktree", True):
                wt_path = self.ai_dir / "worktree"
                if wt_path.exists():
                    base_dir = wt_path

            target_path = (base_dir / filepath_str).resolve()
            # Safety check: ensure it is inside base_dir
            if base_dir not in target_path.parents and target_path != base_dir:
                log_warning(f"Skipping file write outside target directory: {filepath_str}")
                continue
                
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
            written_files.append(filepath_str)
            log_success(f"Developer wrote file: {filepath_str}")
        return written_files

    def step_implementing(self):
        log_header("4. IMPLEMENTING CODE (Developer)")
        if not self.requirements_path.exists() or not self.plan_path.exists():
            log_error("Requirements or Plan missing. Cannot implement.")
            sys.exit(1)

        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()

        tasks = self.state.get("tasks", [])
        pending_tasks = [t for t in tasks if t["status"] == "pending"]

        if not pending_tasks:
            log_success("All tasks are already marked completed.")
            self.state["state"] = "TESTING"
            self.save_state()
            return

        log_info(f"Found {len(pending_tasks)} pending tasks out of {len(tasks)} total tasks.")
        
        # We can implement task by task
        developer_logs = []
        rd_staff = self.staffing("rd")
        rd_next = {"senior": 0, "middle": 0, "junior": 0}
        for task in pending_tasks:
            log_info(f"Implementing Task {task['id']}: {task['description']}")
            requested_level = task.get("assignee_level", "senior")
            if requested_level not in {"junior", "middle", "senior"}:
                requested_level = "senior"
            level = requested_level if rd_staff[requested_level] else "senior"
            rd_next[level] += 1
            agent_number = (rd_next[level] - 1) % max(rd_staff[level], 1) + 1
            agent_role = f"developer_{level}"
            backend = self.config["backends"].get(agent_role, self.config["backends"].get("developer", "codex"))
            
            prompt = (
                f"We are implementing the project in the workspace root. Here are the requirements:\n"
                f"```markdown\n{requirements}\n```\n\n"
                f"And here is our implementation plan:\n"
                f"```markdown\n{plan}\n```\n\n"
                f"Please implement the following task:\n"
                f"Task ID: {task['id']}\n"
                f"Description: {task['description']}\n\n"
            )
            
            if backend in ["ollama", "gemini", "agy"]:
                prompt += (
                    "Please write the code for any files that need to be created or modified. "
                    "You MUST wrap the code for each file exactly inside the following file-marker blocks:\n"
                    "[FILE_START: path/to/file.ext]\n"
                    "// code contents here\n"
                    "[FILE_END: path/to/file.ext]\n\n"
                    "Make sure the path is relative to the project root. "
                    "Only output files wrapped in this format will be modified in the repository. "
                    "Explain your changes briefly outside these blocks."
                )
            else:
                prompt += "Modify the code files directly in the repository. Provide details of the changes you make."
            
            if self.state["code_revisions"] > 0:
                feedback = ""
                if self.qa_report_path.exists():
                    with open(self.qa_report_path, "r", encoding="utf-8") as f:
                        feedback += f"\n--- QA Feedback ---\n{f.read()}\n"
                if self.reviewer_output_path.exists():
                    with open(self.reviewer_output_path, "r", encoding="utf-8") as f:
                        feedback += f"\n--- Code Review Feedback ---\n{f.read()}\n"
                if feedback:
                    prompt += f"\n\nNote: The previous implementation had issues. Feedback:\n{feedback}\nPlease fix these issues."

            # We use Developer backend to make modifications
            system_prompt = f"You are a {level.title()} AI Developer"
            system_prompt += f" (RD {agent_number}). Write and edit code to fulfill the task."
            dev_output = self.call_agent(agent_role, prompt, system_prompt)
            
            # If text-based API backend, parse and write files
            if backend in ["ollama", "gemini", "agy"]:
                written = self.parse_and_write_files(dev_output)
                if written:
                    log_success(f"Successfully processed files written by Developer: {', '.join(written)}")
                else:
                    log_warning("No files were parsed from Developer response. Ensure they used [FILE_START: path] blocks.")
            
            developer_logs.append(f"--- Task {task['id']} implementation output ---\n{dev_output}\n")
            
            # Mark task completed
            task["status"] = "completed"
            self.save_state()
            
            # Save step output
            with open(self.action_items_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2)

        # Write cumulative developer logs
        with open(self.developer_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(developer_logs))

        log_success("All pending tasks processed.")
        self.state["state"] = "TESTING"
        self.save_state()

    def step_testing(self):
        log_header("5. TESTING & VERIFICATION (QA Agent)")
        test_cmd = self.config.get("test_command", "git diff --stat")
        log_info(f"Running test command: {test_cmd}")
        
        # split command safely (assuming bash execution for custom command)
        code, output = self.run_command(["bash", "-c", test_cmd], timeout=600, capture=True)
        
        with open(self.test_results_path, "w", encoding="utf-8") as f:
            f.write(f"Command: {test_cmd}\nExit Code: {code}\nOutput:\n{output}")
            
        log_info(f"Test exit code: {code}")
        
        # Run the manager-selected QA team. Each report independently gates review.
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()
        # get git diff
        if self.has_git:
            _, git_diff = self.run_command(["git", "diff", self.base_branch], capture=True)
            if not git_diff.strip():
                _, git_diff = self.run_command(["git", "diff"], capture=True)
        else:
            git_diff = "No git repository, changes are directly in workspace."


        task_assignments = [{key: task.get(key) for key in ("id", "description", "complexity", "assignee_level")} for task in self.state.get("tasks", [])]
        qa_prompt = f"""Analyze the test execution results for our changes.\n\nRequirements:\n{requirements}\n\nImplementation Plan:\n{plan}\n\nTask assignments:\n{json.dumps(task_assignments)}\n\nGit Diff:\n{git_diff}\n\nRaw Test Output:\n{output}\nTest Exit Code: {code}\n\nGenerate a detailed QA test report in Markdown. If all tests pass and the implementation looks correct and safe, start with 'PASSED'. Otherwise start with 'FAILED' and list the issues and fixes."""
        qa_staff = self.staffing("qa")
        qa_team = ["senior"] * qa_staff["senior"] + ["middle"] * qa_staff["middle"] + ["junior"] * qa_staff["junior"] or ["senior"]
        qa_reports = [
            self.call_agent(
                f"qa_{level}",
                qa_prompt,
                "You are a Senior Quality Assurance Engineer. Review complex/design/high-risk tasks and their regressions."
                if level == "senior"
                else "You are a Middle Quality Assurance Engineer. Review moderate feature work and integration regressions."
                if level == "middle"
                else "You are a Junior Quality Assurance Engineer. Verify routine task behavior and obvious regressions; flag design risks for senior QA.",
            )
            for level in qa_team
        ]
        qa_report = "\n\n".join(
            f"## QA {index + 1} ({level})\n{report}"
            for index, (level, report) in enumerate(zip(qa_team, qa_reports))
        )
        
        with open(self.qa_report_path, "w", encoding="utf-8") as f:
            f.write(qa_report)
        log_success(f"QA report generated and saved to {self.qa_report_path}")
        
        is_passed = all(report.strip().upper().replace("*", "").startswith("PASSED") for report in qa_reports)
        
        if is_passed:
            log_success("QA verification PASSED!")
            self.state["state"] = "REVIEWING_CODE"
            self.save_state()
        else:
            log_warning("QA verification FAILED!")
            self.escalate_developer_backend()
            max_rev = self.config.get("max_revisions", 2)
            if self.state["code_revisions"] < max_rev:
                self.state["code_revisions"] += 1
                self.state["state"] = "IMPLEMENTING"
                # Instead of marking original tasks pending, append a QA fix task
                fix_task_id = f"FIX-QA-{self.state['code_revisions']}"
                self.state["tasks"].append({
                    "id": fix_task_id,
                    "description": f"Fix QA verification issues. Feedback from QA:\n{qa_report[:2000]}",
                    "status": "pending"
                })
                self.save_state()
                log_info(f"Revising code based on QA report (Revision {self.state['code_revisions']}/{max_rev})...")
            else:
                log_warning("Reached max code revisions. Proceeding to final architect review.")
                self.state["state"] = "REVIEWING_CODE"
                self.save_state()

    def step_reviewing_code(self):
        log_header("6. REVIEWING CODE (Architect / Reviewer)")
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()
        with open(self.test_results_path, "r", encoding="utf-8") as f:
            test_results = f.read()

        # Get git diff
        if self.has_git:
            _, git_diff = self.run_command(["git", "diff", self.base_branch], capture=True)
            if not git_diff.strip():
                _, git_diff = self.run_command(["git", "diff"], capture=True)
        else:
            git_diff = "No git repository, changes are directly in workspace."


        prompt = f"""Review the code changes made. Here is the context:\n\nRequirements:\n{requirements}\n\nPlan:\n{plan}\n\nTest Results:\n{test_results}\n\nGit Diff:\n{git_diff}\n\nVerify if the implementation matches requirements and plan, and if the tests pass.\nIf acceptable, start your response with 'APPROVED'.\nIf there are bugs, logic errors, style issues, or failures, start your response with 'REJECTED' followed by detailed feedback.\n\nFormat:\n[APPROVED or REJECTED]\n[Feedback details]"""

        system_prompt = "You are a Senior Code Reviewer. Review the git diff and test results."
        review = self.call_agent("reviewer", prompt, system_prompt)

        with open(self.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)
            
        log_info(f"Code Review response saved. Preview:\n{review[:200]}...")

        is_approved = review.strip().upper().replace("*", "").startswith("APPROVED")
        
        if is_approved:
            log_success("Code changes APPROVED by Reviewer!")
            self.state["state"] = "COMPLETED"
            self.save_state()
        else:
            log_warning("Code changes REJECTED by Reviewer.")
            self.escalate_developer_backend()
            max_rev = self.config.get("max_revisions", 2)
            if self.state["code_revisions"] < max_rev:
                self.state["code_revisions"] += 1
                self.state["state"] = "IMPLEMENTING"
                # Instead of marking all original tasks as pending, append a new task containing Reviewer's feedback
                fix_task_id = f"FIX-REV-{self.state['code_revisions']}"
                self.state["tasks"].append({
                    "id": fix_task_id,
                    "description": f"Fix Code Review Issues. Feedback from Reviewer:\n{review[:2000]}",
                    "status": "pending"
                })
                self.save_state()
                log_info(f"Revising implementation (Revision {self.state['code_revisions']}/{max_rev})...")
            else:
                log_warning("Reached max code review revisions. Pausing for human review.")
                self.state["state"] = "WAITING_FOR_OWNER"
                self.save_state()

    def step_completed(self):
        log_header("7. GENERATING SUMMARY (Ollama Manager)")
        
        with open(self.request_path, "r", encoding="utf-8") as f:
            request = f.read()
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        
        # Get final git diff stat
        wt_path = self.ai_dir / "worktree"
        if self.config.get("use_worktree", True) and wt_path.exists() and self.has_git:
            _, diff_stat = self.run_command(["git", "diff", "--stat", self.base_branch], cwd=wt_path)
            _, diff_patch = self.run_command(["git", "diff", self.base_branch], cwd=wt_path)
        elif self.has_git:
            _, diff_stat = self.run_command(["git", "diff", "--stat", self.base_branch])
            _, diff_patch = self.run_command(["git", "diff", self.base_branch])
        else:
            diff_stat = "No git repository."
            diff_patch = "No git repository."


        prompt = f"""We have successfully completed the tasks.\nOriginal Request:\n{request}\n\nRequirements:\n{requirements}\n\nGit Diff Stat:\n{diff_stat}\n\nPlease generate a Final Report in Markdown. Summarize what was built, files modified, and verify how requirements were met."""

        system_prompt = "You are a Project Manager. Write a beautiful project final report."
        summary = self.call_manager(prompt, system_prompt)

        with open(self.final_report_path, "w", encoding="utf-8") as f:
            f.write(summary)
            
        log_success(f"Final project report generated at {self.final_report_path}")
        
        # Assistant generates CHANGELOG
        log_info("Asking Assistant to generate CHANGELOG.md...")
        changelog_prompt = f"""Please generate a CHANGELOG entry for the following completed task.\n\nSummary:\n{summary}\n\nDiff:\n{diff_patch[:5000]}"""
        changelog_system = "You are the project Assistant. You write concise, professional markdown CHANGELOG entries."
        changelog = self.call_agent("assistant", changelog_prompt, changelog_system)
        
        with open(self.workspace / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write("\n\n" + changelog)
            
        log_success("CHANGELOG.md updated successfully!")
        
        # Merge and clean up isolated worktree
        self.cleanup_worktree(merge=True)
        
        log_success("Multi-agent workflow process has finished successfully!")

    # Flow Controller
    def step(self):
        self.load_config_and_state()
        current_state = self.state["state"]
        log_info(f"Current state is: {current_state}")

        if current_state == "PLANNING":
            self.step_planning()
        elif current_state == "DEVELOPING_PLAN":
            self.step_developing_plan()
        elif current_state == "REVIEWING_PLAN":
            self.step_reviewing_plan()
        elif current_state == "IMPLEMENTING":
            self.step_implementing()
        elif current_state == "TESTING":
            self.step_testing()
        elif current_state == "REVIEWING_CODE":
            self.step_reviewing_code()
        elif current_state == "COMPLETED":
            self.step_completed()
        elif current_state == "WAITING_FOR_OWNER":
            log_warning("Currently waiting for owner approval. Edit state.json to resume.")
        elif current_state == "FAILED":
            log_error("Workflow failed. Please reset or start a new task.")
        else:
            log_error(f"Unknown state: {current_state}")

    def run_to_end(self):
        self.load_config_and_state()
        while self.state["state"] not in ["COMPLETED", "WAITING_FOR_OWNER", "FAILED"]:
            self.step()
            self.load_config_and_state()
        if self.state["state"] == "COMPLETED":
            self.step()  # Generates summary report

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
    backend_parser.add_argument("role", choices=["manager", "developer", "reviewer", "qa"], help="The agent role")
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
            "tasks": []
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
            print(f"{"Current State:":<20}{Colors.BOLD}{orchestrator.state['state']}{Colors.ENDC}")
            print(f"{"Plan Revisions:":<20}{orchestrator.state['plan_revisions']}/{orchestrator.config['max_revisions']}")
            print(f"{"Code Revisions:":<20}{orchestrator.state['code_revisions']}/{orchestrator.config['max_revisions']}")
            print(f"Ollama Model:       {orchestrator.config['ollama_model']}")
            print(f"{"Developer Backend:":<20}{orchestrator.config['backends']['developer']}")
            print(f"{"Reviewer Backend:":<20}{orchestrator.config['backends']['reviewer']}")
            print(f"{"QA Backend:":<20}{orchestrator.config['backends'].get('qa', 'ollama')}")
            print(f"{"Test Command:":<20}{orchestrator.config['test_command']}")
            
            tasks = orchestrator.state.get("tasks", [])
            if tasks:
                print(f"\n{"Action Items"} ({len(tasks)} total):")
                for t in tasks:
                    status_color = Colors.GREEN if t['status'] == 'completed' else Colors.WARNING
                    print(f" - [{status_color}{t['status']}{Colors.ENDC}] {t['id']}: {t['description']}")
            else:
                print(f"\n{"No tasks parsed yet."}")
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
            orchestrator.cleanup_worktree(merge=False)
            orchestrator.save_state()
            log_success(f"State reset to {args.state}")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    elif args.command == "set-backend":
        try:
            orchestrator.load_config_and_state()
            orchestrator.config["backends"][args.role] = args.backend
            with open(orchestrator.config_path, "w", encoding="utf-8") as f:
                json.dump(orchestrator.config, f, indent=2)
            log_success(f"Successfully configured '{args.role}' backend to '{args.backend}'")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
