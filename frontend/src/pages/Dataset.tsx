import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, type CheckResult, type Settings, type SetupChatItem, type SetupChatState, type Upload } from "../api";
import { CheckList, type Explanations, Notice, SourceTag, Stepper } from "../components/common";

type ParamSpec = { type: string; values?: string[]; default: unknown; description?: string };

const ORGANISM_LABELS: Record<string, string> = {
  auto: "Detect from gene IDs",
  human: "Human",
  mouse: "Mouse",
  other: "Other (no gene names or pathways)",
};

const SETTING_LABELS: Record<string, string> = {
  padj_cutoff: "Adjusted p-value cutoff", log2fc_cutoff: "Minimum |log2 fold change|", lfc_shrinkage: "Fold-change shrinkage",
  min_count: "Minimum reads per gene", run_enrichment: "Pathway analysis", gene_set_collections: "Gene-set collections",
  exclude_samples: "Samples left out", n_label_volcano: "Genes labelled on the volcano plot",
  volcano_label_genes: "Genes to label on the volcano plot", n_heatmap_genes: "Genes in the heatmap",
  plot_palette: "Colour palette", group_colours: "Group colours", up_colour: "Colour for higher genes",
  down_colour: "Colour for lower genes", pca_colour_by: "PCA coloured by", pca_shape_by: "PCA shapes by",
  pca_label_samples: "Sample names on the PCA",
};

const PALETTE_LABELS: Record<string, string> = {
  okabe_ito: "Colour-blind safe (default)", set2: "Soft colours", viridis: "Viridis", greys: "Greys",
};

const EXAMPLE_PROMPTS = [
  "Also account for sex.",
  "Use a stricter cutoff: adjusted p-value below 0.01.",
  "Colour the PCA by batch and use shapes for sex.",
  "Label Gapdh, Actb and Krt5 on the volcano plot.",
];

function fmtValue(v: unknown): string {
  if (Array.isArray(v)) return v.join(", ") || "none";
  if (typeof v === "boolean") return v ? "on" : "off";
  return String(v);
}

const sameSettings = (a: Settings | null | undefined, b: Settings | null | undefined) =>
  !!a && !!b && JSON.stringify({ ...a, covariates: [...a.covariates].sort() }) ===
    JSON.stringify({ ...b, covariates: [...b.covariates].sort() });

export default function Dataset() {
  const { uploadId = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const firstMessage = (location.state as { firstMessage?: string } | null)?.firstMessage;
  const [upload, setUpload] = useState<Upload | null>(null);
  const [chat, setChat] = useState<SetupChatState | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [checkedFor, setCheckedFor] = useState<string>("");
  const [params, setParams] = useState<Record<string, ParamSpec>>({});
  const [busy, setBusy] = useState<"check" | "run" | null>(null);
  const [thinking, setThinking] = useState(false);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [explain, setExplain] = useState<Explanations>({});
  const started = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);

  const runCheck = useCallback(async (s: Settings) => {
    if (!s.condition || !s.reference_level || !s.test_level) return null;
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

  const send = useCallback(async (text: string, current?: Settings) => {
    setThinking(true);
    setError(null);
    setChat((c) => c && { ...c, transcript: [...c.transcript, { role: "user", text: text || "(no description given)" }] });
    try {
      const st = await api.sendSetupChat(uploadId, text, current);
      setChat(st);
      setSettings(st.setup);
      runCheck(st.setup);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setThinking(false);
    }
  }, [uploadId, runCheck]);

  useEffect(() => {
    api.options().then((o) => setParams(o.parameters)).catch(() => undefined);
    Promise.all([api.getUpload(uploadId), api.setupChat(uploadId)])
      .then(([up, st]) => {
        setUpload(up);
        setChat(st);
        setSettings(st.setup);
        runCheck(st.setup);
        if (st.transcript.length === 0 && !started.current) {
          started.current = true;
          send(firstMessage ?? "");
        }
      })
      .catch((e) => setError((e as Error).message));
  }, [uploadId, runCheck, send, firstMessage]);

  useEffect(() => { bottom.current?.scrollIntoView({ block: "nearest" }); }, [chat?.transcript.length, thinking]);

  const lastSetup = useMemo(() => {
    const answers = (chat?.transcript ?? []).filter((t) => t.role === "assistant" && t.setup);
    return answers.length ? answers[answers.length - 1].setup! : chat?.setup ?? null;
  }, [chat]);
  const edited = !!settings && !!lastSetup && !sameSettings(settings, lastSetup);

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
  if (!upload || !settings || !chat) return <p><span className="spinner" /> Loading your files…</p>;

  const set = (patch: Partial<Settings>) => setSettings({ ...settings, ...patch });
  const setAdvanced = (key: string, value: unknown) => {
    const adv = { ...settings.advanced };
    if (value === undefined || value === "" || JSON.stringify(value) === JSON.stringify(params[key]?.default)) delete adv[key];
    else adv[key] = value;
    set({ advanced: adv });
  };

  async function changeCondition(condition: string) {
    try {
      const up = await api.getUpload(uploadId, condition);
      setUpload(up);
      const p = up.proposal;
      setSettings({ ...settings!, condition, reference_level: p.reference_level ?? "", test_level: p.test_level ?? "",
        covariates: p.covariates });
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function sendInput() {
    const text = input.trim();
    if (!text || thinking) return;
    setInput("");
    send(text, edited ? settings! : undefined);
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendInput(); }
  }

  async function approveAndRun() {
    const s = settings!;
    let res = check;
    if (!res || stale) res = await runCheck(s);
    if (!res || res.status === "fail") return;
    setBusy("run");
    try {
      const job = await api.startRun(uploadId, s, {
        source: chat!.source === "model" ? "setup_chat" : "rules", model: chat!.model, setup_chat: chat!.id,
        changed_by_user: edited,
      });
      navigate(`/runs/${job.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  const fileChecks = check?.checks.filter((c) => c.group === "files") ?? [];
  const designChecks = check?.checks.filter((c) => c.group === "design") ?? [];
  const fileFail = fileChecks.some((c) => c.status === "fail");
  const collections = params.gene_set_collections;
  const chosenCollections = (settings.advanced.gene_set_collections as string[] | undefined)
    ?? (collections?.default as string[] | undefined) ?? [];
  const excluded = (settings.advanced.exclude_samples as string[] | undefined) ?? [];
  const columnNames = upload.metadata_preview.columns.slice(1);
  const groupColours = Object.fromEntries(((settings.advanced.group_colours as string[] | undefined) ?? [])
    .map((e) => e.split("=") as [string, string]));
  const canRun = !!settings.condition && !!settings.reference_level && !!settings.test_level &&
    !(check && !stale && check.status === "fail");

  return (
    <>
      <Stepper current={1} />
      <h1>Set up your analysis</h1>
      <p className="muted">
        Counts: <strong>{upload.counts_original_name}</strong> · Samples: <strong>{upload.metadata_original_name}</strong>
        {" "}({upload.metadata_preview.n_rows} samples)
      </p>
      {error && <Notice kind="fail">{error}</Notice>}

      <div className="setup-layout">
        <section className="card chat setup-chat">
          <div className="row">
            <h2 style={{ margin: 0 }}>Setup assistant</h2>
            <SourceTag source={chat.source} model={chat.model} />
            <span className="spacer" />
            <button className="link small" disabled={thinking} onClick={async () => {
              const st = await api.resetSetupChat(uploadId);
              setChat(st); setSettings(st.setup);
            }}>Start over</button>
          </div>
          <div className="chat-log">
            {chat.transcript.map((t, i) => <SetupMessage key={i} item={t} />)}
            {thinking && <p className="muted small"><span className="spinner" /> The assistant is reading your sample table… (up to a minute the first time)</p>}
            <div ref={bottom} />
          </div>
          {chat.transcript.length > 0 && chat.transcript.length < 3 && !thinking && (
            <div className="suggestions">
              {EXAMPLE_PROMPTS.map((s) => <button key={s} className="secondary small" onClick={() => setInput(s)}>{s}</button>)}
            </div>
          )}
          <div className="chat-input">
            <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} rows={2} disabled={thinking}
              placeholder="Ask for changes: covariates, groups, cutoffs, samples to leave out, plot colours or shapes… (Enter to send)"
              aria-label="Message to the setup assistant" />
            <button onClick={sendInput} disabled={thinking || !input.trim()}>Send</button>
          </div>
          <p className="muted small" style={{ marginBottom: 0 }}>
            The assistant can only change the code bank's settings. The methods (DESeq2, fgsea) are fixed.
          </p>
        </section>

        <section className="card setup-card">
          <h2>Proposed setup</h2>
          <dl className="setup-summary">
            <dt>Compare</dt>
            <dd><strong>{settings.test_level || "?"}</strong> vs <strong>{settings.reference_level || "?"}</strong>
              {conditionColumn && <span className="muted small"> ({conditionColumn.levels[settings.test_level] ?? 0} vs {conditionColumn.levels[settings.reference_level] ?? 0} samples, column {settings.condition})</span>}
            </dd>
            <dt>Accounting for</dt>
            <dd>{settings.covariates.length ? settings.covariates.join(", ") : <span className="muted">nothing else</span>}</dd>
            <dt>Organism</dt>
            <dd>{ORGANISM_LABELS[settings.organism] ?? settings.organism}</dd>
            {Object.entries(settings.advanced).map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{SETTING_LABELS[k] ?? k}</dt><dd>{fmtValue(v)}</dd>
              </div>
            ))}
          </dl>
          {edited && <Notice kind="info">You edited the setup. The assistant will start from your version.</Notice>}
          <div className="check-status">
            {busy === "check" && <p className="small"><span className="spinner" /> Checking the files and the design…</p>}
            {check && !stale && busy !== "check" && (
              check.status === "fail"
                ? <Notice kind="fail">{fileFail ? "The files need fixing before the analysis can run." : "This setup cannot run yet."} See the checks below.</Notice>
                : <Notice kind={check.status === "warning" ? "warning" : "pass"}>
                    {check.status === "warning" ? `Ready to run, with ${check.n_warning} warning(s) below.` : "Files and design checked: ready to run."}
                  </Notice>
            )}
            {stale && <p className="muted small">The setup changed; it is checked again before the run starts.</p>}
          </div>
          {check && !stale && designChecks.some((c) => c.status !== "pass") && (
            <CheckList checks={designChecks.filter((c) => c.status !== "pass")} explain={explain} />
          )}
          <div className="row" style={{ marginTop: 12 }}>
            <button onClick={approveAndRun} disabled={busy !== null || thinking || !canRun}>
              {busy === "run" ? <><span className="spinner" /> Starting…</> : "Approve and run"}
            </button>
            <button className="secondary" disabled={busy !== null || !settings.condition} onClick={() => runCheck(settings)}>
              Check again
            </button>
          </div>
          <p className="muted small">Nothing runs until you approve. The run records who proposed the setup and whether you changed it.</p>
        </section>
      </div>

      <section className="card">
        <h2>File check</h2>
        {busy === "check" && !check && <p><span className="spinner" /> Checking your files…</p>}
        {check && (
          <>
            {fileFail
              ? <Notice kind="fail">Your files need fixing before the analysis can run. See the problems below.</Notice>
              : <Notice kind="pass">Your files look good: raw counts, and sample names match between the two files.</Notice>}
            <CheckList checks={fileChecks} explain={explain} />
          </>
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
        <details>
          <summary><strong>Edit the setup directly</strong> <span className="muted small">(optional: everything the assistant can change)</span></summary>

          <label className="field" style={{ marginTop: 12 }}>
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

          <h3>Statistics</h3>
          <div className="grid-2">
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
            <small className="muted">Only for samples with a known problem (e.g. failed library).</small>
          </div>

          {params.plot_palette && (
            <>
              <h3>Figures</h3>
              <p className="muted small">These change how the figures look, never the results.</p>
              <div className="grid-2">
                <label className="field">
                  <span>Colour palette for groups</span>
                  <select value={(settings.advanced.plot_palette as string | undefined) ?? (params.plot_palette.default as string)}
                    onChange={(e) => setAdvanced("plot_palette", e.target.value)}>
                    {(params.plot_palette.values ?? []).map((v) => <option key={v} value={v}>{PALETTE_LABELS[v] ?? v}</option>)}
                  </select>
                </label>
                {params.pca_colour_by && (
                  <label className="field">
                    <span>Colour the PCA by</span>
                    <select value={(settings.advanced.pca_colour_by as string | undefined) ?? ""}
                      onChange={(e) => setAdvanced("pca_colour_by", e.target.value || undefined)}>
                      <option value="">The compared groups</option>
                      {columnNames.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </label>
                )}
                {params.pca_shape_by && (
                  <label className="field">
                    <span>Point shapes on the PCA</span>
                    <select value={(settings.advanced.pca_shape_by as string | undefined) ?? ""}
                      onChange={(e) => setAdvanced("pca_shape_by", e.target.value || undefined)}>
                      <option value="">First covariate (default)</option>
                      <option value="none">No shapes</option>
                      {columnNames.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </label>
                )}
                {params.pca_label_samples && (
                  <label className="field">
                    <span>
                      <input type="checkbox" checked={(settings.advanced.pca_label_samples as boolean | undefined) ?? true}
                        onChange={(e) => setAdvanced("pca_label_samples", e.target.checked)} /> Sample names on the PCA
                    </span>
                  </label>
                )}
              </div>
              {params.group_colours && conditionColumn && (
                <div className="field">
                  <span style={{ fontWeight: 600, display: "block", marginBottom: 4 }}>Colour for each group</span>
                  <div className="checks-inline">
                    {levels.map((lv) => (
                      <label key={lv}>
                        <input type="color" value={groupColours[lv] ?? "#888888"} onChange={(e) => {
                          const next = { ...groupColours, [lv]: e.target.value };
                          setAdvanced("group_colours", Object.entries(next).map(([k, v]) => `${k}=${v}`));
                        }} /> {lv}{groupColours[lv] ? "" : " (palette)"}
                      </label>
                    ))}
                    {Object.keys(groupColours).length > 0 && (
                      <button className="link small" onClick={() => setAdvanced("group_colours", undefined)}>Use the palette</button>
                    )}
                  </div>
                </div>
              )}
              <div className="grid-2">
                {params.up_colour && (
                  <label className="field">
                    <span>Genes higher in {settings.test_level || "the test group"}</span>
                    <input type="color" value={(settings.advanced.up_colour as string | undefined) ?? (params.up_colour.default as string)}
                      onChange={(e) => setAdvanced("up_colour", e.target.value.toUpperCase())} />
                  </label>
                )}
                {params.down_colour && (
                  <label className="field">
                    <span>Genes lower in {settings.test_level || "the test group"}</span>
                    <input type="color" value={(settings.advanced.down_colour as string | undefined) ?? (params.down_colour.default as string)}
                      onChange={(e) => setAdvanced("down_colour", e.target.value.toUpperCase())} />
                  </label>
                )}
              </div>
              {params.volcano_label_genes && (
                <label className="field">
                  <span>Genes to label on the volcano plot</span>
                  <input type="text" placeholder="e.g. Krt5, Elf5, Acta2 (empty = the top genes)"
                    value={((settings.advanced.volcano_label_genes as string[] | undefined) ?? []).join(", ")}
                    onChange={(e) => {
                      const genes = e.target.value.split(/[,\s]+/).filter(Boolean);
                      setAdvanced("volcano_label_genes", genes.length ? genes : undefined);
                    }} />
                </label>
              )}
            </>
          )}
        </details>
      </section>
    </>
  );
}

function SetupMessage({ item }: { item: SetupChatItem }) {
  if (item.role === "user") return <div className="msg user">{item.text}</div>;
  return (
    <div className="msg assistant">
      <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{item.text}</p>
      {item.questions && item.questions.length > 0 && (
        <ul className="small">{item.questions.map((q) => <li key={q}><strong>Please check:</strong> {q}</li>)}</ul>
      )}
      {item.changes && item.changes.length > 0 && (
        <div className="change-chips">{item.changes.map((c) => <span key={c} className="chip">{c}</span>)}</div>
      )}
      {item.guardrail && item.guardrail.length > 0 && (
        <p className="muted small">⛔ Kept the code bank's methods: {item.guardrail.map((g) => g.matched).join(", ")} is not available.</p>
      )}
      {item.fallback_reason && <p className="muted small">Assistant not used: {item.fallback_reason}</p>}
    </div>
  );
}
