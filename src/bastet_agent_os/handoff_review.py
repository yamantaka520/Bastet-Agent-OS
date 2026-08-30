"""Bounded Agent-to-Agent review before a workflow DAG node starts."""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import collaboration
from .planning_rounds import _agent_turn, _json_payload


@dataclass(frozen=True)
class ReviewOutcome:
    status: str  # accepted | rework_required | human_ruling
    detail: str = ""
    source_stage: str = ""
    challenge_id: str = ""


RECEIVER_PROMPT = """\
你是即將接手工作流階段的 Agent。開始自己的工作前，必須質疑前一階段交接，不可照單
全收。檢查摘要、變更檔案、驗證證據、風險是否足以支撐你的階段。不要呼叫工具。
只輸出 JSON：
{"verdict":"accept|challenge","response":"理由", "handoff_id":"要挑戰的 id",
 "evidence_gap":"缺少的證據", "requested_resolution":"要來源 Agent 補什麼"}
"""

SOURCE_PROMPT = """\
你是交接來源 Agent。請針對接手 Agent 的質疑給出可核驗回應；如果現有成果確實不足，
必須接受返工，不可用文字硬拗。不要呼叫工具。只輸出 JSON：
{"resolution":"answer|rework_required","response":"回答或返工理由"}
"""


def _handoff_payload(rows) -> list[dict]:
    return [{"id": row["id"], "from_stage": row["from_stage"],
             "summary": row["summary"],
             "changed_paths": json.loads(row["changed_paths_json"] or "[]"),
             "verification": json.loads(row["verification_json"] or "[]"),
             "risks": json.loads(row["risks_json"] or "[]")}
            for row in rows]


async def review(db, *, job, stage, receiver, workdir: str) -> ReviewOutcome:
    """Run a maximum-five-exchange review and persist the complete transcript."""
    all_rows = db.query("SELECT * FROM stage_handoffs WHERE job_id=? AND to_stage=? "
                        "ORDER BY at,rowid", (job["id"], stage.name))
    # A reworked predecessor publishes a new handoff.  Its old receipt remains
    # valid history, but only the latest handoff from each dependency is an
    # admissible input to the next attempt.
    latest = {row["from_stage"]: row for row in all_rows}
    rows = list(latest.values())
    if not rows or not stage.challenge:
        return ReviewOutcome("accepted")
    collaboration.deliver_handoffs(db, job["id"], stage.name, receiver["id"])
    accepted = all(db.one(
        "SELECT 1 AS ok FROM handoff_receipts WHERE handoff_id=? AND agent_id=? "
        "AND acknowledged_at IS NOT NULL", (row["id"], receiver["id"]))
        is not None for row in rows)
    if accepted:
        return ReviewOutcome("accepted", "durable handoff review already accepted")
    evidence = _handoff_payload(rows)
    handoff_ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in handoff_ids)
    existing = db.one(
        f"SELECT * FROM handoff_challenges WHERE handoff_id IN ({placeholders}) "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1", tuple(handoff_ids))
    if existing is not None:
        challenge = collaboration._challenge_dict(existing)
        selected = next(row for row in rows if row["id"] == challenge["handoff_id"])
        if challenge["status"] == "accepted":
            detail = challenge["exchanges"][-1]["content"]
            for row in rows:
                collaboration.acknowledge_handoff(
                    db, row["id"], agent_id=receiver["id"], acknowledgement=detail)
            return ReviewOutcome("accepted", detail, selected["from_stage"],
                                 challenge["id"])
        if challenge["status"] in ("rework_required", "human_ruling"):
            return ReviewOutcome(challenge["status"],
                                 challenge["exchanges"][-1]["content"],
                                 selected["from_stage"], challenge["id"])
    else:
        raw = await _agent_turn(
            db, receiver, workdir=workdir,
            prompt=f"{RECEIVER_PROMPT}\n\n## 目標階段\n{stage.name}\n\n"
                   f"## 交接證據\n{json.dumps(evidence, ensure_ascii=False)}")
        payload = _json_payload(raw)
        verdict = str(payload.get("verdict") or "").lower()
        response = str(payload.get("response") or "").strip()
        if verdict == "accept":
            for row in rows:
                collaboration.acknowledge_handoff(
                    db, row["id"], agent_id=receiver["id"],
                    acknowledgement=response or "前置檢核完成，交接證據可接受")
            return ReviewOutcome("accepted", response)
        if verdict != "challenge":
            raise ValueError("handoff reviewer returned an invalid verdict")
        handoff_id = str(payload.get("handoff_id") or rows[0]["id"])
        selected = next((row for row in rows if row["id"] == handoff_id), None)
        if selected is None:
            raise ValueError("handoff reviewer challenged an unknown handoff")
        challenge = collaboration.open_handoff_challenge(
            db, handoff_id, agent_id=receiver["id"],
            claim=response or "交接證據不足",
            evidence_gap=str(payload.get("evidence_gap") or ""),
            requested_resolution=str(payload.get("requested_resolution") or ""))
    source = db.one("SELECT * FROM agents WHERE id=? AND enabled=1",
                    (selected["agent_id"],))
    if source is None:
        challenge = collaboration.respond_handoff_challenge(
            db, challenge["id"], agent_id=receiver["id"],
            content="來源 Agent 已不可用，需要人工裁決", resolution="human_ruling")
        return ReviewOutcome("human_ruling", "source agent unavailable",
                             selected["from_stage"], challenge["id"])

    while challenge["status"] == "open":
        last_agent = challenge["exchanges"][-1]["agent_id"]
        if last_agent == receiver["id"]:
            source_raw = await _agent_turn(
                db, source, workdir=workdir,
                prompt=f"{SOURCE_PROMPT}\n\n## 交接\n"
                       f"{json.dumps(evidence, ensure_ascii=False)}\n\n## 挑戰歷程\n"
                       f"{json.dumps(challenge['exchanges'], ensure_ascii=False)}")
            source_payload = _json_payload(source_raw)
            resolution = str(source_payload.get("resolution") or "").lower()
            if resolution not in ("answer", "rework_required"):
                raise ValueError("handoff source returned an invalid resolution")
            response = str(source_payload.get("response") or "").strip()
            challenge = collaboration.respond_handoff_challenge(
                db, challenge["id"], agent_id=source["id"],
                content=response or "來源 Agent 未提供具體回應",
                resolution="rework_required" if resolution == "rework_required" else "")
            if challenge["status"] == "rework_required":
                return ReviewOutcome("rework_required", response,
                                     selected["from_stage"], challenge["id"])
        else:
            receiver_raw = await _agent_turn(
                db, receiver, workdir=workdir,
                prompt=f"{RECEIVER_PROMPT}\n\n## 目標階段\n{stage.name}\n\n"
                       f"## 挑戰歷程\n"
                       f"{json.dumps(challenge['exchanges'], ensure_ascii=False)}")
            receiver_payload = _json_payload(receiver_raw)
            verdict = str(receiver_payload.get("verdict") or "").lower()
            if verdict not in ("accept", "challenge"):
                raise ValueError("handoff reviewer returned an invalid follow-up verdict")
            response = str(receiver_payload.get("response") or "").strip()
            challenge = collaboration.respond_handoff_challenge(
                db, challenge["id"], agent_id=receiver["id"],
                content=response or "仍需補強交接證據",
                resolution="accepted" if verdict == "accept" else "")
            if challenge["status"] == "accepted":
                for row in rows:
                    collaboration.acknowledge_handoff(
                        db, row["id"], agent_id=receiver["id"],
                        acknowledgement=response or "挑戰已解決，接受交接")
                return ReviewOutcome("accepted", response,
                                     selected["from_stage"], challenge["id"])
    return ReviewOutcome("human_ruling", "五次 exchange 內未形成可接受結論",
                         selected["from_stage"], challenge["id"])
