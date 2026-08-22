"""Project-room, handoff-context, and incremental-test evidence contracts."""

from __future__ import annotations

import json
import subprocess

from bastet_agent_os import collaboration
from bastet_agent_os.context_engine import build_context
from bastet_agent_os.workflow import parse_stages


def test_project_room_members_follow_project_roles(seeded):
    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','ag1','developer',10)")
    room_id = collaboration.ensure_room(seeded, "proj1")
    assert collaboration.ensure_room(seeded, "proj1") == room_id
    assert [member["id"] for member in collaboration.members(seeded, "proj1")] == ["ag1"]

    message_id = collaboration.post(
        seeded, "proj1", author_type="pm", author_id="user",
        kind="assignment", content="請 ag1 接手實作。", meta={"job_id": "job1"},
    )
    message = collaboration.messages(seeded, "proj1")[-1]
    assert message["id"] == message_id
    assert message["meta"] == {"job_id": "job1"}


def test_handoff_is_written_to_room_and_selected_for_next_agent(seeded):
    handoff_id = collaboration.record_handoff(
        seeded, project_id="proj1", job_id="job1", run_id="run1",
        from_stage="implement", to_stage="review", agent_id="ag1",
        summary="完成 context selector", paths=["src/context.py"],
        verification=["pytest tests/test_context.py"], risks=["migration"],
    )
    assert collaboration.messages(seeded, "proj1")[-1]["meta"]["handoff_id"] == handoff_id

    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    text, report = build_context(seeded, job, "review", stage_role="reviewer")
    assert "完成 context selector" in text
    assert "src/context.py" in text
    included = {item["bucket"] for item in report.sections if item["included"]}
    assert "handoff" in included


def _commit(repo, message):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True,
                   capture_output=True)


def test_incremental_gate_reuses_only_unaffected_passes(orch, seeded, tmp_path):
    repo = tmp_path / "evidence-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "src").mkdir()
    (repo / "docs").mkdir()
    (repo / "src" / "a.py").write_text("A = 1\n")
    (repo / "src" / "b.py").write_text("B = 1\n")
    (repo / "docs" / "guide.md").write_text("v1\n")
    _commit(repo, "initial")

    log = tmp_path / "executed.log"
    stage = parse_stages([{
        "name": "test", "gate": "tests-pass",
        "gate_config": {
            "command": "true",
            "cases": [
                {"id": "a", "command": f"echo a >> {log}",
                 "covered_paths": ["src/a.py"]},
                {"id": "b", "command": f"echo b >> {log}",
                 "covered_paths": ["src/b.py"]},
            ],
        },
    }])[0]
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")

    first = orch._judge_incremental_tests(stage, str(repo), job, "run1")
    assert first.verdict == "passed"
    assert log.read_text().splitlines() == ["a", "b"]

    (repo / "docs" / "guide.md").write_text("v2\n")
    _commit(repo, "docs only")
    second = orch._judge_incremental_tests(stage, str(repo), job, "run1")
    assert second.verdict == "passed"
    assert second.detail.count("SKIP") == 2
    assert log.read_text().splitlines() == ["a", "b"]

    (repo / "src" / "a.py").write_text("A = 2\n")
    _commit(repo, "change a")
    third = orch._judge_incremental_tests(stage, str(repo), job, "run1")
    assert third.verdict == "passed"
    assert "PASSED a" in third.detail and "SKIP b" in third.detail
    assert log.read_text().splitlines() == ["a", "b", "a"]

    evidence = seeded.query("SELECT covered_paths_json FROM test_evidence ORDER BY rowid")
    assert json.loads(evidence[-1]["covered_paths_json"]) == ["src/a.py"]
