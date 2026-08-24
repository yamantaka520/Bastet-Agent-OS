export type Job = {
  id: string;
  project_id: string;
  template_id: string;
  title: string;
  stage: string;
  status: string;
  archived: number;
  stages_snapshot_json: string;
  rework_count?: number;         // how many times a gate sent this card back
  // liveness (in_progress only): the latest run's last words and when
  run_status?: string;
  heartbeat_at?: string | null;
  progress_text?: string | null;
  progress_at?: string | null;
  updated_at: string;
};

export type Run = {
  id: string;
  stage: string;
  attempt: number;
  agent_id: string;
  status: string;
  cost_usd: number;
  accounting_precision: string | null;
  error: string | null;          // why the stage failed, shown next to the retry
  started_at: string | null;
  finished_at: string | null;
};

export type Gate = {
  gate_type: string;
  verdict: string;
  reviewer_kind: string;
  reviewer_id: string;
  detail_md: string;
  config_error?: number;   // the gate could not run — a setting, not a test
  at: string;
};

export type Interaction = {
  request_id: string;
  kind: string;
  payload_json: string;
  status: string;
  created_at: string;
};

export type PmDecision = {
  pm: string; at: string;
  action: "retry" | "retry_other_agent" | "supply_then_retry" | "escalate" | null;
  reason: string | null; cycle?: number; max?: number;
};

export type JobDetail = Job & {
  spec_md: string;
  worktree_path: string | null;
  runs: Run[];
  gates: Gate[];
  // what the PM did — and, on an escalation, the question it needs answered
  pm_decision?: PmDecision | null;
};

export type UsageRow = {
  project_id: string;
  agent_id: string;
  accounting_precision: string | null;
  runs: number;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read: number | null;
  cost_usd: number | null;
};

export type Me = { user_id: string; name: string; role: string };

export function getToken(): string {
  return localStorage.getItem("bastet_token") ?? "";
}

export function setToken(token: string) {
  localStorage.setItem("bastet_token", token);
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${getToken()}`,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (resp.status === 401) throw new Error("unauthorized");
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail ?? body.error ?? `HTTP ${resp.status}`);
  }
  return resp.json();
}

export const post = <T,>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const put = <T,>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });

export const del = <T,>(path: string) => api<T>(path, { method: "DELETE" });

export function openLoginSocket(
  sessionId: string,
  onOutput: (text: string) => void,
  onDone: (exitCode: number | null) => void,
): { send: (input: string) => void; close: () => void } {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/login-sessions/${sessionId}/ws`);
  ws.onopen = () => ws.send(JSON.stringify({ token: getToken() }));
  ws.onmessage = (msg) => {
    let data: { output?: string; done?: boolean; exit_code?: number | null };
    try {
      data = JSON.parse(msg.data);
    } catch {
      return;  // ignore malformed frames — but NEVER swallow handler errors
    }
    if (data.output) onOutput(data.output);
    if (data.done) onDone(data.exit_code ?? null);
  };
  return {
    send: (input: string) => ws.send(JSON.stringify({ input })),
    close: () => ws.close(),
  };
}

export function openEventSocket(
  projectId: string | null,
  onEvent: (event: Record<string, unknown>) => void,
): () => void {
  let ws: WebSocket | null = null;
  let closed = false;

  const connect = () => {
    if (closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/ws`);
    ws.onopen = () => {
      // browser WS can't set headers; token goes in the first message, never the URL
      ws!.send(JSON.stringify({ token: getToken(), project_id: projectId }));
    };
    ws.onmessage = (msg) => {
      try {
        onEvent(JSON.parse(msg.data));
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      if (!closed) setTimeout(connect, 3000); // auto-reconnect
    };
  };

  connect();
  return () => {
    closed = true;
    ws?.close();
  };
}

/** Fetch a binary (preview image, artifact) with the auth header — a plain
 *  <img src> or <a href> cannot carry the token. */
export async function apiBlob(path: string): Promise<Blob> {
  const resp = await fetch(path, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.blob();
}
