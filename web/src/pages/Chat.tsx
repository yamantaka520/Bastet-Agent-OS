import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, apiBlob, del, post, put, getToken } from "../api";
import { useT, type T } from "../i18n";
import { Section, onEnterSubmit, useList, fmtTime } from "../ui";

/** The human end of the loop: plan a project by talking about it, hand over
 *  files, then dispatch or authorise from the same place. Sessions are bound to
 *  a real project, so what is discussed and what runs cannot drift apart. */

type Responder = { kind: string; id: string; label: string; detail: string };
type Session = { id: string; scope_type: string; scope_id: string; title: string;
                 responder_kind: string; responder_id: string; channel: string;
                 messages: number; updated_at: string; state: string;
                 planning_round_id?: string };
type Attachment = { id: string; name: string; size: number; mime: string };
type Message = { id: string; role: string; author: string; content: string;
                 attachments: Attachment[]; meta: Record<string, unknown>;
                 at: string };
type Pending = { id: string; title: string; stage: string };
type ProjectRow = { id: string; team_id: string };
type Planning = { round: null | { id: string; state: string; ordinal: number;
                                  solution_md: string };
                  intake: { id: string; kind: string; content: string }[] };

export default function ChatPage(props: { canOperate: boolean; refreshKey: number }) {
  const t = useT();
  const [projects] = useList<ProjectRow>("/api/projects", props.refreshKey);
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
              ? <Conversation key={current} sessionId={current}
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

function Conversation({ sessionId, responders, canOperate, refreshKey,
                        onChanged, onDeleted, t }: {
  sessionId: string; responders: Responder[];
  canOperate: boolean; refreshKey: number; onChanged: () => void;
  onDeleted: () => void; t: T;
}) {
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState<Pending[]>([]);
  const [planning, setPlanning] = useState<Planning | null>(null);
  const [intake, setIntake] = useState("");
  const [busy, setBusy] = useState(false);
  // while the user is mid-message we must not re-render the composer: a WS
  // event arriving during IME composition drops the text being composed
  const typing = useRef(false);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    api<{ session: Session; messages: Message[]; pending_approvals: Pending[];
          planning: Planning | null }>(
      `/api/chat/sessions/${sessionId}/messages`)
      .then((body) => {
        setSession(body.session);
        setMessages(body.messages);
        setPending(body.pending_approvals);
        setPlanning(body.planning);
      }).catch((e) => setError(String((e as Error).message)));
  }, [sessionId]);
  useEffect(() => {
    if (typing.current) return;     // never refresh out from under a half-typed message
    load();
  }, [load, refreshKey]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); },
            [messages.length, busy]);

  const upload = async (file: File): Promise<Attachment | null> => {
    setError("");
    const form = new FormData();
    form.append("file", file);
    const resp = await fetch(`/api/chat/sessions/${sessionId}/files`, {
      method: "POST", body: form,
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!resp.ok) { setError(await resp.text()); return null; }
    return await resp.json();
  };

  const send = async (content: string, attachmentIds: string[]) => {
    setBusy(true);
    setError("");
    try {
      const body = await post<{ messages: Message[] }>(
        `/api/chat/sessions/${sessionId}/messages`,
        { content, attachment_ids: attachmentIds });
      setMessages(body.messages);
      onChanged();
      return true;
    } catch (e) {
      setError(String((e as Error).message));
      load();                       // the user's message is kept even on failure
      return false;
    } finally { setBusy(false); }
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
              <span className="card-meta">{fmtTime(m.at, { seconds: true })}
                {typeof m.meta.tokens_in === "number" && (m.meta.tokens_in as number) > 0
                  ? ` · ${t("chat.usage", {
                      tokens: (m.meta.tokens_in as number)
                              + ((m.meta.tokens_out as number) || 0),
                      cost: ((m.meta.cost_usd as number) || 0).toFixed(4) })}`
                  : ""}</span>
            </div>
            <div className="chat-msg-body">{m.content}</div>
            {m.role === "assistant" && canOperate && (
              <ConfigProposal content={m.content} t={t} />
            )}
            {!!m.attachments.length && (
              <div className="chat-files">
                {m.attachments.map((a) => (
                  <MediaAttachment key={a.id} sessionId={sessionId} item={a} />
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
          {session.state !== "frozen" ? (
            <Composer busy={busy} onSend={send} onUpload={upload} t={t}
                      onTypingChange={(active) => { typing.current = active; }} />
          ) : (
            <div className="chat-pending">
              <b>{t("chat.roundFrozen")}</b>
              <p className="muted">{t("chat.roundFrozenHint")}</p>
              <div className="inline-form">
                <textarea value={intake} onChange={(e) => setIntake(e.target.value)}
                          placeholder={t("chat.intakePh")} />
                <button disabled={!intake.trim()} onClick={async () => {
                  try {
                    await post(`/api/projects/${session.scope_id}/planning-intake`,
                               { kind: "idea", content: intake });
                    setIntake(""); load();
                  } catch (e) { setError(String((e as Error).message)); }
                }}>{t("chat.addIntake")}</button>
              </div>
            </div>
          )}

          {session.scope_type === "project" && !planning?.round && (
            <div className="chat-dispatch">
              <button onClick={async () => {
                try {
                  await post(`/api/chat/sessions/${sessionId}/planning-round`, {});
                  load();
                } catch (e) { setError(String((e as Error).message)); }
              }}>{t("chat.startRound")}</button>
            </div>
          )}

          {session.scope_type === "project" && (
            <div className="chat-dispatch">
              <b>{t("chat.decompose")}</b>
              <div className="inline-form">
                <button onClick={async () => {
                  setError("");
                  setBusy(true);
                  try {
                    const out = await post<{ tasks: unknown[] }>(
                      `/api/chat/sessions/${sessionId}/decompose`, {});
                    setError("");
                    load();
                    onChanged();
                    window.alert(t("chat.decomposed", { n: out.tasks.length }));
                  } catch (e) { setError(String((e as Error).message)); }
                  finally { setBusy(false); }
                }} disabled={busy || planning?.round?.state !== "proposed"}>
                  {busy ? t("proj.decomposing") : t("chat.decompose")}</button>
              </div>
              <p className="muted">{planning?.round?.state === "proposed"
                ? t("chat.decomposeHint") : t("chat.decomposeLocked")}</p>
            </div>
          )}
        </>
      )}
    </>
  );
}

/** The message box, deliberately its own component with its own state.
 *
 *  Chat lives next to a live event stream, and a re-render arriving mid-IME
 *  composition wipes the characters being composed — so nothing outside this
 *  component may own the draft. It also grows with the text instead of hiding a
 *  long message inside three rows, and it reports whether the user has
 *  something unsent so the conversation can pause its background reloads. */
function Composer({ busy, onSend, onUpload, onTypingChange, t }: {
  busy: boolean;
  onSend: (content: string, attachmentIds: string[]) => Promise<boolean>;
  onUpload: (file: File) => Promise<Attachment | null>;
  onTypingChange: (active: boolean) => void; t: T;
}) {
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const box = useRef<HTMLTextAreaElement>(null);

  const grow = () => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 320)}px`;
  };
  useEffect(grow, [draft]);
  useEffect(() => { onTypingChange(!!draft.trim() || !!files.length); },
            [draft, files, onTypingChange]);

  const submit = async () => {
    if (busy || (!draft.trim() && !files.length)) return;
    const sent = await onSend(draft, files.map((f) => f.id));
    if (sent) {
      setDraft("");
      setFiles([]);
      onTypingChange(false);
    }
  };

  const pick = async (picked: FileList | null) => {
    for (const file of Array.from(picked ?? [])) {
      const item = await onUpload(file);
      if (item) setFiles((old) => [...old, item]);
    }
  };

  return (
    <div className="chat-compose">
      <textarea ref={box} rows={3} value={draft} placeholder={t("chat.messagePh")}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={onEnterSubmit(submit)} />
      <div className="row">
        <label className="ghost file-btn">
          {t("chat.attach")}
          <input type="file" multiple style={{ display: "none" }}
                 onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
        </label>
        {!!files.length && (
          <span className="muted">{t("chat.attached", { n: files.length })}：
            {files.map((f) => f.name).join(", ")}
            <button className="ghost chip-x" onClick={() => setFiles([])}>✕</button>
          </span>
        )}
        <button onClick={submit} disabled={busy || (!draft.trim() && !files.length)}>
          {busy ? t("chat.thinking") : t("c.send")}</button>
      </div>
    </div>
  );
}

/** A ```bastet-config``` block in an assistant reply is a configuration
 *  PROPOSAL. This card is the human half: it lists the actions in plain terms
 *  and the button is the authority — the audit rows name whoever clicks. */
function ConfigProposal({ content, t }: { content: string; t: T }) {
  const [results, setResults] = useState<{ op: string; status: string;
    detail: string }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const actions = useMemo(() => {
    const marker = "```bastet-config";
    if (!content.includes(marker)) return null;
    const chunk = content.split(marker).pop() ?? "";
    if (!chunk.includes("```")) return null;
    try {
      const data = JSON.parse(chunk.split("```")[0].trim());
      return Array.isArray(data.actions) && data.actions.length
        ? data.actions as Record<string, string>[] : null;
    } catch { return null; }
  }, [content]);
  if (!actions) return null;

  const describe = (a: Record<string, string>) => {
    if (a.op === "resource.create") return t("chat.cfgCreate", {
      kind: a.kind ?? "?", name: a.name ?? "?" })
      + (a.endpoint ? ` → ${a.endpoint}` : "")
      + (a.secret_ref ? ` 🔑 ${a.secret_ref}` : "");
    if (a.op === "resource.update") return t("chat.cfgUpdate", {
      name: a.name ?? a.id ?? "?" });
    if (a.op === "grant.create") return t("chat.cfgGrant", {
      resource: a.resource ?? "?",
      scope: `${a.scope_type ?? "?"}:${a.scope_id ?? "*"}` });
    if (a.op === "settings.timezone") return t("chat.cfgTimezone", {
      zone: a.timezone ?? "?" });
    return a.op ?? "?";
  };

  const applyAll = async () => {
    setBusy(true);
    setError("");
    try {
      const out = await post<{ results: typeof results }>(
        "/api/config/apply", { actions });
      setResults(out.results);
    } catch (e) { setError(String((e as Error).message)); }
    finally { setBusy(false); }
  };

  return (
    <div className="config-proposal">
      <b>⚙ {t("chat.cfgTitle", { n: actions.length })}</b>
      <ul>
        {actions.map((a, i) => (
          <li key={i}>
            {describe(a)}
            {results?.[i] && (
              <span className={results[i].status === "ok" ? "notice" : "error"}>
                {" "}{results[i].status === "ok" ? "✅" : "❌"} {results[i].detail}
              </span>
            )}
          </li>
        ))}
      </ul>
      {!results && (
        <button disabled={busy} onClick={applyAll}>
          {busy ? t("chat.cfgApplying") : t("chat.cfgApply")}</button>
      )}
      {results && <span className="muted">{t("chat.cfgDone")}</span>}
      {error && <p className="error">{error}</p>}
      <p className="muted">{t("chat.cfgHint")}</p>
    </div>
  );
}

/** Generated media render inline — an image you have to download to see is a
 *  broken loop. Files are fetched with the auth header (a bare src cannot carry
 *  the token) and shown by mime: image/audio/video inline, the rest a link. */
function MediaAttachment({ sessionId, item }: {
  sessionId: string; item: { id: string; name: string; mime?: string };
}) {
  const [url, setUrl] = useState("");
  const mime = item.mime || "";
  const inline = /^(image|audio|video)\//.test(mime);

  useEffect(() => {
    if (!inline) return;
    let dead = false;
    let objectUrl = "";
    apiBlob(`/api/chat/sessions/${sessionId}/files/${item.id}`)
      .then((blob) => {
        if (dead) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => setUrl(""));
    return () => { dead = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [sessionId, item.id, inline]);

  const open = async () => {
    const blob = await apiBlob(`/api/chat/sessions/${sessionId}/files/${item.id}`);
    window.open(URL.createObjectURL(blob), "_blank");
  };

  if (inline && url) {
    if (mime.startsWith("image/")) {
      return <a href={url} target="_blank" rel="noreferrer">
        <img className="chat-media" src={url} alt={item.name} title={item.name} /></a>;
    }
    if (mime.startsWith("audio/")) {
      return <span className="chat-media-row">🎵 {item.name}
        <audio controls src={url} /></span>;
    }
    return <video className="chat-media" controls src={url} title={item.name} />;
  }
  return <button className="ghost" onClick={open}>📎 {item.name}</button>;
}
