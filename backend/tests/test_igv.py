"""IGV for the splicing results: event places, the genome and transcript indexes, BAMs, junctions and file serving."""

import json

import pytest
from fastapi.testclient import TestClient

from app import igv
from app.config import settings

from test_chat import make_run

JOB = "20260101-000000-abcdef"
FQ = "20261110-101010-a1b2c3"
EXONS = [(1000, 1300), (4000, 4120), (8000, 8400)]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", data)
    monkeypatch.setenv("REFS_DIR", str(tmp_path / "refs"))
    monkeypatch.setenv("FASTQ_INCOMING_DIR", str(tmp_path / "incoming"))
    refs = tmp_path / "refs" / "human"
    refs.mkdir(parents=True)
    seq = ("ACGTTGCA" * 1250)[:10000]
    (refs / "GRCh38.primary_assembly.genome.fa").write_text(
        ">chr1 test\n" + "\n".join(seq[i:i + 60] for i in range(0, len(seq), 60)) + "\n>chr2\n" + "ACGT" * 15 + "\nAC\n")

    def g(t, a, b, tid):
        return (f'chr1\tT\t{t}\t{a + 1}\t{b}\t.\t+\t.\tgene_id "G1"; transcript_id "{tid}"; gene_name "DEMO1"; '
                f'transcript_type "protein_coding"; tag "basic";\n')
    (refs / "gencode.v44.primary_assembly.annotation.gtf").write_text(
        "#comment\n" + "".join(g("exon", a, b, "T1") for a, b in EXONS) + "".join(g("exon", a, b, "T2") for a, b in (EXONS[0], EXONS[2])))
    res = data / "fastq" / FQ / "results"
    (res / "star" / "bams").mkdir(parents=True)
    (res.parent / "job.json").write_text(json.dumps({"id": FQ, "folder": "DEMO", "species": "human", "status": "success"}))
    for i in range(6):
        s = f"s{i}"
        (res / "star" / "bams" / f"{s}.bam").write_bytes(bytes(range(256)) * 4)
        (res / "star" / "bams" / f"{s}.bam.bai").write_bytes(b"BAI")
        d = res / "star" / "star" / s
        d.mkdir(parents=True)
        skip = 30 if i >= 3 else 5
        (d / "SJ.out.tab").write_text(f"chr1\t1301\t4000\t1\t1\t1\t10\t1\t30\nchr1\t1301\t8000\t1\t1\t1\t{skip}\t0\t30\n"
                                      "chr1\t4121\t8000\t1\t1\t1\t10\t0\t30\nchr1\t9000\t9100\t2\t0\t0\t1\t0\t10\n"
                                      "chr2\t10\t20\t1\t1\t1\t50\t0\t10\n")
    jd = data / "jobs" / JOB
    run = make_run(jd)
    up = data / "uploads" / "20260101-000000-123456"
    up.mkdir(parents=True)
    (up / "upload.json").write_text(json.dumps({"id": up.name, "fastq": {"job_id": FQ, "folder": "DEMO"}}))
    job = {"id": JOB, "upload_id": up.name, "status": "success", "run_dir": str(run), "dataset": "demo", "created": "2026",
           "settings": {"condition": "genotype", "reference_level": "WT", "test_level": "KO", "covariates": [],
                        "organism": "human", "run_name": "ko", "advanced": {}}}
    (jd / "job.json").write_text(json.dumps(job))
    igv._jobs.clear()
    return {"job": job, "jd": jd, "run": run, "res": res, "refs": refs, "tmp": tmp_path}


@pytest.mark.parametrize("row,locus", [
    ({"chr": "chr1", "event_start": "1001", "event_end": "8400"}, "chr1:1-10619"),             # rMATS / SUPPA2
    ({"chr": "chr2", "event_start": "50000", "event_end": "50100"}, "chr2:49850-50250"),          # small event: 150 bp each side
    ({"chr": "chr3", "junction": "120345-121000", "lsv_id": "G:s:120000-120100"}, "chr3:120149-121196"),   # MAJIQ
    ({"chr": "chr1"}, None), ({"gene": "x", "event_start": 1, "event_end": 2}, None), ({"chr": "NA", "start": 1, "end": 2}, None),
])
def test_locus_for(row, locus):
    assert igv.locus_for(row) == locus


def test_fai_matches_samtools_layout(env):
    r = igv.reference_files("human")
    out = env["tmp"] / "x.fai"
    igv.build_fai(r["fasta"], out)
    rows = [line.split("\t") for line in out.read_text().splitlines()]
    assert rows[0] == ["chr1", "10000", "11", "60", "61"]
    assert rows[1][:2] == ["chr2", "62"] and rows[1][3:] == ["60", "61"]
    raw = r["fasta"].read_bytes()
    off = int(rows[1][2])
    assert raw[off:off + 4] == b"ACGT"                                      # the offset points at chr2's first base


def test_reference_is_prepared_and_transcripts_served(env):
    st = igv.prepare_reference("human", wait=True)
    assert st["ready"], st
    feats = igv.features("human", "chr1", 3900, 4200)
    assert {f["id"] for f in feats} == {"T1", "T2"}                       # T2 spans the region (an intron of it)
    t1 = next(f for f in feats if f["id"] == "T1")
    assert t1["exons"] == [{"start": a, "end": b} for a, b in EXONS] and t1["name"] == "DEMO1" and t1["strand"] == "+"
    assert igv.features("human", "chr1", 9000, 9500) == [] and igv.features("human", "chrX", 0, 10) == []


def test_view_config_uses_the_fastq_jobs_bams(env):
    cfg = igv.view_config(env["job"], env["jd"], env["run"])
    assert cfg["species"] == "human" and "FASTQ" in cfg["bam_source"] and cfg["missing_bams"] == []
    ko, wt = cfg["groups"]
    assert (ko["name"], ko["role"], ko["samples"]) == ("KO", "test", ["s3", "s4", "s5"]) and wt["name"] == "WT"
    assert all(t["junctions"] for t in ko["tracks"] + wt["tracks"])


def test_junctions_are_summed_per_group(env):
    ko = igv.junctions(env["job"], env["jd"], env["run"], "KO", "chr1", 0, 10000)
    wt = igv.junctions(env["job"], env["jd"], env["run"], "WT", "chr1", 0, 10000)
    skip = {(j["start"], j["end"]): j for j in ko}[(1300, 8000)]
    assert skip["attributes"]["uniquely_mapped"] == "90" and skip["attributes"]["num_samples_with_this_junction"] == "3"
    assert {(j["start"], j["end"]): j for j in wt}[(1300, 8000)]["attributes"]["uniquely_mapped"] == "15"
    assert (8999, 9100) in {(j["start"], j["end"]) for j in ko}            # 3 reads summed (>= min_reads 2), - strand
    assert {(j["start"], j["end"]): j for j in ko}[(8999, 9100)]["strand"] == "-"
    assert igv.junctions(env["job"], env["jd"], env["run"], "KO", "chr1", 5000, 7000) != []   # spans the region
    assert igv.junctions(env["job"], env["jd"], env["run"], "KO", "chr1", 9200, 9900) == []


def test_choose_bams_on_the_server(env, tmp_path, monkeypatch):
    lab = tmp_path / "lab"
    lab.mkdir()
    monkeypatch.setenv("SERVER_FILE_ROOTS", f"Lab={lab}")
    for s in ("s0", "s3", "other"):
        (lab / f"{s}.bam").write_bytes(b"B")
        (lab / f"{s}.bam.bai").write_bytes(b"I")
    (lab / "s1.bam").write_bytes(b"no index")
    out = igv.choose_bams(env["job"], env["jd"], env["run"], ["lab:"])
    assert out["matched"] == ["s0", "s3"] and "s1" in out["not_found"]
    cfg = igv.view_config(env["job"], env["jd"], env["run"])
    assert cfg["bam_source"] == "chosen on the server" and set(cfg["missing_bams"]) == {"s1", "s2", "s4", "s5"}
    with pytest.raises(igv.IgvError, match="named like a sample"):
        igv.choose_bams(env["job"], env["jd"], env["run"], ["lab:other.bam"])
    with pytest.raises(igv.IgvError):
        igv.choose_bams(env["job"], env["jd"], env["run"], ["lab:../data"])


def test_api_serves_registered_files_with_ranges(env):
    from app.main import app
    with TestClient(app) as c:
        cfg = c.get(f"/api/runs/{JOB}/igv").json()
        key = cfg["groups"][0]["tracks"][0]["bam"]
        r = c.get(f"/api/runs/{JOB}/igv/file/{key}", headers={"Range": "bytes=10-19"})
        assert r.status_code == 206 and r.content == bytes(range(10, 20))
        assert c.get(f"/api/runs/{JOB}/igv/file/fai").status_code in (200, 404)   # .fai appears once built
        assert c.get(f"/api/runs/{JOB}/igv/file/0123456789abcdef").status_code == 404
        assert c.get(f"/api/runs/{JOB}/igv/file/..%2F..%2Fjob.json").status_code == 404
        j = c.get(f"/api/runs/{JOB}/igv/junctions", params={"group": "KO", "chr": "chr1", "start": 0, "end": 10000}).json()
        assert len(j) == 4


def test_splicing_rows_carry_their_igv_locus(env):
    jd = env["jd"]
    ed = jd / "extras" / "splicing" / "20260102-000000-aaaaaa"
    rd = ed / "runs" / "r1" / "S02_splicing_results"
    rd.mkdir(parents=True)
    (ed / "extra.json").write_text(json.dumps({"id": ed.name, "module": "splicing", "status": "success"}))
    (rd / "splicing_events_significant.csv").write_text(
        "gene_id,gene_symbol,event_type,chr,strand,event_start,event_end,psi_1,psi_2,delta_psi,fdr\n"
        "G1,DEMO1,SE,chr1,+,1001,8400,0.27,0.86,-0.59,1e-12\n")
    from app import extras
    row = extras.overview(ed)["top_tables"][0]["rows"][0]
    assert row["_locus"] == "chr1:1-10619" and row["_event"] == "chr1:1001-8400"
