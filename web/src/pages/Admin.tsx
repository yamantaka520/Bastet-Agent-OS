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
      <Section title={t("adm.users")}>
        <InlineForm
          fields={[{ name: "name", placeholder: t("adm.namePh") },
                   { name: "role", placeholder: t("adm.rolePh") }]}
          submit={t("c.add")}
          onSubmit={async (v) => {
            const created = await post<{ id: string; token: string }>(
              "/api/users", { name: v.name, role: v.role || "operator" });
            setFreshToken(created.token);
            reloadUsers();
          }} />
        {freshToken && (
          <p className="notice">{t("adm.tokenOnce")}<code>{freshToken}</code></p>
        )}
        <DataTable
          head={["id", t("c.name"), t("c.role"), "enabled", t("adm.headLastUsed"), ""]}
          rows={users.map((u) => [
            u.id, u.name, u.role, u.enabled ? "✅" : "⛔", u.last_used_at ?? "—",
            <button key={u.id} className="ghost" onClick={async () => {
              await post(`/api/users/${u.id}/enabled`, { enabled: !u.enabled });
              reloadUsers();
            }}>{u.enabled ? t("c.disable") : t("c.enable")}</button>,
          ])} />
      </Section>

      <SecretsSection projects={projects} teams={teams} />

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
