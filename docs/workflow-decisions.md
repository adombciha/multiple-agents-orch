# 共用流程決策與驗收對照

本次簡化涵蓋五項：盤點狀態與操作、集中下一步決策、讓 GUI 呈現共用決策、統一操作驗證，以及補齊舊狀態與完整流程測試。現有階段名稱和儲存格式沿用；沒有另建第二套狀態機。

## 決策來源與執行入口

- `core/status.py::workflow_decision` 產生標題、說明、可用操作和 owner 操作的目的階段。正式 GUI 直接呈現這份結果；CLI、人工報告和保留的舊 GUI 由 `next_actions` 轉為命令。
- `owner_review_kind` 讀取新紀錄的 `human_review_kind`，並核對來源與轉移欄位。只有缺少此欄位的舊紀錄使用既有報告前綴判讀；讀取不會改寫任務。未知或矛盾的類型會停止繼續操作。
- 新的 Architect、QA、Reviewer 決策由呼叫端明確指定類型，`pause_for_human_review` 驗證後原子保存。一般例外與沒有有效審核結果的暫停不會自行變成核准。
- `main.py::apply_continue` 是 GUI 與 CLI 的共同繼續入口。`apply_approve`、`apply_review_decision` 在變更狀態、清除暫停或記錄核准前，先呼叫 `owner_action_target` 驗證操作及目的階段。
- Git 交付仍由 `validate_delivery_preview` 驗證準備狀態、已展示的快照、內容、索引、來源與目標提交。這是 GUI 和 CLI 共用的交付驗證；顯示按鈕不代表取得交付權限。

## 狀態／證據 → 操作 → 結果 → 測試

| 狀態與證據                                                                                                  | GUI／CLI 操作                                                                                                     | 結果與限制                                                                                                 | 主要測試                                                                                                                            |
| ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `PLANNING`、`DEVELOPING_PLAN`、`REVIEWING_PLAN`、`IMPLEMENTING`、`TESTING`、`REVIEWING_CODE`、`RESEARCHING` | 繼續執行／`run`、`continue`                                                                                       | 執行至下個決策點；執行中只能提出安全暫停，不能重複啟動                                                     | `test_workflow_e2e.py`、`test_gui.py`                                                                                               |
| `WAITING_FOR_OWNER`，Architect 明確核准計畫且原需求要求 owner 核准                                          | 核准實作／`approve --run`、`continue`                                                                             | 進入 `IMPLEMENTING`，保存綁定需求與計畫內容的核准紀錄；不建立修訂任務                                      | `test_new_pause_requires_explicit_architect_verdict_and_survives_restart`、`test_start_pause_restart_test_review_and_real_delivery` |
| 計畫或程式審核否決                                                                                          | 提交修訂／`review revise --run`、`continue`                                                                       | 計畫修訂回 `DEVELOPING_PLAN`；程式修訂回 `IMPLEMENTING`；沿用既有有界重試與 owner 明確重試預算             | `test_decision_matches_gui_cli_and_legacy_state`、`test_reliability.py`                                                             |
| 有有效否決結果的 owner 覆核                                                                                 | 接受目前結果並繼續／`review pass --run`                                                                           | 沿已保存的通過路徑；不代表確認 Git 交付                                                                    | `test_qa_evidence.py`、`test_reviewer_evidence.py`                                                                                  |
| QA 回覆格式無效，或測試環境待處理                                                                           | 重試 QA 驗證／`continue`、`approve --run`                                                                         | 只能重回 `TESTING`；`review pass`、`review revise` 不可把無效回覆當通過或程式缺陷                          | `test_test_retry_cannot_be_overridden_as_a_pass_or_code_repair`、`test_qa_json_contract.py`                                         |
| Requirements 遺漏交付檔案                                                                                   | 重新產生需求／`continue`                                                                                          | 重回 `PLANNING`，保留原始需求                                                                              | `test_reliability.py`                                                                                                               |
| Reviewer 提出需要 owner 決定的流程疑義                                                                      | 展示接受範圍 → 接受現有變更並重新審查／`review preview-existing` → `review accept-existing --snapshot <ID> --run` | 接受紀錄綁定已展示內容；變動後必須重新展示。一般重試只重新審查，不記錄接受；禁止將流程疑義轉成 RD 修復任務 | `test_existing_changes_acceptance.py`、`test_process_blocker_cannot_generate_a_code_repair_task`                                    |
| Developer、Orchestrator 或 owner 的一般暫停                                                                 | 繼續目前任務／`continue`                                                                                          | 依已保存階段恢復；清除失敗路由仍須明確選擇。操作失敗不清除暫停請求                                         | `test_reliability.py`、`test_gui_revalidates_a_stale_approval_before_clearing_pause`                                                |
| `WAITING_FOR_DELIVERY`，背景仍在準備報告與 CHANGELOG                                                        | 顯示準備中；交付操作停用                                                                                          | 工作完成前不能確認；背景已停止時可要求準備並展示                                                           | `test_delivery_running_and_confirmation_are_distinct`、`test_gui.py`                                                                |
| `WAITING_FOR_DELIVERY`，成果已備妥                                                                          | 展示 Git 影響／`deliver`                                                                                          | 展示持久化快照，仍不合併                                                                                   | `test_gui.py`、`test_delivery_repair.py`                                                                                            |
| 準備完成且已展示有效快照                                                                                    | 確認二次交付／`deliver --confirm <ID>`                                                                            | 鎖內重新驗證後才 commit／merge；失敗保留 worktree；變更須重新展示確認                                      | `test_start_pause_restart_test_review_and_real_delivery`、`test_delivery_repair.py`                                                 |
| 不在交付階段或交付成果未備妥                                                                                | 不提供交付確認                                                                                                    | 共用後端也拒絕確認，不讀取 Git 或寫入確認旗標                                                              | `test_delivery_confirmation_checks_stage_before_git_or_state_mutation`                                                              |
| `COMPLETED`                                                                                                 | 查看報告、提交新任務或明確重置                                                                                    | 研究模式直接完成；未交付的舊 worktree 仍須恢復交付流程                                                     | `test_workflow_e2e.py`、`test_gui.py`、`test_orchestrator.py`                                                                       |
| `FAILED`                                                                                                    | 重新規劃／`reject`、明確重置／`reset`                                                                             | 保留需求重新規劃，或清除本次任務；均需要原有鎖與清理保護                                                   | `test_next_actions_cover_workflow_decision_states`                                                                                  |
| 新紀錄的決策類型與來源／轉移不一致                                                                          | 只可拒絕或明確重置                                                                                                | 不授予實作核准、不清除暫停、不執行既有錯誤轉移                                                             | `test_inconsistent_saved_approval_fails_closed`                                                                                     |

`approve` 對一般審核來源保留既有 CLI 覆核語意；它不會新增「接受現有變更」證據。`continue` 在交付確認階段被拒絕，`run` 也不會代替二次確認。當資料更新或重啟後，後端會在鎖內重新讀取狀態，舊按鈕不能繞過驗證。

## 驗證方式與範圍

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider --tb=short
```

測試使用離線模型回覆、暫存 Git 儲存庫及本機 HTTP/socket。完整流程包含 owner 核准、暫停、重啟、實作、測試、審核、交付展示、確認後真正合併，以及合併失敗保留成果。它不證明真實模型回覆品質、遠端服務可用性或視覺外觀；未以模型回覆作為核准或測試證據。

儲存相容性：既有階段、`resume_state` 與 `pass_state` 保持可讀；新紀錄增加經驗證的決策類型。原子儲存、跨程序鎖、交付快照和失敗成果保留仍由原有共用實作負責。
