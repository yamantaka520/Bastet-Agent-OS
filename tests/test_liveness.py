"""Nothing a run spawns may wait for a human, and a working run must look alive.

Both halves come from one live incident. A PM stage ran `npm exec playwright
--version`; npx wanted to install the package and asked "Ok to proceed? (y)".
Its stdin was a tty, so it waited — 52 minutes, 2 seconds of CPU, the agent
blocked on its own child, the card frozen behind a question no human would ever
see. And nobody could tell, because that executor printed nothing until it
exited: the board had no heartbeat for the whole 52 minutes.
"""

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

from bastet_agent_os.executors.agy import _progress_text, unwrap_envelope
from bastet_agent_os.executors.base import NONINTERACTIVE_ENV, TaskSpec, run_env

CLI_EXECUTORS = ["claude_code.py", "codex.py", "hermes.py", "grok.py", "agy.py"]
EXEC_DIR = Path(__file__).resolve().parents[1] / "src" / "bastet_agent_os" / "executors"


def _spawn_kwargs(source: str) -> list[dict[str, ast.expr]]:
    """Every create_subprocess_exec call's keyword arguments, parsed.

    Parsed, not grepped: a previous fix to these same call sites was silently
    swallowed by a trailing comment, and only an AST test caught it."""
    calls = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None)
        if name in ("create_subprocess_exec", "create_subprocess_shell"):
            calls.append({kw.arg: kw.value for kw in node.keywords if kw.arg})
    return calls


@pytest.mark.parametrize("filename", CLI_EXECUTORS)
def test_no_child_can_wait_on_a_prompt(filename):
    """stdin is /dev/null, so an interactive prompt reads EOF and fails fast."""
    calls = _spawn_kwargs((EXEC_DIR / filename).read_text())
    assert calls, f"{filename}: no subprocess spawn found"
    for kwargs in calls:
        assert "stdin" in kwargs, f"{filename}: spawn inherits stdin — a child can hang on a prompt"
        assert ast.unparse(kwargs["stdin"]).endswith("DEVNULL"), \
            f"{filename}: stdin is not DEVNULL"


def _spawning_functions(source: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function that spawns a child process."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if any(getattr(c.func, "attr", None) in ("create_subprocess_exec",
                                                 "create_subprocess_shell")
               for c in ast.walk(node) if isinstance(c, ast.Call)):
            out.append(node)
    return out


@pytest.mark.parametrize("filename", CLI_EXECUTORS)
def test_every_executor_spawns_through_run_env(filename):
    """The non-interactive env must reach the child — and its grandchildren.

    Checked on the function that spawns, not on the `env=` expression: most of
    these assign `env = run_env(task)` and pass the local. What must never come
    back is the old `{**os.environ, ...}` literal, which silently drops the
    no-prompt block."""
    source = (EXEC_DIR / filename).read_text()
    functions = _spawning_functions(source)
    assert functions, f"{filename}: no subprocess spawn found"
    for function in functions:
        body = ast.unparse(function)
        assert "run_env(" in body, \
            f"{filename}.{function.name}: builds an env without run_env — npx/git can prompt again"
        assert "os.environ" not in body, \
            f"{filename}.{function.name}: raw os.environ bypasses the non-interactive block"


def test_run_env_disables_the_prompts_that_hung_a_live_run():
    task = TaskSpec(run_id="r1", prompt="p", workdir="/tmp")
    env = run_env(task)
    assert env["npm_config_yes"] == "true"     # npx installs without asking
    assert env["GIT_TERMINAL_PROMPT"] == "0"   # git fails instead of asking
    assert env["CI"] == "1"
    assert "PATH" in env                       # the real environment is still there


def test_pool_credentials_outrank_our_defaults():
    task = TaskSpec(run_id="r1", prompt="p", workdir="/tmp",
                    extra_env={"CI": "0", "BASTET_RES_X_KEY": "secret"})
    env = run_env(task, EXTRA="1")
    assert env["CI"] == "0"                    # a resource may override deliberately
    assert env["BASTET_RES_X_KEY"] == "secret"
    assert env["EXTRA"] == "1"
    assert set(NONINTERACTIVE_ENV) - set(env) == set()


# --- agy: the executor that printed nothing for 53 minutes --------------------

def test_agy_streams_instead_of_waiting_for_the_end():
    source = inspect.getsource(
        __import__("bastet_agent_os.executors.agy", fromlist=["AgyExecutor"]).AgyExecutor.start)
    assert '"stream-json"' in source, "agy back on one-shot json: the card goes dark again"


def test_unwrap_envelope_accepts_both_shapes():
    streamed = {"event": "result",
                "result": {"status": "SUCCESS", "response": "PONG\n",
                           "usage": {"input_tokens": 15076}}}
    one_shot = {"status": "SUCCESS", "response": "PONG\n",
                "usage": {"input_tokens": 15076}}
    for shape in (streamed, one_shot):
        envelope = unwrap_envelope(shape)
        assert envelope["status"] == "SUCCESS"
        assert envelope["usage"]["input_tokens"] == 15076
    assert unwrap_envelope(None) == {}
    assert unwrap_envelope({"event": "result", "result": "not a dict"}) == \
        {"event": "result", "result": "not a dict"}


# verbatim from the validation host: `agy --output-format stream-json -p "Reply
# with exactly: PONG"`. The last line is the envelope, wrapped — reading the
# wrapper as the envelope makes status None, i.e. every agy run "fails".
AGY_STREAM = (
    '{"event":"init","conversation_id":"b59c","init":{"cwd":"/tmp"}}\n'
    '{"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE",'
    '"step_type":"agent_response","text_delta":"PONG"}}\n'
    '{"event":"result","result":{"conversation_id":"b59c","status":"SUCCESS",'
    '"response":"PONG\\n","num_turns":1,"usage":{"input_tokens":15076,'
    '"output_tokens":62,"thinking_tokens":57,"cache_read_tokens":0}}}\n'
)


@pytest.mark.parametrize("raw_stdout,label", [
    (AGY_STREAM, "stream-json"),
    ('{"status":"SUCCESS","response":"PONG\\n","usage":{"input_tokens":15076,'
     '"output_tokens":62,"thinking_tokens":57,"cache_read_tokens":0}}\n', "json"),
])
def test_agy_result_reads_a_real_transcript(raw_stdout, label):
    """The end-to-end path, not just the helper: a passing unwrap_envelope test
    while result() ignored it would have proved nothing."""
    from bastet_agent_os.executors.agy import AgyExecutor, AgyHandle

    handle = AgyHandle(task=TaskSpec(run_id="r1", prompt="p", workdir="/tmp"))
    handle.process = _FakeProcess(returncode=0)
    handle.raw_stdout = raw_stdout
    result = asyncio.run(AgyExecutor().result(handle))
    assert result.status == "succeeded", f"{label}: a successful run read as {result.status}"
    assert result.summary.strip() == "PONG"
    assert result.tokens_in == 15076
    assert result.tokens_out == 62 + 57          # thinking tokens are output-side


def test_agy_progress_prefers_the_agents_own_words():
    say = {"event": "step_update",
           "step_update": {"state": "ACTIVE", "step_type": "agent_response",
                           "text_delta": "PONG"}}
    work = {"event": "step_update",
            "step_update": {"state": "ACTIVE", "step_type": "run_command"}}
    assert _progress_text(say) == "PONG"
    assert _progress_text(work) == "[run_command]"
    # protocol noise must not beat: an idle board line is worse than none
    assert _progress_text({"event": "init", "init": {}}) == ""
    assert _progress_text({"event": "step_update",
                           "step_update": {"state": "DONE", "step_type": "unknown"}}) == ""
    assert _progress_text(None) == ""


# --- the beat that does not depend on the executor talking --------------------

class _FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode


@pytest.fixture
def orch_with_running_run(orch):
    """The seeded fixture's `run1` is already status='running'."""
    return orch, "run1", orch.db


def test_a_silent_run_still_reports_alive(orch_with_running_run):
    """A one-shot executor says nothing for its whole life; the card must not
    look dead for its whole life."""
    orch, run_id, db = orch_with_running_run
    orch.LIVENESS_PERIOD_S = 0.01

    async def beat_briefly():
        task = asyncio.create_task(
            orch._liveness_beat(run_id, type("H", (), {"process": _FakeProcess()})()))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(beat_briefly())
    row = db.one("SELECT heartbeat_at, progress_text FROM runs WHERE id=?", (run_id,))
    assert row["heartbeat_at"], "a live run reported no heartbeat"
    # it was alive, not talking — claiming words it never said would be a lie
    assert row["progress_text"] is None


def test_the_beat_stops_when_the_process_exits(orch_with_running_run):
    orch, run_id, db = orch_with_running_run
    orch.LIVENESS_PERIOD_S = 0.01

    async def beat_briefly():
        task = asyncio.create_task(
            orch._liveness_beat(run_id, type("H", (), {"process": _FakeProcess(returncode=0)})()))
        await asyncio.sleep(0.05)
        return task.done()

    assert asyncio.run(beat_briefly()), "the beat outlived the process it claims to watch"
    assert db.one("SELECT heartbeat_at FROM runs WHERE id=?", (run_id,))["heartbeat_at"] is None


def test_a_beat_failure_never_breaks_the_run(orch_with_running_run):
    orch, run_id, _ = orch_with_running_run
    orch.LIVENESS_PERIOD_S = 0.01

    def explode(*a, **k):
        raise RuntimeError("database is locked")

    orch.db.write = explode

    async def beat_briefly():
        task = asyncio.create_task(
            orch._liveness_beat(run_id, type("H", (), {"process": _FakeProcess()})()))
        await asyncio.sleep(0.05)
        return task
    task = asyncio.run(beat_briefly())
    assert task.done() and task.exception() is None


# --- the worktree's git metadata lives outside the worktree ---------------------

def test_worktree_git_dir_reads_the_gitdir_pointer(tmp_path):
    """The fact that broke every git write in a sandbox: a linked worktree's
    `.git` is a FILE pointing into the main repository."""
    from bastet_agent_os.executors.base import worktree_git_dir

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /home/u/repo/.git/worktrees/job_a\n")
    assert worktree_git_dir(str(wt)) == "/home/u/repo/.git/worktrees/job_a"

    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    assert worktree_git_dir(str(plain)) is None      # ordinary checkout

    assert worktree_git_dir(str(tmp_path / "nope")) is None      # not a repo
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / ".git").write_text("something else entirely")
    assert worktree_git_dir(str(odd)) is None        # never guess
