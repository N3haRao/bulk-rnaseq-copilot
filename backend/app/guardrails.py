"""Guardrails shared by both assistants: the methods are the code bank's.

Dr. Hari's rule for the trial: an assistant may adjust what the code bank lets
it adjust (covariates, cutoffs, plot colours and shapes, which genes to label)
but never swap a method (no edgeR instead of DESeq2, no other enrichment tool
instead of fgsea). The policy is read from the code bank's manifest
(`methods.allowed` / `methods.not_allowed`), so it stays tied to the code bank.

Three checks, all in code (not only in a prompt):
  check_code()     code an assistant wants to run: blocked if it uses a method
                   that is not allowed, or installs packages
  check_request()  a user message asking for a method that is not allowed:
                   the assistant answers with a fixed explanation
  settings are checked by params.validate (only manifest parameters exist)
"""

import re
from dataclasses import dataclass
from functools import lru_cache

from . import codebank

# Used only with code banks older than v0.2.0, whose manifest has no `methods` section.
FALLBACK_POLICY = {
    "allowed": {
        "normalisation": {"method": "DESeq2 median-of-ratios size factors", "packages": ["DESeq2"]},
        "transformation": {"method": "DESeq2 variance-stabilising transformation (vst, blind)", "packages": ["DESeq2"]},
        "exploratory": {"method": "PCA (prcomp) on the most variable VST genes; Euclidean sample distances"},
        "differential_expression": {"method": "DESeq2 negative binomial GLM, Wald test", "packages": ["DESeq2"]},
        "fold_change_shrinkage": {"method": "apeglm (default), ashr or normal", "packages": ["apeglm", "ashr"]},
        "multiple_testing": {"method": "Benjamini-Hochberg adjusted p-values"},
        "pathway_enrichment": {"method": "fgsea pre-ranked GSEA and fgsea::fora over-representation",
                               "packages": ["fgsea", "msigdbr"]},
    },
    "not_allowed": [
        {"name": "edgeR", "detect": ["edgeR", "DGEList", "calcNormFactors", "glmQLFit", "glmQLFTest", "exactTest", "glmLRT"],
         "instead": "DESeq2 (the code bank's differential expression)"},
        {"name": "limma / voom", "detect": ["limma", "voom", "lmFit", "eBayes", "topTable", "removeBatchEffect"],
         "instead": "DESeq2 with covariates in the design"},
        {"name": "other DE tools", "detect": ["NOISeq", "baySeq", "sleuth", "EBSeq", "DSS", "pydeseq2", "diffxpy"],
         "instead": "DESeq2 (the code bank's differential expression)"},
        {"name": "per-gene t / rank tests", "detect": ["t.test", "wilcox.test", "ttest_ind", "ttest_rel", "mannwhitneyu",
                                                       "wilcoxon", "ranksums", "kruskal"],
         "instead": "DESeq2 for gene-level tests"},
        {"name": "batch correction of counts", "detect": ["ComBat", "ComBat_seq", "RUVSeq", "RUVg", "svaseq"],
         "instead": "add the batch column as a covariate"},
        {"name": "other enrichment tools", "detect": ["clusterProfiler", "enricher", "enrichGO", "enrichKEGG", "gseGO",
                                                      "gseKEGG", "ReactomePA", "enrichR", "enrichr", "gseapy", "GSVA",
                                                      "gsva", "ssgsea", "topGO", "goseq", "GOstats", "gage", "decoupler",
                                                      "gprofiler"],
         "instead": "fgsea (GSEA and fora), as in step 06"},
    ],
}

# Installing software makes an analysis impossible to reproduce from the code .zip
INSTALL = re.compile(r"install\.packages\s*\(|BiocManager\s*::\s*install|remotes\s*::\s*install|devtools\s*::\s*install"
                     r"|\bpip3?\s+install\b|\bconda\s+install\b|\bmamba\s+install\b")
# Words in a user message that ask for something to be done with a method
ASKING = re.compile(r"\b(use|using|run|rerun|switch|change|instead|try|with|via|do|apply|redo|compare|"
                    r"swap|replace|prefer|want|could|can|please|perform|calculate|test)\b", re.I)
ACTING = re.compile(r"\b(use|run|rerun|switch|instead|try|apply|redo|swap|replace|perform|calculate)\b", re.I)
# "What does edgeR do?" is a question to answer, not a request to refuse
QUESTION = re.compile(r"^\s*(what|why|how|explain|describe|tell me|is|are|does|difference|compare\w* (it|this|deseq2) (to|with))\b", re.I)
# Plain-language names people use that are not package names
REQUEST_ALIASES = {
    "per-gene t / rank tests": ["t-test", "t test", "ttest", "wilcoxon", "mann-whitney", "mann whitney", "rank test"],
    "batch correction of counts": ["combat", "batch correct", "batch-correct", "remove the batch effect"],
    "limma / voom": ["limma", "voom"],
    "other enrichment tools": ["clusterprofiler", "enrichr", "gsea software", "david", "gprofiler", "g:profiler",
                               "metascape", "gsva", "ipa", "ingenuity"],
}


@dataclass
class Violation:
    name: str       # e.g. "edgeR"
    matched: str    # what was found, e.g. "glmQLFit"
    instead: str    # what the code bank uses instead

    def message(self) -> str:
        if self.name == "installing packages":
            return (f"Installing software ({self.matched}) is not allowed: the analysis must be reproducible from the "
                    "code bank and the packages already installed.")
        return f"{self.name} ({self.matched}) is not one of the code bank's methods. Instead: {self.instead}."

    def as_dict(self) -> dict:
        return {"name": self.name, "matched": self.matched, "instead": self.instead, "message": self.message()}


def policy() -> dict:
    """The code bank's methods policy (`methods` in manifest/tools.yaml)."""
    return codebank.manifest().get("methods") or FALLBACK_POLICY


def policy_source() -> str:
    return "code bank manifest" if codebank.manifest().get("methods") else "built-in copy (code bank older than v0.2.0)"


@lru_cache(maxsize=4)
def _code_patterns(key: int) -> list[tuple[re.Pattern, dict]]:
    out = []
    for entry in policy()["not_allowed"]:
        for term in entry.get("detect") or []:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._]*", term):
                continue  # phrases such as "quantile normalization" only matter in requests
            t = re.escape(term)
            # used as a package (library(x), x::f, import x, from x import) or called as a function x(
            pat = (rf"(?:library|require|requireNamespace|loadNamespace)\s*\(\s*['\"]?{t}['\"]?\s*[,)]"
                   rf"|(?<![\w.]){t}\s*:::?"
                   rf"|^\s*(?:import|from)\s+{t}\b"
                   rf"|(?<![\w$@]){t}\s*\(")  # also stats.ttest_ind( and stats::t.test(
            out.append((re.compile(pat, re.M), entry))
    return out


def _strip_comments(code: str) -> str:
    """Code without comment text (a mention in a comment is not a use). Strings are kept."""
    lines = []
    for line in code.splitlines():
        out, quote = [], None
        for i, ch in enumerate(line):
            if quote:
                if ch == quote and line[i - 1] != "\\":
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "#":
                break
            out.append(ch)
        lines.append("".join(out))
    return "\n".join(lines)


def check_code(code: str) -> list[Violation]:
    """Methods that are not allowed, used in Python or R code (empty list = allowed)."""
    text = _strip_comments(code)
    found: dict[str, Violation] = {}
    for pat, entry in _code_patterns(id(codebank.manifest())):
        m = pat.search(text)
        if m and entry["name"] not in found:
            term = re.sub(r"^(?:library|require|requireNamespace|loadNamespace|import|from)\W*|\W+$", "", m.group(0).strip())
            found[entry["name"]] = Violation(entry["name"], term or m.group(0).strip(), entry.get("instead", ""))
    if m := INSTALL.search(text):
        found["installing packages"] = Violation("installing packages", m.group(0).strip(" ("), "")
    return list(found.values())


def check_request(text: str) -> list[Violation]:
    """A user message asking to use a method that is not allowed."""
    if not ASKING.search(text) or (QUESTION.search(text) and not ACTING.search(text)):
        return []
    low = text.lower()
    found = []
    for entry in policy()["not_allowed"]:
        terms = [t for t in entry.get("detect") or [] if len(t) >= 4 and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", t)]
        terms += REQUEST_ALIASES.get(entry["name"], [])
        for t in terms:
            if re.search(rf"(?<![\w-]){re.escape(t.lower())}(?![\w-])", low):
                found.append(Violation(entry["name"], t, entry.get("instead", "")))
                break
    return found


def refusal(violations: list[Violation]) -> str:
    """The fixed answer to a request for a method that is not allowed."""
    parts = [f"{v.matched}: the code bank does not use {v.name}; it uses {v.instead}." for v in violations]
    return ("I can only use the lab's tested code bank methods, so I can't switch methods. " + " ".join(parts) +
            " I can still change the settings the code bank offers: the comparison, covariates, cutoffs, samples "
            "to leave out, gene-set collections, and plot colours, shapes and labels.")


def describe_for_prompt() -> str:
    """The policy as text for the assistants' instructions."""
    pol = policy()
    allowed = "\n".join(f"- {k.replace('_', ' ')}: {v['method'] if isinstance(v, dict) else v}"
                        for k, v in pol["allowed"].items())
    not_allowed = "\n".join(f"- {e['name']} ({', '.join((e.get('detect') or [])[:6])}...): use {e.get('instead')}"
                            for e in pol["not_allowed"])
    return f"Allowed methods (the code bank's):\n{allowed}\n\nNot allowed (blocked automatically):\n{not_allowed}"
  
