"""The code of an analysis as one .zip, for a paper's code availability / supplementary material.

Everything that produced the results, and nothing that did not:

    README.md                  what is inside, how to rerun it, the methods text
    run_analysis.sh            reruns the analysis: code bank + settings + your input files
    codebank/                  the code bank's code at the exact commit the run used (git archive)
    analysis/run_config.yaml   every parameter of the run (the only thing assistants could change)
    analysis/resolved_params.yaml, analysis/design.txt
    environment/               R version, package versions, the run's provenance record
    data/README.md             input file names and SHA-256 checksums (the files themselves only on request)
    setup/                     how the setup was chosen (assistant / rules / user) and the setup conversation
    custom_analyses/<chat>/    code the chat assistant ran, in order, with reproduce.sh

The statistics are the code bank's: assistants change settings, never methods (guardrails.py),
so run_analysis.sh with this code bank commit and these settings gives the same results.
"""

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

import yaml

from . import codebank, guardrails, results
from .config import settings

# Code-bank files that belong in the bundle (data/ and runs/ never do)
CODEBANK_PATHS = ["scripts", "R", "config", "manifest", "docs", "templates", "tests", "README.md", "CHANGELOG.md",
                  "LICENSE", "Dockerfile"]
RUNTIME_KEYS = {"original_counts_file", "original_metadata_file", "run_dir", "based_on"}


class BundleError(RuntimeError):
    pass


def build(job: dict, job_dir: Path, run_dir: Path, *, include_data: bool = False) -> Path:
    prov = _json(run_dir / "provenance.json") or {}
    cb = prov.get("code_bank") or job.get("codebank") or {}
    params = yaml.safe_load((run_dir / "params.yaml").read_text()) or {}
    out_dir = job_dir / "code"
    out_dir.mkdir(exist_ok=True)
    name = f"code_{run_dir.name}"
    target = out_dir / (name + ("_with_data" if include_data else "") + ".zip")
    tmp = target.with_suffix(".tmp")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        def put(rel: str, data: bytes | str, executable: bool = False) -> None:
            info = zipfile.ZipInfo(f"{name}/{rel}", date_time=datetime.now().timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if executable else 0o644) << 16
            z.writestr(info, data if isinstance(data, bytes) else data.encode())

        code_note = _add_codebank(put, cb)
        inputs = sorted(p for p in (run_dir / "inputs").glob("*") if p.is_file())
        put("analysis/run_config.yaml", _run_config(params, inputs))
        if (run_dir / "01_validate_inputs" / "resolved_params.yaml").exists():
            put("analysis/resolved_params.yaml", (run_dir / "01_validate_inputs" / "resolved_params.yaml").read_bytes())
        put("run_analysis.sh", _run_script(inputs), executable=True)
        put("environment/provenance.json", json.dumps(prov, indent=2))
        put("environment/R_packages.tsv", "package\tversion\n" + "".join(
            f"{k}\t{v}\n" for k, v in sorted((prov.get("packages") or {}).items())))
        put("environment/R_version.txt", f"{prov.get('r_version', 'unknown')}\n{prov.get('platform', '')}\n")
        checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
        put("data/SHA256SUMS", "".join(f"{h}  {n}\n" for n, h in checksums.items()))
        put("data/README.md", _data_readme(job, checksums, include_data))
        if include_data:
            for p in inputs:
                z.write(p, f"{name}/data/{p.name}")
        setup = _setup_record(job)
        put("setup/setup_record.json", json.dumps(setup, indent=2, default=str))
        if setup.get("conversation"):
            put("setup/setup_conversation.md", _setup_markdown(setup["conversation"]))
        customs = _add_custom_analyses(put, job_dir)
        methods = run_dir / "07_report" / "methods.md"
        put("README.md", _readme(job, run_dir, prov, cb, code_note, customs, setup,
                                 methods.read_text() if methods.exists() else "", include_data))
    tmp.replace(target)
    return target


# ---- code bank -------------------------------------------------------------------------------
def _add_codebank(put, cb: dict) -> str:
    """Add codebank/ at the run's commit. Returns a note on how exact the copy is."""
    commit = cb.get("commit")
    if commit:
        try:
            tar = subprocess.run(["git", "-C", str(settings.codebank_dir), "archive", "--format=tar", commit, "--",
                                  *[p for p in CODEBANK_PATHS if _tracked(commit, p)]],
                                 capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            tar = None
        if tar is not None and tar.returncode == 0 and tar.stdout:
            with tarfile.open(fileobj=io.BytesIO(tar.stdout)) as t:
                for m in t.getmembers():
                    if m.isfile():
                        put(f"codebank/{m.name}", t.extractfile(m).read(), executable=bool(m.mode & 0o111))
            note = f"exact copy of code bank commit {commit}"
            if cb.get("uncommitted_changes"):
                note += (" (WARNING: the code bank had uncommitted changes when the analysis ran, so the results may "
                         "not be reproducible from this commit alone)")
            return note
    # no git history here (e.g. a container without .git): copy the files as they are now
    current = codebank.version_info()
    for rel in CODEBANK_PATHS:
        src = settings.codebank_dir / rel
        for f in ([src] if src.is_file() else sorted(src.rglob("*")) if src.is_dir() else []):
            if f.is_file() and "/runs/" not in str(f) and "/output/" not in str(f):
                put(f"codebank/{f.relative_to(settings.codebank_dir)}", f.read_bytes())
    same = commit and current.get("commit") and commit.startswith(current["commit"])
    if same:
        return f"copy of the code bank files at commit {commit} (git history was not available; files taken from disk)"
    return (f"WARNING: commit {commit or 'unknown'} could not be read; these are the code bank files currently installed "
            f"(version {current.get('version')}, commit {current.get('commit') or 'unknown'}). Check them against the "
            "commit recorded in environment/provenance.json.")


def _tracked(commit: str, path: str) -> bool:
    r = subprocess.run(["git", "-C", str(settings.codebank_dir), "cat-file", "-e", f"{commit}:{path}"],
                       capture_output=True, timeout=20)
    return r.returncode == 0


# ---- analysis settings -------------------------------------------------------------------------
def _run_config(params: dict, inputs: list[Path]) -> str:
    cfg = {k: v for k, v in params.items() if k not in RUNTIME_KEYS}
    for key in ("counts_file", "metadata_file"):
        if cfg.get(key):
            cfg[key] = f"data/{Path(str(cfg[key])).name}"
    head = ("# Every parameter of this analysis (code-bank defaults plus the settings chosen for this run).\n"
            "# Assistants could only change values here; the methods are fixed by the code bank.\n")
    return head + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)


def _run_script(inputs: list[Path]) -> str:
    checks = "\n".join(f'[ -f "data/{p.name}" ] || {{ echo "Put the input file data/{p.name} here first (see data/README.md)."; exit 1; }}'
                       for p in inputs)
    return f"""#!/usr/bin/env bash
# Rerun this analysis with the exact code-bank code and settings.
# Needs R (version in environment/R_version.txt) with the packages in environment/R_packages.tsv
# (install them with: Rscript codebank/scripts/00_setup_environment.R).
# Results are written to runs/<date>_<name>/ next to this script.
set -euo pipefail
cd "$(dirname "$0")"
{checks}
if command -v shasum >/dev/null; then (cd data && shasum -a 256 -c SHA256SUMS); fi
Rscript codebank/scripts/run_pipeline.R --config analysis/run_config.yaml --runs_dir runs
echo "Done. Custom analyses (if any): custom_analyses/*/reproduce.sh"
"""


def _data_readme(job: dict, checksums: dict, included: bool) -> str:
    lines = ["# Input data", ""]
    if included:
        lines += ["The input files are included in this folder.", ""]
    else:
        lines += ["The input files are not included (deposit them in a public repository such as GEO and cite the "
                  "accession). To rerun, place them in this folder with these exact names; `run_analysis.sh` checks "
                  "the SHA-256 checksums.", ""]
    lines += ["| File | SHA-256 |", "|---|---|"] + [f"| {n} | `{h}` |" for n, h in checksums.items()]
    lines += ["", f"Dataset: {job.get('dataset') or 'not recorded'}"]
    return "\n".join(lines) + "\n"


# ---- how the setup was chosen ---------------------------------------------------------------------
def _setup_record(job: dict) -> dict:
    rec = {"settings_run": job.get("settings"), "setup_origin": job.get("setup_origin"),
           "methods_policy": guardrails.policy_source()}
    conv_id = (job.get("setup_origin") or {}).get("setup_chat")
    if conv_id and job.get("upload_id"):
        p = settings.uploads_dir / job["upload_id"] / "setup_chat" / f"{conv_id}.json"
        if p.exists():
            rec["conversation"] = json.loads(p.read_text()).get("transcript", [])
    return rec


def _setup_markdown(transcript: list[dict]) -> str:
    out = ["# Setup conversation", "",
           "The open-weight setup assistant proposed the setup below; the user approved it before anything ran.", ""]
    for t in transcript:
        who = {"user": "**User**", "assistant": "**Assistant**"}.get(t.get("role"), f"**{t.get('role')}**")
        out.append(f"{who}: {t.get('text', '')}")
        if t.get("setup"):
            out.append("\n```yaml\n" + yaml.safe_dump(t["setup"], sort_keys=False) + "```")
        out.append("")
    return "\n".join(out)


# ---- the chat assistant's own code ------------------------------------------------------------------
def _add_custom_analyses(put, job_dir: Path) -> list[dict]:
    out = []
    base = job_dir / "chat"
    if not base.exists():
        return out
    for conv in sorted(p for p in base.iterdir() if (p / "transcript.json").exists()):
        cells_dir = conv / "workspace" / "cells"
        cells = sorted(p for p in cells_dir.glob("*") if p.suffix in (".py", ".R")) if cells_dir.exists() else []
        if not cells:
            continue
        transcript = json.loads((conv / "transcript.json").read_text())
        meta = _json(conv / "meta.json") or {}
        ok_cells = [c for c in cells if _cell_ok(c)]
        prefix = f"custom_analyses/{conv.name}"
        for c in cells:
            put(f"{prefix}/cells/{c.name}", c.read_bytes())
            log = c.with_suffix(".log")
            if log.exists():
                put(f"{prefix}/cells/{log.name}", log.read_bytes())
        put(f"{prefix}/reproduce.sh", _reproduce_script(ok_cells), executable=True)
        put(f"{prefix}/conversation.md", _conversation_markdown(meta, transcript))
        blocked = conv / "workspace" / "blocked_code.jsonl"
        if blocked.exists():
            put(f"{prefix}/blocked_by_guardrails.jsonl", blocked.read_bytes())
        outputs = conv / "workspace" / "outputs"
        out.append({"id": conv.name, "title": meta.get("title"), "n_cells": len(cells), "n_ok": len(ok_cells),
                    "outputs": sorted(str(p.relative_to(outputs)) for p in outputs.rglob("*") if p.is_file())
                    if outputs.exists() else []})
    return out


def _cell_ok(cell: Path) -> bool:
    log = cell.with_suffix(".log")
    return log.exists() and log.read_text(errors="replace").startswith("exit 0")


def _reproduce_script(cells: list[Path]) -> str:
    lines = [f'python3 -I "cells/{c.name}"' if c.suffix == ".py" else f'Rscript --vanilla "cells/{c.name}"' for c in cells]
    return f"""#!/usr/bin/env bash
# Rerun the chat assistant's custom analyses, in the order they ran (scripts that failed and were
# then corrected are left out; they are kept in cells/ with their logs).
# Usage: ./reproduce.sh [run folder]   (default: the newest folder in ../../runs from run_analysis.sh)
set -euo pipefail
cd "$(dirname "$0")"
RUN="${{1:-$(ls -d ../../runs/*/ 2>/dev/null | tail -1)}}"
[ -n "$RUN" ] && [ -d "$RUN" ] || {{ echo "Run ../../run_analysis.sh first, or give the run folder."; exit 1; }}
ln -sfn "$(cd "$RUN" && pwd)" run
mkdir -p outputs
{chr(10).join(lines) if lines else "echo 'No successful scripts to rerun.'"}
echo "Outputs are in $(pwd)/outputs"
"""


def _conversation_markdown(meta: dict, transcript: list[dict]) -> str:
    out = [f"# {meta.get('title') or 'Chat'}", "",
           "Questions, answers and the scripts the chat assistant ran (scripts are in cells/).", ""]
    for t in transcript:
        role = t.get("role")
        if role == "user":
            out += [f"**User:** {t.get('text', '')}", ""]
        elif role == "assistant":
            out += [f"**Assistant:** {t.get('text', '')}", ""]
        elif role == "tool" and t.get("code"):
            status = "blocked by the guardrails, not run" if t.get("blocked") else ("ran" if t.get("ok") else "failed")
            out += [f"- `{t.get('script') or '(not saved)'}` ({status}): {t.get('title', '')}" +
                    (f" → {', '.join(t.get('files') or [])}" if t.get("files") else ""), ""]
    return "\n".join(out)


# ---- README ---------------------------------------------------------------------------------------------
def _readme(job, run_dir, prov, cb, code_note, customs, setup, methods_md, include_data) -> str:
    p = results.overview(run_dir)["resolved_params"] or {}
    origin = setup.get("setup_origin") or {}
    who = {"model": f"the open-weight setup assistant ({origin.get('model') or ''})", "rules": "built-in rules",
           "example": "the example's settings",
           "setup_chat": f"the open-weight setup assistant ({origin.get('model') or ''}) in the setup conversation",
           "chat": f"the chat assistant ({origin.get('model') or ''}) as a rerun of an earlier analysis"
           }.get(origin.get("source"), "not recorded")
    changed = "changed by the user before running" if origin.get("changed_by_user") else "approved by the user before running"
    custom_lines = "\n".join(
        f"- `custom_analyses/{c['id']}/`: {c['title'] or 'chat'} ({c['n_ok']} script(s) to rerun"
        + (f"; outputs: {', '.join(c['outputs'][:6])}" if c["outputs"] else "") + ")" for c in customs) or "- none"
    return f"""# Code for RNA-seq analysis {run_dir.name}

Comparison: **{p.get('test_level')} vs {p.get('reference_level')}** (column `{p.get('condition')}`), design `{p.get('design_formula', '')}`.
Generated {datetime.now():%Y-%m-%d} by the RNA-seq analysis site.

## What is in this folder

| Path | Contents |
|---|---|
| `run_analysis.sh` | Reruns the analysis from the input files |
| `codebank/` | The analysis code: {code_note} |
| `analysis/run_config.yaml` | Every parameter of the run |
| `analysis/resolved_params.yaml` | The values the pipeline settled on (reference group, filters, design) |
| `environment/` | R version, package versions and the full provenance record |
| `data/` | Input file names and checksums{' and the files' if include_data else ''} |
| `setup/` | How the setup was chosen and approved |
| `custom_analyses/` | Code the chat assistant ran for this analysis, with `reproduce.sh` |

## How the analysis was set up

The setup was suggested by {who} and {changed}. All statistics were computed by the code bank's tested
scripts (code bank {cb.get('version', '')}, commit `{cb.get('commit') or 'unknown'}`). The assistants
on the site can only change the code bank's parameters (comparison, covariates, cutoffs, samples, gene sets,
plot colours, shapes and labels); they cannot change its methods. Methods policy: {setup.get('methods_policy')}.

## Reproduce

```bash
# 1. put the input files in data/ (names and checksums in data/README.md)
# 2. R {str(prov.get('r_version', '')).replace('R version ', '')} with the packages in environment/R_packages.tsv:
Rscript codebank/scripts/00_setup_environment.R
# 3. rerun
./run_analysis.sh
# 4. custom analyses from the chat (optional)
./custom_analyses/<conversation>/reproduce.sh
```

A container with R and every package: `docker build -t rnaseq-codebank codebank/`.

## Custom analyses from the chat

{custom_lines}

These are labelled in the chat as custom analyses; they use only the code bank's methods (code using any
other method was blocked before it ran; see `blocked_by_guardrails.jsonl` where present).

## Methods

{methods_md.strip() or '(no methods text was written for this run)'}
"""


def _json(p: Path):
    return json.loads(p.read_text()) if p.exists() else None
