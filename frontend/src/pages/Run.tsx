import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, codeZipUrl, fileUrl, type AgentWarnings, type Job, type ReportStatus, type Results } from "../api";
import { Badge, Figures, Markdown, Notice, SourceTag, Stepper, Tabs } from "../components/common";
import ChatTab from "../components/ChatTab";
import DeTab from "../components/DeTab";
import PathwaysTab from "../components/PathwaysTab";
import { fmtInt, fmtNum, fmtP, fmtTime, geneLabel } from "../format";

const RUNNING = new Set(["queued", "running"]);
type TabId = "overview" | "chat" | "qc" | "samples" | "de" | "heatmap" | "pathways" | "methods";

export default function Run() {
  const { jobId = "" } = useParams();
  const [job, setJob] = useState<Job | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>("overview");
  const [report, setReport] = useState<ReportStatus | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);

  // Once results exist, make sure the report is written (the assistant may take up to a minute)
  useEffect(() => {
    if (!results) return;
    let cancelled = false;
    (async () => {
      try {
        const st = await api.reportStatus(jobId);
        if (cancelled) return;
        if (st.available) { setReport(st); return; }
        setReportBusy(true);
        const built = await api.buildReport(jobId);
        if (!cancelled) setReport(built);
      } catch (e) {
        if (!cancelled) setReportError((e as Error).message);
      } finally {
        if (!cancelled) setReportBusy(false);
      }
    })();
    return () => { cancelled = true; };
  }, [jobId, results]);

  async function rewriteReport() {
    setReportBusy(true);
    setReportError(null);
    try { setReport(await api.buildReport(jobId, true)); }
    catch (e) { setReportError((e as Error).message); }
    finally { setReportBusy(false); }
  }

  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const poll = async () => {
      try {
        const j = await api.run(jobId);
        if (stopped) return;
        setJob(j);
        if (RUNNING.has(j.status)) timer = window.setTimeout(poll, 2000);
        else if (j.status === "success" || j.status === "success_with_errors") setResults(await api.results(jobId));
      } catch (e) {
        setError((e as Error).message);
      }
    };
    poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [jobId]);

  if (error) return <Notice kind="fail">{error}</Notice>;
  if (!job) return <p><span className="spinner" /> Loading…</p>;
  const s = job.settings;
  const running = RUNNING.has(job.status);

  return (
    <>
      <Stepper current={running ? 2 : 3} />
      <div className="row">
        <h1 style={{ margin: 0 }}>{s.test_level} vs {s.reference_level}</h1>
        <Badge status={job.status} />
      </div>
      <p className="muted">
        {job.dataset} · groups from <strong>{s.condition}</strong>
        {s.covariates.length > 0 && <> · accounting for <strong>{s.covariates.join(", ")}</strong></>}
        {" "}· started {fmtTime(job.started ?? job.created)}
      </p>

      {(running || !results) && <Progress job={job} />}
      {!running && job.error && (
        <Notice kind="fail">
          {job.error}{" "}
          <Link to={`/datasets/${job.upload_id}`}>Change the setup and try again</Link>.
        </Notice>
      )}

      {results && (
        <>
          <Downloads jobId={jobId} results={results} report={report} busy={reportBusy} error={reportError}
            onRewrite={rewriteReport} />
          <Tabs<TabId>
            active={tab}
            onChange={setTab}
            tabs={[
              { id: "overview", label: "Summary" },
              { id: "chat", label: "Ask (chat)" },
              { id: "qc", label: "Quality control" },
              { id: "samples", label: "Samples (PCA)" },
              { id: "de", label: "Differential expression" },
              { id: "heatmap", label: "Heatmap" },
              { id: "pathways", label: "Pathways" },
              { id: "methods", label: "Methods & settings" },
            ]}
          />
          {tab === "overview" && <Overview jobId={jobId} r={results} onTab={setTab} report={report} />}
          {tab === "chat" && <ChatTab job={job} />}
          {tab === "qc" && <QcTab jobId={jobId} r={results} />}
          {tab === "samples" && <SamplesTab jobId={jobId} r={results} />}
          {tab === "de" && <DeTab jobId={jobId} r={results} />}
          {tab === "heatmap" && <Figures jobId={jobId} figures={results.figures.heatmap} />}
          {tab === "pathways" && <PathwaysTab jobId={jobId} r={results} />}
          {tab === "methods" && <MethodsTab job={job} r={results} />}
        </>
      )}
    </>
  );
}

function Progress({ job }: { job: Job }) {
  const [cancelling, setCancelling] = useState(false);
  return (
    <section className="card">
      <div className="row">
        <h2 style={{ margin: 0 }}>{RUNNING.has(job.status) ? "Running the analysis" : "Analysis steps"}</h2>
        <span className="spacer" />
        {RUNNING.has(job.status) && (
          <button className="secondary" disabled={cancelling}
            onClick={() => { setCancelling(true); api.cancel(job.id).catch(() => setCancelling(false)); }}>
            Stop
          </button>
        )}
      </div>
      {job.status === "queued" && <p className="muted">Waiting for another analysis to finish…</p>}
      <ul className="progress-list">
        {(job.steps ?? []).map((st) => (
          <li key={st.name}>
            <span style={{ width: 90 }}>
              {st.status === "running" ? <><span className="spinner" /> running</> : <Badge status={st.status === "success" ? "pass" : st.status} label={st.status === "success" ? "done" : undefined} />}
            </span>
            <div>
              <strong>{st.name.replace(/^\d+_/, "").replace(/_/g, " ")}</strong>
              {st.seconds !== null && <span className="muted small"> · {fmtNum(st.seconds, 1)} s</span>}
              <div className="muted small">{st.description}</div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Downloads({ jobId, results, report, busy, error, onRewrite }: {
  jobId: string; results: Results; report: ReportStatus | null; busy: boolean; error: string | null; onRewrite: () => void;
}) {
  const ready = report?.available && !busy;
  return (
    <section className="card">
      <div className="row">
        <strong>Report:</strong>
        {busy && <span className="muted"><span className="spinner" /> {report?.available ? "Rewriting" : "Writing"} the report (up to a minute with the assistant)…</span>}
        {ready && (
          <>
            <a className="button" href={`/api/runs/${jobId}/report/html`} target="_blank" rel="noreferrer">Open report</a>
            <a className="button secondary" href={`/api/runs/${jobId}/report/pdf`}>Download PDF</a>
            <a className="button secondary" href={`/api/runs/${jobId}/report/html?download=true`}>Download HTML</a>
            <a className="button secondary" href={`/api/runs/${jobId}/report/pptx`}>Download PowerPoint</a>
            <button className="link" onClick={onRewrite}>Rewrite</button>
            <span className="muted small">
              {report!.source === "model"
                ? <>Text by the assistant ({report!.model?.replace(/^ollama:/, "")}), checked against the results{report!.n_removed ? `; ${report!.n_removed} unsupported sentence(s) removed` : ""}.</>
                : <>Text written by fixed rules{report!.fallback_reason ? ` (assistant not used: ${report!.fallback_reason})` : ""}.</>}
            </span>
          </>
        )}
      </div>
      {error && <Notice kind="fail">{error}</Notice>}
      <div className="row" style={{ marginTop: 10 }}>
        <strong>Data:</strong>
        <a className="button secondary" href={fileUrl(jobId, "04_differential_expression/de_results_all.csv", true)}>All genes (CSV)</a>
        <a className="button secondary" href={fileUrl(jobId, "04_differential_expression/de_results_significant.csv", true)}>
          Significant genes (CSV)
        </a>
        {results.methods_md && (
          <a className="button secondary" href={fileUrl(jobId, "07_report/methods.md", true)}>Methods text</a>
        )}
        <a className="button secondary" href={`/api/runs/${jobId}/bundle`}>Everything (.zip)</a>
        {results.report_available && (
          <a href={fileUrl(jobId, "07_report/report.html")} target="_blank" rel="noreferrer" className="small">Technical report (code bank)</a>
        )}
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <strong>Code:</strong>
        <a className="button secondary" href={codeZipUrl(jobId)}>All code used (.zip)</a>
        <a className="button secondary" href={codeZipUrl(jobId, true)}>Code + input files (.zip)</a>
        <span className="muted small">
          For a paper's code availability / supplementary material: the code bank at the exact version used, every
          setting, the software versions, a script that reruns the analysis, and any code the chat assistant ran.
        </span>
      </div>
    </section>
  );
}

function Overview({ jobId, r, onTab, report }: {
  jobId: string; r: Results; onTab: (t: TabId) => void; report: ReportStatus | null;
}) {
  const s = r.summary;
  const p = r.resolved_params;
  const groups = Object.entries(s.group_sizes ?? {}).map(([g, n]) => `${g} (${n})`).join(" and ");
  const volcano = r.figures.de.find((f) => f.path.endsWith("volcano_plot.png"));
  const pca = r.figures.samples.find((f) => f.path.endsWith("pca_plot.png"));
  const assoc = r.exploratory.summary?.pc_association ?? [];
  const cond = assoc.find((a) => a.variable === p.condition);
  return (
    <>
      {report?.available && report.source === "model" && (
        <section className="card">
          <div className="row" style={{ gap: 4 }}>
            <h2 style={{ margin: 0 }}>In plain words</h2>
            <SourceTag source="model" model={report.model ?? null} />
          </div>
          <p style={{ marginTop: 10 }}>{report.summary}</p>
          {report.key_findings && report.key_findings.length > 0 && (
            <ul>{report.key_findings.map((k) => <li key={k}>{k}</li>)}</ul>
          )}
          <p className="muted small" style={{ margin: 0 }}>
            Checked against the results: sentences with numbers, genes or pathways not found in them were removed.
          </p>
        </section>
      )}
      <section className="card">
        <h2>Summary</h2>
        <p>
          <strong>{fmtInt(s.n_significant)}</strong> of {fmtInt(s.n_genes_tested)} tested genes differ between{" "}
          <strong>{p.test_level}</strong> and <strong>{p.reference_level}</strong>:{" "}
          <span className="up">{fmtInt(s.n_up)} higher</span> and <span className="down">{fmtInt(s.n_down)} lower</span> in{" "}
          {p.test_level}. A gene counts as different when {s.significance_rule}.
        </p>
        <p className="muted">
          {s.n_samples} samples: {groups}. Design: <code>{s.design_formula}</code>
          {p.covariates.length > 0 && <> (the comparison accounts for {p.covariates.join(", ")})</>}.
          {cond && (
            <> The main source of variation between samples (PC1, {fmtNum(s.pca_percent_variance?.PC1, 1)}% of the
              variance) {cond.r2_PC1 >= 0.5 ? <>lines up with <strong>{p.condition}</strong>.</> :
                <>is <strong>not</strong> explained by {p.condition}; see the Samples tab.</>}</>
          )}
        </p>
        <div className="tiles">
          <div className="tile"><div className="value">{s.n_samples}</div><div className="label">samples</div></div>
          <div className="tile"><div className="value">{fmtInt(s.n_genes_tested)}</div><div className="label">genes tested</div></div>
          <div className="tile"><div className="value">{fmtInt(s.n_significant)}</div><div className="label">significant genes</div></div>
          <div className="tile"><div className="value up">{fmtInt(s.n_up)}</div><div className="label">higher in {p.test_level}</div></div>
          <div className="tile"><div className="value down">{fmtInt(s.n_down)}</div><div className="label">lower in {p.test_level}</div></div>
        </div>
      </section>

      <section className="card">
        <h2>Things to check</h2>
        <RunWarnings jobId={jobId} warnings={s.warnings} />
        {r.enrichment && r.enrichment.status !== "done" && (
          <Notice kind="warning">Pathway analysis was not run: {r.enrichment.reason}</Notice>
        )}
      </section>

      <div className="grid-2">
        <TopGenes title={`Top genes higher in ${p.test_level}`} genes={s.top_up} kind="up" />
        <TopGenes title={`Top genes lower in ${p.test_level}`} genes={s.top_down} kind="down" />
      </div>
      <p className="muted small">
        Full table in the <button className="link" onClick={() => onTab("de")}>Differential expression</button> tab.
      </p>
      <Figures jobId={jobId} figures={[pca, volcano].filter((f): f is NonNullable<typeof f> => !!f)} />
    </>
  );
}

function RunWarnings({ jobId, warnings }: { jobId: string; warnings: string[] }) {
  const [explained, setExplained] = useState<AgentWarnings | null>(null);
  const [pending, setPending] = useState(false);
  useEffect(() => {
    if (!warnings.length) return;
    setPending(true);
    api.agentRunWarnings(jobId).then(setExplained).catch(() => undefined).finally(() => setPending(false));
  }, [jobId, warnings.length]);

  if (!warnings.length) {
    return <Notice kind="pass">No warnings were raised about the files, sample quality or sample clustering.</Notice>;
  }
  if (!explained || explained.source === "rules") {
    return (
      <>
        {pending && <p className="muted small"><span className="spinner" /> The assistant is reading the warnings…</p>}
        <ul className="check-list">
          {warnings.map((w) => (
            <li key={w}><span className="badge warning">check</span><span>{w.replace(/^\[[^\]]+\]\s*/, "")}</span></li>
          ))}
        </ul>
      </>
    );
  }
  return (
    <>
      <p>{explained.headline} <SourceTag source={explained.source} model={explained.model} /></p>
      <ul className="check-list">
        {explained.items.map((it) => (
          <li key={it.index}>
            <span className={`severity ${it.severity}`}>{it.severity}</span>
            <div>
              {it.title && <strong>{it.title}. </strong>}
              {it.explanation}
              {it.what_to_do && <div className="small"><strong>What to do:</strong> {it.what_to_do}</div>}
              {it.title && <details><summary className="muted small">Original warning</summary><span className="small">{it.original}</span></details>}
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

function TopGenes({ title, genes, kind }: { title: string; genes: Results["summary"]["top_up"]; kind: "up" | "down" }) {
  return (
    <section className="card">
      <h3>{title}</h3>
      {genes.length === 0 ? <p className="muted">None.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Gene</th><th className="num">log2 fold change</th><th className="num">adj. p-value</th></tr></thead>
            <tbody>
              {genes.map((g) => (
                <tr key={g.gene_id}>
                  <td>{geneLabel(g)}</td>
                  <td className={`num ${kind}`}>{fmtNum(g.log2FoldChange)}</td>
                  <td className="num">{fmtP(g.padj)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function QcTab({ jobId, r }: { jobId: string; r: Results }) {
  const rows = r.qc.metrics;
  return (
    <>
      <section className="card">
        <h2>Per-sample quality</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Sample</th><th>Group</th><th className="num">Reads (millions)</th><th className="num">Genes detected</th>
                <th className="num">% genes with zero reads</th><th>Flags</th></tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.sample_id}>
                  <td>{m.sample_id}</td><td>{m.group}</td>
                  <td className="num">{m.library_size_millions}</td>
                  <td className="num">{fmtInt(Number(m.detected_genes))}</td>
                  <td className="num">{m.pct_zero_genes}</td>
                  <td>{m.flags ? <span className="badge warning">{m.flags}</span> : <span className="muted">–</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <Figures jobId={jobId} figures={r.figures.qc} />
    </>
  );
}

function SamplesTab({ jobId, r }: { jobId: string; r: Results }) {
  const ex = r.exploratory.summary;
  return (
    <>
      {ex && (
        <section className="card">
          <h2>What drives the differences between samples?</h2>
          <p className="muted">
            PC1 explains {fmtNum(ex.percent_variance.PC1, 1)}% and PC2 {fmtNum(ex.percent_variance.PC2, 1)}% of the variation.
            The table shows how much of each axis is explained by each sample characteristic (0 = none, 1 = all).
          </p>
          <div className="table-wrap" style={{ maxWidth: 520 }}>
            <table>
              <thead><tr><th>Characteristic</th><th className="num">PC1</th><th className="num">PC2</th></tr></thead>
              <tbody>
                {ex.pc_association.map((a) => (
                  <tr key={a.variable}><td>{a.variable}</td><td className="num">{fmtNum(a.r2_PC1)}</td><td className="num">{fmtNum(a.r2_PC2)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          {ex.flagged_samples.length > 0 ? (
            ex.flagged_samples.map((f) => <Notice key={f.sample_id + f.flag} kind="warning"><strong>{f.sample_id}</strong>: {f.message}</Notice>)
          ) : (
            <Notice kind="pass">Every sample clusters with its own group; no possible mislabels or outliers.</Notice>
          )}
        </section>
      )}
      <Figures jobId={jobId} figures={r.figures.samples} />
    </>
  );
}

function MethodsTab({ job, r }: { job: Job; r: Results }) {
  const p = r.resolved_params;
  const prov = r.provenance;
  const settingsRows: [string, string][] = [
    ["Comparison", `${p.test_level} vs ${p.reference_level} (column ${p.condition})`],
    ["Design", String(p.design_formula ?? "")],
    ["Covariates", p.covariates.length ? p.covariates.join(", ") : "none"],
    ["Organism / gene IDs", `${p.organism} / ${String(p.gene_id_type ?? "")}`],
    ["Low-count filter", `at least ${p.min_count} reads in at least ${p.min_samples} samples`],
    ["Significance", r.summary.significance_rule],
    ["Fold-change shrinkage", r.de?.lfc_shrinkage_used ?? ""],
    ["Samples left out", (p.exclude_samples as string[] | undefined)?.join(", ") || "none"],
  ];
  return (
    <>
      <section className="card">
        {r.methods_md ? <Markdown text={r.methods_md} /> : <p className="muted">No methods text was written.</p>}
      </section>
      <section className="card">
        <h2>Settings used</h2>
        <div className="table-wrap">
          <table><tbody>{settingsRows.map(([k, v]) => <tr key={k}><th>{k}</th><td>{v}</td></tr>)}</tbody></table>
        </div>
      </section>
      {prov && (
        <section className="card">
          <h2>Reproducibility</h2>
          <p>
            Code bank version {prov.code_bank.version}
            {prov.code_bank.commit && <>, commit <code>{prov.code_bank.commit.slice(0, 7)}</code></>}
            {prov.code_bank.tag && <> (tag {prov.code_bank.tag})</>}. {prov.r_version}.
          </p>
          {job.codebank?.uncommitted_changes && (
            <Notice kind="warning">The code bank had uncommitted changes when this ran, so the commit alone does not reproduce it exactly.</Notice>
          )}
          <p className="muted small">Rerun from the command line in the code bank folder: <code>{prov.rerun_command}</code></p>
          <details>
            <summary className="small">Software versions ({Object.keys(prov.packages).length} packages)</summary>
            <div className="table-wrap" style={{ maxWidth: 420 }}>
              <table><tbody>{Object.entries(prov.packages).map(([k, v]) => <tr key={k}><td>{k}</td><td>{v}</td></tr>)}</tbody></table>
            </div>
          </details>
        </section>
      )}
    </>
  );
}
