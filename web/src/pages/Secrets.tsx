import { useCallback, useEffect, useState } from "react";
import { api, del, post } from "../api";
import { DataTable, Section } from "../ui";

/** Credentials card: the same resources(kind=secret) + grants tables the
 *  resource pool uses — layered by 全域 / TEAM / 專案 visibility. */

export type Secret = {
  id: string; name: string; enabled: number; secret_scheme: string;
  env_name: string | null; note: string;
  scopes: { scope_type: string; scope_id: string }[];
};
type Project = { id: string; team_id: string };

const SCOPE_LABEL: Record<string, string> = {
  global: "🌐 全域", team: "🏷 團隊", project: "📁 專案",
};

export function scopeText(s: Secret): string {
  if (!s.scopes.length) return "（未授權任何範圍）";
  return s.scopes.map((sc) => sc.scope_type === "global"
    ? SCOPE_LABEL.global
    : `${SCOPE_LABEL[sc.scope_type] ?? sc.scope_type}:${sc.scope_id}`).join("、");
}

export default function SecretsSection({ projects, teams, onChanged }: {
  projects: Project[]; teams: string[]; onChanged?: () => void;
}) {
  const [secrets, setSecrets] = useState<Secret[]>([]);
  const [form, setForm] = useState({ name: "", value: "", env_name: "",
                                     scope_type: "project", scope_id: "", note: "" });
  const [created, setCreated] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<Secret[]>("/api/secrets").then(setSecrets).catch(() => setSecrets([]));
  }, []);
  useEffect(load, [load]);

  const scopeOptions = form.scope_type === "project"
    ? projects.map((p) => p.id)
    : form.scope_type === "team" ? teams : [];

  const add = async () => {
    setError("");
    setCreated(null);
    try {
      const r = await post<{ env_name: string }>("/api/secrets", form);
      setCreated(r.env_name);
      setForm({ ...form, name: "", value: "", env_name: "", note: "" });
      load();
      onChanged?.();
    } catch (e) { setError(String((e as Error).message)); }
  };

  return (
    <Section title="憑證與機敏資料（Token / KEY / 帳密），依可見範圍分層">
      <div className="inline-form">
        <input placeholder="名稱（例：部署 Token）" value={form.name}
               onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input type="password" placeholder="內容（或貼既有 ref keyring:/file:/env:）"
               style={{ width: "18rem" }} value={form.value}
               onChange={(e) => setForm({ ...form, value: e.target.value })} />
        <input placeholder="環境變數名（可留空自動產生）" value={form.env_name}
               onChange={(e) => setForm({ ...form, env_name: e.target.value })} />
        <select value={form.scope_type}
                onChange={(e) => setForm({ ...form, scope_type: e.target.value,
                                           scope_id: "" })}>
          <option value="project">📁 專案可見</option>
          <option value="team">🏷 團隊可見</option>
          <option value="global">🌐 全域可見</option>
        </select>
        {form.scope_type !== "global" && (
          <select value={form.scope_id}
                  onChange={(e) => setForm({ ...form, scope_id: e.target.value })}>
            <option value="">選擇範圍…</option>
            {scopeOptions.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        )}
        <button onClick={add}
                disabled={!form.name || !form.value ||
                          (form.scope_type !== "global" && !form.scope_id)}>
          ＋ 新增憑證</button>
      </div>
      {created && (
        <p className="notice">已儲存。任務執行時會以環境變數 <code>{created}</code> 注入
          可見範圍內的 run（值只存在磁碟上的 0600 檔案／keyring，不進資料庫）。</p>
      )}
      {error && <p className="error">{error}</p>}
      <DataTable
        head={["名稱", "可見範圍", "注入環境變數", "存放方式", "備註", ""]}
        rows={secrets.map((s) => [
          s.name, scopeText(s), s.env_name ?? "—", `${s.secret_scheme}:…`, s.note,
          <button key={s.id} className="ghost danger-text" onClick={async () => {
            setError("");
            try {
              const r = await del<{ note: string }>(`/api/secrets/${s.id}`);
              window.alert(r.note);
              load();
              onChanged?.();
            } catch (e) { setError(String((e as Error).message)); }
          }}>刪除</button>,
        ])} />
      <p className="muted">與「資源」頁同一份資料（resources + grants）— 這裡只是依
        可見範圍呈現的另一個視角。注入 run 的憑證應假設 agent 可讀取，請優先使用
        短效、最小範圍的 token。</p>
    </Section>
  );
}
