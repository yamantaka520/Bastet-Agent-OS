import { useCallback, useEffect, useState } from "react";
import { api, post } from "../api";
import { DataTable, InlineForm, Section, useList } from "../ui";

type Project = { id: string; team_id: string; repo_path: string | null };
type Agent = { id: string; name: string; executor_type: string; enabled: number };
type OrgView = {
  amos: boolean;
  local_only: string[];
  teams: { id: string; name: string; members: string[];
           projects: { id: string; members: string[]; bound: boolean }[] }[];
};
type Executor = { kind: string; name: string; installed: boolean;
                  supports_accounts: boolean; auth_note: string };
type Account = { id: string; executor_type: string; name: string; status: string;
                 login_instruction: string };

export default function OrgPage(props: { canOperate: boolean; refreshKey: number }) {
  const [projects, reloadProjects] = useList<Project>("/api/projects");
  const [agents, reloadAgents] = useList<Agent>("/api/agents");

  return (
    <div className="page">
      <Section title="Projects (1:1 with AMOS projects)">
        {props.canOperate && (
          <InlineForm
            fields={[{ name: "id", placeholder: "project id" },
                     { name: "repo", placeholder: "/path/to/repo", width: "20rem" },
                     { name: "team", placeholder: "team id (optional)" }]}
            submit="add"
            onSubmit={async (v) => {
              await post("/api/projects", { id: v.id, repo_path: v.repo,
                                            team_id: v.team || null });
              reloadProjects();
            }} />
        )}
        <DataTable head={["id", "team", "repo"]}
                   rows={projects.map((p) => [p.id, p.team_id, p.repo_path ?? "—"])} />
      </Section>

      <AgentsSection canOperate={props.canOperate} agents={agents}
                     reloadAgents={reloadAgents} />

      <FederationSection canOperate={props.canOperate} onBound={reloadProjects} />

      <Section title="Role assignment (stage → agent matching)">
        {props.canOperate && (
          <InlineForm
            fields={[{ name: "project", placeholder: "project id" },
                     { name: "agent", placeholder: "agent id" },
                     { name: "role", placeholder: "engineer|reviewer|security-reviewer|pm" },
                     { name: "pref", placeholder: "preference (0)" }]}
            submit="assign"
            onSubmit={async (v) => {
              await post("/api/roles", { project_id: v.project, agent_id: v.agent,
                                         role: v.role, preference: Number(v.pref || 0) });
            }} />
        )}
        <p className="muted">Stages with a <code>role</code> pick the highest-preference
          agent holding that role in the project; otherwise the job's default agent runs.</p>
      </Section>
    </div>
  );
}

function AgentsSection({ canOperate, agents, reloadAgents }:
  { canOperate: boolean; agents: Agent[]; reloadAgents: () => void }) {
  const [executors, setExecutors] = useState<Executor[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [form, setForm] = useState({ id: "", name: "", executor: "claude-code",
                                     account: "" });
  const [newAccountName, setNewAccountName] = useState("");
  const [created, setCreated] = useState<{ login_instruction: string } | null>(null);
  const [error, setError] = useState("");

  const loadAccounts = useCallback(() => {
    api<Account[]>("/api/executor-accounts").then(setAccounts).catch(() => {});
  }, []);
  useEffect(() => {
    api<Executor[]>("/api/executors").then(setExecutors).catch(() => {});
    loadAccounts();
  }, [loadAccounts]);

  const selected = executors.find((e) => e.kind === form.executor);
  const typeAccounts = accounts.filter((a) => a.executor_type === form.executor);

  const addAgent = async () => {
    setError("");
    try {
      await post("/api/agents", { id: form.id, name: form.name || form.id,
                                  executor_type: form.executor,
                                  account_id: form.account || null });
      setForm({ ...form, id: "", name: "", account: "" });
      reloadAgents();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const addAccount = async () => {
    setError("");
    try {
      const result = await post<{ login_instruction: string }>(
        "/api/executor-accounts",
        { executor_type: form.executor, name: newAccountName });
      setCreated(result);
      setNewAccountName("");
      loadAccounts();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const accountName = (id: string | null) =>
    accounts.find((a) => a.id === id)?.name ?? "—";

  return (
    <Section title="Agents (executor bindings)">
      {canOperate && (
        <>
          <div className="inline-form">
            <input placeholder="agent id" value={form.id}
                   onChange={(e) => setForm({ ...form, id: e.target.value })} />
            <input placeholder="display name" value={form.name}
                   onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <select value={form.executor}
                    onChange={(e) => setForm({ ...form, executor: e.target.value,
                                               account: "" })}>
              {executors.map((e) => (
                <option key={e.kind} value={e.kind}>
                  {e.name}{e.installed ? "" : "（未安裝）"}
                </option>
              ))}
            </select>
            {selected?.supports_accounts && (
              <select value={form.account}
                      onChange={(e) => setForm({ ...form, account: e.target.value })}>
                <option value="">全域登入（預設）</option>
                {typeAccounts.map((a) => (
                  <option key={a.id} value={a.id}>{a.name}（{a.status}）</option>
                ))}
              </select>
            )}
            <button onClick={addAgent} disabled={!form.id}>add</button>
          </div>
          {selected?.supports_accounts && (
            <div className="inline-form">
              <input placeholder={`新增 ${form.executor} 帳號名稱`} value={newAccountName}
                     onChange={(e) => setNewAccountName(e.target.value)} />
              <button className="ghost" onClick={addAccount}
                      disabled={!newAccountName}>＋ 帳號</button>
            </div>
          )}
          {selected && !selected.supports_accounts && (
            <p className="muted">{selected.auth_note === "global-only"
              ? "此 executor 僅支援全域登入（單一帳號）。"
              : "此 executor 的憑證來自資源池，不需要帳號。"}</p>
          )}
          {created && (
            <p className="notice">在你的終端執行完成登入：
              <code>{created.login_instruction}</code></p>
          )}
          {error && <p className="error">{error}</p>}
        </>
      )}
      <DataTable head={["id", "name", "executor", "account", "enabled"]}
                 rows={agents.map((a) => [a.id, a.name, a.executor_type,
                                          accountName((a as Agent & {account_id?: string})
                                                      .account_id ?? null),
                                          a.enabled ? "✅" : "⛔"])} />
      {accounts.length > 0 && (
        <>
          <h3>Executor 帳號</h3>
          <DataTable head={["name", "executor", "status", "登入指令"]}
                     rows={accounts.map((a) => [a.name, a.executor_type, a.status,
                                                <code key={a.id} className="detail">
                                                  {a.login_instruction}</code>])} />
        </>
      )}
    </Section>
  );
}

function FederationSection({ canOperate, onBound }:
  { canOperate: boolean; onBound: () => void }) {
  const [org, setOrg] = useState<OrgView | null>(null);
  const [binding, setBinding] = useState<string | null>(null);
  const [repo, setRepo] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<OrgView>("/api/org").then(setOrg).catch(() => setOrg(null));
  }, []);
  useEffect(load, [load]);

  const bind = async (projectId: string) => {
    setError("");
    try {
      await post("/api/org/bind", { project_id: projectId, repo_path: repo });
      setBinding(null);
      setRepo("");
      load();
      onBound();
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  return (
    <Section title="Federation — shared AMOS org view">
      {!org?.amos && <p className="muted">AMOS unavailable — org view offline.</p>}
      {org?.teams.map((team) => (
        <div key={team.id} className="fed-team">
          <b>🏷 {team.name}</b>
          <span className="card-meta"> members: {team.members.join(", ") || "—"}</span>
          <ul>
            {team.projects.map((p) => (
              <li key={p.id}>
                {p.bound ? "🔗" : "◌"} {p.id}
                <span className="card-meta"> ({p.members.length} members)</span>
                {!p.bound && canOperate && (
                  binding === p.id ? (
                    <span className="inline-form" style={{ marginLeft: ".5rem" }}>
                      <input placeholder="/path/to/local/repo" value={repo}
                             onChange={(e) => setRepo(e.target.value)} />
                      <button onClick={() => bind(p.id)} disabled={!repo}>bind</button>
                      <button className="ghost" onClick={() => setBinding(null)}>✕</button>
                    </span>
                  ) : (
                    <button className="ghost" onClick={() => setBinding(p.id)}>bind…</button>
                  )
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {!!org?.local_only.length && (
        <p className="muted">local-only projects (no AMOS record): {org.local_only.join(", ")}</p>
      )}
      <p className="muted">Teams/projects/members converge across nodes via AMOS
        federation; a project synced from another node shows here as ◌ until you
        bind it to a local repo. Resources, grants and jobs stay per-node.</p>
    </Section>
  );
}
