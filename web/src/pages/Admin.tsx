import { useState } from "react";
import { post } from "../api";
import { DataTable, InlineForm, Section, useList } from "../ui";

type User = { id: string; name: string; role: string; enabled: number;
              created_at: string; last_used_at: string | null };
type Channel = { id: string; kind: string; secret_ref: string; enabled: number;
                 paired_users: string[] };

export default function AdminPage(props: { refreshKey: number }) {
  const [users, reloadUsers] = useList<User>("/api/users");
  const [channels, reloadChannels] = useList<Channel>("/api/channels");
  const [freshToken, setFreshToken] = useState<string | null>(null);
  const [pairCode, setPairCode] = useState<string | null>(null);

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
            <button className="ghost" onClick={async () => {
              await post(`/api/users/${u.id}/enabled`, { enabled: !u.enabled });
              reloadUsers();
            }}>{u.enabled ? "disable" : "enable"}</button>,
          ])} />
      </Section>

      <Section title="Channels (Telegram)">
        <InlineForm
          fields={[{ name: "secret_ref",
                     placeholder: "bot token ref, e.g. keyring:bastet/tg-bot",
                     width: "22rem" }]}
          submit="add telegram"
          onSubmit={async (v) => {
            await post("/api/channels", { kind: "telegram", secret_ref: v.secret_ref });
            reloadChannels();
          }} />
        <DataTable
          head={["id", "kind", "secret", "enabled", "paired", ""]}
          rows={channels.map((c) => [
            c.id, c.kind, c.secret_ref, c.enabled ? "✅" : "⛔",
            c.paired_users.join(", ") || "—",
            <button className="ghost" onClick={async () => {
              const r = await post<{ code: string }>(`/api/channels/${c.id}/pair`, {});
              setPairCode(r.code);
            }}>pair…</button>,
          ])} />
        {pairCode && (
          <p className="notice">對 bot 傳：<code>/pair {pairCode}</code>（15 分鐘內有效）</p>
        )}
        <p className="muted">新增 channel 後需重啟 <code>bastet serve</code> 才會開始輪詢。</p>
      </Section>
    </div>
  );
}
