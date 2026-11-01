"""Optional analyses on a finished run: splicing (rMATS tables) and isoforms (Salmon output).

These run the code bank's R modules, so they are skipped when R lacks DESeq2, DEXSeq,
stageR, tximport or fgsea (set RSCRIPT to an R that has them).
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

from app import code_bundle, extras
from app.config import settings
from app.report import _extras_html

from test_chat import make_run

PKGS = ["DESeq2", "DEXSeq", "stageR", "tximport", "fgsea"]


def r_ready() -> bool:
    if "modules" not in __import__("app.codebank", fromlist=["manifest"]).manifest():
        return False
    try:
        out = subprocess.run([settings.rscript, "-e", f"cat(all(sapply(c({', '.join(repr(p) for p in PKGS)}), requireNamespace, quietly=TRUE)))"],
                             capture_output=True, text=True, timeout=120)
        return out.stdout.strip().endswith("TRUE")
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not r_ready(), reason="R with DESeq2, DEXSeq, stageR, tximport and fgsea is needed")
SAMPLES = [f"s{i}" for i in range(6)]          # make_run: s0-s2 WT, s3-s5 KO


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    job_dir = tmp_path / "jobs" / "20260101-000000-abcdef"
    run = make_run(job_dir)
    (run / "params.yaml").write_text(yaml.safe_dump({"condition": "genotype"}))
    (run / "provenance.json").write_text(json.dumps({"code_bank": {"version": "x", "commit": None}}))
    job = {"id": job_dir.name, "upload_id": "u", "status": "success", "created": "2026-01-01", "dataset": "test",
           "run_dir": str(run), "settings": {"condition": "genotype", "reference_level": "WT", "test_level": "KO",
                                             "covariates": ["sex"], "organism": "mouse", "run_name": "ko",
                                             "advanced": {"run_enrichment": False}}}
    (job_dir / "job.json").write_text(json.dumps(job))
    from app.main import app
    with TestClient(app) as c:
        yield c, job_dir, run


def wait(c, job_id, module, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ov = c.get(f"/api/runs/{job_id}/extras/{module}").json()
        if ov["status"] in extras.FINAL:
            return ov
        time.sleep(2)
    raise TimeoutError(module)


def rmats_zip() -> bytes:
    rnd = random.Random(5)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for et, coords in {"SE": ["exonStart_0base", "exonEnd", "upstreamES", "upstreamEE", "downstreamES", "downstreamEE"],
                           "RI": ["riExonStart_0base", "riExonEnd", "upstreamES", "upstreamEE", "downstreamES", "downstreamEE"]}.items():
            head = ["ID", "GeneID", "geneSymbol", "chr", "strand", *coords, "ID", "IJC_SAMPLE_1", "SJC_SAMPLE_1", "IJC_SAMPLE_2",
                    "SJC_SAMPLE_2", "IncFormLen", "SkipFormLen", "PValue", "FDR", "IncLevel1", "IncLevel2", "IncLevelDifference"]
            rows = ["\t".join(head)]
            for i in range(150):
                planted = et == "SE" and i < 20
                p2 = rnd.uniform(0.3, 0.7)
                p1 = p2 - 0.3 if planted else p2 + rnd.gauss(0, 0.02)
                base = 10000 * (i + 1)
                rows.append("\t".join(map(str, [i, f'"ENSMUSG{i:011d}"', f'"Gene{i}"', "chr1", "+", base + 500, base + 600, base, base + 100,
                                                base + 1000, base + 1100, i, "30,32,31", "30,29,33", "30,31,30", "29,30,31", 249, 99,
                                                1e-9 if planted else rnd.random(), 1e-7 if planted else rnd.uniform(0.2, 1),
                                                ",".join(f"{p1:.3f}" for _ in range(3)), ",".join(f"{p2:.3f}" for _ in range(3)), round(p1 - p2, 3)])))
            z.writestr(f"rmats_out/{et}.MATS.JCEC.txt", "\n".join(rows) + "\n")
        z.writestr("rmats_out/summary.txt", "not a table")
    return buf.getvalue()


def salmon_zip(samples=SAMPLES, ko_from: int = 3) -> bytes:
    """GENCODE-style transcript names (gene in the name), 300 genes, 25 with an isoform switch in KO."""
    rnd = random.Random(9)
    genes = []
    for g in range(300):
        k = rnd.choice([1, 2, 2, 3])
        genes.append([(f"ENSMUST{g:06d}{t}.1|ENSMUSG{g:011d}.1|-|-|Gene{g}-20{t}|Gene{g}|1500|protein_coding|", t) for t in range(k)])
    switch = [g for g in range(300) if len(genes[g]) >= 2][:25]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for si, s in enumerate(samples):
            ko = si >= ko_from
            lines = ["Name\tLength\tEffectiveLength\tTPM\tNumReads"]
            for g, txs in enumerate(genes):
                total = 400 + 7 * g % 900
                shares = [0.8] + [0.2 / (len(txs) - 1)] * (len(txs) - 1) if len(txs) > 1 else [1.0]
                if ko and g in switch:
                    shares = [0.2] + [0.8 / (len(txs) - 1)] * (len(txs) - 1)
                for (name, _), sh in zip(txs, shares):
                    reads = max(0, round(rnd.gauss(total * sh, (total * sh) ** 0.5 + 1)))
                    lines.append(f"{name}\t1500\t1300\t{reads / 13:.3f}\t{reads}")
            z.writestr(f"salmon/{s}/quant.sf", "\n".join(lines) + "\n")
            z.writestr(f"salmon/{s}/logs/salmon_quant.log", "log")
    return buf.getvalue()


def test_splicing_on_a_run(client):
    c, job_dir, run = client
    assert set(c.get(f"/api/runs/{job_dir.name}/extras").json()["available"]) == {"splicing", "isoforms"}
    r = c.post(f"/api/runs/{job_dir.name}/extras/splicing", files=[("files", ("rmats.zip", rmats_zip(), "application/zip"))],
               data={"delta_psi_cutoff": "0.1"})
    assert r.status_code == 200, r.text
    ov = wait(c, job_dir.name, "splicing")
    assert ov["status"] == "success", ov.get("error")
    sm = ov["summary"]
    assert sm["n_significant"] == 20 and sm["n_down"] == 20 and sm["comparison"] == "KO vs WT"
    assert ov["figures"] and ov["methods_md"] and len(ov["top"]) == 20
    assert sm["splicing_vs_expression"]                        # compared with the run's DE results
    fig = c.get(f"/api/runs/{job_dir.name}/extras/splicing/files/{ov['figures'][0]['path']}")
    assert fig.status_code == 200 and fig.headers["content-type"] == "image/png"
    assert c.get(f"/api/runs/{job_dir.name}/extras/splicing/files/../../job.json").status_code == 404
    assert c.get(f"/api/runs/{job_dir.name}/extras/splicing/download").content[:2] == b"PK"
    # report section, code zip and chat workspace include it
    job = json.loads((job_dir / "job.json").read_text())
    assert "Alternative splicing" in _extras_html(job, run)
    with zipfile.ZipFile(code_bundle.build(job, job_dir, run)) as z:
        names = z.namelist()
        assert any(n.endswith("extra_analyses/splicing/run_config.yaml") for n in names)
        assert any(n.endswith("extra_analyses/splicing/run_splicing.sh") for n in names)
        cfg = yaml.safe_load(z.read(next(n for n in names if n.endswith("extra_analyses/splicing/run_config.yaml"))))
        assert cfg["rmats_dir"] == "data/rmats" and "de_run" not in cfg
        assert not any("/data/rmats/SE.MATS" in n for n in names)
    from app.chat.agent import Conversation
    conv = Conversation.create(job_dir, run, job)
    assert (conv.workspace.root / "extras" / "splicing" / "S02_splicing_results" / "splicing_events_all.csv").exists()
    assert "optional_analyses" in conv.system()[0]["text"]


def test_isoforms_on_a_run(client):
    c, job_dir, run = client
    r = c.post(f"/api/runs/{job_dir.name}/extras/isoforms", files={"quant_zip": ("salmon.zip", salmon_zip(), "application/zip")})
    assert r.status_code == 200, r.text
    ov = wait(c, job_dir.name, "isoforms")
    assert ov["status"] == "success", ov.get("error")
    dui = ov["summary"]["dui"]
    assert dui["n_genes_significant"] >= 20 and dui["n_isoform_switches"] >= 20, dui
    assert ov["top"] and ov["top_dei"] and any("usage" in f["title"].lower() for f in ov["figures"])
    assert ov["settings"]["annotation"] == "tx2gene_from_names.csv"     # gene taken from GENCODE names


def test_bad_uploads_are_explained(client):
    c, job_dir, _ = client
    r = c.post(f"/api/runs/{job_dir.name}/extras/splicing", files=[("files", ("notes.txt", b"hello", "text/plain"))])
    assert r.status_code == 422 and "No rMATS tables" in r.json()["detail"]
    r = c.post(f"/api/runs/{job_dir.name}/extras/isoforms", files={"quant_zip": ("s.zip", salmon_zip(SAMPLES[:4]), "application/zip")})
    assert r.status_code == 422 and "s4, s5" in r.json()["detail"]
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escape/SE.MATS.JCEC.txt", "x")
    r = c.post(f"/api/runs/{job_dir.name}/extras/splicing", files=[("files", ("e.zip", evil.getvalue(), "application/zip"))])
    assert r.status_code == 422 and "unsafe path" in r.json()["detail"]
    assert not (job_dir / "extras" / "escape").exists()
  
