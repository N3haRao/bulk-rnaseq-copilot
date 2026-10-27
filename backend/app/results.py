"""Read a finished run folder into what the results tabs show.

Everything here is read from files the code bank wrote. Nothing is
recomputed, so the website shows exactly what the report and tables contain.
"""

import csv
import json
import shutil
from functools import lru_cache
from pathlib import Path

import yaml

SERVABLE = {".png", ".csv", ".json", ".md", ".html", ".yaml", ".log", ".txt", ".gz", ".tsv", ".zip"}

# Plain-language captions for the code bank's figures (path relative to the run folder)
FIGURES = {
    "qc": [
        ("02_qc/library_sizes.png", "Reads per sample",
         "Total reads counted for each sample. Very low bars mean a sample had little data."),
        ("02_qc/detected_genes.png", "Genes detected per sample",
         "How many genes have at least one read. A sample far below the others may have failed."),
        ("02_qc/logcpm_boxplot.png", "Expression distribution per sample",
         "Boxes should sit at similar heights; one shifted box suggests a problem with that sample."),
        ("02_qc/logcpm_density.png", "Expression density per sample",
         "Curves should overlap. A curve with a very different shape points to a problem sample."),
    ],
    "samples": [
        ("03_exploratory/pca_plot.png", "PCA: samples by group",
         "Each dot is a sample. Samples that are close have similar overall expression. "
         "Ideally the groups form separate clusters."),
        ("03_exploratory/pca_scree.png", "Variance explained by each component",
         "How much of the sample-to-sample variation each PCA axis captures."),
        ("03_exploratory/sample_distance_heatmap.png", "Sample-to-sample distances",
         "Darker = more similar. Samples of the same group should cluster together."),
    ],
    "de": [
        ("05_de_plots/volcano_plot.png", "Volcano plot",
         "Each dot is a gene: change between groups (x) against strength of evidence (y). "
         "Coloured genes pass the significance rule."),
        ("05_de_plots/ma_plot.png", "MA plot",
         "Change between groups (y) against average expression (x)."),
        ("05_de_plots/pvalue_histogram.png", "P-value histogram",
         "A flat histogram with a peak near 0 is the healthy shape for a working test."),
        ("04_differential_expression/dispersion_plot.png", "Dispersion fit",
         "A technical check of the statistical model; the red line should follow the cloud of points."),
    ],
    "heatmap": [
        ("05_de_plots/heatmap_top_degs.png", "Top differentially expressed genes",
         "Rows are genes, columns are samples; colours show expression relative to each gene's average."),
    ],
}

DE_NUMERIC = {"baseMean", "log2FoldChange", "lfcSE", "log2FoldChange_unshrunken", "stat", "pvalue", "padj"}


def safe_path(run_dir: Path, rel: str) -> Path:
    """A file inside the run folder, or KeyError (no '..' escapes, only result file types)."""
    p = (run_dir / rel).resolve()
    if run_dir.resolve() not in p.parents or not p.is_file() or p.suffix.lower() not in SERVABLE:
        raise KeyError(rel)
    return p


def _json(run_dir: Path, rel: str):
    p = run_dir / rel
    return json.loads(p.read_text()) if p.exists() else None


def _csv_rows(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        rows = []
        for i, r in enumerate(csv.DictReader(f)):
            if limit is not None and i >= limit:
                break
            rows.append(r)
        return rows


def overview(run_dir: Path) -> dict:
    """Everything the results page needs except the large tables."""
    figures = {}
    for tab, items in FIGURES.items():
        figures[tab] = [{"path": rel, "title": t, "caption": c} for rel, t, c in items if (run_dir / rel).exists()]
    for p in sorted((run_dir / "03_exploratory").glob("pca_by_*.png")):
        covariate = p.stem.removeprefix("pca_by_")
        figures["samples"].insert(1, {"path": f"03_exploratory/{p.name}", "title": f"PCA: samples by {covariate}",
                                      "caption": f"The same PCA, coloured by {covariate}. If samples cluster by "
                                                 f"{covariate} more than by group, {covariate} has a strong effect."})
    methods = run_dir / "07_report" / "methods.md"
    validation = _json(run_dir, "01_validate_inputs/validation_report.json")
    return {
        "run_id": run_dir.name,
        "summary": _json(run_dir, "run_summary.json"),
        "validation": validation,
        "resolved_params": _resolved(run_dir),
        "qc": {"summary": _json(run_dir, "02_qc/qc_summary.json"),
               "metrics": _csv_rows(run_dir / "02_qc" / "qc_metrics.csv")},
        "exploratory": {"summary": _json(run_dir, "03_exploratory/exploratory_summary.json"),
                        "outliers": _csv_rows(run_dir / "03_exploratory" / "outlier_flags.csv")},
        "de": _json(run_dir, "04_differential_expression/de_summary.json"),
        "plots": _json(run_dir, "05_de_plots/plots_summary.json"),
        "enrichment": _json(run_dir, "06_enrichment/enrichment_summary.json"),
        "provenance": _json(run_dir, "provenance.json"),
        "figures": figures,
        "methods_md": methods.read_text() if methods.exists() else None,
        "report_available": (run_dir / "07_report" / "report.html").exists(),
    }


def _resolved(run_dir: Path) -> dict | None:
    p = run_dir / "01_validate_inputs" / "resolved_params.yaml"
    if not p.exists():
        return None
    r = yaml.safe_load(p.read_text()) or {}
    for k in ("covariates", "exclude_samples", "numeric_covariates"):
        v = r.get(k)
        r[k] = [] if v is None else (v if isinstance(v, list) else [v])
    r.pop("counts_file", None)
    r.pop("metadata_file", None)
    return r


@lru_cache(maxsize=8)
def _de_table(path: str, mtime: float) -> tuple[list[str], list[dict]]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            for k in DE_NUMERIC:
                v = r.get(k)
                r[k] = float(v) if v not in (None, "", "NA") else None
            r["significant"] = r.get("significant") in ("TRUE", "True", "true")
            rows.append(r)
        return list(reader.fieldnames or []), rows


def de_table(run_dir: Path, *, search: str = "", direction: str = "all", sort: str = "padj",
             order: str = "asc", page: int = 1, page_size: int = 50) -> dict:
    p = run_dir / "04_differential_expression" / "de_results_all.csv"
    if not p.exists():
        raise KeyError("de_results_all.csv")
    columns, rows = _de_table(str(p), p.stat().st_mtime)
    sel = rows
    if direction in ("up", "down"):
        sel = [r for r in sel if r.get("direction") == direction]
    elif direction == "significant":
        sel = [r for r in sel if r["significant"]]
    if search.strip():
        terms = [t.lower() for t in search.replace(",", " ").split() if t]
        exact = [r for r in sel if any(t in ((r.get("gene_symbol") or "").lower(), r["gene_id"].lower()) for t in terms)]
        sel = exact if len(terms) > 1 or exact else [
            r for r in sel if any(t in " ".join([r["gene_id"], r.get("gene_symbol") or "", r.get("gene_name") or ""]).lower()
                                  for t in terms)]
    if sort in columns:
        # missing values (e.g. padj NA) always go last
        present = [r for r in sel if r.get(sort) not in (None, "")]
        missing = [r for r in sel if r.get(sort) in (None, "")]
        if sort in DE_NUMERIC:
            key = (lambda r: abs(r[sort])) if order == "abs" else (lambda r: r[sort])
        else:
            key = lambda r: str(r[sort]).lower()  # noqa: E731
        present.sort(key=key, reverse=order in ("desc", "abs"))
        sel = present + missing
    page_size = max(1, min(page_size, 500))
    start = (max(page, 1) - 1) * page_size
    return {"columns": columns, "total": len(sel), "page": page, "page_size": page_size,
            "rows": sel[start:start + page_size]}


def sample_columns(run_dir: Path) -> list[str]:
    """Columns of the run's validated sample table (for checking column settings)."""
    p = run_dir / "01_validate_inputs" / "metadata_validated.csv"
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return next(csv.reader(f), [])


def gene_names(run_dir: Path) -> set[str]:
    """Every gene ID and symbol in the run's results table."""
    p = run_dir / "04_differential_expression" / "de_results_all.csv"
    _, rows = _de_table(str(p), p.stat().st_mtime)
    return {x for r in rows for x in (r.get("gene_symbol"), r.get("gene_id")) if x}


def enrichment_table(run_dir: Path, collection_folder: str, kind: str, limit: int = 200) -> dict:
    if kind not in ("gsea", "ora_up", "ora_down"):
        raise KeyError(kind)
    p = safe_path(run_dir, f"06_enrichment/{collection_folder}/{kind}.csv")
    rows = _csv_rows(p, limit)
    figures = {"gsea": "gsea_barplot.png", "ora_up": "ora_up_dotplot.png", "ora_down": "ora_down_dotplot.png"}
    fig = f"06_enrichment/{collection_folder}/{figures[kind]}"
    return {"rows": rows, "figure": fig if (run_dir / fig).exists() else None}


def bundle(run_dir: Path, dest_dir: Path) -> Path:
    """A .zip of the whole run folder (results, figures, report, provenance)."""
    target = dest_dir / f"{run_dir.name}.zip"
    if not target.exists() or target.stat().st_mtime < (run_dir / "run_summary.json").stat().st_mtime:
        tmp = shutil.make_archive(str(dest_dir / (run_dir.name + "_tmp")), "zip", root_dir=run_dir.parent,
                                  base_dir=run_dir.name)
        Path(tmp).replace(target)
    return target
    
