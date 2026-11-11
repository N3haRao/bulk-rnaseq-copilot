"""Optional analyses on top of a finished run, driven by the code bank's manifest.

Every code-bank module whose manifest entry has `site: {attach_to: run}` is offered on a finished run
(splicing and isoforms today). The manifest says everything the site needs, so a new module, setting,
figure or table needs no site code:

  site.inputs      uploads, each stored and checked by a handler (HANDLERS below) and passed as a parameter
  site.form        settings the user can change (number, enum, bool, text, run_group)
  site.from_run    parameters taken from the finished run (organism, groups, covariates, sample table, ...)
  site.fixed       parameters with a fixed value;  site.inherit: advanced settings copied from the run
  site.results     summary tiles, headline sentences, figures, top tables, downloads, methods, chat files
  site.rerun       how the code .zip reruns it (which parameters point at input data)

Field reference: codebank/docs/ADDING_A_MODULE.md. Each analysis lives in
data/jobs/<job>/extras/<module>/<id>/ (extra.json state, inputs/, run_config.yaml, console.log, runs/).
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
from typing import BinaryIO, Callable

import yaml

from . import codebank
from .config import settings

SERVABLE = {".png", ".csv", ".json", ".md", ".txt", ".yaml"}
FINAL = {"success", "success_with_errors", "failed", "interrupted"}
STEP_START = re.compile(r"Step (\d+)/(\d+): (\S+) \.\.\.")
MAX_UNZIPPED = 5 * 1024**3   # refuse archives that unpack to more than 5 GB
FORM_TYPES = {"number", "integer", "enum", "bool", "text", "run_group"}
COLUMN_FORMATS = {"num0", "num2", "p", "flag"}
COLUMN_STYLES = {"small", "sign"}
TILE_STYLES = {"up", "down"}
RUN_VALUES = {"organism", "dir", "condition", "reference_level", "test_level", "covariates", "metadata_file", "samples"}

Upload = tuple[str, BinaryIO]


class ExtraError(ValueError):
    """A problem with the user's files or settings, in plain language."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- What the manifest offers ---------------------------------------------------------------------
def modules() -> dict[str, dict]:
    """{module: manifest entry} for every module offered on a finished run."""
    mods = codebank.manifest().get("modules") or {}
    return {k: v for k, v in mods.items() if ((v or {}).get("site") or {}).get("attach_to") == "run"}


def available() -> dict:
    return {k: v["site"].get("title", k) for k, v in modules().items()}


def _spec(module: str) -> dict:
    m = modules().get(module)
    if m is None:
        raise KeyError(module)
    return m


def _fill(text: str, values: dict) -> str:
    """'{test}_vs_{reference}' with the run's / form's values; unknown names stay as they are."""
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: str(values.get(m.group(1), m.group(0))), str(text))


def form_spec(module: str, job: dict) -> dict:
    """What the frontend needs to draw this module's card: description, inputs and form fields with defaults."""
    m = _spec(module)
    site, params = m["site"], m.get("parameters") or {}
    words = _words(job)
    fields = []
    for f in site.get("form") or []:
        p = params.get(f["name"]) or {}
        field = {**f, "default": f.get("default", p.get("default")), "description": p.get("description")}
        if f["type"] == "enum":
            vals = f.get("values") or p.get("values") or []
            labels = f.get("labels") or {}
            field["options"] = [{"value": v, "label": labels.get(v, v)} for v in vals]
        if f["type"] == "run_group":
            field["options"] = [{"value": "test", "label": f"{words['test']} (test group)"},
                                {"value": "reference", "label": f"{words['reference']} (reference group)"}]
        fields.append(field)
    inputs = [{k: v for k, v in i.items() if k in ("name", "label", "accept", "multiple", "required", "help")}
              for i in site.get("inputs") or []]
    return {"module": module, "title": site.get("title", module), "description": _fill(" ".join(str(site.get("description", "")).split()), words),
            "inputs": inputs, "form": fields}


def _words(job: dict) -> dict:
    s = job["settings"]
    return {"test": s["test_level"], "reference": s["reference_level"], "condition": s["condition"],
            "covariates": ", ".join(s.get("covariates") or []) or "nothing"}


# ---- Storage ----------------------------------------------------------------------------------------
def _module_dir(job_dir: Path, module: str) -> Path:
    _spec(module)
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


def finished(job_dir: Path) -> list[tuple[str, Path]]:
    """[(module, analysis folder)] of the latest successful analysis of each module, in manifest order."""
    out = []
    for module in modules():
        d = latest(job_dir, module) if (job_dir / "extras" / module).exists() else None
        if d is not None and _load(d).get("status") in ("success", "success_with_errors"):
            out.append((module, d))
    return out


def run_dir_of(d: Path) -> Path | None:
    runs = sorted(p for p in (d / "runs").iterdir() if p.is_dir()) if (d / "runs").exists() else []
    return runs[-1] if runs else None


# ---- Upload handlers: store an input, check it, return the parameters it sets ---------------------------
def _safe_extract(zf: zipfile.ZipFile, dest: Path, keep: Callable[[str], bool]) -> list[Path]:
    """Extract the members `keep(name)` accepts, refusing paths outside dest and zip bombs."""
    if sum(i.file_size for i in zf.infolist()) > MAX_UNZIPPED:
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


def _sets(spec: dict) -> list[str]:
    s = spec.get("sets") or []
    return [s] if isinstance(s, str) else list(s)


def _h_file(spec: dict, uploads: list[Upload], folder: Path, ctx: dict) -> dict:
    """One file, stored as is; the parameter gets its path."""
    name, fh = uploads[0]
    base = Path(name).name
    accept = spec.get("accept") or []
    if accept and not base.lower().endswith(tuple(a.lower() for a in accept)):
        raise ExtraError(f"'{base}' is not one of the accepted file types ({', '.join(accept)}) for {spec.get('label', spec['name'])}.")
    return {_sets(spec)[0]: str(_save_upload(fh, folder / base))}


def _h_files(spec: dict, uploads: list[Upload], folder: Path, ctx: dict) -> dict:
    """Several files and/or .zip files; keeps those matching `keep` (default: all); the parameter gets the folder."""
    keep_re = re.compile(spec.get("keep") or ".*")
    folder.mkdir(parents=True, exist_ok=True)
    for name, fh in uploads:
        base = Path(name).name
        if base.lower().endswith(".zip"):
            tmp = _save_upload(fh, folder.parent / f"{spec['name']}_upload.zip")
            try:
                with zipfile.ZipFile(tmp) as zf:
                    for p in _safe_extract(zf, folder.parent / f"{spec['name']}_unzipped", lambda n: bool(keep_re.search(Path(n).name))):
                        shutil.move(str(p), folder / p.name)
            except zipfile.BadZipFile as e:
                raise ExtraError(f"'{base}' is not a readable .zip file.") from e
            finally:
                tmp.unlink(missing_ok=True)
                shutil.rmtree(folder.parent / f"{spec['name']}_unzipped", ignore_errors=True)
        elif keep_re.search(base):
            _save_upload(fh, folder / base)
    found = sorted(p.name for p in folder.iterdir())
    if not found:
        raise ExtraError(spec.get("none_found") or f"No usable files were uploaded for {spec.get('label', spec['name'])}.")
    if spec.get("require"):
        req = re.compile(_fill(spec["require"], ctx))
        if not any(req.search(f) for f in found):
            raise ExtraError(_fill(spec.get("not_required_found") or "None of the uploaded files fits the chosen settings ({found}).",
                                   {**ctx, "found": ", ".join(found)}))
    return {_sets(spec)[0]: str(folder)}


def _h_sample_folders(spec: dict, uploads: list[Upload], folder: Path, ctx: dict) -> dict:
    """A .zip with one folder per sample of the run, each holding `file` (e.g. quant.sf)."""
    name, fh = uploads[0]
    want = spec.get("file") or "quant.sf"
    if not name.lower().endswith(".zip"):
        raise ExtraError(f"Upload {spec.get('label', spec['name'])} as one .zip file with a folder per sample (<sample>/{want}).")
    tmp = _save_upload(fh, folder.parent / f"{spec['name']}_upload.zip")
    unz = folder.parent / f"{spec['name']}_unzipped"
    try:
        with zipfile.ZipFile(tmp) as zf:
            files = _safe_extract(zf, unz, lambda n: Path(n).name == want)
    except zipfile.BadZipFile as e:
        raise ExtraError(f"'{Path(name).name}' is not a readable .zip file.") from e
    finally:
        tmp.unlink(missing_ok=True)
    try:
        found = {}
        for f in files:
            found.setdefault(f.parent.name, f)
        if not found:
            raise ExtraError(f"No {want} files in the .zip. Zip the output folders (one folder per sample, named like the samples).")
        missing = [s for s in ctx["samples"] if s not in found]
        if missing:
            raise ExtraError(f"No {want} for sample(s) {', '.join(missing)}. The folders in the .zip are named "
                             f"{', '.join(sorted(found))}; they must match the sample names of this analysis.")
        for s in ctx["samples"]:
            (folder / s).mkdir(parents=True, exist_ok=True)
            shutil.move(str(found[s]), folder / s / want)
    finally:
        shutil.rmtree(unz, ignore_errors=True)
    return {_sets(spec)[0]: str(folder)}


def _h_annotation(spec: dict, uploads: list[Upload], folder: Path, ctx: dict) -> dict:
    """Transcript-to-gene annotation: an uploaded GTF or tx2gene table, or (nothing uploaded) the gene taken from
    GENCODE-style transcript names in the quantification files. Sets gtf_file or tx2gene_file."""
    folder.mkdir(parents=True, exist_ok=True)
    if uploads:
        name, fh = uploads[0]
        low = Path(name).name.lower()
        if low.endswith((".gtf", ".gtf.gz")):
            return {"gtf_file": str(_save_upload(fh, folder / ("annotation.gtf.gz" if low.endswith(".gz") else "annotation.gtf")))}
        if low.endswith((".csv", ".tsv", ".txt")):
            return {"tx2gene_file": str(_save_upload(fh, folder / ("tx2gene" + Path(low).suffix)))}
        raise ExtraError("The annotation must be a GTF (.gtf or .gtf.gz) or a table with transcript_id and gene_id (.csv/.tsv).")
    quant = next(iter(sorted(ctx["inputs"].rglob("quant.sf"))), None)
    if quant is None:
        raise ExtraError("Upload an annotation (GTF or tx2gene table).")
    rows = []
    with open(quant) as f:
        next(f)
        for line in f:
            parts = line.split("\t", 1)[0].split("|")
            if len(parts) < 6:
                raise ExtraError("The transcript names do not include their gene. Upload the GTF the Salmon index was "
                                 "built from (or a table with transcript_id and gene_id).")
            rows.append((parts[0], parts[1], parts[5]))
    p = folder / "tx2gene_from_names.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["transcript_id", "gene_id", "gene_name"])
        w.writerows(rows)
    return {"tx2gene_file": str(p)}


# name -> (handler, runs even when nothing was uploaded)
HANDLERS: dict[str, tuple[Callable, bool]] = {
    "file": (_h_file, False),
    "files": (_h_files, False),
    "sample_folders": (_h_sample_folders, False),
    "annotation": (_h_annotation, True),
}


# ---- Settings from the form and the run -------------------------------------------------------------
def _form_values(module: str, job: dict, raw: dict[str, str]) -> tuple[dict, dict, list[str]]:
    """(module parameters, settings as shown to the user, notes) from the submitted form."""
    m = _spec(module)
    params = m.get("parameters") or {}
    s = job["settings"]
    words = _words(job)
    out, shown, notes = {}, {}, []
    for f in m["site"].get("form") or []:
        name, typ = f["name"], f["type"]
        p = params.get(name) or {}
        default = f.get("default", p.get("default"))
        v = raw.get(name)
        if v is None or v == "":
            v = default
        label = f.get("label", name)
        if typ in ("number", "integer"):
            try:
                v = float(v) if typ == "number" else int(float(v))
            except (TypeError, ValueError):
                raise ExtraError(f"{label}: a number is needed.")
            lo, hi = f.get("min"), f.get("max")
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise ExtraError(f"{label} must be between {lo if lo is not None else '-inf'} and {hi if hi is not None else 'inf'}.")
        elif typ == "enum":
            allowed = f.get("values") or p.get("values") or []
            if allowed and v not in allowed:
                raise ExtraError(f"{label} must be one of {', '.join(map(str, allowed))}.")
        elif typ == "bool":
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif typ == "run_group":
            if v not in ("test", "reference"):
                raise ExtraError(f"{label} must be 'test' or 'reference'.")
            first, second = (s["test_level"], s["reference_level"]) if v == "test" else (s["reference_level"], s["test_level"])
            out[name] = first
            if f.get("pair"):
                out[f["pair"]] = second
            shown[name] = v
            note = (f.get("notes") or {}).get(v)
            if note:
                notes.append(_fill(note, words))
            continue
        out[name] = v
        shown[name] = v
    return out, shown, notes


def _from_run(module: str, job: dict, run_dir: Path, inputs: Path) -> dict:
    m = _spec(module)
    s = job["settings"]
    resolved = _resolved(run_dir)
    words = _words(job)
    meta = run_dir / "01_validate_inputs" / "metadata_validated.csv"
    out = {}
    for key, src in (m["site"].get("from_run") or {}).items():
        if isinstance(src, str) and src.startswith("run."):
            what = src[4:]
            if what == "organism":
                out[key] = resolved.get("organism") or s.get("organism") or "auto"
            elif what == "dir":
                out[key] = str(run_dir)
            elif what in ("condition", "reference_level", "test_level"):
                out[key] = s[what]
            elif what == "covariates":
                out[key] = list(s.get("covariates") or [])
            elif what == "metadata_file":
                shutil.copy(meta, inputs / "samples.csv")
                out[key] = str(inputs / "samples.csv")
            elif what == "samples":
                out[key] = _samples(run_dir)
            else:
                raise ExtraError(f"The code bank asks for '{src}', which this site does not know (manifest site.from_run).")
        else:
            out[key] = _fill(src, words) if isinstance(src, str) else src
    return out


def _samples(run_dir: Path) -> list[str]:
    meta = run_dir / "01_validate_inputs" / "metadata_validated.csv"
    with open(meta, newline="") as f:
        return [r[0] for i, r in enumerate(csv.reader(f)) if i > 0 and r]


# ---- Starting an analysis ----------------------------------------------------------------------------
class ExtrasManager:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extras")
        self._lock = threading.Lock()

    def start(self, module: str, job: dict, job_dir: Path, run_dir: Path, files: dict[str, list[Upload]],
              form: dict[str, str]) -> dict:
        m = _spec(module)
        site = m["site"]
        values, shown, notes = _form_values(module, job, form)
        for spec in site.get("inputs") or []:
            if spec.get("required") and not files.get(spec["name"]):
                raise ExtraError(f"{spec.get('label', spec['name'])}: no file was uploaded.")
            if spec["handler"] not in HANDLERS:
                raise ExtraError(f"The code bank asks for an upload handler '{spec['handler']}' that this site does not have yet.")
        d, rec = self._new(job_dir, module)
        inputs = d / "inputs"
        try:
            cfg = {**(site.get("fixed") or {}), **_from_run(module, job, run_dir, inputs)}
            adv = job["settings"].get("advanced") or {}
            cfg.update({k: adv[k] for k in site.get("inherit") or [] if k in adv})
            cfg.update(values)
            ctx = {**_words(job), **values, "samples": _samples(run_dir), "inputs": inputs}
            used = []
            for spec in site.get("inputs") or []:
                ups = files.get(spec["name"]) or []
                handler, always = HANDLERS[spec["handler"]]
                if not ups and not always:
                    continue
                set_by = handler(spec, ups, inputs / spec["name"], ctx)
                cfg.update(set_by)
                used.append({"input": spec["name"], "files": [Path(n).name for n, _ in ups],
                             "used": {k: Path(v).name for k, v in set_by.items()}})
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        rec.update(settings=shown, inputs=used, note=" ".join(notes) or None)
        return self._launch(d, rec, cfg)

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

    def _launch(self, d: Path, rec: dict, cfg: dict) -> dict:
        (d / "run_config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        cmd = [settings.rscript, str(codebank.path(_spec(rec["module"])["entry_point"])), "--config", str(d / "run_config.yaml"),
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


# ---- Reading results ----------------------------------------------------------------------------------
def _json(p: Path | None):
    return json.loads(p.read_text()) if p and p.exists() else None


def _resolved(run_dir: Path) -> dict:
    p = run_dir / "01_validate_inputs" / "resolved_params.yaml"
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def _csv_rows(p: Path, n: int, where: dict | None = None) -> list[dict]:
    if not p.exists():
        return []
    opener = gzip.open if p.suffix == ".gz" else open
    out = []
    with opener(p, "rt", newline="") as f:
        for r in csv.DictReader(f):
            if where and any(str(r.get(k, "")).upper() != str(v).upper() for k, v in where.items()):
                continue
            out.append(r)
            if len(out) >= n:
                break
    return out


def lookup(sources: dict, path: str):
    """'s.n_significant' -> the value in source s (None if missing)."""
    cur = sources
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def fmt_value(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.0f}" if v.is_integer() else f"{v:.3g}"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, dict):
        return "; ".join(f"{k}: {fmt_value(x)}" for k, x in v.items())
    if isinstance(v, list):
        return ", ".join(fmt_value(x) for x in v)
    return str(v).rstrip(".")


def headline(results: dict, sources: dict) -> list[str]:
    """The headline sentences (markdown with **bold**), with conditions applied and values filled in."""
    out = []
    for item in results.get("headline") or []:
        if isinstance(item, str):
            item = {"text": item}
        if "when" in item and not lookup(sources, item["when"]):
            continue
        if "unless" in item and lookup(sources, item["unless"]):
            continue
        out.append(re.sub(r"\{([A-Za-z_][\w.]*)\}", lambda m: fmt_value(lookup(sources, m.group(1))), item["text"]))
    return out


def overview(d: Path) -> dict:
    rec = _load(d)
    module = rec["module"]
    m = _spec(module)
    res = m["site"].get("results") or {}
    out = {k: rec.get(k) for k in ("id", "module", "status", "created", "started", "finished", "error", "settings", "inputs", "note")}
    out["title"] = m["site"].get("title", module)
    out["steps"] = _progress(d, module, rec["status"])
    rd = run_dir_of(d)
    if not rd:
        return out
    sources = {k: _json(rd / v) or {} for k, v in (res.get("sources") or {}).items()}
    out["run_id"] = rd.name
    out["headline"] = headline(res, sources)
    out["tiles"] = [{"label": t["label"], "value": lookup(sources, t["value"]), "style": t.get("style")} for t in res.get("tiles") or []]
    out["figures"] = [f for f in res.get("figures") or [] if (rd / f["path"]).exists()]
    out["top_tables"] = [{"title": t.get("title", ""), "columns": t.get("columns") or [], "empty": t.get("empty", "None."),
                          "rows": _csv_rows(rd / t["path"], int(t.get("rows", 50)), t.get("filter"))}
                         for t in res.get("top_tables") or []]
    out["tables"] = [p for p in res.get("tables") or [] if (rd / p).exists()]
    meth = rd / res["methods"] if res.get("methods") else None
    out["methods_md"] = meth.read_text() if meth and meth.exists() else None
    out["validation"] = _json(next(iter(sorted(rd.glob("*/validation_report.json"))), None))
    out["summary"] = sources
    return out


def _progress(d: Path, module: str, status: str) -> list[dict]:
    spec = _spec(module).get("steps") or []
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
    for m in modules():
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


# ---- Checking a code bank's manifest against this site (used by tests and the release agent) -------------
def check_manifest(manifest: dict | None = None) -> list[str]:
    """Problems that stop the site from offering a module exactly as the manifest describes it (empty = fine)."""
    manifest = manifest if manifest is not None else codebank.manifest()
    problems = []
    for name, m in (manifest.get("modules") or {}).items():
        site = (m or {}).get("site")
        if not site:
            problems.append(f"{name}: no `site` block (say attach_to: run, page or none)")
            continue
        where = site.get("attach_to")
        if where == "none":
            continue
        if where == "page":
            if site.get("page") not in KNOWN_PAGES:
                problems.append(f"{name}: page '{site.get('page')}' does not exist on this site (known: {', '.join(sorted(KNOWN_PAGES))})")
            continue
        if where != "run":
            problems.append(f"{name}: attach_to must be run, page or none (is '{where}')")
            continue
        params = m.get("parameters") or {}
        if not m.get("entry_point"):
            problems.append(f"{name}: no entry_point")
        for i in site.get("inputs") or []:
            if i.get("handler") not in HANDLERS:
                problems.append(f"{name}: input '{i.get('name')}' uses handler '{i.get('handler')}', which this site does not have "
                                f"(known: {', '.join(HANDLERS)})")
            for p in _sets(i):
                if p not in params:
                    problems.append(f"{name}: input '{i.get('name')}' sets '{p}', which is not a parameter of the module")
        for f in site.get("form") or []:
            if f.get("type") not in FORM_TYPES:
                problems.append(f"{name}: form field '{f.get('name')}' has type '{f.get('type')}' (known: {', '.join(sorted(FORM_TYPES))})")
            for p in [f.get("name"), f.get("pair")]:
                if p and p not in params:
                    problems.append(f"{name}: form field '{p}' is not a parameter of the module")
        for k, src in (site.get("from_run") or {}).items():
            if k not in params:
                problems.append(f"{name}: from_run sets '{k}', which is not a parameter of the module")
            if isinstance(src, str) and src.startswith("run.") and src[4:] not in RUN_VALUES:
                problems.append(f"{name}: from_run uses '{src}' (known: {', '.join('run.' + v for v in sorted(RUN_VALUES))})")
        for k in list((site.get("fixed") or {})) + list(site.get("inherit") or []):
            if k not in params:
                problems.append(f"{name}: '{k}' (fixed / inherit) is not a parameter of the module")
        res = site.get("results") or {}
        srcs = set((res.get("sources") or {}).keys())
        for t in res.get("tiles") or []:
            if str(t.get("value", "")).split(".")[0] not in srcs:
                problems.append(f"{name}: tile '{t.get('label')}' reads '{t.get('value')}' but results.sources has no '{str(t.get('value', '')).split('.')[0]}'")
        for f in res.get("figures") or []:
            if not {"path", "title"} <= set(f):
                problems.append(f"{name}: a figure needs path and title ({f})")
        for t in res.get("top_tables") or []:
            if not t.get("path"):
                problems.append(f"{name}: top table '{t.get('title')}' has no path")
            for c in t.get("columns") or []:
                if c.get("format") is not None and c["format"] not in COLUMN_FORMATS:
                    problems.append(f"{name}: column '{c.get('label')}' has format {c['format']!r} (known: {', '.join(sorted(COLUMN_FORMATS))}; "
                                    "write words like yes/no/on in quotes, YAML reads them as true/false)")
                if c.get("style") is not None and c["style"] not in COLUMN_STYLES:
                    problems.append(f"{name}: column '{c.get('label')}' has style {c['style']!r} (known: {', '.join(sorted(COLUMN_STYLES))})")
                if not (c.get("value") or c.get("template")):
                    problems.append(f"{name}: column '{c.get('label')}' needs value or template")
        for t in res.get("tiles") or []:
            if t.get("style") is not None and t["style"] not in TILE_STYLES:
                problems.append(f"{name}: tile '{t.get('label')}' has style {t['style']!r} (known: {', '.join(sorted(TILE_STYLES))})")
    return problems


KNOWN_PAGES = {"compare"}

extras = ExtrasManager()
