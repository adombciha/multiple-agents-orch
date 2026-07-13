# Codex Skills

這個目錄保存可放進 Codex 使用的 skill 設定。

## 匯入與安裝方式

Codex 會從 `~/.codex/skills/` 讀取個人 skills。要使用本 repo 內的 skill，請將對應目錄複製過去：

```bash
cp -R codex/git-commit ~/.codex/skills/
cp -R codex/test-proposal-generator ~/.codex/skills/
cp -R codex/test-generator ~/.codex/skills/
cp -R codex/cpp-class-diagram ~/.codex/skills/
cp -R codex/code-flowchart ~/.codex/skills/
```

目錄結構需保留 `SKILL.md`，以及 skill 內引用到的輔助檔案。例如 `test-generator` 需要保留 `scripts/extract_markdown_test_spec.py`，並搭配 repo 共用的 `scripts/generate_test_report.py` 產生固定格式報告。

### 套用全域指令 (AGENTS.md)

[`../AGENTS.md`](../AGENTS.md) 是一份團隊共用的「AI 行為準則」，用來限制助手（例如要求使用繁體中文、禁止亂動 Git 等）。

最簡單的套用方式，就是將此檔案直接複製到您的 Codex 個人設定資料夾中：

```bash
cp AGENTS.md ~/.codex/
```

這樣 Codex 就會在對話時自動讀取並套用這些全域行為規範。

## git-commit

用途：檢查目前 repo 的 staged changes，或在沒有 staged changes 時檢查指定 stash，並產生英文 commit message。

使用方式：

```text
git-commit
```

也可以指定要分析 stash：

```text
git-commit 幫我看 stash@{0} 並產生 commit message
```

這個 skill 的限制：

- 不會執行 `git commit`、`git push`、`git stash apply`、`git reset` 等會改變 repo 狀態的指令。
- 預設優先分析 staged changes。
- 輸出會包含 recommended commit message 與簡短原因。

## test-proposal-generator

用途：從明確定義的測試範圍產生 QA 測試提案、測試場景、輸入需求、golden reference 需求、比對規則、報告需求與 release gate criteria。

使用方式：

```text
使用 test-proposal-generator 針對 qPCR Ct analysis pipeline 產生自動化測試提案
```

這個 skill 會：

- 先檢查測試範圍是否足夠明確。
- 如果測試目標、納入 / 排除範圍、成功標準或 golden reference 不清楚，會先要求 RD clarification。
- 在範圍足夠時產生 automation-ready test proposal，供後續測試程式產生流程使用。
- 不會直接產生測試程式；若需要產生測試程式，應先用它產生提案，再交給 `test-generator` 或其他自動化測試實作流程。

## test-generator

用途：讀取 Markdown 測試計畫、驗收條件、validation protocol 或 expected-output 文件，從內容產生測試程式與 traceable report。

使用方式：

```text
使用 test-generator 讀取 docs/test-plan.md，產生自動化測試與報告
```

這個 skill 會：

- 用 Python helper 解析 Markdown 結構。
- 抽出 headings、tables、code blocks、commands、expected/pass/fail/tolerance 等關鍵資訊。
- 依照 repo 既有測試框架產生測試；若沒有明確框架，會產生 standalone Python harness。
- 要求測試結果寫入 `test-results.json`，再用 `python3 scripts/generate_test_report.py --spec <output-dir>/markdown-test-spec.json --results <output-dir>/test-results.json --out-dir <output-dir>` 產生固定格式報告。
- 固定輸出 `test-report.json`、`test-report.md`、`rd-action-report.json`、`rd-action-report.md`；修正後可直接重跑測試與 report generator 產生第二次報告。

## cpp-class-diagram

用途：掃描 C++ / Qt repository 的 header，產生 class inheritance Markdown 文件，並可選擇輸出 Graphviz DOT、PNG 或 SVG 圖。

使用方式：

```text
使用 cpp-class-diagram 產生這個 C++ repo 的 class diagram
```

也可以指定輸出格式或檔名：

```text
使用 cpp-class-diagram 掃描 control/ 和 include/，輸出 CODEBASE_ANALYSIS.generated.md 與 CODEBASE_ANALYSIS.class_diagram.png
```

這個 skill 會：

- 檢查 repo 內的 C++ header 與既有文件位置。
- 產生 class inheritance 文件與 Graphviz diagram。
- 預設隱藏常見 Qt framework base class，讓 diagram 保持可讀。
- 在需要時保留 DOT / SVG / PNG 等輸出供檢查或嵌入 Markdown。

注意：若要輸出 PNG 或 SVG，系統需要安裝 Graphviz `dot`。沒有 Graphviz 時仍可先輸出 DOT。

## code-flowchart

用途：依指定檔案、資料夾、feature 或整個 codebase，產生簡潔漂亮且適合 review 的 Mermaid 流程圖。

使用方式：

```text
使用 code-flowchart 針對 scripts/generate_test_report.py 產生 Mermaid 流程圖
```

也可以指定較大的範圍：

```text
使用 code-flowchart 針對 test-generator feature 產生高層流程圖
```

這個 skill 會：

- 先界定 scope，必要時用 `rg` 找 feature entrypoints。
- 只根據可讀到的程式碼畫圖，不猜外部流程。
- 預設輸出 Mermaid `flowchart TD` Markdown。
- 控制圖面複雜度，優先保持 6-12 個高層節點，必要時建議拆成子圖。

## architecture-container-diagram

Read the current project architecture from `graphify-out/graph.json` when present, or scan the codebase directly. Generate a C4 Model Level 2 Container diagram as Mermaid code in the project root `architecture.mmd`, then render it locally with:

```bash
mmdc -i architecture.mmd -o architecture.png --theme=neutral
```

Hard rules:

- Do not only print Mermaid code in chat; write `architecture.mmd` and render `architecture.png`.
- Use `graph TB` for top-to-bottom hierarchy.
- Use `subgraph` blocks to group related containers, such as MCP Server, market scan, pullback scan, MLP prediction, data storage, and cache areas.
- Do not leave more than four connected nodes exposed outside subgraphs.
- Use `-->` for tight calls inside a subgraph, and `--->` or `---->` for long cross-subgraph calls.
- Add invisible alignment links such as `A ~~~ B` at the bottom for parallel modules that should stay side by side.
- Untangle high-degree God Nodes such as `handle_request()` or `StockBar` by routing connections through grouped containers instead of letting many lines cross the whole diagram.

## 更新 skill

如果要從目前 Codex 個人設定同步回這個 repo，可以複製來源 skill 目錄到 `codex/`：

```bash
cp -R ~/.codex/skills/git-commit codex/
cp -R ~/.codex/skills/test-proposal-generator codex/
cp -R ~/.codex/skills/test-generator codex/
cp -R ~/.codex/skills/cpp-class-diagram codex/
cp -R ~/.codex/skills/code-flowchart codex/
```

同步後建議檢查差異：

```bash
git diff -- codex/
```
