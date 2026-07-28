import { useState } from "react";
import { post } from "../api";
import { DataTable, Section, useList } from "../ui";

type Template = { id: string; version: number; stages_json: string };

const EXAMPLE = JSON.stringify([
  { name: "plan", gate: "human-approve" },
  { name: "implement", gate: "tests-pass", gate_config: { command: "pytest -q" } },
  { name: "review", role: "reviewer", gate: "agent-review", read_only: true },
  { name: "merge", gate: "human-approve" },
], null, 2);

export default function TemplatesPage(props: { canOperate: boolean; refreshKey: number }) {
  const [templates, reload] = useList<Template>("/api/templates");
  const [name, setName] = useState("");
  const [stages, setStages] = useState(EXAMPLE);
  const [error, setError] = useState("");

  const save = async () => {
    setError("");
    try {
      await post("/api/templates", { name, stages: JSON.parse(stages) });
      setName("");
      reload();
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  return (
    <div className="page">
      <Section title="Workflow templates">
        <DataTable head={["id", "version", "stages"]}
                   rows={templates.map((t) => {
                     let names = "?";
                     try {
                       names = (JSON.parse(t.stages_json) as { name: string; gate: string }[])
                         .map((s) => `${s.name}(${s.gate})`).join(" → ");
                     } catch { /* leave ? */ }
                     return [t.id, t.version, names];
                   })} />
      </Section>
      {props.canOperate && (
        <Section title="Add / replace template">
          <input placeholder="template name" value={name}
                 onChange={(e) => setName(e.target.value)} />
          <textarea rows={12} value={stages} spellCheck={false}
                    onChange={(e) => setStages(e.target.value)} />
          <div className="row">
            <button onClick={save} disabled={!name}>Save</button>
            {error && <span className="error">{error}</span>}
          </div>
        </Section>
      )}
    </div>
  );
}
