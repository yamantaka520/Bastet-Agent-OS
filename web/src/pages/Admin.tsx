import { useEffect, useState } from "react";
import { api, del, post, put } from "../api";
import SecretsSection from "./Secrets";
import { useT } from "../i18n";
import { DataTable, InlineForm, Section, useList } from "../ui";

const CHANNEL_STATUS: Record<string, string> = {
  polling: "adm.chPolling", credential_error: "adm.chCredError",
  restart_needed: "adm.chRestart", disabled: "adm.chDisabled",
};

type User = { id: string; name: string; role: string; enabled: number;
              created_at: string; last_used_at: string | null };
type Channel = { id: string; kind: string; name: string | null; secret_ref: string;
                 enabled: number; paired_users: string[]; status: string;
                 responder: { kind: string; id: string } | null;
                 project_id: string };
type Responder = { kind: string; id: string; label: string; detail: string };
type RoleCap = { id: string; rank: number; can: string[]; cannot: string[] };

type ProjectRow = { id: string; team_id: string };

export default function AdminPage(props: { refreshKey: number }) {
  const t = useT();
  const [projects] = useList<ProjectRow>("/api/projects", props.refreshKey);
  const teams = [...new Set(projects.map((p) => p.team_id))];
  const [users, reloadUsers] = useList<User>("/api/users", props.refreshKey);
  const [channels, reloadChannels] = useList<Channel>("/api/channels", props.refreshKey);
  const [freshToken, setFreshToken] = useState<string | null>(null);
  const [responders, setResponders] = useState<Responder[]>([]);
  useEffect(() => {
    api<Responder[]>("/api/chat/responders").then(setResponders).catch(() => {});
  }, []);
  const [pairing, setPairing] = useState<{ channelId: string; code: string;
                                           baseline: number } | null>(null);

  // pairing feedback: when the channel's paired list grows past the baseline,
  // the /pair on Telegram worked (WS channel.paired bumps refreshKey for us)
  const pairedChannel = pairing
    ? channels.find((c) => c.id === pairing.channelId) : null;
  const pairDone = !!(pairing && pairedChannel
    && pairedChannel.paired_users.length > pairing.baseline);
  useEffect(() => {
    if (!pairing || pairDone) return;
    const timer = setInterval(reloadChannels, 3000);  // belt-and-braces poll
    return () => clearInterval(timer);
  }, [pairing, pairDone, reloadChannels]);

  const startPair = async (channel: Channel) => {
    const r = await post<{ code: string }>(`/api/channels/${channel.id}/pair`, {});
    setPairing({ channelId: channel.id, code: r.code,
                 baseline: channel.paired_users.length });
  };

  return (
    <div className="page">
      <UsersSection users={users} reload={reloadUsers} freshToken={freshToken}
                    setFreshToken={setFreshToken} />

      <SecretsSection projects={projects} teams={teams} />

      <MaintenanceSection />

      <Section title={t("adm.channels")}>
        <InlineForm
          fields={[{ name: "name", placeholder: t("adm.channelNamePh") },
                   { name: "secret_ref", placeholder: t("adm.botTokenPh"),
                     width: "24rem" }]}
          submit={t("adm.addTelegram")}
          onSubmit={async (v) => {
            await post("/api/channels", { kind: "telegram", name: v.name,
                                          secret_ref: v.secret_ref });
            reloadChannels();
          }} />
        <DataTable
          head={[t("c.name"), "kind", "secret", t("c.status"), t("adm.headPaired"), ""]}
          rows={channels.map((c) => [
            c.name ?? c.kind, c.kind, c.secret_ref,
            CHANNEL_STATUS[c.status] ? t(CHANNEL_STATUS[c.status]) : c.status,
            c.paired_users.join(", ") || "—",
            <span key={c.id} className="row-ops">
              <button className="ghost" onClick={() => startPair(c)}>{t("adm.pair")}</button>
              <button className="ghost" onClick={async () => {
                await post(`/api/channels/${c.id}/enabled`, { enabled: !c.enabled });
                reloadChannels();
              }}>{c.enabled ? t("c.disable") : t("c.enable")}</button>
              <button className="ghost danger-text" onClick={async () => {
                await del(`/api/channels/${c.id}`);
                reloadChannels();
              }}>{t("c.delete")}</button>
            </span>,
          ])} />
        {pairing && !pairDone && (
          <p className="notice">{t("adm.pairWaiting", { code: pairing.code })}</p>
        )}
        {pairing && pairDone && (
          <p className="notice">
            {t("adm.pairDone", { users: pairedChannel!.paired_users.join(", ") })}
            <button className="ghost"
                    onClick={() => setPairing(null)}>{t("adm.gotIt")}</button>
          </p>
        )}
        {channels.map((c) => (
          <ChannelChat key={`chat-${c.id}`} channel={c} responders={responders}
                       projects={projects} onSaved={reloadChannels} />
        ))}
        <p className="muted">{t("adm.restartHint")}</p>
      </Section>
    </div>
  );
}

/** Which agent/LLM answers plain messages on this channel, for which project —
 *  the second authorisation channel next to the WebUI chat. */
function ChannelChat({ channel, responders, projects, onSaved }: {
  channel: Channel; responders: Responder[]; projects: ProjectRow[];
  onSaved: () => void;
}) {
  const t = useT();
  const [responder, setResponder] = useState(
    channel.responder ? `${channel.responder.kind}:${channel.responder.id}` : "");
  const [projectId, setProjectId] = useState(channel.project_id || "");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
    setError("");
    const [kind, id] = responder ? responder.split(":") : ["", ""];
    try {
      await put(`/api/channels/${channel.id}/chat`,
                { responder_kind: kind, responder_id: id, project_id: projectId });
      setSaved(true);
      onSaved();
    } catch (e) { setError(String((e as Error).message)); }
  };

  return (
    <div className="res-row">
      <div className="res-head">
        <b>{channel.name ?? channel.kind}</b>
        <span className="card-meta">{t("adm.chatChannel")}</span>
      </div>
      <div className="inline-form">
        <select value={responder} onChange={(e) => { setResponder(e.target.value);
                                                     setSaved(false); }}>
          <option value="">{t("adm.chatNone")}</option>
          <optgroup label={t("chat.groupAgents")}>
            {responders.filter((r) => r.kind === "agent").map((r) => (
              <option key={r.id} value={`agent:${r.id}`}>{r.label}</option>
            ))}
          </optgroup>
          <optgroup label={t("chat.groupLLMs")}>
            {responders.filter((r) => r.kind === "resource").map((r) => (
              <option key={r.id} value={`resource:${r.id}`}>
                {r.label}（{r.detail}）</option>
            ))}
          </optgroup>
        </select>
        <label className="res-field">
          <span>{t("adm.chatProject")}</span>
          <select value={projectId} onChange={(e) => { setProjectId(e.target.value);
                                                       setSaved(false); }}>
            <option value="">{t("sec.labelGlobal")}</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
          </select>
        </label>
        <button onClick={save}>{t("adm.chatSave")}</button>
        {saved && <span className="muted">✅</span>}
        {error && <span className="error">{error}</span>}
      </div>
      <p className="muted">{t("adm.chatHint")}</p>
    </div>
  );
}

/** Users: the role dropdown explains what it grants, and a token can be copied
 *  once, disabled, rotated (old one dies at once) or deleted. */
function UsersSection({ users, reload, freshToken, setFreshToken }: {
  users: User[]; reload: () => void; freshToken: string | null;
  setFreshToken: (token: string | null) => void;
}) {
  const t = useT();
  const [roles, setRoles] = useState<RoleCap[]>([]);
  const [draft, setDraft] = useState({ name: "", role: "operator" });
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<RoleCap[]>("/api/user-roles").then(setRoles).catch(() => {});
  }, []);

  const guard = async (fn: () => Promise<unknown>) => {
    setError("");
    try { await fn(); reload(); }
    catch (e) { setError(String((e as Error).message)); }
  };

  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch { setError("clipboard blocked — select the token and copy manually"); }
  };

  const selected = roles.find((r) => r.id === draft.role);

  return (
    <Section title={t("adm.users")}>
      {error && <p className="error">{error}</p>}
      <div className="inline-form">
        <input placeholder={t("adm.namePh")} value={draft.name}
               onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        <label className="res-field">
          <span>{t("c.role")}</span>
          <select value={draft.role}
                  onChange={(e) => setDraft({ ...draft, role: e.target.value })}>
            {(roles.length ? roles.map((r) => r.id)
                           : ["viewer", "operator", "admin"]).map((id) => (
              <option key={id} value={id}>{id}</option>
            ))}
          </select>
        </label>
        <button disabled={!draft.name.trim()} onClick={() => guard(async () => {
          const created = await post<{ token: string }>(
            "/api/users", { name: draft.name.trim(), role: draft.role });
          setFreshToken(created.token);
          setDraft({ name: "", role: draft.role });
        })}>{t("c.add")}</button>
      </div>
      {selected && (
        <p className="muted">
          <b>{selected.id}</b> — {t("adm.roleCan")}: {selected.can.join("、")}
          {selected.cannot.length
            ? ` ／ ${t("adm.roleCannot")}: ${selected.cannot.join("、")}` : ""}
        </p>
      )}
      {freshToken && (
        <p className="notice">{t("adm.tokenOnce")}
          <code className="token-value">{freshToken}</code>
          <button className="ghost" onClick={() => copy(freshToken)}>
            {copied ? t("adm.copied") : t("adm.copy")}</button>
          <button className="ghost" onClick={() => setFreshToken(null)}>
            {t("adm.gotIt")}</button>
        </p>
      )}
      <DataTable
        head={["id", t("c.name"), t("c.role"), "enabled", t("adm.headLastUsed"), ""]}
        rows={users.map((u) => [
          u.id, u.name,
          <select key={`role-${u.id}`} value={u.role}
                  onChange={(e) => guard(() =>
                    put(`/api/users/${u.id}`, { role: e.target.value }))}>
            {(roles.length ? roles.map((r) => r.id)
                           : ["viewer", "operator", "admin"]).map((id) => (
              <option key={id} value={id}>{id}</option>
            ))}
          </select>,
          u.enabled ? "✅" : "⛔", u.last_used_at ?? "—",
          <span key={`ops-${u.id}`} className="row-ops">
            <button className="ghost" onClick={() => guard(() =>
              post(`/api/users/${u.id}/enabled`, { enabled: !u.enabled }))}>
              {u.enabled ? t("c.disable") : t("c.enable")}</button>
            <button className="ghost" onClick={() => {
              if (!window.confirm(t("adm.rotateWarn"))) return;
              guard(async () => {
                const fresh = await post<{ token: string }>(
                  `/api/users/${u.id}/token`, {});
                setFreshToken(fresh.token);
              });
            }}>{t("adm.rotate")}</button>
            <button className="ghost danger-text" onClick={() => {
              if (!window.confirm(t("adm.deleteUser"))) return;
              guard(() => del(`/api/users/${u.id}`));
            }}>{t("c.delete")}</button>
          </span>,
        ])} />
      <p className="muted">{t("adm.roleHint")}</p>
    </Section>
  );
}

/** Maintenance: what is installed, what is newer, update one or all.
 *
 *  Bastet orchestrates other people's tools, so staying current is a question
 *  about a dozen things installed in different ways. Nothing self-updates —
 *  changing the agents under a running project is not something you could
 *  reason about afterwards. */
type Component = { id: string; label: string; kind: string; installed: string | null;
                   available: string | null; state: string; source: string };
type UpdateResult = { id: string; status: string; from: string | null;
                      to: string | null; log: string; restart_required: boolean };

const STATE_BADGE: Record<string, string> = {
  current: "🟢", outdated: "🟡", missing: "🔴", unknown: "⚪",
};

function MaintenanceSection() {
  const t = useT();
  const [rows, setRows] = useState<Component[] | null>(null);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<UpdateResult[] | null>(null);
  const [error, setError] = useState("");

  const check = async () => {
    setBusy("check");
    setError("");
    try { setRows(await api<Component[]>("/api/maintenance/components")); }
    catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(""); }
  };
  useEffect(() => { check(); }, []);

  const updateOne = async (id: string) => {
    setBusy(id);
    setError("");
    try {
      const out = await post<UpdateResult>(
        `/api/maintenance/components/${id}/update`, {});
      setResult([out]);
      await check();
    } catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(""); }
  };

  const updateAll = async () => {
    if (!window.confirm(t("mnt.allConfirm"))) return;
    setBusy("all");
    setError("");
    try {
      const out = await post<{ results: UpdateResult[] }>(
        "/api/maintenance/update-all", {});
      setResult(out.results);
      await check();
    } catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(""); }
  };

  const outdated = (rows ?? []).filter((r) => r.state === "outdated").length;
  const restart = (result ?? []).some((r) => r.restart_required);

  return (
    <Section title={t("mnt.title")}
             action={
               <span className="row-ops">
                 <button className="ghost" disabled={!!busy} onClick={check}>
                   {busy === "check" ? t("mnt.checking") : t("mnt.checkAll")}</button>
                 <button disabled={!!busy} onClick={updateAll}>
                   {busy === "all" ? t("mnt.updating") : t("mnt.updateAll")}</button>
               </span>}>
      {error && <p className="error">{error}</p>}
      {!rows && <p className="muted">{t("mnt.checking")}</p>}
      {rows && (
        <>
          <p className="muted">{outdated
            ? t("mnt.outdatedCount", { n: outdated })
            : t("mnt.allCurrent")}</p>
          <DataTable
            head={[t("c.name"), t("mnt.installed"), t("mnt.available"),
                   t("c.status"), ""]}
            rows={rows.map((r) => [
              r.label,
              r.installed ?? "—",
              r.available ?? "—",
              <span key={`s-${r.id}`}>
                {STATE_BADGE[r.state] ?? "⚪"} {t(`mnt.state.${r.state}`,
                                                 undefined, r.state)}</span>,
              <button key={`u-${r.id}`} className="ghost" disabled={!!busy}
                      onClick={() => updateOne(r.id)}>
                {busy === r.id ? t("mnt.updating")
                               : r.state === "missing" ? t("mnt.install")
                               : t("mnt.update")}</button>,
            ])} />
        </>
      )}
      {result && (
        <div className="stage-editor">
          {result.map((r) => (
            <p key={r.id} className={r.status === "failed" ? "error" : "notice"}>
              {r.id}: {t(`mnt.result.${r.status}`, undefined, r.status)}
              {r.from || r.to ? ` (${r.from ?? "—"} → ${r.to ?? "—"})` : ""}
            </p>
          ))}
          {restart && <p className="notice">{t("mnt.restartNeeded")}</p>}
          <details>
            <summary className="muted">{t("res.installLog")}</summary>
            <pre className="spec">{result.map((r) => r.log).join("\n\n")}</pre>
          </details>
        </div>
      )}
      <p className="muted">{t("mnt.hint")}</p>
    </Section>
  );
}
