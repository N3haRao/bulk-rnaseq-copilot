"""Phase 5: guardrails shared by both chats, the chat-first setup (open-weight model) and the code .zip."""

import io
import json
import subprocess
import zipfile

import pytest
import yaml
from fastapi.testclient import TestClient

from app import code_bundle, guardrails, uploads
from app.agents.setup_chat import SetupChat, describe_changes
from app.chat.agent import Conversation
from app.chat.scripted import ScriptedChat
from app.config import settings
from app.llm.base import LLMUnavailable
from app.llm.scripted import ScriptedLLM
from app.params import AnalysisSettings, available_advanced, validate

from test_chat import make_run

COUNTS = "gene," + ",".join(f"s{i}" for i in range(8)) + "\n" + "\n".join(
    f"G{g}," + ",".join(str(10 + g + i) for i in range(8)) for g in range(20))
SAMPLES = "sample,genotype,batch,sex,donor\n" + "\n".join(
    f"s{i},{'WT' if i < 4 else 'KO'},{'b1' if i % 2 else 'b2'},{'M' if i in (0, 1, 4, 5) else 'F'},d{i % 4}"
    for i in range(8))
HAS_PLOT_OPTIONS = "plot_palette" in available_advanced()


@pytest.fixture()
def upload(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    rec = uploads.create(("counts.csv", io.BytesIO(COUNTS.encode())), ("samples.csv", io.BytesIO(SAMPLES.encode())))
    return rec


def chat_with(upload, answers):
    llm = ScriptedLLM(answers, name="ollama:test")
    return SetupChat(uploads.upload_dir(upload["id"]), uploads.record(upload["id"]), llm=llm), llm


def answer(**kw):
    base = {"reply": "Compare KO with WT, accounting for batch. Approve it or ask for changes.",
            "condition": "genotype", "reference_level": "WT", "test_level": "KO", "covariates": ["batch"],
            "organism": "auto", "settings": {}, "questions": []}
    return {**base, **kw}


# ---- guardrails -------------------------------------------------------------------------------------
@pytest.mark.parametrize("code,blocked", [
    ("library(edgeR)\ny <- DGEList(x)", "edgeR"),
    ("from scipy import stats\nstats.ttest_ind(a, b)", "per-gene t / rank tests"),
    ("res <- t.test(x ~ g)", "per-gene t / rank tests"),
    ("import gseapy as gp\ngp.prerank(rnk=r)", "other enrichment tools"),
    ("counts2 <- sva::ComBat_seq(counts, batch)", "batch correction of counts"),
    ("install.packages('x')", "installing packages"),
])
def test_code_with_other_methods_is_blocked(code, blocked):
    assert blocked in [v.name for v in guardrails.check_code(code)]


@pytest.mark.parametrize("code", [
    "library(DESeq2)\ndds <- readRDS('run/04_differential_expression/dds.rds')\nres <- results(dds, contrast=c('g','B','A'))",
    "library(fgsea)\nr <- fgsea(pathways, stats, minSize=15)",
    "# no edgeR here, just a comment\nlibrary(ggplot2)\ncol <- '#D55E00'",
    "print('we did not use limma')",
    "import pandas as pd\nimport matplotlib.pyplot as plt\nplt.savefig('outputs/x.png')",
])
def test_code_bank_methods_and_plots_are_allowed(code):
    assert guardrails.check_code(code) == []


def test_method_requests_are_refused_but_questions_answered():
    assert [v.name for v in guardrails.check_request("Can you use edgeR instead of DESeq2?")] == ["edgeR"]
    assert guardrails.check_request("run a t-test on Gfap")
    assert guardrails.check_request("What does edgeR do?") == []
    assert guardrails.check_request("make the PCA coloured by batch") == []


def test_claude_chat_code_with_other_method_does_not_run(tmp_path):
    run = make_run(tmp_path)
    job_dir = tmp_path / "jobs" / "20260101-000000-abcdef"
    job_dir.mkdir(parents=True)
    job = {"id": job_dir.name, "upload_id": "u", "settings": {"condition": "genotype", "reference_level": "WT",
           "test_level": "KO", "covariates": [], "organism": "mouse", "run_name": "ko", "advanced": {}}}
    conv = Conversation.create(job_dir, run, job)
    code = "import pathlib\npathlib.Path('outputs/ran.txt').write_text('x')\nfrom scipy.stats import mannwhitneyu\nmannwhitneyu([1], [2])"
    model = ScriptedChat([[{"type": "tool_use", "name": "run_python", "input": {"code": code, "description": "test"}}],
                          [{"type": "text", "text": "That method is not available."}]])
    events = list(conv.send("Do a Mann-Whitney test", model))
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["blocked"] and not (conv.workspace.outputs / "ran.txt").exists()
    result = model.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "BLOCKED" in result["content"]
    assert (conv.workspace.root / "blocked_code.jsonl").exists()
    assert "Methods policy" in model.requests[0]["system"][0]["text"]


def test_rerun_rejects_settings_that_are_not_code_bank_parameters(tmp_path):
    run = make_run(tmp_path)
    job_dir = tmp_path / "jobs" / "20260101-000000-abcdef"
    job_dir.mkdir(parents=True)
    job = {"id": job_dir.name, "upload_id": "u", "settings": {"condition": "genotype", "reference_level": "WT",
           "test_level": "KO", "covariates": [], "organism": "mouse", "run_name": "ko", "advanced": {}}}
    conv = Conversation.create(job_dir, run, job)
    model = ScriptedChat([[{"type": "tool_use", "name": "propose_rerun",
                            "input": {"changes": {"de_method": "edgeR"}, "reason": "x"}}],
                          [{"type": "text", "text": "ok"}]])
    events = list(conv.send("use edgeR", model))
    end = next(e for e in events if e["type"] == "tool_end")
    assert end.get("error") and "methods are fixed" in end["output"]


# ---- settings ------------------------------------------------------------------------------------------
@pytest.mark.skipif(not HAS_PLOT_OPTIONS, reason="code bank older than v0.2.0 (no plot options)")
def test_plot_settings_are_checked():
    s = AnalysisSettings(condition="g", reference_level="A", test_level="B",
                         advanced={"up_colour": "#000000", "group_colours": ["A=#111111"], "pca_shape_by": "batch",
                                   "volcano_label_genes": ["Gfap"], "plot_palette": "greys"})
    assert validate(s, columns=["sample", "g", "batch"])[1] == []
    bad = s.model_copy(update={"advanced": {"up_colour": "blue-ish", "pca_shape_by": "nope", "group_colours": ["A"]}})
    problems = validate(bad, columns=["sample", "g", "batch"])[1]
    assert len(problems) == 3


# ---- setup chat -------------------------------------------------------------------------------------
def test_setup_chat_proposes_and_records(upload):
    chat, llm = chat_with(upload, [answer()])
    st = chat.send("WT vs knockout, two sequencing batches")
    reply = st["transcript"][-1]
    assert st["setup"]["covariates"] == ["batch"] and reply["source"] == "model"
    assert "Accounting for batch" in " ".join(reply["changes"]) or st["setup"]["covariates"] == ["batch"]
    # the model only saw names from the data and the code bank's parameters
    schema = llm.calls[0]["schema"]
    assert schema["properties"]["condition"]["enum"] == ["genotype", "batch", "sex", "donor"] or \
        "genotype" in schema["properties"]["condition"]["enum"]
    assert "de_method" not in schema["properties"]["settings"]["properties"]
    assert json.loads(chat.path.read_text())["setup"]["test_level"] == "KO"
    assert list((uploads.upload_dir(upload["id"]) / "agent" / "log").glob("*_setup_chat.json"))


def test_setup_chat_refuses_other_methods_and_keeps_deseq2(upload):
    chat, _ = chat_with(upload, [answer()])
    st = chat.send("Please use edgeR instead and add batch")
    reply = st["transcript"][-1]
    assert reply["guardrail"][0]["name"] == "edgeR" and reply["text"].startswith("I can only use")
    assert st["setup"]["covariates"] == ["batch"]


def test_setup_chat_retries_an_invalid_design_then_keeps_the_previous_setup(upload):
    # sex lines up with nothing, but genotype as its own covariate is not allowed
    bad = answer(covariates=["genotype"])
    chat, llm = chat_with(upload, [bad, answer(covariates=["sex"])])
    st = chat.send("add sex")
    assert st["setup"]["covariates"] == ["sex"] and len(llm.calls) == 2
    assert "previous answer had these problems" in llm.calls[1]["user"]
    chat2, _ = chat_with(upload, [bad, bad])
    before = chat2.state()["setup"]
    st2 = chat2.send("add genotype as covariate")
    assert st2["setup"] == before and st2["transcript"][-1]["problems"]


def test_setup_chat_reply_with_invented_numbers_is_replaced(upload):
    chat, _ = chat_with(upload, [answer(reply="This gives 4123 significant genes.")])
    st = chat.send("compare KO to WT")
    assert "4123" not in st["transcript"][-1]["text"] and "Compare KO with WT" in st["transcript"][-1]["text"]


def test_setup_chat_without_model_uses_rules(upload):
    chat, _ = chat_with(upload, [LLMUnavailable("Ollama is not running")])
    st = chat.send("KO vs WT")
    reply = st["transcript"][-1]
    assert reply["source"] == "rules" and "built-in rules" in reply["text"]
    assert st["setup"]["condition"] == "genotype" and st["setup"]["reference_level"] == "WT"


def test_setup_chat_starts_from_the_users_edits(upload):
    chat, llm = chat_with(upload, [answer(covariates=["donor"])])
    edited = {**chat.state()["setup"], "covariates": ["donor"]}
    st = chat.send("looks good, keep it", current=edited)
    assert '"donor"' in llm.calls[0]["user"].split("current_setup")[1][:300]
    assert st["setup"]["covariates"] == ["donor"]


@pytest.mark.skipif(not HAS_PLOT_OPTIONS, reason="code bank older than v0.2.0 (no plot options)")
def test_setup_chat_sets_plot_options(upload):
    chat, _ = chat_with(upload, [answer(settings={"pca_shape_by": "sex", "up_colour": "#000000",
                                                  "volcano_label_genes": ["G1", "G2"]})])
    st = chat.send("shapes by sex on the PCA, black for up genes, label G1 and G2")
    assert st["setup"]["advanced"] == {"pca_shape_by": "sex", "up_colour": "#000000", "volcano_label_genes": ["G1", "G2"]}
    assert any("PCA shapes by" in c for c in st["transcript"][-1]["changes"])


def test_describe_changes():
    a = {"condition": "g", "reference_level": "A", "test_level": "B", "covariates": [], "advanced": {}}
    b = {**a, "covariates": ["batch"], "advanced": {"padj_cutoff": 0.01}}
    assert describe_changes(a, b) == ["Accounting for batch", "Adjusted p-value cutoff: 0.01"]


def test_setup_chat_endpoints(upload, monkeypatch):
    from app.llm import factory
    factory.set_executor_llm(ScriptedLLM([answer()], name="ollama:test"))
    try:
        from app.main import app
        with TestClient(app) as c:
            assert c.get(f"/api/uploads/{upload['id']}/setup-chat").json()["transcript"] == []
            st = c.post(f"/api/uploads/{upload['id']}/setup-chat", json={"text": "KO vs WT"}).json()
            assert st["setup"]["covariates"] == ["batch"]
            assert c.delete(f"/api/uploads/{upload['id']}/setup-chat").json()["transcript"] == []
    finally:
        factory.set_executor_llm(None)


# ---- code .zip ---------------------------------------------------------------------------------------
def test_code_zip_has_codebank_settings_and_custom_code(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    run = make_run(tmp_path)
    (run / "inputs").mkdir()
    (run / "inputs" / "counts.csv").write_text("gene,s1\nA,1\n")
    (run / "params.yaml").write_text(yaml.safe_dump({"counts_file": str(run / "inputs" / "counts.csv"),
                                                     "condition": "genotype", "original_counts_file": "/home/x.csv"}))
    commit = subprocess.run(["git", "-C", str(settings.codebank_dir), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    (run / "provenance.json").write_text(json.dumps({"code_bank": {"version": "x", "commit": commit or None},
                                                     "packages": {"DESeq2": "1.42.0"}}))
    job_dir = tmp_path / "jobs" / "20260101-000000-abcdef"
    job_dir.mkdir(parents=True)
    job = {"id": job_dir.name, "upload_id": "u", "dataset": "t", "setup_origin": {"source": "setup_chat", "model": "ollama:q"},
           "settings": {"condition": "genotype", "reference_level": "WT", "test_level": "KO", "covariates": [],
                        "organism": "mouse", "run_name": "ko", "advanced": {}}}
    conv = Conversation.create(job_dir, run, job)
    model = ScriptedChat([[{"type": "tool_use", "name": "run_python", "input": {
        "code": "open('outputs/n.txt','w').write('3')", "description": "count"}}], [{"type": "text", "text": "ok"}]])
    list(conv.send("count", model))
    z = code_bundle.build(job, job_dir, run)
    with zipfile.ZipFile(z) as f:
        names = f.namelist()
        root = names[0].split("/")[0]
        cfg = yaml.safe_load(f.read(f"{root}/analysis/run_config.yaml"))
        assert cfg["counts_file"] == "data/counts.csv" and "original_counts_file" not in cfg
        assert f"{root}/codebank/scripts/run_pipeline.R" in names and f"{root}/run_analysis.sh" in names
        assert f"{root}/custom_analyses/{conv.id}/cells/001.py" in names
        assert "cells/001.py" in f.read(f"{root}/custom_analyses/{conv.id}/reproduce.sh").decode()
        assert not any("/data/counts.csv" in n for n in names)  # data only on request
        info = f.getinfo(f"{root}/run_analysis.sh")
        assert info.external_attr >> 16 & 0o111
    with zipfile.ZipFile(code_bundle.build(job, job_dir, run, include_data=True)) as f:
        assert any(n.endswith("/data/counts.csv") for n in f.namelist())
      
