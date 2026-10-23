"""Checks that text written by a model only states what its facts contain.

Used by every agent that writes for the user. A sentence is kept only if
  * every number in it appears in the facts the model was given, and
  * every gene or pathway it names is one the facts mention (the model was not
    shown the other genes, so naming one means it came from its own memory).
"""

import re

NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")  # not digits inside names (TP53, Krt18, v1.2)
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")
# Pathway identifiers as they appear in gene-set collections: HALLMARK_..., GOBP_..., REACTOME_...
PATHWAY_ID = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+){1,}\b")
# Words shaped like gene symbols (letters with a digit: FKBP5, IL-6, Trp53, Elf5)
GENE_SHAPE = re.compile(r"^(?:[A-Z][A-Z0-9-]*\d[A-Z0-9-]*|[A-Z][a-z]+\d+[a-z0-9]*)$")
COMMON_WORDS = set("""
the and for are was were has had have not but all any can its may one two six ten top set max cat map pan
rest clock star fat hand spin tank coil arc bad car kit met ace aim ret mars pal ash gas era sun ski was
also both each more most less low high mean data cell cells gene genes group groups sample samples type
time day week dose with from into over than then this that these those they them their there here when
which while what where after before during between within without about above below under other same
""".split())
WORD = re.compile(r"[A-Za-z][A-Za-z0-9.\-]*[A-Za-z0-9]|[A-Za-z]")


def _norm(n: str) -> str:
    n = n.replace(",", "")
    return n.rstrip("0").rstrip(".") if "." in n else n


def numbers_ok(text: str, source: str) -> bool:
    """Every number in `text` also appears in `source` (the facts the model was given)."""
    allowed = {_norm(n) for n in NUMBER.findall(source)}
    allowed |= {"1", "2"}  # "one or two", "2 groups" phrasing is harmless
    return all(_norm(n) in allowed for n in NUMBER.findall(text))


# "plain name (PATHWAY_ID)": the plain words before an ID in brackets
NAMED_PATHWAY = re.compile(r"((?:[A-Za-z][\w-]*\s+){0,4}[A-Za-z][\w-]*)\s*\(\s*(" + PATHWAY_ID.pattern[2:-2] + r")\s*\)")


def _numbers(text: str) -> set[str]:
    return {_norm(n) for n in NUMBER.findall(text)}


class Grounding:
    """Sentence-level check against the facts and the full list of gene names in the run.

    `gene_records` (gene -> its facts entry) adds a stricter rule: in a sentence that
    names genes, each number must belong to one of those genes or to the general facts,
    so a value cannot be moved from one gene to another.
    """

    def __init__(self, facts_text: str, all_genes: set[str], gene_records: dict[str, dict] | None = None) -> None:
        self.facts_text = facts_text
        self.gene_numbers = {g: _numbers(" ".join(str(v) for v in rec.values())) for g, rec in (gene_records or {}).items()}
        general = facts_text
        for g, rec in (gene_records or {}).items():
            for v in rec.values():
                general = general.replace(str(v), " ")
        self.general_numbers = _numbers(general) | {"1", "2"}
        self.facts_words = set(WORD.findall(facts_text)) | set(PATHWAY_ID.findall(facts_text))
        # Gene symbols of this run, matched exactly (case-sensitive). Symbols that are also
        # everyday words ("SET", "REST", "Clock") are skipped so normal prose is not flagged.
        self.genes = {g for g in all_genes if g and len(g) >= 3 and g.lower() not in COMMON_WORDS}

    def problems(self, sentence: str) -> list[str]:
        out = []
        if not numbers_ok(sentence, self.facts_text):
            out.append("number not in the results")
        for w in WORD.findall(sentence):
            if w in self.facts_words:
                continue
            if w in self.genes:
                out.append(f"gene '{w}' not in the facts")
            elif GENE_SHAPE.match(w):
                out.append(f"gene-like name '{w}' not in the results")
        for p in PATHWAY_ID.findall(sentence):
            if p not in self.facts_words:
                out.append(f"pathway '{p}' not in the facts")
        # a value stated next to genes must be one of those genes' values
        named = [g for g in self.gene_numbers if g in set(WORD.findall(sentence))]
        if named:
            allowed = self.general_numbers.union(*(self.gene_numbers[g] for g in named))
            wrong = sorted(n for n in _numbers(sentence) if n not in allowed)
            if wrong:
                out.append(f"number(s) {', '.join(wrong)} do not belong to {', '.join(named)}")
        # "oxidative phosphorylation (HALLMARK_ESTROGEN_RESPONSE_EARLY)": the plain name must match the ID
        for name, pid in NAMED_PATHWAY.findall(sentence):
            id_words = [t.lower() for t in pid.split("_")]
            name_words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", name)]
            if name_words and not any(n[:5] == t[:5] for n in name_words for t in id_words):
                out.append(f"plain name before '{pid}' does not match it")
        return out

    def filter(self, text: str) -> tuple[str, list[dict]]:
        """(text with unsupported sentences removed, list of removed sentences with reasons)"""
        kept, dropped = [], []
        for s in split_sentences(text):
            p = self.problems(s)
            if p:
                dropped.append({"sentence": s, "problems": p})
            else:
                kept.append(s)
        return " ".join(kept), dropped


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    return [s for s in SENTENCE.split(text) if s]
