# Multi-Agent Orchestrator

[繁體中文](README.md) | [English](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md)

这是一个基于 Python 的多代理开发流程，使用确定性状态机完成规划、实现、QA、代码审查和最终报告。

## 系统要求与安装

请在 shell 中执行以下命令。默认 Git worktree 流程需要 Git；项目需要 Python 3。项目使用 `requests`，测试还使用 `pytest`。

```bash
git clone https://github.com/adombciha/multiple-agents-orch.git
cd multiple-agents-orch
python3 -m pip install requests pytest
python3 orchestrator.py --help
```

最后一条命令只验证 CLI 能加载，不会连接 AI backend 或创建 worktree。项目没有 `--version` 选项。

要运行 AI workflow，请安装并登录 `.ai-company/config.json` 中选定的 CLI backend（`grok`、`codex`、`agy` 或 `claude`），或为 `ollama` role 运行 Ollama 服务。协调器不定义额外的 Grok API-key 环境变量；请按照 `grok` CLI 的要求完成认证。只有在 Git worktree 隔离不可用或不需要时，才将配置中的 `use_worktree` 设为 `false`。

## Workflow 与文件

`start` 保存 request 并将 state 重置为 `PLANNING`。正常 state 流程如下：

```text
PLANNING → DEVELOPING_PLAN → REVIEWING_PLAN → IMPLEMENTING
→ TESTING → REVIEWING_CODE → COMPLETED
```

PM 分析需求并安排可选 specialists。其结果会提供给 Architect 进行 plan review。RD 实现任务，QA 执行配置的 test command 并报告结果，Reviewer 评估需求、计划、测试、specialist 结果和 Git diff，Assistant 在完成后创建 `CHANGELOG.md`。

`init` 创建 `.ai-company/`，其中包括 `config.json`、`state.json`、`request.md`、`requirements.md`、`implementation_plan.md`、`action_items.json`、agent 输出、`test_results.txt`、`qa_report.md`、`reviewer_output.md`、`specialist_reviews.md`、`human_report.md` 和 `final_report.md`。

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

## Grok specialists

Grok 是动态 RA 和 Sales specialists 使用的外部 CLI backend，默认两者都配置为 `grok`。它不是顶层 workflow state，也不替代 PM、Architect、RD、QA 或 Reviewer。PM 只在任务需要时于需求分析和 staffing 阶段选择 RA 或 Sales；分析结果会提供给 Architect 进行 plan review。

在 workflow 选择任一 role 前，请安装可用且已认证的 `grok` CLI。项目使用的直接接口是：

```bash
grok -p "<prompt>" -m grok-4.5
```

支持配置的 Grok model 是 `grok-4.5`。请在 `.ai-company/config.json` 中配置 `backends.ra` / `backends.sales`、`role_models.ra` / `role_models.sales`、`role_model_backends.ra` / `role_model_backends.sales`、`role_model_tiers.ra` / `role_model_tiers.sales` 和 `model_tiers.grok`。role model 依次从 `role_model_tiers`、`role_models`，最后从 `grok-4.5` 解析。

`set-backend` 可以选择 `ra` 和 `sales` roles，但 backend choices 不包含 `grok`；请编辑 JSON 文件配置 Grok。若 Grok 不可用或请求失败，role 按以下顺序 fallback：

```text
grok CLI → AGY (gpt-oss-120b) → Ollama (configured model) → error
```

RA 输出是 model review，不是经过验证的法律研究。项目没有直接强制选择 specialist 或将 backend 设为 `grok` 的 CLI 子命令。

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
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`、`backend` | None | 更新 config 中的 `backends`。roles：`manager`、`architect`、`developer`、`reviewer`、`qa`、`assistant`、`ra`、`security`、`sales`、`sre`。backends：`ollama`、`codex`、`claude`、`gemini`、`agy`。`developer` 和 `qa` 会更新 senior/middle/junior routes。`python3 orchestrator.py set-backend reviewer agy` |

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

设置受支持的 backend，或在运行 workflow 前通过配置项为 specialist 配置 Grok：

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json: keep backends.ra or backends.sales as "grok".
grok -p "Summarize the regulatory risks" -m grok-4.5
```

处理暂停的 review：

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

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

测试使用 mocks、fixtures 和 temporary directories，因此不需要真实 Grok 或其他 external AI credential。pytest 成功时会显示通过摘要并以 exit code `0` 结束；`verify_alignment.py` 会报告所有翻译文件结构对齐并以 `0` 退出。repository 没有独立配置的 lint、type-check 或 formatter command，因此不额外记录虚构命令；`git diff --check` 是可用的基本 whitespace/patch 检查。

真实 workflow 执行仍需要选定的 backend CLI 和登录状态、可访问的 `ollama` role 所需 Ollama server，以及 `use_worktree` 为 true 时的 Git worktree 支持。目标项目需要其他测试时，可在 `.ai-company/config.json` 中通过 `test_command` 修改 test command。

## Scope and limitations

- Specialist 选择是动态的；没有 CLI command 可以强制选择 Grok、RA 或 Sales。
- Grok 的配置 model list 包含 `grok-4.5`；没有第二个配置的 Grok-model fallback。
- `set-backend` 只支持文档列出的 roles 和 backend values，不能设置 `grok`。
- `reset --state` 接受 string；请使用有效 workflow states 才能恢复有意义的工作。
- AI workflow commands 可能修改目标项目的 Git worktree，并可能产生 provider 使用费用。
