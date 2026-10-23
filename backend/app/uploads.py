"""Uploaded (or example) datasets, and the input check that runs step 01 on them."""

import hashlib
import json
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from . import codebank, tables
from .agents.executor import ExecutorAgent
from .config import settings
from .params import AnalysisSettings
from .setup_proposal import propose

ALLOWED_EXTENSIONS = tables.TABLE_EXTENSIONS
KEEP_CHECKS = 3


class UploadError(ValueError):
    """A problem with the user's files, explained in plain language."""


def _new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _safe_name(filename: str, kind: str) -> str:
    ext = tables.table_extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadError(f"The {kind} file must be .csv, .tsv, .txt or .xlsx (optionally .gz); got '{filename}'.")
    gz = ".gz" if filename.lower().endswith(".gz") else ""
    return f"{kind}{ext}{gz}"


def upload_dir(upload_id: str) -> Path:
    if not upload_id or any(ch not in "0123456789abcdef-" for ch in upload_id):
        raise KeyError(upload_id)
    d = settings.uploads_dir / upload_id
    if not (d / "upload.json").exists():
        raise KeyError(upload_id)
    return d


def _save_stream(src: BinaryIO, dest: Path) -> None:
    limit = settings.max_upload_mb * 1024 * 1024
    written = 0
    with open(dest, "wb") as out:
        while chunk := src.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise UploadError(f"File is larger than {settings.max_upload_mb} MB.")
            out.write(chunk)
    if written == 0:
        raise UploadError(f"The file '{dest.name}' is empty.")


def create(counts: tuple[str, BinaryIO], metadata: tuple[str, BinaryIO], *, source: str = "upload",
           example: str | None = None) -> dict:
    uid = _new_id()
    d = settings.uploads_dir / uid
    d.mkdir(parents=True)
    try:
        counts_name = _safe_name(counts[0], "counts")
        meta_name = _safe_name(metadata[0], "metadata")
        _save_stream(counts[1], d / counts_name)
        _save_stream(metadata[1], d / meta_name)
        record = {
            "id": uid, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source, "example": example,
            "counts_file": counts_name, "metadata_file": meta_name,
            "counts_original_name": Path(counts[0]).name, "metadata_original_name": Path(metadata[0]).name,
        }
        (d / "upload.json").write_text(json.dumps(record, indent=2))
        describe(uid)  # fail early if the sample table cannot be read at all
        return record
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


def create_from_example(name: str) -> dict:
    ex = next((e for e in codebank.examples() if e["name"] == name), None)
    if ex is None:
        raise KeyError(name)
    if not ex["available"]:
        raise UploadError(f"The example files are not there yet. In the codebank folder run: {ex['make_command']}")
    with open(ex["counts_file"], "rb") as c, open(ex["metadata_file"], "rb") as m:
        return create((ex["counts_file"].name, c), (ex["metadata_file"].name, m), source="example", example=name)


def record(upload_id: str) -> dict:
    return json.loads((upload_dir(upload_id) / "upload.json").read_text())


def describe(upload_id: str, condition: str | None = None) -> dict:
    """Upload record, a preview of both files, the metadata columns and a proposed setup."""
    rec = record(upload_id)
    d = upload_dir(upload_id)
    try:
        header, rows = tables.read_table(d / rec["metadata_file"])
    except (ValueError, UnicodeError) as e:
        raise UploadError(f"The sample table could not be read: {e}") from e
    if len(header) < 2 or not rows:
        raise UploadError("The sample table needs a header row, one row per sample, and at least two columns "
                          "(sample ID and the groups being compared).")
    cols, prop = propose(header, rows, condition=condition)
    example_cfg = None
    if rec.get("example"):
        ex = next((e for e in codebank.examples() if e["name"] == rec["example"]), None)
        example_cfg = ex["config"] if ex else None
    return {
        **rec,
        "metadata_preview": {"columns": header, "rows": rows[:200], "n_rows": len(rows)},
        "counts_preview": tables.count_file_preview(d / rec["counts_file"]),
        "columns": [c.as_dict() for c in cols],
        "proposal": prop.as_dict(),
        "example_settings": example_cfg,
    }


def check(upload_id: str, s: AnalysisSettings) -> dict:
    """Run step 01 (input validation) with these settings; fast, so it runs while the user waits."""
    rec = record(upload_id)
    d = upload_dir(upload_id)
    checks_dir = d / "checks"
    checks_dir.mkdir(exist_ok=True)
    cmd = codebank.pipeline_command(
        counts_file=d / rec["counts_file"], metadata_file=d / rec["metadata_file"], runs_dir=checks_dir,
        steps="1", condition=s.condition, reference_level=s.reference_level, test_level=s.test_level,
        covariates=s.covariates, organism=s.organism, run_name="check",
        overrides={k: v for k, v in s.advanced.items() if k in ("exclude_samples", "min_count")})
    before = set(checks_dir.iterdir())
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=settings.codebank_dir)
    except FileNotFoundError as e:
        raise RuntimeError(f"Rscript was not found ({settings.rscript}). Install R or set RSCRIPT.") from e
    new_dirs = sorted(set(checks_dir.iterdir()) - before)
    if not new_dirs:
        raise RuntimeError("The input check did not start: " + (proc.stderr or proc.stdout)[-2000:])
    run_dir = new_dirs[-1]
    report_path = run_dir / "01_validate_inputs" / "validation_report.json"
    if not report_path.exists():
        log = run_dir / "logs" / "01_validate_inputs.log"
        raise RuntimeError("The input check stopped unexpectedly: " +
                           (log.read_text()[-2000:] if log.exists() else (proc.stdout + proc.stderr)[-2000:]))
    report = json.loads(report_path.read_text())
    for c in report.get("checks", []):
        c["group"] = "files" if c["id"] in codebank.FILE_CHECK_IDS else "design"
    _prune(checks_dir)
    result = {"status": report.get("status"), "n_fail": report.get("n_fail"), "n_warning": report.get("n_warning"),
              "summary": report.get("summary"), "checks": report.get("checks", []),
              "settings": s.model_dump()}
    (d / "last_check.json").write_text(json.dumps(result, indent=2))
    return result


# ---- Executor agent ------------------------------------------------------------------------
# Answers are cached per upload (the model is slow on a laptop and should give the
# same answer for the same table), and every call is logged under agent/log/.

def agent_setup(upload_id: str, condition: str | None = None, *, refresh: bool = False) -> dict:
    rec = record(upload_id)
    d = upload_dir(upload_id)
    key = hashlib.sha1((condition or "").encode()).hexdigest()[:10]
    cache = d / "agent" / f"setup_{key}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    header, rows = tables.read_table(d / rec["metadata_file"])
    result = ExecutorAgent().propose_setup(
        header, rows, condition=condition, log_dir=d / "agent" / "log",
        files={"counts": rec["counts_original_name"], "samples": rec["metadata_original_name"]})
    if result["source"] == "model":  # do not cache fallbacks: the model may be started later
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(result, indent=2))
    return result


def agent_explain_check(upload_id: str) -> dict:
    rec = record(upload_id)
    d = upload_dir(upload_id)
    last = d / "last_check.json"
    if not last.exists():
        raise KeyError("No input check has been run yet.")
    chk = json.loads(last.read_text())
    key = hashlib.sha1(json.dumps([(c["id"], c["status"], c["message"]) for c in chk["checks"]]).encode()).hexdigest()[:10]
    cache = d / "agent" / f"explain_{key}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    context = {"counts_file": rec["counts_original_name"], "sample_table": rec["metadata_original_name"],
               "comparison_column": chk["settings"]["condition"]}
    result = ExecutorAgent().explain_checks(chk["checks"], context=context, log_dir=d / "agent" / "log")
    if result["source"] == "model":
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(result, indent=2))
    return result


def _prune(checks_dir: Path) -> None:
    dirs = sorted(p for p in checks_dir.iterdir() if p.is_dir())
    for old in dirs[:-KEEP_CHECKS]:
        shutil.rmtree(old, ignore_errors=True)
