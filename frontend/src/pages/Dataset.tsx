import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, type AgentSetup, type CheckResult, type Settings, type Upload } from "../api";
import { CheckList, type Explanations, Notice, SourceTag, Stepper } from "../components/common";

type ParamSpec = { type: string; values?: string[]; default: unknown };

const ORGANISM_LABELS: Record<string, string> = {
  auto: "Detect from gene IDs",
  human: "Human",
  mouse: "Mouse",
  other: "Other (no gene names or pathways)",
};

function initialSettings(up: Upload): Settings {
  const ex = (up.example_settings ?? {}) as Partial<Settings> & { covariates?: string[] | string };
  const p = up.proposal;
  const exCovs = ex.covariates === undefined ? undefined : Array.isArray(ex.covariates) ? ex.covariates : [ex.covariates];
  return {
    condition: ex.condition ?? p.condition ?? "",
    reference_level: ex.reference_level ?? p.reference_level ?? "",
    test_level: ex.test_level ?? p.test_level ?? "",
    covariates: exCovs ?? p.covariates,
    organism: ex.organism ?? "auto",
    run_name: up.example ?? "analysis",
    advanced: {},
  };
}

function rulesSuggestion(up: Upload): AgentSetup {
  const p = up.proposal;
  return {
    source: "rules", model: null, condition: p.condition, reference_level: p.reference_level, test_level: p.test_level,
    covariates: p.covariates, explanation: p.reasons.join(" "), reasons: p.reasons,
    questions: p.reference_guessed ? ["Please check that the control group is right."] : [], warnings: p.warnings,
  };
}

function withSuggestion(s: Settings, sug: AgentSetup): Settings {
  return {
    ...s,
    condition: sug.condition ?? s.condition,
    reference_level: sug.reference_level ?? s.reference_level,
    test_level: sug.test_level ?? s.test_level,
    covariates: sug.covariates,
  };
}

function sameSetup(s: Settings, sug: AgentSetup | null): boolean {
  return !!sug && s.condition === sug.condition && s.reference_level === sug.reference_level &&
    s.test_level === sug.test_level && [...s.covariates].sort().join("|") === [...sug.covariates].sort().join("|");
}

export default function Dataset() {
  const { uploadId = "" } = useParams();
  const navigate = useNavigate();
  const [upload, setUpload] = useState<Upload | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [checkedFor, setCheckedFor] = useState<string>("");
  const [params, setParams] = useState<Record<string, ParamSpec>>({});
  const [busy, setBusy] = useState<"check" | "run" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<AgentSetup | null>(null);
  const [agentPending, setAgentPending] = useState(false);
  const [explain, setExplain] = useState<Explanations>({});
  // Increases with every change the user makes, so a slow assistant answer never overwrites their edits
  const editSeq = useRef(0);

  const runCheck = useCallback(async (s: Settings) => {
    setBusy("check");
    setError(null);
    setExplain({});
    try {
      const res = await api.check(uploadId, s);
      setCheck(res);
      setCheckedFor(JSON.stringify(s));
      if (res.checks.some((c) => c.status !== "pass")) {
        api.agentExplainCheck(uploadId)
          .then((ex) => setExplain(Object.fromEntries(ex.items.map((i) => [i.id, i]))))
          .catch(() => undefined);
      }
      return res;
    } catch (e) {
      setError((e as Error).message);
      return null;
    } finally {
      setBusy(null);
    }
  }, [uploadId]);

  const askAssistant = useCallback((base: Settings, condition: string | undefined, autoApply: boolean) => {
    const seq = editSeq.current;
    setAgentPending(true);
    api.agentSetup(uploadId, condition)
      .then((sug) => {
        if (editSeq.current !== seq) return; // the user changed something meanwhile
        setSuggestion(sug);
        if (autoApply && sug.source === "model" && !sameSetup(base, sug)) {
          const next = withSuggestion(base, sug);
          setSettings(next);
          runCheck(next);
        }
      })
      .catch(() => undefined)
      .finally(() => setAgentPending(false));
  }, [uploadId, runCheck]);

  useEffect(() => {
    api.options().then((o) => setParams(o.parameters)).catch(() => undefined);
    api.getUpload(uploadId)
      .then((up) => {
        setUpload(up);
        const s = initialSettings(up);
        setSettings(s);
        setSuggestion(rulesSuggestion(up));
        if (s.condition && s.reference_level && s.test_level) runCheck(s);
        // Example datasets come with a curated setup: show the assistant's view, do not apply it
        askAssistant(s, undefined, !up.example_settings);
      })
      .catch((e) => setError((e as Error).message));
  }, [uploadId, runCheck, askAssistant]);

  const groupColumns = useMemo(
    () => (upload?.columns ?? []).filter((c) => c.n_levels >= 2 && !c.unique_per_sample && !c.numeric && c.n_missing === 0),
    [upload],
  );
  const conditionColumn = upload?.columns.find((c) => c.name === settings?.condition);
  const levels = conditionColumn ? Object.keys(conditionColumn.levels) : [];
  const covariateOptions = (upload?.columns ?? []).filter(
    (c) => c.name !== settings?.condition && c.n_levels >= 2 && !c.unique_per_sample,
  );
  const sampleIds = upload ? upload.metadata_preview.rows.map((r) => r[0]) : [];
  const stale = check !== null && checkedFor !== JSON.stringify(settings);

  if (error && !upload) return <Notice kind="fail">{error}</Notice>;
  if (!upload || !settings) return <p><span className="spinner" /> Loading your files…</p>;

  const set = (patch: Partial<Settings>) => {
    editSeq.current += 1;
    setSettings({ ...settings, ...patch });
  };
  const setAdvanced = (key: string, value: unknown) => {
    const adv = { ...settings.advanced };
    if (value === undefined || value === params[key]?.default) delete adv[key];
    else adv[key] = value;
    set({ advanced: adv });
  };

  async function changeCondition(condition: string) {
    // Ask the backend for a fresh proposal (control group, covariates) for this column
    editSeq.current += 1;
    try {
      const up = await api.getUpload(uploadId, condition);
      setUpload(up);
      const rules = rulesSuggestion(up);
      const next = withSuggestion({ ...settings!, condition }, rules);
      setSettings(next);
      setSuggestion(rules);
      askAssistant(next, condition, true);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function checkAndRun() {
    const s = settings!;
    let res = check;
    if (!res || stale) res = await runCheck(s);
    if (!res || res.status === "fail") return;
    setBusy("run");
    try {
      const job = await api.startRun(uploadId, s, {
        source: upload!.example_settings && !sameSetup(s, suggestion) ? "example" : suggestion?.source ?? "rules",
        model: suggestion?.model ?? null,
        changed_by_user: !sameSetup(s, suggestion),
      });
      navigate(`/runs/${job.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  const fileChecks = check?.checks.filter((c) => c.group === "files") ?? [];
  const designChecks = check?.checks.filter((c) => c.group === "design") ?? [];
  const sug = suggestion ?? rulesSuggestion(upload);
  const canApply = !sameSetup(settings, sug) && sug.condition;
  const collections = params.gene_set_collections;
  const chosenCollections = (settings.advanced.gene_set_collections as string[] | undefined)
    ?? (collections?.default as string[] | undefined) ?? [];
  const excluded = (settings.advanced.exclude_samples as string[] | undefined) ?? [];

  return (
    <>
      <Stepper current={1} />
      <h1>Check your files and set up the comparison</h1>
      <p className="muted">
        Counts: <strong>{upload.counts_original_name}</strong> · Samples: <strong>{upload.metadata_original_name}</strong>
        {" "}({upload.metadata_preview.n_rows} samples)
      </p>
      {error && <Notice kind="fail">{error}</Notice>}

      <section className="card">
        <h2>1. File check</h2>
        {busy === "check" && !check && <p><span className="spinner" /> Checking your files…</p>}
        {check && (
          <>
            {check.checks.some((c) => c.group === "files" && c.status === "fail") ? (
              <Notice kind="fail">Your files need fixing before the analysis can run. See the problems below.</Notice>
            ) : (
              <Notice kind="pass">Your files look good: raw counts, and sample names match between the two files.</Notice>
            )}
            <CheckList checks={fileChecks} explain={explain} />
          </>
        )}
        {!check && busy !== "check" && (
          <p className="muted">The files are checked once the comparison below is chosen.</p>
        )}
        <details style={{ marginTop: 12 }}>
          <summary className="muted small">Preview your files</summary>
          <h3 style={{ marginTop: 12 }}>Sample table</h3>
          <div className="table-wrap" style={{ maxHeight: 280 }}>
            <table>
              <thead><tr>{upload.metadata_preview.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {upload.metadata_preview.rows.slice(0, 50).map((r, i) => (
                  <tr key={i}>{r.map((v, j) => <td key={j}>{v}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3 style={{ marginTop: 12 }}>Counts table (first rows)</h3>
          <div className="table-wrap">
            <table>
              <thead><tr>{upload.counts_preview.columns.slice(0, 9).map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {upload.counts_preview.first_rows.map((r, i) => (
                  <tr key={i}>{r.slice(0, 9).map((v, j) => <td key={j} className={j ? "num" : ""}>{v}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
          {upload.counts_preview.n_columns > 9 && (
            <p className="muted small">…and {upload.counts_preview.n_columns - 9} more columns.</p>
          )}
        </details>
      </section>

      <section className="card">
        <h2>2. What do you want to compare?</h2>
        <Notice kind="info">
          <div className="row" style={{ gap: 4 }}>
            <strong>Suggested setup</strong>
            <SourceTag source={sug.source} model={sug.model} />
            {agentPending && <span className="muted small" style={{ marginLeft: 8 }}><span className="spinner" /> The assistant is reading your sample table…</span>}
          </div>
          <p style={{ margin: "6px 0 0" }}>{sug.explanation}</p>
          {sug.questions.length > 0 && (
            <ul>{sug.questions.map((q) => <li key={q}><strong>Please check:</strong> {q}</li>)}</ul>
          )}
          {canApply && (
            <div className="row" style={{ marginTop: 8 }}>
              <span className="small">
                Suggestion: <strong>{sug.test_level}</strong> vs <strong>{sug.reference_level}</strong> in <strong>{sug.condition}</strong>
                {sug.covariates.length > 0 && <>, accounting for <strong>{sug.covariates.join(", ")}</strong></>}
              </span>
              <button className="secondary" onClick={() => { editSeq.current += 1; setSettings(withSuggestion(settings, sug)); }}>
                Use this suggestion
              </button>
            </div>
          )}
          {sug.source === "rules" && sug.fallback_reason && !agentPending && (
            <p className="muted small" style={{ margin: "6px 0 0" }}>Assistant not used: {sug.fallback_reason}</p>
          )}
        </Notice>
        {sug.warnings.map((w) => <Notice key={w} kind="warning">{w}</Notice>)}

        <label className="field">
          <span>Which column describes the groups?</span>
          <select value={settings.condition} onChange={(e) => changeCondition(e.target.value)}>
            {!settings.condition && <option value="">Choose a column…</option>}
            {groupColumns.map((c) => (
              <option key={c.name} value={c.name}>{c.name} ({Object.keys(c.levels).join(", ")})</option>
            ))}
          </select>
        </label>

        {conditionColumn && (
          <div className="grid-2">
            <label className="field">
              <span>Control (baseline) group</span>
              <select value={settings.reference_level} onChange={(e) => {
                const ref = e.target.value;
                set({ reference_level: ref, test_level: settings.test_level === ref ? levels.find((l) => l !== ref) ?? "" : settings.test_level });
              }}>
                {levels.map((l) => <option key={l} value={l}>{l} ({conditionColumn.levels[l]} samples)</option>)}
              </select>
              <small>Fold changes are reported relative to this group.</small>
            </label>
            <label className="field">
              <span>Compare against</span>
              <select value={settings.test_level} onChange={(e) => set({ test_level: e.target.value })}>
                {levels.filter((l) => l !== settings.reference_level).map((l) => (
                  <option key={l} value={l}>{l} ({conditionColumn.levels[l]} samples)</option>
                ))}
              </select>
              <small>Positive fold change = higher in this group.</small>
            </label>
          </div>
        )}

        <div className="field">
          <span style={{ fontWeight: 600, display: "block", marginBottom: 4 }}>Account for</span>
          {covariateOptions.length === 0 ? (
            <p className="muted small">No other columns to account for.</p>
          ) : (
            <div className="checks-inline">
              {covariateOptions.map((c) => (
                <label key={c.name}>
                  <input type="checkbox" checked={settings.covariates.includes(c.name)}
                    onChange={(e) => set({
                      covariates: e.target.checked
                        ? [...settings.covariates, c.name]
                        : settings.covariates.filter((x) => x !== c.name),
                    })} />
                  {c.name}{c.numeric ? " (numeric)" : ""}
                </label>
              ))}
            </div>
          )}
          <small className="muted">
            Tick batch, sex, donor/animal (for paired samples) or other factors that differ between samples but are not
            what you are testing.
          </small>
        </div>

        <div className="grid-2">
          <label className="field">
            <span>Organism</span>
            <select value={settings.organism} onChange={(e) => set({ organism: e.target.value })}>
              {(params.organism?.values ?? ["auto", "human", "mouse", "other"]).map((o) => (
                <option key={o} value={o}>{ORGANISM_LABELS[o] ?? o}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Name for this analysis</span>
            <input type="text" value={settings.run_name} maxLength={40} onChange={(e) => set({ run_name: e.target.value })} />
          </label>
        </div>

        <details>
          <summary>Advanced settings</summary>
          <div className="grid-2" style={{ marginTop: 12 }}>
            <label className="field">
              <span>Adjusted p-value cutoff</span>
              <input type="number" step="0.01" min="0.0001" max="1"
                value={(settings.advanced.padj_cutoff as number | undefined) ?? (params.padj_cutoff?.default as number) ?? 0.05}
                onChange={(e) => setAdvanced("padj_cutoff", e.target.value === "" ? undefined : Number(e.target.value))} />
            </label>
            <label className="field">
              <span>Minimum |log2 fold change|</span>
              <input type="number" step="0.1" min="0"
                value={(settings.advanced.log2fc_cutoff as number | undefined) ?? (params.log2fc_cutoff?.default as number) ?? 1}
                onChange={(e) => setAdvanced("log2fc_cutoff", e.target.value === "" ? undefined : Number(e.target.value))} />
              <small>1 = at least a 2-fold change. 0.58 ≈ 1.5-fold.</small>
            </label>
          </div>
          <label className="field">
            <span>
              <input type="checkbox" checked={(settings.advanced.run_enrichment as boolean | undefined) ?? true}
                onChange={(e) => setAdvanced("run_enrichment", e.target.checked)} /> Run pathway enrichment
            </span>
          </label>
          {collections?.values && (
            <div className="field">
              <span style={{ fontWeight: 600, display: "block", marginBottom: 4 }}>Gene-set collections</span>
              <div className="checks-inline">
                {collections.values.map((v) => (
                  <label key={v}>
                    <input type="checkbox" checked={chosenCollections.includes(v)} onChange={(e) => {
                      const next = e.target.checked ? [...chosenCollections, v] : chosenCollections.filter((x) => x !== v);
                      const same = JSON.stringify([...next].sort()) === JSON.stringify([...((collections.default as string[]) ?? [])].sort());
                      setAdvanced("gene_set_collections", same ? undefined : next);
                    }} />
                    {v}
                  </label>
                ))}
              </div>
            </div>
          )}
          <div className="field">
            <span style={{ fontWeight: 600, display: "block", marginBottom: 4 }}>Leave samples out</span>
            <div className="checks-inline">
              {sampleIds.map((sid) => (
                <label key={sid}>
                  <input type="checkbox" checked={excluded.includes(sid)} onChange={(e) => {
                    const next = e.target.checked ? [...excluded, sid] : excluded.filter((x) => x !== sid);
                    setAdvanced("exclude_samples", next.length ? next : undefined);
                  }} />
                  {sid}
                </label>
              ))}
            </div>
            <small className="muted">Only for samples with a known problem (e.g. failed library). The reason belongs in your notes.</small>
          </div>
        </details>
      </section>

      <section className="card">
        <h2>3. Check the setup and run</h2>
        {check && !stale && designChecks.length > 0 && <CheckList checks={designChecks} explain={explain} />}
        {stale && <Notice kind="info">You changed the setup. It will be checked again before the analysis starts.</Notice>}
        {check && !stale && check.status === "fail" && (
          <Notice kind="fail">The analysis cannot run with these files or settings yet. Fix the problems marked above.</Notice>
        )}
        <div className="row" style={{ marginTop: 12 }}>
          <button onClick={checkAndRun}
            disabled={busy !== null || !settings.condition || !settings.reference_level || !settings.test_level || (!!check && !stale && check.status === "fail")}>
            {busy === "run" ? <><span className="spinner" /> Starting…</> : busy === "check" ? <><span className="spinner" /> Checking…</> : "Run analysis"}
          </button>
          <button className="secondary" disabled={busy !== null || !settings.condition} onClick={() => runCheck(settings)}>
            Check again
          </button>
          <span className="muted small">
            Comparing <strong>{settings.test_level || "?"}</strong> vs <strong>{settings.reference_level || "?"}</strong>
            {settings.covariates.length > 0 && <> accounting for <strong>{settings.covariates.join(", ")}</strong></>}
          </span>
        </div>
      </section>
    </>
  );
}
