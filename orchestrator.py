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
        "manager": "ollama",
        "developer": "codex",
        "reviewer": "ollama",  # Default to ollama; user can change to 'claude' when ready
        "qa": "ollama"         # Default to ollama QA backend
    },
    "gemini_model": "gemini-2.5-flash",  # Default to gemini-2.5-flash which is very fast and capable
    "gemini_api_key": "",
    "use_ponytail": False  # Enforces minimalist senior developer/reviewer principles (YAGNI)
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
        self.final_report_path = self.ai_dir / "final_report.md"

        self.config = DEFAULT_CONFIG
        self.state = {
            "state": "PLANNING",
            "plan_revisions": 0,
            "code_revisions": 0,
            "tasks": []
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

    def run_command(self, cmd: list[str], timeout: int = 1800, capture: bool = True) -> tuple[int, str]:
        """Runs a subprocess command safety and returns (returncode, output)."""
        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace,
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
    def call_ollama(self, prompt: str, system_prompt: str | None = None) -> str:
        url = f"{self.config['ollama_url']}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": self.config["ollama_model"],
            "messages": messages,
            "stream": False
        }
        
        try:
            response = requests.post(url, json=payload, timeout=180)
            response.raise_for_status()
            return response.json()["message"]["content"]
        except requests.exceptions.RequestException as e:
            log_error(f"Failed to communicate with Ollama at {url}.")
            log_error(f"Error detail: {e}")
            raise RuntimeError(f"Ollama connection failed: {e}")

    def call_codex(self, prompt: str, system_prompt: str | None = None) -> str:
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
            "--skip-git-repo-check",
            "-"
        ]
        
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

    def call_claude(self, prompt: str, system_prompt: str | None = None) -> str:
        # Check Claude availability / authentication
        # Try running with print flag
        full_prompt = ""
        if system_prompt:
            full_prompt += f"{system_prompt}\n\n"
        full_prompt += prompt

        cmd = ["claude", "--print", "--dangerously-skip-permissions", full_prompt]
        log_info("Running Claude Code CLI...")
        
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

    def call_gemini(self, prompt: str, system_prompt: str | None = None) -> str:
        api_key = self.config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY not found. Please set it in .ai-company/config.json as 'gemini_api_key' "
                "or export it in your environment as GEMINI_API_KEY."
            )
        
        model = self.config.get("gemini_model", "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        
        contents = {"parts": [{"text": prompt}]}
        payload = {"contents": [contents]}
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
            
        headers = {"Content-Type": "application/json"}
        log_info(f"Calling Gemini API (Model: {model})...")
        response = requests.post(url, json=payload, headers=headers, timeout=120)
        response.raise_for_status()
        
        result_json = response.json()
        try:
            return result_json["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected response structure from Gemini API: {e}\n{result_json}")

    def call_agy(self, prompt: str, system_prompt: str | None = None) -> str:
        # Prepend system prompt to the user prompt if present
        full_prompt = ""
        if system_prompt:
            full_prompt += f"System Instructions:\n{system_prompt}\n\n"
        full_prompt += prompt

        cmd = ["agy", "--print", full_prompt]
        log_info("Running agy CLI...")
        
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
                return self.call_codex(prompt, system_prompt)
            except Exception as e:
                log_warning(f"Codex manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt)
        elif backend == "claude":
            try:
                return self.call_claude(prompt, system_prompt)
            except Exception as e:
                log_warning(f"Claude manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt)
        elif backend == "gemini":
            try:
                return self.call_gemini(prompt, system_prompt)
            except Exception as e:
                log_warning(f"Gemini manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt)
        elif backend == "agy":
            try:
                return self.call_agy(prompt, system_prompt)
            except Exception as e:
                log_warning(f"agy manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt)
        else:
            return self.call_ollama(prompt, system_prompt)

    def call_agent(self, role: str, prompt: str, system_prompt: str | None = None) -> str:
        backend = self.config["backends"].get(role, "ollama")
        log_info(f"Requesting Agent '{role}' (Backend: {backend})...")
        
        # Inject ponytail prompt if enabled and role is developer or reviewer
        if self.config.get("use_ponytail", False) and role in ["developer", "reviewer"]:
            if system_prompt:
                system_prompt += PONYTAIL_PROMPT
            else:
                system_prompt = PONYTAIL_PROMPT.strip()

        if backend == "claude":
            try:
                return self.call_claude(prompt, system_prompt)
            except Exception as e:
                log_warning(f"Claude backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt)
        elif backend == "codex":
            try:
                return self.call_codex(prompt, system_prompt)
            except Exception as e:
                log_warning(f"Codex backend failed: {e}")
                log_warning("Falling open to Ollama backend.")
                return self.call_ollama(prompt, system_prompt)
        elif backend == "gemini":
            try:
                return self.call_gemini(prompt, system_prompt)
            except Exception as e:
                log_warning(f"Gemini backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt)
        elif backend == "agy":
            try:
                return self.call_agy(prompt, system_prompt)
            except Exception as e:
                log_warning(f"agy backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt)
        else:
            return self.call_ollama(prompt, system_prompt)

    # Workflow Steps
    def step_planning(self):
        log_header("1. PLANNING (Ollama Manager)")
        if not self.request_path.exists():
            log_error(f"No request file found at {self.request_path}. Please run 'start' command first.")
            sys.exit(1)
            
        with open(self.request_path, "r", encoding="utf-8") as f:
            request = f.read()

        system_prompt = (
            "You are the Project Manager of an AI software company. Your job is to analyze the user's request "
            "and write a detailed, clear requirements document in Markdown format.\n"
            "Requirements must contain:\n"
            "1. Project Overview & Context\n"
            "2. Specific Functional Requirements\n"
            "3. Technical Specifications & Stack constraints\n"
            "4. Scope boundaries (what is NOT included)\n\n"
            "Output ONLY the Markdown content for requirements.md. Do not add any greeting, preamble, or conversational introduction."
        )
        
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

        # Now parse plan into tasks (Action Items) using Manager (Ollama)
        log_info("Parsing implementation plan into structured action items...")
        parse_prompt = (
            f"Read this implementation plan:\n\n"
            f"{plan}\n\n"
            f"Extract a flat JSON array of tasks representing the steps to be coded.\n"
            f"Each task must be a JSON object with fields:\n"
            f"- 'id': unique string ID (e.g. 'TASK-001', 'TASK-002')\n"
            f"- 'description': concise description of the coding step\n"
            f"- 'status': 'pending'\n\n"
            f"Respond ONLY with a valid JSON array. Do not include markdown code block syntax (like ```json) or any other text."
        )
        
        parsed_items_raw = self.call_manager(parse_prompt, "You are a Project Manager. Output only raw JSON lists of tasks.")
        
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
            tasks = json.loads(clean_json)
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
        except json.JSONDecodeError as e:
            log_warning(f"Could not parse tasks as JSON. Saving raw output. Error: {e}")
            log_warning(f"Raw Ollama output was: {parsed_items_raw}")
            # Write a fallback task
            fallback_tasks = [{"id": "TASK-001", "description": "Implement overall implementation plan", "status": "pending"}]
            self.state["tasks"] = fallback_tasks
            self.save_state()

        self.state["state"] = "REVIEWING_PLAN"
        self.save_state()

    def step_reviewing_plan(self):
        log_header("3. REVIEWING PLAN (Architect / Reviewer)")
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()

        prompt = (
            f"Review the implementation plan against the requirements.\n\n"
            f"Requirements:\n{requirements}\n\n"
            f"Implementation Plan:\n{plan}\n\n"
            f"Check for architectural issues, gaps in requirements, and safety.\n"
            f"If acceptable, start your response with 'APPROVED'.\n"
            f"If issues exist, start your response with 'REJECTED' followed by detailed feedback.\n\n"
            f"Format:\n"
            f"[APPROVED or REJECTED]\n"
            f"[Feedback details]"
        )

        system_prompt = "You are a Senior Software Architect. Review the implementation plan."
        review = self.call_agent("reviewer", prompt, system_prompt)

        with open(self.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)
        log_info(f"Reviewer response saved. Preview:\n{review[:200]}...")

        is_approved = review.strip().upper().startswith("APPROVED")
        
        if is_approved:
            log_success("Implementation plan APPROVED by Reviewer!")
            self.state["state"] = "IMPLEMENTING"
            self.save_state()
        else:
            log_warning("Implementation plan REJECTED by Reviewer.")
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
                
            target_path = (self.workspace / filepath_str).resolve()
            # Safety check: ensure it is inside workspace
            if self.workspace not in target_path.parents and target_path != self.workspace:
                log_warning(f"Skipping file write outside workspace: {filepath_str}")
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
        backend = self.config["backends"].get("developer", "codex")
        for task in pending_tasks:
            log_info(f"Implementing Task {task['id']}: {task['description']}")
            
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
            system_prompt = "You are an expert AI Developer. Write and edit code to fulfill the task."
            dev_output = self.call_agent("developer", prompt, system_prompt)
            
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
        
        # Now run QA agent analysis
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()
        # get git diff
        _, git_diff = self.run_command(["git", "diff", "master"], capture=True)
        if not git_diff.strip():
            _, git_diff = self.run_command(["git", "diff"], capture=True)

        qa_prompt = (
            f"You are the QA Engineer. Analyze the test execution results for our changes.\n\n"
            f"Requirements:\n{requirements}\n\n"
            f"Implementation Plan:\n{plan}\n\n"
            f"Git Diff:\n{git_diff}\n\n"
            f"Raw Test Output:\n{output}\n"
            f"Test Exit Code: {code}\n\n"
            f"Generate a detailed QA test report in Markdown. "
            f"If all tests pass and the implementation looks correct and safe, your report MUST start with 'PASSED'. "
            f"If there are any test failures, errors, unhandled exceptions, or missing deliverables, your report MUST start with 'FAILED' followed by the details of the issues and suggested fixes."
        )
        
        system_prompt = "You are a Senior Quality Assurance Engineer. Generate a QA report."
        qa_report = self.call_agent("qa", qa_prompt, system_prompt)
        
        with open(self.qa_report_path, "w", encoding="utf-8") as f:
            f.write(qa_report)
        log_success(f"QA report generated and saved to {self.qa_report_path}")
        
        is_passed = qa_report.strip().upper().startswith("PASSED")
        
        if is_passed:
            log_success("QA verification PASSED!")
            self.state["state"] = "REVIEWING_CODE"
            self.save_state()
        else:
            log_warning("QA verification FAILED!")
            max_rev = self.config.get("max_revisions", 2)
            if self.state["code_revisions"] < max_rev:
                self.state["code_revisions"] += 1
                self.state["state"] = "IMPLEMENTING"
                # Mark tasks as pending to trigger re-implementation with QA report feedback
                for t in self.state["tasks"]:
                    t["status"] = "pending"
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
        _, git_diff = self.run_command(["git", "diff", "master"], capture=True)
        if not git_diff.strip():
            _, git_diff = self.run_command(["git", "diff"], capture=True)

        prompt = (
            f"Review the code changes made. Here is the context:\n\n"
            f"Requirements:\n{requirements}\n\n"
            f"Plan:\n{plan}\n\n"
            f"Test Results:\n{test_results}\n\n"
            f"Git Diff:\n{git_diff}\n\n"
            f"Verify if the implementation matches requirements and plan, and if the tests pass.\n"
            f"If acceptable, start your response with 'APPROVED'.\n"
            f"If there are bugs, logic errors, style issues, or failures, start your response with 'REJECTED' followed by detailed feedback.\n\n"
            f"Format:\n"
            f"[APPROVED or REJECTED]\n"
            f"[Feedback details]"
        )

        system_prompt = "You are a Senior Code Reviewer. Review the git diff and test results."
        review = self.call_agent("reviewer", prompt, system_prompt)

        with open(self.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)
            
        log_info(f"Code Review response saved. Preview:\n{review[:200]}...")

        is_approved = review.strip().upper().startswith("APPROVED")
        
        if is_approved:
            log_success("Code changes APPROVED by Reviewer!")
            self.state["state"] = "COMPLETED"
            self.save_state()
        else:
            log_warning("Code changes REJECTED by Reviewer.")
            max_rev = self.config.get("max_revisions", 2)
            if self.state["code_revisions"] < max_rev:
                self.state["code_revisions"] += 1
                self.state["state"] = "IMPLEMENTING"
                # Mark tasks as pending to trigger re-implementation
                for t in self.state["tasks"]:
                    t["status"] = "pending"
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
        _, diff_stat = self.run_command(["git", "diff", "--stat"])

        prompt = (
            f"We have successfully completed the tasks.\n"
            f"Original Request:\n{request}\n\n"
            f"Requirements:\n{requirements}\n\n"
            f"Git Diff Stat:\n{diff_stat}\n\n"
            f"Please generate a Final Report in Markdown. Summarize what was built, files modified, and verify how requirements were met."
        )

        system_prompt = "You are a Project Manager. Write a beautiful project final report."
        summary = self.call_manager(prompt, system_prompt)

        with open(self.final_report_path, "w", encoding="utf-8") as f:
            f.write(summary)
            
        log_success(f"Final project report generated at {self.final_report_path}")
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
            print(f"Current State:      {Colors.BOLD}{orchestrator.state['state']}{Colors.ENDC}")
            print(f"Plan Revisions:     {orchestrator.state['plan_revisions']}/{orchestrator.config['max_revisions']}")
            print(f"Code Revisions:     {orchestrator.state['code_revisions']}/{orchestrator.config['max_revisions']}")
            print(f"Ollama Model:       {orchestrator.config['ollama_model']}")
            print(f"Developer Backend:  {orchestrator.config['backends']['developer']}")
            print(f"Reviewer Backend:   {orchestrator.config['backends']['reviewer']}")
            print(f"QA Backend:         {orchestrator.config['backends'].get('qa', 'ollama')}")
            print(f"Test Command:       {orchestrator.config['test_command']}")
            
            tasks = orchestrator.state.get("tasks", [])
            if tasks:
                print(f"\nAction Items ({len(tasks)} total):")
                for t in tasks:
                    status_color = Colors.GREEN if t['status'] == 'completed' else Colors.WARNING
                    print(f" - [{status_color}{t['status']}{Colors.ENDC}] {t['id']}: {t['description']}")
            else:
                print("\nNo tasks parsed yet.")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    elif args.command == "reset":
        try:
            orchestrator.load_config_and_state()
            orchestrator.state["state"] = args.state
            orchestrator.state["plan_revisions"] = 0
            orchestrator.state["code_revisions"] = 0
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
