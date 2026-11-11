"""Start from FASTQ: folders, pairing, groups, checks, the job (a stand-in for run_upstream.R), the hand-off to a
dataset, and the rMATS / Salmon files for the run's optional analyses. The real pipeline is tested in the code bank
(later/tests/run_later_tests.R) and inside the image."""

import csv
import gzip
import json
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import fastq, uploads
from app.config import settings
from app.llm.scripted import ScriptedLLM

STUB = textwrap.dedent('''
    import csv, json, sys, yaml, pathlib, random
    cfg = yaml.safe_load(open(sys.argv[sys.argv.index("--config") + 1]))
    out = pathlib.Path(cfg["out_dir"]); out.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(open(cfg["sample_sheet"]), delimiter="\\t"))
    samples = list(dict.fromkeys(r["sample"] for r in rows))
    cond = {r["sample"]: r["condition"] for r in rows}
    summ = {"steps": {}}
    def done(s):
        print(f"== {s} ==", flush=True); summ["steps"][s] = {"status": "success", "seconds": 1}
        (out / "upstream_summary.json").write_text(json.dumps(summ))
    if "qc" in cfg["steps"]:
        (out / "trimmed" / "reports").mkdir(parents=True); (out / "trimmed" / "reports" / "a.html").write_text("<p>qc</p>")
        with open(out / "trimmed" / "qc_summary.csv", "w") as f:
            f.write("sample,percent_kept,flags\\n" + "".join(f"{s},98.5,\\n" for s in samples))
        done("qc")
    if "star" in cfg["steps"]:
        (out / "star").mkdir(); (out / "star" / "strandedness.txt").write_text("reverse")
        with open(out / "star" / "alignment_summary.csv", "w") as f:
            f.write("sample,unique_percent\\n" + "".join(f"{s},{88 + i}\\n" for i, s in enumerate(samples)))
        done("star")
    for s in samples:
        d = out / "salmon" / s; d.mkdir(parents=True)
        d.joinpath("quant.sf").write_text("Name\\tLength\\tEffectiveLength\\tTPM\\tNumReads\\n"
            "ENSMUST01|ENSMUSG01|x|x|Gene1-201|Gene1|1000|protein_coding|\\t1000\\t900\\t10\\t100\\n")
    done("salmon")
    (out / "counts").mkdir()
    random.seed(1)
    with open(out / "counts" / "counts.csv", "w") as f:
        f.write("gene_id," + ",".join(samples) + "\\n")
        for g in range(300):
            f.write(f"ENSMUSG{g:05d}," + ",".join(str(random.randint(50, 500)) for _ in samples) + "\\n")
    with open(out / "counts" / "samples.csv", "w") as f:
        f.write("sample,condition\\n" + "".join(f"{s},{cond[s]}\\n" for s in samples))
    done("gene_counts")
    if "rmats" in cfg["steps"]:
        for g in sorted(set(cond.values()) - {cfg["control"]}):
            t = out / "rmats" / f"{cfg['control']}_vs_{g}" / "tables"; t.mkdir(parents=True)
            t.joinpath("SE.MATS.JCEC.txt").write_text("ID\\tGeneID\\n1\\tg\\n")
            t.joinpath("SE.MATS.JC.txt").write_text("ID\\tGeneID\\n1\\tg\\n")
        done("rmats")
''')


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    inc = tmp_path / "incoming"
    monkeypatch.setenv("FASTQ_INCOMING_DIR", str(inc))
    monkeypatch.setenv("REFS_DIR", str(tmp_path / "refs"))
    exp = inc / "rbp_ko"
    exp.mkdir(parents=True)
    for s in ["WT_1", "WT_2", "WT_3", "KO_1", "KO_2", "KO_3"]:
        for m in (1, 2):
            with gzip.open(exp / f"{s}_S1_L001_R{m}_001.fastq.gz", "wt") as f:
                f.write("@r\nACGT\n+\nIIII\n")
    # a reference that is "on the server" (files only; the indexes are the pipeline's job)
    for k, url in fastq.REFERENCES["mouse"]["files"].items():
        p = tmp_path / "refs" / "mouse" / Path(url).name[:-3]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(">x\nACGT\n")
    stub = tmp_path / "stub_upstream.py"
    stub.write_text(STUB)
    monkeypatch.setattr(fastq, "upstream_command", lambda config: [sys.executable, str(stub), "--config", str(config)])
    settings.uploads_dir.mkdir(parents=True)
    settings.jobs_dir.mkdir(parents=True)
    return tmp_path


def wait(fid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = fastq.fastq_jobs.load(fid)
        if j["status"] in fastq.FINAL:
            return j
        time.sleep(0.2)
    raise AssertionError("FASTQ job did not finish")


def test_folders_pairing_and_groups(env):
    f = fastq.list_folders()
    assert f["folders"][0]["name"] == "rbp_ko" and f["folders"][0]["n_files"] == 12
    p = fastq.pair_files("rbp_ko")
    assert p["layout"] == "paired-end" and not p["problems"]
    assert [s["sample"] for s in p["samples"]] == ["KO_1", "KO_2", "KO_3", "WT_1", "WT_2", "WT_3"]
    assert p["samples"][0]["r1"].endswith("_R1_001.fastq.gz") and p["samples"][0]["r2"].endswith("_R2_001.fastq.gz")
    assert p["suggestion"]["groups"]["KO_2"] == "KO" and p["suggestion"]["control"] == "WT"
    # a missing mate and a second lane are both reported
    (env / "incoming" / "rbp_ko" / "WT_3_S1_L001_R2_001.fastq.gz").unlink()
    with gzip.open(env / "incoming" / "rbp_ko" / "KO_1_S1_L002_R1_001.fastq.gz", "wt") as fh:
        fh.write("@r\nA\n+\nI\n")
    probs = " ".join(fastq.pair_files("rbp_ko")["problems"])
    assert "KO_1: 2 R1" in probs and "only R1" in probs
    with pytest.raises(fastq.FastqError):
        fastq.pair_files("../etc")


def test_assistant_groups_are_checked(env):
    names = ["KO_1", "KO_2", "KO_3", "WT_1", "WT_2", "WT_3"]
    good = ScriptedLLM([{"groups": {n: n.split("_")[0] for n in names}, "control": "WT", "batch": {"WT_1": "1"},
                         "species": "mouse", "note": "grouped by name"}])
    a = fastq.assist("rbp_ko", "mouse cortex, KO vs WT", llm=good)
    assert a["control"] == "WT" and a["species"] == "mouse" and a["groups"]["KO_3"] == "KO"
    bad = ScriptedLLM([{"groups": {"KO_1": "KO"}, "control": "WT", "species": "mouse", "note": ""}])
    a = fastq.assist("rbp_ko", "x", llm=bad)
    assert a["origin"] == "rules" and a["groups"]["WT_2"] == "WT"


def spec(**kw):
    s = {"folder": "rbp_ko", "groups": {n: n.split("_")[0] for n in ["KO_1", "KO_2", "KO_3", "WT_1", "WT_2", "WT_3"]},
         "control": "WT", "species": "mouse", "steps": ["qc", "star", "salmon", "gene_counts", "rmats"], "threads": 4}
    s.update(kw)
    return s


def test_checks_stop_bad_setups(env):
    assert fastq.fastq_jobs.check(spec())["ok"]
    c = fastq.fastq_jobs.check(spec(control="Ctrl"))
    assert not c["ok"] and any("control group" in l["text"] for l in c["lines"])
    c = fastq.fastq_jobs.check(spec(steps=["qc", "salmon"]))
    assert not c["ok"]
    c = fastq.fastq_jobs.check(spec(steps=["qc", "salmon", "gene_counts", "rmats"]))
    assert any("rMATS needs" in l["text"] for l in c["lines"])
    c = fastq.fastq_jobs.check(spec(batch={"KO_1": "1", "KO_2": "1", "KO_3": "1", "WT_1": "2", "WT_2": "2", "WT_3": "2"}))
    assert not c["ok"] and any("cannot be separated" in l["text"] for l in c["lines"])
    c = fastq.fastq_jobs.check(spec(species="human"))
    assert not c["ok"] and any("download them first" in l["text"] for l in c["lines"])


def test_job_to_dataset_and_extras_inputs(env):
    job = fastq.fastq_jobs.start(spec())
    sheet = (settings.data_dir / "fastq" / job["id"] / "samples.tsv").read_text().splitlines()
    assert sheet[0] == "file_name\tsample\tcondition\tread_orientation" and len(sheet) == 13
    import yaml
    cfg = yaml.safe_load((settings.data_dir / "fastq" / job["id"] / "upstream.yaml").read_text())
    assert cfg["control"] == "WT" and cfg["star_index"].endswith("refs/mouse/star_index") and cfg["threads"] == 4
    j = wait(job["id"])
    assert j["status"] == "success", j
    ov = fastq.fastq_jobs.overview(job["id"])
    assert ov["progress"] == 100 and ov["comparisons"] == ["WT_vs_KO"]
    assert {s["id"]: s["status"] for s in ov["step_list"]}["qc"] == "success"
    facts = {s["id"]: s["facts"] for s in ov["step_list"]}
    assert "98%" in facts["qc"][0] and facts["star"] == ["uniquely mapped 88–93%", "library: reverse"]
    assert {f["label"] for f in ov["files"]} >= {"Counts table", "QC reports (fastp)"}
    rec = fastq.fastq_jobs.to_dataset(job["id"])
    assert rec["source"] == "fastq" and rec["fastq"]["job_id"] == job["id"]
    assert fastq.fastq_jobs.to_dataset(job["id"])["id"] == rec["id"]      # once
    run_job = {"upload_id": rec["id"], "settings": {"reference_level": "WT", "test_level": "KO"}}
    assert fastq.source_for_run(run_job)["job_id"] == job["id"]
    av = fastq.extras_available(job["id"], run_job)
    assert av["splicing"]["available"] and av["isoforms"]["available"]
    ins = fastq.extras_inputs(job["id"], "splicing", run_job)
    assert sorted(n for n, _ in ins["rmats_tables"]) == ["SE.MATS.JC.txt", "SE.MATS.JCEC.txt"]
    z = zipfile.ZipFile(fastq.extras_inputs(job["id"], "isoforms", run_job)["quant_zip"][0][1])
    assert sorted(z.namelist())[0] == "KO_1/quant.sf" and len(z.namelist()) == 6
    other = {"upload_id": rec["id"], "settings": {"reference_level": "KO", "test_level": "WT"}}
    assert not fastq.extras_available(job["id"], other)["splicing"]["available"]


def test_api_flow(env):
    from app.main import app
    with TestClient(app) as c:
        f = c.get("/api/fastq/folders").json()
        assert f["folders"][0]["name"] == "rbp_ko" and f["references"][0]["files_ready"] is True
        assert c.get("/api/fastq/folders/rbp_ko").json()["layout"] == "paired-end"
        assert c.post("/api/fastq/check", json=spec()).json()["ok"]
        r = c.post("/api/fastq/jobs", json=spec(control="nope"))
        assert r.status_code == 422
        jid = c.post("/api/fastq/jobs", json=spec()).json()["id"]
        wait(jid)
        assert c.get(f"/api/fastq/jobs/{jid}").json()["status"] == "success"
        assert c.get(f"/api/fastq/jobs/{jid}/files/counts/counts.csv").status_code == 200
        assert c.get(f"/api/fastq/jobs/{jid}/files/../../../etc/passwd").status_code in (404, 422)
        z = c.get(f"/api/fastq/jobs/{jid}/files/trimmed/reports")
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        d = c.post(f"/api/fastq/jobs/{jid}/dataset").json()
        assert d["source"] == "fastq" and "condition" in d["metadata_preview"]["columns"]
