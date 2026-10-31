# AGY 結構化回覆契約

需求整理等階段已提供 `response_schema` 時，Orchestrator 會透過路由與 usage wrapper 傳給 AGY 的原生 `--json-schema`。提示同時要求只使用該次提供的內容，不檢查環境、執行命令、建立 artifact 或要求互動確認。AGY 使用內部 `finish` 工具提交結構化結果；提示明確允許此工具，禁止其他工具，不再使用禁止所有工具的衝突指令。

此提示不是工具權限的強制隔離；AGY 仍保留原本的 sandbox 與 plan mode，沒有使用略過權限或自動核准選項。原生 schema 限制最終回覆形狀，不證明內容與需求一致；原有本機 validator 和明確檔案覆蓋檢查仍會執行。

AGY 的 stream-json 外層 result 與內層 response 分開處理：CLI 即使回報 `SUCCESS`，空白內層回覆仍是契約失敗。診斷會指出 empty final response，提醒檢查 AGY 日誌是否有被拒絕的工具確認或被中斷的 turn。這類呼叫在 usage 中記為失敗。

JSON 階段優先使用 result 的 `structured_output`（物件或 JSON 字串），再交由原有角色 validator 檢查。此欄位存在但內容無效時，不改用 response 掩蓋錯誤。沒有此欄位時，文字 response 必須完整由 JSON 構成：單份 JSON 可直接使用；若 CLI 合併了 planner 與 finish 的相同 JSON，允許鍵順序與空白不同的相同內容。不同 JSON、前後夾雜說明與 Markdown fences 均拒絕，不任意挑選其中一段。

解析或路由 validator 拒絕 AGY JSON 時，錯誤包含 `.ai-company-*/diagnostics/agy-contract-*.json` 的診斷路徑。解析失敗保存原始 stdout，路由驗證失敗保存送入 validator 的回覆。檔案權限為 `0600`，可能包含模型輸出及任務內容，請勿公開分享；寫入失敗仍會回報原契約錯誤，不會繼續執行。

嚴格 JSON 階段遇到契約失敗會停止，保留人工檢查，不自動訂正或切換下一個付費路由。Developer 的 FILE／SECTION blocks 不傳 JSON schema，保持原輸出協定。指令日誌只顯示 `--json-schema <schema>`，不展開 schema 內容。

離線回歸入口：

```bash
python3 -m pytest -q -p no:cacheprovider test_agy_json_contract.py
```

測試驗證傳遞路徑、原生旗標、finish 提示、結構化欄位優先、相同 JSON 去重、衝突及雜訊拒絕、診斷權限、空白回覆、失敗記錄、停止回退及 file-block 相容性；沒有呼叫真實 AGY 模型。真實 CLI 是否依 schema 正確產生回覆仍需下一輪實測，不能以離線替身結果宣稱已驗證。
