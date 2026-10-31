# 提交前自動格式化

此 repo 提供 `.githooks/pre-commit`，以 Pyink 格式化 Python，以 Prettier 格式化 Markdown、JSON、YAML、JavaScript／TypeScript、CSS、HTML。其他格式不處理。

每個 clone 需啟用一次（設定只作用於這個 repository）：

```bash
git config --local core.hooksPath .githooks
```

需要 Python 3.12 以上。Pyink 優先使用專案 `.venv/bin/pyink`，Prettier 優先使用 `node_modules/.bin/prettier`；找不到時使用 PATH 中的指令。缺少工具時提交會停止，不會自行下載工具。可用 `pipx install pyink==26.5.1`、`npm install --global prettier` 安裝；Node 版本切換後，全域 npm 工具可能需要重新安裝。

正常提交流程：

1. Stage 要提交的檔案，再執行 `git commit`。
2. Hook 只格式化新增、修改或重新命名且已 stage 的支援檔案。
3. 若產生格式變更，這次 commit 會停止。檢視 `git diff`，重新 stage 格式化後的檔案，再次 commit。
4. 若沒有格式變更，commit 繼續。

Hook 不會自行 `git add`。同一檔案若同時有 staged 與 unstaged 修改，會在執行 formatter 前停止，避免把未準備提交的內容一起處理。此時請手動格式化並重新選擇 stage 範圍。符號連結不會被 formatter 追隨；若 formatter 本身失敗，請先檢查工作區差異再重試。

Hook 使用當下工作區的 formatter 設定。提交期間請避免同時修改相關檔案。它只處理本機提交，不會自動格式化整個 repo，也不能替代測試或 CI；新 clone 不會自動啟用 hooks。

停用此 repo 的自訂 hook：

```bash
git config --local --unset core.hooksPath
```

參考：[Git hooks](https://git-scm.com/docs/githooks)、[Prettier CLI](https://prettier.io/docs/cli)、[Pyink](https://github.com/google/pyink)。
