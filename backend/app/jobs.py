"""Analysis jobs: one full code-bank run each, executed in the background.

A job folder holds job.json (state), console.log (run_pipeline.R output) and
runs/<code-bank run folder>/ with every result. Jobs run one at a time, which
is what a laptop can handle; on a server this becomes a proper queue.
"""

import json
import os
import re
import secrets
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import codebank, uploads
from .agents.executor import ExecutorAgent
from .config import settings
from .params import AnalysisSettings

STEP_START = re.compile(r"Step (\d+)/(\d+): (\S+) \.\.\.")
STEP_DONE = re.compile(r"\bdone in ([0-9.]+) s")
STEP_FAILED = re.compile(r"\bFAILED \(exit code")
FINAL_STATES = {"success", "success_with_errors", "failed", "cancelled", "interrupted"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobManager:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # ---- storage -------------------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        if not job_id or any(ch not in "0123456789abcdef-" for ch in job_id):
            raise KeyError(job_id)
        d = settings.jobs_dir / job_id
        if not (d / "job.json").exists():
            raise KeyError(job_id)
        return d

    def load(self, job_id: str) -> dict:
        return json.loads((self.job_dir(job_id) / "job.json").read_text())

    def _save(self, job: dict) -> None:
        d = settings.jobs_dir / job["id"]
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=2))
        tmp.replace(d / "job.json")

    def _update(self, job_id: str, **fields) -> dict:
        with self._lock:
            job = self.load(job_id)
            job.update(fields)
            self._save(job)
            return job

    def all_jobs(self) -> list[dict]:
        if not settings.jobs_dir.exists():
            return []
        out = []
        for d in sorted(settings.jobs_dir.iterdir(), reverse=True):
            if (d / "job.json").exists():
                out.append(json.loads((d / "job.json").read_text()))
        return out

    def run_dir(self, job_id: str) -> Path | None:
        job = self.load(job_id)
        return Path(job["run_dir"]) if job.get("run_dir") else None

    # ---- lifecycle ------------------------------------------------------------------
    def recover(self) -> None:
        """Jobs left queued/running by a previous server process can no longer finish."""
        for job in self.all_jobs():
            if job["status"] not in FINAL_STATES:
                self._update(job["id"], status="interrupted", finished=_now(),
                             error="The app was stopped while this analysis was running. Start it again.")

    def start(self, upload_id: str, s: AnalysisSettings, setup_origin: dict | None = None) -> dict:
        rec = uploads.record(upload_id)
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        d = settings.jobs_dir / job_id
        (d / "runs").mkdir(parents=True)
        up = uploads.upload_dir(upload_id)
        cmd = codebank.pipeline_command(
            counts_file=up / rec["counts_file"], metadata_file=up / rec["metadata_file"], runs_dir=d / "runs",
            steps="1-7", condition=s.condition, reference_level=s.reference_level, test_level=s.test_level,
            covariates=s.covariates, organism=s.organism, run_name=s.run_name, overrides=s.advanced)
        job = {
            "id": job_id, "upload_id": upload_id, "status": "queued", "created": _now(),
            "started": None, "finished": None, "settings": s.model_dump(),
            "dataset": rec.get("example") or rec.get("counts_original_name"),
            "command": cmd, "codebank": codebank.version_info(), "run_dir": None, "error": None,
            # who proposed the settings (model / rules) and whether the user changed them
            "setup_origin": setup_origin,
        }
        self._save(job)
        self._pool.submit(self._run, job_id, cmd)
        return job

    def _run(self, job_id: str, cmd: list[str]) -> None:
        d = settings.jobs_dir / job_id
        if self.load(job_id)["status"] == "cancelled":
            return
        self._update(job_id, status="running", started=_now())
        try:
            with open(d / "console.log", "w") as log:
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=settings.codebank_dir,
                                        start_new_session=True)
                self._procs[job_id] = proc
                code = proc.wait()
        except FileNotFoundError:
            self._update(job_id, status="failed", finished=_now(),
                         error=f"Rscript was not found ({settings.rscript}). Install R or set RSCRIPT.")
            return
        finally:
            self._procs.pop(job_id, None)
        runs = sorted(p for p in (d / "runs").iterdir() if p.is_dir())
        run_dir = runs[-1] if runs else None
        if self.load(job_id)["status"] == "cancelled":
            self._update(job_id, finished=_now(), run_dir=str(run_dir) if run_dir else None)
            return
        status, error = "failed", None
        summary_path = run_dir / "run_summary.json" if run_dir else None
        if summary_path and summary_path.exists():
            status = json.loads(summary_path.read_text()).get("status", "failed")
        if code != 0 or status == "failed":
            status = "failed"
            error = self._failure_message(d, run_dir)
        self._update(job_id, status=status, finished=_now(), exit_code=code,
                     run_dir=str(run_dir) if run_dir else None, error=error)

    def _failure_message(self, job_dir: Path, run_dir: Path | None) -> str:
        """The most useful plain-language reason for a failed run."""
        if run_dir:
            report = run_dir / "01_validate_inputs" / "validation_report.json"
            if report.exists():
                rep = json.loads(report.read_text())
                fails = [c["message"] for c in rep.get("checks", []) if c["status"] == "fail"]
                if fails:
                    return "The input check failed: " + " ".join(fails)
            logs = sorted((run_dir / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime)
            if logs:
                errors = [ln for ln in logs[-1].read_text().splitlines() if "ERROR" in ln or "Error" in ln]
                if errors:
                    return f"Step {logs[-1].stem} failed: " + " ".join(errors[-3:])
        console = job_dir / "console.log"
        tail = console.read_text().splitlines()[-5:] if console.exists() else []
        return "The analysis failed. " + " ".join(tail)

    def cancel(self, job_id: str) -> dict:
        job = self.load(job_id)
        if job["status"] in FINAL_STATES:
            return job
        job = self._update(job_id, status="cancelled", error="Stopped by the user.")
        proc = self._procs.get(job_id)
        if proc and proc.poll() is None:
            # run_pipeline.R starts each step as a child process; stop the whole group
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
        return job

    # ---- executor agent ---------------------------------------------------------------
    def agent_warnings(self, job_id: str) -> dict:
        """Plain-language reading of a finished run's warnings (cached in the job folder)."""
        d = self.job_dir(job_id)
        cache = d / "agent" / "run_warnings.json"
        if cache.exists():
            return json.loads(cache.read_text())
        run_dir = self.run_dir(job_id)
        summary_path = run_dir / "run_summary.json" if run_dir else None
        if not summary_path or not summary_path.exists():
            raise KeyError("This run has no results yet.")
        result = ExecutorAgent().explain_run_warnings(json.loads(summary_path.read_text()), log_dir=d / "agent" / "log")
        if result["source"] == "model":
            cache.parent.mkdir(exist_ok=True)
            cache.write_text(json.dumps(result, indent=2))
        return result

    # ---- progress -------------------------------------------------------------------
    def progress(self, job_id: str) -> list[dict]:
        """Per-step status, read from run_pipeline.R's console output."""
        job = self.load(job_id)
        console = self.job_dir(job_id) / "console.log"
        steps = {s["name"]: {**s, "status": "pending", "seconds": None} for s in codebank.steps()}
        current = None
        if console.exists():
            for line in console.read_text(errors="replace").splitlines():
                if m := STEP_START.search(line):
                    current = m.group(3)
                    if current in steps:
                        steps[current]["status"] = "running"
                elif current and (m := STEP_DONE.search(line)):
                    steps[current].update(status="success", seconds=float(m.group(1)))
                    current = None
                elif current and STEP_FAILED.search(line):
                    steps[current]["status"] = "failed"
                    current = None
        if job["status"] in FINAL_STATES:
            for s in steps.values():
                if s["status"] == "running":
                    s["status"] = "failed" if job["status"] != "cancelled" else "cancelled"
                elif s["status"] == "pending":
                    s["status"] = "skipped"
        return list(steps.values())


jobs = JobManager()
