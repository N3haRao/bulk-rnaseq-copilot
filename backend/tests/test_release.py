"""Code bank releases reach the site through integrate -> review -> approve (and rollback), on local git only.

A scripted stand-in plays the coding model, so the flow is tested without Ollama:
  A. a release the site already handles: no agent, ready, approved only by the releasing bioinformatician
  B. a release that needs a site change (a new upload handler): the agent edits extras.py in the staging copy,
     cannot touch the code bank, the release tooling or the contract test; the final checks are rerun by the
     command; approval merges it; rollback puts the previous version back
  C. a model that stops answering: not ready, approval refused
Needs CODEBANK_DIR to point at a code bank with site blocks (v0.6.0+), and R for the contract test.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import codebank
from app.config import settings
from app.llm.base import LLMUnavailable
from app.llm.scripted import ScriptedLLM
from app.release import cli
from app.release.workspace import ToolError, Workspace

SITE = Path(__file__).resolve().parents[2]


def has_site_blocks() -> bool:
    mods = codebank.manifest().get("modules") or {}
    return bool(mods) and all("site" in (m or {}) for m in mods.values())


pytestmark = pytest.mark.skipif(not has_site_blocks() or shutil.which("git") is None,
                                reason="needs a code bank with site blocks (v0.6.0+) and git")


def sh(cwd, *args):
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def tag(repo: Path, t: str, who: str = "alice"):
    sh(repo, "git", "-c", "user.name=x", "-c", "user.email=x@x", "tag", "-a", t, "-m", f"Code bank {t}\nreleased-by: {who}")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    sh(h, "git", "clone", "-q", str(settings.codebank_dir), "codebank")
    sh(h, "git", "clone", "-q", str(SITE), "copilot")
    # the live site = this working tree's code (committed or not), so the test checks what is being developed
    for d in ("backend/app", "backend/tests"):
        shutil.rmtree(h / "copilot" / d)
        shutil.copytree(SITE / d, h / "copilot" / d, ignore=shutil.ignore_patterns("__pycache__"))
    sh(h / "copilot", "git", "add", "-A")
    sh(h / "copilot", "git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "--allow-empty", "-m", "site under test")
    tag(h / "codebank", "v0.6.0")
    monkeypatch.setenv("RELEASE_FINAL_TESTS", "tests_quick")
    monkeypatch.delenv("COPILOT_RESTART_CMD", raising=False)
    return cli.Paths(home=h, codebank=h / "codebank", site=h / "copilot")


NEW_HANDLER = '''def _h_lines(spec: dict, uploads: list[Upload], folder: Path, ctx: dict) -> dict:
    """A plain text file, one item per line; the parameter gets its path."""
    name, fh = uploads[0]
    return {_sets(spec)[0]: str(_save_upload(fh, folder / Path(name).name))}


# name -> (handler, runs even when nothing was uploaded)'''


def add_toy_module(cb: Path):
    """Code bank v0.6.1: a module whose upload needs a handler the site does not have yet."""
    import yaml
    m = yaml.safe_load((cb / "manifest" / "tools.yaml").read_text())
    m["version"] = "0.6.1"
    m["modules"]["genelist"] = {
        "description": "Scores a gene list.", "entry_point": "genelist/run.R", "steps": [{"id": 1, "name": "G01", "critical": True}],
        "parameters": {"list_file": {"type": "path"}, "run_name": {"type": "string", "default": "genelist"}},
        "site": {"attach_to": "run", "title": "Gene list score",
                 "inputs": [{"name": "genes", "handler": "lines", "sets": "list_file", "label": "Gene list", "required": True}],
                 "results": {}}}
    (cb / "manifest" / "tools.yaml").write_text(yaml.safe_dump(m, sort_keys=False))
    (cb / "genelist").mkdir()
    (cb / "genelist" / "run.R").write_text("cat('Step 1/1: G01 ...\\n')\n")
    with open(cb / "CHANGELOG.md", "a") as f:
        f.write("\n## v0.6.1\n\n- New genelist module (upload: a plain gene list).\n")
    sh(cb, "git", "add", "-A")
    sh(cb, "git", "-c", "user.name=alice", "-c", "user.email=a@x", "commit", "-q", "-m", "genelist module")
    tag(cb, "v0.6.1")


def test_release_without_site_changes(home):
    pend = cli.integrate(home, "v0.6.0", agent_mode="auto", final_tests="tests_quick", out=lambda *_: None)
    assert pend["ready"] and not pend["agent_used"] and pend["files_changed"] == []
    review = (home.staging("v0.6.0") / "RELEASE_REVIEW.md").read_text()
    assert "Not needed: the site already works" in review and "ops/release approve v0.6.0" in review
    with pytest.raises(cli.ReleaseError, match="released by alice"):
        cli.approve(home, "v0.6.0", user="bob", out=lambda *_: None)
    live = cli.approve(home, "v0.6.0", user="alice", out=lambda *_: None)
    assert live["approved_by"] == "alice"
    assert (home.site / "CODEBANK_RELEASE").read_text().startswith("v0.6.0 ")
    assert home.live_link.resolve() == (home.releases / "v0.6.0").resolve()
    assert "Live: v0.6.0" in cli.status(home) and "Pending: none" in cli.status(home)


def test_release_that_needs_the_agent(home):
    cli.integrate(home, "v0.6.0", final_tests="tests_quick", out=lambda *_: None)
    cli.approve(home, "v0.6.0", user="alice", out=lambda *_: None)
    add_toy_module(home.codebank)
    ext = "backend/app/extras.py"
    script = ScriptedLLM([
        {"thought": "Try to fix the code bank instead.", "action": "write_file", "path": "codebank/manifest/tools.yaml", "content": "x"},
        {"thought": "Loosen the check.", "action": "replace_in_file", "path": "backend/tests/test_module_contract.py", "old": "a", "new": "b"},
        {"thought": "Change the tooling.", "action": "write_file", "path": "backend/app/release/cli.py", "content": "x"},
        {"thought": "What does the release need?", "action": "read_codebank", "path": "manifest/tools.yaml"},
        {"thought": "Find the handlers.", "action": "search", "query": "HANDLERS", "path": "backend/app"},
        {"thought": "Add a 'lines' handler.", "action": "replace_in_file", "path": ext,
         "old": "# name -> (handler, runs even when nothing was uploaded)", "new": NEW_HANDLER},
        {"thought": "Register it.", "action": "replace_in_file", "path": ext,
         "old": '    "annotation": (_h_annotation, True),\n}', "new": '    "annotation": (_h_annotation, True),\n    "lines": (_h_lines, False),\n}'},
        {"thought": "Check.", "action": "run_check", "check": "manifest"},
        {"thought": "Done.", "action": "finish", "summary": "Added a 'lines' upload handler to extras.py for the genelist module.",
         "manual_review": ["Open a run and check the Gene list score card."]},
    ])
    pend = cli.integrate(home, "v0.6.1", llm=script, final_tests="tests_quick", out=lambda *_: None)
    assert pend["agent_used"] and pend["files_changed"] == [ext], pend
    assert pend["ready"], (home.staging("v0.6.1") / "RELEASE_REVIEW.md").read_text()[-3000:]
    log = [json.loads(x) for x in (home.staging("v0.6.1") / "agent_log.jsonl").read_text().splitlines()]
    assert "off limits" in log[0]["result"] and "may not be changed" in log[1]["result"] and "may not be changed" in log[2]["result"]
    assert log[7]["result"].startswith("PASSED")
    review = (home.staging("v0.6.1") / "RELEASE_REVIEW.md").read_text()
    assert "Added a 'lines' upload handler" in review and "- [ ] Open a run" in review and "_h_lines" in review
    assert "## v0.6.1" in review                                  # the CHANGELOG entry is shown
    assert "_h_lines" not in (home.site / ext).read_text()        # live site untouched until approved
    cli.approve(home, "v0.6.1", user="alice", out=lambda *_: None)
    assert "_h_lines" in (home.site / ext).read_text()
    assert home.live_link.resolve() == (home.releases / "v0.6.1").resolve()
    cli.rollback(home, yes=True, user="alice", out=lambda *_: None)
    assert "_h_lines" not in (home.site / ext).read_text()
    assert home.live_link.resolve() == (home.releases / "v0.6.0").resolve()
    assert "Live: v0.6.0" in cli.status(home)


def test_model_that_stops_is_not_approvable(home):
    add_toy_module(home.codebank)
    pend = cli.integrate(home, "v0.6.1", llm=ScriptedLLM([LLMUnavailable("Ollama is not running")]), final_tests="tests_quick",
                         out=lambda *_: None)
    assert not pend["ready"] and pend["checks"]["manifest"] is False
    review = (home.staging("v0.6.1") / "RELEASE_REVIEW.md").read_text()
    assert "model_error" in review and "handler 'lines'" in review
    with pytest.raises(cli.ReleaseError, match="not ready"):
        cli.approve(home, "v0.6.1", user="alice", out=lambda *_: None)


def test_workspace_limits(tmp_path):
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "x.py").write_text("a = 1\na = 1\n")
    ws = Workspace(tmp_path, tmp_path)
    for bad in ("../outside.txt", "data/jobs/x.json", ".git/config", "codebank/R/utils.R"):
        with pytest.raises(ToolError):
            ws.write_file(bad, "x")
    with pytest.raises(ToolError, match="exactly once"):
        ws.replace_in_file("backend/app/x.py", "a = 1", "a = 2")
    with pytest.raises(ToolError, match="Unknown check"):
        ws.run_check("rm -rf /")
    assert ws.write_file("backend/app/new.py", "b = 2\n").startswith("Wrote") and ws.changed == {"backend/app/new.py"}
