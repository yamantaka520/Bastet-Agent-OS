"""Chat: the human end of the loop.

Runs are the machine side of Bastet; this is where a person says what the
project actually needs, drops in a spec or a screenshot, argues about the plan,
and authorises the next step. That makes three requirements non-negotiable:

* **It hangs off a real project.** A session's scope is a real project / team /
  global row, so what is discussed and what is executed cannot drift apart.
* **The discussion becomes durable.** Each turn is written to Agent Memory OS in
  the session's scope, so the next run's context pack already knows about it.
* **It can act.** A session can dispatch a job and approve a blocked gate, and
  every such action is audited with the message that caused it.

Two responder kinds: an `agent` (its executor answers, read-only, with the
project's repo in view) or a pool `resource` LLM (a direct metered call). We
never fake precision — token usage is recorded when the provider reports it.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import secrets_store
from .db import new_id, now

log = logging.getLogger("bastet.chat")

HISTORY_TURNS = 24            # what the responder sees
TEXT_INLINE_LIMIT = 20_000    # per text attachment
IMAGE_INLINE_LIMIT = 4_000_000
CHAT_TIMEOUT_S = 180
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".csv",
                 ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs",
                 ".c", ".h", ".cpp", ".sql", ".sh", ".toml", ".ini", ".cfg",
                 ".html", ".css", ".xml", ".log", ".rst"}

SCOPES = ("global", "team", "project")

# who owns a chat turn in AMOS: the control plane, not any one agent — the
# memory outlives whichever agent happened to answer
MEMORY_OWNER = "bastet"


class ChatError(Exception):
    pass


@dataclass
class Responder:
    kind: str          # agent | resource
    id: str
    label: str


# ---- sessions --------------------------------------------------------------------

def create_session(db, *, scope_type: str, scope_id: str, responder_kind: str,
                   responder_id: str, title: str = "", channel: str = "web",
                   actor: str = "", config: dict[str, Any] | None = None) -> str:
    if scope_type not in SCOPES:
        raise ChatError(f"scope must be one of {SCOPES}")
    if scope_type != "global" and not scope_id:
        raise ChatError("team/project scope needs an id")
    if scope_type == "project" and db.one("SELECT id FROM projects WHERE id=?",
                                          (scope_id,)) is None:
        raise ChatError(f"project {scope_id} does not exist")   # no orphan sessions
    _responder(db, responder_kind, responder_id)                # validates
    session_id = new_id("cht")
    ts = now()
    db.write("INSERT INTO chat_sessions(id, scope_type, scope_id, title, "
             "responder_kind, responder_id, channel, config_json, created_by, "
             "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (session_id, scope_type, scope_id or "*", title or _default_title(scope_id),
              responder_kind, responder_id, channel, json.dumps(config or {}),
              actor, ts, ts))
    db.audit(actor or "system", "chat.session.create", "chat", session_id,
             {"scope": f"{scope_type}:{scope_id or '*'}",
              "responder": f"{responder_kind}:{responder_id}", "channel": channel})
    return session_id


def _default_title(scope_id: str) -> str:
    return f"{scope_id or 'global'} · chat"


def update_session(db, session_id: str, *, title: str | None = None,
                   responder_kind: str | None = None,
                   responder_id: str | None = None, actor: str = "") -> None:
    row = get_session(db, session_id)
    kind = responder_kind or row["responder_kind"]
    rid = responder_id or row["responder_id"]
    if responder_kind or responder_id:
        _responder(db, kind, rid)
    db.write("UPDATE chat_sessions SET title=?, responder_kind=?, responder_id=?, "
             "updated_at=? WHERE id=?",
             (title if title is not None else row["title"], kind, rid, now(), session_id))
    db.audit(actor or "system", "chat.session.update", "chat", session_id,
             {"responder": f"{kind}:{rid}"})


def get_session(db, session_id: str):
    row = db.one("SELECT * FROM chat_sessions WHERE id=?", (session_id,))
    if row is None:
        raise ChatError("session not found")
    return row


def list_sessions(db, scope_type: str | None = None,
                  scope_id: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT s.*, (SELECT COUNT(*) FROM chat_messages m "
           "WHERE m.session_id = s.id) AS messages FROM chat_sessions s")
    params: list[Any] = []
    if scope_type:
        sql += " WHERE s.scope_type=?"
        params.append(scope_type)
        if scope_id:
            sql += " AND s.scope_id=?"
            params.append(scope_id)
    sql += " ORDER BY s.updated_at DESC"
    return [dict(r) for r in db.query(sql, tuple(params))]


def delete_session(db, session_id: str, actor: str = "") -> None:
    get_session(db, session_id)
    db.write("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
    db.write("DELETE FROM chat_sessions WHERE id=?", (session_id,))
    db.audit(actor or "system", "chat.session.delete", "chat", session_id, {})


def find_or_create_channel_session(db, *, channel: str, external_id: str,
                                   scope_type: str, scope_id: str,
                                   responder_kind: str, responder_id: str,
                                   title: str, actor: str = "") -> str:
    """One session per external conversation (e.g. a Telegram user), so the
    thread survives restarts instead of starting over each message."""
    for row in db.query("SELECT * FROM chat_sessions WHERE channel=?", (channel,)):
        if json.loads(row["config_json"] or "{}").get("external_id") == external_id:
            if (row["responder_kind"], row["responder_id"]) != (responder_kind,
                                                                responder_id):
                update_session(db, row["id"], responder_kind=responder_kind,
                               responder_id=responder_id, actor=actor)
            return row["id"]
    return create_session(db, scope_type=scope_type, scope_id=scope_id,
                          responder_kind=responder_kind, responder_id=responder_id,
                          title=title, channel=channel, actor=actor,
                          config={"external_id": external_id})


# ---- messages --------------------------------------------------------------------

def add_message(db, session_id: str, *, role: str, content: str, author: str = "",
                attachments: list[dict[str, Any]] | None = None,
                meta: dict[str, Any] | None = None) -> str:
    get_session(db, session_id)
    message_id = new_id("msg")
    db.write("INSERT INTO chat_messages(id, session_id, role, author, content, "
             "attachments_json, meta_json, at) VALUES(?,?,?,?,?,?,?,?)",
             (message_id, session_id, role, author, content,
              json.dumps(attachments or []), json.dumps(meta or {}), now()))
    db.write("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now(), session_id))
    return message_id


def messages(db, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM chat_messages WHERE session_id=? "
                    "ORDER BY at DESC, rowid DESC LIMIT ?", (session_id, limit))
    out = []
    for r in reversed(rows):
        item = dict(r)
        item["attachments"] = json.loads(item.pop("attachments_json") or "[]")
        item["meta"] = json.loads(item.pop("meta_json") or "{}")
        out.append(item)
    return out


# ---- attachments -----------------------------------------------------------------

def attachment_dir(home_root: Path | str, session_id: str) -> Path:
    return Path(home_root) / "chat" / session_id


def save_attachment(home_root: Path | str, session_id: str, filename: str,
                    data: bytes) -> dict[str, Any]:
    directory = attachment_dir(home_root, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name.replace("/", "_") or "file"
    file_id = new_id("att")
    path = directory / f"{file_id}-{safe}"
    path.write_bytes(data)
    kind = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    return {"id": file_id, "name": safe, "size": len(data), "mime": kind,
            "path": str(path)}


def _attachment_text(item: dict[str, Any]) -> str | None:
    path = Path(item.get("path", ""))
    if not path.exists():
        return None
    if path.suffix.lower() in TEXT_SUFFIXES or item.get("mime", "").startswith("text/"):
        try:
            return path.read_text(errors="replace")[:TEXT_INLINE_LIMIT]
        except OSError:
            return None
    return None


def _attachment_image(item: dict[str, Any]) -> tuple[str, str] | None:
    path = Path(item.get("path", ""))
    mime = item.get("mime", "")
    if not (path.exists() and mime.startswith("image/")):
        return None
    if item.get("size", 0) > IMAGE_INLINE_LIMIT:
        return None
    return mime, base64.b64encode(path.read_bytes()).decode()


# ---- context ---------------------------------------------------------------------

def _team_of(db, project_id: str) -> str:
    row = db.one("SELECT team_id FROM projects WHERE id=?", (project_id,))
    return row["team_id"] if row else ""


def system_prompt(db, session) -> str:
    """What the responder needs to be useful: the scope it is talking about, the
    project's real state, and what it is allowed to do here."""
    scope_type, scope_id = session["scope_type"], session["scope_id"]
    parts = [
        "你是 Bastet Agent OS 的專案對話助理。使用者透過這個對話進行專案規劃、"
        "討論執行細節、提供檔案與需求，並在這裡授權下一步動作。",
        f"## 對話範圍\n{scope_type}：{scope_id}",
    ]
    if scope_type == "project":
        project = db.one("SELECT * FROM projects WHERE id=?", (scope_id,))
        if project is not None:
            config = json.loads(project["config_json"] or "{}")
            lines = [f"- id：{project['id']}", f"- team：{project['team_id']}",
                     f"- repo（在 Bastet 主機上）：{project['repo_path'] or '未設定'}"]
            if config.get("description"):
                lines.append(f"- 說明：{config['description']}")
            if project["default_template_id"]:
                lines.append(f"- 套用的工作流：{project['default_template_id']}")
            parts.append("## 專案\n" + "\n".join(lines))
        roles = db.query("SELECT par.role, a.name FROM project_agent_roles par "
                         "JOIN agents a ON a.id = par.agent_id WHERE par.project_id=? "
                         "ORDER BY par.role", (scope_id,))
        if roles:
            parts.append("## 專案團隊\n" + "\n".join(
                f"- {r['role']}：{r['name']}" for r in roles))
        jobs = db.query("SELECT id, title, stage, status FROM jobs WHERE project_id=? "
                        "ORDER BY updated_at DESC LIMIT 8", (scope_id,))
        if jobs:
            parts.append("## 近期任務\n" + "\n".join(
                f"- {j['id']}｜{j['title']}｜{j['stage']}｜{j['status']}" for j in jobs))
        from . import resource_access
        pool = resource_access.visible(db, scope_id, _team_of(db, scope_id))
        if pool:
            parts.append("## 此專案可用的資源池物件\n" + "\n".join(
                f"- {r['name']}（{r['kind']}）" for r in pool))
    recall = _memory_recall(db, session)
    if recall:
        parts.append("## 團隊記憶（AMOS 召回）\n" + recall)
    parts.append("## 你可以做什麼\n"
                 "- 幫使用者把需求整理成可執行的任務規格（明確的驗收條件）\n"
                 "- 指出缺少的資訊、風險與前置條件\n"
                 "- 使用者說「派工/開始執行」時，回覆一份可直接送出的任務規格；"
                 "實際派工與批准由使用者在介面上按下確認，你不會自己執行\n"
                 "誠實優先：不確定就說不確定，不要編造專案內容或檔案。")
    from .self_config import PROMPT_NOTE
    parts.append(PROMPT_NOTE.strip())
    return "\n\n".join(parts)


def _memory_recall(db, session, query: str = "") -> str:
    try:
        from agent_memory_os.client import MemoryClient
    except Exception:
        return ""
    scope_type, scope_id = session["scope_type"], session["scope_id"]
    try:
        client = MemoryClient()
        # AMOS `scope` is the bare level; the id lives in the visibility grant,
        # so narrow by level upstream and by grant here
        kwargs: dict[str, Any] = {"limit": 12}
        if scope_type in ("project", "team"):
            kwargs["scope"] = scope_type
        hits = client.search(query or session["title"] or "project planning", **kwargs)
    except Exception as exc:                      # AMOS is optional at runtime
        log.info("chat memory recall skipped: %s", type(exc).__name__)
        return ""
    grant = f"{scope_type}:{scope_id}" if scope_type != "global" else ""
    lines = []
    for hit in hits or []:
        record = getattr(hit, "record", hit)
        if isinstance(record, dict):
            content, grants = record.get("content", ""), record.get("visibility") or []
        else:
            content = getattr(record, "content", "") or ""
            grants = getattr(record, "visibility", None) or []
        if grant and grant not in grants:
            continue
        if content:
            lines.append(f"- {content[:300]}")
        if len(lines) >= 6:
            break
    return "\n".join(lines)


def remember(db, session, role: str, content: str) -> bool:
    """Write a turn into AMOS so later runs inherit the decision.

    AMOS scope is the bare level (`project`/`team`/`global`) and the id travels
    as a visibility grant (`project:<id>`) — that grant is what gates which
    agents can recall it. Passing `project_id=` as a keyword raises TypeError,
    which this function used to swallow: every chat turn looked remembered and
    none of it was."""
    if not content.strip():
        return False
    try:
        from agent_memory_os.client import MemoryClient
    except Exception:
        return False
    scope_type, scope_id = session["scope_type"], session["scope_id"]
    kwargs: dict[str, Any] = {"scope": "global"}
    if scope_type == "project":
        team = _team_of(db, scope_id)
        grants = [f"project:{scope_id}"] + ([f"team:{team}"] if team else [])
        kwargs = {"scope": "project", "visibility": grants}
    elif scope_type == "team":
        kwargs = {"scope": "team", "visibility": [f"team:{scope_id}"]}
    try:
        MemoryClient().add(f"[chat/{role}] {content[:2000]}", type="note",
                           owner=MEMORY_OWNER, **kwargs)
        return True
    except Exception as exc:
        log.warning("chat memory write failed (%s): %s", type(exc).__name__, exc)
        return False


# ---- responders ------------------------------------------------------------------

def _responder(db, kind: str, responder_id: str) -> Responder:
    if kind == "agent":
        row = db.one("SELECT * FROM agents WHERE id=?", (responder_id,))
        if row is None:
            raise ChatError(f"agent {responder_id} not found")
        return Responder("agent", responder_id, row["name"])
    if kind == "resource":
        row = db.one("SELECT * FROM resources WHERE id=? AND kind='llm'",
                     (responder_id,))
        if row is None:
            raise ChatError(f"llm resource {responder_id} not found")
        return Responder("resource", responder_id, row["name"])
    raise ChatError("responder must be agent or resource")


def responders(db) -> list[dict[str, Any]]:
    """What the dropdown offers: enabled agents + pool LLMs."""
    out = [{"kind": "agent", "id": r["id"], "label": r["name"],
            "detail": r["executor_type"]}
           for r in db.query("SELECT * FROM agents WHERE enabled=1 ORDER BY name")]
    out += [{"kind": "resource", "id": r["id"], "label": r["name"],
             "detail": json.loads(r["config_json"] or "{}").get("default_model")
                       or r["api_flavor"] or "llm"}
            for r in db.query("SELECT * FROM resources WHERE kind='llm' AND enabled=1 "
                              "ORDER BY name")]
    return out


async def reply(db, home_root: Path | str, session_id: str,
                actor: str = "") -> dict[str, Any]:
    """Answer the latest user message. Returns the stored assistant message."""
    session = get_session(db, session_id)
    history = messages(db, session_id, limit=HISTORY_TURNS)
    if not history:
        raise ChatError("nothing to reply to")
    responder = _responder(db, session["responder_kind"], session["responder_id"])

    if responder.kind == "resource":
        text, meta = await _reply_resource(db, session, history, responder)
    else:
        text, meta = await _reply_agent(db, home_root, session, history, responder)

    message_id = add_message(db, session_id, role="assistant", content=text,
                             author=f"{responder.kind}:{responder.id}", meta=meta)
    remember(db, session, "assistant", text)
    db.audit(actor or "chat", "chat.reply", "chat", session_id,
             {"responder": f"{responder.kind}:{responder.id}",
              "cost_usd": meta.get("cost_usd"), "model": meta.get("model")})
    return next(m for m in messages(db, session_id, limit=2) if m["id"] == message_id)


def _wire_messages(db, session, history: list[dict[str, Any]],
                   flavor: str) -> list[dict[str, Any]]:
    """Transcript in provider shape, attachments folded in: text inlined,
    images attached when the wire supports them."""
    out: list[dict[str, Any]] = []
    for msg in history:
        if msg["role"] == "system":
            continue
        blocks: list[dict[str, Any]] = []
        text = msg["content"]
        for item in msg["attachments"]:
            inline = _attachment_text(item)
            if inline is not None:
                text += f"\n\n<file name=\"{item['name']}\">\n{inline}\n</file>"
                continue
            image = _attachment_image(item)
            if image is None:
                text += f"\n\n[附件：{item['name']}（{item.get('mime')}，"\
                        f"{item.get('size', 0)} bytes）— 內容未內嵌]"
                continue
            mime, b64 = image
            if flavor == "anthropic":
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": b64}})
            else:
                blocks.append({"type": "image_url",
                               "image_url": {"url": f"data:{mime};base64,{b64}"}})
        if blocks:
            text_block = ({"type": "text", "text": text} if flavor == "anthropic"
                          else {"type": "text", "text": text})
            out.append({"role": msg["role"], "content": [text_block, *blocks]})
        else:
            out.append({"role": msg["role"], "content": text})
    return out


async def _reply_resource(db, session, history, responder) -> tuple[str, dict]:
    resource = db.one("SELECT * FROM resources WHERE id=?", (responder.id,))
    config = json.loads(resource["config_json"] or "{}")
    from .resource_kinds import base_endpoint

    base, _ = base_endpoint(resource["endpoint"])
    if not base:
        raise ChatError("this LLM resource has no endpoint")
    flavor = (resource["api_flavor"] or "openai").lower()
    model = config.get("default_model")
    if not model:
        raise ChatError("this LLM resource has no default model — set one to chat with it")
    try:
        key = secrets_store.resolve(secrets_store.expand(db, resource["secret_ref"] or ""))
    except secrets_store.SecretError as exc:
        raise ChatError(f"credential error: {exc}") from exc

    wire = _wire_messages(db, session, history, flavor)
    prompt = system_prompt(db, session)
    if flavor == "anthropic":
        url = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
        payload = {"model": model, "max_tokens": 4096, "system": prompt,
                   "messages": wire}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url = (f"{base}/chat/completions" if base.endswith("/v1")
               else f"{base}/v1/chat/completions")
        payload = {"model": model,
                   "messages": [{"role": "system", "content": prompt}, *wire]}
        headers = {"Authorization": f"Bearer {key}"}

    async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ChatError(f"upstream error: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise ChatError(f"upstream HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()

    if flavor == "anthropic":
        text = "".join(block.get("text", "") for block in body.get("content", [])
                       if block.get("type") == "text")
        usage = body.get("usage") or {}
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)
    else:
        choices = body.get("choices") or [{}]
        text = ((choices[0].get("message") or {}).get("content")) or ""
        usage = body.get("usage") or {}
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)

    meta: dict[str, Any] = {"responder": "resource", "resource_id": responder.id,
                            "model": body.get("model") or model,
                            "tokens_in": tokens_in, "tokens_out": tokens_out,
                            "precision": "reported" if usage else "none"}
    if usage:
        from .pricing import PriceBook, Usage
        meta["cost_usd"] = PriceBook().cost_usd(
            meta["model"], Usage(tokens_in=tokens_in, tokens_out=tokens_out))
    return (text or "(empty response)"), meta


async def _reply_agent(db, home_root, session, history, responder) -> tuple[str, dict]:
    """The agent's own executor answers: same account, same model, and it can
    read the project's repo — a read-only run with the transcript as its task."""
    from .executors.base import TaskSpec, get_executor

    agent = db.one("SELECT * FROM agents WHERE id=?", (responder.id,))
    workdir = str(home_root)
    if session["scope_type"] == "project":
        project = db.one("SELECT repo_path FROM projects WHERE id=?",
                         (session["scope_id"],))
        if project is not None and project["repo_path"]:
            from .config import expand_repo_path
            candidate = Path(expand_repo_path(project["repo_path"]))
            if candidate.is_dir():
                workdir = str(candidate)

    transcript = "\n\n".join(
        f"### {m['role']}\n{m['content']}"
        + "".join(f"\n\n<file name=\"{a['name']}\">\n{_attachment_text(a) or ''}\n</file>"
                  for a in m["attachments"])
        for m in history if m["role"] != "system")
    agent_cfg = json.loads(agent["config_json"] or "{}")
    extra_env: dict[str, str] = {}
    if agent["account_id"] if "account_id" in agent.keys() else None:
        from .executors.accounts import account_env
        account = db.one("SELECT * FROM executor_accounts WHERE id=?",
                         (agent["account_id"],))
        if account is not None:
            extra_env = account_env(agent["executor_type"], account["home_dir"])

    spec = TaskSpec(
        run_id=new_id("chat"),
        prompt=f"{system_prompt(db, session)}\n\n## 對話紀錄\n{transcript}\n\n"
               f"請以繁體中文（或使用者的語言）回覆最後一則訊息。只輸出回覆內容。",
        workdir=workdir,
        timeout_s=CHAT_TIMEOUT_S,
        read_only=True,            # chat never writes: it is a conversation
        llm={"model": agent_cfg.get("model")} if agent_cfg.get("model") else None,
        extra_env=extra_env,
        isolation="chat",          # not a run: no worktree, no container wrap
    )
    executor = get_executor(agent["executor_type"])
    handle = await executor.start(spec)
    async for _ in executor.stream(handle):
        pass
    result = await executor.result(handle)
    if result.status != "succeeded" and not result.summary:
        raise ChatError(f"agent run {result.status} with no output")
    meta = {"responder": "agent", "agent_id": responder.id,
            "executor": agent["executor_type"], "status": result.status,
            "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
            "cost_usd": result.cost_usd,
            "precision": result.precision or "none"}
    return (result.summary or "(empty response)"), meta
