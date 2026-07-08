# Multi-Agent 流程协调器 (Orchestrator)

[繁體中文](README.md) | [English](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md)

本项目是一个用 Python 编写的轻量级 Multi-Agent 流程协调器。它能够协调本机的 Ollama 模型（Manager 与 Reviewer）、Codex CLI（Developer）与 Claude Code，以固定状态机（State Machine）的方式自动执行需求规划、代码实现、单元测试和代码审查的闭环开发流程。

---

## 系统架构

```text
               你输入需求
                   ↓
         [ Python Orchestrator ]
                   ↓
         [ Manager (负责分析需求、拆解任务) ]
                   ↓
  ┌────────────────┬────────────────┐
  │   Developer    │    Reviewer    │
  │ (负责实现任务) │ (负责代码审查) │
  └────────────────┴────────────────┘
                   ↓
         [ QA Agent 进行自动化验证 ]
                   ↓
         [ Reviewer 进行代码审查 ]
          ├── 通过 → 合并分支并生成 Final Report
          └── 退回 → 生成修复任务单 (FIX-TASK) 交回 Developer 单点修改
                   ↓
         [ Assistant (自动生成 CHANGELOG.md) ]
```

---

## 快速上手指令

### 1. 初始化环境
```bash
python3 orchestrator.py init
```

### 2. 启动新任务
```bash
python3 orchestrator.py start "加入联系人搜索功能，并在 search.py 写好对应测试"
```

### 3. 单步执行
```bash
python3 orchestrator.py step
```

### 4. 全自动执行到结束
```bash
python3 orchestrator.py run
```
