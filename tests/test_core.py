"""Unit tests: db, audit chain, run tokens, pricing, usage extraction, grants."""


import pytest

from bastet_agent_os import run_tokens
from bastet_agent_os.governance import QuotaError, Reservations, dispatch_check, resolve_grant
from bastet_agent_os.pricing import PriceBook, Usage
from bastet_agent_os.usage_extract import (
    SseUsageAccumulator,
    anthropic_usage,
    inject_stream_options,
    openai_usage,
)

# ---- db / audit -------------------------------------------------------------

def test_audit_chain_verifies_and_detects_tampering(db):
    db.audit("user", "a.one", "x", "1")
    db.audit("user", "a.two", "x", "2", {"k": "v"})
    assert db.verify_audit_chain()
    db.write("UPDATE audit_log SET action='evil' WHERE action='a.one'")
    assert not db.verify_audit_chain()


def test_cas_update(seeded):
    assert seeded.cas_update("jobs", "job1", 0, {"status": "done"})
    assert not seeded.cas_update("jobs", "job1", 0, {"status": "cancelled"})  # stale version
    assert seeded.one("SELECT status FROM jobs WHERE id='job1'")["status"] == "done"


# ---- run tokens (SPEC §5.2.1) ------------------------------------------------

def test_run_token_roundtrip_and_revocation(seeded):
    token = run_tokens.issue(seeded, "run1", ttl_seconds=60)
    assert token.startswith("brt_")
    assert run_tokens.verify(seeded, token) == "run1"
    # only the hash is at rest
    assert seeded.one("SELECT * FROM run_tokens WHERE token_hash=?", (token,)) is None
    run_tokens.revoke_for_run(seeded, "run1")
    assert run_tokens.verify(seeded, token) is None  # terminal state => 401


def test_run_token_expiry(seeded):
    token = run_tokens.issue(seeded, "run1", ttl_seconds=-1)
    assert run_tokens.verify(seeded, token) is None


def test_run_token_garbage(seeded):
    assert run_tokens.verify(seeded, "brt_not-a-real-token") is None


# ---- pricing (SPEC §5.2.3): cache tokens priced separately --------------------

def test_cost_separates_cache_tokens():
    book = PriceBook()
    plain = book.cost_usd("claude-sonnet-4-20250514", Usage(tokens_in=1000))
    cached = book.cost_usd("claude-sonnet-4-20250514", Usage(cache_read=1000))
    assert plain > cached > 0  # cache reads are ~10x cheaper, never free


def test_unknown_model_costs_zero():
    assert PriceBook().cost_usd("mystery-model", Usage(tokens_in=100)) == 0.0


# ---- usage extraction (SPEC §5.2.2) -------------------------------------------

def test_openai_usage_splits_cached_tokens():
    u = openai_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                "prompt_tokens_details": {"cached_tokens": 60}}})
    assert (u.tokens_in, u.tokens_out, u.cache_read) == (40, 20, 60)


def test_anthropic_usage_reads_cache_fields():
    u = anthropic_usage({"usage": {"input_tokens": 10, "output_tokens": 5,
                                   "cache_read_input_tokens": 700,
                                   "cache_creation_input_tokens": 300}})
    assert (u.tokens_in, u.tokens_out, u.cache_read, u.cache_write) == (10, 5, 700, 300)


def test_sse_accumulator_anthropic_stream():
    acc = SseUsageAccumulator("anthropic")
    acc.feed_line('data: {"type":"message_start","message":{"model":"claude-x",'
                  '"usage":{"input_tokens":9,"cache_read_input_tokens":100}}}')
    acc.feed_line('data: {"type":"content_block_delta","delta":{"text":"hi"}}')
    acc.feed_line('data: {"type":"message_delta","usage":{"output_tokens":42}}')
    assert acc.model == "claude-x"
    assert (acc.usage.tokens_in, acc.usage.tokens_out, acc.usage.cache_read) == (9, 42, 100)
    assert acc.complete


def test_sse_accumulator_openai_final_chunk():
    acc = SseUsageAccumulator("openai")
    acc.feed_line('data: {"model":"gpt-4o","choices":[{"delta":{"content":"hi"}}]}')
    assert not acc.complete
    acc.feed_line('data: {"model":"gpt-4o","choices":[],'
                  '"usage":{"prompt_tokens":7,"completion_tokens":3}}')
    acc.feed_line("data: [DONE]")
    assert acc.complete
    assert (acc.usage.tokens_in, acc.usage.tokens_out) == (7, 3)


def test_responses_usage_splits_cached_tokens():
    from bastet_agent_os.usage_extract import responses_usage

    u = responses_usage({"usage": {"input_tokens": 100, "output_tokens": 30,
                                   "input_tokens_details": {"cached_tokens": 70}}})
    assert (u.tokens_in, u.tokens_out, u.cache_read) == (30, 30, 70)


def test_sse_accumulator_responses_stream():
    acc = SseUsageAccumulator("openai-responses")
    acc.feed_line('data: {"type":"response.output_text.delta","delta":"hi"}')
    assert not acc.complete
    acc.feed_line('data: {"type":"response.completed","response":{"model":"gpt-x",'
                  '"usage":{"input_tokens":9,"output_tokens":4,'
                  '"input_tokens_details":{"cached_tokens":5}}}}')
    assert acc.complete and acc.model == "gpt-x"
    assert (acc.usage.tokens_in, acc.usage.tokens_out, acc.usage.cache_read) == (4, 4, 5)


def test_inject_stream_options_only_when_streaming():
    assert inject_stream_options({"stream": True})["stream_options"] == {"include_usage": True}
    assert "stream_options" not in inject_stream_options({"stream": False})


# ---- governance (SPEC §5.2.4) --------------------------------------------------

def test_grant_resolution_prefers_most_specific(seeded):
    seeded.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
                 "VALUES('grt-agent','res1','agent','ag1',datetime('now'))")
    grant = resolve_grant(seeded, "res1", "proj1", "ag1")
    assert grant.id == "grt-agent"  # agent scope beats project scope


def test_dispatch_check_blocks_exhausted_budget(seeded):
    seeded.write("UPDATE grants SET budget_usd=0.01 WHERE id='grt1'")
    seeded.write("INSERT INTO usage_ledger(id, run_id, resource_id, cost_usd, at) "
                 "VALUES('l1','run1','res1',0.02,datetime('now'))")
    grant = resolve_grant(seeded, "res1", "proj1", "ag1")
    with pytest.raises(QuotaError):
        dispatch_check(seeded, grant)


def test_reservations_admit_and_settle(seeded):
    grant = resolve_grant(seeded, "res1", "proj1", "ag1")  # budget 10 USD
    res = Reservations(reserve_usd=6.0)
    res.admit(seeded, grant)          # 6 reserved of 10 — ok
    with pytest.raises(QuotaError):
        res.admit(seeded, grant)      # 12 > 10 — blocked
    res.settle(grant)
    res.admit(seeded, grant)          # slot freed — ok again
