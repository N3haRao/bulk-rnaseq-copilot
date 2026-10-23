import { useEffect, useState } from "react";
import { api, type DeTable, type Results } from "../api";
import { Figures, Notice } from "./common";
import { fmtInt, fmtNum, fmtP, geneLabel } from "../format";

type Direction = "all" | "significant" | "up" | "down";

export default function DeTab({ jobId, r }: { jobId: string; r: Results }) {
  const p = r.resolved_params;
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [direction, setDirection] = useState<Direction>("significant");
  const [sort, setSort] = useState<{ col: string; order: "asc" | "desc" | "abs" }>({ col: "padj", order: "asc" });
  const [page, setPage] = useState(1);
  const [data, setData] = useState<DeTable | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Searching for genes looks through all genes, not only the significant ones
  useEffect(() => {
    const t = window.setTimeout(() => { setQuery(search); setPage(1); }, 300);
    return () => window.clearTimeout(t);
  }, [search]);

  useEffect(() => {
    api.de(jobId, { search: query, direction: query ? "all" : direction, sort: sort.col, order: sort.order, page, page_size: 50 })
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError((e as Error).message));
  }, [jobId, query, direction, sort, page]);

  const sortBy = (col: string) => {
    setPage(1);
    if (col === "log2FoldChange") {
      const next = sort.col !== col ? "abs" : sort.order === "abs" ? "desc" : sort.order === "desc" ? "asc" : "abs";
      setSort({ col, order: next });
    } else {
      setSort({ col, order: sort.col === col && sort.order === "asc" ? "desc" : "asc" });
    }
  };
  const arrow = (col: string) => sort.col !== col ? "" : sort.order === "asc" ? " ▲" : sort.order === "desc" ? " ▼" : " |▼|";
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const refCol = `mean_normalized_${p.reference_level}`;
  const testCol = `mean_normalized_${p.test_level}`;
  const hasMeans = data?.columns.includes(refCol) && data?.columns.includes(testCol);

  return (
    <>
      <section className="card">
        <h2>Gene table</h2>
        <p className="muted small">{r.de?.sign_convention}. Fold changes are shrunken estimates ({r.de?.lfc_shrinkage_used}).</p>
        <div className="row" style={{ marginBottom: 12 }}>
          <input type="search" placeholder="Find genes, e.g. Gfap or FKBP5 PER1" value={search}
            onChange={(e) => setSearch(e.target.value)} style={{ maxWidth: 320 }} aria-label="Search genes" />
          <select value={direction} disabled={!!query} onChange={(e) => { setDirection(e.target.value as Direction); setPage(1); }}
            style={{ maxWidth: 260 }} aria-label="Which genes">
            <option value="significant">Significant genes</option>
            <option value="up">Higher in {p.test_level}</option>
            <option value="down">Lower in {p.test_level}</option>
            <option value="all">All tested genes</option>
          </select>
          <span className="muted small">{data && `${fmtInt(data.total)} genes`}</span>
        </div>
        {error && <Notice kind="fail">{error}</Notice>}
        {data && data.total === 0 && query && (
          <Notice kind="warning">
            No gene matches “{query}”. It may be named differently in your counts file (check the gene ID type), or it had too
            few reads and was removed by the low-count filter.
          </Notice>
        )}
        {data && data.rows.length > 0 && (
          <div className="table-wrap" style={{ maxHeight: 560 }}>
            <table>
              <thead>
                <tr>
                  <th className="sortable" onClick={() => sortBy("gene_symbol")}>Gene{arrow("gene_symbol")}</th>
                  <th>Description</th>
                  <th className="num sortable" onClick={() => sortBy("log2FoldChange")}>log2 fold change{arrow("log2FoldChange")}</th>
                  <th className="num sortable" onClick={() => sortBy("padj")}>adj. p-value{arrow("padj")}</th>
                  <th className="num sortable" onClick={() => sortBy("baseMean")}>Mean count{arrow("baseMean")}</th>
                  {hasMeans && <th className="num">Mean {p.reference_level}</th>}
                  {hasMeans && <th className="num">Mean {p.test_level}</th>}
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((g) => (
                  <tr key={g.gene_id}>
                    <td title={g.gene_id}>{geneLabel(g)}</td>
                    <td className="muted" style={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis" }} title={g.gene_name}>{g.gene_name || "–"}</td>
                    <td className={`num ${g.direction === "up" ? "up" : g.direction === "down" ? "down" : ""}`}>{fmtNum(g.log2FoldChange)}</td>
                    <td className="num" title={g.note || undefined}>{fmtP(g.padj)}{g.note ? " *" : ""}</td>
                    <td className="num">{fmtInt(g.baseMean)}</td>
                    {hasMeans && <td className="num">{fmtInt(Number(g[refCol]))}</td>}
                    {hasMeans && <td className="num">{fmtInt(Number(g[testCol]))}</td>}
                    <td>{g.direction === "up" ? <span className="up">higher</span> : g.direction === "down" ? <span className="down">lower</span> : <span className="muted">no change</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data && data.rows.some((g) => g.note) && (
          <p className="muted small">* No adjusted p-value: hover to see why (for example, too few reads or an outlier sample).</p>
        )}
        {pages > 1 && (
          <div className="row" style={{ marginTop: 10 }}>
            <button className="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
            <span className="muted small">Page {page} of {pages}</span>
            <button className="secondary" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        )}
      </section>
      <Figures jobId={jobId} figures={r.figures.de} />
    </>
  );
}
