

## Testing

*   **Improvement:** Added a comprehensive unit test suite (`test_orchestrator.py`) for `AgentOrchestrator`.
*   This new suite significantly increases code coverage and validates critical internal logic paths without relying on external services or real file system operations (e.g., mock Git commands, Ollama API calls).
*   Testing now rigorously covers state persistence, command execution behaviors (success/failure/timeout), AI model communication payload validation, and the entire workflow lifecycle including worktree setup and cleanup.

### Documentation / Localization

*   Finalized the Japanese (`README_ja.md`) and Simplified Chinese (`README_zh-CN.md`) localized README documentation files.
*   Ensured both localized documents are fully aligned with the structure, content, examples, commands, and ordering of the primary `README.md`.
*   The updates focused on natural language translation while strictly preserving all technical identifiers, code blocks, commands, and link targets to maintain content parity across languages.

### 📚 Documentation

*   **Multi-lingual Coverage:** Updated and refined four primary README files (`README.md`, `README_en.md`, `README_ja.md`, `README_zh-CN.md`) to ensure comprehensive, consistent documentation across all supported languages.
*   **CLI Reference Guide:** Added an extensive reference guide detailing the usage, parameters, default behaviors, and prerequisites for core CLI commands (`init`, `start`, `step`, `run`, `status`, `approve`, `review`, `reset`, etc.).
*   **Agent Integration Documentation:** Provided detailed documentation for the new Grok agent, including its roles (RA/Sales Specialist), workflow placement, dynamic activation methods, and CLI usage patterns.
*   **Workflow Clarity:** Enhanced sections describing the full review lifecycle (`pass`, `revise`, `reject`), detailing state transitions, ownership tracking, revision counting, and termination conditions.
*   **Usage Improvements:** Standardized instructions for repository cloning and provided comprehensive guides on running unit tests, including dependency setup, mocking fixtures, and expected results validation (`git diff --check`).

*(Note: This update consists purely of documentation improvements to the README files and does not involve any changes to Python code or configuration settings.)*


## ✨ Documentation
*   **Complete English Localization:** The primary `README.md` file has been fully translated and modernized into professional, technical English. This significantly improves international accessibility for developers using the orchestrator.
*   **Clarity and Polish:** Key sections—including "Requirements and Installation," "Workflow Steps," and detailed configuration descriptions—have been thoroughly reviewed and updated to ensure maximum clarity regarding AI workflow mechanics (e.g., state machine flow, artifact naming conventions).

## Version X.Y.Z - [Date]

### ✨ Features / Architecture Overhaul

*   **Automated Weighted Staffing System:** Implemented a dynamic role-playing staffing mechanism that treats internal staff (Ollama) and paid vendors (Claude, Grok, etc.) as employees of a single organization. Instead of fixed roles, the system dynamically assigns influence weights based on demonstrated performance.
*   **Dynamic Trust Weighting (EWMA):** Task completion, QA outcomes, and review results now trigger Exponentially Weighted Moving Average (EWMA) updates for internal staff weights (`worker_weights`). This ensures that trust levels adapt naturally over time rather than relying on manual assignments.
*   **Weighted QA Voting:** The system no longer requires unanimous agreement for passing a task. Each staff member's vote contributes based on their current trust weight, and the run passes when the collective weighted proportion meets the `qa_vote_threshold`.
*   **Internal Memory (Company Brain):** Internal staff roles now have access to a sandboxed SQLite database ("brain"). This system logs failure lessons and meeting summaries using RAG injection, keeping vital corporate knowledge isolated from vendor prompts.

### 🚀 Improvements / Enhancements

*   **Advanced Staff/Vendor Separation:** Clearly separates the performance metrics: internal staff weights are tracked independently via `worker_weights`, while external paid vendors maintain their own independent performance scorecards (`vendor_scores`).
*   **Manager Review Loop:** Added a formal Manager Performance Review phase at the end of each run, allowing for minor (and bounded) adjustments to staff weights and updating configuration automatically.
*   **Automated Daily Roster & Escalation:** The system now reads a daily roster to determine available staff/vendor capacity. If internal staff fail or drop below usability thresholds, tasks are automatically escalated to the highest-rated available vendor ("firefighting").

### 💡 Commands & Config

A suite of new CLI commands and configuration keys have been introduced for operational control:

*   **New Commands:**
    *   `weights`: View current internal staff trust weights.
    *   `roster`: Display today's available RD/QA roles (staff and vendor).
    *   `brain search <query>`: Query the company memory database.
*   **New Config Keys:** `worker_weights`, `qa_vote_threshold`, `weight_adjustment_max_delta`, `vendor_scores`, and `brain_db`.