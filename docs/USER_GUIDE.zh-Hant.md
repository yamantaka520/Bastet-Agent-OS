# Bastet Agent OS — 操作手冊

從第一次登入到日常維運，照使用順序寫。安裝見
[INSTALLATION.md](INSTALLATION.md)；工作流引擎的完整規則（每種關卡、每種停下的
狀況）見 [WORKFLOWS.md](WORKFLOWS.md)；踩坑清單見 [CAUTIONS.md](CAUTIONS.md)。

## 1. 核心概念

| 名詞 | 意思 |
|---|---|
| **Team** | 組織頂層；記憶可以在這層共享 |
| **專案** | 與 Bastet 主機上的一個真實 git repo 一對一，也與一個 AMOS 專案一對一。有生命週期狀態與燈號 |
| **Agent** | executor + 帳號 + 模型設定。`claude-code`、`claude-sdk`、`codex`、`grok`、`agy`、`hermes`、`bastet-lite` |
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

### 資源

分類分組（LLM/MCP/API/SKILL/git/媒體），每個資源自己的可見範圍。**測試按鈕**做
agent 會做的事（LLM 列模型不花 token、MCP 真握手、git 真驗證），三態結果：
`ok` / `warn`（可達但不如預期）/ `failed`，附實際請求。MCP/SKILL 的安裝指令由
**人**在這頁按執行，完整輸出回傳。

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
  Pillow、Playwright、五個 executor CLI），絕不自動更新

### 稽核

hash 串接、append-only。搜尋：關鍵字（含 detail 內容）、類別（來自實際資料）、
操作者、起訖時間。

## 4. 卡住了怎麼辦（速查）

| 看到 | 意思 | 動作 |
|---|---|---|
| ⏸ 等人工核准 | 設計中的停點 | 看預覽 → 核准/駁回（WebUI 或 Telegram） |
| ⏳ 額度用盡，會自己續跑 | 廠商限額，重置時間已從錯誤訊息解析 | 不用動；等不及就按重試 |
| 🔧 自動返工 n 次（進行中） | 迴圈正在修沒過的關卡 | 不用動 |
| 🟠 已返工 N 次仍未通過 | 迴圈不收斂 | 讀關卡輸出：修環境或修驗收條件（用補給裁定），按重試（額度重算） |
| 🟠 設定問題 | 關卡指令在這個 repo 跑不起來 | 改範本指令（重試勾範本刷新）或讓 agent 補相依 |
| 🟠 execution failed/timeout | executor 掛了 | 看錯誤；重活給 `timeout_s`；重試（可換 agent） |
| 🟠 還活著，但已沉默 N 分鐘 | 行程沒死，只是沒有任何輸出 —— 典型是卡在等輸入的子行程上 | 上主機看 `pgrep -P <pid>`：有子行程停在 `ep_poll`／0% CPU 就是它。收掉那個子行程，該輪通常會自己繼續 |

診斷順序：drawer 的失敗輸出 → `bastet audit` → 心跳最後一行 →
`journalctl --user -u bastet`。

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
