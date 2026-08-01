import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useT, type T } from "../i18n";
import { DataTable, Section, onEnterSubmit, fmtTime } from "../ui";

/** The audit trail is the record of every state change; a log you cannot search
 *  is one nobody reads, so the filters come first and the rows follow. */

type AuditRow = { at: string; actor: string; action: string;
                  target_type: string; target_id: string; detail_json: string };
type Body = { rows: AuditRow[]; count: number; categories: string[];
              actions: string[] };

export default function AuditPage(props: { refreshKey: number }) {
  const t = useT();
  const [query, setQuery] = useState({ q: "", action: "", actor: "",
                                       since: "", until: "", limit: 100 });
  const [body, setBody] = useState<Body | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    const params = new URLSearchParams({ limit: String(query.limit) });
    if (query.q) params.set("q", query.q);
    if (query.action) params.set("action", query.action);
    if (query.actor) params.set("actor", query.actor);
    if (query.since) params.set("since", query.since);
    if (query.until) params.set("until", query.until);
    api<Body>(`/api/audit?${params.toString()}`)
      .then(setBody).catch((e) => setError(String((e as Error).message)));
  }, [query]);
  useEffect(load, [load, props.refreshKey]);

  return (
    <div className="page">
      <Section title={t("aud.title")}>
        <div className="inline-form">
          <input placeholder={t("aud.searchPh")} value={query.q} style={{ flex: 1 }}
                 onChange={(e) => setQuery({ ...query, q: e.target.value })}
                 onKeyDown={onEnterSubmit(load)} />
          <label className="res-field">
            <span>{t("aud.category")}</span>
            <select value={query.action}
                    onChange={(e) => setQuery({ ...query, action: e.target.value })}>
              <option value="">{t("proj.all")}</option>
              {(body?.categories ?? []).map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
          <label className="res-field">
            <span>{t("aud.actor")}</span>
            <input value={query.actor} style={{ width: "9rem" }}
                   onChange={(e) => setQuery({ ...query, actor: e.target.value })}
                   onKeyDown={onEnterSubmit(load)} />
          </label>
          <label className="res-field">
            <span>{t("aud.since")}</span>
            <input type="date" value={query.since}
                   onChange={(e) => setQuery({ ...query, since: e.target.value })} />
          </label>
          <label className="res-field">
            <span>{t("aud.until")}</span>
            <input type="date" value={query.until}
                   onChange={(e) => setQuery({ ...query, until: e.target.value })} />
          </label>
          <label className="res-field">
            <span>{t("aud.limit")}</span>
            <select value={query.limit}
                    onChange={(e) => setQuery({ ...query,
                                                limit: Number(e.target.value) })}>
              {[100, 300, 1000].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <button onClick={load}>{t("c.search")}</button>
          <button className="ghost"
                  onClick={() => setQuery({ q: "", action: "", actor: "", since: "",
                                            until: "", limit: 100 })}>
            {t("aud.clear")}</button>
        </div>
        {error && <p className="error">{error}</p>}
        <p className="muted">{t("aud.count", { n: body?.count ?? 0 })}</p>
        <AuditTable rows={body?.rows ?? []} t={t} />
      </Section>
    </div>
  );
}

function AuditTable({ rows, t }: { rows: AuditRow[]; t: T }) {
  return (
    <DataTable
      head={[t("aud.at"), t("aud.actor"), t("aud.action"), t("aud.target"),
             t("aud.detail")]}
      rows={rows.map((r) => [
        fmtTime(r.at, { seconds: true }),
        r.actor, r.action, `${r.target_type}:${r.target_id}`,
        <code className="detail">{r.detail_json}</code>,
      ])} />
  );
}
