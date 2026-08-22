"""Small persistent golden-case harness for selective context assembly."""

from __future__ import annotations

import json

from .context_engine import build_context
from .db import new_id, now


def evaluate(db, *, job_id: str, stage: str, role: str | None = None,
             expected_buckets: list[str] | None = None,
             expected_terms: list[str] | None = None,
             forbidden_terms: list[str] | None = None,
             budget_tokens: int = 6000) -> dict:
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if job is None:
        raise ValueError(f"unknown job {job_id!r}")
    expected_buckets = expected_buckets or []
    expected_terms = expected_terms or []
    forbidden_terms = forbidden_terms or []
    text, report = build_context(db, job, stage, budget_tokens=budget_tokens,
                                 stage_role=role)
    included = {s["bucket"] for s in report.sections if s["included"]}
    bucket_hits = sum(bucket in included for bucket in expected_buckets)
    term_hits = sum(term.casefold() in text.casefold() for term in expected_terms)
    noise = [term for term in forbidden_terms if term.casefold() in text.casefold()]
    bucket_recall = bucket_hits / len(expected_buckets) if expected_buckets else 1.0
    term_recall = term_hits / len(expected_terms) if expected_terms else 1.0
    passed = bucket_recall == 1.0 and term_recall == 1.0 and not noise
    evaluation_id = new_id("ctxeval")
    db.write("INSERT INTO context_evaluations(id,project_id,job_id,stage,role,"
             "expected_buckets_json,expected_terms_json,forbidden_terms_json,"
             "report_json,bucket_recall,term_recall,noise_count,passed,at) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (evaluation_id, job["project_id"], job_id, stage, role,
              json.dumps(expected_buckets), json.dumps(expected_terms, ensure_ascii=False),
              json.dumps(forbidden_terms, ensure_ascii=False),
              json.dumps(report.to_dict(), ensure_ascii=False), bucket_recall,
              term_recall, len(noise), int(passed), now()))
    return {"id": evaluation_id, "passed": passed, "bucket_recall": bucket_recall,
            "term_recall": term_recall, "noise": noise,
            "included_buckets": sorted(included), "report": report.to_dict()}


def recent(db, limit: int = 100) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM context_evaluations ORDER BY at DESC LIMIT ?", (limit,))]
