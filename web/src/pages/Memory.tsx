import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useT } from "../i18n";
import { Section, onEnterSubmit } from "../ui";

type Hit = { id: string; score: number; content: string; scope: string;
             type: string; visibility?: string[] };

type BrowseItem = { id: string; content: string; type: string; scope: string;
                    owner?: string; tags?: string[]; visibility?: string[];
                    created_at?: string };
type Browse = { items: BrowseItem[]; stats: Record<string, unknown>;
                console: { url: string; command: string; installed: boolean } };

export default function MemoryPage() {
  const t = useT();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [error, setError] = useState("");

  const search = async () => {
    setError("");
    try {
      setHits(await api<Hit[]>(`/api/memory/search?q=${encodeURIComponent(query)}`));
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  return (
    <div className="page">
      <Section title={t("mem.title")}>
        <div className="inline-form">
          <input placeholder={t("mem.searchPh")} value={query}
                 style={{ width: "24rem" }}
                 onChange={(e) => setQuery(e.target.value)}
                 onKeyDown={onEnterSubmit(() => query && search())} />
          <button onClick={search} disabled={!query}>{t("c.search")}</button>
        </div>
        {error && <p className="error">{error}</p>}
        {hits !== null && hits.length === 0 && <p className="muted">{t("c.noMatches")}</p>}
        {hits?.map((h) => (
          <article key={h.id} className="memory-hit">
            <div className="card-meta">
              {h.type} · {h.scope} · score {Number(h.score).toFixed(3)}
            </div>
            <div>{h.content}</div>
          </article>
        ))}
      </Section>

      <MemoryBrowse />

      <Section title={t("mem.about")}>
        <p className="muted">{t("mem.hint")}{" "}
          <a href="https://github.com/yamantaka520/Agent-Memory-OS"
             target="_blank" rel="noreferrer">Agent Memory OS</a></p>
      </Section>
    </div>
  );
}

/** Browsing, not just searching: you cannot search for what you do not know is
 *  there. Bastet shows the recent stream and hands off to the full AMOS console
 *  rather than reimplementing it badly. */
function MemoryBrowse() {
  const t = useT();
  const [body, setBody] = useState<Browse | null>(null);
  const [scope, setScope] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setError("");
    api<Browse>(`/api/memory/browse?limit=50${scope ? `&scope=${scope}` : ""}`)
      .then(setBody).catch((e) => setError(String((e as Error).message)));
  }, [scope]);
  useEffect(load, [load]);

  return (
    <Section title={t("mem.browse")}
             action={<button className="ghost" onClick={load}>{t("app.refresh")}</button>}>
      <div className="inline-form">
        <label className="res-field">
          <span>{t("chat.scope")}</span>
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="">{t("proj.all")}</option>
            <option value="project">{t("sec.labelProject")}</option>
            <option value="team">{t("sec.labelTeam")}</option>
            <option value="global">{t("sec.labelGlobal")}</option>
          </select>
        </label>
        {body?.console && (
          body.console.url
            ? <a className="ghost file-btn" href={body.console.url} target="_blank"
                 rel="noreferrer">{t("mem.openConsole")}</a>
            : <span className="muted">{t("mem.consoleHint",
                                        { command: body.console.command })}</span>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      {body && !body.items.length && <p className="muted">{t("mem.empty")}</p>}
      {body?.items.map((item) => (
        <article key={item.id} className="memory-hit">
          <div className="card-meta">
            {item.type} · {item.scope}
            {(item.visibility ?? []).length ? ` · 📁 ${item.visibility!.join("、")}`
                                            : ""}
            {item.owner ? ` · ${item.owner}` : ""}
            {item.created_at ? ` · ${item.created_at.replace("T", " ").slice(0, 16)}`
                             : ""}
          </div>
          <div>{item.content}</div>
        </article>
      ))}
    </Section>
  );
}
