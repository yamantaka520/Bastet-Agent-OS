"""Project rooms, stage handoffs, and test-evidence validity.

These records are context inputs, not an alternative source of truth: job and
gate state remain in the workflow tables and every write is audited by callers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .db import new_id, now


def ensure_room(db, project_id: str) -> str:
    row = db.one("SELECT id FROM project_rooms WHERE project_id=?", (project_id,))
    if row:
        return row["id"]
    room_id = new_id("room")
    ts = now()
    db.write("INSERT INTO project_rooms(id,project_id,title,created_at,updated_at) "
             "VALUES(?,?,?,?,?)", (room_id, project_id, f"{project_id} 專案會議室", ts, ts))
    return room_id


def members(db, project_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT DISTINCT a.id,a.name,a.executor_type,par.role,par.preference "
        "FROM project_agent_roles par JOIN agents a ON a.id=par.agent_id "
        "WHERE par.project_id=? ORDER BY par.role,par.preference DESC", (project_id,))]


def post(db, project_id: str, *, author_type: str, author_id: str,
         content: str, kind: str = "message", meta: dict | None = None) -> str:
    room_id = ensure_room(db, project_id)
    message_id = new_id("rmsg")
    stamp = now()
    db.write_many([
        ("INSERT INTO room_messages(id,room_id,author_type,author_id,kind,content,"
         "meta_json,at) VALUES(?,?,?,?,?,?,?,?)",
         (message_id, room_id, author_type, author_id, kind, content,
          json.dumps(meta or {}, ensure_ascii=False), stamp)),
        ("UPDATE project_rooms SET updated_at=? WHERE id=?", (stamp, room_id)),
    ])
    return message_id


def messages(db, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
    room_id = ensure_room(db, project_id)
    rows = db.query("SELECT * FROM room_messages WHERE room_id=? "
                    "ORDER BY at DESC,rowid DESC LIMIT ?", (room_id, limit))
    out = []
    for row in reversed(rows):
        item = dict(row)
        item["meta"] = json.loads(item.pop("meta_json") or "{}")
        out.append(item)
    return out


def changed_paths(workdir: str, base: str = "HEAD^") -> list[str]:
    if not (Path(workdir) / ".git").exists():
        return []
    proc = subprocess.run(["git", "-C", workdir, "diff", "--name-only", base, "HEAD"],
                          capture_output=True, text=True)
    return sorted({p.strip() for p in proc.stdout.splitlines() if p.strip()}) \
        if proc.returncode == 0 else []


def record_handoff(db, *, project_id: str, job_id: str, run_id: str,
                   from_stage: str, to_stage: str | None, agent_id: str,
                   summary: str, paths: list[str], verification: list[str] | None = None,
                   risks: list[str] | None = None) -> str:
    handoff_id = new_id("hnd")
    stamp = now()
    db.write("INSERT INTO stage_handoffs(id,project_id,job_id,run_id,from_stage,to_stage,"
             "agent_id,summary,changed_paths_json,verification_json,risks_json,at) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (handoff_id, project_id, job_id, run_id, from_stage, to_stage, agent_id,
              summary, json.dumps(paths), json.dumps(verification or []),
              json.dumps(risks or []), stamp))
    post(db, project_id, author_type="agent", author_id=agent_id, kind="handoff",
         content=f"{from_stage} → {to_stage or '完成'}\n{summary}",
         meta={"handoff_id": handoff_id, "job_id": job_id, "run_id": run_id,
               "changed_paths": paths, "verification": verification or [],
               "risks": risks or []})
    return handoff_id


def latest_handoffs(db, job_id: str, limit: int = 6) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM stage_handoffs WHERE job_id=? ORDER BY at DESC LIMIT ?",
                    (job_id, limit))
    return [dict(r) for r in reversed(rows)]


def project_handoffs(db, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM stage_handoffs WHERE project_id=? ORDER BY at DESC LIMIT ?",
        (project_id, limit))]


def deliver_handoffs(db, job_id: str, stage: str, agent_id: str) -> list[str]:
    """Record every agent that receives a handoff, including replacements."""
    rows = db.query("SELECT id,project_id FROM stage_handoffs WHERE job_id=? "
                    "AND to_stage=? ORDER BY at", (job_id, stage))
    stamp = now()
    delivered = []
    for row in rows:
        cursor = db.write(
            "INSERT OR IGNORE INTO handoff_receipts(id,handoff_id,job_id,stage,"
            "agent_id,delivered_at) VALUES(?,?,?,?,?,?)",
            (new_id("hrc"), row["id"], job_id, stage, agent_id, stamp))
        if cursor.rowcount:
            delivered.append(row["id"])
            db.write("UPDATE stage_handoffs SET delivered_at=?,"
                     "delivered_to_agent_id=? WHERE id=?",
                     (stamp, agent_id, row["id"]))
    if delivered:
        post(db, rows[0]["project_id"], author_type="system",
             author_id="orchestrator", kind="assignment",
             content=(f"指派 {agent_id} 執行「{stage}」，已交付 "
                      f"{len(delivered)} 份前階段交接：{', '.join(delivered)}。"),
             meta={"job_id": job_id, "stage": stage, "agent_id": agent_id,
                   "handoff_ids": delivered})
    return delivered


def acknowledge_handoff(db, handoff_id: str, *, agent_id: str,
                        acknowledgement: str, questions: list[str] | None = None) -> dict:
    row = db.one("SELECT * FROM stage_handoffs WHERE id=?", (handoff_id,))
    if row is None:
        raise ValueError(f"unknown handoff {handoff_id!r}")
    receipt = db.one("SELECT id FROM handoff_receipts WHERE handoff_id=? "
                     "AND agent_id=? ORDER BY delivered_at DESC LIMIT 1",
                     (handoff_id, agent_id))
    expected = row["delivered_to_agent_id"]
    if receipt is None and expected and expected != agent_id:
        raise ValueError(f"handoff was delivered to {expected}, not {agent_id}")
    if receipt is not None:
        db.write("UPDATE handoff_receipts SET acknowledged_at=?,acknowledgement=?,"
                 "questions_json=? WHERE id=?",
                 (now(), acknowledgement.strip(),
                  json.dumps(questions or [], ensure_ascii=False), receipt["id"]))
    db.write("UPDATE stage_handoffs SET acknowledged_at=?,acknowledged_by=?,"
             "acknowledgement=?,questions_json=? WHERE id=?",
             (now(), agent_id, acknowledgement.strip(),
              json.dumps(questions or [], ensure_ascii=False), handoff_id))
    post(db, row["project_id"], author_type="agent", author_id=agent_id,
         kind="handoff_ack", content=acknowledgement.strip() or "已接收交接",
         meta={"handoff_id": handoff_id, "questions": questions or []})
    return dict(db.one("SELECT * FROM stage_handoffs WHERE id=?", (handoff_id,)))


def acknowledge_delivered_handoffs(db, *, job_id: str, stage: str,
                                    agent_id: str, summary: str) -> list[str]:
    """Close delivered receipts using the receiving Agent's own stage report."""
    rows = db.query(
        "SELECT handoff_id FROM handoff_receipts WHERE job_id=? AND stage=? "
        "AND agent_id=? AND acknowledged_at IS NULL ORDER BY delivered_at",
        (job_id, stage, agent_id))
    acknowledgement = (
        "已接收並完成本階段處理。以下為接收者自己的執行回報：\n"
        + (summary.strip()[:2000] or "（Agent 未提供文字摘要）"))
    for row in rows:
        acknowledge_handoff(db, row["handoff_id"], agent_id=agent_id,
                            acknowledgement=acknowledgement)
    return [row["handoff_id"] for row in rows]


def path_matches(path: str, patterns: list[str]) -> bool:
    from fnmatch import fnmatch
    def root(pattern: str) -> str:
        return pattern[:-3] if pattern.endswith("/**") else pattern.rstrip("/")
    return any(fnmatch(path, pattern) or path.startswith(root(pattern) + "/")
               for pattern in patterns)


def evidence_reusable(evidence, changed: list[str]) -> bool:
    if evidence is None or evidence["verdict"] != "passed":
        return False
    covered = json.loads(evidence["covered_paths_json"] or "[]")
    # No declared coverage means we cannot prove that a change is irrelevant.
    return bool(covered) and not any(path_matches(path, covered) for path in changed)
