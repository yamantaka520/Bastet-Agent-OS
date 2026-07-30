import { useCallback, useEffect, useState } from "react";
import { api, del, post, put } from "../api";
import LoginWizard from "../LoginWizard";
import RoleAssignSection from "./RoleAssign";
import { useT } from "../i18n";
import { DataTable, InlineForm, Section, useList } from "../ui";

type Project = { id: string; team_id: string; repo_path: string | null };
type Agent = { id: string; name: string; executor_type: string; enabled: number;
               account_id?: string | null; config_json?: string };
type OrgView = {
  amos: boolean;
  local_only: string[];
  teams: { id: string; name: string; members: string[];
           projects: { id: string; members: string[]; bound: boolean }[] }[];
};
type Executor = { kind: string; name: string; installed: boolean;
                  configured: boolean; supports_accounts: boolean;
                  auth_note: string; models: string[] };
type Quota = { windows?: { label: string; used_percent: number;
                           resets_at: string | null }[];
               plan?: string; note?: string; error?: string; unsupported?: string };
type AccountUsage = { runs: number; tokens_in: number; tokens_out: number;
                      cost_usd: number };
type Account = { id: string; executor_type: string; name: string; status: string;
                 login_instruction: string;
                 usage_today: AccountUsage; usage_7d: AccountUsage };

export default function OrgPage(props: { canOperate: boolean; refreshKey: number }) {
  const [projects, reloadProjects] = useList<Project>("/api/projects");
  const [agents, reloadAgents] = useList<Agent>("/api/agents");

  return (
    <div className="page">
      <TeamsProjectsSection canOperate={props.canOperate} projects={projects}
                            reloadProjects={reloadProjects} />

      <AgentsSection canOperate={props.canOperate} agents={agents}
                     reloadAgents={reloadAgents} />

      <FederationSection canOperate={props.canOperate} onBound={reloadProjects} />

      <RoleAssignBridge canOperate={props.canOperate} projects={projects}
                        agents={agents} />

    </div>
  );
}

function RoleAssignBridge({ canOperate, projects, agents }:
  { canOperate: boolean; projects: Project[]; agents: Agent[] }) {
  const [roles, setRoles] = useState<{ id: string; label: string; hint: string }[]>([]);
  useEffect(() => {
    api<{ roles: { id: string; label: string; hint: string }[] }>("/api/workflow-catalog")
      .then((c) => setRoles(c.roles)).catch(() => {});
  }, []);
  return <RoleAssignSection canOperate={canOperate} projects={projects}
                            agents={agents} roles={roles} />;
}

function TeamsProjectsSection({ canOperate, projects, reloadProjects }:
  { canOperate: boolean; projects: Project[]; reloadProjects: () => void }) {
  const t = useT();
  const [teams, setTeams] = useState<{ id: string; name: string }[]>([]);
  const [team, setTeam] = useState("");
  const [error, setError] = useState("");

  const loadTeams = useCallback(() => {
    api<{ teams: { id: string; name: string }[] }>("/api/org")
      .then((org) => {
        setTeams(org.teams);
        setTeam((current) => current || org.teams[0]?.id || "");
      }).catch(() => {});
  }, []);
  useEffect(loadTeams, [loadTeams]);

  return (
    <Section title={t("org.teams")}>
      {canOperate && (
        <>
          <InlineForm
            fields={[{ name: "id", placeholder: t("org.newTeamId") },
                     { name: "name", placeholder: t("org.teamNamePh") }]}
            submit={t("org.addTeam")}
            onSubmit={async (v) => {
              await post("/api/teams", { id: v.id, name: v.name });
              loadTeams();
            }} />
          <div className="inline-form">
            <select value={team} onChange={(e) => setTeam(e.target.value)}>
              {teams.map((t) => <option key={t.id} value={t.id}>🏷 {t.name}</option>)}
            </select>
            <ProjectAdd team={team} onDone={() => { reloadProjects(); loadTeams(); }}
                        onError={setError} />
          </div>
          {error && <p className="error">{error}</p>}
        </>
      )}
      <p className="muted">{t("org.repoHint")}</p>
      {teams.map((team) => {
        const inTeam = projects.filter((p) => p.team_id === team.id);
        return (
          <div key={team.id} className="fed-team">
            <b>🏷 {team.name}</b>
            <ul>
              {inTeam.length === 0 &&
                <li className="muted">{t("org.noProjectsInTeam")}</li>}
              {inTeam.map((p) => (
                <li key={p.id}>📁 {p.id}
                  <span className="card-meta"> {p.repo_path ?? ""}</span></li>
              ))}
            </ul>
          </div>
        );
      })}
    </Section>
  );
}

function ProjectAdd({ team, onDone, onError }:
  { team: string; onDone: () => void; onError: (e: string) => void }) {
  const t = useT();
  const [id, setId] = useState("");
  const [repo, setRepo] = useState("");
  return (
    <>
      <input placeholder={t("org.projectIdPh")} value={id}
             onChange={(e) => setId(e.target.value)} />
      <input placeholder={t("org.repoPh")} style={{ width: "20rem" }}
             value={repo} onChange={(e) => setRepo(e.target.value)} />
      <button disabled={!id || !repo || !team} onClick={async () => {
        onError("");
        try {
          await post("/api/projects", { id, repo_path: repo, team_id: team });
          setId(""); setRepo("");
          onDone();
        } catch (e) { onError(String((e as Error).message)); }
      }}>{t("org.addProject")}</button>
    </>
  );
}

function AgentsSection({ canOperate, agents, reloadAgents }:
  { canOperate: boolean; agents: Agent[]; reloadAgents: () => void }) {
  const t = useT();
  const [executors, setExecutors] = useState<Executor[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [form, setForm] = useState({ id: "", name: "", executor: "claude-code",
                                     account: "", model: "" });
  const [wizard, setWizard] = useState<{ title: string; executorType: string;
                                         accountId: string | null } | null>(null);
  const [quota, setQuota] = useState<Record<string, Quota>>({});
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
                                  account_id: form.account || null,
                                  model: form.model || null });
      setForm({ ...form, id: "", name: "", account: "", model: "" });
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

  const agentModel = (a: Agent): string => {
    try { return JSON.parse(a.config_json ?? "{}").model ?? ""; }
    catch { return ""; }
  };

  const [editing, setEditing] = useState<string | null>(null);

  const saveEdit = async (agentId: string) => {
    setError("");
    try {
      await put(`/api/agents/${agentId}`, {
        name: (document.getElementById(`edit-name-${agentId}`) as HTMLInputElement).value,
        executor_type: (document.getElementById(`edit-exec-${agentId}`) as HTMLSelectElement).value,
        account_id: (document.getElementById(`edit-acct-${agentId}`) as HTMLSelectElement).value,
        model: (document.getElementById(`edit-model-${agentId}`) as HTMLSelectElement).value,
      });
      setEditing(null);
      reloadAgents();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const toggleAgent = async (a: Agent) => {
    await put(`/api/agents/${a.id}`, { enabled: a.enabled ? 0 : 1 });
    reloadAgents();
  };

  const removeAgent = async (agentId: string) => {
    setError("");
    try {
      await del(`/api/agents/${agentId}`);
      reloadAgents();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const renameAccount = async (a: Account) => {
    const name = window.prompt(t("org.newNamePrompt"), a.name);
    if (!name) return;
    await put(`/api/executor-accounts/${a.id}`, { name });
    loadAccounts();
  };

  const removeAccount = async (accountId: string) => {
    setError("");
    try {
      const r = await del<{ note: string }>(`/api/executor-accounts/${accountId}`);
      window.alert(r.note);
      loadAccounts();
    } catch (e) { setError(String((e as Error).message)); }
  };

  return (
    <Section title={t("org.agents")}>
      {canOperate && (
        <>
          <div className="inline-form">
            <input placeholder={t("org.agentIdPh")} value={form.id}
                   onChange={(e) => setForm({ ...form, id: e.target.value })} />
            <input placeholder={t("org.agentNamePh")} value={form.name}
                   onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <select value={form.executor}
                    onChange={(e) => setForm({ ...form, executor: e.target.value,
                                               account: "" })}>
              {executors.map((e) => (
                <option key={e.kind} value={e.kind}>
                  {e.name}{!e.installed ? t("org.notInstalled")
                          : !e.configured ? t("org.notConfigured") : ""}
                </option>
              ))}
            </select>
            {selected?.supports_accounts && (
              <select value={form.account}
                      onChange={(e) => setForm({ ...form, account: e.target.value })}>
                <option value="">{t("org.accountGlobal")}</option>
                {typeAccounts.map((a) => (
                  <option key={a.id} value={a.id}>{a.name}（{a.status}）</option>
                ))}
              </select>
            )}
            {(selected?.models.length ?? 0) > 0 && (
              <select value={form.model}
                      onChange={(e) => setForm({ ...form, model: e.target.value })}>
                <option value="">{t("org.modelDefault")}</option>
                {selected!.models.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            )}
            <button onClick={addAgent} disabled={!form.id}>{t("c.add")}</button>
            {selected && selected.kind !== "bastet-lite" && (
              <button className={selected.configured ? "ghost" : ""} onClick={() =>
                setWizard({ title: selected.name, executorType: selected.kind,
                            accountId: null })}>
                {t("org.webLogin")}{!selected.configured && " ←"}</button>
            )}
          </div>
          {selected?.supports_accounts && (
            <div className="inline-form">
              <input placeholder={t("org.newAccountPh", { executor: form.executor })}
                     value={newAccountName}
                     onChange={(e) => setNewAccountName(e.target.value)} />
              <button className="ghost" onClick={addAccount}
                      disabled={!newAccountName}>{t("org.addAccount")}</button>
            </div>
          )}
          {selected && !selected.supports_accounts && (
            <p className="muted">{selected.auth_note === "global-only"
              ? t("org.globalOnlyNote") : t("org.poolNote")}</p>
          )}
          {created && (
            <p className="notice">{t("org.terminalNote")}
              <code>{created.login_instruction}</code></p>
          )}
          {error && <p className="error">{error}</p>}
        </>
      )}
      <DataTable head={["id", t("c.name"), "executor", "account", "model",
                        "enabled", ""]}
                 rows={agents.map((a) => (
        editing === a.id ? [
          a.id,
          <input key="n" defaultValue={a.name} id={`edit-name-${a.id}`} />,
          <select key="e" defaultValue={a.executor_type} id={`edit-exec-${a.id}`}>
            {executors.map((e) => <option key={e.kind} value={e.kind}>{e.kind}</option>)}
          </select>,
          <select key="a" defaultValue={a.account_id ?? ""} id={`edit-acct-${a.id}`}>
            <option value="">{t("org.accountGlobalShort")}</option>
            {accounts.filter((x) => x.executor_type === a.executor_type)
                     .map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>,
          <select key="m" defaultValue={agentModel(a)} id={`edit-model-${a.id}`}>
            <option value="">{t("c.defaultModel")}</option>
            {(executors.find((e) => e.kind === a.executor_type)?.models ?? [])
              .map((m) => <option key={m} value={m}>{m}</option>)}
          </select>,
          a.enabled ? "✅" : "⛔",
          <span key="ops" className="row-ops">
            <button onClick={() => saveEdit(a.id)}>{t("c.save")}</button>
            <button className="ghost" onClick={() => setEditing(null)}>✕</button>
          </span>,
        ] : [
          a.id, a.name, a.executor_type, accountName(a.account_id ?? null),
          agentModel(a) || t("c.defaultModel"),
          a.enabled ? "✅" : "⛔",
          canOperate ? (
            <span key="ops" className="row-ops">
              <button className="ghost"
                      onClick={() => setEditing(a.id)}>{t("c.edit")}</button>
              <button className="ghost" onClick={() => toggleAgent(a)}>
                {a.enabled ? t("c.disable") : t("c.enable")}</button>
              <button className="ghost danger-text"
                      onClick={() => removeAgent(a.id)}>{t("c.delete")}</button>
            </span>
          ) : null,
        ]))} />
      {wizard && (
        <LoginWizard title={wizard.title} executorType={wizard.executorType}
                     accountId={wizard.accountId}
                     onClose={() => {
                       setWizard(null);
                       loadAccounts();
                       api<Executor[]>("/api/executors").then(setExecutors).catch(() => {});
                     }} />
      )}
      {accounts.length > 0 && (
        <>
          <h3>{t("org.accountsTitle")}</h3>
          <DataTable head={[t("c.name"), "executor", t("c.status"),
                            t("org.usageToday"), t("org.usage7d"),
                            t("org.loginCommand"), ""]}
                     rows={accounts.map((a) => [a.name, a.executor_type, a.status,
                       <span key="u1" className="detail">
                         {a.usage_today.runs} runs ·
                         ${a.usage_today.cost_usd.toFixed(3)}</span>,
                       <span key="u7" className="detail">
                         {a.usage_7d.runs} runs · {a.usage_7d.tokens_in
                           + a.usage_7d.tokens_out} tok ·
                         ${a.usage_7d.cost_usd.toFixed(3)}</span>,
                                                <code key={a.id} className="detail">
                                                  {a.login_instruction}</code>,
                       canOperate ? (
                         <span key="ops" className="row-ops">
                           <button className="ghost" onClick={() =>
                             setWizard({ title: `${a.name}（${a.executor_type}）`,
                                         executorType: a.executor_type,
                                         accountId: a.id })}>{t("c.login")}</button>
                           <button className="ghost" onClick={async () =>
                             setQuota({ ...quota,
                               [a.id]: await api<Quota>(
                                 `/api/executor-accounts/${a.id}/quota`) })}>
                             {t("org.quota")}</button>
                           <button className="ghost" onClick={() => renameAccount(a)}>
                             {t("c.rename")}</button>
                           <button className="ghost danger-text"
                                   onClick={() =>
                                     removeAccount(a.id)}>{t("c.delete")}</button>
                         </span>
                       ) : null])} />
          {Object.entries(quota).map(([accountId, q]) => (
            <p key={accountId} className="notice">
              <b>{accounts.find((a) => a.id === accountId)?.name}</b>：
              {q.error ?? q.unsupported ?? (
                <>
                  {q.plan}｜{(q.windows ?? []).map((w) =>
                    `${t("org.quotaUsed", { label: w.label, pct: w.used_percent })}` +
                    `（${w.resets_at
                      ? t("org.quotaResets",
                          { when: new Date(w.resets_at).toLocaleString() })
                      : "—"}）`
                  ).join("｜") || t("org.quotaNoWindows")}
                  <span className="muted">（{q.note}）</span>
                </>
              )}
            </p>
          ))}
        </>
      )}
    </Section>
  );
}

function FederationSection({ canOperate, onBound }:
  { canOperate: boolean; onBound: () => void }) {
  const t = useT();
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
    <Section title={t("org.federation")}>
      {!org?.amos && <p className="muted">{t("org.amosOffline")}</p>}
      {org?.teams.map((team) => (
        <div key={team.id} className="fed-team">
          <b>🏷 {team.name}</b>
          <span className="card-meta"> {t("org.members")}: {team.members.join(", ")
            || "—"}</span>
          <ul>
            {team.projects.map((p) => (
              <li key={p.id}>
                {p.bound ? "🔗" : "◌"} {p.id}
                <span className="card-meta">
                  （{t("org.memberCount", { n: p.members.length })}）</span>
                {!p.bound && canOperate && (
                  binding === p.id ? (
                    <span className="inline-form" style={{ marginLeft: ".5rem" }}>
                      <input placeholder={t("org.bindPh")} value={repo}
                             onChange={(e) => setRepo(e.target.value)} />
                      <button onClick={() => bind(p.id)}
                              disabled={!repo}>{t("org.bind")}</button>
                      <button className="ghost" onClick={() => setBinding(null)}>✕</button>
                    </span>
                  ) : (
                    <button className="ghost"
                            onClick={() => setBinding(p.id)}>{t("org.bindOpen")}</button>
                  )
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {!!org?.local_only.length && (
        <p className="muted">{t("org.localOnly",
          { list: org.local_only.join(", ") })}</p>
      )}
      <p className="muted">{t("org.federationHint")}</p>
    </Section>
  );
}
