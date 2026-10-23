"""Chat assistant: sandbox, tool loop, failure handling and the streaming endpoint (scripted model)."""

import io
import json
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from app.chat import sandbox
from app.chat.agent import Conversation
from app.chat.model import set_chat_model
from app.chat.scripted import ScriptedChat
from app.config import settings


def make_run(root):
    """A tiny run folder with the files the chat reads."""
    run = root / "runs" / "20260101-000000_test"
    for d in ("01_validate_inputs", "02_qc", "03_exploratory", "04_differential_expression", "05_de_plots"):
        (run / d).mkdir(parents=True)
    (run / "01_validate_inputs" / "resolved_params.yaml").write_text(yaml.safe_dump({
        "condition": "genotype", "reference_level": "WT", "test_level": "KO", "covariates": ["sex"],
        "organism": "mouse", "gene_id_type": "symbol", "min_count": 10, "min_samples": 3,
        "design_formula": "~ sex + genotype", "exclude_samples": []}))
    (run / "01_validate_inputs" / "metadata_validated.csv").write_text(
        "sample_id,genotype,sex\n" + "\n".join(f"s{i},{'WT' if i < 3 else 'KO'},{'M' if i % 2 else 'F'}" for i in range(6)))
    (run / "04_differential_expression" / "de_results_all.csv").write_text(
        "gene_id,gene_symbol,gene_name,baseMean,log2FoldChange,lfcSE,stat,pvalue,padj,mean_normalized_WT,mean_normalized_KO,significant,direction,note\n"
        "Gfap,Gfap,glial fibrillary acidic protein,500,2.5,0.3,8,1e-10,1e-8,200,1100,TRUE,up,\n"
        "Krt5,Krt5,keratin 5,900,-3.1,0.4,-7,1e-9,5e-8,1500,180,TRUE,down,\n"
        "Actb,Actb,actin beta,20000,0.05,0.1,0.5,0.6,0.8,20000,20500,FALSE,not significant,\n")
    (run / "04_differential_expression" / "de_summary.json").write_text(json.dumps({
        "padj_cutoff": 0.05, "log2fc_cutoff": 1, "lfc_shrinkage_used": "apeglm", "sign_convention": "positive = higher in KO"}))
    (run / "run_summary.json").write_text(json.dumps({
        "status": "success", "comparison": "KO vs WT", "design_formula": "~ sex + genotype", "warnings": [],
        "n_samples": 6, "group_sizes": {"WT": 3, "KO": 3}, "n_genes_tested": 3, "n_significant": 2, "n_up": 1,
        "n_down": 1, "significance_rule": "padj < 0.05 and |shrunken log2FC| > 1", "top_up": [], "top_down": []}))
    (run / "05_de_plots" / "volcano_plot.png").write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                      "1f15c4890000000d49444154789c6360000000000200015e2c2c0b0000000049454e44ae426082"))
    return run


@pytest.fixture()
def conv(tmp_path):
    run = make_run(tmp_path)
    job_dir = tmp_path / "jobs" / "20260101-000000-abcdef"
    job_dir.mkdir(parents=True)
    job = {"id": job_dir.name, "upload_id": "20260101-000000-123456", "dataset": "test",
           "settings": {"condition": "genotype", "reference_level": "WT", "test_level": "KO", "covariates": ["sex"],
                        "organism": "mouse", "run_name": "ko", "advanced": {}}}
    return Conversation.create(job_dir, run, job)


def use(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def test_tool_loop_answers_from_files(conv):
    model = ScriptedChat([[{"type": "text", "text": "Checking."}, use("find_genes", genes=["Gfap", "Nope1"])],
                          [{"type": "text", "text": "Gfap is higher in KO (log2FC 2.5)."}]])
    events = list(conv.send("Is Gfap upregulated?", model))
    assert [e["type"] for e in events if e["type"] != "text"] == ["tool_start", "tool_end", "done"]
    # the second request carries the tool result, in one user message after the tool call
    msgs = model.requests[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    result = json.loads(msgs[2]["content"][0]["content"])
    assert result["rows"][0]["gene_symbol"] == "Gfap" and result["not_found"] == ["Nope1"]
    # the system prompt holds the run's facts and is cached
    system = model.requests[0]["system"][0]
    assert "KO vs WT" in system["text"] and system["cache_control"] == {"type": "ephemeral"}
    roles = [t["role"] for t in conv.transcript()]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_code_runs_in_sandbox_without_secrets(conv, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    code = ("import os, pathlib\n"
            "print('secret' if any('ANTHROPIC' in k for k in os.environ) else 'clean')\n"
            "pathlib.Path('outputs/table.csv').write_text('a,b\\n1,2\\n')\n"
            "pathlib.Path('run/run_summary.json').write_text('changed')\n")
    model = ScriptedChat([[use("run_python", code=code, description="test")], [{"type": "text", "text": "Done."}]])
    events = list(conv.send("make a table", model))
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["ok"] and "clean" in end["output"] and end["files"] == ["outputs/table.csv"]
    # the original run folder is untouched; only the workspace copy changed
    assert json.loads((conv.run_dir / "run_summary.json").read_text())["status"] == "success"
    assert (conv.dir / "workspace" / "cells" / "001.py").read_text() == code


def test_timeout_and_path_escape(conv, monkeypatch):
    monkeypatch.setattr(sandbox, "TIMEOUT_S", 2)
    r = conv.workspace.run("python", "import time; time.sleep(30)")
    assert not r.ok and r.timed_out and "longer than 2 seconds" in r.stderr
    with pytest.raises(ValueError):
        conv.workspace.resolve("../../job.json")


def test_tool_errors_go_back_to_the_model(conv):
    model = ScriptedChat([[use("read_file", path="/etc/passwd")], [use("find_genes", bogus=1)],
                          [{"type": "text", "text": "Sorry."}]])
    list(conv.send("read it", model))
    first = model.requests[1]["messages"][-1]["content"][0]
    assert first["is_error"] and "outside the workspace" in first["content"]
    second = model.requests[2]["messages"][-1]["content"][0]
    assert second["is_error"] and "Invalid arguments" in second["content"]


def test_rerun_is_proposed_not_started(conv):
    model = ScriptedChat([[use("propose_rerun", changes={"padj_cutoff": 0.01, "exclude_samples": ["s1"]}, reason="stricter")],
                          [use("propose_rerun", changes={"design": "~ batch"}, reason="x")],
                          [{"type": "text", "text": "Proposed."}]])
    events = list(conv.send("use a stricter cutoff", model))
    ends = [e for e in events if e["type"] == "tool_end"]
    rerun = ends[0]["rerun"]
    assert rerun["settings"]["advanced"] == {"padj_cutoff": 0.01, "exclude_samples": ["s1"]}
    assert rerun["settings"]["condition"] == "genotype" and rerun["upload_id"] == "20260101-000000-123456"
    assert ends[1]["error"]  # 'design' cannot be changed


def test_refusal_takes_the_exchange_back_out(conv):
    model = ScriptedChat([[{"type": "text", "text": "", "stop_reason": "refusal"}]])
    events = list(conv.send("something", model))
    assert events[-1]["type"] == "error" and "declined" in events[-1]["text"]
    assert json.loads((conv.dir / "messages.json").read_text()) == []
    assert conv.transcript()[-1]["role"] == "error"


def test_view_image_returns_an_image_block(conv):
    model = ScriptedChat([[use("view_image", path="run/05_de_plots/volcano_plot.png")], [{"type": "text", "text": "ok"}]])
    list(conv.send("look", model))
    content = model.requests[1]["messages"][-1]["content"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"


def test_streaming_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    run = make_run(tmp_path)
    from app.main import app, jobs
    job_id = "20260101-000000-abcdef"
    (settings.jobs_dir / job_id).mkdir(parents=True)
    (settings.jobs_dir / job_id / "job.json").write_text(json.dumps({
        "id": job_id, "upload_id": "u", "status": "success", "run_dir": str(run), "dataset": "test",
        "settings": {"condition": "genotype", "reference_level": "WT", "test_level": "KO", "covariates": [],
                     "organism": "mouse", "run_name": "ko", "advanced": {}}}))
    set_chat_model(ScriptedChat([[use("run_python", code="open('outputs/p.txt','w').write('hi')", description="file")],
                                 [{"type": "text", "text": "Made **p.txt**."}]]))
    try:
        with TestClient(app) as c:
            assert c.get("/api/chat/status").json()["configured"] is True
            conv_id = c.post(f"/api/runs/{job_id}/chats").json()["id"]
            with c.stream("POST", f"/api/runs/{job_id}/chats/{conv_id}/messages", json={"text": "make a file"}) as r:
                assert r.headers["content-type"].startswith("text/event-stream")
                events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
            assert events[-1]["type"] == "done"
            assert "".join(e["text"] for e in events if e["type"] == "text") == "Made **p.txt**."
            f = c.get(f"/api/runs/{job_id}/chats/{conv_id}/files/outputs/p.txt")
            assert f.status_code == 200 and f.text == "hi"
            assert c.get(f"/api/runs/{job_id}/chats/{conv_id}/files/../../job.json").status_code == 404
            assert c.get(f"/api/runs/{job_id}/chats").json()[0]["title"] == "make a file"
    finally:
        set_chat_model(None)
    _ = (jobs, io, os)
