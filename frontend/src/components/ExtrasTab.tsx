import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, extraFileUrl, type ExtraAnalysis, type ExtraModule, type Job } from "../api";
import { Badge, Markdown, Notice } from "./common";
import { fmtNum, fmtP } from "../format";

const RUNNING = new Set(["queued", "running"]);

export default function ExtrasTab({ job }: { job: Job }) {
  const [available, setAvailable] = useState<Partial<Record<ExtraModule, string>> | null>(null);
  const [analyses, setAnalyses] = useState<Record<ExtraModule, ExtraAnalysis | null>>({ splicing: null, isoforms: null });
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await api.extras(job.id);
      setAvailable(r.available);
      setAnalyses(r.analyses);
      return r.analyses;
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  }, [job.id]);

  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const poll = async () => {
      const a = await refresh();
      if (!stopped && a && Object.values(a).some((x) => x && RUNNING.has(x.status))) timer = window.setTimeout(poll, 3000);
    };
    poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [refresh]);

  const started = (a: ExtraAnalysis) => {
    setAnalyses((cur) => ({ ...cur, [a.module]: a }));
    const poll = async () => {
      const x = await api.extra(job.id, a.module).catch(() => null);
      if (x) setAnalyses((cur) => ({ ...cur, [a.module]: x }));
      if (!x || RUNNING.has(x.status)) window.setTimeout(poll, 3000);
    };
    window.setTimeout(poll, 2000);
  };

  if (error) return <Notice kind="fail">{error}</Notice>;
  if (!available) return <p><span className="spinner" /> Loading…</p>;
  if (!available.splicing && !available.isoforms) {
    return <Notice kind="info">The code bank in use has no splicing or isoform modules (they arrived in code bank v0.3.0 / v0.4.0).</Notice>;
  }
  const s = job.settings;
  return (
    <>
      <p className="muted">
        Optional analyses for the same comparison (<strong>{s.test_level}</strong> vs <strong>{s.reference_level}</strong>
        {s.covariates.length > 0 && <>, accounting for {s.covariates.join(", ")}</>}), run with the code bank's tested modules. They need
        extra input files made from the sequencing reads; when they finish, they are added to the report, the PowerPoint, the
        code download and new chats.
      </p>
      {available.splicing && <SplicingCard job={job} a={analyses.splicing} onStarted={started} />}
      {available.isoforms && <IsoformsCard job={job} a={analyses.isoforms} onStarted={started} />}
    </>
  );
}

// ---- Splicing ---------------------------------------------------------------------------------
function SplicingCard({ job, a, onStarted }: { job: Job; a: ExtraAnalysis | null; onStarted: (a: ExtraAnalysis) => void }) {
  const s = job.settings;
  const [files, setFiles] = useState<File[]>([]);
  const [b1, setB1] = useState("test");
  const [countType, setCountType] = useState("JCEC");
  const [fdr, setFdr] = useState("0.05");
  const [dpsi, setDpsi] = useState("0.1");
  const [reads, setReads] = useState("10");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true); setErr(null);
    try {
      onStarted(await api.startSplicing(job.id, files, { b1_group: b1, count_type: countType, fdr_cutoff: fdr, delta_psi_cutoff: dpsi, min_reads_per_sample: reads }));
      setShowForm(false);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  const running = a && RUNNING.has(a.status);
  return (
    <section className="card">
      <div className="row">
        <h2 style={{ margin: 0 }}>Alternative splicing</h2>
        {a && <Badge status={a.status} />}
        <span className="spacer" />
        {a && !running && !showForm && <button className="secondary" onClick={() => setShowForm(true)}>Run again</button>}
      </div>
      <p className="muted small">
        Which exons are included more or less often in {s.test_level} than in {s.reference_level}: skipped exons, retained introns,
        alternative 5'/3' splice sites and mutually exclusive exons, from rMATS. Results are compared with this run's gene-level
        results, so you can see which genes change splicing, expression, or both.
      </p>
      {(!a || showForm) && !running && (
        <form onSubmit={submit}>
          <label className="field">
            <span>rMATS output tables</span>
            <input type="file" multiple accept=".txt,.zip" onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
            <small>The files rMATS wrote (SE.MATS.JCEC.txt, MXE…, A3SS…, A5SS…, RI…), or a .zip of its output folder. rMATS needs
              BAM files; the code bank's later/scripts/U04_rmats.R runs it.</small>
          </label>
          <div className="grid-2">
            <label className="field">
              <span>rMATS sample 1 (--b1) was</span>
              <select value={b1} onChange={(e) => setB1(e.target.value)}>
                <option value="test">{s.test_level} (test group)</option>
                <option value="reference">{s.reference_level} (reference group)</option>
              </select>
              <small>Sets the direction: dPSI = PSI(sample 1) − PSI(sample 2).</small>
            </label>
            <label className="field">
              <span>Reads used</span>
              <select value={countType} onChange={(e) => setCountType(e.target.value)}>
                <option value="JCEC">Junction + exon-body reads (JCEC, recommended)</option>
                <option value="JC">Junction reads only (JC)</option>
              </select>
            </label>
            <label className="field"><span>FDR cutoff</span>
              <input type="number" step="any" min="0.001" max="1" value={fdr} onChange={(e) => setFdr(e.target.value)} /></label>
            <label className="field"><span>Minimum |dPSI|</span>
              <input type="number" step="any" min="0" max="0.9" value={dpsi} onChange={(e) => setDpsi(e.target.value)} />
              <small>0.1 = 10 percentage points of inclusion.</small></label>
            <label className="field"><span>Minimum reads per sample</span>
              <input type="number" step="1" min="0" value={reads} onChange={(e) => setReads(e.target.value)} /></label>
          </div>
          {err && <Notice kind="fail">{err}</Notice>}
          <div className="row">
            <button type="submit" disabled={busy || !files.length}>{busy ? <><span className="spinner" /> Uploading…</> : "Run splicing analysis"}</button>
            {a && <button type="button" className="link" onClick={() => setShowForm(false)}>Cancel</button>}
          </div>
        </form>
      )}
      {a && <AnalysisView jobId={job.id} a={a} />}
    </section>
  );
}

// ---- Isoforms -------------------------------------------------------------------------------------
function IsoformsCard({ job, a, onStarted }: { job: Job; a: ExtraAnalysis | null; onStarted: (a: ExtraAnalysis) => void }) {
  const s = job.settings;
  const [zip, setZip] = useState<File | null>(null);
  const [ann, setAnn] = useState<File | null>(null);
  const [delta, setDelta] = useState("0.1");
  const [alpha, setAlpha] = useState("0.05");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    if (!zip) return;
    setBusy(true); setErr(null);
    try {
      onStarted(await api.startIsoforms(job.id, zip, ann, { min_delta_usage: delta, dui_alpha: alpha }));
      setShowForm(false);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  const running = a && RUNNING.has(a.status);
  return (
    <section className="card">
      <div className="row">
        <h2 style={{ margin: 0 }}>Transcript isoforms</h2>
        {a && <Badge status={a.status} />}
        <span className="spacer" />
        {a && !running && !showForm && <button className="secondary" onClick={() => setShowForm(true)}>Run again</button>}
      </div>
      <p className="muted small">
        <strong>Isoform expression (DEI):</strong> which transcripts are more or less abundant (DESeq2 per transcript, with this run's
        settings). <strong>Isoform usage (DUI):</strong> which genes change the share each transcript makes up (an isoform switch),
        even when the gene's total does not change (DEXSeq + stageR). Uses this run's sample table, groups and covariates
        ({s.condition}: {s.test_level} vs {s.reference_level}).
      </p>
      {(!a || showForm) && !running && (
        <form onSubmit={submit}>
          <label className="field">
            <span>Salmon output (.zip)</span>
            <input type="file" accept=".zip" onChange={(e) => setZip(e.target.files?.[0] ?? null)} />
            <small>One folder per sample containing quant.sf, folders named exactly like this run's samples. The code bank's
              later/scripts/U02_salmon_quant.R makes them.</small>
          </label>
          <label className="field">
            <span>Annotation (optional)</span>
            <input type="file" accept=".gtf,.gz,.csv,.tsv,.txt" onChange={(e) => setAnn(e.target.files?.[0] ?? null)} />
            <small>The GTF the Salmon index was built from (.gtf or .gtf.gz), or a table with transcript_id and gene_id. Not needed when
              the transcript names include the gene (GENCODE transcript FASTA).</small>
          </label>
          <div className="grid-2">
            <label className="field"><span>Minimum change in usage</span>
              <input type="number" step="any" min="0" max="0.9" value={delta} onChange={(e) => setDelta(e.target.value)} />
              <small>0.1 = the transcript's share of its gene changes by 10 percentage points.</small></label>
            <label className="field"><span>FDR for isoform usage</span>
              <input type="number" step="any" min="0.001" max="1" value={alpha} onChange={(e) => setAlpha(e.target.value)} /></label>
          </div>
          {err && <Notice kind="fail">{err}</Notice>}
          <div className="row">
            <button type="submit" disabled={busy || !zip}>{busy ? <><span className="spinner" /> Uploading…</> : "Run isoform analysis"}</button>
            {a && <button type="button" className="link" onClick={() => setShowForm(false)}>Cancel</button>}
          </div>
        </form>
      )}
      {a && <AnalysisView jobId={job.id} a={a} />}
    </section>
  );
}

// ---- Results of one analysis ---------------------------------------------------------------------------
function AnalysisView({ jobId, a }: { jobId: string; a: ExtraAnalysis }) {
  const [zoom, setZoom] = useState<string | null>(null);
  if (RUNNING.has(a.status)) {
    return (
      <ul className="check-list" style={{ marginTop: 12 }}>
        {a.steps.map((st) => (
          <li key={st.name}>
            {st.status === "running" ? <span className="spinner" /> : <span className="tool-icon">{st.status === "success" ? "✓" : st.status === "failed" ? "✗" : "·"}</span>}
            {st.description || st.name}
          </li>
        ))}
      </ul>
    );
  }
  if (a.status === "failed" || a.status === "interrupted") return <Notice kind="fail">{a.error ?? "The analysis failed."}</Notice>;
  const sm = a.summary ?? {};
  const warn = (a.validation?.checks ?? []).filter((c) => c.status === "warning");
  return (
    <div style={{ marginTop: 12 }}>
      {a.status === "success_with_errors" && <Notice kind="warning">Finished, but an optional step failed (see the methods text and the download).</Notice>}
      {a.note && <Notice kind="info">{a.note}</Notice>}
      {warn.map((c) => <Notice key={c.id} kind="warning">{c.message}</Notice>)}
      {a.module === "splicing" ? (
        <>
          <div className="tiles">
            <div className="tile"><div className="value">{fmtNum(sm.n_events_tested, 0)}</div><div className="label">events tested</div></div>
            <div className="tile"><div className="value">{fmtNum(sm.n_significant, 0)}</div><div className="label">significant</div></div>
            <div className="tile"><div className="value up">{fmtNum(sm.n_up, 0)}</div><div className="label">more inclusion</div></div>
            <div className="tile"><div className="value down">{fmtNum(sm.n_down, 0)}</div><div className="label">less inclusion</div></div>
            <div className="tile"><div className="value">{fmtNum(sm.n_genes_significant, 0)}</div><div className="label">genes</div></div>
          </div>
          <p className="muted small">{sm.significance_rule}. {sm.sign_convention}.</p>
          {sm.splicing_vs_expression?.genes && (
            <p className="small">Compared with this run's expression results: {Object.entries(sm.splicing_vs_expression.genes as Record<string, number>)
              .map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>
          )}
        </>
      ) : (
        <>
          <div className="tiles">
            <div className="tile"><div className="value">{fmtNum(sm.dei?.n_significant, 0)}</div><div className="label">transcripts change (DEI)</div></div>
            <div className="tile"><div className="value">{fmtNum(sm.dei?.n_genes_with_significant_transcripts, 0)}</div><div className="label">genes (DEI)</div></div>
            <div className="tile"><div className="value">{fmtNum(sm.dui?.n_genes_significant, 0)}</div><div className="label">genes change usage (DUI)</div></div>
            <div className="tile"><div className="value">{fmtNum(sm.dui?.n_isoform_switches, 0)}</div><div className="label">isoform switches</div></div>
          </div>
          <p className="muted small">DEI: {sm.dei?.significance_rule}. DUI: {sm.dui?.status ? `not tested (${sm.dui.reason})` : sm.dui?.significance_rule}.</p>
        </>
      )}
      <div className="figures">
        {(a.figures ?? []).map((f) => (
          <figure className="figure" key={f.path}>
            <img src={extraFileUrl(jobId, a.module, f.path)} alt={f.title} loading="lazy" onClick={() => setZoom(f.path)} />
            <figcaption><strong>{f.title}</strong>{f.caption}{" "}<a href={extraFileUrl(jobId, a.module, f.path, true)}>Download</a></figcaption>
          </figure>
        ))}
      </div>
      {zoom && (
        <div className="lightbox" onClick={() => setZoom(null)} role="dialog" aria-label="Enlarged figure">
          <img src={extraFileUrl(jobId, a.module, zoom)} alt="" />
        </div>
      )}
      {a.module === "splicing" ? <SplicingTable rows={a.top ?? []} /> : <IsoformTables genes={a.top ?? []} dei={a.top_dei ?? []} />}
      <div className="row" style={{ marginTop: 10 }}>
        <strong>Download:</strong>
        {(a.tables ?? []).map((t) => (
          <a key={t} className="button secondary" href={extraFileUrl(jobId, a.module, t, true)}>{t.split("/").pop()}</a>
        ))}
        <a className="button secondary" href={`/api/runs/${jobId}/extras/${a.module}/download`}>Everything (.zip)</a>
      </div>
      {a.methods_md && (
        <details style={{ marginTop: 10 }}><summary>Methods</summary><Markdown text={a.methods_md} /></details>
      )}
    </div>
  );
}

function SplicingTable({ rows }: { rows: Record<string, string>[] }) {
  if (!rows.length) return <p className="muted">No significant events.</p>;
  return (
    <>
      <h3>Top significant events</h3>
      <div className="table-wrap" style={{ maxHeight: 380 }}>
        <table>
          <thead><tr><th>Gene</th><th>Type</th><th>Location</th><th className="num">PSI 1</th><th className="num">PSI 2</th><th className="num">dPSI</th><th className="num">FDR</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.event_id}>
                <td>{r.gene_symbol || r.gene_id}</td><td>{r.event_type}</td>
                <td className="small">{r.chr}:{r.event_start}-{r.event_end} ({r.strand})</td>
                <td className="num">{fmtNum(Number(r.psi_1), 2)}</td><td className="num">{fmtNum(Number(r.psi_2), 2)}</td>
                <td className={"num " + (Number(r.delta_psi) > 0 ? "up" : "down")}>{fmtNum(Number(r.delta_psi), 2)}</td>
                <td className="num">{fmtP(Number(r.fdr))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function IsoformTables({ genes, dei }: { genes: Record<string, string>[]; dei: Record<string, string>[] }) {
  return (
    <>
      <h3>Genes that change isoform usage</h3>
      {genes.length ? (
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table>
            <thead><tr><th>Gene</th><th className="num">adj. p (gene)</th><th className="num">max |Δ usage|</th><th>Switch</th><th>Gaining share</th><th>Losing share</th></tr></thead>
            <tbody>
              {genes.map((g) => (
                <tr key={g.gene_id}>
                  <td>{g.gene_name || g.gene_id}</td><td className="num">{fmtP(Number(g.gene_padj))}</td>
                  <td className="num">{fmtNum(Number(g.max_abs_delta_usage), 2)}</td><td>{g.isoform_switch === "TRUE" ? "yes" : ""}</td>
                  <td className="small">{g.transcripts_up}</td><td className="small">{g.transcripts_down}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">None.</p>}
      <h3>Top transcripts by isoform expression</h3>
      {dei.length ? (
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table>
            <thead><tr><th>Transcript</th><th>Gene</th><th className="num">log2 FC</th><th className="num">adj. p</th></tr></thead>
            <tbody>
              {dei.map((t) => (
                <tr key={t.transcript_id}>
                  <td className="small">{t.transcript_id}</td><td>{t.gene_name || t.gene_id}</td>
                  <td className={"num " + (Number(t.log2FoldChange) > 0 ? "up" : "down")}>{fmtNum(Number(t.log2FoldChange), 2)}</td>
                  <td className="num">{fmtP(Number(t.padj))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">None.</p>}
    </>
  );
}
