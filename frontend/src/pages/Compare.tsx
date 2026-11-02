import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, type ComparisonItem, type CompareRun, type StudySpec } from "../api";
import { Badge, Notice } from "../components/common";
import { fmtTime } from "../format";

interface RunChoice { selected: boolean; label: string; flip: boolean }
interface TableChoice { file: File | null; label: string; organism: string; comparison: string; flip: boolean }

const ORGANISMS = [
  { id: "auto", label: "Detect from the gene IDs" },
  { id: "human", label: "Human" },
  { id: "mouse", label: "Mouse" },
  { id: "other", label: "Other" },
];

export default function Compare() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const preselect = params.get("with");
  const [available, setAvailable] = useState<boolean | null>(null);
  const [runs, setRuns] = useState<CompareRun[]>([]);
  const [earlier, setEarlier] = useState<ComparisonItem[]>([]);
  const [choice, setChoice] = useState<Record<string, RunChoice>>({});
  const [tables, setTables] = useState<TableChoice[]>([]);
  const [title, setTitle] = useState("");
  const [padj, setPadj] = useState("0.05");
  const [lfc, setLfc] = useState("1");
  const [pathways, setPathways] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.comparisons().then((r) => {
      setAvailable(r.available);
      setRuns(r.runs);
      setEarlier(r.comparisons);
      setChoice(Object.fromEntries(r.runs.map((x) => [x.id, { selected: x.id === preselect, label: x.label, flip: false }])));
    }).catch((e) => setError((e as Error).message));
  }, [preselect]);

  const selectedRuns = runs.filter((r) => choice[r.id]?.selected);
  const readyTables = tables.filter((t) => t.file);
  const n = selectedRuns.length + readyTables.length;
  const setRun = (id: string, patch: Partial<RunChoice>) => setChoice((c) => ({ ...c, [id]: { ...c[id], ...patch } }));
  const setTable = (i: number, patch: Partial<TableChoice>) => setTables((ts) => ts.map((t, j) => (j === i ? { ...t, ...patch } : t)));

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true); setError(null);
    const files: File[] = [];
    const studies: StudySpec[] = selectedRuns.map((r) => ({ kind: "run", job_id: r.id, label: choice[r.id].label, flip: choice[r.id].flip }));
    for (const t of readyTables) {
      files.push(t.file!);
      studies.push({ kind: "table", file: files.length - 1, label: t.label, organism: t.organism, comparison: t.comparison, flip: t.flip });
    }
    try {
      const c = await api.startComparison({ title: title.trim() || undefined, studies, padj_cutoff: Number(padj),
        log2fc_cutoff: Number(lfc), compare_pathways: pathways }, files);
      navigate(`/compare/${c.id}`);
    } catch (e) { setError((e as Error).message); setBusy(false); }
  }

  if (available === false) {
    return <Notice kind="info">The code bank in use has no compare module (it arrived in code bank v0.5.0). Update the code bank to compare studies.</Notice>;
  }
  return (
    <>
      <h1>Compare studies</h1>
      <p className="muted">
        Do two studies agree? Pick finished analyses (your own, or public datasets imported from GEO) and, if you like, published
        results tables. Each study keeps its own analysis. The comparison uses their <strong>results</strong>, not merged counts: how well
        the fold changes agree, which genes are significant in both and in which direction, a threshold-free rank-rank overlap
        (RRHO), and which pathways are shared. Human and mouse studies are matched through orthologs.
      </p>
      {error && <Notice kind="fail">{error}</Notice>}
      <form onSubmit={submit}>
        <section className="card">
          <h2>Analyses on this site</h2>
          {available === null && <p><span className="spinner" /> Loading…</p>}
          {available && !runs.length && (
            <p className="muted">No finished analyses yet. <Link to="/">Run one</Link> (or import a GEO dataset) first.</p>
          )}
          {runs.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th></th><th>Analysis</th><th>Label in the comparison</th><th>Flip direction</th></tr></thead>
                <tbody>
                  {runs.map((r) => {
                    const c = choice[r.id];
                    if (!c) return null;
                    return (
                      <tr key={r.id}>
                        <td><input type="checkbox" checked={c.selected} aria-label={`Compare ${r.label}`} onChange={(e) => setRun(r.id, { selected: e.target.checked })} /></td>
                        <td><Link to={`/runs/${r.id}`}>{r.comparison}</Link><div className="muted small">{r.dataset} · {fmtTime(r.created)}</div></td>
                        <td><input type="text" value={c.label} maxLength={40} disabled={!c.selected} onChange={(e) => setRun(r.id, { label: e.target.value })} /></td>
                        <td><input type="checkbox" checked={c.flip} disabled={!c.selected} aria-label="Flip direction"
                          onChange={(e) => setRun(r.id, { flip: e.target.checked })} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted small">"Flip direction" reverses a study's fold changes, for a study that compared the groups the other way
            round (WT vs KO instead of KO vs WT), so that "up" means the same thing in every study.</p>
        </section>

        <section className="card">
          <div className="row"><h2 style={{ margin: 0 }}>Published results tables</h2><span className="spacer" />
            <button type="button" className="secondary" onClick={() => setTables((t) => [...t, { file: null, label: "", organism: "auto", comparison: "", flip: false }])}>
              Add a table</button></div>
          <p className="muted small">A paper's differential-expression table (.csv, .tsv or .xlsx) with a gene column, a log2 fold change and an
            adjusted p value. Column names from DESeq2 (log2FoldChange, padj), edgeR (logFC, FDR) and limma (logFC, adj.P.Val) are recognised.</p>
          {tables.map((t, i) => (
            <div key={i} className="card" style={{ background: "var(--surface-2)" }}>
              <div className="grid-2">
                <label className="field"><span>Table</span>
                  <input type="file" accept=".csv,.tsv,.txt,.xlsx,.xls,.gz" onChange={(e) => {
                    const f = e.target.files?.[0] ?? null;
                    setTable(i, { file: f, label: t.label || (f ? f.name.replace(/\.[^.]+(\.gz)?$/, "").slice(0, 40) : "") });
                  }} /></label>
                <label className="field"><span>Label</span>
                  <input type="text" value={t.label} maxLength={40} onChange={(e) => setTable(i, { label: e.target.value })} placeholder="e.g. Smith2024" /></label>
                <label className="field"><span>What was compared</span>
                  <input type="text" value={t.comparison} maxLength={80} onChange={(e) => setTable(i, { comparison: e.target.value })} placeholder="e.g. KO vs WT" /></label>
                <label className="field"><span>Organism</span>
                  <select value={t.organism} onChange={(e) => setTable(i, { organism: e.target.value })}>
                    {ORGANISMS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                  </select></label>
              </div>
              <div className="row">
                <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={t.flip} onChange={(e) => setTable(i, { flip: e.target.checked })} />
                  Flip direction</label>
                <span className="spacer" />
                <button type="button" className="link" onClick={() => setTables((ts) => ts.filter((_, j) => j !== i))}>Remove</button>
              </div>
            </div>
          ))}
        </section>

        <section className="card">
          <h2>Settings</h2>
          <div className="grid-2">
            <label className="field"><span>Title (optional)</span>
              <input type="text" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Our KO vs published KO" /></label>
            <label className="field"><span>Adjusted p-value cutoff</span>
              <input type="number" step="any" min="0.001" max="1" value={padj} onChange={(e) => setPadj(e.target.value)} />
              <small>The same rule is used for every study, so the overlaps are comparable.</small></label>
            <label className="field"><span>Minimum |log2 fold change|</span>
              <input type="number" step="any" min="0" max="10" value={lfc} onChange={(e) => setLfc(e.target.value)} />
              <small>1 = a two-fold change.</small></label>
            <label className="field"><span>Pathways</span>
              <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={pathways} onChange={(e) => setPathways(e.target.checked)} />
                Compare pathway results (GSEA)</label></label>
          </div>
          <div className="row">
            <button type="submit" disabled={busy || n < 2 || n > 8}>
              {busy ? <><span className="spinner" /> Starting…</> : `Compare ${n || ""} ${n === 1 ? "study" : "studies"}`}
            </button>
            {n < 2 && <span className="muted small">Choose at least two studies (up to eight).</span>}
          </div>
        </section>
      </form>

      {earlier.length > 0 && (
        <section className="card">
          <h2>Earlier comparisons</h2>
          <ul className="check-list">
            {earlier.map((c) => (
              <li key={c.id}>
                <Badge status={c.status} />
                <Link to={`/compare/${c.id}`}>{c.title}</Link>
                <span className="muted small">{c.studies.length} studies · {fmtTime(c.created)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
