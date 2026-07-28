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
    ("spec", 0.30),
    ("history", 0.20),
    ("deps", 0.20),
    ("memory", 0.30),
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
                  skip: frozenset[str] = frozenset()) -> tuple[str, ContextReport]:
    """Assemble the run's task-layer context. Returns (text, report).

    `skip` omits buckets the caller already carries elsewhere (e.g. "spec"
    when the executor prompt contains the job spec verbatim).
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
        text = _gather(db, job, stage_name, bucket, amos_query)
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


def _rollover(bucket: str, budget: int, remaining: int) -> int:
    """Later buckets may use budget earlier buckets left unused."""
    planned_before = sum(f for name, f in BUCKETS
                         if BUCKETS.index((name, f)) < [b for b, _ in BUCKETS].index(bucket))
    expected_remaining = int(budget * (1 - planned_before))
    return max(0, remaining - expected_remaining)


def _gather(db: Db, job, stage_name: str, bucket: str, amos_query: str | None) -> str:
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

            pack = MemoryClient().context_pack(query, max_tokens=1200)
            text = pack if isinstance(pack, str) else str(pack or "")
            return f"## Team memory (AMOS)\n{text}" if text.strip() else ""
        except Exception as exc:  # AMOS optional — degrade to no memory bucket
            log.debug("AMOS context pack unavailable: %s", type(exc).__name__)
            return ""

    return ""
