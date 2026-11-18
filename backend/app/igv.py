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
