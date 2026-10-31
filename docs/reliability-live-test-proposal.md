# Automated Test Proposal

日期：2026-10-31。狀態：待實測；下列案例未宣稱已通過真實模型驗證。

## 1. Scope Summary

在獨立臨時 Git 專案內，使用本次修改後的 Orchestrator 與實際安裝、登入的 Codex／AGY／Ollama，驗證小型程式任務、GUI 操作、暫停恢復、QA、審核及交付。由 owner 啟動並操作。本提案不替 owner 啟動真實 workflow。

## 2. QA Interpretation

主要風險是無效模型回覆造成假修復循環、背景操作被誤報失敗、人工修訂漏掉理由，以及測試通過卻沒有真的交付。離線替身已用於程式回歸；本提案補真實模型、實際帳號與瀏覽器操作證據。

## 3. Test Objective

證明一次小任務能在有限額度內完成並交付，異常時能看懂原因且保留成果。未達停止條件前不重置任務、不反覆清除失敗路由。這不是模型品質排行或產品發布認證。

## 4. Included Scope

CLI 初始化、GUI 提交、模型實作格式、實際 pytest、QA 回覆、Reviewer、Git 影響展示與確認、步驟邊界暫停、GUI 重新啟動及人工修訂。

## 5. Excluded Scope

原 repository 的交付、remote push、敏感資料、平台上架、壓力測試、跨使用者服務、強制中斷模型。AGY／Codex／Ollama 不必全部在同一輪成功；分別記錄實際採用與回退的路由。

## 6. System Under Test

`orchestrator/main.py`、`orchestrator/gui.py`、`orchestrator/roles/{developer,qa,architect,reviewer,manager}.py` 及現有 Git 交付核心。版本以測試時的 Git commit 加未提交差異記錄為準。

## 7. Code Logic Boundary

依 `docs/reliability-and-skill-readiness-plan.md` 作 spec-based 驗收。觀察工作樹內容、pytest 原始輸出、持久狀態、模型路由、commit 與分支內容。不能只用代理的自然語言宣稱判斷成功。

## 8. UI Behavior Boundary

使用正式 `gui` 入口。只顯示當下可用操作，準備中不誤報鎖失敗；執行時可讀狀態，重複操作被阻擋。審核否決與格式無效須提供不同的下一步；交付須先展示快照再確認。

## 9. Testing Scenarios

| Scenario ID | Scenario                   | Quality Risk           | Expected Behavior                                   | Priority |
| ----------- | -------------------------- | ---------------------- | --------------------------------------------------- | -------- |
| LIVE-01     | 新增小函式與測試           | 基本流程不可用         | 有效修改、實際 pytest 通過、Reviewer 通過後等待交付 | P0       |
| LIVE-02     | 執行中查看及停止           | GUI 卡住、資料丟失     | 頁面可讀、停止於安全邊界、保留 worktree             | P0       |
| LIVE-03     | 重啟 GUI 後繼續            | 暫停遺失或狀態錯亂     | 暫停原因與任務保留，只繼續未完成步驟                | P0       |
| LIVE-04     | 展示後交付                 | 假完成或未確認即 merge | 預覽一致才交付，主分支包含成果                      | P0       |
| LIVE-05     | 預覽後變更內容             | 過期確認仍有效         | 原確認失效，重新展示與確認                          | P0       |
| LIVE-06     | 審核否決後修訂             | 遺漏理由、繞過否決     | 下一次規劃／實作取得具體理由                        | P1       |
| LIVE-07     | QA 無效回答（若自然出現）  | 假修復及 token 浪費    | 一次訂正後以 QA_RESPONSE 暫停，不建立假 FIX-QA      | P0       |
| LIVE-08     | 模型格式錯誤（若自然出現） | 重複花費               | 有限次修正／回退，耗盡後顯示明確暫停原因            | P1       |

## 10. Required Inputs

| Input Type   | Required Data                   | Source       | Notes                |
| ------------ | ------------------------------- | ------------ | -------------------- |
| Source       | 本次修改後的 harness 副本       | 目前工作目錄 | 不使用舊 worktree    |
| Request      | 下方 LIVE-01 prompt             | 本提案       | 固定兩個目標檔案     |
| Model access | 已安裝的 CLI／Ollama 與有效登入 | owner 本機   | 不將憑證放入測試副本 |

先在原 repo 執行下列命令，建立獨立副本；整段操作只在臨時專案建立 Git 紀錄：

```bash
smoke_lab=$(mktemp -d /tmp/multi-agents-live-XXXXXX)
smoke_python=/home/oss-gp/multi-agents/.venv/bin/python
cp -a orchestrator orchestrator.py requirements.txt AGENTS.md README*.md test_*.py verify_alignment.py "$smoke_lab/"
printf '.ai-company*/\nbrain.db*\n__pycache__/\n.pytest_cache/\n' > "$smoke_lab/.gitignore"
git -C "$smoke_lab" init -b master
git -C "$smoke_lab" config user.name 'Local Smoke Test'
git -C "$smoke_lab" config user.email 'smoke@example.invalid'
git -C "$smoke_lab" add .
git -C "$smoke_lab" commit -m 'Prepare isolated reliability smoke test'
cd "$smoke_lab"
"$smoke_python" orchestrator.py init
```

記下輸出的臨時路徑。`init` 若詢問 Windows Ollama endpoint，確認是目前實際服務的位址再接受。從此處起，所有 workflow 指令只在臨時副本內操作。

## 11. Required Configuration

| Config Type  | Required Fields                                                                                   | Source                          | Notes                                  |
| ------------ | ------------------------------------------------------------------------------------------------- | ------------------------------- | -------------------------------------- |
| Workflow     | `use_worktree=true`、`require_delivery_confirmation=true`                                         | 產生的 config.json              | 保留人工交付                           |
| QA command   | `/home/oss-gp/multi-agents/.venv/bin/python -m pytest -q -p no:cacheprovider test_live_answer.py` | owner 編輯臨時 config           | 只跑本輪新增測試                       |
| Limits       | `max_revisions=1`、`max_plan_escalations=1`                                                       | 臨時 config，鍵名以現有設定核對 | 暫停後先看證據，勿自動延長             |
| Model routes | 保留實際可用路由及動態探索                                                                        | 臨時 config／安裝環境           | 記錄版本，不因 Tev1 分數低就判定品質差 |

在副本啟動 GUI：

```bash
"$smoke_python" orchestrator.py gui --port 8766
```

瀏覽 `http://127.0.0.1:8766`。若該連接埠已使用，換一個未使用的本機連接埠。

## 12. Required Expected Outputs / Golden Reference

LIVE-01 功能參考是明確規格：輸入整數 20、22，結果必須是整數 42。測試必須另外涵蓋負數和零。Git 參考是測試前副本的 master commit；交付前檔案只存在 feature worktree，交付後由 `git show HEAD:live_answer.py` 讀取。

模型回覆措辭、延遲和 Tev1 機率不是 golden reference。本提案是工程驗收草案，不能據此宣稱正式產品發布就緒。

## 13. Test Case Matrix

| Case ID | Scenario ID | Purpose        | Input                          | Expected Result                               | Comparison Rule        | Priority |
| ------- | ----------- | -------------- | ------------------------------ | --------------------------------------------- | ---------------------- | -------- |
| L01     | LIVE-01     | 低成本完整任務 | 下方固定 prompt                | pytest exit 0，兩檔內容符合需求，等待人工交付 | 原始輸出＋檔案檢視     | P0       |
| L02     | LIVE-02     | 可讀與安全暫停 | 執行時按停止                   | 當前步驟結束後 WAITING_FOR_OWNER，檔案保留    | 狀態＋Git status       | P0       |
| L03     | LIVE-03     | 重啟恢復       | 安全暫停後關閉、重開 GUI       | 原任務仍在，繼續後僅完成剩餘工作              | 任務 ID／狀態          | P0       |
| L04     | LIVE-04     | 真實交付       | 展示 Git 影響，再二次確認      | COMPLETED、master 有兩個檔案與 CHANGELOG      | 實際 Git commit        | P0       |
| L05     | LIVE-05     | 快照失效       | 改動 worktree 檔案後使用舊確認 | 拒絕交付並要求重看，成果仍保留                | Git＋狀態              | P0       |
| L06     | LIVE-06     | 審核理由傳遞   | 依實際否決內容按修訂           | 下一份計畫處理具體理由                        | 原理由／新計畫比對     | P1       |
| L07     | LIVE-07     | 假 QA 缺陷阻擋 | 自然出現無效回覆               | 訂正最多一次，之後 QA_RESPONSE                | 原始 QA 回覆＋任務清單 | P0       |
| L08     | LIVE-08     | 有限重試       | 自然格式失敗                   | 達額度暫停，不無限續跑                        | 模型請求計數           | P1       |

在 GUI「提交任務」使用：

```text
只新增 live_answer.py 與 test_live_answer.py。
live_answer.py 提供 add(a: int, b: int) -> int，回傳 a + b，不要加入其他功能。
test_live_answer.py 使用 pytest，測試 add(20, 22) == 42、add(-2, 1) == -1、add(0, 0) == 0，並確認回傳值為 int。
只修改這兩個檔案；流程自行產生的 CHANGELOG 與內部報告依既有交付機制處理。
執行設定中的測試命令。報告實際結果，無有效證據不要宣稱通過。
```

先完成 L01、L04，再用另一份乾淨臨時副本跑 L02、L03。L05 在尚未交付的副本內執行；改動只加一行無功能影響的註解，記錄 diff，重新展示後才考慮確認。

## 14. Boundary / Abnormal Cases

| Case ID | Condition                | Expected Behavior      | Failure Signal       | Priority |
| ------- | ------------------------ | ---------------------- | -------------------- | -------- |
| B01     | 連續點擊操作             | 只有一個 worker        | 重複實作或重複計費   | P0       |
| B02     | 準備超過十秒             | 仍顯示準備中，狀態可讀 | 假報鎖失敗但背景繼續 | P0       |
| B03     | 瀏覽器重新載入／中斷連線 | 從持久狀態恢復畫面     | 失去任務、重複提交   | P0       |
| B04     | 模型服務逾時／不可用     | 明確有限回退或暫停     | 反覆重試沒有進展     | P1       |

## 15. Regression Risks Covered

| Risk                  | Test Coverage         | Detection Method           |
| --------------------- | --------------------- | -------------------------- |
| QA 胡言亂語導致假修復 | L07＋離線回歸         | QA_RESPONSE、不新增 FIX-QA |
| GUI 假逾時            | B02＋事件同步離線測試 | 準備狀態與 worker 數       |
| 未交付就完成          | L04、L05              | master 內容與交付狀態      |
| 修訂遺失              | L06＋離線回歸         | 審核理由傳入 Manager       |

## 16. Comparison and Tolerance Rules

功能值、pytest exit code、狀態名稱、任務數與 Git 內容須精確比對。模型輸出文字只檢查必要證據，不要求逐字相同。Tev1 的排序分數不作通過門檻。

每輪先以 15 分鐘作人工觀察上限；若超時先提出安全停止並記錄正在執行的模型，不直接判定演算法錯誤。付費額度上限由 owner 在啟動前決定；沒有可接受的額度就只做離線案例。

## 17. Report Requirements

逐例記錄：case ID、預期、實際、通過／失敗／未執行、時間、模型及 CLI 版本、請求次數、原始 test_results、QA／reviewer 回覆、狀態快照、Git diff／HEAD，以及停止原因。保留日誌時遮蔽帳號、token 和私有內容。

下游自動化報告使用 test-proposal-generator 隨附的 `scripts/generate_test_report.py`，產生 `test-report.json`、`test-report.md`、`rd-action-report.json`、`rd-action-report.md`，不要由模型手寫「已通過」報告。

## 18. Traceability Requirements

記錄副本路徑、base commit、原 repo commit 與未提交差異、prompt／config 雜湊、模型 ID、CLI／Python／pytest 版本、開始結束時間及人工確認內容。更換模型或修改預期後必須記錄原因。

## 19. Release Gate Criteria

L01、L04 及離線回歸須通過，沒有未處理的資料遺失或未確認交付問題。暫停恢復與快照失效需另輪驗證。L06～L08 若未自然觸發，標記未執行，不能拿離線結果冒充真實模型證據。任何一輪出現無限重試、範圍外修改或未確認合併就停止測試並保留現場。

## 20. Biological / Clinical Validation Planning

不適用：本範圍是開發工具的小型整數函式與 workflow，沒有生物、臨床、法規或硬體正確性宣稱。

## 21. RD Confirmation Points

| ID  | Topic | Question                         | Blocking                  | Current Assumption   | Affected Sections |
| --- | ----- | -------------------------------- | ------------------------- | -------------------- | ----------------- |
| C01 | 額度  | 本輪可接受多少付費額度？         | 真實呼叫前需要 owner 決定 | 先跑一輪，不自動延長 | 16                |
| C02 | 後端  | 本輪保留哪些已登入且可用的後端？ | 非阻擋提案                | 沿用現有可用路由     | 11                |

## 22. Risks and Assumptions

真實模型速度與格式遵循無法由離線替身保證。程式自動產生 CHANGELOG 與報告屬既有交付範圍；除此之外的檔案變更需要人工檢視。已刪除的原 worktree 不應用來恢復本輪測試；不要在原 repo 執行 reset／reject 作測試。

## 23. Downstream Generation Notes

先使用此手動提案取得真實路由與 GUI 證據。後續自動化沿用 pytest、臨時 Git repository 與本機 HTTP，不新增框架。報告依 `SPEC > CODE > UI behavior reasonableness` 判定，將網路／模型不可用與程式缺陷分開。

報告生成命令格式：`python3 /home/oss-gp/.codex/skills/test-proposal-generator/scripts/generate_test_report.py --spec <output-dir>/markdown-test-spec.json --results <output-dir>/test-results.json --out-dir <output-dir>`。這是使用者已安裝的外部 skill，不是 Orchestrator 的執行依賴。輸入由後續 test-generator 提取／測試執行產生；修復後重跑測試與報告腳本，不重新生成此提案來取代結果。
