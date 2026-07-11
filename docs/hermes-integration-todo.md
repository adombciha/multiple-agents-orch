# Hermes Integration TODO

目標：讓 Hermes 成為協調者的外部記憶與可重用 skill 成長層；既有 orchestrator 仍負責受控 SDLC、Git isolation、QA、Reviewer 與人工核准。

## Phase 1 — 無 Hermes 依賴的候選 lesson

- [ ] 在 workflow 完成或進入人工審核後，從 `meeting_memory.md`、`human_report.md`、QA 與 Reviewer 報告產生候選 lesson。
- [ ] 將候選 lesson 寫入人類可讀的 `playbooks/lessons-candidates.md`。
- [ ] 每個 lesson 記錄：問題、根因、適用條件、建議動作、來源 run、風險等級。
- [ ] 新增人工核准指令，將候選 lesson 升級到 versioned `playbooks/lessons.md`。
- [ ] 規劃前只讀取與當前 request 相關的已核准 lesson，避免把完整歷史塞進 prompt。

## Phase 2 — Hermes 記憶與 skill bridge

- [ ] 定義 Hermes 與 orchestrator 的最小交換資料：request、已核准 lesson、workflow outcome、人工決策。
- [ ] 由 Hermes 檢索跨 run 記憶，提供 PM 的「建議」而非直接執行指令。
- [ ] 將重複且有效的 lesson 包裝為 Hermes skill 草稿，仍須人工核准才啟用。
- [ ] 讓 Hermes 排程低風險工作，例如整理候選 lesson、提醒未處理 human review、彙整研究來源。

## 不可自動化的安全邊界

- [ ] Hermes 不可直接修改 FSM transition、Git merge/reset、worktree 清理規則或安全寫檔規則。
- [ ] Hermes 不可自動變更 `role_model_routes`、backend、模型額度或安裝 plugin。
- [ ] RA、Security、醫療或法規結論只能作為建議，必須有人類覆核。
- [ ] 自動產生的 lesson 不可直接進入正式 playbook 或下一次執行的強制規則。

## 先驗證的成功條件

- [ ] 已核准 lesson 能減少重複 workflow 失敗，而非增加 prompt 或 token 成本。
- [ ] 每項自動建議都可追溯到來源 run 與人工核准紀錄。
- [ ] 沒有任何 Hermes integration 能繞過既有 QA、Reviewer 或 human approval gate。
