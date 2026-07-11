# Multi-Agent Orchestrator

[繁體中文](README.md) | [English](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md)

這是一個基於 Python 的多代理開發流程，使用確定性狀態機完成規劃、實作、QA、程式碼審查和最終報告。

## 系統要求與安裝

請在 shell 中執行以下命令。預設 Git worktree 流程需要 Git；專案需要 Python 3。專案使用 `requests`，測試也使用 `pytest`。

```bash
git clone https://github.com/adombciha/multiple-agents-orch.git
cd multiple-agents-orch
python3 -m pip install requests pytest
python3 orchestrator.py --help
```

最後一條命令只驗證 CLI 能載入，不會連線至 AI backend 或建立 worktree。專案沒有 `--version` 選項。

要執行 AI workflow，請安裝並登入 `.ai-company/config.json` 中選定的 CLI backend（`grok`、`codex`、`agy` 或 `claude`），或為 `ollama` role 執行 Ollama 服務。協調器不定義額外的 Grok API-key 環境變數；請按照 `grok` CLI 的要求完成認證。只有在 Git worktree 隔離不可用或不需要時，才將設定中的 `use_worktree` 設為 `false`。

## Workflow 與檔案

`start` 儲存 request 並將 state 重設為 `PLANNING`。正常 state 流程如下：

```text
PLANNING → DEVELOPING_PLAN → REVIEWING_PLAN → IMPLEMENTING
→ TESTING → REVIEWING_CODE → COMPLETED
```

PM 分析需求並安排可選 specialists。其結果會提供給 Architect 進行 plan review。RD 實作任務，QA 執行設定的 test command 並報告結果，Reviewer 評估需求、計畫、測試、specialist 結果和 Git diff，Assistant 在完成後建立 `CHANGELOG.md`。

`init` 建立 `.ai-company/`，其中包括 `config.json`、`state.json`、`request.md`、`requirements.md`、`implementation_plan.md`、`action_items.json`、agent 輸出、`test_results.txt`、`qa_report.md`、`reviewer_output.md`、`specialist_reviews.md`、`human_report.md` 和 `final_report.md`。

## 設定

編輯 `.ai-company/config.json` 前先執行 `python3 orchestrator.py init`。缺少的鍵會使用 `orchestrator/core/config.py` 中的預設值。

| Key | 用途與預設行為 |
| --- | --- |
| `ollama_url`、`ollama_model` | Ollama endpoint 與預設 model（`http://localhost:11434`、`gemma4:latest`）。 |
| `ollama_keep_alive` | Ollama model 在回覆後保留於記憶體的時間；預設 `0`，每次回覆後立即卸載以釋放 VRAM。 |
| `test_command`、`max_revisions` | QA command 與自動 plan/code revision 上限；預設值為 `python3 -m pytest -q` 和 `2`。 |
| `backends` | 分配給各 role 的 backend。 |
| `model_tiers`、`role_models`、`role_model_backends`、`role_model_tiers` | legacy model 清單及每個 role 的主要 model/backend 路由。 |
| `role_model_routes` | 每個既有 role 的跨 backend model 回退鏈；依表格順序嘗試，quota 耗盡的 backend 會在本次 workflow 跳過。 |
| `use_ponytail`、`use_worktree` | 極簡提示與 Git-worktree 隔離；預設分別為 `false` 和 `true`。 |
| `backend_escalation_path`、`staffing_limits` | backend 升級路徑及 RD/QA staffing 限制。 |

## 建議角色與模型路由

下表是依目前本機實測結果整理的目標路由。模型回退會在主要模型不可用時依序嘗試；quota 耗盡或本 run 失敗的 route 會被跳過。實際啟用路由以 `orchestrator/core/config.py` 為準。

| 角色 | 何時 invoke | 主要模型 | 回退 1 | 回退 2 |
| --- | --- | --- | --- | --- |
| PM | 每次任務 | `gpt-5.6-sol` | `gemini-3.5-flash` | `qwen3:8b` |
| Architect | 每次任務 | `gemma4:latest` | `qwen3:8b` | `qwen2.5-coder:7b` |
| RD Senior | 跨模組／架構／高風險實作 | `grok-4.5` | `gpt-oss:20b` | `granite4.1:8b` |
| RD Middle | 一般功能實作 | `grok-4.5` | `granite4.1:8b` | `codegemma:7b` |
| RD Junior | 重複、明確、小修改 | `codegemma:7b` | `granite4.1:8b` | `qwen2.5-coder:7b` |
| QA Senior | 安全、架構、複雜流程 | `gemma4:latest` | `qwen2.5-coder:7b` | — |
| QA Middle | 整合與一般功能驗證 | `gemma4:latest` | `qwen2.5-coder:7b` | — |
| QA Junior | 格式、文件、基本 regression | `gemma4:latest` | `codegemma:7b` | `qwen2.5-coder:7b` |
| Reviewer | 最終程式碼審查 | `gpt-5.6-sol` | `deepseek-coder:6.7b` | `gemma4:latest` |
| Security | auth、secrets、PII、payment、攻擊面 | `deepseek-coder:6.7b` | `gemma4:latest` | `qwen2.5-coder:7b` |
| DevOps | CI/CD、Docker、release、rollback、pipeline | `granite4.1:8b` | `qwen2.5-coder:7b` | `gemma4:latest` |
| RA | 法規、合規、產業規範 | `grok-4.5` | `qwen3:8b` | `gemma4:latest` |
| Sales | 競品、商業需求、客戶價值 | `grok-4.5` | `qwen3:8b` | `gemma4:latest` |
| UI/UX | UI、流程、a11y、wireframe | `granite4.1:8b` | `gemma4:latest` | — |
| UI/UX Visual Review | 有 screenshot／mockup 時 | `qwen3-vl:4b` | — | — |
| FAE | 客戶環境、SDK、設備、驗收 | `granite4.1:8b` | `gemma4:latest` | — |
| Integration | 外部 API、設備協定、第三方平台 | `deepseek-coder:6.7b` | `granite4.1:8b` | `gemma4:latest` |
| Assistant | changelog、摘要、文件整理 | `granite4.1:8b` | `gemma4:latest` | `ministral-3:3b` |

模型分析不能取代實體硬體、客戶環境或實際部署驗證。涉及設備、客戶或 pipeline 發布時，FAE、Integration 和 DevOps 的輸出應列出需由人或執行環境確認的項目。

本機驗證顯示 `gpt-oss:20b` 能穩定遵守檔案 block 協議，但較慢，適合 Senior/Middle 的高品質 fallback；`codegemma:7b` 是 Junior 寫檔主力。`qwen3.6:latest` 能回覆但耗時較長，保留作手動高價值審核；`granite4.1-guardian:8b` 僅適合作為 PHI／安全分類 gate，不作一般聊天或程式角色。`medgemma1.5:4b` 可作 PCR、生醫檢測與 HIPAA 相關的醫療語境輔助研究，不取代 RA、Security 或真人法規審核。

使用 `start` 的 `--image` 可提供 screenshot 或 mockup（可重複指定），UI/UX Visual Review 會將它送至 Ollama vision API。圖片任務必須使用 vision-capable model，不可降級到純文字 fallback：

```bash
python3 orchestrator.py start "Review the settings screen" --image /absolute/path/to/screenshot.png
```

內部也支援 specialist context 的 `[IMAGE: /absolute/path/to/screenshot.png]` marker；不存在或無法讀取的檔案會略過，並維持文字 review。

## Grok specialists 說明

Grok 是動態 RA 和 Sales specialists 使用的外部 CLI backend，預設兩者都設定為 `grok`。它不是頂層 workflow state，也不替代 PM、Architect、RD、QA 或 Reviewer。PM 只在任務需要時於需求分析和 staffing 階段選擇 RA 或 Sales；分析結果會提供給 Architect 進行 plan review。

在 workflow 選擇任一 role 前，請安裝可用且已認證的 `grok` CLI。專案使用的直接介面是：

```bash
grok -p "<prompt>" -m grok-4.5
```

支援設定的 Grok model 是 `grok-4.5`。請在 `.ai-company/config.json` 中設定 `backends.ra` / `backends.sales`、`role_models.ra` / `role_models.sales`、`role_model_backends.ra` / `role_model_backends.sales`、`role_model_tiers.ra` / `role_model_tiers.sales` 和 `model_tiers.grok`。role model 依序從 `role_model_tiers`、`role_models`，最後從 `grok-4.5` 解析。

`set-backend` 可以選擇 `ra` 和 `sales` roles，但 backend choices 不包含 `grok`；請編輯 JSON 檔案設定 Grok。若 Grok 不可用或請求失敗，role 按以下順序 fallback：

```text
grok CLI → AGY (gpt-oss-120b) → Ollama (configured model) → error
```

RA 輸出是 model review，不是經過驗證的法律研究。專案沒有直接強制選擇 specialist 或將 backend 設為 `grok` 的 CLI 子命令。

## 自動 review 與 human review

Automatic review 與 owner review 使用不同的 result 名稱和 transition。

| 產生者 | Result | 影響 |
| --- | --- | --- |
| QA | `PASSED` | 從 `TESTING` 繼續到 `REVIEWING_CODE`。 |
| QA | `FAILED` | 建立 QA fix task 並返回 `IMPLEMENTING`；達到 `max_revisions` 後暫停在 `WAITING_FOR_OWNER`。 |
| Reviewer | `APPROVED` | 繼續完成流程。 |
| Reviewer | `REJECTED` | 建立 reviewer fix task 並返回 `IMPLEMENTING`；達到 `max_revisions` 後暫停在 `WAITING_FOR_OWNER`。 |
| Architect | plan revision | 修訂計畫直到 `max_revisions`；之後繼續到 `IMPLEMENTING`，不會建立 human review。 |

`max_revisions` 預設是 `2`，但實際值由目前 `.ai-company/config.json` 控制。QA 和 Reviewer 失敗會增加 `code_revisions`；達到 retry limit 後才建立 owner review。AI backend failure、test command 非零 exit status 或其他 command error 屬於 execution failure，不等同於 owner 明確作出的 `reject` decision。

在 `WAITING_FOR_OWNER` 中使用 `approve` 或 `review`：

| Decision | 含義 | 後續行為 |
| --- | --- | --- |
| `pass` | 接受儲存的通過路徑。 | 從記錄的 `pass_state` 恢復，然後可選擇使用 `--run` 繼續。 |
| `revise` | 請求再次實作。 | 增加 `code_revisions`，新增 `HUMAN-REVIEW-N`，並移到 `IMPLEMENTING`；不受之前 automatic retry threshold 阻擋。 |
| `reject` | 拒絕本次 workflow 結果。 | 移到 `FAILED`；正常執行停止。 |

`approve` 是暫停的通過路徑的快捷方式。成功的 `review` 輸出可透過以下內容識別：

```text
Human review '<decision>' recorded; workflow moved to <STATE>.
```

使用 `python3 orchestrator.py status` 查看目前 state 和 revision counters。`--run` 只有在所選 decision 留下可執行 workflow 時才繼續；`reject` 後不會執行。

## CLI 參考

執行 `python3 orchestrator.py --help` 查看目前 command list。以下每條命令都從 repository root 執行；除 `init`、`start` 和 `--help` 外，其他命令都需要已初始化的 `.ai-company/` 目錄。

| Command | Syntax | 必填 arguments | 選填 arguments / 預設值 | Output、前置條件與範例 |
| --- | --- | --- | --- | --- |
| Help | `python3 orchestrator.py --help` | None | `-h`、`--help` | 輸出 commands 並結束。`python3 orchestrator.py --help` |
| `init` | `python3 orchestrator.py init` | None | None | 建立 `.ai-company/config.json` 和 state files。`python3 orchestrator.py init` |
| `start` | `python3 orchestrator.py start <prompt>` | `prompt`: development request | None | 初始化、儲存 request 並重設為 `PLANNING`；不會執行 agents。`python3 orchestrator.py start "Add a command and tests"` |
| `step` | `python3 orchestrator.py step` | None | None | 執行一個 state-machine step；可能需要選定的 AI CLIs、Ollama 和 Git worktree。`python3 orchestrator.py step` |
| `run` | `python3 orchestrator.py run` | None | None | 執行直到 `COMPLETED`、`WAITING_FOR_OWNER` 或 `FAILED`。`python3 orchestrator.py run` |
| `status` | `python3 orchestrator.py status` | None | None | 輸出 state、revision counters、選定 backends、test command 和 action items。`python3 orchestrator.py status` |
| `approve` | `python3 orchestrator.py approve [--run]` | None | `--run`：approval 後繼續；預設關閉 | 僅在 `WAITING_FOR_OWNER` 有效；從記錄的 state 恢復。`python3 orchestrator.py approve --run` |
| `review` | `python3 orchestrator.py review {pass,revise,reject} [--run]` | `decision`：`pass`、`revise` 或 `reject` | `--run`：在 `pass` 或 `revise` 後繼續；預設關閉 | 僅在 `WAITING_FOR_OWNER` 有效；輸出 recorded-decision message。`python3 orchestrator.py review revise --run` |
| `reset` | `python3 orchestrator.py reset [--state STATE]` | None | `--state STATE`；預設 `PLANNING`，接受 string state value | 清除 revision/runtime routing data，移除 worktree 但不合併，並儲存選定 state。`python3 orchestrator.py reset --state DEVELOPING_PLAN` |
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`、`backend` | None | 更新 config 中的 `backends`。roles：`manager`、`architect`、`developer`、`reviewer`、`qa`、`assistant`、`ra`、`security`、`sales`、`sre`。backends：`ollama`、`codex`、`claude`、`gemini`、`agy`。`developer` 和 `qa` 會更新 senior/middle/junior routes。`python3 orchestrator.py set-backend reviewer agy` |

每個 subcommand 也支援 `--help`，例如 `python3 orchestrator.py review --help`。無效的 role/backend/decision 值會被 argparse 拒絕。`.ai-company/` 缺失時，`status`、`approve`、`review`、`reset` 和 `set-backend` 會報告需要初始化；`approve` 和 `review` 也會拒絕非 `WAITING_FOR_OWNER` state。

### 常見 CLI 序列

最小本機設定與任務：

```bash
python3 orchestrator.py init
python3 orchestrator.py start "Add contact search and tests"
python3 orchestrator.py step
python3 orchestrator.py status
```

執行正常的 automatic workflow（需要已設定的 backends，預設也需要 Git）：

```bash
python3 orchestrator.py run
```

設定受支援的 backend，或在執行 workflow 前透過設定項為 specialist 設定 Grok：

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json: keep backends.ra or backends.sales as "grok".
grok -p "Summarize the regulatory risks" -m grok-4.5
```

處理暫停的 review：

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

## 測試與驗證

從 repository root 安裝測試相依套件並執行可用檢查：

```bash
python3 -m pip install requests pytest
python3 -m pytest -q
python3 -m pytest -q test_orchestrator.py
python3 -m pytest -q test_orchestrator.py::test_initialized_orchestrator_fixture_loads_temp_config_and_state
python3 verify_alignment.py
git diff --check
```

測試使用 mocks、fixtures 和 temporary directories，因此不需要真實 Grok 或其他 external AI credential。pytest 成功時會顯示通過摘要並以 exit code `0` 結束；`verify_alignment.py` 會報告所有翻譯檔案結構對齊並以 `0` 結束。repository 沒有獨立設定的 lint、type-check 或 formatter command，因此不額外記錄虛構命令；`git diff --check` 是可用的基本 whitespace/patch 檢查。

真實 workflow 執行仍需要選定的 backend CLI 和登入狀態、可存取的 `ollama` role 所需 Ollama server，以及 `use_worktree` 為 true 時的 Git worktree 支援。目標專案需要其他測試時，可在 `.ai-company/config.json` 中透過 `test_command` 修改 test command。

## 範圍與限制

- Specialist 選擇是動態的；沒有 CLI command 可以強制選擇 Grok、RA 或 Sales。
- Grok 的設定 model list 包含 `grok-4.5`；沒有第二個設定的 Grok-model fallback。
- `set-backend` 只支援文件列出的 roles 和 backend values，不能設定 `grok`。
- `reset --state` 接受 string；請使用有效 workflow states 才能恢復有意義的工作。
- AI workflow commands 可能修改目標專案的 Git worktree，並可能產生 provider 使用費用。
