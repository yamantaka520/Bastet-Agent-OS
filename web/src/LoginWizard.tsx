import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { del, openLoginSocket, post } from "./api";

/** Guided login: the executor's login command runs in a server-side PTY and
 *  this is a REAL terminal for it (xterm.js) — arrow keys, Enter, full TUI
 *  rendering, clickable URLs. The command is fixed server-side: no shell. */

export default function LoginWizard({ title, executorType, accountId, onClose }: {
  title: string; executorType: string; accountId: string | null;
  onClose: () => void;
}) {
  const [command, setCommand] = useState("");
  const [done, setDone] = useState<number | null | "running">("running");
  const [error, setError] = useState("");
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

    let closed = false;
    post<{ id: string; command: string }>("/api/login-sessions",
        { executor_type: executorType, account_id: accountId })
      .then((session) => {
        if (closed) return;
        sessionId.current = session.id;
        setCommand(session.command);
        socket.current = openLoginSocket(session.id,
          (text) => term.write(text),
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
  }, [executorType, accountId]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wizard" onClick={(e) => e.stopPropagation()}>
        <h2>登入：{title}</h2>
        {command && <p className="card-meta"><code>{command}</code></p>}
        {error && <p className="error">{error}</p>}
        <div ref={container} className="xterm-host" />
        <div className="row">
          {done !== "running" && (
            <p className="notice">{done === 0 ? "✅ 登入流程結束（成功）"
              : `流程結束（exit ${done}）— 若未完成可重試`}</p>
          )}
          <button className="ghost" onClick={onClose}>
            {done === "running" ? "取消" : "關閉"}</button>
        </div>
        <p className="muted">這是完整終端：直接在黑框內打字/方向鍵/Enter 操作；
          出現的網址可直接點開。</p>
      </div>
    </div>
  );
}
