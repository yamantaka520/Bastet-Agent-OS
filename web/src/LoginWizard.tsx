import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { del, openLoginSocket, post, put } from "./api";
import { useT } from "./i18n";
import { onEnterSubmit } from "./ui";

/** Guided login: the executor's login command runs in a server-side PTY and
 *  this is a REAL terminal for it (xterm.js) — arrow keys, Enter, full TUI
 *  rendering, clickable URLs. The command is fixed server-side: no shell. */

const KEYS = [
  { label: "Enter", seq: "\r" },
  { label: "↑", seq: "\x1b[A" },
  { label: "↓", seq: "\x1b[B" },
  { label: "Esc", seq: "\x1b" },
  { label: "Ctrl+C", seq: "\x03" },
];

export default function LoginWizard({ title, executorType, accountId, agentId,
                                      currentModel, onClose }: {
  title: string; executorType: string; accountId: string | null;
  agentId?: string | null; currentModel?: string;
  onClose: () => void;
}) {
  const t = useT();
  const [command, setCommand] = useState("");
  const [done, setDone] = useState<number | null | "running">("running");
  const [error, setError] = useState("");
  const [paste, setPaste] = useState("");
  const [model, setModel] = useState(currentModel ?? "");
  const [modelSaved, setModelSaved] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const socket = useRef<ReturnType<typeof openLoginSocket> | null>(null);
  const sessionId = useRef<string | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const term = new Terminal({
      cols: 100, rows: 30, convertEol: false, cursorBlink: true,
      fontSize: 13, theme: { background: "#0c0f13" },
    });
    term.loadAddon(new WebLinksAddon((_e, uri) => window.open(uri, "_blank")));
    term.open(container.current);  // fixed 100x30 — matches the PTY winsize
    term.write(`\x1b[90m[bastet] ${t("lw.connecting")}\x1b[0m\r\n`);  // proves it renders

    let closed = false;
    post<{ id: string; command: string }>("/api/login-sessions",
        { executor_type: executorType, account_id: accountId })
      .then((session) => {
        if (closed) return;
        sessionId.current = session.id;
        setCommand(session.command);
        socket.current = openLoginSocket(session.id,
          (text) => {
            try {
              term.write(text);
            } catch (e) {
              setError(t("lw.writeFailed", { err: String(e) }));
            }
          },
          (exitCode) => setDone(exitCode));
        // every keystroke (arrows, Enter=\r, ctrl…) goes straight to the PTY
        term.onData((data) => socket.current?.send(data));
        term.focus();
      })
      .catch((e) => setError(String((e as Error).message)));

    return () => {
      closed = true;
      socket.current?.close();
      if (sessionId.current) del(`/api/login-sessions/${sessionId.current}`).catch(() => {});
      term.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [executorType, accountId]);

  const sendPaste = () => {
    socket.current?.send(paste + "\r");
    setPaste("");
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wizard" onClick={(e) => e.stopPropagation()}>
        <h2>{t("lw.title", { name: title })}</h2>
        {command && <p className="card-meta"><code>{command}</code></p>}
        {error && <p className="error">{error}</p>}
        {agentId && (
          <div className="inline-form">
            <label>{t("org.modelDefault")}</label>
            <input value={model} style={{ width: "22rem" }}
                   placeholder={t("org.modelDefault")}
                   onChange={(e) => { setModel(e.target.value); setModelSaved(false); }} />
            <button className="ghost" onClick={async () => {
              setError("");
              try {
                await put(`/api/agents/${encodeURIComponent(agentId)}`, { model });
                setModelSaved(true);
              } catch (e) { setError(String((e as Error).message)); }
            }}>{t("c.save")}</button>
            {modelSaved && <span className="notice">✓</span>}
          </div>
        )}
        <div ref={container} className="xterm-host" />
        {done === "running" && (
          <div className="row keypad">
            <span className="muted">{t("lw.keys")}</span>
            {KEYS.map((k) => (
              <button key={k.label} className="ghost"
                      onClick={() => socket.current?.send(k.seq)}>{k.label}</button>
            ))}
            <input placeholder={t("lw.pastePh")} value={paste} style={{ flex: 1 }}
                   onChange={(e) => setPaste(e.target.value)}
                   onKeyDown={onEnterSubmit(sendPaste)} />
            <button onClick={sendPaste} disabled={!paste}>{t("c.send")}</button>
          </div>
        )}
        <div className="row">
          {done !== "running" && (
            <p className="notice">{done === 0 ? t("lw.doneOk")
              : t("lw.doneFail", { code: String(done) })}</p>
          )}
          <button className="ghost" onClick={onClose}>
            {done === "running" ? t("c.cancel") : t("c.close")}</button>
        </div>
        <p className="muted">{t("lw.hint")}</p>
      </div>
    </div>
  );
}
