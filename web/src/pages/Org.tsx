import { post } from "../api";
import { DataTable, InlineForm, Section, useList } from "../ui";

type Project = { id: string; team_id: string; repo_path: string | null };
type Agent = { id: string; name: string; executor_type: string; enabled: number };

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
                       placeholder: "claude-code|claude-sdk|codex|hermes|bastet-lite",
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
