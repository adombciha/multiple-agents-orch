# AI Skills

這個 repository 用來整理不同 AI coding assistant 的 skill / instruction 設定。

## 目錄

- `codex/`: Codex skills 與使用說明。
- `claude/`: Claude 相關設定文件。
- `gemini/`: Gemini 相關設定文件。
- `workmux.md`: Workmux 相關設定與說明。

## 全域行為準則 (Agent Instructions)

為了讓 AI 助手能更安全、精準地協助開發，我們提供了行為規範 [`AGENTS.md`](AGENTS.md)。建議將其內容設定為 AI 助手（如 Codex、Gemini）的系統提示詞 (System Prompt) 或 Custom Instructions 內：

- **語言與溝通**：優先使用繁體中文回覆，但程式碼註解必須全為英文。
- **安全與權限**：禁止未經授權的程式碼修改或 Git 操作（包含 commit、push、修改歷史等）。
- **嚴謹流程**：在修改任何檔案前，必須先提出具體的執行計畫並說明影響範圍，等待使用者核准後才能執行。

## 支援的 Skills

目前提供以下技能，並分別在各 AI 助手目錄下提供對應的設定格式（例如 `codex/` 為目錄格式，`gemini/` 為單一 Markdown 格式）：

- **git-commit**: 讀取 staged diff 或 stash，產生英文 commit message。這個 skill 只檢查 Git 狀態，不會 commit、push、stash apply 或修改 repo。
- **test-proposal-generator**: 從明確的測試範圍、function name、白箱 scope、algorithm scope 或 regression concern 產生 QA 測試提案與 automation-ready 測試場景，不產生測試程式。function-name / 白箱模式會先做唯讀 code discovery，整理每個可見 function 的邏輯、演算法風險、side effects、error paths、testability 與 scope boundary。
- **test-generator**: 從 Markdown 測試計畫、驗收條件或 validation protocol 產生測試程式，並透過共用程式輸出固定格式 traceable report 與 RD action report。對 function-derived / white-box proposal，會保留 function-level traceability，並避免產生只呼叫 function 但沒有 assertion 的空測試。
- **cpp-class-diagram**: 從 C++ / Qt header 產生 class inheritance Markdown 文件，並可輸出 Graphviz PNG / SVG / DOT 圖。
- **code-flowchart**: 依指定檔案、資料夾、feature 或整個 codebase 產生簡潔漂亮的 Mermaid 流程圖。
- **code-review-expert**: 提供專家級的深度 Code Review，檢視程式碼正確性、安全性、效能與最佳實踐（SOLID, DRY 等），並使用繁體中文給予具體改善建議。

詳細的設定文件與指令說明，請分別參考：

- Codex: [`codex/codex.md`](codex/codex.md)
- Gemini: [`gemini/gemini.md`](gemini/gemini.md)
- Workmux: [`workmux.md`](workmux.md)

## 標準報告產生器

測試報告與 RD action report 應由程式產生，不由 AI agent 自由撰寫。共用入口是：

```bash
python3 scripts/generate_test_report.py --spec <output-dir>/markdown-test-spec.json --results <output-dir>/test-results.json --out-dir <output-dir>
```

固定輸出：

- `test-report.json`
- `test-report.md`
- `rd-action-report.json`
- `rd-action-report.md`

修正測試程式或 SUT 後，重新執行測試並更新 `test-results.json`，再重跑上述 command 即可產生第二次報告；不需要只為了報告重跑 `test-proposal-generator` 或整個 `test-generator` 流程。

## 自動化 QA 腳本 (Auto QA Scripts)

我們提供了針對不同系統的自動化隔離測試腳本，能在乾淨的工作區中啟動 AI Agent：

- **Windows**: `scripts/windows/wt-qa.ps1`
- **Linux**: `scripts/linux/workmux-qa.sh`

### 常用參數說明

- `FeatureName`: 開發或測試的功能名稱（用於建立隔離的 Git Worktree，例如 `login`）。
- `--agent`: AI Agent 的執行指令（例如 `agy`, `claude`）。
- `--auto-fix`: (選填) 若帶入此參數，AI 會在產生測試腳本後自動執行、Debug 並修復錯誤，直到全部通過。若不帶入則會暫停等候人工檢閱。
- `--scope`: (選填) 預設 AI 會讀取 `git diff` 來決定測試範圍。若給定此參數，您可以覆蓋預設行為，指派明確的需求或範圍。

### 使用範例 (以 Windows PowerShell 為例)

```powershell
# 將執行權限繞過，避免 PSSecurityException 錯誤
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 針對特定功能進行自動化 QA，並明確指定 Scope 讓 Agent 直接執行
.\scripts\windows\wt-qa.ps1 -FeatureName "CtAlgorithmTest" -Agent "agy" -Scope "`$test-proposal-generator 請比較 output.csv 和 Excel Ct value"
```

### Clean Up (Windows / Linux)

When you are done reviewing the AI's work and no longer need the test environment, you can safely remove the git worktree and clean up all generated files:

**Windows:**

```powershell
.\scripts\windows\clean-qa.ps1 -FeatureName "CtAlgorithmTest"
```

**Linux:**

```bash
./scripts/linux/clean-qa.sh "CtAlgorithmTest"
```

## 安裝到 Codex

Codex 的長期指示建議分成兩層：

- 全域個人偏好：放在 `~/.codex/AGENTS.md`，例如語言、權限、安全流程等跨 repository 的共同規則。
- Repository 規範：放在專案根目錄的 `AGENTS.md`，記錄此 repository 專用的 coding style、測試方式與工作流程。

開始新的 Codex session 時，若要確保全域規則已套用，可以先要求 Codex 讀取 `~/.codex/AGENTS.md`，並確認目前 repository 的 `AGENTS.md` 也已讀取。

把需要的 skill 目錄複製到 Codex skills 目錄：

```bash
cp -R codex/git-commit ~/.codex/skills/
cp -R codex/test-proposal-generator ~/.codex/skills/
cp -R codex/test-generator ~/.codex/skills/
cp -R codex/cpp-class-diagram ~/.codex/skills/
cp -R codex/code-flowchart ~/.codex/skills/
cp -R codex/code-review-expert ~/.codex/skills/
```

安裝後，在 Codex 對話中直接提到 skill 名稱即可觸發，例如：

```text
git-commit
```

```text
使用 test-proposal-generator 針對 qPCR Ct analysis pipeline 產生自動化測試提案
```

```text
使用 test-proposal-generator 針對 calcCt function 做白箱 function-by-function 邏輯與演算法測試 scope 評估
```

```text
使用 test-generator 讀取 docs/test-plan.md 並產生測試
```

```text
使用 cpp-class-diagram 產生這個 C++ repo 的 class diagram
```

```text
使用 code-flowchart 針對 scripts/generate_test_report.py 產生 Mermaid 流程圖
```

```text
使用 code-review-expert 針對剛寫好的 worker.cpp 進行深度的 Code Review
```

## 安裝到 Gemini

Gemini 的長期指示同樣建議分成兩層：

- **全域個人偏好**：放在 `~/.gemini/AGENTS.md`，負責定義語言、權限、安全流程等跨 repository 的共同規則。
- **Repository 規範**：放在專案根目錄的 `AGENTS.md`，記錄此 repository 專用的 coding style、測試方式與工作流程。

與 Codex 不同的是，Gemini 在啟動新的 session 時，會**自動**讀取上述兩個位置的 `AGENTS.md` 檔案，並將其內容轉為全域指令 (Global Rules) 與專案規則。因此只要將檔案放置到位，後續每次對話都不需再手動提醒讀取。

若要安裝技能，可將對應的 Markdown 檔案複製到 Gemini 的 skills 目錄中：

```bash
cp gemini/git-commit.md ~/.gemini/skills/
cp gemini/test-proposal-generator.md ~/.gemini/skills/
cp gemini/test-generator.md ~/.gemini/skills/
cp gemini/cpp-class-diagram.md ~/.gemini/skills/
cp gemini/code-flowchart.md ~/.gemini/skills/
cp gemini/code-review-expert.md ~/.gemini/skills/
```

安裝後，使用方式與 Codex 相同，直接在對話中呼叫技能名稱即可。

## 安裝到 Workmux (Linux)

Workmux 是一個基於 Rust 開發的工作流程管理工具。關於如何在 Workmux 搭配平行 AI agent 進行環境配置與使用這些 skills，請詳細參考根目錄的 [`workmux.md`](workmux.md)。
