# LiteLLM Usage Telemetry

## Overview

LiteLLM usage telemetry 用於記錄 orchestrator 每次 LLM 呼叫的 token 使用量與結果，支援成本追蹤、容量分析及 RA/SRE 問題排查。Telemetry 由 orchestrator 自行管理：orchestrator 使用自建的 `record_call` 寫入 usage record；`litellm` 僅作為 `token_counter` 使用，不依賴或宣稱任何 LiteLLM 的環境變數、callback 或 logging 開關。

預設設定為 `telemetry_enabled: true`。`_call_with_usage` 在呼叫成功與失敗時都會寫入記錄；寫入位置由 `ai_dir` 與 `llm_usage_log` 組成，預設為 `.ai-company/llm_usage.jsonl`。本功能沒有專用的 telemetry environment variable。

## `telemetry_enabled`

`telemetry_enabled` 是 `config.json` 頂層的布林值，預設為 `true`。設為 `true` 時，orchestrator 會在每次 LLM 呼叫成功或失敗後，透過自建的 `record_call` 記錄 token 使用量與結果；設為 `false` 時，不會記錄這些 usage records。此設定沒有獨立的 telemetry environment variable，也不使用 LiteLLM callback、logging 或 usage 開關。

操作人員可直接修改 `config.json` 中的 `telemetry_enabled` 為 `true` 或 `false`，再重新載入設定或重跑 orchestrator，使設定生效。

## `llm_usage_log`

此配置鍵定義了 LLM 使用量記錄的檔案路徑和行為。它指定了寫入 usage log 的相對檔名 (`llm_usage.jsonl`)。預設路徑透過 `ai_dir` 和該值組合成，解析後為 `.ai-company/llm_usage.jsonl`。此日誌採用 JSON Lines 格式並以追加（append）模式寫入。記錄操作由 `_call_with_usage` 在呼叫成功或失敗時觸發；路徑建構遵循 orchestrator 的內部邏輯，不考慮外部環境變數覆蓋。

## `llm_usage.jsonl` field catalog

`llm_usage.jsonl` 採用 JSON Lines 格式，每行一個 JSON object。下表列出每個 usage record 的所有鍵；所有鍵都會寫入記錄，因此均為必填鍵，但部分值可為 `null`。

| Name | Type | Required | Semantics |
| --- | --- | --- | --- |
| `timestamp` | string | yes | UTC ISO 8601 記錄時間。 |
| `role` | string | yes | 發起 LLM 呼叫的 orchestrator role。 |
| `backend` | string | yes | 使用的 LLM backend。 |
| `model` | string \| null | yes | 使用的模型名稱；無模型名稱時為 `null`。 |
| `success` | boolean | yes | 呼叫成功為 `true`，失敗為 `false`。 |
| `error_category` | string \| null | yes | 成功時為 `null`；失敗時為 `timeout`、`quota`、`contract`，或未分類例外的 Python exception class name。 |
| `elapsed_ms` | number | yes | LLM 呼叫耗時，單位為 milliseconds。 |
| `input_characters` | integer | yes | prompt 與 system prompt 的字元數總和。 |
| `output_characters` | integer | yes | output 的字元數；無 output 時為 `0`。 |
| `input_tokens` | integer \| null | yes | 輸入 token 數；無法計算或未提供時為 `null`。 |
| `output_tokens` | integer \| null | yes | 輸出 token 數；無法計算或未提供 output 時為 `null`。 |
| `total_tokens` | integer \| null | yes | `input_tokens` 與 `output_tokens` 的總和；兩者皆無法取得時為 `null`。 |
| `token_status` | string | yes | token 來源或狀態：`exact`、`provider`、`litellm_unavailable` 或 `unsupported_model`。 |
| `image_count` | integer | yes | 該呼叫包含的 image 數量。 |

Possible `error_category` values are `null`, `timeout`, `quota`, `contract`, or the Python exception class name for other failures. Exception/error text is never logged; only the category is recorded.

## `token_status` values

`token_status` 僅有下列值；它表示 token 數值的來源或可用性，不表示 LLM 呼叫是否成功。

| Value | Trigger | Aggregation impact |
| --- | --- | --- |
| `exact` | LiteLLM `token_counter` 成功計算輸入與輸出 token。 | `input_tokens`、`output_tokens` 與 `total_tokens` 可直接納入 usage totals。 |
| `litellm_unavailable` | LiteLLM 不可用，因而無法進行 token 計算。 | 相關 token 欄位為 `null`；彙總若將 `null` 視為 `0`，會低估 usage totals。 |
| `unsupported_model` | LiteLLM 可用，但不支援指定模型的 token 計算。 | 相關 token 欄位為 `null`；彙總若將 `null` 視為 `0`，會低估 usage totals。 |
| `provider` | 使用 provider/API 或 extension path 提供的 token usage。主流程可能不會產生此值。 | provider 提供的 token 數可納入 usage totals；若 provider 未提供數值，對應欄位仍可能為 `null`。 |

`summary_markdown` 將 `null` 轉為 `0` 顯示或計算時，並不代表實際使用量為零；這會把「未知／未取得」納入零值，造成 usage totals 的 undercount。需要完整用量時，應另外保留並統計 `null` 筆數。

## Privacy limits and compliance

Telemetry 不得記錄或持久化 prompt、system prompt、completion/output 的完整文字、API key、原始 error message，或任何 PII。`llm_usage.jsonl` 僅可包含上一節 field catalog 列出的欄位與其允許的值；不得新增未列出的內容、原始請求／回應、header、credential 或錯誤文字。`error_category` 只能記錄 catalog 規定的分類或未分類例外的 Python exception class name，不得記錄 exception message。

LiteLLM 僅在記憶體中使用 `token_counter` 計算 token；token 計數器本身不會寫入磁碟。持久化資料僅限本機 `ai_dir` 下的 usage log；本功能不提供內建的 retention、rotation 或 encryption。部署與操作人員必須自行透過檔案權限、磁碟保護、保留期限與刪除政策管理本機檔案。

`summary_markdown` 可能呈現 JSONL 以外的 aggregate token counts；因此即使 JSONL 不含完整文字或其他受禁止內容，summary 的彙總 token 數仍可能對外顯示，使用時應套用相同的隱私與存取控制要求。

## Installation and enablement

安裝專案既有依賴（包含 `litellm`）即可啟用 token 計算：

```bash
python3 -m pip install -r requirements.txt
```

在 `config.json` 頂層設定 `"telemetry_enabled": true`，確認 `ai_dir` 與 `llm_usage_log` 指向預期位置，然後重新載入設定或重跑 orchestrator。執行一次實際的 LLM 呼叫（或既有的 telemetry 測試）後，檢查 `.ai-company/llm_usage.jsonl`（或組合後的自訂路徑）是否以追加方式產生一行有效 JSON object。確認該行只有 field catalog 中的欄位，且不含 prompt、system prompt、completion/output 完整文字、API key 或 error message。

若停用功能，將 `telemetry_enabled` 設為 `false` 並重新載入或重跑；後續 LLM 呼叫不應新增 usage record。若 usage log 寫入失敗，寫入路徑的處理會執行 `except OSError: return`：吞掉 `OSError`、不發出告警，也不影響主要 LLM 呼叫流程。驗證時應將「沒有新增 JSONL」與「寫入失敗被靜默處理」區分開來。
