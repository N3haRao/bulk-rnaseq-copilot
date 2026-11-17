"""Choosing inputs of the optional analyses from server storage (no R needed)."""

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import extras, server_files
from app.config import settings


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    inc, refs, lab = tmp_path / "incoming", tmp_path / "refs", tmp_path / "lab"
    monkeypatch.setenv("FASTQ_INCOMING_DIR", str(inc))
    monkeypatch.setenv("REFS_DIR", str(refs))
    monkeypatch.setenv("SERVER_FILE_ROOTS", f"Lab projects={lab};Missing=/no/such/folder")
    res = tmp_path / "data" / "fastq" / "20260101-000000-abcdef" / "results"
    for cmp in ("Non.targeting_vs_CHERP", "Non.targeting_vs_RBM17"):
        t = res / "rmats" / cmp / "tables"
        t.mkdir(parents=True)
        for f in ("SE.MATS.JCEC.txt", "RI.MATS.JCEC.txt", "SE.MATS.JC.txt", "notes.txt"):
            (t / f).write_text("ID\n1\n")
    (res.parent / "job.json").write_text(json.dumps({"folder": "GSE107648", "species": "human", "status": "success"}))
    bams = res / "star" / "bams"
    bams.mkdir(parents=True)
    for s in ("s0", "s1"):
        (bams / f"{s}.bam").write_bytes(b"BAM")
        (bams / f"{s}.bam.bai").write_bytes(b"BAI")
        q = res / "salmon" / s
        q.mkdir(parents=True)
        (q / "quant.sf").write_text("Name\tLength\n")
    refs.joinpath("human").mkdir(parents=True)
    (refs / "human" / "gencode.v44.annotation.gtf.gz").write_bytes(b"x")
    (refs / "human" / ".secret").write_text("no")
    lab.mkdir()
    (lab / "proj").mkdir()
    (tmp_path / "outside.gtf").write_text("x")
    (lab / "proj" / "escape.gtf").symlink_to(tmp_path / "outside.gtf")
    inc.mkdir()
    return tmp_path, res


def test_roots_and_browsing(env):
    tmp, res = env
    top = server_files.browse()
    keys = [r["key"] for r in top["roots"]]
    assert keys == ["incoming", "fastq", "refs", "lab_projects"]          # a missing folder is left out
    jobs = server_files.browse("fastq")
    assert jobs["entries"][0]["note"] == "GSE107648 · human · success"
    t = server_files.browse("fastq", "20260101-000000-abcdef/results/rmats/Non.targeting_vs_CHERP/tables", [".txt"])
    assert {e["name"] for e in t["entries"]} == {"SE.MATS.JCEC.txt", "RI.MATS.JCEC.txt", "SE.MATS.JC.txt", "notes.txt"}
    assert t["parent"] == "20260101-000000-abcdef/results/rmats/Non.targeting_vs_CHERP"
    bams = server_files.browse("fastq", "20260101-000000-abcdef/results/star/bams", [".bam"])
    assert [e["name"] for e in bams["entries"]] == ["s0.bam", "s1.bam"]
    refs = server_files.browse("refs", "human")
    assert [e["name"] for e in refs["entries"]] == ["gencode.v44.annotation.gtf.gz"]   # hidden files are not listed


@pytest.mark.parametrize("token", ["refs:../outside.gtf", "refs:human/.secret", "lab_projects:proj/escape.gtf",
                                   "nope:x", "refs:human/missing.gtf", "/etc/passwd"])
def test_choices_outside_the_roots_are_refused(env, token):
    with pytest.raises(server_files.PickError):
        server_files.resolve(token)


def test_server_uploads_expand_folders(env):
    tmp, res = env
    j = "fastq:20260101-000000-abcdef/results"
    up = extras.server_uploads("splicing", {"rmats_tables": [f"{j}/rmats/Non.targeting_vs_CHERP"]})   # one level above tables/
    assert [n for n, _ in up["rmats_tables"]] == ["RI.MATS.JCEC.txt", "SE.MATS.JC.txt", "SE.MATS.JCEC.txt"]
    assert all(isinstance(fh, extras.ServerFile) for _, fh in up["rmats_tables"])
    with pytest.raises(extras.ExtraError, match="several"):
        extras.server_uploads("splicing", {"rmats_tables": [f"{j}/rmats"]})        # two comparisons: ambiguous
    up = extras.server_uploads("majiq", {"bams": [f"{j}/star/bams"], "gff3": ["refs:human/gencode.v44.annotation.gtf.gz"]})
    assert [n for n, _ in up["bams"]] == ["s0.bam", "s0.bam.bai", "s1.bam", "s1.bam.bai"]
    with pytest.raises(extras.ExtraError, match="choose a file"):
        extras.server_uploads("suppa", {"gtf": ["refs:human"]})
    with pytest.raises(extras.ExtraError, match="choose a folder"):
        extras.server_uploads("isoforms", {"quant_zip": ["refs:human/gencode.v44.annotation.gtf.gz"]})
    with pytest.raises(extras.ExtraError, match="outside"):
        extras.server_uploads("suppa", {"gtf": ["refs:../outside.gtf"]})


def test_sample_folders_from_a_server_folder(env, tmp_path):
    _, res = env
    up = extras.server_uploads("isoforms", {"quant_zip": ["fastq:20260101-000000-abcdef/results/salmon"]})
    spec = {"name": "quant_zip", "sets": "quant_dir", "file": "quant.sf"}
    out = tmp_path / "an" / "inputs" / "quant_zip"
    got = extras._h_sample_folders(spec, up["quant_zip"], out, {"samples": ["s0", "s1"]})
    assert got == {"quant_dir": str(out)}
    q = out / "s0" / "quant.sf"
    assert q.is_symlink() and q.resolve() == (res / "salmon" / "s0" / "quant.sf").resolve()
    with pytest.raises(extras.ExtraError, match="s2"):
        extras._h_sample_folders(spec, up["quant_zip"], tmp_path / "b", {"samples": ["s0", "s2"]})
    # a .zip chosen on the server works like an uploaded one
    z = res / "salmon.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for s in ("s0", "s1"):
            zf.writestr(f"{s}/quant.sf", "Name\tLength\n")
    up = extras.server_uploads("isoforms", {"quant_zip": ["fastq:20260101-000000-abcdef/results/salmon.zip"]})
    got = extras._h_sample_folders(spec, up["quant_zip"], tmp_path / "c" / "quant_zip", {"samples": ["s0", "s1"]})
    assert (Path(got["quant_dir"]) / "s1" / "quant.sf").is_file()
    assert z.exists()                                                          # the server's .zip is left alone


def test_form_spec_says_how_to_choose(env):
    from test_chat import make_run  # noqa: F401  (job shape only)
    job = {"settings": {"test_level": "CHERP", "reference_level": "Non-targeting", "condition": "c", "covariates": []}}
    picks = {i["name"]: i["pick"] for m in ("splicing", "isoforms", "majiq") for i in extras.form_spec(m, job)["inputs"]}
    assert picks == {"rmats_tables": "files", "quant_zip": "folder", "annotation": "file", "bams": "files", "gff3": "file"}


def test_api(env):
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/api/server-files")
        assert r.status_code == 200 and r.json()["root"] is None
        r = c.get("/api/server-files", params={"root": "refs", "path": "human", "accept": ".gtf.gz,.gff3"})
        assert [e["name"] for e in r.json()["entries"]] == ["gencode.v44.annotation.gtf.gz"]
        assert c.get("/api/server-files", params={"root": "refs", "path": "../.."}).status_code == 422
