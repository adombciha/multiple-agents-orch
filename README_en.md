# Multi-Agent Orchestrator

[繁體中文](README.md) | [English](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md)

A Python multi-agent development workflow with a deterministic state machine for planning, implementation, QA, code review, and final reporting.

## Requirements and installation

Run these commands from a shell. Git is required for the default Git worktree workflow; Python 3 is required. The project uses `requests`; tests also use `pytest`.

```bash
git clone https://github.com/adombciha/multiple-agents-orch.git
cd multiple-agents-orch
python3 -m pip install requests pytest
python3 orchestrator.py --help
```

The final command verifies that the CLI loads. It does not contact an AI backend or create a worktree. There is no `--version` option.

To run an AI workflow, install and authenticate each CLI backend selected in `.ai-company/config.json` (`grok`, `codex`, `agy`, or `claude`), or run an Ollama service for an `ollama` role. The orchestrator defines no extra Grok API-key environment variable: authenticate the `grok` CLI according to that CLI's requirements. Set `use_worktree` to `false` in the config only if Git worktree isolation is unavailable or unwanted.

## Workflow and files

`start` stores the request and resets the state to `PLANNING`. The normal state flow is:

```text
PLANNING → DEVELOPING_PLAN → REVIEWING_PLAN → IMPLEMENTING
→ TESTING → REVIEWING_CODE → COMPLETED
```

The PM analyzes requirements and staffs optional specialists. Their findings are passed to the Architect for plan review. RD implements the tasks, QA runs the configured test command and reports results, Reviewer evaluates the requirements, plan, tests, specialist findings, and Git diff, and Assistant creates `CHANGELOG.md` when complete.

`init` creates `.ai-company/`, including `config.json`, `state.json`, `request.md`, `requirements.md`, `implementation_plan.md`, `action_items.json`, agent outputs, `test_results.txt`, `qa_report.md`, `reviewer_output.md`, `specialist_reviews.md`, `human_report.md`, and `final_report.md`.

## Configuration

Run `python3 orchestrator.py init` before editing `.ai-company/config.json`. Missing keys use the defaults in `orchestrator/core/config.py`.

| Key | Purpose and default behavior |
| --- | --- |
| `ollama_url`, `ollama_model` | Ollama endpoint and default model (`http://localhost:11434`, `gemma4:latest`). |
| `test_command`, `max_revisions` | QA command and automatic plan/code revision limit; defaults are `python3 -m pytest -q` and `2`. |
| `backends` | Backend assigned to roles. |
| `model_tiers`, `role_models`, `role_model_backends`, `role_model_tiers` | Model lists and per-role model/backend routing. |
| `use_ponytail`, `use_worktree` | Minimalist prompting and Git-worktree isolation; both default to `false` and `true` respectively. |
| `backend_escalation_path`, `staffing_limits` | Backend escalation and RD/QA staffing limits. |

## Grok specialists

Grok is an external CLI backend for dynamic RA and Sales specialists, both configured as `grok` by default. It is neither a top-level workflow state nor a direct replacement for PM, Architect, RD, QA, or Reviewer. The PM selects RA or Sales during requirements analysis and staffing only when the task calls for the specialist; the resulting analysis informs the Architect's plan review.

Install a working, authenticated `grok` CLI before a workflow selects either role. The direct interface used by the project is:

```bash
grok -p "<prompt>" -m grok-4.5
```

The supported configured Grok model is `grok-4.5`. Configure the backend and model in `.ai-company/config.json` with `backends.ra` / `backends.sales`, `role_models.ra` / `role_models.sales`, `role_model_backends.ra` / `role_model_backends.sales`, `role_model_tiers.ra` / `role_model_tiers.sales`, and `model_tiers.grok`. The role model resolves from `role_model_tiers`, then `role_models`, then `grok-4.5`.

`set-backend` can select the `ra` and `sales` roles, but its backend choices do not include `grok`; configure Grok by editing the JSON file. If Grok is unavailable or its request fails, the role falls back in this order:

```text
grok CLI → AGY (gpt-oss-120b) → Ollama (configured model) → error
```

RA output is a model review, not verified legal research. The project does not provide a direct CLI subcommand to force a specialist or set a backend to `grok`.

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

Use `python3 orchestrator.py status` to see the current state and revision counts. `--run` continues the loop only when the selected decision leaves a runnable workflow; it does not run after `reject`.

## CLI reference

Run `python3 orchestrator.py --help` for the current command list. Every command below is run from the repository root; commands other than `init`, `start`, and `--help` require an initialized `.ai-company/` directory.

| Command | Syntax | Required arguments | Optional arguments / defaults | Output, prerequisites, and example |
| --- | --- | --- | --- | --- |
| Help | `python3 orchestrator.py --help` | None | `-h`, `--help` | Prints commands and exits. `python3 orchestrator.py --help` |
| `init` | `python3 orchestrator.py init` | None | None | Creates `.ai-company/config.json` and state files. `python3 orchestrator.py init` |
| `start` | `python3 orchestrator.py start <prompt>` | `prompt`: development request | None | Initializes, saves the request, resets to `PLANNING`; it does not run agents. `python3 orchestrator.py start "Add a command and tests"` |
| `step` | `python3 orchestrator.py step` | None | None | Runs one state-machine step; selected AI CLIs, Ollama, and Git worktree support may be needed. `python3 orchestrator.py step` |
| `run` | `python3 orchestrator.py run` | None | None | Runs until `COMPLETED`, `WAITING_FOR_OWNER`, or `FAILED`. `python3 orchestrator.py run` |
| `status` | `python3 orchestrator.py status` | None | None | Prints state, revision counters, selected backends, test command, and action items. `python3 orchestrator.py status` |
| `approve` | `python3 orchestrator.py approve [--run]` | None | `--run`: continue after approval; default off | Only valid in `WAITING_FOR_OWNER`; resumes at recorded state. `python3 orchestrator.py approve --run` |
| `review` | `python3 orchestrator.py review {pass,revise,reject} [--run]` | `decision`: `pass`, `revise`, or `reject` | `--run`: continue after `pass` or `revise`; default off | Only valid in `WAITING_FOR_OWNER`; prints the recorded-decision message. `python3 orchestrator.py review revise --run` |
| `reset` | `python3 orchestrator.py reset [--state STATE]` | None | `--state STATE`; default `PLANNING`, accepts a string state value | Clears revision/runtime routing data, removes the worktree without merging it, and stores the selected state. `python3 orchestrator.py reset --state DEVELOPING_PLAN` |
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`, `backend` | None | Updates `backends` in config. Roles: `manager`, `architect`, `developer`, `reviewer`, `qa`, `assistant`, `ra`, `security`, `sales`, `sre`. Backends: `ollama`, `codex`, `claude`, `gemini`, `agy`. `developer` and `qa` update their senior/middle/junior routes. `python3 orchestrator.py set-backend reviewer agy` |

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

Set a supported backend, or configure Grok for a specialist by editing its config entries before running the workflow:

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json: keep backends.ra or backends.sales as "grok".
grok -p "Summarize the regulatory risks" -m grok-4.5
```

Resolve a paused review:

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

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

The tests use mocks, fixtures, and temporary directories, so they require no real Grok or other external AI credential. A successful pytest run ends with a passing summary and exit code `0`; `verify_alignment.py` reports that all translation files are structurally aligned and exits `0`. The repository has no separate configured lint, type-check, or formatter command, so none is documented. `git diff --check` is the available basic whitespace/patch check.

Real workflow execution still needs the selected backend CLI and login, an accessible Ollama server for `ollama` roles, and Git worktree support when `use_worktree` is true. Test commands can be changed with `test_command` in `.ai-company/config.json` when the target project needs a different command.

## Scope and limitations

- Specialist selection is dynamic; no CLI command forces Grok, RA, or Sales for a task.
- Grok's configured model list contains `grok-4.5`; it has no configured second Grok-model fallback.
- `set-backend` supports only its documented roles and backend values, and cannot set `grok`.
- `reset --state` accepts a string; use valid workflow states to resume meaningful work.
- AI workflow commands can modify the target project's Git worktree and can incur provider usage or charges.
