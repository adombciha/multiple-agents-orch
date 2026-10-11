# Tev1 動態 Ollama 模型路由測試提案

## 狀態與執行閘門

本文件是測試提案，尚未獲 owner 核准。自動化測試可在隔離環境以 mock 執行；任何 Windows Ollama smoke test 或外部模型呼叫，必須先取得 owner 明確核准。未核准前，不連線、不拉取模型，也不執行下方 smoke test。

## 影響範圍

- **預計涵蓋模組：** `orchestrator/core/backends.py` 的模型安裝清單探索與 Tev1 決策路由、`orchestrator/core/state.py` 的靜態路由鏈整合，以及 `test_orchestrator.py` 的自動化測試。
- **預期結果：** 路由器無法使用時，路由決策回傳「未選定」，呼叫端保留設定的靜態路由順序；有效且信心足夠的選擇只能優先排序已安裝的候選模型。Tev1 只能做決策路由，不可成為 `/api/chat` 任務執行模型。
- **風險：** 若部署版本的 `/v1/systemone` schema 與程式假設不同，決策路由會退回靜態順序；若回退鏈或已安裝模型清單的測試 fixture 設定錯誤，可能誤判安全回退。Smoke test 可能載入模型、使用本機資源或傳送外部資料，因此需核准與限制輸入。
- **本提案不包含：** 路由實作變更、模型安裝或刪除、設定修改，以及執行真實服務測試。

## 唯讀核對結果：目前可確認的契約

以下是從目前程式與既有測試核對出的實際行為；這些是程式契約，不代表已向 Ollama 服務驗證 endpoint 相容性。

### 路由器探索與候選條件

- 安裝模型清單由 `GET {ollama_url}/api/tags` 取得；程式讀取 JSON 的 `models` 陣列，取各項 `name` 欄位。成功結果快取 60 秒，請求 timeout 使用 `ollama_router_timeout`（預設 10 秒）。
- 預設路由器模型為 `tev1:4b`。路由器未在安裝清單中、沒有可用候選，或候選只有一個時，不呼叫 `/v1/systemone`，改回傳未選定或直接使用唯一候選。
- 路由器請求為 `POST {ollama_url}/v1/systemone`，timeout 使用 `ollama_router_timeout`（預設 10 秒）。Payload 形狀如下：

```json
{
  "model": "tev1:4b",
  "state": "Role: developer_middle. Task: Refactor parser module. Candidates: granite4.2:8b: enterprise software engineering, refactoring, and integration; qwen3.5:9b: fast capable coding, UI/UX, and scripting",
  "questions": {
    "selected_model": {
      "type": "choice",
      "instructions": "Select the best model for the role 'developer_middle'.",
      "criteria": {
        "granite4.2:8b": "enterprise software engineering, refactoring, and integration",
        "qwen3.5:9b": "fast capable coding, UI/UX, and scripting"
      }
    }
  }
}
```

Task 摘要最多 120 字元；不傳送完整任務 prompt。`choice.criteria` 必須是選項名稱對應說明文字的物件，符合 [Ollama System One API](https://github.com/ollama/ollama/blob/main/docs/openapi.yaml)；真實 Windows 服務相容性仍待核准後驗證。

### 回應與錯誤處理

程式預期 HTTP 200 回應 JSON 中包含：

```json
{
  "answers": {
    "selected_model": {
      "choice": "qwen3.5:9b",
      "confidence": 0.88
    }
  }
}
```

- 只有 `choice` 屬於已安裝候選，且 `confidence >= ollama_router_min_confidence`（預設 `0.5`）時，才接受選擇。
- 非 200（包含 HTTP 404）、JSON 解碼錯誤、回應欄位缺漏或型別不符等例外，皆應由路由器函式捕捉並回傳未選定。低 confidence 或候選外選擇亦回傳未選定。
- 既有執行紀錄顯示 `/v1/systemone` 曾回 HTTP 400 並退回靜態路由；當時 `choice.criteria` 為陣列，不符官方要求的物件形狀，是高度可信的原因。由於未保存錯誤 body，也未獲准進行 Windows smoke test，真實服務相容性仍未確認。
- 呼叫端收到未選定時應沿用原設定的靜態路由順序。整合層是否在所有路由器失敗情況均維持此順序，需由提案案例驗證。
- `/api/chat` 使用 Ollama chat 請求；Tev1 模型會在發送 chat 請求前被拒絕。既有測試驗證該限制。
- `/api/version` 回應 schema、`/api/tags` 的完整官方 schema，以及 `/api/chat` 的成功／錯誤回應細節，尚未由本次唯讀程式核對確認，列為待查證假設。

## 假設與阻塞問題

### 假設（自動化案例可用 mock 驗證）

1. 模型名稱比較依現行 `_matches` 規則處理 tag 與 `:latest` 別名；測試應以明確列出的安裝清單和候選名稱固定此條件。
2. 對 router timeout、連線失敗或 JSON 錯誤，預期路由函式回傳未選定，整合層使用靜態順序。
3. 錯誤回應可能是非 JSON 或 JSON 欄位結構錯誤；錯誤 body 是否有固定 schema 未確認，不對其內容形狀作硬性要求。

### 阻塞（真實服務驗證前必須處理）

1. 請 owner 確認測試機、Windows Ollama endpoint/base URL、允許的模型與資源上限。
2. 請 owner 核准 smoke test 及其範圍：`GET /api/version`、`GET /api/tags`、`POST /v1/systemone`、以及最小 `POST /api/chat`。未核准前不執行。
3. 需在獲核准後，以目標 Windows Ollama 版本確認 `/v1/systemone` 是否存在、request 欄位及 response/error schema。HTTP 404 是待測情境，不應視為服務端已確認的標準錯誤格式。
4. 確認最小 `/api/chat` 請求模型已預先安裝、輸入不含機密，並確認實際回應格式；不得為測試安裝模型。
5. 若要呼叫外部模型或使用非本機服務，需另行核准目的地、資料內容與費用上限。

## 自動化測試提案

### 共通前置條件與追蹤欄位

以下案例均為離線自動化測試提案，以 pytest、mock HTTP 回應和初始化 orchestrator fixture 執行；不得連線真實 Ollama，也不得呼叫外部模型。共通設定：

- Router enabled；router model `tev1:4b`；候選至少為 `granite4.2:8b`、`qwen3.5:9b`。
- `/api/tags` 以 mock 回傳 router 和候選均已安裝；整合測試使用明確靜態路由順序 `[granite4.2:8b, qwen3.5:9b]`。
- 每例清除安裝清單 cache，避免前一案例污染；用 mock call args 驗證 HTTP method、URL、timeout 與 JSON payload。
- 每例記錄追蹤欄位：`case_id`、`test_name`、`environment`（mock/offline）、`router_enabled`、`router_model`、`candidate_models`、`installed_models`、`trigger`、`http_status_or_exception`、`request_payload`、`response_payload_or_error`、`router_choice`、`confidence`、`expected_route_order`、`actual_route_order`、`fallback_used`、`assertion_result`、`owner_approval_id`（mock 案例填 N/A，真實測試須填核准識別）。
- 判定方式：pytest assertion 全數通過為 PASS；任一值或 route 順序不符為 FAIL；因環境／核准缺失而未執行標為 BLOCKED，不得以 SKIP 當作通過。

### 案例表

| ID | 前置條件 | 輸入 | 步驟 | 預期結果 | 判定方式 |
|---|---|---|---|---|---|
| AUTO-01 有效決策 | 共通條件；HTTP mock 已設定 | `/v1/systemone` 回 200，`answers.selected_model.choice=qwen3.5:9b`、`confidence=0.88` | 呼叫 `route_ollama_model_with_tev1`；另呼叫路由整合方法觀察實際模型呼叫順序 | router 回傳 `qwen3.5:9b`；整合路由只將該已安裝候選排到前面，其餘靜態路由相對順序保留 | pytest 斷言返回值、payload schema、首個 executor model 及後續順序 |
| AUTO-02 Router 不可用／連線錯誤 | 共通條件；`/api/tags` 成功 | router POST 拋出 connection refused | 執行 router 函式及整合路由 | router 回傳未選定；靜態順序不變 | 斷言未選定、靜態第一候選先被呼叫、沒有因 router 失敗丟出未捕捉例外 |
| AUTO-03 HTTP 404 | 共通條件；`/api/tags` 成功 | router POST 回 404 | 執行 router 函式及整合路由 | 回傳未選定並保留靜態順序；不假定錯誤 body schema | 斷言 404 被視為非 200，fallback 順序不變 |
| AUTO-04 Router timeout | 共通條件；`/api/tags` 成功 | router POST 拋出 `requests.Timeout` | 執行 router 函式及整合路由 | 回傳未選定並保留靜態順序；使用設定的 router timeout | 斷言捕捉 timeout、timeout 參數值與整合順序 |
| AUTO-05 無效 JSON | 共通條件；HTTP status 200 | `response.json()` 拋出 JSON decode error | 執行 router 函式及整合路由 | 回傳未選定並保留靜態順序 | 斷言例外被安全捕捉且靜態第一候選先執行 |
| AUTO-06 回應 schema 缺漏／錯型 | 共通條件；HTTP status 200 | 依序測試缺 `answers`、缺 `selected_model`、缺 `choice`、confidence 非數值 | 每個輸入獨立呼叫 router 函式及整合路由 | 每個畸形回應皆回傳未選定，靜態順序不變 | 逐個 payload assertion；驗證沒有例外逸出 |
| AUTO-07 低 confidence | 共通條件；HTTP status 200 | 合法候選 choice，confidence `0.35`（門檻預設 0.5） | 呼叫 router 函式及整合路由 | 回傳未選定，靜態順序不變 | 斷言 `confidence < threshold` 導致 fallback |
| AUTO-08 候選外選擇 | 共通條件；HTTP status 200 | `choice=unauthorized-model:latest`、confidence `0.99` | 呼叫 router 函式及整合路由 | 拒絕候選外模型並保留靜態順序 | 斷言結果不在候選 allowlist 且 fallback 順序不變 |
| AUTO-09 Tev1 未安裝 | `/api/tags` 成功但不含 `tev1:4b`；至少兩個候選已安裝 | router 候選模型缺席 | 呼叫 router 函式及整合路由 | 不呼叫 `/v1/systemone`，回傳未選定，沿靜態順序執行 | 斷言 router POST mock 未呼叫與 executor 順序 |
| AUTO-10 執行模型未安裝 | 安裝清單只含 `tev1:4b` 與靜態候選中的一個 | `/api/tags` 不含另一候選 | 呼叫 router 函式及整合路由 | router 不得選擇未安裝模型；只有一個可用候選時不需 POST，整合層不得呼叫未安裝候選，並按既有路由可用性處理 | 斷言 POST 未呼叫、實際模型呼叫僅來自已安裝／可用路由；若現有呼叫層並無安裝清單過濾，記錄為阻塞，不把缺失行為當既有保證 |
| AUTO-11 Tev1 不可作 executor | 使用 `model=tev1:4b` 呼叫 chat backend | 最小 task prompt | 呼叫 `call_ollama` | 發送 HTTP 前拋出限制錯誤，不呼叫 `/api/chat` | 斷言 RuntimeError 與 chat POST 未呼叫 |

### 離線 A/B 比較與人工標籤

先建立至少 12 筆固定的黃金樣本，涵蓋 `manager`、`architect`、`developer_senior`、`developer_middle`、`qa_senior`、`reviewer` 各至少兩種不同難度的任務。每筆記錄 `case_id`、角色、去識別化的 120 字元內 task 摘要、已安裝候選、靜態路由順序、人工標註的首選模型與標註理由。標註者只根據候選能力與任務需求決定首選，不看 Tev1 結果；若候選能力無法區分，標為「無唯一首選」，不計入首選命中率，但仍計入安全回退率。凍結樣本及標籤後才執行 A/B，避免事後改答案。

同一批樣本分別執行 A 組（停用 router、純靜態路由）與 B 組（啟用 Tev1、使用相同安裝清單與靜態路由）；以 mock 回應先驗證計算與追蹤欄位。真實 Tev1 決策及模型任務執行均需另獲 owner 核准。每組記錄最終首選、候選外選擇、fallback 原因、路由耗時、任務成功與否，以及模型呼叫次數／token 或可取得的成本代理指標。比較首選命中率、任務成功率、安全回退率、p50/p95 路由延遲與呼叫成本；每項均報分子、分母和 A/B 差值，不以少量樣本宣稱統計顯著。

離線 gate：AUTO-01～AUTO-11 全部通過；候選外或未安裝模型執行次數為 0；router 失敗時靜態順序保持率 100%；追蹤欄位無缺漏。真實 A/B 的採用 gate 由 owner 在執行前設定成功率、延遲與成本容忍值；若未設定或樣本不足，結果只作探索，不宣稱 B 優於 A。

## 經核准後的 Windows Ollama smoke test 提案

**前置核准：** owner 明確核准後，指定 Windows 主機、Ollama base URL／版本、允許模型、執行時段、timeout 與資源上限；`tev1:4b` 與至少一個 executor 候選必須已安裝。測試不安裝或刪除模型。需要外部模型或非本機連線時另行核准。

1. `GET /api/version`：記錄 status、版本欄位與原始 JSON；預期 200 且 JSON 可解析。版本欄位名稱與錯誤 body schema待服務版本核對。
2. `GET /api/tags`：記錄 status、`models[].name`、所需模型是否已安裝；預期 200、JSON 可解析，且路由器及測試候選已安裝。未安裝即停止，不拉取模型。
3. `POST /v1/systemone`：只送最小、無機密 task state 與已安裝候選；使用程式目前 payload 形狀。記錄 status、回應 JSON 與錯誤 body。預期 200 回應符合 `answers.selected_model.choice` 及 `confidence` 契約；404 或 schema 不符記為相容性失敗並停止路由 smoke。
4. 最小 `POST /api/chat`：只對預先安裝的非 Tev1 executor 候選送單輪、無機密提示；按程式目前 payload `{"model": "<installed executor>", "messages": [{"role": "user", "content": "Reply with OK"}], "stream": false, ...}` 執行。確認 status、response JSON 與 `message.content`；其餘選項以實際 `call_ollama` 組裝值為準。不得以 Tev1 呼叫 chat。

每步記錄時間、主機／Ollama 版本、URL path（不含憑證）、HTTP status、耗時、request 欄位（敏感文字遮罩）、response 欄位或錯誤、模型名稱、執行人、owner 核准識別與 PASS/FAIL/BLOCKED。未獲核准前所有 smoke 步驟均為 BLOCKED。

## 精簡實作與驗證計畫

1. Owner 審閱並核准本提案及指定的 smoke 範圍；先解決 Windows endpoint、模型預裝狀態與資料／資源限制阻塞。
2. 核准後只修改 `docs/testing/tev1-routing-test-plan.md`，依案例矩陣補成可執行測試規格；本任務不含路由實作修改。
3. 以既有 `test_orchestrator.py` fixture 和 mock 執行 AUTO 案例，凍結人工標籤並完成離線 A/B 計算，記錄 pytest 結果及追蹤欄位；不安裝模型。
4. 如需 Windows smoke，待 owner 核准後逐步執行四個 endpoint 範圍；任一前置條件或 schema 不符即停止並記錄阻塞，不改用未核准的外部模型。

## 調查限制

本次確認來源為工作樹程式、既有測試、README 說明、既有執行紀錄及 Ollama 官方 System One API 規格；沒有在本次調查中連線 Windows Ollama。因此部署版本的相容性、服務端錯誤 schema 與真實延遲均未確認，等待 owner 核准後再進行任何服務 smoke test。
