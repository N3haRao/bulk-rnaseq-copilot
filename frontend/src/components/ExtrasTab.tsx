import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, extraFileUrl, type ExtraAnalysis, type ExtraColumn, type ExtraForm, type Job } from "../api";
import { Badge, Markdown, Notice } from "./common";
import { fmtNum, fmtP } from "../format";

// Every optional analysis here is drawn from the code bank's manifest (modules.<name>.site): its card,
// form, results tiles, figures and tables. A new code-bank module appears here without any site code.

const RUNNING = new Set(["queued", "running"]);

export default function ExtrasTab({ job }: { job: Job }) {
  const [forms, setForms] = useState<Record<string, ExtraForm> | null>(null);
  const [analyses, setAnalyses] = useState<Record<string, ExtraAnalysis | null>>({});
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await api.extras(job.id);
      setForms(r.forms);
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
  if (!forms) return <p><span className="spinner" /> Loading…</p>;
  if (!Object.keys(forms).length) {
    return <Notice kind="info">The code bank in use offers no optional analyses on a finished run.</Notice>;
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
      {Object.values(forms).map((f) => <ModuleCard key={f.module} job={job} form={f} a={analyses[f.module] ?? null} onStarted={started} />)}
    </>
  );
}

// **bold** in manifest text
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : <span key={i}>{part}</span>);
}

function ModuleCard({ job, form, a, onStarted }: { job: Job; form: ExtraForm; a: ExtraAnalysis | null; onStarted: (a: ExtraAnalysis) => void }) {
  const initial = Object.fromEntries(form.form.map((f) => [f.name, f.default === undefined || f.default === null ? "" : String(f.default)]));
  const [values, setValues] = useState<Record<string, string>>(initial);
  const [files, setFiles] = useState<Record<string, File[]>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const missing = form.inputs.some((i) => i.required && !(files[i.name]?.length));

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true); setErr(null);
    try {
      onStarted(await api.startExtra(job.id, form.module, files, values));
      setShowForm(false);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  const running = a && RUNNING.has(a.status);
  return (
    <section className="card">
      <div className="row">
        <h2 style={{ margin: 0 }}>{form.title}</h2>
        {a && <Badge status={a.status} />}
        <span className="spacer" />
        {a && !running && !showForm && <button className="secondary" onClick={() => setShowForm(true)}>Run again</button>}
      </div>
      <p className="muted small">{inline(form.description)}</p>
      {(!a || showForm) && !running && (
        <form onSubmit={submit}>
          {form.inputs.map((i) => (
            <label className="field" key={i.name}>
              <span>{i.label ?? i.name}</span>
              <input type="file" multiple={!!i.multiple} accept={(i.accept ?? []).join(",")}
                onChange={(e) => setFiles((cur) => ({ ...cur, [i.name]: Array.from(e.target.files ?? []) }))} />
              {i.help && <small>{i.help}</small>}
            </label>
          ))}
          {form.form.length > 0 && (
            <div className="grid-2">
              {form.form.map((f) => (
                <label className="field" key={f.name}>
                  <span>{f.label ?? f.name}</span>
                  <Field f={f} value={values[f.name] ?? ""} onChange={(v) => setValues((cur) => ({ ...cur, [f.name]: v }))} />
                  {f.help && <small>{f.help}</small>}
                </label>
              ))}
            </div>
          )}
          {err && <Notice kind="fail">{err}</Notice>}
          <div className="row">
            <button type="submit" disabled={busy || missing}>{busy ? <><span className="spinner" /> Uploading…</> : `Run ${form.title.toLowerCase()}`}</button>
            {a && <button type="button" className="link" onClick={() => setShowForm(false)}>Cancel</button>}
          </div>
        </form>
      )}
      {a && <AnalysisView jobId={job.id} a={a} />}
    </section>
  );
}

function Field({ f, value, onChange }: { f: ExtraForm["form"][number]; value: string; onChange: (v: string) => void }) {
  if (f.type === "enum" || f.type === "run_group") {
    return (
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {(f.options ?? []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    );
  }
  if (f.type === "bool") {
    return <input type="checkbox" checked={value === "true"} onChange={(e) => onChange(e.target.checked ? "true" : "false")} />;
  }
  if (f.type === "number" || f.type === "integer") {
    return <input type="number" step={f.type === "integer" ? 1 : "any"} min={f.min} max={f.max} value={value} onChange={(e) => onChange(e.target.value)} />;
  }
  return <input type="text" value={value} onChange={(e) => onChange(e.target.value)} />;
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
  const warn = (a.validation?.checks ?? []).filter((c) => c.status === "warning");
  return (
    <div style={{ marginTop: 12 }}>
      {a.status === "success_with_errors" && <Notice kind="warning">Finished, but an optional step failed (see the methods text and the download).</Notice>}
      {a.note && <Notice kind="info">{a.note}</Notice>}
      {warn.map((c) => <Notice key={c.id} kind="warning">{c.message}</Notice>)}
      {(a.tiles ?? []).length > 0 && (
        <div className="tiles">
          {a.tiles!.map((t) => (
            <div className="tile" key={t.label}>
              <div className={"value " + (t.style ?? "")}>{typeof t.value === "number" ? fmtNum(t.value, 0) : (t.value ?? "–")}</div>
              <div className="label">{t.label}</div>
            </div>
          ))}
        </div>
      )}
      {(a.headline ?? []).map((h, i) => <p key={i} className="small">{inline(h)}</p>)}
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
      {(a.top_tables ?? []).map((t) => <TopTable key={t.title} t={t} />)}
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

function cell(c: ExtraColumn, r: Record<string, string>): { text: string; cls: string } {
  let raw = c.template ? c.template.replace(/\{(\w+)\}/g, (_, k) => r[k] ?? "") : (r[c.value ?? ""] ?? "");
  if ((raw === "" || raw === "NA") && c.fallback) raw = r[c.fallback] ?? "";
  const n = Number(raw);
  let text = raw;
  if (c.format === "num0") text = fmtNum(n, 0);
  else if (c.format === "num2") text = fmtNum(n, 2);
  else if (c.format === "p") text = fmtP(n);
  else if (c.format === "flag") text = ["TRUE", "True", "true"].includes(raw) ? "yes" : "";
  const num = c.format === "num0" || c.format === "num2" || c.format === "p";
  const cls = [num ? "num" : "", c.style === "small" ? "small" : "", c.style === "sign" ? (n > 0 ? "up" : n < 0 ? "down" : "") : ""].join(" ").trim();
  return { text, cls };
}

function TopTable({ t }: { t: NonNullable<ExtraAnalysis["top_tables"]>[number] }) {
  return (
    <>
      <h3>{t.title}</h3>
      {t.rows.length ? (
        <div className="table-wrap" style={{ maxHeight: 360 }}>
          <table>
            <thead><tr>{t.columns.map((c) => <th key={c.label} className={c.format && c.format !== "flag" ? "num" : ""}>{c.label}</th>)}</tr></thead>
            <tbody>
              {t.rows.map((r, i) => (
                <tr key={i}>{t.columns.map((c) => { const x = cell(c, r); return <td key={c.label} className={x.cls}>{x.text}</td>; })}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">{t.empty}</p>}
    </>
  );
}
