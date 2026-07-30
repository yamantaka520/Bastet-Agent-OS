import { useCallback, useEffect, useState } from "react";
import { api, del, post, put } from "../api";
import { useT, useVocab, type T } from "../i18n";
import { DataTable, Section, useList } from "../ui";
import { Secret, scopeText } from "./Secrets";

/** One collapsible card per project, grouped by lifecycle status. The header
 *  carries the light, progress and run controls; the body — loaded only when
 *  expanded, so a hundred projects stay usable — is everything about it. */

type Progress = { total: number; done: number; active: number; blocked: number;
                  open: number; cancelled: number };
type Project = {
  id: string; team_id: string; repo_path: string | null; description: string;
  default_template_id: string | null; status: string; light: string;
  transitions: string[]; progress: Progress; task_count: number;
  running: boolean; created_at: string; updated_at: string;
};
type Stage = { name: string; role?: string | null; gate: string; read_only?: boolean };
type Agent = { id: string; name: string; executor_type: string; enabled: number };
type Role = { id: string; label: string };
type Template = { id: string };
type PoolResource = { id: string; name: string; kind: string };
type Task = { title: string; spec: string; role?: string; job_id?: string;
               origin?: string; job_status?: string; job_stage?: string };
type Overview = {
  project: { id: string; team_id: string; repo_path: string | null;
             description: string; template_id: string | null };
  stages: Stage[];
  role_coverage: { stage: string; role: string;
                   agents: { agent_id: string; agent_name: string;
                             executor_type: string; preference: number }[] }[];
  resources: { id: string; name: string; kind: string; grant_id: string;
               scope_type: string; budget_usd: number | null;
               max_concurrency: number | null; on_exceed: string }[];
  secrets: Secret[];
  jobs: { id: string; title: string; stage: string; status: string;
          updated_at: string }[];
};

const GROUPS = ["planning", "ready", "running", "paused", "maintenance", "closed"];
const JOB_BADGE: Record<string, string> = {
  in_progress: "🔵", blocked: "🟠", done: "✅", cancelled: "⚪", open: "⚪",
  missing: "❓",
};

export default function ProjectPage(props: { canOperate: boolean; refreshKey: number }) {
  const t = useT();
  const [query, setQuery] = useState({ q: "", since: "", until: "", status: "" });
  const [projects, setProjects] = useState<Project[]>([]);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");

  const load = useCallback(() => {
    const params = new URLSearchParams();
    if (query.q) params.set("q", query.q);
    if (query.since) params.set("since", query.since);
    if (query.until) params.set("until", query.until);
    if (query.status) params.set("status", query.status);
    api<Project[]>(`/api/projects?${params.toString()}`)
      .then(setProjects).catch(() => setProjects([]));
  }, [query]);
  useEffect(load, [load, props.refreshKey]);

  const move = async (projectId: string, transition: string) => {
    setError("");
    try {
      await post(`/api/projects/${projectId}/lifecycle/${transition}`, {});
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const filtering = !!(query.q || query.status || query.since || query.until);

  return (
    <div className="page">
      <div className="inline-form proj-search">
        <input placeholder={t("proj.searchPh")} value={query.q} style={{ flex: 1 }}
               onChange={(e) => setQuery({ ...query, q: e.target.value })} />
        <label className="res-field">
          <span>{t("proj.statusFilter")}</span>
          <select value={query.status}
                  onChange={(e) => setQuery({ ...query, status: e.target.value })}>
            <option value="">{t("proj.all")}</option>
            {GROUPS.map((s) => (
              <option key={s} value={s}>{t(`proj.status.${s}`)}</option>
            ))}
          </select>
        </label>
        <label className="res-field">
          <span>{t("proj.since")}</span>
          <input type="date" value={query.since}
                 onChange={(e) => setQuery({ ...query, since: e.target.value })} />
        </label>
        <label className="res-field">
          <span>{t("proj.until")}</span>
          <input type="date" value={query.until}
                 onChange={(e) => setQuery({ ...query, until: e.target.value })} />
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      {!projects.length && (
        <p className="muted">{filtering ? t("proj.noMatch") : t("project.noneYet")}</p>
      )}

      {GROUPS.map((group) => {
        const rows = projects.filter((p) => p.status === group);
        if (!rows.length) return null;
        return (
          <Section key={group} title={`${t(`proj.group.${group}`)}（${rows.length}）`}>
            {rows.map((p) => (
              <ProjectCard key={p.id} project={p} t={t} open={!!open[p.id]}
                           canOperate={props.canOperate} refreshKey={props.refreshKey}
                           onToggle={() => setOpen({ ...open, [p.id]: !open[p.id] })}
                           onMove={(tx) => move(p.id, tx)} onChanged={load} />
            ))}
          </Section>
        );
      })}
    </div>
  );
}

function ProjectCard({ project, open, canOperate, refreshKey, onToggle, onMove,
                       onChanged, t }: {
  project: Project; open: boolean; canOperate: boolean; refreshKey: number;
  onToggle: () => void; onMove: (transition: string) => void;
  onChanged: () => void; t: T;
}) {
  const p = project;
  return (
    <div className={`proj-card ${p.status}`}>
      <div className="proj-head">
        <button className="ghost proj-toggle" onClick={onToggle}
                title={t("proj.detail")}>{open ? "▾" : "▸"}</button>
        <b>{p.light} {p.id}</b>
        <span className="flow-tag">{t(`proj.status.${p.status}`)}</span>
        <span className="card-meta">🏷 {p.team_id}</span>
        {!!p.progress.total && (
          <span className="card-meta" title={t("proj.progressDetail", p.progress)}>
            {t("proj.progress", p.progress)}</span>
        )}
        {p.running && <span className="card-meta">⚙ {t("proj.runnerActive")}</span>}
        {canOperate && (
          <span className="row-ops">
            {p.transitions.map((tx) => (
              <button key={tx}
                      className={tx === "start" || tx === "resume" ? "" : "ghost"}
                      onClick={() => onMove(tx)}>{t(`proj.tx.${tx}`)}</button>
            ))}
          </span>
        )}
      </div>
      {p.description && <p className="proj-desc muted">{p.description}</p>}
      {open && (
        <ProjectDetail projectId={p.id} project={p} canOperate={canOperate}
                       refreshKey={refreshKey} onChanged={onChanged} t={t} />
      )}
    </div>
  );
}

function ProjectDetail({ projectId, project, canOperate, refreshKey, onChanged, t }: {
  projectId: string; project: Project; canOperate: boolean; refreshKey: number;
  onChanged: () => void; t: T;
}) {
  const vocab = useVocab();
  const [templates] = useList<Template>("/api/templates", refreshKey);
  const [agents] = useList<Agent>("/api/agents", refreshKey);
  const [pool] = useList<PoolResource>("/api/resources", refreshKey);
  const [roles, setRoles] = useState<Role[]>([]);
  const [ov, setOv] = useState<Overview | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<Overview>(`/api/projects/${projectId}/overview`)
      .then(setOv).catch(() => setOv(null));
  }, [projectId]);
  useEffect(load, [load, refreshKey]);
  useEffect(() => {
    api<{ roles: Role[] }>("/api/workflow-catalog")
      .then((c) => setRoles(c.roles)).catch(() => {});
  }, []);

  const roleLabel = (id: string) =>
    vocab.roleLabel(id, roles.find((r) => r.id === id)?.label ?? id);
  const guard = async (fn: () => Promise<unknown>) => {
    setError("");
    try { await fn(); load(); onChanged(); }
    catch (e) { setError(String((e as Error).message)); }
  };

  if (!ov) return <p className="muted">…</p>;

  return (
    <div className="proj-body">
      {error && <p className="error">{error}</p>}

      <h4>{t("project.content")}</h4>
      <ContentEditor projectId={projectId} canOperate={canOperate} t={t}
                     repo={ov.project.repo_path ?? ""}
                     desc={ov.project.description}
                     onSaved={() => { load(); onChanged(); }} />

      <h4>{t("project.workflowBlock")}</h4>
      <div className="inline-form">
        <span className="muted">{t("project.workflowLabel")}</span>
        <select value={ov.project.template_id ?? ""} disabled={!canOperate}
                onChange={(e) => guard(() =>
                  post(`/api/projects/${projectId}/template`,
                       { template_id: e.target.value || null }))}>
          <option value="">{t("project.workflowNone")}</option>
          {templates.map((tpl) => <option key={tpl.id} value={tpl.id}>{tpl.id}</option>)}
        </select>
        {!ov.project.template_id &&
          <span className="muted">{t("project.workflowNoneHint")}</span>}
      </div>
      {!!ov.stages.length && (
        <DataTable
          head={["#", t("c.stage"), t("project.headRole"),
                 t("project.headAssigned"), ""]}
          rows={ov.stages.map((st, i) => {
            const cov = ov.role_coverage.find((c) => c.stage === st.name);
            const assigned = cov?.agents ?? [];
            return [
              i + 1,
              st.name + (st.read_only ? " 🔒" : ""),
              st.role ? roleLabel(st.role)
                      : <span className="muted">{t("project.roleAny")}</span>,
              !st.role
                ? <span className="muted">{t("project.roleAnyHint")}</span>
                : assigned.length
                  ? (<span className="role-agents">{assigned.map((a) => (
                      <span key={a.agent_id} className="role-chip">{a.agent_name}
                        <span className="card-meta"> ({a.executor_type})</span>
                        {canOperate && (
                          <button className="ghost chip-x"
                                  title={t("project.removeAssign")}
                                  onClick={() => guard(() => del(
                                    `/api/roles?project_id=${encodeURIComponent(projectId)}`
                                    + `&agent_id=${encodeURIComponent(a.agent_id)}`
                                    + `&role=${encodeURIComponent(st.role as string)}`))}>
                            ✕</button>
                        )}
                      </span>))}</span>)
                  : <span className="danger-text">{t("project.missing")}</span>,
              st.role && canOperate ? (
                <Picker options={agents.filter((a) => a.enabled
                          && !assigned.some((x) => x.agent_id === a.id))
                          .map((a) => ({ value: a.id, label: a.name }))}
                        label={assigned.length ? t("project.assignSwap")
                                               : t("project.assignPick")}
                        empty={t("project.noOtherAgents")} t={t}
                        onPick={(agentId) => guard(() => post("/api/roles",
                          { project_id: projectId, agent_id: agentId,
                            role: st.role, preference: 0 }))} />
              ) : null,
            ];
          })} />
      )}
      <p className="muted">{t("project.assignHint")}</p>

      <TaskPlan projectId={projectId} project={project} agents={agents}
                canOperate={canOperate} refreshKey={refreshKey} t={t}
                onChanged={onChanged} />

      <h4>{t("project.grants")}</h4>
      <DataTable
        head={[t("project.headResource"), t("c.kind"), t("project.headSource"),
               t("project.headBudget"), t("project.headConcurrency"),
               t("project.headOnExceed"), ""]}
        rows={ov.resources.map((r) => [
          r.name, t(`res.kind.${r.kind}`, undefined, r.kind),
          r.scope_type === "project"
            ? t("sec.labelProject")
            : <span className="card-meta">{t("project.resInherited")}（
                {t(r.scope_type === "team" ? "sec.labelTeam"
                                           : "sec.labelGlobal")}）</span>,
          r.budget_usd != null ? `$${r.budget_usd}` : "∞",
          r.max_concurrency ?? "∞", r.on_exceed,
          r.scope_type === "project" && canOperate ? (
            <button className="ghost danger-text chip-x" title={t("project.resRemove")}
                    onClick={() => guard(() =>
                      del(`/api/projects/${projectId}/resources/${r.id}`))}>✕</button>
          ) : null,
        ])} />
      {canOperate && (
        <Picker options={pool.filter((r) => !ov.resources.some((x) => x.id === r.id))
                  .map((r) => ({ value: r.id, label: r.name }))}
                label={t("project.resAdd")} empty={t("project.resAllAdded")} t={t}
                onPick={(rid) => guard(() =>
                  post(`/api/projects/${projectId}/resources`,
                       { resource_id: rid }))} />
      )}
      <p className="muted">{t("project.resHint")}</p>

      <h4>{t("project.secrets")}</h4>
      <DataTable
        head={[t("c.name"), t("sec.headScope"), t("sec.headEnv"), t("c.note")]}
        rows={ov.secrets.map((s) => [s.name, scopeText(s, t), s.env_name ?? "—",
                                     s.note])} />

      <h4>{t("project.jobs")}</h4>
      <DataTable
        head={[t("project.headJob"), t("c.stage"), t("c.status"), t("c.updatedAt")]}
        rows={ov.jobs.map((j) => [j.title, j.stage, j.status,
                                  j.updated_at?.replace("T", " ") ?? ""])} />
    </div>
  );
}

/** The PM agent's decomposition: propose → edit → human confirm → runnable. */
function TaskPlan({ projectId, project, agents, canOperate, refreshKey, onChanged,
                    t }: {
  projectId: string; project: Project; agents: Agent[]; canOperate: boolean;
  refreshKey: number; onChanged: () => void; t: T;
}) {
  const [plan, setPlan] = useState<{ tasks: Task[]; confirmed: boolean;
                                     by: string } | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [pmAgent, setPmAgent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<{ task_plan: { tasks: Task[]; confirmed: boolean; by: string } }>(
      `/api/projects/${projectId}/lifecycle`)
      .then((state) => {
        setPlan(state.task_plan);
        setTasks(state.task_plan.tasks);
      }).catch(() => setPlan(null));
  }, [projectId]);
  useEffect(load, [load, refreshKey]);

  const decompose = async () => {
    setBusy(true);
    setError("");
    try {
      const out = await post<{ tasks: Task[] }>(
        `/api/projects/${projectId}/decompose`, { agent_id: pmAgent });
      setTasks(out.tasks);
      setPlan({ tasks: out.tasks, confirmed: false, by: pmAgent });
    } catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(false); }
  };

  const confirm = async () => {
    setError("");
    try {
      await put(`/api/projects/${projectId}/tasks`, { tasks });
      load();
      onChanged();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const patch = (i: number, key: keyof Task, value: string) =>
    setTasks(tasks.map((task, idx) => (idx === i ? { ...task, [key]: value } : task)));

  return (
    <>
      <h4>{t("proj.tasks")}</h4>
      {error && <p className="error">{error}</p>}
      {canOperate && (
        <div className="inline-form">
          <label className="res-field">
            <span>{t("proj.pmAgent")}</span>
            <select value={pmAgent} onChange={(e) => setPmAgent(e.target.value)}>
              <option value="">pm</option>
              {agents.filter((a) => a.enabled).map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
          </label>
          <button onClick={decompose} disabled={busy}>
            {busy ? t("proj.decomposing") : t("proj.decompose")}</button>
          {plan && (plan.confirmed
            ? <span className="muted">{t("proj.tasksConfirmed")}</span>
            : !!tasks.length
              && <span className="danger-text">{t("proj.tasksPending")}</span>)}
        </div>
      )}
      {!tasks.length && <p className="muted">{t("proj.noTasks")}</p>}
      {tasks.map((task, i) => (
        <div key={i} className="task-row">
          <span className="card-meta">{i + 1}</span>
          <input value={task.title} placeholder={t("proj.taskTitlePh")}
                 disabled={!canOperate} style={{ width: "14rem" }}
                 onChange={(e) => patch(i, "title", e.target.value)} />
          <input value={task.spec} placeholder={t("proj.taskSpecPh")}
                 disabled={!canOperate} style={{ flex: 1 }}
                 onChange={(e) => patch(i, "spec", e.target.value)} />
          <input value={task.role ?? ""} placeholder={t("proj.taskRolePh")}
                 disabled={!canOperate} style={{ width: "9rem" }}
                 onChange={(e) => patch(i, "role", e.target.value)} />
          {task.job_id
            ? (
              // the same job the board shows, with its live state: this is what
              // makes the plan and the Kanban one picture instead of two
              <span className="card-meta task-job">
                {JOB_BADGE[task.job_status ?? ""] ?? "•"}{" "}
                {task.job_status ? t(`proj.job.${task.job_status}`,
                                     undefined, task.job_status) : ""}
                {task.job_stage ? ` · ${task.job_stage}` : ""}
                {task.origin ? ` · ${task.origin}` : ""}
                <code className="detail"> {task.job_id}</code>
              </span>
            )
            : canOperate && (
              <button className="ghost danger-text"
                      onClick={() => setTasks(tasks.filter((_, idx) => idx !== i))}>
                ✕</button>
            )}
        </div>
      ))}
      {canOperate && (
        <div className="row">
          <button className="ghost"
                  onClick={() => setTasks([...tasks, { title: "", spec: "" }])}>
            {t("proj.addTask")}</button>
          <button onClick={confirm}
                  disabled={!tasks.length || !tasks.every((x) => x.title.trim())}>
            {t("proj.confirmTasks")}</button>
        </div>
      )}
      <p className="muted">{t("proj.planSyncHint")}</p>
      <p className="muted">{t("proj.runHint")}</p>
      {project.status === "closed" && <p className="muted">{t("proj.reopenHint")}</p>}
    </>
  );
}

function Picker({ options, label, empty, onPick, t }: {
  options: { value: string; label: string }[]; label: string; empty: string;
  onPick: (value: string) => void; t: T;
}) {
  const [value, setValue] = useState("");
  if (!options.length) return <span className="muted">{empty}</span>;
  return (
    <span className="row-ops">
      <select value={value} onChange={(e) => setValue(e.target.value)}>
        <option value="">{label}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      <button className="ghost" disabled={!value}
              onClick={() => { onPick(value); setValue(""); }}>{t("c.apply")}</button>
    </span>
  );
}

/** Repo path + description, with their own state.
 *
 *  These fields used to be reset from the server on every background reload, so
 *  a WS event mid-typing snapped the path back and the edit looked impossible.
 *  State lives here, seeded once per project, and is only re-seeded when the
 *  server value actually changes while the field is untouched. */
function ContentEditor({ projectId, repo, desc, canOperate, onSaved, t }: {
  projectId: string; repo: string; desc: string; canOperate: boolean;
  onSaved: () => void; t: T;
}) {
  const [draft, setDraft] = useState({ repo, desc });
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!dirty) setDraft({ repo, desc });     // adopt server values only if idle
  }, [repo, desc, dirty]);

  const save = async () => {
    setError("");
    try {
      await put(`/api/projects/${projectId}`,
                { repo_path: draft.repo.trim(), description: draft.desc });
      setDirty(false);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2000);
      onSaved();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const patch = (part: Partial<{ repo: string; desc: string }>) => {
    setDraft({ ...draft, ...part });
    setDirty(true);
    setSaved(false);
  };

  return (
    <>
      <div className="inline-form">
        <label className="res-field">
          <span>{t("project.repoLabel")}</span>
          <input placeholder={t("project.repoPh")} style={{ width: "22rem" }}
                 value={draft.repo} disabled={!canOperate}
                 onChange={(e) => patch({ repo: e.target.value })} />
        </label>
        <input placeholder={t("project.descPh")} style={{ flex: 1 }}
               value={draft.desc} disabled={!canOperate}
               onChange={(e) => patch({ desc: e.target.value })} />
        {canOperate && (
          <button onClick={save} disabled={!dirty}>{t("c.save")}</button>
        )}
        {saved && <span className="muted">✅</span>}
      </div>
      <p className="muted">{t("project.repoHint")}</p>
      {error && <p className="error">{error}</p>}
    </>
  );
}
