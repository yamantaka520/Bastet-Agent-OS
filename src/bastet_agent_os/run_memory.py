"""What every run leaves behind in Agent Memory OS.

Bastet's pitch is that a team of agents accumulates knowledge instead of
starting from zero each time. That only holds if runs actually write memories —
and until now only the `bastet-lite` executor did. A project driven by Claude
Code, Codex, Grok or agy went through its whole lifecycle contributing nothing,
so the memory bucket in every later context pack was empty. Recall was reading
from a store nothing wrote to.

Writing happens here, in the orchestrator's path, so it works the same for every
executor: what a stage did, what a gate rejected, and how a job ended. The
memory is attributed to the agent that ran (its AMOS id) and carries the
project/team visibility grants, which is what lets that project's agents recall
it and keeps it out of other projects' packs.

Nothing here may raise: a memory write failing must never take down a run. But
it is logged at warning — a silently skipped write is exactly how the previous
gap survived for so long.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

log = logging.getLogger("bastet.memory")

# AMOS scope is the bare level; the ids travel as visibility grants
CONTENT_LIMIT = 4000

# Bastet's own writes when no agent is attributable (engine-level events)
ENGINE_OWNER = "bastet"

TYPES = {"note", "decision", "warning", "procedure", "fact", "preference",
         "environment"}


def never_fails(default=None):
    """Enforce the "a memory write cannot break a run" rule at the boundary.

    Relying on each internal call to be individually safe is how a guarantee
    quietly stops holding: one un-guarded line (an import, an attribute lookup
    on an unexpected object) and a failing memory layer takes the job with it."""
    def wrap(fn):
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                log.warning("memory %s failed (%s): %s", fn.__name__,
                            type(exc).__name__, exc)
                return default
        return guarded
    return wrap


def _client():
    try:
        from agent_memory_os.client import MemoryClient

        return MemoryClient()
    except Exception as exc:                     # AMOS optional at runtime
        log.debug("AMOS unavailable (%s)", type(exc).__name__)
        return None


def team_of(db, project_id: str) -> str:
    row = db.one("SELECT team_id FROM projects WHERE id=?", (project_id,))
    return row["team_id"] if row and row["team_id"] else ""


def grants(db, project_id: str) -> list[str]:
    """Who may recall this: the project, and the team above it.

    The team grant matters because a project's lessons are usually worth having
    in the next project of the same team — and because an agent assigned to the
    team but not yet to the project would otherwise see nothing."""
    out = [f"project:{project_id}"]
    team = team_of(db, project_id)
    if team:
        out.append(f"team:{team}")
    return out


def amos_agent_id(db, agent_id: str | None) -> str:
    if not agent_id:
        return ENGINE_OWNER
    row = db.one("SELECT amos_agent_id FROM agents WHERE id=?", (agent_id,))
    return (row["amos_agent_id"] if row and row["amos_agent_id"] else agent_id)


@never_fails(False)
def ensure_org(db, project_id: str, agent_id: str | None = None) -> bool:
    """Register team/project/agent in AMOS so the ACL can resolve them.

    AMOS gates project memory on membership, and membership requires the team
    first. Doing this lazily at write time means a project created before AMOS
    was reachable still works the first time an agent runs in it."""
    client = _client()
    if client is None:
        return False
    team = team_of(db, project_id)
    try:
        if team:
            client.create_team(team)
        client.create_project(project_id, team_id=team) if team else None
    except Exception as exc:                     # already exists is the norm
        log.debug("AMOS org ensure: %s", type(exc).__name__)
    if not agent_id:
        return True
    owner = amos_agent_id(db, agent_id)
    try:
        client.register_agent(owner)
        if team:
            client.add_team_member(team, owner)
        client.add_project_member(project_id, owner)
    except Exception as exc:
        log.debug("AMOS membership ensure: %s", type(exc).__name__)
    return True


@never_fails(None)
def remember(db, project_id: str, text: str, *, kind: str = "note",
             agent_id: str | None = None, importance: float = 0.5) -> str | None:
    """Write one memory for a project. Returns its id, or None if it did not
    happen (AMOS missing, or a write error — never an exception)."""
    if not text.strip() or not project_id:
        return None
    client = _client()
    if client is None:
        return None
    memory_type = kind if kind in TYPES else "note"
    try:
        record = client.add(
            text[:CONTENT_LIMIT],
            type=memory_type,
            owner=amos_agent_id(db, agent_id),
            scope="project",
            visibility=grants(db, project_id),
            importance=importance,
        )
        return getattr(record, "id", None)
    except Exception as exc:
        # loud on purpose: the last time this failed quietly, a month of
        # planning conversations went nowhere
        log.warning("AMOS write failed for project %s (%s): %s",
                    project_id, type(exc).__name__, exc)
        return None


@never_fails(None)
def stage_done(db, job, stage_name: str, agent_id: str, summary: str) -> str | None:
    """What a stage actually did, in the words of the agent that did it."""
    head = " ".join((summary or "").split())[:1500]
    if not head:
        return None
    return remember(
        db, job["project_id"],
        f"任務「{job['title']}」的「{stage_name}」階段完成：{head}",
        kind="note", agent_id=agent_id)


@never_fails(None)
def gate_failed(db, job, stage_name: str, gate: str, detail: str,
                back_to: str, cycle: int) -> str | None:
    """A rejected gate is the highest-value memory a run produces: it records a
    mistake that was actually made in this codebase, which is what stops the
    next agent from repeating it."""
    return remember(
        db, job["project_id"],
        f"工作流返工：任務「{job['title']}」在「{stage_name}」（{gate}）沒過，"
        f"退回「{back_to}」修正（第 {cycle} 次）。失敗輸出：{detail[:1200]}",
        kind="warning", importance=0.7)


@never_fails(None)
def job_finished(db, job, status: str, reason: str = "") -> str | None:
    if status == "done":
        text = f"任務「{job['title']}」完成，走完 {job['template_id'] or '單階段'} 工作流。"
        return remember(db, job["project_id"], text, kind="decision")
    text = (f"任務「{job['title']}」在「{job['stage']}」停下（{status}）："
            f"{reason[:1200]}")
    return remember(db, job["project_id"], text, kind="warning", importance=0.7)


@never_fails({})
def recall_kwargs(db, job, agent_id: str | None) -> dict[str, Any]:
    """Identity for a context-pack read.

    Passing the requester turns on AMOS's ACL, which is what keeps one project's
    memories out of another project's runs — an unscoped `context_pack(query)`
    happily recalls the whole store."""
    kwargs: dict[str, Any] = {}
    owner = amos_agent_id(db, agent_id) if agent_id else ""
    if owner and owner != ENGINE_OWNER:
        kwargs["requester_agent_id"] = owner
    team = team_of(db, job["project_id"])
    if team:
        kwargs["requester_team_id"] = team
    return kwargs
