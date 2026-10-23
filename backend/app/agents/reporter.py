"""Report agent (open-weight model): writes the narrative parts of the report.

The model gets a compact set of facts read from the run folder (counts, top
genes with their descriptions, PCA, pathways, warnings) plus style examples,
and returns the narrative sections as JSON. Every sentence is then checked
(grounding.py): a sentence with a number, gene or pathway that is not in the
facts is removed. A section left too thin is replaced by a template written
from the same facts, so the report is always complete. Which parts came from
the model, and every removed sentence, are recorded.

Figures, tables, methods and settings in the report never come from the model.
"""

import csv
import json
from pathlib import Path

from .. import results
from ..llm import LLMClient, LLMError, LLMUnavailable, executor_llm
from .executor import _Recorder, _prompt
from ..setup_proposal import _confounded
from .grounding import Grounding, split_sentences

EXAMPLES = Path(__file__).parent / "report_examples"
TEXT_SECTIONS = ("summary", "sample_structure", "gene_highlights", "pathway_highlights")
LIST_SECTIONS = ("key_findings", "caveats", "next_steps")
N_TOP_GENES = 8


def _p(x) -> str:
    if x is None:
        return "NA"
    return "<1e-300" if x == 0 else f"{x:.2g}"


def _r(x, digits=2):
    """Round for the facts; None for missing values. digits=0 gives an integer."""
    if x in (None, "", "NA"):
        return None
    v = round(float(x), digits)
    return int(v) if digits == 0 else v


def build_facts(run_dir: Path) -> tuple[dict, set[str]]:
    """Facts for the narrative, and every gene name in the run (for the grounding check)."""
    ov = results.overview(run_dir)
    s, p, de = ov["summary"], ov["resolved_params"], ov["de"] or {}
    test, ref = p["test_level"], p["reference_level"]

    def top(direction: str, sort: str = "padj", order: str = "asc", n: int = N_TOP_GENES) -> list[dict]:
        rows = results.de_table(run_dir, direction=direction, sort=sort, order=order, page_size=n)["rows"]
        out = []
        for r in rows:
            g = {"gene": r.get("gene_symbol") or r["gene_id"], "log2FC": _r(r["log2FoldChange"]), "padj": _p(r["padj"]),
                 f"mean_{ref}": _r(r.get(f"mean_normalized_{ref}"), 0),
                 f"mean_{test}": _r(r.get(f"mean_normalized_{test}"), 0)}
            if r.get("gene_name"):
                g["description"] = r["gene_name"]
            out.append(g)
        return out

    ex = ov["exploratory"]["summary"] or {}
    facts: dict = {
        "comparison": f"{test} vs {ref} (column '{p['condition']}')",
        "condition_column": p["condition"],
        "test_group": test, "reference_group": ref,
        "accounted_for": p["covariates"], "design": p.get("design_formula"),
        "organism": p.get("organism"),
        "n_samples": s["n_samples"], "group_sizes": s["group_sizes"],
        "n_genes_tested": s["n_genes_tested"], "significance_rule": s["significance_rule"],
        "n_significant": s["n_significant"], "n_up": s["n_up"], "n_down": s["n_down"],
        "fold_change_note": f"log2FC > 0 means higher in {test}; log2FC 1 = 2-fold",
        "gene_lists_note": ("top_higher_in_test / top_lower_in_test are sorted by adjusted p-value (strongest evidence), "
                            "not by size of change; largest_increase / largest_decrease are sorted by log2FC"),
        "top_higher_in_test": top("up"), "top_lower_in_test": top("down"),
        "largest_increase_in_test": top("up", "log2FoldChange", "desc", 5),
        "largest_decrease_in_test": top("down", "log2FoldChange", "asc", 5),
        **_sample_table(run_dir, p["condition"]),
        "pca": {"PC1_percent": (ex.get("percent_variance") or {}).get("PC1"),
                "PC2_percent": (ex.get("percent_variance") or {}).get("PC2"),
                "explained_by": {a["variable"]: {"PC1": a["r2_PC1"], "PC2": a["r2_PC2"]} for a in ex.get("pc_association", [])}},
        "flagged_samples": [f["message"] for f in ex.get("flagged_samples", [])],
        "qc_flags": [f["message"] for f in ((ov["qc"]["summary"] or {}).get("flags") or [])],
        "warnings": [w.split("] ", 1)[-1] for w in s.get("warnings") or []],
    }
    en = ov["enrichment"]
    if not en or en.get("status") != "done":
        facts["pathways"] = f"not run: {(en or {}).get('reason', 'no enrichment results')}"
    else:
        pw = {}
        for name, c in list((en.get("collections") or {}).items())[:4]:
            g = c.get("gsea") or {}
            entry = {"n_significant_gsea": g.get("n_significant"),
                     "gsea_higher_in_test": (g.get("top_higher_in_test") or [])[:5],
                     "gsea_lower_in_test": (g.get("top_higher_in_reference") or [])[:5]}
            for k in ("ora_up", "ora_down"):
                if isinstance(c.get(k), dict):
                    entry[f"{k}_top"] = [{kk: t[kk] for kk in ("pathway", "padj", "n_overlap") if kk in t}
                                         for t in (c[k].get("top") or [])[:5]]
            pw[name] = entry
        facts["pathways"] = pw

    return facts, results.gene_names(run_dir)


def _sample_table(run_dir: Path, condition: str) -> dict:
    """The sample table's columns with their values, and columns that line up with the comparison."""
    path = run_dir / "01_validate_inputs" / "metadata_validated.csv"
    if not path.exists():
        return {}
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    cols = [c for c in (rows[0].keys() if rows else []) if c != "sample_id"]
    values = {c: [r[c] for r in rows] for c in cols}
    table = {}
    for c in cols:
        counts: dict[str, int] = {}
        for v in values[c]:
            counts[v] = counts.get(v, 0) + 1
        table[c] = counts if len(counts) <= 12 else f"{len(counts)} different values"
    lined_up = [c for c in cols if c != condition and 1 < len(set(values[c])) < len(rows)
                and _confounded(values[c], values[condition])]
    return {"sample_table_columns": table, "lines_up_with_comparison": lined_up}


def gene_records(facts: dict) -> dict[str, dict]:
    """gene -> its entries in the facts (for the check that numbers stay with their gene)."""
    recs: dict[str, dict] = {}
    for key in ("top_higher_in_test", "top_lower_in_test", "largest_increase_in_test", "largest_decrease_in_test"):
        for g in facts.get(key) or []:
            recs.setdefault(g["gene"], {}).update({k: v for k, v in g.items() if k != "description"})
    return recs


def template_narrative(f: dict) -> dict:
    """The same sections written from the facts by rules (fallback, and for sections the model got wrong)."""
    test, ref = f["test_group"], f["reference_group"]
    up, down = f["top_higher_in_test"], f["top_lower_in_test"]
    adj = f" accounting for {', '.join(f['accounted_for'])}" if f["accounted_for"] else ""
    pca = f["pca"]
    cond_r2 = (pca["explained_by"].get(f["condition_column"]) or {}).get("PC1")
    pc_sentence = (f"PC1 explains {pca['PC1_percent']}% of the variance between samples"
                   + (" and lines up with the comparison" if cond_r2 is not None and cond_r2 >= 0.5 else "")
                   + ".") if pca.get("PC1_percent") is not None else ""
    if isinstance(f["pathways"], str):
        pathway_text = f"Pathway analysis was {f['pathways']}."
    else:
        bits = []
        for name, c in f["pathways"].items():
            hi = c["gsea_higher_in_test"][:1]
            lo = c["gsea_lower_in_test"][:1]
            part = f"{name}: {c['n_significant_gsea']} gene sets significant by GSEA"
            if hi:
                part += f"; top higher in {test} {hi[0]['pathway']} (NES {hi[0]['NES']})"
            if lo:
                part += f"; top lower in {test} {lo[0]['pathway']} (NES {lo[0]['NES']})"
            bits.append(part + ".")
        pathway_text = " ".join(bits)
    genes_text = []
    if up:
        genes_text.append(f"Higher in {test}: " + ", ".join(f"{g['gene']} (log2FC {g['log2FC']})" for g in up[:3]) + ".")
    if down:
        genes_text.append(f"Lower in {test}: " + ", ".join(f"{g['gene']} (log2FC {g['log2FC']})" for g in down[:3]) + ".")
    caveats = list(f["warnings"])
    for c in f.get("lines_up_with_comparison") or []:
        caveats.append(f"'{c}' lines up exactly with '{f['condition_column']}', so its effect cannot be separated from the comparison.")
    small = [g for g, n in f["group_sizes"].items() if n < 3]
    if small:
        caveats.append(f"Fewer than 3 samples in: {', '.join(small)}; results are less reliable.")
    return {
        "summary": " ".join(x for x in [
            f"{f['n_samples']} samples were analysed, comparing {test} with {ref}{adj}.",
            f"{f['n_significant']} genes pass the significance rule ({f['significance_rule']}): "
            f"{f['n_up']} higher and {f['n_down']} lower in {test}.",
            (f"The genes with the strongest evidence are {up[0]['gene']} (higher) and {down[0]['gene']} (lower)."
             if up and down else ""),
            pc_sentence,
            f"{len(f['warnings'])} warnings were raised; see Things to check." if f["warnings"] else "No warnings were raised.",
        ] if x),
        "key_findings": [
            f"{f['n_significant']} of {f['n_genes_tested']} tested genes pass the significance rule: "
            f"{f['n_up']} higher and {f['n_down']} lower in {test}.",
            *([f"Strongest increase: {up[0]['gene']} (log2FC {up[0]['log2FC']}, adjusted p-value {up[0]['padj']})."] if up else []),
            *([f"Strongest decrease: {down[0]['gene']} (log2FC {down[0]['log2FC']}, adjusted p-value {down[0]['padj']})."] if down else []),
            *([pc_sentence] if pc_sentence else []),
        ],
        "sample_structure": " ".join([pc_sentence] + (f["flagged_samples"] or ["No sample was flagged as a possible outlier or mislabel."])),
        "gene_highlights": " ".join(genes_text) or "No genes pass the significance rule.",
        "pathway_highlights": pathway_text,
        "caveats": caveats,
        "next_steps": [
            "Confirm a few of the top genes by qPCR or in independent samples.",
            "Look at the PCA and sample-distance plots for samples that do not group as expected.",
        ],
    }


class ReportAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or executor_llm()

    @staticmethod
    def system_prompt() -> str:
        parts = [_prompt("write_report")]
        for i, path in enumerate(sorted(EXAMPLES.glob("*.json")), start=1):
            ex = json.loads(path.read_text())
            parts.append(f"\nExample {i} facts:\n{json.dumps(ex['facts_excerpt'])}\n"
                         f"Example {i} answer:\n{json.dumps(ex['narrative'])}")
        return "\n".join(parts)

    def write(self, facts: dict, all_genes: set[str], *, log_dir: Path | None = None) -> dict:
        template = template_narrative(facts)
        rec = _Recorder(log_dir, "write_report", self.llm.name)
        facts_text = json.dumps(facts, indent=1, default=str)
        str_prop = {"type": "string"}
        schema = {
            "type": "object",
            "properties": {
                **{k: str_prop for k in TEXT_SECTIONS},
                "key_findings": {"type": "array", "items": str_prop, "minItems": 3, "maxItems": 6},
                "caveats": {"type": "array", "items": str_prop, "maxItems": 4},
                "next_steps": {"type": "array", "items": str_prop, "maxItems": 3},
            },
            "required": [*TEXT_SECTIONS, *LIST_SECTIONS],
        }
        user = "Facts:\n" + facts_text
        try:
            out = self.llm.chat_json(self.system_prompt(), user, schema)
        except (LLMUnavailable, LLMError) as e:
            rec.attempt(user, None, [], error=str(e))
            return rec.finish({"source": "rules", "model": None, "sections": template,
                               "section_sources": {k: "rules" for k in template}, "dropped": [],
                               "fallback_reason": str(e)})

        g = Grounding(facts_text, all_genes, gene_records(facts))
        sections, sources, dropped = {}, {}, []
        for k in TEXT_SECTIONS:
            text, d = g.filter(str(out.get(k, "")))
            dropped += [{**x, "section": k} for x in d]
            original = len(split_sentences(str(out.get(k, ""))))
            # keep the model's text if most of it survived the check; otherwise use the template
            if text and len(split_sentences(text)) >= max(1, (original + 1) // 2):
                sections[k], sources[k] = text, "model"
            else:
                sections[k], sources[k] = template[k], "rules"
        for k in LIST_SECTIONS:
            kept = []
            for item in out.get(k) or []:
                p = g.problems(" ".join(str(item).split()))
                if p:
                    dropped.append({"sentence": item, "problems": p, "section": k})
                else:
                    kept.append(str(item).strip())
            minimum = 3 if k == "key_findings" else 1
            if len(kept) >= minimum or (k == "caveats" and not out.get(k)):
                sections[k], sources[k] = kept, "model"
            else:
                sections[k], sources[k] = template[k], "rules"
        # a warning the model did not mention still has to reach the reader
        if facts["warnings"] and sources["caveats"] == "model" and not sections["caveats"]:
            sections["caveats"], sources["caveats"] = template["caveats"], "rules"
        rec.attempt(user, out, [f"{d['section']}: {d['sentence']} ({'; '.join(d['problems'])})" for d in dropped])
        n_model = sum(v == "model" for v in sources.values())
        return rec.finish({"source": "model" if n_model else "rules", "model": self.llm.name if n_model else None,
                           "sections": sections, "section_sources": sources, "dropped": dropped})
