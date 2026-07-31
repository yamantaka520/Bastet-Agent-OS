"""Built-in workflow presets, roles and gate vocabulary (SPEC §5.4).

Presets are read-only starting points: copy one into your own templates to
edit it, or assign it to a project as-is. Every preset's stages must parse
with workflow.parse_stages() — a test enforces that.
"""

from __future__ import annotations

# Roles a stage can call for. Stage → agent matching uses these ids
# (project_agent_roles); the labels are for humans in the UI.
ROLES = [
    {"id": "pm", "label": "專案/產品經理", "hint": "規劃、驗收、對外溝通"},
    {"id": "engineer", "label": "工程師", "hint": "通用開發"},
    {"id": "backend-engineer", "label": "後端工程師", "hint": "API、資料層"},
    {"id": "frontend-engineer", "label": "前端工程師", "hint": "介面實作"},
    {"id": "mobile-engineer", "label": "行動端工程師", "hint": "iOS/Android"},
    {"id": "tester", "label": "測試工程師", "hint": "測試撰寫與執行"},
    {"id": "reviewer", "label": "審查者", "hint": "程式/內容審查"},
    {"id": "security-reviewer", "label": "安全審查者", "hint": "資安風險把關"},
    {"id": "designer", "label": "設計師", "hint": "版型、使用者流程"},
    {"id": "researcher", "label": "研究員", "hint": "調查設計與資料蒐集"},
    {"id": "analyst", "label": "分析師", "hint": "數據與市場分析"},
    {"id": "writer", "label": "撰稿", "hint": "文件、腳本、報告"},
    {"id": "editor", "label": "編輯/剪輯", "hint": "潤稿、影音剪輯"},
    {"id": "producer", "label": "製作", "hint": "素材與流程協調"},
    {"id": "ops-engineer", "label": "運維工程師", "hint": "部署、監控、故障處理"},
    {"id": "maintainer", "label": "維護工程師", "hint": "相依更新、技術債、長期健康"},
]

# Gate vocabulary with human labels (engine semantics live in workflow.py)
GATES = [
    {"id": "auto", "label": "自動通過", "icon": "▶",
     "hint": "階段跑完就進下一關，適合不需把關的工作"},
    {"id": "tests-pass", "label": "測試通過", "icon": "✓",
     "hint": "由引擎執行你指定的指令，exit code 0 才過關"},
    {"id": "agent-review", "label": "AI 審核", "icon": "🔍",
     "hint": "審查 agent 必須回傳結構化裁決；沒有裁決一律不通過"},
    {"id": "human-approve", "label": "人工核准", "icon": "⏸",
     "hint": "停下來等人在看板或 Telegram 上按核准"},
]

PRESETS = [
    {
        "id": "fullstack-dev",
        "name": "前後端程式開發",
        "description": "完整的軟體開發循環：規劃 → 後端 → 前端 → 整合測試 → "
                       "程式審查 → 安全審查 → 合併。",
        "stages": [
            {"name": "需求規劃", "role": "pm", "gate": "human-approve",
             "desc": "拆解需求、產出實作計畫，等你核准後才動工"},
            {"name": "後端實作", "role": "backend-engineer", "gate": "tests-pass",
             "gate_config": {"command": "pytest -q"},
             "max_retries": 1, "desc": "API 與資料層，單元測試必須綠燈"},
            {"name": "前端實作", "role": "frontend-engineer", "gate": "tests-pass",
             "gate_config": {"command": "npm test --silent"},
             "max_retries": 1, "desc": "介面與串接，前端測試必須綠燈"},
            {"name": "整合測試", "role": "tester", "gate": "tests-pass",
             "gate_config": {"command": "pytest -q && npm test --silent"},
             "desc": "端到端跑一次完整測試"},
            {"name": "程式審查", "role": "reviewer", "gate": "agent-review",
             "read_only": True, "desc": "唯讀審查 diff，回傳結構化裁決"},
            {"name": "安全審查", "role": "security-reviewer", "gate": "agent-review",
             "read_only": True, "desc": "資安與機敏資料風險檢查"},
            {"name": "合併發布", "role": "engineer", "gate": "human-approve",
             "desc": "有副作用的一步，預設要你親自放行"},
        ],
    },
    {
        "id": "web-dev",
        "name": "網頁開發",
        "description": "以頁面為單位的網站開發：版型 → 實作 → 無障礙/響應式檢查 → "
                       "E2E → 上線核准。",
        "stages": [
            {"name": "需求與版型", "role": "designer", "gate": "human-approve",
             "desc": "確認頁面結構與視覺方向"},
            {"name": "頁面實作", "role": "frontend-engineer", "gate": "auto",
             "max_retries": 1, "desc": "切版與互動邏輯"},
            {"name": "響應式與無障礙檢查", "role": "reviewer", "gate": "agent-review",
             "read_only": True, "desc": "檢視 RWD 斷點、對比、鍵盤操作與語意標籤"},
            {"name": "E2E 測試", "role": "tester", "gate": "tests-pass",
             "gate_config": {"command": "npm run test:e2e"},
             "desc": "瀏覽器流程自動化測試"},
            {"name": "上線核准", "role": "pm", "gate": "human-approve",
             "desc": "確認後才部署"},
        ],
    },
    {
        "id": "mobile-app",
        "name": "手機 APP 開發",
        "description": "行動應用開發：規格 → 實作 → 建置測試 → 程式審查 → 送審準備。",
        "stages": [
            {"name": "規格與畫面流程", "role": "pm", "gate": "human-approve",
             "desc": "定義畫面流程與驗收標準"},
            {"name": "功能實作", "role": "mobile-engineer", "gate": "auto",
             "max_retries": 1, "desc": "依規格實作畫面與邏輯"},
            {"name": "建置與單元測試", "role": "tester", "gate": "tests-pass",
             "gate_config": {"command": "make test"},
             "desc": "確認可建置且測試通過"},
            {"name": "程式審查", "role": "reviewer", "gate": "agent-review",
             "read_only": True, "desc": "唯讀審查變更"},
            {"name": "送審準備", "role": "pm", "gate": "human-approve",
             "desc": "版本說明、截圖與商店資訊確認"},
        ],
    },
    {
        "id": "market-research",
        "name": "市場調查",
        "description": "調查設計 → 資料蒐集 → 分析 → 洞察審核 → 報告 → 交付。",
        "stages": [
            {"name": "調查設計", "role": "researcher", "gate": "human-approve",
             "desc": "界定問題、對象與方法，等你確認方向"},
            {"name": "資料蒐集", "role": "researcher", "gate": "auto",
             "desc": "公開資料、競品資訊與訪談整理"},
            {"name": "市場與競品分析", "role": "analyst", "gate": "auto",
             "desc": "量化整理與趨勢判讀"},
            {"name": "洞察審核", "role": "reviewer", "gate": "agent-review",
             "read_only": True, "desc": "檢查推論是否有資料支撐、是否過度延伸"},
            {"name": "報告產出", "role": "writer", "gate": "auto",
             "desc": "撰寫可交付的調查報告"},
            {"name": "交付核准", "role": "pm", "gate": "human-approve",
             "desc": "你確認後才視為完成"},
        ],
    },
    {
        "id": "academic-research",
        "name": "學術研究",
        "description": "研究問題 → 文獻回顧 → 方法設計 → 分析 → 結果驗證 → "
                       "撰稿 → 定稿。",
        "stages": [
            {"name": "研究問題與文獻回顧", "role": "researcher",
             "gate": "human-approve", "desc": "確立問題意識並盤點既有研究"},
            {"name": "方法設計", "role": "researcher", "gate": "human-approve",
             "desc": "設計方法與資料來源，需你認可後執行"},
            {"name": "資料分析", "role": "analyst", "gate": "auto",
             "desc": "執行分析並記錄可重現的步驟"},
            {"name": "結果驗證", "role": "reviewer", "gate": "agent-review",
             "read_only": True, "desc": "檢驗結論與資料一致、方法無誤用"},
            {"name": "論文撰寫", "role": "writer", "gate": "auto",
             "desc": "依格式撰寫並附上引用"},
            {"name": "定稿審閱", "role": "editor", "gate": "human-approve",
             "desc": "潤稿與最終確認"},
        ],
    },
    {
        "id": "video-production",
        "name": "影片製作",
        "description": "企劃腳本 → 素材準備 → 剪輯 → 內容審核 → 成品核准。",
        "stages": [
            {"name": "企劃與腳本", "role": "writer", "gate": "human-approve",
             "desc": "主題、結構與逐字腳本"},
            {"name": "素材準備", "role": "producer", "gate": "auto",
             "desc": "畫面、配樂、字卡等素材清單與產出"},
            {"name": "剪輯", "role": "editor", "gate": "auto",
             "desc": "組裝影片、字幕與節奏調整"},
            {"name": "內容審核", "role": "reviewer", "gate": "agent-review",
             "read_only": True, "desc": "事實、版權與品牌一致性檢查"},
            {"name": "成品核准", "role": "pm", "gate": "human-approve",
             "desc": "你看過成品後才發布"},
        ],
    },
    {
        "id": "ops-incident",
        "name": "運維處理",
        "description": "線上狀況的處理循環：定位 → 止血 → 根因 → 修復驗證 → "
                       "部署 → 事後記錄。以最短時間恢復服務為第一優先。",
        "stages": [
            {"name": "問題定位", "role": "ops-engineer", "gate": "auto",
             "desc": "重現與收斂範圍：什麼壞了、影響誰、從何時開始"},
            {"name": "緊急處置", "role": "ops-engineer", "gate": "human-approve",
             "desc": "先讓服務能用（回滾、關閉旗標、擴容）—— 有副作用，要你放行"},
            {"name": "根因分析", "role": "ops-engineer", "gate": "auto",
             "desc": "找到真正的原因，而不是只讓症狀消失"},
            {"name": "修復與回歸測試", "role": "backend-engineer",
             "gate": "tests-pass",
             "gate_config": {"command": "pytest -q"},
             "max_cycles": 3,
             "desc": "修好並補上能重現這個故障的測試；沒過就自己修到過"},
            {"name": "變更審查", "role": "reviewer", "gate": "agent-review",
             "read_only": True, "rework_target": "修復與回歸測試",
             "desc": "唯讀檢查修復是否對症、有無新風險"},
            {"name": "部署上線", "role": "ops-engineer", "gate": "human-approve",
             "on_fail": "block",
             "desc": "真的動到線上環境的一步，永遠由人放行"},
            {"name": "事後記錄", "role": "writer", "gate": "human-approve",
             "desc": "時間軸、根因、後續行動 —— 你看過才結案，內容會進團隊記憶"},
        ],
    },
    {
        "id": "continuous-maintenance",
        "name": "持續維護",
        "description": "已上線專案的定期維護：健康檢查 → 相依與安全更新 → "
                       "回歸測試 → 技術債整理 → 驗收。適合排程反覆執行。",
        "stages": [
            {"name": "健康檢查", "role": "maintainer", "gate": "auto",
             "desc": "盤點目前狀態：錯誤、效能、待辦、過期相依"},
            {"name": "相依與安全更新", "role": "maintainer", "gate": "tests-pass",
             "gate_config": {"command": "pytest -q"},
             "max_cycles": 3,
             "desc": "升級套件並修掉升級帶來的破壞；測試必須綠燈"},
            {"name": "回歸測試", "role": "tester", "gate": "tests-pass",
             "gate_config": {"command": "pytest -q"},
             "rework_target": "相依與安全更新",
             "desc": "完整跑一次；失敗就退回上一階段修"},
            {"name": "技術債整理", "role": "maintainer", "gate": "auto",
             "desc": "小步重構與補文件，不夾帶新功能"},
            {"name": "維護審查", "role": "reviewer", "gate": "agent-review",
             "read_only": True, "rework_target": "技術債整理",
             "desc": "確認這一輪沒有偷偷改變行為"},
            {"name": "維護驗收", "role": "pm", "gate": "human-approve",
             "desc": "你確認這一輪維護的結果"},
        ],
    },
]


def preset(preset_id: str) -> dict | None:
    return next((p for p in PRESETS if p["id"] == preset_id), None)
