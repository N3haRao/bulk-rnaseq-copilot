"""Findings about samples come from the code bank, not from a model.

Rule: an assistant may explain what the code bank's checks found, but it may not find things itself.
Whether a sample is an outlier, mislabelled, swapped or of poor quality is decided by the code bank's own checks:

  02_qc/qc_summary.json                flags      low library size, small library, few detected genes
  03_exploratory/outlier_flags.csv     flag       clusters with another group / far from its own group (PCA)

A sentence that makes such a claim is supported only if it agrees with those files:
  * it names a sample the code bank flagged (or, with a negation, one it did not flag);
  * without naming a sample ("one sample looks like an outlier"), only if the code bank flagged at least one.
Everything else is an unsupported finding. The report and the warnings explainer drop such sentences; the chat and
the setup chat label them as the assistant's own observation, with what the code bank actually says.
"""

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CLAIM = re.compile(
    r"\b(outliers?|mis-?label(?:l?ed|s|ing)?|wrongly labell?ed|labell?ed wrong(?:ly)?|swap(?:ped|s)?|switched|"
    r"mix-?ups?|mixed up|contaminat\w*|low[- ]quality|poor[- ]quality|degraded|failed (?:library|sample)|"
    r"odd one out|does ?n[o']t (?:cluster|group)|do not (?:cluster|group)|clusters? (?:with|among) the (?:other|wrong)|"
    r"(?:should|could|might) be (?:removed|excluded|dropped)|(?:remove|exclude|drop) (?:it|this sample|that sample|sample)|"
    r"anomal\w*|suspicious|problematic|(?:low|small) library|few detected genes)\b", re.I)
NEGATION = re.compile(r"\b(not|no|never|none|nothing|without|neither|nor)\b|n't\b", re.I)
SOME_SAMPLE = re.compile(r"\b(a|one|two|three|some|several|\d+|another|this|that|the) (?:of the )?samples?\b", re.I)


@dataclass
class SampleFlags:
    """What the code bank's checks say about each sample of a run."""
    samples: set[str] = field(default_factory=set)
    flags: dict[str, list[dict]] = field(default_factory=dict)   # sample -> [{source, flag, message}]

    @classmethod
    def from_run(cls, run_dir: Path | None) -> "SampleFlags":
        sf = cls()
        if not run_dir:
            return sf
        run_dir = Path(run_dir)
        meta = run_dir / "01_validate_inputs" / "metadata_validated.csv"
        for path in (meta, run_dir / "03_exploratory" / "outlier_flags.csv", run_dir / "02_qc" / "qc_metrics.csv"):
            if path.exists():
                with open(path, newline="") as f:
                    for r in csv.DictReader(f):
                        if r.get("sample_id"):
                            sf.samples.add(r["sample_id"])
        of = run_dir / "03_exploratory" / "outlier_flags.csv"
        if of.exists():
            with open(of, newline="") as f:
                for r in csv.DictReader(f):
                    if (r.get("flag") or "").strip():
                        sf.flags.setdefault(r["sample_id"], []).append(
                            {"source": "code bank step 03_exploratory (outlier_flags.csv)", "flag": r["flag"], "message": r.get("message", "")})
        qs = run_dir / "02_qc" / "qc_summary.json"
        if qs.exists():
            try:
                for r in json.loads(qs.read_text()).get("flags") or []:
                    if r.get("sample_id"):
                        sf.flags.setdefault(r["sample_id"], []).append(
                            {"source": "code bank step 02_qc (qc_summary.json)", "flag": r.get("flag", ""), "message": r.get("message", "")})
            except ValueError:
                pass
        return sf

    def _named(self, sentence: str) -> list[str]:
        return [s for s in sorted(self.samples, key=len, reverse=True)
                if re.search(rf"(?<![\w.-]){re.escape(s)}(?![\w-])", sentence)]

    def problems(self, sentence: str) -> list[str]:
        """Why this sentence states a sample finding the code bank did not make ([] when it is fine)."""
        if not CLAIM.search(sentence):
            return []
        negated = bool(NEGATION.search(sentence))
        named = self._named(sentence)
        if named:
            if negated:
                wrong = [s for s in named if s in self.flags]
                return [f"says {s} is fine, but the code bank flagged it" for s in wrong]
            return [f"{s}: not flagged by the code bank's checks" for s in named if s not in self.flags]
        if not negated and not self.flags and SOME_SAMPLE.search(sentence):
            return ["a sample finding, but the code bank flagged no sample"]
        return []

    def what_code_bank_says(self) -> str:
        if not self.flags:
            return ("The code bank's checks (02_qc: library size and detected genes; 03_exploratory: PCA position "
                    "against the groups) flagged no sample.")
        parts = [f"{s} ({', '.join(sorted({x['flag'] for x in v}))})" for s, v in sorted(self.flags.items())]
        return "The code bank's checks flagged: " + "; ".join(parts) + "."


def split(text: str) -> list[str]:
    from .grounding import split_sentences
    return split_sentences(text)


def unsupported(text: str, flags: SampleFlags) -> list[dict]:
    """[{sentence, problems}] for the sentences of `text` that state sample findings the code bank did not make."""
    out = []
    for s in split(text):
        p = flags.problems(s)
        if p:
            out.append({"sentence": s, "problems": p})
    return out


def note(found: list[dict], flags: SampleFlags) -> str:
    """The label shown under an answer that made its own sample findings."""
    return ("Guardrail: this answer says something about samples that the code bank's checks did not find ("
            + "; ".join(sorted({p for f in found for p in f["problems"]})) + "). Treat it as the assistant's own "
            "observation, not a result. " + flags.what_code_bank_says())
