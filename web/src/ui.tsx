import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

/** List loader; re-fetches when `refreshKey` changes (WS events bump it). */
export function useList<T>(path: string, refreshKey?: number): [T[], () => void] {
  const [rows, setRows] = useState<T[]>([]);
  const reload = useCallback(() => {
    api<T[]>(path).then(setRows).catch(() => setRows([]));
  }, [path]);
  useEffect(reload, [reload, refreshKey]);
  return [rows, reload];
}

export function Section(props: { title: string; children: React.ReactNode;
                                 action?: React.ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{props.title}</h2>
        {props.action}
      </div>
      {props.children}
    </section>
  );
}

export function DataTable(props: { head: string[]; rows: React.ReactNode[][] }) {
  if (!props.rows.length) return <p className="muted">— empty —</p>;
  return (
    <div className="scroll-x">
      <table>
        <thead><tr>{props.head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>
          {props.rows.map((cells, i) => (
            <tr key={i}>{cells.map((c, j) => <td key={j}>{c}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Tiny inline form: a row of inputs + a submit button. */
export function InlineForm(props: {
  fields: { name: string; placeholder: string; width?: string }[];
  submit: string;
  onSubmit: (values: Record<string, string>) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    setError("");
    try {
      await props.onSubmit(values);
      setValues({});
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="inline-form">
      {props.fields.map((f) => (
        <input key={f.name} placeholder={f.placeholder} style={{ width: f.width }}
               value={values[f.name] ?? ""}
               onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />
      ))}
      <button disabled={busy} onClick={go}>{props.submit}</button>
      {error && <span className="error">{error}</span>}
    </div>
  );
}
