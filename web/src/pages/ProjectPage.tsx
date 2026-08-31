import { useCallback, useEffect, useState } from "react";
import { api, del, post, put } from "../api";
import { useT, useVocab, type T } from "../i18n";
import { DataTable, Section, useList, fmtTime } from "../ui";
import { Secret, scopeText } from "./Secrets";

/** One collapsible card per project, grouped by lifecycle status. The header
 *  carries the light, progress and run controls; the body — loaded only when
 *  expanded, so a hundred projects stay usable — is everything about it. */

type Progress = { total: number; done: number; active: number; blocked: number;
                  open: number; cancelled: number };
type ProjectBudget = { enabled: boolean; limit_usd: number | null;
  spent_usd: number; remaining_usd: number | null; exceeded: boolean;
  timezone: string; period_start: string; resets_at: string };
type Project = {
  id: string; team_id: string; repo_path: string | null; description: string;
  default_template_id: string | null; status: string; light: string;
  transitions: string[]; progress: Progress; task_count: number;
  running: boolean; budget: ProjectBudget; created_at: string; updated_at: string;
};
type Stage = { name: string; role?: string | null; gate: string; read_only?: boolean;
               needs?: string[]; workspace?: "shared" | "isolated" };
type Agent = { id: string; name: string; executor_type: string; enabled: number };
type Role = { id: string; label: string };
type Template = { id: string };
type PoolResource = { id: string; name: string; kind: string };
type Delivery = { mode: "none" | "branch" | "integration" | "production";
                  version?: string };
type Task = { id?: string; title: string; needs?: string[]; spec: string;
               role?: string; delivery?: Delivery;
               job_id?: string; origin?: string; job_status?: string;
               job_stage?: string; delivery_status?: string };
type Plan = { tasks: Task[]; confirmed: boolean; by: string; at?: string;
              stale?: boolean; unverified?: boolean; dispatched?: number;
              source?: { kind?: string; at?: string; messages?: number;
                         chat_at?: string };
              chat?: { messages: number; last_at: string | null } };
type Admission = { ok: boolean; errors: { code: string; detail: string }[];
                   warnings: { code: string; detail: string }[] };
type Overview = {
  project: { id: string; team_id: string; repo_path: string | null;
             description: string; template_id: string | null;
             delivery_profile?: DeliveryProfile; budget: ProjectBudget };
  stages: Stage[];
  role_coverage: { stage: string; role: string;
                   agents: { agent_id: string; agent_name: string;
                             executor_type: string; preference: number }[] }[];
  admission: Admission;
  resources: { id: string; name: string; kind: string; grant_id: string;
               scope_type: string; budget_usd: number | null;
               max_concurrency: number | null; on_exceed: string }[];
  secrets: Secret[];
  jobs: { id: string; title: string; stage: string; status: string;
          updated_at: string }[];
};
type DeliveryProfile = { target_branch?: string; target?: string;
  predeploy_command?: string; deploy_command?: string; verify_command?: string;
  provider?: "web" | "app_store_connect" | "google_play";
  status_adapter?: "command" | "official_api";
  submission_recovery?: "command" | "official_api";
  submission_adapter?: "command" | "official_api";
  release_goal?: "uploaded" | "submitted" | "approved" | "published";
  app_id?: string; platform?: string; build_number?: string;
  apple_release_type?: "MANUAL";
  apple_upload_parallelism?: string;
  apple_required_locales?: string;
  apple_metadata_required_fields?: string;
  apple_app_info_id?: string;
  apple_require_screenshots?: boolean;
  apple_required_screenshot_display_types?: string;
  package_name?: string; track?: string; version_code?: string;
  artifact_path?: string; artifact_sha256?: string;
  google_release_status?: "draft" | "completed";
  google_changes_not_sent_for_review?: boolean;
  poll_interval_seconds?: string };
type RoomMember = { id: string; name: string; role: string; executor_type: string };
type RoomMessage = { id: string; author_type: string; author_id: string;
                     kind: string; content: string; at: string };
type Room = { project_id: string; members: RoomMember[]; messages: RoomMessage[] };
type Schedule = {
  id: string; name: string; cron: string; timezone: string; prompt: string;
  role: string; agent_id: string | null; template_id: string | null;
  timeout_s: number; enabled: boolean; next_run_at: string;
  last_run_at: string | null; last_job_id: string | null;
  last_occurrence: { status: string; error: string; scheduled_for: string } | null;
};

const GROUPS = ["planning", "ready", "running", "paused", "maintenance", "closed"];
const JOB_BADGE: Record<string, string> = {
  in_progress: "🔵", blocked: "🟠", done: "✅", cancelled: "⚪", open: "⚪",
  missing: "❓",
};

export default function ProjectPage(props: { canOperate: boolean; isAdmin: boolean;
                                            refreshKey: number }) {
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
                           isAdmin={props.isAdmin}
                           onMove={(tx) => move(p.id, tx)} onChanged={load} />
            ))}
          </Section>
        );
      })}
    </div>
  );
}

function ProjectCard({ project, open, canOperate, isAdmin, refreshKey, onToggle,
                       onMove, onChanged, t }: {
  project: Project; open: boolean; canOperate: boolean; isAdmin: boolean;
  refreshKey: number; onToggle: () => void; onMove: (transition: string) => void;
  onChanged: () => void; t: T;
}) {
  const p = project;
  const [error, setError] = useState("");

  /** Trial projects pile up and there was nowhere to remove one. Two prompts,
   *  because this takes the jobs and runs with it: the first confirms, and the
   *  server's own refusal (spend, or work in flight) becomes the second. */
  const remove = async () => {
    if (!window.confirm(t("proj.deleteConfirm", { id: p.id }))) return;
    setError("");
    try {
      await del(`/api/projects/${p.id}`);
      onChanged();
    } catch (e) {
      const message = String((e as Error).message);
      if (!window.confirm(`${message}\n\n${t("proj.deleteForce")}`)) {
        setError(message);
        return;
      }
      try {
        const out = await del<{ usage_usd: number; jobs: number }>(
          `/api/projects/${p.id}?force=true`);
        window.alert(t("proj.deleted", { jobs: out.jobs, usd: out.usage_usd }));
        onChanged();
      } catch (e2) { setError(String((e2 as Error).message)); }
    }
  };

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
        {p.budget?.enabled && (
          <span className={p.budget.exceeded ? "error" : "card-meta"}>
            💰 ${p.budget.spent_usd.toFixed(2)} / ${p.budget.limit_usd?.toFixed(2)}
            {p.budget.exceeded ? ` · ${t("project.budgetPaused")}` : ""}
          </span>
        )}
        {canOperate && (
          <span className="row-ops">
            {p.transitions.map((tx) => (
              <button key={tx}
                      className={tx === "start" || tx === "resume" ? "" : "ghost"}
                      onClick={() => onMove(tx)}>{t(`proj.tx.${tx}`)}</button>
            ))}
            {isAdmin && (
              <button className="ghost danger-text" onClick={remove}
                      title={t("proj.deleteHint")}>{t("c.delete")}</button>
            )}
          </span>
        )}
      </div>
      {error && <p className="error">{error}</p>}
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
                     deliveryProfile={ov.project.delivery_profile ?? {}}
                     budget={ov.project.budget}
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
                        <span className="card-meta"> ({a.executor_type} · {
                          t("role.prefShort", { n: a.preference })})</span>
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
      {!ov.admission.ok && (
        <div className="notice">
          <b>{t("project.admissionBlocked")}</b>
          <ul>{ov.admission.errors.map((item, i) =>
            <li key={`${item.code}-${i}`}>{item.detail}</li>)}</ul>
        </div>
      )}
      {ov.admission.ok && !!ov.admission.warnings.length && (
        <p className="muted">{t("project.admissionWarnings", {
          list: ov.admission.warnings.map((item) => item.detail).join("；") })}</p>
      )}
      <p className="muted">{t("project.assignHint")}</p>

      <TaskPlan projectId={projectId} project={project}
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
                                  fmtTime(j.updated_at)])} />

      <ProjectSchedules projectId={projectId} canOperate={canOperate}
                        agents={agents} roles={roles} templates={templates}
                        refreshKey={refreshKey} t={t} />

      <ProjectRoom projectId={projectId} canOperate={canOperate}
                   refreshKey={refreshKey} t={t} />
    </div>
  );
}

function ProjectSchedules({ projectId, canOperate, agents, roles, templates,
                            refreshKey, t }: {
  projectId: string; canOperate: boolean; agents: Agent[]; roles: Role[];
  templates: Template[]; refreshKey: number; t: T;
}) {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const empty = { name: "", cron: "0 9 * * 1-5", timezone: zone, prompt: "",
                  role: "", agent_id: "", template_id: "" };
  const [items, setItems] = useState<Schedule[]>([]);
  const [draft, setDraft] = useState(empty);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    api<Schedule[]>(`/api/projects/${projectId}/schedules`)
      .then(setItems).catch(() => setItems([]));
  }, [projectId]);
  useEffect(load, [load, refreshKey]);

  const act = async (fn: () => Promise<unknown>) => {
    setError("");
    try { await fn(); load(); }
    catch (e) { setError(String((e as Error).message)); }
  };
  const create = () => act(async () => {
    await post(`/api/projects/${projectId}/schedules`, {
      ...draft, agent_id: draft.agent_id || null,
      template_id: draft.template_id || null,
    });
    setDraft(empty);
  });

  return (
    <>
      <h4>{t("project.schedules")}</h4>
      {error && <p className="error">{error}</p>}
      {!items.length && <p className="muted">{t("project.scheduleEmpty")}</p>}
      {!!items.length && <DataTable
        head={[t("c.name"), t("project.scheduleTiming"), t("project.scheduleTarget"),
               t("project.scheduleNext"), t("project.scheduleLast"), ""]}
        rows={items.map((item) => [
          <span title={item.prompt}>{item.enabled ? "🟢" : "⚪"} {item.name}</span>,
          <><code>{item.cron}</code><span className="card-meta"> {item.timezone}</span></>,
          item.agent_id || item.role || t("project.scheduleDefaultAgent"),
          fmtTime(item.next_run_at), item.last_occurrence
            ? <span title={item.last_occurrence.error || undefined}>
                {item.last_occurrence.status} · {fmtTime(item.last_occurrence.scheduled_for)}
              </span> : "—",
          canOperate ? <span className="row-ops">
            <button className="ghost" onClick={() => act(() => put(
              `/api/schedules/${item.id}`, { enabled: !item.enabled }))}>
              {item.enabled ? t("project.schedulePause") : t("project.scheduleEnable")}
            </button>
            <button className="ghost" disabled={!item.enabled}
                    onClick={() => act(() => post(
                      `/api/schedules/${item.id}/run-now`, {}))}>
              {t("project.scheduleRunNow")}
            </button>
            <button className="ghost danger-text" onClick={() => {
              if (window.confirm(t("project.scheduleDeleteConfirm", { name: item.name })))
                act(() => del(`/api/schedules/${item.id}`));
            }}>{t("c.delete")}</button>
          </span> : null,
        ])} />}
      {canOperate && <div className="task-row schedule-form">
        <input value={draft.name} placeholder={t("project.scheduleNamePh")}
               onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        <input value={draft.cron} placeholder="cron: 0 9 * * 1-5"
               onChange={(e) => setDraft({ ...draft, cron: e.target.value })} />
        <input value={draft.timezone} placeholder="Asia/Taipei"
               onChange={(e) => setDraft({ ...draft, timezone: e.target.value })} />
        <select value={draft.role}
                onChange={(e) => setDraft({ ...draft, role: e.target.value })}>
          <option value="">{t("project.scheduleAnyRole")}</option>
          {roles.map((role) => <option key={role.id} value={role.id}>{role.label}</option>)}
        </select>
        <select value={draft.agent_id}
                onChange={(e) => setDraft({ ...draft, agent_id: e.target.value })}>
          <option value="">{t("project.scheduleRoleAgent")}</option>
          {agents.filter((a) => a.enabled).map((agent) =>
            <option key={agent.id} value={agent.id}>{agent.name}</option>)}
        </select>
        <select value={draft.template_id}
                onChange={(e) => setDraft({ ...draft, template_id: e.target.value })}>
          <option value="">{t("project.workflowNone")}</option>
          {templates.map((tpl) => <option key={tpl.id} value={tpl.id}>{tpl.id}</option>)}
        </select>
        <input value={draft.prompt} style={{ flex: 1 }}
               placeholder={t("project.schedulePromptPh")}
               onChange={(e) => setDraft({ ...draft, prompt: e.target.value })} />
        <button disabled={!draft.name.trim() || !draft.cron.trim()
                          || !draft.prompt.trim()} onClick={create}>
          {t("project.scheduleAdd")}
        </button>
      </div>}
      <p className="muted">{t("project.scheduleHint")}</p>
    </>
  );
}

function ProjectRoom({ projectId, canOperate, refreshKey, t }: {
  projectId: string; canOperate: boolean; refreshKey: number; t: T;
}) {
  const [room, setRoom] = useState<Room | null>(null);
  const [content, setContent] = useState("");
  const [kind, setKind] = useState("message");
  const [error, setError] = useState("");
  const load = useCallback(() => {
    api<Room>(`/api/projects/${projectId}/room`).then(setRoom).catch(() => setRoom(null));
  }, [projectId]);
  useEffect(load, [load, refreshKey]);

  const send = async () => {
    if (!content.trim()) return;
    setError("");
    try {
      await post(`/api/projects/${projectId}/room/messages`, { content, kind });
      setContent("");
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  return (
    <>
      <h4>{t("project.room")}</h4>
      {error && <p className="error">{error}</p>}
      <p className="muted">
        {t("project.roomMembers")}：
        {room?.members.map((m) => `${m.name}（${m.role}）`).join("、") || "—"}
      </p>
      <div className="project-room-log">
        {!room?.messages.length && <span className="muted">
          {t("project.roomEmpty")}</span>}
        {room?.messages.map((m) => (
          <div key={m.id} className={`chat-msg ${m.kind}`}>
            <div className="chat-msg-head">
              <b>{m.author_id}</b><span className="flow-tag">{m.kind}</span>
              <span className="muted">{fmtTime(m.at)}</span>
            </div>
            <div className="chat-msg-body">{m.content}</div>
          </div>
        ))}
      </div>
      {canOperate && (
        <div className="project-room-compose">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="message">{t("project.roomMessage")}</option>
            <option value="assignment">{t("project.roomAssignment")}</option>
          </select>
          <textarea value={content} onChange={(e) => setContent(e.target.value)}
                    placeholder={t("project.roomPlaceholder")} />
          <button onClick={send}>{t("c.send")}</button>
        </div>
      )}
    </>
  );
}

/** The PM agent's decomposition: propose → edit → human confirm → runnable. */
function TaskPlan({ projectId, project, canOperate, refreshKey, onChanged,
                    t }: {
  projectId: string; project: Project; canOperate: boolean;
  refreshKey: number; onChanged: () => void; t: T;
}) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<{ task_plan: Plan }>(`/api/projects/${projectId}/lifecycle`)
      .then((state) => {
        setPlan(state.task_plan);
        setTasks(state.task_plan.tasks);
      }).catch(() => setPlan(null));
  }, [projectId]);
  useEffect(load, [load, refreshKey]);

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
  const patchDelivery = (i: number, value: Partial<Delivery>) =>
    setTasks(tasks.map((task, idx) => idx === i
      ? { ...task, delivery: { mode: task.delivery?.mode ?? "integration",
                               ...task.delivery, ...value } }
      : task));

  return (
    <>
      <h4>{t("proj.tasks")}</h4>
      {error && <p className="error">{error}</p>}
      {plan && (plan.confirmed
        ? <span className="muted">{t("proj.tasksConfirmed")}</span>
        : !!tasks.length && <span className="danger-text">{t("proj.tasksPending")}</span>)}
      {plan?.stale && (
        <p className="error">{t("proj.planStale",
          { messages: plan.chat?.messages ?? 0 })}</p>
      )}
      {plan?.unverified && !plan.stale && (
        <p className="error">{t("proj.planNoSource")}</p>
      )}
      {!!tasks.length && plan && (
        <p className="muted">
          {plan.source?.messages != null
            ? t("proj.planSource", { by: plan.by || "—",
                                     when: fmtTime(plan.source.at || plan.at),
                                     messages: plan.source.messages })
            : ""}
          {plan.dispatched
            ? ` · ${t("proj.dispatchedCount", { n: plan.dispatched })}` : ""}
        </p>
      )}
      {!tasks.length && <p className="muted">{t("proj.noTasks")}</p>}
      {tasks.map((task, i) => (
        <div key={i} className="task-row">
          <span className="card-meta">{i + 1}</span>
          <input value={task.id ?? ""} placeholder="task-id"
                 disabled={!canOperate || !!task.job_id} style={{ width: "8rem" }}
                 onChange={(e) => patch(i, "id", e.target.value)} />
          <input value={task.title} placeholder={t("proj.taskTitlePh")}
                 disabled={!canOperate} style={{ width: "14rem" }}
                 onChange={(e) => patch(i, "title", e.target.value)} />
          <input value={task.spec} placeholder={t("proj.taskSpecPh")}
                 disabled={!canOperate} style={{ flex: 1 }}
                 onChange={(e) => patch(i, "spec", e.target.value)} />
          <input value={task.role ?? ""} placeholder={t("proj.taskRolePh")}
                 disabled={!canOperate} style={{ width: "9rem" }}
                 onChange={(e) => patch(i, "role", e.target.value)} />
          <input value={(task.needs ?? []).join(",")} placeholder="needs: id,id"
                 disabled={!canOperate || !!task.job_id} style={{ width: "11rem" }}
                 onChange={(e) => setTasks(tasks.map((item, idx) => idx === i
                   ? { ...item, needs: e.target.value.split(",").map((x) => x.trim())
                       .filter(Boolean) } : item))} />
          <select value={task.delivery?.mode ?? "integration"}
                  disabled={!canOperate || !!task.job_id}
                  title={t("proj.deliveryMode")}
                  onChange={(e) => patchDelivery(i, {
                    mode: e.target.value as Delivery["mode"] })}>
            <option value="none">{t("proj.deliveryNone")}</option>
            <option value="branch">{t("proj.deliveryBranch")}</option>
            <option value="integration">{t("proj.deliveryIntegration")}</option>
            <option value="production">{t("proj.deliveryProduction")}</option>
          </select>
          {(task.delivery?.mode === "production") && (
            <input value={task.delivery.version ?? ""}
                   placeholder={t("proj.deliveryVersionPh")}
                   disabled={!canOperate || !!task.job_id}
                   style={{ width: "7rem" }}
                   onChange={(e) => patchDelivery(i, { version: e.target.value })} />
          )}
          {task.job_id
            ? (
              // the same job the board shows, with its live state: this is what
              // makes the plan and the Kanban one picture instead of two
              <span className="card-meta task-job">
                {JOB_BADGE[task.job_status ?? ""] ?? "•"}{" "}
                {task.job_status ? t(`proj.job.${task.job_status}`,
                                     undefined, task.job_status) : ""}
                {task.job_stage ? ` · ${task.job_stage}` : ""}
                {task.delivery_status && task.delivery_status !== "not_required"
                  ? ` · ${t("proj.deliveryStatus")}: ${task.delivery_status}` : ""}
                {task.origin
                  ? ` · ${t(`proj.origin.${task.origin}`, undefined, task.origin)}` : ""}
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
                  onClick={() => setTasks([...tasks, { title: "", spec: "",
                    delivery: { mode: "integration" } }])}>
            {t("proj.addTask")}</button>
          {!!tasks.filter((x) => !x.job_id).length && (
            <button className="ghost danger-text" onClick={async () => {
              if (!window.confirm(t("proj.clearConfirm"))) return;
              setError("");
              try { await del(`/api/projects/${projectId}/tasks`); load(); onChanged(); }
              catch (e) { setError(String((e as Error).message)); }
            }}>{t("proj.clearTasks")}</button>
          )}
          <button onClick={confirm}
                  disabled={!tasks.length || !tasks.every((x) => x.title.trim()
                    && (x.delivery?.mode !== "production" || !!x.delivery.version?.trim()))}>
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
function ContentEditor({ projectId, repo, desc, deliveryProfile, budget,
  canOperate, onSaved, t }: {
  projectId: string; repo: string; desc: string; canOperate: boolean;
  deliveryProfile: DeliveryProfile; budget: ProjectBudget; onSaved: () => void; t: T;
}) {
  const [draft, setDraft] = useState({ repo, desc, deliveryProfile,
    dailyLimit: budget.limit_usd?.toString() ?? "", dailyTimezone: budget.timezone });
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!dirty) setDraft({ repo, desc, deliveryProfile,
      dailyLimit: budget.limit_usd?.toString() ?? "", dailyTimezone: budget.timezone });
  }, [repo, desc, deliveryProfile, budget.limit_usd, budget.timezone, dirty]);

  const save = async () => {
    setError("");
    try {
      await put(`/api/projects/${projectId}`,
                { repo_path: draft.repo.trim(), description: draft.desc,
                  delivery_profile: draft.deliveryProfile,
                  daily_cost_limit_usd: draft.dailyLimit.trim()
                    ? Number(draft.dailyLimit) : null,
                  daily_cost_timezone: draft.dailyTimezone.trim() || "UTC" });
      setDirty(false);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2000);
      onSaved();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const patch = (part: Partial<{ repo: string; desc: string;
                                deliveryProfile: DeliveryProfile;
                                dailyLimit: string; dailyTimezone: string }>) => {
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
      <div className="inline-form">
        <label className="res-field">
          <span>{t("project.dailyCostLimit")}</span>
          <input type="number" min="0.01" step="0.01"
                 placeholder={t("project.dailyCostLimitPh")}
                 value={draft.dailyLimit} disabled={!canOperate}
                 onChange={(e) => patch({ dailyLimit: e.target.value })} />
        </label>
        <label className="res-field">
          <span>{t("project.dailyCostTimezone")}</span>
          <input placeholder="Asia/Taipei" value={draft.dailyTimezone}
                 disabled={!canOperate}
                 onChange={(e) => patch({ dailyTimezone: e.target.value })} />
        </label>
        {budget.enabled && (
          <span className={budget.exceeded ? "error" : "muted"}>
            {t("project.dailyCostUsage", {
              spent: budget.spent_usd.toFixed(2),
              limit: budget.limit_usd?.toFixed(2) ?? "0.00",
            })}
          </span>
        )}
      </div>
      <p className="muted">{t("project.dailyCostHint")}</p>
      <details>
        <summary>{t("project.deliveryProfile")}</summary>
        <div className="stage-editor">
          <div className="inline-form">
            <label className="res-field">
              <span>{t("project.delivery.provider")}</span>
              <select value={draft.deliveryProfile.provider ?? "web"}
                      disabled={!canOperate}
                      onChange={(e) => patch({ deliveryProfile: {
                        ...draft.deliveryProfile,
                        provider: e.target.value as DeliveryProfile["provider"],
                      } })}>
                <option value="web">Web / command</option>
                <option value="app_store_connect">Apple App Store Connect</option>
                <option value="google_play">Google Play</option>
              </select>
            </label>
            {(["target_branch", "target", "predeploy_command"] as const)
              .map((key) => (
              <label className="res-field" key={key}>
                <span>{t(`project.delivery.${key}`)}</span>
                <input value={draft.deliveryProfile[key] ?? ""}
                       disabled={!canOperate}
                       onChange={(e) => patch({ deliveryProfile: {
                         ...draft.deliveryProfile, [key]: e.target.value } })} />
              </label>
            ))}
            {(draft.deliveryProfile.submission_adapter ?? "command") === "command" && (
              <label className="res-field">
                <span>{t("project.delivery.deploy_command")}</span>
                <input value={draft.deliveryProfile.deploy_command ?? ""}
                       disabled={!canOperate}
                       onChange={(e) => patch({ deliveryProfile: {
                         ...draft.deliveryProfile, deploy_command: e.target.value } })} />
              </label>
            )}
            {(draft.deliveryProfile.provider === "app_store_connect"
              || draft.deliveryProfile.provider === "google_play") && (<>
              <label className="res-field">
                <span>{t("project.delivery.status_adapter")}</span>
                <select value={draft.deliveryProfile.status_adapter ?? "command"}
                        disabled={!canOperate}
                        onChange={(e) => patch({ deliveryProfile: {
                          ...draft.deliveryProfile,
                          status_adapter: e.target.value as DeliveryProfile["status_adapter"],
                        } })}>
                  <option value="command">{t("project.delivery.adapter.command")}</option>
                  <option value="official_api">
                    {t("project.delivery.adapter.official_api")}
                  </option>
                </select>
              </label>
              <label className="res-field">
                <span>{t("project.delivery.release_goal")}</span>
                <select value={draft.deliveryProfile.release_goal ?? "published"}
                        disabled={!canOperate}
                        onChange={(e) => patch({ deliveryProfile: {
                          ...draft.deliveryProfile,
                          release_goal: e.target.value as DeliveryProfile["release_goal"],
                        } })}>
                  {(["uploaded", "submitted", "approved", "published"] as const)
                    .map((goal) => <option key={goal} value={goal}>{goal}</option>)}
                </select>
              </label>
              {(draft.deliveryProfile.status_adapter ?? "command") === "official_api" && (
                <label className="res-field">
                  <span>{t("project.delivery.submission_recovery")}</span>
                  <select value={draft.deliveryProfile.submission_recovery ?? "command"}
                          disabled={!canOperate}
                          onChange={(e) => patch({ deliveryProfile: {
                            ...draft.deliveryProfile,
                            submission_recovery:
                              e.target.value as DeliveryProfile["submission_recovery"],
                          } })}>
                    <option value="command">
                      {t("project.delivery.recovery.command")}
                    </option>
                    <option value="official_api">
                      {t("project.delivery.recovery.official_api")}
                    </option>
                  </select>
                </label>
              )}
              {draft.deliveryProfile.provider === "app_store_connect" ? (
                <>
                  {(["app_id", "platform", "build_number"] as const).map((key) => (
                    <label className="res-field" key={key}>
                      <span>{t(`project.delivery.${key}`)}</span>
                      <input value={draft.deliveryProfile[key]
                                      ?? (key === "platform" ? "IOS" : "")}
                             disabled={!canOperate}
                             onChange={(e) => patch({ deliveryProfile: {
                               ...draft.deliveryProfile, [key]: e.target.value } })} />
                    </label>
                  ))}
                  {(draft.deliveryProfile.status_adapter ?? "command")
                    === "official_api" && (<>
                    <label className="res-field">
                      <span>{t("project.delivery.submission_adapter")}</span>
                      <select value={draft.deliveryProfile.submission_adapter ?? "command"}
                              disabled={!canOperate}
                              onChange={(e) => patch({ deliveryProfile: {
                                ...draft.deliveryProfile,
                                submission_adapter:
                                  e.target.value as DeliveryProfile["submission_adapter"],
                                ...(e.target.value === "official_api"
                                  ? { submission_recovery: "official_api" as const }
                                  : {}),
                              } })}>
                        <option value="command">
                          {t("project.delivery.submit.command")}
                        </option>
                        <option value="official_api">
                          {t("project.delivery.submit.apple_official_api")}
                        </option>
                      </select>
                    </label>
                    {(draft.deliveryProfile.submission_adapter ?? "command")
                      === "official_api" && (
                      <>
                        <label className="res-field">
                          <span>{t("project.delivery.apple_release_type")}</span>
                          <select value={draft.deliveryProfile.apple_release_type ?? "MANUAL"}
                                  disabled={!canOperate}
                                  onChange={(e) => patch({ deliveryProfile: {
                                    ...draft.deliveryProfile,
                                    apple_release_type:
                                      e.target.value as DeliveryProfile["apple_release_type"],
                                  } })}>
                            <option value="MANUAL">MANUAL</option>
                          </select>
                        </label>
                        <label className="res-field">
                          <span>{t("project.delivery.apple_artifact_path")}</span>
                          <input value={draft.deliveryProfile.artifact_path ?? ""}
                                 disabled={!canOperate}
                                 onChange={(e) => patch({ deliveryProfile: {
                                   ...draft.deliveryProfile,
                                   artifact_path: e.target.value,
                                 } })} />
                        </label>
                        <label className="res-field">
                          <span>{t("project.delivery.apple_artifact_sha256")}</span>
                          <input value={draft.deliveryProfile.artifact_sha256 ?? ""}
                                 disabled={!canOperate}
                                 onChange={(e) => patch({ deliveryProfile: {
                                   ...draft.deliveryProfile,
                                   artifact_sha256: e.target.value,
                                 } })} />
                        </label>
                        <label className="res-field">
                          <span>{t("project.delivery.apple_upload_parallelism")}</span>
                          <input type="number" min="1" max="8"
                                 value={draft.deliveryProfile.apple_upload_parallelism ?? "4"}
                                 disabled={!canOperate}
                                 onChange={(e) => patch({ deliveryProfile: {
                                   ...draft.deliveryProfile,
                                   apple_upload_parallelism: e.target.value,
                                 } })} />
                        </label>
                        <label className="res-field">
                          <span>{t("project.delivery.apple_required_locales")}</span>
                          <input value={draft.deliveryProfile.apple_required_locales ?? ""}
                                 disabled={!canOperate}
                                 placeholder="en-US,zh-Hant"
                                 onChange={(e) => patch({ deliveryProfile: {
                                   ...draft.deliveryProfile,
                                   apple_required_locales: e.target.value,
                                 } })} />
                        </label>
                        <label className="res-field">
                          <span>{t("project.delivery.apple_metadata_required_fields")}</span>
                          <input value={draft.deliveryProfile.apple_metadata_required_fields
                                         ?? "description,supportUrl,whatsNew"}
                                 disabled={!canOperate}
                                 onChange={(e) => patch({ deliveryProfile: {
                                   ...draft.deliveryProfile,
                                   apple_metadata_required_fields: e.target.value,
                                 } })} />
                        </label>
                        <label className="res-field">
                          <span>{t("project.delivery.apple_app_info_id")}</span>
                          <input value={draft.deliveryProfile.apple_app_info_id ?? ""}
                                 disabled={!canOperate}
                                 onChange={(e) => patch({ deliveryProfile: {
                                   ...draft.deliveryProfile,
                                   apple_app_info_id: e.target.value,
                                 } })} />
                        </label>
                        <label className="res-field">
                          <span>{t("project.delivery.apple_required_screenshot_display_types")}</span>
                          <input value={draft.deliveryProfile
                                         .apple_required_screenshot_display_types ?? ""}
                                 disabled={!canOperate}
                                 placeholder="APP_IPHONE_67,APP_IPAD_PRO_3GEN_129"
                                 onChange={(e) => patch({ deliveryProfile: {
                                   ...draft.deliveryProfile,
                                   apple_required_screenshot_display_types: e.target.value,
                                 } })} />
                        </label>
                        <label className="res-field res-check">
                          <input type="checkbox"
                                 checked={draft.deliveryProfile
                                   .apple_require_screenshots ?? true}
                                 disabled={!canOperate}
                                 onChange={(e) => patch({ deliveryProfile: {
                                   ...draft.deliveryProfile,
                                   apple_require_screenshots: e.target.checked,
                                 } })} />
                          <span>{t("project.delivery.apple_require_screenshots")}</span>
                        </label>
                      </>
                    )}
                  </>)}
                </>
              ) : (<>
                <label className="res-field">
                  <span>{t("project.delivery.package_name")}</span>
                  <input value={draft.deliveryProfile.package_name ?? ""}
                         disabled={!canOperate}
                         onChange={(e) => patch({ deliveryProfile: {
                           ...draft.deliveryProfile, package_name: e.target.value } })} />
                </label>
                <label className="res-field">
                  <span>{t("project.delivery.track")}</span>
                  <input value={draft.deliveryProfile.track ?? "production"}
                         disabled={!canOperate}
                         onChange={(e) => patch({ deliveryProfile: {
                           ...draft.deliveryProfile, track: e.target.value } })} />
                </label>
                <label className="res-field">
                  <span>{t("project.delivery.version_code")}</span>
                  <input value={draft.deliveryProfile.version_code ?? ""}
                         disabled={!canOperate}
                         onChange={(e) => patch({ deliveryProfile: {
                           ...draft.deliveryProfile, version_code: e.target.value } })} />
                </label>
                {(draft.deliveryProfile.status_adapter ?? "command") === "official_api" && (<>
                  <label className="res-field">
                    <span>{t("project.delivery.submission_adapter")}</span>
                    <select value={draft.deliveryProfile.submission_adapter ?? "command"}
                            disabled={!canOperate}
                            onChange={(e) => patch({ deliveryProfile: {
                              ...draft.deliveryProfile,
                              submission_adapter:
                                e.target.value as DeliveryProfile["submission_adapter"],
                              ...(e.target.value === "official_api"
                                ? { submission_recovery: "official_api" as const }
                                : {}),
                            } })}>
                      <option value="command">
                        {t("project.delivery.submit.command")}
                      </option>
                      <option value="official_api">
                        {t("project.delivery.submit.official_api")}
                      </option>
                    </select>
                  </label>
                  {(draft.deliveryProfile.submission_adapter ?? "command")
                    === "official_api" && (<>
                    {(["artifact_path", "artifact_sha256"] as const).map((key) => (
                      <label className="res-field" key={key}>
                        <span>{t(`project.delivery.${key}`)}</span>
                        <input value={draft.deliveryProfile[key] ?? ""}
                               disabled={!canOperate}
                               onChange={(e) => patch({ deliveryProfile: {
                                 ...draft.deliveryProfile, [key]: e.target.value } })} />
                      </label>
                    ))}
                    <label className="res-field">
                      <span>{t("project.delivery.google_release_status")}</span>
                      <select value={draft.deliveryProfile.google_release_status ?? "completed"}
                              disabled={!canOperate}
                              onChange={(e) => patch({ deliveryProfile: {
                                ...draft.deliveryProfile,
                                google_release_status:
                                  e.target.value as DeliveryProfile["google_release_status"],
                              } })}>
                        <option value="draft">draft</option>
                        <option value="completed">completed</option>
                      </select>
                    </label>
                    <label className="res-field checkbox-field">
                      <span>{t("project.delivery.google_changes_not_sent_for_review")}</span>
                      <input type="checkbox"
                             checked={draft.deliveryProfile
                               .google_changes_not_sent_for_review ?? true}
                             disabled={!canOperate}
                             onChange={(e) => patch({ deliveryProfile: {
                               ...draft.deliveryProfile,
                               google_changes_not_sent_for_review: e.target.checked,
                             } })} />
                    </label>
                  </>)}
                </>)}
              </>)}
              <label className="res-field">
                <span>{t("project.delivery.poll_interval_seconds")}</span>
                <input type="number" min="1" max="86400"
                       value={draft.deliveryProfile.poll_interval_seconds ?? "300"}
                       disabled={!canOperate}
                       onChange={(e) => patch({ deliveryProfile: {
                         ...draft.deliveryProfile,
                         poll_interval_seconds: e.target.value } })} />
              </label>
            </>)}
            {(draft.deliveryProfile.provider === "web"
              || (draft.deliveryProfile.status_adapter ?? "command") === "command") && (
              <label className="res-field">
                <span>{t("project.delivery.verify_command")}</span>
                <input value={draft.deliveryProfile.verify_command ?? ""}
                       disabled={!canOperate}
                       onChange={(e) => patch({ deliveryProfile: {
                         ...draft.deliveryProfile, verify_command: e.target.value } })} />
              </label>
            )}
          </div>
          <p className="muted">{t("project.deliveryHint")}</p>
        </div>
      </details>
      {error && <p className="error">{error}</p>}
    </>
  );
}
