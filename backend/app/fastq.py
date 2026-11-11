"""Start from FASTQ files: raw reads already on the server -> the code bank's later/ pipeline -> a normal analysis.

The site never receives FASTQ files through the browser. They are copied into a folder under INCOMING
(rsync / scp today, Globus later); this module
  - lists those folders and pairs the files into samples (R1/R2 from the names),
  - suggests the groups from the sample names (or the setup assistant does, from a description),
  - manages the reference files per species (GENCODE downloads; the indexes are built by the pipeline),
  - writes the sample sheet and upstream.yaml and runs later/scripts/run_upstream.R (one job at a time),
  - reports progress per step, and hands the counts to a normal dataset (uploads.create),
  - gives the rMATS tables and Salmon folders to the run's optional analyses (splicing, isoforms).
All methods stay in the code bank: this file only prepares inputs and starts its scripts.
"""

import csv
import gzip
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import uploads
from .config import settings

FASTQ_RE = re.compile(r"\.(fastq|fq)(\.gz)?$", re.IGNORECASE)
# R1/R2 in the name: Illumina (_S1_L001_R1_001), plain (_R1, .R1, -R1) and bare (_1/_2)
MATE_RES = [re.compile(r"^(?P<s>.+?)(?:_S\d+)?(?:_L\d{3})?[._-]R(?P<m>[12])(?:[._-]0*\d+)?$", re.IGNORECASE),
            re.compile(r"^(?P<s>.+?)[._-](?P<m>[12])$")]
CONTROL_WORDS = re.compile(r"^(wt|wild[ _-]?type|ctrl|control|con|untreated|vehicle|veh|mock|sham|naive|scr|scramble|nt|sictrl)",
                           re.IGNORECASE)
STEPS = [  # (id, label); "gene_counts" makes the counts table for the main pipeline
    ("qc", "QC and trimming (fastp)"), ("read_length", "Read length"), ("star", "Alignment (STAR)"),
    ("salmon", "Quantification (Salmon)"), ("gene_counts", "Gene counts (tximport)"), ("rmats", "Splicing events (rMATS)"),
]
REQUIRED_STEPS = {"salmon", "gene_counts"}   # the counts table needs them
GENCODE = "https://ftp.ebi.ac.uk/pub/databases/gencode"
REFERENCES = {
    "mouse": {"label": "Mouse · GRCm39 · GENCODE M33", "organism": "mouse",
              "files": {"genome_fasta": f"{GENCODE}/Gencode_mouse/release_M33/GRCm39.primary_assembly.genome.fa.gz",
                        "gtf": f"{GENCODE}/Gencode_mouse/release_M33/gencode.vM33.primary_assembly.annotation.gtf.gz",
                        "transcripts_fasta": f"{GENCODE}/Gencode_mouse/release_M33/gencode.vM33.transcripts.fa.gz"}},
    "human": {"label": "Human · GRCh38 · GENCODE 44", "organism": "human",
              "files": {"genome_fasta": f"{GENCODE}/Gencode_human/release_44/GRCh38.primary_assembly.genome.fa.gz",
                        "gtf": f"{GENCODE}/Gencode_human/release_44/gencode.v44.primary_assembly.annotation.gtf.gz",
                        "transcripts_fasta": f"{GENCODE}/Gencode_human/release_44/gencode.v44.transcripts.fa.gz"}},
}
FINAL = {"success", "failed", "cancelled", "interrupted"}


class FastqError(ValueError):
    """A problem with the user's files or choices, in plain language."""


def incoming_dir() -> Path:
    return Path(os.environ.get("FASTQ_INCOMING_DIR", settings.data_dir / "incoming")).resolve()


def refs_dir() -> Path:
    return Path(os.environ.get("REFS_DIR", settings.data_dir / "refs")).resolve()


def jobs_root() -> Path:
    return settings.data_dir / "fastq"


def max_threads() -> int:
    return int(os.environ.get("FASTQ_THREADS") or max(1, min(32, (os.cpu_count() or 2) - 2)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- Folders and samples ---------------------------------------------------------------------------
def _folder(name: str) -> Path:
    root = incoming_dir()
    p = (root / name).resolve()
    if not name or p == root or root not in p.parents or not p.is_dir():
        raise FastqError(f"No folder '{name}' in {root}.")
    return p


def _fastqs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and FASTQ_RE.search(p.name))


def list_folders() -> dict:
    root = incoming_dir()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                files = _fastqs(d)
                out.append({"name": d.name, "n_files": len(files), "gb": round(sum(f.stat().st_size for f in files) / 1e9, 2),
                            "modified": datetime.fromtimestamp(d.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return {"incoming": str(root), "folders": out}


def _split(name: str) -> tuple[str, str | None]:
    stem = FASTQ_RE.sub("", name)
    for rx in MATE_RES:
        m = rx.match(stem)
        if m:
            return m.group("s"), m.group("m")
    return stem, None


def pair_files(folder_name: str) -> dict:
    """Samples from the file names: {sample, r1, r2, gb}; problems that stop a run; layout."""
    folder = _folder(folder_name)
    files = _fastqs(folder)
    if not files:
        raise FastqError(f"No FASTQ files (.fastq.gz, .fq.gz, .fastq, .fq) in {folder}.")
    by: dict[str, dict] = {}
    problems = []
    for f in files:
        rel = f.relative_to(folder).as_posix()
        s, m = _split(f.name)
        e = by.setdefault(s, {"sample": s, "r1": [], "r2": [], "bytes": 0})
        e["r2" if m == "2" else "r1"].append(rel)
        e["bytes"] += f.stat().st_size
    samples = []
    for s, e in sorted(by.items()):
        if len(e["r1"]) != 1 or len(e["r2"]) > 1:
            problems.append(f"{s}: {len(e['r1'])} R1 and {len(e['r2'])} R2 files. Each sample needs one file per read "
                            "(merge the lanes first, e.g. cat S_L001_R1.fastq.gz S_L002_R1.fastq.gz > S_R1.fastq.gz).")
        samples.append({"sample": s, "r1": e["r1"][0] if e["r1"] else None, "r2": e["r2"][0] if e["r2"] else None,
                        "gb": round(e["bytes"] / 1e9, 2)})
    paired = [x for x in samples if x["r2"]]
    if paired and len(paired) != len(samples):
        problems.append("Some samples have R2 files and others do not: " +
                        ", ".join(x["sample"] for x in samples if not x["r2"]) + " have only R1.")
    layout = "paired-end" if paired and len(paired) == len(samples) else ("single-end" if not paired else "mixed")
    if len(samples) < 4:
        problems.append(f"Only {len(samples)} samples: a comparison needs at least 2 per group.")
    return {"folder": folder_name, "path": str(folder), "samples": samples, "layout": layout, "problems": problems,
            "gb": round(sum(x["gb"] for x in samples), 2), "suggestion": suggest_groups([x["sample"] for x in samples])}


def suggest_groups(names: list[str]) -> dict:
    """Rule-based groups from sample names: the name without its replicate number (WT_1, WT_2 -> WT)."""
    groups = {}
    for n in names:
        g = re.sub(r"(?:[._ -]?(?:rep|r|replicate|n)?\d+)$", "", n, flags=re.IGNORECASE) or n
        groups[n] = g
    levels = sorted(set(groups.values()))
    control = next((g for g in levels if CONTROL_WORDS.match(g)), levels[0] if levels else None)
    return {"groups": groups, "control": control, "origin": "rules"}


ASSIST_SCHEMA = {
    "type": "object",
    "properties": {"groups": {"type": "object", "additionalProperties": {"type": "string"}},
                   "control": {"type": "string"},
                   "batch": {"type": "object", "additionalProperties": {"type": "string"}},
                   "species": {"type": "string", "enum": ["mouse", "human", "unknown"]},
                   "note": {"type": "string"}},
    "required": ["groups", "control", "species", "note"],
}
ASSIST_PROMPT = """You set up the sample groups of an RNA-seq experiment from FASTQ sample names and the researcher's
description. Answer in JSON. Rules:
- "groups": every sample name -> its experimental group (short names like WT, KO, RBP1_KO). Use the description and the names.
- "control": the reference group (wild type, untreated, vehicle, control...).
- "batch": sample -> batch, ONLY if the description says which samples were in which batch; otherwise {}.
- "species": mouse or human if the description or names say so, else unknown.
- "note": one or two plain sentences: what you grouped and anything the researcher should confirm.
You only set up groups; you do not choose methods."""


def assist(folder_name: str, description: str, llm=None) -> dict:
    """The setup assistant's grouping (open-weight model); falls back to the rules. Checked before use."""
    paired = pair_files(folder_name)
    names = [s["sample"] for s in paired["samples"]]
    rules = paired["suggestion"]
    if llm is None:
        from .llm.factory import executor_llm
        llm = executor_llm()
    try:
        a = llm.chat_json(ASSIST_PROMPT, json.dumps({"samples": names, "description": description}), ASSIST_SCHEMA)
    except Exception as e:  # noqa: BLE001 - no model: the rules answer
        return {**rules, "note": f"The assistant is not available ({e}); the groups come from the sample names.",
                "species": None, "batch": {}}
    groups = {n: str((a.get("groups") or {}).get(n) or "").strip() for n in names}
    if not all(groups.values()) or a.get("control") not in set(groups.values()) or len(set(groups.values())) < 2:
        return {**rules, "batch": {}, "species": None,
                "note": "The assistant's grouping did not cover every sample; the groups come from the sample names."}
    batch = {n: str(v) for n, v in (a.get("batch") or {}).items() if n in groups and str(v).strip()}
    return {"groups": groups, "control": a["control"], "batch": batch,
            "species": a.get("species") if a.get("species") in REFERENCES else None,
            "note": a.get("note") or "", "origin": getattr(llm, "name", "assistant")}


# ---- References ------------------------------------------------------------------------------------
def _ref_paths(species: str) -> dict:
    d = refs_dir() / species
    files = {k: d / Path(url).name[:-3] for k, url in REFERENCES[species]["files"].items()}   # without .gz
    return {**files, "star_index": d / "star_index", "salmon_index": d / "salmon_index", "dir": d}


def reference_status(species: str) -> dict:
    if species not in REFERENCES:
        raise FastqError(f"Unknown species '{species}' (known: {', '.join(REFERENCES)}).")
    p = _ref_paths(species)
    have = {k: p[k].is_file() for k in REFERENCES[species]["files"]}
    st = {}
    sf = p["dir"] / "download.json"
    if sf.exists():
        st = json.loads(sf.read_text())
    return {"species": species, "label": REFERENCES[species]["label"], "files_ready": all(have.values()), "files": have,
            "star_index": (p["star_index"] / "SA").exists(), "salmon_index": (p["salmon_index"] / "info.json").exists(),
            "download": st, "dir": str(p["dir"])}


def references() -> list[dict]:
    return [reference_status(s) for s in REFERENCES]


_dl_lock = threading.Lock()


def download_reference(species: str) -> dict:
    """Download and unpack the GENCODE files in the background (once per species)."""
    st = reference_status(species)
    if st["files_ready"]:
        return st
    with _dl_lock:
        if (st.get("download") or {}).get("status") == "running":
            return st
        p = _ref_paths(species)
        p["dir"].mkdir(parents=True, exist_ok=True)
        (p["dir"] / "download.json").write_text(json.dumps({"status": "running", "started": _now(), "file": None}))
        threading.Thread(target=_download, args=(species,), daemon=True, name=f"refs-{species}").start()
    return reference_status(species)


def _download(species: str) -> None:
    p = _ref_paths(species)
    sf = p["dir"] / "download.json"
    try:
        for key, url in REFERENCES[species]["files"].items():
            dest = p[key]
            if dest.is_file():
                continue
            gz = dest.with_name(dest.name + ".gz.part")
            sf.write_text(json.dumps({"status": "running", "file": Path(url).name, "started": _now()}))
            with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
                r.raise_for_status()
                with open(gz, "wb") as f:
                    for chunk in r.iter_bytes(4 << 20):
                        f.write(chunk)
            tmp = dest.with_name(dest.name + ".part")
            with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, 16 << 20)
            tmp.replace(dest)
            gz.unlink(missing_ok=True)
        sf.write_text(json.dumps({"status": "done", "finished": _now()}))
    except Exception as e:  # noqa: BLE001 - shown on the page
        sf.write_text(json.dumps({"status": "failed", "error": f"{type(e).__name__}: {e}", "finished": _now()}))


# ---- Jobs ------------------------------------------------------------------------------------------
def _job_dir(job_id: str) -> Path:
    if not job_id or any(ch not in "0123456789abcdef-" for ch in job_id):
        raise KeyError(job_id)
    d = jobs_root() / job_id
    if not (d / "job.json").exists():
        raise KeyError(job_id)
    return d


def upstream_command(config: Path) -> list[str]:
    return [settings.rscript, str(settings.codebank_dir / "later" / "scripts" / "run_upstream.R"), "--config", str(config)]


class FastqManager:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fastq")
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def load(self, job_id: str) -> dict:
        return json.loads((_job_dir(job_id) / "job.json").read_text())

    def _save(self, job: dict) -> None:
        d = jobs_root() / job["id"]
        (d / "job.json.tmp").write_text(json.dumps(job, indent=2))
        (d / "job.json.tmp").replace(d / "job.json")

    def _update(self, job_id: str, **fields) -> dict:
        with self._lock:
            job = self.load(job_id)
            job.update(fields)
            self._save(job)
            return job

    def all(self) -> list[dict]:
        root = jobs_root()
        if not root.exists():
            return []
        return [json.loads((d / "job.json").read_text()) for d in sorted(root.iterdir(), reverse=True) if (d / "job.json").exists()]

    def recover(self) -> None:
        for j in self.all():
            if j["status"] not in FINAL:
                self._update(j["id"], status="interrupted", finished=_now(),
                             error="The site was stopped while this job was running. Start it again (finished steps are redone).")

    # -- checks and start --
    def check(self, spec: dict) -> dict:
        """Everything that can be known before spending hours of compute: pass / note / fail lines."""
        paired = pair_files(spec.get("folder") or "")
        samples = [s["sample"] for s in paired["samples"]]
        groups = {s: str((spec.get("groups") or {}).get(s) or "").strip() for s in samples}
        batch = {s: str((spec.get("batch") or {}).get(s) or "").strip() for s in samples}
        control = spec.get("control")
        species = spec.get("species")
        steps = [s for s, _ in STEPS if s in (spec.get("steps") or [x for x, _ in STEPS])]
        lines = []

        def add(status, text):
            lines.append({"status": status, "text": text})
        for p in paired["problems"]:
            add("fail", p)
        if not paired["problems"]:
            add("pass", f"{len(samples)} samples, {paired['layout']}, every sample has its files")
        missing = [s for s, g in groups.items() if not g]
        if missing:
            add("fail", "No group for: " + ", ".join(missing))
        levels = sorted(set(g for g in groups.values() if g))
        counts = {g: sum(1 for v in groups.values() if v == g) for g in levels}
        if len(levels) < 2:
            add("fail", "At least two groups are needed.")
        elif control not in levels:
            add("fail", f"The control group must be one of: {', '.join(levels)}.")
        else:
            small = [g for g, n in counts.items() if n < 2]
            if small:
                add("fail", "Groups need at least 2 samples (rMATS and DESeq2 estimate variability): " + ", ".join(small))
            else:
                add("pass", "groups: " + ", ".join(f"{g} ({n})" for g, n in counts.items()) + f"; control {control}")
        if any(batch.values()):
            if not all(batch.values()):
                add("fail", "Batch is filled in for some samples only.")
            else:
                confounded = all(len({batch[s] for s in samples if groups[s] == g}) == 1 for g in levels) and \
                    len({batch[s] for s in samples}) == len(levels)
                add("fail" if confounded else "pass",
                    "batch lines up exactly with the groups: their effects cannot be separated" if confounded
                    else "batch is not confounded with the groups (it becomes a covariate in the analysis)")
        bad = REQUIRED_STEPS - set(steps)
        if bad:
            add("fail", "Salmon and gene counts are needed for the counts table.")
        if "rmats" in steps and "star" not in steps:
            add("fail", "rMATS needs the STAR alignment.")
        if species not in REFERENCES:
            add("fail", "Choose the species (reference).")
        else:
            r = reference_status(species)
            if r["files_ready"]:
                add("pass", f"reference {r['label']} on the server" +
                    ("" if r["star_index"] or "star" not in steps else "; the STAR index is built first (about 1 hour, 32 GB memory)"))
            elif (r["download"] or {}).get("status") == "running":
                add("fail", f"The reference is still downloading ({(r['download'] or {}).get('file')}).")
            else:
                add("fail", "The reference files are not on the server yet: download them first.")
        need_gb = round(paired["gb"] * (2.5 if "star" in steps else 1.3) + 5, 0)
        free_gb = round(shutil.disk_usage(settings.data_dir if settings.data_dir.exists() else Path("/")).free / 1e9, 0)
        add("fail" if free_gb < need_gb else "note", f"needs about {need_gb:.0f} GB of free space; {free_gb:.0f} GB available")
        threads = int(spec.get("threads") or max_threads())
        hours = max(0.5, paired["gb"] / max(1, threads) * 1.2)
        return {"ok": not any(l["status"] == "fail" for l in lines), "lines": lines, "steps": steps, "threads": threads,
                "estimate_hours": round(hours, 1), "need_gb": need_gb, "free_gb": free_gb}

    def start(self, spec: dict) -> dict:
        chk = self.check(spec)
        if not chk["ok"]:
            raise FastqError("; ".join(l["text"] for l in chk["lines"] if l["status"] == "fail"))
        paired = pair_files(spec["folder"])
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        d = jobs_root() / job_id
        d.mkdir(parents=True)
        groups, batch = spec["groups"], spec.get("batch") or {}
        has_batch = any(str(batch.get(s["sample"]) or "").strip() for s in paired["samples"])
        with open(d / "samples.tsv", "w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["file_name", "sample", "condition", "read_orientation"] + (["batch"] if has_batch else []))
            for s in paired["samples"]:
                for orient, fn in (("R1", s["r1"]), ("R2", s["r2"])):
                    if fn:
                        w.writerow([fn, s["sample"], groups[s["sample"]], orient if s["r2"] else "SE"] +
                                   ([str(batch[s["sample"]]).strip()] if has_batch else []))
        ref = _ref_paths(spec["species"])
        cfg = {"sample_sheet": str(d / "samples.tsv"), "fastq_dir": paired["path"], "out_dir": str(d / "results"),
               "condition_column": "condition", "control": spec["control"],
               "genome_fasta": str(ref["genome_fasta"]), "gtf": str(ref["gtf"]), "transcripts_fasta": str(ref["transcripts_fasta"]),
               "star_index": str(ref["star_index"]), "salmon_index": str(ref["salmon_index"]),
               "threads": chk["threads"], "steps": chk["steps"]}
        import yaml
        (d / "upstream.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        job = {"id": job_id, "status": "queued", "created": _now(), "started": None, "finished": None, "error": None,
               "folder": spec["folder"], "species": spec["species"], "organism": REFERENCES[spec["species"]]["organism"],
               "control": spec["control"], "groups": groups, "batch": batch if has_batch else {}, "steps": chk["steps"],
               "threads": chk["threads"], "layout": paired["layout"], "n_samples": len(paired["samples"]), "gb": paired["gb"],
               "description": spec.get("description") or "", "setup_origin": spec.get("setup_origin"),
               "upload_id": None, "codebank": _codebank_info()}
        self._save(job)
        self._pool.submit(self._run, job_id)
        return job

    def _run(self, job_id: str) -> None:
        d = jobs_root() / job_id
        if self.load(job_id)["status"] == "cancelled":
            return
        self._update(job_id, status="running", started=_now())
        try:
            with open(d / "console.log", "w") as log:
                proc = subprocess.Popen(upstream_command(d / "upstream.yaml"), stdout=log, stderr=subprocess.STDOUT,
                                        cwd=settings.codebank_dir, start_new_session=True)
                self._procs[job_id] = proc
                code = proc.wait()
        except FileNotFoundError:
            self._update(job_id, status="failed", finished=_now(), error=f"Rscript was not found ({settings.rscript}).")
            return
        finally:
            self._procs.pop(job_id, None)
        if self.load(job_id)["status"] == "cancelled":
            self._update(job_id, finished=_now())
            return
        ok = code == 0 and (d / "results" / "counts" / "counts.csv").exists()
        self._update(job_id, status="success" if ok else "failed", finished=_now(), exit_code=code,
                     error=None if ok else _tail_error(d / "console.log"))

    def cancel(self, job_id: str) -> dict:
        job = self.load(job_id)
        if job["status"] in FINAL:
            return job
        proc = self._procs.get(job_id)
        self._update(job_id, status="cancelled", finished=_now(), error="Cancelled.")
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return self.load(job_id)

    # -- progress and results --
    def overview(self, job_id: str) -> dict:
        d = _job_dir(job_id)
        job = self.load(job_id)
        res = d / "results"
        summ = json.loads((res / "upstream_summary.json").read_text()) if (res / "upstream_summary.json").exists() else {}
        done = summ.get("steps") or {}
        current = None
        log = d / "console.log"
        if log.exists():
            for line in log.read_text(errors="replace").splitlines():
                m = re.search(r"== (\w+) ==", line)
                if m:
                    current = m.group(1)
        steps = []
        for sid, label in STEPS:
            if sid not in job["steps"]:
                continue
            st = (done.get(sid) or {}).get("status")
            status = st or ("running" if job["status"] == "running" and current == sid else
                            "skipped" if job["status"] == "success" else
                            ("stopped" if job["status"] in FINAL else "waiting"))
            steps.append({"id": sid, "label": label, "status": status, "seconds": (done.get(sid) or {}).get("seconds"),
                          "facts": _facts(res, sid) if st == "success" else []})
        if current == "salmon_index" and job["status"] == "running":
            for s in steps:
                if s["id"] == "salmon":
                    s["status"], s["facts"] = "running", ["building the Salmon index first (once per species)"]
        n_done = sum(1 for s in steps if s["status"] == "success")
        files = [{"label": lab, "path": rel} for lab, rel in (
            ("QC reports (fastp)", "trimmed/reports"), ("QC summary", "trimmed/qc_summary.csv"),
            ("Alignment summary", "star/alignment_summary.csv"), ("Salmon summary", "salmon/salmon_run_summary.csv"),
            ("Counts table", "counts/counts.csv"), ("Sample table", "counts/samples.csv")) if (res / rel).exists()]
        return {**job, "progress": 100 if job["status"] == "success" else round(100 * n_done / max(1, len(steps))), "step_list": steps, "current": current,
                "files": files, "comparisons": sorted(p.name for p in (res / "rmats").iterdir() if p.is_dir())
                if (res / "rmats").is_dir() else [], "log_tail": _tail(log, 25)}

    def file(self, job_id: str, rel: str) -> Path:
        res = (_job_dir(job_id) / "results").resolve()
        p = (res / rel).resolve()
        if res not in p.parents or not p.exists():
            raise KeyError(rel)
        return p

    def to_dataset(self, job_id: str) -> dict:
        """The counts and sample table become a normal dataset (setup, checks, analysis, report)."""
        job = self.load(job_id)
        if job["status"] != "success":
            raise FastqError("The FASTQ processing has not finished successfully.")
        if job.get("upload_id"):
            try:
                uploads.upload_dir(job["upload_id"])
                return uploads.record(job["upload_id"])
            except KeyError:
                pass
        res = _job_dir(job_id) / "results" / "counts"
        with open(res / "counts.csv", "rb") as c, open(res / "samples.csv", "rb") as m:
            rec = uploads.create(("counts.csv", c), ("samples.csv", m), source="fastq",
                                 fastq={"job_id": job_id, "folder": job["folder"], "species": job["species"],
                                        "organism": job["organism"], "control": job["control"]})
        self._update(job_id, upload_id=rec["id"])
        return rec


def _codebank_info() -> dict:
    try:
        from . import codebank
        return codebank.version_info()
    except Exception:  # noqa: BLE001
        return {}


def _tail(p: Path, n: int) -> list[str]:
    if not p.exists():
        return []
    return p.read_text(errors="replace").splitlines()[-n:]


def _tail_error(p: Path) -> str:
    lines = [l for l in _tail(p, 40) if "ERROR" in l or "failed" in l.lower() or "Step '" in l]
    return (lines[-1] if lines else "The FASTQ processing failed; see the log below.").strip()[:500]


def _read_csv(p: Path) -> list[dict]:
    try:
        with open(p, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _num(rows: list[dict], *cols: str) -> list[float]:
    out = []
    for r in rows:
        for c in cols:
            if c in r:
                try:
                    out.append(float(str(r[c]).rstrip("%")))
                except ValueError:
                    pass
                break
    return out


def _range(xs: list[float]) -> str:
    if not xs:
        return ""
    lo, hi = min(xs), max(xs)
    return f"{lo:.0f}%" if round(lo) == round(hi) else f"{lo:.0f}–{hi:.0f}%"


def _facts(res: Path, step: str) -> list[str]:
    """A line or two per finished step, from the scripts' own summaries (best effort)."""
    try:
        if step == "qc":
            rows = _read_csv(res / "trimmed" / "qc_summary.csv")
            r = _range(_num(rows, "percent_kept"))
            flags = sum(1 for x in rows if str(x.get("flags") or "").strip() not in ("", "NA"))
            return [f"{len(rows)} samples" + (f" · {r} of reads kept" if r else "") + (f" · {flags} flagged" if flags else "")]
        if step == "star":
            rows = _read_csv(res / "star" / "alignment_summary.csv")
            r = _range(_num(rows, "unique_percent"))
            strand = (res / "star" / "strandedness.txt").read_text().strip() if (res / "star" / "strandedness.txt").exists() else ""
            return [x for x in ((f"uniquely mapped {r}" if r else ""), (f"library: {strand}" if strand else "")) if x]
        if step == "salmon":
            rows = _read_csv(res / "salmon" / "salmon_run_summary.csv")
            r = _range(_num(rows, "mapping_rate"))
            return [f"mapping rate {r}"] if r else []
        if step == "gene_counts":
            p = res / "counts" / "counts.csv"
            with open(p) as f:
                n = sum(1 for _ in f) - 1
            return [f"{n:,} genes"]
        if step == "read_length":
            p = res / "read_length" / "read_length.txt"
            return [f"{p.read_text().strip()} bp"] if p.exists() else []
        if step == "rmats":
            return [", ".join(sorted(p.name for p in (res / "rmats").iterdir() if p.is_dir()))]
    except (OSError, ValueError, StopIteration):
        return []
    return []


# ---- The run's optional analyses from the FASTQ results ----------------------------------------------
def source_for_run(job: dict) -> dict | None:
    """The FASTQ job a run's dataset came from, if any."""
    try:
        rec = uploads.record(job["upload_id"])
    except (KeyError, FileNotFoundError):
        return None
    fq = rec.get("fastq")
    if not fq:
        return None
    try:
        _job_dir(fq["job_id"])
    except KeyError:
        return None
    return fq


def extras_inputs(fq_job_id: str, module: str, run_job: dict) -> dict:
    """Server-side files for an optional analysis, in the shape the upload handlers take: {input name: [(name, fh)]}."""
    res = _job_dir(fq_job_id) / "results"
    s = run_job["settings"]
    if module == "splicing":
        cmp = res / "rmats" / f"{s['reference_level']}_vs_{s['test_level']}" / "tables"
        if not cmp.is_dir():
            have = ", ".join(p.name for p in (res / "rmats").iterdir()) if (res / "rmats").is_dir() else "none"
            raise FastqError(f"rMATS compared each group with the control; there are no results for "
                             f"{s['reference_level']} vs {s['test_level']} (available: {have}).")
        files = sorted(p for p in cmp.iterdir() if re.match(r"^(SE|MXE|A3SS|A5SS|RI)\.MATS\.(JC|JCEC)\.txt$", p.name))
        if not files:
            raise FastqError(f"No rMATS tables in {cmp}.")
        return {"rmats_tables": [(p.name, open(p, "rb")) for p in files]}
    if module == "isoforms":
        sal = res / "salmon"
        quants = sorted(sal.glob("*/quant.sf"))
        if not quants:
            raise FastqError("No Salmon results in the FASTQ job.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for q in quants:
                z.write(q, f"{q.parent.name}/quant.sf")
        buf.seek(0)
        return {"quant_zip": [("salmon.zip", buf)]}
    raise FastqError(f"The FASTQ results do not feed the '{module}' analysis.")


def extras_available(fq_job_id: str, run_job: dict) -> dict:
    out = {}
    for m in ("splicing", "isoforms"):
        try:
            ins = extras_inputs(fq_job_id, m, run_job)
            for lst in ins.values():
                for _, fh in lst:
                    fh.close()
            out[m] = {"available": True}
        except (FastqError, KeyError) as e:
            out[m] = {"available": False, "why": str(e)}
    return out


fastq_jobs = FastqManager()
