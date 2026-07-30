"""The 「測試」 button: each kind's check must exercise the real thing.

No network: HTTP kinds hit a local stub server, MCP stdio spawns a tiny real
MCP server (JSON-RPC over stdin/stdout), skills look at the filesystem.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from bastet_agent_os import resource_test
from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.server import create_app

# a stdio MCP server: answers initialize + tools/list, nothing more
MCP_STUB = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "stub-server", "version": "1.2.3"}}}), flush=True)
    elif msg.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "tools": [{"name": "search"}, {"name": "fetch"}]}}), flush=True)
'''

MCP_BROKEN = r'''
import sys
sys.stderr.write("Traceback: missing API key\n")
sys.exit(1)
'''


class Stub(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):                                    # noqa: N802
        status, body = self.routes.get(self.path, (404, {"error": "nope"}))
        if status == 401 and self.headers.get("Authorization") == "Bearer good":
            status, body = 200, {"data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}]}
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):                        # keep test output clean
        pass


@pytest.fixture
def stub():
    Stub.routes = {"/v1/models": (401, {"error": "unauthorized"}),
                   "/health": (200, {"ok": True})}
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "t.db")
    yield d
    d.close()


def add(db, rid, kind, *, endpoint=None, flavor=None, ref=None, config=None):
    ts = now()
    db.write("INSERT INTO resources(id, kind, name, endpoint, api_flavor, secret_ref, "
             "config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
             (rid, kind, rid, endpoint, flavor, ref, json.dumps(config or {}), ts, ts))
    return rid


# ---- LLM -------------------------------------------------------------------------

def test_llm_check_lists_models_and_never_spends_tokens(db, stub, tmp_path):
    key = tmp_path / "k"
    key.write_text("good")
    add(db, "llm-ok", "llm", endpoint=f"{stub}/v1", flavor="openai", ref=f"file:{key}")
    state = resource_test.run(db, "llm-ok", "tester")
    assert state["status"] == "ok"
    assert "2 models available" in state["detail"] and "gpt-5" in state["detail"]
    assert state["checked"] == f"GET {stub}/v1/models"   # a listing, not a completion


def test_llm_check_calls_out_a_rejected_credential(db, stub, tmp_path):
    key = tmp_path / "k"
    key.write_text("wrong")
    add(db, "llm-bad", "llm", endpoint=stub, flavor="openai", ref=f"file:{key}")
    state = resource_test.run(db, "llm-bad", "tester")
    assert state["status"] == "failed" and "credential rejected" in state["detail"]


def test_unreachable_host_fails_with_the_transport_error(db):
    add(db, "llm-down", "llm", endpoint="http://127.0.0.1:9", flavor="openai",
        ref="env:NOPE")
    state = resource_test.run(db, "llm-down", "tester")
    assert state["status"] == "failed"
    assert "credential could not be resolved" in state["detail"]  # checked first


def test_reachable_but_wrong_path_is_a_warning_not_a_failure(db, stub):
    """Distinguishing "host is up, path is wrong" from "host is down" is the
    difference between two very different debugging sessions."""
    add(db, "api-404", "api", endpoint=f"{stub}/nothing-here")
    state = resource_test.run(db, "api-404", "tester")
    assert state["status"] == "warn" and "reachable" in state["detail"]


def test_api_check_uses_the_configured_auth_header(db, stub, tmp_path):
    key = tmp_path / "k"
    key.write_text("t")
    add(db, "api-ok", "api", endpoint=f"{stub}/health", ref=f"file:{key}",
        config={"auth_header": "X-Api-Key"})
    assert resource_test.run(db, "api-ok", "tester")["status"] == "ok"


# ---- MCP -------------------------------------------------------------------------

def test_mcp_stdio_check_completes_a_real_handshake(db, tmp_path):
    script = tmp_path / "stub_mcp.py"
    script.write_text(MCP_STUB)
    add(db, "mcp-ok", "mcp",
        config={"mcp_transport": "stdio",
                "mcp_command": f"{sys.executable} {script}"})
    state = resource_test.run(db, "mcp-ok", "tester")
    assert state["status"] == "ok"
    assert "stub-server" in state["detail"] and "1.2.3" in state["detail"]
    assert "2 tools: search, fetch" in state["detail"]   # it really spoke MCP


def test_mcp_server_that_crashes_reports_its_stderr(db, tmp_path):
    script = tmp_path / "broken.py"
    script.write_text(MCP_BROKEN)
    add(db, "mcp-bad", "mcp",
        config={"mcp_transport": "stdio",
                "mcp_command": f"{sys.executable} {script}"})
    state = resource_test.run(db, "mcp-bad", "tester")
    assert state["status"] == "failed" and "missing API key" in state["detail"]


def test_mcp_without_a_command_says_so(db):
    add(db, "mcp-empty", "mcp", config={"mcp_transport": "stdio"})
    state = resource_test.run(db, "mcp-empty", "tester")
    assert state["status"] == "failed" and "launch command" in state["detail"]


def test_mcp_http_needs_an_initialize_result(db, stub):
    add(db, "mcp-http", "mcp", config={"mcp_transport": "http",
                                       "mcp_url": f"{stub}/health"})
    state = resource_test.run(db, "mcp-http", "tester")
    assert state["status"] == "failed" and "no MCP initialize result" in state["detail"]


def test_sse_framed_initialize_result_is_understood():
    text = ('event: message\n'
            'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05",'
            '"serverInfo":{"name":"remote","version":"9"}}}\n')
    verdict = resource_test._verdict_from_mcp_text(text, 200)
    assert verdict["status"] == "ok" and "remote" in verdict["detail"]


# ---- skills & git ----------------------------------------------------------------

def test_skill_source_is_checked_on_the_bastet_host(db, tmp_path):
    skill = tmp_path / "skills" / "pptx"
    skill.mkdir(parents=True)
    add(db, "skill-ok", "skill", config={"skill_source": str(skill)})
    assert resource_test.run(db, "skill-ok", "tester")["status"] == "ok"

    add(db, "skill-missing", "skill", config={"skill_source": str(tmp_path / "ghost")})
    state = resource_test.run(db, "skill-missing", "tester")
    assert state["status"] == "failed" and "Bastet host" in state["detail"]


def test_git_without_a_credential_says_only_public_was_checked(db, monkeypatch):
    add(db, "git-pub", "git", config={"git_provider": "github"})
    monkeypatch.setattr(resource_test, "_get",
                        lambda url, headers: {"status": "ok", "detail": "HTTP 200"})
    state = resource_test.run(db, "git-pub", "tester")
    assert state["status"] == "ok" and "no credential configured" in state["detail"]


def test_custom_git_over_ssh_uses_ls_remote(db, tmp_path, monkeypatch):
    calls = {}
    def fake(url):
        calls["url"] = url
        return {"status": "ok", "checked": f"git ls-remote {url}", "detail": "HEAD"}
    monkeypatch.setattr(resource_test, "_git_ls_remote", fake)
    add(db, "git-ssh", "git", endpoint="git@example.com:team/repo.git",
        config={"git_provider": "custom"})
    assert resource_test.run(db, "git-ssh", "tester")["status"] == "ok"
    assert calls["url"] == "git@example.com:team/repo.git"


# ---- the button, end to end -------------------------------------------------------

def test_test_endpoint_records_and_audits_the_verdict(tmp_path, stub):
    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    rid = client.post("/api/resources", json={
        "name": "health probe", "kind": "api", "endpoint": f"{stub}/health",
        "scope_type": "global"}).json()["id"]

    assert client.get("/api/resources").json()[0]["test"]["status"] == "unknown"
    state = client.post(f"/api/resources/{rid}/test").json()
    assert state["status"] == "ok"
    # the verdict sticks around, so the UI still shows it after a reload
    assert client.get("/api/resources").json()[0]["test"]["status"] == "ok"
    assert any(r["action"] == "resource.test.ok"
               for r in client.get("/api/audit?limit=50").json())
    assert client.post("/api/resources/res_ghost/test").status_code == 400


# ---- endpoint shape: the mistake that only shows up at run time --------------------

def test_operation_url_endpoint_is_flagged_before_a_run_wastes_it(db, stub, tmp_path):
    """The gateway appends its own operation path, so storing a full
    chat/completions URL breaks at dispatch time. Say it at config time."""
    from bastet_agent_os import resource_kinds
    full = f"{stub}/v1/chat/completions"
    assert resource_kinds.validate("llm", full, "env:K", {}) \
        == ["endpoint-is-operation-url"]
    assert resource_kinds.base_endpoint(full) == (f"{stub}/v1", True)
    assert resource_kinds.base_endpoint(f"{stub}/v1") == (f"{stub}/v1", False)

    key = tmp_path / "k"
    key.write_text("good")
    add(db, "llm-op", "llm", endpoint=full, flavor="openai", ref=f"file:{key}")
    state = resource_test.run(db, "llm-op", "tester")
    # probed the base (so the check is fair) and still warned about the shape
    assert state["checked"] == f"GET {stub}/v1/models"
    assert state["status"] == "warn" and "operation URL" in state["detail"]
    assert "credential accepted" in state["detail"]   # the key itself is fine
    assert "payload" not in state                     # probe internals stay internal
