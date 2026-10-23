import { useEffect, useState } from "react";
import { api, fileUrl, type Results } from "../api";
import { Notice } from "./common";
import { fmtNum, fmtP } from "../format";

type Kind = "gsea" | "ora_up" | "ora_down";

export default function PathwaysTab({ jobId, r }: { jobId: string; r: Results }) {
  const en = r.enrichment;
  const p = r.resolved_params;
  const collections = Object.entries(en?.collections ?? {});
  const [collection, setCollection] = useState(collections[0]?.[0] ?? "");
  const [kind, setKind] = useState<Kind>("gsea");
  const [rows, setRows] = useState<Record<string, string>[]>([]);
  const [figure, setFigure] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const folder = en?.collections?.[collection]?.folder;

  useEffect(() => {
    if (!folder) return;
    setError(null);
    api.enrichment(jobId, folder, kind)
      .then((d) => { setRows(d.rows); setFigure(d.figure); })
      .catch((e) => { setRows([]); setFigure(null); setError((e as Error).message); });
  }, [jobId, folder, kind]);

  if (!en || en.status !== "done") {
    return <Notice kind="warning">Pathway analysis was not run: {en?.reason ?? "no enrichment results in this run."}</Notice>;
  }
  const cutoff = 0.05;
  const significant = rows.filter((x) => Number(x.padj) < cutoff);

  return (
    <section className="card">
      <h2>Pathway enrichment</h2>
      <p className="muted small">
        <strong>GSEA</strong> uses all tested genes ranked from most higher to most lower in {p.test_level}; a positive NES means
        the pathway's genes tend to be higher in {p.test_level}. <strong>Over-representation</strong> asks whether a pathway has
        more significant genes than expected by chance, separately for genes higher and lower in {p.test_level}.
      </p>
      <div className="row" style={{ marginBottom: 12 }}>
        <select value={collection} onChange={(e) => setCollection(e.target.value)} style={{ maxWidth: 260 }} aria-label="Gene-set collection">
          {collections.map(([k, c]) => <option key={k} value={k}>{k} ({c.source})</option>)}
        </select>
        <select value={kind} onChange={(e) => setKind(e.target.value as Kind)} style={{ maxWidth: 300 }} aria-label="Analysis">
          <option value="gsea">GSEA (all genes)</option>
          <option value="ora_up">Over-representation: genes higher in {p.test_level}</option>
          <option value="ora_down">Over-representation: genes lower in {p.test_level}</option>
        </select>
        <span className="muted small">{significant.length} pathways with adjusted p &lt; {cutoff}</span>
      </div>
      {error && <Notice kind="warning">No results for this choice (for example, too few significant genes).</Notice>}
      {figure && (
        <figure className="figure" style={{ maxWidth: 760 }}>
          <img src={fileUrl(jobId, figure)} alt="Pathway enrichment plot" />
        </figure>
      )}
      {rows.length > 0 && (
        <div className="table-wrap" style={{ maxHeight: 520, marginTop: 12 }}>
          <table>
            <thead>
              <tr>
                <th>Pathway</th>
                {kind === "gsea" ? <th className="num">NES</th> : <th className="num">Genes in overlap</th>}
                {kind !== "gsea" && <th className="num">Fold enrichment</th>}
                <th className="num">Set size</th>
                <th className="num">adj. p-value</th>
                <th>{kind === "gsea" ? "Leading-edge genes" : "Genes"}</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 100).map((x) => {
                const genes = (kind === "gsea" ? x.leading_edge_genes : x.overlap_genes) ?? "";
                return (
                  <tr key={x.pathway}>
                    <td title={x.pathway}>{x.pathway.replace(/_/g, " ")}</td>
                    {kind === "gsea"
                      ? <td className={`num ${Number(x.NES) > 0 ? "up" : "down"}`}>{fmtNum(Number(x.NES))}</td>
                      : <td className="num">{x.n_overlap}</td>}
                    {kind !== "gsea" && <td className="num">{fmtNum(Number(x.fold_enrichment), 1)}</td>}
                    <td className="num">{x.set_size}</td>
                    <td className="num">{fmtP(Number(x.padj))}</td>
                    <td className="muted small" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }} title={genes}>{genes}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
