import { useEffect, useState } from "react";
import { del, post } from "../api";
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
                 enabled: number; paired_users: string[]; status: string };

type ProjectRow = { id: string; team_id: string };

export default function AdminPage(props: { refreshKey: number }) {
  const t = useT();
  const [projects] = useList<ProjectRow>("/api/projects", props.refreshKey);
  const teams = [...new Set(projects.map((p) => p.team_id))];
  const [users, reloadUsers] = useList<User>("/api/users", props.refreshKey);
  const [channels, reloadChannels] = useList<Channel>("/api/channels", props.refreshKey);
  const [freshToken, setFreshToken] = useState<string | null>(null);
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
        <p className="muted">{t("adm.restartHint")}</p>
      </Section>
    </div>
  );
}
