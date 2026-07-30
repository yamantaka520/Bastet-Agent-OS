import { useCallback, useEffect, useState } from "react";
import { api, del, post } from "../api";
import { Section } from "../ui";

/** Project-centric role assignment: one block per project, many agent→role
 *  rows inside, everything picked from dropdowns. */

type Assignment = { project_id: string; agent_id: string; role: string;
                    preference: number; agent_name: string; executor_type: string };
type Project = { id: string; team_id: string };
type Agent = { id: string; name: string; executor_type: string; enabled: number };
type Role = { id: string; label: string; hint: string };

export default function RoleAssignSection({ canOperate, projects, agents, roles }: {
  canOperate: boolean; projects: Project[]; agents: Agent[]; roles: Role[];
}) {
  const [rows, setRows] = useState<Assignment[]>([]);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<Record<string, { agent: string; role: string;
                                                     pref: string }>>({});

  const load = useCallback(() => {
    api<Assignment[]>("/api/roles").then(setRows).catch(() => setRows([]));
  }, []);
  useEffect(load, [load]);

  const roleLabel = (id: string) => roles.find((r) => r.id === id)?.label ?? id;
  const draftFor = (pid: string) => draft[pid] ?? { agent: "", role: "", pref: "0" };
  const setDraftFor = (pid: string, patch: Partial<{ agent: string; role: string;
                                                     pref: string }>) =>
    setDraft({ ...draft, [pid]: { ...draftFor(pid), ...patch } });

  const add = async (pid: string) => {
    const d = draftFor(pid);
    setError("");
    try {
      await post("/api/roles", { project_id: pid, agent_id: d.agent, role: d.role,
                                 preference: Number(d.pref || 0) });
      setDraftFor(pid, { agent: "", role: "" });
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const remove = async (a: Assignment) => {
    setError("");
    try {
      await del(`/api/roles?project_id=${encodeURIComponent(a.project_id)}` +
                `&agent_id=${encodeURIComponent(a.agent_id)}` +
                `&role=${encodeURIComponent(a.role)}`);
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  return (
    <Section title="角色指派（以專案為單位）">
      {error && <p className="error">{error}</p>}
      {!projects.length && <p className="muted">先建立專案，才能指派角色。</p>}
      {projects.map((p) => {
        const mine = rows.filter((r) => r.project_id === p.id);
        const byRole: Record<string, Assignment[]> = {};
        mine.forEach((r) => { (byRole[r.role] ??= []).push(r); });
        const d = draftFor(p.id);
        return (
          <div key={p.id} className="role-block">
            <div className="role-head">
              <b>📁 {p.id}</b>
              <span className="card-meta">{mine.length} 筆指派</span>
            </div>
            {!mine.length && <p className="muted">尚未指派任何角色。</p>}
            {Object.entries(byRole).map(([role, list]) => (
              <div key={role} className="role-row">
                <span className="role-name">👤 {roleLabel(role)}</span>
                <span className="role-agents">
                  {list.map((a) => (
                    <span key={a.agent_id} className="role-chip">
                      {a.agent_name}
                      <span className="card-meta"> ({a.executor_type}
                        {list.length > 1 ? ` · 優先 ${a.preference}` : ""})</span>
                      {canOperate && (
                        <button className="ghost chip-x"
                                onClick={() => remove(a)}>✕</button>
                      )}
                    </span>
                  ))}
                </span>
              </div>
            ))}
            {canOperate && (
              <div className="inline-form">
                <select value={d.agent}
                        onChange={(e) => setDraftFor(p.id, { agent: e.target.value })}>
                  <option value="">選擇 Agent…</option>
                  {agents.filter((a) => a.enabled).map((a) => (
                    <option key={a.id} value={a.id}>{a.name}（{a.executor_type}）</option>
                  ))}
                </select>
                <select value={d.role}
                        onChange={(e) => setDraftFor(p.id, { role: e.target.value })}>
                  <option value="">選擇角色…</option>
                  {roles.map((r) => (
                    <option key={r.id} value={r.id}>{r.label}</option>
                  ))}
                </select>
                <label className="chk">優先順序
                  <input type="number" min={0} max={99} style={{ width: "4.5rem" }}
                         value={d.pref}
                         onChange={(e) => setDraftFor(p.id, { pref: e.target.value })} />
                </label>
                <button disabled={!d.agent || !d.role}
                        onClick={() => add(p.id)}>＋ 指派</button>
              </div>
            )}
          </div>
        );
      })}
      <p className="muted">
        同一個專案可以有多個 Agent、多個角色；同一個角色也能有多個 Agent。
        <b>優先順序</b>是同角色多人時的挑選依據 —— 工作流跑到需要該角色的階段時，
        Bastet 會選數字最大的那個 Agent（其餘視為備援）。
      </p>
    </Section>
  );
}
