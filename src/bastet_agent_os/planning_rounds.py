"""Durable customer planning rounds and next-round intake."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .db import new_id, now

ROUND_STATES = ("discovery", "analysis", "proposed", "approved", "frozen",
                "executing", "accepted")
INTAKE_KINDS = ("defect", "suggestion", "idea")
MAX_NEGOTIATION_EXCHANGES = 5

PM_PROMPT = """\
你是此專案的 PM。根據客戶對話與系統分析的上一輪挑戰，提出或修訂一份具體方案。
方案必須包含：目標與非目標、系統邊界、主要使用者流程、架構/資料契約、可並行工作、
整合點、驗收證據、Git/交付策略、風險與待人類裁決事項。不要拆成正式任務卡，也不要
呼叫工具。只輸出 JSON：
{"solution":"完整方案 Markdown","response":"本輪如何處理上一輪挑戰"}
"""

SA_PROMPT = """\
你是此專案的系統分析師。挑戰 PM 方案，不可照單全收。檢查需求邊界、狀態與資料流、
權限、外部依賴、失敗模式、並行安全、整合順序、驗收可測性、Git/交付完整性與缺少的
角色。若仍有實質缺口就 challenge；只有方案足以產生可執行 DAG 時才 accept。不要
呼叫工具。只輸出 JSON：
{"verdict":"accept|challenge","response":"可顯示給需求者的結論或挑戰",
"issues":["具體缺口"]}
"""


class PlanningRoundError(Exception):
    pass


def _json_payload(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    excerpt = " ".join((text or "").split())[:300] or "(empty output)"
    raise PlanningRoundError(f"planning agent did not return JSON: {excerpt}")


def _role_agent(db, project_id: str, role: str):
    return db.one(
        "SELECT a.* FROM project_agent_roles par JOIN agents a ON a.id=par.agent_id "
        "WHERE par.project_id=? AND par.role=? AND a.enabled=1 "
        "AND a.depleted_at IS NULL ORDER BY par.preference DESC LIMIT 1",
        (project_id, role))


async def _agent_turn(db, agent, *, prompt: str, workdir: str) -> str:
    from .executors.base import TaskSpec, get_executor

    agent_cfg = json.loads(agent["config_json"] or "{}")
    extra_env: dict[str, str] = {}
    if "account_id" in agent.keys() and agent["account_id"]:
        from .executors.accounts import account_env
        account = db.one("SELECT * FROM executor_accounts WHERE id=?",
                         (agent["account_id"],))
        if account is not None:
            extra_env = account_env(agent["executor_type"], account["home_dir"])
    spec = TaskSpec(
        run_id=new_id("planx"), prompt=prompt, workdir=workdir,
        timeout_s=300, read_only=True, isolation="plan", extra_env=extra_env,
        llm={"model": agent_cfg.get("model")} if agent_cfg.get("model") else None)
    executor = get_executor(agent["executor_type"])
    handle = await executor.start(spec)
    async for _ in executor.stream(handle):
        pass
    result = await executor.result(handle)
    if result.status != "succeeded" or not result.summary:
        raise PlanningRoundError(
            f"{agent['name']} planning turn failed ({result.status})")
    return result.summary


async def negotiate(db, home_root, round_id: str, actor: str = "",
                    on_exchange: Callable[[int, str], None] | None = None
                    ) -> dict[str, Any]:
    """Run visible, bounded PM ↔ system-analysis convergence."""
    row = db.one("SELECT * FROM planning_rounds WHERE id=?", (round_id,))
    if row is None:
        raise PlanningRoundError("planning round not found")
    if row["state"] not in ("discovery", "analysis"):
        raise PlanningRoundError("planning negotiation is not available in this state")
    pm = _role_agent(db, row["project_id"], "pm")
    analyst = _role_agent(db, row["project_id"], "system-analyst")
    if pm is None or analyst is None:
        missing = "pm" if pm is None else "system-analyst"
        raise PlanningRoundError(f"project has no enabled {missing} agent")
    project = db.one("SELECT repo_path FROM projects WHERE id=?", (row["project_id"],))
    from .config import expand_repo_path
    candidate = expand_repo_path(project["repo_path"]) if project else ""
    workdir = candidate if candidate and Path(candidate).is_dir() else str(home_root)
    from . import chat
    from .project_runner import _planning_context

    context = _planning_context(db, row["project_id"])
    exchanges: list[dict[str, Any]] = json.loads(row["negotiation_json"] or "[]")
    if len(exchanges) >= MAX_NEGOTIATION_EXCHANGES:
        raise PlanningRoundError("PM 與系統分析已用完五輪協商，需需求者裁決")
    issues = list(exchanges[-1].get("issues") or []) if exchanges else []
    solution = row["solution_md"] or ""
    db.write("UPDATE planning_rounds SET state='analysis', updated_at=? WHERE id=?",
             (now(), round_id))
    for number in range(len(exchanges) + 1, MAX_NEGOTIATION_EXCHANGES + 1):
        pm_raw = await _agent_turn(
            db, pm, workdir=workdir,
            prompt=f"{PM_PROMPT}\n\n## 專案上下文\n{context}\n\n"
                   f"## 上輪系統分析缺口\n{json.dumps(issues, ensure_ascii=False)}\n\n"
                   f"## 現有方案\n{solution or '（第一輪）'}")
        pm_payload = _json_payload(pm_raw)
        solution = str(pm_payload.get("solution") or "").strip()
        if not solution:
            raise PlanningRoundError("PM returned no concrete solution")
        pm_response = str(pm_payload.get("response") or solution).strip()
        chat.add_message(db, row["session_id"], role="assistant", author=pm["name"],
                         content=f"## PM 方案（第 {number} 輪）\n{pm_response}\n\n{solution}",
                         meta={"planning_round_id": round_id, "exchange": number,
                               "planning_role": "pm"})

        sa_raw = await _agent_turn(
            db, analyst, workdir=workdir,
            prompt=f"{SA_PROMPT}\n\n## 專案上下文\n{context}\n\n## PM 方案\n{solution}")
        sa_payload = _json_payload(sa_raw)
        verdict = str(sa_payload.get("verdict") or "").lower()
        if verdict not in ("accept", "challenge"):
            raise PlanningRoundError("system analyst returned an invalid verdict")
        issues_value = sa_payload.get("issues") or []
        if not isinstance(issues_value, list):
            raise PlanningRoundError("system analyst issues must be a list")
        issues = [str(item).strip() for item in issues_value if str(item).strip()]
        response = str(sa_payload.get("response") or "").strip()
        exchange = {"number": number, "pm": pm_response, "solution": solution,
                    "system_analyst": response, "issues": issues,
                    "verdict": verdict}
        exchanges.append(exchange)
        chat.add_message(
            db, row["session_id"], role="assistant", author=analyst["name"],
            content=f"## 系統分析（第 {number} 輪）\n{response}\n" +
                    ("\n".join(f"- {issue}" for issue in issues) if issues else ""),
            meta={"planning_round_id": round_id, "exchange": number,
                  "planning_role": "system-analyst", "verdict": verdict})
        db.write("UPDATE planning_rounds SET negotiation_json=?, solution_md=?, "
                 "updated_at=? WHERE id=?",
                 (json.dumps(exchanges, ensure_ascii=False), solution, now(), round_id))
        db.audit(actor or "system", "planning.negotiation.exchange",
                 "planning_round", round_id,
                 {"exchange": number, "verdict": verdict, "issues": len(issues),
                  "pm": pm["id"], "system_analyst": analyst["id"]})
        if on_exchange is not None:
            on_exchange(number, verdict)
        if verdict == "accept":
            propose(db, round_id, solution=solution, negotiation=exchanges,
                    actor=actor or "system")
            return {"state": "proposed", "solution": solution,
                    "negotiation": exchanges}
    db.audit(actor or "system", "planning.negotiation.escalate", "planning_round",
             round_id, {"exchanges": len(exchanges), "issues": issues})
    raise PlanningRoundError("PM 與系統分析在五輪內未能形成結論，需需求者裁決")


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
    from .admission import project_workflow_report
    return {"round": result, "intake": intake,
            "admission": project_workflow_report(db, project_id)}
