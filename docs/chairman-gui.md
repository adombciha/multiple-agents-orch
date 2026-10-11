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

不要直接編輯 `.ai-company/state.json` 來模擬狀態轉換；請使用上述 CLI 或 GUI 操作。

## CLI 交付命令

除了通過 GUI 操作，您也可以使用 Orchestrator CLI 建立交付預覽，檢視 Git 影響後再以人工二次確認交付：

```bash
python3 orchestrator.py preview
python3 orchestrator.py confirm
```

`preview` 會保存當前交付預覽及其工作樹快照。`confirm` 只會在持久化預覽仍有效且已完成人工確認時執行交付；預覽或工作樹狀態改變時，必須重新檢視並確認。

## Git 影響確認

Chairman GUI 不會自動確認、提交或合併 Git 變更。請先點擊「展示 Git 影響」檢視持久化預覽，再點擊「確認二次交付」完成人工確認；若工作樹或預覽內容改變，系統會要求重新建立預覽並確認。

## 任務流程

1. 開始一個新任務時，用戶需要通過 GUI 或 CLI 提交任務。
2. 系統確認任務後，開始執行任務。
3. 用戶可以通過 GUI 或 CLI 查看持久化任務狀態。
4. 如果任務需要暫停，用戶可以點擊 GUI 中的「停止」按鈕，系統會在安全邊界保存 `WAITING_FOR_OWNER`。
5. 用戶必須明確核准或提交審查決定，任務才會恢復執行。
6. 任務完成但仍有未交付工作樹時，系統會進入交付確認流程。
7. 在 Git 操作前，系統先展示持久化的 Git 影響預覽。
8. 用戶必須人工二次確認有效的預覽，系統才會執行交付；預覽失效時必須重新檢視。

## 二次交付確認

任務完成不代表 Git 變更已交付。系統會先展示 Git 影響，只有用戶完成二次確認且交付成功後才會標記完成。

如果工作樹或 Git 影響在確認前改變，原預覽會失效，必須重新展示並確認。如果交付或合併失敗，系統會保留工作樹與既有修復流程；重試時不得重複寫入 CHANGELOG。既有 `COMPLETED` 但仍有未交付工作樹的執行，必須回到交付確認流程。

## 研究模式

研究流程使用 CLI 的 `start --research` 啟動：`python3 orchestrator.py start --research "研究任務描述"`。

研究模式完成後直接進入 `COMPLETED`，不需要 Git 交付預覽、人工確認、commit 或 merge。
