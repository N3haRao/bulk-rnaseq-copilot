"""Import a public dataset from GEO by its series ID (GSE...).

    inspect(gse)          series title, summary, organism, samples (from the series matrix)
                          and the count tables on offer:
                            * NCBI's raw counts (GEO's own RNA-seq pipeline: one standard
                              alignment and count for many human / mouse series; Entrez IDs)
                            * the series' supplementary files that look like count tables
    import_dataset(gse, option)
                          downloads the chosen counts, builds the sample table from the
                          samples' GEO characteristics (matched to the count columns), and
                          creates an upload like any other, with the GEO details recorded

Downloads are cached in data/geo/<GSE>/. All network access goes through one
httpx client (`set_transport` swaps it in tests).
"""

import csv
import gzip
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import tables, uploads
from .config import settings

GSE_RE = re.compile(r"^GSE\d{1,8}$")
FTP = "https://ftp.ncbi.nlm.nih.gov/geo/series"
NCBI_COUNTS = "https://www.ncbi.nlm.nih.gov/geo/download/?type=rnaseq_counts&acc={gse}&format=file&file={file}"
NCBI_BUILDS = {"GRCh38.p13": "human", "GRCm39": "mouse"}
TABLE_FILE = re.compile(r"\.(txt|tsv|csv|tab|xlsx)(\.gz)?$", re.I)
NOT_COUNTS = re.compile(r"_RAW\.tar|\.(bw|bigwig|bed|bam|h5|h5ad|mtx|rds|rda|loom|wig|narrowpeak|broadpeak)(\.gz)?$|"
                        r"barcodes|features|genes\.tsv|fpkm|tpm|rpkm|cpm|normali[sz]ed|deseq|edger|diff|_de_|results", re.I)
COUNTISH = re.compile(r"count|raw|reads|matrix|expression|gene", re.I)

_transport: httpx.BaseTransport | None = None


class GeoError(ValueError):
    """A problem a user can act on, in plain language."""


def set_transport(t: httpx.BaseTransport | None) -> None:
    """Tests replace the network with a mock transport."""
    global _transport
    _transport = t


def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=20.0),
                        headers={"User-Agent": "rnaseq-copilot (GEO import)"})


def _series_dir(gse: str) -> str:
    num = gse[3:]
    stub = "GSE" + (num[:-3] if len(num) > 3 else "") + "nnn"
    return f"{FTP}/{stub}/{gse}"


def _cache(gse: str) -> Path:
    d = settings.data_dir / "geo" / gse
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get(url: str, *, limit_mb: int | None = None) -> bytes:
    limit = (limit_mb or settings.max_upload_mb) * 1024 * 1024
    try:
        with _client() as c, c.stream("GET", url) as r:
            if r.status_code == 404:
                raise FileNotFoundError(url)
            if r.status_code >= 400:
                raise GeoError(f"GEO answered with an error ({r.status_code}) for {url}.")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > limit:
                    raise GeoError(f"The file is larger than {limit // (1024 * 1024)} MB (MAX_UPLOAD_MB): {url}")
            return bytes(buf)
    except httpx.HTTPError as e:
        raise GeoError(f"Could not reach GEO ({type(e).__name__}). Check the internet connection, or download the "
                       f"files from GEO yourself and upload them.") from e


def _cached(gse: str, name: str, url: str) -> bytes:
    p = _cache(gse) / name
    if p.exists():
        return p.read_bytes()
    data = _get(url)
    p.write_bytes(data)
    return data


def _listing(url: str) -> list[tuple[str, str]]:
    """(file name, size text) from an NCBI FTP-over-HTTPS folder listing."""
    try:
        html = _get(url, limit_mb=5).decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    out = []
    for m in re.finditer(r'<a href="([^"?/][^"]*)">[^<]*</a>\s*([^\n<]*)', html):
        name = m.group(1)
        if name.endswith("/"):
            continue
        size = (m.group(2).split() or [""])[-1]
        out.append((name, size))
    return out


def _gunzip(data: bytes) -> bytes:
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


# ---- Series matrix ---------------------------------------------------------------------------
def _parse_matrix(text: str) -> tuple[dict, list[dict]]:
    series: dict[str, list[str]] = {}
    sample_rows: list[tuple[str, list[str]]] = []
    for row in csv.reader(io.StringIO(text), delimiter="\t"):
        if not row or not row[0].startswith("!"):
            continue
        key = row[0][1:]
        if key.startswith("Series_"):
            series.setdefault(key[7:], []).append(" ".join(row[1:]).strip())
        elif key.startswith("Sample_"):
            sample_rows.append((key[7:], row[1:]))
        elif key == "series_matrix_table_begin":
            break
    n = max((len(v) for _, v in sample_rows), default=0)
    samples = [dict() for _ in range(n)]
    for key, vals in sample_rows:
        for i, v in enumerate(vals[:n]):
            v = v.strip()
            if not v:
                continue
            if key.startswith("characteristics"):
                k, sep, val = v.partition(":")
                name = _clean_key(k) if sep else f"characteristic_{len([x for x in samples[i] if x.startswith('characteristic_')]) + 1}"
                samples[i].setdefault("characteristics", {})[name] = (val if sep else v).strip()
            elif key in ("description", "data_processing", "supplementary_file_1", "supplementary_file_2", "relation"):
                samples[i].setdefault(key, []).append(v)
            else:
                samples[i].setdefault(key, v)
    return {k: " ".join(v) for k, v in series.items()}, samples


def _clean_key(k: str) -> str:
    k = re.sub(r"[^0-9a-zA-Z]+", "_", k.strip().lower()).strip("_")
    return k or "characteristic"


def _matrix(gse: str) -> tuple[dict, list[dict]]:
    files = [f for f, _ in _listing(f"{_series_dir(gse)}/matrix/") if f.endswith("series_matrix.txt.gz")]
    if not files:
        raise GeoError(f"{gse} was not found on GEO (no series matrix). Check the accession, e.g. GSE60450.")
    series, samples = {}, []
    for f in sorted(files):
        s, smp = _parse_matrix(_gunzip(_cached(gse, f, f"{_series_dir(gse)}/matrix/{f}")).decode("utf-8", "replace"))
        series = series or s
        samples += smp
    return series, samples


# ---- Count tables on offer -------------------------------------------------------------------------
def _ncbi_counts(gse: str) -> tuple[str, str, bytes] | None:
    """(file name, organism, gz bytes) of NCBI's raw counts, when GEO computed them for this series.
    The answer (found or not) is remembered, so GEO is asked once per series."""
    memo = _cache(gse) / "ncbi_counts.json"
    if memo.exists():
        m = json.loads(memo.read_text())
        if not m:
            return None
        p = _cache(gse) / m["file"]
        if p.exists():
            return m["file"], m["organism"], p.read_bytes()
    for build, organism in NCBI_BUILDS.items():
        name = f"{gse}_raw_counts_{build}_NCBI.tsv.gz"
        try:
            data = _get(NCBI_COUNTS.format(gse=gse, file=name))
        except FileNotFoundError:
            continue
        if data[:2] == b"\x1f\x8b":
            (_cache(gse) / name).write_bytes(data)
            memo.write_text(json.dumps({"file": name, "organism": organism}))
            return name, organism, data
    memo.write_text("null")
    return None


def _suppl_candidates(gse: str) -> list[dict]:
    out = []
    for name, size in _listing(f"{_series_dir(gse)}/suppl/"):
        if not TABLE_FILE.search(name) or NOT_COUNTS.search(name):
            continue
        out.append({"id": f"suppl:{name}", "file": name, "size": size, "source": "supplementary",
                    "label": f"Supplementary file {name}" + (f" ({size})" if size else ""),
                    "likely": bool(COUNTISH.search(name))})
    out.sort(key=lambda o: (not o["likely"], o["file"]))
    return out


def inspect(gse: str) -> dict:
    gse = gse.strip().upper()
    if not GSE_RE.match(gse):
        raise GeoError("Enter a GEO series accession such as GSE60450.")
    series, samples = _matrix(gse)
    options = []
    ncbi = _ncbi_counts(gse)
    if ncbi:
        options.append({"id": "ncbi", "file": ncbi[0], "source": "ncbi", "likely": True,
                        "label": f"Raw counts computed by NCBI ({ncbi[1]}, Entrez gene IDs; recommended: one standard pipeline for all samples)"})
    options += _suppl_candidates(gse)
    strategies = sorted({s.get("library_strategy", "") for s in samples} - {""})
    organisms = sorted({s.get("organism_ch1", "") for s in samples} - {""})
    out = {
        "accession": gse, "title": series.get("title", ""), "summary": series.get("summary", ""),
        "overall_design": series.get("overall_design", ""), "organisms": organisms, "library_strategies": strategies,
        "n_samples": len(samples), "url": f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={gse}",
        "samples": [{"geo_accession": s.get("geo_accession"), "title": s.get("title"),
                     "characteristics": s.get("characteristics", {})} for s in samples[:200]],
        "counts_options": options,
        "recommended": options[0]["id"] if options else None,
        "warnings": [],
    }
    if strategies and "RNA-Seq" not in strategies:
        out["warnings"].append(f"The samples are {', '.join(strategies)}, not RNA-Seq; this site analyses bulk RNA-seq counts.")
    if not options:
        out["warnings"].append("No count table found: GEO has no NCBI-computed counts for this series and no supplementary file "
                               "that looks like a count table (per-sample files packed in GSE..._RAW.tar are not read yet). "
                               "Download the counts yourself and upload them.")
    (_cache(gse) / "inspect.json").write_text(json.dumps(out, indent=1))
    return out


# ---- Matching count columns to samples -----------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.lower())


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-zA-Z]+", s.lower()) if t}


def _sample_names(s: dict) -> list[str]:
    names = [s.get("geo_accession", ""), s.get("title", ""), s.get("source_name_ch1", ""), *s.get("description", [])]
    names += [r.rsplit("/", 1)[-1] for r in s.get("relation", []) if "SRA" in r]
    return [n for n in names if n]


def match_columns(columns: list[str], samples: list[dict]) -> dict[str, int]:
    """count column -> index of its GEO sample. Exact names first, then names the column starts with,
    then names whose words all appear in the column; a column or a sample is never used twice."""
    found: dict[str, int] = {}
    used: set[int] = set()
    strategies = [
        lambda col, name: _norm(col) == _norm(name),
        lambda col, name: len(_norm(name)) >= 3 and _norm(col).startswith(_norm(name)),
        lambda col, name: _tokens(name) and _tokens(name) <= _tokens(col),
    ]
    for test in strategies:
        for col in columns:
            if col in found:
                continue
            hits = {i for i, s in enumerate(samples) if i not in used and any(test(col, n) for n in _sample_names(s))}
            if len(hits) == 1:
                i = hits.pop()
                # the sample must not fit another unmatched column equally well
                rivals = [c for c in columns if c != col and c not in found and any(test(c, n) for n in _sample_names(samples[i]))]
                if not rivals:
                    found[col] = i
                    used.add(i)
    return found


def _sample_ids(samples: list[dict], idx: list[int]) -> list[str]:
    """Readable sample IDs: the GEO titles when they are unique, otherwise the GSM accessions."""
    titles = [re.sub(r"\s+", "_", (samples[i].get("title") or "").strip())[:50] for i in idx]
    if all(titles) and len(set(titles)) == len(titles):
        return titles
    return [samples[i].get("geo_accession") or f"sample{i + 1}" for i in idx]


def _sample_table(samples: list[dict], idx: list[int], ids: list[str]) -> tuple[list[str], list[list[str]]]:
    keys: list[str] = []
    for i in idx:
        for k in samples[i].get("characteristics", {}):
            if k not in keys:
                keys.append(k)
    header = ["sample_id", *keys, "title", "source_name", "geo_accession"]
    rows = [[sid, *[samples[i].get("characteristics", {}).get(k, "") for k in keys], samples[i].get("title", ""),
             samples[i].get("source_name_ch1", ""), samples[i].get("geo_accession", "")] for sid, i in zip(ids, idx)]
    # drop characteristics that are the same for every sample (they cannot be compared or adjusted for)
    keep = [j for j, h in enumerate(header) if h in ("sample_id", "title", "geo_accession") or len({r[j] for r in rows}) > 1]
    return [header[j] for j in keep], [[r[j] for j in keep] for r in rows]


# ---- Import -------------------------------------------------------------------------------------------------
def import_dataset(gse: str, option: str) -> dict:
    gse = gse.strip().upper()
    cached = _cache(gse) / "inspect.json"
    info = json.loads(cached.read_text()) if cached.exists() else inspect(gse)
    opt = next((o for o in info["counts_options"] if o["id"] == option), None)
    if opt is None:
        raise GeoError(f"'{option}' is not one of the count tables offered for {gse}.")
    _, samples = _matrix(gse)
    if opt["source"] == "ncbi":
        name, organism, data = _ncbi_counts(gse)
    else:
        name = opt["file"]
        data = _cached(gse, name, f"{_series_dir(gse)}/suppl/{name}")
        organism = None
    raw = _cache(gse) / name
    if not raw.exists():
        raw.write_bytes(data)
    header, rows = tables.read_table(raw)
    if len(header) < 3 or not rows:
        raise GeoError(f"{name} does not look like a count table (too few columns or rows).")
    found = match_columns(header[1:], samples)
    if not found:
        raise GeoError(f"The columns of {name} could not be matched to the samples of {gse} "
                       f"(columns like {', '.join(header[1:4])}). Download it and upload it with your own sample table.")
    matched_cols = [c for c in header[1:] if c in found]
    unmatched_samples = [samples[i].get("geo_accession") for i in range(len(samples)) if i not in found.values()]
    idx = [found[c] for c in matched_cols]
    ids = _sample_ids(samples, idx)
    col_pos = [header.index(c) for c in matched_cols]
    counts_out = io.StringIO()
    w = csv.writer(counts_out, delimiter="\t", lineterminator="\n")
    w.writerow([header[0] or "gene_id", *ids])
    for r in rows:
        w.writerow([r[0], *[r[p] if p < len(r) else "" for p in col_pos]])
    meta_header, meta_rows = _sample_table(samples, idx, ids)
    meta_out = io.StringIO()
    mw = csv.writer(meta_out, lineterminator="\n")
    mw.writerow(meta_header)
    mw.writerows(meta_rows)
    geo = {"accession": gse, "title": info["title"], "url": info["url"], "counts_file": name, "counts_source": opt["source"],
           "organism": organism or (info["organisms"][0] if len(info["organisms"]) == 1 else None),
           "n_samples_matched": len(ids), "samples_without_counts": unmatched_samples,
           "columns_left_out": [c for c in header[1:] if c not in found],
           "imported": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    rec = uploads.create((f"{gse}_counts.tsv", io.BytesIO(counts_out.getvalue().encode())),
                         (f"{gse}_samples.csv", io.BytesIO(meta_out.getvalue().encode())), source="geo", geo=geo)
    desc = describe_for_chat(info, geo)
    return {"upload_id": rec["id"], "geo": geo, "description": desc}


def describe_for_chat(info: dict, geo: dict) -> str:
    """The text handed to the setup assistant as the user's description of the experiment."""
    text = f"Public dataset {info['accession']} from GEO: {info['title']}."
    if info.get("overall_design"):
        text += f" Design: {info['overall_design']}"
    elif info.get("summary"):
        text += f" Summary: {info['summary']}"
    text = text[:1500]
    if geo.get("samples_without_counts"):
        text += f" ({len(geo['samples_without_counts'])} GEO samples have no counts in the chosen table and were left out.)"
    return text
