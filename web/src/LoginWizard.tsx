import { useEffect, useRef, useState } from "react";
import { del, openLoginSocket, post } from "./api";

/** Guided login terminal: runs the executor's login command in a server-side
 *  PTY and bridges it here — device codes and URLs appear inline, paste-back
 *  flows type into the input box. No shell access: the command is fixed. */

const ANSI = /\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][0-9A-B]|[\x00-\x08\x0b-\x1f]/g;

function linkify(text: string): React.ReactNode[] {
  const parts = text.split(/(https?:\/\/[^\s]+)/g);
  return parts.map((part, i) =>
    part.startsWith("http")
      ? <a key={i} href={part} target="_blank" rel="noreferrer">{part}</a>
      : <span key={i}>{part}</span>);
}

export default function LoginWizard({ title, executorType, accountId, onClose }: {
  title: string; executorType: string; accountId: string | null;
  onClose: () => void;
}) {
  const [output, setOutput] = useState("");
  const [command, setCommand] = useState("");
  const [input, setInput] = useState("");
  const [done, setDone] = useState<number | null | "running">("running");
  const [error, setError] = useState("");
  const socket = useRef<ReturnType<typeof openLoginSocket> | null>(null);
  const sessionId = useRef<string | null>(null);
  const pre = useRef<HTMLPreElement>(null);

  useEffect(() => {
    let closed = false;
    post<{ id: string; command: string }>("/api/login-sessions",
        { executor_type: executorType, account_id: accountId })
      .then((session) => {
        if (closed) return;
        sessionId.current = session.id;
        setCommand(session.command);
        socket.current = openLoginSocket(session.id,
          (text) => setOutput((old) => (old + text).slice(-20000)),
          (exitCode) => setDone(exitCode));
      })
      .catch((e) => setError(String((e as Error).message)));
    return () => {
      closed = true;
      socket.current?.close();
      if (sessionId.current) del(`/api/login-sessions/${sessionId.current}`).catch(() => {});
    };
  }, [executorType, accountId]);

  useEffect(() => {
    pre.current?.scrollTo(0, pre.current.scrollHeight);
  }, [output]);

  const send = () => {
    socket.current?.send(input + "\n");
    setInput("");
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wizard" onClick={(e) => e.stopPropagation()}>
        <h2>登入：{title}</h2>
        {command && <p className="card-meta"><code>{command}</code></p>}
        {error && <p className="error">{error}</p>}
        <pre ref={pre} className="terminal">
          {linkify(output.replace(ANSI, ""))}
        </pre>
        {done === "running" ? (
          <div className="row">
            <input placeholder="需要輸入時在此打字（Enter 送出）" value={input}
                   style={{ flex: 1 }}
                   onChange={(e) => setInput(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && send()} />
            <button onClick={send}>送出</button>
            <button className="ghost" onClick={onClose}>取消</button>
          </div>
        ) : (
          <div className="row">
            <p className="notice">{done === 0 ? "✅ 登入流程結束（成功）"
              : `流程結束（exit ${done}）— 若未完成可重試`}</p>
            <button onClick={onClose}>關閉</button>
          </div>
        )}
        <p className="muted">流程中出現的網址可直接點開（手機掃碼/任何瀏覽器皆可），
          需要貼回代碼的流程就貼在輸入框。</p>
      </div>
    </div>
  );
}
