from __future__ import annotations
import sys
import json
import re
from pathlib import Path
from orchestrator.roles.base_agent import BaseAgent, extract_json_response, is_strict_json_response

TASK_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {"type": "array", "items": {"type": "object"}},
        "staffing": {"type": "object"},
        "specialists": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["tasks", "staffing", "specialists"],
    "additionalProperties": True,
}

def is_task_plan_response(response: str) -> bool:
    try:
        parsed = json.loads(response.strip())
        return isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False

class DeveloperAgent(BaseAgent):
    def _markdown_headings(self, base_dir: Path, filepath: str) -> list[str]:
        path = base_dir / filepath
        if path.suffix.lower() != ".md" or not path.is_file():
            return []
        content = path.read_text(encoding="utf-8")
        return [
            line for line in content.splitlines()
            if re.match(r"^#{1,6}\s+\S", line)
        ]

    def _section_heading_for_file(
        self,
        base_dir: Path,
        filepath: str,
        requested_heading: str | None,
        reference_headings: list[str] | None = None,
    ) -> str | None:
        headings = self._markdown_headings(base_dir, filepath)
        if not requested_heading:
            return None
        if requested_heading in headings:
            return requested_heading
        if reference_headings and requested_heading in reference_headings:
            index = reference_headings.index(requested_heading)
            if index < len(headings):
                return headings[index]
        return None

    def _reference_headings_for_fix_task(
        self,
        tasks: list[dict],
        task: dict,
        base_dir: Path,
    ) -> list[str]:
        requested_heading = task.get("section_heading")
        if not requested_heading:
            return []
        task_root = re.sub(r"-\d+$", "", str(task.get("id", "")))
        candidates = [task]
        candidates.extend(
            candidate for candidate in tasks
            if candidate is not task
            and re.sub(r"-\d+$", "", str(candidate.get("id", ""))) == task_root
        )
        for candidate in candidates:
            for filepath in candidate.get("target_files", []):
                headings = self._markdown_headings(base_dir, filepath)
                if requested_heading in headings:
                    return headings
        return []

    def step_developing_plan(self):
        from orchestrator.core.state import log_header, log_success, log_info, log_warning

        log_header("2. DEVELOPING PLAN (Developer)")
        with open(self.orchestrator.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()

        planning_input = requirements
        if self.orchestrator.state["plan_revisions"] > 0 and self.orchestrator.reviewer_output_path.exists():
            with open(self.orchestrator.reviewer_output_path, "r", encoding="utf-8") as f:
                feedback = f.read()
            planning_input += f"\n\nPrevious plan review feedback:\n{feedback}"

        log_info("Creating structured action items from requirements...")
        capacity = self.orchestrator.config.get("staffing_limits", {})
        capabilities = {
            "backends": self.orchestrator.config.get("backends", {}),
            "model_tiers": self.orchestrator.config.get("model_tiers", {}),
            "role_models": self.orchestrator.config.get("role_models", {}),
        }
        workload = {
            "requirements_characters": len(planning_input),
            "plan_revisions": self.orchestrator.state.get("plan_revisions", 0),
            "code_revisions": self.orchestrator.state.get("code_revisions", 0),
        }
        request_text = self.orchestrator.request_path.read_text(encoding="utf-8") if self.orchestrator.request_path.exists() else planning_input
        requested_files = []
        for name in re.findall(r"[A-Za-z0-9_.\-/]+\.(?:md|py)(?![A-Za-z0-9_])", request_text, re.IGNORECASE):
            path = Path(name)
            target = (self.orchestrator.workspace / path).resolve()
            if not path.is_absolute() and self.orchestrator.workspace in target.parents:
                requested_files.append(str(path))
        requested_files = list(dict.fromkeys(requested_files))
        requested_markdown = [
            name for name in requested_files if Path(name).suffix.lower() == ".md"
        ]
        markdown_headings = {}
        for name in requested_markdown:
            path = self.orchestrator.workspace / name
            markdown_headings[name] = [
                line for line in path.read_text(encoding="utf-8").splitlines()
                if re.match(r"^#{1,6}\s+\S", line)
            ] if path.is_file() else []
        docs_only = bool(requested_files) and any(marker in requirements.lower() for marker in ("only allow modifying", "only allowed to modify", "only allow adding", "only allowed to add", "only modify these", "只允許修改", "僅允許修改", "只允許新增", "僅允許新增"))
        language_rewrite = any(marker in f"{request_text}\n{requirements}".lower() for marker in (
            "swap", "role-swap", "exchange", "translation", "multilingual", "localization",
            "互換", "交換", "語言角色", "語系角色", "多國語系", "整份翻譯", "翻譯成",
        ))
        whole_file_docs = bool(requested_markdown) and (
            language_rewrite
            or (
                docs_only and (
                    any(marker in request_text.lower() for marker in ("one task per file", "one task for each", "do not split", "禁止再依 markdown heading 拆分", "每個語系一個 task"))
                    or any(not (self.orchestrator.workspace / name).is_file() for name in requested_markdown)
                )
            )
        )
        markdown_task_rule = (
            "For this documentation-only request, create exactly one whole-file task per requested Markdown file. Do not split by heading and do not include section_heading."
            if whole_file_docs else
            "For existing Markdown files, create one task per independent heading-bounded section, set target_files to exactly one file, and add section_heading copied exactly from the heading inventory below."
        )
        file_coverage_rule = (
            f"Cover every requested file exactly once, including non-Markdown files, as a whole-file task. Requested files: {json.dumps(requested_files, ensure_ascii=False)}."
            if docs_only or whole_file_docs else
            ""
        )
        parse_prompt = f"""Read these project requirements:\n\n{planning_input}\n\nCreate a JSON object with:\n- 'tasks': a flat array of coding tasks. Each has 'id', 'description', non-empty 'target_files' (relative paths), 'status': 'pending', 'complexity' ('routine', 'moderate', or 'complex'), plus independent 'rd_level' and 'qa_level' fields ('junior', 'middle', or 'senior'). Include only tasks that modify one or more project files. Never emit planning, inventory, inspection, research, or verification-only tasks. {markdown_task_rule} {file_coverage_rule} Assign isolated repetitive implementation to junior RD, ordinary known-pattern features to middle RD, and architecture, cross-module, security, migration, ambiguity, or design work to senior RD. Set QA level independently based on the testing risk.\n- 'staffing': an allocation based on task count/scope, available capacity, capabilities, and workload below. Include only workers required by the rd_level and qa_level assignments.\n- 'specialists': only include relevant roles: 'sales' for business scope, 'security' for auth/secrets/payment/PII, 'ra' for compliance, 'sre' for monitoring, 'devops' for CI/CD/deployment/containers/rollback, 'uiux' for UI/user flows/accessibility, 'uiux_visual_review' when screenshots/mockups need review, 'fae' for customer environments/hardware/SDK validation, and 'integration' for APIs/protocols/third-party systems. Each item has 'role' and a short 'reason'.\n\nExisting Markdown heading inventory:\n{json.dumps(markdown_headings, ensure_ascii=False)}\n\nAvailable capacity:\n{json.dumps(capacity)}\n\nCapabilities:\n{json.dumps(capabilities)}\n\nWorkload:\n{json.dumps(workload)}\n\nThe staffing object must contain rd and qa, each with integer senior, middle, and junior counts. Respond ONLY with valid JSON."""

        parsed_items_raw = self.call_manager(
            parse_prompt,
            "You are a Project Manager. Output exactly one valid JSON object, with no Markdown fences, comments, explanation, headings, or prose. The first character must be { and the last character must be }.",
            is_task_plan_response,
            TASK_PLAN_SCHEMA,
        )

        # Clean potential markdown wrapping
        clean_json = parsed_items_raw.strip()

        try:
            parsed = json.loads(clean_json)
            tasks = parsed if isinstance(parsed, list) else parsed["tasks"]
            if not isinstance(tasks, list):
                raise ValueError("tasks must be a JSON array")
            planning_prefixes = ("gather ", "inventory ", "inspect ", "review ", "verify ", "validate ", "produce ", "research ", "盤點", "檢查", "審查", "驗證", "蒐集", "研究")
            tasks = [task for task in tasks if not str(task.get("description", "")).lstrip().lower().startswith(planning_prefixes)]
            if not tasks:
                raise ValueError("Manager returned no file-change tasks")
            if docs_only or whole_file_docs:
                files = requested_files
                allowed = set(files)
                markdown_allowed = set(requested_markdown)
                if whole_file_docs:
                    by_file = {}
                    for task in tasks:
                        targets = task.get("target_files")
                        if isinstance(targets, list) and len(targets) == 1 and targets[0] in allowed:
                            task.pop("section_heading", None)
                            by_file.setdefault(targets[0], task)
                    tasks = list(by_file.values())
                else:
                    tasks = [
                        task for task in tasks
                        if task.get("target_files")
                        and len(task["target_files"]) == 1
                        and (
                            task["target_files"][0] in allowed
                            and (
                                task["target_files"][0] not in markdown_allowed
                                or task.get("section_heading") in markdown_headings.get(task["target_files"][0], [])
                            )
                        )
                    ]
                covered = {task["target_files"][0] for task in tasks}
                if covered != allowed:
                    raise ValueError("Manager must create one valid task for every requested file")
            if isinstance(parsed, dict):
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
                target_files = task.get("target_files", [])
                if not isinstance(target_files, list) or not target_files or not all(isinstance(path, str) and path and not path.startswith("/") for path in target_files):
                    raise ValueError("each task requires non-empty relative target_files")
                description_lower = str(task.get("description", "")).lower()
                navigation_only = (
                    whole_file_docs
                    and any(
                        marker in description_lower
                        for marker in ("language navigation", "language-switcher", "navigation", "語言導覽", "語系導覽", "導覽", "导航")
                    )
                    and any(
                        marker in description_lower
                        for marker in ("leave body", "body content", "otherwise unchanged", "preserve", "保留", "不變", "保持")
                    )
                )
                if navigation_only:
                    task["output_contract"] = {"format": "file_edits", "response_must_start_with": "[FILE_EDIT_START:", "allow_prose": False}
                elif task.get("section_heading"):
                    task["output_contract"] = {"format": "markdown_section_replacements", "response_must_start_with": "[SECTION_EDIT_START:", "allow_prose": False}
                else:
                    task["output_contract"] = {"format": "file_blocks", "response_must_start_with": "[FILE_START:", "allow_prose": False}
                if (
                    whole_file_docs
                    and any(Path(path).suffix.lower() == ".md" for path in target_files)
                    and (
                        language_rewrite
                        and any(Path(path).name in {"README.md", "README_en.md"} for path in target_files)
                        or any(
                            marker in str(task.get("description", "")).lower()
                            for marker in ("swap", "interchange", "full body", "full content", "互換", "交換", "完整內容", "整份")
                        )
                    )
                ):
                    task["output_contract"]["allow_markdown_heading_changes"] = True
                if task.get("complexity") not in {"routine", "moderate", "complex"}:
                    task["complexity"] = "complex"
                legacy_level = task.get("assignee_level", "senior")
                for role in ("rd", "qa"):
                    level_key = f"{role}_level"
                    if task.get(level_key) not in {"junior", "middle", "senior"}:
                        task[level_key] = legacy_level if legacy_level in {"junior", "middle", "senior"} else "senior"
            target_files = list(dict.fromkeys(path for task in tasks for path in task["target_files"]))
            plan = "# Implementation Plan\n\n## Target Files\n" + "".join(f"- `{path}`\n" for path in target_files)
            plan += "\n## Implementation Steps\n" + "".join(f"- {task['description']}\n" for task in tasks)
            plan += "\n## Verification\n- Run the configured test command.\n- Run `git diff --check`.\n"
            self.orchestrator.plan_path.write_text(plan, encoding="utf-8")
            log_success(f"Implementation plan generated and saved to {self.orchestrator.plan_path}")
            self.orchestrator.allocate_workers("rd", tasks)
            self.orchestrator.allocate_workers("qa", tasks)
            # Retain completed work only when the revised task is unchanged.
            task_signature = lambda task: tuple(
                task.get(key) for key in ("id", "description", "section_heading", "complexity", "rd_level", "qa_level")
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
            log_warning(f"Could not create file-change tasks. Error: {e}")
            self.orchestrator.pause_for_human_review("Manager", parsed_items_raw, "DEVELOPING_PLAN", "DEVELOPING_PLAN")
            return

        self.orchestrator.state["state"] = "REVIEWING_PLAN"
        self.orchestrator.save_state()

    def parse_and_write_files(
        self,
        text: str,
        allowed_files: list[str] | None = None,
        dry_run: bool = False,
        allowed_heading: str | None = None,
        allow_markdown_heading_changes: bool = False,
    ) -> list[str]:
        from orchestrator.core.state import log_success, log_warning
        pattern = re.compile(r'\[FILE_START:\s*(.*?)\](.*?)\[FILE_END:\s*\1\]', re.DOTALL)
        matches = pattern.findall(text)
        edit_pattern = re.compile(
            r'\[FILE_EDIT_START:\s*(.*?)\]\s*\[OLD\]\n?(.*?)\n?\[NEW\]\n?(.*?)\n?\[FILE_EDIT_END:\s*\1\]',
            re.DOTALL,
        )
        edits = edit_pattern.findall(text)
        section_pattern = re.compile(
            r'\[SECTION_EDIT_START:\s*(.*?)\]\s*\[HEADING\]\n?(.*?)\n?\[CONTENT\]\n?(.*?)\n?\[SECTION_EDIT_END:\s*\1\]',
            re.DOTALL,
        )
        section_edits = section_pattern.findall(text)
        if not matches and not edits and not section_edits and allowed_files and len(allowed_files) == 1:
            filepath = str(Path(allowed_files[0]))
            candidate = text.strip()
            if Path(filepath).suffix.lower() == ".md" and candidate.startswith("#"):
                if candidate.startswith("```") and candidate.endswith("```"):
                    candidate = "\n".join(candidate.splitlines()[1:-1]).strip()
                matches = [(filepath, candidate)]
        allowed = {str(Path(path)) for path in allowed_files} if allowed_files is not None else None

        written_files = []
        for filepath_str, content in matches:
            filepath_str = filepath_str.strip()
            content = content.strip()
            if not content:
                log_warning(f"Skipping empty file content: {filepath_str}")
                continue
            if allowed is not None and str(Path(filepath_str)) not in allowed:
                log_warning(f"Skipping file not declared by task contract: {filepath_str}")
                continue

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
            if target_path.suffix.lower() == ".md" and target_path.exists():
                original = target_path.read_text(encoding="utf-8")
                if content == original:
                    log_warning(f"Skipping unchanged file response: {filepath_str}")
                    continue
                headings = [line for line in original.splitlines() if re.match(r"^#{1,6}\s+\S", line)]
                if any(heading not in content for heading in headings) and not allow_markdown_heading_changes:
                    log_warning(f"Skipping Markdown rewrite that removes existing headings: {filepath_str}")
                    continue

            if dry_run:
                written_files.append(filepath_str)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
            written_files.append(filepath_str)
            log_success(f"Developer wrote file: {filepath_str}")

        for filepath_str, old, new in edits:
            filepath_str = filepath_str.strip()
            old = old.strip("\n")
            new = new.strip("\n")
            if allowed is not None and str(Path(filepath_str)) not in allowed:
                log_warning(f"Skipping file not declared by task contract: {filepath_str}")
                continue

            base_dir = self.orchestrator.workspace
            worktree = self.orchestrator.ai_dir / "worktree"
            if self.orchestrator.config.get("use_worktree", True) and worktree.exists():
                base_dir = worktree
            target_path = (base_dir / filepath_str).resolve()
            if base_dir not in target_path.parents or not target_path.is_file():
                log_warning(f"Skipping edit for missing or unsafe file: {filepath_str}")
                continue

            original = target_path.read_text(encoding="utf-8")
            if not old or original.count(old) != 1:
                log_warning(f"Skipping edit whose OLD text is not unique: {filepath_str}")
                continue
            updated = original.replace(old, new, 1)
            if updated == original:
                log_warning(f"Skipping unchanged edit response: {filepath_str}")
                continue
            headings = [line for line in original.splitlines() if re.match(r"^#{1,6}\s+\S", line)]
            if any(heading not in updated for heading in headings):
                log_warning(f"Skipping Markdown edit that removes existing headings: {filepath_str}")
                continue
            if not dry_run:
                target_path.write_text(updated, encoding="utf-8")
                log_success(f"Developer edited file: {filepath_str}")
            written_files.append(filepath_str)

        for filepath_str, heading, content in section_edits:
            filepath_str = filepath_str.strip()
            heading = heading.strip()
            content = content.strip("\n")
            if not content.strip():
                log_warning(f"Skipping empty section content: {filepath_str}: {heading}")
                continue
            if allowed is not None and str(Path(filepath_str)) not in allowed:
                log_warning(f"Skipping file not declared by task contract: {filepath_str}")
                continue
            if allowed_heading and heading != allowed_heading:
                if not heading:
                    heading = allowed_heading
                else:
                    log_warning(f"Skipping section not declared by task contract: {heading}")
                    continue
            if allowed_heading is None:
                log_warning(f"Skipping section block for a file-level task: {filepath_str}")
                continue

            base_dir = self.orchestrator.workspace
            worktree = self.orchestrator.ai_dir / "worktree"
            if self.orchestrator.config.get("use_worktree", True) and worktree.exists():
                base_dir = worktree
            target_path = (base_dir / filepath_str).resolve()
            if base_dir not in target_path.parents or not target_path.is_file():
                log_warning(f"Skipping section edit for missing or unsafe file: {filepath_str}")
                continue

            original = target_path.read_text(encoding="utf-8")
            lines = original.splitlines(keepends=True)
            indices = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == heading]
            heading_match = re.match(r"^(#{1,6})\s+\S", heading)
            if len(indices) != 1 or not heading_match:
                log_warning(f"Skipping edit whose heading is missing or ambiguous: {filepath_str}: {heading}")
                continue
            start = indices[0] + 1
            level = len(heading_match.group(1))
            end = len(lines)
            for index in range(start, len(lines)):
                next_heading = re.match(r"^(#{1,6})\s+\S", lines[index])
                if next_heading and len(next_heading.group(1)) <= level:
                    end = index
                    break
            original_body = "".join(lines[start:end]).strip()
            if content.strip() == original_body:
                log_warning(f"Skipping unchanged section response: {filepath_str}: {heading}")
                continue
            updated = "".join(lines[:start]) + f"\n{content}\n\n" + "".join(lines[end:])
            if updated == original:
                log_warning(f"Skipping unchanged section response: {filepath_str}: {heading}")
                continue
            headings = [line.rstrip("\r\n") for line in lines if re.match(r"^#{1,6}\s+\S", line)]
            if any(existing_heading not in updated for existing_heading in headings):
                log_warning(f"Skipping Markdown section edit that removes existing headings: {filepath_str}")
                continue
            if not dry_run:
                target_path.write_text(updated, encoding="utf-8")
                log_success(f"Developer edited section {heading} in {filepath_str}")
            written_files.append(filepath_str)
        return list(dict.fromkeys(written_files))

    def retry_task_or_pause(self, task: dict, error: str) -> None:
        from orchestrator.core.state import log_info

        self.orchestrator.escalate_developer_backend()
        max_revisions = self.orchestrator.config.get("max_revisions", 2)
        revisions = int(task.get("revisions", 0))
        if revisions < max_revisions:
            task["revisions"] = revisions + 1
            self.orchestrator.state["state"] = "IMPLEMENTING"
            self.orchestrator.state["last_developer_error"] = error
            self.orchestrator.save_state()
            self.orchestrator.action_items_path.write_text(json.dumps(self.orchestrator.state["tasks"], indent=2), encoding="utf-8")
            log_info(f"Revising task {task['id']} (Revision {task['revisions']}/{max_revisions})...")
            return
        self.orchestrator.pause_for_human_review("Developer", error, "IMPLEMENTING", "IMPLEMENTING")

    def step_implementing(self):
        from orchestrator.core.state import log_header, log_error, log_success, log_info, log_warning

        log_header("4. IMPLEMENTING CODE (Developer)")
        if not self.orchestrator.requirements_path.exists() or not self.orchestrator.plan_path.exists():
            log_error("Requirements or Plan missing. Cannot implement.")
            sys.exit(1)

        tasks = self.orchestrator.state.get("tasks", [])
        for task in tasks:
            if task.get("status") != "pending" or not str(task.get("id", "")).startswith("FIX-"):
                continue
            section_base = self.orchestrator.workspace
            worktree = self.orchestrator.ai_dir / "worktree"
            if self.orchestrator.config.get("use_worktree", True) and worktree.exists():
                section_base = worktree
            reference_headings = self._reference_headings_for_fix_task(tasks, task, section_base)
            for filepath in task.get("target_files", []):
                heading = self._section_heading_for_file(
                    section_base,
                    filepath,
                    task.get("section_heading"),
                    reference_headings,
                )
                if heading and len(task.get("target_files", [])) == 1:
                    task["section_heading"] = heading
                    task["output_contract"] = {
                        "format": "markdown_section_replacements",
                        "response_must_start_with": "[SECTION_EDIT_START:",
                        "allow_prose": False,
                    }
                    log_info(f"Scoped {task['id']} to damaged section {task['section_heading']}.")
                break
        for task in list(tasks):
            if task.get("status") != "pending" or not str(task.get("id", "")).startswith("FIX-"):
                continue
            target_files = task.get("target_files") or list(dict.fromkeys(
                path for existing_task in tasks for path in existing_task.get("target_files", [])
            ))
            if len(target_files) <= 1:
                continue
            task_index = tasks.index(task)
            task_id = task["id"]
            section_base = self.orchestrator.workspace
            worktree = self.orchestrator.ai_dir / "worktree"
            if self.orchestrator.config.get("use_worktree", True) and worktree.exists():
                section_base = worktree
            reference_headings = self._reference_headings_for_fix_task(tasks, task, section_base)
            split_tasks = []
            for index, path in enumerate(target_files, 1):
                split_task = {
                    **task,
                    "id": f"{task_id}-{index}",
                    "target_files": [path],
                    "output_contract": {
                        "format": "markdown_section_replacements" if task.get("section_heading") else "file_blocks",
                        "response_must_start_with": "[SECTION_EDIT_START:" if task.get("section_heading") else "[FILE_START:",
                        "allow_prose": False,
                    },
                }
                heading = self._section_heading_for_file(
                    section_base,
                    path,
                    task.get("section_heading"),
                    reference_headings,
                )
                if heading:
                    split_task["section_heading"] = heading
                    split_task["output_contract"] = {
                        "format": "markdown_section_replacements",
                        "response_must_start_with": "[SECTION_EDIT_START:",
                        "allow_prose": False,
                    }
                split_tasks.append(split_task)
            tasks[task_index:task_index + 1] = split_tasks
            for state_key in ("task_developer_promotions", "task_failed_model_routes"):
                task_state = self.orchestrator.state.setdefault(state_key, {})
                inherited = task_state.pop(task_id, None)
                if inherited is not None:
                    for split_task in split_tasks:
                        task_state[split_task["id"]] = inherited.copy()
            if self.orchestrator.state.get("active_task_id") == task_id:
                self.orchestrator.state.pop("active_task_id")
            log_info(f"Split multi-file revision {task_id} into {len(split_tasks)} file-scoped tasks.")
            self.orchestrator.save_state()
            with open(self.orchestrator.action_items_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2)
        pending_tasks = [t for t in tasks if t["status"] == "pending"]

        if not pending_tasks:
            log_success("All tasks are already marked completed.")
            self.orchestrator.state.pop("active_task_id", None)
            self.orchestrator.state["state"] = "TESTING"
            self.orchestrator.save_state()
            return

        log_info(f"Found {len(pending_tasks)} pending tasks out of {len(tasks)} total tasks.")

        developer_logs = []
        _, rd_assignments = self.orchestrator.allocate_workers("rd", tasks)
        for task in pending_tasks:
            self.orchestrator.state["active_task_id"] = task["id"]
            log_info(f"Implementing Task {task['id']}: {task['description']}")
            description = task["description"].lstrip().lower()
            if description.startswith(("inventory ", "inspect ", "review ", "verify ", "validate ", "盤點", "檢查", "審查", "驗證")):
                log_info(f"Task {task['id']} is read-only; no developer response required.")
                task["status"] = "completed"
                self.orchestrator.state.pop("active_task_id", None)
                self.orchestrator.save_state()
                with open(self.orchestrator.action_items_path, "w", encoding="utf-8") as f:
                    json.dump(tasks, f, indent=2)
                continue
            worker_id = rd_assignments[task["id"]]
            _, level, agent_number = worker_id.rsplit("-", 2)
            agent_role = f"developer_{level}"
            task_promotions = self.orchestrator.state.setdefault("task_developer_promotions", {}).setdefault(task["id"], {})
            effective_role = task_promotions.get(agent_role, agent_role)
            backend = self.orchestrator.get_backend(effective_role)

            target_files = task.get("target_files", [])
            if not target_files and str(task.get("id", "")).startswith("FIX-"):
                target_files = list(dict.fromkeys(
                    path
                    for existing_task in tasks
                    for path in existing_task.get("target_files", [])
                ))
                task["target_files"] = target_files
                task["output_contract"] = {
                    "format": "file_blocks",
                    "response_must_start_with": "[FILE_START:",
                    "allow_prose": False,
                }
            machine_contract = {
                "stage": "IMPLEMENTING",
                "allowed_actions": ["modify_files"],
                "target_files": target_files,
                "output_contract": task.get("output_contract", {}),
            }
            base_dir = self.orchestrator.workspace
            worktree = self.orchestrator.ai_dir / "worktree"
            if self.orchestrator.config.get("use_worktree", True) and worktree.exists():
                base_dir = worktree
            if task.get("section_heading"):
                # A scoped repair is always section-based, even if an older
                # persisted task still carries the file-level contract.
                task["output_contract"] = {
                    "format": "markdown_section_replacements",
                    "response_must_start_with": "[SECTION_EDIT_START:",
                    "allow_prose": False,
                }
            use_section_blocks = (
                bool(task.get("section_heading"))
                and bool(target_files)
                and all(
                    Path(filepath).suffix.lower() == ".md"
                    and (base_dir / filepath).is_file()
                    and task["section_heading"] in self._markdown_headings(base_dir, filepath)
                    for filepath in target_files
                )
            )
            if task.get("section_heading") and not use_section_blocks:
                task.pop("section_heading", None)
                task["output_contract"] = {
                    "format": "file_blocks",
                    "response_must_start_with": "[FILE_START:",
                    "allow_prose": False,
                }
                machine_contract["output_contract"] = task["output_contract"]
            if use_section_blocks:
                machine_contract["output_contract"] = {
                    "format": "markdown_section_replacements",
                    "response_must_start_with": "[SECTION_EDIT_START:",
                    "allow_prose": False,
                }
                if task.get("section_heading"):
                    machine_contract["section_heading"] = task["section_heading"]
            use_file_edits = task.get("output_contract", {}).get("format") == "file_edits"
            current_files = []
            for filepath in target_files:
                target_path = (base_dir / filepath).resolve()
                if base_dir in target_path.parents and target_path.is_file():
                    current_content = target_path.read_text(encoding="utf-8")
                    section_heading = task.get("section_heading")
                    if section_heading:
                        lines = current_content.splitlines(keepends=True)
                        indices = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == section_heading]
                        heading_match = re.match(r"^(#{1,6})\s+\S", section_heading)
                        if len(indices) == 1 and heading_match:
                            start = indices[0]
                            end = len(lines)
                            section_level = len(heading_match.group(1))
                            for index in range(start + 1, len(lines)):
                                next_heading = re.match(r"^(#{1,6})\s+\S", lines[index])
                                if next_heading and len(next_heading.group(1)) <= section_level:
                                    end = index
                                    break
                            current_content = "".join(lines[start:end])
                    current_files.append(
                        f"[CURRENT_FILE: {filepath}]\n"
                        f"{current_content}\n"
                        f"[END_CURRENT_FILE: {filepath}]"
                    )
            if (
                (base_dir / "README.md").is_file()
                and any(
                    Path(filepath).name.startswith("README_")
                    and Path(filepath).suffix.lower() == ".md"
                    for filepath in target_files
                )
                and "README.md" not in target_files
            ):
                current_files.append(
                    "[READ_ONLY_REFERENCE: README.md]\n"
                    + (base_dir / "README.md").read_text(encoding="utf-8")
                    + "\n[END_READ_ONLY_REFERENCE: README.md]"
                )

            allow_markdown_heading_changes = bool(
                task.get("output_contract", {}).get("allow_markdown_heading_changes")
            )
            heading_rule = (
                "Preserve all unrelated content; this explicit full-file rewrite may replace existing Markdown headings when required by the task."
                if allow_markdown_heading_changes
                else
                "Preserve all unrelated content and existing Markdown headings."
            )
            before_contents = {
                filepath: (
                    (base_dir / filepath).read_bytes()
                    if (base_dir / filepath).is_file()
                    else None
                )
                for filepath in target_files
            }
            prompt = (
                "Implement this single task in the workspace root.\n"
                f"Task ID: {task['id']}\n"
                f"Description: {task['description']}\n"
                f"Machine contract: {json.dumps(machine_contract)}\n"
                f"Modify only target_files. {heading_rule} "
                "Any READ_ONLY_REFERENCE content is for factual comparison only and must never be modified.\n"
            )
            if current_files:
                prompt += "\nCurrent target file contents:\n" + "\n\n".join(current_files) + "\n"

            if backend in ["ollama", "agy", "grok"]:
                if use_section_blocks:
                    filepath = target_files[0]
                    required_heading = task["section_heading"]
                    prompt += (
                        "Return only heading-bounded section replacement blocks for the declared target file. "
                        + f"Use exactly this heading: {required_heading}\n"
                        + f"[SECTION_EDIT_START: {filepath}]\n"
                        f"[HEADING]\n{required_heading}\n[CONTENT]\n"
                        f"[SECTION_EDIT_END: {filepath}]\n"
                        "Insert the complete replacement section content between [CONTENT] and [SECTION_EDIT_END:]. "
                        "Your response MUST begin with [SECTION_EDIT_START:. CONTENT must not repeat HEADING or be empty. "
                        "Preserve existing nested headings unless the task explicitly updates them. Do not return the complete file, "
                        "other files, analysis, Markdown fences, or explanations."
                    )
                elif use_file_edits:
                    filepath = target_files[0]
                    prompt += (
                        "Return only an exact file edit block for the declared target file. "
                        "Put the complete existing text to replace in [OLD] and the complete replacement text in [NEW]. "
                        "Do not return the complete file, other files, analysis, Markdown fences, or explanations.\n"
                        f"[FILE_EDIT_START: {filepath}]\n[OLD]\n<exact existing text>\n[NEW]\n<replacement text>\n[FILE_EDIT_END: {filepath}]"
                    )
                else:
                    prompt += (
                        "Please write the code for any files that need to be created or modified. "
                        "You MUST wrap the code for each file exactly inside the following file-marker blocks:\n"
                        "[FILE_START: path/to/file.ext]\n"
                        "[FILE_END: path/to/file.ext]\n\n"
                        "Insert the complete file content between the two markers. "
                        "Make sure the path is relative to the project root. "
                        "Your response MUST begin with [FILE_START: and contain only file-marker blocks. "
                        "Do not output analysis, a plan, Markdown fences, or explanations."
                    )
            else:
                prompt += "Modify the code files directly in the repository. Provide details of the changes you make."

            if self.orchestrator.state["code_revisions"] > 0 or task.get("revisions", 0) > 0:
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
            if backend in ["ollama", "agy", "grok"]:
                if use_section_blocks:
                    system_prompt += " Respond only with [SECTION_EDIT_START: path], [HEADING], [CONTENT], and [SECTION_EDIT_END: path] blocks; never provide prose."
                else:
                    system_prompt += " Respond only with [FILE_START: path] and [FILE_END: path] blocks; never provide prose."
            self.orchestrator.state["last_developer_role"] = agent_role

            def valid_file_response(response):
                return bool(self.parse_and_write_files(
                    response,
                    task.get("target_files"),
                    dry_run=True,
                    allowed_heading=task.get("section_heading"),
                    allow_markdown_heading_changes=allow_markdown_heading_changes,
                ))

            try:
                dev_output = self.call_agent(
                    effective_role,
                    prompt,
                    system_prompt,
                    response_validator=valid_file_response if backend in ["ollama", "agy", "grok"] else None,
                )
            except RuntimeError as error:
                self.retry_task_or_pause(task, str(error))
                return

            if backend in ["ollama", "agy", "grok"]:
                written = self.parse_and_write_files(
                    dev_output,
                    task.get("target_files"),
                    allowed_heading=task.get("section_heading"),
                    allow_markdown_heading_changes=allow_markdown_heading_changes,
                )
                if written:
                    log_success(f"Successfully processed files written by Developer: {', '.join(written)}")
                else:
                    log_warning("No files were parsed from Developer response. Ensure they used [FILE_START: path] blocks.")
                    self.retry_task_or_pause(task, f"Task {task['id']} produced no permitted file changes.")
                    return
            else:
                changed_files = [
                    filepath for filepath in target_files
                    if (
                        (base_dir / filepath).read_bytes()
                        if (base_dir / filepath).is_file()
                        else None
                    ) != before_contents[filepath]
                ]
                if not changed_files:
                    self.retry_task_or_pause(task, f"Task {task['id']} produced no permitted file changes.")
                    return
                log_success(f"Developer changed files directly: {', '.join(changed_files)}")

            developer_logs.append(f"--- Task {task['id']} implementation output ---\n{dev_output}\n")
            task["status"] = "completed"
            self.orchestrator.state.pop("active_task_id", None)
            self.orchestrator.save_state()

            with open(self.orchestrator.action_items_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2)

        with open(self.orchestrator.developer_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(developer_logs))

        log_success("All pending tasks processed.")
        self.orchestrator.state.pop("active_task_id", None)
        self.orchestrator.state["state"] = "TESTING"
        self.orchestrator.save_state()
