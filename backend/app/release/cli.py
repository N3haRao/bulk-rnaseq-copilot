"""Bring a code bank release into the site: integrate (staging + checks + agent), approve, status, rollback.

Everything is local folders and git on the server; GitHub is not needed.

    $COPILOT_HOME/
      codebank/                    the bioinformatician's code bank (git); releases are tags vX.Y.Z
      copilot/                     the live site (git, this repository)
      codebank-releases/<tag>/     each released code bank, checked out at its tag (read-only use)
      codebank-live -> codebank-releases/<approved tag>     what the running site uses (CODEBANK_DIR)
      staging/<tag>/site/          staging copy of the site (git worktree, branch release/<tag>)
      staging/<tag>/RELEASE_REVIEW.md, agent_log.jsonl
      release-state.json           live release, pending release, history

    python -m app.release integrate v0.6.0     (run by the code bank's scripts/release.sh)
    python -m app.release approve v0.6.0       (by the bioinformatician who made the release)
    python -m app.release status | rollback --yes
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .agent import ReleaseAgent
from .workspace import Workspace

SITE_ROOT = Path(__file__).resolve().parents[3]
BOT = ["-c", "user.name=Copilot release agent", "-c", "user.email=release-agent@localhost"]


class ReleaseError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


class Paths:
    def __init__(self, home: Path | None = None, codebank: Path | None = None, site: Path | None = None) -> None:
        self.site = (site or SITE_ROOT).resolve()
        self.home = (home or Path(os.environ.get("COPILOT_HOME", self.site.parent))).resolve()
        self.codebank = (codebank or Path(os.environ.get("COPILOT_CODEBANK_REPO", self.home / "codebank"))).resolve()
        self.releases = self.home / "codebank-releases"
        self.live_link = self.home / "codebank-live"
        self.staging_root = self.home / "staging"
        self.state_file = self.home / "release-state.json"

    def staging(self, tag: str) -> Path:
        return self.staging_root / tag

    def load(self) -> dict:
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {"live": None, "pending": None, "history": []}

    def save(self, state: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.state_file)


def _check_tag(tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+([-.][A-Za-z0-9.]+)?", tag):
        raise ReleaseError(f"'{tag}' is not a release tag like v0.6.0.")
    return tag


def tag_info(p: Paths, tag: str) -> dict:
    if not git(p.codebank, "tag", "-l", tag):
        raise ReleaseError(f"The code bank at {p.codebank} has no tag {tag}. Release it with scripts/release.sh first.")
    msg = git(p.codebank, "tag", "-l", "--format=%(contents)", tag)
    m = re.search(r"released-by:\s*(\S+)", msg)
    return {"tag": tag, "commit": git(p.codebank, "rev-list", "-n", "1", tag), "message": msg,
            "released_by": m.group(1) if m else git(p.codebank, "tag", "-l", "--format=%(taggername)", tag) or None}


def _changelog(text: str, tag: str) -> str:
    m = re.search(rf"^## {re.escape(tag)}\b.*?(?=^## v|\Z)", text, re.M | re.S)
    return m.group(0).strip() if m else "(no CHANGELOG entry for this version)"


def _checkout_release(p: Paths, tag: str) -> Path:
    d = p.releases / tag
    if not d.exists():
        p.releases.mkdir(parents=True, exist_ok=True)
        git(p.codebank, "worktree", "add", "--detach", str(d), tag)
    return d


# ---- integrate ------------------------------------------------------------------------------------
def integrate(p: Paths, tag: str, agent_mode: str = "auto", llm=None, final_tests: str | None = None,
              max_steps: int = 40, out=print) -> dict:
    _check_tag(tag)
    info = tag_info(p, tag)
    state = p.load()
    prev = (state.get("live") or {}).get("tag")
    cb_dir = _checkout_release(p, tag)
    out(f"Code bank {tag} ({info['commit'][:7]}), released by {info['released_by'] or 'unknown'}; live now: {prev or 'none'}")

    # staging copy of the live site, on branch release/<tag>
    sdir = p.staging(tag)
    site = sdir / "site"
    if site.exists():
        git(p.site, "worktree", "remove", "--force", str(site), check=False)
    sdir.mkdir(parents=True, exist_ok=True)
    for f in ("agent_log.jsonl", "RELEASE_REVIEW.md"):
        (sdir / f).unlink(missing_ok=True)
    base = git(p.site, "rev-parse", "HEAD")
    git(p.site, "worktree", "add", "-B", f"release/{tag}", str(site), base)
    nm = p.site / "frontend" / "node_modules"
    if nm.exists() and not (site / "frontend" / "node_modules").exists():
        (site / "frontend" / "node_modules").symlink_to(nm)

    ws = Workspace(site, cb_dir)
    changelog = _changelog((cb_dir / "CHANGELOG.md").read_text() if (cb_dir / "CHANGELOG.md").exists() else "", tag)
    manifest_diff = git(p.codebank, "diff", f"{prev}..{tag}", "--", "manifest/tools.yaml", check=False)[:20000] if prev else "(first release on this server)"
    files_changed = git(p.codebank, "diff", "--stat", f"{prev}..{tag}", check=False)[-4000:] if prev else ""

    first = {}
    for c in ("manifest", "tests_quick"):
        ok, txt = ws.run_check(c)
        first[c] = {"ok": ok, "output": txt[-6000:]}
        out(f"  check {c}: {'passed' if ok else 'FAILED'}")

    agent_res = None
    need = not all(v["ok"] for v in first.values())
    if agent_mode == "always" or (agent_mode == "auto" and need):
        if llm is None:
            from ..llm.ollama import OllamaClient
            llm = OllamaClient(model=os.environ.get("RELEASE_AGENT_MODEL", "qwen3-coder:30b"),
                               url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
                               timeout=float(os.environ.get("RELEASE_AGENT_TIMEOUT", "1200")),
                               num_ctx=int(os.environ.get("RELEASE_AGENT_CONTEXT", "32768")))
        task = _task(tag, info, prev, changelog, manifest_diff, files_changed, first)
        out(f"  release agent ({getattr(llm, 'name', 'model')}) working in {site} ...")
        agent_res = ReleaseAgent(llm, ws, max_steps=max_steps, log=sdir / "agent_log.jsonl").run(task)
        out(f"  agent {'finished' if agent_res.finished else 'stopped: ' + (agent_res.stopped_because or '')}; "
            f"files changed: {', '.join(agent_res.changed) or 'none'}")

    # final checks, always by this command (never taken from the agent's word)
    final = {}
    plan = ["manifest", final_tests or os.environ.get("RELEASE_FINAL_TESTS", "tests_full")]
    if any(f.startswith("frontend/") for f in ws.changed):
        plan.append("frontend")
    for c in plan:
        ok, txt = ws.run_check(c)
        final[c] = {"ok": ok, "output": txt[-6000:]}
        out(f"  final check {c}: {'passed' if ok else 'FAILED'}")
    ready = all(v["ok"] for v in final.values())

    (site / "CODEBANK_RELEASE").write_text(f"{tag} {info['commit']}\n")
    git(site, "add", "-A", "--", ".", ":!frontend/node_modules")
    msg = f"Integrate code bank {tag}" + (" (release agent)" if ws.changed else "") + \
          (f"\n\nChanged by the release agent: {', '.join(sorted(ws.changed))}\n\n{agent_res.summary}" if agent_res and ws.changed else "")
    git(site, *BOT, "commit", "-q", "-m", msg)
    commit = git(site, "rev-parse", "HEAD")
    diffstat = git(site, "diff", "--stat", base, commit)
    diff = git(site, "diff", base, commit, "--", ".", ":!CODEBANK_RELEASE")

    pending = {"tag": tag, "codebank_commit": info["commit"], "released_by": info["released_by"], "staging": str(site),
               "branch": f"release/{tag}", "base": base, "staging_commit": commit, "ready": ready,
               "checks": {k: v["ok"] for k, v in final.items()}, "agent_used": agent_res is not None,
               "files_changed": sorted(ws.changed), "integrated": _now()}
    state["pending"] = pending
    p.save(state)
    review = _review(pending, info, prev, changelog, first, final, agent_res, diffstat, diff)
    (sdir / "RELEASE_REVIEW.md").write_text(review)
    out(f"\n{'READY FOR APPROVAL' if ready else 'NEEDS ATTENTION'}: read {sdir / 'RELEASE_REVIEW.md'}")
    return pending


def _task(tag, info, prev, changelog, manifest_diff, files_changed, first) -> str:
    checks = "\n\n".join(f"### {k}: {'passed' if v['ok'] else 'FAILED'}\n{v['output'][-5000:]}" for k, v in first.items())
    return f"""# Code bank release {tag} (previous live release: {prev or 'none'})

## CHANGELOG entry
{changelog}

## Manifest changes (git diff)
```
{manifest_diff[:12000]}
```

## Code bank files changed
```
{files_changed}
```

## Checks of the CURRENT site against the new code bank
{checks}

Make the site work with this release (staging copy; a person reviews your changes). Use read_codebank to read the
new code bank (e.g. manifest/tools.yaml, docs/ADDING_A_MODULE.md, CHANGELOG.md)."""


def _review(pending, info, prev, changelog, first, final, agent_res, diffstat, diff) -> str:
    tag = pending["tag"]
    lines = [f"# Review: code bank {tag} on the site", "",
             f"- Released by: **{pending['released_by'] or 'unknown'}** (commit {info['commit'][:10]}); previous live release: {prev or 'none'}",
             f"- Staging copy: `{pending['staging']}` (branch `{pending['branch']}`, commit {pending['staging_commit'][:10]})",
             f"- Status: **{'ready for approval' if pending['ready'] else 'NOT ready: a check failed (see below)'}**", "",
             "## What changed in the code bank", "", changelog, "",
             "## Checks before any change", ""]
    lines += [f"- {k}: {'passed' if v['ok'] else 'FAILED'}" for k, v in first.items()]
    lines += ["", "## Release agent", ""]
    if agent_res is None:
        lines.append("Not needed: the site already works with this release (no website code changed).")
    else:
        lines += [f"Finished: {'yes' if agent_res.finished else 'no (' + str(agent_res.stopped_because) + ')'}; "
                  f"{len(agent_res.steps)} actions; log: `agent_log.jsonl`.", "", "**Its summary (check it against the diff):**", "",
                  agent_res.summary or "(none)", ""]
        tests_touched = [f for f in pending["files_changed"] if f.startswith("backend/tests/")]
        if tests_touched:
            lines += [f"**Warning: the agent changed test files ({', '.join(tests_touched)}). Check that it did not weaken a test "
                      "to make it pass.**", ""]
        if agent_res.manual_review:
            lines += ["**Look at by hand:**", ""] + [f"- [ ] {x}" for x in agent_res.manual_review] + [""]
    lines += ["## Checks after the changes (run by the release command, not by the agent)", ""]
    for k, v in final.items():
        lines.append(f"- {k}: {'passed' if v['ok'] else 'FAILED'}")
        if not v["ok"]:
            lines += ["", "```", v["output"][-3000:], "```", ""]
    lines += ["", "## Changes to the site", "", "```", diffstat or "(only CODEBANK_RELEASE)", "```", ""]
    if diff:
        lines += ["<details><summary>Full diff</summary>", "", "```diff", diff[:60000], "```", "", "</details>", ""]
    lines += ["## Decide", "",
              f"- Approve (as {pending['released_by'] or 'the releasing bioinformatician'}):  `ops/release approve {tag}`",
              f"- Look at the staging site first:  `ops/release preview {tag}`  (port 8001)",
              "- Reject: do nothing (or fix the code bank and release a new version); the live site is unchanged.", ""]
    return "\n".join(lines)


# ---- approve / rollback / status ---------------------------------------------------------------------
def approve(p: Paths, tag: str, user: str | None = None, override: str | None = None, out=print) -> dict:
    state = p.load()
    pend = state.get("pending")
    if not pend or pend["tag"] != tag:
        raise ReleaseError(f"No pending release {tag} (pending: {pend['tag'] if pend else 'none'}). Run integrate first.")
    if not pend["ready"]:
        raise ReleaseError(f"{tag} is not ready: a check failed. Read {p.staging(tag) / 'RELEASE_REVIEW.md'}.")
    user = user or getpass.getuser()
    if pend.get("released_by") and user != pend["released_by"] and not override:
        raise ReleaseError(f"{tag} was released by {pend['released_by']}; they approve it (you are {user}). "
                           "An admin can use --override 'reason'.")
    head = git(Path(pend["staging"]), "rev-parse", "HEAD")
    if head != pend["staging_commit"]:
        raise ReleaseError("The staging copy changed after the review was written. Run integrate again.")
    if git(p.site, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError(f"The live site folder {p.site} has uncommitted changes; commit or remove them first.")
    previous = git(p.site, "rev-parse", "HEAD")
    r = subprocess.run(["git", "-C", str(p.site), "merge", "--ff-only", pend["branch"]], capture_output=True, text=True)
    if r.returncode != 0:
        git(p.site, *BOT, "merge", "--no-edit", "-m", f"Approve code bank {tag}", pend["branch"])
    _point_live(p, tag)
    entry = {"tag": tag, "codebank_commit": pend["codebank_commit"], "copilot_commit": git(p.site, "rev-parse", "HEAD"),
             "previous_copilot_commit": previous, "previous_tag": (state.get("live") or {}).get("tag"),
             "released_by": pend.get("released_by"), "approved_by": user, "approved": _now(),
             "override": override, "agent_used": pend["agent_used"], "files_changed": pend["files_changed"]}
    state["live"] = entry
    state["history"].append({**entry, "event": "approved"})
    state["pending"] = None
    p.save(state)
    git(p.site, "worktree", "remove", "--force", pend["staging"], check=False)
    out(f"{tag} is live (site commit {entry['copilot_commit'][:7]}, code bank {p.live_link} -> {tag}).")
    _restart(out)
    return entry


def _point_live(p: Paths, tag: str) -> None:
    target = _checkout_release(p, tag)
    tmp = p.home / ".codebank-live.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    tmp.replace(p.live_link)


def _restart(out) -> None:
    cmd = os.environ.get("COPILOT_RESTART_CMD")
    if cmd:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        out(f"Restart ({cmd}): {'done' if r.returncode == 0 else 'FAILED: ' + r.stderr.strip()}")
    else:
        out("Restart the site so it uses the new version (e.g. scancel <site job>; sbatch containers/ada/site.slurm).")


def rollback(p: Paths, yes: bool = False, user: str | None = None, out=print) -> dict:
    state = p.load()
    live = state.get("live")
    if not live or not live.get("previous_copilot_commit"):
        raise ReleaseError("Nothing to roll back to.")
    if not yes:
        raise ReleaseError(f"This puts the site back to {live.get('previous_tag') or 'the version before ' + live['tag']}. Add --yes.")
    if git(p.site, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError(f"The live site folder {p.site} has uncommitted changes.")
    git(p.site, "reset", "-q", "--hard", live["previous_copilot_commit"])
    if live.get("previous_tag"):
        _point_live(p, live["previous_tag"])
    back = next((h for h in reversed(state["history"]) if h.get("event") == "approved" and h["tag"] == live.get("previous_tag")), None)
    state["history"].append({"event": "rolled back", "from": live["tag"], "to": live.get("previous_tag"), "by": user or getpass.getuser(), "at": _now()})
    state["live"] = back
    p.save(state)
    out(f"Rolled back from {live['tag']} to {live.get('previous_tag') or 'the previous site version'}.")
    _restart(out)
    return state


def status(p: Paths) -> str:
    s = p.load()
    live, pend = s.get("live"), s.get("pending")
    out = [f"Copilot home: {p.home}",
           f"Live: {live['tag']} (site {live['copilot_commit'][:7]}, approved by {live['approved_by']} {live['approved']})" if live else "Live: none recorded",
           (f"Pending: {pend['tag']} ({'ready for approval' if pend['ready'] else 'needs attention'}; released by {pend['released_by']}; "
            f"review: {p.staging(pend['tag']) / 'RELEASE_REVIEW.md'})") if pend else "Pending: none"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ops/release", description="Bring code bank releases into the site.")
    ap.add_argument("--home", type=Path, help="COPILOT_HOME (default: the folder above the site)")
    ap.add_argument("--codebank", type=Path, help="the bioinformatician's code bank repository (default: $COPILOT_HOME/codebank)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("integrate", help="check the site against a code bank release; fix it in a staging copy if needed")
    a.add_argument("tag")
    a.add_argument("--agent", choices=["auto", "always", "never"], default="auto")
    a.add_argument("--max-steps", type=int, default=40)
    b = sub.add_parser("approve", help="make a reviewed release live")
    b.add_argument("tag")
    b.add_argument("--override", help="approve although you did not make the release (reason is recorded)")
    c = sub.add_parser("rollback", help="put the site back to the previous release")
    c.add_argument("--yes", action="store_true")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    p = Paths(args.home, args.codebank)
    try:
        if args.cmd == "integrate":
            return 0 if integrate(p, args.tag, args.agent, max_steps=args.max_steps)["ready"] else 2
        if args.cmd == "approve":
            approve(p, args.tag, override=args.override)
        elif args.cmd == "rollback":
            rollback(p, yes=args.yes)
        else:
            print(status(p))
        return 0
    except ReleaseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
