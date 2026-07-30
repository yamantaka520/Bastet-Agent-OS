import { useCallback, useEffect, useState } from "react";
import { api, getToken, setToken, openEventSocket, Me } from "./api";
import { LanguagePicker, useT } from "./i18n";
import AdminPage from "./pages/Admin";
import AuditPage from "./pages/Audit";
import BoardPage from "./pages/Board";
import MemoryPage from "./pages/Memory";
import ProjectPage from "./pages/ProjectPage";
import OrgPage from "./pages/Org";
import ResourcesPage from "./pages/Resources";
import TemplatesPage from "./pages/Templates";

const ROLE_RANK: Record<string, number> = { viewer: 0, operator: 1, admin: 2 };

type Tab = { key: string; minRole: string };
const TABS: Tab[] = [
  { key: "board", minRole: "viewer" },
  { key: "project", minRole: "viewer" },
  { key: "resources", minRole: "viewer" },
  { key: "org", minRole: "viewer" },
  { key: "templates", minRole: "viewer" },
  { key: "memory", minRole: "viewer" },
  { key: "admin", minRole: "admin" },
  { key: "audit", minRole: "viewer" },
];

/** Version comes from the server (unauthenticated) so it is the version that
 *  is actually running, not what the bundle was built against. */
function useVersion(): string {
  const [version, setVersion] = useState("");
  useEffect(() => {
    api<{ version: string }>("/api/version")
      .then((v) => setVersion(v.version)).catch(() => {});
  }, []);
  return version;
}

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
  const t = useT();
  const version = useVersion();
  const [value, setValue] = useState(getToken());
  const [error, setError] = useState("");
  const submit = async () => {
    setToken(value.trim());
    try {
      onOk(await api<Me>("/api/me"));
    } catch {
      setError(t("app.tokenRejected"));
    }
  };
  return (
    <div className="center">
      <div className="token-card">
        <h1>🐈 Bastet {version && <small className="ver">v{version}</small>}</h1>
        <p>{t("app.tokenPrompt")}<br />
          <code>{t("app.tokenHint")}</code></p>
        <input type="password" value={value} autoFocus
               onChange={(e) => setValue(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && submit()} />
        <button onClick={submit}>{t("app.connect")}</button>
        {error && <p className="error">{error}</p>}
        <div className="row"><LanguagePicker /></div>
      </div>
    </div>
  );
}

function Workbench({ me }: { me: Me }) {
  const t = useT();
  const version = useVersion();
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
        <h1>🐈 Bastet Agent OS
          {version && <small className="ver">v{version}</small>}</h1>
        <nav>
          {TABS.filter((x) => rank >= ROLE_RANK[x.minRole]).map((x) => (
            <button key={x.key} className={tab === x.key ? "tab active" : "tab"}
                    onClick={() => setTab(x.key)}>{t(`tab.${x.key}`)}</button>
          ))}
        </nav>
        {tab === "board" && (
          <select value={projectId ?? ""} onChange={(e) => setProjectId(e.target.value)}>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
          </select>
        )}
        <span className="me">{me.name} · {me.role}</span>
        <LanguagePicker />
        <button className="ghost" title={t("app.refresh")}
                onClick={() => setRefreshKey((k) => k + 1)}>↻</button>
      </header>

      {tab === "board" && (projectId
        ? <BoardPage projectId={projectId} refreshKey={refreshKey} canOperate={canOperate} />
        : <div className="page"><p className="muted">{t("app.noProjects")}</p></div>)}
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
