# CatsWalker 執行事故：從卡片重試改為專案級監督

## 事故摘要

CatsWalker 的連續任務暴露出控制面的責任缺口：PM 完成拆卡後退出治理，project
runner 只依序等待 job，orchestrator 只處理單一 stage。只要 executor 假活、達到
max turns、driver 在 run 成功後遺失，整個專案就沒有任何元件負責判斷與接手。
心跳只能證明程序未退出，不能證明任務仍在前進。

實際觀察到的失敗包括：

- worktree 從主機當下 checkout 的舊 feature branch 建立，代理反覆在錯誤基線工作。
- Grok E2E 執行 28 分鐘後 `max turns reached`，job blocked，PM/runner 均未處理。
- Claude 程序仍有 heartbeat，但 progress 與檔案長時間不變，形成假活。
- run 已 succeeded，但 tests-pass gate 尚在執行；只看 job 時像卡住，貿然 retry 會重複 driver。
- 核准證據存在 worktree，但 Telegram 只真正傳部分圖片；PDF、文字、影片只有檔名。
- 執行教訓主要留在對話，沒有穩定寫回 AMOS，因此後續代理容易重犯。

## 核心決策

1. **PM 是專案治理角色，不只負責拆卡。** 由 deterministic supervisor 承擔 24/7
   監督，PM agent 在需要判斷時才作為接手者；不能用另一個無限 agent loop 取代控制面。
2. **heartbeat 與 progress 分離。** heartbeat 是 process liveness；progress_at 才是
   semantic liveness。預設 15 分鐘無語意進度才中斷，避免誤殺正常長測試。
3. **恢復必須有界。** 只自動恢復 engine/executor failure（max turns、no output、
   driver lost/orphaned），每 job 最多兩次；驗收失敗與 human-approve 絕不自動放行。
4. **成功 run 未必代表 stage 完成。** 只有 job 沒有 active driver、latest run terminal
   且缺 gate result 時才重建 driver；tests-pass 正在跑時禁止重複啟動。
5. **核准附件是交付物。** 引擎自動產生 `_review-manifest.md`；Telegram 將圖片送為
   photo、影片送為 video、PDF/HTML/Markdown/文字送為 document。
6. **處置即記憶。** supervisor 的 interrupt/retry 同時寫 audit 與 project-scoped
   AMOS memory；記憶失敗不得中斷任務，但必須留下 warning log。

## 不可退化的驗收條件

- human approval 不會被 supervisor retry 或 approve。
- gate 執行中的 driver 不會被判定遺失並重複啟動。
- max-turns failure 可自動換同 role 的另一 enabled agent。
- 自動恢復達上限後停止，保留證據給人處理。
- Telegram 可實際收到 image/video/document 三類附件。
- 新 worktree 使用 explicit `base_ref`，否則 main/master，不使用 ambient HEAD。
