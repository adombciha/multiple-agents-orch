# Multi-Agent Orchestrator

[English](README.md) | [繁體中文](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md) | [Русский](README_ru.md)

這是一個基於 Python 的多代理開發流程，使用確定性狀態機完成規劃、實作、QA、程式碼審查和最終報告。

## 系統要求與安裝

請在 shell 中執行以下命令。預設 Git worktree 流程需要 Git；專案需要 Python 3。專案使用 `requests`、`litellm`，測試也使用 `pytest`。

```bash
git clone https://github.com/adombciha/multiple-agents-orch.git
cd multiple-agents-orch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python orchestrator.py --help
```

最後一條命令只驗證 CLI 能載入，不會連線至 AI backend 或建立 worktree。專案沒有 `--version` 選項。

要執行 AI workflow，請安裝並登入選定的 CLI backend（`codex` 或 `agy`），或為 `ollama` role 執行 Ollama 服務。只有在 Git worktree 隔離不可用或不需要時，才將 `use_worktree` 設為 `false`。

## Workflow 與檔案

`start` 儲存 request 並將 state 重設為 `PLANNING`。正常 state 流程如下：

```text
PLANNING → DEVELOPING_PLAN → REVIEWING_PLAN → IMPLEMENTING
→ TESTING → REVIEWING_CODE → COMPLETED
```

PM 分析需求並安排可選 specialists。其結果會提供給 Architect 進行 plan review。RD 實作任務，QA 執行設定的 test command 並報告結果，Reviewer 評估需求、計畫、測試、specialist 結果和 Git diff，Assistant 在完成後建立 `CHANGELOG.md`。

`init` 建立 `.ai-company/`，其中包括 `config.json`、`state.json`、`request.md`、`requirements.json`、由程式產生的人類可讀 `requirements.md`、`implementation_plan.md`、`action_items.json`、agent 輸出、`test_results.txt`、`qa_report.md`、`reviewer_output.json`、`reviewer_output.md`、`specialist_reviews.md`、`human_report.md` 和 `final_report.md`。機器交換資料優先使用 JSON，人類閱讀使用 Markdown。

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
| `telemetry_enabled`、`llm_usage_log` | LiteLLM token metadata 記錄開關與 JSONL 路徑；預設記錄到 workflow 目錄的 `llm_usage.jsonl`，不保存 prompt/output。 |

## 建議角色與模型路由

下表是依目前本機實測結果整理的目標路由。模型回退會在主要模型不可用時依序嘗試；quota 耗盡或本 run 失敗的 route 會被跳過。實際啟用路由以 `orchestrator/core/config.py` 為準。

| 角色 | 何時 invoke | 主要模型 | 回退 1 | 回退 2 |
| --- | --- | --- | --- | --- |
| PM | 每次任務 | Codex `auto` | AGY `auto:gemini-flash-medium` | `qwen3:8b` |
| Architect | 每次任務 | Codex `auto` | AGY `auto:gemini-flash-medium` | `gemma4:latest` |
| RD Senior | 跨模組／架構／高風險實作 | Codex `auto` (`low`) | `gpt-oss:20b` | `granite4.1:8b` |
| RD Middle | 一般功能實作 | `granite4.1:8b` | `codegemma:7b` | `qwen2.5-coder:7b` |
| RD Junior | 重複、明確、小修改 | `gemma4:latest` | `qwen2.5-coder:7b` | `codegemma:7b` |
| QA Senior | 安全、架構、複雜流程 | `gemma4:latest` | `qwen2.5-coder:7b` | — |
| QA Middle | 整合與一般功能驗證 | `gemma4:latest` | `qwen2.5-coder:7b` | — |
| QA Junior | 格式、文件、基本 regression | `gemma4:latest` | `codegemma:7b` | `qwen2.5-coder:7b` |
| Reviewer | 最終程式碼審查 | Codex `auto` | AGY `auto:gemini-flash-medium` | `gemma4:latest` |
| Security | auth、secrets、PII、payment、攻擊面 | `deepseek-coder:6.7b` | `gemma4:latest` | `qwen2.5-coder:7b` |
| DevOps | CI/CD、Docker、release、rollback、pipeline | `granite4.1:8b` | `qwen2.5-coder:7b` | `gemma4:latest` |
| RA | 法規、合規、產業規範 | AGY `auto:gemini-flash-medium` | `qwen3:8b` | `gemma4:latest` |
| Sales | 競品、商業需求、客戶價值 | AGY `auto:gemini-flash-medium` | `qwen3:8b` | `gemma4:latest` |
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

## 自動選型說明

Codex role 使用 `auto`，交由已安裝的 Codex CLI 選擇推薦模型。AGY role 預設使用 `auto:gemini-flash-medium`，執行時透過 `agy models` 選擇最新 Gemini Flash Medium，清單快取一小時。也可以用 `auto:gemini-flash-low`、`auto:gemini-pro-low`、`auto:claude-sonnet-medium`、`auto:claude-opus-medium` 等系列選擇器。

AGY 的 Gemini 3.8 Flash Low、3.7 Flash Low/Medium、3.1 Pro、Claude Sonnet 與 Claude Opus 型號也都可以直接指定 ID；手動指定就維持固定版本。移除的是獨立的 Claude Code 與 Grok backend，不是 AGY 內的 Claude 模型。

```bash
agy models
```

載入舊設定時，已移除的 Grok／Claude Code backend 會遷移至該角色的新預設；Ollama 仍只使用本機已安裝的模型，不會自動下載。

`set-backend` 支援 `ollama`、`codex`、`agy`。RA 輸出是 model review，不是經過驗證的法律研究；specialist 仍由 PM 依任務需求選擇。

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
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`、`backend` | None | 更新 config 中的 `backends`。roles：`manager`、`architect`、`developer`、`reviewer`、`qa`、`assistant`、`ra`、`security`、`sales`、`sre`。backends：`ollama`、`codex`、`agy`。`developer` 和 `qa` 會更新 senior/middle/junior routes。`python3 orchestrator.py set-backend reviewer agy` |

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

設定受支援的 backend，或在執行 workflow 前為 specialist 選擇 AGY 模型：

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json to pin role_model_routes.ra[0][1] if needed.
agy models
```

處理暫停的 review：

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

## 自動加權與角色扮演式 staffing

系統將內部 staff（Ollama）和付費 vendor（Codex、Agy）視為一家小型公司，根據實際表現調整信任權重與投票影響力。

### Staff 與 vendor

- **Internal staff**：路由至 `ollama` backend 的 `developer_senior/middle/junior` 和 `qa_senior/middle/junior` role，會被視為免費、長期 staff，並累積信任權重（`worker_weights`）。
- **Paid vendors**：`codex` 和 `agy` CLI backend 不計入 internal performance；每個 `backend/model` 配對會以獨立的 **vendor scorecard**（`vendor_scores`）追蹤。

### 信任權重與評分事件

- 每個 role（`rd`／`qa`）x level（`senior`／`middle`／`junior`）都有介於 `0.2` 和 `3.0` 之間的信任權重，預設值來自 `worker_weights`。
- Task completion、QA pass/fail 和 Reviewer approve/reject 事件會觸發 EWMA（exponentially weighted moving average）更新，讓權重隨時間逐步調整；vendor 則更新自己的 scorecard，因此兩者不會互相干擾。

### QA 加權投票

- QA 不再要求一致同意。每個 level 的 vote 依其信任權重加權；加權比例達到 `qa_vote_threshold`（預設 `0.6`）時，該 run 即通過。非零的 test-command exit code 仍會直接使 run 失敗，不受投票結果影響。
- 如果加權多數推翻少數的 FAILED vote，該異議會記錄在 QA report 中，並提供給 Reviewer 作為第二次檢查。如果 human 之後推翻 QA failure，投票為 FAILED 的 level 會因 false positive 受到懲罰。

### Manager performance review

- 每次 run 結束時，Manager 會根據該 run 的 telemetry 提出小幅（±`weight_adjustment_max_delta`，預設 `0.3`）權重調整，限制在合法範圍內並自動寫回 config。Vendor scorecard 僅供參考，不會被這項 review 修改。

### Daily roster

- 在為 task staffing 前，Manager 會讀取今日 roster：哪些 internal staff role 在線（Ollama 是否可連線）、哪些 vendor 可用，以及各自目前的 weight 或 rating。
- 如果 assigned level 離線，task 會自動重新分配給最近的可用 level；在其他條件同樣適合時，優先選擇 weight/rating 較高者。

### Vendor firefighting

- 當 internal staff 持續難以處理 task（revision 快用完、信任權重降得太低，或 Ollama 無法連線）時，系統會自動從 `backend_escalation_path` 將 task 交給評分最高的可用 vendor 進行 firefighting。這是 deterministic decision，不是額外的 model call。

### Internal company memory（brain）

- Internal staff（僅 Ollama backend）可存取 sandboxed SQLite + FTS5「brain」database，記錄 failure 的 lessons 和 meeting summaries，並透過 RAG 注入相關的後續 prompt。Vendor prompt 看不到這份 memory，因此 company knowledge 不會離開 sandbox。
- 只有固定的 code hooks 會寫入此 database；model output 不會直接寫入，避免 prompt injection 污染長期 memory。

### New CLI commands

| Command | Description |
| --- | --- |
| `weights` | 顯示目前 internal staff 的信任權重 |
| `weights set <role> <level> <value>` | 手動覆寫權重 |
| `roster` | 顯示今日可用的 RD/QA role（staff 與 vendor） |
| `vendors` | 顯示 paid-vendor scorecard |
| `brain search <query>` | 搜尋 internal company memory |
| `brain prune [--keep N]` | 清理舊的 memory，只保留最近的 N 筆 |

### New configuration keys

`worker_weights`、`qa_vote_threshold`、`weight_adjustment_max_delta`、`vendor_scores`、`vendor_hire_min_jobs`、`brain_db`（請參閱 `orchestrator/core/config.py` 中的預設值）。

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

測試使用 mocks、fixtures 和 temporary directories，因此不需要 external AI credential。pytest 成功時會顯示通過摘要並以 exit code `0` 結束；`verify_alignment.py` 檢查翻譯檔案結構。`git diff --check` 是可用的基本 whitespace/patch 檢查。

真實 workflow 執行仍需要選定的 backend CLI 和登入狀態、可存取的 `ollama` role 所需 Ollama server，以及 `use_worktree` 為 true 時的 Git worktree 支援。目標專案需要其他測試時，可在 `.ai-company/config.json` 中透過 `test_command` 修改 test command。

## 範圍與限制

- Specialist 選擇是動態的；沒有 CLI command 可以強制選擇 RA 或 Sales。
- AGY auto selector 需要 `agy models` 成功；手動指定的模型 ID 保持固定。
- `set-backend` 只支援文件列出的 roles 和 backend values。
- `reset --state` 接受 string；請使用有效 workflow states 才能恢復有意義的工作。
- AI workflow commands 可能修改目標專案的 Git worktree，並可能產生 provider 使用費用。

## Future Direction

This project aims to evolve from a deterministic multi-agent orchestration framework into an AI organization simulator capable of dynamically forming engineering teams, allocating responsibilities, and governing autonomous collaboration.
