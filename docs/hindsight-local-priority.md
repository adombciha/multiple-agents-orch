# Hindsight 本機背景工作與前景 QA 協調

此部署沿用已安裝的 Hindsight 1.2.0 映像與 `hindsight-data` volume，不下載映像、不建立新資料庫。部署工具只適用目前單一網路、已停止的 `hindsight` 容器，保留原容器作為回復點。

## 行為

- Stock API 以 `HINDSIGHT_API_WORKER_ENABLED=false` 啟動，查詢與接受非同步 retain 的 HTTP 服務保持可用。
- API ready 後啟動 `hindsight-worker`，與 API 使用同一容器、HOME 和 pg0 instance。安裝版的 MemoryEngine 會連接已啟動的 pg0，只有啟動資料庫的 API 擁有關閉責任。
- 前景 Orchestrator 使用 `ollama_resource_dir` 的共用檔案鎖通知背景 worker 排空；worker 收到一次 SIGTERM，先停止領取新任務，再等待最多 30 秒，取消其餘任務並將仍持有的操作退回 pending。
- 原生 worker 關閉上限為 120 秒；guard 等待至少 125 秒並持有資源鎖直到程序結束。前景預設等待 60 秒，超時會明確回報本機資源忙碌，不切換雲端。
- 前景完成後，guard 等待 30 秒安靜期才重新啟動背景 worker。所有 LLM provider 明確設定為本機 Ollama；worker slots 與各 LLM concurrency 都限制為 1。
- API 關閉前會先等待 guard 和 native worker 結束，讓 pending 狀態寫回仍在運作的資料庫。
- guard 異常死亡時，入口程序會先等待其 worker process group 125 秒，讓已開始排空的 worker 自行結束；仍存活才送出一次 SIGTERM，再等待原生關閉。一般異常回復最多約 250 秒；前景仍在 60 秒等待預算用盡時回報忙碌。若 worker 無法退出，入口程序繼續保留 API 與資料庫，不假裝已釋放資源。

## 部署

先檢查產物並執行唯讀預覽：

```bash
python3 scripts/deploy_hindsight_local.py
```

核准後，以可使用 Docker socket 的原使用者執行：

```bash
python3 scripts/deploy_hindsight_local.py --apply
```

工具會保留原 image ID、環境中的私密值、既有掛載、port binding、網路、使用者、資源限制與 restart policy；私密值不會輸出至終端。原容器先改名為 `hindsight-before-local-priority-<UTC timestamp>`，保持停止狀態，再建立替代容器。

共享鎖目錄預設是 `/home/oss-gp/.local/state/ollama-coordination`，須由主機使用者與容器使用者共同存取（此機器皆 UID 1000）。Orchestrator 設定必須將 `ollama_resource_dir` 指向相同的主機路徑，背景容器會將它掛載至 `/opt/hindsight-coordination`。API 與 guard 的原始碼僅掛載三個所需檔案，為唯讀掛載；不要在執行中編輯它們。

在 `~/.hindsight/coding-agent.json` 保留既有欄位並設定 `"autoInject": "pages"`，避免 prompt hook 自動呼叫同步 reflect。此客戶端設定須另外套用，部署工具不改動外部設定檔。`retainSessions` 保持啟用；背景暫停期間接受的非同步 retain 留在資料庫等待處理。

部署驗收需確認 API `/health`、既有 bank/page 讀取、worker `/health/live`、前景期間 worker 退出且新 retain 維持 pending，及前景結束後重新消化工作。請用一次輕量前景 QA 驗證延遲，避免以完整模型壓力測試干擾正常工作。

## 實際驗證（2026-11-08）

- 完整測試通過：1235 passed、46 subtests passed，耗時 89.77 秒。
- 已部署替代容器；原容器以 `hindsight-before-local-priority-20261108T145844Z` 保留，沿用相同映像與資料 volume。API `/health` 回傳 200。
- 第一次實機前景鎖驗證確實遇到資源競爭，等待 1.105 秒後取得鎖；驗證前、持鎖期間與結束後 API health 皆為 200。此驗證未送出模型請求。
- 背景 worker PID 535 優雅退出，return code 為 0；安靜期後 PID 719 自動啟動，確認背景工作恢復。
- 已執行原生 `decommission-worker hindsight-local --schema public --yes`，成功將舊 worker 遺留的 7 筆 processing claims 退回 pending，未刪除任務內容。
- 用戶端 `autoInject` 已由預設 reflect 改為 pages，其餘設定保留。這會避免自動 prompt synthesis；明確呼叫同步 reflect 仍會繞過此共用鎖。

第二次實機忙碌情境驗證通過：確認遇到資源競爭，等待 31.778 秒後取得鎖，總耗時 32.806 秒；驗證前、持鎖期間與結束後 API health 皆為 200，未送出模型請求。此工作未建立 Git commit、merge 或 push。

## 限制與回復

明確呼叫 Hindsight `reflect` 或同步 retain 的其他客戶端仍可直接從 API 發起推論，未經此共用鎖；它們可能與前景競爭。此方案協調自動背景 worker 和已接入鎖的 Orchestrator 本機請求，不能保證其他 Ollama 客戶端的全域優先序。

若需回復，先 `docker stop -t 360 hindsight`，將替代容器改名保留，再把原備份容器改回 `hindsight` 並啟動。原容器的 `unless-stopped` restart policy 在備份停止後不會自行啟動。兩個容器共用同一資料 volume，禁止同時啟動兩個 API。原生 worker 的 requeue 語意是可重試，工作可能重做；不保證每次推論恰好執行一次。
