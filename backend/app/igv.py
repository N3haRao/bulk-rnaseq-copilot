"""IGV for the splicing results: the genome, the annotation, each sample's reads and the splice junctions per group.

Everything comes from the server, nothing from the internet (igv.js is bundled with the frontend):

  reference   the genome FASTA in REFS_DIR/<species> + its .fai index, built once here
              (data/igv/<species>/genome.fa.fai; the FASTA is read in pieces, never sent whole)
  annotation  transcripts from the reference GTF, indexed once per chromosome (data/igv/<species>/transcripts/)
              and sent for the region on screen only (the GTF is 1.5 GB)
  reads       BAM + .bai per sample: an analysis's own BAMs (MAJIQ), the Start-from-FASTQ job's star/bams/,
              or BAMs chosen on the server (data/jobs/<run>/igv.json); streamed with HTTP range requests
  junctions   STAR's SJ.out.tab per sample (next to the BAMs), summed per group for the region: one
              arc track per group, thickness = uniquely mapped reads (a sashimi-style view)

Files are served by a key registered for the run, never by a path from the browser.
"""

import csv
import gzip
import hashlib
import json
import re
import threading
from collections import OrderedDict
from pathlib import Path

from .config import settings

PAD_FRACTION, MIN_PAD = 0.3, 150
MAX_FEATURES = 3000
_jobs: dict[str, dict] = {}            # species -> {"status", "error"} of the index build
_lock = threading.Lock()


class IgvError(ValueError):
    """A problem in plain language."""


def igv_dir(species: str) -> Path:
    return settings.data_dir / "igv" / species


# ---- where an event is --------------------------------------------------------------------------
COORD = re.compile(r"(\d+)\s*-\s*(\d+)")


def event_span(row: dict) -> tuple[str, int, int] | None:
    """(chr, start, end) of a result row's event, 1-based; None when the row has no place in the genome.
    Reads chr + event_start/event_end (rMATS, SUPPA2), start/end, or the 'a-b' pairs in junction / coordinates / lsv_id."""
    chrom = row.get("chr") or row.get("seqid") or row.get("chromosome")
    if not chrom or str(chrom) in ("NA", "nan"):
        return None
    lo = hi = None
    for a, b in (("event_start", "event_end"), ("start", "end")):
        try:
            lo, hi = int(float(row[a])), int(float(row[b]))
            break
        except (KeyError, TypeError, ValueError):
            continue
    if lo is None:
        for k in ("junction", "coordinates", "lsv_id"):
            nums = [int(x) for pair in COORD.findall(str(row.get(k) or "")) for x in pair]
            if nums:
                lo, hi = min(nums), max(nums)
                break
    if lo is None:
        return None
    return str(chrom), min(lo, hi), max(lo, hi)


def locus_for(row: dict) -> str | None:
    """'chr:start-end' around a row's event, padded so its flanking exons show."""
    sp = event_span(row)
    if not sp:
        return None
    chrom, lo, hi = sp
    pad = max(MIN_PAD, int((hi - lo) * PAD_FRACTION))
    return f"{chrom}:{max(1, lo - pad)}-{hi + pad}"


# ---- reference: .fai and the transcript index, built once per species ------------------------------
def build_fai(fasta: Path, out: Path) -> None:
    """samtools-compatible FASTA index: name, length, offset, bases per line, bytes per line."""
    rows, name, length, offset, lb, lw, pos = [], None, 0, 0, 0, 0, 0
    with open(fasta, "rb") as f:
        for line in f:
            n = len(line)
            if line.startswith(b">"):
                if name is not None:
                    rows.append((name, length, offset, lb, lw))
                name = line[1:].split()[0].decode()
                length, lb, lw = 0, 0, 0
                offset = pos + n
            else:
                s = len(line.rstrip(b"\r\n"))
                if lb == 0:
                    lb, lw = s, n
                length += s
            pos += n
    if name is not None:
        rows.append((name, length, offset, lb, lw))
    tmp = out.with_suffix(".tmp")
    tmp.write_text("".join(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\t{r[4]}\n" for r in rows))
    tmp.replace(out)


ATTR = re.compile(r'(\w+) "([^"]*)"')


def build_transcripts(gtf: Path, out: Path) -> None:
    """One file per chromosome, <out>/<chr>.json.gz: [[start0, end, strand, transcript_id, gene_name, transcript_type, tag,
    "exon_start0-exon_end,..."], ...] sorted by start; <out>/done marks a finished index. Per chromosome, so the site only
    holds the chromosomes on screen in memory (all of GENCODE would be hundreds of MB)."""
    tx: dict[str, dict] = {}
    opener = gzip.open if gtf.name.endswith(".gz") else open
    with opener(gtf, "rt") as f:
        for line in f:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 9 or p[2] != "exon":
                continue
            a = dict(ATTR.findall(p[8]))
            tid = a.get("transcript_id")
            if not tid:
                continue
            t = tx.get(tid)
            if t is None:
                t = tx[tid] = {"chr": p[0], "strand": p[6], "name": tid, "gene": a.get("gene_name") or a.get("gene_id", ""),
                               "type": a.get("transcript_type") or a.get("transcript_biotype", ""),
                               "tags": "basic" if 'tag "basic"' in p[8] else "", "exons": []}
            t["exons"].append([int(p[3]) - 1, int(p[4])])
    by_chr: dict[str, list] = {}
    for t in tx.values():
        ex = sorted(t["exons"])
        by_chr.setdefault(t["chr"], []).append([ex[0][0], max(e[1] for e in ex), t["strand"], t["name"], t["gene"], t["type"],
                                                t["tags"], ",".join(f"{a}-{b}" for a, b in ex)])
    tx.clear()
    tmp = out.with_name(out.name + ".tmp")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for chrom, rows in by_chr.items():
        rows.sort()
        with gzip.open(tmp / f"{_safe(chrom)}.json.gz", "wt") as f:
            json.dump(rows, f, separators=(",", ":"))
    (tmp / "done").write_text(str(sum(len(v) for v in by_chr.values())))
    shutil.rmtree(out, ignore_errors=True)
    tmp.replace(out)


def _safe(chrom: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", chrom)


def reference_files(species: str) -> dict:
    from . import fastq
    if species not in fastq.REFERENCES:
        raise IgvError(f"No reference is set up for '{species}' (known: {', '.join(fastq.REFERENCES)}).")
    p = fastq._ref_paths(species)
    d = igv_dir(species)
    return {"fasta": p["genome_fasta"], "gtf": p["gtf"], "fai": d / "genome.fa.fai", "transcripts": d / "transcripts",
            "label": fastq.REFERENCES[species]["label"]}


def reference_status(species: str) -> dict:
    r = reference_files(species)
    missing = [str(r[k]) for k in ("fasta", "gtf") if not Path(r[k]).is_file()]
    ready = not missing and r["fai"].is_file() and (r["transcripts"] / "done").is_file()
    job = _jobs.get(species) or {}
    return {"species": species, "label": r["label"], "ready": ready, "missing": missing,
            "status": "ready" if ready else ("missing" if missing else job.get("status", "not_built")), "error": job.get("error")}


def prepare_reference(species: str, wait: bool = False) -> dict:
    """Build the .fai and the transcript index in the background (a few minutes, once per species)."""
    st = reference_status(species)
    if st["ready"] or st["missing"]:
        return st
    with _lock:
        if (_jobs.get(species) or {}).get("status") == "building":
            return reference_status(species)
        _jobs[species] = {"status": "building", "error": None}

    r = reference_files(species)
    args = [str(r["fasta"]), str(r["gtf"]), str(r["fai"]), str(r["transcripts"])]

    def work():
        try:
            if wait:
                _build(*args)
            else:                 # its own process: indexing GENCODE needs ~0.5 GB for a minute, given back afterwards
                import subprocess
                import sys
                p = subprocess.run([sys.executable, "-m", "app.igv", *args], cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True)
                if p.returncode != 0:
                    raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else f"exit {p.returncode}")
            _jobs[species] = {"status": "ready", "error": None}
        except Exception as e:  # noqa: BLE001 - shown on the page
            _jobs[species] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    if wait:
        work()
    else:
        threading.Thread(target=work, daemon=True, name=f"igv-ref-{species}").start()
    return reference_status(species)


def _build(fasta: str, gtf: str, fai: str, transcripts: str) -> None:
    Path(fai).parent.mkdir(parents=True, exist_ok=True)
    if not Path(fai).is_file():
        build_fai(Path(fasta), Path(fai))
    if not (Path(transcripts) / "done").is_file():
        build_transcripts(Path(gtf), Path(transcripts))


_tx_cache: "OrderedDict[str, list]" = OrderedDict()


def _transcripts(species: str, chrom: str) -> list:
    d = reference_files(species)["transcripts"]
    if not (d / "done").is_file():
        raise IgvError("The annotation index is not built yet.")
    path = d / f"{_safe(chrom)}.json.gz"
    if not path.is_file():
        return []
    key = f"{path}:{path.stat().st_mtime}"
    if key not in _tx_cache:
        with gzip.open(path, "rt") as f:
            _tx_cache[key] = json.load(f)
        while len(_tx_cache) > 4:            # a few chromosomes at a time
            _tx_cache.popitem(last=False)
    else:
        _tx_cache.move_to_end(key)
    return _tx_cache[key]


def features(species: str, chrom: str, start: int, end: int) -> list[dict]:
    """igv.js annotation features (transcripts with exons) overlapping the region."""
    rows = _transcripts(species, chrom)
    out = []
    for r in rows:               # sorted by start; transcripts are at most a few Mb long
        if r[0] > end:
            break
        if r[1] < start:
            continue
        out.append({"chr": chrom, "start": r[0], "end": r[1], "strand": r[2], "name": r[4] or r[3], "id": r[3],
                    "exons": [{"start": int(a), "end": int(b)} for a, b in (x.split("-") for x in r[7].split(","))],
                    "attributes": {"transcript_id": r[3], "gene_name": r[4], "transcript_type": r[5], "tag": r[6]}})
        if len(out) >= MAX_FEATURES:
            break
    return out


# ---- the run's samples, BAMs and junctions ---------------------------------------------------------
def _samples(run_dir: Path, condition: str) -> dict[str, str]:
    p = run_dir / "01_validate_inputs" / "metadata_validated.csv"
    if not p.exists():
        return {}
    with open(p, newline="") as f:
        return {r["sample_id"]: r.get(condition, "") for r in csv.DictReader(f)}


def _bams_in(folder: Path, samples: list[str]) -> dict[str, Path]:
    out = {}
    for s in samples:
        for c in (folder / f"{s}.bam", folder / s / "Aligned.sortedByCoord.out.bam"):
            if c.is_file() and _bai(c):
                out[s] = c.resolve()
                break
    return out


def _bai(bam: Path) -> Path | None:
    for c in (Path(str(bam) + ".bai"), bam.with_suffix(".bai")):
        if c.is_file():
            return c
    return None


def _sj_for(bam: Path, sample: str) -> Path | None:
    """STAR's SJ.out.tab for a sample: next to the original BAM, or in star/star/<sample>/ of a FASTQ job."""
    real = bam.resolve()
    for c in (real.parent / f"{sample}.SJ.out.tab", real.parent / "SJ.out.tab", real.parent.parent / "star" / sample / "SJ.out.tab",
              real.parent.parent / sample / "SJ.out.tab"):
        if c.is_file():
            return c
    return None


def _chosen_file(job_dir: Path) -> Path:
    return job_dir / "igv.json"


def bam_sources(job: dict, job_dir: Path, run_dir: Path) -> tuple[dict[str, Path], str | None]:
    """({sample: bam}, where they came from) for the run's samples: chosen on the server, an analysis's own BAMs
    (MAJIQ), or the Start-from-FASTQ job's star/bams/."""
    from . import fastq
    samples = list(_samples(run_dir, job["settings"]["condition"]))
    chosen = _chosen_file(job_dir)
    if chosen.exists():
        rec = json.loads(chosen.read_text())
        found = {s: Path(p) for s, p in (rec.get("bams") or {}).items() if Path(p).is_file()}
        if found:
            return found, "chosen on the server"
    for d in sorted((job_dir / "extras").glob("*/*/inputs/bams"), reverse=True):
        found = _bams_in(d, samples)
        if found:
            return found, f"the {d.parents[2].name} analysis"
    fq = fastq.source_for_run(job)
    if fq:
        res = fastq._job_dir(fq["job_id"]) / "results"
        found = _bams_in(res / "star" / "bams", samples)
        if found:
            return found, f"the Start-from-FASTQ job ({fq.get('folder') or fq['job_id']})"
    return {}, None


def choose_bams(job: dict, job_dir: Path, run_dir: Path, tokens: list[str]) -> dict:
    """BAMs (or folders of them) chosen in the server file browser, matched to the run's samples by file name."""
    from . import server_files
    samples = list(_samples(run_dir, job["settings"]["condition"]))
    files: list[Path] = []
    for t in tokens:
        try:
            p = server_files.resolve(t)
        except server_files.PickError as e:
            raise IgvError(str(e)) from e
        files += sorted(p.glob("*.bam")) + sorted(p.glob("*/Aligned.sortedByCoord.out.bam")) if p.is_dir() else [p]
    found: dict[str, str] = {}
    for f in files:
        name = f.parent.name if f.name == "Aligned.sortedByCoord.out.bam" else f.name[:-4]
        if name in samples and _bai(f):
            found[name] = str(f.resolve())
    if not found:
        raise IgvError("None of the chosen BAMs is named like a sample of this analysis (<sample>.bam) with its .bai index. "
                       f"The samples are: {', '.join(samples)}.")
    _chosen_file(job_dir).write_text(json.dumps({"bams": found}, indent=1))
    return {"matched": sorted(found), "not_found": [s for s in samples if s not in found]}


def _key(path: Path) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:16]


def view_config(job: dict, job_dir: Path, run_dir: Path) -> dict:
    """What the IGV page loads: the reference, groups with their samples' BAM keys, and which groups have junctions."""
    from . import fastq
    s = job["settings"]
    organism = (s.get("organism") or "").lower()
    species = organism if organism in fastq.REFERENCES else None
    fq = fastq.source_for_run(job)
    if not species and fq:
        species = fastq.fastq_jobs.load(fq["job_id"]).get("species")
    if species not in fastq.REFERENCES:
        raise IgvError("IGV needs to know the organism of this analysis (human or mouse) to load its genome.")
    ref = prepare_reference(species)
    groups_of = _samples(run_dir, s["condition"])
    bams, source = bam_sources(job, job_dir, run_dir)
    files: dict[str, str] = {}
    order = [s["test_level"], s["reference_level"]]
    groups = []
    for g in order:
        members = sorted(x for x, grp in groups_of.items() if grp == g)
        tracks = []
        for x in members:
            if x in bams:
                b, i = bams[x], _bai(bams[x])
                files[_key(b)], files[_key(i)] = str(b), str(i)
                tracks.append({"sample": x, "bam": _key(b), "bai": _key(i), "junctions": _sj_for(b, x) is not None})
        groups.append({"name": g, "role": "test" if g == s["test_level"] else "reference", "samples": members, "tracks": tracks,
                       "junctions": any(t["junctions"] for t in tracks)})
    r = reference_files(species)
    files["fasta"], files["fai"] = str(r["fasta"]), str(r["fai"])
    (job_dir / "igv_files.json").write_text(json.dumps(files))
    return {"species": species, "reference": ref, "groups": groups, "bam_source": source,
            "missing_bams": sorted(x for g in groups for x in g["samples"] if x not in bams)}


def served_file(job_dir: Path, key: str) -> Path:
    reg = job_dir / "igv_files.json"
    files = json.loads(reg.read_text()) if reg.exists() else {}
    if key not in files:
        raise KeyError(key)
    return Path(files[key])


_sj_cache: "OrderedDict[tuple, list]" = OrderedDict()


def _sj_rows(path: Path, chrom: str) -> list[tuple]:
    k = (str(path), path.stat().st_mtime, chrom)
    if k not in _sj_cache:
        rows = []
        with open(path) as f:
            for line in f:
                p = line.split("\t", 1)
                if p[0] != chrom:
                    continue
                c = line.rstrip("\n").split("\t")
                rows.append((int(c[1]) - 1, int(c[2]), c[3], c[4], c[5], int(c[6]), int(c[7])))
        _sj_cache[k] = rows
        while len(_sj_cache) > 64:
            _sj_cache.popitem(last=False)
    return _sj_cache[k]


STRAND = {"1": "+", "2": "-", "0": "."}
MOTIF = {"0": "non-canonical", "1": "GT/AG", "2": "CT/AC", "3": "GC/AG", "4": "CT/GC", "5": "AT/AC", "6": "GT/AT"}


def junctions(job: dict, job_dir: Path, run_dir: Path, group: str, chrom: str, start: int, end: int, min_reads: int = 2) -> list[dict]:
    """One group's splice junctions in the region, summed over its samples (igv.js junction features)."""
    bams, _ = bam_sources(job, job_dir, run_dir)
    members = [x for x, g in _samples(run_dir, job["settings"]["condition"]).items() if g == group and x in bams]
    sjs = [p for p in (_sj_for(bams[x], x) for x in members) if p]
    acc: dict[tuple, list] = {}
    for p in sjs:
        for a, b, strand, motif, annot, uniq, multi in _sj_rows(p, chrom):
            if b < start or a > end:
                continue
            v = acc.setdefault((a, b, strand), [motif, annot, 0, 0, 0])
            v[2] += uniq
            v[3] += multi
            v[4] += 1 if uniq + multi else 0
    out = []
    for (a, b, strand), (motif, annot, uniq, multi, n) in sorted(acc.items()):
        if uniq + multi < min_reads:
            continue
        out.append({"chr": chrom, "start": a, "end": b, "strand": STRAND.get(strand, "."), "name": f"{uniq} reads",
                    "score": uniq, "attributes": {"uniquely_mapped": str(uniq), "multi_mapped": str(multi),
                                                  "motif": MOTIF.get(motif, motif), "annotated_junction": "true" if annot == "1" else "false",
                                                  "num_samples_with_this_junction": str(n), "num_samples_total": str(len(sjs)),
                                                  "group": group}})
    return out


if __name__ == "__main__":          # python -m app.igv <fasta> <gtf> <fai out> <transcripts dir out>
    import sys
    _build(*sys.argv[1:5])


# ---- sashimi plots (exportable): coverage + junction arcs with read counts + transcripts ----------------
MAX_SASHIMI_BP = 1_000_000
LOCUS = re.compile(r"^([^:\s]+):(\d[\d,]*)-(\d[\d,]*)$")
COLOURS = {"test": "#c0392b", "reference": "#1f6fb2"}


def parse_locus(locus: str) -> tuple[str, int, int]:
    m = LOCUS.match((locus or "").strip())
    if not m:
        raise IgvError(f"'{locus}' is not a region like chr1:1000-2000.")
    a, b = int(m.group(2).replace(",", "")), int(m.group(3).replace(",", ""))
    if b <= a:
        raise IgvError("The region's end must be after its start.")
    if b - a > MAX_SASHIMI_BP:
        raise IgvError(f"Zoom in: a sashimi plot covers at most {MAX_SASHIMI_BP // 1000} kb.")
    return m.group(1), a, b


def samtools() -> str:
    import os
    import shutil
    for c in (os.environ.get("SAMTOOLS"), shutil.which("samtools"), "/opt/conda/envs/tools/bin/samtools"):
        if c and Path(c).exists():
            return c
    raise IgvError("samtools was not found (set SAMTOOLS); it is needed to read the coverage for sashimi plots.")


def coverage(bam: Path, chrom: str, start: int, end: int, nbins: int) -> list[float]:
    """Mean read depth in `nbins` equal bins of [start, end] (1-based, inclusive), from samtools depth."""
    import subprocess
    p = subprocess.run([samtools(), "depth", "-a", "-Q", "0", "-r", f"{chrom}:{start}-{end}", str(bam)],
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise IgvError(f"samtools depth failed for {bam.name}: {p.stderr.strip()[:300]}")
    width = (end - start + 1) / nbins
    sums, counts = [0.0] * nbins, [0] * nbins
    for line in p.stdout.splitlines():
        c = line.split("\t")
        if len(c) < 3:
            continue
        i = min(nbins - 1, int((int(c[1]) - start) / width))
        if i >= 0:
            sums[i] += int(c[2])
            counts[i] += 1
    return [s / n if n else 0.0 for s, n in zip(sums, counts)]


def _sample_junctions(sj: Path | None, chrom: str, start: int, end: int) -> dict[tuple[int, int], int]:
    if sj is None:
        return {}
    out: dict[tuple[int, int], int] = {}
    for a, b, _strand, _motif, _annot, uniq, _multi in _sj_rows(sj, chrom):
        if b >= start and a <= end and uniq:
            out[(a, b)] = out.get((a, b), 0) + uniq
    return out


def sashimi(job: dict, job_dir: Path, run_dir: Path, locus: str, mode: str = "sample", min_reads: int = 5,
            fmt: str = "svg", event: str | None = None, title: str | None = None) -> bytes:
    """A sashimi plot of the region: one row per sample (or per group, averaged), coverage as a filled area, splice
    junctions as arcs labelled with their uniquely mapped reads, the event shaded, and the transcripts below."""
    import io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import PathPatch, Rectangle
    from matplotlib.path import Path as MPath

    if fmt not in ("svg", "png", "pdf"):
        raise IgvError("Format must be svg, png or pdf.")
    chrom, start, end = parse_locus(locus)
    cfg = view_config(job, job_dir, run_dir)
    bams, _ = bam_sources(job, job_dir, run_dir)
    nbins = 600
    xs = [start + (i + 0.5) * (end - start + 1) / nbins for i in range(nbins)]
    rows = []                                   # (label, colour, coverage, {(a, b): reads})
    for g in cfg["groups"]:
        members = [t["sample"] for t in g["tracks"]]
        per = [(x, coverage(bams[x], chrom, start, end, nbins), _sample_junctions(_sj_for(bams[x], x), chrom, start, end))
               for x in members]
        if not per:
            continue
        if mode == "group":
            cov = [sum(p[1][i] for p in per) / len(per) for i in range(nbins)]
            keys = set().union(*(p[2] for p in per))
            jn = {k: sum(p[2].get(k, 0) for p in per) / len(per) for k in keys}
            rows.append((f"{g['name']} (mean of {len(per)})", COLOURS[g["role"]], cov, jn))
        else:
            rows += [(f"{g['name']} · {x}", COLOURS[g["role"]], c, j) for x, c, j in per]
    if not rows:
        raise IgvError("No BAM files for this analysis, so there is nothing to plot. Choose BAMs on the IGV page.")
    tx = []
    try:
        tx = features(cfg["species"], chrom, start, end)
    except IgvError:
        pass
    tx = sorted(tx, key=lambda t: (t["attributes"].get("tag") != "basic", t["start"]))[:8]

    n = len(rows)
    tx_h = 0.32 * max(1, len(tx)) + 0.3
    # a fixed layout (no "tight" bounding box): every plot has the same width and margins, whatever is drawn
    height = 1.25 * n + tx_h + 1.4
    fig = plt.figure(figsize=(10, height))
    hr = [1.25] * n + [tx_h]
    gs = fig.add_gridspec(n + 1, 1, height_ratios=hr, hspace=0.12, left=0.075, right=0.79,
                          top=1 - 0.75 / height, bottom=0.55 / height)
    ev = None
    if event:
        try:
            ev = parse_locus(event)
        except IgvError:
            ev = None
    for r, (label, colour, cov, jn) in enumerate(rows):
        ax = fig.add_subplot(gs[r])
        top = max(cov) if cov else 0
        ymax = max(top, 1.0)
        ax.fill_between(xs, cov, step="mid", color=colour, alpha=0.75, linewidth=0)
        kept = sorted(((k, v) for k, v in jn.items() if v >= min_reads), key=lambda kv: kv[0])
        vmax = max([v for _, v in kept] or [1])

        def depth_at(pos):
            i = min(nbins - 1, max(0, int((pos - start) / (end - start + 1) * nbins)))
            return cov[i]
        span = end - start
        labels = []                              # (x, y, text, reads): placed afterwards, biggest first
        for i, ((a, b), v) in enumerate(kept):
            below = i % 2 == 1                   # alternate above / below, as in sashimi plots
            ya = depth_at(a) if a >= start else 0.0          # an end outside the region starts at 0
            yb = depth_at(b + 1) if b <= end else 0.0
            h = ymax * (0.35 + 0.35 * (v / vmax))
            mid_y = (-h if below else max(ya, yb) + h)
            y0, y1 = (0, 0) if below else (ya, yb)
            path = MPath([(a, y0), ((a + b) / 2, mid_y), (b, y1)], [MPath.MOVETO, MPath.CURVE3, MPath.CURVE3])
            lw = 0.6 + 2.4 * (v / vmax)
            partial = a < start or b > end          # continues outside the view: dashed
            ax.add_patch(PathPatch(path, facecolor="none", edgecolor=colour, lw=lw, clip_on=True,   # stays in the region
                                   linestyle=(0, (4, 2)) if partial else "-", alpha=0.75 if partial else 1))
            # label on the visible part of the arc: x(t) is linear on this curve, so t follows from x
            xc = min(max((a + b) / 2, start + 0.03 * span), end - 0.03 * span)
            t = min(1.0, max(0.0, (xc - a) / max(1, b - a)))
            yc = (1 - t) ** 2 * y0 + 2 * t * (1 - t) * mid_y + t ** 2 * y1
            labels.append((xc, yc, f"{v:.0f}" if v >= 10 or float(v).is_integer() else f"{v:.1f}", v))
        placed: list[tuple[float, float]] = []
        for xc, yc, lab, _v in sorted(labels, key=lambda x: -x[3]):
            # skip a label that would sit on top of a bigger one (crowded regions stay readable)
            if any(abs(xc - px) < 0.035 * span and abs(yc - py) < 0.16 * ymax * 2.6 for px, py in placed):
                continue
            placed.append((xc, yc))
            ax.text(xc, yc, lab, ha="center", va="center", fontsize=7.5, color="#222", clip_on=True,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85))
        ax.set_xlim(start, end)
        ax.set_ylim(-ymax * 0.75, ymax * 1.85)
        ax.set_yticks([0, round(top)] if top >= 1 else [0])
        ax.tick_params(axis="y", labelsize=7)
        ax.set_xticks([])
        for sp in ("top", "right", "bottom"):
            ax.spines[sp].set_visible(False)
        ax.axhline(0, color="#999", lw=0.5)
        ax.text(0.005, 0.97, label, transform=ax.transAxes, fontsize=8.5, va="top", color=colour, fontweight="bold")
        if r == 0:
            unit = "mean reads per sample" if mode == "group" else "reads"
            ax.set_title((title + "\n" if title else "") + f"{chrom}:{start:,}-{end:,}   ·   arcs: junction {unit} "
                         f"(uniquely mapped, ≥ {min_reads})", fontsize=10, pad=10)
        if ev and ev[0] == chrom:
            ax.axvspan(ev[1], ev[2], color="#ffc400", alpha=0.12, lw=0)
    ax = fig.add_subplot(gs[n])
    for i, t in enumerate(tx):
        y = -i
        ax.plot([max(t["start"], start), min(t["end"], end)], [y, y], color="#444", lw=0.8)
        for e in t["exons"]:
            if e["end"] >= start and e["start"] <= end:
                ax.add_patch(Rectangle((e["start"], y - 0.3), e["end"] - e["start"], 0.6, color="#333"))
        name = f"{t['name']} ({t['id']})"
        ax.text(end, y, f"  {name[:34] + '…' if len(name) > 35 else name} {t['strand']}", fontsize=7, va="center", ha="left",
                clip_on=False)
    if not tx:
        ax.text(0.5, 0.5, "(no annotated transcripts here)", transform=ax.transAxes, ha="center", fontsize=8, color="#777")
    if ev and ev[0] == chrom:
        ax.axvspan(ev[1], ev[2], color="#ffc400", alpha=0.12, lw=0)
    ax.set_xlim(start, end)
    ax.set_ylim(-max(1, len(tx)) + 0.4, 0.7)
    ax.set_yticks([])
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.ticklabel_format(axis="x", style="plain", useOffset=False)
    ax.tick_params(axis="x", labelsize=7.5)
    ax.set_xlabel(f"{chrom} ({cfg['reference']['label']})", fontsize=8)
    buf = io.BytesIO()
    fig.text(0.075, 0.12 / height, "Dashed arcs: junctions that continue outside this region.", fontsize=7, color="#666")
    fig.savefig(buf, format=fmt, dpi=200 if fmt == "png" else None)
    plt.close(fig)
    return buf.getvalue()
