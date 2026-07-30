import { useState } from "react";
import { api } from "../api";
import { useT } from "../i18n";
import { Section, onEnterSubmit } from "../ui";

type Hit = { id: string; score: number; content: string; scope: string; type: string };

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
        <p className="muted">{t("mem.hint")}{" "}
          <a href="https://github.com/yamantaka520/Agent-Memory-OS"
             target="_blank" rel="noreferrer">Agent Memory OS</a></p>
      </Section>
    </div>
  );
}
