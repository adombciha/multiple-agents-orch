# Auto-Weighting Proposal — 員工考績與加權投票機制

## 設計哲學:角色扮演的延伸

本系統模擬一間 AI 軟體公司:PM(Manager)、Architect、分級 RD、分級 QA、各類專家顧問。
本提案把「模擬」再往前推一步——**員工有考績,主管做績效評估**:

- 每個 worker level(`rd_senior`、`qa_junior`⋯)有一個**信任權重 W**,相當於員工的考績分數。
- 表現好(任務一次過)加分;表現差(retry、被 reviewer 打回、QA 誤報)扣分。
- **Manager 在專案收尾時做績效評估**,審核本次 run 的表現並調整權重——
  senior/junior 的任用不再由人手動排,而是由 Manager 依考績決定。
- QA 驗收從「一票否決」改為**加權投票**:資深 QA 的話語權高於菜鳥 QA,
  就像真實公司裡 code review 的意見權重。
- Manager 每天開工前先**點名(roster)**:哪些角色今天可用、哪些「請假」
  (backend quota 爆掉、Ollama 連不上),排班時只排在場的人。
- **正職 vs 外包**:Ollama 上的角色是自家員工(免費,算內部考績);
  claude / codex / gemini(agy) / grok 是**外包廠商**(按 token 計費,
  不算內部考績,另有**廠商評鑑**)。專案進度有困難時,Manager 可決定
  花錢請外包救火,依廠商評鑑挑選。

## 現況問題

1. **回饋迴路斷裂**:`llm_usage.jsonl` 記錄了成功率/耗時/token,但沒有任何
   決策讀它。QA fail、reviewer reject、contract violation 跑完就丟。
2. **QA 一票否決**(`qa.py`):`all(passed(...))` — junior QA 一句 FAILED
   就整案打回,與 senior QA 權重相同。
3. **升級是硬編碼單向階梯**(`backends.py escalate_developer_backend`):
   junior→middle→senior 只升不降,且只在本次 run 有效,不會記住。
4. **Manager 排班時不知道誰在場**:planning prompt 只注入靜態
   `staffing_limits` 與 config 中的 backends 表;quota 已爆的 backend、
   離線的 Ollama 都要等到實際呼叫失敗才知道。

## 核心設計

### 0. 正職員工 vs 外包廠商

以 backend 區分身分,決定適用哪套評量:

| 身分 | Backend | 成本 | 評量制度 |
|---|---|---|---|
| 正職員工 | `ollama` | 免費(自家算力) | 內部考績(信任權重 W) |
| 外包廠商 | `claude` / `codex` / `agy` / `grok` | 按 token 計費 | 廠商評鑑(vendor scorecard) |

判定方式:呼叫實際落地的 backend(route 表或 fallback 的結果)決定該次
表現記入哪一套——同一個 `developer_senior` 職位,今天由 ollama 頂上就記
考績,由 codex 接單就記廠商評鑑。**內部考績的計分事件只在 backend 為
ollama 時觸發**,外包做壞了扣的是廠商評鑑,不是該 level 的考績。

### 1. 信任權重(考績分數,僅正職)

每個 `(role, level)` 一個權重 `W ∈ [0.2, 3.0]`,存於 `config.json`
(它是 `clear_run_artifacts` 唯一保留的檔案,天然具備跨 run 記憶):

```json
"worker_weights": {
  "rd":  {"senior": 1.0, "middle": 1.0, "junior": 1.0},
  "qa":  {"senior": 1.5, "middle": 1.0, "junior": 0.6}
},
"qa_vote_threshold": 0.6
```

更新公式(EWMA,α = 0.3):

```
W ← clamp(0.2, 3.0, (1−α)·W + α·(W + score·0.1))
```

### 2. 計分事件

全部掛在現有程式碼已經發生的時刻,每處只插一行 `record_event(...)`:

| 事件 | 掛鉤位置 | 分數 |
|---|---|---|
| 任務一次通過(`revisions == 0`) | `developer.py` task 標記 completed | +1.0 |
| 任務 retry / contract 失敗 | `developer.py retry_task_or_pause` | −1.0 |
| 需 promotion 才完成 | `backends.py escalate_developer_backend`(記原 level) | −1.5 |
| QA 整體 FAIL | `qa.py`(歸因本次 run 的 rd levels) | −0.5 |
| QA 投 FAILED 但人審判定誤報 | `main.py review pass` | 該 QA level −0.5 |
| Reviewer REJECTED | `reviewer.py`(涉事 rd level) | −0.5 |

### 3. QA 加權投票

`qa.py` 的 `is_passed` 從一票否決改為:

```python
votes = sum(weight[level] * (1 if passed(r) else -1) for _, level, r in qa_reports)
total = sum(weight[level] for _, level, _ in qa_reports)
is_passed = code == 0 and qa_reports and (votes / total) >= config["qa_vote_threshold"]
```

- junior QA(0.6)誤報無法單獨擋案;senior QA(1.5)的 FAILED 幾乎必擋。
- **測試失敗(`code != 0`)永遠 FAIL,不受投票影響** — 安全底線不可協商。

### 4. Manager 績效評估(自動調權)

`manager.py step_completed` 收尾時新增一個 strict-JSON 呼叫
(沿用現有 `call_manager` + schema 模式):

- **輸入**:本次 run 計分事件摘要 + 目前權重 + `summary_markdown()` telemetry。
- **輸出 schema**:

```json
{"adjustments": [{"role": "rd", "level": "junior", "weight": 0.9, "reason": "..."}]}
```

- orchestrator 對輸出做**確定性 clamp**(單次調幅 ≤ ±0.3、範圍 [0.2, 3.0])
  後寫回 `config.json`。LLM 只能建議,護欄由程式碼保證。

### 4b. 外包評量制度(Vendor Scorecard)

每個外包 `backend/model` 一張評鑑卡,存於 `config.json`:

```json
"vendor_scores": {
  "codex/gpt-5.6-luna": {"rating": 1.2, "jobs": 14, "failures": 1, "avg_tokens": 8200},
  "grok/grok-4.5":      {"rating": 1.0, "jobs": 22, "failures": 3, "avg_tokens": 5100}
}
```

- **資料來源**:`llm_usage.jsonl` 已逐筆記錄 backend、model、success、
  error_category、token 數——評鑑只是彙整,不需新增記錄點。
- **rating 更新**:與考績同一套 EWMA/clamp 函式(共用 `scoring.py`),
  事件為:交付成功 +1.0、contract violation −1.0、quota/timeout −0.5、
  被 QA/Reviewer 打回 −1.0。
- **rating 的用途**:
  1. 同一角色的 `role_model_routes` 依 rating 重新排序——評鑑好的廠商優先接單。
  2. `backend_escalation_path` 的外包段依 rating 排序。
  3. 注入 Manager prompt,作為「要不要花這個錢」的依據。

### 4c. 專案進度困難時的外包決策

外包不是預設選項(要收錢),而是**救火機制**。觸發條件(任一):

- 某 task `revisions` 達到 `max_revisions − 1`(再失敗就要人審了)。
- QA 加權投票連續兩輪未過。
- 該 level 的正職權重已低於 0.5(考績太差,不宜再派)。
- 正職 backend(ollama)離線或該角色全員請假。

觸發後由 Manager 以 strict-JSON 決策(附成本脈絡):

```json
{"hire_vendor": true, "vendor": "codex/gpt-5.6-luna", "reason": "..."}
```

- 輸入包含:vendor_scores、該廠商近期 avg_tokens(成本代理)、剩餘 revision 次數。
- orchestrator 護欄:`hire_vendor` 只在觸發條件成立時被接受;
  指名的 vendor 必須存在於該角色的 route 表且 backend 可用。
- 這是現有 `escalate_developer_backend` 階梯的取代/升級:從「盲目往上升」
  變成「Manager 看評鑑與成本後決定找哪家外包」。

### 5. Roster:Manager 開工點名

新增 `orchestrator/core/roster.py`,在 PLANNING 與 DEVELOPING_PLAN 前組出
「今日可用角色」快照並注入 Manager prompt:

```json
{
  "available_roles": {
    "developer_senior": {"backend": "codex", "type": "vendor", "status": "available", "rating": 1.2, "paid": true},
    "developer_junior": {"backend": "ollama", "type": "staff", "status": "offline", "weight": 0.7},
    "qa_senior":        {"backend": "ollama", "type": "staff", "status": "available", "weight": 1.5}
  },
  "unavailable_backends": ["agy"]
}
```

每個角色標示 `type`(staff/vendor)與對應的考績或評鑑分數;
外包角色標 `paid: true`,讓 Manager 排班時把成本納入考量
(能用正職就不用外包,除非觸發 4c 的救火條件)。

判定來源(全部已存在,只是組裝):

- `backends.backend_available()` — quota 已爆的 backend(`quota_exhausted_backends`)。
- `main.ollama_available()` — Ollama endpoint 是否連得上(搬到共用位置)。
- `role_model_routes` — 該角色所有 route 的 backend 都不可用 ⇒ 角色請假。
- `staffing_limits` — 上限為 0 的 level 不列入。

Planning prompt 附規則:「**只指派 status 為 available 的角色;
某 level 全員請假時,將其任務指派給最接近的可用 level。**」
另加確定性護欄:parse 完 tasks 後,若指派到不可用角色,orchestrator
直接改派最近的可用 level — 不信任 LLM 一定遵守。

### 6. Planning 端權重生效

- `developer.py` 的 `workload` dict(已注入 planning prompt)加入
  `worker_weights` 與 roster。
- Prompt 加規則:junior 權重 < 0.5 不派 moderate 以上;權重 > 1.5 可承接 moderate。
- 確定性護欄:`complexity == "complex"` 而指派 level 權重 < 0.5 時,自動升一級。

## 實作步驟(每步可獨立驗證)

1. **`orchestrator/core/scoring.py`**(約 100 行):`get_weight()`、
   `record_event()`(EWMA + clamp + 寫回 config.json + log),
   考績與廠商評鑑共用同一套更新函式。附 clamp/EWMA 測試。
2. **QA 加權投票**:改 `qa.py` 三行 + `config.py` 預設值。
   測試:junior FAILED + senior/middle PASSED ⇒ 通過;senior FAILED ⇒ 擋下。
3. **插入計分事件**:上表六個掛鉤點各一行,依實際落地 backend 分流至
   考績(ollama)或廠商評鑑(其餘)。
4. **Manager 收尾調權**:`manager.py step_completed` 加 schema 呼叫 + clamp 寫回,
   同時彙整本次 run 的 vendor_scores。
5. **Roster**:`roster.py` 組裝快照(含 staff/vendor 標記),注入 PLANNING /
   DEVELOPING_PLAN prompt,加不可用角色改派護欄。
   測試:偽造 `quota_exhausted_backends` 驗證改派。
6. **外包救火決策**:以 4c 的觸發條件 + Manager strict-JSON 決策取代
   `escalate_developer_backend` 的盲目階梯;route 表依 vendor rating 排序。
7. **CLI**:`python3 orchestrator.py weights`(顯示考績)、
   `weights set rd junior 0.8`(手動 override 逃生口)、
   `roster`(今日點名)、`vendors`(廠商評鑑表)。
8. **Brain 資料庫**:`orchestrator/core/brain.py`(SQLite + FTS5,約 80 行):
   `remember()` / `recall()`;掛鉤 `step_completed` 與失敗路徑寫入,
   ollama 呼叫前注入 top-3 記憶;`brain.db` 加入 `.gitignore`。
   測試:寫入後可檢索、外包 prompt 驗證不含 `[COMPANY_MEMORY]`。
9. **CLI 補齊**:`brain search <query>`、`brain prune`;
   (可選)stdio-only MCP server 包裝,供人工查閱。
10. **回歸**:`python3 -m pytest -q` 全綠後,以 docs-only request 實跑一輪,
   驗證權重、roster、廠商評鑑、brain 皆有生效。

預估總 diff 約 450 行,無新依賴(SQLite/FTS5 為 stdlib 內建)。

## 內部大腦資料庫(Brain MCP,僅限 sandbox)

### 定位

給正職員工(ollama 角色)一個**公司內部知識庫**:過往 run 的
meeting memory、任務成敗紀錄、常犯錯誤模式、修過的坑。
角色扮演語意:**這是商業機密,只給自家員工,外包廠商不得存取**——
外包(claude/codex/agy/grok)的 prompt 一律不注入 brain 內容。

### 架構(最小可行)

- **儲存**:單一 SQLite 檔 `brain.db`(stdlib `sqlite3`,零依賴),
  放 workspace 層級(跨 branch 共享記憶,順便解掉 R5 的一部分)。
  兩張表就夠:`memories(id, kind, task_kind, content, created_at)` 與
  FTS5 全文索引(SQLite 內建,免向量資料庫)。
- **寫入時機**(orchestrator 代寫,非模型自主):
  - `step_completed`:把 meeting_memory 摘要、各 task 的成敗與 level 寫入。
  - `retry_task_or_pause` / QA FAIL:把錯誤模式與修正方式寫入(`kind=lesson`)。
- **讀取時機**:ollama 角色被呼叫前,orchestrator 以 task description
  對 FTS5 做關鍵字檢索,取 top-3 摘要注入 prompt
  (`[COMPANY_MEMORY] ... [END_COMPANY_MEMORY]`,上限 ~1500 字元,
  避免 R8 的 prompt 膨脹)。

### 為什麼是「orchestrator 代查」而不是模型自己呼叫 MCP

ollama 角色走單純 HTTP `generate`,沒有 tool-calling 迴圈;
要讓模型自主呼叫 MCP 得先建 agent loop,成本高且小模型駕馭不了。
RAG 式注入用 20 行程式碼達到同樣效果。

### MCP server 介面(可選的第二步)

若之後想讓人類或其他工具查閱這顆大腦,再包一層 MCP server:

- **僅 stdio transport,不綁任何 TCP port** ——不對外是「結構上做不到對外」,
  不是靠防火牆規則。
- 只暴露 `search_memory` / `add_memory` 兩個 tool,路徑鎖死在 `brain.db`,
  不提供任意 SQL。
- 由本機 process 以 stdio 拉起,sandbox 邊界即 process 邊界。

### Sandbox 規則

1. `brain.db` 路徑寫死於 workspace 內,拒絕任何外部路徑。
2. 注入內容單向流動:brain → 正職 prompt;外包 prompt 永不注入。
3. 寫入只由 orchestrator 的固定掛鉤點執行,模型輸出不能直接寫庫
   (防 prompt injection 汙染長期記憶)。
4. 加入 `.gitignore`,記憶不進版控、不隨 push 外流。

## 預期問題與風險分析

實作前先盤點可能踩到的坑,每項附對策與接受的殘餘風險:

### R1. 歸因錯誤(最大的風險)

QA 的 `test_command` 是整包執行,失敗時無法精確指到是哪個 task、哪個
level 寫壞的——「QA 整體 FAIL 扣該 run 所有 rd levels」會冤枉無辜者。

- **對策**:只歸因到本輪 revision cycle 有改動檔案的 tasks 對應的 levels;
  扣分幅度刻意設小(−0.5)讓誤傷可被之後的正分洗回來。
- **殘餘風險**:粒度仍然粗。要精確就得 per-task 跑測試,成本不划算,先接受。

### R2. 樣本數太小、分數震盪

一次 run 只有個位數事件,EWMA 在小樣本上會跳動。

- **對策**:α = 0.3 偏保守、單事件影響幅度 0.1、clamp [0.2, 3.0];
  廠商評鑑加 `jobs` 欄位,**jobs < 5 時 rating 僅顯示不參與排序**(冷啟動保護)。

### R3. 考績死亡螺旋

權重掉太低 → 不被派任務 → 沒有機會加分 → 永遠翻不了身。

- **對策**:下限 0.2 保底;Manager 收尾調權時允許向 1.0 方向的回歸調整
  (均值回歸);routine 任務仍可派給低權重 junior 當「觀察期」。

### R4. 加權投票放走真 bug

junior QA 說對了但被權重蓋掉,壞 code 過關。

- **對策**:`code != 0` 硬性 FAIL 不可投票推翻;senior FAILED 在預設權重下
  必定擋案;落敗的反對票(dissent)完整附進 QA report,交給 REVIEWING_CODE
  的 Reviewer 二次把關——投票只是門檻,不是終審。

### R5. 跨 branch 不共享記憶

`config.json` 位於 `.ai-company-<workspace>-<branch>/`,權重與評鑑
**每個 branch 各自一份**,換 branch 等於考績歸零。

- **對策**:第一版接受(branch 通常對應同一批模型,冷啟動成本低);
  若成為痛點,再把 scoring 檔案搬到 workspace 層級並在
  `clear_run_artifacts` 中比照 config 保留。

### R6. Manager LLM 幻覺調權

Manager 可能給出離譜的權重或不存在的角色/廠商。

- **對策**:LLM 輸出一律過確定性護欄——單次調幅 clamp ±0.3、range clamp、
  角色與 vendor 必須存在於 route 表、`hire_vendor` 只在觸發條件成立時接受。
  護欄由程式碼保證,prompt 只是建議通道。

### R7. 換模型後歷史失效

`role_models` 換了模型,舊考績/評鑑描述的是舊模型的表現。

- **對策**:廠商評鑑以 `backend/model` 為 key,天然隔離;
  正職考績在該 level 的 ollama 模型變更時重置為 1.0(在 scoring.py
  記錄 `model_at_last_update` 一併比對即可)。

### R8. Prompt 膨脹拖累小模型

roster + 權重 + 廠商評鑑全塞進 planning prompt,對 grok 沒問題,
但 fallback 到 gemma 這類小模型時可能干擾 JSON 輸出。

- **對策**:注入的快照用壓縮 JSON、只含必要欄位(status/type/分數三項);
  護欄本來就不信任 LLM 遵守,超載時最壞情況是退回護欄改派,不會壞資料。

### R9. 大腦記憶過期或被汙染

brain.db 的舊經驗可能已不適用(程式碼改了),或錯誤結論被寫入後
長期誤導後續 run。

- **對策**:每筆記憶帶 `created_at`,檢索時偏好近期記錄並限量 top-3;
  寫入只走 orchestrator 固定掛鉤(模型輸出不得直接寫庫);
  提供 `brain prune` CLI 清除過期記憶。
- **殘餘風險**:記憶品質取決於 meeting memory 摘要品質,先接受。

### R10. 成本訊號只是代理

`avg_tokens` 不等於真實金額,各廠商單價不同。

- **對策**:第一版就用 token 當成本代理(同廠商內比較夠用);
  之後真要精算再加一張單價表乘上去,一行的事。

## 優先順序與刻意不做的事

**優先序**:步驟 1–2(加權投票,立即解決 junior QA 誤殺)→ 5(roster,
Manager 排班不再撞牆)→ 3–4(自動學習迴路)→ 6(外包救火)→
8(brain)→ 7、9–10 收尾。

**刻意不做**(YAGNI,等有數據再說):

- per-model 細粒度考績 — level 粒度夠用(廠商評鑑已是 per-model)。
- 獨立權重資料庫 — `config.json` 就是現成跨 run 儲存。
- 多 Manager 共識投票 — 單一 Manager + 程式碼 clamp 已足夠安全。
- roster 的即時健康檢查輪詢 — Ollama 可達性在 `init`/`start` 時檢查一次並存入
  `state["ollama_reachable"]`,PLANNING 只讀取快取值,保持該步驟純函式、可測試。
- 向量資料庫 / embedding 檢索 — FTS5 關鍵字檢索先上,證明不夠再換。
- ollama 自主 tool-calling 迴圈 — RAG 注入達到同樣效果,成本低一個數量級。
