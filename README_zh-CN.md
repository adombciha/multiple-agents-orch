# Multi-Agent 流程协调器 (Orchestrator)

[繁體中文](README.md) | [English](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md)

本项目是一个用 Python 编写的轻量级 Multi-Agent 流程协调器。它使用确定性状态机（State Machine）进行需求规划、架构审查、代码实现、验证、代码审查和发布说明。每个任务都会根据其复杂度和领域风险路由到相应的角色和模型层级。

---

## 系统架构

```text
               你输入需求 (User Input)
                    ↓
           [ Python Orchestrator ]
                    ↓
           [ PM (负责分析需求、拆解任务) ]
                    ↓
       [ 专家 (RA / Sales → Grok CLI) ]
                    ↓
           [ Architect (负责计划与架构审查) ]
                    ↓
        [ RD 团队 (Senior / Middle / Junior) ]
                    ↓
           [ QA 团队 (Senior / Middle / Junior) ]
                    ↓
           [ Reviewer (负责代码审查) ]
            ├── APPROVED → 合并分支并生成 Final Report
            └── REJECTED → 生成修复任务单 (FIX-TASK) 交回 Developer 单点修改
                     ↓
           [ Assistant (自动生成 CHANGELOG.md) ]
```

---

## 角色高度自定义与动态分配 (Highly Customizable & Dynamic Role Allocation)

此协调器一律启用 PM、Architect、RD、Reviewer、QA 和 Assistant。PM 只有在项目需要时才会选择领域专家（Specialists），然后他们的分析结果会在计划批准前提供给 Architect。

| 角色 | 使用时机 | 默认模型路由 |
| --- | --- | --- |
| PM | 每个项目：需求与任务分配 | Codex `gpt-5.6-sol` |
| Architect | 每个项目：计划与架构审查 | AGY Gemini `gemini-3.1-pro` |
| RD / QA senior | 架构、安全、迁移或模糊的工作 | Codex `gpt-5.6-terra` |
| RD / QA middle | 标准功能与集成工作 | Codex `gpt-5.6-luna` |
| RD / QA junior | 孤立、重复的常规工作 | AGY Gemini `gemini-3.5-flash` |
| Reviewer | 每个项目：代码与测试结果审查 | Codex `gpt-5.6-sol` |
| Assistant | 每个项目：CHANGELOG 与常规文档 | Local Ollama `gemma4:latest` |
| Grok | RA 或 Sales 专家需要时的领域分析 | Grok CLI `grok-4.5` |

### 动态专家 (Dynamic Specialists)

PM 仅在适用其触发条件时启用以下专家：

| 专家 | 触发条件 | 默认模型路由 |
| --- | --- | --- |
| Sales (业务) | 业务范围或验收标准不明确 | Grok CLI `grok-4.5` |
| Security (安全) | 涉及身份验证、密钥、支付、PII（个人识别信息）或攻击面 | Local Ollama `deepseek-r1:latest` |
| RA (法规) | 适用法律、法规、医疗保健、财务合规或隐私义务 | Grok CLI `grok-4.5` |
| SRE | CI/CD、容器、部署、监控或运维可靠性在范围内 | AGY Gemini `gemini-3.1-pro` |

RA 提供的是模型审查，而非经证实的法律研究。生产环境的合规工作应增加权威来源的检索与引用。

### 本次 README 验收与系统角色

Sales 是由 PM 视需求动态启用的系统专家；本次 README 修改的验收流程不安排 Sales 参与，并不代表移除或停用 Sales。QA 负责验证 Markdown 结构、README 所列命令与路径，以及安全可执行的检查结果；QA 通过后，Reviewer 依需求、实现与 QA 结果审查文档正确性、修改范围和首次用户流程，并决定核准或退回修正。

### Grok agent

Grok 是由协调器选择的外部 CLI agent，负责 RA（法规）和 Sales（业务）专家的领域分析；它不是额外的流程阶段，也不会取代 PM、Architect、RD、QA 或 Reviewer。PM 仅在满足专家触发条件时使用 Grok，然后将分析结果交给 Architect 进行计划审查。

使用前必须在运行环境中安装并确保 `grok` CLI 可执行。新初始化的项目已将 Grok 设为 RA 和 Sales 的默认后端：

```bash
python3 orchestrator.py init
```

协调器会通过以下接口调用已配置角色：

```bash
grok -p "<prompt>" -m grok-4.5
```

目前 Grok 仅支持 `grok-4.5`，它也是 RA 和 Sales 的默认模型；没有第二个 Grok 模型用于 fallback。直接使用 `grok` CLI 时可通过 `-m` 传入模型；协调器从 `.ai-company/config.json` 选择 RA 或 Sales 的模型，依次采用 `role_model_tiers.ra[0]` 或 `role_model_tiers.sales[0]`、`role_models.ra` 或 `role_models.sales`，最后使用 `grok-4.5`。`set-backend` 支持 `ra` 和 `sales` 角色，但不接受 `grok` 作为后端参数。如果 Grok CLI 或请求失败，该角色会依次回退到使用 `gpt-oss-120b` 的 AGY，再回退到使用其配置模型的 Ollama。如果这些后端也失败，错误会继续向上抛出。Grok 的 RA 输出是模型审查，不等同于经过验证的法律研究。

### 🚀 最小化配置 (适合：小型工具、单一脚本、快速迭代)

面对明确且范围小的任务，可以仅配置单一角色，以极速产出为主：

```mermaid
graph LR
    classDef role fill:#e1f5fe,stroke:#03a9f4,stroke-width:2px,color:#01579b;
    classDef model_light fill:#e8f5e9,stroke:#4caf50,stroke-width:2px,color:#1b5e20;

    RD_Min["👨‍💻 AI RD<br>(快速代码生成 / 调试)"]:::role --> M_Min(("轻量级模型<br>(如: Gemma 4B)<br>⚡ 极速响应")):::model_light
```

### 🏢 终极最大化配置 (适合：企业级、全生命周期 DevSecOps)

对于企业级和高度合规要求的软件开发，系统能扩充为一支完整的虚拟团队：

* **跨领域协作与合规把关**：AI Business 提出需求，AI PM 转化为工程规格。在合并前，由 AI Security Guard（安全守门员） and AI RA (Regulatory Affairs，法规审查员) 检查合规性。
* **核心实现与交付**：AI RD 负责实现，AI Reviewer 检视质量，最后交由 AI SRE 编写 CI/CD 与部署脚本。
* **辅助与高频任务**：AI QA 负责编写测试用例，AI Assistant 使用轻量模型处理文档生成，以节省计算资源。

---

## 文件目录结构

本工具执行后，会自动在当前目录下创建 `.ai-company/` 文件夹，并包含以下文件：

```text
.ai-company/
├── config.json             # 系统配置文件
├── state.json              # 状态记录与任务清单
├── request.md              # 您的原始需求
├── requirements.md         # Manager 生成的详细功能需求说明书
├── implementation_plan.md  # Developer 生成的步骤化实现计划
├── action_items.json       # 结构化 JSON 任务清单
├── developer_output.md     # Developer 的日志与输出
├── reviewer_output.md      # Reviewer 的审查意见
├── test_results.txt        # 测试指令执行的输出结果
├── qa_report.md            # QA 的验证报告
├── human_report.md         # 等待所有者审查时的报告
├── specialist_reviews.md   # 动态专家的咨询结果
└── final_report.md         # 项目完成后的总结报告

# 项目根目录
└── CHANGELOG.md            # Assistant 自动实时更新的变更日志
```

---

## 快速上手指令

以下主要示例使用项目根目录的包装入口 `python3 orchestrator.py`。模块入口 `python3 -m orchestrator.main <command>` 与其等效，可在偏好 Python 模块执行方式时替代使用；请在同一流程中择一使用。

### 1. 初始化环境
```bash
python3 orchestrator.py init
```

### 2. 启动新任务
```bash
python3 orchestrator.py start "Add contact search feature and write tests in search.py"
```

### 3. 单步执行 (推荐用于调试)
```bash
python3 orchestrator.py step
```

### 4. 全自动执行到结束
```bash
python3 orchestrator.py run
```

### 5. 查看当前状态
```bash
python3 orchestrator.py status
```

### 6. 重置状态
```bash
python3 orchestrator.py reset --state DEVELOPING_PLAN
```

### 7. 更换代理人 (Agent) 后端
```bash
python3 orchestrator.py set-backend developer codex
python3 orchestrator.py set-backend reviewer agy
```

### 8. 批准暂停的工作流程
```bash
python3 orchestrator.py approve --run
```

`approve` 仅适用于当前状态为 `WAITING_FOR_OWNER` 的任务；省略 `--run` 时只恢复状态，不会继续执行。

### 9. 基本验证
```bash
python3 -m pytest -q
```

`run`、`step` 和 `approve --run` 可能调用外部 AI CLI、修改 Git worktree 或产生费用；未配置外部服务时，请先使用 `--help`、`verify_alignment.py` 和上述测试命令进行安全验证。

---

## Ponytail 极简开发原则 (极简代码)

在 `.ai-company/config.json` 中启用 ponytail 模式：
```json
"use_ponytail": true
```
这会强制执行 YAGNI (You Aren't Gonna Need It)，并推动 AI 在不进行过度设计的情况下使用尽可能短的代码变更（Shortest Diff Wins）。

---

## 核心亮点功能

1. **Git Worktree 隔离开发 (零风险)**：所有 AI 操作都在独立的分支与工作区中进行 (`.ai-company/worktree`)。
2. **单点精准修复**：当 QA 验证失败时，仅针对具体失败的逻辑进行修复。
3. **多语言支持**：支持 `en`、`zh-TW`、`ja` 和 `zh-CN`。可在 `config.json` 中修改 `"language"` 设置。
4. **自动生成 CHANGELOG**：Assistant 代理人在项目完成后会自动生成 `CHANGELOG.md`。
