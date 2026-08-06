"""Bastet configuring Bastet: the built-in skill and the chat-apply protocol.

The ask: "set up a media API resource by talking about it" — and more generally,
let configuration happen in the conversation instead of a form. Two halves:

* A built-in **skill** (`bastet-config`) whose source is a generated guide to
  Bastet's own configuration: every resource kind with its fields, how
  credentials travel (`secret:<id>`, never values), scopes, and the action
  protocol below. It is a pool resource with a global grant, so any chat
  responder or running agent can read it the way it reads any other skill.

* An **action protocol**: an assistant that wants to configure something ends
  its reply with a fenced block —

      ```bastet-config
      {"actions": [{"op": "resource.create", "kind": "api", "name": "…", …}]}
      ```

  The chat UI renders the block as a card with an 套用 button. **The model never
  applies anything** — the block is a proposal, the click is the authority, the
  audit row names the human. This is the same shape as dispatch-from-chat and
  gate approval: agents propose, people press buttons.

The whitelist below is deliberately small. Users, tokens and channel bindings
stay out: anything that changes *who can act* is not configuration, and a
prompt-injected "add an admin" must have nothing here to call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import resource_kinds as rk
from . import secrets_store
from .db import Db, new_id, now

log = logging.getLogger("bastet.selfconfig")

SKILL_NAME = "bastet-config"
FENCE = "bastet-config"

ALLOWED_OPS = ("resource.create", "resource.update", "grant.create",
               "settings.timezone")

MAX_ACTIONS = 10


# ---- the guide (skill source) --------------------------------------------------

def guide_markdown() -> str:
    catalog = rk.catalog()
    lines = [
        "# Bastet Agent OS — 自我設定指南（bastet-config skill）",
        "",
        "這份文件由 Bastet 產生，描述如何以結構化動作設定 Bastet 自己：資源池",
        "（LLM / MCP / API / SKILL / git / 媒體）、授權範圍與系統設定。",
        "",
        "## 原則",
        "",
        "- 你（agent）**提出**設定，人**套用**設定。把動作放在回覆最後的",
        f"  ```{FENCE}``` 圍欄區塊裡，UI 會渲染成一張可套用的卡片。",
        "- 不要宣稱已套用 —— 你做不到，按鈕在人手上。",
        "- 憑證永遠用 `secret:<憑證id>` 指標（管理 → 憑證 建立後取得），",
        "  不要把金鑰原文放進動作或對話。",
        "",
        "## 動作協議",
        "",
        "```" + FENCE,
        json.dumps({"actions": [
            {"op": "resource.create", "kind": "api", "name": "eleven-labs-tts",
             "endpoint": "https://api.elevenlabs.io", "secret_ref": "secret:res_xxx",
             "config": {"note": "語音合成"},
             "scope_type": "project", "scope_id": "catswalker"},
            {"op": "grant.create", "resource": "eleven-labs-tts",
             "scope_type": "team", "scope_id": "Meow1"},
            {"op": "settings.timezone", "timezone": "Asia/Taipei"},
        ]}, ensure_ascii=False, indent=2),
        "```",
        "",
        "支援的 op：" + "、".join(f"`{op}`" for op in ALLOWED_OPS),
        "（使用者、token、通知頻道**不在**此協議內 —— 誰能動手不是設定問題。）",
        "",
        "### resource.create / resource.update",
        "欄位：`kind`、`name`、`endpoint`、`api_flavor`（openai|anthropic，LLM 用）、",
        "`secret_ref`（`secret:<id>`）、`config`（見下方各 kind 欄位）、",
        "選填 `scope_type`+`scope_id`（順便建立授權）。update 以 `name` 或 `id` 指認。",
        "",
        "### grant.create",
        "`resource`（名稱或 id）+ `scope_type`（global|team|project）+ `scope_id`。",
        "",
        "## 資源種類",
        "",
    ]
    auth_text = {"required": "需要憑證", "optional": "憑證可選", "none": "不需要憑證"}
    for kind in catalog["kinds"]:
        if kind["id"] == "secret":
            continue                     # credentials are never set up via chat
        fields = "、".join(kind.get("fields", [])) or "—"
        lines.append(f"- **{kind['id']}**（群組 {kind.get('group')}）：欄位 {fields}；"
                     f"{auth_text.get(kind.get('auth'), '')}")
    lines += [
        "",
        "### auth_header 的寫法",
        "接受兩種形態，系統會自動正規化：只寫名稱（`X-API-Key`、`Authorization`）",
        "或完整一行含占位符（`Authorization: Bearer {API_KEY}`）。實際金鑰由",
        "secret_ref 指向的憑證在使用時代入，永遠不要把金鑰寫進 auth_header。",
        "",
        "### SKILL 的安裝",
        "skill 資源可帶 `install_command`（config 內）。流程：提案建立 → 人套用 → ",
        "人到「資源」頁按「安裝」（admin 權限、完整輸出回傳、有稽核）。安裝指令",
        "不會因套用提案而自動執行 —— 在主機上跑 shell 永遠是人親自按的那一下。",
        "執行中的 agent 也可以在自己的 run 裡用 Bash 安裝到 worktree。",
        "",
        "enum：`mcp_transport` ∈ " + str(catalog["enums"]["mcp_transport"]) +
        "；`git_provider` ∈ " + str(catalog["enums"]["git_provider"]),
        "",
        "## 執行期（設定完之後 agent 拿到什麼）",
        "",
        "- env：`BASTET_RES_<名稱>_URL / _KEY / _TOKEN / _MODEL / _SOURCE`",
        "- MCP：`BASTET_MCP_CONFIG` 指向已解憑證的 mcpServers 檔（run 結束即刪）",
        "- 任務說明附資源清單；git 資源已備好 `GIT_SSH_COMMAND`",
    ]
    return "\n".join(lines) + "\n"


def seed_skill(db: Db, home_root) -> str:
    """Write the guide and make sure the pool carries it, globally visible.

    The file is rewritten every boot (the guide tracks the code), the resource
    row is created once and left alone — users may rescope or disable it."""
    from pathlib import Path

    folder = Path(home_root) / "skills"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{SKILL_NAME}.md"
    path.write_text(guide_markdown())
    existing = db.one("SELECT id FROM resources WHERE name=? AND kind='skill'",
                      (SKILL_NAME,))
    if existing is not None:
        return existing["id"]
    rid = new_id("res")
    ts = now()
    db.write("INSERT INTO resources(id, kind, name, endpoint, api_flavor, "
             "secret_ref, config_json, created_at, updated_at) "
             "VALUES(?, 'skill', ?, '', NULL, NULL, ?, ?, ?)",
             (rid, SKILL_NAME,
              json.dumps({"skill_source": str(path),
                          "note": "內建：Bastet 自我設定指南（對話中設定資源用）"}),
              ts, ts))
    db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
             "VALUES(?, ?, 'global', '*', ?)", (new_id("grt"), rid, ts))
    db.audit("server", "resource.create", "resource", rid,
             {"kind": "skill", "name": SKILL_NAME, "builtin": True})
    return rid


# ---- the prompt side ------------------------------------------------------------

# The schema travels IN the prompt, not by reference: the first live test showed
# an agent that could not read the skill file inventing its own shape
# (type/payload instead of op/fields), which parses as nothing. A chat responder
# has no filesystem guarantee, so the contract must be self-contained here; the
# skill file remains the deeper reference for agents that can read it.
PROMPT_NOTE = (
    "\n## 設定 Bastet 本身（bastet-config）\n"
    "使用者要求新增/調整 Bastet 的資源或系統設定時，在回覆**最後**用 ```"
    + FENCE + "``` 圍欄放一個提案區塊，**欄位必須完全依照這個 schema**（不要自創"
    "欄位名）：\n"
    '```' + FENCE + '\n'
    '{"actions": [\n'
    '  {"op": "resource.create", "kind": "tts", "name": "eleven-tts",\n'
    '   "endpoint": "https://api.elevenlabs.io", "secret_ref": "secret:res_xxx",\n'
    '   "config": {"default_model": "eleven_v3", "note": "說明"},\n'
    '   "scope_type": "project", "scope_id": "CatsWalker"},\n'
    '  {"op": "grant.create", "resource": "eleven-tts", "scope_type": "team",\n'
    '   "scope_id": "Meow1"},\n'
    '  {"op": "settings.timezone", "timezone": "Asia/Taipei"}\n'
    ']}\n'
    '```\n'
    "op 只有四種：resource.create / resource.update / grant.create / "
    "settings.timezone。kind ∈ llm|mcp|api|skill|git|image|video|music|tts|stt。"
    "config 常用鍵：default_model、mcp_transport、mcp_command、mcp_url、"
    "auth_header、skill_source、git_provider、note。\n"
    "規則：(1) 你只能提出，套用由人按按鈕完成，不要宣稱已設定；(2) secret_ref "
    "只能是 secret:<憑證id> 指標，缺憑證就先請使用者到 管理→憑證 建立，欄位留空；"
    "(3) 不確定欄位就先問，不要猜端點。細節見 bastet-config skill 指南。\n"
)


# ---- parsing and applying --------------------------------------------------------

def extract_actions(text: str) -> list[dict[str, Any]] | None:
    """Pull the proposal out of an assistant message, tolerantly but strictly:
    the LAST well-formed fenced block wins, anything malformed is None (the UI
    then shows nothing rather than a broken apply card)."""
    marker = "```" + FENCE
    if marker not in (text or ""):
        return None
    chunk = text.rsplit(marker, 1)[1]
    if "```" not in chunk:
        return None
    body = chunk.split("```", 1)[0].strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list) or not actions:
        return None
    return actions[:MAX_ACTIONS]


def _find_resource(db: Db, ref: str):
    return db.one("SELECT * FROM resources WHERE id=? OR name=?", (ref, ref))


def _check_scope(db: Db, scope_type: str, scope_id: str) -> str | None:
    if scope_type == "global":
        return None
    if scope_type not in ("team", "project"):
        return f"scope_type 必須是 global/team/project，不是 {scope_type!r}"
    if not scope_id:
        return "team/project 範圍需要指定 id"
    if scope_type == "project":
        if not db.one("SELECT id FROM projects WHERE id=?", (scope_id,)):
            return f"project {scope_id!r} 不存在"
        return None
    # Teams are AMOS org objects — there is NO local `teams` table, which the
    # first live apply discovered the hard way (`no such table: teams`). What
    # Bastet knows locally is which teams its projects reference; a team no
    # project references yet is still legal (the rest of the product accepts
    # it), so unknown is not an error here.
    return None


def apply(db: Db, home_root, actions: list[dict[str, Any]], actor: str) -> list[dict]:
    """Execute a proposal. `actor` is the human who pressed the button.

    Per-action results, never all-or-nothing: a five-action proposal with one
    typo should land the four good ones and say precisely what the fifth needs.
    Every action writes its own audit row (actor = the person, with
    `via: "chat"` so the trail shows how it happened)."""
    results: list[dict] = []
    for action in actions[:MAX_ACTIONS]:
        op = action.get("op", "")
        try:
            if op not in ALLOWED_OPS:
                raise ValueError(f"不支援的動作 {op!r}（允許：{', '.join(ALLOWED_OPS)}）")
            results.append(_apply_one(db, home_root, action, actor))
        except Exception as exc:
            results.append({"op": op or "?", "status": "failed",
                            "detail": str(exc)[:300]})
    return results


def _apply_one(db: Db, home_root, action: dict[str, Any], actor: str) -> dict:
    op = action["op"]
    ts = now()

    if op == "settings.timezone":
        from . import settings as settings_mod
        zone = action.get("timezone", "")
        if not settings_mod.valid_timezone(zone):
            raise ValueError(f"未知的時區 {zone!r}")
        from .config import Home
        home = Home(home_root)
        config = home.config()
        config["timezone"] = zone
        home.save_config(config)
        db.audit(actor, "settings.timezone", "settings", "timezone",
                 {"to": zone, "via": "chat"})
        return {"op": op, "status": "ok", "detail": zone}

    if op == "grant.create":
        row = _find_resource(db, action.get("resource", ""))
        if row is None:
            raise ValueError(f"資源 {action.get('resource')!r} 不存在")
        scope_type = action.get("scope_type", "")
        scope_id = action.get("scope_id", "*")
        problem = _check_scope(db, scope_type, scope_id)
        if problem:
            raise ValueError(problem)
        if db.one("SELECT id FROM grants WHERE resource_id=? AND scope_type=? "
                  "AND scope_id=?", (row["id"], scope_type, scope_id)):
            return {"op": op, "status": "ok", "detail": "已存在，未重複建立"}
        gid = new_id("grt")
        db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, "
                 "created_at) VALUES(?,?,?,?,?)",
                 (gid, row["id"], scope_type, scope_id, ts))
        db.audit(actor, "grant.create", "grant", gid,
                 {"resource": row["name"], "scope": f"{scope_type}:{scope_id}",
                  "via": "chat"})
        return {"op": op, "status": "ok",
                "detail": f"{row['name']} → {scope_type}:{scope_id}"}

    # resource.create / resource.update
    kind = action.get("kind", "")
    config = {k: v for k, v in (action.get("config") or {}).items()
              if k in rk.CONFIG_FIELDS or k == "note"}
    secrets_store.reject_secrets_in_config(config)
    raw_ref = action.get("secret_ref") or ""
    if raw_ref and not raw_ref.startswith("secret:"):
        # secret: pointers ONLY — stricter than the admin UI on purpose (review
        # finding). A raw key has already been through the model; and a
        # model-proposed file:/env: ref could point a "credential" at an
        # arbitrary host file (file:~/.bastet/api_token) which a run would then
        # send to whatever endpoint the same proposal named. The saved-credential
        # indirection is the whole safety story here.
        raise ValueError("經由對話設定時 secret_ref 只能是 secret:<憑證id>（管理→憑證 "
                         "建立後取得）。金鑰原文與 file:/env:/keyring: 指標都不收。")

    if op == "resource.create":
        if kind not in rk.BY_ID:
            raise ValueError(f"unknown kind {kind!r}")
        if kind == "secret":
            raise ValueError("憑證不能經由對話建立 —— 值會流經模型。請用 管理→憑證。")
        name = (action.get("name") or "").strip()
        if not name:
            raise ValueError("resource.create 需要 name")
        if _find_resource(db, name) is not None:
            raise ValueError(f"名稱 {name!r} 已存在（要改用 resource.update 嗎？）")
        scope_type = action.get("scope_type") or ""
        if scope_type:
            problem = _check_scope(db, scope_type, action.get("scope_id", ""))
            if problem:
                raise ValueError(problem)
        rid = new_id("res")
        if raw_ref:
            secrets_store.expand(db, raw_ref)     # raises if the pointer is dangling
        secret_ref = raw_ref or None
        problems = rk.validate(kind, action.get("endpoint", ""), secret_ref, config)
        db.write("INSERT INTO resources(id, kind, name, endpoint, api_flavor, "
                 "secret_ref, config_json, created_at, updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (rid, kind, name, action.get("endpoint", ""),
                  action.get("api_flavor"), secret_ref, json.dumps(config), ts, ts))
        if scope_type:
            db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, "
                     "created_at) VALUES(?,?,?,?,?)",
                     (new_id("grt"), rid, scope_type,
                      action.get("scope_id") or "*", ts))
        db.audit(actor, "resource.create", "resource", rid,
                 {"kind": kind, "name": name, "via": "chat",
                  "problems": problems})
        detail = f"{kind}/{name}"
        if problems:
            detail += f"（提醒：{'；'.join(problems)}）"
        return {"op": op, "status": "ok", "detail": detail, "id": rid}

    # resource.update
    row = _find_resource(db, action.get("id") or action.get("name") or "")
    if row is None:
        raise ValueError(f"資源 {action.get('id') or action.get('name')!r} 不存在")
    if row["kind"] == "secret":
        # rewriting a credential row's ref through a model proposal would let a
        # poisoned conversation redirect every resource that points at it
        raise ValueError("憑證不能經由對話修改 —— 請用 管理→憑證。")
    if row["name"] == SKILL_NAME:
        # the guide every agent reads must not be redirectable by a proposal —
        # pointing skill_source at attacker-chosen text would poison the next
        # conversation's instructions
        raise ValueError("內建的 bastet-config skill 不能經由對話修改。")
    merged = json.loads(row["config_json"] or "{}")
    merged.update(config)
    db.write("UPDATE resources SET endpoint=COALESCE(?, endpoint), "
             "api_flavor=COALESCE(?, api_flavor), "
             "secret_ref=COALESCE(?, secret_ref), config_json=?, updated_at=? "
             "WHERE id=?",
             (action.get("endpoint"), action.get("api_flavor"),
              raw_ref or None, json.dumps(merged), ts, row["id"]))
    db.audit(actor, "resource.update", "resource", row["id"],
             {"name": row["name"], "via": "chat",
              "fields": sorted(set(action) - {"op"})})
    return {"op": op, "status": "ok", "detail": row["name"]}
