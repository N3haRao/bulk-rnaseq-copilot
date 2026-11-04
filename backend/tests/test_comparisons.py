"""Cross-study comparison through the API: finished runs plus an uploaded results table.

Runs the code bank's compare module (v0.5.0+), so it is skipped when the code bank has no
compare module or R lacks fgsea / babelgene (set RSCRIPT and CODEBANK_DIR).
"""

import io
import json
import random
import subprocess
import time
import zipfile

import pytest
import yaml
from fastapi.testclient import TestClient

from app import codebank, comparisons
from app.config import settings


def r_ready() -> bool:
    if not comparisons.available():
        return False
    try:
        out = subprocess.run([settings.rscript, "-e", "cat(all(sapply(c('fgsea','babelgene','ggplot2'), requireNamespace, quietly=TRUE)))"],
                             capture_output=True, text=True, timeout=120)
        return out.stdout.strip().endswith("TRUE")
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not r_ready(), reason="code bank with the compare module and R with fgsea + babelgene needed")

N = 1500
GENES = [f"GENE{i:04d}" for i in range(N)]
UP, DOWN = set(GENES[:100]), set(GENES[100:200])


def de_rows(seed: int, scale: float = 1.0, discord: bool = False) -> list[list]:
    rnd = random.Random(seed)
    rows = []
    for g in GENES:
        lfc = rnd.gauss(0, 0.3)
        if g in UP:
            lfc = 2 * scale
        elif g in DOWN:
            lfc = -2 * scale
        elif discord and g in set(GENES[200:250]):
            lfc = -2
        p = 1e-10 if abs(lfc) > 1 else rnd.random()
        rows.append([g, lfc, lfc / 0.3, p])
    pv = sorted(r[3] for r in rows)
    for r in rows:   # crude BH
        r.append(min(1.0, r[3] * N / (pv.index(r[3]) + 1)))
    return rows


def make_job(root, name: str, seed: int, discord: bool = False) -> dict:
    """A finished job with a run folder holding what the compare module reads."""
    job_id = f"20260101-00000{seed}-abcde{seed}"
    run = root / "jobs" / job_id / "runs" / f"20260101-00000{seed}_{name}"
    for d in ("01_validate_inputs", "04_differential_expression", "06_enrichment/test_sets"):
        (run / d).mkdir(parents=True)
    (run / "01_validate_inputs" / "resolved_params.yaml").write_text(yaml.safe_dump({
        "condition": "genotype", "reference_level": "WT", "test_level": "KO", "organism": "other", "gene_id_type": "symbol"}))
    rows = de_rows(seed, discord=discord)
    lines = ["gene_id,gene_symbol,entrez_id,baseMean,log2FoldChange,lfcSE,stat,pvalue,padj,significant,direction"]
    for g, lfc, stat, p, q in rows:
        sig = q < 0.05 and abs(lfc) > 1
        lines.append(f"{g},{g},,100,{lfc:.4f},0.3,{stat:.4f},{p:.3g},{q:.3g},{'TRUE' if sig else 'FALSE'},"
                     f"{('up' if lfc > 0 else 'down') if sig else 'not significant'}")
    (run / "04_differential_expression" / "de_results_all.csv").write_text("\n".join(lines) + "\n")
    (run / "04_differential_expression" / "de_summary.json").write_text(json.dumps({
        "comparison": "KO vs WT", "significance_rule": "padj < 0.05 and |shrunken log2FC| > 1", "lfc_shrinkage_used": "apeglm"}))
    rnd = random.Random(seed)
    gsea = ["pathway,pvalue,padj,ES,NES,set_size,direction,leading_edge_genes",
            "PLANTED_UP,1e-8,1e-6,0.8,2.5,60,higher in KO,GENE0001", "PLANTED_DOWN,1e-8,1e-6,-0.8,-2.4,60,higher in WT,GENE0101"]
    gsea += [f"RANDOM_{i:02d},{rnd.random():.3f},{0.5 + rnd.random() / 2:.3f},0.1,{rnd.gauss(0, 0.6):.3f},40,higher in KO," for i in range(15)]
    (run / "06_enrichment" / "test_sets" / "gsea.csv").write_text("\n".join(gsea) + "\n")
    (run / "run_summary.json").write_text(json.dumps({"status": "success"}))
    job = {"id": job_id, "upload_id": "none", "status": "success", "created": "2026-01-01T00:00:00+00:00",
           "dataset": f"{name}.csv", "run_dir": str(run), "settings": {
               "condition": "genotype", "reference_level": "WT", "test_level": "KO", "covariates": [], "advanced": {}}}
    (root / "jobs" / job_id / "job.json").write_text(json.dumps(job))
    return job


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.main import app
    with TestClient(app) as c:
        yield c


def wait(c, cid: str, timeout: float = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        ov = c.get(f"/api/comparisons/{cid}").json()
        if ov["status"] in comparisons.FINAL:
            return ov
        time.sleep(1)
    raise AssertionError("comparison did not finish")


def published_table() -> bytes:
    """An edgeR-style table that compared WT vs KO (the other way round)."""
    rows = de_rows(9, scale=0.9)
    out = ["Gene\tlogFC\tPValue\tFDR"] + [f"{g}\t{-lfc:.4f}\t{p:.3g}\t{q:.3g}" for g, lfc, _, p, q in rows]
    return ("\n".join(out) + "\n").encode()


def test_compare_runs_and_a_table(client, tmp_path):
    j1 = make_job(tmp_path, "ours", 1)
    j2 = make_job(tmp_path, "geo", 2, discord=True)
    lst = client.get("/api/comparisons").json()
    assert lst["available"] and {r["id"] for r in lst["runs"]} == {j1["id"], j2["id"]}

    spec = {"title": "KO: ours vs GEO vs paper", "padj_cutoff": 0.05, "log2fc_cutoff": 1, "studies": [
        {"kind": "run", "job_id": j1["id"], "label": "ours"},
        {"kind": "run", "job_id": j2["id"], "label": "GSE1"},
        {"kind": "table", "file": 0, "label": "paper", "organism": "other", "comparison": "WT vs KO", "flip": True}]}
    r = client.post("/api/comparisons", data={"spec": json.dumps(spec)},
                    files=[("files", ("supp_table3.tsv", published_table(), "text/tab-separated-values"))])
    assert r.status_code == 200, r.text
    ov = wait(client, r.json()["id"])
    assert ov["status"] == "success", ov.get("error")
    assert [s["label"] for s in ov["study_results"]] == ["ours", "GSE1", "paper"]
    assert ov["study_results"][2]["comparison"] == "KO vs WT" and ov["study_results"][2]["flipped"]
    pair = next(p for p in ov["pairs"] if p["study_a"] == "ours" and p["study_b"] == "GSE1")
    assert pair["up_up"] >= 95 and pair["down_down"] >= 95 and pair["n_significant_b"] - pair["n_significant_both"] >= 45   # GSE1-only genes
    assert pair["figures"]["scatter"] and pair["figures"]["rrho"] and "agree" in pair["rrho"]["pattern"]
    flipped = next(p for p in ov["pairs"] if p["study_b"] == "paper" and p["study_a"] == "ours")
    assert flipped["spearman_log2FC"]["r"] > 0.2 and flipped["up_up"] >= 95   # agrees once flipped
    # pathways: the two runs' own GSEA; the table has no gene sets here and is left out
    assert ov["pathways_status"]["status"] == "done" and ov["pathway_collections"][0]["collection"] == "test_sets"
    assert any(p["pathways"] for p in ov["pairs"])
    assert all(s["status"] == "success" for s in ov["steps"])

    cid = ov["id"]
    assert client.get(f"/api/comparisons/{cid}/files/{pair['figures']['scatter']}").headers["content-type"] == "image/png"
    assert client.get(f"/api/comparisons/{cid}/files/../comparison.json").status_code == 404
    html = client.get(f"/api/comparisons/{cid}/report/html").text
    assert "KO: ours vs GEO vs paper" in html and "data:image/png;base64" in html and "RRHO2" in html
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/comparisons/{cid}/code").content))
    names = z.namelist()
    root = names[0].split("/")[0]
    assert f"{root}/run_comparison.sh" in names and f"{root}/codebank/compare/scripts/run_compare.R" in names
    params = yaml.safe_load(z.read(f"{root}/comparison/params.yaml"))
    assert all(s["input_dir"].startswith("comparison/inputs/") for s in params["studies"])
    assert f"{root}/comparison/inputs/03_paper/de_file.tsv" in names
    assert client.get(f"/api/comparisons/{cid}/download").headers["content-type"] == "application/zip"
    assert client.get("/api/comparisons").json()["comparisons"][0]["id"] == cid


def test_code_zip_reruns(client, tmp_path):
    """The code .zip reruns on its own and gives the same overlaps."""
    j1, j2 = make_job(tmp_path, "a", 3), make_job(tmp_path, "b", 4)
    spec = {"studies": [{"kind": "run", "job_id": j1["id"]}, {"kind": "run", "job_id": j2["id"]}], "compare_pathways": False}
    ov = wait(client, client.post("/api/comparisons", data={"spec": json.dumps(spec)}).json()["id"])
    assert ov["status"] == "success", ov.get("error")
    zbytes = client.get(f"/api/comparisons/{ov['id']}/code").content
    out = tmp_path / "unzipped"
    zipfile.ZipFile(io.BytesIO(zbytes)).extractall(out)
    root = next(out.iterdir())
    r = subprocess.run([settings.rscript, "codebank/compare/scripts/run_compare.R", "--config", "comparison/params.yaml",
                        "--runs_dir", "results", "--steps", "1-2"], cwd=root, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    rerun = json.loads(next((root / "results").glob("*/C02_compare_genes/gene_comparison.json")).read_text())
    assert rerun["pairs"][0]["up_up"] == ov["pairs"][0]["up_up"] and rerun["pairs"][0]["down_down"] == ov["pairs"][0]["down_down"]


def test_bad_requests(client, tmp_path):
    j1 = make_job(tmp_path, "a", 5)
    post = lambda spec, **kw: client.post("/api/comparisons", data={"spec": json.dumps(spec)}, **kw)  # noqa: E731
    assert "between 2 and 8" in post({"studies": [{"kind": "run", "job_id": j1["id"]}]}).json()["detail"]
    r = post({"studies": [{"kind": "run", "job_id": j1["id"], "label": "x"}, {"kind": "run", "job_id": j1["id"], "label": "x"}]})
    assert "same label" in r.json()["detail"]
    assert "not found" in post({"studies": [{"kind": "run", "job_id": j1["id"]}, {"kind": "run", "job_id": "20990101-000000-ffffff"}]}).json()["detail"]
    r = post({"studies": [{"kind": "run", "job_id": j1["id"]}, {"kind": "table", "file": 0, "label": "t"}]})
    assert "no results table" in r.json()["detail"]
    r = post({"studies": [{"kind": "run", "job_id": j1["id"]}, {"kind": "table", "file": 0, "label": "t"}]},
             files=[("files", ("slides.pptx", b"x", "application/octet-stream"))])
    assert "is not a table" in r.json()["detail"]
    assert client.get("/api/comparisons/20990101-000000-ffffff").status_code == 404
    assert not list((tmp_path / "comparisons").iterdir())          # nothing left behind by refused requests


def test_failed_check_is_explained(client, tmp_path):
    j1 = make_job(tmp_path, "a", 6)
    bad = b"gene,foldchange,q\n" + b"".join(f"G{i},1,0.5\n".encode() for i in range(200))
    spec = {"studies": [{"kind": "run", "job_id": j1["id"]}, {"kind": "table", "file": 0, "label": "bad"}]}
    ov = wait(client, client.post("/api/comparisons", data={"spec": json.dumps(spec)}, files=[("files", ("bad.csv", bad, "text/csv"))]).json()["id"])
    assert ov["status"] == "failed" and "log2FoldChange" in ov["error"]
  
