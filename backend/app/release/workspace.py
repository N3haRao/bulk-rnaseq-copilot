"""The release agent's only tools: read and edit files in the staging copy of the site, and run fixed checks.

The agent can never:
  - touch the code bank (methods are the bioinformatician's), user data, git internals or installed packages;
  - edit outside the allowed folders (EDITABLE);
  - run a command of its choice (only the checks in CHECKS, with fixed arguments).
"""

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

# Folders / files of the site the agent may change (relative to the staging root)
EDITABLE = ["backend/app/**", "backend/tests/**", "frontend/src/**", "docs/**", "README.md", "containers/env-*.yml",
            "containers/README.md"]
# Never readable or writable, even if under an allowed folder
FORBIDDEN = ["codebank/**", "data/**", ".git/**", "**/.git/**", "**/node_modules/**", "**/.venv/**", "**/__pycache__/**",
             "**/*.key", "**/.env", "**/secrets*", "CODEBANK_RELEASE"]
# Readable but never writable: the release tooling, the methods guardrails, and the check that keeps the site in
# step with the code bank (the agent must make the site pass it, not change it)
PROTECTED = ["backend/app/release/**", "backend/app/agents/prompts/release_agent.md", "backend/app/guardrails.py",
             "backend/tests/test_module_contract.py", "backend/tests/test_release.py"]
MAX_READ = 60_000          # characters returned by one read
MAX_FILE = 400_000         # largest file the agent may write


class ToolError(ValueError):
    """Shown to the agent as the result of its action, so it can correct itself."""


def _match(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or (p.endswith("/**") and (rel == p[:-3] or rel.startswith(p[:-2]))) for p in patterns)


class Workspace:
    def __init__(self, root: Path, codebank_dir: Path, python: str | None = None, test_timeout: int = 1800) -> None:
        self.root = root.resolve()
        self.codebank_dir = codebank_dir.resolve()
        self.python = python or sys.executable
        self.test_timeout = test_timeout
        self.changed: set[str] = set()

    # ---- paths ----
    def rel(self, path: str) -> str:
        p = (self.root / path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"'{path}' is outside the site's folder.")
        rel = p.relative_to(self.root).as_posix()
        if _match(rel, FORBIDDEN):
            raise ToolError(f"'{rel}' is off limits (code bank, data, git or installed packages).")
        return rel

    def editable(self, rel: str) -> bool:
        return _match(rel, EDITABLE) and not _match(rel, FORBIDDEN) and not _match(rel, PROTECTED)

    # ---- tools ----
    def list_files(self, path: str = ".") -> str:
        rel = self.rel(path) if path not in ("", ".") else ""
        base = self.root / rel
        if not base.is_dir():
            raise ToolError(f"'{path}' is not a folder.")
        out = []
        for p in sorted(base.rglob("*")):
            r = p.relative_to(self.root).as_posix()
            if p.is_file() and not _match(r, FORBIDDEN) and "dist/" not in r:
                out.append(r + ("" if self.editable(r) else "   (read only)"))
            if len(out) >= 400:
                out.append("... (more files; list a sub-folder)")
                break
        return "\n".join(out) or "(empty)"

    def read_file(self, path: str, start_line: int = 1) -> str:
        rel = self.rel(path)
        p = self.root / rel
        if not p.is_file():
            raise ToolError(f"No file '{rel}'.")
        lines = p.read_text(errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        text, n = [], 0
        for i in range(start - 1, len(lines)):
            line = f"{i + 1:5d}  {lines[i]}"
            n += len(line) + 1
            if n > MAX_READ:
                text.append(f"... (file continues; read again with start_line={i + 1})")
                break
            text.append(line)
        return f"{rel} ({len(lines)} lines)\n" + "\n".join(text)

    def read_codebank(self, path: str) -> str:
        """Read-only view of the NEW code bank release (manifest, docs, changelog, module files)."""
        p = (self.codebank_dir / path).resolve()
        if self.codebank_dir not in p.parents or not p.is_file():
            raise ToolError(f"No code bank file '{path}'.")
        t = p.read_text(errors="replace")
        return t if len(t) <= MAX_READ else t[:MAX_READ] + "\n... (truncated)"

    def search(self, query: str, path: str = ".") -> str:
        if not query:
            raise ToolError("search needs a query.")
        rel = self.rel(path) if path not in ("", ".") else ""
        base = self.root / rel
        pat = re.compile(re.escape(query), re.IGNORECASE)
        hits = []
        for p in sorted(base.rglob("*") if base.is_dir() else [base]):
            r = p.relative_to(self.root).as_posix()
            if not p.is_file() or _match(r, FORBIDDEN) or "dist/" in r or p.suffix in (".png", ".zip", ".gz", ".pdf", ".sif"):
                continue
            try:
                for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    if pat.search(line):
                        hits.append(f"{r}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 80:
                            return "\n".join(hits) + "\n... (more matches; narrow the search)"
            except OSError:
                continue
        return "\n".join(hits) or "No matches."

    def replace_in_file(self, path: str, old: str, new: str) -> str:
        rel = self.rel(path)
        if not self.editable(rel):
            raise ToolError(f"'{rel}' may not be changed by the release agent (allowed: {', '.join(EDITABLE)}).")
        p = self.root / rel
        if not p.is_file():
            raise ToolError(f"No file '{rel}' (use write_file to create one).")
        text = p.read_text()
        n = text.count(old) if old else 0
        if n != 1:
            raise ToolError(f"'old' must appear exactly once in {rel}; it appears {n} times. Read the file and copy the text exactly.")
        p.write_text(text.replace(old, new, 1))
        self.changed.add(rel)
        return f"Changed {rel}."

    def write_file(self, path: str, content: str) -> str:
        rel = self.rel(path)
        if not self.editable(rel):
            raise ToolError(f"'{rel}' may not be changed by the release agent (allowed: {', '.join(EDITABLE)}).")
        if len(content) > MAX_FILE:
            raise ToolError("File too large.")
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.changed.add(rel)
        return f"Wrote {rel} ({len(content.splitlines())} lines)."

    # ---- checks (fixed commands only) ----
    def env(self) -> dict:
        e = dict(os.environ)
        e["CODEBANK_DIR"] = str(self.codebank_dir)
        e["PYTHONDONTWRITEBYTECODE"] = "1"
        return e

    def run_check(self, check: str) -> tuple[bool, str]:
        if check not in CHECKS:
            raise ToolError(f"Unknown check '{check}' (known: {', '.join(CHECKS)}).")
        return CHECKS[check](self)


def _run(ws: Workspace, cmd: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, env=ws.env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout} s: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return False, f"Not available here: {e}"
    out = (r.stdout + "\n" + r.stderr).strip()
    return r.returncode == 0, out[-12000:]


def _check_manifest(ws: Workspace) -> tuple[bool, str]:
    code = ("import json, sys; from app import extras; p = extras.check_manifest(); "
            "print(json.dumps(p)); sys.exit(1 if p else 0)")
    ok, out = _run(ws, [ws.python, "-c", code], ws.root / "backend", 300)
    return ok, ("The site can show everything in the manifest." if ok else "Problems:\n" + out)


def _tests(ws: Workspace, args: list[str]) -> tuple[bool, str]:
    return _run(ws, [ws.python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *args], ws.root / "backend", ws.test_timeout)


def _frontend(ws: Workspace) -> tuple[bool, str]:
    fe = ws.root / "frontend"
    if not (fe / "node_modules").exists():
        return False, "frontend/node_modules is missing in the staging copy (the release command links it from the live site)."
    ok, out = _run(ws, ["npx", "tsc", "--noEmit", "-p", "."], fe, 600)
    if not ok:
        return False, out
    return _run(ws, ["npm", "run", "build"], fe, 900)


CHECKS = {
    "manifest": _check_manifest,
    "tests_quick": lambda ws: _tests(ws, ["tests/test_module_contract.py", "tests/test_unit.py"]),
    "tests_full": lambda ws: _tests(ws, []),
    "frontend": _frontend,
}
