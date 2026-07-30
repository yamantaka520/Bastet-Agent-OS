import { useCallback, useEffect, useState } from "react";
import { api, del, post, put } from "../api";
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
type Overview = {
  project: { id: string; team_id: string; repo_path: string | null;
             description: string; template_id: string | null };
  stages: Stage[];
  role_coverage: { stage: string; role: string;
                   agents: { agent_id: string; agent_name: string;
                             executor_type: string; preference: number }[] }[];
  assignments: { role: string; agent_id: string; agent_name: string }[];
  resources: { id: string; name: string; kind: string; budget_usd: number | null;
               max_concurrency: number | null; on_exceed: string }[];
  secrets: Secret[];
  jobs: { id: string; title: string; stage: string; status: string;
          updated_at: string }[];
};

export default function ProjectPage(props: { canOperate: boolean; refreshKey: number }) {
  const [projects] = useList<Project>("/api/projects", props.refreshKey);
  const [templates] = useList<Template>("/api/templates", props.refreshKey);
  const [agents] = useList<Agent>("/api/agents", props.refreshKey);
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

  const roleLabel = (id: string) => roles.find((r) => r.id === id)?.label ?? id;

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

  const setTemplate = async (templateId: string) => {
    setError("");
    try {
      await post(`/api/projects/${selected}/template`, { template_id: templateId || null });
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  if (!projects.length) {
    return <div className="page"><p className="muted">
      還沒有專案 — 到「組織」頁的 Teams → Projects 建立第一個。</p></div>;
  }

  return (
    <div className="page">
      <div className="toolbar">
        <span className="muted">專案：</span>
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        {ov && <span className="card-meta">🏷 {ov.project.team_id}</span>}
      </div>
      {error && <p className="error">{error}</p>}

      {ov && (
        <>
          <Section title="專案內容">
            <div className="inline-form">
              <input placeholder="repo 路徑（Bastet 主機上）" style={{ width: "22rem" }}
                     value={edit.repo} disabled={!props.canOperate}
                     onChange={(e) => setEdit({ ...edit, repo: e.target.value })} />
              <input placeholder="專案說明（會成為任務背景）" style={{ flex: 1 }}
                     value={edit.desc} disabled={!props.canOperate}
                     onChange={(e) => setEdit({ ...edit, desc: e.target.value })} />
              {props.canOperate && <button onClick={saveProject}>儲存</button>}
            </div>
          </Section>

          <Section title="套用的工作流 與 角色 Agent 指派">
            <div className="inline-form">
              <span className="muted">工作流：</span>
              <select value={ov.project.template_id ?? ""} disabled={!props.canOperate}
                      onChange={(e) => setTemplate(e.target.value)}>
                <option value="">（未指派）</option>
                {templates.map((t) => <option key={t.id} value={t.id}>{t.id}</option>)}
              </select>
              {!ov.project.template_id &&
                <span className="muted">未指派時，派工會走單階段流程。</span>}
            </div>
            {!!ov.stages.length && (
              <DataTable
                head={["#", "階段", "需要角色", "已指派 Agent", ""]}
                rows={ov.stages.map((st, i) => {
                  const cov = ov.role_coverage.find((c) => c.stage === st.name);
                  const agentsForRole = cov?.agents ?? [];
                  return [
                    i + 1,
                    st.name + (st.read_only ? " 🔒" : ""),
                    st.role ? roleLabel(st.role) : <span className="muted">不指定</span>,
                    !st.role
                      ? <span className="muted">用專案預設 agent</span>
                      : agentsForRole.length
                        ? (
                          <span className="role-agents">
                            {agentsForRole.map((a) => (
                              <span key={a.agent_id} className="role-chip">
                                {a.agent_name}
                                <span className="card-meta"> ({a.executor_type})</span>
                                {props.canOperate && (
                                  <button className="ghost chip-x" title="移除此指派"
                                          onClick={() => unassignRole(st.role as string,
                                                                     a.agent_id)}>✕</button>
                                )}
                              </span>
                            ))}
                          </span>
                        )
                        : <span className="danger-text">⚠ 缺人</span>,
                    // assignment stays editable: add another agent or swap by
                    // removing the chip above — never a one-shot decision
                    st.role && props.canOperate ? (
                      <AssignInline
                        agents={agents.filter((a) =>
                          !agentsForRole.some((x) => x.agent_id === a.id))}
                        label={agentsForRole.length ? "加入/更換…" : "指派 Agent…"}
                        onPick={(aid) => assignRole(st.role as string, aid)} />
                    ) : null,
                  ];
                })} />
            )}
            <p className="muted">指派可隨時調整：✕ 移除、下拉選單加入其他 Agent。
              同一角色指派多個 Agent 時，優先順序最高者執行（在「組織」頁調整優先順序）。</p>
          </Section>

          <Section title="資源與預算授權">
            <DataTable
              head={["資源", "類型", "預算", "併發上限", "超額行為"]}
              rows={ov.resources.map((r) => [
                r.name, r.kind,
                r.budget_usd != null ? `$${r.budget_usd}` : "∞",
                r.max_concurrency ?? "∞", r.on_exceed,
              ])} />
            {!ov.resources.length && <p className="muted">尚無授權 —
              到「資源」頁為此專案（或其團隊）建立 grant。</p>}
          </Section>

          <Section title="可用憑證（含團隊/全域繼承）">
            <DataTable
              head={["名稱", "可見範圍", "注入環境變數", "備註"]}
              rows={ov.secrets.map((s) => [s.name, scopeText(s),
                                           s.env_name ?? "—", s.note])} />
            {!ov.secrets.length && <p className="muted">尚無可用憑證 —
              到「管理」頁新增，範圍選這個專案或其團隊。</p>}
          </Section>

          <Section title="近期任務">
            <DataTable
              head={["任務", "階段", "狀態", "更新時間"]}
              rows={ov.jobs.map((j) => [j.title, j.stage, j.status,
                                        j.updated_at?.replace("T", " ") ?? ""])} />
            {!ov.jobs.length && <p className="muted">尚無任務 — 到「看板」派第一個。</p>}
          </Section>
        </>
      )}
    </div>
  );
}

function AssignInline({ agents, onPick, label = "指派 Agent…" }: {
  agents: Agent[]; onPick: (agentId: string) => void; label?: string;
}) {
  const [value, setValue] = useState("");
  if (!agents.filter((a) => a.enabled).length) {
    return <span className="muted">（無其他可用 Agent）</span>;
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
              onClick={() => { onPick(value); setValue(""); }}>套用</button>
    </span>
  );
}
