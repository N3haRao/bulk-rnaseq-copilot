"""Choose input files that are already on the server, instead of uploading them from the browser.

Only a few folders can be browsed, and they are fixed on the server (never chosen in the browser):

  incoming    data/incoming (FASTQ_INCOMING_DIR): files copied to the server for the site
  fastq       data/fastq: the Start-from-FASTQ jobs (BAMs, Salmon output, rMATS tables)
  refs        data/refs (REFS_DIR): the reference genomes and annotations
  + any folders in SERVER_FILE_ROOTS, e.g.  SERVER_FILE_ROOTS="Lab projects=/mnt/lab/projects;Everest=/data/everest"

A choice is sent as "<root>:<path inside it>". It is resolved on the server, symlinks included, and refused
if it lands outside its folder or passes through a hidden file or folder. Chosen files are linked into the
analysis folder, not copied (see extras.ServerFile).
"""

import json
import os
import re
from pathlib import Path

from .config import settings

MAX_ENTRIES = 2000


class PickError(ValueError):
    """A choice that cannot be used, in plain language."""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "folder"


def roots() -> dict[str, dict]:
    """{key: {label, path}} of the folders that can be browsed (the ones that exist)."""
    from . import fastq
    out = {
        "incoming": {"label": "Incoming data", "path": fastq.incoming_dir()},
        "fastq": {"label": "Start-from-FASTQ jobs", "path": fastq.jobs_root()},
        "refs": {"label": "References", "path": fastq.refs_dir()},
    }
    for part in (os.environ.get("SERVER_FILE_ROOTS") or "").split(";"):
        if "=" not in part:
            continue
        label, path = (x.strip() for x in part.split("=", 1))
        if label and path:
            key = _slug(label)
            while key in out:
                key += "_"
            out[key] = {"label": label, "path": Path(path).expanduser()}
    return {k: {"label": v["label"], "path": Path(v["path"]).resolve()} for k, v in out.items() if Path(v["path"]).is_dir()}


def resolve(token: str) -> Path:
    """'<root>:<rel>' -> the path on the server, if it is inside that root and exists."""
    if ":" not in token:
        raise PickError(f"'{token}' is not a server file choice.")
    key, rel = token.split(":", 1)
    r = roots().get(key)
    if r is None:
        raise PickError(f"There is no server folder called '{key}'.")
    rel = rel.strip("/")
    if any(part.startswith(".") for part in Path(rel).parts):
        raise PickError(f"'{rel}' cannot be used (hidden files and '..' are not allowed).")
    base = r["path"]
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise PickError(f"'{rel}' is outside {r['label']}.")
    if not p.exists():
        raise PickError(f"'{rel}' was not found in {r['label']}.")
    return p


def _matches(name: str, accept: list[str]) -> bool:
    return not accept or name.lower().endswith(tuple(a.lower() for a in accept))


def _fastq_note(d: Path) -> str | None:
    try:
        j = json.loads((d / "job.json").read_text())
    except (OSError, ValueError):
        return None
    bits = [j.get("folder"), j.get("species"), j.get("status")]
    return " · ".join(str(b) for b in bits if b)


def browse(root: str | None = None, path: str = "", accept: list[str] | None = None) -> dict:
    """One folder's contents: sub-folders, and the files that fit `accept` (all files when it is empty)."""
    rs = roots()
    listing = [{"key": k, "label": v["label"], "path": str(v["path"])} for k, v in rs.items()]
    if not root:
        return {"roots": listing, "root": None, "path": "", "entries": [], "truncated": False}
    d = resolve(f"{root}:{path}")
    if not d.is_dir():
        raise PickError(f"'{path}' is a file, not a folder.")
    accept = accept or []
    entries, truncated = [], False
    try:
        items = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError as e:
        raise PickError(f"The site cannot read '{path or rs[root]['label']}'.") from e
    rel_dir = d.relative_to(rs[root]["path"]).as_posix() if d != rs[root]["path"] else ""
    for p in items:
        if p.name.startswith("."):
            continue
        try:
            is_dir = p.is_dir()
            if not is_dir and not _matches(p.name, accept):
                continue
            size = None if is_dir else p.stat().st_size
        except OSError:
            continue
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        e = {"name": p.name, "path": f"{rel_dir}/{p.name}".lstrip("/"), "dir": is_dir, "size": size}
        if root == "fastq" and not rel_dir and is_dir:
            e["note"] = _fastq_note(p)
        entries.append(e)
    parent = None if not rel_dir else (rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else "")
    return {"roots": listing, "root": root, "root_label": rs[root]["label"], "path": rel_dir, "parent": parent,
            "entries": entries, "truncated": truncated}
