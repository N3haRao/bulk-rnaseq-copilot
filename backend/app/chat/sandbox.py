"""Where the chat assistant's own code runs.

Each conversation gets a workspace:

    workspace/run/       a COPY of the analysis run folder (results, figures, dds.rds ...)
    workspace/outputs/   everything the assistant makes (plots, tables, slides)
    workspace/cells/     every script it ran, numbered, kept as the record

The original run folder is never touched. Code runs as a separate process in
the workspace, with a time limit, no API keys or tokens in its environment,
file-size and (on Linux) memory limits. This is a convenience boundary for a
single-user laptop trial, not a security boundary: on the BCM server the same
interface should run inside a container without network access.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings

TIMEOUT_S = int(os.environ.get("CHAT_CODE_TIMEOUT", "300"))
MAX_OUTPUT_CHARS = 12000
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|ANTHROPIC|AWS_|GITHUB|GH_", re.I)
OUTPUT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".pdf", ".csv", ".tsv", ".xlsx", ".txt", ".md", ".html",
                     ".pptx", ".docx", ".json", ".gmt", ".zip"}


@dataclass
class RunResult:
    ok: bool
    stdout: str
    stderr: str
    seconds: float
    new_files: list[str] = field(default_factory=list)  # paths relative to the workspace
    script: str = ""
    timed_out: bool = False


class Workspace:
    def __init__(self, root: Path, run_dir: Path) -> None:
        self.root = root
        self.run_copy = root / "run"
        self.outputs = root / "outputs"
        self.cells = root / "cells"
        if not self.run_copy.exists():
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(run_dir, self.run_copy, ignore=shutil.ignore_patterns("*.zip"))
        self.outputs.mkdir(exist_ok=True)
        self.cells.mkdir(exist_ok=True)

    # ---- paths the model may name ---------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        """A path inside the workspace (run/... or outputs/...), or ValueError."""
        rel = rel.strip()
        if rel.startswith(("/", "~")) or (len(rel) > 1 and rel[1] == ":"):
            raise ValueError(f"'{rel}' is outside the workspace; use paths starting with run/ or outputs/.")
        p = (self.root / rel).resolve()
        if p != self.root.resolve() and self.root.resolve() not in p.parents:
            raise ValueError(f"'{rel}' is outside the workspace; use paths starting with run/ or outputs/.")
        return p

    def rel(self, p: Path) -> str:
        return str(p.resolve().relative_to(self.root.resolve()))

    def output_files(self) -> dict[str, float]:
        return {self.rel(p): p.stat().st_mtime for p in self.outputs.rglob("*") if p.is_file()}

    # ---- running code --------------------------------------------------------------------------
    def run(self, language: str, code: str) -> RunResult:
        n = len(list(self.cells.glob("*"))) + 1
        if language == "python":
            script = self.cells / f"{n:03d}.py"
            cmd = [sys.executable, "-I", str(script)]
        elif language == "r":
            script = self.cells / f"{n:03d}.R"
            cmd = [settings.rscript, "--vanilla", str(script)]
        else:
            raise ValueError(f"Unknown language {language!r}")
        script.write_text(code)
        before = self.output_files()
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, cwd=self.root, env=_clean_env(self.root), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True,
                                    preexec_fn=_limits if os.name == "posix" else None)
        except FileNotFoundError as e:
            return RunResult(False, "", f"Could not start {cmd[0]}: {e}", 0.0, script=self.rel(script))
        timed_out = False
        try:
            out, err = proc.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()
            err += f"\nStopped: the code ran longer than {TIMEOUT_S} seconds."
        after = self.output_files()
        new = sorted(f for f, m in after.items() if before.get(f) != m and Path(f).suffix.lower() in OUTPUT_EXTENSIONS)
        (self.cells / f"{n:03d}.log").write_text(f"exit {proc.returncode}\n--- stdout\n{out}\n--- stderr\n{err}")
        return RunResult(proc.returncode == 0 and not timed_out, _cut(out), _cut(err),
                         round(time.monotonic() - t0, 1), new, self.rel(script), timed_out)


def _clean_env(home: Path) -> dict[str, str]:
    """The environment for the assistant's code: no credentials, plots without a screen."""
    env = {k: v for k, v in os.environ.items() if not SECRET_NAME.search(k)}
    env.update(HOME=str(home), MPLBACKEND="Agg", MPLCONFIGDIR=str(home / ".mpl"), PYTHONDONTWRITEBYTECODE="1",
               # no keychain for the assistant's code: the saved API key stays out of reach of ordinary code
               PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
    return env


def _limits() -> None:  # pragma: no cover - runs in the child process
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**3, 2 * 1024**3))  # no file over 2 GB
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))  # 8 GB of memory


def _cut(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return text[:half] + f"\n... [{len(text) - MAX_OUTPUT_CHARS} characters cut] ...\n" + text[-half:]
