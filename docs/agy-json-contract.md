# AGY 結構化回覆契約

需求整理等階段已提供 `response_schema` 時，Orchestrator 會透過路由與 usage wrapper 傳給 AGY 的原生 `--json-schema`。提示同時要求只使用該次提供的內容，不檢查環境、執行命令、建立 artifact 或要求互動確認。

此提示不是工具權限的強制隔離；AGY 仍保留原本的 sandbox 與 plan mode，沒有使用略過權限或自動核准選項。原生 schema 限制最終回覆形狀，不證明內容與需求一致；原有本機 validator 和明確檔案覆蓋檢查仍會執行。

AGY 的 stream-json 外層 result 與內層 response 分開處理：CLI 即使回報 `SUCCESS`，空白內層回覆仍是契約失敗。診斷會指出 empty final response，提醒檢查 AGY 日誌是否有被拒絕的工具確認或被中斷的 turn。這類呼叫在 usage 中記為失敗。

嚴格 JSON 階段遇到契約失敗會停止，保留人工檢查，不自動訂正或切換下一個付費路由。Developer 的 FILE／SECTION blocks 不傳 JSON schema，保持原輸出協定。指令日誌只顯示 `--json-schema <schema>`，不展開 schema 內容。

離線回歸入口：

```bash
python3 -m pytest -q -p no:cacheprovider test_agy_json_contract.py
```

測試驗證傳遞路徑、原生旗標、空白回覆、失敗記錄、停止回退及 file-block 相容性；沒有呼叫真實 AGY 模型。真實 CLI 是否依 schema 正確產生回覆仍需下一輪實測，不能以離線替身結果宣稱已驗證。
