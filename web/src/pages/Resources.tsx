import { api, post, UsageRow } from "../api";
import { DataTable, InlineForm, Section, useList } from "../ui";

type Resource = { id: string; kind: string; name: string; endpoint: string | null;
                  api_flavor: string | null; secret_ref: string; enabled: number };
type Grant = { id: string; resource_id: string; scope_type: string; scope_id: string;
               budget_usd: number | null; period: string; max_concurrency: number | null;
               on_exceed: string; enabled: number };

export default function ResourcesPage(props: { isAdmin: boolean; refreshKey: number }) {
  const [resources, reloadResources] = useList<Resource>("/api/resources");
  const [grants, reloadGrants] = useList<Grant>("/api/grants");
  const [usage] = useList<UsageRow>("/api/usage");

  const resourceName = (id: string) => resources.find((r) => r.id === id)?.name ?? id;
  const spentFor = (g: Grant) =>
    usage.filter((u) => (g.scope_type === "project" && u.project_id === g.scope_id)
                     || (g.scope_type === "agent" && u.agent_id === g.scope_id))
         .reduce((s, u) => s + (u.cost_usd ?? 0), 0);

  return (
    <div className="page">
      <Section title="Resources">
        {props.isAdmin && (
          <InlineForm
            fields={[{ name: "name", placeholder: "name" },
                     { name: "kind", placeholder: "kind (llm/image/tts/stt/secret)", width: "14rem" },
                     { name: "endpoint", placeholder: "endpoint base URL", width: "16rem" },
                     { name: "flavor", placeholder: "openai|anthropic" },
                     { name: "secret_ref", placeholder: "keyring:svc/name | env:NAME", width: "14rem" }]}
            submit="add"
            onSubmit={async (v) => {
              await post("/api/resources", { name: v.name, kind: v.kind || "llm",
                endpoint: v.endpoint || null, api_flavor: v.flavor || null,
                secret_ref: v.secret_ref || null });
              reloadResources();
            }} />
        )}
        <DataTable
          head={["name", "kind", "flavor", "endpoint", "secret", "enabled", ""]}
          rows={resources.map((r) => [
            r.name, r.kind, r.api_flavor ?? "—", r.endpoint ?? "—", r.secret_ref,
            r.enabled ? "✅" : "⛔",
            props.isAdmin ? (
              <button className="ghost" onClick={async () => {
                await post(`/api/resources/${r.id}/enabled`, { enabled: !r.enabled });
                reloadResources();
              }}>{r.enabled ? "disable" : "enable"}</button>
            ) : null,
          ])} />
      </Section>

      <Section title="Grants">
        {props.isAdmin && (
          <InlineForm
            fields={[{ name: "resource", placeholder: "resource name/id" },
                     { name: "scope", placeholder: "project:<id> | agent:<id> | team:<id>", width: "16rem" },
                     { name: "budget", placeholder: "budget USD" },
                     { name: "conc", placeholder: "max concurrency" }]}
            submit="grant"
            onSubmit={async (v) => {
              const resource = resources.find((r) => r.name === v.resource || r.id === v.resource);
              if (!resource) throw new Error("unknown resource");
              const [scopeType, scopeId] = (v.scope || "").split(":");
              await post("/api/grants", { resource_id: resource.id,
                scope_type: scopeType, scope_id: scopeId,
                budget_usd: v.budget ? Number(v.budget) : null,
                max_concurrency: v.conc ? Number(v.conc) : null });
              reloadGrants();
            }} />
        )}
        <DataTable
          head={["resource", "scope", "budget", "burn", "concurrency", "on_exceed"]}
          rows={grants.map((g) => {
            const spent = spentFor(g);
            const pct = g.budget_usd ? Math.min(100, (spent / g.budget_usd) * 100) : 0;
            return [
              resourceName(g.resource_id),
              `${g.scope_type}:${g.scope_id}`,
              g.budget_usd != null ? `$${g.budget_usd}` : "∞",
              g.budget_usd != null ? (
                <span className="burn">
                  <span className="burn-bar"><i style={{ width: `${pct}%` }} /></span>
                  ${spent.toFixed(4)}
                </span>
              ) : `$${spent.toFixed(4)}`,
              g.max_concurrency ?? "∞",
              g.on_exceed,
            ];
          })} />
      </Section>

      <Section title="Usage by project / agent">
        <DataTable
          head={["project", "agent", "precision", "runs", "in", "out", "cache read", "cost"]}
          rows={usage.map((u) => [
            u.project_id, u.agent_id, u.accounting_precision ?? "—", u.runs,
            u.tokens_in ?? 0, u.tokens_out ?? 0, u.cache_read ?? 0,
            `$${(u.cost_usd ?? 0).toFixed(4)}`,
          ])} />
      </Section>
    </div>
  );
}
