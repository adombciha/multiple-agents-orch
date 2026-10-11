# Multi-Agent Orchestrator

[English](README.md) | [繁體中文](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md) | [Русский](README_ru.md)

这是一个基于 Python 的多代理开发流程，使用确定性状态机完成规划、实现、QA、代码审查和最终报告。

## 系统要求与安装

请在 shell 中执行以下命令。默认 Git worktree 流程需要 Git；项目需要 Python 3。项目使用 `requests` 和 `litellm`，测试还使用 `pytest`。

```bash
git clone https://github.com/adombciha/multiple-agents-orch.git
cd multiple-agents-orch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python orchestrator.py --help
```

最后一条命令只验证 CLI 能加载，不会连接 AI backend 或创建 worktree。项目没有 `--version` 选项。

要运行 AI workflow，请安装并登录选定的 CLI backend（`codex` 或 `agy`），或为 `ollama` role 运行 Ollama 服务。仅在 Git worktree 隔离不可用或不需要时将 `use_worktree` 设为 `false`。

## Workflow 与文件

replacement content below the heading

## 配置

编辑 `.ai-company/config.json` 前先运行 `python3 orchestrator.py init`。缺少的键会使用 `orchestrator/core/config.py` 中的默认值。

| Key | 用途与默认行为 |
| --- | --- |
| `ollama_url`、`ollama_model` | Ollama endpoint 与默认 model（`http://localhost:11434`、`gemma4:latest`）。 |
| `test_command`、`max_revisions` | QA command 与自动 plan/code revision 上限；默认值为 `python3 -m pytest -q` 和 `2`。 |
| `backends` | 分配给各 role 的 backend。 |
| `model_tiers`、`role_models`、`role_model_backends`、`role_model_tiers` | model 列表及每个 role 的 model/backend 路由。 |
| `use_ponytail`、`use_worktree` | 极简提示与 Git-worktree 隔离；默认分别为 `false` 和 `true`。 |
| `backend_escalation_path`、`staffing_limits` | backend 升级路径及 RD/QA staffing 限制。 |
| `telemetry_enabled`、`llm_usage_log` | LiteLLM token metadata 记录开关与 JSONL 路径；默认记录到 workflow 目录的 `llm_usage.jsonl`，不保存 prompt/output。 |

## 自动选择模型

Codex 的 `auto` 使用 CLI 当前推荐模型；AGY 的 `auto:gemini-flash-medium` 会通过 `agy models` 选择最新可用的 Gemini Flash Medium，并缓存一小时。也可使用 `auto:gemini-flash-low`、`auto:gemini-pro-low`、`auto:claude-sonnet-medium` 或 `auto:claude-opus-medium`。具体型号（包括 Gemini 3.8 Flash Low、3.7 Flash Low/Medium、3.1 Pro、Claude Sonnet 和 Opus）仍可直接指定。移除的是独立的 Claude Code 和 Grok backend，不是 AGY 内的 Claude 型号。

```bash
agy models
```

### 本地 Ollama 动态模型路由

执行本地 Ollama 任务时，`tev1:4b` 仅通过 Ollama `/v1/systemone` endpoint 作为决策路由器使用，不会通过 `/api/chat` 执行任务。
- **模型发现**：通过已配置的 Ollama endpoint 的 `/api/tags` 动态发现已安装模型；不会自动拉取或删除模型。
- **隐私**：路由器只接收角色、精简任务摘要和候选模型说明，不会收到完整 task prompt。
- **验证与回退**：验证所选模型、confidence 和每个已安装候选模型的概率，并按概率从高到低尝试本地模型。低 confidence 会被记录，但有效排序仍可使用；endpoint 或路由器不可用、结果无效时，使用本地模型的固定顺序。
- **本地隔离**：动态路由仅在 Ollama 本地完成，不会改变 Codex 或 AGY 路由。

## Automatic 与 human review

Automatic review 与 owner review 使用不同的 result 名称和 transition。

| Producer | Result | Effect |
| --- | --- | --- |
| QA | `PASSED` | 从 `TESTING` 继续到 `REVIEWING_CODE`。 |
| QA | `FAILED` | 创建 QA fix task 并返回 `IMPLEMENTING`；达到 `max_revisions` 后暂停在 `WAITING_FOR_OWNER`。 |
| Reviewer | `APPROVED` | 继续完成流程。 |
| Reviewer | `REJECTED` | 创建 reviewer fix task 并返回 `IMPLEMENTING`；达到 `max_revisions` 后暂停在 `WAITING_FOR_OWNER`。 |
| Architect | plan revision | 修订计划直到 `max_revisions`；之后继续到 `IMPLEMENTING`，不会创建 human review。 |

`max_revisions` 默认是 `2`，但实际值由当前 `.ai-company/config.json` 控制。QA 和 Reviewer 失败会增加 `code_revisions`；达到 retry limit 后才创建 owner review。AI backend failure、test command 非零 exit status 或其他 command error 属于 execution failure，不等同于 owner 明确作出的 `reject` decision。

在 `WAITING_FOR_OWNER` 中使用 `approve` 或 `review`：

| Decision | 含义 | 后续行为 |
| --- | --- | --- |
| `pass` | 接受保存的通过路径。 | 从记录的 `pass_state` 恢复，然后可选择使用 `--run` 继续。 |
| `revise` | 请求再次实现。 | 增加 `code_revisions`，添加 `HUMAN-REVIEW-N`，并移到 `IMPLEMENTING`；不受之前 automatic retry threshold 阻挡。 |
| `reject` | 拒绝本次 workflow 结果。 | 移到 `FAILED`；正常执行停止。 |

`approve` 是暂停的通过路径的快捷方式。成功的 `review` 输出可通过以下内容识别：

```text
Human review '<decision>' recorded; workflow moved to <STATE>.
```

使用 `python3 orchestrator.py status` 查看当前 state 和 revision counters。`--run` 只有在所选 decision 留下可运行 workflow 时才继续；`reject` 后不会运行。

## CLI reference

运行 `python3 orchestrator.py --help` 查看当前 command list。以下每条命令都从 repository root 执行；除 `init`、`start` 和 `--help` 外，其他命令都需要已初始化的 `.ai-company/` 目录。

| Command | Syntax | Required arguments | Optional arguments / defaults | Output, prerequisites, and example |
| --- | --- | --- | --- | --- |
| Help | `python3 orchestrator.py --help` | None | `-h`、`--help` | 输出 commands 并退出。`python3 orchestrator.py --help` |
| `init` | `python3 orchestrator.py init` | None | None | 创建 `.ai-company/config.json` 和 state files。`python3 orchestrator.py init` |
| `start` | `python3 orchestrator.py start <prompt>` | `prompt`: development request | None | 初始化、保存 request 并重置为 `PLANNING`；不会运行 agents。`python3 orchestrator.py start "Add a command and tests"` |
| `step` | `python3 orchestrator.py step` | None | None | 执行一个 state-machine step；可能需要选定的 AI CLIs、Ollama 和 Git worktree。`python3 orchestrator.py step` |
| `run` | `python3 orchestrator.py run` | None | None | 运行直到 `COMPLETED`、`WAITING_FOR_OWNER` 或 `FAILED`。`python3 orchestrator.py run` |
| `status` | `python3 orchestrator.py status` | None | None | 输出 state、revision counters、选定 backends、test command 和 action items。`python3 orchestrator.py status` |
| `approve` | `python3 orchestrator.py approve [--run]` | None | `--run`：approval 后继续；默认关闭 | 仅在 `WAITING_FOR_OWNER` 有效；从记录的 state 恢复。`python3 orchestrator.py approve --run` |
| `review` | `python3 orchestrator.py review {pass,revise,reject} [--run]` | `decision`：`pass`、`revise` 或 `reject` | `--run`：在 `pass` 或 `revise` 后继续；默认关闭 | 仅在 `WAITING_FOR_OWNER` 有效；输出 recorded-decision message。`python3 orchestrator.py review revise --run` |
| `reset` | `python3 orchestrator.py reset [--state STATE]` | None | `--state STATE`；默认 `PLANNING`，接受 string state value | 清除 revision/runtime routing data，移除 worktree 但不合并，并保存选定 state。`python3 orchestrator.py reset --state DEVELOPING_PLAN` |
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`、`backend` | None | 更新 config 中的 `backends`。roles：`manager`、`architect`、`developer`、`reviewer`、`qa`、`assistant`、`ra`、`security`、`sales`、`sre`。backends：`ollama`、`codex`、`agy`。`developer` 和 `qa` 会更新 senior/middle/junior routes。`python3 orchestrator.py set-backend reviewer agy` |

每个 subcommand 也支持 `--help`，例如 `python3 orchestrator.py review --help`。无效的 role/backend/decision 值会被 argparse 拒绝。`.ai-company/` 缺失时，`status`、`approve`、`review`、`reset` 和 `set-backend` 会报告需要初始化；`approve` 和 `review` 也会拒绝非 `WAITING_FOR_OWNER` state。

### Common CLI sequences

最小本地设置与任务：

```bash
python3 orchestrator.py init
python3 orchestrator.py start "Add contact search and tests"
python3 orchestrator.py step
python3 orchestrator.py status
```

运行正常的 automatic workflow（需要已配置的 backends，默认还需要 Git）：

```bash
python3 orchestrator.py run
```

设置受支持的 backend，或在运行 workflow 前选择 AGY 型号：

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json to pin role_model_routes.ra[0][1] if needed.
agy models
```

处理暂停的 review：

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

## Automatic weighting 与 role-play staffing

系统根据内部 staff（Ollama）和付费 vendors（Codex、Agy）的实际表现调整信任权重和投票影响力。

### Staff 与 vendors

- **Internal staff**：路由到 `ollama` backend 的 `developer_senior/middle/junior` 和 `qa_senior/middle/junior` roles，会被视为免费的长期 staff，并累积 trust weight（`worker_weights`）。
- **Paid vendors**：`codex` 和 `agy` CLI backends 由 `vendor_scores` 独立跟踪。

### Trust weights 与 scoring events

- 每个 role（`rd`/`qa`）x level（`senior`/`middle`/`junior`）都有介于 `0.2` 和 `3.0` 之间的 trust weight，默认使用 `worker_weights` 中的值。
- Task completion、QA pass/fail 以及 Reviewer approve/reject events 会触发 EWMA（exponentially weighted moving average）更新，使 weight 随时间逐步调整；vendors 则更新各自的 scorecard，因此两者不会互相影响。

### QA weighted voting

- QA 不再要求 unanimous agreement。每个 level 的 vote 按其 trust weight 加权；weighted ratio 达到 `qa_vote_threshold`（默认 `0.6`）时 run 通过。无论 vote 结果如何，test-command 的非零 exit code 仍会直接使 run 失败。
- 如果 weighted majority 覆盖少数 FAILED vote，该 dissent 会记录在 QA report 中，并作为第二次检查呈现给 Reviewer。如果 human 后续覆盖 QA failure，投了 FAILED 的 level(s) 会因 false positive 受到惩罚。

### Manager performance review

- 每次 run 结束时，Manager 根据该次 run 的 telemetry 提议小幅（±`weight_adjustment_max_delta`，默认 `0.3`）weight adjustments，限制在合法范围内并自动写回 config。Vendor scorecards 仅供参考，不会被此 review 修改。

### Daily roster

- 在为 task staffing 前，Manager 会读取 today's roster：哪些 internal staff roles online（Ollama 是否可访问）、哪些 vendors available，以及每个角色当前的 weight 或 rating。
- 如果 assigned level offline，task 会自动 reassigned 到最近的 available level；在其他条件相同时，优先选择 weight/rating 较高者。

### Vendor firefighting

- 当 internal staff 持续难以完成 task（revisions 即将耗尽、trust weight 已降得过低，或 Ollama 不可访问）时，系统会从 `backend_escalation_path` 中自动将 task 交给 rating 最佳的 available vendor 进行 firefighting。这是 deterministic decision，不会进行额外的 model call。

### Internal company memory (brain)

- Internal staff（仅限 Ollama backend）可以访问 sandboxed SQLite + FTS5 “brain” database，其中记录 failure lessons 和 meeting summaries，并通过 RAG 注入相关的 future prompts。Vendor prompts 看不到这段 memory，因此 company knowledge 不会离开 sandbox。
- 只有 fixed code hooks 会写入该 database；model output 不会直接写入，从而防止 prompt injection 污染 long-term memory。

### New CLI commands

| Command | Description |
| --- | --- |
| `weights` | 显示当前 internal staff trust weights |
| `weights set <role> <level> <value>` | 手动覆盖一个 weight |
| `roster` | 显示今天可用的 RD/QA roles（staff 和 vendors） |
| `vendors` | 显示 paid-vendor scorecard |
| `brain search <query>` | 搜索 internal company memory |
| `brain prune [--keep N]` | 清理旧 memory，仅保留最近的 N 条 |

### New configuration keys

`worker_weights`、`qa_vote_threshold`、`weight_adjustment_max_delta`、`vendor_scores`、`vendor_hire_min_jobs`、`brain_db`（默认值请参阅 `orchestrator/core/config.py`）。

## Tests and verification

从 repository root 安装测试依赖并运行可用检查：

```bash
python3 -m pip install requests pytest
python3 -m pytest -q
python3 -m pytest -q test_orchestrator.py
python3 -m pytest -q test_orchestrator.py::test_initialized_orchestrator_fixture_loads_temp_config_and_state
python3 verify_alignment.py
git diff --check
```

测试使用 mocks、fixtures 和 temporary directories，不需要外部 AI 认证。pytest 成功时退出码为 `0`；`git diff --check` 可检查基本空白问题。

真实 workflow 执行仍需要选定的 backend CLI 和登录状态、可访问的 `ollama` role 所需 Ollama server，以及 `use_worktree` 为 true 时的 Git worktree 支持。目标项目需要其他测试时，可在 `.ai-company/config.json` 中通过 `test_command` 修改 test command。

## Scope and limitations

- Specialist selection is dynamic; no CLI command forces RA or Sales for a task.
- AGY auto selectors require `agy models` to succeed; explicit model IDs stay pinned.
- `set-backend` supports only the documented roles and backend values.
- `reset --state` 接受 string；请使用有效 workflow states 才能恢复有意义的工作。
- AI workflow commands 可能修改目标项目的 Git worktree，并可能产生 provider 使用费用。

## Future Direction

This project aims to evolve from a deterministic multi-agent orchestration framework into an AI organization simulator capable of dynamically forming engineering teams, allocating responsibilities, and governing autonomous collaboration.
