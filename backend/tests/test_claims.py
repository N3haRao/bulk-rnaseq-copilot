"""Findings about samples (outliers, mislabels, quality) come from the code bank's checks, never from a model."""

import csv
import json

import pytest

from app.agents.claims import SampleFlags, unsupported
from app.agents.executor import ExecutorAgent
from app.agents.grounding import Grounding
from app.chat.scripted import ScriptedChat
from app.llm.scripted import ScriptedLLM

from test_chat import conv, make_run, use  # noqa: F401  (fixture)
from test_guardrails_setup_chat import answer, chat_with, upload  # noqa: F401  (fixtures)


def flag_run(run, outliers=(), qc=()):
    with open(run / "03_exploratory" / "outlier_flags.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sample_id", "group", "nearest_group", "flag", "message"])
        for i in range(6):
            s = f"s{i}"
            w.writerow([s, "WT" if i < 3 else "KO", "KO", "clusters_with_other_group" if s in outliers else "",
                        f"{s} sits closer to the KO samples." if s in outliers else ""])
    (run / "02_qc" / "qc_summary.json").write_text(json.dumps(
        {"flags": [{"sample_id": s, "flag": "small_library", "message": f"{s} has a small library."} for s in qc]}))
    return run


@pytest.fixture()
def run(tmp_path):
    return flag_run(make_run(tmp_path), outliers=["s2"], qc=["s4"])


def test_flags_are_read_from_the_code_bank_files(run):
    f = SampleFlags.from_run(run)
    assert f.samples == {f"s{i}" for i in range(6)}
    assert set(f.flags) == {"s2", "s4"} and "03_exploratory" in f.flags["s2"][0]["source"]
    assert "s2 (clusters_with_other_group)" in f.what_code_bank_says()


@pytest.mark.parametrize("sentence,ok", [
    ("s2 may be mislabelled: it sits with the KO samples.", True),          # the code bank flagged s2
    ("s4 has a small library and could be of low quality.", True),          # QC flagged s4
    ("s1 looks like an outlier in the PCA.", False),                        # not flagged: the model's own finding
    ("s3 and s5 might be swapped.", False),
    ("s1 is not an outlier.", True),
    ("s2 is not an outlier.", False),                                       # contradicts the code bank
    ("Gfap is higher in KO.", True),                                        # not about samples
    ("PC1 separates the groups.", True),
    ("One sample looks like an outlier.", True),                            # some sample was flagged
    ("s10 looks fine.", True),
])
def test_sample_claims(run, sentence, ok):
    assert (SampleFlags.from_run(run).problems(sentence) == []) is ok


def test_unnamed_claims_when_nothing_was_flagged(tmp_path):
    f = SampleFlags.from_run(flag_run(make_run(tmp_path)))
    assert f.problems("One sample looks like an outlier.")
    assert not f.problems("No sample was flagged as a possible outlier or mislabel.")
    assert "flagged no sample" in f.what_code_bank_says()


def test_report_drops_unsupported_sample_findings(run):
    g = Grounding("PC1 50", {"Gfap"}, sample_flags=SampleFlags.from_run(run))
    text, dropped = g.filter("PC1 explains 50% of the variance. Sample s1 looks like an outlier. Sample s2 may be mislabelled.")
    assert text == "PC1 explains 50% of the variance. Sample s2 may be mislabelled."
    assert dropped[0]["problems"] == ["s1: not flagged by the code bank's checks"]


def test_warning_explanations_cannot_add_sample_findings(run):
    summary = {"comparison": "KO vs WT", "group_sizes": {"WT": 3, "KO": 3}, "n_samples": 6,
               "warnings": ["[03_exploratory] s2 sits closer to the KO samples.", "[04] Few genes."]}
    llm = ScriptedLLM([{"headline": "s1 is an outlier.", "items": [
        {"index": 0, "title": "Possible mislabel", "explanation": "s2 may be mislabelled.", "what_to_do": "Check records.", "severity": "caution"},
        {"index": 1, "title": "Few genes", "explanation": "Also s1 looks like an outlier.", "what_to_do": "Remove s1.", "severity": "info"}]}])
    out = ExecutorAgent(llm).explain_run_warnings(summary, sample_flags=SampleFlags.from_run(run))
    assert out["items"][0]["title"] == "Possible mislabel"                  # explains the code bank's flag: kept
    assert out["items"][1]["title"] is None and out["items"][1]["explanation"] == "Few genes."   # own finding: original words
    assert out["headline"] is None


def test_chat_answer_with_its_own_outlier_call_is_labelled(conv):  # noqa: F811
    flag_run(conv.run_dir, outliers=["s2"])
    model = ScriptedChat([[{"type": "text", "text": "In the PCA, s1 looks like an outlier. Sample s2 may be mislabelled."}]])
    events = list(conv.send("any bad samples?", model))
    g = [e for e in events if e["type"] == "guardrail"]
    assert len(g) == 1 and "s1: not flagged" in g[0]["text"] and "s2 (clusters_with_other_group)" in g[0]["text"]
    assert conv.transcript()[-1]["role"] == "guardrail"
    model = ScriptedChat([[{"type": "text", "text": "The code bank flagged s2 as a possible mislabel."}]])
    assert not [e for e in conv.send("and now?", model) if e["type"] == "guardrail"]


def test_chat_rerun_leaving_out_an_unflagged_sample_is_labelled(conv):  # noqa: F811
    flag_run(conv.run_dir, outliers=["s2"])
    model = ScriptedChat([[use("propose_rerun", changes={"exclude_samples": ["s1", "s2"]}, reason="outliers")],
                          [{"type": "text", "text": "Proposed."}]])
    ev = [e for e in conv.send("leave out the bad ones", model) if e["type"] == "tool_end"][0]
    assert "did not flag s1" in ev["rerun"]["guardrail"] and "s2" not in ev["rerun"]["guardrail"].split(".")[0]


def test_setup_chat_cannot_leave_samples_out_on_its_own(upload):  # noqa: F811
    chat, _ = chat_with(upload, [answer(reply="s3 looks like an outlier, so I left it out.", settings={"exclude_samples": ["s3"]}),
                                 answer(settings={"exclude_samples": ["s5"]})])
    st = chat.send("WT vs KO")
    assert "exclude_samples" not in st["setup"]["advanced"]
    reply = st["transcript"][-1]["text"]
    assert "that was undone" in reply and "not from a code bank check" in reply
    st = chat.send("please leave out s5, it failed in the lab")
    assert st["setup"]["advanced"]["exclude_samples"] == ["s5"]           # the user asked: kept
