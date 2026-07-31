"""Testing a git resource the way an agent uses it: `git ls-remote`.

The live case had all three failure modes at once — a repo URL where the checker
expected a host, an SSH private key paired with an HTTPS URL, and a Cloudflare
interstitial reported as "credential rejected".
"""

import subprocess

import pytest

from bastet_agent_os import resource_test
from bastet_agent_os.db import Db, now


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "t.db")
    yield d
    d.close()


def add_git(db, rid, *, endpoint, provider="gitlab", ref=None):
    import json
    ts = now()
    db.write("INSERT INTO resources(id, kind, name, endpoint, secret_ref, config_json, "
             "created_at, updated_at) VALUES(?,'git',?,?,?,?,?,?)",
             (rid, rid, endpoint, ref, json.dumps({"git_provider": provider}), ts, ts))
    return rid


def test_url_shapes_are_told_apart():
    shape = resource_test._url_shape
    assert shape("git@gitlab.com:group/project.git") == "ssh"
    assert shape("ssh://git@gitlab.com/group/project.git") == "ssh"
    assert shape("https://gitlab.com/group/project.git") == "repo"
    assert shape("https://gitlab.example.com") == "host"
    assert shape("/srv/git/project.git") == "path"
    assert shape("") == ""


def test_an_ssh_key_with_an_https_url_is_reported_as_the_mismatch_it_is(db, tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"
                   "-----END OPENSSH PRIVATE KEY-----\n")
    add_git(db, "g1", endpoint="https://gitlab.com/me/project.git", ref=f"file:{key}")
    state = resource_test.run(db, "g1", "tester")
    assert state["status"] == "failed"
    assert "SSH 私鑰" in state["detail"] and "HTTPS" in state["detail"]
    # it must not have gone near the network to work this out
    assert "credential vs" in state["checked"]


def test_a_token_with_an_ssh_url_is_also_a_mismatch(db, tmp_path):
    token = tmp_path / "tok"
    token.write_text("glpat-xxxxxxxxxxxx")
    add_git(db, "g2", endpoint="git@gitlab.com:me/project.git", ref=f"file:{token}")
    state = resource_test.run(db, "g2", "tester")
    assert state["status"] == "failed" and "SSH 私鑰" in state["detail"]


def test_an_ssh_endpoint_without_a_key_says_so(db):
    add_git(db, "g3", endpoint="git@gitlab.com:me/project.git")
    state = resource_test.run(db, "g3", "tester")
    assert state["status"] == "failed" and "私鑰憑證" in state["detail"]


def test_a_reachable_repo_passes(db, tmp_path):
    """A real ls-remote against a real repo — no network needed."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "f.txt").write_text("hi")
    for args in (["add", "f.txt"],
                 ["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init"],
                 ["remote", "add", "origin", str(origin)],
                 ["push", "-q", "origin", "HEAD:refs/heads/main"]):
        subprocess.run(["git", "-C", str(work), *args], check=True)

    add_git(db, "g4", endpoint=str(origin), provider="custom")
    state = resource_test.run(db, "g4", "tester")
    assert state["status"] == "ok", state["detail"]
    assert "ls-remote" in state["checked"]


def test_an_unreachable_repo_reports_gits_own_error(db):
    add_git(db, "g5", endpoint="/nonexistent/repo.git", provider="custom")
    state = resource_test.run(db, "g5", "tester")
    assert state["status"] == "failed"
    assert state["detail"]                      # git said something


def test_ssh_key_failures_carry_an_actionable_hint():
    hint = resource_test._ssh_hint
    assert "公鑰已加到" in hint("git@gitlab.com: Permission denied (publickey).")
    assert "格式不正確" in hint("Load key \"/tmp/x\": invalid format")
    assert "連不到主機" in hint("ssh: Could not resolve hostname gitlab.invalid")
    assert hint("something unexpected") == ""


def test_a_bot_challenge_is_not_a_rejected_credential():
    import httpx

    challenge = httpx.Response(
        403, headers={"content-type": "text/html; charset=UTF-8"},
        text="<!DOCTYPE html><html><head><title>Just a moment...</title>")
    assert resource_test._looks_like_challenge(challenge) is True
    real = httpx.Response(403, headers={"content-type": "application/json"},
                          text='{"message":"401 Unauthorized"}')
    assert resource_test._looks_like_challenge(real) is False


# ---- SSH repos must be usable at run time, not just testable -----------------------

def test_an_ssh_git_resource_reaches_the_agent_as_a_usable_key(db, tmp_path):
    """Testing SSH is not enough: without a key file and a git that uses it, the
    agent can only look at the repo it is supposed to clone."""
    import os
    import stat

    from bastet_agent_os import resource_access
    from bastet_agent_os.db import now as _now

    key = tmp_path / "deploy_key"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n"
                   "-----END OPENSSH PRIVATE KEY-----")      # no trailing newline
    db.write("INSERT INTO projects(id, team_id, repo_path, created_at, updated_at) "
             "VALUES('p','t','/x',?,?)", (_now(), _now()))
    add_git(db, "GitLab CatsWalker", endpoint="git@gitlab.com:me/catswalker.git",
            ref=f"file:{key}")
    db.write("UPDATE resources SET name='GitLab CatsWalker' WHERE id='GitLab CatsWalker'")
    db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
             "VALUES('g','GitLab CatsWalker','project','p',?)", (_now(),))

    access = resource_access.build(db, tmp_path, "p", "t", "run-ssh")
    prefix = "BASTET_RES_GITLAB_CATSWALKER"
    assert access.env[f"{prefix}_URL"] == "git@gitlab.com:me/catswalker.git"
    key_file = access.env[f"{prefix}_SSH_KEY"]
    assert os.path.exists(key_file)
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600   # ssh refuses otherwise
    assert open(key_file).read().endswith("\n")               # ssh demands it
    # a plain `git clone` must work, and the agent can also pick deliberately
    assert access.env["GIT_SSH_COMMAND"] == access.env[f"{prefix}_SSH_COMMAND"]
    assert "IdentitiesOnly=yes" in access.env["GIT_SSH_COMMAND"]
    assert "BatchMode=yes" in access.env["GIT_SSH_COMMAND"]
    assert "clone" in access.notes and "不要把金鑰內容印出來" in access.notes
    # the key lives outside the worktree and goes away with the run
    resource_access.cleanup(tmp_path, "run-ssh")
    assert not os.path.exists(key_file)


def test_an_ssh_repo_without_a_key_is_advertised_as_broken(db, tmp_path):
    from bastet_agent_os import resource_access
    from bastet_agent_os.db import now as _now

    db.write("INSERT INTO projects(id, team_id, repo_path, created_at, updated_at) "
             "VALUES('p','t','/x',?,?)", (_now(), _now()))
    add_git(db, "no-key-repo", endpoint="git@gitlab.com:me/x.git")
    db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
             "VALUES('g','no-key-repo','project','p',?)", (_now(),))
    access = resource_access.build(db, tmp_path, "p", "t", "run-nokey")
    item = next(m for m in access.manifest if m["name"] == "no-key-repo")
    assert any("no key configured" in h for h in item["how"])
