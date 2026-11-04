"""Cross-study comparison: do two (or more) analyses agree?

Runs the code bank's compare/ module on finished analyses of this site and, optionally,
published DE results tables (e.g. a paper's supplementary table). Studies are compared
by their RESULTS (fold-change correlation, overlap of significant genes, RRHO, pathways),
never by merging raw counts: each study keeps its own normalisation and design.

Each comparison lives in data/comparisons/<id>/:
    comparison.json    state (queued / running / success / success_with_errors / failed) and settings
    inputs/            uploaded results tables
    run_config.yaml    every parameter passed to the module
    console.log        the module's output; runs/<module run folder>/ all results
    report/            comparison_report.html (+ .pdf), code/ the code .zip
"""

import base64
import csv
import html
import json
import re
import secrets
import shutil
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

import yaml

from . import code_bundle, codebank
from .config import settings
from .jobs import jobs

ENTRY = "compare/scripts/run_compare.R"
FINAL = {"success", "success_with_errors", "failed", "interrupted"}
SERVABLE = {".png", ".csv", ".json", ".md", ".txt", ".yaml"}
STEP_START = re.compile(r"Step (\d+)/(\d+): (\S+) \.\.\.")
TABLE_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx", ".xls", ".csv.gz", ".tsv.gz", ".txt.gz")
ORGANISMS = ("auto", "human", "mouse", "other")


class CompareError(ValueError):
    """A problem with the user's choices or files, in plain language."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def available() -> bool:
    return "compare" in (codebank.manifest().get("modules") or {})


def base_dir() -> Path:
    return settings.data_dir / "comparisons"


def safe_label(x: str) -> str:
    """The code bank's safe_label(): what a study label becomes in file and folder names."""
    s = re.sub(r"[^A-Za-z0-9.-]+", "_", x).strip("_")
    return s or "study"


def pair_folder(a: str, b: str) -> str:
    return f"{safe_label(a)}_vs_{safe_label(b)}"


def comparison_dir(cid: str) -> Path:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", cid):
        raise KeyError(cid)
    d = base_dir() / cid
    if not (d / "comparison.json").exists():
        raise KeyError(cid)
    return d


def _load(d: Path) -> dict:
    return json.loads((d / "comparison.json").read_text())


def _save(d: Path, rec: dict) -> None:
    tmp = d / "comparison.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2, default=str))
    tmp.replace(d / "comparison.json")


def run_dir_of(d: Path) -> Path | None:
    runs = sorted(p for p in (d / "runs").iterdir() if p.is_dir()) if (d / "runs").exists() else []
    return runs[-1] if runs else None


def _json(p: Path | None):
    return json.loads(p.read_text()) if p and p.exists() else None


def default_label(job: dict) -> str:
    """A short study label for a run: the GEO accession, the example name or the file name, plus the comparison."""
    s = job.get("settings") or {}
    up = {}
    try:
        from . import uploads
        up = uploads.record(job["upload_id"])
    except Exception:
        pass
    base = ((up.get("geo") or {}).get("accession") or up.get("example") or job.get("dataset") or job["id"])
    base = re.sub(r"\.(csv|tsv|txt|xlsx|gz)$", "", str(base), flags=re.I)
    return f"{base} {s.get('test_level', '')}".strip()[:40]


# ---- Starting a comparison ---------------------------------------------------------------------
class ComparisonManager:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="compare")
        self._lock = threading.Lock()

    def start(self, spec: dict, files: list[tuple[str, BinaryIO]] | None = None) -> dict:
        """spec = {title?, studies: [{kind: run, job_id, label?, flip?} | {kind: table, file: index, label,
        organism?, comparison?, flip?}], padj_cutoff?, log2fc_cutoff?, compare_pathways?}"""
        if not available():
            raise CompareError("This code bank version has no compare module (v0.5.0 or newer needed).")
        files = files or []
        studies_in = spec.get("studies") or []
        if not 2 <= len(studies_in) <= 8:
            raise CompareError("Choose between 2 and 8 studies to compare.")
        padj = float(spec.get("padj_cutoff", 0.05))
        lfc = float(spec.get("log2fc_cutoff", 1))
        if not (0 < padj <= 1) or not (0 <= lfc <= 10):
            raise CompareError("Cutoffs out of range (padj 0-1, |log2FC| 0-10).")

        cid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        d = base_dir() / cid
        (d / "inputs").mkdir(parents=True)
        (d / "runs").mkdir()
        try:
            studies, described = self._studies(studies_in, files, d / "inputs")
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        cfg = {"run_name": "compare", "studies": studies, "padj_cutoff": padj, "log2fc_cutoff": lfc,
               "compare_pathways": bool(spec.get("compare_pathways", True))}
        first_adv = next((jobs.load(s["job_id"])["settings"].get("advanced") or {} for s in described if s["kind"] == "run"), {})
        for k in ("gene_set_collections", "plot_palette", "up_colour", "down_colour"):
            if k in first_adv:
                cfg[k] = first_adv[k]
        title = (spec.get("title") or "").strip()[:120] or " vs ".join(s["label"] for s in described)
        rec = {"id": cid, "title": title, "status": "queued", "created": _now(), "started": None, "finished": None,
               "error": None, "codebank": codebank.version_info(), "studies": described,
               "settings": {"padj_cutoff": padj, "log2fc_cutoff": lfc, "compare_pathways": cfg["compare_pathways"]}}
        (d / "run_config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        cmd = [settings.rscript, str(codebank.path(ENTRY)), "--config", str(d / "run_config.yaml"), "--runs_dir", str(d / "runs")]
        rec["command"] = cmd
        _save(d, rec)
        self._pool.submit(self._run, d, cmd)
        return overview(d)

    def _studies(self, studies_in: list[dict], files: list[tuple[str, BinaryIO]], inputs: Path) -> tuple[list[dict], list[dict]]:
        studies, described, seen = [], [], set()
        for i, s in enumerate(studies_in):
            kind = s.get("kind", "run")
            flip = bool(s.get("flip", False))
            if kind == "run":
                try:
                    job = jobs.load(str(s.get("job_id")))
                except (KeyError, FileNotFoundError, ValueError):
                    raise CompareError(f"Analysis {s.get('job_id')} was not found.")
                if job["status"] not in ("success", "success_with_errors") or not job.get("run_dir") or not Path(job["run_dir"]).exists():
                    raise CompareError(f"Analysis {job['id']} has not finished successfully, so it cannot be compared.")
                label = (s.get("label") or default_label(job)).strip()[:40]
                st = job["settings"]
                entry = {"label": label, "run_dir": job["run_dir"]}
                desc = {"kind": "run", "job_id": job["id"], "label": label, "flip": flip, "dataset": job.get("dataset"),
                        "comparison": f"{st['test_level']} vs {st['reference_level']}"}
            elif kind == "table":
                idx = s.get("file")
                if not isinstance(idx, int) or not 0 <= idx < len(files):
                    raise CompareError(f"Study {i + 1}: no results table was attached.")
                name, fh = files[idx]
                base = Path(name).name
                if not base.lower().endswith(TABLE_SUFFIXES):
                    raise CompareError(f"'{base}' is not a table (.csv, .tsv, .txt or .xlsx).")
                label = (s.get("label") or Path(base).stem).strip()[:40]
                org = s.get("organism") or "auto"
                if org not in ORGANISMS:
                    raise CompareError(f"Organism must be one of {', '.join(ORGANISMS)}.")
                target = inputs / f"{i + 1:02d}_{safe_label(label)}{''.join(Path(base).suffixes[-2:]) if base.endswith('.gz') else Path(base).suffix}"
                _save_upload(fh, target)
                comp = (s.get("comparison") or "").strip()[:80] or "(not stated)"
                entry = {"label": label, "de_file": str(target), "organism": org, "comparison": comp}
                desc = {"kind": "table", "label": label, "flip": flip, "file_name": base, "organism": org, "comparison": comp}
            else:
                raise CompareError(f"Unknown study kind '{kind}'.")
            if not label:
                raise CompareError(f"Study {i + 1} needs a label.")
            if safe_label(label) in seen:
                raise CompareError(f"Two studies have the same label ('{label}'). Give each study its own name.")
            seen.add(safe_label(label))
            if flip:
                entry["flip"] = True
            studies.append(entry)
            described.append(desc)
        return studies, described

    def _run(self, d: Path, cmd: list[str]) -> None:
        rec = _load(d)
        rec.update(status="running", started=_now())
        _save(d, rec)
        try:
            with open(d / "console.log", "w") as log:
                code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=settings.codebank_dir).returncode
        except FileNotFoundError:
            rec.update(status="failed", finished=_now(), error=f"Rscript was not found ({settings.rscript}).")
            _save(d, rec)
            return
        rd = run_dir_of(d)
        summary = _json(rd / "run_summary.json") if rd else None
        status = (summary or {}).get("status", "failed")
        if code != 0:
            status = "failed"
        rec.update(status=status, finished=_now(), exit_code=code, run_dir=str(rd) if rd else None,
                   error=_failure(d, rd) if status == "failed" else None)
        _save(d, rec)

    def recover(self) -> None:
        """Comparisons left running by a stopped server cannot finish."""
        for f in base_dir().glob("*/comparison.json"):
            rec = json.loads(f.read_text())
            if rec["status"] not in FINAL:
                rec.update(status="interrupted", finished=_now(), error="The app was stopped while this comparison was running. Start it again.")
                _save(f.parent, rec)


def _save_upload(src: BinaryIO, dest: Path) -> Path:
    limit = settings.max_upload_mb * 1024 * 1024
    written = 0
    with open(dest, "wb") as out:
        while chunk := src.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise CompareError(f"A file is larger than {settings.max_upload_mb} MB (MAX_UPLOAD_MB).")
            out.write(chunk)
    return dest


def _failure(d: Path, rd: Path | None) -> str:
    if rd:
        rep = _json(rd / "C01_harmonise_studies" / "validation_report.json")
        fails = [c["message"] for c in (rep or {}).get("checks", []) if c["status"] == "fail"]
        if fails:
            return "The studies cannot be compared: " + " ".join(fails)
        logs = sorted((rd / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime)
        if logs:
            errs = [ln for ln in logs[-1].read_text(errors="replace").splitlines() if "ERROR" in ln or "Error" in ln]
            if errs:
                return f"Step {logs[-1].stem} failed: " + " ".join(errs[-3:])
    tail = (d / "console.log").read_text(errors="replace").splitlines()[-5:] if (d / "console.log").exists() else []
    return "The comparison failed. " + " ".join(tail)


# ---- Reading results -----------------------------------------------------------------------------
def _csv_rows(p: Path, n: int = 50) -> list[dict]:
    if not p.exists():
        return []
    with open(p, newline="") as f:
        out = []
        for i, r in enumerate(csv.DictReader(f)):
            if i >= n:
                break
            out.append(r)
        return out


def _progress(d: Path, status: str) -> list[dict]:
    spec = ((codebank.manifest().get("modules") or {}).get("compare") or {}).get("steps") or []
    steps = {s["name"]: {"name": s["name"], "description": s.get("description", ""), "status": "pending"} for s in spec}
    log = d / "console.log"
    current = None
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            if m := STEP_START.search(line):
                current = m.group(3)
                if current in steps:
                    steps[current]["status"] = "running"
            elif current and "done in" in line:
                steps[current]["status"] = "success"; current = None
            elif current and "FAILED (exit code" in line:
                steps[current]["status"] = "failed"; current = None
    if status in FINAL:
        for s in steps.values():
            if s["status"] in ("running", "pending"):
                s["status"] = "failed" if s["status"] == "running" else "skipped"
    return list(steps.values())


def _exists(rd: Path, rel: str) -> str | None:
    return rel if (rd / rel).exists() else None


def overview(d: Path) -> dict:
    rec = _load(d)
    out = {k: rec.get(k) for k in ("id", "title", "status", "created", "started", "finished", "error", "studies", "settings")}
    out["steps"] = _progress(d, rec["status"])
    rd = run_dir_of(d)
    if not rd:
        return out
    out["run_id"] = rd.name
    st = _json(rd / "C01_harmonise_studies" / "studies.json") or {}
    gc = _json(rd / "C02_compare_genes" / "gene_comparison.json") or {}
    rr = _json(rd / "C03_rrho" / "rrho_summary.json") or {}
    pw = _json(rd / "C04_compare_pathways" / "pathway_comparison.json") or {}
    summ = _json(rd / "run_summary.json") or {}
    out["validation"] = _json(rd / "C01_harmonise_studies" / "validation_report.json")
    out["study_results"] = st.get("studies") or []
    for k in ("match_method", "rank_by", "significance_rule", "n_genes_in_all_studies"):
        out[k] = st.get(k)
    rrho = {(p["study_a"], p["study_b"]): p for p in rr.get("pairs") or []}
    paths = {}
    for p in pw.get("pairs") or []:
        paths.setdefault((p["study_a"], p["study_b"]), []).append({**p, "figure": _exists(rd, f"C04_compare_pathways/{p['folder']}/nes_scatter.png")})
    pairs = []
    for p in gc.get("pairs") or []:
        a, b = p["study_a"], p["study_b"]
        folder = p.get("folder") or pair_folder(a, b)
        r = rrho.get((a, b)) or {}
        pairs.append({**p, "rrho": {k: r.get(k) for k in ("status", "reason", "pattern", "best", "n_genes", "step",
                                                          "strongest_concordant", "strongest_discordant")},
                      "pathways": paths.get((a, b), []),
                      "figures": {"scatter": _exists(rd, f"C02_compare_genes/{folder}/fc_scatter.png"),
                                  "overlap": _exists(rd, f"C02_compare_genes/{folder}/overlap_matrix.png"),
                                  "rrho": _exists(rd, f"C03_rrho/{folder}/rrho_map.png")},
                      "tables": [t for t in (f"C02_compare_genes/{folder}/genes_compared.csv", f"C03_rrho/{folder}/rrho_best_genes_up_up.csv",
                                             f"C03_rrho/{folder}/rrho_best_genes_down_down.csv") if (rd / t).exists()]})
    out["pairs"] = pairs
    out["figures"] = [f for f in (
        {"path": _exists(rd, "C02_compare_genes/correlation_heatmap.png"), "title": "Fold-change correlation between studies",
         "caption": "Spearman correlation of log2 fold changes on the genes each pair of studies shares."},
        {"path": _exists(rd, "C02_compare_genes/intersections.png"), "title": "Significant genes shared by the studies",
         "caption": "Genes tested in every study, counted once in the exact set of studies where they are significant."},
    ) if f["path"]]
    out["pathway_collections"] = [
        {"collection": k, **v, "heatmap": _exists(rd, f"C04_compare_pathways/{v['folder']}/nes_heatmap.png"),
         "table": _exists(rd, f"C04_compare_pathways/{v['folder']}/pathways_all_studies.csv")}
        for k, v in (pw.get("collections") or {}).items()] if pw.get("status") == "done" else []
    out["pathways_status"] = {"status": pw.get("status"), "reason": pw.get("reason"), "gsea_source": pw.get("gsea_source"),
                              "padj_cutoff": pw.get("padj_cutoff")} if pw else None
    out["recurrent_genes"] = _csv_rows(rd / "C02_compare_genes" / "genes_significant_in_several_studies.csv", 100)
    out["warnings"] = summ.get("warnings") or []
    m = rd / "C05_compare_report" / "methods.md"
    out["methods_md"] = m.read_text() if m.exists() else None
    out["tables"] = [t for t in ("C01_harmonise_studies/matched_genes.csv", "C02_compare_genes/genes_significant_in_several_studies.csv",
                                 "C02_compare_genes/correlation_matrix.csv", "C02_compare_genes/intersections.csv") if (rd / t).exists()]
    out["report_ready"] = (d / "report" / "comparison_report.html").exists()
    return out


def list_all() -> list[dict]:
    if not base_dir().exists():
        return []
    out = []
    for f in sorted(base_dir().glob("*/comparison.json"), reverse=True):
        rec = json.loads(f.read_text())
        out.append({k: rec.get(k) for k in ("id", "title", "status", "created", "finished", "studies")})
    return out


def safe_file(d: Path, rel: str) -> Path:
    rd = run_dir_of(d)
    if rd is None:
        raise KeyError(rel)
    p = (rd / rel).resolve()
    if rd.resolve() not in p.parents or not p.is_file() or p.suffix.lower() not in SERVABLE:
        raise KeyError(rel)
    return p


def results_zip(d: Path) -> Path:
    rd = run_dir_of(d)
    if rd is None or not (rd / "run_summary.json").exists():
        raise KeyError("no results")
    target = d / f"{rd.name}.zip"
    if not target.exists() or target.stat().st_mtime < (rd / "run_summary.json").stat().st_mtime:
        tmp = shutil.make_archive(str(d / (rd.name + "_tmp")), "zip", root_dir=rd.parent, base_dir=rd.name)
        Path(tmp).replace(target)
    return target


# ---- Code .zip ---------------------------------------------------------------------------------
def code_zip(d: Path) -> Path:
    """Code bank at the commit used, the comparison's settings and its inputs (each study's results), and a
    script that reruns it: for a paper's code availability statement."""
    rd = run_dir_of(d)
    if rd is None or not (rd / "params.yaml").exists():
        raise KeyError("no results")
    prov = _json(rd / "provenance.json") or {}
    cb = prov.get("code_bank") or _load(d).get("codebank") or {}
    name = f"code_{rd.name}"
    out = d / "code"
    out.mkdir(exist_ok=True)
    target = out / f"{name}.zip"
    params = yaml.safe_load((rd / "params.yaml").read_text()) or {}
    # point every study at its copy inside the zip
    for i, s in enumerate(params.get("studies") or []):
        folder = Path(s.get("input_dir") or "").name
        s["input_dir"] = f"comparison/inputs/{folder}"
        if s.get("de_file"):
            s["de_file"] = f"comparison/inputs/{folder}/{Path(s['de_file']).name}"
        for k in ("run_dir", "original_run_dir", "original_de_file"):
            s.pop(k, None)
    params.pop("based_on", None)
    with zipfile.ZipFile(target.with_suffix(".tmp"), "w", zipfile.ZIP_DEFLATED) as z:
        def put(rel: str, data: bytes | str, executable: bool = False) -> None:
            info = zipfile.ZipInfo(f"{name}/{rel}", date_time=datetime.now().timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if executable else 0o644) << 16
            z.writestr(info, data if isinstance(data, bytes) else data.encode())
        note = code_bundle._add_codebank(put, cb, paths=["compare", "R", "scripts", "manifest", "README.md", "CHANGELOG.md", "LICENSE"])
        put("comparison/params.yaml", yaml.safe_dump(params, sort_keys=False))
        for f in sorted((rd / "inputs").rglob("*")):
            if f.is_file():
                put(f"comparison/inputs/{f.relative_to(rd / 'inputs')}", f.read_bytes())
        for f in ("provenance.json", "run_summary.json", "C05_compare_report/methods.md"):
            if (rd / f).exists():
                put(f"environment/{Path(f).name}" if f != "C05_compare_report/methods.md" else "methods.md", (rd / f).read_bytes())
        put("run_comparison.sh", "#!/usr/bin/env bash\n# Reruns the comparison with the code bank in codebank/ (R and the packages in environment/provenance.json).\n"
            "set -euo pipefail\ncd \"$(dirname \"$0\")\"\nmkdir -p results\n"
            "Rscript codebank/compare/scripts/run_compare.R --config comparison/params.yaml --runs_dir results\n", executable=True)
        put("README.md", _code_readme(_load(d), rd, cb, note))
    target.with_suffix(".tmp").replace(target)
    return target


def _code_readme(rec: dict, rd: Path, cb: dict, note: str) -> str:
    studies = "\n".join(f"- **{s['label']}**: {s.get('comparison', '')}"
                        f" ({'analysis on this site' if s['kind'] == 'run' else 'results table ' + s.get('file_name', '')})"
                        f"{', fold changes flipped' if s.get('flip') else ''}" for s in rec["studies"])
    return f"""# Code: {rec['title']}

Cross-study comparison made with the RNA-seq code bank's compare module
(version {cb.get('version') or '?'}, commit {cb.get('commit') or 'unknown'}).
Studies are compared by their results, not by merging counts.

## Studies

{studies}

## Contents

- `codebank/`: the code bank ({note})
- `comparison/params.yaml`: every setting of the comparison
- `comparison/inputs/`: each study's DE results (and GSEA tables) exactly as compared
- `environment/provenance.json`: R and package versions, step records
- `methods.md`: the methods paragraph
- `run_comparison.sh`: reruns the comparison into `results/`

## Rerun

```bash
bash run_comparison.sh
```

Needs R with data.table, yaml, jsonlite, optparse, ggplot2, ggrepel, fgsea, msigdbr and babelgene
(`Rscript codebank/scripts/00_setup_environment.R` installs them, if that script is included, or see the code bank README).
"""


# ---- Report -------------------------------------------------------------------------------------
def report_html(d: Path) -> Path:
    from .report import CSS, _md, _table
    E = html.escape
    ov = overview(d)
    rd = run_dir_of(d)
    if rd is None or ov["status"] not in ("success", "success_with_errors"):
        raise KeyError("no results")

    def img(rel: str | None, title: str, caption: str, small: bool = False) -> str:
        if not rel:
            return ""
        data = base64.b64encode((rd / rel).read_bytes()).decode()
        style = ' style="max-width:520px"' if small else ""
        return (f'<figure{style}><img src="data:image/png;base64,{data}" alt="{E(title)}">'
                f"<figcaption><strong>{E(title)}.</strong> {E(caption)}</figcaption></figure>")

    def p_(x) -> str:
        if x is None:
            return "–"
        x = float(x)
        return "< 1e-300" if x == 0 else (f"{x:.1e}" if x < 0.001 else f"{x:.3f}")

    def r_(x) -> str:
        return "–" if x is None else f"{float(x):.2f}"

    studies = _table(["Study", "Compared", "Organism", "Genes matched", "Significant", "Up", "Down"],
                     [[s["label"], s["comparison"] + (" (flipped)" if s.get("flipped") else ""), s["organism"], f"{s['n_matched']:,}",
                       f"{s['n_significant']:,}", f"{s['n_up']:,}", f"{s['n_down']:,}"] for s in ov["study_results"]], {3, 4, 5, 6})
    pair_rows = [[f"{p['study_a']} vs {p['study_b']}", f"{p['n_shared_genes']:,}", r_(p["spearman_log2FC"]["r"]),
                  r_(p["spearman_log2FC_significant_either"]["r"]), f"{p['up_up']} ({p['expected_up_up']:.0f})",
                  f"{p['down_down']} ({p['expected_down_down']:.0f})", p_(p["fisher_up_up"]["p"]), p_(p["fisher_down_down"]["p"]),
                  f"{p['n_opposite_direction']}", (p["rrho"].get("pattern") or "–").split(" (")[0]] for p in ov["pairs"]]
    pairs_tbl = _table(["Pair", "Shared genes", "Spearman (all)", "Spearman (signif.)", "Up in both (expected)",
                        "Down in both (expected)", "p up/up", "p down/down", "Opposite", "RRHO"], pair_rows, {1, 2, 3, 6, 7, 8})
    sections = []
    for p in ov["pairs"]:
        a, b = p["study_a"], p["study_b"]
        top_up = ", ".join(g["gene"] for g in p.get("top_shared_up") or []) or "none"
        top_down = ", ".join(g["gene"] for g in p.get("top_shared_down") or []) or "none"
        pw = "".join(
            f"<p><strong>{E(x['collection'])} pathways:</strong> {x['n_significant_both']} significant in both "
            f"({x['n_same_direction']} same direction, {x['n_opposite_direction']} opposite); NES Spearman {r_(x['spearman_NES'])}. "
            f"Shared: {E(', '.join(t['pathway'] for t in x.get('top_shared') or []) or 'none')}.</p>"
            + img(x.get("figure"), f"{x['collection']} pathways, {a} vs {b}", "Normalised enrichment score of each pathway in the two studies.")
            for x in p["pathways"])
        sections.append(
            f"<h3>{E(a)} vs {E(b)}</h3>"
            f"<p>{p['n_shared_genes']:,} shared genes. {p['n_significant_both']} genes are significant in both studies, "
            f"{p['n_same_direction']} of them in the same direction (Jaccard index {r_(p['jaccard'])}). "
            f"Top genes up in both: {E(top_up)}. Down in both: {E(top_down)}.</p>"
            + img(p["figures"]["scatter"], f"Fold changes, {a} vs {b}", "Each dot is a gene tested in both studies, coloured by where it is significant.")
            + img(p["figures"]["overlap"], f"Significant genes, {a} vs {b}", "Genes by direction in each study; p values from one-sided Fisher's exact tests.")
            + img(p["figures"]["rrho"], f"Rank-rank overlap, {a} vs {b}",
                  "Overlap of the two rankings at every pair of thresholds (BY-adjusted). Hot spots at bottom left (up in both) and top right (down in both) mean the studies agree.")
            + pw)
    overall = "".join(img(f["path"], f["title"], f["caption"], small="correlation" in f["path"]) for f in ov["figures"])
    heat = "".join(img(c.get("heatmap"), f"{c['collection']} pathways across studies",
                       "NES of the top pathways significant in at least one study; * padj below the cutoff.") for c in ov["pathway_collections"])
    rec_rows = [[g["gene"], g["n_up"], g["n_down"], "yes" if g["consistent"] in ("TRUE", "True", "true") else "no"]
                for g in ov["recurrent_genes"][:40]]
    plain = [re.sub(r"^\[validation:[^]]+\] ", "", w) for w in ov["warnings"]]
    warn = "".join(f"<li>{E(w)}</li>" for w in plain)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{E(ov['title'])}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{CSS}</style></head><body><main>
<h1>{E(ov['title'])}</h1>
<p class="muted">Cross-study comparison · code bank compare module · {E((ov.get('finished') or '')[:16].replace('T', ' '))} UTC</p>
<section><h2>What was compared</h2>
<p>Each study was analysed on its own; the comparison uses their results, not merged counts. Genes were matched by
{E(ov.get('match_method') or '')}; a gene is significant when {E(ov.get('significance_rule') or '')} (the same rule for every study);
RRHO ranks genes by {E(ov.get('rank_by') or '')}.</p>{studies}
{f'<div class="warn"><strong>Check:</strong><ul>{warn}</ul></div>' if warn else ''}</section>
<section><h2>Agreement at a glance</h2>{pairs_tbl}
<p class="muted">Expected = the overlap if the two studies' significant genes were unrelated. Correlations of all genes are often modest even
for studies that agree, because most genes do not change; the overlaps and RRHO are more telling.</p>{overall}</section>
<section><h2>Each pair</h2>{''.join(sections)}</section>
{f'<section><h2>Pathways across studies</h2>{heat}</section>' if heat else ''}
{f'<section><h2>Genes significant in several studies</h2>{_table(["Gene", "Up in", "Down in", "Same direction"], rec_rows, {1, 2})}</section>' if rec_rows else ''}
<section><h2>Methods</h2>{_md(ov['methods_md'] or '')}</section>
</main></body></html>"""
    out = d / "report"
    out.mkdir(exist_ok=True)
    (out / "comparison_report.html").write_text(page)
    (out / "comparison_report.pdf").unlink(missing_ok=True)
    return out / "comparison_report.html"


def report_pdf(d: Path) -> Path:
    from .report import print_pdf
    h = d / "report" / "comparison_report.html"
    if not h.exists():
        report_html(d)
    return print_pdf(h, d / "report" / "comparison_report.pdf")


comparisons = ComparisonManager()
