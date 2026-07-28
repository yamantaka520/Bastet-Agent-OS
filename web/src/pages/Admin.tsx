import { useEffect, useState } from "react";
import { post } from "../api";
import { DataTable, InlineForm, Section, useList } from "../ui";

type User = { id: string; name: string; role: string; enabled: number;
              created_at: string; last_used_at: string | null };
type Channel = { id: string; kind: string; name: string | null; secret_ref: string;
                 enabled: number; paired_users: string[] };

export default function AdminPage(props: { refreshKey: number }) {
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
    const timer = setInterval(reloadChannels, 3000);  // WS 之外的保險輪詢
    return () => clearInterval(timer);
  }, [pairing, pairDone, reloadChannels]);

  const startPair = async (channel: Channel) => {
    const r = await post<{ code: string }>(`/api/channels/${channel.id}/pair`, {});
    setPairing({ channelId: channel.id, code: r.code,
                 baseline: channel.paired_users.length });
  };

  return (
    <div className="page">
      <Section title="Users">
        <InlineForm
          fields={[{ name: "name", placeholder: "name" },
                   { name: "role", placeholder: "viewer|operator|admin" }]}
          submit="add"
          onSubmit={async (v) => {
            const created = await post<{ id: string; token: string }>(
              "/api/users", { name: v.name, role: v.role || "operator" });
            setFreshToken(created.token);
            reloadUsers();
          }} />
        {freshToken && (
          <p className="notice">token（只顯示這一次）：<code>{freshToken}</code></p>
        )}
        <DataTable
          head={["id", "name", "role", "enabled", "last used", ""]}
          rows={users.map((u) => [
            u.id, u.name, u.role, u.enabled ? "✅" : "⛔", u.last_used_at ?? "—",
            <button key={u.id} className="ghost" onClick={async () => {
              await post(`/api/users/${u.id}/enabled`, { enabled: !u.enabled });
              reloadUsers();
            }}>{u.enabled ? "disable" : "enable"}</button>,
          ])} />
      </Section>

      <Section title="Channels (Telegram)">
        <InlineForm
          fields={[{ name: "name", placeholder: "名稱（例：值班通知）" },
                   { name: "secret_ref",
                     placeholder: "bot token ref, e.g. keyring:bastet/tg-bot",
                     width: "22rem" }]}
          submit="add telegram"
          onSubmit={async (v) => {
            await post("/api/channels", { kind: "telegram", name: v.name,
                                          secret_ref: v.secret_ref });
            reloadChannels();
          }} />
        <DataTable
          head={["名稱", "id", "kind", "secret", "enabled", "已配對", ""]}
          rows={channels.map((c) => [
            c.name ?? c.kind, c.id, c.kind, c.secret_ref, c.enabled ? "✅" : "⛔",
            c.paired_users.join(", ") || "—",
            <button key={c.id} className="ghost"
                    onClick={() => startPair(c)}>pair…</button>,
          ])} />
        {pairing && !pairDone && (
          <p className="notice">⏳ 對 bot 傳：<code>/pair {pairing.code}</code>
            （15 分鐘內有效）— 等待配對中，完成會自動顯示…</p>
        )}
        {pairing && pairDone && (
          <p className="notice">✅ 配對完成：{pairedChannel!.paired_users.join(", ")}
            <button className="ghost" onClick={() => setPairing(null)}>知道了</button>
          </p>
        )}
        <p className="muted">新增 channel 後需重啟 <code>bastet serve</code> 才會開始輪詢。</p>
      </Section>
    </div>
  );
}
