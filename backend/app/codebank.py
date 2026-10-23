"""Everything the app knows about the R code bank comes from its own files.

The app never builds analysis commands by hand beyond the documented
`run_pipeline.R` options, and reads parameter names and allowed values from
`manifest/tools.yaml`, so updating the code bank does not need app changes.
"""

import subprocess
from functools import lru_cache
from pathlib import Path

import yaml

from .config import settings

# Step 01 checks that are about the files themselves; the rest are about the
# experimental design (comparison, covariates). Used to group checks in the UI.
FILE_CHECK_IDS = {
    "files_found", "counts_readable", "gene_id_column", "annotation_columns", "numeric_columns",
    "excluded_samples", "missing_values", "negative_values", "count_values", "ensembl_versions",
    "duplicate_gene_ids", "all_zero_genes", "library_sizes", "metadata_readable",
    "sample_id_column", "duplicate_samples", "sample_matching", "gene_ids_organism",
}


def path(*parts: str) -> Path:
    return settings.codebank_dir.joinpath(*parts)


@lru_cache(maxsize=1)
def manifest() -> dict:
    with open(path("manifest", "tools.yaml")) as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def defaults() -> dict:
    with open(path("config", "default_params.yaml")) as f:
        return yaml.safe_load(f)


def steps() -> list[dict]:
    """[{id, name, description, critical}] in pipeline order."""
    return [
        {"id": s["id"], "name": s["name"], "description": " ".join(str(s.get("description", "")).split()),
         "critical": bool(s.get("critical", True))}
        for s in manifest()["steps"]
    ]


def version_info() -> dict:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(settings.codebank_dir), *args],
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or None if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    return {
        "version": manifest().get("version"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "tag": git("describe", "--tags", "--exact-match"),
        "uncommitted_changes": bool(git("status", "--porcelain", "--untracked-files=no")),
    }


def examples() -> list[dict]:
    """Example datasets from config/examples/*.yaml (the template is skipped)."""
    out = []
    for cfg_path in sorted(path("config", "examples").glob("*.yaml")):
        if cfg_path.stem == "template":
            continue
        text = cfg_path.read_text()
        cfg = yaml.safe_load(text) or {}
        first_comment = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), "")
        title = first_comment.removeprefix("Example run:").strip() or cfg_path.stem
        counts = path(cfg["counts_file"]) if cfg.get("counts_file") else None
        meta = path(cfg["metadata_file"]) if cfg.get("metadata_file") else None
        out.append({
            "name": cfg_path.stem,
            "title": title,
            "config": cfg,
            "counts_file": counts,
            "metadata_file": meta,
            "available": bool(counts and meta and counts.exists() and meta.exists()),
            "make_command": f"Rscript scripts/make_example_data.R {cfg_path.stem}",
        })
    return out


def templates() -> dict[str, Path]:
    return {"counts": path("templates", "counts_template.csv"),
            "metadata": path("templates", "metadata_template.csv")}


def pipeline_command(*, counts_file: Path, metadata_file: Path, runs_dir: Path, steps: str,
                     condition: str | None, reference_level: str | None, test_level: str | None,
                     covariates: list[str], organism: str, run_name: str,
                     overrides: dict | None = None) -> list[str]:
    """Command line for scripts/run_pipeline.R (options from the manifest's entry point)."""
    cmd = [settings.rscript, str(path("scripts", "run_pipeline.R")),
           "--counts_file", str(counts_file), "--metadata_file", str(metadata_file),
           "--runs_dir", str(runs_dir), "--steps", steps, "--run_name", run_name,
           "--organism", organism]
    if condition:
        cmd += ["--condition", condition]
    if reference_level:
        cmd += ["--reference_level", reference_level]
    if test_level:
        cmd += ["--test_level", test_level]
    sets = []
    # covariates go through --set so an empty list can be passed explicitly
    sets.append("covariates=[" + ", ".join(_yaml_scalar(c) for c in covariates) + "]")
    for k, v in (overrides or {}).items():
        sets.append(f"{k}={_yaml_scalar(v)}")
    cmd += ["--set", ";".join(sets)]
    return cmd


def _yaml_scalar(v) -> str:
    """A value written so that run_pipeline.R's YAML parser reads it back unchanged."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_yaml_scalar(x) for x in v) + "]"
    s = str(v)
    if ";" in s:
        # run_pipeline.R splits --set on ';' before reading YAML, so it cannot be quoted
        raise ValueError(f"Names containing ';' are not supported: {s!r}")
    try:
        plain_ok = yaml.safe_load(s) == s  # e.g. "yes", "1", "null" would change type
    except yaml.YAMLError:
        plain_ok = False
    if not plain_ok or any(ch in s for ch in ",[]{}:#'\"") or s.strip() != s:
        return "'" + s.replace("'", "''") + "'"
    return s
