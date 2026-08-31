# Bastet Agent OS — 操作手冊

從第一次登入到日常維運，照使用順序寫。安裝見
[INSTALLATION.md](INSTALLATION.md)；工作流引擎的完整規則（每種關卡、每種停下的
狀況）見 [WORKFLOWS.md](WORKFLOWS.md)；踩坑清單見 [CAUTIONS.md](CAUTIONS.md)。

## 1. 核心概念

| 名詞 | 意思 |
|---|---|
| **Team** | 組織頂層；記憶可以在這層共享 |
| **專案** | 與 Bastet 主機上的一個真實 git repo 一對一，也與一個 AMOS 專案一對一。有生命週期狀態與燈號 |
| **Agent** | executor + 帳號 + 模型設定。`claude-code`、`claude-sdk`、`codex`、`grok`、`agy`、`hermes`、`pi`、`openclaw`、`bastet-lite` |
| **角色** | agent 在專案裡「做什麼」（engineer、reviewer、pm、ops-engineer…）。階段指定角色，專案的角色指派決定實際由誰跑 |
| **工作流範本** | 有序的階段清單，每階段一種關卡 |
| **關卡** | 階段怎麼算過：`auto`（跑完即過）、`tests-pass`（引擎跑指令看 exit code）、`agent-review`（結構化裁決檔）、`human-approve`（等人） |
| **任務（Job）** | 一件工作走一條工作流；看板上的一張卡 |
| **Run** | 一張卡的一個階段由一個 agent 執行一次；帶用量與成本 |
| **資源** | agent 可以調用的東西：LLM 端點、MCP、API、SKILL、git、媒體生成 |
| **授權（Grant）** | 誰能用某資源（全域/TEAM/專案），含預算與併發上限 |
| **憑證** | 以指標儲存的機敏資料；資源指向它，永不複製 |

## 2. 第一次上手（七步）

1. 開 `http://<主機>:8890/ui`，貼上 `~/.bastet/api_token` 的內容。
2. 右上角選語言（每個瀏覽器記住自己的）。
3. **組織**：建 team → 建專案（repo 路徑是「Bastet 主機上」的路徑）→ 建 agent。
   executor 顯示 未安裝/未設定/ready；未設定就用登入精靈（瀏覽器裡的真終端）。
   每張 Agent 列都有「登入與模型設定」：會進入該 Agent 綁定帳號的實際 Hermes／Pi／
   OpenClaw 環境，可輸入 API Key 並設定模型。Agent ID 也可在編輯模式改名；若仍有
   live run，系統會拒絕改名，等該輪結束再做。
4. **模板**：挑一個內建範本（前後端開發、網頁開發、運維處理、持續維護…）直接
   指派，或複製後改。重活記得給 `timeout_s`。
5. **組織**：把 agent 指派到工作流需要的角色（這同時授予 AMOS 專案記憶的成員資格）。
6. **對話**：選專案範圍，把需求講清楚 —— 這裡就是規劃的正式輸入。
7. **專案**：確認 PM 拆出的任務清單，按 ▶。

## 3. 日常操作，按頁籤

### 看板

卡片即時移動。執行中的卡有**進度條**（第幾階段/共幾階段）和**心跳**。心跳分兩件
事，因為它們會不一致：**還活著**（行程沒退出，每 20 秒確認一次，就算 agent 一句
話都沒說）與**還在講話**（最後一行輸出是什麼、隔多久了）。🟢 正常；🟠 有兩種
——三分鐘沒有心跳（可能真的死了），或還活著但**沉默超過十分鐘**（通常是卡在某個
會發問、沒人回答的子行程上）。卡上的 🔧 是被自動返工的次數。

點卡開 drawer：規格、各階段 run（含成本）、關卡結果、diff、以及卡住時的處理區 ——
**重試**（可換 agent＝該階段一次性覆蓋、可勾範本刷新、可改規格）、**任務補給**
（把執行中才知道的資料交給任務：部署目標、專案 id、決策裁定 —— 憑證形狀的內容
會被拒絕，那要走管理→憑證）。人工核准在這裡按，**附預覽**（截圖、HTML、摘要）。

### 對話

選回應者（agent 或資源池 LLM）與範圍（專案/TEAM/全域）。專案範圍把真實狀態帶進
prompt。可以：

- 附檔（文字內嵌、圖片隨線路支援附上）
- **要求設定 Bastet**：「幫我接上某家的圖片 API」→ agent 讀內建 bastet-config
  指南 → 回覆尾端出提案卡 → **你按套用才生效**，稽核記你的名字。金鑰原文會被
  拒收，只吃 `secret:<id>` 指標
- **收到生成的媒體**：agent 用授權資源生成的圖/音/影經 outbox 附回對話，內嵌顯示
- 核准待審關卡、把整段討論派成任務 —— 按鈕永遠在人手上

### 專案

依狀態分組的可收合卡片（規劃中/待執行/執行中/維護中/已結案），搜尋依關鍵字或
時間。卡上：燈號、進度、▶ ⏸ ■、結案/重啟/刪除。展開看任務計畫（含出處與
「對話已更新」的過期警示）、角色覆蓋、資源、憑證視圖。

PM 拆解是唯讀提案，你編輯確認後 runner 才派工；全部收束進維護中等驗收。

專案可設定「每日成本上限（USD）」與 IANA 時區（例如 `Asia/Taipei`）。成本會合併
Gateway 精確 ledger 與直連 executor 回報值而不重複計算。達上限後，手動、排程與 runner
都不能再建立新 job；runner 保持存活，下一個當地午夜自動續行。已在執行的 job 不會被
中途殺掉，因此並行中的工作可能造成有界超額。專案卡、audit 與 Telegram 都會顯示暫停／
自動恢復；清空金額即停用此圍欄。

分支交付完成後，卡片會顯示變更檔案、diff stat 與有大小上限的 patch 預覽；此預覽
明確是和本機目標分支快照比較。按「合併並驗證目標」後，系統會重新抓取遠端目標、
拒絕衝突或競態、重跑 pre-deploy 閘道、原子推送並核對遠端 commit。失敗時卡片會
停在 blocked 並保留 worktree，連點也不會啟動第二個交付程序。

### 資源

分類分組（LLM/MCP/API/SKILL/git/媒體），每個資源自己的可見範圍。**測試按鈕**做
agent 會做的事（LLM 列模型不花 token、MCP 真握手、git 真驗證），三態結果：
`ok` / `warn`（可達但不如預期）/ `failed`，附實際請求。MCP/SKILL 的安裝指令由
**人**在這頁按執行，完整輸出回傳。

供應商若是非同步生成，可在媒體資源展開「非同步媒體擷取」：設定含
`{task_id}` 的 `async_status_path`、狀態／結果欄位、成功與失敗值、輪詢間隔、
次數、檔案上限及額外下載主機白名單。Agent 以 run token 登記 claim 後即可結束；
卡片會顯示等待，Bastet 在背景取回實體檔並自動重跑同一階段核驗。供應商憑證只送
狀態 API，不會轉送下載主機；redirect、越出 worktree 的路徑與超限檔案一律拒絕。
任務抽屜會留下 polling 次數、bytes、SHA-256 與 MIME 證據。

### 模板

範本庫（點開看流程圖）、我的範本（就地編輯會 +1 版）、角色定義 Prompt、
專案↔工作流對應（已結案專案不列）。階段欄位完整說明見
[WORKFLOWS.md](WORKFLOWS.md#2-stage-fields)。

### 記憶

搜尋 + 瀏覽（近期寫入、範圍過濾、統計）。頁面標示目前召回模式：🟢 語意
（turbovec）或 ⚪ 關鍵字。完整管理介面連去 AMOS console。

### 管理

- **使用者**：三級角色，token 複製/停用/撤銷/輪替/刪除，權限即時生效
- **憑證與機敏資料**：多行輸入（PEM 直接貼）、值寫入後不可讀回、可改名/範圍/輪替
- **系統設定**：顯示時區（儲存永遠 UTC）
- **通知頻道**：Telegram 配對、回應者與專案綁定
- **維護**：所有元件的版本檢查與更新（Bastet、AMOS、turbovec、SDK、pytest、
  Pillow、Playwright、七個 executor CLI），絕不自動更新

### 稽核

hash 串接、append-only。搜尋：關鍵字（含 detail 內容）、類別（來自實際資料）、
操作者、起訖時間。

## 4. 卡住了怎麼辦（速查）

| 看到 | 意思 | 動作 |
|---|---|---|
| ⏸ 等人工核准 | 設計中的停點 | 看預覽 → 核准/駁回（WebUI 或 Telegram） |
| ⏳ 額度用盡，會自己續跑 | 廠商限額，重置時間已從錯誤訊息解析 | 不用動；等不及就按重試 |
| 🔧 自動返工 n 次（進行中） | 迴圈正在修沒過的關卡 | 不用動 |
| 🤖 PM 監督介入 | 卡片業務性卡死，專案 PM 已診斷並處置（重跑/換手/補裁定） | 不用動；它會說明決定與理由 |
| 🟠 已返工 N 次仍未通過 | 迴圈不收斂，PM 已介入 2 次 | supervisor 先用最新 gate、run 與兩次 PM 決定做一次確定性重查：有失敗關卡就送回可修改階段並換手，有 executor 故障就重跑／換手；同一證據只做一次，仍失敗才交由人處理 |
| 🟠 設定問題 | 關卡指令在這個 repo 跑不起來 | 改範本指令（重試勾範本刷新）或讓 agent 補相依 |
| 🟠 execution failed/timeout | executor 掛了 | 看錯誤；重活給 `timeout_s`；重試（可換 agent） |
| 🟠 explicitly assigned agent … is incompatible | 指定 Agent 的直連／Gateway、API flavor、model、唯讀能力或 grant 不符合該階段 | 改選相容 Agent，或替卡片綁定正確的 LLM Resource；卡片不會被改動，也不會消耗 PM 介入 |
| 🟠 還活著，但已沉默 N 分鐘 | 行程沒死，只是沒有任何輸出 —— 典型是卡在等輸入的子行程上 | 上主機看 `pgrep -P <pid>`：有子行程停在 `ep_poll`／0% CPU 就是它。收掉那個子行程，該輪通常會自己繼續 |

診斷順序：drawer 的失敗輸出 → `bastet audit` → 心跳最後一行 →
`journalctl --user -u bastet`。

自動路由會在建立 run 前排除不相容 executor，並以 `run.routed` 記錄真正的 Agent、
executor 與 direct／gateway 路徑。Hermes 未綁 LLM Resource 時使用自己的登入設定；
綁定時走 Bastet Gateway，resource 必須是 OpenAI flavor 且設定 model。

Pi 使用暫態 JSONL 模式，由 Bastet 注入 context 並停用專案 extension／package；帳號
profile 透過 Pi 登入／設定安裝的 provider 套件則以明確白名單載入，不會開放專案注入。
修改階段使用可寫工具清單，review 使用真正的 `read,grep,find,ls` 唯讀清單，且可走
直連帳號或 OpenAI／Anthropic Gateway。直連且指定模型時，Bastet 以相同隔離環境的
模型清單解析唯一 provider/model；缺少該路由的 Key 不會再原路重試。OpenClaw 使用
`agent exec --json --isolated`；首版只接受直連、可寫的 code／light-task 階段，因為
上游 exec 目前沒有足以保證唯讀的工具白名單。

### 專案監督與自動解卡

Project runner 不再只等待卡片。背景 supervisor 會監督整個專案：heartbeat 只代表
程序存活，`progress_at` 才代表有實質進度。若 live run 連續 15 分鐘沒有語意進度，
引擎會保留 worktree 後中斷；遇到 max-turns、executor 無輸出或 driver 遺失，會做
最多兩次受控恢復並優先換同角色代理。驗收失敗與人工核准不會被自動跳過。
PM 用完兩次介入後也不會靜默停住：supervisor 會依最新關卡、執行結果與兩次 PM
交接做一次有界重查，將可修的問題送回正確修改階段；證據沒有改變時不會無限循環。

human-approve 前的 `._bastet/preview/` 會整理成核准附件清單；Telegram 直接傳送
圖片、影片與文件，WebUI 卡片保留同一份附件，不再只顯示檔名。

## 5. CLI 速查

```bash
bastet team add / project add / agent add / role-assign / user add
bastet dispatch <專案> "<任務>" --agent <id> [--template <id>] [--resource <id>]
bastet jobs / job <id> / approve <id> [--reject]
bastet runs / run <id> / usage / audit
bastet template add <file.yaml> / template list
bastet resource add / grant add
bastet doctor / service install|status / gc / pricing-update / whoami
```

## 6. 日常維運

- **部署前**看板上有沒有執行中的重活 —— 重啟會殺掉當前輪（啟動會自動接手，但
  該輪進度沒了）。
- **備份**：`~/.bastet/bastet.db`（SQLite，WAL）+ `~/.agent-memory/`。
- **升級**：管理→維護按更新（版本沒變會誠實說 `unchanged`），或
  `pip install -U bastet-agent-os` 後重啟服務。
- **完工分支**：每張完成卡在 `bastet/<job_id>` 分支且已推上遠端（推送輸出含
  MR 連結）；審查合併是你的 git 操作。
- 其他坑見 [CAUTIONS.md](CAUTIONS.md)。
