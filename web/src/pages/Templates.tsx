import { useCallback, useEffect, useState } from "react";
import { api, del, post } from "../api";
import RolePromptsSection from "./RolePrompts";
import { Section, useList } from "../ui";

type Stage = {
  name: string; role?: string | null; gate: string;
  gate_config?: { command?: string }; read_only?: boolean;
  max_retries?: number; desc?: string;
};
type Preset = { id: string; name: string; description: string; stages: Stage[] };
type Role = { id: string; label: string; hint: string };
type Gate = { id: string; label: string; icon: string; hint: string };
type Catalog = { presets: Preset[]; roles: Role[]; gates: Gate[] };
type Template = { id: string; version: number; stages_json: string;
                  assigned_projects: string[] };
type Project = { id: string; team_id: string; default_template_id: string | null };

const BLANK_STAGE: Stage = { name: "", role: null, gate: "auto" };

export default function TemplatesPage(props: { canOperate: boolean; refreshKey: number }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [templates, reloadTemplates] = useList<Template>("/api/templates", props.refreshKey);
  const [projects, reloadProjects] = useList<Project>("/api/projects", props.refreshKey);
  const [openId, setOpenId] = useState<string | null>(null);
  const [builder, setBuilder] = useState<{ name: string; stages: Stage[] } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Catalog>("/api/workflow-catalog").then(setCatalog).catch(() => {});
  }, []);

  const roleLabel = (id?: string | null) =>
    catalog?.roles.find((r) => r.id === id)?.label ?? (id || "未指定角色");
  const gateInfo = (id: string) =>
    catalog?.gates.find((g) => g.id === id) ?? { id, label: id, icon: "•", hint: "" };

  const parseStages = (json: string): Stage[] => {
    try { return JSON.parse(json) as Stage[]; } catch { return []; }
  };

  const refreshAll = () => { reloadTemplates(); reloadProjects(); };

  const copyToMine = (name: string, stages: Stage[]) => {
    setBuilder({ name: `${name} (我的版本)`, stages: stages.map((s) => ({ ...s })) });
    setError("");
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  };

  const assign = async (projectId: string, templateId: string | null) => {
    setError("");
    try {
      await post(`/api/projects/${projectId}/template`, { template_id: templateId });
      refreshAll();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const saveBuilder = async () => {
    if (!builder) return;
    setError("");
    const clean = builder.stages
      .filter((s) => s.name.trim())
      .map((s) => ({
        name: s.name.trim(),
        role: s.role || null,
        gate: s.gate,
        ...(s.gate === "tests-pass"
          ? { gate_config: { command: s.gate_config?.command || "echo no-command" } }
          : {}),
        ...(s.read_only ? { read_only: true } : {}),
        ...(s.max_retries ? { max_retries: Number(s.max_retries) } : {}),
        ...(s.desc ? { desc: s.desc } : {}),
      }));
    if (!clean.length) { setError("至少需要一個階段"); return; }
    try {
      await post("/api/templates", { name: builder.name.trim(), stages: clean });
      setBuilder(null);
      reloadTemplates();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const unassigned = projects.filter((p) => !p.default_template_id);
  const assigned = projects.filter((p) => p.default_template_id);

  return (
    <div className="page">
      {error && <p className="error">{error}</p>}

      <Section title="工作流範本庫（內建，點選展開流程）">
        <div className="wf-cards">
          {(catalog?.presets ?? []).map((p) => (
            <button key={p.id} className={`wf-card ${openId === p.id ? "active" : ""}`}
                    onClick={() => setOpenId(openId === p.id ? null : p.id)}>
              <b>{p.name}</b>
              <span className="card-meta">{p.stages.length} 個階段</span>
              <span className="card-meta">{p.description}</span>
            </button>
          ))}
        </div>
        {(catalog?.presets ?? []).filter((p) => p.id === openId).map((p) => (
          <div key={p.id} className="wf-detail">
            <Flow stages={p.stages} roleLabel={roleLabel} gateInfo={gateInfo} />
            <StageTable stages={p.stages} roleLabel={roleLabel} gateInfo={gateInfo} />
            {props.canOperate && (
              <div className="inline-form">
                <button onClick={() => copyToMine(p.name, p.stages)}>
                  複製為我的範本並編輯</button>
                <AssignPicker projects={projects} label="直接指派到專案"
                              onPick={async (projectId) => {
                                // presets live in code; materialize before assigning
                                await post("/api/templates",
                                           { name: p.name, stages: p.stages });
                                await assign(projectId, p.name);
                              }} />
              </div>
            )}
          </div>
        ))}
      </Section>

      <Section title="我的範本">
        {!templates.length && <p className="muted">還沒有自己的範本 —
          從上面的範本庫複製一份開始，或用下方的編輯器從零建立。</p>}
        {templates.map((t) => {
          const stages = parseStages(t.stages_json);
          const open = openId === `mine:${t.id}`;
          return (
            <div key={t.id} className="wf-mine">
              <div className="wf-mine-head">
                <button className="ghost" onClick={() =>
                  setOpenId(open ? null : `mine:${t.id}`)}>{open ? "▾" : "▸"} {t.id}</button>
                <span className="card-meta">v{t.version} · {stages.length} 階段
                  {t.assigned_projects.length
                    ? ` · 已指派：${t.assigned_projects.join(", ")}`
                    : " · 尚未指派"}</span>
                {props.canOperate && (
                  <span className="row-ops">
                    <button className="ghost" onClick={() => copyToMine(t.id, stages)}>
                      編輯/另存</button>
                    <AssignPicker projects={projects} label="指派"
                                  onPick={(pid) => assign(pid, t.id)} />
                    <button className="ghost danger-text" onClick={async () => {
                      setError("");
                      try { await del(`/api/templates/${t.id}`); refreshAll(); }
                      catch (e) { setError(String((e as Error).message)); }
                    }}>刪除</button>
                  </span>
                )}
              </div>
              {open && (
                <>
                  <Flow stages={stages} roleLabel={roleLabel} gateInfo={gateInfo} />
                  <StageTable stages={stages} roleLabel={roleLabel} gateInfo={gateInfo} />
                </>
              )}
            </div>
          );
        })}
      </Section>

      <RolePromptsSection canOperate={props.canOperate} />

      <Section title="專案 ↔ 工作流對應">
        <h3>等待指派（{unassigned.length}）</h3>
        {!unassigned.length && <p className="muted">所有專案都已指派工作流 ✅</p>}
        {unassigned.map((p) => (
          <div key={p.id} className="wf-assign-row">
            <span>📁 <b>{p.id}</b> <span className="card-meta">{p.team_id}</span></span>
            {props.canOperate && (
              <TemplatePicker templates={templates} label="選擇工作流"
                              onPick={(tid) => assign(p.id, tid)} />
            )}
          </div>
        ))}
        <h3>已指派（{assigned.length}）</h3>
        {!assigned.length && <p className="muted">尚無專案指派工作流。</p>}
        {assigned.map((p) => (
          <div key={p.id} className="wf-assign-row">
            <span>📁 <b>{p.id}</b> → <b>{p.default_template_id}</b></span>
            {props.canOperate && (
              <span className="row-ops">
                <TemplatePicker templates={templates} label="改為"
                                onPick={(tid) => assign(p.id, tid)} />
                <button className="ghost" onClick={() => assign(p.id, null)}>取消指派</button>
              </span>
            )}
          </div>
        ))}
        <p className="muted">指派後，該專案的派工若未指定範本就會自動走這條工作流。</p>
      </Section>

      {props.canOperate && (
        <Section title="範本編輯器"
                 action={!builder ? (
                   <button onClick={() => setBuilder({ name: "", stages: [{ ...BLANK_STAGE }] })}>
                     ＋ 從零建立</button>
                 ) : undefined}>
          {!builder && <p className="muted">按右上角建立，或從範本庫/我的範本複製一份來編輯。</p>}
          {builder && catalog && (
            <Builder builder={builder} catalog={catalog} setBuilder={setBuilder}
                     onSave={saveBuilder} onCancel={() => setBuilder(null)}
                     roleLabel={roleLabel} gateInfo={gateInfo} />
          )}
        </Section>
      )}
    </div>
  );
}

/** Horizontal flow diagram: one box per stage, arrows between. */
function Flow({ stages, roleLabel, gateInfo }: {
  stages: Stage[]; roleLabel: (id?: string | null) => string;
  gateInfo: (id: string) => Gate;
}) {
  return (
    <div className="flow">
      {stages.map((s, i) => (
        <div key={i} className="flow-item">
          <div className={`flow-node gate-${s.gate}`}>
            <span className="flow-idx">{i + 1}</span>
            <b>{s.name || "（未命名）"}</b>
            <span className="flow-role">👤 {roleLabel(s.role)}</span>
            <span className="flow-gate">{gateInfo(s.gate).icon} {gateInfo(s.gate).label}</span>
            {s.read_only && <span className="flow-tag">唯讀</span>}
          </div>
          {i < stages.length - 1 && <span className="flow-arrow">→</span>}
        </div>
      ))}
    </div>
  );
}

function StageTable({ stages, roleLabel, gateInfo }: {
  stages: Stage[]; roleLabel: (id?: string | null) => string;
  gateInfo: (id: string) => Gate;
}) {
  return (
    <div className="scroll-x">
      <table>
        <thead><tr><th>#</th><th>階段</th><th>負責角色</th><th>關卡</th>
          <th>說明</th><th>重試</th></tr></thead>
        <tbody>
          {stages.map((s, i) => (
            <tr key={i}>
              <td>{i + 1}</td>
              <td>{s.name}{s.read_only ? " 🔒" : ""}</td>
              <td>{roleLabel(s.role)}</td>
              <td>{gateInfo(s.gate).icon} {gateInfo(s.gate).label}
                {s.gate === "tests-pass" && s.gate_config?.command
                  ? <code className="detail"> {s.gate_config.command}</code> : null}</td>
              <td className="card-meta">{s.desc ?? ""}</td>
              <td>{s.max_retries ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AssignPicker({ projects, label, onPick }: {
  projects: Project[]; label: string; onPick: (projectId: string) => void;
}) {
  const [value, setValue] = useState("");
  return (
    <span className="row-ops">
      <select value={value} onChange={(e) => setValue(e.target.value)}>
        <option value="">{label}…</option>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
      </select>
      <button className="ghost" disabled={!value}
              onClick={() => { onPick(value); setValue(""); }}>指派</button>
    </span>
  );
}

function TemplatePicker({ templates, label, onPick }: {
  templates: Template[]; label: string; onPick: (templateId: string) => void;
}) {
  const [value, setValue] = useState("");
  return (
    <span className="row-ops">
      <select value={value} onChange={(e) => setValue(e.target.value)}>
        <option value="">{label}…</option>
        {templates.map((t) => <option key={t.id} value={t.id}>{t.id}</option>)}
      </select>
      <button className="ghost" disabled={!value}
              onClick={() => { onPick(value); setValue(""); }}>套用</button>
    </span>
  );
}

/** Form-based stage builder: no JSON, live diagram preview. */
function Builder({ builder, catalog, setBuilder, onSave, onCancel, roleLabel, gateInfo }: {
  builder: { name: string; stages: Stage[] };
  catalog: Catalog;
  setBuilder: (b: { name: string; stages: Stage[] }) => void;
  onSave: () => void; onCancel: () => void;
  roleLabel: (id?: string | null) => string; gateInfo: (id: string) => Gate;
}) {
  const update = (i: number, patch: Partial<Stage>) => {
    const stages = builder.stages.map((s, idx) => (idx === i ? { ...s, ...patch } : s));
    setBuilder({ ...builder, stages });
  };
  const move = (i: number, delta: number) => {
    const stages = [...builder.stages];
    const j = i + delta;
    if (j < 0 || j >= stages.length) return;
    [stages[i], stages[j]] = [stages[j], stages[i]];
    setBuilder({ ...builder, stages });
  };
  const remove = (i: number) =>
    setBuilder({ ...builder, stages: builder.stages.filter((_, idx) => idx !== i) });

  return (
    <>
      <div className="inline-form">
        <input placeholder="範本名稱（例：我的網站開發流程）" style={{ width: "22rem" }}
               value={builder.name}
               onChange={(e) => setBuilder({ ...builder, name: e.target.value })} />
        <button onClick={onSave} disabled={!builder.name.trim()}>儲存範本</button>
        <button className="ghost" onClick={onCancel}>取消</button>
      </div>

      <Flow stages={builder.stages} roleLabel={roleLabel} gateInfo={gateInfo} />

      {builder.stages.map((s, i) => (
        <div key={i} className="stage-editor">
          <div className="stage-editor-head">
            <b>階段 {i + 1}</b>
            <span className="row-ops">
              <button className="ghost" onClick={() => move(i, -1)}>↑</button>
              <button className="ghost" onClick={() => move(i, 1)}>↓</button>
              <button className="ghost danger-text" onClick={() => remove(i)}>移除</button>
            </span>
          </div>
          <div className="inline-form">
            <input placeholder="階段名稱（例：後端實作）" value={s.name}
                   onChange={(e) => update(i, { name: e.target.value })} />
            <select value={s.role ?? ""}
                    onChange={(e) => update(i, { role: e.target.value || null })}>
              <option value="">不指定角色（用專案預設 agent）</option>
              {catalog.roles.map((r) => (
                <option key={r.id} value={r.id}>{r.label}</option>
              ))}
            </select>
            <select value={s.gate}
                    onChange={(e) => update(i, { gate: e.target.value })}>
              {catalog.gates.map((g) => (
                <option key={g.id} value={g.id}>{g.icon} {g.label}</option>
              ))}
            </select>
            {s.gate === "tests-pass" && (
              <input placeholder="測試指令（例：pytest -q）" style={{ width: "16rem" }}
                     value={s.gate_config?.command ?? ""}
                     onChange={(e) => update(i, { gate_config: { command: e.target.value } })} />
            )}
            <label className="chk">
              <input type="checkbox" checked={!!s.read_only}
                     onChange={(e) => update(i, { read_only: e.target.checked })} />
              唯讀（審查用）
            </label>
            <label className="chk">重試
              <input type="number" min={0} max={3} style={{ width: "4rem" }}
                     value={s.max_retries ?? 0}
                     onChange={(e) => update(i, { max_retries: Number(e.target.value) })} />
            </label>
          </div>
          <input placeholder="這個階段要做什麼（會寫進 agent 的任務說明）"
                 style={{ width: "100%" }} value={s.desc ?? ""}
                 onChange={(e) => update(i, { desc: e.target.value })} />
          <p className="muted">{gateInfo(s.gate).hint}</p>
        </div>
      ))}

      <button className="ghost" onClick={() =>
        setBuilder({ ...builder, stages: [...builder.stages, { ...BLANK_STAGE }] })}>
        ＋ 新增階段</button>
    </>
  );
}
