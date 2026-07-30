import { useCallback, useEffect, useState } from "react";
import { api, del, post, put } from "../api";
import { useT, useVocab } from "../i18n";
import { DataTable, Section, useList } from "../ui";
import { Secret, scopeText } from "./Secrets";

/** One project, everything about it: content, workflow, role coverage,
 *  resources & budgets, credentials, recent jobs. Every block reads the same
 *  data the other tabs write — this is a lens, not a separate store. */

type Project = { id: string; team_id: string; repo_path: string | null;
                 default_template_id: string | null };
type Stage = { name: string; role?: string | null; gate: string; read_only?: boolean };
type Agent = { id: string; name: string; executor_type: string; enabled: number };
type Role = { id: string; label: string };
type Template = { id: string };
type PoolResource = { id: string; name: string; kind: string };
type Overview = {
  project: { id: string; team_id: string; repo_path: string | null;
             description: string; template_id: string | null };
  stages: Stage[];
  role_coverage: { stage: string; role: string;
                   agents: { agent_id: string; agent_name: string;
                             executor_type: string; preference: number }[] }[];
  assignments: { role: string; agent_id: string; agent_name: string }[];
  resources: { id: string; name: string; kind: string; grant_id: string;
               scope_type: string; budget_usd: number | null;
               max_concurrency: number | null; on_exceed: string }[];
  secrets: Secret[];
  jobs: { id: string; title: string; stage: string; status: string;
          updated_at: string }[];
};

export default function ProjectPage(props: { canOperate: boolean; refreshKey: number }) {
  const t = useT();
  const vocab = useVocab();
  const [projects] = useList<Project>("/api/projects", props.refreshKey);
  const [templates] = useList<Template>("/api/templates", props.refreshKey);
  const [agents] = useList<Agent>("/api/agents", props.refreshKey);
  const [pool] = useList<PoolResource>("/api/resources", props.refreshKey);
  const [roles, setRoles] = useState<Role[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [ov, setOv] = useState<Overview | null>(null);
  const [edit, setEdit] = useState({ repo: "", desc: "" });
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ roles: Role[] }>("/api/workflow-catalog")
      .then((c) => setRoles(c.roles)).catch(() => {});
  }, []);
  useEffect(() => {
    if (!selected && projects.length) setSelected(projects[0].id);
  }, [projects, selected]);

  const load = useCallback(() => {
    if (!selected) return;
    api<Overview>(`/api/projects/${selected}/overview`).then((data) => {
      setOv(data);
      setEdit({ repo: data.project.repo_path ?? "", desc: data.project.description });
    }).catch(() => setOv(null));
  }, [selected]);
  useEffect(load, [load, props.refreshKey]);

  // localised by stable role id; falls back to whatever the catalog sent
  const roleLabel = (id: string) =>
    vocab.roleLabel(id, roles.find((r) => r.id === id)?.label ?? id);

  const saveProject = async () => {
    setError("");
    try {
      await put(`/api/projects/${selected}`,
                { repo_path: edit.repo, description: edit.desc });
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const assignRole = async (role: string, agentId: string) => {
    setError("");
    try {
      await post("/api/roles", { project_id: selected, agent_id: agentId, role,
                                 preference: 0 });
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const unassignRole = async (role: string, agentId: string) => {
    setError("");
    try {
      await del(`/api/roles?project_id=${encodeURIComponent(selected)}` +
                `&agent_id=${encodeURIComponent(agentId)}` +
                `&role=${encodeURIComponent(role)}`);
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const attachResource = async (resourceId: string) => {
    setError("");
    try {
      await post(`/api/projects/${selected}/resources`, { resource_id: resourceId });
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const detachResource = async (resourceId: string) => {
    setError("");
    try {
      await del(`/api/projects/${selected}/resources/${resourceId}`);
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const setTemplate = async (templateId: string) => {
    setError("");
    try {
      await post(`/api/projects/${selected}/template`, { template_id: templateId || null });
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  if (!projects.length) {
    return <div className="page">
      <p className="muted">{t("project.noneYet")}</p></div>;
  }

  return (
    <div className="page">
      <div className="toolbar">
        <span className="muted">{t("project.selector")}</span>
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        {ov && <span className="card-meta">🏷 {ov.project.team_id}</span>}
      </div>
      {error && <p className="error">{error}</p>}

      {ov && (
        <>
          <Section title={t("project.content")}>
            <div className="inline-form">
              <input placeholder={t("project.repoPh")} style={{ width: "22rem" }}
                     value={edit.repo} disabled={!props.canOperate}
                     onChange={(e) => setEdit({ ...edit, repo: e.target.value })} />
              <input placeholder={t("project.descPh")} style={{ flex: 1 }}
                     value={edit.desc} disabled={!props.canOperate}
                     onChange={(e) => setEdit({ ...edit, desc: e.target.value })} />
              {props.canOperate && <button onClick={saveProject}>{t("c.save")}</button>}
            </div>
          </Section>

          <Section title={t("project.workflowBlock")}>
            <div className="inline-form">
              <span className="muted">{t("project.workflowLabel")}</span>
              <select value={ov.project.template_id ?? ""} disabled={!props.canOperate}
                      onChange={(e) => setTemplate(e.target.value)}>
                <option value="">{t("project.workflowNone")}</option>
                {templates.map((t) => <option key={t.id} value={t.id}>{t.id}</option>)}
              </select>
              {!ov.project.template_id &&
                <span className="muted">{t("project.workflowNoneHint")}</span>}
            </div>
            {!!ov.stages.length && (
              <DataTable
                head={["#", t("c.stage"), t("project.headRole"), t("project.headAssigned"), ""]}
                rows={ov.stages.map((st, i) => {
                  const cov = ov.role_coverage.find((c) => c.stage === st.name);
                  const agentsForRole = cov?.agents ?? [];
                  return [
                    i + 1,
                    st.name + (st.read_only ? " 🔒" : ""),
                    st.role ? roleLabel(st.role) : <span className="muted">{t("project.roleAny")}</span>,
                    !st.role
                      ? <span className="muted">{t("project.roleAnyHint")}</span>
                      : agentsForRole.length
                        ? (
                          <span className="role-agents">
                            {agentsForRole.map((a) => (
                              <span key={a.agent_id} className="role-chip">
                                {a.agent_name}
                                <span className="card-meta"> ({a.executor_type})</span>
                                {props.canOperate && (
                                  <button className="ghost chip-x"
                                          title={t("project.removeAssign")}
                                          onClick={() => unassignRole(st.role as string,
                                                                     a.agent_id)}>✕</button>
                                )}
                              </span>
                            ))}
                          </span>
                        )
                        : <span className="danger-text">{t("project.missing")}</span>,
                    // assignment stays editable: add another agent or swap by
                    // removing the chip above — never a one-shot decision
                    st.role && props.canOperate ? (
                      <AssignInline
                        agents={agents.filter((a) =>
                          !agentsForRole.some((x) => x.agent_id === a.id))}
                        label={agentsForRole.length ? t("project.assignSwap")
                                                    : t("project.assignPick")}
                        onPick={(aid) => assignRole(st.role as string, aid)} />
                    ) : null,
                  ];
                })} />
            )}
            <p className="muted">{t("project.assignHint")}</p>
          </Section>

          <Section title={t("project.grants")}>
            <DataTable
              head={[t("project.headResource"), t("c.kind"), t("project.headSource"),
                     t("project.headBudget"), t("project.headConcurrency"),
                     t("project.headOnExceed"), ""]}
              rows={ov.resources.map((r) => [
                r.name, t(`res.kind.${r.kind}`, undefined, r.kind),
                r.scope_type === "project"
                  ? t("sec.labelProject")
                  : <span className="card-meta">{t("project.resInherited")}（
                      {t(r.scope_type === "team" ? "sec.labelTeam" : "sec.labelGlobal")}）
                    </span>,
                r.budget_usd != null ? `$${r.budget_usd}` : "∞",
                r.max_concurrency ?? "∞", r.on_exceed,
                r.scope_type === "project" && props.canOperate ? (
                  <button className="ghost danger-text chip-x"
                          title={t("project.resRemove")}
                          onClick={() => detachResource(r.id)}>✕</button>
                ) : null,
              ])} />
            {!ov.resources.length &&
              <p className="muted">{t("project.noGrants")}</p>}
            {props.canOperate && (
              <AssignInline
                agents={pool.filter((r) =>
                  !ov.resources.some((x) => x.id === r.id))
                  .map((r) => ({ id: r.id, name: r.name,
                                 executor_type: r.kind, enabled: 1 }))}
                label={t("project.resAdd")}
                emptyLabel={t("project.resAllAdded")}
                onPick={attachResource} />
            )}
            <p className="muted">{t("project.resHint")}</p>
          </Section>

          <Section title={t("project.secrets")}>
            <DataTable
              head={[t("c.name"), t("sec.headScope"), t("sec.headEnv"), t("c.note")]}
              rows={ov.secrets.map((s) => [s.name, scopeText(s, t),
                                           s.env_name ?? "—", s.note])} />
            {!ov.secrets.length &&
              <p className="muted">{t("project.noSecrets")}</p>}
          </Section>

          <Section title={t("project.jobs")}>
            <DataTable
              head={[t("project.headJob"), t("c.stage"), t("c.status"), t("c.updatedAt")]}
              rows={ov.jobs.map((j) => [j.title, j.stage, j.status,
                                        j.updated_at?.replace("T", " ") ?? ""])} />
            {!ov.jobs.length && <p className="muted">{t("project.noJobs")}</p>}
          </Section>
        </>
      )}
    </div>
  );
}

function AssignInline({ agents, onPick, label, emptyLabel }: {
  agents: Agent[]; onPick: (agentId: string) => void; label: string;
  emptyLabel?: string;   // the resource card reuses this picker
}) {
  const t = useT();
  const [value, setValue] = useState("");
  if (!agents.filter((a) => a.enabled).length) {
    return <span className="muted">
      {emptyLabel ?? t("project.noOtherAgents")}</span>;
  }
  return (
    <span className="row-ops">
      <select value={value} onChange={(e) => setValue(e.target.value)}>
        <option value="">{label}</option>
        {agents.filter((a) => a.enabled).map((a) => (
          <option key={a.id} value={a.id}>{a.name}</option>
        ))}
      </select>
      <button className="ghost" disabled={!value}
              onClick={() => { onPick(value); setValue(""); }}>{t("c.apply")}</button>
    </span>
  );
}
