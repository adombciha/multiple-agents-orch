## Testing

- **Improvement:** Added a comprehensive unit test suite (`test_orchestrator.py`) for `AgentOrchestrator`.
- This new suite significantly increases code coverage and validates critical internal logic paths without relying on external services or real file system operations (e.g., mock Git commands, Ollama API calls).
- Testing now rigorously covers state persistence, command execution behaviors (success/failure/timeout), AI model communication payload validation, and the entire workflow lifecycle including worktree setup and cleanup.

### Documentation / Localization

- Finalized the Japanese (`README_ja.md`) and Simplified Chinese (`README_zh-CN.md`) localized README documentation files.
- Ensured both localized documents are fully aligned with the structure, content, examples, commands, and ordering of the primary `README.md`.
- The updates focused on natural language translation while strictly preserving all technical identifiers, code blocks, commands, and link targets to maintain content parity across languages.

### 📚 Documentation

- **Multi-lingual Coverage:** Updated and refined four primary README files (`README.md`, `README_en.md`, `README_ja.md`, `README_zh-CN.md`) to ensure comprehensive, consistent documentation across all supported languages.
- **CLI Reference Guide:** Added an extensive reference guide detailing the usage, parameters, default behaviors, and prerequisites for core CLI commands (`init`, `start`, `step`, `run`, `status`, `approve`, `review`, `reset`, etc.).
- **Agent Integration Documentation:** Provided detailed documentation for the new Grok agent, including its roles (RA/Sales Specialist), workflow placement, dynamic activation methods, and CLI usage patterns.
- **Workflow Clarity:** Enhanced sections describing the full review lifecycle (`pass`, `revise`, `reject`), detailing state transitions, ownership tracking, revision counting, and termination conditions.
- **Usage Improvements:** Standardized instructions for repository cloning and provided comprehensive guides on running unit tests, including dependency setup, mocking fixtures, and expected results validation (`git diff --check`).

_(Note: This update consists purely of documentation improvements to the README files and does not involve any changes to Python code or configuration settings.)_

## ✨ Documentation

- **Complete English Localization:** The primary `README.md` file has been fully translated and modernized into professional, technical English. This significantly improves international accessibility for developers using the orchestrator.
- **Clarity and Polish:** Key sections—including "Requirements and Installation," "Workflow Steps," and detailed configuration descriptions—have been thoroughly reviewed and updated to ensure maximum clarity regarding AI workflow mechanics (e.g., state machine flow, artifact naming conventions).

## Version X.Y.Z - [Date]

### ✨ Features / Architecture Overhaul

- **Automated Weighted Staffing System:** Implemented a dynamic role-playing staffing mechanism that treats internal staff (Ollama) and paid vendors (Claude, Grok, etc.) as employees of a single organization. Instead of fixed roles, the system dynamically assigns influence weights based on demonstrated performance.
- **Dynamic Trust Weighting (EWMA):** Task completion, QA outcomes, and review results now trigger Exponentially Weighted Moving Average (EWMA) updates for internal staff weights (`worker_weights`). This ensures that trust levels adapt naturally over time rather than relying on manual assignments.
- **Weighted QA Voting:** The system no longer requires unanimous agreement for passing a task. Each staff member's vote contributes based on their current trust weight, and the run passes when the collective weighted proportion meets the `qa_vote_threshold`.
- **Internal Memory (Company Brain):** Internal staff roles now have access to a sandboxed SQLite database ("brain"). This system logs failure lessons and meeting summaries using RAG injection, keeping vital corporate knowledge isolated from vendor prompts.

### 🚀 Improvements / Enhancements

- **Advanced Staff/Vendor Separation:** Clearly separates the performance metrics: internal staff weights are tracked independently via `worker_weights`, while external paid vendors maintain their own independent performance scorecards (`vendor_scores`).
- **Manager Review Loop:** Added a formal Manager Performance Review phase at the end of each run, allowing for minor (and bounded) adjustments to staff weights and updating configuration automatically.
- **Automated Daily Roster & Escalation:** The system now reads a daily roster to determine available staff/vendor capacity. If internal staff fail or drop below usability thresholds, tasks are automatically escalated to the highest-rated available vendor ("firefighting").

### 💡 Commands & Config

A suite of new CLI commands and configuration keys have been introduced for operational control:

- **New Commands:**
  - `weights`: View current internal staff trust weights.
  - `roster`: Display today's available RD/QA roles (staff and vendor).
  - `brain search <query>`: Query the company memory database.
- **New Config Keys:** `worker_weights`, `qa_vote_threshold`, `weight_adjustment_max_delta`, `vendor_scores`, and `brain_db`.

**CHANGELOG**

- **日期**: [Enter the date of completion]
- **任务**: GUI 背景更新状态提示与回归测试完成
- **描述**:
  - GUI 背景更新状态提示已沿用现有机制，无需重新加载整个页面。用户可通过文字辨识数据更新状态及当前展示上次成功取得的数据。
  - 补充离线回归测试，涵盖成功、失败、恢复及初始状态，确保任务输入、报告展开及焦点状态的保留行为。
- **变更文件**:
  - `orchestrator/gui.py`: 更新显示最近一次成功的更新时间，并在更新失败时提示“更新失败，正在显示上次资料”。保留现有内容与时间。
  - `test_gui_refresh.py`: 补充离线回归测试，涵盖更多状态。
- **测试状态**: 测试尚未通过，需进一步验证。
- **注意事项**:
  - 确认Git比较范围，确保所有修改已正确提交。
  - 测试证据不足，需补全验证证据并确认后合并在项目中。

---

**By:** [Your Name]
**On:** [Date]
