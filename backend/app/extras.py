"""Optional analyses on top of a finished run: alternative splicing and transcript isoforms.

Both use the run's own comparison, so the user only adds the extra input:

  splicing   rMATS output tables (or a .zip of them)      -> code bank splicing/ module
             compared with this run's gene-level results (de_run)
  isoforms   a .zip of Salmon output folders (<sample>/quant.sf) and, unless the
             transcript names carry the gene (GENCODE), a GTF or tx2gene file
             -> code bank isoforms/ module: DEI (DESeq2) and DUI (DEXSeq + stageR)
             with this run's sample table, groups and covariates

Each analysis lives in data/jobs/<job>/extras/<module>/<id>/:
    extra.json         state (queued / running / success / success_with_errors / failed)
    inputs/            the uploaded files, unpacked
    run_config.yaml    every parameter passed to the module
    console.log        the module's output; runs/<module run folder>/ all results
The latest analysis of each module is the one shown; earlier ones are kept.
"""

import csv
import gzip
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

from . import codebank
from .config import settings

MODULES = {
    "splicing": {"entry": "splicing/scripts/run_splicing.R", "title": "Alternative splicing (rMATS)"},
    "isoforms": {"entry": "isoforms/scripts/run_isoforms.R", "title": "Transcript isoforms (DEI and DUI)"},
}
RMATS_TABLE = re.compile(r"^(SE|MXE|A3SS|A5SS|RI)\.MATS\.(JC|JCEC)\.txt$")
SERVABLE = {".png", ".csv", ".json", ".md", ".txt", ".yaml"}
FINAL = {"success", "success_with_errors", "failed", "interrupted"}
STEP_START = re.compile(r"Step (\d+)/(\d+): (\S+) \.\.\.")
MAX_UNZIPPED = 5 * 1024**3   # refuse archives that unpack to more than 5 GB


class ExtraError(ValueError):
    """A problem with the user's files or settings, in plain language."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def available() -> dict:
    """Optional modules this code bank has (older code banks have none)."""
    mods = codebank.manifest().get("modules") or {}
    return {k: v["title"] for k, v in MODULES.items() if k in mods}


# ---- Storage -------------------------------------------------------------------------------
def _module_dir(job_dir: Path, module: str) -> Path:
    if module not in MODULES:
        raise KeyError(module)
    return job_dir / "extras" / module


def _load(d: Path) -> dict:
    return json.loads((d / "extra.json").read_text())


def _save(d: Path, rec: dict) -> None:
    tmp = d / "extra.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2, default=str))
    tmp.replace(d / "extra.json")


def latest(job_dir: Path, module: str) -> Path | None:
    base = _module_dir(job_dir, module)
    if not base.exists():
        return None
    dirs = sorted(p for p in base.iterdir() if (p / "extra.json").exists())
    return dirs[-1] if dirs else None


def run_dir_of(d: Path) -> Path | None:
    runs = sorted(p for p in (d / "runs").iterdir() if p.is_dir()) if (d / "runs").exists() else []
    return runs[-1] if runs else None


# ---- Unpacking uploads ----------------------------------------------------------------------
def _safe_extract(zf: zipfile.ZipFile, dest: Path, keep) -> list[Path]:
    """Extract the members `keep(name)` accepts, refusing paths outside dest and zip bombs."""
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_UNZIPPED:
        raise ExtraError("The .zip file unpacks to more than 5 GB.")
    out = []
    root = dest.resolve()
    for info in zf.infolist():
        if info.is_dir() or not keep(info.filename):
            continue
        target = (dest / info.filename).resolve()
        if root not in target.parents:
            raise ExtraError(f"The .zip file contains an unsafe path: {info.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        out.append(target)
    return out


def _save_upload(src: BinaryIO, dest: Path) -> Path:
    limit = settings.max_upload_mb * 1024 * 1024
    written = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        while chunk := src.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise ExtraError(f"A file is larger than {settings.max_upload_mb} MB (MAX_UPLOAD_MB).")
            out.write(chunk)
    return dest


def _rmats_inputs(files: list[tuple[str, BinaryIO]], inputs: Path) -> list[str]:
    tables = inputs / "rmats"
    tables.mkdir(parents=True)
    for name, fh in files:
        base = Path(name).name
        if base.lower().endswith(".zip"):
            tmp = _save_upload(fh, inputs / "upload.zip")
            try:
                with zipfile.ZipFile(tmp) as zf:
                    for p in _safe_extract(zf, inputs / "unzipped", lambda n: bool(RMATS_TABLE.match(Path(n).name))):
                        shutil.move(str(p), tables / p.name)
            except zipfile.BadZipFile as e:
                raise ExtraError(f"'{base}' is not a readable .zip file.") from e
            finally:
                tmp.unlink(missing_ok=True)
                shutil.rmtree(inputs / "unzipped", ignore_errors=True)
        elif RMATS_TABLE.match(base):
            _save_upload(fh, tables / base)
    found = sorted(p.name for p in tables.iterdir())
    if not found:
        raise ExtraError("No rMATS tables found. Upload the files named like SE.MATS.JCEC.txt (or a .zip of rMATS's output folder).")
    return found


def _quant_inputs(quant_zip: tuple[str, BinaryIO], inputs: Path, samples: list[str]) -> list[str]:
    name, fh = quant_zip
    if not name.lower().endswith(".zip"):
        raise ExtraError("Upload the Salmon output as one .zip file with a folder per sample (<sample>/quant.sf).")
    tmp = _save_upload(fh, inputs / "upload.zip")
    quant = inputs / "quant"
    try:
        with zipfile.ZipFile(tmp) as zf:
            files = _safe_extract(zf, inputs / "unzipped", lambda n: Path(n).name == "quant.sf")
    except zipfile.BadZipFile as e:
        raise ExtraError("The Salmon upload is not a readable .zip file.") from e
    finally:
        tmp.unlink(missing_ok=True)
    found = {}
    for f in files:
        found.setdefault(f.parent.name, f)
    if not found:
        shutil.rmtree(inputs / "unzipped", ignore_errors=True)
        raise ExtraError("No quant.sf files in the .zip. Zip the Salmon output folders (one folder per sample, named like the samples).")
    missing = [s for s in samples if s not in found]
    if missing:
        shutil.rmtree(inputs / "unzipped", ignore_errors=True)
        raise ExtraError(f"No quant.sf for sample(s) {', '.join(missing)}. The folders in the .zip are named "
                         f"{', '.join(sorted(found))}; they must match the sample names of this analysis.")
    for s in samples:
        (quant / s).mkdir(parents=True, exist_ok=True)
        shutil.move(str(found[s]), quant / s / "quant.sf")
    shutil.rmtree(inputs / "unzipped", ignore_errors=True)
    return samples


def _annotation(annotation: tuple[str, BinaryIO] | None, inputs: Path) -> dict:
    """Annotation settings for the isoform module: an uploaded GTF / tx2gene, or GENCODE transcript names."""
    if annotation is not None:
        name = Path(annotation[0]).name
        low = name.lower()
        if low.endswith((".gtf", ".gtf.gz")):
            p = _save_upload(annotation[1], inputs / ("annotation.gtf.gz" if low.endswith(".gz") else "annotation.gtf"))
            return {"gtf_file": str(p)}
        if low.endswith((".csv", ".tsv", ".txt")):
            p = _save_upload(annotation[1], inputs / ("tx2gene" + Path(low).suffix))
            return {"tx2gene_file": str(p)}
        raise ExtraError("The annotation must be a GTF (.gtf or .gtf.gz) or a table with transcript_id and gene_id (.csv/.tsv).")
    # GENCODE transcript FASTA names: ENST...|ENSG...|OTTHUMG|OTTHUMT|tx name|gene name|length|biotype|
    first = next(iter(sorted((inputs / "quant").glob("*/quant.sf"))))
    rows = []
    with open(first) as f:
        next(f)
        for line in f:
            parts = line.split("\t", 1)[0].split("|")
            if len(parts) < 6:
                raise ExtraError("The transcript names do not include their gene. Upload the GTF the Salmon index was "
                                 "built from (or a table with transcript_id and gene_id).")
            rows.append((parts[0], parts[1], parts[5]))
    p = inputs / "tx2gene_from_names.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["transcript_id", "gene_id", "gene_name"])
        w.writerows(rows)
    return {"tx2gene_file": str(p)}


# ---- Starting an analysis ----------------------------------------------------------------------
class ExtrasManager:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extras")
        self._lock = threading.Lock()

    def _new(self, job_dir: Path, module: str) -> tuple[Path, dict]:
        base = _module_dir(job_dir, module)
        prev = latest(job_dir, module)
        if prev and _load(prev)["status"] not in FINAL:
            raise ExtraError("This analysis is already running. Wait for it to finish.")
        eid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        d = base / eid
        (d / "inputs").mkdir(parents=True)
        (d / "runs").mkdir()
        return d, {"id": eid, "module": module, "status": "queued", "created": _now(), "started": None, "finished": None,
                   "error": None, "codebank": codebank.version_info()}

    def start_splicing(self, job: dict, job_dir: Path, run_dir: Path, files: list[tuple[str, BinaryIO]], *,
                       b1_group: str = "test", count_type: str = "JCEC", fdr_cutoff: float = 0.05,
                       delta_psi_cutoff: float = 0.1, min_reads_per_sample: float = 10) -> dict:
        s = job["settings"]
        test, ref = s["test_level"], s["reference_level"]
        if b1_group not in ("test", "reference"):
            raise ExtraError("b1_group must be 'test' or 'reference'.")
        if count_type not in ("JCEC", "JC"):
            raise ExtraError("count_type must be JCEC or JC.")
        if not (0 < fdr_cutoff <= 1) or not (0 <= delta_psi_cutoff < 1) or min_reads_per_sample < 0:
            raise ExtraError("Cutoffs out of range (FDR 0-1, dPSI 0-1, reads >= 0).")
        d, rec = self._new(job_dir, "splicing")
        try:
            tables = _rmats_inputs(files, d / "inputs")
            if not any(t.endswith(f".MATS.{count_type}.txt") for t in tables):
                raise ExtraError(f"No {count_type} tables among the uploaded files ({', '.join(tables)}).")
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        resolved = _resolved(run_dir)
        g1, g2 = (test, ref) if b1_group == "test" else (ref, test)
        cfg = {"run_name": f"{test}_vs_{ref}_splicing", "rmats_dir": str(d / "inputs" / "rmats"), "count_type": count_type,
               "group_1_name": g1, "group_2_name": g2, "organism": resolved.get("organism") or "auto",
               "fdr_cutoff": fdr_cutoff, "delta_psi_cutoff": delta_psi_cutoff, "min_reads_per_sample": min_reads_per_sample,
               "de_run": str(run_dir)}
        adv = s.get("advanced") or {}
        for k in ("gene_set_collections", "run_enrichment", "plot_palette", "up_colour", "down_colour", "group_colours"):
            if k in adv:
                cfg[k] = adv[k]
        rec.update(settings={"b1_group": b1_group, "count_type": count_type, "fdr_cutoff": fdr_cutoff,
                             "delta_psi_cutoff": delta_psi_cutoff, "min_reads_per_sample": min_reads_per_sample},
                   inputs=tables, note=None if b1_group == "test" else
                   f"rMATS sample 1 (--b1) was {ref}: dPSI = PSI({ref}) - PSI({test}); positive = more inclusion in {ref}.")
        return self._launch(d, rec, cfg)

    def start_isoforms(self, job: dict, job_dir: Path, run_dir: Path, quant_zip: tuple[str, BinaryIO],
                       annotation: tuple[str, BinaryIO] | None = None, *, min_delta_usage: float = 0.1,
                       dui_alpha: float = 0.05) -> dict:
        if not (0 < dui_alpha <= 1) or not (0 <= min_delta_usage < 1):
            raise ExtraError("Cutoffs out of range (FDR 0-1, delta usage 0-1).")
        s = job["settings"]
        meta = run_dir / "01_validate_inputs" / "metadata_validated.csv"
        with open(meta, newline="") as f:
            samples = [r[0] for i, r in enumerate(csv.reader(f)) if i > 0 and r]
        d, rec = self._new(job_dir, "isoforms")
        try:
            _quant_inputs(quant_zip, d / "inputs", samples)
            ann = _annotation(annotation, d / "inputs")
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        shutil.copy(meta, d / "inputs" / "samples.csv")
        adv = s.get("advanced") or {}
        cfg = {"run_name": f"{s['test_level']}_vs_{s['reference_level']}_isoforms", "quant_dir": str(d / "inputs" / "quant"),
               "quant_type": "salmon", "metadata_file": str(d / "inputs" / "samples.csv"), **ann,
               "condition": s["condition"], "reference_level": s["reference_level"], "test_level": s["test_level"],
               "covariates": s.get("covariates") or [], "organism": _resolved(run_dir).get("organism") or "auto",
               "dui_alpha": dui_alpha, "min_delta_usage": min_delta_usage, "threads": 2}
        for k in ("padj_cutoff", "log2fc_cutoff", "lfc_shrinkage", "min_count", "plot_palette", "up_colour", "down_colour"):
            if k in adv:
                cfg[k] = adv[k]
        rec.update(settings={"min_delta_usage": min_delta_usage, "dui_alpha": dui_alpha,
                             "annotation": Path(next(iter(ann.values()))).name},
                   inputs=samples, note=None)
        return self._launch(d, rec, cfg)

    def _launch(self, d: Path, rec: dict, cfg: dict) -> dict:
        (d / "run_config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        cmd = [settings.rscript, str(codebank.path(MODULES[rec["module"]]["entry"])), "--config", str(d / "run_config.yaml"),
               "--runs_dir", str(d / "runs")]
        rec["command"] = cmd
        _save(d, rec)
        self._pool.submit(self._run, d, cmd)
        return overview(d)

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
        _refresh_report(d)

    def recover(self, jobs_dir: Path) -> None:
        """Analyses left running by a stopped server cannot finish."""
        for f in jobs_dir.glob("*/extras/*/*/extra.json"):
            rec = json.loads(f.read_text())
            if rec["status"] not in FINAL:
                rec.update(status="interrupted", finished=_now(), error="The app was stopped while this analysis was running. Start it again.")
                _save(f.parent, rec)


def _failure(d: Path, rd: Path | None) -> str:
    if rd:
        for rep in sorted(rd.glob("*/validation_report.json")):
            fails = [c["message"] for c in json.loads(rep.read_text()).get("checks", []) if c["status"] == "fail"]
            if fails:
                return "The input check failed: " + " ".join(fails)
        logs = sorted((rd / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime)
        if logs:
            errs = [ln for ln in logs[-1].read_text(errors="replace").splitlines() if "ERROR" in ln or "Error" in ln]
            if errs:
                return f"Step {logs[-1].stem} failed: " + " ".join(errs[-3:])
    tail = (d / "console.log").read_text(errors="replace").splitlines()[-5:] if (d / "console.log").exists() else []
    return "The analysis failed. " + " ".join(tail)


def _refresh_report(d: Path) -> None:
    """Re-render the run's report so it includes the finished analysis (the narrative is kept)."""
    try:
        from . import report
        from .jobs import jobs
        job_dir = d.parents[2]
        if (job_dir / "report" / "narrative.json").exists():
            job = jobs.load(job_dir.name)
            report.build(job, job_dir, Path(job["run_dir"]))
    except Exception:  # the analysis itself succeeded; the report can be rewritten from the run page
        pass


# ---- Reading results -------------------------------------------------------------------------------
def _json(p: Path):
    return json.loads(p.read_text()) if p and p.exists() else None


def _resolved(run_dir: Path) -> dict:
    p = run_dir / "01_validate_inputs" / "resolved_params.yaml"
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def _csv_head(p: Path, n: int = 50) -> list[dict]:
    if not p.exists():
        return []
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", newline="") as f:
        out = []
        for i, r in enumerate(csv.DictReader(f)):
            if i >= n:
                break
            out.append(r)
        return out


FIGURES = {
    "splicing": [
        ("S02_splicing_results/event_counts.png", "Significant events by type", "Events per type that pass the significance rule, split by direction."),
        ("S02_splicing_results/dpsi_volcano.png", "Change in inclusion", "Each dot is an event: change in percent spliced in (x) against the evidence (y)."),
        ("S02_splicing_results/dpsi_distribution.png", "dPSI by event type", "Violins: all events with enough reads; points: significant events."),
        ("S02_splicing_results/splicing_vs_expression.png", "Splicing vs expression", "Genes whose splicing changes, whose expression changes (this run), or both."),
        ("S02_splicing_results/psi_scatter.png", "PSI in each group", "Mean inclusion per event in the two groups."),
        ("S02_splicing_results/top_events_heatmap.png", "Top events per sample", "Inclusion (PSI) of the strongest significant events in every sample."),
    ],
    "isoforms": [
        ("I03_differential_isoform_usage/dui_top_genes_usage.png", "Isoform usage, top genes", "Share of each gene made up by each transcript; bars are group means, points samples."),
        ("I03_differential_isoform_usage/dui_volcano.png", "Differential isoform usage", "Change in a transcript's share of its gene against the evidence."),
        ("I02_differential_isoform_expression/dei_volcano.png", "Differential isoform expression", "Each dot is a transcript: fold change against the evidence."),
        ("I02_differential_isoform_expression/dei_ma_plot.png", "MA plot, transcripts", "Fold change against mean expression."),
    ],
}


def overview(d: Path) -> dict:
    rec = _load(d)
    rd = run_dir_of(d)
    module = rec["module"]
    out = {k: rec.get(k) for k in ("id", "module", "status", "created", "started", "finished", "error", "settings", "inputs", "note")}
    out["title"] = MODULES[module]["title"]
    out["steps"] = _progress(d, module, rec["status"])
    if not rd:
        return out
    out["run_id"] = rd.name
    out["figures"] = [{"path": p, "title": t, "caption": c} for p, t, c in FIGURES[module] if (rd / p).exists()]
    if module == "splicing":
        genes = ((_json(rd / "S02_splicing_results" / "splicing_summary.json") or {}).get("splicing_vs_expression") or {}).get("genes") or {}
        if not any(k in genes for k in ("splicing and expression", "splicing only", "expression only", "neither")):
            # no gene matched the run's DE results (e.g. other gene IDs): the comparison figure is empty
            out["figures"] = [f for f in out["figures"] if "splicing_vs_expression" not in f["path"]]
    out["methods_md"] = (rd / f"{'S04_splicing_report' if module == 'splicing' else 'I04_isoform_report'}" / "methods.md").read_text() \
        if (rd / f"{'S04_splicing_report' if module == 'splicing' else 'I04_isoform_report'}" / "methods.md").exists() else None
    out["validation"] = _json(next(iter(sorted(rd.glob("*/validation_report.json"))), None))
    if module == "splicing":
        s = _json(rd / "S02_splicing_results" / "splicing_summary.json") or {}
        out["summary"] = {k: s.get(k) for k in ("comparison", "sign_convention", "significance_rule", "n_events_tested",
                                                 "n_events_passing_coverage", "n_significant", "n_up", "n_down",
                                                 "n_genes_significant", "by_event_type", "splicing_vs_expression")}
        out["top"] = _csv_head(rd / "S02_splicing_results" / "splicing_events_significant.csv", 50)
        out["enrichment"] = _json(rd / "S03_splicing_enrichment" / "enrichment_summary.json")
        out["tables"] = [p for p in ("S02_splicing_results/splicing_events_all.csv", "S02_splicing_results/splicing_events_significant.csv",
                                     "S02_splicing_results/splicing_vs_expression.csv") if (rd / p).exists()]
    else:
        dei = _json(rd / "I02_differential_isoform_expression" / "dei_summary.json") or {}
        dui = _json(rd / "I03_differential_isoform_usage" / "dui_summary.json") or {}
        out["summary"] = {"dei": {k: dei.get(k) for k in ("comparison", "significance_rule", "n_transcripts_tested", "n_significant",
                                                           "n_up", "n_down", "n_genes_with_significant_transcripts")},
                          "dui": {k: dui.get(k) for k in ("status", "reason", "significance_rule", "n_genes_tested", "n_genes_significant",
                                                           "n_isoform_switches", "n_transcripts_significant", "sign_convention")}}
        out["top"] = [r for r in _csv_head(rd / "I03_differential_isoform_usage" / "dui_genes.csv", 200) if r.get("significant") in ("TRUE", "True", "true")][:50]
        out["top_dei"] = _csv_head(rd / "I02_differential_isoform_expression" / "dei_results_significant.csv", 50)
        out["tables"] = [p for p in ("I02_differential_isoform_expression/dei_results_all.csv",
                                     "I03_differential_isoform_usage/dui_transcripts_all.csv",
                                     "I03_differential_isoform_usage/dui_genes.csv") if (rd / p).exists()]
    return out


def _progress(d: Path, module: str, status: str) -> list[dict]:
    spec = (codebank.manifest().get("modules") or {}).get(module, {}).get("steps") or []
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


def all_for(job_dir: Path) -> dict:
    out = {}
    for m in MODULES:
        d = latest(job_dir, m)
        out[m] = overview(d) if d else None
    return out


def safe_file(d: Path, rel: str) -> Path:
    rd = run_dir_of(d)
    if rd is None:
        raise KeyError(rel)
    p = (rd / rel).resolve()
    if rd.resolve() not in p.parents or not p.is_file() or p.suffix.lower() not in SERVABLE:
        raise KeyError(rel)
    return p


def bundle(d: Path) -> Path:
    rd = run_dir_of(d)
    if rd is None:
        raise KeyError("no results")
    target = d / f"{rd.name}.zip"
    if not target.exists() or target.stat().st_mtime < (rd / "run_summary.json").stat().st_mtime:
        tmp = shutil.make_archive(str(d / (rd.name + "_tmp")), "zip", root_dir=rd.parent, base_dir=rd.name)
        Path(tmp).replace(target)
    return target


extras = ExtrasManager()
