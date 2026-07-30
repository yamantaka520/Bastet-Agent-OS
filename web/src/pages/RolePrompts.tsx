import { useCallback, useEffect, useState } from "react";
import { api, del, post } from "../api";
import { Section } from "../ui";

/** Role definition prompts: what a role means when an agent plays it.
 *  Applied at run time — the stage's role prompt is prepended to the task. */

type RolePrompt = {
  role: string; label: string; prompt: string; builtin: number;
  in_use: boolean; used_by: { templates: string[]; projects: string[] };
};

export default function RolePromptsSection({ canOperate }: { canOperate: boolean }) {
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
    <Section title="角色定義 Prompt（任務執行時自動套用，提升精準度）"
             action={canOperate && openRole !== "__new__" ? (
               <button onClick={() => {
                 setOpenRole("__new__");
                 setDraft({ role: "", label: "", prompt: "" });
               }}>＋ 新增角色</button>
             ) : undefined}>
      {error && <p className="error">{error}</p>}
      {openRole === "__new__" && (
        <div className="stage-editor">
          <div className="inline-form">
            <input placeholder="角色 id（英數，例：data-engineer）" value={draft.role}
                   onChange={(e) => setDraft({ ...draft, role: e.target.value })} />
            <input placeholder="顯示名稱（例：資料工程師）" value={draft.label}
                   onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
          </div>
          <textarea rows={4} placeholder="這個角色該怎麼工作、重視什麼、不做什麼"
                    value={draft.prompt}
                    onChange={(e) => setDraft({ ...draft, prompt: e.target.value })} />
          <div className="row">
            <button disabled={!draft.role.trim() || !draft.prompt.trim()}
                    onClick={() => save(draft.role.trim(), draft.label, draft.prompt)}>
              建立</button>
            <button className="ghost" onClick={() => setOpenRole(null)}>取消</button>
          </div>
        </div>
      )}
      {rows.map((r) => (
        <div key={r.role} className="role-prompt">
          <div className="role-head">
            <b>👤 {r.label}</b>
            <code className="detail">{r.role}</code>
            {r.builtin ? <span className="flow-tag">內建</span> : null}
            <span className="card-meta">
              {r.in_use
                ? `使用中：${[...r.used_by.templates, ...r.used_by.projects].join("、")}`
                : "尚未使用"}</span>
            {canOperate && (
              <span className="row-ops">
                <button className="ghost" onClick={() =>
                  setOpenRole(openRole === r.role ? null : r.role)}>
                  {openRole === r.role ? "收起" : "編輯"}</button>
                <button className="ghost danger-text" disabled={r.in_use}
                        title={r.in_use ? "此角色仍被範本或專案使用中，無法刪除" : ""}
                        onClick={() => remove(r.role)}>刪除</button>
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
      <p className="muted">工作流階段指定了角色時，該角色的定義會加在任務最前面
        （agent 會被明確告知身分與工作準則）；未指定角色的階段不套用。</p>
    </Section>
  );
}

function EditPrompt({ row, onSave }: { row: RolePrompt; onSave: (p: string) => void }) {
  const [text, setText] = useState(row.prompt);
  return (
    <>
      <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="row">
        <button onClick={() => onSave(text)} disabled={!text.trim()}>儲存</button>
      </div>
    </>
  );
}
