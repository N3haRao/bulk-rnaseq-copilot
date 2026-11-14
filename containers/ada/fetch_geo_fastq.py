#!/usr/bin/env python3
"""Download the raw FASTQ files of a public GEO series (GSE...) into the site's incoming folder.

    fetch_geo_fastq.py GSE314221                 # list the samples, the files and their size (nothing downloaded)
    fetch_geo_fastq.py GSE314221 --download      # download into $COPILOT_HOME/data/incoming/GSE314221/
    fetch_geo_fastq.py GSE314221 --download --only GSM1234567,GSM1234568 --folder my_name

Looks the series up at NCBI (its SRA runs and BioProject), then takes the FASTQ files and their MD5 sums from ENA,
which mirrors SRA as gzipped FASTQ. Each file is named after what differs between the
samples in GEO (their "characteristics", e.g. genotype and treatment) plus a replicate number, such as
APP-CETPtg_evacetrapib_1_R1.fastq.gz, so the "From FASTQ" page finds the samples and suggests the groups. With
--name-by title the GEO sample titles are used instead. geo_samples.tsv in the folder lists every sample's GEO details. Runs of the same sample are joined. Downloads
resume if interrupted (run the same command again); every file is checked against its MD5 sum.
Python 3 standard library + curl only (both on fuji); no SRA tools needed.
"""
import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
GEO = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"
ENA = "https://www.ebi.ac.uk/ena/portal/api/filereport"
ENA_FIELDS = "run_accession,sample_alias,sample_title,experiment_title,library_layout,read_count,fastq_ftp,fastq_md5,fastq_bytes"


def get(url: str, params: dict, tries: int = 4) -> str:
    full = url + "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(full, headers={"User-Agent": "rnaseq-site-fetch"}),
                                        timeout=120) as r:
                return r.read().decode()
        except Exception as e:  # noqa: BLE001 - network: retry, then explain
            if i == tries - 1:
                sys.exit(f"Could not reach {url} ({e}). Check the internet connection and try again.")
            time.sleep(2 ** (i + 1))
    return ""


def geo_samples(gse: str) -> dict:
    """GSM -> title, from the GEO DataSets summary."""
    ids = json.loads(get(f"{EUTILS}/esearch.fcgi", {"db": "gds", "term": f"{gse}[ACCN] AND gse[ETYP]",
                                                     "retmode": "json"}))["esearchresult"]["idlist"]
    if not ids:
        sys.exit(f"{gse} was not found in GEO (it may still be private: GEO keeps a series hidden until its paper "
                 "is published or the authors release it).")
    summ = json.loads(get(f"{EUTILS}/esummary.fcgi", {"db": "gds", "id": ids[0], "retmode": "json"}))["result"][ids[0]]
    out = {"title": summ.get("title", ""), "taxon": summ.get("taxon", ""), "samples": {}}
    for s in summ.get("samples", []):
        out["samples"][s["accession"]] = s.get("title", s["accession"])
    return out


def geo_characteristics(gse: str) -> dict:
    """GSM -> {field: value} from the series' SOFT text ("genotype: APPtg/CETPtg"); {} if GEO does not answer."""
    try:
        text = get(GEO, {"acc": gse, "targ": "gsm", "form": "text", "view": "brief"}, tries=2)
    except SystemExit:
        return {}
    out: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("^SAMPLE"):
            cur = out.setdefault(line.split("=", 1)[1].strip(), {})
        elif cur is not None and line.startswith("!Sample_characteristics_ch1"):
            v = line.split("=", 1)[1].strip()
            k, _, val = v.partition(":")
            if not val:
                k, val = f"field{len(cur) + 1}", k
            cur[k.strip().lower()] = val.strip()
    return out


def design_names(chars: dict, gsms: list[str]) -> dict:
    """GSM -> name from the characteristics that differ between samples (not per-sample IDs) + a replicate number."""
    fields = []
    for k in dict.fromkeys(k for g in gsms for k in chars.get(g, {})):
        vals = [chars.get(g, {}).get(k, "") for g in gsms]
        if 1 < len(set(vals)) < len(vals):
            fields.append(k)
    if not fields:
        return {}
    labels = {g: "_".join(safe(chars.get(g, {}).get(k, "NA"))[:24] for k in fields) for g in gsms}
    seen: dict[str, int] = {}
    out = {}
    for g in sorted(gsms):
        seen[labels[g]] = seen.get(labels[g], 0) + 1
        out[g] = f"{labels[g]}_{seen[labels[g]]}"
    return out


def sra_bioprojects(gse: str) -> list[str]:
    ids = json.loads(get(f"{EUTILS}/esearch.fcgi", {"db": "sra", "term": gse, "retmax": 1000,
                                                     "retmode": "json"}))["esearchresult"]["idlist"]
    if not ids:
        return []
    text = get(f"{EUTILS}/efetch.fcgi", {"db": "sra", "id": ",".join(ids[:500]), "rettype": "runinfo", "retmode": "text"})
    rows = list(csv.DictReader(io.StringIO(text)))
    return sorted({r["BioProject"] for r in rows if r.get("BioProject")})


def ena_runs(project: str) -> list[dict]:
    text = get(ENA, {"accession": project, "result": "read_run", "fields": ENA_FIELDS, "format": "tsv"})
    return list(csv.DictReader(io.StringIO(text), delimiter="\t")) if text.strip() else []


def safe(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9-]+", "_", name).strip("_")
    return n or "sample"


def plan(gse: str, only: set[str] | None, name_by: str = "design") -> tuple[dict, list[dict]]:
    geo = geo_samples(gse)
    geo["chars"] = geo_characteristics(gse)
    projects = sra_bioprojects(gse)
    if not projects:
        sys.exit(f"{gse} has no SRA runs (no raw reads were deposited, or they are not public yet).")
    runs = [r for p in projects for r in ena_runs(p)]
    gsms = set(geo["samples"])
    runs = [r for r in runs if r["sample_alias"] in gsms or not gsms]   # a BioProject can hold other series too
    if only:
        runs = [r for r in runs if r["sample_alias"] in only or r["run_accession"] in only]
    by: dict[str, dict] = {}
    for r in sorted(runs, key=lambda x: x["run_accession"]):
        gsm = r["sample_alias"]
        title = geo["samples"].get(gsm) or r.get("sample_title") or gsm
        s = by.setdefault(gsm, {"gsm": gsm, "title": title, "name": safe(title), "runs": [], "layout": r["library_layout"],
                                "bytes": 0, "reads": 0})
        urls = [u for u in r["fastq_ftp"].split(";") if u]
        md5s = r["fastq_md5"].split(";")
        sizes = [int(x or 0) for x in r["fastq_bytes"].split(";")]
        # paired runs at ENA: run_1.fastq.gz + run_2.fastq.gz (+ sometimes run.fastq.gz with unpaired reads: skipped)
        files = {}
        for u, m, b in zip(urls, md5s, sizes):
            mate = re.search(r"_([12])\.fastq\.gz$", u)
            if r["library_layout"] == "PAIRED" and not mate:
                continue
            files["R" + (mate.group(1) if mate else "1")] = {"url": "https://" + u, "md5": m, "bytes": b}
        s["runs"].append({"run": r["run_accession"], "files": files, "reads": int(r.get("read_count") or 0)})
        s["bytes"] += sum(f["bytes"] for f in files.values())
        s["reads"] += int(r.get("read_count") or 0)
    if name_by == "design":
        dn = design_names(geo["chars"], sorted(by))
        for g, s in by.items():
            s["name"] = dn.get(g, s["name"])
    names = [s["name"] for s in by.values()]
    for s in by.values():   # two titles that clean up to the same name: add the GSM
        if names.count(s["name"]) > 1:
            s["name"] = f"{s['name']}_{s['gsm']}"
    return geo, sorted(by.values(), key=lambda s: s["name"])


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


CURL_CODES = {6: "host not found", 7: "could not connect", 18: "connection closed early", 22: "server refused (HTTP error)",
              28: "too slow / timed out", 33: "server cannot resume", 35: "TLS error", 52: "empty reply",
              56: "connection reset", 92: "HTTP/2 stream error"}


def fetch(url: str, dest: Path, want_md5: str, quiet: bool = False, tries: int = 10) -> None:
    """curl with resume. A connection that slows to a crawl (< 300 kB/s for 60 s) is dropped and reopened where it
    stopped; attempts that made progress are not counted as failures. Failures alternate between ENA's HTTPS and FTP
    servers and wait longer each time (30 s up to 5 min). A finished file with the wrong MD5 is downloaded again
    from the start (no resume)."""
    if dest.exists() and md5(dest) == want_md5:
        return
    part = dest.with_name(dest.name + ".part")
    urls = [url, "ftp://" + url.split("://", 1)[1]]
    failures, resume = 0, True
    while failures < tries:
        u = urls[failures % 2]
        before = part.stat().st_size if part.exists() else 0
        if not resume:
            part.unlink(missing_ok=True)
            before, resume = 0, True
        rc = subprocess.call(["curl", "-fL", "--retry", "3", "--retry-delay", "15", "--speed-limit", "300000",
                              "--speed-time", "60", "-C", "-", "-o", str(part), u] + (["-sS"] if quiet else []))
        if rc == 0 and md5(part) == want_md5:
            part.rename(dest)
            return
        if rc == 0:
            why, resume = "finished but the MD5 sum is wrong; starting this file again", False
        elif (part.stat().st_size if part.exists() else 0) > before:
            print(f"   {dest.name}: connection dropped ({CURL_CODES.get(rc, f'curl {rc}')}), resuming", flush=True)
            continue
        else:
            why = CURL_CODES.get(rc, f"curl error {rc}")
            if rc == 33:
                resume = False
        failures += 1
        wait = min(300, 30 * 2 ** (failures - 1))
        print(f"   {dest.name}: {why} [{u.split(':')[0]}]; retry {failures}/{tries} in {wait} s", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"could not download {url} correctly")


def sra_tool(name: str) -> str | None:
    """fasterq-dump / prefetch / pigz from PATH, $SRA_TOOLS_DIR or $COPILOT_HOME/envs/sra/bin."""
    for d in [os.environ.get("SRA_TOOLS_DIR"), Path(os.environ.get("COPILOT_HOME", Path.home() / "rnaseq-copilot")) / "envs/sra/bin"]:
        if d and (Path(d) / name).is_file():
            return str(Path(d) / name)
    return shutil.which(name)


def fetch_ncbi(s: dict, out: Path, tmp: Path, threads: int) -> list[str]:
    """One sample from NCBI SRA: prefetch + fasterq-dump per run, gzip, join the runs. Checked by read count
    (NCBI's FASTQ is the same reads as ENA's but not the same .gz bytes, so there is no MD5 to compare)."""
    prefetch, fqd, pigz = sra_tool("prefetch"), sra_tool("fasterq-dump"), sra_tool("pigz")
    mates = sorted({m for r in s["runs"] for m in r["files"]})
    parts: dict[str, list[Path]] = {m: [] for m in mates}
    for r in s["runs"]:
        acc, d = r["run"], tmp / r["run"]
        d.mkdir(parents=True, exist_ok=True)
        done_flag = d / "ok"
        if not done_flag.exists():
            for f in d.glob("*.fastq*"):
                f.unlink()
            for i in range(4):
                rc = subprocess.call([prefetch, acc, "-O", str(d), "--max-size", "500g"])
                if rc == 0:
                    break
                print(f"   {acc}: prefetch failed (exit {rc}); retry {i + 1}/4 in 60 s", flush=True)
                time.sleep(60)
            else:
                raise RuntimeError(f"prefetch {acc} failed")
            sra = next(d.rglob(f"{acc}.sra*"), None)
            rc = subprocess.call([fqd, str(sra or acc), "--split-3", "--threads", str(threads), "--skip-technical",
                                  "-O", str(d), "-t", str(d / "fqd_tmp")])
            if rc != 0:
                raise RuntimeError(f"fasterq-dump {acc} failed (exit {rc})")
            want = r.get("reads") or 0
            for m in mates:
                fq = d / (f"{acc}_{m[1]}.fastq" if "R2" in mates else f"{acc}.fastq")
                if not fq.exists() and m == "R1":
                    fq = d / f"{acc}_1.fastq"
                if not fq.exists():
                    raise RuntimeError(f"fasterq-dump made no {fq.name}")
                with open(fq, "rb") as fh:
                    n = sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 24), b"")) // 4
                if want and n != want:
                    raise RuntimeError(f"{fq.name}: {n} reads, expected {want}")
                if pigz:
                    subprocess.check_call([pigz, "-p", str(threads), "-f", str(fq)])
                else:
                    subprocess.check_call(["gzip", "-f", str(fq)])
            for sra_file in d.rglob("*.sra*"):
                if sra_file.is_file():
                    sra_file.unlink()
            done_flag.write_text("ok")
        for m in mates:
            gz = d / (f"{acc}_{m[1]}.fastq.gz" if "R2" in mates else f"{acc}.fastq.gz")
            if not gz.exists() and m == "R1":
                gz = d / f"{acc}_1.fastq.gz"
            parts[m].append(gz)
    names = []
    for m in mates:
        final = out / f"{s['name']}_{m}.fastq.gz"
        tmpf = final.with_name(final.name + ".part")
        with open(tmpf, "wb") as w:
            for p in parts[m]:
                with open(p, "rb") as r_:
                    shutil.copyfileobj(r_, w, 1 << 22)
        tmpf.rename(final)
        names.append(final.name)
    for r in s["runs"]:
        shutil.rmtree(tmp / r["run"], ignore_errors=True)
    return names


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gse")
    ap.add_argument("--download", action="store_true", help="download (default: only list)")
    ap.add_argument("--only", default="", help="comma-separated GSM or SRR/ERR accessions to keep")
    ap.add_argument("--folder", default=None, help="folder name under the incoming folder (default: the GSE)")
    ap.add_argument("--parallel", type=int, default=4, help="files downloaded at the same time (default 4)")
    ap.add_argument("--source", choices=["ena", "ncbi"], default="ena",
                    help="ena: FASTQ files from ENA (default). ncbi: NCBI SRA with prefetch + fasterq-dump (sra-tools), "
                         "for when ENA's download server is unreachable")
    ap.add_argument("--name-by", choices=["design", "title"], default="design",
                    help="file names from the GEO characteristics (default) or the GEO sample titles")
    ap.add_argument("--incoming", default=None, help="default: $FASTQ_INCOMING_DIR or $COPILOT_HOME/data/incoming")
    a = ap.parse_args()
    gse = a.gse.strip().upper()
    if not re.fullmatch(r"GSE\d+", gse):
        sys.exit("Give a GEO series number, e.g. GSE314221.")
    only = {x.strip() for x in a.only.split(",") if x.strip()} or None
    geo, samples = plan(gse, only, a.name_by)
    if not samples:
        sys.exit(f"ENA has no FASTQ files for {gse} yet (ENA mirrors new SRA data within a few days), "
                 "or none match --only.")
    total = sum(s["bytes"] for s in samples)
    print(f"{gse}: {geo['title']}\nOrganism: {geo['taxon']}\n")
    print(f"{'sample (file name)':40} {'GSM':12} {'title':10} {'layout':7} {'runs':>4} {'M reads':>8} {'GB':>6}")
    for s in samples:
        print(f"{s['name'][:40]:40} {s['gsm']:12} {s['title'][:10]:10} {s['layout'][:6]:7} {len(s['runs']):>4} "
              f"{s['reads'] / 1e6:>8.1f} {s['bytes'] / 1e9:>6.1f}")
    fields = list(dict.fromkeys(k for s in samples for k in geo["chars"].get(s["gsm"], {})))
    if fields:
        print("\nGEO characteristics:")
        for s in samples:
            c = geo["chars"].get(s["gsm"], {})
            print(f"  {s['name'][:40]:40} " + "; ".join(f"{k}: {c.get(k, '')}" for k in fields))
    print(f"\n{len(samples)} samples, {total / 1e9:.1f} GB of FASTQ to download.")
    missing = [g for g in geo["samples"] if g not in {s["gsm"] for s in samples}]
    if missing and not only:
        print(f"Not at ENA (or not sequencing runs): {', '.join(missing)}")
    if not a.download:
        print("\nNothing downloaded. Add --download to fetch them (and --only GSM...,GSM... to take some samples).")
        return

    incoming = Path(a.incoming or os.environ.get("FASTQ_INCOMING_DIR")
                    or Path(os.environ.get("COPILOT_HOME", Path.home() / "rnaseq-copilot")) / "data" / "incoming")
    out = incoming / (a.folder or gse)
    out.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(out).free
    if free < total * 1.1:
        sys.exit(f"Only {free / 1e9:.0f} GB free in {out}; {total / 1e9:.0f} GB needed.")
    tmp = incoming / f".download_{out.name}"   # hidden from the site while it fills
    tmp.mkdir(parents=True, exist_ok=True)
    if a.source == "ncbi":
        if not (sra_tool("prefetch") and sra_tool("fasterq-dump")):
            sys.exit("--source ncbi needs sra-tools (prefetch, fasterq-dump): on PATH, in $SRA_TOOLS_DIR, or in "
                     "$COPILOT_HOME/envs/sra/bin (see docs/fuji_setup_commands.sh, step 7).")
        todo = [s for s in samples if any(not (out / f"{s['name']}_{m}.fastq.gz").exists()
                                          for m in {m for r in s["runs"] for m in r["files"]})]
        print(f"From NCBI: {len(todo)} samples left ({len(samples) - len(todo)} already here).", flush=True)
        threads = max(2, (os.cpu_count() or 8) // max(1, a.parallel) // 2)
        failed = []
        with concurrent.futures.ThreadPoolExecutor(max(1, a.parallel)) as ex:
            futs = {ex.submit(fetch_ncbi, s, out, tmp, threads): s for s in todo}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    print(f"   done {', '.join(fut.result())}", flush=True)
                except Exception as e:  # noqa: BLE001 - report all failures at the end
                    failed.append(f"{futs[fut]['name']}: {e}")
        if failed:
            sys.exit("Some samples did not download:\n  " + "\n  ".join(failed) + "\nRun the same command again.")
        jobs = []
    else:
        jobs = None
    jobs = jobs if jobs is not None else [(k, s, mate) for k, s in enumerate(samples, 1) for mate in sorted({m for r in s["runs"] for m in r["files"]})
            if not (out / f"{s['name']}_{mate}.fastq.gz").exists()]
    quiet = a.parallel > 1

    def one(k: int, s: dict, mate: str) -> str:
        final = out / f"{s['name']}_{mate}.fastq.gz"
        print(f"[{k}/{len(samples)}] {final.name}: downloading", flush=True)
        parts = []
        for r in s["runs"]:
            f = r["files"][mate]
            p = tmp / f"{r['run']}_{mate}.fastq.gz"
            fetch(f["url"], p, f["md5"], quiet)
            parts.append(p)
        if len(parts) == 1:
            parts[0].rename(final)
        else:                                   # gzip files can simply be joined
            with open(final.with_name(final.name + ".part"), "wb") as w:
                for p in parts:
                    with open(p, "rb") as r_:
                        shutil.copyfileobj(r_, w, 1 << 22)
            final.with_name(final.name + ".part").rename(final)
            for p in parts:
                p.unlink()
        return final.name

    failed, done = [], len({(s["name"], m) for s in samples for r in s["runs"] for m in r["files"]}) - len(jobs)
    total_files = done + len(jobs)
    with concurrent.futures.ThreadPoolExecutor(max(1, a.parallel)) as ex:
        futs = {ex.submit(one, *j): j for j in jobs}
        for fut in concurrent.futures.as_completed(futs):
            try:
                name = fut.result()
                done += 1
                print(f"   done {name} ({done}/{total_files} files)", flush=True)
            except Exception as e:  # noqa: BLE001 - report all failures at the end
                failed.append(f"{futs[fut][1]['name']}_{futs[fut][2]}: {e}")
    if failed:
        sys.exit("Some files did not download:\n  " + "\n  ".join(failed)
                 + "\nRun the same command again (finished files are kept, partial ones resume).")
    shutil.rmtree(tmp, ignore_errors=True)
    with open(out / "geo_samples.tsv", "w") as f:
        f.write("\t".join(["sample", "gsm", "title", "runs"] + fields) + "\n")
        for s in samples:
            c = geo["chars"].get(s["gsm"], {})
            f.write("\t".join([s["name"], s["gsm"], s["title"], ",".join(r["run"] for r in s["runs"])]
                               + [c.get(k, "") for k in fields]) + "\n")
    print(f"\nDone: {out}\nOn the site: From FASTQ -> folder '{out.name}'. The GEO titles are in {out / 'geo_samples.tsv'}.")


if __name__ == "__main__":
    main()
