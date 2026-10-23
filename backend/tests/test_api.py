"""End-to-end API test: upload, check, run the full code-bank pipeline, read results.

Needs R with the code bank's packages (skipped otherwise). Uses a small
simulated paired dataset with a planted effect, so it takes about a minute.
"""

import io
import math
import os
import random
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings


def r_ready() -> bool:
    if not shutil.which(settings.rscript) and not os.path.exists(settings.rscript):
        return False
    out = subprocess.run([settings.rscript, "-e", "quit(status = !requireNamespace('DESeq2', quietly = TRUE))"],
                         capture_output=True)
    return out.returncode == 0


pytestmark = pytest.mark.skipif(not r_ready(), reason="R with DESeq2 is not installed")


def simulated_files(n_genes: int = 1500) -> tuple[bytes, bytes]:
    """3 donors x (vehicle, drug); the first 100 genes are 4x higher with drug."""
    rng = random.Random(1)
    samples = [f"S{i}" for i in range(1, 7)]
    lines = ["gene_id," + ",".join(samples)]
    for g in range(n_genes):
        mu = math.exp(rng.uniform(2, 7))
        row = []
        for i in range(6):
            m = mu * (4 if g < 100 and i >= 3 else 1)
            # gamma-Poisson (negative binomial) counts
            lam = rng.gammavariate(10, m / 10)
            row.append(str(_poisson(rng, lam)))
        lines.append(f"ENSG{g:011d}," + ",".join(row))
    meta = "sample_id,treatment,donor\n" + "\n".join(
        f"{s},{'vehicle' if i < 3 else 'drug'},D{i % 3 + 1}" for i, s in enumerate(samples))
    return "\n".join(lines).encode(), meta.encode()


def _poisson(rng: random.Random, lam: float) -> int:
    if lam > 50:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_upload_check_run_results(client):
    counts, meta = simulated_files()
    r = client.post("/api/uploads", files={"counts": ("counts.csv", io.BytesIO(counts)),
                                           "metadata": ("samples.csv", io.BytesIO(meta))})
    assert r.status_code == 200, r.text
    up = r.json()
    p = up["proposal"]
    assert (p["condition"], p["reference_level"], p["test_level"], p["covariates"]) == \
        ("treatment", "vehicle", "drug", ["donor"])

    s = {k: p[k] for k in ("condition", "reference_level", "test_level", "covariates")}
    s.update(organism="other", run_name="drug vs vehicle")
    chk = client.post(f"/api/uploads/{up['id']}/check", json=s).json()
    assert chk["status"] != "fail", chk
    assert {c["group"] for c in chk["checks"]} == {"files", "design"}

    job = client.post("/api/runs", json={"upload_id": up["id"], **s}).json()
    for _ in range(300):
        job = client.get(f"/api/runs/{job['id']}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(1)
    assert job["status"] in ("success", "success_with_errors"), job
    assert all(st["status"] == "success" for st in job["steps"][:5])

    res = client.get(f"/api/runs/{job['id']}/results").json()
    assert res["resolved_params"]["design_formula"] == "~ donor + treatment"
    assert res["summary"]["n_up"] >= 50          # the planted effect is found
    assert res["figures"]["de"] and res["methods_md"]

    de = client.get(f"/api/runs/{job['id']}/de", params={"search": "ENSG00000000001"}).json()
    assert de["total"] == 1 and de["rows"][0]["direction"] == "up"
    img = client.get(f"/api/runs/{job['id']}/files/05_de_plots/volcano_plot.png")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert client.get(f"/api/runs/{job['id']}/files/04_differential_expression/dds.rds").status_code == 404

    # report (no model running in tests: the template narrative is used)
    from app.llm.factory import NoLLM, set_executor_llm
    set_executor_llm(NoLLM())
    try:
        rep = client.post(f"/api/runs/{job['id']}/report").json()
    finally:
        set_executor_llm(None)
    assert rep["available"] and rep["source"] == "rules" and "drug" in rep["summary"]
    page = client.get(f"/api/runs/{job['id']}/report/html")
    assert page.status_code == 200 and "Differential expression" in page.text and "data:image/png;base64" in page.text
    if os.environ.get("CHROMIUM_PATH") or os.environ.get("TEST_PDF"):
        pdf = client.get(f"/api/runs/{job['id']}/report/pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_check_explains_mismatched_samples(client):
    counts, meta = simulated_files(200)
    meta = meta.replace(b"S6,", b"S_6,")
    up = client.post("/api/uploads", files={"counts": ("c.csv", io.BytesIO(counts)),
                                            "metadata": ("m.csv", io.BytesIO(meta))}).json()
    chk = client.post(f"/api/uploads/{up['id']}/check",
                      json={"condition": "treatment", "reference_level": "vehicle", "test_level": "drug"}).json()
    assert chk["status"] == "fail"
    msg = next(c["message"] for c in chk["checks"] if c["status"] == "fail")
    assert "S6" in msg and "S_6" in msg


def test_rejects_unknown_file_type(client):
    r = client.post("/api/uploads", files={"counts": ("c.pdf", io.BytesIO(b"x")),
                                           "metadata": ("m.csv", io.BytesIO(b"a,b\n1,2\n"))})
    assert r.status_code == 422 and ".csv" in r.json()["detail"]
