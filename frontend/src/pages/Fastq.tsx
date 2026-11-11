import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type FastqAssist, type FastqCheck, type FastqFolder, type FastqHome, type FastqJob, type FastqSpec } from "../api";
import { Badge, Notice } from "../components/common";
import { fmtTime } from "../format";

// Start from FASTQ files that are already on the server (copied with rsync/scp; Globus later, into the same folder).
// The processing is the code bank's later/scripts/run_upstream.R; the counts then go through the normal setup.

const STEP_HELP: Record<string, string> = {
  qc: "adapter and quality trimming, a QC report per sample",
  read_length: "needed by rMATS",
  star: "splice-aware alignment to the genome (BAM files); also tells the library strandedness",
  salmon: "transcript quantification; the gene counts and the isoform analysis come from it",
  gene_counts: "the counts table and sample table for the analysis",
  rmats: "alternative splicing events, each group against the control (for the splicing analysis)",
};

export default function Fastq() {
  const { jobId } = useParams();
  return <div className="fastq">{jobId ? <FastqProgress jobId={jobId} /> : <FastqSetup />}</div>;
}

function FastqSetup() {
  const navigate = useNavigate();
  const [home, setHome] = useState<FastqHome | null>(null);
  const [folderName, setFolderName] = useState("");
  const [folder, setFolder] = useState<FastqFolder | null>(null);
  const [groups, setGroups] = useState<Record<string, string>>({});
  const [batch, setBatch] = useState<Record<string, string>>({});
  const [control, setControl] = useState<string>("");
  const [species, setSpecies] = useState<string>("");
  const [steps, setSteps] = useState<string[]>([]);
  const [threads, setThreads] = useState(8);
  const [description, setDescription] = useState("");
  const [assist, setAssist] = useState<FastqAssist | null>(null);
  const [origin, setOrigin] = useState<string>("rules");
  const [check, setCheck] = useState<FastqCheck | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => api.fastqHome().then((h) => {
    setHome(h);
    setSteps((s) => (s.length ? s : h.steps.map((x) => x.id)));
    setThreads((t) => (t === 8 ? Math.min(h.max_threads, 32) : t));
  }).catch((e) => setError((e as Error).message));
  useEffect(() => { load(); }, []);

  // reference download progress
  const downloading = home?.references.some((r) => r.download?.status === "running");
  useEffect(() => {
    if (!downloading) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [downloading]);

  async function pick(name: string) {
    setFolderName(name); setFolder(null); setCheck(null); setAssist(null); setError(null);
    if (!name) return;
    setBusy("folder");
    try {
      const f = await api.fastqFolder(name);
      setFolder(f);
      setGroups(f.suggestion.groups);
      setControl(f.suggestion.control ?? "");
      setBatch({});
      setOrigin("rules");
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  async function askAssistant() {
    if (!folder) return;
    setBusy("assist"); setError(null);
    try {
      const a = await api.fastqAssist(folder.folder, description);
      setAssist(a);
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  function useAssistant() {
    if (!assist) return;
    setGroups(assist.groups);
    if (assist.control) setControl(assist.control);
    if (Object.keys(assist.batch).length) setBatch(assist.batch);
    if (assist.species) setSpecies(assist.species);
    setOrigin(assist.origin);
    setAssist(null); setCheck(null);
  }

  const levels = useMemo(() => Array.from(new Set(Object.values(groups).filter(Boolean))).sort(), [groups]);
  const spec: FastqSpec = { folder: folderName, groups, batch, control: control || null, species: species || null, steps, threads,
    description, setup_origin: origin };
  const ref = home?.references.find((r) => r.species === species);

  async function runCheck() {
    setBusy("check"); setError(null);
    try { setCheck(await api.fastqCheck(spec)); } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  async function start() {
    setBusy("start"); setError(null);
    try {
      const j = await api.fastqStart(spec);
      navigate(`/fastq/${j.id}`);
    } catch (e) { setError((e as Error).message); setBusy(null); }
  }

  async function download(s: string) {
    setError(null);
    try { await api.fastqReference(s); load(); } catch (e) { setError((e as Error).message); }
  }

  // any change after a check makes the check stale
  useEffect(() => { setCheck(null); }, [JSON.stringify(spec)]);

  return (
    <>
      <h1>Start from FASTQ files</h1>
      <p className="muted">
        Raw sequencing files that are already on the server are trimmed, aligned and counted with the code bank's
        FASTQ pipeline (<code>later/</code>: fastp, STAR, Salmon, tximport, rMATS). The counts then go through the normal setup and
        analysis, and the splicing and isoform analyses start on their own once that analysis has finished.
        Already have a counts table and a sample table? <Link to="/">Upload them on the start page</Link>.
      </p>
      {error && <Notice kind="fail">{error}</Notice>}

      <section className="card">
        <div className="row"><h2 style={{ margin: 0 }}>1 · Folder on the server</h2></div>
        <div className="row" style={{ marginTop: 10 }}>
          <select value={folderName} onChange={(e) => pick(e.target.value)} style={{ minWidth: 360 }} aria-label="Folder with FASTQ files">
            <option value="">{home?.folders.length ? "Choose a folder…" : "No folders yet"}</option>
            {home?.folders.map((f) => (
              <option key={f.name} value={f.name} disabled={!f.n_files}>{f.name} ({f.n_files} files, {f.gb} GB)</option>
            ))}
          </select>
          <button className="secondary" onClick={() => { load(); if (folderName) pick(folderName); }}>Refresh</button>
          {busy === "folder" && <span className="spinner" />}
        </div>
        <details className="small" style={{ marginTop: 8 }} open={home !== null && home.folders.length === 0}>
          <summary className="muted">How do files get here?</summary>
          <p>Make one folder per experiment in <code>{home?.incoming ?? "…/incoming"}</code> and copy the FASTQ files into it, for example
            from your computer:</p>
          <pre className="small">rsync -av --progress /path/to/fastq/ you@server:{home?.incoming ?? "…/incoming"}/my_experiment/</pre>
          <p className="muted">Globus transfers will land in the same folder later; nothing on this page changes. File names should say the read:
            <code>_R1</code>/<code>_R2</code> (Illumina's <code>_S1_L001_R1_001</code> works too). One file per read per sample: merge lanes first.</p>
        </details>
      </section>

      {folder && (
        <>
          <section className="card">
            <h2>2 · Describe the experiment <span className="muted small" style={{ fontWeight: 400 }}>(optional: the assistant fills in the groups)</span></h2>
            <textarea rows={3} value={description} onChange={(e) => setDescription(e.target.value)}
              placeholder="Describe the experiment, e.g. “3 RBP knockouts vs wild type, mouse cortex, 3 replicates each; WT_1, KO1_1 and KO2_1 were sequenced in a second batch.”"
              aria-label="Describe the experiment" />
            <div className="row" style={{ marginTop: 8 }}>
              <button className="secondary" onClick={askAssistant} disabled={busy !== null || !description.trim()}>
                {busy === "assist" ? <><span className="spinner" /> Thinking…</> : "Suggest groups"}
              </button>
            </div>
            {assist && (
              <div style={{ marginTop: 10 }}>
                <p className="small"><strong>{assist.origin === "rules" ? "Built-in rules" : `Assistant · ${assist.origin.replace(/^ollama:/, "")}`}:</strong> {assist.note}</p>
                <p className="small muted">Groups: {Array.from(new Set(Object.values(assist.groups))).map((g) =>
                  `${g} (${Object.values(assist.groups).filter((x) => x === g).length})`).join(", ")} · control {assist.control}
                  {assist.species ? ` · ${assist.species}` : ""}{Object.keys(assist.batch).length ? " · batch filled in" : ""}</p>
                <div className="row"><button onClick={useAssistant}>Use this</button><button className="secondary" onClick={() => setAssist(null)}>Dismiss</button></div>
              </div>
            )}
            <p className="muted small" style={{ marginTop: 10 }}>The assistant only sets up groups, control and batch; the processing methods are the code bank's.</p>
          </section>
          <section className="card">
            <h2>Samples and groups <span className={folder.problems.length ? "badge fail" : "badge pass"}>
              {folder.samples.length} samples · {folder.layout} · {folder.gb} GB</span></h2>
            {folder.problems.map((p) => <Notice key={p} kind="fail">{p}</Notice>)}
            <div className="table-wrap" style={{ maxHeight: 420 }}>
              <table>
                <thead><tr><th>Sample</th><th>Files (R1 / R2)</th><th>GB</th><th>Group</th><th>Batch (optional)</th></tr></thead>
                <tbody>{folder.samples.map((s) => (
                  <tr key={s.sample}>
                    <td><strong>{s.sample}</strong></td><td className="small muted">{s.r1 ?? "—"}{s.r2 && <><br />{s.r2}</>}</td><td>{s.gb < 0.01 ? "<0.01" : s.gb}</td>
                    <td><input value={groups[s.sample] ?? ""} size={10} aria-label={`Group of ${s.sample}`}
                      onChange={(e) => setGroups({ ...groups, [s.sample]: e.target.value.trim() })} /></td>
                    <td><input value={batch[s.sample] ?? ""} size={4} aria-label={`Batch of ${s.sample}`}
                      onChange={(e) => setBatch({ ...batch, [s.sample]: e.target.value.trim() })} /></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              <span className="muted">Control group</span>
              <select value={control} onChange={(e) => setControl(e.target.value)}>
                <option value="">—</option>
                {levels.map((l) => <option key={l} value={l}>{l}</option>)}
              </select>
              <span className="muted small">Groups from {origin === "rules" ? "the sample names" : `the assistant (${origin.replace(/^ollama:/, "")})`}; edit any cell.</span>
            </div>
          </section>

        </>
      )}

      {folder && home && (
        <div className="grid-2">
          <section className="card">
            <h2>3 · Reference and steps</h2>
            <label className="field">
              <span>Species (reference)</span>
              <select value={species} onChange={(e) => setSpecies(e.target.value)}>
                <option value="">Choose…</option>
                {home.references.map((r) => <option key={r.species} value={r.species}>{r.label}</option>)}
              </select>
            </label>
            {ref && (
              <p className="small">
                {ref.files_ready
                  ? <>Reference on the server. {ref.star_index ? "STAR index ready." : "The STAR index is built at the start of the first run (about 1 hour, 32 GB memory)."}
                    {" "}{ref.salmon_index ? "Salmon index ready." : "The Salmon index is built when first needed."}</>
                  : ref.download?.status === "running"
                    ? <><span className="spinner" /> Downloading {ref.download.file} from GENCODE…</>
                    : <>{ref.download?.status === "failed" && <Notice kind="fail">Download failed: {ref.download.error}</Notice>}
                      <button className="secondary" onClick={() => download(ref.species)}>Download the reference (~1.5 GB, unpacked ~4 GB)</button></>}
              </p>
            )}
            <fieldset className="steps-pick">
              <legend className="muted small">Steps</legend>
              {home.steps.map((s) => (
                <label key={s.id} className="check-row">
                  <input type="checkbox" checked={steps.includes(s.id)} disabled={home.required_steps.includes(s.id)}
                    onChange={(e) => setSteps(e.target.checked ? [...steps, s.id] : steps.filter((x) => x !== s.id))} />
                  <span><strong>{s.label}</strong> <span className="muted small">{STEP_HELP[s.id]}</span></span>
                </label>
              ))}
            </fieldset>
            <p className="muted small">The splicing (rMATS tables) and isoform (Salmon, DEI/DUI) analyses run on the finished analysis, so they
              use its comparison and covariates and are compared with its gene-level results.</p>
            <label className="field">
              <span>CPU threads</span>
              <input type="number" min={1} max={home.max_threads} value={threads} onChange={(e) => setThreads(Number(e.target.value))} />
              <small>of {home.max_threads} available</small>
            </label>
          </section>

          <section className="card">
            <h2>4 · Check and start</h2>
            <button className="secondary" onClick={runCheck} disabled={busy !== null}>
              {busy === "check" ? <><span className="spinner" /> Checking…</> : "Check"}
            </button>
            {check && (
              <>
                <ul className="check-list" style={{ marginTop: 10 }}>
                  {check.lines.map((l) => <li key={l.text}><Badge status={l.status} label={l.status} /> {l.text}</li>)}
                </ul>
                <p className="muted small">Rough estimate: {check.estimate_hours} h with {check.threads} threads (the first run for a species also builds the indexes).</p>
                <button onClick={start} disabled={!check.ok || busy !== null}>
                  {busy === "start" ? <><span className="spinner" /> Starting…</> : "Start processing"}
                </button>
              </>
            )}
          </section>
        </div>
      )}

      {home && home.jobs.length > 0 && (
        <section className="card">
          <h2>Earlier FASTQ jobs</h2>
          <ul className="check-list">
            {home.jobs.map((j) => (
              <li key={j.id}><Badge status={j.status} /> <Link to={`/fastq/${j.id}`}>{j.folder}</Link>
                <span className="muted small">{j.n_samples} samples · {j.species} · {fmtTime(j.created)}</span></li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function FastqProgress({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  const [job, setJob] = useState<FastqJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stop = false;
    const tick = () => api.fastqJob(jobId).then((j) => {
      if (stop) return;
      setJob(j);
      if (!["success", "failed", "cancelled", "interrupted"].includes(j.status)) setTimeout(tick, 5000);
    }).catch((e) => setError((e as Error).message));
    tick();
    return () => { stop = true; };
  }, [jobId]);

  async function toAnalysis() {
    if (!job) return;
    setBusy(true); setError(null);
    try {
      const up = await api.fastqToDataset(job.id);
      const groups = Array.from(new Set(Object.values(job.groups)));
      const msg = `${job.description ? job.description + "\n\n" : ""}Counts from FASTQ files (${job.folder}; ${job.species}). ` +
        `Compare each group with the control ${job.control}. Groups: ${groups.join(", ")}.`;
      navigate(`/datasets/${up.id}`, { state: { firstMessage: msg } });
    } catch (e) { setError((e as Error).message); setBusy(false); }
  }

  if (!job) return error ? <Notice kind="fail">{error}</Notice> : <p><span className="spinner" /> Loading…</p>;
  const running = job.status === "running" || job.status === "queued";
  const file = (p: string) => `/api/fastq/jobs/${job.id}/files/${p}`;
  return (
    <>
      <p className="small"><Link to="/fastq">← Start from FASTQ</Link></p>
      <h1>FASTQ processing: {job.folder}</h1>
      <p className="muted">{job.n_samples} samples · {job.layout} · {job.gb} GB · {job.species} · control {job.control} · {job.threads} threads ·
        started {job.started ? fmtTime(job.started) : "—"}</p>
      {error && <Notice kind="fail">{error}</Notice>}
      {job.error && <Notice kind="fail">{job.error}</Notice>}

      <section className="card">
        <div className="row">
          <h2 style={{ margin: 0 }}>Progress</h2><Badge status={job.status} /><span className="spacer" />
          {running && <button className="secondary" onClick={() => api.fastqCancel(job.id).then(setJob)}>Cancel</button>}
        </div>
        <div className="progress" style={{ margin: "12px 0" }}><div style={{ width: `${job.progress ?? 0}%` }} /></div>
        <table>
          <thead><tr><th>Step</th><th>Status</th><th>Result</th></tr></thead>
          <tbody>{(job.step_list ?? []).map((s) => (
            <tr key={s.id}>
              <td>{s.label}</td>
              <td>{s.status === "running" ? <><span className="spinner" /> running</> : <Badge status={s.status} />}</td>
              <td className="small">{s.facts.join(" · ")}{s.seconds ? <span className="muted"> · {Math.round(s.seconds / 60)} min</span> : null}</td>
            </tr>
          ))}</tbody>
        </table>
        {job.files && job.files.length > 0 && (
          <p className="small" style={{ marginTop: 10 }}>Files: {job.files.map((f, i) => (
            <span key={f.path}>{i > 0 && " · "}<a href={file(f.path) + "?download=true"}>{f.label}</a></span>))}</p>
        )}
        {job.log_tail && job.log_tail.length > 0 && (
          <details className="small" open={job.status === "failed"}>
            <summary className="muted">Log (last lines)</summary>
            <pre>{job.log_tail.join("\n")}</pre>
          </details>
        )}
      </section>

      {job.status === "success" && (
        <section className="card">
          <h2>Next: the analysis</h2>
          <p>The counts table and sample table are ready ({job.comparisons?.length ? `rMATS: ${job.comparisons.join(", ")}` : "no rMATS"}).
            Set up the analysis as usual; when it finishes, the splicing and isoform analyses start on their own in its <strong>More analyses</strong> tab.</p>
          <button onClick={toAnalysis} disabled={busy}>{busy ? <><span className="spinner" /> Preparing…</> : job.upload_id ? "Open the analysis setup" : "Set up the analysis"}</button>
        </section>
      )}
    </>
  );
}
