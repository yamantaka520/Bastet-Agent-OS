# 🐈 Bastet Agent OS

**給 AI agent 團隊的 local-first 作業系統。** Bastet 把你已經在用的 agent —— Claude
Code（CLI 或 Agent SDK）、Codex CLI、Grok Build、Google Antigravity（`agy`）、
Hermes，或任何 OpenAI / Claude 相容端點 —— 組織成有角色、有工作流、有資源治理的
執行團隊，讓多個專案可控地併發推進。

Bastet 是**控制平面，不是另一個 agent 框架**。執行力來自調度既有的 agent；Bastet
補上的是治理、一個遇到失敗還會繼續跑的工作流引擎，以及團隊記憶。

Linux · macOS · Windows · WebUI + CLI · Apache-2.0 ·
[English](README.md)

## 為什麼需要它

跑一個 coding agent 很容易。要讓**一個團隊**的 agent 同時推進好幾個專案，問題就來了：

- 測試一失敗整條線就停下來等人，即使寫這段程式的 agent 才是最有能力修的人。
- 事後沒人說得出：跑了什麼、用誰的帳號、花了多少。
- 每個專案都從零開始，因為 agent 學到的東西沒有留下來。
- 憑證散落在十幾個設定檔裡。
- 「這六個 CLI 哪一個過期了？」沒有答案。

Bastet 把這些收在一個地方、跑在你自己的機器上，每一次狀態變更後面都有一條
append-only 的稽核紀錄。

## 功能

- **會繼續跑的工作流引擎。** 多階段流程（規劃 → 實作 → 測試 → 審查 → 合併）搭配
  四種關卡。關卡沒過時，卡片會**退回**能修的階段，把失敗輸出一起帶過去，然後流程
  自己往下走。只有真的走不下去才停下來問人。→ [關卡沒過的時候](#關卡沒過的時候)
- **有燈號的專案生命週期。** 規劃中 → 待執行 → 執行中 ⇄ 暫停 → 維護中 → 已結案，
  是真正的狀態機：只允許宣告過的轉換，每次轉換都有稽核。卡片上有執行/暫停/停止。
- **看板。** 任務卡透過 WebSocket 即時在階段欄位間移動；每張卡顯示標題、階段、
  狀態，以及被自動返工過幾次。
- **資源池 + 計量 gateway。** LLM / MCP / API / SKILL / git 資源，各自有可見範圍
  （全域 / TEAM / 專案）、憑證用選單指向而不是複製一份、每個資源都有真的測試按鈕，
  並提供 OpenAI / Claude 相容 gateway 計量每次執行的花費。
- **團隊記憶。** 建立在 [Agent Memory OS](https://github.com/yamantaka520/Agent-Memory-OS)
  上：每次執行都會寫下做了什麼、關卡退了什麼、任務怎麼結束，歸屬到實際執行的
  agent 並限定在該專案範圍。Context pack 以**執行中的 agent 身分**讀取，AMOS 的
  ACL 才會生效。
- **對話作為輸入通道。** 跟 agent 或資源池的 LLM 討論規劃、附上檔案與截圖，然後
  直接從對話派工。Telegram 是第二個這樣的通道，支援 inline 核准。
- **看得懂的治理。** 三級角色、個別使用者 token、每個授權的預算與併發上限、每次
  執行的 worktree 或容器隔離，以及可搜尋的 hash 串接稽核紀錄。
- **五語 WebUI。** 繁體中文 · 简体中文 · English · 日本語 · 한국어。

## 安裝

在要跑控制平面的機器上，一行：

```bash
curl -fsSL https://raw.githubusercontent.com/yamantaka520/Bastet-Agent-OS/main/install.sh | bash
```

它會建立 `~/.bastet/venv`，安裝 Bastet + Agent Memory OS + Claude Agent SDK +
pytest，用各家官方 installer 安裝 executor CLI，執行 `bastet init`，最後跑
`bastet doctor`。細節、參數與各 executor 的登入步驟見
[docs/INSTALLATION.md](docs/INSTALLATION.md)。

從 clone 開發：

```bash
pip install -e '.[dev]'
bastet init            # ~/.bastet：資料庫、api token、設定
bastet serve           # 控制平面 + gateway，127.0.0.1:8890
```

WebUI 在 `http://127.0.0.1:8890/ui`，貼上 `~/.bastet/api_token` 的 token 即可。

## 快速開始

```bash
# 建立組織：一個 team、一個綁到真實 repo 的專案、一個 agent
bastet team add meow "Meow Team"
bastet project add catswalker ~/Github/catswalker --team meow
bastet agent add cc-worker --name "Claude Code Worker" --executor claude-code

# 指定工作流並派工
bastet template add standard-dev.yaml
bastet role-assign catswalker cc-worker engineer
bastet dispatch catswalker "修好 tests/test_booking.py 的失敗測試" \
  --agent cc-worker --template standard-dev

bastet runs                  # 正在跑什麼
bastet run <run_id>          # 細節：用量帳、diff artifact
bastet usage                 # 依專案 / agent / 計量精度看花費
bastet audit                 # append-only 稽核紀錄
bastet doctor                # 健檢，含關卡需要的工具
```

要讓流量走 gateway 計量（而不是走訂閱制 CLI），註冊 LLM 資源後加 `--resource`：

```bash
bastet resource add anthropic-api --endpoint https://api.anthropic.com \
  --flavor anthropic --secret-ref keyring:bastet/anthropic
bastet grant add <resource_id> project:catswalker --budget-usd 5 --max-concurrency 2
bastet dispatch catswalker "..." --agent cc-worker --resource <resource_id>
```

每個頁籤與每個指令的完整說明：[docs/USER_GUIDE.md](docs/USER_GUIDE.md)。

## 關卡沒過的時候

測試失敗在開發循環裡是常態，所以它不該讓看板停住。卡片會**退回**能修的階段 ——
越過唯讀的審查階段，回到最後一個會動手寫的階段 —— 把關卡真正的輸出一起帶過去，
然後流程繼續，不需要任何人介入。

一起帶過去的指示會明確點出不准走的捷徑：不要改測試指令、不要刪測試、不要把斷言
改成恆真、不要加 skip/xfail、不要動工作流設定。因為讓關卡通過最便宜的做法就是把
關卡弄鬆，而只被告知「讓它變綠」的 agent 就會這麼做。

還是會停下來問你的只有三種：

| 情況 | 為什麼要人 |
|---|---|
| 階段設了 `on_fail: block` | 部署、發布這種步驟不該被 agent 迴圈重試 |
| 前面沒有可寫的階段 | 整條線都是唯讀階段時，沒有人能動手 |
| 返工次數用完（`max_cycles`，預設 3） | 連續三次修不好的 agent，不會在第三十次修好 |

agent 產出的內容在執行結束時會 commit 到該任務自己的 `bastet/<job_id>` 分支，所以
跑完的迴圈留下的是可以審查的成果，而不是一個 diff 檔。你自己的分支不會被寫入：
合併是刻意的一步。

每一次退回都以 `job.rework` 記入稽核並顯示在卡片上。返工的通知讀起來是進度（什麼
沒過、誰在修、第幾次），真正停下來的通知會帶上失敗輸出與一顆重試按鈕。

## 專案生命週期

專案有狀態，以燈號顯示在卡片上：**規劃中 → 待執行 → 執行中 ⇄ 暫停 → 維護中 →
已結案**（可重啟）。只允許宣告過的轉換，每次都有稽核，所以燈號是事實，不是從
任務列表猜出來的。

規劃與執行之間站著一個人。專案管理 agent 把談定的規劃變成任務清單（唯讀：它看得到
repo、工作流階段與規劃對話），你編輯並確認後，runner 才會開始派工 —— 一個任務接
一個，各自依專案的工作流與角色指派執行。停在關卡的任務會讓 runner 一起等；它絕不
自己核准。所有任務都收束後，專案進入維護中，等你驗收。

卡片上的控制：▶ 執行、⏸ 暫停（停止**下一個**派工，目前的任務跑完）、■ 停止
（取消進行中的）、結案、重啟、刪除。

## 對話：迴圈的人這一端

對話頁籤是人用談話來規劃專案的地方。一個 session 選定由誰回答 —— **agent**
（用它自己的 executor 與帳號，唯讀，看得到專案 repo）或**資源池的 LLM** —— 以及
範圍：專案、TEAM、全域。專案範圍會把真實專案狀態帶進 prompt：描述、repo、工作流、
團隊角色、最近的任務，以及它可以用的資源。

Session 依專案儲存，所以討論不會跟實際執行的組織脫節。檔案、文件、截圖可以送進去，
每一輪都會依 session 的範圍寫進 Agent Memory OS；授權也在這裡發生：待人工核准的
關卡會列出來附上核准/駁回，整段討論也可以直接派成一個任務。agent 不會自己派工 ——
按鈕由人按。

Telegram 是第二個這樣的通道：在管理頁籤給頻道指定回應者與專案，直接傳訊息給 bot
就會在該使用者自己的 session 裡得到回應，重啟後仍然延續，附件也支援。

## 資源池

資源有分類（`llm` · `mcp` · `api` · `skill` · `git` · 媒體），每一個都有自己的可見
範圍 —— 全域、TEAM、專案。憑證欄位是管理頁籤已存憑證的選單：資源存的是
`secret:<id>` 指標，所以換一把 key 就會更新所有用到它的資源。不需要憑證的類型
（SKILL）不會顯示這個欄位。

MCP 資源保留廠商提供的安裝指令；你在 WebUI 按下去執行，完整輸出會回傳，所以裝失敗
可以就地修好再重試。不會有任何隱含安裝。

每個資源都有**測試按鈕**，做的是 agent 會做的事：LLM 列出模型（只列清單、不做
completion，所以測試不花 token）、MCP 完成真正的 `initialize` 握手並回報 server 的
工具清單、SKILL 檢查來源在 Bastet 主機上存在、git 用 HTTPS 或 SSH 對供應商驗證憑證。
判定有三種：`ok`、`warn`（有回應，但不是我們期待的樣子 —— 連得上但 404 跟主機掛掉
是不同的問題）、`failed`，並附上實際發出的請求。

已授權的資源可以被跑該專案的 agent 直接調用。執行開始時 Bastet 會用環境變數
（`BASTET_RES_<NAME>_URL` / `_KEY` / `_TOKEN` / `_MODEL` / `_SOURCE`）、一份
`mcpServers` 設定檔（`BASTET_MCP_CONFIG`，Claude Code 另外給 `--mcp-config`）以及
寫進任務說明的清單交給它。MCP 檔含有已解析的憑證，所以放在 worktree 外、權限 0600，
執行結束就刪除。

## 工作流關卡工具

`tests-pass` 關卡是在 **Bastet 主機上**用服務的 PATH 執行指令，不是在專案的
virtualenv 裡。內建範本用 `pytest -q`、`npm test`、`make test`，所以 `install.sh`
會連 pytest 一起裝，而 `bastet doctor` 會列出目前設定的範本需要哪些程式，並指名是
哪個範本需要它：

```
  ✓ gate tool `npm` → /usr/bin/npm
  ✗ gate tool `pytest` not found — 內建範本 前後端程式開發 的測試關卡會失敗
```

Bastet 自己的 venv 放在 PATH **最後**，所以專案自備的 runner 會贏。專案有自己的環境
時，在範本的指令裡寫明確路徑（`.venv/bin/pytest -q`、`npx vitest run`）。

完全跑不起來的指令會被回報成設定問題，而不是測試不通過 —— 並且交回給能補上缺少的
腳本或相依套件的 agent，附帶「不准假造 exit 0」的指示。

## 團隊記憶

不管由哪個 executor 驅動，每次執行都會寫進 Agent Memory OS：每個階段做了什麼
（歸屬到該 agent 的 AMOS id）、關卡退了什麼、任務怎麼結束。Context pack 以**執行中
的 agent 身分**讀取，所以 AMOS 的 ACL 會生效，一個專案的記憶不會跑進另一個專案的
執行裡。

語意召回需要 `turbovec`（隨 `agent-memory-os[full]` 一起裝）。沒有它時 AMOS 會安靜
地退回關鍵字比對，所以記憶頁會標示目前是哪一種模式，維護卡片也把這個套件列出來。

## 保持最新

Bastet 跑的是別人的工具，所以管理頁籤會列出每個元件 —— Bastet 自己、Agent Memory
OS、turbovec、Claude Agent SDK、pytest，以及 `claude` / `codex` / `grok` / `agy` /
`hermes` CLI —— 顯示已安裝與可用版本，可以個別或一次全部更新。

不會有任何自動更新。在專案執行中偷偷換掉底下的 agent，事後沒人推得出發生了什麼，
所以更新只在你按下按鈕時發生，並且有稽核。查不到可用版本的元件（用官方安裝腳本、
沒有版本查詢端點）會回報 `unknown` 而不是暗示它是最新；跑成功但版本沒有變化會回報
`unchanged` 而不是宣稱更新完成。

## 文件

| 文件 | 內容 |
|---|---|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | install.sh、系統需求、executor 登入、跑成服務、升級 |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | 每個頁籤與每個 CLI 指令，從頭到尾 |
| [docs/HISTORY.md](docs/HISTORY.md) | 專案歷程，以及每個決策為什麼這樣定 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 接下來要做什麼，以及刻意不做什麼 |
| [docs/FEDERATION.md](docs/FEDERATION.md) | 跨主機的共同組織視圖 |
| [SPEC.md](SPEC.md) | 設計規格與資料模型 |
| [CHANGELOG.md](CHANGELOG.md) | 每一個發布版本 |
| [PROGRESS.md](PROGRESS.md) | 目前狀態快照 |
| [COMPATIBILITY.md](COMPATIBILITY.md) | 支援的平台、Python 版本、executor |
| [SECURITY.md](SECURITY.md) | 威脅模型、機敏資料處理、回報方式 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 怎麼在這個 repo 上工作 |

## 授權

Apache-2.0。建立在 [Agent Memory OS](https://github.com/yamantaka520/Agent-Memory-OS) 之上。
