# Chairman GUI 應用程序手冊

## 起動方式

要啟動本機 Chairman GUI，請按照以下步驟操作：

1. 確保已安裝專案所需的依賴。
2. 在專案根目錄打開終端或命令提示符。
3. 執行以下命令：

   ```bash
   python3 orchestrator.py gui
   ```

預設會在本機連接埠 `8765` 提供 GUI；也可以使用 `--port` 指定 `1` 至 `65535` 之間的有效連接埠。

## 單任務限制

Chairman GUI 在任何時刻只能執行一個任務。如果用戶嘗試啟動多個任務，系統將會提示錯誤。

## 暫停與恢復

執行中的任務可在 GUI 中按「停止」，或執行 `python3 orchestrator.py stop`（`pause` 亦可作為別名）。系統會以持久化請求要求工作流程在下一個安全步驟邊界暫停，不會直接將執行中的狀態改為 `FAILED`。

任務仍在執行時，GUI 會顯示「目前步驟完成後暫停」；停止請求不會中斷正在執行的模型或測試。

安全暫停後，狀態會保存為 `WAITING_FOR_OWNER`，重啟後仍會保留暫停請求。必須由用戶明確核准或提交人工審查決定後才會恢復，例如：

```bash
python3 orchestrator.py approve --run
python3 orchestrator.py review pass --run
```

恢復操作必須先取得與 CLI 共用的跨程序執行鎖，再於鎖內讀取最新狀態、驗證仍可恢復，並清除暫停請求。若 CLI 已在執行，GUI 不得提前清除暫停請求；操作被拒絕時，原請求必須保留。

不要直接編輯 `.ai-company/state.json` 來模擬狀態轉換；請使用上述 CLI 或 GUI 操作。

## CLI 交付命令

除了通過 GUI 操作，您也可以使用 Orchestrator CLI 建立交付預覽，檢視 Git 影響後再以人工二次確認交付：

```bash
python3 orchestrator.py deliver
python3 orchestrator.py deliver --confirm <SNAPSHOT_ID>
```

`deliver` 會展示目前 Git 影響並輸出快照 ID；檢視後再執行 `deliver --confirm <SNAPSHOT_ID>` 才會重新驗證並交付。交付預覽只能在 Reviewer 通過後建立，且最終報告、CHANGELOG 與其他交付成果會先準備完成。

持久化快照包含來源與目標分支及提交、來源 worktree 的 Git status、索引，以及追蹤／未追蹤檔案內容。重新確認時，任一已綁定資料改變都必須重新展示預覽後才能確認。

## Git 影響確認

Chairman GUI 不會自動確認、提交或合併 Git 變更。請先點擊「展示 Git 影響」檢視持久化預覽，再點擊「確認二次交付」完成人工確認；若工作樹、索引、分支或預覽內容改變，系統會要求重新建立預覽並確認。

符號連結在預覽中只能記錄連結本身及其 Git 模式，不得追隨連結讀取工作樹外的檔案，也不得把外部目標內容納入交付快照。任何超出來源 worktree 邊界的連結目標都必須被拒絕或排除。

## 任務流程

1. 開始一個新任務時，用戶需要通過 GUI 或 CLI 提交任務。
2. 系統確認任務後，開始執行任務。
3. 用戶可以通過 GUI 或 CLI 查看持久化任務狀態。
4. 如果任務需要暫停，用戶可以點擊 GUI 中的「停止」按鈕，系統會在安全邊界保存 `WAITING_FOR_OWNER`。
5. 用戶必須明確核准或提交審查決定，任務才會恢復執行。
6. Reviewer 通過後，系統先完成最終報告、CHANGELOG 及其他交付成果，不得把這些準備延後到確認階段。
7. 完整成果準備完成後，系統建立並持久化包含完整 Git 快照的影響預覽。
8. 用戶必須人工二次確認仍與快照一致的預覽，系統才會執行實際 commit／merge；預覽失效時必須重新展示並檢視。
9. GUI 確認交付後必須執行等待交付步驟，實際呼叫 Manager 的交付流程完成 commit／merge 並進入 `COMPLETED`；只執行通用 `run_to_end()` 不代表交付已完成。

## 二次交付確認

任務完成不代表 Git 變更已交付。系統會先準備完整成果，再展示 Git 影響；只有用戶完成二次確認、所有快照驗證通過且 commit／merge 成功後才會標記 `COMPLETED`。

確認時不得刪除或忽略 `git_status` 比對。若來源 worktree 分支、目標分支、追蹤檔模式、索引、工作樹、Git status 或任何交付檔案內容在確認前改變，原預覽會失效，必須重新展示並確認。

如果交付或合併失敗，系統會保留工作樹與既有修復流程；重試時不得重複寫入 CHANGELOG。既有 `COMPLETED` 但仍有未交付工作樹的執行，必須回到交付確認流程，不能直接再次標記完成。GUI 恢復任務時，必須先取得與 CLI 共用的跨程序執行鎖，再於鎖內讀取最新狀態、驗證可恢復並清除暫停請求；取得鎖失敗時不得清除原請求。

交付測試必須驗證實際 commit／merge 與最終 `COMPLETED`，不能只驗證 mock 被呼叫；也必須保留暫停請求、來源／目標變動、符號連結邊界及 staged 變更的回歸案例。HTTP 測試應透過正式 CLI web 入口啟動，並覆蓋真實 GUI 最終交付與完整封存啟動。所有測試通過前不得核准交付。

## 研究模式

研究流程使用 CLI 的 `start --research` 啟動：`python3 orchestrator.py start --research "研究任務描述"`。

研究模式完成後直接進入 `COMPLETED`，不需要 Git 交付預覽、人工確認、commit 或 merge。
