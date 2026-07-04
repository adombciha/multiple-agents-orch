# Multi-Agent 流程協調器 (Orchestrator)

本專案是一個用 Python 撰寫的輕量級 Multi-Agent 流程協調器。它能夠協調整合本機 Ollama 模型（Manager 與 Reviewer）、Codex CLI（Developer）與 Claude Code，以固定狀態機（State Machine）的方式自動執行需求規劃、程式實作、單元測試和代碼審查的閉環開發流程。

---

## 系統架構

```text
               你輸入需求
                   ↓
         [ Python Orchestrator ]
                   ↓
         [ Ollama (gemma4) ]
         角色：Manager (負責分析需求、拆解任務、產生 Final Report)
                   ↓
  ┌────────────────┬────────────────┐
  │   Codex CLI    │  Claude Code   │ (或使用 Ollama 作為 Fallback Reviewer)
  │   角色：Developer│  角色：Reviewer │
  └────────────────┴────────────────┘
                   ↓
         [ Orchestrator 執行測試 ] (e.g. npm test / pytest / git diff)
                   ↓
         [ Reviewer 進行代碼審核 ]
          ├── 通過 → 結束並產生 Final Report
          └── 退回 → 重新修改實作 (最多兩輪)
```

---

## 檔案目錄結構

本工具執行後，會自動在當前目錄下建立 `.ai-company/` 資料夾，並包含以下檔案：

```text
.ai-company/
├── config.json             # 系統設定檔 (API URL, 模型名稱, 測試指令, 代理人後端)
├── state.json              # 狀態記錄檔 (記錄當前執行狀態與任務清單)
├── request.md              # 你的原始自然語言需求
├── requirements.md         # Manager 產生的詳細功能需求說明書
├── implementation_plan.md  # Developer 產生的步驟化實作計畫
├── action_items.json       # 經 Manager 拆解後的結構化 JSON 任務清單
├── developer_output.md     # 實作過程中 Developer 的日誌與輸出
├── reviewer_output.md      # Reviewer 針對計畫與程式碼的審查意見
├── test_results.txt        # 測試指令執行的輸出結果
└── final_report.md         # 專案完成後的總結報告
```

---

## 如何避免 WSL 記憶體不足與卡死（非常重要 ⚠️）

您的 WSL 虛擬機器目前配置的實體記憶體為 **7.7 GB**。因為 `gemma4:latest` 的大小約為 **9.6 GB**，如果直接在 WSL 內部跑 Ollama 載入此模型，會導致 WSL 的記憶體嚴重不足、瘋狂進行交換（Swapping）並使系統完全卡死。

### 建議的解決方案：使用 Windows Host 的 Ollama
1. **在 Windows 主機下載並啟動 Ollama**（Windows 主機可以使用 GPU 顯存和更大的系統記憶體）。
2. 在 WSL 中，使用 `ip route show | grep default` 查詢 Windows 主機的 IP（在初始化時，協調器會自動幫你計算出建議的 Windows Host IP，例如 `172.17.144.1`）。
3. 修改 `.ai-company/config.json`，將 `ollama_url` 指向 Windows 的 IP：
   ```json
   {
     "ollama_url": "http://172.17.144.1:11434",
     "ollama_model": "gemma4:latest",
     ...
   }
   ```
4. 這樣一來，WSL 內的 Python Orchestrator 就會透過內部網路向 Windows 的 Ollama 發送請求，既能享用本機運算又不會佔用 WSL 寶貴的 7.7GB 記憶體！

---

## 快速上手指令

### 1. 初始化環境
在當前 Git 專案目錄下執行：
```bash
python3 orchestrator.py init
```
這會建立 `.ai-company/` 資料夾並產生預設設定檔。

### 2. 啟動新任務
輸入你的自然語言需求，啟動開發流程：
```bash
python3 orchestrator.py start "加入聯絡人搜尋功能，並在 search.py 寫好對應測試"
```
這會將狀態重設為 `PLANNING`，並將需求寫入 `.ai-company/request.md`。

### 3. 單步執行（推薦用於除錯或逐部審查）
每次執行下一個狀態轉移：
```bash
python3 orchestrator.py step
```
這會執行當前狀態（例如 `PLANNING` -> `DEVELOPING_PLAN`），並在完成後暫停，方便你查看中間產生的文件（例如 `requirements.md`）。

### 4. 全自動執行到結束
自動在背景跑完所有流程（遇到 Review 退回會自動進行最多 2 輪修改，直到完成或需要人工介入）：
```bash
python3 orchestrator.py run
```

### 5. 檢視當前狀態
顯示目前狀態、設定值、修改輪數以及各項任務的完成進度：
```bash
python3 orchestrator.py status
```

### 6. 重設狀態
若想重新執行某一階段（例如重新產生實作計畫）：
```bash
python3 orchestrator.py reset --state DEVELOPING_PLAN
```

### 7. 更換代理人（Agent）後端
您可以隨時更換個別角色的執行後端（支援 `ollama`、`codex`、`claude`、`gemini`）：
* **將實作者改為 Codex CLI (預設值)**:
  ```bash
  python3 orchestrator.py set-backend developer codex
  ```
* **將審查者 (Reviewer) 改為 Gemini Pro**:
  ```bash
  python3 orchestrator.py set-backend reviewer gemini
  ```
* **未來加入 Claude Code 後，將審查者改為 Claude**:
  ```bash
  python3 orchestrator.py set-backend reviewer claude
  ```

---

## 整合使用 Gemini Pro API

協調器已原生支援 Gemini API，您可以直接將任何一個角色（例如 `reviewer`）切換到 Gemini：

1. **設定 API 金鑰**：
   有兩種設定方式（擇一即可）：
   * **方式 A（推薦）**：在環境變數中設定：
     ```bash
     export GEMINI_API_KEY="您的_GEMINI_API_KEY"
     ```
   * **方式 B**：直接寫入設定檔，編輯 `.ai-company/config.json`，在最外層加入 API 金鑰：
     ```json
     {
       "ollama_url": "http://localhost:11434",
       "gemini_api_key": "您的_GEMINI_API_KEY",
       "gemini_model": "gemini-2.5-flash", // 可選，預設為 gemini-2.5-flash
       ...
     }
     ```

2. **切換角色後端**：
   ```bash
   python3 orchestrator.py set-backend reviewer gemini
   ```

---

## 後端代理人容錯機制 (Graceful Fallback)

為了確保在某個後端 API 或 CLI 無法運作時流程不中斷：
* 如果 `claude`、`codex` 或 `gemini` 尚未登入或設定 API Key 報錯，系統會自動降級（Fallback）使用本機的 **Ollama (gemma4)** 進行對應動作。
* 當您日後配置完成後，協調器會自動恢復使用您指定的進階 API/CLI。

