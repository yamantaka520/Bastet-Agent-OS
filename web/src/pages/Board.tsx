import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, post, Interaction, Job, JobDetail, UsageRow,
} from "../api";

const STATUS_BADGE: Record<string, string> = {
  in_progress: "🔵", blocked: "🟠", done: "✅", cancelled: "⚪", open: "⚪",
};

export default function BoardPage(props: { projectId: string; refreshKey: number;
                                           canOperate: boolean }) {
  const { projectId, refreshKey } = props;
  const [jobs, setJobs] = useState<Job[]>([]);
  const [usage, setUsage] = useState<UsageRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [showDispatch, setShowDispatch] = useState(false);

  const refresh = useCallback(() => {
    api<Job[]>(`/api/jobs?project_id=${projectId}&limit=100`).then(setJobs);
    api<UsageRow[]>(`/api/usage?project_id=${projectId}`).then(setUsage);
  }, [projectId]);
  useEffect(refresh, [refresh, refreshKey]);

  const totalCost = usage.reduce((sum, row) => sum + (row.cost_usd ?? 0), 0);

  return (
    <>
      <div className="toolbar">
        <span className="cost">Σ ${totalCost.toFixed(4)}</span>
        {props.canOperate && (
          <button onClick={() => setShowDispatch(true)}>＋ dispatch</button>
        )}
      </div>
      <Board jobs={jobs} onSelect={setSelected} />
      {selected && (
        <JobDrawer jobId={selected} canOperate={props.canOperate}
                   onClose={() => setSelected(null)} onChanged={refresh} />
      )}
      {showDispatch && (
        <DispatchModal projectId={projectId}
                       onClose={() => { setShowDispatch(false); refresh(); }} />
      )}
    </>
  );
}

function Board({ jobs, onSelect }: { jobs: Job[]; onSelect: (id: string) => void }) {
  const columns = useMemo(() => {
    const names: string[] = [];
    for (const job of jobs) {
      try {
        for (const stage of JSON.parse(job.stages_snapshot_json) as { name: string }[]) {
          if (!names.includes(stage.name)) names.push(stage.name);
        }
      } catch { /* skip bad snapshots */ }
    }
    return [...names, "done"];
  }, [jobs]);

  const inColumn = (col: string) =>
    col === "done"
      ? jobs.filter((j) => j.status === "done")
      : jobs.filter((j) => j.stage === col && j.status !== "done");

  return (
    <main className="board">
      {columns.map((col) => (
        <section key={col} className="column">
          <h2>{col} <span className="count">{inColumn(col).length}</span></h2>
          {inColumn(col).map((job) => (
            <button key={job.id} className={`card ${job.status}`}
                    onClick={() => onSelect(job.id)}>
              <div className="card-title">{STATUS_BADGE[job.status] ?? "⚪"} {job.title}</div>
              <div className="card-meta">{job.id} · {job.template_id}</div>
            </button>
          ))}
        </section>
      ))}
    </main>
  );
}

function DispatchModal({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const [agents, setAgents] = useState<{ id: string }[]>([]);
  const [templates, setTemplates] = useState<{ id: string }[]>([]);
  const [resources, setResources] = useState<{ id: string; name: string; kind: string }[]>([]);
  const [form, setForm] = useState({ prompt: "", title: "", agent_id: "",
                                     template_id: "", resource_id: "" });
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ id: string }[]>("/api/agents").then((a) => {
      setAgents(a);
      if (a.length) setForm((f) => ({ ...f, agent_id: a[0].id }));
    });
    api<{ id: string }[]>("/api/templates").then(setTemplates);
    api<{ id: string; name: string; kind: string }[]>("/api/resources")
      .then((r) => setResources(r.filter((x) => x.kind === "llm")))
      .catch(() => setResources([]));  // non-admin cannot list resources
  }, []);

  const go = async () => {
    try {
      await post("/api/dispatch", {
        project_id: projectId, prompt: form.prompt, title: form.title,
        agent_id: form.agent_id, template_id: form.template_id || null,
        resource_id: form.resource_id || null,
      });
      onClose();
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Dispatch a job</h2>
        <input placeholder="title" value={form.title}
               onChange={(e) => setForm({ ...form, title: e.target.value })} />
        <textarea placeholder="task prompt / spec" rows={5} value={form.prompt}
                  onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
        <div className="row">
          <select value={form.agent_id}
                  onChange={(e) => setForm({ ...form, agent_id: e.target.value })}>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.id}</option>)}
          </select>
          <select value={form.template_id}
                  onChange={(e) => setForm({ ...form, template_id: e.target.value })}>
            <option value="">single-stage</option>
            {templates.map((t) => <option key={t.id} value={t.id}>{t.id}</option>)}
          </select>
          <select value={form.resource_id}
                  onChange={(e) => setForm({ ...form, resource_id: e.target.value })}>
            <option value="">direct (subscription)</option>
            {resources.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </div>
        <div className="row">
          <button onClick={go} disabled={!form.prompt || !form.agent_id}>Dispatch</button>
          <button className="ghost" onClick={onClose}>Cancel</button>
        </div>
        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}

function JobDrawer({ jobId, canOperate, onClose, onChanged }:
  { jobId: string; canOperate: boolean; onClose: () => void; onChanged: () => void }) {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [comment, setComment] = useState("");
  const [interactions, setInteractions] = useState<Record<string, Interaction[]>>({});
  const [diff, setDiff] = useState<string | null>(null);

  const load = useCallback(() => {
    api<JobDetail>(`/api/jobs/${jobId}`).then(async (j) => {
      setJob(j);
      const map: Record<string, Interaction[]> = {};
      for (const run of j.runs) {
        if (run.status === "waiting_input") {
          map[run.id] = await api<Interaction[]>(`/api/runs/${run.id}/interactions`);
        }
      }
      setInteractions(map);
      const last = j.runs[j.runs.length - 1];
      if (last) {
        api<{ diff: string | null }>(`/api/runs/${last.id}/diff`)
          .then((d) => setDiff(d.diff)).catch(() => setDiff(null));
      }
    });
  }, [jobId]);
  useEffect(load, [load]);

  if (!job) return null;
  const lastGate = job.gates[job.gates.length - 1];
  const waitingApproval = job.status === "blocked" && lastGate?.verdict === "pending";

  const decide = async (approved: boolean) => {
    await post(`/api/jobs/${jobId}/approve`, { approved, comment });
    onChanged();
    load();
  };

  const answer = async (runId: string, requestId: string, allow: boolean) => {
    await post(`/api/runs/${runId}/respond`,
               { request_id: requestId, reply: { behavior: allow ? "allow" : "deny" } });
    onChanged();
    load();
  };

  return (
    <aside className="drawer">
      <button className="ghost close" onClick={onClose}>✕</button>
      <h2>{job.title}</h2>
      <p className="card-meta">{job.id} · stage <b>{job.stage}</b> · {job.status}</p>
      <pre className="spec">{job.spec_md}</pre>

      {canOperate && waitingApproval && (
        <div className="approval">
          <h3>⏸ waiting for your approval</h3>
          <input placeholder="comment (optional)" value={comment}
                 onChange={(e) => setComment(e.target.value)} />
          <div>
            <button onClick={() => decide(true)}>Approve</button>
            <button className="danger" onClick={() => decide(false)}>Reject</button>
          </div>
        </div>
      )}

      {canOperate && Object.entries(interactions).map(([runId, items]) =>
        items.filter((i) => i.status === "pending").map((i) => (
          <div className="approval" key={i.request_id}>
            <h3>✋ {i.kind} — run {runId}</h3>
            <pre className="spec">{i.payload_json}</pre>
            <div>
              <button onClick={() => answer(runId, i.request_id, true)}>Allow</button>
              <button className="danger"
                      onClick={() => answer(runId, i.request_id, false)}>Deny</button>
            </div>
          </div>
        )))}

      <h3>Runs</h3>
      <table>
        <thead><tr><th>stage</th><th>#</th><th>agent</th><th>status</th><th>cost</th></tr></thead>
        <tbody>
          {job.runs.map((r) => (
            <tr key={r.id}>
              <td>{r.stage}</td><td>{r.attempt}</td><td>{r.agent_id}</td>
              <td>{r.status}</td>
              <td>${r.cost_usd.toFixed(4)} <small>{r.accounting_precision ?? ""}</small></td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Gates</h3>
      <ul className="gates">
        {job.gates.map((g, i) => (
          <li key={i}>
            <b>{g.gate_type}</b> → {g.verdict}
            <span className="card-meta"> by {g.reviewer_kind}:{g.reviewer_id}</span>
            {g.detail_md && <div className="gate-detail">{g.detail_md}</div>}
          </li>
        ))}
      </ul>

      {diff && (
        <>
          <h3>Diff</h3>
          <pre className="spec diff">{diff}</pre>
        </>
      )}
    </aside>
  );
}
