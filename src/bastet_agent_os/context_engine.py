"""Dynamic context engine (SPEC §5.6): the outer allocator.

Assembles the task-layer context for a run inside a token budget, treating
each source as a bucket: job spec, pipeline history, dependency-job
conclusions, and the AMOS memory pack. Unused budget flows to later buckets.
Every include/exclude decision lands in the report — auditable selection,
never silent truncation.

Rough token accounting uses a 4-chars-per-token heuristic; the point is
budget *discipline*, not tokenizer precision.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .db import Db

log = logging.getLogger("bastet.context")

CHARS_PER_TOKEN = 4

# budget fractions per bucket; unused budget rolls over in this order
BUCKETS = [
    ("spec", 0.22),
    ("handoff", 0.22),
    ("history", 0.12),
    ("test_evidence", 0.12),
    ("room", 0.10),
    ("deps", 0.10),
    ("memory", 0.12),
]


def _tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def _clip(text: str, budget_tokens: int) -> str:
    limit = budget_tokens * CHARS_PER_TOKEN
    return text if len(text) <= limit else text[:limit] + "\n…[clipped]"


@dataclass
class ContextReport:
    budget_tokens: int
    sections: list[dict] = field(default_factory=list)

    def add(self, bucket: str, included: bool, tokens: int, note: str = "") -> None:
        self.sections.append({"bucket": bucket, "included": included,
                              "tokens": tokens, "note": note})

    def to_dict(self) -> dict:
        return {"budget_tokens": self.budget_tokens, "sections": self.sections}


def build_context(db: Db, job, stage_name: str, budget_tokens: int = 6000,
                  amos_query: str | None = None,
                  skip: frozenset[str] = frozenset(),
                  recall: dict | None = None,
                  stage_role: str | None = None,
                  agent_id: str | None = None) -> tuple[str, ContextReport]:
    """Assemble the run's task-layer context. Returns (text, report).

    `skip` omits buckets the caller already carries elsewhere (e.g. "spec"
    when the executor prompt contains the job spec verbatim).
    `recall` is the AMOS requester identity (agent/team) — passing it turns the
    memory bucket from "everything in the store" into "what this agent may see".
    """
    report = ContextReport(budget_tokens=budget_tokens)
    parts: list[str] = []
    remaining = budget_tokens

    for bucket, fraction in BUCKETS:
        if bucket in skip:
            report.add(bucket, False, 0, "skipped by caller")
            continue
        allowance = min(remaining, int(budget_tokens * fraction) + _rollover(
            bucket, budget_tokens, remaining))
        if not _relevant(bucket, stage_name, stage_role):
            report.add(bucket, False, 0, f"not relevant to role {stage_role or '-'}")
            continue
        text = _gather(db, job, stage_name, bucket, amos_query, recall, agent_id)
        if not text:
            report.add(bucket, False, 0, "empty")
            continue
        clipped = _clip(text, allowance)
        used = _tokens(clipped)
        if used > remaining:
            report.add(bucket, False, used, "over budget")
            continue
        parts.append(clipped)
        remaining -= used
        report.add(bucket, True, used, "clipped" if clipped != text else "")

    return "\n\n".join(parts), report


def _relevant(bucket: str, stage_name: str, role: str | None) -> bool:
    """Cheap, deterministic first-stage selector; every decision is audited.

    Test evidence is useful to testers/reviewers and to a rework stage, while
    broad room discussion is most useful to PM/review/co-ordination roles.
    Handoffs remain universal: they are the narrowest description of what the
    previous agent changed.
    """
    label = f"{stage_name} {role or ''}".lower()
    if bucket == "test_evidence":
        return any(x in label for x in ("test", "qa", "review", "驗", "測", "審"))
    if bucket == "room":
        return any(x in label for x in ("pm", "plan", "review", "lead", "整合", "審"))
    return True


def _rollover(bucket: str, budget: int, remaining: int) -> int:
    """Later buckets may use budget earlier buckets left unused."""
    planned_before = sum(f for name, f in BUCKETS
                         if BUCKETS.index((name, f)) < [b for b, _ in BUCKETS].index(bucket))
    expected_remaining = int(budget * (1 - planned_before))
    return max(0, remaining - expected_remaining)


def _gather(db: Db, job, stage_name: str, bucket: str, amos_query: str | None,
            recall: dict | None = None, agent_id: str | None = None) -> str:
    if bucket == "spec":
        return f"# Task: {job['title']}\n{job['spec_md']}"

    if bucket == "history":
        rows = db.query(
            "SELECT r.stage, g.verdict, g.detail_md FROM runs r "
            "LEFT JOIN gate_results g ON g.run_id = r.id "
            "WHERE r.job_id=? AND r.stage != ? AND r.status='succeeded' "
            "ORDER BY r.finished_at DESC LIMIT 5", (job["id"], stage_name))
        if not rows:
            return ""
        lines = [f"- stage {r['stage']}: gate {r['verdict'] or 'n/a'} {r['detail_md'] or ''}"
                 for r in reversed(rows)]
        return "## Pipeline history\n" + "\n".join(lines)

    if bucket == "handoff":
        from .collaboration import deliver_handoffs, latest_handoffs
        if agent_id:
            deliver_handoffs(db, job["id"], stage_name, agent_id)
        rows = latest_handoffs(db, job["id"])
        if not rows:
            return ""
        lines = []
        for row in rows:
            paths = json.loads(row["changed_paths_json"] or "[]")
            checks = json.loads(row["verification_json"] or "[]")
            lines.append(f"- [handoff:{row['id']}] {row['from_stage']} → "
                         f"{row['to_stage'] or '完成'}: "
                         f"{row['summary']}\n  changed: {', '.join(paths) or 'none'}"
                         + (f"\n  verified: {', '.join(checks)}" if checks else ""))
        return ("## Stage handoffs\n" + "\n".join(lines)
                + "\n\n逐項說明你對 handoff 的理解與問題；Bastet 會用你的階段完成回報"
                  "在專案會議室登記接收確認。不要宣稱未由權威 gate 產生的測試結果。")

    if bucket == "test_evidence":
        rows = db.query(
            "SELECT case_id,command,verdict,base_commit,covered_paths_json,at "
            "FROM test_evidence WHERE job_id=? ORDER BY at DESC LIMIT 12", (job["id"],))
        if not rows:
            return ""
        return "## Test evidence\n" + "\n".join(
            f"- {r['case_id']}: {r['verdict']} at {r['base_commit'] or 'unknown'}; "
            f"coverage={r['covered_paths_json']}" for r in rows)

    if bucket == "room":
        from .collaboration import messages
        rows = messages(db, job["project_id"], limit=12)
        if not rows:
            return ""
        return "## Project room (recent)\n" + "\n".join(
            f"- [{r['kind']}] {r['author_id']}: {r['content'][:500]}" for r in rows)

    if bucket == "deps":
        rows = db.query(
            "SELECT d.depends_on_job_id, j.title, j.status FROM job_deps d "
            "JOIN jobs j ON j.id = d.depends_on_job_id "
            "WHERE d.job_id=? AND d.effect='context'", (job["id"],))
        if not rows:
            return ""
        lines = []
        for dep in rows:
            last = db.one("SELECT error, artifacts_json FROM runs WHERE job_id=? "
                          "AND status='succeeded' ORDER BY finished_at DESC LIMIT 1",
                          (dep["depends_on_job_id"],))
            summary = ""
            if last:
                arts = json.loads(last["artifacts_json"] or "{}")
                summary = arts.get("summary") or ""
            lines.append(f"- {dep['depends_on_job_id']} ({dep['title']}, {dep['status']})"
                         + (f": {summary}" if summary else ""))
        return "## Dependency conclusions\n" + "\n".join(lines)

    if bucket == "memory":
        query = amos_query or f"{job['title']} {job['spec_md'][:200]}"
        try:
            from agent_memory_os.client import MemoryClient

            # recall AS the running agent: without a requester AMOS applies no
            # ACL, so every project's memories land in every project's pack
            client = MemoryClient()
            # Search is relevance-first. context_pack's importance-heavy ranking
            # can crowd a new task out with old high-importance memories.
            hits = client.search(query, limit=10, **(recall or {}))
            selected = []
            for hit in hits or []:
                record = getattr(hit, "record", hit)
                content = (record.get("content", "") if isinstance(record, dict)
                           else getattr(record, "content", ""))
                if content:
                    selected.append(str(content)[:600])
                if len(selected) >= 6:
                    break
            text = "\n".join(f"- {item}" for item in selected)
            return f"## Team memory (AMOS)\n{text}" if text.strip() else ""
        except Exception as exc:  # AMOS optional — degrade to no memory bucket
            log.debug("AMOS context pack unavailable: %s", type(exc).__name__)
            return ""

    return ""
