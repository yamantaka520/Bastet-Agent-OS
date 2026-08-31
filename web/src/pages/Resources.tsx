import { useCallback, useEffect, useState } from "react";
import { api, del, post, put, UsageRow } from "../api";
import { useT, type T } from "../i18n";
import { DataTable, InlineForm, Section, useList } from "../ui";

/** The resource pool, by category. A resource is inert until a grant makes it
 *  visible to a project — so scope lives right next to it here, and the same
 *  grants power the budgets card below. */

type KindSpec = { id: string; group: string; auth: string; fields: string[] };
type Catalog = { groups: string[]; kinds: KindSpec[];
                 enums: Record<string, string[]> };
type Scope = { grant_id: string; scope_type: string; scope_id: string };
type Install = { status: string; at: string | null; exit_code: number | null;
                 command: string; log: string; digest: string; target: string;
                 version: string };
type TestState = { status: string; at: string | null; checked: string;
                   detail: string; digest: string };
type Resource = {
  id: string; kind: string; name: string; endpoint: string | null;
  api_flavor: string | null; enabled: number; secret_ref: string;
  credential_name: string | null; config: Record<string, string>;
  install: Install; test: TestState; scopes: Scope[]; problems: string[];
};
type SecretRow = { id: string; name: string };
type Grant = { id: string; resource_id: string; scope_type: string; scope_id: string;
               budget_usd: number | null; period: string; max_concurrency: number | null;
               on_exceed: string; enabled: number };
type ProjectRow = { id: string; team_id: string };

type Draft = {
  name: string; kind: string; endpoint: string; api_flavor: string;
  secret: string; secretManual: string; config: Record<string, string>;
  scope_type: string; scope_id: string;
};

const BLANK: Draft = { name: "", kind: "llm", endpoint: "", api_flavor: "openai",
                       secret: "", secretManual: "", config: {},
                       scope_type: "global", scope_id: "" };

const SCOPE_KEY: Record<string, string> = { global: "sec.labelGlobal",
                                            team: "sec.labelTeam",
                                            project: "sec.labelProject" };
const INSTALLABLE = "install_command";
const MEDIA_ASYNC_FIELDS = ["async_status_path", "async_status_field",
  "async_success_values", "async_failure_values", "async_result_url_field",
  "async_download_hosts", "async_poll_interval_seconds", "async_max_attempts",
  "async_max_bytes"];

/** Mirrors resource_test._url_shape: the URL form decides which credential the
 *  resource needs, so the form can say which one to paste. */
function gitShape(url: string): "ssh" | "https" {
  const value = (url || "").trim();
  return value.startsWith("git@") || value.startsWith("ssh://") ? "ssh" : "https";
}

export default function ResourcesPage(props: { isAdmin: boolean; refreshKey: number }) {
  const t = useT();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [resources, setResources] = useState<Resource[]>([]);
  const [secrets, setSecrets] = useState<SecretRow[]>([]);
  const [projects] = useList<ProjectRow>("/api/projects", props.refreshKey);
  const [grants, reloadGrants] = useList<Grant>("/api/grants", props.refreshKey);
  const [usage] = useList<UsageRow>("/api/usage", props.refreshKey);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const teams = [...new Set(projects.map((p) => p.team_id))];

  const load = useCallback(() => {
    api<Resource[]>("/api/resources").then(setResources).catch(() => setResources([]));
    // credentials are admin-only; a non-admin simply gets no picker
    api<SecretRow[]>("/api/secrets").then(setSecrets).catch(() => setSecrets([]));
  }, []);
  useEffect(load, [load, props.refreshKey]);
  useEffect(() => {
    api<Catalog>("/api/resource-kinds").then(setCatalog).catch(() => {});
  }, []);

  const spec = (kind: string) => catalog?.kinds.find((k) => k.id === kind);

  const save = async () => {
    if (!draft) return;
    setError("");
    const ref = draft.secret === "__manual__" ? draft.secretManual : draft.secret;
    try {
      const body = { name: draft.name, kind: draft.kind,
                     endpoint: draft.endpoint || null,
                     api_flavor: draft.api_flavor || null,
                     secret_ref: ref || null, config: draft.config };
      if (editing) {
        await put(`/api/resources/${editing}`, body);
      } else {
        await post("/api/resources", { ...body, scope_type: draft.scope_type,
                                       scope_id: draft.scope_id });
      }
      setDraft(null);
      setEditing(null);
      load();
      reloadGrants();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const startEdit = (r: Resource) => {
    setEditing(r.id);
    setDraft({ name: r.name, kind: r.kind, endpoint: r.endpoint ?? "",
               api_flavor: r.api_flavor ?? "", secret: "", secretManual: "",
               config: { ...r.config }, scope_type: "global", scope_id: "" });
  };

  const remove = async (r: Resource) => {
    setError("");
    try { await del(`/api/resources/${r.id}`); load(); reloadGrants(); }
    catch (e) { setError(String((e as Error).message)); }
  };

  const addScope = async (r: Resource, scopeType: string, scopeId: string) => {
    setError("");
    try {
      await post(`/api/resources/${r.id}/scopes`,
                 { scope_type: scopeType, scope_id: scopeId });
      load(); reloadGrants();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const dropScope = async (r: Resource, grantId: string) => {
    setError("");
    try { await del(`/api/resources/${r.id}/scopes/${grantId}`); load(); reloadGrants(); }
    catch (e) { setError(String((e as Error).message)); }
  };

  const test = async (r: Resource) => {
    setError("");
    setBusy(`test:${r.id}`);
    try { await post(`/api/resources/${r.id}/test`, {}); load(); }
    catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(""); }
  };

  const install = async (r: Resource) => {
    setError("");
    setBusy(`install:${r.id}`);
    try { await post(`/api/resources/${r.id}/install`, {}); load(); }
    catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(""); }
  };

  const resourceName = (id: string) => resources.find((r) => r.id === id)?.name ?? id;
  const spentFor = (g: Grant) =>
    usage.filter((u) => (g.scope_type === "project" && u.project_id === g.scope_id)
                     || (g.scope_type === "agent" && u.agent_id === g.scope_id))
         .reduce((s, u) => s + (u.cost_usd ?? 0), 0);

  return (
    <div className="page">
      <Section title={t("res.pool")}
               action={props.isAdmin && !draft ? (
                 <button onClick={() => setDraft({ ...BLANK })}>
                   {t("res.addResource")}</button>
               ) : undefined}>
        {error && <p className="error">{error}</p>}
        {draft && catalog && (
          <ResourceForm draft={draft} setDraft={setDraft} catalog={catalog}
                        secrets={secrets} projects={projects} teams={teams}
                        editing={!!editing} onSave={save}
                        onCancel={() => { setDraft(null); setEditing(null); }} t={t} />
        )}
        {(catalog?.groups ?? []).map((group) => {
          const kinds = (catalog?.kinds ?? []).filter((k) => k.group === group);
          const rows = resources.filter((r) => kinds.some((k) => k.id === r.kind));
          return (
            <div key={group} className="res-group">
              <h3>{t(`res.group.${group}`)}</h3>
              {!rows.length && <p className="muted">{t("res.emptyGroup")}</p>}
              {rows.map((r) => (
                <div key={r.id} className="res-row">
                  <div className="res-head">
                    <b>{r.name}</b>
                    <span className="flow-tag">
                      {t(`res.kind.${r.kind}`, undefined, r.kind)}</span>
                    {!r.enabled && <span className="card-meta">⛔</span>}
                    {!!r.problems.length && (
                      <span className="danger-text">⚠ {t("res.problems", {
                        list: r.problems.map((p) =>
                          t(`res.problem.${p}`, undefined, p)).join("、") })}</span>
                    )}
                    {props.isAdmin && (
                      <span className="row-ops">
                        <button className="ghost" onClick={() => startEdit(r)}>
                          {t("c.edit")}</button>
                        <button className="ghost" onClick={async () => {
                          await post(`/api/resources/${r.id}/enabled`,
                                     { enabled: !r.enabled });
                          load();
                        }}>{r.enabled ? t("c.disable") : t("c.enable")}</button>
                        <button className="ghost danger-text"
                                onClick={() => remove(r)}>{t("c.delete")}</button>
                      </span>
                    )}
                  </div>
                  <div className="res-detail">
                    {r.endpoint && <span><code>{r.endpoint}</code></span>}
                    {Object.entries(r.config).filter(([, v]) => v).map(([k, v]) => (
                      <span key={k}>{t(`res.field.${k}`, undefined, k)}:{" "}
                        <code>{String(v)}</code></span>
                    ))}
                    {r.credential_name
                      ? <span>🔑 {r.credential_name}</span>
                      : r.secret_ref ? <span>🔑 <code>{r.secret_ref}</code></span> : null}
                    <span className="card-meta">{t("res.envPrefix")}:{" "}
                      <code>BASTET_RES_{envSlug(r.name)}</code></span>
                  </div>
                  <div className="res-scopes">
                    <span className="muted">{t("res.scope")}：</span>
                    {!r.scopes.length &&
                      <span className="danger-text">{t("res.noScope")}</span>}
                    {r.scopes.map((s) => (
                      <span key={s.grant_id} className="role-chip">
                        {t(SCOPE_KEY[s.scope_type] ?? "", undefined, s.scope_type)}
                        {s.scope_type !== "global" ? `:${s.scope_id}` : ""}
                        {props.isAdmin && (
                          <button className="ghost chip-x"
                                  onClick={() => dropScope(r, s.grant_id)}>✕</button>
                        )}
                      </span>
                    ))}
                    {props.isAdmin && (
                      <ScopeAdd projects={projects} teams={teams} t={t}
                                onAdd={(st, si) => addScope(r, st, si)} />
                    )}
                  </div>
                  {spec(r.kind)?.fields.includes(INSTALLABLE) && (
                    <InstallPanel resource={r} isAdmin={props.isAdmin} t={t}
                                  busy={busy === `install:${r.id}`}
                                  onInstall={() => install(r)} />
                  )}
                  <TestPanel resource={r} isAdmin={props.isAdmin} t={t}
                             busy={busy === `test:${r.id}`}
                             onTest={() => test(r)} />
                </div>
              ))}
            </div>
          );
        })}
        <p className="muted">{t("res.callableHint")}</p>
      </Section>

      <Section title={t("res.grants")}>
        {props.isAdmin && (
          <InlineForm
            fields={[{ name: "resource", placeholder: "resource name/id" },
                     { name: "scope", placeholder: "project:<id> | team:<id> | agent:<id>",
                       width: "16rem" },
                     { name: "budget", placeholder: "budget USD" },
                     { name: "conc", placeholder: "max concurrency" }]}
            submit={t("c.add")}
            onSubmit={async (v) => {
              const resource = resources.find((r) => r.name === v.resource
                                                  || r.id === v.resource);
              if (!resource) throw new Error("unknown resource");
              const [scopeType, scopeId] = (v.scope || "").split(":");
              await post("/api/grants", { resource_id: resource.id,
                scope_type: scopeType, scope_id: scopeId,
                budget_usd: v.budget ? Number(v.budget) : null,
                max_concurrency: v.conc ? Number(v.conc) : null });
              reloadGrants();
            }} />
        )}
        <DataTable
          head={[t("project.headResource"), t("res.scope"), t("project.headBudget"),
                 "burn", t("project.headConcurrency"), t("project.headOnExceed")]}
          rows={grants.map((g) => {
            const spent = spentFor(g);
            const pct = g.budget_usd ? Math.min(100, (spent / g.budget_usd) * 100) : 0;
            return [
              resourceName(g.resource_id),
              `${g.scope_type}:${g.scope_id}`,
              g.budget_usd != null ? `$${g.budget_usd}` : "∞",
              g.budget_usd != null ? (
                <span className="burn">
                  <span className="burn-bar"><i style={{ width: `${pct}%` }} /></span>
                  ${spent.toFixed(4)}
                </span>
              ) : `$${spent.toFixed(4)}`,
              g.max_concurrency ?? "∞",
              g.on_exceed,
            ];
          })} />
      </Section>

      <Section title={t("res.usage")}>
        <DataTable
          head={["project", "agent", "precision", "runs", "in", "out", "cache read",
                 "cost"]}
          rows={usage.map((u) => [
            u.project_id, u.agent_id, u.accounting_precision ?? "—", u.runs,
            u.tokens_in ?? 0, u.tokens_out ?? 0, u.cache_read ?? 0,
            `$${(u.cost_usd ?? 0).toFixed(4)}`,
          ])} />
      </Section>
    </div>
  );
}

/** Mirrors resource_kinds.slug() so the UI can show the env prefix up front. */
function envSlug(name: string): string {
  return name.replace(/[^0-9a-zA-Z]+/g, "_").replace(/^_+|_+$/g, "")
             .toUpperCase().slice(0, 40) || "RES";
}

function ResourceForm({ draft, setDraft, catalog, secrets, projects, teams, editing,
                        onSave, onCancel, t }: {
  draft: Draft; setDraft: (d: Draft) => void; catalog: Catalog;
  secrets: SecretRow[]; projects: ProjectRow[]; teams: string[]; editing: boolean;
  onSave: () => void; onCancel: () => void; t: T;
}) {
  const spec = catalog.kinds.find((k) => k.id === draft.kind);
  const fields = spec?.fields ?? [];
  const setConfig = (key: string, value: string) =>
    setDraft({ ...draft, config: { ...draft.config, [key]: value } });
  const transport = draft.config.mcp_transport || "stdio";

  const text = (key: string, wide = false) => (
    <label key={key} className="res-field">
      <span>{t(`res.field.${key}`)}</span>
      <input value={draft.config[key] ?? ""} style={wide ? { width: "26rem" } : undefined}
             onChange={(e) => setConfig(key, e.target.value)} />
    </label>
  );

  return (
    <div className="stage-editor">
      <div className="inline-form">
        <label className="res-field">
          <span>{t("res.field.name")}</span>
          <input value={draft.name} autoFocus
                 onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        </label>
        <label className="res-field">
          <span>{t("res.kind")}</span>
          <select value={draft.kind} disabled={editing}
                  onChange={(e) => setDraft({ ...draft, kind: e.target.value,
                                              config: {} })}>
            {catalog.groups.map((group) => (
              <optgroup key={group} label={t(`res.group.${group}`)}>
                {catalog.kinds.filter((k) => k.group === group).map((k) => (
                  <option key={k.id} value={k.id}>
                    {t(`res.kind.${k.id}`, undefined, k.id)}</option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        {fields.includes("endpoint") && (
          <label className="res-field">
            <span>{t("res.field.endpoint")}</span>
            <input value={draft.endpoint} style={{ width: "18rem" }}
                   onChange={(e) => setDraft({ ...draft, endpoint: e.target.value })} />
          </label>
        )}
        {fields.includes("api_flavor") && (
          <label className="res-field">
            <span>{t("res.field.api_flavor")}</span>
            <select value={draft.api_flavor}
                    onChange={(e) => setDraft({ ...draft, api_flavor: e.target.value })}>
              <option value="openai">openai</option>
              <option value="anthropic">anthropic</option>
            </select>
          </label>
        )}
        {fields.includes("git_provider") && (
          <label className="res-field">
            <span>{t("res.field.git_provider")}</span>
            <select value={draft.config.git_provider ?? "github"}
                    onChange={(e) => setConfig("git_provider", e.target.value)}>
              {(catalog.enums.git_provider ?? []).map((p) =>
                <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
        )}
        {fields.includes("mcp_transport") && (
          <label className="res-field">
            <span>{t("res.field.mcp_transport")}</span>
            <select value={transport}
                    onChange={(e) => setConfig("mcp_transport", e.target.value)}>
              {(catalog.enums.mcp_transport ?? []).map((p) =>
                <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
        )}
        {fields.includes("default_model") && text("default_model")}
        {fields.includes("auth_header") && text("auth_header")}
        {fields.includes("mcp_secret_env") && transport === "stdio"
          && text("mcp_secret_env")}
      </div>

      {draft.kind === "git" && (
        <p className="muted">{t(gitShape(draft.endpoint) === "ssh"
          ? "res.gitSshHint" : "res.gitHttpsHint")}</p>
      )}

      {fields.includes("async_status_path") && (
        <details>
          <summary>{t("res.asyncMedia")}</summary>
          <p className="muted">{t("res.asyncMediaHint")}</p>
          <div className="inline-form">
            {MEDIA_ASYNC_FIELDS.map((field) => text(field, true))}
          </div>
        </details>
      )}

      <div className="inline-form">
        {fields.includes("mcp_command") && transport === "stdio"
          && text("mcp_command", true)}
        {fields.includes("mcp_url") && transport === "http" && text("mcp_url", true)}
        {fields.includes("skill_id") && text("skill_id")}
        {fields.includes("skill_version") && text("skill_version")}
        {fields.includes("skill_source") && text("skill_source", true)}
        {fields.includes("skill_target") && text("skill_target", true)}
        {fields.includes("skill_digest") && text("skill_digest", true)}
        {fields.includes("compatible_executors") && text("compatible_executors", true)}
        {fields.includes("install_command") && text("install_command", true)}
        {fields.includes("health_command") && text("health_command", true)}
      </div>

      {fields.includes("secret") && (
        <div className="inline-form">
          <label className="res-field">
            <span>{t("res.field.secret")}{spec?.auth === "required" ? " *" : ""}</span>
            <select value={draft.secret}
                    onChange={(e) => setDraft({ ...draft, secret: e.target.value })}>
              <option value="">{spec?.auth === "required"
                ? t("res.secretPick") : t("res.secretNone")}</option>
              {secrets.map((s) => (
                <option key={s.id} value={`secret:${s.id}`}>{s.name}</option>
              ))}
              <option value="__manual__">{t("res.secretManual")}</option>
            </select>
          </label>
          {draft.secret === "__manual__" && (
            <textarea placeholder={t("res.secretManualPh")} rows={2}
                      spellCheck={false} className="secret-value"
                      value={draft.secretManual}
                      onChange={(e) =>
                        setDraft({ ...draft, secretManual: e.target.value })} />
          )}
          {!secrets.length && <span className="muted">{t("res.noSecrets")}</span>}
        </div>
      )}

      {!editing && (
        <div className="inline-form">
          <label className="res-field">
            <span>{t("res.scope")}</span>
            <select value={draft.scope_type}
                    onChange={(e) => setDraft({ ...draft, scope_type: e.target.value,
                                                scope_id: "" })}>
              <option value="global">{t("sec.scopeGlobal")}</option>
              <option value="team">{t("sec.scopeTeam")}</option>
              <option value="project">{t("sec.scopeProject")}</option>
            </select>
          </label>
          {draft.scope_type !== "global" && (
            <select value={draft.scope_id}
                    onChange={(e) => setDraft({ ...draft, scope_id: e.target.value })}>
              <option value="">{t("sec.pickScope")}</option>
              {(draft.scope_type === "team" ? teams : projects.map((p) => p.id))
                .map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          )}
        </div>
      )}

      <div className="row">
        <button onClick={onSave}
                disabled={!draft.name.trim()
                          || (!editing && draft.scope_type !== "global"
                              && !draft.scope_id)}>{t("c.save")}</button>
        <button className="ghost" onClick={onCancel}>{t("c.cancel")}</button>
      </div>
    </div>
  );
}

function ScopeAdd({ projects, teams, onAdd, t }: {
  projects: ProjectRow[]; teams: string[];
  onAdd: (scopeType: string, scopeId: string) => void; t: T;
}) {
  const [open, setOpen] = useState(false);
  const [scopeType, setScopeType] = useState("project");
  const [scopeId, setScopeId] = useState("");
  if (!open) {
    return <button className="ghost" onClick={() => setOpen(true)}>
      {t("res.addScope")}</button>;
  }
  return (
    <span className="row-ops">
      <select value={scopeType} onChange={(e) => { setScopeType(e.target.value);
                                                   setScopeId(""); }}>
        <option value="project">{t("sec.scopeProject")}</option>
        <option value="team">{t("sec.scopeTeam")}</option>
        <option value="global">{t("sec.scopeGlobal")}</option>
      </select>
      {scopeType !== "global" && (
        <select value={scopeId} onChange={(e) => setScopeId(e.target.value)}>
          <option value="">{t("sec.pickScope")}</option>
          {(scopeType === "team" ? teams : projects.map((p) => p.id)).map((id) =>
            <option key={id} value={id}>{id}</option>)}
        </select>
      )}
      <button className="ghost" disabled={scopeType !== "global" && !scopeId}
              onClick={() => { onAdd(scopeType, scopeId); setOpen(false);
                               setScopeId(""); }}>{t("c.add")}</button>
      <button className="ghost" onClick={() => setOpen(false)}>✕</button>
    </span>
  );
}

/** Install state + the vendor command + its full output. A failed install is
 *  expected: the log is what lets the operator fix the command and retry. */
function InstallPanel({ resource, isAdmin, busy, onInstall, t }: {
  resource: Resource; isAdmin: boolean; busy: boolean; onInstall: () => void; t: T;
}) {
  const [openLog, setOpenLog] = useState(false);
  const state = resource.install;
  const badge = state.status === "installed" ? "🟢"
              : state.status === "failed" ? "🔴" : "⚪";
  return (
    <div className="res-install">
      <span>{badge} {t(`res.install.${state.status}`, undefined, state.status)}</span>
      {state.command
        ? <code className="detail">{state.command}</code>
        : <span className="muted">{t("res.field.install_command")}: —</span>}
      {state.version && <span className="card-meta">v{state.version}</span>}
      {state.target && <code className="detail">{state.target}</code>}
      {state.digest && <code className="detail">{state.digest}</code>}
      {isAdmin && state.command && (
        <button className="ghost" disabled={busy} onClick={onInstall}>
          {busy ? t("res.installing")
                : state.status === "absent" ? t("res.install") : t("res.reinstall")}
        </button>
      )}
      {state.log && (
        <button className="ghost" onClick={() => setOpenLog(!openLog)}>
          {openLog ? "▾" : "▸"} {t("res.installLog")}
          {state.exit_code != null ? ` (exit ${state.exit_code})` : ""}</button>
      )}
      {openLog && <pre className="spec">{state.log}</pre>}
      {isAdmin && <p className="muted">{t("res.installHint")}</p>}
    </div>
  );
}

/** Verdict of the last test, with what was actually checked. Three states on
 *  purpose: "reachable but the wrong path" is not the same bug as "host down". */
function TestPanel({ resource, isAdmin, busy, onTest, t }: {
  resource: Resource; isAdmin: boolean; busy: boolean; onTest: () => void; t: T;
}) {
  const [open, setOpen] = useState(false);
  const state = resource.test;
  const badge = state.status === "ok" ? "🟢"
              : state.status === "warn" ? "🟡"
              : state.status === "failed" ? "🔴" : "⚪";
  return (
    <div className="res-install">
      <span>{badge} {t(`res.test.${state.status}`, undefined, state.status)}</span>
      {isAdmin && (
        <button className="ghost" disabled={busy} onClick={onTest}>
          {busy ? t("res.testing")
                : state.status === "unknown" ? t("res.test") : t("res.retest")}
        </button>
      )}
      {state.at && <span className="card-meta">
        {t("res.testAt", { when: new Date(state.at).toLocaleString() })}</span>}
      {state.detail && (
        <button className="ghost" onClick={() => setOpen(!open)}>
          {open ? "▾" : "▸"} {state.checked || t("res.test")}</button>
      )}
      {open && <pre className="spec">{`${state.checked}\n\n${state.detail}`}</pre>}
      {isAdmin && open && <p className="muted">{t("res.testHint")}</p>}
    </div>
  );
}
