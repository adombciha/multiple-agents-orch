# Gemini Skill: test-generator

## Description

Generate automated test code, detailed reports, RD action reports, and test-environment setup from a user-provided Markdown test plan, reliability script, or validation protocol. If no implementation language is specified, default to Python3 tests; for Qt/C++ software scopes, include QtTest when C++ core behavior, Qt GUI, widget workflow, signal/slot, or model/view validation is required.

## Instructions for Gemini

1. **Extract Spec**: Run the Python parser script on the user-provided markdown file to extract its structure:

   ```bash
   # Run the script bundled with this skill (located at scripts/extract_markdown_test_spec.py relative to this SKILL.md)
   python3 <absolute_path_to_skill_script>/extract_markdown_test_spec.py <md-file> --out <output-dir>/markdown-test-spec.json
   ```

2. **Analyze**: Read the generated `markdown-test-spec.json` using `view_file`.
3. **Choose Framework and Language**:
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
   - If the repo has no clear framework, scaffold a standalone Python harness under an appropriate test or tools directory and document how to run it.
4. **Generate Tests**: Write the test cases based on the criteria extracted, and save generated executable test scripts, such as `test_*.py`, directly under `<output-dir>` so local validation and report regeneration use the same artifact set. Preserve function-level traceability for function-derived proposals by linking each generated case to the target function, helper function, branch, loop, side effect, or error path it covers. Do not generate broad tests that only execute a function without checking behavior; each executable case must assert a return value, state change, emitted output, error condition, or generated artifact. If a visible function lacks a stable entry point, approved expected output, or controllable input, create a blocked or pending case with the missing testability requirement instead of inventing assertions.
5. **Self-Review and Execution Loop**:
   - Before producing the final RD action report, you must perform a self-review or invoke a code-review subagent (e.g., `ponytail-review` or a dedicated reviewer) to examine any failing tests.
   - You must distinguish between a **SW Bug** (a flaw in the system-under-test's production code) and a **QA/Test Flaw** (a flaw in the generated test code, such as invalid API usage, wrong boundary expectations, or testing uneditable UI components).
   - If a test fails due to a Test Flaw, you must fix the test code and re-run. Do NOT assign test code bugs to RD in the RD action report.
   - Only log genuine implementation, setup, or requirement blockers as RD actions.
6. **Produce Reports**:
   - Do not hand-write the final report body. Write or update a machine-readable test result JSON, then generate the final reports with the shared report generator:

     ```bash
     # Run the script bundled with this skill (located at scripts/generate_test_report.py relative to this SKILL.md)
     python3 <absolute_path_to_skill_script>/generate_test_report.py --spec <output-dir>/markdown-test-spec.json --results <output-dir>/test-results.json --out-dir <output-dir>
     ```

   - `test-results.json` must contain a non-empty `cases` list. Each case must include a supported `status` (`passed`, `failed`, `skipped`, `blocked`, `partial`, `pending`, `unimplemented`, or `environment-dependent`). Optional `artifacts`, `assumptions`, `unresolved_gaps`, `rd_actions`, and `gaps` fields must be lists; optional `environment` must be an object.
   - The report generator produces fixed-format `test-report.json`, `test-report.md`, `rd-action-report.json`, and `rd-action-report.md`.
   - Final report format must remain stable across sessions: agents may change report content, but must not hand-edit, add, remove, or reorder final report sections or JSON fields outside `scripts/generate_test_report.py`.
   - Re-running the generated tests after a fix should only require updating `test-results.json` and re-running `generate_test_report.py`; do not require re-running `test-proposal-generator` or regenerating the whole test package just to produce a second report.
   - Generate RD action report content through the report generator whenever tests are blocked, partial, pending, unimplemented, failed, or environment-dependent.
   - The RD action report is not a duplicate of the detailed test report. It should translate test gaps into prioritized engineering actions with owner, priority, rationale, unlocked test coverage, related cases, required inputs/artifacts, and recommended next command or handoff.
   - Use P0 for blockers that prevent meaningful executable regression or release evidence, P1 for engineering follow-ups that broaden coverage, and P2 for cleanup or readiness improvements.
   - For Qt/C++ projects, call out missing CLI/test executable/API entry points, missing golden fixtures, missing report schemas, missing QtTest targets, missing build commands, and headless GUI setup blockers when applicable.
   - When biological, clinical, or release evidence is in scope, separate RD-owned software actions from QA/lab/reviewer approval actions.
   - If the detailed report or RD action report cannot be generated, produce a failure report that identifies the failed stage, missing artifact path, command attempted, stdout/stderr summary, partial artifacts produced, likely cause, and recovery steps.
7. **Validate Reports**:
   - Verify that `test-report.json`, `test-report.md`, `rd-action-report.json`, and `rd-action-report.md` exist after running `generate_test_report.py`.
   - Treat any `generate_test_report.py` validation error as a failed generation, not a completed report. Fix `test-results.json` or produce failure diagnostics.
   - Verify that the RD action report uses the fixed generator format when any generated test is blocked, partial, pending, unimplemented, failed, or depends on missing environment/SUT inputs.
   - If validation fails, keep or generate a diagnostic report that explains why the generated test/report run failed.
8. **Set Up Test Environment**:
   - Identify the required runtime, tools, and environment variables for the generated tests.
   - Prefer commands that use existing repository tooling and avoid installing new dependencies.
   - If dependencies are missing, report them clearly and ask for approval before installing anything.
   - For Python tests, verify the Python version and whether `unittest` or `pytest` is used.
   - For QtTest or GUI tests, document required Qt version, build command, test executable, and headless GUI requirements such as `QT_QPA_PLATFORM=offscreen` or Xvfb.
   - Generate or update a local run instruction section in the generated report.
   - Run lightweight setup checks when feasible, such as `python3 --version`, test discovery, import checks, or dry-run commands.
   - Do not modify system-wide environment, install packages, or change CI configuration unless the user explicitly approves it.
9. **Report**: Default to compact chat output. Generated tests, `test-results.json`, `test-report.md`, `test-report.json`, `rd-action-report.md`, and `rd-action-report.json` are the authoritative artifacts. Do not paste full generated test code or full report bodies unless the user explicitly asks for them. Output a summary of the analyzed source, generated test files, detailed Markdown report path, machine-readable report path, RD action report path when applicable, runnable commands, test environment setup steps, required runtime/tools, local test commands, CI/headless notes, unresolved setup gaps, failure diagnostics, and any assumptions or gaps around executable coverage, missing inputs, unavailable SUT entry points, and unapproved golden/reference data.
