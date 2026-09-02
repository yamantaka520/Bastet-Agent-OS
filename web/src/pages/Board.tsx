import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, apiBlob, post, put, BranchReview, Interaction, Job, JobDetail, RepoBrowse,
  UsageRow,
} from "../api";
import { useT, type T } from "../i18n";
import { fmtAgo, fmtTime } from "../ui";

const STATUS_BADGE: Record<string, string> = {
  in_progress: "🔵", blocked: "🟠", done: "✅", cancelled: "⚪", open: "⚪",
};

export default function BoardPage(props: { projectId: string; refreshKey: number;
                                           canOperate: boolean }) {
  const t = useT();
  const { projectId, refreshKey } = props;
  const [jobs, setJobs] = useState<Job[]>([]);
  const [usage, setUsage] = useState<UsageRow[]>([]);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [showDispatch, setShowDispatch] = useState(false);

  const refresh = useCallback(() => {
    api<Job[]>(`/api/jobs?project_id=${projectId}&limit=100&include_archived=${includeArchived}`).then(setJobs);
    api<UsageRow[]>(`/api/usage?project_id=${projectId}`).then(setUsage);
  }, [projectId, includeArchived]);
  useEffect(refresh, [refresh, refreshKey]);

  const totalCost = usage.reduce((sum, row) => sum + (row.cost_usd ?? 0), 0);

  return (
    <>
      <div className="toolbar">
        <label className="row"><input type="checkbox" checked={includeArchived}
          onChange={(e) => setIncludeArchived(e.target.checked)} />
          {t("board.showArchived")}</label>
        <span className="cost">Σ ${totalCost.toFixed(4)}</span>
        {props.canOperate && (
          <button onClick={() => setShowDispatch(true)}>{t("board.dispatch")}</button>
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
  const t = useT();
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
          <h2>{col === "done" ? t("board.colDone") : col}
            <span className="count">{inColumn(col).length}</span></h2>
          {inColumn(col).map((job) => (
            <button key={job.id} className={`card ${job.status}`}
                    onClick={() => onSelect(job.id)}>
              {/* the title is what identifies work to a human; the id is
                  plumbing, so it goes underneath */}
              <div className="card-title">{job.archived ? "📦 " : ""}{STATUS_BADGE[job.status] ?? "⚪"} {job.title}</div>
              <div className="card-meta">{t(`board.status.${job.status}`,
                                            undefined, job.status)}
                {job.template_id ? ` · ${job.template_id}` : ""}
                {job.rework_count ? ` · 🔧 ${t("board.reworked",
                                               { n: job.rework_count })}` : ""}
                {job.delivery_status && job.delivery_status !== "not_required"
                  ? ` · 🚚 ${job.delivery_status}` : ""}</div>
              <StageProgress job={job} />
              {job.status === "in_progress" && <Heartbeat job={job} t={t} />}
              <div className="card-meta card-id">{job.id}</div>
            </button>
          ))}
        </section>
      ))}
    </main>
  );
}

/** Where the card is in its pipeline: n of m stages done. Derived from the
 *  snapshot, so it is honest about pipelines of different lengths. */
function StageProgress({ job }: { job: Job }) {
  let stages: { name: string }[] = [];
  try { stages = JSON.parse(job.stages_snapshot_json || "[]"); } catch { /* keep [] */ }
  if (stages.length < 2) return null;
  const nodes = job.stage_nodes ?? [];
  const index = Math.max(0, stages.findIndex((s) => s.name === job.stage));
  const done = job.status === "done" ? stages.length
    : nodes.length ? nodes.filter((node) => node.status === "passed").length : index;
  const active = nodes.filter((node) => node.status === "running")
    .map((node) => node.stage);
  return (
    <div className="stage-progress"
         title={`${active.join(", ") || job.stage} (${done}/${stages.length})`}>
      <div className="stage-progress-fill"
           style={{ width: `${(done / stages.length) * 100}%` }} />
    </div>
  );
}

/** Liveness: what the run last said and how long ago. This is the difference
 *  between "working" and "stuck" — updated_at cannot tell you, because a long
 *  stage legitimately goes minutes between DB writes. Re-renders on a timer so
 *  the "ago" counts up even without new events. */
function Heartbeat({ job, t }: { job: Job; t: T }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), 5000);
    return () => window.clearInterval(timer);
  }, []);
  const at = job.heartbeat_at;
  if (!at) return <div className="card-meta pulse">⏳ {t("board.starting")}</div>;
  const ago = (s: string) =>
    Math.round((Date.now() - new Date(s.match(/[+Z]/i) ? s : s + "Z").getTime()) / 1000);
  // alive and talking are different questions. A run blocked on a child process
  // that is waiting for input stays alive indefinitely while saying nothing —
  // that is what a stuck card actually looks like, so silence gets its own clock.
  const dead = ago(at) > 180;
  const silent = job.progress_at ? ago(job.progress_at) > 600 : false;
  const worry = dead || silent;
  return (
    <div className={`card-meta ${worry ? "stale" : "pulse"}`}>
      {worry ? "🟠" : "🟢"} {fmtAgo(at, t)}
      {job.progress_text ? ` · ${job.progress_text.slice(0, 60)}` : ""}
      {dead ? ` · ${t("board.maybeStuck")}`
            : silent ? ` · ${t("board.silentFor")} ${fmtAgo(job.progress_at!, t)}`
            : ""}
    </div>
  );
}

function DispatchModal({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const t = useT();
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
        <h2>{t("board.dispatchTitle")}</h2>
        <input placeholder={t("board.titlePh")} value={form.title}
               onChange={(e) => setForm({ ...form, title: e.target.value })} />
        <textarea placeholder={t("board.promptPh")} rows={5} value={form.prompt}
                  onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
        <div className="row">
          <select value={form.agent_id}
                  onChange={(e) => setForm({ ...form, agent_id: e.target.value })}>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.id}</option>)}
          </select>
          <select value={form.template_id}
                  onChange={(e) => setForm({ ...form, template_id: e.target.value })}>
            <option value="">{t("board.singleStage")}</option>
            {templates.map((t) => <option key={t.id} value={t.id}>{t.id}</option>)}
          </select>
          <select value={form.resource_id}
                  onChange={(e) => setForm({ ...form, resource_id: e.target.value })}>
            <option value="">{t("board.direct")}</option>
            {resources.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </div>
        <div className="row">
          <button onClick={go}
                  disabled={!form.prompt || !form.agent_id}>{t("board.go")}</button>
          <button className="ghost" onClick={onClose}>{t("c.cancel")}</button>
        </div>
        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}

function JobDrawer({ jobId, canOperate, onClose, onChanged }:
  { jobId: string; canOperate: boolean; onClose: () => void; onChanged: () => void }) {
  const t = useT();
  const [job, setJob] = useState<JobDetail | null>(null);
  const [comment, setComment] = useState("");
  const [approvalStage, setApprovalStage] = useState("");
  const [interactions, setInteractions] = useState<Record<string, Interaction[]>>({});
  const [diff, setDiff] = useState<string | null>(null);
  const [agents, setAgents] = useState<{ id: string }[]>([]);
  const [retryAgent, setRetryAgent] = useState("");
  const [retrySpec, setRetrySpec] = useState<string | null>(null);
  const [refreshWorkflow, setRefreshWorkflow] = useState(true);
  const [renewRecoveryLease, setRenewRecoveryLease] = useState(false);

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

  // hooks first, unconditionally: this one used to sit after the early return
  // below, which threw React #310 ("more hooks than the previous render") and
  // white-screened the app the moment a card was opened
  useEffect(() => {
    api<{ id: string }[]>("/api/agents").then(setAgents).catch(() => {});
  }, []);

  const [answerText, setAnswerText] = useState<Record<string, string>>({});
  const [previews, setPreviews] = useState<string[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [supplies, setSupplies] = useState<{ id: string; name: string;
    content: string; created_at: string }[]>([]);
  const [supplyName, setSupplyName] = useState("");
  const [supplyText, setSupplyText] = useState("");
  const [supplyError, setSupplyError] = useState("");
  const [rulingText, setRulingText] = useState("");
  const [rulingError, setRulingError] = useState("");
  const [supplyNote, setSupplyNote] = useState("");
  const [repairDeliveryMode, setRepairDeliveryMode] =
    useState<"branch" | "integration" | "production">("integration");
  const [repairDeliveryVersion, setRepairDeliveryVersion] = useState("");
  const [repairDeliveryError, setRepairDeliveryError] = useState("");
  const [branchReview, setBranchReview] = useState<BranchReview | null>(null);
  const [branchReviewError, setBranchReviewError] = useState("");
  const [mergeError, setMergeError] = useState("");
  const [repoBrowse, setRepoBrowse] = useState<RepoBrowse | null>(null);
  const [repoBrowseError, setRepoBrowseError] = useState("");
  const imagePreviewNames = useMemo(
    () => previews.filter((name) => /\.(png|jpe?g|gif|webp)$/i.test(name)),
    [previews]);

  useEffect(() => {
    api<string[]>(`/api/jobs/${jobId}/previews`).then(setPreviews)
      .catch(() => setPreviews([]));
    api<typeof supplies>(`/api/jobs/${jobId}/supplies`).then(setSupplies)
      .catch(() => setSupplies([]));
  }, [jobId, job?.status]);

  /* preview files sit behind the API token, so <img src> cannot fetch them
     directly — pull each one with the auth header and hand out a blob URL */
  useEffect(() => {
    let dead = false;
    const urls: Record<string, string> = {};
    (async () => {
      for (const name of imagePreviewNames) {
        try {
          const blob = await apiBlob(`/api/jobs/${jobId}/previews/${encodeURIComponent(name)}`);
          if (dead) return;
          urls[name] = URL.createObjectURL(blob);
          setPreviewUrls({ ...urls });
        } catch { /* listed but unreadable: the name row still shows */ }
      }
    })();
    return () => { dead = true; Object.values(urls).forEach(URL.revokeObjectURL); };
  }, [imagePreviewNames, jobId]);

  useEffect(() => {
    let deliveryMode = "";
    try { deliveryMode = JSON.parse(job?.delivery_json || "{}").mode || ""; }
    catch { /* malformed historic contract cannot offer a merge */ }
    const available = job?.status === "done"
      && job.delivery_status === "succeeded" && deliveryMode === "branch";
    if (!available) {
      setBranchReview(null);
      setBranchReviewError("");
      return;
    }
    let dead = false;
    setBranchReviewError("");
    api<BranchReview>(`/api/jobs/${jobId}/branch-review`)
      .then((review) => { if (!dead) setBranchReview(review); })
      .catch((error) => {
        if (!dead) {
          setBranchReview(null);
          setBranchReviewError(String((error as Error).message));
        }
      });
    return () => { dead = true; };
  }, [jobId, job?.status, job?.delivery_status, job?.delivery_json]);

  const browseRepo = useCallback((path = "") => {
    setRepoBrowseError("");
    api<RepoBrowse>(`/api/jobs/${jobId}/files?path=${encodeURIComponent(path)}`)
      .then(setRepoBrowse)
      .catch((error) => { setRepoBrowse(null); setRepoBrowseError(String(error)); });
  }, [jobId]);

  useEffect(() => { browseRepo(); }, [browseRepo, job?.status]);

  const addSupply = async () => {
    setSupplyError("");
    setSupplyNote("");
    try {
      const out = await post<{ delivered_to_live_worktree: boolean }>(
        `/api/jobs/${jobId}/supplies`, { name: supplyName, content: supplyText });
      setSupplyName("");
      setSupplyText("");
      setSupplyNote(out.delivered_to_live_worktree
        ? t("board.supplyLive") : t("board.supplyNextRun"));
      api<typeof supplies>(`/api/jobs/${jobId}/supplies`).then(setSupplies);
    } catch (e) { setSupplyError(String((e as Error).message)); }
  };


  if (!job) return null;
  const lastGate = job.gates[job.gates.length - 1];
  let stageDefs: { name: string; gate?: string }[] = [];
  try { stageDefs = JSON.parse(job.stages_snapshot_json || "[]"); } catch { /* keep [] */ }
  const pendingApprovalStages = (job.stage_nodes ?? []).filter((node) => {
    if (node.status !== "blocked"
        || stageDefs.find((stage) => stage.name === node.stage)?.gate !== "human-approve") {
      return false;
    }
    const runIds = new Set(job.runs.filter((run) => run.stage === node.stage)
      .map((run) => run.id));
    return job.gates.some((gate) => runIds.has(gate.run_id)
      && gate.gate_type === "human-approve" && gate.verdict === "pending");
  }).map((node) => node.stage);
  const waitingApproval = job.status === "blocked"
    && (pendingApprovalStages.length > 0 || lastGate?.verdict === "pending");
  const selectedApprovalStage = approvalStage || pendingApprovalStages[0] || job.stage;

  // the reason the stage failed: the last run's error, shown where the retry is
  const failure = job.runs.slice().reverse()
    .map((r) => r.error).find((e) => e && e.trim()) || "";

  // One action because the ruling only helps if the card runs again. Always
  // adopt the current execution contract; a ruling does not silently reopen
  // spent recovery budgets.
  const ruleAndRetry = async () => {
    setRulingError("");
    try {
      await post(`/api/jobs/${jobId}/supplies`,
                 { name: "human-ruling", content: rulingText });
      await post(`/api/jobs/${jobId}/retry`, { agent_id: "", spec: "",
                                               refresh_workflow: true,
                                               renew_recovery_lease: false,
                                               restart_from_rework_target: true });
      setRulingText("");
      onChanged();
      load();
    } catch (e) { setRulingError(String((e as Error).message)); }
  };

  const retry = async () => {
    await post(`/api/jobs/${jobId}/retry`, {
      agent_id: retryAgent,
      spec: retrySpec ?? "",                    // blank = keep the current spec
      refresh_workflow: refreshWorkflow,
      renew_recovery_lease: renewRecoveryLease });
    setRetrySpec(null);
    onChanged();
    load();
  };

  const decide = async (approved: boolean) => {
    await post(`/api/jobs/${jobId}/approve`, {
      approved, comment, stage: selectedApprovalStage,
    });
    onChanged();
    load();
  };

  const repairDelivery = async () => {
    setRepairDeliveryError("");
    try {
      await put(`/api/jobs/${jobId}/delivery`, {
        mode: repairDeliveryMode,
        ...(repairDeliveryMode === "production"
          ? { version: repairDeliveryVersion.trim() } : {}),
      });
      onChanged();
      load();
    } catch (e) { setRepairDeliveryError(String((e as Error).message)); }
  };

  const mergeBranch = async () => {
    setMergeError("");
    try {
      await post(`/api/jobs/${jobId}/branch-review/merge`, {});
      onChanged();
      load();
    } catch (e) { setMergeError(String((e as Error).message)); }
  };

  const answer = async (runId: string, requestId: string, allow: boolean) => {
    const message = (answerText[requestId] || "").trim();
    await post(`/api/runs/${runId}/respond`,
               { request_id: requestId,
                 reply: { behavior: allow ? "allow" : "deny",
                          ...(message ? { message } : {}) } });
    onChanged();
    load();
  };

  return (
    <aside className="drawer">
      <button className="ghost close" onClick={onClose}>✕</button>
      <h2>{job.title}</h2>
      <p className="card-meta">{job.id} · {t("board.jobStage")} <b>{job.stage}</b> · {job.status}</p>
      {job.delivery_status && job.delivery_status !== "not_required" && (
        <div className={job.delivery_status === "failed" ? "approval" : "notice"}>
          <b>🚚 {t("board.delivery")}: {job.delivery_status}</b>
          {job.deliveries?.map((d) => (
            <p className="card-meta" key={d.id}>
              {d.mode} · {d.target || "—"}
              {d.version ? ` · v${d.version}` : ""}
              {d.commit_sha ? ` · ${d.commit_sha.slice(0, 12)}` : ""}
              {d.error ? ` · ${d.error.slice(0, 240)}` : ""}
            </p>
          ))}
          {job.delivery_actions?.map((a) => (
            <p className="card-meta" key={`${a.action}:${a.idempotency_key}`}>
              ↻ {a.action} · {a.provider} · {a.status}
              {a.idempotency_key ? ` · key ${a.idempotency_key.slice(0, 12)}` : ""}
              {a.error ? ` · ${a.error.slice(0, 240)}` : ""}
            </p>
          ))}
        </div>
      )}
      {(branchReview || branchReviewError) && (
        <div className="approval">
          <h3>{t("board.branchReview")}</h3>
          {branchReview && (
            <>
              <p className="muted">{t("board.branchReviewHint", {
                branch: branchReview.target_branch,
              })}</p>
              <p className="card-meta">
                {branchReview.base_commit.slice(0, 12)} → {branchReview.branch_commit.slice(0, 12)}
                {` · ${branchReview.files.length} ${t("board.changedFiles")}`}
              </p>
              <ul>
                {branchReview.files.map((file, index) => (
                  <li className="card-meta" key={`${file.status}:${file.path}:${index}`}>
                    <b>{file.status}</b> {file.previous_path
                      ? `${file.previous_path} → ${file.path}` : file.path}
                  </li>
                ))}
              </ul>
              {!!branchReview.stat && <pre className="spec">{branchReview.stat}</pre>}
              {!!branchReview.patch && <pre className="spec diff">{branchReview.patch}</pre>}
              {branchReview.truncated && <p className="muted">{t("board.patchTruncated")}</p>}
              {canOperate && (
                <button onClick={mergeBranch}>{t("board.mergeBranch")}</button>
              )}
            </>
          )}
          {branchReviewError && <p className="error">{branchReviewError}</p>}
          {mergeError && <p className="error">{mergeError}</p>}
        </div>
      )}
      {!!job.media_claims?.length && (
        <div className="notice">
          <h3>{t("board.mediaClaims")}</h3>
          {job.media_claims.map((claim) => (
            <p className="card-meta" key={claim.id}>
              {claim.status === "fetched" ? "✅" : claim.status === "failed" ? "🟠" : "⏳"}
              {` ${claim.destination} · ${claim.status}`}
              {claim.provider_status ? ` · ${claim.provider_status}` : ""}
              {claim.attempts ? ` · ${claim.attempts} ${t("board.pollAttempts")}` : ""}
              {claim.bytes ? ` · ${claim.bytes.toLocaleString()} B` : ""}
              {claim.sha256 ? ` · sha256:${claim.sha256.slice(0, 12)}` : ""}
              {claim.error ? ` · ${claim.error.slice(0, 240)}` : ""}
            </p>
          ))}
        </div>
      )}
      {canOperate && job.status === "done"
        && (!job.delivery_status || job.delivery_status === "not_required") && (
        <div className="approval">
          <h3>{t("board.repairDelivery")}</h3>
          <p className="muted">{t("board.repairDeliveryHint")}</p>
          <div className="row">
            <select value={repairDeliveryMode}
                    onChange={(e) => setRepairDeliveryMode(
                      e.target.value as "branch" | "integration" | "production") }>
              <option value="branch">{t("proj.deliveryBranch")}</option>
              <option value="integration">{t("proj.deliveryIntegration")}</option>
              <option value="production">{t("proj.deliveryProduction")}</option>
            </select>
            {repairDeliveryMode === "production" && (
              <input value={repairDeliveryVersion}
                     placeholder={t("proj.deliveryVersionPh")}
                     onChange={(e) => setRepairDeliveryVersion(e.target.value)} />
            )}
            <button disabled={repairDeliveryMode === "production"
                              && !repairDeliveryVersion.trim()}
                    onClick={repairDelivery}>{t("board.repairDeliveryGo")}</button>
          </div>
          {repairDeliveryError && <p className="error">{repairDeliveryError}</p>}
        </div>
      )}
      {!!job.rework_count && (
        <p className="notice">🔧 {t("board.reworkNote", { n: job.rework_count })}</p>
      )}
      <pre className="spec">{job.spec_md}</pre>

      {(repoBrowse || repoBrowseError) && (
        <div className="notice">
          <h3>{t("board.repoEvidence")}</h3>
          {repoBrowse && (
            <>
              <p className="card-meta">
                commit {repoBrowse.commit.slice(0, 12)} · /{repoBrowse.path}
              </p>
              {!!repoBrowse.path && (
                <button className="ghost" onClick={() => browseRepo(
                  repoBrowse.path.split("/").slice(0, -1).join("/"))}>← {t("board.repoParent")}</button>
              )}
              {repoBrowse.kind === "directory" ? (
                <ul>{repoBrowse.entries?.map((entry) => (
                  <li key={entry.path}>
                    <button className="ghost" onClick={() => browseRepo(entry.path)}>
                      {entry.kind === "directory" ? "📁" : "📄"} {entry.name}
                      {entry.size != null ? ` · ${entry.size.toLocaleString()} B` : ""}
                    </button>
                  </li>
                ))}</ul>
              ) : repoBrowse.binary ? (
                <p className="muted">{t("board.repoBinary")} · {repoBrowse.size?.toLocaleString()} B</p>
              ) : repoBrowse.truncated ? (
                <p className="muted">{t("board.repoTooLarge")}</p>
              ) : (
                <pre className="spec">{repoBrowse.content}</pre>
              )}
            </>
          )}
          {repoBrowseError && <p className="muted">{repoBrowseError}</p>}
        </div>
      )}

      {!!job.stage_nodes?.length && (
        <>
          <h3>{t("board.stageGraph", undefined, "Stage graph")}</h3>
          <table>
            <thead><tr><th>stage</th><th>needs</th><th>workspace</th><th>status</th></tr></thead>
            <tbody>
              {job.stage_nodes.map((node) => (
                <tr key={node.stage}>
                  <td>{node.stage}</td>
                  <td>{JSON.parse(node.needs_json || "[]").join(", ") || "—"}</td>
                  <td>{node.workspace}</td><td>{node.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {!!job.evidence_matrix?.length && (
        <>
          <h3>Evidence matrix</h3>
          <table>
            <thead><tr><th>evidence</th><th>stage</th><th>gate</th><th>verdict</th></tr></thead>
            <tbody>{job.evidence_matrix.map((row, index) => (
              <tr key={`${row.kind}:${row.stage}:${index}`}>
                <td>{row.kind}</td><td>{row.stage}</td><td>{row.gate}</td>
                <td>{row.verdict}</td>
              </tr>
            ))}</tbody>
          </table>
        </>
      )}

      {canOperate && job.status === "blocked"
        && job.pm_decision?.action === "escalate" && (
        <div className="approval pm-ask">
          <h3>🤖 {t("board.pmAsks")}</h3>
          <p className="card-meta">
            {job.pm_decision.pm} · {fmtTime(job.pm_decision.at)}
            {job.pm_decision.cycle
              ? ` · ${job.pm_decision.cycle}/${job.pm_decision.max}`
              : ""}
          </p>
          <pre className="spec">{job.pm_decision.reason}</pre>
          <textarea rows={3} placeholder={t("board.rulingPh")} value={rulingText}
                    onChange={(e) => setRulingText(e.target.value)} />
          <div className="row">
            <button disabled={!rulingText.trim()} onClick={ruleAndRetry}>
              {t("board.ruleAndRetry")}</button>
            <span className="muted">{t("board.rulingHint")}</span>
          </div>
          {rulingError && <p className="error">{rulingError}</p>}
        </div>
      )}

      {canOperate && (job.status === "blocked" || job.status === "cancelled") && (
        <div className="approval">
          <h3>{t("board.stuck")}</h3>
          {failure && <pre className="spec">{failure}</pre>}
          <div className="row">
            <select value={retryAgent} onChange={(e) => setRetryAgent(e.target.value)}>
              <option value="">{t("board.retrySameAgent")}</option>
              {agents.map((a) => <option key={a.id} value={a.id}>{a.id}</option>)}
            </select>
            <label className="chk">
              <input type="checkbox" checked={refreshWorkflow}
                     onChange={(e) => setRefreshWorkflow(e.target.checked)} />
              {t("board.retryRefresh")}
            </label>
            <label className="chk">
              <input type="checkbox" checked={renewRecoveryLease}
                     onChange={(e) => setRenewRecoveryLease(e.target.checked)} />
              {t("board.retryRenewLease")}
            </label>
            <button onClick={retry}>{t("board.retry")}</button>
          </div>
          <textarea rows={5} value={retrySpec ?? job.spec_md}
                    onChange={(e) => setRetrySpec(e.target.value)} />
          <p className="muted">{t("board.retryHint")}</p>
          <p className="muted">{t("board.retrySpecHint")}</p>
        </div>
      )}

      {canOperate && waitingApproval && (
        <div className="approval">
          <h3>{t("board.waitingApproval")}</h3>
          {pendingApprovalStages.length > 1 && (
            <select value={selectedApprovalStage}
                    onChange={(e) => setApprovalStage(e.target.value)}>
              {pendingApprovalStages.map((stage) => (
                <option key={stage} value={stage}>{stage}</option>
              ))}
            </select>
          )}
          {previews.length > 0 ? (
            <div className="previews">
              {previews.map((name) => previewUrls[name] ? (
                <a key={name} href={previewUrls[name]} target="_blank" rel="noreferrer">
                  <img src={previewUrls[name]} alt={name} title={name} />
                </a>
              ) : (
                <PreviewLink key={name} jobId={jobId} name={name} />
              ))}
            </div>
          ) : (
            <p className="muted">{t("board.noPreview")}</p>
          )}
          <input placeholder={t("board.commentPh")} value={comment}
                 onChange={(e) => setComment(e.target.value)} />
          <div>
            <button onClick={() => decide(true)}>{t("board.approve")}</button>
            <button className="danger"
                    onClick={() => decide(false)}>{t("board.reject")}</button>
          </div>
        </div>
      )}

      {canOperate && Object.entries(interactions).map(([runId, items]) =>
        items.filter((i) => i.status === "pending").map((i) => (
          <div className="approval" key={i.request_id}>
            <h3>✋ {i.kind} — run {runId} · {t("board.needsYou")}</h3>
            <pre className="spec">{i.payload_json}</pre>
            <input placeholder={t("board.answerPh")}
                   value={answerText[i.request_id] || ""}
                   onChange={(e) => setAnswerText({ ...answerText,
                                                    [i.request_id]: e.target.value })} />
            <div>
              <button onClick={() =>
                answer(runId, i.request_id, true)}>{t("board.allow")}</button>
              <button className="danger" onClick={() =>
                answer(runId, i.request_id, false)}>{t("board.deny")}</button>
            </div>
          </div>
        )))}

      {canOperate && job.status !== "done" && job.status !== "cancelled" && (
        <div className="supplies">
          <h3>📦 {t("board.supplies")}</h3>
          {supplies.map((sup) => (
            <div key={sup.id} className="supply-row">
              <b>{sup.name}</b> <span className="card-meta">{fmtTime(sup.created_at)}</span>
              <pre className="spec">{sup.content}</pre>
            </div>
          ))}
          <input placeholder={t("board.supplyNamePh")} value={supplyName}
                 onChange={(e) => setSupplyName(e.target.value)} />
          <textarea rows={3} placeholder={t("board.supplyContentPh")}
                    value={supplyText}
                    onChange={(e) => setSupplyText(e.target.value)} />
          <div className="row">
            <button disabled={!supplyName.trim() || !supplyText.trim()}
                    onClick={addSupply}>{t("board.supplyAdd")}</button>
            <span className="muted">{t("board.supplyHint")}</span>
          </div>
          {supplyNote && <p className="notice">{supplyNote}</p>}
          {supplyError && <p className="error">{supplyError}</p>}
        </div>
      )}

      {canOperate && (job.status === "cancelled" || job.status === "done") && (
        <div className="row job-removal">
          <button className="ghost" onClick={async () => {
            await post(`/api/jobs/${jobId}/archive`, { archived: !job.archived });
            onChanged();
            onClose();
          }}>{t(job.archived ? "board.unarchive" : "board.archive")}</button>
          <span className="muted">{t("board.removalHint")}</span>
        </div>
      )}

      <h3>{t("board.runs")}</h3>
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

      <h3>{t("board.gates")}</h3>
      <ul className="gates">
        {job.gates.map((g, i) => (
          <li key={i}>
            <b>{g.gate_type}</b> → {g.verdict}
            <span className="card-meta"> {t("board.by")} {g.reviewer_kind}:{g.reviewer_id}</span>
            {g.detail_md && <div className="gate-detail">{g.detail_md}</div>}
            {g.config_error ? (
              <div className="gate-config-error">{t("board.configError")}</div>
            ) : null}
          </li>
        ))}
      </ul>

      {diff && (
        <>
          <h3>{t("board.diff")}</h3>
          <pre className="spec diff">{diff}</pre>
        </>
      )}
    </aside>
  );
}

/** A non-image preview (HTML snapshot, Markdown summary): fetched with auth and
 *  opened as a blob, because a plain link cannot carry the token. */
function PreviewLink({ jobId, name }: { jobId: string; name: string }) {
  const open = async () => {
    const blob = await apiBlob(`/api/jobs/${jobId}/previews/${encodeURIComponent(name)}`);
    window.open(URL.createObjectURL(blob), "_blank");
  };
  return <button className="ghost" onClick={open}>📄 {name}</button>;
}
