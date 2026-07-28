import { useCallback, useEffect, useState } from "react";
import { api, post } from "../api";
import { DataTable, InlineForm, Section, useList } from "../ui";

type Project = { id: string; team_id: string; repo_path: string | null };
type Agent = { id: string; name: string; executor_type: string; enabled: number };
type OrgView = {
  amos: boolean;
  local_only: string[];
  teams: { id: string; name: string; members: string[];
           projects: { id: string; members: string[]; bound: boolean }[] }[];
};

export default function OrgPage(props: { canOperate: boolean; refreshKey: number }) {
  const [projects, reloadProjects] = useList<Project>("/api/projects");
  const [agents, reloadAgents] = useList<Agent>("/api/agents");

  return (
    <div className="page">
      <Section title="Projects (1:1 with AMOS projects)">
        {props.canOperate && (
          <InlineForm
            fields={[{ name: "id", placeholder: "project id" },
                     { name: "repo", placeholder: "/path/to/repo", width: "20rem" },
                     { name: "team", placeholder: "team id (optional)" }]}
            submit="add"
            onSubmit={async (v) => {
              await post("/api/projects", { id: v.id, repo_path: v.repo,
                                            team_id: v.team || null });
              reloadProjects();
            }} />
        )}
        <DataTable head={["id", "team", "repo"]}
                   rows={projects.map((p) => [p.id, p.team_id, p.repo_path ?? "—"])} />
      </Section>

      <Section title="Agents (executor bindings)">
        {props.canOperate && (
          <InlineForm
            fields={[{ name: "id", placeholder: "agent id" },
                     { name: "name", placeholder: "display name" },
                     { name: "executor",
                       placeholder: "claude-code|claude-sdk|codex|hermes|grok|agy|bastet-lite",
                       width: "20rem" }]}
            submit="add"
            onSubmit={async (v) => {
              await post("/api/agents", { id: v.id, name: v.name || v.id,
                                          executor_type: v.executor || "claude-code" });
              reloadAgents();
            }} />
        )}
        <DataTable head={["id", "name", "executor", "enabled"]}
                   rows={agents.map((a) => [a.id, a.name, a.executor_type,
                                            a.enabled ? "✅" : "⛔"])} />
      </Section>

      <FederationSection canOperate={props.canOperate} onBound={reloadProjects} />

      <Section title="Role assignment (stage → agent matching)">
        {props.canOperate && (
          <InlineForm
            fields={[{ name: "project", placeholder: "project id" },
                     { name: "agent", placeholder: "agent id" },
                     { name: "role", placeholder: "engineer|reviewer|security-reviewer|pm" },
                     { name: "pref", placeholder: "preference (0)" }]}
            submit="assign"
            onSubmit={async (v) => {
              await post("/api/roles", { project_id: v.project, agent_id: v.agent,
                                         role: v.role, preference: Number(v.pref || 0) });
            }} />
        )}
        <p className="muted">Stages with a <code>role</code> pick the highest-preference
          agent holding that role in the project; otherwise the job's default agent runs.</p>
      </Section>
    </div>
  );
}

function FederationSection({ canOperate, onBound }:
  { canOperate: boolean; onBound: () => void }) {
  const [org, setOrg] = useState<OrgView | null>(null);
  const [binding, setBinding] = useState<string | null>(null);
  const [repo, setRepo] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<OrgView>("/api/org").then(setOrg).catch(() => setOrg(null));
  }, []);
  useEffect(load, [load]);

  const bind = async (projectId: string) => {
    setError("");
    try {
      await post("/api/org/bind", { project_id: projectId, repo_path: repo });
      setBinding(null);
      setRepo("");
      load();
      onBound();
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  return (
    <Section title="Federation — shared AMOS org view">
      {!org?.amos && <p className="muted">AMOS unavailable — org view offline.</p>}
      {org?.teams.map((team) => (
        <div key={team.id} className="fed-team">
          <b>🏷 {team.name}</b>
          <span className="card-meta"> members: {team.members.join(", ") || "—"}</span>
          <ul>
            {team.projects.map((p) => (
              <li key={p.id}>
                {p.bound ? "🔗" : "◌"} {p.id}
                <span className="card-meta"> ({p.members.length} members)</span>
                {!p.bound && canOperate && (
                  binding === p.id ? (
                    <span className="inline-form" style={{ marginLeft: ".5rem" }}>
                      <input placeholder="/path/to/local/repo" value={repo}
                             onChange={(e) => setRepo(e.target.value)} />
                      <button onClick={() => bind(p.id)} disabled={!repo}>bind</button>
                      <button className="ghost" onClick={() => setBinding(null)}>✕</button>
                    </span>
                  ) : (
                    <button className="ghost" onClick={() => setBinding(p.id)}>bind…</button>
                  )
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {!!org?.local_only.length && (
        <p className="muted">local-only projects (no AMOS record): {org.local_only.join(", ")}</p>
      )}
      <p className="muted">Teams/projects/members converge across nodes via AMOS
        federation; a project synced from another node shows here as ◌ until you
        bind it to a local repo. Resources, grants and jobs stay per-node.</p>
    </Section>
  );
}
