# Multi-Agent Orchestrator

[English](README.md) | [繁體中文](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md) | [Русский](README_ru.md)

A Python multi-agent development workflow with a deterministic state machine for planning, implementation, QA, code review, and final reporting.

## Requirements and installation

Run these commands from a shell. Git is required for the default Git worktree workflow; Python 3 is required. The project uses `requests` and `litellm`; tests also use `pytest`.

```bash
git clone https://github.com/adombciha/multiple-agents-orch.git
cd multiple-agents-orch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python orchestrator.py --help
```

The final command verifies that the CLI loads. It does not contact an AI backend or create a worktree. There is no `--version` option.

To run an AI workflow, install and authenticate each selected CLI backend (`codex` or `agy`), or run an Ollama service for an `ollama` role. Set `use_worktree` to `false` only if Git worktree isolation is unavailable or unwanted.

## Workflow and files

`start` stores the request and resets the state to `PLANNING`. It refuses to replace an active or undelivered run. The normal state flow is:

```text
PLANNING → DEVELOPING_PLAN → REVIEWING_PLAN → IMPLEMENTING
→ TESTING → REVIEWING_CODE → COMPLETED
```

The PM analyzes requirements and staffs optional specialists. Their findings are passed to the Architect for plan review. RD implements the tasks, QA runs the configured test command and reports results, Reviewer evaluates the requirements, plan, tests, specialist findings, and Git diff, and Assistant creates `CHANGELOG.md` when complete. In delivery mode, completion prepares a Git preview but does not merge it; confirm the preview with `deliver --confirm <token>`. Research mode finishes without a Git delivery step.

`init` creates `.ai-company/`, including `config.json`, `state.json`, `status.json`, `request.md`, `requirements.json`, the generated human-readable `requirements.md`, `implementation_plan.md`, `action_items.json`, agent outputs, `test_results.txt`, `qa_report.md`, `reviewer_output.json`, `reviewer_output.md`, `specialist_reviews.md`, `human_report.md`, and `final_report.md`. `status.json` is an atomic status snapshot; `pause_request.json` records a pending step-boundary pause, while `delivery_preview.md` stores the last displayed Git preview. Machine-to-machine artifacts use JSON where possible; human-facing output uses Markdown.

## Configuration

Run `python3 orchestrator.py init` before editing `.ai-company/config.json`. Missing keys use the defaults in `orchestrator/core/config.py`.

| Key | Purpose and default behavior |
| --- | --- |
| `ollama_url`, `ollama_model` | Ollama endpoint and default model (`http://localhost:11434`, `gemma4:latest`). |
| `test_command`, `max_revisions` | QA command and automatic plan/code revision limit; defaults are `python3 -m pytest -q` and `2`. |
| `backends` | Backend assigned to roles. |
| `model_tiers`, `role_models`, `role_model_backends`, `role_model_tiers` | Model lists and per-role model/backend routing. |
| `use_ponytail`, `use_worktree` | Minimalist prompting and Git-worktree isolation; both default to `false` and `true` respectively. |
| `require_delivery_confirmation`, `gui_port` | Require explicit delivery confirmation (default `true`) and set the local GUI port (default `8765`). |
| `backend_escalation_path`, `staffing_limits` | Backend escalation and RD/QA staffing limits. |
| `telemetry_enabled`, `llm_usage_log` | LiteLLM token metadata logging and JSONL path; defaults to `llm_usage.jsonl` in the workflow directory and does not store prompts or outputs. |

## Automatic model selection

Codex roles default to `auto`, which lets the installed Codex CLI select its recommended model. AGY roles default to `auto:gemini-flash-medium`; the orchestrator reads `agy models` and selects the latest available Gemini Flash Medium version (Gemini 3.8 Flash Medium), caching the list for one hour. Other AGY families can use selectors such as `auto:gemini-flash-low`, `auto:gemini-pro-low`, `auto:claude-sonnet-medium`, or `auto:claude-opus-medium`.

Any AGY model ID listed by `agy models` can also be pinned explicitly, including Gemini 3.8 Flash Low, Gemini 3.7 Flash Low/Medium, Gemini 3.1 Pro, Claude Sonnet, and Claude Opus. An explicit ID stays pinned. Claude models are available through AGY; the separate Claude Code backend and Grok backend are removed.

```bash
agy models
```

### Dynamic model routing

For local Ollama execution, the orchestrator uses `tev1:4b` exclusively as a fast decision router via the Ollama `/v1/systemone` endpoint (never as a task executor via `/api/chat`).
- **Endpoint model discovery**: Installed models are discovered dynamically from the configured Ollama endpoint (`/api/tags`) without pulling or deleting models.
- **Privacy and compact state**: Candidate models are selected based on a compact role and task summary along with candidate descriptions—the full task prompt is never transmitted to the router.
- **Validation and safe fallback**: The orchestrator validates the choice, confidence, and probability for every installed candidate, then tries local models in descending probability order. Low confidence is logged but does not discard a valid ranking. If the endpoint or router is unavailable, or the response is invalid, it uses the static local model order.
- **Runtime cloud selection**: Tev1 also ranks the configured backends for each task. Codex `auto` candidates and supported reasoning levels come from `codex debug models`; AGY `auto:*` candidates come from `agy models`. Only a compact task summary goes to the local router. Explicit model IDs remain pinned; discovery or scoring failures keep the existing route order and auto behavior.

On load, legacy config entries using Grok or Claude Code are migrated to the current role defaults. Unsupported models are not downloaded automatically; Ollama still uses locally installed model tags.

`set-backend` supports `ollama`, `codex`, and `agy`. RA output is a model review, not verified legal research; PM still selects specialists based on task needs.

## Automatic and human review

Automatic review and owner review use different result names and transitions.

| Producer | Result | Effect |
| --- | --- | --- |
| QA | `PASSED` | Continue from `TESTING` to `REVIEWING_CODE`. |
| QA | `FAILED` | Create a QA fix task and return to `IMPLEMENTING`; at `max_revisions`, pause in `WAITING_FOR_OWNER`. |
| Reviewer | `APPROVED` | Continue to completion. |
| Reviewer | `REJECTED` | Create a reviewer fix task and return to `IMPLEMENTING`; at `max_revisions`, pause in `WAITING_FOR_OWNER`. |
| Architect | plan revision | Revise the plan until `max_revisions`; then continue to `IMPLEMENTING`, without creating human review. |

`max_revisions` defaults to `2`, but the active `.ai-company/config.json` controls it. QA and Reviewer failures increment `code_revisions`; their retry limit is the condition that creates owner review. An AI backend failure, non-zero test-command exit status, or other command error is an execution failure. It is not the same as the owner's explicit `reject` decision.

In `WAITING_FOR_OWNER`, use `approve` or `review`:

| Decision | Meaning | Next behavior |
| --- | --- | --- |
| `pass` | Accept the saved passing path. | Resume at the recorded `pass_state`, then optionally continue with `--run`. |
| `revise` | Request another implementation pass. | Increment `code_revisions`, add `HUMAN-REVIEW-N`, and move to `IMPLEMENTING`; it is not blocked by the previous automatic retry threshold. |
| `reject` | Reject this workflow result. | Move to `FAILED`; normal execution stops. |

`approve` is the shortcut for the paused passing path. Successful `review` output is identifiable as:

```text
Human review '<decision>' recorded; workflow moved to <STATE>.
```

Use `python3 orchestrator.py status` to see the persisted state snapshot, lock owner, pause request, and next actions. `--run` continues the loop only when the selected decision leaves a runnable workflow; it does not deliver or merge changes.

## CLI reference

Run `python3 orchestrator.py --help` for the current command list. Every command below is run from the repository root; commands other than `init`, `start`, and `--help` require an initialized `.ai-company/` directory.

| Command | Syntax | Required arguments | Optional arguments / defaults | Output, prerequisites, and example |
| --- | --- | --- | --- | --- |
| Help | `python3 orchestrator.py --help` | None | `-h`, `--help` | Prints commands and exits. `python3 orchestrator.py --help` |
| `init` | `python3 orchestrator.py init` | None | None | Creates `.ai-company/config.json` and state files. `python3 orchestrator.py init` |
| `start` | `python3 orchestrator.py start <prompt>` | `prompt`: development request | None | Initializes and saves the request; refuses to overwrite an active or undelivered run. It does not run agents. `python3 orchestrator.py start "Add a command and tests"` |
| `step` | `python3 orchestrator.py step` | None | None | Runs one state-machine step; selected AI CLIs, Ollama, and Git worktree support may be needed. `python3 orchestrator.py step` |
| `run` | `python3 orchestrator.py run` | None | None | Runs until the next decision point or completion preview; never bypasses delivery confirmation. `python3 orchestrator.py run` |
| `continue` | `python3 orchestrator.py continue` | None | None | Auto-resumes using the existing approve/revise decision; for a completed but undelivered run, prints the preview without merging. `python3 orchestrator.py continue` |
| `pause` | `python3 orchestrator.py pause [--cancel]` | None | `--cancel`: cancel a pending pause request | Requests a pause after the current workflow step; does not interrupt an active model call. `python3 orchestrator.py pause` |
| `deliver` | `python3 orchestrator.py deliver [--confirm TOKEN]` | None | `--confirm TOKEN`: confirm a previously displayed preview | Without a token, displays and saves the current Git preview. With the displayed token, rechecks freshness and merges; changed content requires a new preview and confirmation. `python3 orchestrator.py deliver --confirm 0123456789ab` |
| `gui` | `python3 orchestrator.py gui [--port PORT]` | None | `--port`: defaults to `gui_port` | Starts a token-protected dashboard bound only to `127.0.0.1`. `python3 orchestrator.py gui` |
| `status` | `python3 orchestrator.py status` | None | None | Reads the status snapshot without reloading worker state; prints lock owner, pause status, and next actions. `python3 orchestrator.py status` |
| `approve` | `python3 orchestrator.py approve [--run]` | None | `--run`: continue after approval; default off | Only valid in `WAITING_FOR_OWNER`; resumes at recorded state. `python3 orchestrator.py approve --run` |
| `review` | `python3 orchestrator.py review {pass,revise,reject} [--run] [--reset-revisions]` | `decision`: `pass`, `revise`, or `reject` | `--run`: continue after `pass` or `revise`; default off. `--reset-revisions`: with `revise`, clear the code/plan revision counter first so the auto-revise loop gets a fresh `max_revisions` budget instead of immediately re-pausing; keeps the current worktree and tasks, unlike `reset` | Only valid in `WAITING_FOR_OWNER`; prints the recorded-decision message. Note this `reject` decision marks the run `FAILED` (terminal) -- it's a different action from the top-level `reject` command below. `python3 orchestrator.py review revise --run --reset-revisions` |
| `reject` | `python3 orchestrator.py reject [--run]` | None | `--run`: continue after rejecting; default off | Sends the whole project back to `PLANNING` but **keeps the saved request prompt**, so it gets re-planned from the same ask -- nothing has to be re-typed. Works from any state, not just `WAITING_FOR_OWNER`. `python3 orchestrator.py reject --run` |
| `reset` | `python3 orchestrator.py reset [--state STATE]` | None | `--state STATE`; default `PLANNING`, accepts a string state value | Full restart: clears revision/runtime routing data, removes the worktree without merging it, stores the selected state, **and deletes the saved request prompt** -- `start` must be run again before `run`. Use `reject` instead to keep the same prompt. `python3 orchestrator.py reset --state DEVELOPING_PLAN` |
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`, `backend` | None | Updates `backends` in config. Roles: `manager`, `architect`, `developer`, `reviewer`, `qa`, `assistant`, `ra`, `security`, `sales`, `sre`. Backends: `ollama`, `codex`, `agy`. `developer` and `qa` update their senior/middle/junior routes. `python3 orchestrator.py set-backend reviewer agy` |

Each subcommand also supports `--help`, for example `python3 orchestrator.py review --help`. Invalid role/backend/decision values are rejected by argparse. `status`, `approve`, `review`, `reset`, and `set-backend` report that initialization is required when `.ai-company/` is absent; `approve` and `review` also reject states other than `WAITING_FOR_OWNER`.

### Common CLI sequences

Minimal local setup and a task:

```bash
python3 orchestrator.py init
python3 orchestrator.py start "Add contact search and tests"
python3 orchestrator.py step
python3 orchestrator.py status
```

Run the normal automated workflow (requires the configured backends and, by default, Git):

```bash
python3 orchestrator.py run
```

Set a supported backend or select an AGY model before running the workflow:

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json to pin role_model_routes.ra[0][1] if needed.
agy models
```

Resolve a paused review:

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

## Automatic weighting and role-play staffing

The system manages internal staff (Ollama) and paid vendors (Codex, Agy) like a small company, adjusting trust weights and voting influence based on performance.

### Staff vs. vendors

- **Internal staff**: `developer_senior/middle/junior` and `qa_senior/middle/junior` roles routed to the `ollama` backend are treated as free, long-term staff with an accumulating trust weight (`worker_weights`).
- **Paid vendors**: `codex` and `agy` CLI backends do not count toward internal performance; each `backend/model` pairing is tracked on a **vendor scorecard** (`vendor_scores`).

### Trust weights and scoring events

- Each role (`rd`/`qa`) x level (`senior`/`middle`/`junior`) has a trust weight between `0.2` and `3.0`, defaulting to the values in `worker_weights`.
- Task completions, QA pass/fail, and Reviewer approve/reject events trigger an EWMA (exponentially weighted moving average) update that nudges the weight over time; vendors update their own scorecard instead, so the two never interfere with each other.

### QA weighted voting

- QA no longer requires unanimous agreement. Each level's vote is weighted by its trust weight; the run passes once the weighted ratio reaches `qa_vote_threshold` (default `0.6`). A non-zero test-command exit code still fails the run outright, regardless of the vote.
- If a weighted majority overrides a minority FAILED vote, that dissent is recorded in the QA report and surfaced to the Reviewer as a second check. If a human later overrides a QA failure, the level(s) that voted FAILED get penalized as a false positive.

### Manager performance review

- At the end of each run, the Manager proposes small (±`weight_adjustment_max_delta`, default `0.3`) weight adjustments based on that run's telemetry, clamped to the legal range and written back to config automatically. Vendor scorecards are informational only and are not touched by this review.

### Daily roster

- Before staffing a task, the Manager reads today's roster: which internal staff roles are online (is Ollama reachable?), which vendors are available, and each one's current weight or rating.
- If the assigned level is offline, the task is automatically reassigned to the nearest available level, preferring higher weight/rating when otherwise equally suited.

### Vendor firefighting

- When internal staff keeps struggling on a task (running low on revisions, trust weight has dropped too far, or Ollama is unreachable), the system automatically hands the task to the best-rated available vendor from `backend_escalation_path` to firefight it. This is a deterministic decision, not a separate model call.

### Internal company memory (brain)

- Internal staff (Ollama backend only) have access to a sandboxed SQLite + FTS5 "brain" database that records lessons from failures and meeting summaries, injected back into relevant future prompts using RAG. Vendor prompts never see this memory, so company knowledge never leaves the sandbox.
- Only fixed code hooks write to this database; model output is never written directly, which prevents prompt injection from poisoning long-term memory.

### New CLI commands

| Command | Description |
| --- | --- |
| `weights` | Show current internal staff trust weights |
| `weights set <role> <level> <value>` | Manually override a weight |
| `roster` | Show today's available RD/QA roles (staff and vendors) |
| `vendors` | Show the paid-vendor scorecard |
| `brain search <query>` | Search the internal company memory |
| `brain prune [--keep N]` | Prune old memories, keeping only the most recent N |

### New configuration keys

`worker_weights`, `qa_vote_threshold`, `weight_adjustment_max_delta`, `vendor_scores`, `vendor_hire_min_jobs`, `brain_db` (see the defaults in `orchestrator/core/config.py`).

## Tests and verification

From the repository root, install test dependencies and run the available checks:

```bash
python3 -m pip install requests pytest
python3 -m pytest -q
python3 -m pytest -q test_orchestrator.py
python3 -m pytest -q test_orchestrator.py::test_initialized_orchestrator_fixture_loads_temp_config_and_state
python3 verify_alignment.py
git diff --check
```

The tests use mocks, fixtures, and temporary directories, so they require no external AI credential. A successful pytest run ends with a passing summary and exit code `0`; `verify_alignment.py` checks translation-file structure. `git diff --check` is the available basic whitespace/patch check.

Real workflow execution still needs the selected backend CLI and login, an accessible Ollama server for `ollama` roles, and Git worktree support when `use_worktree` is true. Test commands can be changed with `test_command` in `.ai-company/config.json` when the target project needs a different command.

## Scope and limitations

- Specialist selection is dynamic; no CLI command forces RA or Sales for a task.
- AGY auto selectors require `agy models` to succeed; explicit model IDs stay pinned.
- `set-backend` supports only its documented roles and backend values.
- `reset --state` accepts a string; use valid workflow states to resume meaningful work.
- AI workflow commands can modify the target project's Git worktree and can incur provider usage or charges.

## Future Direction

This project aims to evolve from a deterministic multi-agent orchestration framework into an AI organization simulator capable of dynamically forming engineering teams, allocating responsibilities, and governing autonomous collaboration.
