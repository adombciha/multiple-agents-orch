# 需求與原始 Request 一致性修正

日期：2026-10-31。

本次先觀察到 `requirements.md` 描述 worktree 錯誤調查，而 Manager 回傳空 tasks。恢復前重新讀取時，磁碟的 `request.md` 本身也是 WAITING_FOR_OWNER／Git worktree setup failed 狀態訊息。因此不能據此認定模型自行污染需求；先前「原始 request 一直正確」的判斷不足，已更正。

修正內容：

- Manager prompt 明確標記原始 request 為需求來源；既有狀態、錯誤與報告只作診斷背景。
- 共用檢查從明確 Scope／目標檔案宣告擷取相對 Python／Markdown 檔名，排除禁止修改及 REFERENCE-ONLY 區塊。
- 需求漏掉這些檔名時不保存錯誤需求；既有需求偏移時不呼叫規劃模型。兩者都以 Requirements 暫停，繼續時重新執行 PLANNING。
- 規劃輸入同時保留經參考區塊過濾的原始 request 與衍生需求，避免只有衍生摘要可供比較。

限制：檔名出現不等於行為需求正確；未明確列出目標的請求或其他副檔名不在這項檢查的保證範圍。

本機完整離線回歸：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider --tb=short`，387 passed（9.38 秒）。真實模型是否依新提示正確產生需求，仍待 owner 下一輪實測。

此次任務恢復已在共用執行鎖內確認沒有實作 tasks、worktree 乾淨，再備份 request、state、requirements 與 human report 到 workflow 目錄的 `recovery/20261031T035903702490Z/`。從對話恢復約定的 live_answer.py／test_live_answer.py 任務，將錯誤需求和審核報告移出作用中的路徑，狀態改回 PLANNING。原 worktree 保留，沒有啟動模型呼叫。
