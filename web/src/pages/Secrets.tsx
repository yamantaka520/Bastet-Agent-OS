import { useCallback, useEffect, useState } from "react";
import { api, del, post } from "../api";
import { useT, type T } from "../i18n";
import { DataTable, Section } from "../ui";

/** Credentials card: the same resources(kind=secret) + grants tables the
 *  resource pool uses — layered by global / team / project visibility. */

export type Secret = {
  id: string; name: string; enabled: number; secret_scheme: string;
  env_name: string | null; note: string;
  scopes: { scope_type: string; scope_id: string }[];
};
type Project = { id: string; team_id: string };

/** Scope shown the way the credential is granted: global has no id, team and
 *  project carry theirs. Needs `t` because it renders outside a component. */
export function scopeText(s: Secret, t: T): string {
  if (!s.scopes.length) return t("sec.scopeNone");
  return s.scopes.map((sc) => sc.scope_type === "global"
    ? t("sec.labelGlobal")
    : `${t(`sec.label${sc.scope_type === "team" ? "Team" : "Project"}`)}:${sc.scope_id}`)
    .join("、");
}

export default function SecretsSection({ projects, teams, onChanged }: {
  projects: Project[]; teams: string[]; onChanged?: () => void;
}) {
  const t = useT();
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
    <Section title={t("sec.title")}>
      <div className="inline-form">
        <input placeholder={t("sec.namePh")} value={form.name}
               onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input type="password" placeholder={t("sec.valuePh")}
               style={{ width: "18rem" }} value={form.value}
               onChange={(e) => setForm({ ...form, value: e.target.value })} />
        <input placeholder={t("sec.envPh")} value={form.env_name}
               onChange={(e) => setForm({ ...form, env_name: e.target.value })} />
        <select value={form.scope_type}
                onChange={(e) => setForm({ ...form, scope_type: e.target.value,
                                           scope_id: "" })}>
          <option value="project">{t("sec.scopeProject")}</option>
          <option value="team">{t("sec.scopeTeam")}</option>
          <option value="global">{t("sec.scopeGlobal")}</option>
        </select>
        {form.scope_type !== "global" && (
          <select value={form.scope_id}
                  onChange={(e) => setForm({ ...form, scope_id: e.target.value })}>
            <option value="">{t("sec.pickScope")}</option>
            {scopeOptions.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        )}
        <button onClick={add}
                disabled={!form.name || !form.value ||
                          (form.scope_type !== "global" && !form.scope_id)}>
          {t("sec.addSecret")}</button>
      </div>
      {created && (
        <p className="notice">{t("sec.saved", { env: created })}</p>
      )}
      {error && <p className="error">{error}</p>}
      <DataTable
        head={[t("c.name"), t("sec.headScope"), t("sec.headEnv"), t("sec.headStore"),
               t("c.note"), ""]}
        rows={secrets.map((s) => [
          s.name, scopeText(s, t), s.env_name ?? "—", `${s.secret_scheme}:…`, s.note,
          <button key={s.id} className="ghost danger-text" onClick={async () => {
            setError("");
            try {
              const r = await del<{ note: string }>(`/api/secrets/${s.id}`);
              window.alert(r.note);
              load();
              onChanged?.();
            } catch (e) { setError(String((e as Error).message)); }
          }}>{t("c.delete")}</button>,
        ])} />
      <p className="muted">{t("sec.hint")}</p>
    </Section>
  );
}
