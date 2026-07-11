from __future__ import annotations
import sys
import json
import re
from orchestrator.roles.base_agent import BaseAgent

class DeveloperAgent(BaseAgent):
    def step_developing_plan(self):
        from orchestrator.core.state import log_header, log_success, log_info, log_warning

        log_header("2. DEVELOPING PLAN (Developer)")
        with open(self.orchestrator.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()

        prompt = (
            f"Please read the following project requirements:\n\n"
            f"{requirements}\n\n"
            f"Draft a step-by-step implementation plan in Markdown. The plan should include:\n"
            f"1. Target files to create or modify.\n"
            f"2. Specific changes or logic details for each file.\n"
            f"3. Sequence of work (action items).\n"
            f"4. Testing strategy.\n\n"
            f"Use these exact Markdown headings: '## Target Files', '## Implementation Steps', and '## Verification'.\n\n"
            f"Write ONLY the Markdown implementation plan. Do not include any conversational preamble or postscript."
        )

        if self.orchestrator.state["plan_revisions"] > 0 and self.orchestrator.reviewer_output_path.exists():
            with open(self.orchestrator.reviewer_output_path, "r", encoding="utf-8") as f:
                feedback = f.read()
            prompt = (
                f"Your previous implementation plan was REJECTED by the reviewer with feedback:\n\n"
                f"{feedback}\n\n"
                f"Please revise the implementation plan to address all reviewer comments.\n"
                f"Write the complete updated implementation plan in Markdown using these exact headings: '## Target Files', '## Implementation Steps', and '## Verification'. Only output the plan content."
            )

        system_prompt = "You are a Lead Software Developer. Generate a clear step-by-step implementation plan."
        plan = self.call_agent("developer_senior", prompt, system_prompt)

        with open(self.orchestrator.plan_path, "w", encoding="utf-8") as f:
            f.write(plan)
        log_success(f"Implementation plan generated and saved to {self.orchestrator.plan_path}")

        # Manager assigns tasks and scales the RD/QA team within configured capacity.
        log_info("Parsing implementation plan into structured action items...")
        capacity = self.orchestrator.config.get("staffing_limits", {})
        capabilities = {
            "backends": self.orchestrator.config.get("backends", {}),
            "model_tiers": self.orchestrator.config.get("model_tiers", {}),
            "role_models": self.orchestrator.config.get("role_models", {}),
        }
        workload = {
            "plan_characters": len(plan),
            "plan_revisions": self.orchestrator.state.get("plan_revisions", 0),
            "code_revisions": self.orchestrator.state.get("code_revisions", 0),
        }
        parse_prompt = f"""Read this implementation plan:\n\n{plan}\n\nCreate a JSON object with:\n- 'tasks': a flat array of coding tasks. Each has 'id', 'description', 'status': 'pending', 'complexity' ('routine', 'moderate', or 'complex'), plus independent 'rd_level' and 'qa_level' fields ('junior', 'middle', or 'senior'). Assign isolated repetitive implementation to junior RD, ordinary known-pattern features to middle RD, and architecture, cross-module, security, migration, ambiguity, or design work to senior RD. Set QA level independently based on the testing risk.\n- 'staffing': an allocation based on task count/scope, available capacity, capabilities, and workload below. Include only workers required by the rd_level and qa_level assignments.\n- 'specialists': only include relevant roles: 'sales' for business scope, 'security' for auth/secrets/payment/PII, 'ra' for compliance, 'sre' for monitoring, 'devops' for CI/CD/deployment/containers/rollback, 'uiux' for UI/user flows/accessibility, 'uiux_visual_review' when screenshots/mockups need review, 'fae' for customer environments/hardware/SDK validation, and 'integration' for APIs/protocols/third-party systems. Each item has 'role' and a short 'reason'.\n\nAvailable capacity:\n{json.dumps(capacity)}\n\nCapabilities:\n{json.dumps(capabilities)}\n\nWorkload:\n{json.dumps(workload)}\n\nThe staffing object must contain rd and qa, each with integer senior, middle, and junior counts. Respond ONLY with valid JSON."""

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
            docs_only = "readme" in requirements.lower() and any(marker in requirements.lower() for marker in ("only allow modifying", "only allowed to modify", "only modify these", "只允許修改", "僅允許修改"))
            if docs_only:
                request = self.orchestrator.request_path.read_text(encoding="utf-8")
                files = [name for name in ("README.md", "README_en.md", "README_ja.md", "README_zh-CN.md") if name in request]
                tasks = [
                    {"id": f"DOCS-{index}", "description": f"Update only {name} according to this request:\n{request}", "status": "pending", "complexity": "routine", "rd_level": "junior", "qa_level": "junior"}
                    for index, name in enumerate(files, 1)
                ]
                self.orchestrator.state["staffing"] = {"rd": {"junior": 1}, "qa": {"junior": 1}}
            if isinstance(parsed, dict):
                if not docs_only:
                    self.orchestrator.state["staffing"] = parsed.get("staffing", self.orchestrator.state.get("staffing", {}))
                specialists = parsed.get("specialists", [])
                self.orchestrator.state["specialists"] = [
                    item for item in specialists
                    if isinstance(item, dict) and item.get("role") in {"sales", "security", "ra", "sre", "devops", "uiux", "uiux_visual_review", "fae", "integration"}
                ]
                self.orchestrator.ensure_visual_review_specialist()
            for task in tasks:
                if not isinstance(task, dict) or not task.get("id") or not task.get("description"):
                    raise ValueError("each task requires id and description")
                task["status"] = "pending"
                if task.get("complexity") not in {"routine", "moderate", "complex"}:
                    task["complexity"] = "complex"
                legacy_level = task.get("assignee_level", "senior")
                for role in ("rd", "qa"):
                    level_key = f"{role}_level"
                    if task.get(level_key) not in {"junior", "middle", "senior"}:
                        task[level_key] = legacy_level if legacy_level in {"junior", "middle", "senior"} else "senior"
            self.orchestrator.allocate_workers("rd", tasks)
            self.orchestrator.allocate_workers("qa", tasks)
            # Retain completed work only when the revised task is unchanged.
            task_signature = lambda task: tuple(
                task.get(key) for key in ("id", "description", "complexity", "rd_level", "qa_level")
            )
            existing_completed = {
                task_signature(t) for t in self.orchestrator.state["tasks"] if t.get("status") == "completed"
            }
            for t in tasks:
                if task_signature(t) in existing_completed:
                    t["status"] = "completed"
            self.orchestrator.state["tasks"] = tasks
            self.orchestrator.save_state()
            self.orchestrator.write_agent_context()
            with open(self.orchestrator.action_items_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2)
            log_success(f"Saved {len(tasks)} tasks to {self.orchestrator.action_items_path}")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log_warning(f"Could not parse tasks as JSON. Saving raw output. Error: {e}")
            log_warning(f"Raw manager output was: {parsed_items_raw}")
            # Write a fallback task
            fallback_tasks = [{"id": "TASK-001", "description": "Implement overall implementation plan", "status": "pending", "rd_level": "senior", "qa_level": "senior"}]
            self.orchestrator.state["tasks"] = fallback_tasks
            self.orchestrator.state["specialists"] = []
            self.orchestrator.ensure_visual_review_specialist()
            self.orchestrator.state["staffing"] = {"rd": {"senior": 1}, "qa": {"senior": 1}}
            self.orchestrator.allocate_workers("rd", fallback_tasks)
            self.orchestrator.allocate_workers("qa", fallback_tasks)
            self.orchestrator.save_state()
            self.orchestrator.write_agent_context()

        self.orchestrator.state["state"] = "REVIEWING_PLAN"
        self.orchestrator.save_state()

    def parse_and_write_files(self, text: str) -> list[str]:
        from orchestrator.core.state import log_success, log_warning
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
            base_dir = self.orchestrator.workspace
            if self.orchestrator.config.get("use_worktree", True):
                wt_path = self.orchestrator.ai_dir / "worktree"
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
        from orchestrator.core.state import log_header, log_error, log_success, log_info, log_warning

        log_header("4. IMPLEMENTING CODE (Developer)")
        if not self.orchestrator.requirements_path.exists() or not self.orchestrator.plan_path.exists():
            log_error("Requirements or Plan missing. Cannot implement.")
            sys.exit(1)

        with open(self.orchestrator.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.orchestrator.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()

        tasks = self.orchestrator.state.get("tasks", [])
        pending_tasks = [t for t in tasks if t["status"] == "pending"]

        if not pending_tasks:
            log_success("All tasks are already marked completed.")
            self.orchestrator.state["state"] = "TESTING"
            self.orchestrator.save_state()
            return

        log_info(f"Found {len(pending_tasks)} pending tasks out of {len(tasks)} total tasks.")

        developer_logs = []
        _, rd_assignments = self.orchestrator.allocate_workers("rd", tasks)
        for task in pending_tasks:
            log_info(f"Implementing Task {task['id']}: {task['description']}")
            worker_id = rd_assignments[task["id"]]
            _, level, agent_number = worker_id.rsplit("-", 2)
            agent_role = f"developer_{level}"
            effective_role = self.orchestrator.state.get("developer_promotions", {}).get(agent_role, agent_role)
            backend = self.orchestrator.get_backend(effective_role)

            prompt = (
                f"We are implementing the project in the workspace root. Here are the requirements:\n"
                f"```markdown\n{requirements}\n```\n\n"
                f"And here is our implementation plan:\n"
                f"```markdown\n{plan}\n```\n\n"
                f"Please implement the following task:\n"
                f"Task ID: {task['id']}\n"
                f"Description: {task['description']}\n\n"
            )

            if backend in ["ollama", "gemini", "agy", "grok"]:
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

            if self.orchestrator.state["code_revisions"] > 0:
                feedback = ""
                if self.orchestrator.qa_report_path.exists():
                    with open(self.orchestrator.qa_report_path, "r", encoding="utf-8") as f:
                        feedback += f"\n--- QA Feedback ---\n{f.read()}\n"
                if self.orchestrator.test_results_path.exists():
                    with open(self.orchestrator.test_results_path, "r", encoding="utf-8") as f:
                        feedback += f"\n--- Test Results ---\n{f.read()}\n"
                if self.orchestrator.reviewer_output_path.exists():
                    with open(self.orchestrator.reviewer_output_path, "r", encoding="utf-8") as f:
                        feedback += f"\n--- Code Review Feedback ---\n{f.read()}\n"
                if feedback:
                    prompt += f"\n\nNote: The previous implementation had issues. Feedback:\n{feedback}\nPlease fix these issues."

            system_prompt = f"You are a {level.title()} AI Developer"
            system_prompt += f" (RD {agent_number}). Write and edit code to fulfill the task."
            dev_output = self.call_agent(effective_role, prompt, system_prompt)
            self.orchestrator.state["last_developer_role"] = agent_role

            if backend in ["ollama", "gemini", "agy", "grok"]:
                written = self.parse_and_write_files(dev_output)
                if written:
                    log_success(f"Successfully processed files written by Developer: {', '.join(written)}")
                else:
                    log_warning("No files were parsed from Developer response. Ensure they used [FILE_START: path] blocks.")

            developer_logs.append(f"--- Task {task['id']} implementation output ---\n{dev_output}\n")
            task["status"] = "completed"
            self.orchestrator.save_state()

            with open(self.orchestrator.action_items_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2)

        with open(self.orchestrator.developer_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(developer_logs))

        log_success("All pending tasks processed.")
        self.orchestrator.state["state"] = "TESTING"
        self.orchestrator.save_state()
