"""The rework loop: a failed gate is handled by the agents, not by a human.

The old behaviour stopped the card and sent a one-line notification, which put
a person in the position of doing what the writing agent is better placed to do.
These tests pin the new contract, including the parts that must still stop:
a stage declared `on_fail: block`, a pipeline with no writable stage to return
to, and a loop that has spent its cycles without converging.
"""

import json
from pathlib import Path

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.executors.base import RunResult

pytestmark = pytest.mark.asyncio


def audit(db, action: str) -> list[dict]:
    return [json.loads(r["detail_json"]) for r in db.query(
        "SELECT detail_json FROM audit_log WHERE action=? ORDER BY id", (action,))]


def stages_of(db, job_id: str) -> list[str]:
    # rowid, not id: run ids are random tokens, so ordering by them is arbitrary
    return [r["stage"] for r in db.query(
        "SELECT stage FROM runs WHERE job_id=? ORDER BY rowid", (job_id,))]


def fixes(path_name: str, summary: str = "fixed it"):
    """A scripted agent that actually changes the workdir, so the gate that
    failed can genuinely pass on the next pass."""
    def run(task):
        (Path(task.workdir) / path_name).write_text("done\n")
        return RunResult(status="succeeded", summary=summary)
    return run


async def test_failing_test_gate_goes_back_and_the_job_finishes(orch, seeded):
    """The headline behaviour: tests fail, the writing stage fixes them, the
    pipeline carries on. Nobody is asked to approve anything."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "tests-pass",
         "gate_config": {"command": "test -f fixed.txt"}},
        {"name": "ship", "gate": "auto"},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="wrote code, forgot the fix"))
    SCRIPT.append(fixes("fixed.txt"))
    SCRIPT.append(RunResult(status="succeeded", summary="shipped"))

    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"
    assert stages_of(seeded, job_id) == ["implement", "implement", "ship"]
    events = audit(seeded, "job.rework")
    assert len(events) == 1
    assert events[0]["failed_stage"] == "implement"
    assert events[0]["back_to"] == "implement"
    assert events[0]["cycle"] == 1
    assert job["rework_note"] is None       # cleared once the gate passed


async def test_the_fixing_agent_is_told_what_failed(orch, seeded):
    """A retry that does not carry the failure is just the same run again."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "tests-pass",
         "gate_config": {"command": "test -f fixed.txt || (echo 'AssertionError: "
                                    "expected 3 got 4' && exit 1)"}},
    ])
    seen: list[str] = []

    def capture(task):
        seen.append(task.prompt)
        (Path(task.workdir) / "fixed.txt").write_text("y\n")
        return RunResult(status="succeeded", summary="fixed")

    SCRIPT.append(RunResult(status="succeeded", summary="first pass"))
    SCRIPT.append(capture)

    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    brief = seen[0]
    assert "AssertionError: expected 3 got 4" in brief    # the real output
    assert "第 1/3 次返工" in brief                        # and where it is in the loop
    # the shortcuts that would pass the gate without fixing anything
    assert "不要為了過關而改測試指令" in brief
    assert "恆真" in brief


async def test_a_read_only_reviewer_hands_back_to_the_writer(orch, seeded):
    """A reviewer cannot fix what it rejected, so the work goes past it to the
    last stage that can write."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "auto"},
        {"name": "review", "gate": "agent-review", "read_only": True},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="v1"))
    SCRIPT.append(RunResult(status="succeeded", summary="no",
                            structured_verdict={"verdict": "reject",
                                                "reasons": ["race in the retry path"]}))
    SCRIPT.append(RunResult(status="succeeded", summary="v2 fixes the race"))
    SCRIPT.append(RunResult(status="succeeded", summary="ok",
                            structured_verdict={"verdict": "approve"}))

    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    assert stages_of(seeded, job_id) == ["implement", "review", "implement", "review"]
    assert audit(seeded, "job.rework")[0]["back_to"] == "implement"


async def test_the_loop_is_capped_and_then_asks_a_human(orch, seeded):
    """An agent that has failed three times is not converging. The cap is what
    keeps 'self-healing' from meaning 'burns tokens forever'."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "tests-pass", "max_cycles": 2,
         "gate_config": {"command": "exit 1"}},
    ])
    for _ in range(6):
        SCRIPT.append(RunResult(status="succeeded", summary="tried"))

    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "blocked"
    assert job["rework_count"] == 2
    assert len(stages_of(seeded, job_id)) == 3      # first run + 2 reworks, no more
    blocked = audit(seeded, "job.blocked")[-1]
    assert blocked["cycles"] == 2
    assert "返工 2 次" in blocked["reason"]


async def test_on_fail_block_stops_immediately(orch, seeded):
    """Some gates exist precisely to stop: a release step should not be retried
    in a loop by an agent."""
    add_template(seeded, "dev", [
        {"name": "release", "gate": "tests-pass", "on_fail": "block",
         "gate_config": {"command": "exit 1"}},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="tried"))

    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["rework_count"]) == ("blocked", 0)
    assert len(stages_of(seeded, job_id)) == 1
    assert "on_fail: block" in audit(seeded, "job.blocked")[-1]["reason"]


async def test_nothing_to_hand_back_to_blocks_with_that_reason(orch, seeded):
    """A pipeline of only read-only stages has nobody who can fix anything —
    the one case where stopping is the honest answer."""
    add_template(seeded, "dev", [
        {"name": "audit", "gate": "agent-review", "read_only": True},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="rejected",
                            structured_verdict={"verdict": "reject"}))

    orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    reason = audit(seeded, "job.blocked")[-1]["reason"]
    assert "沒有任何前面的可寫階段" in reason


async def test_an_unrunnable_test_command_is_also_handed_back(orch, seeded):
    """`npm ERR! Missing script: "test:e2e"` used to stop the card dead. It is a
    real gap in the project, and the agent that writes the project can close
    it — with the brief spelling out that faking a green exit is not a fix."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "tests-pass",
         "gate_config": {"command": "npm run test:e2e"}},
    ])
    seen: list[str] = []

    def capture(task):
        seen.append(task.prompt)
        return RunResult(status="succeeded", summary="looked at it")

    SCRIPT.append(RunResult(status="succeeded", summary="first pass"))
    for _ in range(4):
        SCRIPT.append(capture)

    orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    assert seen, "the card was never handed back"
    assert "跑不起來" in seen[0]
    assert "不是寫一個空殼讓它 exit 0" in seen[0]
    # and it is recorded as a config problem, not as failing tests
    assert audit(seeded, "job.rework")[0]["config_error"] is True


async def test_blocked_notification_carries_enough_to_act_on(orch, seeded):
    """The complaint that started this: `🟠 job.blocked: job_abc stage tests`
    tells you something broke and nothing about what."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "tests-pass", "max_cycles": 1,
         "on_fail": "block",
         "gate_config": {"command": "echo 'FAILED tests/test_cat.py::test_walk "
                                    "- AssertionError' && exit 1"}},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="tried"))
    job_id = orch.dispatch(req(template_id="dev", title="貓咪散步預約"))
    await orch.wait_idle()

    blocked = audit(seeded, "job.blocked")[-1]
    assert blocked["gate"] == "tests-pass"
    assert "test_walk" in blocked["detail"]         # the actual failing test
    assert "AssertionError" in blocked["reason"]
    job = seeded.one("SELECT title FROM jobs WHERE id=?", (job_id,))
    assert job["title"] == "貓咪散步預約"


async def test_the_work_survives_cleanup(orch, seeded, repo):
    """Found on a live host: a job ran its rework loop, the tests went green,
    and then cleanup deleted the fix. `git worktree remove --force` discards
    uncommitted changes and no stage commits anything, so the bastet/<job>
    branch still pointed at the commit the job started from — the agent's work
    recoverable only by hand-applying a diff file."""
    import subprocess

    add_template(seeded, "dev", [
        {"name": "implement", "gate": "tests-pass",
         "gate_config": {"command": "test -f fixed.txt"}},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="forgot"))
    SCRIPT.append(fixes("fixed.txt"))

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               title="修好加法"))
    await orch.wait_idle()

    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    branch = f"bastet/{job_id}"
    files = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only",
                            branch], capture_output=True, text=True)
    assert "fixed.txt" in files.stdout, (
        f"the agent's work is not on {branch}: {files.stdout!r}")
    message = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s%n%b",
                              branch], capture_output=True, text=True).stdout
    assert "修好加法" in message and job_id in message   # traceable to the card
    # and the project's own branch is untouched: merging stays a deliberate step
    on_master = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r",
                                "--name-only", "HEAD"], capture_output=True, text=True)
    assert "fixed.txt" not in on_master.stdout


async def test_a_job_that_changed_nothing_makes_no_commit(orch, seeded, repo):
    """An empty commit on every read-only run would be noise in the history."""
    import subprocess

    before = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    add_template(seeded, "dev", [{"name": "look", "gate": "auto", "read_only": True}])
    SCRIPT.append(RunResult(status="succeeded", summary="read it, changed nothing"))

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()

    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", f"bastet/{job_id}"],
                         capture_output=True, text=True).stdout.strip()
    assert tip == before


async def test_a_job_whose_driver_died_on_restart_resumes_itself(orch, seeded):
    """Found live: restarting the service to deploy killed a running stage, and
    the card sat at `in_progress` for half an hour with no process behind it.
    Startup orphaned the runs but left the job, the project runner only resumes
    projects with undispatched plan tasks, and retry refuses anything that is not
    blocked — so no button in the product would touch it."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "auto"},
        {"name": "ship", "gate": "auto"},
    ])
    # the state a restart leaves behind: job in_progress, its run orphaned
    seeded.write("INSERT INTO jobs(id, project_id, template_id, stages_snapshot_json, "
                 "title, spec_md, stage, status, default_agent_id, created_at, "
                 "updated_at) VALUES('jobkilled','proj1','dev',?,'中斷的任務','spec',"
                 "'implement','in_progress','fakebot',datetime('now'),datetime('now'))",
                 (json.dumps([{"name": "implement", "gate": "auto"},
                              {"name": "ship", "gate": "auto"}]),))
    seeded.write("INSERT INTO runs(id, job_id, stage, attempt, agent_id, "
                 "executor_type, status) VALUES('runkilled','jobkilled','implement',"
                 "1,'fakebot','fake','orphaned')")
    SCRIPT.append(RunResult(status="succeeded", summary="picked it up again"))
    SCRIPT.append(RunResult(status="succeeded", summary="shipped"))

    out = orch.resume_interrupted_jobs()
    await orch.wait_idle()

    assert "jobkilled" in out["resumed"]
    job = seeded.one("SELECT * FROM jobs WHERE id='jobkilled'")
    assert job["status"] == "done"                    # it finished on its own
    assert audit(seeded, "job.resumed")[0]["stage"] == "implement"


async def test_an_interrupted_job_on_a_paused_project_is_not_restarted(orch, seeded):
    """Pause means the human asked for it to stop. But the card must stop
    claiming it is running, so it is blocked with the real reason."""
    seeded.write("UPDATE projects SET status='paused' WHERE id='proj1'")
    seeded.write("INSERT INTO jobs(id, project_id, template_id, stages_snapshot_json, "
                 "title, spec_md, stage, status, default_agent_id, created_at, "
                 "updated_at) VALUES('jobpaused','proj1','dev',?,'暫停中','spec',"
                 "'implement','in_progress','fakebot',datetime('now'),datetime('now'))",
                 (json.dumps([{"name": "implement", "gate": "auto"}]),))

    out = orch.resume_interrupted_jobs()

    assert out["resumed"] == [] and "jobpaused" in out["parked"]
    job = seeded.one("SELECT status FROM jobs WHERE id='jobpaused'")
    assert job["status"] == "blocked"
    reason = audit(seeded, "job.blocked")[-1]["reason"]
    assert "服務重啟時中斷" in reason and "paused" in reason


async def test_resume_leaves_a_live_job_alone(orch, seeded):
    """A driver that is genuinely running must not be duplicated — two drivers on
    one job is the failure mode this whole path is trying to avoid."""
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    seeded.write("INSERT INTO jobs(id, project_id, template_id, stages_snapshot_json, "
                 "title, spec_md, stage, status, default_agent_id, created_at, "
                 "updated_at) VALUES('joblive','proj1','dev',?,'跑著的','spec',"
                 "'implement','in_progress','fakebot',datetime('now'),datetime('now'))",
                 (json.dumps([{"name": "implement", "gate": "auto"}]),))
    seeded.write("INSERT INTO runs(id, job_id, stage, attempt, agent_id, "
                 "executor_type, status) VALUES('runlive','joblive','implement',"
                 "1,'fakebot','fake','running')")
    orch._live["runlive"] = ("executor", "handle")     # as a real driver would

    out = orch.resume_interrupted_jobs()

    assert "joblive" not in out["resumed"] and "joblive" not in out["parked"]
    assert seeded.one("SELECT status FROM jobs WHERE id='joblive'")["status"] \
        == "in_progress"


async def test_a_human_retry_refills_the_rework_budget(orch, seeded):
    """Live case: a transient network failure burned all 3 cycles; the operator
    pressed retry three times and got three instant re-blocks — the retried
    reviewer kept rejecting the same diff, and the spent budget meant the card
    could never travel back to the stage that would regenerate the work."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "auto"},
        {"name": "review", "gate": "agent-review", "read_only": True,
         "max_cycles": 1},
    ])
    # transient-broken world: implement "works", review honestly rejects twice
    SCRIPT.append(RunResult(status="succeeded", summary="v1（網路掛了，什麼都沒生成）"))
    SCRIPT.append(RunResult(status="succeeded", summary="no",
                            structured_verdict={"verdict": "reject",
                                                "reasons": ["nothing was generated"]}))
    SCRIPT.append(RunResult(status="succeeded", summary="v2 still nothing"))
    SCRIPT.append(RunResult(status="succeeded", summary="no",
                            structured_verdict={"verdict": "reject",
                                                "reasons": ["still nothing"]}))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["rework_count"]) == ("blocked", 1)  # budget spent

    # the world is fixed; the human presses retry at the review stage
    SCRIPT.append(RunResult(status="succeeded", summary="no",
                            structured_verdict={"verdict": "reject",
                                                "reasons": ["same old diff"]}))
    SCRIPT.append(RunResult(status="succeeded", summary="v3 real assets this time"))
    SCRIPT.append(RunResult(status="succeeded", summary="ok",
                            structured_verdict={"verdict": "approve"}))
    orch.retry(job_id, renew_recovery_lease=True)
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done", (
        "with a refilled budget, the failed review hands back to implement "
        "instead of instantly re-blocking")


async def test_retry_with_an_explicit_agent_actually_uses_it(orch, seeded):
    """Live case: the review stage's role mapping kept selecting the agent whose
    runs were failing on a vendor bug; the operator retried with a different
    agent and the role mapping silently won — same agent, same failure. An
    explicit choice on retry must outrank the mapping, once."""
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, "
                 "created_at, updated_at) VALUES('fakebot2','fakebot2','Fake2',"
                 "'fake',datetime('now'),datetime('now'))")
    seeded.write("INSERT INTO project_agent_roles(project_id, agent_id, role, "
                 "preference) VALUES('proj1','fakebot','reviewer',1)")
    add_template(seeded, "dev", [
        {"name": "review", "gate": "agent-review", "on_fail": "block"},
    ])
    SCRIPT.append(RunResult(status="failed", summary="vendor 400: bad schema"))

    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] \
        == "blocked"
    assert seeded.one("SELECT agent_id FROM runs WHERE job_id=? "
                      "ORDER BY rowid DESC LIMIT 1", (job_id,))["agent_id"] \
        == "fakebot"                                # the role mapping's pick

    SCRIPT.append(RunResult(status="succeeded", summary="ok",
                            structured_verdict={"verdict": "approve"}))
    orch.retry(job_id, agent_id="fakebot2", user="manfred")
    await orch.wait_idle()

    last = seeded.one("SELECT agent_id FROM runs WHERE job_id=? "
                      "ORDER BY rowid DESC LIMIT 1", (job_id,))
    assert last["agent_id"] == "fakebot2", (
        "the explicit retry choice lost to the role mapping again")
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"


async def test_the_override_is_one_shot(orch, seeded):
    """The override is for the stage the human was looking at; the next stage
    goes back to the role mapping."""
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, "
                 "created_at, updated_at) VALUES('fakebot2','fakebot2','Fake2',"
                 "'fake',datetime('now'),datetime('now'))")
    seeded.write("INSERT INTO project_agent_roles(project_id, agent_id, role, "
                 "preference) VALUES('proj1','fakebot','engineer',1)")
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "auto", "role": "engineer",
         "on_fail": "block"},
        {"name": "ship", "gate": "auto", "role": "engineer"},
    ])
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    SCRIPT.append(RunResult(status="succeeded", summary="fixed by 2"))
    SCRIPT.append(RunResult(status="succeeded", summary="shipped"))
    orch.retry(job_id, agent_id="fakebot2")
    await orch.wait_idle()

    agents = [r["agent_id"] for r in seeded.query(
        "SELECT agent_id FROM runs WHERE job_id=? ORDER BY rowid", (job_id,))]
    assert agents == ["fakebot", "fakebot2", "fakebot"], (
        "override applies to the retried stage only; the mapping resumes after")
    assert seeded.one("SELECT agent_override FROM jobs WHERE id=?",
                      (job_id,))["agent_override"] is None


async def test_the_stage_timeout_reaches_the_executor(orch, seeded):
    seen: list[int] = []

    def capture(task):
        seen.append(task.timeout_s)
        return RunResult(status="succeeded", summary="ok")

    add_template(seeded, "dev", [
        {"name": "heavy", "gate": "auto", "timeout_s": 7200},
        {"name": "light", "gate": "auto"},
    ])
    SCRIPT.append(capture)
    SCRIPT.append(capture)
    orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    assert seen == [7200, 3600]      # declared budget, then the dispatch default


async def test_work_walks_back_when_the_same_stage_keeps_failing(orch, seeded):
    """Standing still does not converge.

    Live case: an E2E stage failed one test, the rework target was the E2E
    stage itself, and the tester re-ran the same failing test nine times over
    four hours while nobody touched the product code it was failing on. The
    second hand-back must reach someone earlier."""
    add_template(seeded, "dev", [
        {"name": "implement", "role": "engineer", "gate": "auto"},
        {"name": "review", "role": "reviewer", "gate": "agent-review",
         "read_only": True},
        {"name": "e2e", "role": "tester", "gate": "tests-pass",
         "gate_config": {"command": "test -f fixed.txt"}},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="wrote code"))
    SCRIPT.append(RunResult(status="succeeded", summary="looks fine",
                            structured_verdict={"verdict": "approve", "reasons": []}))
    SCRIPT.append(RunResult(status="succeeded", summary="ran tests"))   # gate fails
    SCRIPT.append(RunResult(status="succeeded", summary="reran tests"))  # 1st handback
    SCRIPT.append(fixes("fixed.txt"))            # 2nd handback: the implementer fixes it
    SCRIPT.append(RunResult(status="succeeded", summary="looks fine",
                            structured_verdict={"verdict": "approve", "reasons": []}))
    SCRIPT.append(RunResult(status="succeeded", summary="tests green"))

    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    handbacks = [e["back_to"] for e in audit(seeded, "job.rework")]
    assert handbacks == ["e2e", "implement"], \
        f"the work never walked back to anyone who could fix it: {handbacks}"
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


async def test_a_read_only_reviewer_is_never_a_target(orch, seeded):
    """Walking back must skip reviewers: one cannot fix what it just rejected."""
    from bastet_agent_os.workflow import parse_stages, rework_target_for
    stages = parse_stages([
        {"name": "implement", "gate": "auto"},
        {"name": "review", "gate": "agent-review", "read_only": True},
        {"name": "e2e", "gate": "tests-pass", "gate_config": {"command": "true"}},
    ])
    assert rework_target_for(stages, 2, attempt=0) == 2      # itself first
    assert rework_target_for(stages, 2, attempt=1) == 0      # skips the reviewer
    assert rework_target_for(stages, 2, attempt=9) == 0      # clamps, never None
    # a failed review still hands back to the writer, first try
    assert rework_target_for(stages, 1, attempt=0) == 0


async def test_an_explicit_rework_target_is_never_overridden(orch, seeded):
    """The template author's choice outranks the walk-back."""
    from bastet_agent_os.workflow import parse_stages, rework_target_for
    stages = parse_stages([
        {"name": "design", "gate": "auto"},
        {"name": "implement", "gate": "auto"},
        {"name": "e2e", "gate": "tests-pass", "gate_config": {"command": "true"},
         "rework_target": "implement"},
    ])
    for attempt in (0, 1, 5):
        assert rework_target_for(stages, 2, attempt=attempt) == 1


async def test_each_stage_commits_its_own_work(orch, seeded, repo):
    """Leaving a stage's output uncommitted meant every later stage reasoned
    about a tree matching no commit. Live cost: a reviewer refused test
    evidence because the scripts that produced it were uncommitted changes on
    top of HEAD — and it was right, nothing bound the evidence to the content
    under review."""
    import subprocess
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "auto"},
        {"name": "verify", "gate": "auto"},
    ])
    SCRIPT.append(fixes("feature.txt"))
    SCRIPT.append(fixes("evidence.log"))

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()

    job = seeded.one("SELECT worktree_path FROM jobs WHERE id=?", (job_id,))
    branch = f"bastet/{job_id}"
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s", branch],
                         capture_output=True, text=True).stdout.strip().split("\n")
    subjects = [line for line in log if line.startswith("bastet(")]
    assert any("implement" in s for s in subjects), f"stage commits missing: {log}"
    assert any("verify" in s for s in subjects), f"stage commits missing: {log}"
    # and the tree a later stage sees is clean, which is the whole point
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    assert status.strip() == "" or job["worktree_path"] is None


def test_the_review_brief_states_a_satisfiable_freshness_rule():
    """Demanding that evidence name the commit containing it is a loop with no
    exit: committing the log changes the tip."""
    from bastet_agent_os.workflow import REVIEW_INSTRUCTIONS
    assert "ancestor" in REVIEW_INSTRUCTIONS
    assert "no product code" in REVIEW_INSTRUCTIONS
    assert "uncommitted" in REVIEW_INSTRUCTIONS


async def test_the_engines_scratch_dir_is_never_committed(orch, seeded, repo):
    """`._bastet/` is the engine↔agent boundary — previews, verdicts, inbox —
    not product code. Committing it (which `add -A` did) meant every run
    dirtied the tree again by regenerating those files, so the reviewer kept
    seeing "uncommitted modifications" and refusing the evidence. The card the
    operator was watching went back to step one and hit the same wall."""
    import subprocess
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])

    def writes_both(task):
        from pathlib import Path
        work = Path(task.workdir)
        (work / "product.txt").write_text("real change")
        preview = work / "._bastet" / "preview"
        preview.mkdir(parents=True, exist_ok=True)
        (preview / "screenshot.png").write_bytes(b"png")
        return RunResult(status="succeeded", summary="did it")
    SCRIPT.append(writes_both)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()

    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only",
         f"bastet/{job_id}"], capture_output=True, text=True).stdout
    assert "product.txt" in listing, "the actual work was not committed"
    assert "._bastet" not in listing, \
        f"the engine's scratch area was committed and will dirty every later run:\n{listing}"


async def test_a_repository_tracked_preview_is_updated_not_deleted(
        orch, seeded, repo):
    """Tracked acceptance evidence is repository data, not engine scratch."""
    import subprocess
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    tracked = repo / "._bastet" / "preview" / "acceptance.txt"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("old evidence")
    subprocess.run(["git", "-C", str(repo), "add", "-f", str(tracked)], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "track evidence"],
                   check=True)

    def updates_tracked_evidence(task):
        from pathlib import Path
        (Path(task.workdir) / "._bastet" / "preview" /
         "acceptance.txt").write_text("fresh evidence")
        return RunResult(status="succeeded", summary="updated")
    SCRIPT.append(updates_tracked_evidence)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()
    shown = subprocess.run(
        ["git", "-C", str(repo), "show",
         f"bastet/{job_id}:._bastet/preview/acceptance.txt"],
        capture_output=True, text=True, check=True).stdout
    assert shown == "fresh evidence"
