import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, getToken, setToken, openEventSocket,
  Job, JobDetail, UsageRow,
} from "./api";

const STATUS_BADGE: Record<string, string> = {
  in_progress: "🔵",
  blocked: "🟠",
  done: "✅",
  cancelled: "⚪",
  open: "⚪",
};

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  useEffect(() => {
    api("/api/projects").then(() => setAuthed(true)).catch(() => setAuthed(false));
  }, []);
  if (authed === null) return <div className="center">…</div>;
  if (!authed) return <TokenGate onOk={() => setAuthed(true)} />;
  return <Workbench />;
}

function TokenGate({ onOk }: { onOk: () => void }) {
  const [value, setValue] = useState(getToken());
  const [error, setError] = useState("");
  const submit = async () => {
    setToken(value.trim());
    try {
      await api("/api/projects");
      onOk();
    } catch {
      setError("token rejected — check ~/.bastet/api_token");
    }
  };
  return (
    <div className="center">
      <div className="token-card">
        <h1>🐈 Bastet</h1>
        <p>Paste your API token (<code>~/.bastet/api_token</code>):</p>
        <input
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          autoFocus
        />
        <button onClick={submit}>Connect</button>
        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}

function Workbench() {
  const [projects, setProjects] = useState<{ id: string }[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [usage, setUsage] = useState<UsageRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [feed, setFeed] = useState<string[]>([]);

  useEffect(() => {
    api<{ id: string }[]>("/api/projects").then((rows) => {
      setProjects(rows);
      if (rows.length && !projectId) setProjectId(rows[0].id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = useCallback(() => {
    if (!projectId) return;
    api<Job[]>(`/api/jobs?project_id=${projectId}&limit=100`).then(setJobs);
    api<UsageRow[]>(`/api/usage?project_id=${projectId}`).then(setUsage);
  }, [projectId]);

  useEffect(refresh, [refresh]);

  useEffect(() => {
    if (!projectId) return;
    return openEventSocket(projectId, (event) => {
      if (event.type === "hello") return;
      setFeed((old) => [
        `${String(event.at ?? "").slice(11, 19)} ${event.type} ${event.job_id ?? ""}`,
        ...old.slice(0, 7),
      ]);
      refresh();
    });
  }, [projectId, refresh]);

  const totalCost = usage.reduce((sum, row) => sum + (row.cost_usd ?? 0), 0);

  return (
    <div className="app">
      <header>
        <h1>🐈 Bastet Agent OS</h1>
        <select value={projectId ?? ""} onChange={(e) => setProjectId(e.target.value)}>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        <span className="cost">Σ ${totalCost.toFixed(4)}</span>
        <button className="ghost" onClick={refresh}>↻</button>
      </header>
      <Board jobs={jobs} onSelect={setSelected} />
      <footer>
        {feed.map((line, i) => <div key={i} className="feed-line">{line}</div>)}
      </footer>
      {selected && (
        <JobDrawer jobId={selected} onClose={() => setSelected(null)} onChanged={refresh} />
      )}
    </div>
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
            <article key={job.id} className={`card ${job.status}`}
                     onClick={() => onSelect(job.id)}>
              <div className="card-title">{STATUS_BADGE[job.status] ?? "⚪"} {job.title}</div>
              <div className="card-meta">{job.id} · {job.template_id}</div>
            </article>
          ))}
        </section>
      ))}
    </main>
  );
}

function JobDrawer({ jobId, onClose, onChanged }:
  { jobId: string; onClose: () => void; onChanged: () => void }) {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [comment, setComment] = useState("");
  const load = useCallback(() => { api<JobDetail>(`/api/jobs/${jobId}`).then(setJob); }, [jobId]);
  useEffect(load, [load]);

  if (!job) return null;
  const lastGate = job.gates[job.gates.length - 1];
  const waitingApproval = job.status === "blocked" && lastGate?.verdict === "pending";

  const decide = async (approved: boolean) => {
    await api(`/api/jobs/${jobId}/approve`, {
      method: "POST",
      body: JSON.stringify({ approved, comment }),
    });
    onChanged();
    load();
  };

  return (
    <aside className="drawer">
      <button className="ghost close" onClick={onClose}>✕</button>
      <h2>{job.title}</h2>
      <p className="card-meta">{job.id} · stage <b>{job.stage}</b> · {job.status}</p>
      <pre className="spec">{job.spec_md}</pre>

      {waitingApproval && (
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
    </aside>
  );
}
