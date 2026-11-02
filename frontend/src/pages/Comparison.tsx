import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, compareFileUrl, type ComparePair, type Comparison as ComparisonT } from "../api";
import { Badge, CheckList, Markdown, Notice, Tabs } from "../components/common";
import { fmtInt, fmtNum, fmtP, fmtTime } from "../format";

const RUNNING = new Set(["queued", "running"]);
type Tab = "overview" | "pairs" | "pathways" | "genes" | "methods";

export default function Comparison() {
  const { id = "" } = useParams();
  const [c, setC] = useState<ComparisonT | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");

  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const poll = async () => {
      try {
        const x = await api.comparison(id);
        if (stopped) return;
        setC(x);
        if (RUNNING.has(x.status)) timer = window.setTimeout(poll, 2500);
      } catch (e) { setError((e as Error).message); }
    };
    poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [id]);

  if (error) return <Notice kind="fail">{error}</Notice>;
  if (!c) return <p><span className="spinner" /> Loading…</p>;
  const done = c.status === "success" || c.status === "success_with_errors";
  return (
    <>
      <p className="small"><Link to="/compare">← Compare studies</Link></p>
      <div className="row"><h1 style={{ margin: 0 }}>{c.title}</h1><Badge status={c.status} /></div>
      <p className="muted small">
        {c.studies.length} studies · significant = padj &lt; {c.settings.padj_cutoff} and |log2FC| &gt; {c.settings.log2fc_cutoff} in
        every study · started {fmtTime(c.created)}
      </p>
      {c.error && <Notice kind="fail">{c.error}</Notice>}
      {(RUNNING.has(c.status) || !done) && <Progress c={c} />}
      {c.validation && c.status === "failed" && <CheckList checks={c.validation.checks} />}
      {done && (
        <>
          <div className="row" style={{ margin: "12px 0" }}>
            <a className="button" href={`/api/comparisons/${c.id}/report/html`} target="_blank" rel="noreferrer">Open report</a>
            <a className="button secondary" href={`/api/comparisons/${c.id}/report/pdf`}>Download PDF</a>
            <a className="button secondary" href={`/api/comparisons/${c.id}/download`}>All results (.zip)</a>
            <a className="button secondary" href={`/api/comparisons/${c.id}/code`}>Code used (.zip)</a>
          </div>
          {(c.warnings ?? []).map((w) => <Notice key={w} kind="warning">{w.replace(/^\[validation:[^\]]+\] /, "")}</Notice>)}
          <Tabs<Tab> active={tab} onChange={setTab} tabs={[
            { id: "overview", label: "Overview" }, { id: "pairs", label: "Pair by pair" },
            { id: "pathways", label: "Pathways" }, { id: "genes", label: "Genes in several studies" }, { id: "methods", label: "Methods" }]} />
          {tab === "overview" && <Overview c={c} />}
          {tab === "pairs" && <Pairs c={c} />}
          {tab === "pathways" && <Pathways c={c} />}
          {tab === "genes" && <Recurrent c={c} />}
          {tab === "methods" && (c.methods_md ? <Markdown text={c.methods_md} /> : <p className="muted">No methods text.</p>)}
        </>
      )}
    </>
  );
}

function Progress({ c }: { c: ComparisonT }) {
  return (
    <section className="card">
      {c.status === "queued" && <p className="muted">Waiting for another comparison to finish…</p>}
      <ul className="progress-list">
        {c.steps.map((st) => (
          <li key={st.name}>
            <span style={{ width: 90 }}>
              {st.status === "running" ? <><span className="spinner" /> running</> :
                <Badge status={st.status === "success" ? "pass" : st.status} label={st.status === "success" ? "done" : undefined} />}
            </span>
            <div><strong>{st.name.replace(/^C\d+_/, "").replace(/_/g, " ")}</strong><div className="muted small">{st.description}</div></div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Fig({ id, path, title, caption }: { id: string; path: string | null | undefined; title: string; caption: string }) {
  const [zoom, setZoom] = useState(false);
  if (!path) return null;
  return (
    <figure className="figure">
      <img src={compareFileUrl(id, path)} alt={title} loading="lazy" onClick={() => setZoom(true)} />
      <figcaption><strong>{title}</strong>{caption} <a href={compareFileUrl(id, path, true)}>Download</a></figcaption>
      {zoom && <div className="lightbox" onClick={() => setZoom(false)} role="dialog" aria-label="Enlarged figure">
        <img src={compareFileUrl(id, path)} alt="" /></div>}
    </figure>
  );
}

function verdict(p: ComparePair): { kind: "pass" | "warning" | "info"; text: string } {
  const same = p.n_same_direction, both = p.n_significant_both;
  const fisher = Math.min(p.fisher_up_up.p ?? 1, p.fisher_down_down.p ?? 1);
  const pattern = p.rrho.pattern ?? "";
  if (pattern.includes("disagree") || (p.spearman_log2FC.r ?? 0) < -0.2)
    return { kind: "warning", text: "The studies change genes in opposite directions. Check that both compare the groups the same way round (Flip direction)." };
  if (both > 0 && fisher < 1e-3 && pattern.includes("agree"))
    return { kind: "pass", text: `The studies agree: ${fmtInt(same)} genes change in the same direction in both, far more than expected by chance, and the rankings overlap (RRHO).` };
  if (pattern.includes("agree"))
    return { kind: "info", text: "The rankings overlap (RRHO), but few genes pass the significance rule in both studies. Try a looser cutoff, or look at the RRHO genes." };
  return { kind: "info", text: "No clear agreement between these studies." };
}

function Overview({ c }: { c: ComparisonT }) {
  const pairs = c.pairs ?? [];
  return (
    <>
      <section className="card">
        <h2>Studies</h2>
        <div className="table-wrap"><table>
          <thead><tr><th>Study</th><th>Compared</th><th>Organism</th><th className="num">Genes matched</th>
            <th className="num">Significant</th><th className="num">Up</th><th className="num">Down</th></tr></thead>
          <tbody>{(c.study_results ?? []).map((s) => (
            <tr key={s.label}><td><strong>{s.label}</strong><div className="muted small">{s.source === "run" ? "analysis on this site" : "results table"}</div></td>
              <td>{s.comparison}{s.flipped && <span className="muted small"> (flipped)</span>}</td><td>{s.organism}</td>
              <td className="num">{fmtInt(s.n_matched)}</td><td className="num">{fmtInt(s.n_significant)}</td>
              <td className="num up">{fmtInt(s.n_up)}</td><td className="num down">{fmtInt(s.n_down)}</td></tr>))}
          </tbody></table></div>
        <p className="muted small">Genes matched by {c.match_method}; {fmtInt(c.n_genes_in_all_studies)} genes were tested in every study.
          Rankings (RRHO) use {c.rank_by}.</p>
      </section>
      <section className="card">
        <h2>Agreement at a glance</h2>
        {pairs.map((p) => { const v = verdict(p); return <Notice key={p.study_a + p.study_b} kind={v.kind}><strong>{p.study_a} vs {p.study_b}:</strong> {v.text}</Notice>; })}
        <div className="table-wrap"><table>
          <thead><tr><th>Pair</th><th className="num">Shared genes</th><th className="num">Fold-change correlation</th>
            <th className="num">Up in both (expected)</th><th className="num">Down in both (expected)</th><th className="num">Opposite</th>
            <th className="num">p (up / down)</th></tr></thead>
          <tbody>{pairs.map((p) => (
            <tr key={p.study_a + p.study_b}><td>{p.study_a} vs {p.study_b}</td><td className="num">{fmtInt(p.n_shared_genes)}</td>
              <td className="num">{fmtNum(p.spearman_log2FC.r)}<div className="muted small">signif. genes: {fmtNum(p.spearman_log2FC_significant_either.r)}</div></td>
              <td className="num up">{fmtInt(p.up_up)} <span className="muted">({fmtNum(p.expected_up_up, 0)})</span></td>
              <td className="num down">{fmtInt(p.down_down)} <span className="muted">({fmtNum(p.expected_down_down, 0)})</span></td>
              <td className="num">{fmtInt(p.n_opposite_direction)}</td>
              <td className="num">{fmtP(p.fisher_up_up.p)} / {fmtP(p.fisher_down_down.p)}</td></tr>))}
          </tbody></table></div>
        <p className="muted small">Correlation: Spearman, over all shared genes and over the genes significant in either study. It is often modest
          for studies that agree, because most genes do not change. Expected = the overlap if the studies were unrelated; p from one-sided
          Fisher's exact tests.</p>
      </section>
      {(c.figures ?? []).length > 0 && <div className="figures fill">{c.figures!.map((f) => <Fig key={f.path} id={c.id} {...f} />)}</div>}
    </>
  );
}

function Pairs({ c }: { c: ComparisonT }) {
  const pairs = c.pairs ?? [];
  const [i, setI] = useState(0);
  const p = pairs[i];
  if (!p) return <p className="muted">No pairs.</p>;
  const best = p.rrho.best ?? {};
  const genes = (xs: { gene: string }[]) => xs.map((g) => g.gene).join(", ") || "none";
  return (
    <>
      {pairs.length > 1 && (
        <label className="field" style={{ maxWidth: 360 }}><span>Pair</span>
          <select value={i} onChange={(e) => setI(Number(e.target.value))}>
            {pairs.map((x, j) => <option key={j} value={j}>{x.study_a} vs {x.study_b}</option>)}
          </select></label>
      )}
      <div className="tiles">
        <div className="tile"><div className="value">{fmtInt(p.n_shared_genes)}</div><div className="label">shared genes</div></div>
        <div className="tile"><div className="value">{fmtNum(p.spearman_log2FC.r)}</div><div className="label">fold-change correlation</div></div>
        <div className="tile"><div className="value">{fmtInt(p.n_same_direction)}</div><div className="label">significant in both, same direction</div></div>
        <div className="tile"><div className="value">{fmtInt(p.n_opposite_direction)}</div><div className="label">significant in both, opposite</div></div>
        <div className="tile"><div className="value">{fmtNum(p.jaccard)}</div><div className="label">Jaccard index</div></div>
      </div>
      <p><strong>Up in both:</strong> {genes(p.top_shared_up)}<br /><strong>Down in both:</strong> {genes(p.top_shared_down)}
        {p.top_opposite.length > 0 && <><br /><strong>Opposite directions:</strong> {genes(p.top_opposite)}</>}</p>
      <div className="figures">
        <Fig id={c.id} path={p.figures.scatter} title="Fold changes" caption="Each dot is a gene tested in both studies, coloured by where it is significant." />
        <Fig id={c.id} path={p.figures.overlap} title="Significant genes by direction" caption="Rows: first study; columns: second study." />
        <Fig id={c.id} path={p.figures.rrho} title="Rank-rank overlap (RRHO)"
          caption="Overlap of the two gene rankings at every pair of thresholds. Hot spots at bottom left (up in both) and top right (down in both) mean agreement; the other corners mean opposite changes." />
      </div>
      {p.rrho.status === "done" && (
        <section className="card">
          <h3 style={{ marginTop: 0 }}>RRHO: {p.rrho.pattern}</h3>
          <div className="table-wrap"><table>
            <thead><tr><th>Quadrant</th><th className="num">Genes at the strongest cell</th><th className="num">Expected</th><th className="num">Adjusted p</th><th>Top genes</th></tr></thead>
            <tbody>{Object.values(best).filter(Boolean).map((b) => (
              <tr key={b.quadrant}><td>{b.meaning}</td><td className="num">{fmtInt(b.overlap)}</td><td className="num">{fmtNum(b.expected, 0)}</td>
                <td className="num">{fmtP(b.padj)}</td><td className="small">{b.top_genes.slice(0, 10).join(", ")}</td></tr>))}</tbody>
          </table></div>
        </section>
      )}
      {p.pathways.map((x) => (
        <section className="card" key={x.collection}>
          <h3 style={{ marginTop: 0 }}>{x.collection} pathways</h3>
          <p>{fmtInt(x.n_significant_both)} pathways significant in both ({fmtInt(x.n_same_direction)} same direction, {fmtInt(x.n_opposite_direction)} opposite);
            NES correlation {fmtNum(x.spearman_NES)}.{x.top_shared.length > 0 && <> Shared: {x.top_shared.map((t) => t.pathway).join(", ")}.</>}</p>
          <div className="figures wide"><Fig id={c.id} path={x.figure} title="Pathway enrichment in both studies" caption="Normalised enrichment score (NES) of each pathway." /></div>
        </section>
      ))}
      <p className="small">Tables: {p.tables.map((t) => <a key={t} href={compareFileUrl(c.id, t, true)} style={{ marginRight: 12 }}>{t.split("/").pop()}</a>)}</p>
    </>
  );
}

function Pathways({ c }: { c: ComparisonT }) {
  const st = c.pathways_status;
  if (!st || st.status !== "done") return <Notice kind="info">Pathways were not compared{st?.reason ? `: ${st.reason}` : "."}</Notice>;
  return (
    <>
      <p className="muted small">GSEA source: {Object.entries(st.gsea_source ?? {}).map(([k, v]) => `${k}: ${v}`).join("; ")}.</p>
      {(c.pathway_collections ?? []).map((x) => (
        <section className="card" key={x.collection}>
          <h3 style={{ marginTop: 0 }}>{x.collection}</h3>
          <p>{fmtInt(x.n_significant_in_two_or_more)} of {fmtInt(x.n_pathways)} pathways are significant in two or more studies
            ({fmtInt(x.n_consistent_in_two_or_more)} with the same direction).
            {x.table && <> <a href={compareFileUrl(c.id, x.table, true)}>All pathways (.csv)</a></>}</p>
          <div className="figures wide"><Fig id={c.id} path={x.heatmap} title="Pathways across studies" caption={`NES of the top pathways; * padj < ${st.padj_cutoff}.`} /></div>
        </section>
      ))}
    </>
  );
}

function Recurrent({ c }: { c: ComparisonT }) {
  const rows = c.recurrent_genes ?? [];
  const labels = (c.study_results ?? []).map((s) => s.label);
  if (!rows.length) return <p className="muted">No gene is significant in more than one study.</p>;
  return (
    <>
      <p className="muted small">Genes significant in two or more studies (first 100), most shared first.{" "}
        {(c.tables ?? []).map((t) => <a key={t} href={compareFileUrl(c.id, t, true)} style={{ marginRight: 12 }}>{t.split("/").pop()}</a>)}</p>
      <div className="table-wrap"><table>
        <thead><tr><th>Gene</th><th className="num">Up in</th><th className="num">Down in</th>{labels.map((l) => <th key={l} className="num">log2FC {l}</th>)}</tr></thead>
        <tbody>{rows.map((g) => (
          <tr key={g.key}><td><strong>{g.gene}</strong>{g.consistent !== "TRUE" && <span className="muted small"> (mixed)</span>}</td>
            <td className="num up">{g.n_up}</td><td className="num down">{g.n_down}</td>
            {labels.map((l) => <td key={l} className="num">{g[`log2FC_${l}`] ? fmtNum(Number(g[`log2FC_${l}`])) : "–"}</td>)}</tr>))}
        </tbody></table></div>
    </>
  );
}
    
