# 注意事項 — Operational Cautions

跑一套會自己動的系統，最危險的是「以為它在做什麼」和「它實際在做什麼」不一致。
這份文件收錄實際營運中踩過的每一個坑 —— 每一條都真的發生過，多數已由產品內建
防護，但知道原理才能在變體出現時認得它。

## 安全邊界（先讀這段）

- **Agent 以主機使用者的權限執行。** worktree 隔離擋的是 git 狀態互踩，不是惡意
  程式碼；不受信的工作請用 container 隔離（per-stage `isolation: container`）。
- **對話回應者有 Bash。** 為了讓授權資源真的可以被調用（生圖、TTS），專案範圍的
  對話 agent 拿得到 Bash —— 它能在主機上動手。`chat.send` 是 operator 權限，
  請據此發 token。
- **注入 run 的憑證，就假設該 agent 讀得到。** 優先用短效、最小範圍的 token，
  以專案為單位授權，不要全域。
- **Repo 內容是不可信資料。** 審查指示已明講 diff 裡的「approve this」要無視；
  verdict 走檔案協議正是為了讓 repo 裡的文字無法決定關卡。
- **誰能動手不是設定。** 對話設定協議（bastet-config）刻意不含使用者、token、
  通知頻道 —— 被 prompt injection 塞「加個 admin」時要無函式可呼叫。

## 環境與安裝

- **裸打 `python` 會解析到 Bastet 的 venv**（Ubuntu 沒有 `/usr/bin/python`，而
  venv 在 PATH 上）。所以 venv 必須帶齊媒體工作需要的套件 —— 這就是 Pillow 在
  標準工具清單裡的原因。症狀：「只在 run 裡面」出現 `No module named PIL`，
  shell 上測全部正常。
- **非 editable 的 pip 安裝，同版本號不會真更新。** 從 git 直裝時要
  `pip install --force-reinstall --no-deps`，否則 pip 看版本沒變就跳過。
  維護卡片的更新按鈕已處理這點。
- **`bastet doctor` 在安裝同一個 shell 跑，可能誤報 executor 不存在** —— PATH
  還沒 re-source。開新 shell 再跑一次。
- **Playwright 套件 ≠ 瀏覽器。** 沒跑過 `playwright install chromium` 的主機，
  第一次用就死在 `Executable doesn't exist`。install.sh 與 Docker 映像已一起裝；
  離線主機記得補。
- **服務的 PATH 不是登入 shell 的 PATH。** systemd 使用者服務下 executor 找不到
  是這個原因；Bastet 會自行重建需要的 PATH 並把自己的 venv 排最後（專案自備的
  runner 永遠優先）。

- **worktree 裡的 git metadata 不在 worktree 裡面。** 連結式 worktree 的 `.git`
  是一個檔案，內容是 `gitdir: <主倉庫>/.git/worktrees/<名稱>` —— 所以任何 git
  **寫入**都落在主倉庫，也就是落在 `--sandbox workspace-write` 的範圍之外。症狀
  極具誤導性：`cannot lock ref 'ORIG_HEAD': Read-only file system`、
  `cannot create .git/worktrees/<job>/index.lock`。**去 touch 那個目錄會顯示可寫**
  （它真的可寫），但 agent 還是寫不進去 —— 因為問題是沙箱不是權限。系統已對
  可寫入的 codex run 加上 `--add-dir`；若你自訂 executor 或關卡指令要用 git，
  記得同樣把那個目錄納入可寫範圍。

## 廠商與額度

- **訂閱額度用盡是計時器，不是錯誤。** `You've hit your session limit · resets
  1:30am (Asia/Taipei)` 這類失敗會自動停靠並在重置後自己續跑 —— 期間手動重試
  幾次都一樣死，不用按。等不及可以按，會搶先。
- **廠商會無預警收緊 API 驗證。** OpenAI strict schema 曾讓所有 codex 審查一夜
  全滅（`invalid_json_schema`）。症狀是秒殺且錯誤指向 schema/request 而非任務
  內容 —— 這類是產品修法，重試無效。
- **模型清單會過期。** grok 的整個陣容曾一次換光。模型欄位是自由輸入 + 建議
  清單；廠商明天出的新模型當天就能填，grok 的清單直接問 CLI 本人。
- **DNS 偶發故障會讓一輪生成全空。** 引擎會誠實返工並在額度內重試；若連續
  多輪同樣錯誤，先在主機上 `getent hosts <api host>` 確認再重試。

## 工作流設計

- **重活要宣告自己的時間預算。** 預設 3600 秒；50 分鐘以上的階段（3D 生成、
  大型最佳化）不設 `timeout_s` 就會在整點被殺，整輪工作蒸發。
- **驗收條件要在執行環境內可驗證。** 「真機實測 fps」在 headless 主機上沒有任何
  agent 能誠實做到 —— 審查者會正確地一直拒絕，返工燒完停下。把機器可驗的
  （節流模擬數據）交給機器，只有人能驗的（真機）明確放到 human-approve 關卡。
- **返工上限預設 3 是刻意的。** 連續三次修不好代表不會收斂，該人看了。人按重試
  = 額度重算，這是「我修好環境了，再來」的表達方式。
- **審查階段一定要 `read_only: true`。** 否則返工目標的推算會把工作交回審查者
  自己 —— 它不該修它剛拒絕的東西。
- **測試關卡的失敗，修的人通常不是測試員。** 返工預設先退回失敗階段本身（實作者
  自己的測試沒過，本來就該自己修），但第二輪起會**往回走**到更前面的可寫階段。
  真實事故：E2E 階段有一個測試失敗，返工目標一直是 E2E 自己，測試員四小時內把
  同一個失敗測試重跑九次，沒有人去改它測的那份產品程式碼。若你的工作流有明確
  歸屬，直接在階段上寫 `rework_target`（那會蓋過自動推算）。
- **安靜不等於死掉。** 引擎分開記兩件事：心跳（行程還活著，每 20 秒一次）與
  輸出（最後說了什麼、隔多久）。中斷假活的判準是**心跳消失**，不是沉默 ——
  真實事故：agent 誠實回報「在等 20 關 × 60 秒的 FPS 測試」、心跳正常，卻被
  15 分鐘的沉默容忍度連殺四次（17→21→25→42 分鐘），換 agent 也一樣，因為
  20 分鐘的測試不可能在 15 分鐘的耐心內跑完。長時間安靜的重活請宣告
  `timeout_s`，那才是限制它的正確工具；看板的 🟠「還活著但已沉默」是資訊。
- **會發問的指令＝無限期卡住。** headless 執行沒有人可以回答。實際事故：某階段
  跑 `npm exec playwright --version`，npx 想先安裝、停在「Ok to proceed? (y)」，
  卡了 52 分鐘（只用掉 2 秒 CPU），agent 等自己的子行程，整張卡動不了。系統這端
  已經把每個 executor 的 stdin 接到 `/dev/null` 並設好 `CI`／`npm_config_yes`／
  `GIT_TERMINAL_PROMPT=0`（子孫行程一併繼承），所以現在會**立刻失敗**而不是等；
  但寫工作流時仍要避開會互動的指令，並優先用主機上裝好的工具（Playwright 是
  已安裝的 CLI，不要 `npx`）。

- **PM 監督不碰人工核准關卡。** 業務性卡死（返工耗盡、驗收爭議）會先由專案 PM
  診斷處置（每張卡最多 2 次、escalate 後閂住直到人重試）；但 human-approve 是
  設計中的停點，永遠等人。如果連 PM 都判 escalate，卡片上的理由就是它要給你的
  交接說明。

## 媒體任務

- **廠商回傳的下載 URL 會過期**（有的 48 小時）。只有下載成 worktree 實體檔案的
  產物會被 commit 和推送 —— brief 已強制，但自訂 prompt 時別忘。
- **絕不「背景生成 + 等通知」。** headless run 結束時子行程全部回收，通知永遠
  不會來。曾有卡片這樣空轉三輪。前景輪詢到檔案落地為止。
- **像素/尺寸限制以實測為準。** 文件寫的和 API 實際接受的常不一致（Seedream
  要求 ≥368 萬像素、回傳其實是 JPEG）。第一次接新模型先實測一張，把發現寫進
  資源的 note —— 之後每個 run 的 brief 都會看到。

## 憑證與機敏資料

- **值是寫入式的。** 存進去之後介面上永遠讀不回來 —— 換 key 用輪替，不要想
  「看一眼」。
- **PEM 多行貼上。** 欄位是多行的；萬一貼成一行，系統會依 BEGIN/END 標記修復，
  但貼原樣最保險。**結尾要有換行** —— 一把缺結尾換行的 key 會讓 ssh 報
  `invalid format`，內容其實是好的。
- **對話裡永遠不要出現金鑰原文。** 提案協議只收 `secret:<id>` 指標，原文會被
  拒絕 —— 它已經流經模型。曾有 bot token 貼進對話，之後只能輪替。
- **`~/.bastet/secrets/` 會累積輪替後的舊檔**，目前不會自動清理；定期人工檢視。

## 營運

- **突然重啟採 at-least-once 接續。** SQLite WAL + `synchronous=FULL` 保住已提交
  狀態；啟動時舊 run 轉為 `orphaned` 並撤銷 token，同一張 job 從記錄的 stage 開新
  attempt，task-plan、worktree、gate、交接與用量不會重建成另一張卡。尚未提交的
  Agent 進度仍可能重做，因此部署、付款、發訊等外部副作用必須使用 idempotency key。
- **計畫性部署仍應 drain。** 先進 maintenance fence，等 active jobs/runs 都為 0，
  再重啟；crash recovery 是安全網，不是用來取代正常交接點。
- **正式交付不是 Agent 自述。** 發版卡必須選 `production` 並填新版本；專案的
  delivery profile 由可信任管理者設定，因為其中的 gate/deploy/verify 命令會在
  Bastet 主機執行。只有 main 與不可變 tag 原子推送、部署命令成功，且線上回報的
  JSON receipt 明確包含 `status=verified`，且 target、version、commit 分別與本次
  交付完全一致後，卡片才會變成完成；只有 exit code 0 不構成線上證據。
- **商店上傳、送審、核准、上架是四件事。** App Store Connect／Google Play 必須使用
  release-manager 人工閘道與非同步 store receipt；`waiting_external` 期間只輪詢狀態，
  不能重跑上傳／送審。設定 `release_goal=published` 時，submitted 或 approved 都不能把
  卡片標成完成；rejected 必須阻擋並保留 provider 原始狀態。
- **status／recovery adapter 不替你上傳或送審。** `status_adapter=official_api` 只查詢上傳指令
  receipt 指定的 Apple version ID／Google versionCode。上傳指令若沒有回傳綁定本次
  commit、version、target 與商店物件 ID 的 JSON，交付直接失敗；商店私鑰只能由專案
  Secrets 注入，不能放進 delivery profile、指令輸出或 evidence。
- **商店 canary 的 `ok` 只代表查詢鏈成立。** `bastet store-canary` 不會改任務或發布狀態；
  是否達到交付目標必須看獨立的 `meets_release_goal`。`--project --submission` 的 commit
  provenance 是外部檔案自述，正式驗收應使用綁定 frozen delivery 的 `--job`。Google 查詢
  會建立隨即刪除、絕不 commit 的 read edit；它不等於完全無 API-side object creation。
- **submission command 必須真的使用 idempotency key。** 引擎會固定
  `BASTET_DELIVERY_IDEMPOTENCY_KEY`、要求 receipt 原樣回傳並保存成功 action；這能保證狀態
  查詢失敗後不重跑 command。但若 command 只是回傳 key、實際上沒有 lookup-or-create，主機
  在外部成功而本機尚未保存 receipt 的極小 crash window 仍可能重複副作用。可設定
  `submission_recovery=official_api`，並提供 Apple `build_number`（可選 `platform`）或 Google
  `version_code`；重試會先查供應商上的精確版本，找到即重建收據，查詢錯誤則 fail closed，
  不會執行 command。這仍不是任意外部副作用的通用 exactly-once 保證。
- **內建 Google submitter 目前只准 internal track。** `submission_adapter=official_api` 會真的
  上傳 AAB、更新 track 並 commit edit，因此仍強制 terminal `human-approve`。它逐位元核對本機
  與 Play API 的 SHA-256、保留原有 releases、先 validate，並固定使用
  `changesInReviewBehavior=ERROR_IF_IN_REVIEW`；不得改成 Google 的預設取消既有審核行為。
  `google_changes_not_sent_for_review` 預設為 true。Google production track 尚未內建。
- **內建 Apple submitter 不等於 binary uploader。** 它只接受已處理完成的精確 `VALID` build，
  lookup-or-create App Store version、核對或附掛 build，並在目標高於 uploaded 時建立／重用
  review item 後送審；仍強制 terminal `human-approve`。送審目標的 recovery 必須看到同一版本
  的 review submission 已進入 submitted-or-later，不能只因 build 已附掛就誤判成功。
- **審計是 hash 串接的 append-only。** 不要手動改 audit_log —— 鏈會斷，
  `verify_audit_chain()` 會抓到。刪任務/專案時的用量帳務會被拒絕或要求 force
  並記錄寫掉的金額，這是刻意的。
- **時間顯示是設定，儲存永遠是 UTC。** 管理 → 系統設定選時區只影響顯示；
  audit log 的原始值跨機器可比對。
- **Telegram 一則訊息上限 4096 字。** 通知會自動截尾保留 assertion；完整輸出
  永遠在 drawer 和 audit 裡。
- **LAN 模式的 Host 白名單不要關。** 它擋的是 DNS rebinding；rebound 請求帶的
  是攻擊者網域，不會誤傷正常使用。

## 已知未解（誠實清單）

- 非同步媒體生成若比 run 活得久，沒有背景取回器（規劃中）；目前規則是 run 內
  等到完成。
- `~/.bastet/secrets` 無自動清理。
- 排程觸發工作流（持續維護範本的定期執行）尚未內建。
- 完成分支的審查/合併仍是手動 git 操作（MR 連結會出現在推送輸出裡）。
