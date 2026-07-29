import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { del, openLoginSocket, post } from "./api";

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

export default function LoginWizard({ title, executorType, accountId, onClose }: {
  title: string; executorType: string; accountId: string | null;
  onClose: () => void;
}) {
  const [command, setCommand] = useState("");
  const [done, setDone] = useState<number | null | "running">("running");
  const [error, setError] = useState("");
  const [paste, setPaste] = useState("");
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
    term.write("\x1b[90m[bastet] 連線中…\x1b[0m\r\n");  // proves the terminal renders

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
              setError(`終端寫入失敗：${String(e)}`);
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
  }, [executorType, accountId]);

  const sendPaste = () => {
    socket.current?.send(paste + "\r");
    setPaste("");
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wizard" onClick={(e) => e.stopPropagation()}>
        <h2>登入：{title}</h2>
        {command && <p className="card-meta"><code>{command}</code></p>}
        {error && <p className="error">{error}</p>}
        <div ref={container} className="xterm-host" />
        {done === "running" && (
          <div className="row keypad">
            <span className="muted">按鍵：</span>
            {KEYS.map((k) => (
              <button key={k.label} className="ghost"
                      onClick={() => socket.current?.send(k.seq)}>{k.label}</button>
            ))}
            <input placeholder="貼上代碼後按送出" value={paste} style={{ flex: 1 }}
                   onChange={(e) => setPaste(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && sendPaste()} />
            <button onClick={sendPaste} disabled={!paste}>送出</button>
          </div>
        )}
        <div className="row">
          {done !== "running" && (
            <p className="notice">{done === 0 ? "✅ 登入流程結束（成功）"
              : `流程結束（exit ${done}）— 若未完成可重試`}</p>
          )}
          <button className="ghost" onClick={onClose}>
            {done === "running" ? "取消" : "關閉"}</button>
        </div>
        <p className="muted">點一下黑框即可直接打字/方向鍵/Enter；也可用上面的按鍵按鈕。
          出現的網址可直接點開，代碼貼進輸入框送出。</p>
      </div>
    </div>
  );
}
