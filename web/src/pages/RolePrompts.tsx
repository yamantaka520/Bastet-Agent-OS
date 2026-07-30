import { useCallback, useEffect, useState } from "react";
import { api, del, post } from "../api";
import { useT, useVocab } from "../i18n";
import { Section } from "../ui";

/** Role definition prompts: what a role means when an agent plays it.
 *  Applied at run time — the stage's role prompt is prepended to the task. */

type RolePrompt = {
  role: string; label: string; prompt: string; builtin: number;
  in_use: boolean; used_by: { templates: string[]; projects: string[] };
};

export default function RolePromptsSection({ canOperate }: { canOperate: boolean }) {
  const t = useT();
  const vocab = useVocab();
  const [rows, setRows] = useState<RolePrompt[]>([]);
  const [openRole, setOpenRole] = useState<string | null>(null);
  const [draft, setDraft] = useState({ role: "", label: "", prompt: "" });
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<RolePrompt[]>("/api/role-prompts").then(setRows).catch(() => setRows([]));
  }, []);
  useEffect(load, [load]);

  const save = async (role: string, label: string, prompt: string) => {
    setError("");
    try {
      await post("/api/role-prompts", { role, label, prompt });
      setOpenRole(null);
      setDraft({ role: "", label: "", prompt: "" });
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const remove = async (role: string) => {
    setError("");
    try { await del(`/api/role-prompts/${encodeURIComponent(role)}`); load(); }
    catch (e) { setError(String((e as Error).message)); }
  };

  return (
    <Section title={t("rp.title")}
             action={canOperate && openRole !== "__new__" ? (
               <button onClick={() => {
                 setOpenRole("__new__");
                 setDraft({ role: "", label: "", prompt: "" });
               }}>{t("rp.addRole")}</button>
             ) : undefined}>
      {error && <p className="error">{error}</p>}
      {openRole === "__new__" && (
        <div className="stage-editor">
          <div className="inline-form">
            <input placeholder={t("rp.roleIdPh")} value={draft.role}
                   onChange={(e) => setDraft({ ...draft, role: e.target.value })} />
            <input placeholder={t("rp.labelPh")} value={draft.label}
                   onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
          </div>
          <textarea rows={4} placeholder={t("rp.promptPh")}
                    value={draft.prompt}
                    onChange={(e) => setDraft({ ...draft, prompt: e.target.value })} />
          <div className="row">
            <button disabled={!draft.role.trim() || !draft.prompt.trim()}
                    onClick={() => save(draft.role.trim(), draft.label, draft.prompt)}>
              {t("c.create")}</button>
            <button className="ghost"
                    onClick={() => setOpenRole(null)}>{t("c.cancel")}</button>
          </div>
        </div>
      )}
      {rows.map((r) => (
        <div key={r.role} className="role-prompt">
          <div className="role-head">
            <b>👤 {vocab.roleLabel(r.role, r.label)}</b>
            <code className="detail">{r.role}</code>
            {r.builtin ? <span className="flow-tag">{t("c.builtin")}</span> : null}
            <span className="card-meta">
              {r.in_use
                ? t("c.inUse", { list: [...r.used_by.templates,
                                        ...r.used_by.projects].join("、") })
                : t("c.notUsed")}</span>
            {canOperate && (
              <span className="row-ops">
                <button className="ghost" onClick={() =>
                  setOpenRole(openRole === r.role ? null : r.role)}>
                  {openRole === r.role ? t("c.collapse") : t("c.edit")}</button>
                <button className="ghost danger-text" disabled={r.in_use}
                        title={r.in_use ? t("rp.cantDelete") : ""}
                        onClick={() => remove(r.role)}>{t("c.delete")}</button>
              </span>
            )}
          </div>
          {openRole === r.role ? (
            <EditPrompt row={r} onSave={(prompt) => save(r.role, r.label, prompt)} />
          ) : (
            <p className="muted role-prompt-text">{r.prompt}</p>
          )}
        </div>
      ))}
      <p className="muted">{t("rp.hint")}</p>
    </Section>
  );
}

function EditPrompt({ row, onSave }: { row: RolePrompt; onSave: (p: string) => void }) {
  const t = useT();
  const [text, setText] = useState(row.prompt);
  return (
    <>
      <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="row">
        <button onClick={() => onSave(text)} disabled={!text.trim()}>{t("c.save")}</button>
      </div>
    </>
  );
}
