import { useCallback, useEffect, useState } from "react";
import { api, getToken, setToken, openEventSocket, Me } from "./api";
import AdminPage from "./pages/Admin";
import AuditPage from "./pages/Audit";
import BoardPage from "./pages/Board";
import MemoryPage from "./pages/Memory";
import ProjectPage from "./pages/ProjectPage";
import OrgPage from "./pages/Org";
import ResourcesPage from "./pages/Resources";
import TemplatesPage from "./pages/Templates";

const ROLE_RANK: Record<string, number> = { viewer: 0, operator: 1, admin: 2 };

type Tab = { key: string; label: string; minRole: string };
const TABS: Tab[] = [
  { key: "board", label: "看板", minRole: "viewer" },
  { key: "project", label: "專案", minRole: "viewer" },
  { key: "resources", label: "資源", minRole: "viewer" },
  { key: "org", label: "組織", minRole: "viewer" },
  { key: "templates", label: "模板", minRole: "viewer" },
  { key: "memory", label: "記憶", minRole: "viewer" },
  { key: "admin", label: "管理", minRole: "admin" },
  { key: "audit", label: "稽核", minRole: "viewer" },
];

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  useEffect(() => {
    api<Me>("/api/me").then(setMe).catch(() => setMe(null));
  }, []);
  if (me === undefined) return <div className="center">…</div>;
  if (me === null) return <TokenGate onOk={setMe} />;
  return <Workbench me={me} />;
}

function TokenGate({ onOk }: { onOk: (me: Me) => void }) {
  const [value, setValue] = useState(getToken());
  const [error, setError] = useState("");
  const submit = async () => {
    setToken(value.trim());
    try {
      onOk(await api<Me>("/api/me"));
    } catch {
      setError("token rejected — check ~/.bastet/api_token");
    }
  };
  return (
    <div className="center">
      <div className="token-card">
        <h1>🐈 Bastet</h1>
        <p>Paste your API token (<code>~/.bastet/api_token</code> or a user token):</p>
        <input type="password" value={value} autoFocus
               onChange={(e) => setValue(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && submit()} />
        <button onClick={submit}>Connect</button>
        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}

function Workbench({ me }: { me: Me }) {
  const [tab, setTab] = useState("board");
  const [projects, setProjects] = useState<{ id: string }[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [feed, setFeed] = useState<string[]>([]);

  const rank = ROLE_RANK[me.role] ?? 0;
  const canOperate = rank >= 1;
  const isAdmin = rank >= 2;

  const loadProjects = useCallback(() => {
    api<{ id: string }[]>("/api/projects").then((rows) => {
      setProjects(rows);
      setProjectId((current) => current ?? rows[0]?.id ?? null);
    });
  }, []);
  useEffect(loadProjects, [loadProjects, refreshKey]);

  useEffect(() => {
    return openEventSocket(null, (event) => {
      if (event.type === "hello") return;
      setFeed((old) => [
        `${String(event.at ?? "").slice(11, 19)} ${event.type} ${event.job_id ?? event.run_id ?? ""}`,
        ...old.slice(0, 7),
      ]);
      setRefreshKey((k) => k + 1);
    });
  }, []);

  return (
    <div className="app">
      <header>
        <h1>🐈 Bastet Agent OS</h1>
        <nav>
          {TABS.filter((t) => rank >= ROLE_RANK[t.minRole]).map((t) => (
            <button key={t.key} className={tab === t.key ? "tab active" : "tab"}
                    onClick={() => setTab(t.key)}>{t.label}</button>
          ))}
        </nav>
        {tab === "board" && (
          <select value={projectId ?? ""} onChange={(e) => setProjectId(e.target.value)}>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
          </select>
        )}
        <span className="me">{me.name} · {me.role}</span>
        <button className="ghost" onClick={() => setRefreshKey((k) => k + 1)}>↻</button>
      </header>

      {tab === "board" && (projectId
        ? <BoardPage projectId={projectId} refreshKey={refreshKey} canOperate={canOperate} />
        : <div className="page"><p className="muted">尚無專案 —
            到「組織」頁建立第一個專案。</p></div>)}
      {tab === "project" && <ProjectPage canOperate={canOperate} refreshKey={refreshKey} />}
      {tab === "resources" && <ResourcesPage isAdmin={isAdmin} refreshKey={refreshKey} />}
      {tab === "org" && <OrgPage canOperate={canOperate} refreshKey={refreshKey} />}
      {tab === "templates" && <TemplatesPage canOperate={canOperate} refreshKey={refreshKey} />}
      {tab === "memory" && <MemoryPage />}
      {tab === "admin" && isAdmin && <AdminPage refreshKey={refreshKey} />}
      {tab === "audit" && <AuditPage refreshKey={refreshKey} />}

      <footer>
        {feed.map((line, i) => <div key={i} className="feed-line">{line}</div>)}
      </footer>
    </div>
  );
}
