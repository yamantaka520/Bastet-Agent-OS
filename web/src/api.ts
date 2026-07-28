export type Job = {
  id: string;
  project_id: string;
  template_id: string;
  title: string;
  stage: string;
  status: string;
  stages_snapshot_json: string;
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
  started_at: string | null;
  finished_at: string | null;
};

export type Gate = {
  gate_type: string;
  verdict: string;
  reviewer_kind: string;
  reviewer_id: string;
  detail_md: string;
  at: string;
};

export type Interaction = {
  request_id: string;
  kind: string;
  payload_json: string;
  status: string;
  created_at: string;
};

export type JobDetail = Job & {
  spec_md: string;
  worktree_path: string | null;
  runs: Run[];
  gates: Gate[];
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
