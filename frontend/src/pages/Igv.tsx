import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type ExtraAnalysis, type IgvConfig } from "../api";
import { Notice } from "../components/common";
import ServerPicker from "../components/ServerPicker";
import { fmtNum } from "../format";

// IGV for one splicing event: the genome and annotation from the server, each sample's reads (grouped), and the
// splice junctions per group (STAR's counts, summed; arc thickness = reads). Opened from a row of a results table;
// the event list on the side jumps between events without reloading the tracks.

type Browser = { search: (locus: string) => Promise<unknown>; clearROIs?: () => void; loadROI?: (r: unknown) => Promise<void> };
type Row = Record<string, string>;
const GROUP_COLOURS = { test: "#c0392b", reference: "#1f6fb2" } as const;

function roiOf(event: string | null) {
  const m = event?.match(/^(.+):(\d+)-(\d+)$/);
  return m ? [{ name: "event", color: "rgba(255, 196, 0, 0.13)", features: [{ chr: m[1], start: Number(m[2]) - 1, end: Number(m[3]), name: "event" }] }] : [];
}

export default function Igv() {
  const { jobId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const module = params.get("module") ?? "";
  const locus = params.get("locus") ?? "";
  const event = params.get("event");
  const [cfg, setCfg] = useState<IgvConfig | null>(null);
  const [analysis, setAnalysis] = useState<ExtraAnalysis | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [reload, setReload] = useState(0);
  const div = useRef<HTMLDivElement>(null);
  const browser = useRef<Browser | null>(null);
  const igvMod = useRef<{ removeBrowser: (b: unknown) => void } | null>(null);

  // config; poll while the genome index is being built (once per species)
  useEffect(() => {
    let live = true, timer = 0;
    const load = () => api.igv(jobId).then((c) => {
      if (!live) return;
      setCfg(c);
      if (!c.reference.ready && !c.reference.missing.length && c.reference.status !== "failed") timer = window.setTimeout(load, 3000);
    }).catch((e) => live && setErr((e as Error).message));
    load();
    return () => { live = false; clearTimeout(timer); };
  }, [jobId, reload]);
  useEffect(() => { if (module) api.extra(jobId, module).then(setAnalysis).catch(() => undefined); }, [jobId, module]);

  // build the browser once the reference is ready (and again when the BAMs change)
  const ready = cfg?.reference.ready;
  useEffect(() => {
    if (!cfg || !ready || !div.current) return;
    let cancelled = false;
    const file = (k: string) => `${location.origin}/api/runs/${jobId}/igv/file/${k}`;
    const tracks: unknown[] = [{
      type: "annotation", name: "Transcripts", displayMode: "EXPANDED", height: 140, order: 1, visibilityWindow: 3_000_000,
      reader: { readFeatures: (chr: string, start: number, end: number) =>
        fetch(`/api/igv/${cfg.species}/features?chr=${encodeURIComponent(chr)}&start=${Math.floor(start)}&end=${Math.ceil(end)}`).then((r) => r.ok ? r.json() : []) },
    }];
    let order = 2;
    for (const g of cfg.groups) {
      const colour = GROUP_COLOURS[g.role];
      if (g.junctions) {
        tracks.push({
          type: "junction", name: `${g.name} junctions`, height: 110, order: order++, visibilityWindow: 1_000_000,
          color: colour, thicknessBasedOn: "numUniqueReads", bounceHeightBasedOn: "thickness", labelUniqueReadCount: true,
          featureSource: { getFeatures: ({ chr, start, end }: { chr: string; start: number; end: number }) =>
            fetch(`/api/runs/${jobId}/igv/junctions?group=${encodeURIComponent(g.name)}&chr=${encodeURIComponent(chr)}&start=${Math.floor(start)}&end=${Math.ceil(end)}`)
              .then((r) => r.ok ? r.json() : []).then((fs: Array<{ attributes: Record<string, string> }>) =>
                fs.map((f) => ({ ...f, attributes: { ...f.attributes, color: colour } }))) },
        });
      }
    }
    for (const g of cfg.groups) {
      for (const t of g.tracks) {
        tracks.push({ type: "alignment", format: "bam", name: `${g.name} · ${t.sample}`, url: file(t.bam), indexURL: file(t.bai),
          order: order++, displayMode: "SQUISHED", height: 130, coverageColor: GROUP_COLOURS[g.role], showSoftClips: false });
      }
    }
    import("igv").then(async (igv) => {
      if (cancelled || !div.current) return;
      igvMod.current = igv.default as unknown as { removeBrowser: (b: unknown) => void };
      div.current.innerHTML = "";
      const opts = {
        reference: { id: cfg.species, name: cfg.reference.label, fastaURL: file("fasta"), indexURL: file("fai") },
        loadDefaultGenomes: false,          // everything comes from this server, nothing from igv.org
        locus: locus || undefined, tracks, roi: roiOf(event), showSampleNames: false,
      };
      const b = await igv.default.createBrowser(div.current, opts as never);
      if (cancelled) igvMod.current?.removeBrowser(b); else browser.current = b as unknown as Browser;
    }).catch((e) => setErr(`IGV could not start: ${(e as Error).message}`));
    return () => {
      cancelled = true;
      if (browser.current) igvMod.current?.removeBrowser(browser.current);
      browser.current = null;
    };
  }, [cfg, ready, jobId]); // eslint-disable-line react-hooks/exhaustive-deps

  // a new event from the list: move there without rebuilding the tracks
  useEffect(() => {
    const b = browser.current;
    if (!b || !locus) return;
    b.search(locus).catch(() => undefined);
    if (b.clearROIs && b.loadROI) { b.clearROIs(); b.loadROI(roiOf(event)).catch(() => undefined); }
  }, [locus, event]);

  const rows: Row[] = (analysis?.top_tables ?? []).flatMap((t) => t.rows).filter((r) => r._locus);
  const current = rows.find((r) => r._locus === locus && (!event || r._event === event));
  const label = (r: Row) => `${r.gene_symbol || r.gene_name || r.gene_id || "event"} ${r.event_type || r.lsv_type || ""}`.trim();
  const dpsi = (r: Row) => r.delta_psi ?? r.dpsi;

  async function chooseBams(tokens: string[]) {
    setPicking(false); setErr(null);
    try { await api.igvChooseBams(jobId, tokens); setReload((n) => n + 1); } catch (e) { setErr((e as Error).message); }
  }

  return (
    <div className="igv-page">
      <div className="row" style={{ flexWrap: "wrap" }}>
        <Link to={`/runs/${jobId}?tab=extras`}>← Back to the results</Link>
        <h1 style={{ margin: "0 0 0 8px", fontSize: "1.3rem" }}>
          IGV{current ? `: ${label(current)}` : ""}{analysis ? <span className="muted small"> · {analysis.title}</span> : null}
        </h1>
      </div>
      {current && (
        <p className="muted small" style={{ margin: "4px 0 8px" }}>
          Event {current._event} ({current.strand || "?"}) highlighted in yellow{dpsi(current) ? ` · dPSI ${fmtNum(Number(dpsi(current)), 2)}` : ""}.
          Junction arcs: reads joining two exons, summed over each group's samples (number and thickness = uniquely mapped reads).
          Genome: {cfg?.reference.label}.
        </p>
      )}
      {err && <Notice kind="fail">{err}</Notice>}
      {cfg && !cfg.reference.ready && (
        cfg.reference.missing.length
          ? <Notice kind="fail">The reference files are missing on the server: {cfg.reference.missing.join(", ")}.</Notice>
          : cfg.reference.status === "failed"
            ? <Notice kind="fail">Indexing the genome failed: {cfg.reference.error}</Notice>
            : <Notice kind="info"><span className="spinner" /> Preparing the genome and annotation for IGV ({cfg.reference.label}). This happens once and takes a few minutes.</Notice>
      )}
      {cfg && cfg.missing_bams.length > 0 && (
        <Notice kind="warning">
          {cfg.bam_source ? `No reads for ${cfg.missing_bams.join(", ")} (BAMs from ${cfg.bam_source}).`
            : "No BAM files were found for this analysis (it was not made from FASTQ files on this server)."}{" "}
          <button type="button" className="link" onClick={() => setPicking(!picking)}>Choose BAMs on the server</button>
        </Notice>
      )}
      {cfg && cfg.missing_bams.length === 0 && cfg.bam_source && (
        <p className="muted small">Reads: {cfg.bam_source}. <button type="button" className="link" onClick={() => setPicking(!picking)}>Use other BAMs</button></p>
      )}
      {picking && <ServerPicker mode="files" accept={[".bam"]} onPick={chooseBams} onClose={() => setPicking(false)} />}
      <div className="igv-layout">
        {rows.length > 0 && (
          <aside className="igv-events">
            <strong className="small">Events ({rows.length})</strong>
            {rows.map((r, i) => (
              <button type="button" key={i} className={r === current ? "igv-event active" : "igv-event"}
                onClick={() => setParams({ module, locus: r._locus, event: r._event ?? "" })}>
                <span>{label(r)}</span>
                <span className={`small ${Number(dpsi(r)) > 0 ? "up" : "down"}`}>{dpsi(r) ? fmtNum(Number(dpsi(r)), 2) : ""}</span>
              </button>
            ))}
          </aside>
        )}
        <div ref={div} className="igv-host" />
      </div>
    </div>
  );
}
