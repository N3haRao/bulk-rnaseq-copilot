#!/usr/bin/env python3
"""Run a public GEO study from raw reads to finished analyses on the server, without anyone watching.

    nohup python3 autopilot.py GSE107648 --species human --control scr --mislabel CHERP:RBM17 \
        > $COPILOT_HOME/autopilot_GSE107648.log 2>&1 &

Steps, each through the website's own API (so everything shows up on the site as usual):
  1. download the FASTQ files (fetch_geo_fastq.py, next to this script; --source ncbi when ENA is unreachable)
  2. make sure the reference for the species is on the server (downloads it through the site if not)
  3. groups from the sample names; the control group is the one whose name contains --control
  4. FASTQ processing (QC, STAR, Salmon, counts, rMATS) -> a dataset
  5. one analysis per group: control vs that group (the splicing and isoform analyses follow by themselves)
  6. optional mislabel test (--mislabel FROM:TO): one sample of the FROM group is relabelled as TO in a copy of the
     sample table, that copy is analysed (control vs TO), and the summary says whether the outlier check flagged it

It keeps going after you log out (run it with nohup). Progress is saved in $COPILOT_HOME/data/autopilot/<GSE>.json:
running the same command again picks up where it stopped. The summary is written to
$COPILOT_HOME/autopilot_<GSE>_summary.txt. Python 3 standard library only; the site must be started with
start_site.sh (it is started if it is not running).
"""
import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("COPILOT_HOME", Path.home() / "rnaseq-copilot")).resolve()
POLL = int(os.environ.get("AUTOPILOT_POLL", "120"))
RUN_FINAL = {"success", "success_with_errors", "failed", "cancelled", "interrupted"}
FASTQ_FINAL = {"success", "failed", "cancelled", "interrupted"}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class Stop(Exception):
    """Something a person has to look at; the message says what."""


# ---- the site's API -------------------------------------------------------------------------------
def base_url() -> str:
    if os.environ.get("AUTOPILOT_URL"):
        return os.environ["AUTOPILOT_URL"].rstrip("/")
    f = HOME / "site.job"
    if not f.exists():
        return ""
    parts = f.read_text().split()
    return f"http://127.0.0.1:{parts[2]}" if len(parts) >= 3 else ""


def _request(method: str, path: str, body: bytes | None = None, ctype: str | None = None, raw: bool = False):
    url = base_url()
    if not url:
        raise ConnectionError("no site recorded")
    req = urllib.request.Request(url + path, data=body, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    return data if raw else (json.loads(data) if data else None)


def api(method: str, path: str, payload=None, *, raw: bool = False, files: dict | None = None):
    """JSON (or raw bytes) from the site. Waits for the site through restarts; a 4xx answer raises Stop."""
    body, ctype = None, None
    if files:
        boundary = uuid.uuid4().hex
        chunks = []
        for field, (name, content) in files.items():
            chunks += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\n"
                       "Content-Type: text/csv\r\n\r\n".encode(), content, b"\r\n"]
        chunks.append(f"--{boundary}--\r\n".encode())
        body, ctype = b"".join(chunks), f"multipart/form-data; boundary={boundary}"
    elif payload is not None:
        body, ctype = json.dumps(payload).encode(), "application/json"
    for attempt in range(60):                      # up to ~30 min of the site being down
        try:
            return _request(method, path, body, ctype, raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:1500]
            if 400 <= e.code < 500:
                raise Stop(f"The site refused {method} {path} ({e.code}): {detail}")
            log(f"   site error {e.code} on {path}; retrying in 30 s")
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            if attempt == 2:
                ensure_site()
            elif attempt % 10 == 0:
                log(f"   the site does not answer ({e}); waiting")
        time.sleep(30)
    raise Stop("The site did not answer for 30 minutes. Check the site log and start it again.")


def ensure_site() -> None:
    try:
        _request("GET", "/api/health")
        return
    except Exception:  # noqa: BLE001 - not running: start it
        pass
    if os.environ.get("AUTOPILOT_URL"):
        return
    log("The site is not running: starting it (start_site.sh)")
    subprocess.call([str(HERE / "start_site.sh")])
    time.sleep(60)


# ---- saved progress -------------------------------------------------------------------------------
class State:
    def __init__(self, gse: str):
        self.path = HOME / "data" / "autopilot" / f"{gse}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.d = json.loads(self.path.read_text()) if self.path.exists() else {"gse": gse}

    def __getitem__(self, k):
        return self.d.get(k)

    def set(self, **kw) -> None:
        self.d.update(kw)
        self.path.write_text(json.dumps(self.d, indent=2))


def wait_for(path: str, final: set, label: str, every: int = POLL) -> dict:
    last = None
    while True:
        j = api("GET", path)
        st = j.get("status")
        if st != last:
            log(f"   {label}: {st}")
            last = st
        if st in final:
            return j
        time.sleep(every)


# ---- steps ----------------------------------------------------------------------------------------
def step_download(a, state: State) -> None:
    if state["downloaded"]:
        return
    cmd = [sys.executable, str(HERE / "fetch_geo_fastq.py"), a.gse, "--download", "--source", a.source,
           "--parallel", str(a.parallel), "--folder", a.folder]
    for attempt in range(1, 4):
        log(f"1. Download (attempt {attempt}/3): {' '.join(cmd[1:])}")
        if subprocess.call(cmd) == 0:
            state.set(downloaded=True)
            log("   download complete")
            return
        log("   the download stopped with an error; finished files are kept, trying again in 10 min")
        time.sleep(600)
    raise Stop("The download failed 3 times. See the lines above; the same command resumes it.")


def step_reference(a) -> None:
    ref = next((r for r in api("GET", "/api/fastq/folders")["references"] if r["species"] == a.species), None)
    if ref is None:
        raise Stop(f"The site has no reference for '{a.species}'.")
    if ref["files_ready"]:
        log(f"2. Reference: {ref['label']} is on the server")
        return
    log(f"2. Reference: downloading {ref['label']} through the site")
    if (ref.get("download") or {}).get("status") != "running":
        api("POST", f"/api/fastq/references/{a.species}")
    while True:
        time.sleep(60)
        ref = next(r for r in api("GET", "/api/fastq/folders")["references"] if r["species"] == a.species)
        if ref["files_ready"]:
            log("   reference ready")
            return
        dl = ref.get("download") or {}
        if dl.get("status") == "failed":
            raise Stop(f"The reference download failed: {dl.get('error')}. Copy the GENCODE files into {ref['dir']} "
                       "(see docs/fuji_setup_commands.sh) and run this command again.")


def pick(groups: list[str], word: str, what: str) -> str:
    hits = [g for g in groups if word.lower() in g.lower()]
    if len(hits) != 1:
        raise Stop(f"{what}: '{word}' matches {len(hits)} of the groups ({', '.join(groups)}); "
                   "give a word that matches exactly one.")
    return hits[0]


def step_groups(a) -> tuple[dict, str]:
    p = api("GET", f"/api/fastq/folders/{a.folder}")
    if p["problems"]:
        raise Stop("The FASTQ files have problems: " + " ".join(p["problems"]))
    groups = p["suggestion"]["groups"]
    levels = sorted(set(groups.values()))
    control = pick(levels, a.control, "Control group")
    sizes = {g: sum(1 for v in groups.values() if v == g) for g in levels}
    log(f"3. Groups from the sample names ({p['layout']}, {len(groups)} samples, {p['gb']} GB): "
        + ", ".join(f"{g} ({n})" for g, n in sizes.items()) + f"; control {control}")
    for s, g in sorted(groups.items()):
        log(f"     {s:45} {g}")
    if min(sizes.values()) < 2:
        raise Stop("A group has fewer than 2 samples: the names did not give usable groups. "
                   "Set the groups on the From FASTQ page instead.")
    return groups, control


def step_fastq(a, state: State, groups: dict, control: str) -> str:
    if state["fastq_job"]:
        j = api("GET", f"/api/fastq/jobs/{state['fastq_job']}")
        if j["status"] == "success":
            return state["fastq_job"]
        if j["status"] not in FASTQ_FINAL:
            log(f"4. FASTQ processing already started ({state['fastq_job']})")
            return wait_fastq(state)
        log(f"4. The earlier FASTQ job ended '{j['status']}': starting a new one")
    home = api("GET", "/api/fastq/folders")
    spec = {"folder": a.folder, "groups": groups, "control": control, "species": a.species, "batch": {},
            "steps": [s["id"] for s in home["steps"]], "threads": a.threads}
    chk = api("POST", "/api/fastq/check", spec)
    for line in chk["lines"]:
        log(f"   check {line['status']:5} {line['text']}")
    if not chk["ok"]:
        raise Stop("The setup check failed (lines marked 'fail' above).")
    job = api("POST", "/api/fastq/jobs", spec)
    state.set(fastq_job=job["id"], fastq_tries=(state["fastq_tries"] or 0) + 1)
    log(f"4. FASTQ processing started: job {job['id']} (it waits if another FASTQ job is running)")
    return wait_fastq(state)


def wait_fastq(state: State) -> str:
    j = wait_for(f"/api/fastq/jobs/{state['fastq_job']}", FASTQ_FINAL, "FASTQ job")
    if j["status"] == "success":
        return state["fastq_job"]
    tail = "\n".join((j.get("log_tail") or [])[-15:]) if isinstance(j.get("log_tail"), list) else (j.get("log_tail") or "")
    if j["status"] == "interrupted" and (state["fastq_tries"] or 0) < 2:
        log("   interrupted (the site restarted): starting it once more")
        state.set(fastq_job=None)
        raise RetryFastq()
    raise Stop(f"FASTQ processing ended '{j['status']}': {j.get('error') or ''}\n{tail}")


class RetryFastq(Exception):
    pass


def start_run(upload_id: str, control: str, test: str, a, name: str) -> str:
    body = {"upload_id": upload_id, "condition": "condition", "reference_level": control, "test_level": test,
            "covariates": [], "organism": a.species, "run_name": name,
            "advanced": {"log2fc_cutoff": a.log2fc} if a.log2fc is not None else {}}
    return api("POST", "/api/runs", body)["id"]


def step_analyses(a, state: State, fastq_job: str, control: str, groups: dict) -> dict:
    if not state["upload_id"]:
        rec = api("POST", f"/api/fastq/jobs/{fastq_job}/dataset")
        state.set(upload_id=rec["id"])
    runs = state["runs"] or {}
    for g in sorted(set(groups.values()) - {control}):
        if g not in runs:
            runs[g] = start_run(state["upload_id"], control, g, a, f"{g}_vs_{control}")
            state.set(runs=runs)
            log(f"5. Analysis {g} vs {control} started: {runs[g]}")
    results = {}
    for g, rid in runs.items():
        j = wait_for(f"/api/runs/{rid}", RUN_FINAL, f"analysis {g} vs {control}")
        results[g] = j["status"] + (f" ({(j.get('error') or '')[:300]})" if j["status"] == "failed" else "")
    return results


def step_mislabel(a, state: State, fastq_job: str, control: str, groups: dict) -> dict | None:
    if not a.mislabel:
        return None
    frm, to = a.mislabel.split(":", 1)
    levels = sorted(set(groups.values()))
    g_from, g_to = pick(levels, frm, "Mislabel FROM group"), pick(levels, to, "Mislabel TO group")
    sample = state["mislabel_sample"] or sorted(s for s, g in groups.items() if g == g_from)[-1]
    if not state["mislabel_run"]:
        counts = api("GET", f"/api/fastq/jobs/{fastq_job}/files/counts/counts.csv", raw=True)
        samples = api("GET", f"/api/fastq/jobs/{fastq_job}/files/counts/samples.csv", raw=True).decode()
        rows = list(csv.DictReader(io.StringIO(samples)))
        hit = [r for r in rows if r["sample"] == sample]
        if len(hit) != 1:
            raise Stop(f"Sample {sample} is not in the sample table.")
        hit[0]["condition"] = g_to
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        rec = api("POST", "/api/uploads", files={"counts": ("counts.csv", counts),
                                                  "metadata": (f"samples_{sample}_as_{g_to}.csv", out.getvalue().encode())})
        rid = start_run(rec["id"], control, g_to, a, f"mislabel_{g_to}_vs_{control}")
        state.set(mislabel_sample=sample, mislabel_upload=rec["id"], mislabel_run=rid)
        log(f"6. Mislabel test: {sample} (really {g_from}) labelled {g_to}; analysis {rid} started")
    j = wait_for(f"/api/runs/{state['mislabel_run']}", RUN_FINAL, "mislabel analysis")
    st = j["status"] + (f" ({(j.get('error') or '')[:300]})" if j["status"] == "failed" else "")
    flags = read_flags(state["mislabel_run"])
    me = next((f for f in flags if f.get("sample_id") == sample), None)
    return {"sample": sample, "really": g_from, "labelled": g_to, "status": st, "row": me,
            "others_flagged": [f for f in flags if f.get("flag") and f.get("sample_id") != sample]}


def read_flags(run_id: str) -> list[dict]:
    try:
        txt = api("GET", f"/api/runs/{run_id}/files/03_exploratory/outlier_flags.csv", raw=True).decode()
    except Stop:
        return []
    return list(csv.DictReader(io.StringIO(txt)))


def summary(a, state: State, control: str, results: dict, mis: dict | None) -> str:
    lines = [f"Autopilot summary for {a.gse} ({datetime.now().strftime('%Y-%m-%d %H:%M')})", "",
             f"FASTQ job: {state['fastq_job']}   dataset: {state['upload_id']}", "", "Analyses (control: " + control + "):"]
    for g, st in results.items():
        rid = state["runs"][g]
        flagged = [f for f in read_flags(rid) if f.get("flag")]
        lines.append(f"  {g} vs {control}: {st}   run {rid}   outlier flags: "
                     + (", ".join(f"{f['sample_id']} ({f['flag']})" for f in flagged) or "none"))
    if mis:
        lines += ["", f"Mislabel test: {mis['sample']} (really {mis['really']}) labelled {mis['labelled']} "
                      f"-> analysis {state['mislabel_run']} ({mis['status']})"]
        r = mis["row"] or {}
        if r.get("flag"):
            lines.append(f"  DETECTED: flag '{r['flag']}'. {r.get('message', '')}")
        else:
            lines.append(f"  NOT flagged. Nearest group in the PCA: {r.get('nearest_group', '?')} "
                         f"(distance to own group {r.get('dist_to_own_group', '?')}, "
                         f"to nearest other {r.get('dist_to_nearest_other', '?')}).")
        if mis["others_flagged"]:
            lines.append("  Also flagged: " + ", ".join(f"{f['sample_id']} ({f['flag']})" for f in mis["others_flagged"]))
    lines += ["", "Open the site to see the results: My analyses."]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gse")
    ap.add_argument("--species", required=True, choices=["human", "mouse"])
    ap.add_argument("--control", required=True, help="a word in the control group's name, e.g. scr, ctrl, wt, vehicle")
    ap.add_argument("--mislabel", default=None, help="FROM:TO words of two group names: relabel one FROM sample as TO")
    ap.add_argument("--source", choices=["ena", "ncbi"], default="ncbi")
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--threads", type=int, default=32)
    ap.add_argument("--log2fc", type=float, default=None, help="fold-change cutoff for the analyses (log2), e.g. 0.263")
    ap.add_argument("--folder", default=None, help="folder under incoming (default: the GSE)")
    a = ap.parse_args()
    a.gse = a.gse.upper()
    a.folder = a.folder or a.gse
    state = State(a.gse)
    log(f"Autopilot for {a.gse} (progress saved in {state.path})")
    try:
        step_download(a, state)
        ensure_site()
        step_reference(a)
        groups, control = step_groups(a)
        while True:
            try:
                fastq_job = step_fastq(a, state, groups, control)
                break
            except RetryFastq:
                continue
        results = step_analyses(a, state, fastq_job, control, groups)
        mis = step_mislabel(a, state, fastq_job, control, groups)
        text = summary(a, state, control, results, mis)
        out = HOME / f"autopilot_{a.gse}_summary.txt"
        out.write_text(text + "\n")
        log("All done.\n\n" + text + f"\n\n(saved in {out})")
    except Stop as e:
        log(f"STOPPED: {e}")
        log("Fix what it says, then run the same command again: finished steps are skipped.")
        sys.exit(1)


if __name__ == "__main__":
    main()
