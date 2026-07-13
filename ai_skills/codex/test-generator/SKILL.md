---
name: test-generator
description: Generate executable automated test code, detailed reports, RD action reports, and test-environment setup from a user-provided Markdown specification, test plan, validation protocol, acceptance criteria, release-evidence spec, or testing proposal. Use when the user gives an MD file and asks Codex to derive tests, implement or scaffold the appropriate test programs, run feasible validation, produce traceable human-readable and machine-readable reports, and summarize priority engineering actions. If no implementation language is specified, default to Python3 tests; for Qt/C++ software scopes, include QtTest when C++ core behavior, Qt GUI, widget workflow, signal/slot, or model/view validation is required.
---

# Test Generator

## Purpose

Convert a Markdown specification into automated test programs, detailed traceable reports, RD action reports, and the local setup needed to run the generated tests.

The Markdown file is the source of truth. Use Python to read and extract its structure before generating code. Do not rely only on manual skimming unless the file is tiny.

Default to generating executable tests. Mark output as scaffold, partial, blocked, or demo only when the Markdown explicitly describes demo-only evidence, required inputs are missing, approved golden references are unavailable, the system under test cannot be executed, or the user asks for a scaffold.

## Workflow

1. Locate the Markdown source:
   - Use the file path provided by the user.
   - If no path is provided but exactly one relevant `.md` file is staged or recently added, ask whether to use it only when ambiguity would be risky.
   - If several plausible files exist, ask the user which one to use.

2. Extract a machine-readable test spec:
   - Before generating into `tests/automated/<name>/`, `<output-dir>/`, or any other scoped generated-test directory, check whether that directory already exists.
   - If the output directory exists, explicitly tell the user it will be removed and wait for approval before deleting it. Do not merge new generated files into an old generated-test directory.
   - After approval, remove the existing generated-test directory before writing new artifacts so stale reports, old outputs, bytecode caches, or obsolete fixtures cannot contaminate the new result.
   - Run `python3 <skill_dir>/scripts/extract_markdown_test_spec.py <md-file> --out <output-dir>/markdown-test-spec.json`.
   - Read the generated JSON summary before writing tests.
   - Use the extracted headings, tables, code blocks, commands, expected outputs, tolerance terms, pass/fail language, and file references to infer test cases.

3. Choose the target test framework and language:
   - If the user or Markdown explicitly specifies a language or framework, follow it.
   - Expected behavior priority: SPEC > CODE > UI behavior reasonableness.
   - If SPEC exists, test against SPEC; CODE only verifies implementation conformance.
   - If no SPEC but CODE exists, generate characterization/regression tests only.
   - If neither SPEC nor CODE exists but UI behavior is clear, generate UI behavior tests only and state that algorithm/code correctness is out of scope.
   - If the proposal was derived from a function name, preserve the target function, resolved file/module/signature, caller boundary, direct callees, side effects, and oracle classification in generated test names, comments, or report fields.
   - If the proposal requests white-box coverage, generate or scaffold coverage for every function marked `In Primary Flow` or `Direct Dependency`; include `Utility / Low Risk` functions only when they contain meaningful branch, numerical, state, or error logic.
   - For TDD-ready cases, identify the expected initial failure behavior. For characterization cases, state that tests preserve observed behavior and do not prove algorithm correctness without an approved SPEC, oracle, or golden reference.
   - If no implementation language is specified, default to Python3 tests.
   - Prefer Python standard library `unittest` for the default Python3 harness because it avoids new dependencies.
   - Use `pytest` only when the repository already uses pytest or the user explicitly approves it.
   - Prefer existing test frameworks and naming conventions already present in the repo when they match the requested scope.
   - For Qt/C++ software scopes, include QtTest when the Markdown requires C++ core behavior, Qt widgets, GUI workflow, signal/slot behavior, or model/view state validation.
   - For GUI tests, use QtTest to validate widget state, user interaction, signal emission, form validation, displayed results, and error presentation.
   - For GUI-related proposals, follow the expected behavior priority. Do not use UI automation as a substitute for SPEC or CODE coverage when those inputs are available.
   - Use Python3 for release evidence validation, report validation, fixture generation, hash checks, schema checks, and actual-vs-expected comparison.
   - Treat QtTest and Python3 as complementary layers when the software scope includes both Qt/C++ behavior and release evidence/report validation.
   - GUI tests must not be the sole source of algorithm correctness or clinical/release evidence. Validate core algorithm correctness through headless C++/QtTest or integration tests, while Python validates generated outputs and evidence packages.
   - If the repo has no clear framework, generate a standalone Python harness under an appropriate test or tools directory and document how to run it.

4. Generate tests:
   - Convert each explicit acceptance criterion, expected-output table, boundary case, pass/fail example, or report schema requirement into one or more test cases.
   - Preserve traceability by including the Markdown heading, table row, case ID, or source line in test names, comments, or report fields.
   - Preserve function-level traceability for function-derived proposals by linking each generated case to the target function, helper function, branch, loop, side effect, or error path it covers.
   - Do not generate broad tests that only execute a function without checking behavior. Each executable case must assert a return value, state change, emitted output, error condition, or generated artifact.
   - Generate executable tests when the required inputs, target executable/API, and expected results are available.
   - Save generated executable test scripts, such as `test_*.py`, directly under `<output-dir>` so local validation and report regeneration use the same artifact set.
   - If the target cannot be executed but output artifacts are available, generate executable validation tests against those artifacts and clearly state the execution boundary.
   - Use fixture/sample data from the Markdown as production test evidence only when the Markdown identifies it as approved reference data. Otherwise classify it according to the evidence level stated or implied by the Markdown.
   - If expected values are examples rather than verified golden data, mark only the affected tests as scaffold, partial, pending-golden, or demo; do not downgrade unrelated executable tests.
   - If a function is visible in a white-box proposal but lacks a stable entry point, approved expected output, or controllable input, create a blocked or pending case with the missing testability requirement instead of inventing assertions.
   - Do not claim release, biological, or clinical readiness unless the Markdown provides approved references, acceptance criteria, and required approvals.
   
5. Self-Review and Execution Loop:
   - Before producing the final RD action report, you must perform a self-review or invoke a code-review subagent (e.g., `ponytail-review` or a dedicated reviewer) to examine any failing tests.
   - You must distinguish between a **SW Bug** (a flaw in the system-under-test's production code) and a **QA/Test Flaw** (a flaw in the generated test code, such as invalid API usage, wrong boundary expectations, or testing uneditable UI components).
   - If a test fails due to a Test Flaw, you must fix the test code and re-run. Do NOT assign test code bugs to RD in the RD action report.
   - Only log genuine implementation, setup, or requirement blockers as RD actions.

6. Produce a report:
   - Do not hand-write the final report body. Write or update a machine-readable `test-results.json`, then generate the final reports with the shared report generator:
     ```bash
     python3 <skill_dir>/scripts/generate_test_report.py --spec <output-dir>/markdown-test-spec.json --results <output-dir>/test-results.json --out-dir <output-dir>
     ```
   - `test-results.json` must contain a non-empty `cases` list. Each case must include a supported `status` (`passed`, `failed`, `skipped`, `blocked`, `partial`, `pending`, `unimplemented`, or `environment-dependent`). Optional `artifacts`, `assumptions`, `unresolved_gaps`, `rd_actions`, and `gaps` fields must be lists; optional `environment` must be an object.
   - The report generator produces fixed-format `test-report.json`, `test-report.md`, `rd-action-report.json`, and `rd-action-report.md`.
   - Final report format must remain stable across sessions: agents may change report content, but must not hand-edit, add, remove, or reorder final report sections or JSON fields outside `<skill_dir>/scripts/generate_test_report.py`.
   - Re-running the generated tests after a fix should only require updating `test-results.json` and re-running `generate_test_report.py`; do not require re-running `test-proposal-generator` or regenerating the whole test package just to produce a second report.
   - Generate RD action report content through the report generator whenever there are blocked, partial, pending, unimplemented, failed, or environment-dependent tests.
   - The detailed Markdown report must expand the generated evidence, not only summarize readiness flags.
   - Include source file, extracted cases, generated files, runnable commands, pass/fail status if tests were run, and gaps requiring human confirmation.
   - For proposal-derived tests, the detailed report should include scenarios, test cases, boundary/abnormal cases, tolerance/comparison rules, release gates, clinical or biological validation planning when present, environment setup, unresolved gaps, and evidence boundaries.
   - The RD action report is not a duplicate of the detailed test report. It should translate test gaps into prioritized engineering actions with owner, priority, rationale, unlocked test coverage, related cases, required inputs/artifacts, and recommended next command or handoff.
   - RD action priority should distinguish P0 blockers that prevent meaningful executable regression or release evidence, P1 engineering follow-ups that broaden coverage, and P2 cleanup or readiness improvements.
   - For Qt/C++ projects, the RD action report should call out missing CLI/test executable/API entry points, missing golden fixtures, missing report schemas, missing QtTest targets, missing build commands, and headless GUI setup blockers when applicable.
   - When biological, clinical, or release evidence is in scope, separate RD-owned software actions from QA/lab/reviewer approval actions.
   - Include hashes for the source Markdown and generated spec when practical.
   - If tests are not run, state that clearly and explain what remains to execute them.
   - If the detailed report or RD action report cannot be generated, produce a failure report instead of silently finishing. The failure report must identify the failed stage, missing artifact path, command attempted, stdout/stderr summary, partial artifacts produced, likely cause, and recovery steps.

7. Validate:
   - Run lightweight syntax checks or the relevant test command when feasible.
   - Do not install dependencies or use network access unless the user approves.
   - Keep generated files scoped to the requested test area.
   - Verify that `test-report.json`, `test-report.md`, `rd-action-report.json`, and `rd-action-report.md` exist after running `generate_test_report.py`.
   - Treat any `generate_test_report.py` validation error as a failed generation, not a completed report. Fix `test-results.json` or produce failure diagnostics.
   - Verify that the RD action report uses the fixed generator format when any generated test is blocked, partial, pending, unimplemented, failed, or depends on missing environment/SUT inputs.
   - If validation fails, keep or generate a diagnostic report that explains why the generated test/report run failed.

8. Help set up the test environment:
   - Identify the required runtime, tools, and environment variables for the generated tests.
   - Prefer commands that use existing repository tooling and avoid installing new dependencies.
   - If dependencies are missing, report them clearly and ask for approval before installing anything.
   - For Python tests, verify the Python version and whether `unittest` or `pytest` is used.
   - For QtTest or GUI tests, document required Qt version, build command, test executable, and headless GUI requirements such as `QT_QPA_PLATFORM=offscreen` or Xvfb.
   - Generate or update a local run instruction section in the generated report.
   - Run lightweight setup checks when feasible, such as `python3 --version`, test discovery, import checks, or dry-run commands.
   - Do not modify system-wide environment, install packages, or change CI configuration unless the user explicitly approves it.

## Python Helper

Use `scripts/extract_markdown_test_spec.py` to parse Markdown into JSON. The script extracts:

- headings with source lines
- fenced code blocks with language labels
- Markdown tables
- command-looking blocks
- expected/pass/fail/tolerance/report keywords
- referenced file paths
- SHA-256 of the source Markdown

The JSON is an intermediate artifact. It is not a substitute for engineering judgment; use it to make generation systematic and auditable.

Use the bundled `scripts/generate_test_report.py` to turn the extracted spec and `test-results.json` into stable final reports. This keeps report format ownership in code instead of agent prose.

## Output Expectations

Default to compact chat output. Generated tests, `test-results.json`, `test-report.md`, `test-report.json`, `rd-action-report.md`, and `rd-action-report.json` are the authoritative artifacts. Do not paste full generated test code or full report bodies in chat unless the user explicitly asks for them.

When finished, report:

- Markdown source analyzed
- generated test file paths
- generated detailed Markdown report path: `test-report.md`
- generated machine-readable report path: `test-report.json`
- generated RD action report paths: `rd-action-report.md` and `rd-action-report.json`
- commands run and their result
- test environment setup steps
- required runtime/tools
- commands to run tests locally
- CI/headless notes
- unresolved setup gaps
- failure diagnostics if any expected report was not generated
- any assumptions or gaps, especially executable coverage, missing inputs, unavailable system-under-test entry points, and unapproved golden/reference data
