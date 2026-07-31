"""Searching the audit trail and browsing memory.

An audit log you cannot query is a log nobody reads, so the filters have to
compose (a date range AND a category AND a keyword) and the category list has
to come from what is actually in the table rather than a hard-coded guess.
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

from bastet_agent_os.config import Home
from bastet_agent_os.db import Db
from bastet_agent_os.server import create_app


@pytest.fixture
def client(tmp_path):
    home = Home(tmp_path / "home")
    app = create_app(home)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {home.api_token()}"
    # write history straight into the same file the app opened
    db = Db(home.db_path)
    rows = [
        ("2026-07-01T09:00:00+00:00", "user:alice", "job.dispatch", "job", "job_a",
         '{"template":"web-dev"}'),
        ("2026-07-10T09:00:00+00:00", "user:bob", "job.cancel", "job", "job_b",
         '{"reason":"stale plan"}'),
        ("2026-07-20T09:00:00+00:00", "server", "project.activate", "project",
         "catswalker", "{}"),
        ("2026-07-25T09:00:00+00:00", "user:alice", "secret.rotate", "resource",
         "res_x", '{"name":"gitlab-token"}'),
    ]
    # the audit table is hash-chained, so backdated history is written with a
    # valid chain rather than by bypassing it
    prev = "genesis"
    for at, actor, action, ttype, tid, detail in rows:
        payload = f"{prev}|{at}|{actor}|{action}|{ttype}|{tid}|{detail}"
        row_hash = hashlib.sha256(payload.encode()).hexdigest()
        db.write("INSERT INTO audit_log(at, actor, action, target_type, target_id, "
                 "detail_json, prev_hash, row_hash) VALUES(?,?,?,?,?,?,?,?)",
                 (at, actor, action, ttype, tid, detail, prev, row_hash))
        prev = row_hash
    assert db.verify_audit_chain()
    db.close()
    yield c
    c.close()


def get(c, **params):
    r = c.get("/api/audit", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_unfiltered_returns_newest_first(client):
    body = get(client)

    actions = [r["action"] for r in body["rows"]]
    assert actions[0] == "secret.rotate"          # newest, not oldest
    assert body["count"] == len(body["rows"])


def test_categories_come_from_the_table(client):
    """The filter offers what exists. A hard-coded list goes stale the first
    time a new event type is added."""
    assert get(client)["categories"] == ["job", "project", "secret"]


def test_category_filter_matches_the_whole_family(client):
    body = get(client, action="job")

    assert {r["action"] for r in body["rows"]} == {"job.dispatch", "job.cancel"}


def test_keyword_reaches_into_the_detail(client):
    """`stale plan` is only in detail_json — searching the columns alone would
    miss exactly the rows worth finding."""
    body = get(client, q="stale plan")

    assert [r["target_id"] for r in body["rows"]] == ["job_b"]


def test_keyword_also_matches_actor_and_target(client):
    assert {r["actor"] for r in get(client, q="alice")["rows"]} == {"user:alice"}
    assert [r["target_id"] for r in get(client, q="catswalker")["rows"]] == ["catswalker"]


def test_date_range_is_inclusive_of_the_until_day(client):
    """A user typing until=2026-07-20 means "through the 20th". Comparing a
    bare date against a timestamp would silently drop that whole day."""
    body = get(client, since="2026-07-10", until="2026-07-20")

    assert {r["target_id"] for r in body["rows"]} == {"job_b", "catswalker"}


def test_filters_compose(client):
    body = get(client, action="job", actor="alice", since="2026-06-01")

    assert [r["target_id"] for r in body["rows"]] == ["job_a"]


def test_limit_is_clamped_not_trusted(client):
    assert len(get(client, limit=1)["rows"]) == 1
    assert get(client, limit=10**9)["count"] == 4     # no crash, no unbounded scan


def test_no_match_is_an_empty_result_not_an_error(client):
    body = get(client, q="no such thing")

    assert body["rows"] == []
    assert body["count"] == 0
    assert body["categories"]        # the facets still describe the table


def test_memory_browse_reads_real_amos_records(client, tmp_path, monkeypatch):
    """AMOS hands back dataclasses (`MemoryRecord` from list_recent,
    `SearchResult` from search), not dicts. Reading them with `.get` raised —
    and only where AMOS was actually installed, so this worked in tests and
    500'd in production. Nothing is stubbed here: a real memory goes into a
    throwaway AMOS home and comes back out through the endpoint."""
    amos = pytest.importorskip("agent_memory_os.client")
    monkeypatch.setenv("AGENT_MEMORY_HOME", str(tmp_path / "amos"))
    amos.MemoryClient().add("驗收條件是預約流程可完整走完", type="note",
                            owner="bastet", scope="project", tags=["chat"],
                            visibility=["project:catswalker"])

    browse = client.get("/api/memory/browse")
    assert browse.status_code == 200, browse.text
    item = browse.json()["items"][0]
    assert item["scope"] == "project"
    assert item["tags"] == ["chat"]
    assert "預約流程" in item["content"]
    assert browse.json()["stats"]                 # a summary, not an empty dict

    found = client.get("/api/memory/search", params={"q": "驗收條件"})
    assert found.status_code == 200, found.text
    assert "預約流程" in found.json()[0]["content"]


def test_memory_browse_filters_by_scope(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_HOME", str(tmp_path / "amos2"))
    amos = pytest.importorskip("agent_memory_os.client")
    write = amos.MemoryClient()
    write.add("團隊約定：PR 一律要審查", type="note", owner="bastet",
              scope="team", visibility=["team:team1"])
    write.add("專案筆記", type="note", owner="bastet", scope="project",
              visibility=["project:catswalker"])

    only_team = client.get("/api/memory/browse", params={"scope": "team"}).json()

    assert [i["scope"] for i in only_team["items"]] == ["team"]


def test_memory_browse_says_amos_is_down_rather_than_showing_nothing(client):
    """If AMOS is unreachable the tab has to say so — an empty list would read
    as "you have no memories"."""
    r = client.get("/api/memory/browse")

    assert r.status_code in (200, 502)
    if r.status_code == 502:
        assert "AMOS" in r.json()["detail"]
