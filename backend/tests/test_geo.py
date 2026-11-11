"""Import from GEO by GSE ID, with GEO's file formats served by a mock transport (no network)."""

import gzip
import io
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import geo, tables, uploads
from app.config import settings

# Optional: GEO's real GSE60450 counts file (GSE60450_Lactation-GenewiseCounts.txt.gz) for one extra test
REAL_SUPPL = Path(os.environ["GEO_TEST_FILE"]) if os.environ.get("GEO_TEST_FILE") else None
if REAL_SUPPL is not None and not REAL_SUPPL.is_file():
    REAL_SUPPL = None

LIBS = ["DG", "DH", "DI", "DJ", "DK", "DL", "LA", "LB", "LC", "LD", "LE", "LF"]
CELL = ["basal"] * 6 + ["luminal"] * 6
STATUS = ["virgin", "virgin", "pregnant", "pregnant", "lactating", "lactating"] * 2
GSM = [f"GSM14802{91 + i}" if i < 9 else f"GSM1480{300 + i - 9}" for i in range(12)]


def series_matrix(titles: list[str], descriptions: list[str]) -> bytes:
    q = lambda xs: "\t".join(f'"{x}"' for x in xs)  # noqa: E731
    lines = [
        '!Series_title\t"Transcriptomes of mammary stem cells and luminal cells (synthetic copy for tests)"',
        '!Series_summary\t"Basal and luminal cells from virgin, pregnant and lactating mice."',
        '!Series_overall_design\t"6 groups, 2 biological replicates each."',
        f"!Sample_title\t{q(titles)}",
        f"!Sample_geo_accession\t{q(GSM)}",
        f"!Sample_source_name_ch1\t{q(['mammary gland'] * 12)}",
        f"!Sample_organism_ch1\t{q(['Mus musculus'] * 12)}",
        f"!Sample_characteristics_ch1\t{q(['strain: FVB/N'] * 12)}",
        f"!Sample_characteristics_ch1\t{q(['cell type: ' + c for c in CELL])}",
        f"!Sample_characteristics_ch1\t{q(['developmental stage: ' + s for s in STATUS])}",
        f"!Sample_description\t{q(descriptions)}",
        f"!Sample_library_strategy\t{q(['RNA-Seq'] * 12)}",
        "!series_matrix_table_begin", '"ID_REF"', "!series_matrix_table_end",
    ]
    return gzip.compress(("\n".join(lines) + "\n").encode())


def ncbi_counts() -> bytes:
    rows = ["GeneID\t" + "\t".join(GSM)] + [f"{100 + g}\t" + "\t".join(str((g * 7 + i * 3) % 500) for i in range(12)) for g in range(300)]
    return gzip.compress(("\n".join(rows) + "\n").encode())


def suppl_counts() -> bytes:
    if REAL_SUPPL:
        return REAL_SUPPL.read_bytes()
    cols = [f"MCL1-{lib}_BC2CTUACXX_ACTTGA_L00{1 if lib.startswith('L') else 2}_R1" for lib in LIBS]
    rows = ["EntrezGeneID\tLength\t" + "\t".join(cols)] + [f"{100 + g}\t3000\t" + "\t".join(str(g % 90) for _ in cols) for g in range(300)]
    return gzip.compress(("\n".join(rows) + "\n").encode())


def listing(names: dict[str, str]) -> bytes:
    body = "".join(f'<a href="{n}">{n}</a>     2014-08-01 10:00  {size}\n' for n, size in names.items())
    return f'<html><body><pre><a href="/geo/series/">Parent Directory</a>\n{body}</pre></body></html>'.encode()


def make_transport(*, with_ncbi=True, titles=None, descriptions=None, calls=None):
    titles = titles or [f"{c} {s} rep{1 + i % 2}" for i, (c, s) in enumerate(zip(CELL, STATUS))]
    descriptions = descriptions or [f"MCL1-{lib}" for lib in LIBS]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if calls is not None:
            calls.append(url)
        if url.endswith("/GSE60nnn/GSE60450/matrix/"):
            return httpx.Response(200, content=listing({"GSE60450_series_matrix.txt.gz": "5.2K"}))
        if url.endswith("/matrix/GSE60450_series_matrix.txt.gz"):
            return httpx.Response(200, content=series_matrix(titles, descriptions))
        if url.endswith("/GSE60nnn/GSE60450/suppl/"):
            return httpx.Response(200, content=listing({"GSE60450_Lactation-GenewiseCounts.txt.gz": "1.4M",
                                                         "GSE60450_RAW.tar": "20M", "GSE60450_DE_results.txt.gz": "2M"}))
        if url.endswith("/suppl/GSE60450_Lactation-GenewiseCounts.txt.gz"):
            return httpx.Response(200, content=suppl_counts())
        if "rnaseq_counts" in url:
            if with_ncbi and "GRCm39" in url:
                return httpx.Response(200, content=ncbi_counts())
            return httpx.Response(200, content=b"<html>No file found</html>")
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    yield tmp_path
    geo.set_transport(None)


def test_inspect_offers_ncbi_counts_first(data):
    geo.set_transport(make_transport())
    info = geo.inspect("gse60450")
    assert info["accession"] == "GSE60450" and info["n_samples"] == 12 and info["organisms"] == ["Mus musculus"]
    assert info["recommended"] == "ncbi"
    ids = [o["id"] for o in info["counts_options"]]
    assert ids == ["ncbi", "suppl:GSE60450_Lactation-GenewiseCounts.txt.gz"]       # RAW.tar and DE results are not offered
    assert info["samples"][0]["characteristics"] == {"strain": "FVB/N", "cell_type": "basal", "developmental_stage": "virgin"}


def test_import_ncbi_counts_builds_matching_tables(data):
    geo.set_transport(make_transport())
    out = geo.import_dataset("GSE60450", "ncbi")
    d = uploads.upload_dir(out["upload_id"])
    rec = uploads.record(out["upload_id"])
    assert rec["source"] == "geo" and rec["geo"]["counts_source"] == "ncbi" and rec["geo"]["organism"] == "mouse"
    ch, crows = tables.read_table(d / rec["counts_file"])
    mh, mrows = tables.read_table(d / rec["metadata_file"])
    assert ch[1:] == [r[0] for r in mrows]                       # same samples, same order
    assert ch[1] == "basal_virgin_rep1"                          # readable IDs from the GEO titles
    assert mh == ["sample_id", "cell_type", "developmental_stage", "title", "geo_accession"]   # constant 'strain' dropped
    assert mrows[6][1] == "luminal" and len(crows) == 300
    assert "Design: 6 groups" in out["description"]


@pytest.mark.parametrize("titles,descriptions", [
    (None, None),                                                         # description = library name (MCL1-DG)
    ([f"{lib}" for lib in LIBS], [""] * 12),                              # title = short code (DG) inside the column name
])
def test_import_supplementary_matches_columns(data, titles, descriptions):
    geo.set_transport(make_transport(with_ncbi=False, titles=titles, descriptions=descriptions))
    info = geo.inspect("GSE60450")
    assert info["recommended"] == "suppl:GSE60450_Lactation-GenewiseCounts.txt.gz"
    out = geo.import_dataset("GSE60450", info["recommended"])
    rec = uploads.record(out["upload_id"])
    assert rec["geo"]["n_samples_matched"] == 12 and rec["geo"]["columns_left_out"] == ["Length"]
    d = uploads.upload_dir(out["upload_id"])
    _, mrows = tables.read_table(d / rec["metadata_file"])
    gsm_col = [r[-1] for r in mrows]
    assert gsm_col == GSM                                        # every column matched to the right GSM, in order
    desc = uploads.describe(out["upload_id"])
    assert desc["proposal"]["condition"] in ("cell_type", "developmental_stage")


def test_downloads_are_cached(data):
    calls = []
    geo.set_transport(make_transport(calls=calls))
    geo.inspect("GSE60450")
    n = len(calls)
    geo.import_dataset("GSE60450", "ncbi")
    assert not [c for c in calls[n:] if c.endswith(".gz") or "rnaseq_counts" in c]


def test_errors_are_explained(data):
    geo.set_transport(make_transport())
    with pytest.raises(geo.GeoError, match="GSE60450"):
        geo.inspect("60450")
    with pytest.raises(geo.GeoError, match="not found on GEO"):
        geo.inspect("GSE1")
    with pytest.raises(geo.GeoError, match="not one of the count tables"):
        geo.import_dataset("GSE60450", "suppl:other.txt")

    def offline(req):
        raise httpx.ConnectError("no network")
    geo.set_transport(httpx.MockTransport(offline))
    with pytest.raises(geo.GeoError, match="Could not reach GEO"):
        geo.inspect("GSE60451")


def test_unmatched_columns_fail_clearly(data):
    geo.set_transport(make_transport(with_ncbi=False, titles=[f"sample {i}" for i in range(12)], descriptions=["x"] * 12))
    with pytest.raises(geo.GeoError, match="could not be matched"):
        geo.import_dataset("GSE60450", "suppl:GSE60450_Lactation-GenewiseCounts.txt.gz")


def test_api(data):
    geo.set_transport(make_transport())
    from app.main import app
    with TestClient(app) as c:
        info = c.post("/api/geo/inspect", json={"accession": "GSE60450"}).json()
        assert info["recommended"] == "ncbi"
        up = c.post("/api/geo/import", json={"accession": "GSE60450", "counts_option": "ncbi"}).json()
        assert up["geo"]["accession"] == "GSE60450" and up["metadata_preview"]["n_rows"] == 12 and up["description"]
        assert c.post("/api/geo/inspect", json={"accession": "hello"}).status_code == 422
