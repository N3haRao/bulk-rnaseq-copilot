import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Example, type GeoInfo, type Job } from "../api";
import { Badge, Notice, Stepper } from "../components/common";
import { fmtTime } from "../format";

const PLACEHOLDER =
  "Describe your experiment in your own words, for example:\n" +
  "\"Mouse mammary gland, luminal vs basal cells, 3 mice per group, two sequencing batches. " +
  "I want the genes that differ between luminal and basal, accounting for batch, and the PCA coloured by mouse.\"";

export default function Home() {
  const navigate = useNavigate();
  const [examples, setExamples] = useState<Example[]>([]);
  const [recent, setRecent] = useState<Job[]>([]);
  const [text, setText] = useState("");
  const [counts, setCounts] = useState<File | null>(null);
  const [metadata, setMetadata] = useState<File | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [assistant, setAssistant] = useState<{ available: boolean; model: string; detail: string } | null>(null);
  const countsInput = useRef<HTMLInputElement>(null);
  const samplesInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.examples().then(setExamples).catch((e) => setError(String(e.message ?? e)));
    api.runs().then((r) => setRecent(r.slice(0, 5))).catch(() => undefined);
    api.agentStatus().then(setAssistant).catch(() => undefined);
  }, []);

  async function start() {
    if (!counts || !metadata || busy) return;
    setBusy("upload");
    setError(null);
    try {
      const up = await api.upload(counts, metadata);
      navigate(`/datasets/${up.id}`, { state: { firstMessage: text.trim() } });
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  async function pickExample(ex: Example) {
    setBusy(ex.name);
    setError(null);
    try {
      const up = await api.useExample(ex.name);
      const description = text.trim() || `This is the ${ex.name} example: ${ex.title.replace(/^\S+\s*/, "")}`;
      navigate(`/datasets/${up.id}`, { state: { firstMessage: description } });
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && counts && metadata) { e.preventDefault(); start(); }
  }

  return (
    <>
      <Stepper current={0} />
      <h1>Analyse bulk RNA-seq counts</h1>
      <p className="muted">
        Tell the assistant what you have and what you want to know, and attach your two tables. It suggests how to set up the
        analysis; you approve it before anything runs. The analysis itself (quality control, PCA, DESeq2 differential
        expression, pathway enrichment) is done by the lab's tested code bank.
      </p>
      {error && <Notice kind="fail">{error}</Notice>}

      <section className="card start-chat">
        <h2>Describe your experiment</h2>
        <textarea value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKey} rows={5}
          placeholder={PLACEHOLDER} aria-label="Describe your experiment" disabled={busy !== null} />
        <div className="attach-row">
          <input ref={countsInput} type="file" hidden accept=".csv,.tsv,.txt,.xlsx,.gz"
            onChange={(e) => setCounts(e.target.files?.[0] ?? null)} />
          <input ref={samplesInput} type="file" hidden accept=".csv,.tsv,.txt,.xlsx"
            onChange={(e) => setMetadata(e.target.files?.[0] ?? null)} />
          <button type="button" className={counts ? "secondary attach done" : "secondary attach"} onClick={() => countsInput.current?.click()}>
            {counts ? `✓ ${counts.name}` : "📎 Attach counts table"}
          </button>
          <button type="button" className={metadata ? "secondary attach done" : "secondary attach"} onClick={() => samplesInput.current?.click()}>
            {metadata ? `✓ ${metadata.name}` : "📎 Attach sample table"}
          </button>
          <span className="spacer" />
          <button onClick={start} disabled={!counts || !metadata || busy !== null}>
            {busy === "upload" ? <><span className="spinner" /> Uploading…</> : "Send"}
          </button>
        </div>
        <details className="small" style={{ marginTop: 10 }}>
          <summary className="muted">What the two tables should look like</summary>
          <ul>
            <li><strong>Counts table:</strong> raw read counts (whole numbers, not TPM or FPKM). One row per gene, one column per
              sample; the first column holds gene IDs (Ensembl, Entrez or gene symbols).</li>
            <li><strong>Sample table:</strong> one row per sample. The first column holds sample names that match the counts
              table's column names exactly; other columns describe the samples (condition, batch, sex, donor…).</li>
          </ul>
          Templates: <a href="/api/templates/counts">counts</a> · <a href="/api/templates/metadata">sample table</a>
        </details>
        <p className="muted small" style={{ marginBottom: 0 }}>
          {assistant?.available
            ? <>The setup assistant runs on this computer ({assistant.model.replace(/^ollama:/, "")}, an open-weight model); nothing is sent to the internet.</>
            : <>The setup assistant is not running ({assistant?.detail ?? "checking…"}). The site will suggest a setup with built-in rules instead.</>}
          {" "}It can change the code bank's settings (comparison, covariates, cutoffs, plot colours and shapes), never its methods.
        </p>
      </section>

      <section className="card">
        <div className="row">
          <div style={{ flex: 1, minWidth: 240 }}>
            <h2 style={{ margin: 0 }}>Or start from raw FASTQ files</h2>
            <p className="muted small" style={{ margin: "4px 0 0" }}>Sequencing files already copied to the server: QC and trimming, alignment,
              quantification and counts with the code bank's FASTQ pipeline, then this analysis, with splicing and isoforms.</p>
          </div>
          <Link to="/fastq" className="button secondary">Start from FASTQ</Link>
        </div>
      </section>

      <GeoImport text={text} busy={busy} setBusy={setBusy} />

      <section className="card">
        <h2>Or try an example</h2>
        <p className="muted small">Public datasets with well-known results, to see how the site works. Your description above, if any, is used.</p>
        {examples.map((ex) => (
          <div key={ex.name} className="row" style={{ marginBottom: 12 }}>
            <div style={{ flex: 1, minWidth: 200 }}>
              <strong>{ex.name}</strong>
              <div className="muted small">{ex.title.replace(/^\S+\s*/, "")}</div>
              {!ex.available && (
                <div className="muted small">Not prepared yet. In the codebank folder run <code>{ex.make_command}</code></div>
              )}
            </div>
            <button className="secondary" disabled={!ex.available || busy !== null} onClick={() => pickExample(ex)}>
              {busy === ex.name ? <><span className="spinner" /> Loading…</> : "Use this dataset"}
            </button>
          </div>
        ))}
      </section>

      {recent.length > 0 && (
        <section className="card">
          <div className="row"><h2>Recent analyses</h2><span className="spacer" /><Link to="/runs">See all</Link></div>
          <ul className="check-list">
            {recent.map((j) => (
              <li key={j.id}>
                <Badge status={j.status} />
                <Link to={`/runs/${j.id}`}>
                  {j.settings.test_level} vs {j.settings.reference_level}
                </Link>
                <span className="muted small">{j.dataset} · {fmtTime(j.created)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function GeoImport({ text, busy, setBusy }: { text: string; busy: string | null; setBusy: (b: string | null) => void }) {
  const navigate = useNavigate();
  const [acc, setAcc] = useState("");
  const [info, setInfo] = useState<GeoInfo | null>(null);
  const [option, setOption] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function lookUp(ev: React.FormEvent) {
    ev.preventDefault();
    setBusy("geo-inspect"); setErr(null); setInfo(null);
    try {
      const i = await api.geoInspect(acc.trim());
      setInfo(i);
      setOption(i.recommended ?? "");
    } catch (e) { setErr((e as Error).message); } finally { setBusy(null); }
  }

  async function doImport() {
    if (!info || !option) return;
    setBusy("geo-import"); setErr(null);
    try {
      const up = await api.geoImport(info.accession, option);
      const own = text.trim();
      navigate(`/datasets/${up.id}`, { state: { firstMessage: own ? `${up.description}\n\n${own}` : up.description } });
    } catch (e) { setErr((e as Error).message); setBusy(null); }
  }

  const keys = info ? Array.from(new Set(info.samples.flatMap((s) => Object.keys(s.characteristics)))) : [];
  return (
    <section className="card">
      <h2>Or import a public dataset from GEO</h2>
      <form onSubmit={lookUp} className="row">
        <input type="text" value={acc} onChange={(e) => setAcc(e.target.value)} placeholder="GSE accession, e.g. GSE60450"
          aria-label="GEO series accession" style={{ maxWidth: 260 }} />
        <button type="submit" className="secondary" disabled={!/^\s*GSE\d+\s*$/i.test(acc) || busy !== null}>
          {busy === "geo-inspect" ? <><span className="spinner" /> Looking up…</> : "Look up"}
        </button>
        <span className="muted small">The sample descriptions from GEO become the sample table; the assistant proposes the groups.</span>
      </form>
      {err && <Notice kind="fail">{err}</Notice>}
      {info && (
        <div style={{ marginTop: 12 }}>
          <p style={{ margin: "0 0 4px" }}><strong><a href={info.url} target="_blank" rel="noreferrer">{info.accession}</a>: {info.title}</strong></p>
          <p className="muted small" style={{ margin: 0 }}>
            {info.n_samples} samples · {info.organisms.join(", ") || "organism not stated"} · {info.library_strategies.join(", ")}
          </p>
          {info.summary && <details className="small"><summary className="muted">Summary</summary><p>{info.summary}</p>
            {info.overall_design && <p><strong>Design:</strong> {info.overall_design}</p>}</details>}
          {info.warnings.map((w) => <Notice key={w} kind="warning">{w}</Notice>)}
          {info.samples.length > 0 && (
            <div className="table-wrap" style={{ maxHeight: 220, marginTop: 8 }}>
              <table>
                <thead><tr><th>Sample</th><th>Title</th>{keys.map((k) => <th key={k}>{k}</th>)}</tr></thead>
                <tbody>{info.samples.slice(0, 50).map((s) => (
                  <tr key={s.geo_accession}><td>{s.geo_accession}</td><td>{s.title}</td>{keys.map((k) => <td key={k}>{s.characteristics[k] ?? ""}</td>)}</tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {info.counts_options.length > 0 && (
            <>
              <label className="field" style={{ marginTop: 10 }}>
                <span>Counts</span>
                <select value={option} onChange={(e) => setOption(e.target.value)}>
                  {info.counts_options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
                <small>NCBI's counts come from one standard pipeline for every sample (best for comparing studies). Supplementary files are the
                  authors' own tables; their column names are matched to the samples automatically.</small>
              </label>
              <button onClick={doImport} disabled={!option || busy !== null}>
                {busy === "geo-import" ? <><span className="spinner" /> Downloading from GEO…</> : "Import and set up"}
              </button>
            </>
          )}
        </div>
      )}
    </section>
  );
}
