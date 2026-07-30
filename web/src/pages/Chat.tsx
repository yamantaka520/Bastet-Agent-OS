import { useCallback, useEffect, useRef, useState } from "react";
import { api, del, post, put, getToken } from "../api";
import { useT, type T } from "../i18n";
import { Section, useList } from "../ui";

/** The human end of the loop: plan a project by talking about it, hand over
 *  files, then dispatch or authorise from the same place. Sessions are bound to
 *  a real project, so what is discussed and what runs cannot drift apart. */

type Responder = { kind: string; id: string; label: string; detail: string };
type Session = { id: string; scope_type: string; scope_id: string; title: string;
                 responder_kind: string; responder_id: string; channel: string;
                 messages: number; updated_at: string };
type Attachment = { id: string; name: string; size: number; mime: string };
type Message = { id: string; role: string; author: string; content: string;
                 attachments: Attachment[]; meta: Record<string, unknown>;
                 at: string };
type Pending = { id: string; title: string; stage: string };
type ProjectRow = { id: string; team_id: string };
type AgentRow = { id: string; name: string; enabled: number };

export default function ChatPage(props: { canOperate: boolean; refreshKey: number }) {
  const t = useT();
  const [projects] = useList<ProjectRow>("/api/projects", props.refreshKey);
  const [agents] = useList<AgentRow>("/api/agents", props.refreshKey);
  const [responders, setResponders] = useState<Responder[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [current, setCurrent] = useState<string>("");
  const [error, setError] = useState("");

  const loadSessions = useCallback(() => {
    api<Session[]>("/api/chat/sessions").then((rows) => {
      setSessions(rows);
      setCurrent((now) => now || rows[0]?.id || "");
    }).catch(() => setSessions([]));
  }, []);
  useEffect(loadSessions, [loadSessions, props.refreshKey]);
  useEffect(() => {
    api<Responder[]>("/api/chat/responders").then(setResponders).catch(() => {});
  }, []);

  return (
    <div className="page chat-page">
      <Section title={t("chat.sessions")}>
        {error && <p className="error">{error}</p>}
        {props.canOperate && (
          <NewSession projects={projects} responders={responders} t={t}
                      onError={setError}
                      onCreated={(id) => { loadSessions(); setCurrent(id); }} />
        )}
        <div className="chat-layout">
          <div className="chat-list">
            {!sessions.length && <p className="muted">{t("chat.noSessions")}</p>}
            {sessions.map((s) => (
              <button key={s.id}
                      className={`chat-item ${current === s.id ? "active" : ""}`}
                      onClick={() => setCurrent(s.id)}>
                <b>{s.title}</b>
                <span className="card-meta">
                  {s.scope_type === "project" ? "📁" : s.scope_type === "team" ? "🏷" : "🌐"}
                  {" "}{s.scope_id}
                  {s.channel !== "web" ? ` · ${s.channel}` : ""}
                  {" · "}{s.messages}
                </span>
              </button>
            ))}
          </div>
          <div className="chat-main">
            {current
              ? <Conversation key={current} sessionId={current} agents={agents}
                              responders={responders} canOperate={props.canOperate}
                              refreshKey={props.refreshKey} t={t}
                              onChanged={loadSessions}
                              onDeleted={() => { setCurrent(""); loadSessions(); }} />
              : <p className="muted">{t("chat.noneSelected")}</p>}
          </div>
        </div>
      </Section>
    </div>
  );
}

function ResponderOptions({ responders, t }: { responders: Responder[]; t: T }) {
  const agents = responders.filter((r) => r.kind === "agent");
  const llms = responders.filter((r) => r.kind === "resource");
  return (
    <>
      <optgroup label={t("chat.groupAgents")}>
        {agents.map((r) => (
          <option key={`agent:${r.id}`} value={`agent:${r.id}`}>
            {r.label}（{r.detail}）</option>
        ))}
      </optgroup>
      <optgroup label={t("chat.groupLLMs")}>
        {llms.map((r) => (
          <option key={`resource:${r.id}`} value={`resource:${r.id}`}>
            {r.label}（{r.detail}）</option>
        ))}
      </optgroup>
    </>
  );
}

function NewSession({ projects, responders, onCreated, onError, t }: {
  projects: ProjectRow[]; responders: Responder[];
  onCreated: (id: string) => void; onError: (msg: string) => void; t: T;
}) {
  const teams = [...new Set(projects.map((p) => p.team_id))];
  const [scopeType, setScopeType] = useState("project");
  const [scopeId, setScopeId] = useState("");
  const [responder, setResponder] = useState("");
  const [title, setTitle] = useState("");

  const create = async () => {
    onError("");
    const [kind, id] = responder.split(":");
    try {
      const out = await post<{ id: string }>("/api/chat/sessions", {
        scope_type: scopeType, scope_id: scopeId, responder_kind: kind,
        responder_id: id, title });
      setTitle("");
      onCreated(out.id);
    } catch (e) { onError(String((e as Error).message)); }
  };

  return (
    <>
      <div className="inline-form">
        <label className="res-field">
          <span>{t("chat.scope")}</span>
          <select value={scopeType} onChange={(e) => { setScopeType(e.target.value);
                                                      setScopeId(""); }}>
            <option value="project">{t("sec.labelProject")}</option>
            <option value="team">{t("sec.labelTeam")}</option>
            <option value="global">{t("sec.labelGlobal")}</option>
          </select>
        </label>
        {scopeType !== "global" && (
          <select value={scopeId} onChange={(e) => setScopeId(e.target.value)}>
            <option value="">{t("sec.pickScope")}</option>
            {(scopeType === "team" ? teams : projects.map((p) => p.id)).map((id) =>
              <option key={id} value={id}>{id}</option>)}
          </select>
        )}
        <label className="res-field">
          <span>{t("chat.responder")}</span>
          <select value={responder} onChange={(e) => setResponder(e.target.value)}>
            <option value="">{t("chat.pickResponder")}</option>
            <ResponderOptions responders={responders} t={t} />
          </select>
        </label>
        <input placeholder={t("chat.titlePh")} value={title}
               style={{ width: "16rem" }}
               onChange={(e) => setTitle(e.target.value)} />
        <button onClick={create}
                disabled={!responder || (scopeType !== "global" && !scopeId)}>
          {t("chat.create")}</button>
      </div>
      <p className="muted">{t("chat.scopeHint")}</p>
    </>
  );
}

function Conversation({ sessionId, agents, responders, canOperate, refreshKey,
                        onChanged, onDeleted, t }: {
  sessionId: string; agents: AgentRow[]; responders: Responder[];
  canOperate: boolean; refreshKey: number; onChanged: () => void;
  onDeleted: () => void; t: T;
}) {
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState<Pending[]>([]);
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dispatchAgent, setDispatchAgent] = useState("");
  const [dispatchTitle, setDispatchTitle] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    api<{ session: Session; messages: Message[]; pending_approvals: Pending[] }>(
      `/api/chat/sessions/${sessionId}/messages`)
      .then((body) => {
        setSession(body.session);
        setMessages(body.messages);
        setPending(body.pending_approvals);
      }).catch((e) => setError(String((e as Error).message)));
  }, [sessionId]);
  useEffect(load, [load, refreshKey]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); },
            [messages.length, busy]);

  const upload = async (picked: FileList | null) => {
    if (!picked?.length) return;
    setError("");
    for (const file of Array.from(picked)) {
      const form = new FormData();
      form.append("file", file);
      const resp = await fetch(`/api/chat/sessions/${sessionId}/files`, {
        method: "POST", body: form,
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!resp.ok) { setError(await resp.text()); return; }
      const item: Attachment = await resp.json();
      setFiles((old) => [...old, item]);
    }
  };

  const send = async () => {
    if (!draft.trim() && !files.length) return;
    setBusy(true);
    setError("");
    try {
      const body = await post<{ messages: Message[] }>(
        `/api/chat/sessions/${sessionId}/messages`,
        { content: draft, attachment_ids: files.map((f) => f.id) });
      setMessages(body.messages);
      setDraft("");
      setFiles([]);
      onChanged();
    } catch (e) {
      setError(String((e as Error).message));
      load();                       // the user's message is kept even on failure
    } finally { setBusy(false); }
  };

  const dispatch = async () => {
    setError("");
    try {
      await post(`/api/chat/sessions/${sessionId}/dispatch`,
                 { agent_id: dispatchAgent, title: dispatchTitle });
      setDispatchTitle("");
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  const decide = async (jobId: string, approved: boolean) => {
    setError("");
    try {
      await post(`/api/jobs/${jobId}/approve`, { approved, comment: "via chat" });
      load();
    } catch (e) { setError(String((e as Error).message)); }
  };

  if (!session) return <p className="muted">…</p>;
  const responderLabel = responders.find(
    (r) => r.kind === session.responder_kind && r.id === session.responder_id);

  return (
    <>
      <div className="chat-head">
        <b>{session.title}</b>
        <span className="card-meta">
          {session.scope_type}:{session.scope_id} ·{" "}
          {responderLabel?.label ?? session.responder_id}</span>
        {canOperate && (
          <span className="row-ops">
            <select value={`${session.responder_kind}:${session.responder_id}`}
                    onChange={async (e) => {
                      const [kind, id] = e.target.value.split(":");
                      await put(`/api/chat/sessions/${sessionId}`,
                                { responder_kind: kind, responder_id: id });
                      load(); onChanged();
                    }}>
              <ResponderOptions responders={responders} t={t} />
            </select>
            <button className="ghost danger-text" title={t("chat.deleteSession")}
                    onClick={async () => {
                      await del(`/api/chat/sessions/${sessionId}`); onDeleted();
                    }}>{t("c.delete")}</button>
          </span>
        )}
      </div>

      {!!pending.length && (
        <div className="chat-pending">
          <b>⏸ {t("chat.pending", { n: pending.length })}</b>
          {pending.map((job) => (
            <div key={job.id} className="chat-pending-row">
              <span>{job.title} <span className="card-meta">{job.id} · {job.stage}</span></span>
              {canOperate && (
                <span className="row-ops">
                  <button onClick={() => decide(job.id, true)}>{t("board.approve")}</button>
                  <button className="danger"
                          onClick={() => decide(job.id, false)}>{t("board.reject")}</button>
                </span>
              )}
            </div>
          ))}
          <p className="muted">{t("chat.approveHint")}</p>
        </div>
      )}

      <div className="chat-log">
        {messages.map((m) => (
          <div key={m.id} className={`chat-msg ${m.role}`}>
            <div className="chat-msg-head">
              <b>{m.role === "user" ? t("chat.you")
                  : m.role === "system" ? t("chat.system")
                  : responderLabel?.label ?? m.author}</b>
              <span className="card-meta">{(m.at || "").replace("T", " ").slice(0, 19)}
                {typeof m.meta.tokens_in === "number" && (m.meta.tokens_in as number) > 0
                  ? ` · ${t("chat.usage", {
                      tokens: (m.meta.tokens_in as number)
                              + ((m.meta.tokens_out as number) || 0),
                      cost: ((m.meta.cost_usd as number) || 0).toFixed(4) })}`
                  : ""}</span>
            </div>
            <div className="chat-msg-body">{m.content}</div>
            {!!m.attachments.length && (
              <div className="chat-files">
                {m.attachments.map((a) => (
                  <a key={a.id} className="role-chip"
                     href={`/api/chat/sessions/${sessionId}/files/${a.id}`}
                     target="_blank" rel="noreferrer">📎 {a.name}</a>
                ))}
              </div>
            )}
          </div>
        ))}
        {busy && <p className="muted">{t("chat.thinking")}</p>}
        <div ref={bottom} />
      </div>

      {error && <p className="error">{error}</p>}

      {canOperate && (
        <>
          <div className="chat-compose">
            <textarea rows={3} value={draft} placeholder={t("chat.messagePh")}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          if (!busy) send();
                        }
                      }} />
            <div className="row">
              <label className="ghost file-btn">
                {t("chat.attach")}
                <input type="file" multiple style={{ display: "none" }}
                       onChange={(e) => upload(e.target.files)} />
              </label>
              {!!files.length && (
                <span className="muted">{t("chat.attached", { n: files.length })}：
                  {files.map((f) => f.name).join(", ")}</span>
              )}
              <button onClick={send}
                      disabled={busy || (!draft.trim() && !files.length)}>
                {busy ? t("chat.thinking") : t("c.send")}</button>
            </div>
          </div>

          {session.scope_type === "project" && (
            <div className="chat-dispatch">
              <b>{t("chat.dispatch")}</b>
              <div className="inline-form">
                <label className="res-field">
                  <span>{t("chat.dispatchAgent")}</span>
                  <select value={dispatchAgent}
                          onChange={(e) => setDispatchAgent(e.target.value)}>
                    <option value="">{t("c.pick")}</option>
                    {agents.filter((a) => a.enabled).map((a) => (
                      <option key={a.id} value={a.id}>{a.name}</option>
                    ))}
                  </select>
                </label>
                <input placeholder={t("chat.dispatchTitlePh")} value={dispatchTitle}
                       onChange={(e) => setDispatchTitle(e.target.value)} />
                <button onClick={dispatch} disabled={!dispatchAgent}>
                  {t("chat.dispatchGo")}</button>
              </div>
              <p className="muted">{t("chat.dispatchHint")}</p>
            </div>
          )}
        </>
      )}
    </>
  );
}
