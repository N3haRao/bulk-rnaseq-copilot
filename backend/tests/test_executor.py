"""Executor agent: model answers are checked, retried, or replaced by the rules."""

import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.executor import ExecutorAgent
from app.agents.grounding import numbers_ok
from app.config import settings
from app.llm import LLMUnavailable
from app.llm.factory import set_executor_llm
from app.llm.ollama import OllamaClient
from app.llm.scripted import ScriptedLLM

MAMMARY = """id,cell_type,status,lane
a,basal,virgin,L2
b,basal,virgin,L2
c,basal,pregnant,L2
d,basal,pregnant,L2
e,luminal,virgin,L1
f,luminal,virgin,L1
g,luminal,pregnant,L1
h,luminal,pregnant,L1"""


def table(text: str):
    rows = [ln.split(",") for ln in text.strip().splitlines()]
    return rows[0], rows[1:]


def answer(**kw) -> dict:
    base = {"condition": "cell_type", "reference_level": "basal", "test_level": "luminal", "covariates": ["status"],
            "explanation": "Luminal cells are compared with basal cells, accounting for pregnancy status.",
            "questions": [], "warnings": []}
    return {**base, **kw}


def test_valid_model_answer_is_used(tmp_path):
    llm = ScriptedLLM([answer(reference_level="luminal", test_level="basal")])
    out = ExecutorAgent(llm).propose_setup(*table(MAMMARY), log_dir=tmp_path)
    assert out["source"] == "model" and out["model"] == "scripted"
    assert (out["reference_level"], out["test_level"]) == ("luminal", "basal")
    assert out["differs_from_rules"] is True
    # the schema only allows real columns and values
    schema = llm.calls[0]["schema"]["properties"]
    assert set(schema["condition"]["enum"]) == {"cell_type", "status", "lane"}
    assert "lane" in schema["covariates"]["items"]["enum"]
    # facts tell the model that lane lines up with cell type
    facts = json.loads(llm.calls[0]["user"].split("\n", 1)[1])
    assert next(c for c in facts["columns"] if c["name"] == "cell_type")["lines_up_with"] == ["lane"]
    log = json.loads(next(tmp_path.glob("*_propose_setup.json")).read_text())
    assert log["result"]["source"] == "model" and len(log["attempts"]) == 1


def test_confounded_covariate_is_retried_then_accepted(tmp_path):
    llm = ScriptedLLM([answer(covariates=["status", "lane"]), answer()])
    out = ExecutorAgent(llm).propose_setup(*table(MAMMARY), log_dir=tmp_path)
    assert out["source"] == "model" and out["covariates"] == ["status"]
    assert "lines up exactly" in llm.calls[1]["user"]  # the retry names the problem
    log = json.loads(next(tmp_path.glob("*_propose_setup.json")).read_text())
    assert len(log["attempts"]) == 2 and log["attempts"][0]["problems"]


def test_wrong_level_twice_falls_back_to_rules():
    llm = ScriptedLLM([answer(reference_level="virgin"), answer(test_level="basal")])
    out = ExecutorAgent(llm).propose_setup(*table(MAMMARY))
    assert out["source"] == "rules" and out["condition"] == "cell_type" and out["covariates"] == ["status"]
    assert "did not pass" in out["fallback_reason"]


def test_unavailable_model_falls_back_immediately():
    llm = ScriptedLLM([LLMUnavailable("Ollama is not running")])
    out = ExecutorAgent(llm).propose_setup(*table(MAMMARY))
    assert out["source"] == "rules" and out["fallback_reason"] == "Ollama is not running"
    assert len(llm.calls) == 1


def test_invented_numbers_are_not_shown():
    llm = ScriptedLLM([answer(explanation="This finds about 5,000 differentially expressed genes.")])
    out = ExecutorAgent(llm).propose_setup(*table(MAMMARY))
    assert out["source"] == "model"
    assert "5,000" not in out["explanation"]  # replaced by the rule-based reasons


def test_duplicate_covariates_are_removed():
    header, rows = table("""id,treatment,donor
a,vehicle,D1
b,vehicle,D2
c,drug,D1
d,drug,D2""")
    llm = ScriptedLLM([{"condition": "treatment", "reference_level": "vehicle", "test_level": "drug",
                        "covariates": ["donor", "donor"], "explanation": "", "questions": [], "warnings": []}])
    out = ExecutorAgent(llm).propose_setup(header, rows)
    assert out["source"] == "model" and out["covariates"] == ["donor"]  # duplicates removed


def test_numbers_ok():
    assert numbers_ok("Groups have 6 samples.", '{"basal": 6}')
    assert not numbers_ok("About 40 genes.", '{"basal": 6}')


def test_explain_checks_keeps_only_grounded_items():
    checks = [{"id": "sample_matching", "status": "fail",
               "message": "These sample columns in the counts file have no row in the metadata: 'S6' (closest metadata ID: 'S_6')."},
              {"id": "replicates", "status": "warning", "message": "Group 'KO' has 2 replicates."},
              {"id": "files_found", "status": "pass", "message": "Both input files were found."}]
    llm = ScriptedLLM([{"items": [
        {"id": "sample_matching", "explanation": "One sample name differs.", "how_to_fix": "Rename 'S_6' to 'S6' in the sample table."},
        {"id": "replicates", "explanation": "Only 2 samples; at least 12 are ideal.", "how_to_fix": "Add samples."},
    ]}])
    out = ExecutorAgent(llm).explain_checks(checks)
    assert [i["id"] for i in out["items"]] == ["sample_matching"]  # "12" was not in the facts
    assert [p["id"] for p in json.loads(llm.calls[0]["user"].split("\n", 1)[1])["problems"]] == ["sample_matching", "replicates"]


def test_run_warnings_missing_items_keep_original_text():
    summary = {"comparison": "KO vs WT", "group_sizes": {"WT": 3, "KO": 3}, "n_samples": 6,
               "warnings": ["[qc:low_library] Sample KO_2 has 0.8 million reads.", "[pca:outlier] WT_1 is far from its group."]}
    llm = ScriptedLLM([{"headline": "One warning needs a look.", "items": [
        {"index": 1, "title": "Possible outlier", "explanation": "WT_1 differs from the other WT samples.",
         "what_to_do": "Check WT_1 in the PCA plot.", "severity": "caution"}]}])
    out = ExecutorAgent(llm).explain_run_warnings(summary)
    assert out["items"][1]["title"] == "Possible outlier"
    assert out["items"][0]["explanation"] == "Sample KO_2 has 0.8 million reads."  # original, tag removed


def test_ollama_client_request_and_errors(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        sent.update(url=url, body=json)
        return httpx.Response(200, json={"message": {"content": '{"a": 1}'}})

    monkeypatch.setattr(httpx, "post", fake_post)
    c = OllamaClient("qwen3:8b")
    assert c.chat_json("sys", "user", {"type": "object"}) == {"a": 1}
    assert sent["url"].endswith("/api/chat")
    assert sent["body"]["format"] == {"type": "object"} and sent["body"]["think"] is False
    assert sent["body"]["options"]["temperature"] == 0

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(404, text="model not found"))
    with pytest.raises(LLMUnavailable, match="ollama pull qwen3:8b"):
        c.chat_json("s", "u", {})

    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(LLMUnavailable, match="not running"):
        c.chat_json("s", "u", {})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.main import app
    with TestClient(app) as c:
        yield c
    set_executor_llm(None)


def test_agent_setup_endpoint_caches_model_answers(client, tmp_path):
    counts = b"gene_id,a,b,c,d,e,f,g,h\n" + b"\n".join(f"G{i},1,2,3,4,5,6,7,8".encode() for i in range(5))
    up = client.post("/api/uploads", files={"counts": ("c.csv", io.BytesIO(counts)),
                                            "metadata": ("m.csv", io.BytesIO(MAMMARY.encode()))}).json()
    llm = ScriptedLLM([answer()])
    set_executor_llm(llm)
    first = client.post(f"/api/uploads/{up['id']}/agent/setup").json()
    second = client.post(f"/api/uploads/{up['id']}/agent/setup").json()
    assert first["source"] == second["source"] == "model" and len(llm.calls) == 1
    assert list((tmp_path / "uploads" / up["id"] / "agent" / "log").glob("*_propose_setup.json"))

    set_executor_llm(ScriptedLLM([LLMUnavailable("not running")]))
    fb = client.post(f"/api/uploads/{up['id']}/agent/setup", params={"condition": "status"}).json()
    assert fb["source"] == "rules" and fb["condition"] == "status"
    assert client.get("/api/agent/status").json()["available"] is True
