"""Evidence-grounded, channel-neutral job progress snapshots."""

from __future__ import annotations

import json
from typing import Any

from .db import Db

NODE_ICON = {
    "pending": "▫️", "ready": "⚪", "running": "🔵", "passed": "✅",
    "failed": "❌", "blocked": "🟠", "cancelled": "⚫",
}


def snapshot(db: Db, job_id: str) -> dict[str, Any] | None:
    """Read the durable graph, evidence, challenge and delivery state."""
    row = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if row is None:
        return None
    job = dict(row)
    nodes = [dict(item) for item in db.query(
        "SELECT stage,status,needs_json,head_commit,updated_at FROM job_stage_nodes "
        "WHERE job_id=? ORDER BY rowid", (job_id,))]
    runs = [dict(item) for item in db.query(
        "SELECT id,stage,attempt,status,error,progress_text,artifacts_json,finished_at "
        "FROM runs WHERE job_id=? ORDER BY rowid", (job_id,))]
    gates = [dict(item) for item in db.query(
        "SELECT g.run_id,g.gate_type,g.verdict,g.detail_md,g.at FROM gate_results g "
        "JOIN runs r ON r.id=g.run_id WHERE r.job_id=? ORDER BY g.at", (job_id,))]
    latest_runs: dict[str, dict] = {}
    for run in runs:
        latest_runs[run["stage"]] = run
    gates_by_run = {gate["run_id"]: gate for gate in gates}
    try:
        stages = json.loads(job.get("stages_snapshot_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        stages = []
    evidence = []
    for stage in stages:
        run = latest_runs.get(stage.get("name"))
        gate = gates_by_run.get(run["id"]) if run else None
        for kind in stage.get("evidence") or []:
            evidence.append({"kind": kind, "stage": stage.get("name"),
                             "verdict": gate["verdict"] if gate else "pending"})
    challenges = [dict(item) for item in db.query(
        "SELECT id,from_stage,to_stage,status,exchanges_json,updated_at "
        "FROM handoff_challenges WHERE job_id=? ORDER BY rowid", (job_id,))]
    for challenge in challenges:
        try:
            challenge["exchanges"] = json.loads(challenge.pop("exchanges_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            challenge["exchanges"] = []
    delivery = db.one(
        "SELECT mode,status,target,version,commit_sha,error,finished_at FROM deliveries "
        "WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job_id,))
    latest = runs[-1] if runs else None
    summary = ""
    if latest:
        try:
            summary = str(json.loads(latest.get("artifacts_json") or "{}").get("summary")
                          or latest.get("progress_text") or "")
        except (json.JSONDecodeError, TypeError, AttributeError):
            summary = str(latest.get("progress_text") or "")
    latest_error = str((latest or {}).get("error") or "")
    if not latest_error and job.get("status") in ("blocked", "cancelled"):
        latest_error = str(job.get("rework_note") or "")
    return {
        "job": job, "nodes": nodes, "runs": runs, "evidence": evidence,
        "challenges": challenges, "delivery": dict(delivery) if delivery else None,
        "summary": summary, "latest_error": latest_error,
    }


def progress_line(report: dict[str, Any]) -> str:
    nodes = report["nodes"]
    if not nodes:
        return f"階段：{report['job']['stage']}"
    counts = {status: sum(node["status"] == status for node in nodes)
              for status in ("passed", "running", "ready", "blocked", "failed")}
    return (f"DAG：{counts['passed']}/{len(nodes)} 通過 · "
            f"{counts['running']} 執行 · {counts['ready']} 就緒 · "
            f"{counts['blocked'] + counts['failed']} 阻塞")


def render(db: Db, job_id: str, *, compact: bool = False) -> str:
    report = snapshot(db, job_id)
    if report is None:
        return f"找不到任務 {job_id}"
    job = report["job"]
    lines = [f"{job['title']}\n專案 {job['project_id']} · {job_id}",
             f"狀態：{job['status']} · {progress_line(report)}"]
    nodes = report["nodes"]
    if nodes:
        visible = nodes if not compact else [node for node in nodes
                                             if node["status"] != "pending"]
        lines.append("階段圖：\n" + "\n".join(
            f"{NODE_ICON.get(node['status'], '•')} {node['stage']} · {node['status']}"
            for node in visible[:18]))
    passed_evidence = sorted({item["kind"] for item in report["evidence"]
                              if item["verdict"] == "passed"})
    pending_evidence = sorted({item["kind"] for item in report["evidence"]
                               if item["verdict"] != "passed"})
    if passed_evidence or pending_evidence:
        lines.append("證據：" + ("✅ " + ", ".join(passed_evidence)
                                if passed_evidence else "尚無")
                     + ("；待驗證 " + ", ".join(pending_evidence)
                        if pending_evidence else ""))
    open_challenges = [item for item in report["challenges"]
                       if item["status"] in ("open", "human_ruling", "rework_required")]
    if open_challenges:
        lines.append("交接挑戰：" + "；".join(
            f"{item['from_stage']}→{item['to_stage']} {item['status']} "
            f"({len(item['exchanges'])} 回合)" for item in open_challenges[:5]))
    delivery = report["delivery"]
    if delivery:
        receipt = (delivery.get("commit_sha") or "")[:12]
        lines.append(f"交付：{delivery['mode']} · {delivery['status']} · "
                     f"{delivery.get('target') or '—'}" + (f" · {receipt}" if receipt else ""))
    if report["latest_error"]:
        lines.append("目前問題：" + report["latest_error"][-700:])
    elif report["summary"]:
        lines.append("最新摘要：" + report["summary"][:700])
    return "\n\n".join(lines)
