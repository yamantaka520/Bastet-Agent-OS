import { DataTable, Section, useList } from "../ui";

type AuditRow = { at: string; actor: string; action: string;
                  target_type: string; target_id: string; detail_json: string };

export default function AuditPage(props: { refreshKey: number }) {
  const [rows] = useList<AuditRow>(`/api/audit?limit=200&_=${props.refreshKey}`);
  return (
    <div className="page">
      <Section title="Audit log (append-only, hash-chained)">
        <DataTable
          head={["at", "actor", "action", "target", "detail"]}
          rows={rows.map((r) => [
            r.at.replace("T", " ").replace("+00:00", "Z"),
            r.actor, r.action, `${r.target_type}:${r.target_id}`,
            <code className="detail">{r.detail_json}</code>,
          ])} />
      </Section>
    </div>
  );
}
