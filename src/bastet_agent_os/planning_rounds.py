"""Durable customer planning rounds and next-round intake."""

from __future__ import annotations

import json
from typing import Any

from .db import new_id, now

ROUND_STATES = ("discovery", "analysis", "proposed", "approved", "frozen",
                "executing", "accepted")
INTAKE_KINDS = ("defect", "suggestion", "idea")


class PlanningRoundError(Exception):
    pass


def current(db, project_id: str):
    return db.one("SELECT * FROM planning_rounds WHERE project_id=? "
                  "ORDER BY ordinal DESC LIMIT 1", (project_id,))


def start(db, project_id: str, session_id: str, actor: str = "") -> str:
    if db.one("SELECT id FROM projects WHERE id=?", (project_id,)) is None:
        raise PlanningRoundError("project not found")
    previous = current(db, project_id)
    if previous is not None and previous["state"] != "accepted":
        raise PlanningRoundError(
            "目前規劃輪次尚未驗收；新需求請先放入下一輪等待區")
    session = db.one("SELECT * FROM chat_sessions WHERE id=?", (session_id,))
    if session is None or session["scope_type"] != "project" or \
            session["scope_id"] != project_id:
        raise PlanningRoundError("planning session must belong to the project")
    if session["state"] != "open":
        raise PlanningRoundError("planning session must be open")
    ordinal = int(previous["ordinal"]) + 1 if previous is not None else 1
    round_id, ts = new_id("rnd"), now()
    pending = [dict(row) for row in db.query(
        "SELECT * FROM planning_intake WHERE project_id=? AND consumed_at IS NULL "
        "ORDER BY created_at", (project_id,))]
    db.write_many([
        ("INSERT INTO planning_rounds(id, project_id, ordinal, session_id, state, "
         "created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
         (round_id, project_id, ordinal, session_id, "discovery", actor, ts, ts)),
        ("UPDATE chat_sessions SET planning_round_id=?, updated_at=? WHERE id=?",
         (round_id, ts, session_id)),
        ("UPDATE planning_intake SET consumed_by_round_id=?, consumed_at=? "
         "WHERE project_id=? AND consumed_at IS NULL", (round_id, ts, project_id)),
    ])
    if pending:
        summary = "\n".join(f"- [{item['kind']}] {item['content']}" for item in pending)
        from . import chat
        chat.add_message(db, session_id, role="system", author="planning-round",
                         content="## 上一輪等待區\n" + summary,
                         meta={"planning_round_id": round_id,
                               "intake_items": len(pending)})
    db.audit(actor or "system", "planning.round.start", "planning_round", round_id,
             {"project": project_id, "ordinal": ordinal,
              "intake_items": len(pending)})
    return round_id


def propose(db, round_id: str, *, solution: str,
            negotiation: list[dict[str, Any]], actor: str = "") -> None:
    row = db.one("SELECT * FROM planning_rounds WHERE id=?", (round_id,))
    if row is None:
        raise PlanningRoundError("planning round not found")
    if row["state"] not in ("discovery", "analysis", "proposed"):
        raise PlanningRoundError("planning round can no longer be changed")
    if not solution.strip():
        raise PlanningRoundError("a concrete solution is required")
    if len(negotiation) > 5:
        raise PlanningRoundError("PM/system-analysis negotiation exceeds five exchanges")
    db.write("UPDATE planning_rounds SET state='proposed', solution_md=?, "
             "negotiation_json=?, updated_at=? WHERE id=?",
             (solution.strip(), json.dumps(negotiation, ensure_ascii=False), now(), round_id))
    db.audit(actor or "system", "planning.round.propose", "planning_round", round_id,
             {"exchanges": len(negotiation)})


def approve(db, project_id: str, tasks: list[dict[str, Any]], actor: str = "") -> str:
    row = current(db, project_id)
    if row is None or row["state"] != "proposed":
        raise PlanningRoundError("方案與系統分析結論完成後才能確認任務圖")
    ts = now()
    summary = row["solution_md"].strip()
    db.write_many([
        ("UPDATE planning_rounds SET state='frozen', final_summary_md=?, "
         "task_graph_json=?, approved_at=?, updated_at=? WHERE id=?",
         (summary, json.dumps(tasks, ensure_ascii=False), ts, ts, row["id"])),
        ("UPDATE chat_sessions SET state='frozen', updated_at=? WHERE id=?",
         (ts, row["session_id"])),
    ])
    db.audit(actor or "system", "planning.round.freeze", "planning_round", row["id"],
             {"tasks": len(tasks), "session": row["session_id"]})
    return row["id"]


def add_intake(db, project_id: str, *, kind: str, content: str,
               actor: str = "", attachments: list[dict] | None = None) -> str:
    if kind not in INTAKE_KINDS:
        raise PlanningRoundError(f"kind must be one of {INTAKE_KINDS}")
    if not content.strip():
        raise PlanningRoundError("intake content is required")
    item_id = new_id("intake")
    db.write("INSERT INTO planning_intake(id, project_id, kind, content, "
             "attachments_json, created_by, created_at) VALUES(?,?,?,?,?,?,?)",
             (item_id, project_id, kind, content.strip(),
              json.dumps(attachments or [], ensure_ascii=False), actor, now()))
    db.audit(actor or "system", "planning.intake.add", "planning_intake", item_id,
             {"project": project_id, "kind": kind})
    return item_id


def overview(db, project_id: str) -> dict[str, Any]:
    row = current(db, project_id)
    intake = [dict(item) for item in db.query(
        "SELECT * FROM planning_intake WHERE project_id=? AND consumed_at IS NULL "
        "ORDER BY created_at", (project_id,))]
    result = dict(row) if row is not None else None
    if result:
        result["negotiation"] = json.loads(result.pop("negotiation_json") or "[]")
        result["task_graph"] = json.loads(result.pop("task_graph_json") or "[]")
    return {"round": result, "intake": intake}
