import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Example, type Job } from "../api";
import { Badge, Notice, Stepper } from "../components/common";
import { fmtTime } from "../format";

export default function Home() {
  const navigate = useNavigate();
  const [examples, setExamples] = useState<Example[]>([]);
  const [recent, setRecent] = useState<Job[]>([]);
  const [counts, setCounts] = useState<File | null>(null);
  const [metadata, setMetadata] = useState<File | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.examples().then(setExamples).catch((e) => setError(String(e.message ?? e)));
    api.runs().then((r) => setRecent(r.slice(0, 5))).catch(() => undefined);
  }, []);

  async function pickExample(name: string) {
    setBusy(name);
    setError(null);
    try {
      const up = await api.useExample(name);
      navigate(`/datasets/${up.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  async function upload(ev: FormEvent) {
    ev.preventDefault();
    if (!counts || !metadata) return;
    setBusy("upload");
    setError(null);
    try {
      const up = await api.upload(counts, metadata);
      navigate(`/datasets/${up.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  return (
    <>
      <Stepper current={0} />
      <h1>Analyse bulk RNA-seq counts</h1>
      <p className="muted">
        Upload a table of raw read counts and a table describing your samples. The site checks the files, helps you set up
        the comparison, runs a standard analysis (quality control, PCA, differential expression with DESeq2, pathway
        enrichment) with tested code, and gives you figures, tables and a report with a methods paragraph.
      </p>
      {error && <Notice kind="fail">{error}</Notice>}

      <div className="grid-2">
        <section className="card">
          <h2>Upload your data</h2>
          <form onSubmit={upload}>
            <label className="field">
              <span>Counts table</span>
              <input type="file" accept=".csv,.tsv,.txt,.xlsx,.gz" onChange={(e) => setCounts(e.target.files?.[0] ?? null)} />
              <small>
                Raw read counts (whole numbers, not TPM or FPKM). One row per gene, one column per sample; the first column
                holds gene IDs (Ensembl, Entrez or gene symbols).
              </small>
            </label>
            <label className="field">
              <span>Sample table</span>
              <input type="file" accept=".csv,.tsv,.txt,.xlsx" onChange={(e) => setMetadata(e.target.files?.[0] ?? null)} />
              <small>
                One row per sample. The first column holds sample names that match the counts table's column names exactly;
                other columns describe the samples (e.g. condition, batch, sex).
              </small>
            </label>
            <div className="row">
              <button type="submit" disabled={!counts || !metadata || busy !== null}>
                {busy === "upload" ? <><span className="spinner" /> Uploading…</> : "Upload and check"}
              </button>
              <span className="muted small">
                Templates: <a href="/api/templates/counts">counts</a> · <a href="/api/templates/metadata">sample table</a>
              </span>
            </div>
          </form>
        </section>

        <section className="card">
          <h2>Or try an example</h2>
          <p className="muted small">Public datasets with well-known results, to see how the site works.</p>
          {examples.map((ex) => (
            <div key={ex.name} className="row" style={{ marginBottom: 12 }}>
              <div style={{ flex: 1, minWidth: 200 }}>
                <strong>{ex.name}</strong>
                <div className="muted small">{ex.title.replace(/^\S+\s*/, "")}</div>
                {!ex.available && (
                  <div className="muted small">Not prepared yet. In the codebank folder run <code>{ex.make_command}</code></div>
                )}
              </div>
              <button className="secondary" disabled={!ex.available || busy !== null} onClick={() => pickExample(ex.name)}>
                {busy === ex.name ? <><span className="spinner" /> Loading…</> : "Use this dataset"}
              </button>
            </div>
          ))}
        </section>
      </div>

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
