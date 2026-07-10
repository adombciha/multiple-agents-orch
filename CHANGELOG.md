

## Testing

*   **Improvement:** Added a comprehensive unit test suite (`test_orchestrator.py`) for `AgentOrchestrator`.
*   This new suite significantly increases code coverage and validates critical internal logic paths without relying on external services or real file system operations (e.g., mock Git commands, Ollama API calls).
*   Testing now rigorously covers state persistence, command execution behaviors (success/failure/timeout), AI model communication payload validation, and the entire workflow lifecycle including worktree setup and cleanup.

### Documentation / Localization

*   Finalized the Japanese (`README_ja.md`) and Simplified Chinese (`README_zh-CN.md`) localized README documentation files.
*   Ensured both localized documents are fully aligned with the structure, content, examples, commands, and ordering of the primary `README.md`.
*   The updates focused on natural language translation while strictly preserving all technical identifiers, code blocks, commands, and link targets to maintain content parity across languages.

## 🚀 Features
*   **Updated Core Workflow Diagram**: Overhauled the core development workflow diagram to reflect a more structured and professional Software Development Life Cycle (SDLC). The flow now clearly routes tasks through dedicated PM (Product Manager) $\rightarrow$ Architect $\rightarrow$ RD/QA teams.
*   **Enhanced Role Specialization**: Significantly refined the role descriptions, moving from general "Developer/Reviewer" roles to highly specialized agents like **PM**, **Architect**, **RA** (Regulatory Affairs), and **SRE** (Site Reliability Engineer).

## ✨ Improvements
*   **Modernized Architecture Definition**: The description of the system's coordination logic was updated. The workflow now emphasizes that each task is routed based on complexity, domain risk, and required expertise, rather than a linear state machine sequence.
*   **Dynamic Role Assignment Logic**: Introduced explicit tables detailing when specific roles are activated and which underlying model routes (e.g., `gpt-5.6-sol`, Gemini Pro) they utilize, increasing transparency into the system's resource allocation.
*   **Expanded Specialist Pool**: Added a formal definition for **Dynamic Specialists** (Sales, Security, RA, SRE), allowing the orchestration layer to intelligently invoke domain experts based on specified requirements or project scope.
*   **Conceptual Clarification**: Updated messaging across all languages regarding role naming and system capabilities (e.g., clarifying that model routing is selective, not mandatory for every interaction).

### 📚 Technical Detail
The README was updated to reflect:
1.  A revised workflow starting with PM/Architect scoping before task assignment.
2.  Specific model routing definitions for various roles (PM, Architect, RD/QA Senior/Middle/Junior, etc.).
3.  New mermaid diagrams illustrating both streamlined (Minimal) and complex (Enterprise/DevSecOps) workflows.