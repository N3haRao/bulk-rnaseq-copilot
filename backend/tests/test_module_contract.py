"""The site follows the code bank's manifest: every module is offered as the manifest describes it.

- the installed code bank's manifest passes check_manifest() (nothing the site cannot show);
- a broken manifest is reported field by field (what the release agent and a bioinformatician read);
- a NEW module, added only to a code bank (manifest + R script), is offered, run and shown with no site code.
"""

import json
import subprocess
import time
import zipfile
import io

import pytest
import yaml
from fastapi.testclient import TestClient

from app import codebank, extras
from app.config import settings

from test_chat import make_run


def test_installed_codebank_matches_the_site():
    if "modules" not in codebank.manifest():
        pytest.skip("code bank without modules")
    problems = extras.check_manifest()
    assert problems == [], "\n".join(problems)


def test_problems_are_named():
    m = {"modules": {
        "a": {"entry_point": "a.R", "parameters": {"x": {}},
              "site": {"attach_to": "run", "inputs": [{"name": "up", "handler": "magic", "sets": "y"}],
                       "form": [{"name": "z", "type": "slider"}], "from_run": {"x": "run.colour"},
                       "results": {"sources": {"s": "s.json"}, "tiles": [{"label": "n", "value": "t.n"}],
                                   "top_tables": [{"title": "T", "path": "t.csv", "columns": [{"label": "Switch", "value": "x", "format": True}]}]}}},
        "b": {"site": {"attach_to": "page", "page": "nowhere"}},
        "c": {"parameters": {}},
        "d": {"site": {"attach_to": "none"}}}}
    p = "\n".join(extras.check_manifest(m))
    for bit in ["handler 'magic'", "sets 'y'", "type 'slider'", "'z' is not a parameter", "run.colour",
                "page 'nowhere'", "c: no `site` block", "reads 't.n'", "format True"]:
        assert bit in p, bit
    assert "d:" not in p


TOY_MANIFEST = """
version: 9.9.9
steps: []
modules:
  toy:
    description: A made-up module, added only to the code bank.
    entry_point: toy/run_toy.R
    steps:
      - {id: 1, name: T01_count, critical: true, description: Count rows above the threshold}
    parameters:
      table_file: {type: path, required: true}
      threshold: {type: number, default: 2}
      organism: {type: enum, values: [auto, human, mouse, other], default: auto}
      label: {type: string, default: toy}
    site:
      attach_to: run
      title: Toy analysis
      description: Counts how many values pass a threshold for {test} vs {reference}.
      inputs:
        - {name: table, handler: file, sets: table_file, label: A table, accept: [".csv"], required: true}
      from_run: {organism: run.organism, label: "{test}_toy"}
      form:
        - {name: threshold, type: number, label: Threshold, min: 0, max: 100}
      results:
        sources: {s: T01_count/summary.json}
        tiles: [{label: rows above, value: s.n_above}]
        headline: ["{s.n_above} of {s.n_rows} rows are above {s.threshold} ({s.label}, {s.organism})."]
        figures: [{path: T01_count/plot.png, title: Toy plot, caption: A plot.}]
        top_tables:
          - {title: Rows, path: T01_count/rows.csv, rows: 5, columns: [{label: Value, value: v, format: num2}]}
        tables: [T01_count/rows.csv]
        methods: T01_count/methods.md
"""

TOY_R = r"""
args <- commandArgs(trailingOnly = TRUE)
cfg <- yaml::read_yaml(args[which(args == "--config") + 1])
runs <- args[which(args == "--runs_dir") + 1]
rd <- file.path(runs, paste0(format(Sys.time(), "%Y%m%d-%H%M%S"), "_", cfg$label)); dir.create(file.path(rd, "T01_count"), recursive = TRUE)
cat("Step 1/1: T01_count ...\n")
v <- read.csv(cfg$table_file)$v
write.csv(data.frame(v = v[v > cfg$threshold]), file.path(rd, "T01_count", "rows.csv"), row.names = FALSE)
jsonlite::write_json(list(n_rows = length(v), n_above = sum(v > cfg$threshold), threshold = cfg$threshold,
                          label = cfg$label, organism = cfg$organism), file.path(rd, "T01_count", "summary.json"), auto_unbox = TRUE)
png(file.path(rd, "T01_count", "plot.png")); plot(v); dev.off()
writeLines("# Methods\n\nCounted.", file.path(rd, "T01_count", "methods.md"))
jsonlite::write_json(list(status = "success"), file.path(rd, "run_summary.json"), auto_unbox = TRUE)
cat("  done in 0.1 s\n")
"""


def r_ok() -> bool:
    try:
        return subprocess.run([settings.rscript, "-e", "cat(requireNamespace('yaml') && requireNamespace('jsonlite'))"],
                              capture_output=True, text=True, timeout=60).stdout.strip().endswith("TRUE")
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not r_ok(), reason="R with yaml and jsonlite needed")
def test_new_module_needs_no_site_code(tmp_path, monkeypatch):
    cb = tmp_path / "codebank"
    (cb / "manifest").mkdir(parents=True)
    (cb / "toy").mkdir()
    (cb / "manifest" / "tools.yaml").write_text(TOY_MANIFEST)
    (cb / "toy" / "run_toy.R").write_text(TOY_R)
    monkeypatch.setattr(settings, "codebank_dir", cb)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    codebank.manifest.cache_clear()
    try:
        assert extras.check_manifest() == []
        job_dir = tmp_path / "data" / "jobs" / "20260101-000000-abcdef"
        run = make_run(job_dir)
        job = {"id": job_dir.name, "upload_id": "u", "status": "success", "created": "2026-01-01", "dataset": "t", "run_dir": str(run),
               "settings": {"condition": "genotype", "reference_level": "WT", "test_level": "KO", "covariates": [], "advanced": {}}}
        (job_dir / "job.json").write_text(json.dumps(job))
        from app.main import app
        with TestClient(app) as c:
            lst = c.get(f"/api/runs/{job_dir.name}/extras").json()
            assert lst["available"] == {"toy": "Toy analysis"}
            assert lst["forms"]["toy"]["description"] == "Counts how many values pass a threshold for KO vs WT."
            r = c.post(f"/api/runs/{job_dir.name}/extras/toy", files={"table": ("t.csv", b"v\n1\n3\n5\n7\n", "text/csv")},
                       data={"threshold": "4"})
            assert r.status_code == 200, r.text
            for _ in range(60):
                ov = c.get(f"/api/runs/{job_dir.name}/extras/toy").json()
                if ov["status"] in extras.FINAL:
                    break
                time.sleep(1)
            assert ov["status"] == "success", ov
            assert ov["headline"] == ["2 of 4 rows are above 4 (KO_toy, mouse)."]
            assert ov["tiles"] == [{"label": "rows above", "value": 2, "style": None}]
            assert ov["figures"][0]["path"] == "T01_count/plot.png" and ov["methods_md"].startswith("# Methods")
            assert [r["v"] for r in ov["top_tables"][0]["rows"]] == ["5", "7"]
            assert ov["steps"] == [{"name": "T01_count", "description": "Count rows above the threshold", "status": "success"}]
            # report section and code .zip pick it up too
            from app.report import _extras_html
            assert "Toy analysis" in _extras_html(job, run) and "2 of 4 rows" in _extras_html(job, run)
    finally:
        codebank.manifest.cache_clear()
