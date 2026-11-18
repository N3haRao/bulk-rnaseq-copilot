"""The code bank review: before the site is touched, the release agent reads what changed in the code bank itself.

For every release (not only when the site needs work), against the live release on this server (or else the code
bank's previous tag):

  1. By code, no model: which files changed; which parameter defaults changed (config/default_params.yaml and the
     manifest's module parameters); which changed code files the CHANGELOG never mentions.
  2. By the model, one code file at a time (diff + the new file with line numbers + the CHANGELOG entry): what the file
     does now, each change with its kind (method, default, output, fix, ...) and line numbers, whether earlier results
     would change, and whether the CHANGELOG describes it correctly.
  3. By code again: every change the model reports must point at lines that really changed in this release;
     the ones that do not are marked "not verified" for the reviewer.
  4. One more model call turns the per-file notes into a short headline for the person approving.

The review informs the person approving; it does not make a release ready or not ready (the checks do that).
Written to staging/<tag>/codebank_review.json and as the first section of RELEASE_REVIEW.md.
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from ..llm.base import LLMError, LLMUnavailable

PROMPT = (Path(__file__).resolve().parents[1] / "agents" / "prompts" / "codebank_reviewer.md").read_text()
CODE_SUFFIXES = {".r", ".py", ".sh", ".pl", ".smk", ".nf", ".rmd"}
KINDS = ["method", "default", "output", "fix", "refactor", "messages", "other"]

FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "what_it_does": {"type": "string"},
        "changes": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "kind": {"type": "string", "enum": KINDS}, "lines": {"type": "string"}},
            "required": ["what", "kind", "lines"]}},
        "earlier_results_change": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "earlier_results_why": {"type": "string"},
        "changelog": {"type": "string", "enum": ["matches", "partly", "not_mentioned", "contradicts"]},
        "changelog_note": {"type": "string"},
    },
    "required": ["what_it_does", "changes", "earlier_results_change", "changelog"],
}
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "2-4 sentences: what this release changes, for the person approving."},
        "results_may_change": {"type": "array", "items": {"type": "string"}},
        "check_by_hand": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline"],
}
SUMMARY_PROMPT = ("You summarise a code bank release for the bioinformatician who approves it on the RNA-seq copilot. You get "
                  "per-file notes written by a reviewer who read the code, and the defaults that changed. Answer as JSON: "
                  "headline (2-4 plain sentences: what changed and why it matters), results_may_change (analyses whose "
                  "numbers or calls would differ if rerun, one line each), check_by_hand (what the person should look at "
                  "themselves, e.g. a change the CHANGELOG does not mention). Use only what the notes say.")


@dataclass
class FileReview:
    path: str
    status: str                      # added / modified / removed / renamed
    kind: str                        # code / test / config / docs / other
    reviewed: bool = False
    why_not: str | None = None
    in_changelog: bool | None = None
    what_it_does: str = ""
    changes: list[dict] = field(default_factory=list)   # what, kind, lines, verified
    earlier_results_change: str | None = None
    earlier_results_why: str = ""
    changelog: str | None = None
    changelog_note: str = ""


@dataclass
class CodebankReview:
    base: str | None
    tag: str
    status: str                      # done / partial / skipped / not_done
    reason: str = ""
    files: list[FileReview] = field(default_factory=list)
    defaults: list[dict] = field(default_factory=list)   # where, name, old, new
    headline: str = ""
    results_may_change: list[str] = field(default_factory=list)
    check_by_hand: list[str] = field(default_factory=list)
    model: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


# ---- what changed, by code -----------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    import subprocess
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def previous_tag(repo: Path, tag: str) -> str | None:
    """The code bank's release tag just before `tag` (for a first release on this server)."""
    t = _git(repo, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", f"{tag}^").strip()
    return t or None


def classify(path: str) -> str:
    p = Path(path)
    low = path.lower()
    if "/tests/" in f"/{low}" or p.name.startswith("test_") or p.name.startswith("run_") and "test" in p.name:
        return "test"
    if p.suffix.lower() in CODE_SUFFIXES:
        return "code"
    if p.suffix.lower() in (".yaml", ".yml", ".json", ".toml"):
        return "config"
    if p.suffix.lower() in (".md", ".txt", ".rst") or low.startswith("docs/"):
        return "docs"
    return "other"


def changed_files(repo: Path, base: str, tag: str) -> list[tuple[str, str]]:
    names = {"A": "added", "M": "modified", "D": "removed", "R": "renamed", "C": "copied", "T": "modified"}
    out = []
    for line in _git(repo, "diff", "--name-status", "-M", f"{base}..{tag}").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((names.get(parts[0][:1], "modified"), parts[-1]))
    return out


def _flat(d, prefix="") -> dict:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flat(v, f"{prefix}{k}."))
    else:
        out[prefix[:-1]] = d
    return out


def _yaml_at(repo: Path, rev: str, path: str):
    try:
        return yaml.safe_load(_git(repo, "show", f"{rev}:{path}")) or {}
    except yaml.YAMLError:
        return {}


def default_changes(repo: Path, base: str, tag: str) -> list[dict]:
    """Parameter defaults that differ between the releases (added and removed ones included)."""
    out = []
    a, b = _flat(_yaml_at(repo, base, "config/default_params.yaml")), _flat(_yaml_at(repo, tag, "config/default_params.yaml"))
    for k in sorted(set(a) | set(b)):
        if a.get(k, "∅") != b.get(k, "∅"):
            out.append({"where": "config/default_params.yaml", "name": k, "old": a.get(k, "(not set)"), "new": b.get(k, "(removed)")})
    ma, mb = (_yaml_at(repo, r, "manifest/tools.yaml").get("modules") or {} for r in (base, tag))
    for mod in sorted(set(ma) | set(mb)):
        pa = ((ma.get(mod) or {}).get("parameters") or {})
        pb = ((mb.get(mod) or {}).get("parameters") or {})
        for name in sorted(set(pa) | set(pb)):
            da = (pa.get(name) or {}).get("default", "(not set)") if name in pa else "(new parameter)"
            db = (pb.get(name) or {}).get("default", "(not set)") if name in pb else "(removed)"
            if da != db and not (name not in pa and db == "(not set)"):
                out.append({"where": f"manifest: {mod}", "name": name, "old": da, "new": db})
    return out


def mentioned(path: str, changelog: str) -> bool:
    """Does the CHANGELOG entry name this file (its name, its step id such as SU03, or its module folder)?"""
    text = changelog.lower()
    p = Path(path)
    stem = p.stem.lower()
    step = re.match(r"^([a-z]+\d+[a-z]?)_", stem)
    folder = p.parts[0].lower() if len(p.parts) > 1 else ""
    return (stem in text or p.name.lower() in text or bool(step and re.search(rf"\b{step.group(1)}\b", text))
            or (folder not in ("", "r", "scripts", "config", "tests", "docs", "manifest") and re.search(rf"\b{re.escape(folder)}\b", text) is not None))


def changed_lines(diff: str) -> list[tuple[int, int]]:
    """Line ranges in the NEW file that this diff touches (a pure deletion counts as the line where it was)."""
    out = []
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.M):
        start, n = int(m.group(1)), int(m.group(2) or 1)
        out.append((max(1, start - 1), start + max(n, 1)))      # a line of slack around deletions
    return out


def _verified(lines: str, ranges: list[tuple[int, int]]) -> bool:
    nums = [int(x) for x in re.findall(r"\d+", lines or "")]
    if not nums:
        return False
    a, b = (nums[0], nums[1]) if len(nums) > 1 else (nums[0], nums[0])
    return any(a <= hi and b >= lo for lo, hi in ranges)


def _numbered(text: str, limit: int) -> str:
    lines = text.splitlines()
    out = "\n".join(f"{i:5d}  {l}" for i, l in enumerate(lines, 1))
    return out if len(out) <= limit else out[:limit] + f"\n... (file continues; {len(lines)} lines in all)"


# ---- the review ----------------------------------------------------------------------------------
def review(repo: Path, base: str | None, tag: str, changelog: str, llm, out=print, max_files: int | None = None) -> CodebankReview:
    base = base or previous_tag(repo, tag)
    if not base:
        return CodebankReview(None, tag, "skipped", "No earlier release to compare with (the code bank's first release).")
    rv = CodebankReview(base, tag, "done", model=getattr(llm, "name", None) if llm else None)
    rv.defaults = default_changes(repo, base, tag)
    max_files = max_files or int(os.environ.get("RELEASE_REVIEW_MAX_FILES", "30"))
    for status, path in changed_files(repo, base, tag):
        fr = FileReview(path, status, classify(path))
        if fr.kind == "code":
            fr.in_changelog = mentioned(path, changelog)
        rv.files.append(fr)
    code = [f for f in rv.files if f.kind == "code"]
    if not code:
        rv.reason = "No code files changed."
    if llm is None:
        rv.status, rv.reason = ("not_done", "No model was available to read the code.") if code else (rv.status, rv.reason)
        return rv
    stopped = None
    for i, fr in enumerate(code):
        if stopped:
            fr.why_not = stopped
            continue
        if i >= max_files:
            fr.why_not = f"more than {max_files} code files changed (RELEASE_REVIEW_MAX_FILES)"
            continue
        diff = _git(repo, "diff", "-M", f"{base}..{tag}", "--", fr.path)
        new = "" if fr.status == "removed" else _git(repo, "show", f"{tag}:{fr.path}")
        if "\0" in new[:8000]:
            fr.why_not = "binary file"
            continue
        out(f"  code bank review: {fr.path}")
        user = (f"# Code bank release {tag} (compared with {base})\n\n## CHANGELOG entry\n{changelog[:6000]}\n\n"
                f"## File: {fr.path} ({fr.status})\n\n### Diff\n```diff\n{diff[:16000]}\n```\n\n"
                + (f"### The new file, with line numbers\n```\n{_numbered(new, 24000)}\n```\n" if new else "(the file was removed)\n")
                + "\nYour review (JSON):")
        try:
            a = llm.chat_json(PROMPT, user, FILE_SCHEMA)
        except (LLMUnavailable, LLMError) as e:
            stopped = f"the model stopped answering ({e})"
            fr.why_not = stopped
            continue
        ranges = changed_lines(diff)
        fr.reviewed = True
        fr.what_it_does = str(a.get("what_it_does") or "").strip()
        fr.changes = [{"what": str(c.get("what", "")).strip(), "kind": c.get("kind") if c.get("kind") in KINDS else "other",
                       "lines": str(c.get("lines", "")), "verified": fr.status == "removed" or _verified(str(c.get("lines", "")), ranges)}
                      for c in a.get("changes") or [] if isinstance(c, dict)]
        fr.earlier_results_change = a.get("earlier_results_change") if a.get("earlier_results_change") in ("yes", "no", "unclear") else "unclear"
        fr.earlier_results_why = str(a.get("earlier_results_why") or "")
        fr.changelog = a.get("changelog") if a.get("changelog") in ("matches", "partly", "not_mentioned", "contradicts") else None
        fr.changelog_note = str(a.get("changelog_note") or "")
    done = [f for f in code if f.reviewed]
    if code and not done:
        rv.status, rv.reason = "not_done", stopped or "No code file could be reviewed."
        return rv
    if len(done) < len(code):
        rv.status = "partial"
        rv.reason = f"{len(code) - len(done)} of {len(code)} code files were not read."
    if done:
        notes = json.dumps({"files": [{k: v for k, v in asdict(f).items() if k in ("path", "status", "what_it_does", "changes",
                                                                                   "earlier_results_change", "earlier_results_why",
                                                                                   "changelog", "changelog_note")} for f in done],
                            "defaults_changed": rv.defaults}, default=str)[:30000]
        try:
            s = llm.chat_json(SUMMARY_PROMPT, f"# Code bank {tag} (compared with {base})\n\n{notes}\n\nYour summary (JSON):", SUMMARY_SCHEMA)
            rv.headline = str(s.get("headline") or "")
            rv.results_may_change = [str(x) for x in s.get("results_may_change") or []]
            rv.check_by_hand = [str(x) for x in s.get("check_by_hand") or []]
        except (LLMUnavailable, LLMError):
            pass
    return rv


# ---- as text -------------------------------------------------------------------------------------
def _flags(rv: CodebankReview) -> list[str]:
    """What the person approving should not miss; found by code, not by the model."""
    out = []
    for f in rv.files:
        if f.kind == "code" and f.in_changelog is False:
            out.append(f"`{f.path}` changed but the CHANGELOG does not mention it.")
        if f.changelog == "contradicts":
            out.append(f"`{f.path}`: the reviewer says the CHANGELOG contradicts the code: {f.changelog_note}")
        bad = [c for c in f.changes if not c["verified"]]
        if bad:
            out.append(f"`{f.path}`: {len(bad)} described change(s) do not point at lines changed in this release (marked below).")
        if f.kind == "code" and not f.reviewed and f.why_not:
            out.append(f"`{f.path}` was not read: {f.why_not}.")
    if any(f.kind == "code" and f.status != "removed" for f in rv.files) and not any(f.kind == "test" for f in rv.files):
        out.append("Code changed but no test file did.")
    return out


def markdown(rv: CodebankReview) -> str:
    L = [f"## What changed in the code bank ({rv.base or 'no earlier release'} → {rv.tag}), read by the release agent", ""]
    if rv.status == "skipped":
        return "\n".join(L + [rv.reason, ""])
    by = {"done": "Every changed code file was read.", "partial": f"Partly done: {rv.reason}",
          "not_done": f"**Not done: {rv.reason}** Read the code changes yourself (list below)."}
    L += [by[rv.status] + (f" Model: {rv.model}." if rv.model and rv.status != "not_done" else ""), ""]
    if rv.headline:
        L += ["**In short:** " + rv.headline, ""]
    if rv.results_may_change:
        L += ["**Results that may differ if rerun:**", ""] + [f"- {x}" for x in rv.results_may_change] + [""]
    flags = _flags(rv)
    if flags or rv.check_by_hand:
        L += ["**Check by hand:**", ""] + [f"- [ ] {x}" for x in flags + rv.check_by_hand] + [""]
    if rv.defaults:
        L += ["**Defaults that changed** (found by comparing the files, not by the model):", "",
              "| where | parameter | before | now |", "|---|---|---|---|"]
        L += [f"| {d['where']} | `{d['name']}` | {d['old']} | {d['new']} |" for d in rv.defaults] + [""]
    code = [f for f in rv.files if f.kind == "code"]
    for f in code:
        L += [f"### `{f.path}` ({f.status})", ""]
        if not f.reviewed:
            L += [f"Not read: {f.why_not or 'no model'}.", ""]
            continue
        L += [f.what_it_does, ""]
        for c in f.changes:
            L.append(f"- **{c['kind']}** (lines {c['lines'] or '?'}): {c['what']}" + ("" if c["verified"] else "  ⚠ *not verified: these lines did not change in this release*"))
        if not f.changes:
            L.append("- (no changes described)")
        L += ["", f"- Earlier results would change: **{f.earlier_results_change}**" + (f": {f.earlier_results_why}" if f.earlier_results_why else ""),
              f"- CHANGELOG: **{(f.changelog or 'unclear').replace('_', ' ')}**" + (f": {f.changelog_note}" if f.changelog_note else "")
              + ("" if f.in_changelog is not False else " (the entry does not name this file)"), ""]
    others = [f for f in rv.files if f.kind != "code"]
    if others:
        L += ["**Other files changed** (not read by the model):", ""]
        L += [f"- `{f.path}` ({f.kind}, {f.status})" for f in others] + [""]
    return "\n".join(L)


def brief(rv: CodebankReview) -> str:
    """A few lines for the site agent's task."""
    if rv.status in ("skipped", "not_done"):
        return f"(code review {rv.status}: {rv.reason})"
    lines = [rv.headline] if rv.headline else []
    for f in rv.files:
        if f.reviewed:
            lines.append(f"- {f.path}: " + "; ".join(c["what"] for c in f.changes[:4]))
    return "\n".join(lines)[:4000]
