import { useState } from "react";
import { api } from "../api";
import { Section } from "../ui";

type Hit = { id: string; score: number; content: string; scope: string; type: string };

export default function MemoryPage() {
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
      <Section title="Team memory — Agent Memory OS">
        <div className="inline-form">
          <input placeholder="搜尋團隊記憶（keyword 或關聯召回）" value={query}
                 style={{ width: "24rem" }}
                 onChange={(e) => setQuery(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && query && search()} />
          <button onClick={search} disabled={!query}>search</button>
        </div>
        {error && <p className="error">{error}</p>}
        {hits !== null && hits.length === 0 && <p className="muted">no matches</p>}
        {hits?.map((h) => (
          <article key={h.id} className="memory-hit">
            <div className="card-meta">
              {h.type} · {h.scope} · score {Number(h.score).toFixed(3)}
            </div>
            <div>{h.content}</div>
          </article>
        ))}
        <p className="muted">記憶由 <a href="https://github.com/yamantaka520/Agent-Memory-OS"
          target="_blank" rel="noreferrer">Agent Memory OS</a> 提供：ACL 過濾後的
          team/project 記憶；每個 run 的 context 也經由它的 context pack 組裝
          （見 audit 的 context.assembled 事件）。完整管理介面用
          <code>agent-memory-web</code>。</p>
      </Section>
    </div>
  );
}
