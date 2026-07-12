from __future__ import annotations
import os
import sys
import json
import subprocess
import shutil
import re
from copy import deepcopy
from pathlib import Path
import requests

from orchestrator.core.config import DEFAULT_CONFIG

from orchestrator.roles.base_agent import PONYTAIL_PROMPT, inject_ponytail_prompt

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

        # Check if we are in a git repository
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
        if self.has_git:
            run_name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{workspace.name}-{self.base_branch}").strip("-")
            self.ai_dir = workspace / f".ai-company-{run_name}"
        else:
            self.ai_dir = workspace / ".ai-company"
        self.config_path = self.ai_dir / "config.json"
        self.state_path = self.ai_dir / "state.json"

        # Files for data sharing
        self.request_path = self.ai_dir / "request.md"
        self.requirements_path = self.ai_dir / "requirements.md"
        self.plan_path = self.ai_dir / "implementation_plan.md"
        self.action_items_path = self.ai_dir / "action_items.json"
        self.agent_context_path = self.ai_dir / "agent_context.json"
        self.reviewer_output_path = self.ai_dir / "reviewer_output.md"
        self.developer_output_path = self.ai_dir / "developer_output.md"
        self.test_results_path = self.ai_dir / "test_results.txt"
        self.qa_report_path = self.ai_dir / "qa_report.md"
        self.human_report_path = self.ai_dir / "human_report.md"
        self.meeting_memory_path = self.ai_dir / "meeting_memory.md"
        self.specialist_review_path = self.ai_dir / "specialist_reviews.md"
        self.final_report_path = self.ai_dir / "final_report.md"

        self.config = deepcopy(DEFAULT_CONFIG)
        self.explicit_backends = set()
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
            "quota_exhausted_backends": {},
            "failed_model_routes": [],
            "task_failed_model_routes": {},
            "task_developer_promotions": {},
            "tasks": [],
            "specialists": [],
            "staffing": {"rd": {"senior": 1, "middle": 0, "junior": 0}, "qa": {"senior": 1, "middle": 0, "junior": 0}}
        }

        # Roles
        from orchestrator.roles.manager import ManagerAgent
        from orchestrator.roles.architect import ArchitectAgent
        from orchestrator.roles.developer import DeveloperAgent
        from orchestrator.roles.qa import QAAgent
        from orchestrator.roles.reviewer import ReviewerAgent
        from orchestrator.roles.assistant import AssistantAgent
        from orchestrator.roles.devops import DevOpsAgent
        from orchestrator.roles.uiux import UIUXAgent
        from orchestrator.roles.uiux_visual_review import UIUXVisualReviewAgent
        from orchestrator.roles.fae import FAEAgent
        from orchestrator.roles.integration import IntegrationAgent

        self.manager = ManagerAgent(self)
        self.architect = ArchitectAgent(self)
        self.developer = DeveloperAgent(self)
        self.qa = QAAgent(self)
        self.reviewer = ReviewerAgent(self)
        self.assistant = AssistantAgent(self)
        self.devops = DevOpsAgent(self)
        self.uiux = UIUXAgent(self)
        self.uiux_visual_review = UIUXVisualReviewAgent(self)
        self.fae = FAEAgent(self)
        self.integration = IntegrationAgent(self)

    def init_project(self):
        """Initializes the .ai-company folder and configuration files."""
        if self.state_path.exists():
            try:
                existing_state = json.loads(self.state_path.read_text(encoding="utf-8"))
                if existing_state.get("state") in {"COMPLETED", "FAILED"}:
                    log_info("Clearing completed or failed workflow artifacts.")
                    self.clear_run_artifacts()
            except (OSError, json.JSONDecodeError):
                self.clear_run_artifacts()
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

    def clear_run_artifacts(self):
        config_text = self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else None
        self.cleanup_worktree(merge=False)
        shutil.rmtree(self.ai_dir, ignore_errors=True)
        if self.has_git:
            self.run_command(["git", "worktree", "prune"], cwd=self.workspace)
        self.ai_dir.mkdir(parents=True, exist_ok=True)
        if config_text is not None:
            self.config_path.write_text(config_text, encoding="utf-8")
        self.state = {
            "state": "PLANNING", "plan_revisions": 0, "code_revisions": 0,
            "model_tier_indices": {"developer": 0, "reviewer": 0, "manager": 0, "qa": 0},
            "quota_exhausted_backends": {}, "tasks": [], "specialists": [],
            "failed_model_routes": [], "task_failed_model_routes": {}, "task_developer_promotions": {},
            "staffing": {"rd": {"senior": 1, "middle": 0, "junior": 0}, "qa": {"senior": 1, "middle": 0, "junior": 0}},
        }

    def write_agent_context(self):
        request = self.request_path.read_text(encoding="utf-8") if self.request_path.exists() else ""
        stage = self.state.get("state", "PLANNING")
        contract = {"stage": stage, "allowed_actions": ["modify_files"], "output_contract": {"format": "file_blocks", "response_must_start_with": "[FILE_START:", "allow_prose": False}} if stage == "IMPLEMENTING" else {"stage": stage, "allowed_actions": ["plan_or_review"], "output_contract": {"format": "stage_prompt", "allow_file_blocks": False}}
        task_keys = ("id", "description", "complexity", "rd_level", "qa_level", "status", "target_files", "output_contract")
        context = {
            "request": request,
            "contract": contract,
            "tasks": [{key: task.get(key) for key in task_keys if key in task} for task in self.state.get("tasks", [])],
            "specialists": self.state.get("specialists", []),
        }
        self.agent_context_path.write_text(json.dumps(context, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    def read_agent_context(self) -> dict:
        if self.agent_context_path.exists():
            return json.loads(self.agent_context_path.read_text(encoding="utf-8"))
        return {"request": self.request_path.read_text(encoding="utf-8") if self.request_path.exists() else "", "tasks": self.state.get("tasks", [])}

    def load_config_and_state(self):
        """Loads configuration and state files from disk."""
        if not self.config_path.exists():
            raise FileNotFoundError("Project not initialized. Please run 'init' first.")

        with open(self.config_path, "r", encoding="utf-8") as f:
            loaded_config = json.load(f)
        self.explicit_backends = set(loaded_config.get("backends", {}))

        from orchestrator.core.config import merge_defaults
        self.config = merge_defaults(DEFAULT_CONFIG, loaded_config)

        with open(self.state_path, "r", encoding="utf-8") as f:
            self.state = json.load(f)
        self.state.setdefault("specialists", [])
        self.state.setdefault("staffing", {})
        self.state.setdefault("worker_assignments", {})
        self.state.setdefault("last_developer_role", "developer_senior")
        self.state.setdefault("developer_promotions", {})
        self.state.setdefault("failed_model_routes", [])
        self.state.setdefault("task_failed_model_routes", {})
        self.state.setdefault("task_developer_promotions", {})

    @staticmethod
    def merge_defaults(defaults: dict, values: dict) -> dict:
        from orchestrator.core.config import merge_defaults
        return merge_defaults(defaults, values)

    def save_state(self):
        """Saves current state to state.json."""
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    def pause_for_human_review(self, source: str, details: str, resume_state: str, pass_state: str):
        requirements = self.request_path.read_text(encoding="utf-8") if self.request_path.exists() else ""
        chinese = any("\u4e00" <= char <= "\u9fff" for char in requirements)
        title = "人工審核報告" if chinese else "Human Review Report"
        conclusion = f"{source} 驗證未通過，需要人工確認。" if chinese else f"{source} verification requires human review."
        next_step = "確認後執行" if chinese else "After review, run"
        report = f"# {title}\n\n## 結論\n\n{conclusion}\n\n## 詳細資訊\n\n{details}\n\n## 後續動作\n\n{next_step}:\n\n```bash\npython3 orchestrator.py approve --run\n```\n"
        self.human_report_path.write_text(report, encoding="utf-8")
        self.state.update({"state": "WAITING_FOR_OWNER", "human_review_source": source,
                           "resume_state": resume_state, "pass_state": pass_state,
                           "human_review_details": details})
        self.save_state()

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

    # LLM Wrapper Delegation
    def get_backend(self, role: str) -> str:
        from orchestrator.core import backends
        return backends.get_backend(self, role)

    def get_active_model_for_role(self, role: str, backend: str) -> str | None:
        from orchestrator.core import backends
        return backends.get_active_model_for_role(self, role, backend)

    def call_ollama(self, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None, image_paths: list[str] | None = None) -> str:
        from orchestrator.core import backends
        return backends.call_ollama(self, prompt, system_prompt, role, model, image_paths)

    def call_codex(self, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None) -> str:
        from orchestrator.core import backends
        return backends.call_codex(self, prompt, system_prompt, role, model)

    def call_claude(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        from orchestrator.core import backends
        return backends.call_claude(self, prompt, system_prompt, role)

    def call_agy(self, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None) -> str:
        from orchestrator.core import backends
        return backends.call_agy(self, prompt, system_prompt, role, model)

    def call_grok(self, prompt: str, system_prompt: str | None = None, role: str = "developer", model: str | None = None) -> str:
        from orchestrator.core import backends
        return backends.call_grok(self, prompt, system_prompt, role, model)

    def token_fallback_model(self, role: str, error: Exception) -> str | None:
        from orchestrator.core import backends
        return backends.token_fallback_model(self, role, error)

    def escalate_developer_backend(self):
        from orchestrator.core import backends
        return backends.escalate_developer_backend(self)

    def call_manager(self, prompt: str, system_prompt: str | None = None, response_validator=None) -> str:
        return self.call_agent("manager", prompt, system_prompt, response_validator=response_validator)

    def call_agy_quota_fallback(self, role: str, prompt: str, system_prompt: str | None = None) -> str:
        from orchestrator.core import backends
        if not backends.backend_available(self, "agy"):
            log_warning("AGY quota exhausted; falling back to Ollama backend.")
            return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        try:
            return self.call_agy(prompt, system_prompt, role=role, model="gpt-oss-120b")
        except Exception as e:
            if backends.quota_exhausted(e):
                backends.mark_backend_quota_exhausted(self, "agy")
            return self.call_agent_ollama_fallback(role, prompt, system_prompt)

    def call_agent_ollama_fallback(self, role: str, prompt: str, system_prompt: str | None = None, model: str | None = None, image_paths: list[str] | None = None) -> str:
        if role.startswith("developer") and "[FILE_START:" not in prompt:
            prompt += (
                "\n\nCRITICAL: You do not have access to terminal tools. "
                "To modify files, you must output your changes using this exact block format:\n"
                "[FILE_START: path/to/file.ext]\n"
                "full file content here\n"
                "[FILE_END: path/to/file.ext]\n\n"
                "Any modifications outside this format will be ignored. Write only complete file contents inside the blocks."
            )
        try:
            if model is None:
                return self.call_ollama(prompt, system_prompt, role=role, **({"image_paths": image_paths} if image_paths else {}))
            return self.call_ollama(prompt, system_prompt, role=role, model=model, **({"image_paths": image_paths} if image_paths else {}))
        except Exception:
            if role.startswith("qa") and model is None:
                return self.call_ollama(
                    prompt, system_prompt, role=role,
                    model=self.config.get("qa_ollama_fallback_model", "deepseek-r1:7b"),
                )
            raise

    def call_role_model_routes(self, role: str, prompt: str, system_prompt: str | None = None, image_paths: list[str] | None = None, response_validator=None) -> str | None:
        from orchestrator.core import backends
        routes = self.config.get("role_model_routes", {}).get(role, [])
        if not routes:
            return None
        errors = []
        task_id = self.state.get("active_task_id") if self.state.get("state") == "IMPLEMENTING" else None
        if task_id:
            failed_routes = self.state.setdefault("task_failed_model_routes", {}).setdefault(task_id, [])
        else:
            failed_routes = self.state.setdefault("failed_model_routes", [])
        for backend, model in routes:
            route = f"{backend}/{model}"
            if route in failed_routes:
                log_info(f"Skipping unavailable model route: {route}")
                continue
            if not backends.backend_available(self, backend):
                continue
            try:
                log_info(f"Requesting Agent '{role}' (Backend: {backend}, Model: {model})...")
                if backend == "ollama":
                    response = self.call_agent_ollama_fallback(role, prompt, system_prompt, model=model, **({"image_paths": image_paths} if image_paths else {}))
                    if self.state.get("state") == "IMPLEMENTING" and role.startswith("developer") and not any(marker in response for marker in ("[FILE_START:", "[FILE_EDIT_START:", "[SECTION_EDIT_START:")):
                        raise RuntimeError("Developer response omitted required file blocks")
                    self.validate_routed_response(role, response)
                    if response_validator is not None and not response_validator(response):
                        raise RuntimeError("Agent response failed the task output contract")
                    return response
                if backend == "codex":
                    response = self.call_codex(prompt, system_prompt, role=role, model=model)
                    self.validate_routed_response(role, response)
                    if response_validator is not None and not response_validator(response):
                        raise RuntimeError("Agent response failed the task output contract")
                    return response
                if backend == "agy":
                    response = self.call_agy(prompt, system_prompt, role=role, model=model)
                    self.validate_routed_response(role, response)
                    if response_validator is not None and not response_validator(response):
                        raise RuntimeError("Agent response failed the task output contract")
                    return response
                if backend == "grok":
                    response = self.call_grok(prompt, system_prompt, role=role, model=model)
                    if self.state.get("state") == "IMPLEMENTING" and role.startswith("developer") and not any(marker in response for marker in ("[FILE_START:", "[FILE_EDIT_START:", "[SECTION_EDIT_START:")):
                        raise RuntimeError("Developer response omitted required file blocks")
                    self.validate_routed_response(role, response)
                    if response_validator is not None and not response_validator(response):
                        raise RuntimeError("Agent response failed the task output contract")
                    return response
                if backend == "claude":
                    response = self.call_claude(prompt, system_prompt, role=role)
                    self.validate_routed_response(role, response)
                    if response_validator is not None and not response_validator(response):
                        raise RuntimeError("Agent response failed the task output contract")
                    return response
                raise ValueError(f"Unsupported backend in route: {backend}")
            except Exception as error:
                errors.append(f"{backend}/{model}: {error}")
                if route not in failed_routes:
                    failed_routes.append(route)
                    self.save_state()
                if backends.quota_exhausted(error):
                    backends.mark_backend_quota_exhausted(self, backend)
                log_warning(f"{backend}/{model} failed: {error}; trying next route.")
        raise RuntimeError(f"All configured routes failed for {role}: {'; '.join(errors)}")

    def validate_routed_response(self, role: str, response: str):
        if role not in {"architect", "reviewer"}:
            return
        if self.state.get("state") not in {"REVIEWING_PLAN", "REVIEWING_CODE"}:
            return
        first_line = next((line.strip().upper().strip("[]*") for line in response.splitlines() if line.strip()), "")
        if role == "architect":
            valid = first_line in {"PLAN_STATUS: APPROVED", "PLAN_STATUS: REJECTED", "APPROVED", "REJECTED"}
        else:
            valid = first_line.startswith("APPROVED") or first_line.startswith("REJECTED")
        if not valid:
            raise RuntimeError(f"{role} response omitted required status field")

    def call_agent(self, role: str, prompt: str, system_prompt: str | None = None, image_paths: list[str] | None = None, response_validator=None) -> str:
        from orchestrator.core import backends
        backend = self.get_backend(role)
        log_info(f"Requesting Agent '{role}' (Backend: {backend})...")

        system_prompt = inject_ponytail_prompt(system_prompt, self.config.get("use_ponytail", False), role)

        routed = self.call_role_model_routes(role, prompt, system_prompt, image_paths, response_validator)
        if routed is not None:
            return routed

        if backend == "claude":
            if not backends.backend_available(self, "claude"):
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
            try:
                return self.call_claude(prompt, system_prompt, role=role)
            except Exception as e:
                if backends.quota_exhausted(e):
                    backends.mark_backend_quota_exhausted(self, "claude")
                log_warning(f"Claude backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        elif backend == "grok":
            if not backends.backend_available(self, "grok"):
                return self.call_agy_quota_fallback(role, prompt, system_prompt)
            try:
                return self.call_grok(prompt, system_prompt, role=role)
            except Exception as e:
                if backends.quota_exhausted(e):
                    backends.mark_backend_quota_exhausted(self, "grok")
                log_warning(f"Grok Build backend failed: {e}")
                log_warning("Falling back to AGY, then Ollama.")
                return self.call_agy_quota_fallback(role, prompt, system_prompt)
        elif backend == "codex":
            if not backends.backend_available(self, "codex"):
                return self.call_agy_quota_fallback(role, prompt, system_prompt)
            try:
                return self.call_codex(prompt, system_prompt, role=role)
            except Exception as e:
                if backends.quota_exhausted(e):
                    backends.mark_backend_quota_exhausted(self, "codex")
                    return self.call_agy_quota_fallback(role, prompt, system_prompt)
                fallback_model = self.token_fallback_model(role, e)
                if fallback_model:
                    try:
                        return self.call_codex(prompt, system_prompt, role=role, model=fallback_model)
                    except Exception as retry_error:
                        e = retry_error
                log_warning(f"Codex backend failed: {e}")
                log_warning("Falling open to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        elif backend == "agy":
            if not backends.backend_available(self, "agy"):
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
            try:
                return self.call_agy(prompt, system_prompt, role=role)
            except Exception as e:
                fallback_model = self.token_fallback_model(role, e)
                if fallback_model:
                    try:
                        return self.call_agy(prompt, system_prompt, role=role, model=fallback_model)
                    except Exception as retry_error:
                        e = retry_error
                if backends.quota_exhausted(e):
                    backends.mark_backend_quota_exhausted(self, "agy")
                log_warning(f"agy backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        else:
            return self.call_agent_ollama_fallback(role, prompt, system_prompt)

    # Git & Worktree Isolation
    def setup_worktree(self):
        """Creates an isolated git worktree for development and QA testing."""
        if not self.config.get("use_worktree", True) or not self.has_git:
            return

        wt_path = self.ai_dir / "worktree"
        self.cleanup_worktree()

        log_info(f"Setting up isolated Git worktree at {wt_path} on branch 'ai-feature-branch'...")
        wt_path.parent.mkdir(exist_ok=True, parents=True)

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

        if merge:
            code, _ = self.run_command(["git", "show-ref", "--verify", "--quiet", "refs/heads/ai-feature-branch"], cwd=self.workspace)
            branch_exists = (code == 0)
            if branch_exists:
                log_info(f"Merging 'ai-feature-branch' back into {self.base_branch}...")
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

        wt_list = self.run_command(["git", "worktree", "list"], capture=True)[1]
        if "worktree" in wt_list:
            log_info("Removing Git worktree...")
            self.run_command(["git", "worktree", "remove", "--force", str(wt_path)], cwd=self.workspace)

        self.run_command(["git", "branch", "-D", "ai-feature-branch"], cwd=self.workspace)

    # Sub-Agent Allocation Helpers
    def staffing(self, role: str) -> dict[str, int]:
        limits = self.config.get("staffing_limits", {}).get(role, {})
        selected = self.state.get("staffing", {}).get(role, {})
        def bounded(level: str) -> int:
            try:
                return max(0, min(int(selected.get(level, 0)), int(limits.get(level, 0))))
            except (TypeError, ValueError):
                return 0
        return {level: bounded(level) for level in ("senior", "middle", "junior")}

    def allocate_workers(self, role: str, tasks: list[dict]) -> tuple[list[tuple[str, str]], dict[str, str]]:
        levels = ("senior", "middle", "junior")
        level_key = f"{role}_level"
        required = {task.get(level_key, task.get("assignee_level", "senior")) for task in tasks}
        for level in required:
            if level not in levels or not self.staffing(role)[level]:
                raise ValueError(f"Manager staffing has no {role} {level} capacity")

        workers = [
            (f"{role}-{level}-{number}", level)
            for level in levels
            for number in range(1, self.staffing(role)[level] + 1)
        ]
        by_level = {level: [worker for worker in workers if worker[1] == level] for level in levels}
        next_worker = {level: 0 for level in levels}
        assignments = {}
        for task in tasks:
            level = task.get(level_key, task.get("assignee_level", "senior"))
            available = by_level.get(level)
            if not available:
                raise ValueError(f"No {role} {level} worker capacity is available")
            assignments[task["id"]] = available[next_worker.get(level, 0) % len(available)][0]
            next_worker[level] = next_worker.get(level, 0) + 1

        self.state.setdefault("worker_assignments", {})[role] = assignments
        return workers, assignments

    def fix_task_levels(self) -> dict[str, str]:
        base_role = self.state.get("last_developer_role", "developer_senior")
        role = self.state.get("developer_promotions", {}).get(base_role, base_role)
        level = role.rsplit("_", 1)[-1]
        limits = self.config.get("staffing_limits", {}).get("rd", {})
        if int(limits.get(level, 0)):
            staffing = self.state.setdefault("staffing", {}).setdefault("rd", {})
            staffing[level] = max(1, int(staffing.get(level, 0)))
        else:
            level = base_role.rsplit("_", 1)[-1]
        qa_level = self.state.get("last_qa_level", level)
        return {"rd_level": level, "qa_level": qa_level if qa_level in {"junior", "middle", "senior"} else level}

    def consult_specialists(self, requirements: str, plan: str, context: str = "", roles: set[str] | None = None) -> str:
        request = self.request_path.read_text(encoding="utf-8") if self.request_path.exists() else ""
        scope = f"{request}\n{requirements}\n{plan}".lower()
        if "readme" in scope and any(marker in scope for marker in ("only modify", "only allowed", "must not modify", "僅允許", "不得修改")):
            log_info("README-only task; skipping specialist consultation.")
            return ""
        reports = []
        visual_context = "\n".join(f"[IMAGE: {path}]" for path in self.state.get("visual_image_paths", []))
        for specialist in self.state.get("specialists", []):
            role = specialist.get("role")
            if role not in {"sales", "security", "ra", "sre", "devops", "uiux", "uiux_visual_review", "fae", "integration"} or roles is not None and role not in roles:
                continue
            specialist_agent = getattr(self, role, None)
            if specialist_agent:
                try:
                    focus = specialist.get("reason", "")
                    specialist_context = "\n".join(part for part in (context, visual_context, f"Research focus: {focus}" if focus else "") if part)
                    report = specialist_agent.review(requirements, plan, specialist_context)
                    reports.append(f"## {role.title()}{f' — {focus}' if focus else ''}\n{report}")
                    continue
                except Exception as e:
                    log_warning(f"Optional {role} specialist failed: {e}")
                    continue
            prompt = f"Review this project only for your specialty.\n\nRequirements:\n{requirements}\n\nPlan:\n{plan}\n\nAdditional Context:\n{context or 'None'}\n\nReturn concise risks, missing requirements, and concrete acceptance criteria."
            try:
                report = self.call_agent(role, prompt, f"You are the project's {role.title()} specialist.")
                reports.append(f"## {role.title()}\n{report}")
            except Exception as e:
                log_warning(f"Optional {role} specialist failed: {e}")
        notes = "\n\n".join(reports)
        if notes:
            self.specialist_review_path.write_text(notes, encoding="utf-8")
        return notes

    def ensure_visual_review_specialist(self) -> None:
        if self.state.get("visual_image_paths") and not any(
            item.get("role") == "uiux_visual_review" for item in self.state.get("specialists", [])
        ):
            self.state.setdefault("specialists", []).append({"role": "uiux_visual_review", "reason": "Visual inputs provided"})

    # State Machine Delegation to Role Agents
    def step_planning(self):
        return self.manager.step_planning()

    def step_developing_plan(self):
        return self.developer.step_developing_plan()

    def step_reviewing_plan(self):
        return self.architect.step_reviewing_plan()

    def step_implementing(self):
        return self.developer.step_implementing()

    def step_testing(self):
        return self.qa.step_testing()

    def step_reviewing_code(self):
        return self.reviewer.step_reviewing_code()

    def step_completed(self):
        return self.manager.step_completed()

    def step_researching(self):
        log_header("RESEARCHING (Sales / RA)")
        requirements = self.requirements_path.read_text(encoding="utf-8")
        request = self.request_path.read_text(encoding="utf-8")
        reports = self.consult_specialists(requirements, "Research-only workflow")
        prompt = f"""Write a human-readable research report in the same language as the request.\n\nRequest:\n{request}\n\nResearch reports:\n{reports}\n\nUse sections: Outcome, Findings, Risks and Caveats, Recommended Next Steps. State that legal conclusions require human legal review."""
        report = self.assistant.call_agent("assistant", prompt, "You synthesize factual Sales and RA research for a human decision maker.")
        self.human_report_path.write_text(report, encoding="utf-8")
        self.meeting_memory_path.write_text(report, encoding="utf-8")
        self.state["state"] = "COMPLETED"
        self.save_state()
        log_success(f"Research report saved to {self.human_report_path}")

    def parse_and_write_files(self, text: str, allowed_files: list[str] | None = None, allowed_heading: str | None = None) -> list[str]:
        return self.developer.parse_and_write_files(text, allowed_files, allowed_heading=allowed_heading)

    # State Dispatching
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
        elif current_state == "RESEARCHING":
            self.step_researching()
        elif current_state == "WAITING_FOR_OWNER":
            log_warning("Currently waiting for owner approval. Edit state.json to resume.")
        elif current_state == "FAILED":
            log_error("Workflow failed. Please reset or start a new task.")
        else:
            log_error(f"Unknown state: {current_state}")

    def run_to_end(self):
        self.load_config_and_state()
        while self.state["state"] not in ["COMPLETED", "WAITING_FOR_OWNER", "FAILED"]:
            try:
                self.step()
            except Exception as error:
                failed_state = self.state.get("state", "PLANNING")
                self.pause_for_human_review("Orchestrator", str(error), failed_state, failed_state)
                return
            self.load_config_and_state()
        if self.state["state"] == "COMPLETED":
            self.step()
