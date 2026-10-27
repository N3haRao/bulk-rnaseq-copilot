"""The downloadable report: one self-contained HTML file (figures embedded) and a PDF of it.

Narrative sections come from the report agent (checked against the results);
everything else (numbers, tables, figures, methods, settings, versions) is read
directly from the run folder. Each narrative section says who wrote it.
"""

import base64
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import results
from .agents.reporter import ReportAgent, build_facts


class ReportError(RuntimeError):
    pass


def build(job: dict, job_dir: Path, run_dir: Path, *, refresh: bool = False) -> dict:
    """Write report/report.html (and the narrative it uses); returns the report status."""
    out = job_dir / "report"
    out.mkdir(exist_ok=True)
    narrative_path = out / "narrative.json"
    if narrative_path.exists() and not refresh:
        narrative = json.loads(narrative_path.read_text())
    else:
        facts, genes = build_facts(run_dir)
        narrative = ReportAgent().write(facts, genes, log_dir=job_dir / "agent" / "log")
        narrative["facts"] = facts
        narrative["written"] = datetime.now().isoformat(timespec="seconds")
        narrative_path.write_text(json.dumps(narrative, indent=2, default=str))
        (out / "report.pdf").unlink(missing_ok=True)
        (out / "report.pptx").unlink(missing_ok=True)
    (out / "report.html").write_text(render_html(job, run_dir, narrative))
    return status(job_dir)


def status(job_dir: Path) -> dict:
    out = job_dir / "report"
    n = out / "narrative.json"
    if not n.exists():
        return {"available": False}
    narrative = json.loads(n.read_text())
    return {"available": True, "source": narrative["source"], "model": narrative.get("model"),
            "section_sources": narrative["section_sources"], "n_removed": len(narrative.get("dropped", [])),
            "fallback_reason": narrative.get("fallback_reason"), "written": narrative.get("written"),
            "summary": narrative["sections"]["summary"], "key_findings": narrative["sections"]["key_findings"],
            "pdf_ready": (out / "report.pdf").exists()}


def pdf(job_dir: Path) -> Path:
    """report.pdf, printed from report.html with headless Chromium (Playwright)."""
    html_path = job_dir / "report" / "report.html"
    pdf_path = job_dir / "report" / "report.pdf"
    if not html_path.exists():
        raise ReportError("Create the report first.")
    if pdf_path.exists() and pdf_path.stat().st_mtime >= html_path.stat().st_mtime:
        return pdf_path
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ReportError("PDF export needs Playwright: backend/.venv/bin/pip install playwright && "
                          "backend/.venv/bin/python -m playwright install chromium") from e
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            page.pdf(path=str(pdf_path), format="A4", print_background=True,
                     margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
                     display_header_footer=True, header_template="<span></span>",
                     footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#777">'
                                     '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')
            browser.close()
    except PlaywrightError as e:
        raise ReportError("Chromium for PDF export is not installed: backend/.venv/bin/python -m playwright "
                          f"install chromium ({str(e).splitlines()[0]})") from e
    return pdf_path


def pptx(job: dict, job_dir: Path, run_dir: Path) -> Path:
    """report.pptx with the same narrative, figures and tables as the HTML report."""
    from . import report_pptx
    narrative_path = job_dir / "report" / "narrative.json"
    if not narrative_path.exists():
        build(job, job_dir, run_dir)
    out = job_dir / "report" / "report.pptx"
    if out.exists() and out.stat().st_mtime >= narrative_path.stat().st_mtime:
        return out
    return report_pptx.build(job, job_dir, run_dir, json.loads(narrative_path.read_text()))


# ---- HTML ---------------------------------------------------------------------------------
E = html.escape


def _img(run_dir: Path, rel: str, title: str, caption: str) -> str:
    p = run_dir / rel
    if not p.exists():
        return ""
    data = base64.b64encode(p.read_bytes()).decode()
    return (f'<figure><img src="data:image/png;base64,{data}" alt="{E(title)}">'
            f"<figcaption><strong>{E(title)}.</strong> {E(caption)}</figcaption></figure>")


def _table(headers: list[str], rows: list[list], num_cols: set[int] = frozenset()) -> str:
    th = "".join(f'<th class="{"num" if i in num_cols else ""}">{E(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td class="{"num" if i in num_cols else ""}">{E(str(c))}</td>'
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _md(text: str) -> str:
    """methods.md -> HTML (headings, paragraphs, bullets, *italic*, **bold**)."""
    def inline(s: str) -> str:
        s = E(s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        return re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if block.startswith("# "):
            continue  # the section already has a heading
        if block.startswith("## "):
            out.append(f"<h3>{inline(block[3:])}</h3>")
        elif all(ln.startswith("- ") for ln in lines):
            out.append("<ul>" + "".join(f"<li>{inline(ln[2:])}</li>" for ln in lines) + "</ul>")
        else:
            out.append(f"<p>{inline(' '.join(lines))}</p>")
    return "\n".join(out)


def _who(source: str, model: str | None) -> str:
    if source == "model":
        return f'<span class="who model">Written by the assistant ({E((model or "").replace("ollama:", ""))}), checked against the results</span>'
    return '<span class="who rules">Written from the results by fixed rules</span>'


def _fmt(x, digits=2) -> str:
    if x is None or x == "":
        return "–"
    x = float(x)
    if digits == "p":
        return "< 1e-300" if x == 0 else (f"{x:.1e}" if x < 0.001 else f"{x:.3f}")
    return f"{x:,.{digits}f}"


def render_html(job: dict, run_dir: Path, narrative: dict) -> str:
    ov = results.overview(run_dir)
    s, p = ov["summary"], ov["resolved_params"]
    sec, src, model = narrative["sections"], narrative["section_sources"], narrative.get("model")
    test, ref = p["test_level"], p["reference_level"]
    prov = ov["provenance"] or {}
    cb = prov.get("code_bank") or {}
    who = lambda k: _who(src.get(k, "rules"), model)  # noqa: E731

    def para(k: str) -> str:
        return f"<p>{E(sec[k])}</p>{who(k)}"

    def bullets(k: str) -> str:
        return ("<ul>" + "".join(f"<li>{E(x)}</li>" for x in sec[k]) + "</ul>" + who(k)) if sec[k] else ""

    figs = {f["path"]: f for tab in ov["figures"].values() for f in tab}

    def fig(rel: str) -> str:
        f = figs.get(rel)
        return _img(run_dir, rel, f["title"], f["caption"]) if f else ""

    def top_table(direction: str) -> str:
        rows = results.de_table(run_dir, direction=direction, sort="padj", order="asc", page_size=20)["rows"]
        if not rows:
            return "<p class='muted'>None.</p>"
        return _table(["Gene", "Description", "log2 fold change", "adj. p-value", f"Mean {ref}", f"Mean {test}"],
                      [[r.get("gene_symbol") or r["gene_id"], (r.get("gene_name") or "")[:60], _fmt(r["log2FoldChange"]),
                        _fmt(r["padj"], "p"), _fmt(r.get(f"mean_normalized_{ref}"), 0), _fmt(r.get(f"mean_normalized_{test}"), 0)]
                       for r in rows], {2, 3, 4, 5})

    # pathways
    en = ov["enrichment"]
    pathway_html = ""
    if en and en.get("status") == "done":
        for name, c in (en.get("collections") or {}).items():
            gsea = results.enrichment_table(run_dir, c["folder"], "gsea", limit=10)
            pathway_html += f"<h3>{E(name)} <span class='muted small'>({E(c['source'])})</span></h3>"
            if gsea["figure"]:
                pathway_html += _img(run_dir, gsea["figure"], f"GSEA, {name}",
                                     f"Gene sets with positive NES tend to be higher in {test}; negative, lower.")
            if gsea["rows"]:
                pathway_html += _table(["Pathway", "NES", "adj. p-value", "Set size"],
                                       [[r["pathway"], _fmt(r["NES"]), _fmt(r["padj"], "p"), r["set_size"]] for r in gsea["rows"]],
                                       {1, 2, 3})
    qc_rows = [[m["sample_id"], m["group"], m["library_size_millions"], m["detected_genes"], m["flags"] or "–"]
               for m in ov["qc"]["metrics"]]
    ex = ov["exploratory"]["summary"] or {}
    assoc_rows = [[a["variable"], _fmt(a["r2_PC1"]), _fmt(a["r2_PC2"])] for a in ex.get("pc_association", [])]
    settings_rows = [
        ["Comparison", f"{test} vs {ref} (column {p['condition']})"],
        ["Design", p.get("design_formula", "")],
        ["Accounted for", ", ".join(p["covariates"]) or "nothing"],
        ["Organism / gene IDs", f"{p.get('organism')} / {p.get('gene_id_type')}"],
        ["Low-count filter", f"at least {p.get('min_count')} reads in at least {p.get('min_samples')} samples"],
        ["Significance", s["significance_rule"]],
        ["Samples left out", ", ".join(p.get("exclude_samples") or []) or "none"],
        ["Setup suggested by", _origin(job.get("setup_origin"))],
    ]
    pkg_rows = [[k, v] for k, v in sorted((prov.get("packages") or {}).items())]
    n_removed = len(narrative.get("dropped", []))
    n_model = sum(v == "model" for v in src.values())
    warnings = [w.split("] ", 1)[-1] for w in s.get("warnings") or []]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RNA-seq report: {E(test)} vs {E(ref)}</title>
<style>{CSS}</style></head><body><main>
<header>
  <p class="kicker">RNA-seq analysis report</p>
  <h1>{E(test)} vs {E(ref)}</h1>
  <p class="muted">Dataset: {E(str(job.get('dataset') or ''))} · {s['n_samples']} samples · run {E(ov['run_id'])} ·
  {datetime.now():%d %B %Y} · code bank {E(str(cb.get('version', '')))}{(' (commit ' + E(cb['commit'][:7]) + ')') if cb.get('commit') else ''}</p>
</header>

<section class="lead">
  <h2>Summary</h2>
  {para("summary")}
  <div class="tiles">
    <div><b>{s['n_samples']}</b><span>samples</span></div>
    <div><b>{s['n_genes_tested']:,}</b><span>genes tested</span></div>
    <div><b>{s['n_significant']:,}</b><span>significant genes</span></div>
    <div><b class="up">{s['n_up']:,}</b><span>higher in {E(test)}</span></div>
    <div><b class="down">{s['n_down']:,}</b><span>lower in {E(test)}</span></div>
  </div>
  <p class="muted small">A gene counts as significant when {E(s['significance_rule'])}. Fold changes are {E(test)} relative to {E(ref)}.</p>
  <h3>Key findings</h3>
  {bullets("key_findings")}
</section>

<section>
  <h2>Things to check</h2>
  {bullets("caveats") or "<p>No caveats were found.</p>"}
</section>

<section>
  <h2>Sample quality and structure</h2>
  {para("sample_structure")}
  <div class="figs">{fig("03_exploratory/pca_plot.png")}{fig("03_exploratory/sample_distance_heatmap.png")}</div>
  {('<h3>What explains the main axes of variation</h3>' + _table(["Characteristic", "share of PC1", "share of PC2"], assoc_rows, {1, 2})) if assoc_rows else ""}
  <h3>Per-sample quality</h3>
  {_table(["Sample", "Group", "Reads (millions)", "Genes detected", "Flags"], qc_rows, {2, 3})}
  <div class="figs">{fig("02_qc/library_sizes.png")}{fig("02_qc/logcpm_boxplot.png")}</div>
</section>

<section>
  <h2>Differential expression</h2>
  {para("gene_highlights")}
  <div class="figs">{fig("05_de_plots/volcano_plot.png")}{fig("05_de_plots/ma_plot.png")}</div>
  {fig("05_de_plots/heatmap_top_degs.png")}
  <h3>Top 20 genes higher in {E(test)}</h3>{top_table("up")}
  <h3>Top 20 genes lower in {E(test)}</h3>{top_table("down")}
  <p class="muted small">All genes: 04_differential_expression/de_results_all.csv in the results download.</p>
</section>

<section>
  <h2>Pathways</h2>
  {para("pathway_highlights")}
  {pathway_html}
</section>

<section>
  <h2>Suggested next steps</h2>
  {bullets("next_steps")}
</section>

<section>
  <h2>Methods</h2>
  {_md(ov["methods_md"] or "")}
  <h3>Settings</h3>{_table(["Setting", "Value"], settings_rows)}
  <details open><summary>Software versions</summary>{_table(["Package", "Version"], pkg_rows)}</details>
  <p class="muted small">Reproduce from the code bank: <code>{E(prov.get('rerun_command', ''))}</code></p>
</section>

<section class="about">
  <h2>About this report</h2>
  <p>The figures, tables, numbers, methods and settings come directly from the analysis files.
  {f"{n_model} of {len(src)} text sections were written by an AI assistant ({E((model or '').replace('ollama:', ''))}, an open-weight model running on this computer) and checked automatically: any sentence with a number, gene or pathway not found in the results was removed ({n_removed} removed)." if n_model else "The text sections were written from the results by fixed rules."}
  Read the text with the figures and tables; they are the primary record.</p>
  {('<h3>Warnings raised by the analysis (verbatim)</h3><ul>' + "".join(f"<li>{E(w)}</li>" for w in warnings) + '</ul>') if warnings else ''}
</section>
</main></body></html>"""


def _origin(o: dict | None) -> str:
    if not o:
        return "not recorded"
    who = {"model": f"the assistant ({(o.get('model') or '').replace('ollama:', '')})", "rules": "built-in rules",
           "setup_chat": f"the setup assistant ({(o.get('model') or '').replace('ollama:', '')})",
           "chat": f"a rerun proposed in the chat ({o.get('model') or ''})",
           "example": "the example dataset's settings"}.get(o.get("source"), str(o.get("source")))
    return who + ("; changed by the user" if o.get("changed_by_user") else "; confirmed by the user")


CSS = """
:root { --text:#1c2430; --muted:#5b6676; --border:#dde1e7; --soft:#f3f5f8; --accent:#1f6feb; --up:#c45100; --down:#0067a3; }
* { box-sizing: border-box; }
body { margin:0; color:var(--text); background:#fff; font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width: 900px; margin: 0 auto; padding: 32px 20px 60px; }
h1 { font-size: 1.9rem; margin: 0 0 6px; } h2 { font-size: 1.3rem; margin: 0 0 10px; border-bottom: 2px solid var(--border); padding-bottom: 4px; }
h3 { font-size: 1.02rem; margin: 18px 0 8px; }
.kicker { text-transform: uppercase; letter-spacing: .08em; font-size: .78rem; color: var(--accent); font-weight: 700; margin: 0; }
section { margin: 30px 0; } p { margin: 0 0 10px; } .muted { color: var(--muted); } .small { font-size: .85rem; }
.up { color: var(--up); } .down { color: var(--down); }
.who { display:inline-block; font-size:.74rem; padding:1px 8px; border-radius:999px; margin: 0 0 6px; }
.who.model { background:#e7f0fe; color:#174ea6; } .who.rules { background:var(--soft); color:var(--muted); }
.tiles { display:grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin: 14px 0 8px; }
.tiles div { background: var(--soft); border-radius: 8px; padding: 10px; } .tiles b { display:block; font-size: 1.35rem; }
.tiles span { color: var(--muted); font-size: .8rem; }
.figs { display:grid; grid-template-columns: 1fr 1fr; gap: 14px; }
figure { margin: 10px 0; break-inside: avoid; } figure img { width: 100%; height: auto; border: 1px solid var(--border); border-radius: 4px; }
figcaption { font-size: .82rem; color: var(--muted); margin-top: 4px; }
table { border-collapse: collapse; width: 100%; font-size: .84rem; margin: 6px 0 12px; break-inside: auto; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { background: var(--soft); } td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; } tr { break-inside: avoid; }
code { font-family: ui-monospace, Menlo, monospace; font-size: .82rem; background: var(--soft); padding: 1px 4px; border-radius: 3px; }
.about { border-top: 2px solid var(--border); padding-top: 12px; font-size: .9rem; }
details > summary { cursor: pointer; font-weight: 600; margin: 8px 0; }
@media (max-width: 640px) { .tiles { grid-template-columns: repeat(2, 1fr); } .figs { grid-template-columns: 1fr; } }
@media print { main { padding: 0; max-width: none; } section { margin: 18px 0; } h2 { break-after: avoid; } h3 { break-after: avoid; } }
"""
