import { useEffect, useState } from "react";
import { api, del, post } from "../api";
import RolePromptsSection from "./RolePrompts";
import { useT, useVocab, type T } from "../i18n";
import { Section, useList } from "../ui";

type Stage = {
  name: string; role?: string | null; gate: string;
  gate_config?: { command?: string; precheck_command?: string; metric?: string;
                  operator?: string; threshold?: number; unit?: string };
  read_only?: boolean;
  requires?: string[];
  needs?: string[]; produces?: string[]; consumes?: string[]; evidence?: string[];
  challenge?: boolean; max_challenge_exchanges?: number;
  workspace?: "shared" | "isolated";
  delivery_modes?: ("branch" | "integration" | "production")[];
  max_retries?: number; desc?: string;
};
type Preset = { id: string; name: string; description: string; stages: Stage[] };
type Role = { id: string; label: string; hint: string };
type Gate = { id: string; label: string; icon: string; hint: string };
type Capability = { id: string; label: string; description: string };
type EvidenceType = { id: string; label: string };
type Catalog = { presets: Preset[]; roles: Role[]; gates: Gate[];
                 evidence_types: EvidenceType[]; capabilities: Capability[] };
type Template = { id: string; version: number; stages_json: string;
                  assigned_projects: string[] };
type Project = { id: string; team_id: string; default_template_id: string | null;
                 status?: string };

const BLANK_STAGE: Stage = { name: "", role: null, gate: "auto" };

export default function TemplatesPage(props: { canOperate: boolean; refreshKey: number }) {
  const t = useT();
  const vocab = useVocab();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [templates, reloadTemplates] = useList<Template>("/api/templates", props.refreshKey);
  const [projects, reloadProjects] = useList<Project>("/api/projects", props.refreshKey);
  const [openId, setOpenId] = useState<string | null>(null);
  const [builder, setBuilder] = useState<{ name: string; stages: Stage[];
                                           editing?: boolean } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Catalog>("/api/workflow-catalog").then(setCatalog).catch(() => {});
  }, []);

  const roleLabel = (id?: string | null) => id
    ? vocab.roleLabel(id, catalog?.roles.find((r) => r.id === id)?.label ?? id)
    : t("tpl.roleUnset");
  const gateInfo = (id: string) => {
    const g = catalog?.gates.find((x) => x.id === id);
    return { id, icon: g?.icon ?? "•", hint: g?.hint ?? "",
             label: vocab.gateLabel(id, g?.label ?? id) };
  };

  const parseStages = (json: string): Stage[] => {
    try { return JSON.parse(json) as Stage[]; } catch { return []; }
  };

  const refreshAll = () => { reloadTemplates(); reloadProjects(); };

  /** Load a template into the builder. `keepName` edits it in place (saving
   *  bumps its version); otherwise it becomes a new template. */
  const openInBuilder = (name: string, stages: Stage[], keepName = false) => {
    setBuilder({ name: keepName ? name : `${name}${t("tpl.myCopySuffix")}`,
                 stages: stages.map((s) => ({ ...s })), editing: keepName });
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
          : s.gate === "metric-threshold"
            ? { gate_config: {
                command: s.gate_config?.command || "echo '{\"value\": 0}'",
                metric: s.gate_config?.metric || "metric",
                operator: s.gate_config?.operator || ">=",
                threshold: Number(s.gate_config?.threshold ?? 0),
                ...(s.gate_config?.unit ? { unit: s.gate_config.unit } : {}),
              } }
          : s.gate_config?.precheck_command
            ? { gate_config: { precheck_command: s.gate_config.precheck_command } }
            : {}),
        ...(s.read_only ? { read_only: true } : {}),
        ...(s.requires?.length ? { requires: s.requires } : {}),
        ...(s.needs !== undefined ? { needs: s.needs } : {}),
        ...(s.produces?.length ? { produces: s.produces } : {}),
        ...(s.consumes?.length ? { consumes: s.consumes } : {}),
        ...(s.evidence?.length ? { evidence: s.evidence } : {}),
        ...(s.challenge === false ? { challenge: false } : {}),
        ...(s.max_challenge_exchanges !== undefined
          ? { max_challenge_exchanges: Number(s.max_challenge_exchanges) } : {}),
        ...(s.workspace === "isolated" ? { workspace: "isolated" } : {}),
        ...(s.delivery_modes?.length ? { delivery_modes: s.delivery_modes } : {}),
        ...(s.max_retries ? { max_retries: Number(s.max_retries) } : {}),
        ...(s.desc ? { desc: s.desc } : {}),
      }));
    if (!clean.length) { setError(t("tpl.needStage")); return; }
    try {
      await post("/api/templates", { name: builder.name.trim(), stages: clean });
      setBuilder(null);
      reloadTemplates();
    } catch (e) { setError(String((e as Error).message)); }
  };

  // A closed project has nothing left to assign a workflow to, so listing it
  // here only makes the real work harder to see. It stays on the Projects tab,
  // where closed projects belong (and can be reopened).
  const live = projects.filter((p) => p.status !== "closed");
  const unassigned = live.filter((p) => !p.default_template_id);
  const assigned = live.filter((p) => p.default_template_id);
  const closedCount = projects.length - live.length;

  return (
    <div className="page">
      {error && <p className="error">{error}</p>}

      <Section title={t("tpl.library")}>
        <div className="wf-cards">
          {(catalog?.presets ?? []).map((p) => (
            <button key={p.id} className={`wf-card ${openId === p.id ? "active" : ""}`}
                    onClick={() => setOpenId(openId === p.id ? null : p.id)}>
              <b>{p.name}</b>
              <span className="card-meta">
                {t("tpl.stageCount", { n: p.stages.length })}</span>
              <span className="card-meta">{p.description}</span>
            </button>
          ))}
        </div>
        {(catalog?.presets ?? []).filter((p) => p.id === openId).map((p) => (
          <div key={p.id} className="wf-detail">
            <Flow stages={p.stages} roleLabel={roleLabel} gateInfo={gateInfo} t={t} />
            <StageTable stages={p.stages} roleLabel={roleLabel} gateInfo={gateInfo}
                        t={t} />
            {props.canOperate && (
              <div className="inline-form">
                <button onClick={() => openInBuilder(p.name, p.stages)}>
                  {t("tpl.copyEdit")}</button>
                <AssignPicker projects={projects} label={t("tpl.assignDirect")}
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

      <Section title={t("tpl.mine")}>
        {!templates.length && <p className="muted">{t("tpl.noneMine")}</p>}
        {templates.map((tpl) => {
          const stages = parseStages(tpl.stages_json);
          const open = openId === `mine:${tpl.id}`;
          return (
            <div key={tpl.id} className="wf-mine">
              <div className="wf-mine-head">
                <button className="ghost" onClick={() =>
                  setOpenId(open ? null : `mine:${tpl.id}`)}>
                  {open ? "▾" : "▸"} {tpl.id}</button>
                <span className="card-meta">v{tpl.version} ·{" "}
                  {t("tpl.stagesShort", { n: stages.length })} ·{" "}
                  {tpl.assigned_projects.length
                    ? t("tpl.assignedTo", { list: tpl.assigned_projects.join(", ") })
                    : t("tpl.unassignedYet")}</span>
                {props.canOperate && (
                  <span className="row-ops">
                    <button className="ghost"
                            onClick={() => openInBuilder(tpl.id, stages, true)}>
                      {t("tpl.editInPlace")}</button>
                    <button className="ghost"
                            onClick={() => openInBuilder(tpl.id, stages)}>
                      {t("tpl.saveAsCopy")}</button>
                    <AssignPicker projects={projects} label={t("c.assign")}
                                  onPick={(pid) => assign(pid, tpl.id)} />
                    <button className="ghost danger-text" onClick={async () => {
                      setError("");
                      try { await del(`/api/templates/${tpl.id}`); refreshAll(); }
                      catch (e) { setError(String((e as Error).message)); }
                    }}>{t("c.delete")}</button>
                  </span>
                )}
              </div>
              {open && (
                <>
                  <Flow stages={stages} roleLabel={roleLabel} gateInfo={gateInfo}
                        t={t} />
                  <StageTable stages={stages} roleLabel={roleLabel}
                              gateInfo={gateInfo} t={t} />
                </>
              )}
            </div>
          );
        })}
      </Section>

      <RolePromptsSection canOperate={props.canOperate} />

      <Section title={t("tpl.mapping")}>
        <h3>{t("tpl.awaiting", { n: unassigned.length })}</h3>
        {!unassigned.length && <p className="muted">{t("tpl.allAssigned")}</p>}
        {unassigned.map((p) => (
          <div key={p.id} className="wf-assign-row">
            <span>📁 <b>{p.id}</b> <span className="card-meta">{p.team_id}</span></span>
            {props.canOperate && (
              <TemplatePicker templates={templates} label={t("tpl.pickWorkflow")}
                              onPick={(tid) => assign(p.id, tid)} />
            )}
          </div>
        ))}
        <h3>{t("tpl.assignedHead", { n: assigned.length })}</h3>
        {!assigned.length && <p className="muted">{t("tpl.noneAssigned")}</p>}
        {assigned.map((p) => (
          <div key={p.id} className="wf-assign-row">
            <span>📁 <b>{p.id}</b> → <b>{p.default_template_id}</b></span>
            {props.canOperate && (
              <span className="row-ops">
                <TemplatePicker templates={templates} label={t("tpl.changeTo")}
                                onPick={(tid) => assign(p.id, tid)} />
                <button className="ghost"
                        onClick={() => assign(p.id, null)}>{t("c.unassign")}</button>
              </span>
            )}
          </div>
        ))}
        {closedCount > 0 && (
          <p className="muted">{t("tpl.closedHidden", { n: closedCount })}</p>
        )}
        <p className="muted">{t("tpl.mappingHint")}</p>
        <p className="muted">{t("tpl.presetLangNote")}</p>
      </Section>

      {props.canOperate && (
        <Section title={t("tpl.editor")}
                 action={!builder ? (
                   <button onClick={() =>
                     setBuilder({ name: "", stages: [{ ...BLANK_STAGE }],
                                  editing: false })}>
                     {t("tpl.fromScratch")}</button>
                 ) : undefined}>
          {!builder && <p className="muted">{t("tpl.editorHint")}</p>}
          {builder && catalog && (
            <Builder builder={builder} catalog={catalog} setBuilder={setBuilder}
                     onSave={saveBuilder} onCancel={() => setBuilder(null)}
                     roleLabel={roleLabel} gateInfo={gateInfo} t={t} />
          )}
        </Section>
      )}
    </div>
  );
}

/** Horizontal flow diagram: one box per stage, arrows between. */
function Flow({ stages, roleLabel, gateInfo, t }: {
  stages: Stage[]; roleLabel: (id?: string | null) => string;
  gateInfo: (id: string) => Gate; t: T;
}) {
  return (
    <div className="flow">
      {stages.map((s, i) => (
        <div key={i} className="flow-item">
          <div className={`flow-node gate-${s.gate}`}>
            <span className="flow-idx">{i + 1}</span>
            <b>{s.name || t("tpl.unnamed")}</b>
            <span className="flow-role">👤 {roleLabel(s.role)}</span>
            <span className="flow-gate">{gateInfo(s.gate).icon} {gateInfo(s.gate).label}</span>
            {s.read_only && <span className="flow-tag">{t("tpl.readOnlyTag")}</span>}
            {s.needs?.map((dep) =>
              <span className="flow-tag" key={`need:${dep}`}>↳ {dep}</span>)}
            {s.workspace === "isolated" && <span className="flow-tag">⑂ isolated</span>}
            {s.requires?.map((cap) =>
              <span className="flow-tag" key={cap}>⚙ {cap}</span>)}
            {s.evidence?.map((kind) =>
              <span className="flow-tag" key={`evidence:${kind}`}>✓ {kind}</span>)}
            {s.delivery_modes?.map((mode) =>
              <span className="flow-tag" key={`delivery:${mode}`}>🚚 {mode}</span>)}
          </div>
          {i < stages.length - 1 && <span className="flow-arrow">→</span>}
        </div>
      ))}
    </div>
  );
}

function StageTable({ stages, roleLabel, gateInfo, t }: {
  stages: Stage[]; roleLabel: (id?: string | null) => string;
  gateInfo: (id: string) => Gate; t: T;
}) {
  return (
    <div className="scroll-x">
      <table>
        <thead><tr><th>#</th><th>{t("c.stage")}</th><th>{t("tpl.headOwner")}</th>
          <th>{t("tpl.headGate")}</th><th>{t("tpl.headDesc")}</th>
          <th>{t("tpl.headRetry")}</th></tr></thead>
        <tbody>
          {stages.map((s, i) => (
            <tr key={i}>
              <td>{i + 1}</td>
              <td>{s.name}{s.read_only ? " 🔒" : ""}</td>
              <td>{roleLabel(s.role)}</td>
              <td>{gateInfo(s.gate).icon} {gateInfo(s.gate).label}
                {s.gate === "tests-pass" && s.gate_config?.command
                  ? <code className="detail"> {s.gate_config.command}</code> : null}
                {s.gate === "metric-threshold" && s.gate_config
                  ? <code className="detail"> {s.gate_config.metric || "metric"} {s.gate_config.operator} {s.gate_config.threshold}{s.gate_config.unit || ""}</code>
                  : null}</td>
              <td className="card-meta">
                {s.needs?.length ? `needs: ${s.needs.join(", ")} · ` : ""}
                {s.evidence?.length ? `evidence: ${s.evidence.join(", ")} · ` : ""}
                {s.delivery_modes?.length
                  ? `delivery: ${s.delivery_modes.join(" | ")} · ` : ""}
                {s.desc ?? ""}
              </td>
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
  const t = useT();
  const [value, setValue] = useState("");
  return (
    <span className="row-ops">
      <select value={value} onChange={(e) => setValue(e.target.value)}>
        <option value="">{label}…</option>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
      </select>
      <button className="ghost" disabled={!value}
              onClick={() => { onPick(value); setValue(""); }}>{t("c.assign")}</button>
    </span>
  );
}

function TemplatePicker({ templates, label, onPick }: {
  templates: Template[]; label: string; onPick: (templateId: string) => void;
}) {
  const t = useT();
  const [value, setValue] = useState("");
  return (
    <span className="row-ops">
      <select value={value} onChange={(e) => setValue(e.target.value)}>
        <option value="">{label}…</option>
        {templates.map((x) => <option key={x.id} value={x.id}>{x.id}</option>)}
      </select>
      <button className="ghost" disabled={!value}
              onClick={() => { onPick(value); setValue(""); }}>{t("c.apply")}</button>
    </span>
  );
}

/** Form-based stage builder: no JSON, live diagram preview. */
function Builder({ builder, catalog, setBuilder, onSave, onCancel, roleLabel,
                  gateInfo, t }: {
  builder: { name: string; stages: Stage[]; editing?: boolean };
  catalog: Catalog;
  setBuilder: (b: { name: string; stages: Stage[]; editing?: boolean }) => void;
  onSave: () => void; onCancel: () => void;
  roleLabel: (id?: string | null) => string; gateInfo: (id: string) => Gate;
  t: T;
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
      {builder.editing && <p className="notice">{t("tpl.editingInPlace",
                                                   { name: builder.name })}</p>}
      <div className="inline-form">
        <input placeholder={t("tpl.namePh")} style={{ width: "22rem" }}
               value={builder.name}
               onChange={(e) => setBuilder({ ...builder, name: e.target.value })} />
        <button onClick={onSave}
                disabled={!builder.name.trim()}>{t("tpl.saveTemplate")}</button>
        <button className="ghost" onClick={onCancel}>{t("c.cancel")}</button>
      </div>

      <Flow stages={builder.stages} roleLabel={roleLabel} gateInfo={gateInfo}
            t={t} />

      {builder.stages.map((s, i) => (
        <div key={i} className="stage-editor">
          <div className="stage-editor-head">
            <b>{t("tpl.stageN", { n: i + 1 })}</b>
            <span className="row-ops">
              <button className="ghost" onClick={() => move(i, -1)}>↑</button>
              <button className="ghost" onClick={() => move(i, 1)}>↓</button>
              <button className="ghost danger-text"
                      onClick={() => remove(i)}>{t("c.remove")}</button>
            </span>
          </div>
          <div className="inline-form">
            <input placeholder={t("tpl.stageNamePh")} value={s.name}
                   onChange={(e) => update(i, { name: e.target.value })} />
            <select value={s.role ?? ""}
                    onChange={(e) => update(i, { role: e.target.value || null })}>
              <option value="">{t("tpl.roleNone")}</option>
              {catalog.roles.map((r) => (
                <option key={r.id} value={r.id}>{roleLabel(r.id)}</option>
              ))}
            </select>
            <select value={s.gate}
                    onChange={(e) => update(i, { gate: e.target.value })}>
              {catalog.gates.map((g) => (
                <option key={g.id} value={g.id}>
                  {gateInfo(g.id).icon} {gateInfo(g.id).label}</option>
              ))}
            </select>
            {s.gate === "tests-pass" && (
              <input placeholder={t("tpl.testCmdPh")} style={{ width: "16rem" }}
                     value={s.gate_config?.command ?? ""}
                     onChange={(e) => update(i, { gate_config: { command: e.target.value } })} />
            )}
            {s.gate === "metric-threshold" && (
              <>
                <input placeholder={t("tpl.metricCmdPh")} style={{ width: "18rem" }}
                       value={s.gate_config?.command ?? ""}
                       onChange={(e) => update(i, { gate_config: {
                         ...s.gate_config, command: e.target.value,
                       } })} />
                <input placeholder={t("tpl.metricNamePh")} style={{ width: "8rem" }}
                       value={s.gate_config?.metric ?? ""}
                       onChange={(e) => update(i, { gate_config: {
                         ...s.gate_config, metric: e.target.value,
                       } })} />
                <select value={s.gate_config?.operator ?? ">="}
                        onChange={(e) => update(i, { gate_config: {
                          ...s.gate_config, operator: e.target.value,
                        } })}>
                  {[">=", ">", "<=", "<", "=="].map((op) =>
                    <option key={op} value={op}>{op}</option>)}
                </select>
                <input type="number" step="any" placeholder={t("tpl.metricThresholdPh")} style={{ width: "8rem" }}
                       value={s.gate_config?.threshold ?? 0}
                       onChange={(e) => update(i, { gate_config: {
                         ...s.gate_config, threshold: Number(e.target.value),
                       } })} />
                <input placeholder={t("tpl.metricUnitPh")} style={{ width: "5rem" }}
                       value={s.gate_config?.unit ?? ""}
                       onChange={(e) => update(i, { gate_config: {
                         ...s.gate_config, unit: e.target.value,
                       } })} />
              </>
            )}
            {s.gate === "agent-review" && s.requires?.length ? (
              <input placeholder="Bastet host precheck command" style={{ width: "18rem" }}
                     value={s.gate_config?.precheck_command ?? ""}
                     onChange={(e) => update(i, { gate_config: {
                       ...s.gate_config, precheck_command: e.target.value,
                     } })} />
            ) : null}
            <label className="chk">
              <input type="checkbox" checked={!!s.read_only}
                     onChange={(e) => update(i, { read_only: e.target.checked })} />
              {t("tpl.readOnly")}
            </label>
            {catalog.capabilities.map((cap) => (
              <label className="chk" key={cap.id} title={cap.description}>
                <input type="checkbox" checked={!!s.requires?.includes(cap.id)}
                       onChange={(e) => update(i, {
                         requires: e.target.checked
                           ? [...(s.requires ?? []), cap.id]
                           : (s.requires ?? []).filter((x) => x !== cap.id),
                       })} />
                ⚙ {cap.label}
              </label>
            ))}
            <label className="chk">{t("tpl.retries")}
              <input type="number" min={0} max={3} style={{ width: "4rem" }}
                     value={s.max_retries ?? 0}
                     onChange={(e) => update(i, { max_retries: Number(e.target.value) })} />
            </label>
          </div>
          <input placeholder={t("tpl.stageDescPh")}
                 style={{ width: "100%" }} value={s.desc ?? ""}
                 onChange={(e) => update(i, { desc: e.target.value })} />
          <div className="inline-form">
            <input placeholder="needs: stage-a, stage-b" value={(s.needs ?? []).join(", ")}
                   onChange={(e) => update(i, { needs: e.target.value.split(",")
                     .map((x) => x.trim()).filter(Boolean) })} />
            <input placeholder="produces: artifact ids" value={(s.produces ?? []).join(", ")}
                   onChange={(e) => update(i, { produces: e.target.value.split(",")
                     .map((x) => x.trim()).filter(Boolean) })} />
            <input placeholder="consumes: artifact ids" value={(s.consumes ?? []).join(", ")}
                   onChange={(e) => update(i, { consumes: e.target.value.split(",")
                     .map((x) => x.trim()).filter(Boolean) })} />
            <input placeholder="evidence: functional, security, git"
                   value={(s.evidence ?? []).join(", ")}
                   onChange={(e) => update(i, { evidence: e.target.value.split(",")
                     .map((x) => x.trim()).filter(Boolean) })} />
            <input placeholder="delivery: integration, production (sink only)"
                   value={(s.delivery_modes ?? []).join(", ")}
                   onChange={(e) => update(i, { delivery_modes: e.target.value.split(",")
                     .map((x) => x.trim()).filter(Boolean) as Stage["delivery_modes"] })} />
            <select value={s.workspace ?? "shared"}
                    onChange={(e) => update(i, {
                      workspace: e.target.value as "shared" | "isolated",
                    })}>
              <option value="shared">shared workspace</option>
              <option value="isolated">isolated workspace</option>
            </select>
            <label className="chk">
              <input type="checkbox" checked={s.challenge !== false}
                     onChange={(e) => update(i, { challenge: e.target.checked })} />
              handoff challenge
            </label>
            {s.challenge !== false && (
              <label className="chk">max exchanges
                <input type="number" min={0} max={5} style={{ width: "4rem" }}
                       value={s.max_challenge_exchanges ?? 5}
                       onChange={(e) => update(i, {
                         max_challenge_exchanges: Number(e.target.value),
                       })} />
              </label>
            )}
          </div>
          <p className="muted">{gateInfo(s.gate).hint}</p>
        </div>
      ))}

      <button className="ghost" onClick={() =>
        setBuilder({ ...builder, stages: [...builder.stages, { ...BLANK_STAGE }] })}>
        {t("tpl.addStage")}</button>
    </>
  );
}
