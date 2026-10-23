"""Report agent: sentences are checked against the facts; sections fall back to the template."""

import json

from app.agents.grounding import Grounding
from app.agents.reporter import ReportAgent, template_narrative
from app.llm import LLMUnavailable
from app.llm.scripted import ScriptedLLM

FACTS = {
    "comparison": "KO vs WT (column 'genotype')", "condition_column": "genotype",
    "test_group": "KO", "reference_group": "WT", "accounted_for": ["sex"], "design": "~ sex + genotype",
    "organism": "mouse", "n_samples": 6, "group_sizes": {"WT": 3, "KO": 3}, "n_genes_tested": 14537,
    "significance_rule": "padj < 0.05 and |shrunken log2FC| > 1", "n_significant": 37, "n_up": 12, "n_down": 25,
    "fold_change_note": "log2FC > 0 means higher in KO; log2FC 1 = 2-fold",
    "top_higher_in_test": [{"gene": "Krt18", "log2FC": 3.83, "padj": "1.6e-62", "description": "keratin 18"}],
    "top_lower_in_test": [{"gene": "Myh11", "log2FC": -8.73, "padj": "<1e-300", "description": "myosin heavy chain 11"}],
    "pca": {"PC1_percent": 38.0, "PC2_percent": 22.5, "explained_by": {"genotype": {"PC1": 0.41, "PC2": 0.2}}},
    "flagged_samples": [], "qc_flags": [], "warnings": [],
    "pathways": {"H": {"n_significant_gsea": 4, "gsea_higher_in_test": [{"pathway": "HALLMARK_HYPOXIA", "padj": 0.001, "NES": 2.1}],
                       "gsea_lower_in_test": []}},
}
GENES = {"Krt18", "Myh11", "Acta2", "Elf5", "Gapdh"}


def answer(**kw) -> dict:
    base = {
        "summary": "Three KO and three WT samples were compared, accounting for sex. 37 genes changed: 12 higher and 25 lower in KO. "
                   "Myh11 had the largest decrease (log2FC -8.73).",
        "key_findings": ["37 genes pass the significance rule.", "Krt18 is higher in KO (log2FC 3.83).",
                         "Myh11 is lower in KO (log2FC -8.73)."],
        "sample_structure": "PC1 explains 38.0% of the variance.",
        "gene_highlights": "Krt18 (keratin 18) is higher in KO. Myh11 is lower in KO.",
        "pathway_highlights": "HALLMARK_HYPOXIA is higher in KO (NES 2.1).",
        "caveats": ["With 3 samples per group, smaller changes may be missed."],
        "next_steps": ["Confirm Krt18 and Myh11 by qPCR."],
    }
    return {**base, **kw}


def test_grounded_answer_is_kept_and_examples_are_in_the_prompt():
    llm = ScriptedLLM([answer()])
    out = ReportAgent(llm).write(FACTS, GENES)
    assert out["source"] == "model" and set(out["section_sources"].values()) == {"model"}
    assert out["dropped"] == []
    assert "Example 1 facts" in llm.calls[0]["system"] and "Example 2 answer" in llm.calls[0]["system"]
    assert json.loads(llm.calls[0]["user"].split("\n", 1)[1])["n_up"] == 12


def test_unsupported_sentences_are_removed():
    out = ReportAgent(ScriptedLLM([answer(
        gene_highlights="Krt18 (keratin 18) is higher in KO. Acta2 is also lower. Trp53 is induced. Myh11 is lower in KO.",
        pathway_highlights="HALLMARK_HYPOXIA is higher in KO (NES 2.1). HALLMARK_APOPTOSIS is lower.",
        key_findings=["37 genes pass the significance rule.", "About 20% of genes changed.",
                      "Krt18 is higher in KO (log2FC 3.83).", "Myh11 is lower in KO (log2FC -8.73)."],
    )])).write(FACTS, GENES)
    s = out["sections"]
    assert s["gene_highlights"] == "Krt18 (keratin 18) is higher in KO. Myh11 is lower in KO."
    assert s["pathway_highlights"] == "HALLMARK_HYPOXIA is higher in KO (NES 2.1)."
    assert "About 20% of genes changed." not in s["key_findings"] and len(s["key_findings"]) == 3
    reasons = " ".join(p for d in out["dropped"] for p in d["problems"])
    assert "Acta2" in reasons and "Trp53" in reasons and "HALLMARK_APOPTOSIS" in reasons and "number" in reasons


def test_mostly_wrong_section_is_replaced_by_the_template():
    out = ReportAgent(ScriptedLLM([answer(
        sample_structure="PC1 explains 45% of the variance. PC2 explains 30%. Sample KO_9 is an outlier.",
        key_findings=["About 20% changed.", "Acta2 is lower."],
    )])).write(FACTS, GENES)
    assert out["section_sources"]["sample_structure"] == "rules"
    assert out["sections"]["sample_structure"] == template_narrative(FACTS)["sample_structure"]
    assert out["section_sources"]["key_findings"] == "rules"
    assert out["section_sources"]["summary"] == "model"


def test_no_model_gives_a_complete_template_report(tmp_path):
    out = ReportAgent(ScriptedLLM([LLMUnavailable("Ollama is not running")])).write(FACTS, GENES, log_dir=tmp_path)
    assert out["source"] == "rules" and out["fallback_reason"] == "Ollama is not running"
    assert all(out["sections"][k] for k in ("summary", "key_findings", "gene_highlights", "pathway_highlights", "next_steps"))
    assert list(tmp_path.glob("*_write_report.json"))
    # the template itself only states facts
    g = Grounding(json.dumps(FACTS, indent=1), GENES)
    for k in ("summary", "sample_structure", "gene_highlights", "pathway_highlights"):
        assert g.filter(out["sections"][k])[1] == [], k


def test_example_numbers_are_not_copied():
    # 412 and GENEA1 come from the style example, not from these facts
    out = ReportAgent(ScriptedLLM([answer(summary="412 genes changed. GENEA1 had the largest increase.")])).write(FACTS, GENES)
    assert out["section_sources"]["summary"] == "rules"
