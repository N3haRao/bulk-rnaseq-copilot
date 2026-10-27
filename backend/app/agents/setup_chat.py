"""Setup chat (open-weight model): the user describes the experiment, the model proposes the setup.

The opening page is a conversation instead of a form. Each user message gets a
reply and a complete setup (comparison, covariates, settings, plot options),
shown as a card the user edits or approves before anything runs.

Guardrails, all in code:
  * the model answers in a JSON schema built from the data and the code bank's
    manifest, so it can only name existing columns, groups and samples, and only
    set the code bank's parameters (there is no field for a method);
  * the setup is checked (design can be estimated, covariates not confounded,
    every setting valid for this code bank version); a failing answer is retried
    once with the problems listed, then the previous setup is kept;
  * a request for a method that is not the code bank's gets a fixed answer
    (guardrails.check_request);
  * reply text with numbers that are not in the facts or the user's messages is
    replaced by a plain description of the setup.

The conversation is stored per upload in data/uploads/<id>/setup_chat/main.json
and every model call is logged under agent/log/, like the other agents.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import codebank, guardrails, tables
from ..llm import LLMClient, LLMError, LLMUnavailable, executor_llm
from ..params import AnalysisSettings, available_advanced, manifest_params, validate
from ..setup_proposal import _confounded, describe_columns, propose
from .executor import ExecutorAgent, _Recorder
from .grounding import numbers_ok

PROMPT = Path(__file__).parent / "prompts" / "setup_chat.md"
MAX_ROWS_SHOWN = 40
HISTORY_TURNS = 6
CONV_ID = "main"
_locks: dict[str, threading.Lock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SetupChat:
    def __init__(self, upload_dir: Path, record: dict, llm: LLMClient | None = None) -> None:
        self.dir = upload_dir
        self.rec = record
        self.llm = llm or executor_llm()
        self.path = upload_dir / "setup_chat" / f"{CONV_ID}.json"
        self.header, self.rows = tables.read_table(upload_dir / record["metadata_file"])
        self.sample_col, self.cols = describe_columns(self.header, self.rows)
        self.by_name = {c.name: c for c in self.cols}

    # ---- state ----------------------------------------------------------------------------
    def state(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"id": CONV_ID, "created": _now(), "transcript": [], "setup": self.starting_setup(),
                "source": "rules", "model": None}

    def _save(self, st: dict) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str))
        tmp.replace(self.path)

    def starting_setup(self) -> dict:
        """Before the first answer: the example's curated setup, or the rule-based guess."""
        ex = None
        if self.rec.get("example"):
            ex = next((e for e in codebank.examples() if e["name"] == self.rec["example"]), None)
        if ex:
            c = ex["config"]
            covs = c.get("covariates") or []
            return {"condition": c.get("condition"), "reference_level": c.get("reference_level"),
                    "test_level": c.get("test_level"), "covariates": covs if isinstance(covs, list) else [covs],
                    "organism": c.get("organism", "auto"), "run_name": self.rec["example"], "advanced": {}}
        _, p = propose(self.header, self.rows)
        return {"condition": p.condition, "reference_level": p.reference_level, "test_level": p.test_level,
                "covariates": p.covariates, "organism": "auto", "run_name": "analysis", "advanced": {}}

    # ---- one message ----------------------------------------------------------------------------
    def send(self, text: str, current: dict | None = None) -> dict:
        lock = _locks.setdefault(str(self.dir), threading.Lock())
        with lock:
            return self._send(text.strip(), current)

    def _send(self, text: str, current: dict | None) -> dict:
        st = self.state()
        if current:  # the user edited the setup card since the last answer
            st["setup"] = {**st["setup"], **current}
        before = st["setup"]
        st["transcript"].append({"role": "user", "text": text or "(no description given)", "at": _now()})
        violations = guardrails.check_request(text)
        rec = _Recorder(self.dir / "agent" / "log", "setup_chat", self.llm.name)
        answer = self._ask_model(text, st, before, violations, rec)
        if violations:
            answer["text"] = guardrails.refusal(violations) + " " + answer["text"]
            answer["guardrail"] = [v.as_dict() for v in violations]
        answer["changes"] = describe_changes(before, answer["setup"])
        answer.update(role="assistant", at=_now())
        st["transcript"].append(answer)
        st["setup"], st["source"], st["model"] = answer["setup"], answer["source"], answer.get("model")
        self._save(st)
        rec.finish({k: answer.get(k) for k in ("source", "setup", "changes", "problems")})
        return st

    def _ask_model(self, text: str, st: dict, current: dict, violations: list, rec: _Recorder) -> dict:
        facts = self.facts(current, text)
        facts_text = json.dumps(facts, indent=1, default=str)
        history = [{"role": t["role"], "text": t.get("text", "")} for t in st["transcript"][:-1]][-HISTORY_TURNS:]
        user = ("Facts:\n" + facts_text + "\n\nConversation so far:\n" + json.dumps(history, indent=1) +
                "\n\nNew message from the user:\n" + (text or "(The user gave no description. Propose a setup "
                                                         "from the sample table and ask what they want to compare.)"))
        if violations:
            user += ("\n\nNote: the user asked for a method the code bank does not use (" +
                     ", ".join(v.matched for v in violations) + "). That part is refused automatically; keep the "
                     "code bank's methods and handle the rest of the message.")
        schema = self.schema()
        grounding_source = facts_text + "\n" + "\n".join(t.get("text", "") for t in st["transcript"] if t["role"] == "user")
        problems: list[str] = []
        for _attempt in range(2):
            try:
                out = self.llm.chat_json(PROMPT.read_text(), user, schema)
            except LLMUnavailable as e:
                rec.attempt(user, None, [], error=str(e))
                return self._without_model(st, current, str(e))
            except LLMError as e:
                rec.attempt(user, None, [], error=str(e))
                problems = [str(e)]
                continue
            setup, problems = self.check(out)
            rec.attempt(user, out, problems)
            if not problems:
                reply = out["reply"].strip()
                questions = [q for q in out.get("questions") or [] if numbers_ok(q, grounding_source)][:2]
                if not numbers_ok(reply, grounding_source) or not reply:
                    reply = describe_setup(setup) + " Approve it, or tell me what to change."
                return {"text": reply, "setup": setup, "questions": questions, "source": "model",
                        "model": self.llm.name}
            user += ("\n\nYour previous answer had these problems:\n- " + "\n- ".join(problems) +
                     "\nAnswer again with the complete setup, fixing them.")
        return {"text": "I could not make a valid setup from that request (" + " ".join(problems) +
                        ") The setup is unchanged. You can rephrase, or change it directly in the setup card.",
                "setup": current, "questions": [], "source": "model", "model": self.llm.name, "problems": problems}

    def _without_model(self, st: dict, current: dict, reason: str) -> dict:
        first = sum(t["role"] == "assistant" for t in st["transcript"]) == 0
        if first:
            text = ("The setup assistant (an open-weight model on this computer) is not available, so this setup "
                    "comes from built-in rules: " + describe_setup(current) +
                    " Change anything in the setup card, then approve it.")
        else:
            text = ("The setup assistant is not available right now, so I cannot read your message. Change the setup "
                    "directly in the setup card, or start Ollama and ask again.")
        return {"text": text, "setup": current, "questions": [], "source": "rules", "model": None,
                "fallback_reason": reason}

    # ---- what the model sees ---------------------------------------------------------------------
    def facts(self, current: dict, text: str) -> dict:
        values = {c.name: self._values(c.name) for c in self.cols}
        group_cols = self._group_columns()
        specs = manifest_params()
        defaults = codebank.defaults()
        changeable = {k: {kk: specs[k].get(kk) for kk in ("type", "values", "description") if specs[k].get(kk) is not None}
                      | {"default": defaults.get(k, specs[k].get("default"))} for k in available_advanced()}
        last_check = self.dir / "last_check.json"
        check = None
        if last_check.exists():
            chk = json.loads(last_check.read_text())
            check = {"status": chk.get("status"),
                     "problems": [c["message"] for c in chk.get("checks", []) if c.get("status") in ("fail", "warning")]}
        return {
            "files": {"counts": self.rec.get("counts_original_name"), "samples": self.rec.get("metadata_original_name")},
            "n_samples": len(self.rows),
            "sample_id_column": self.sample_col,
            "columns": [{
                "name": c.name,
                "values": c.levels if not c.numeric else {"numeric, e.g.": list(c.levels)[:6]},
                "different_for_every_sample": c.unique_per_sample,
                "missing_values": c.n_missing,
                "can_be_condition": c.name in {g.name for g in group_cols},
                "lines_up_with": [o.name for o in self.cols if o.name != c.name and len(c.levels) > 1 and len(o.levels) > 1
                                  and not c.unique_per_sample and not o.unique_per_sample
                                  and _confounded(values[c.name], values[o.name])],
            } for c in self.cols],
            "sample_table_rows": [self.header] + self.rows[:MAX_ROWS_SHOWN],
            "rows_not_shown": max(0, len(self.rows) - MAX_ROWS_SHOWN),
            "file_check": check,
            "current_setup": current,
            "settings_you_can_change": changeable,
            "code_bank_methods_fixed": [v["method"] if isinstance(v, dict) else v
                                        for v in guardrails.policy()["allowed"].values()],
        }

    def schema(self) -> dict:
        group_cols = self._group_columns()
        all_levels = sorted({lv for c in group_cols for lv in c.levels}) or [""]
        covariate_names = [c.name for c in self.cols if len(c.levels) >= 2 and not c.unique_per_sample and not c.n_missing]
        column_names = [c.name for c in self.cols]
        sample_ids = [r[0] for r in self.rows]
        specs = manifest_params()
        settings_props: dict[str, dict] = {}
        for k in available_advanced():
            spec = specs[k]
            t = spec.get("type")
            if k == "exclude_samples":
                settings_props[k] = {"type": "array", "items": {"type": "string", "enum": sample_ids}}
            elif t == "number":
                settings_props[k] = {"type": "number"}
            elif t == "integer":
                settings_props[k] = {"type": "integer"}
            elif t == "bool":
                settings_props[k] = {"type": "boolean"}
            elif t == "enum":
                settings_props[k] = {"type": "string", "enum": list(spec["values"])}
            elif t == "column":
                settings_props[k] = {"type": "string", "enum": column_names + ["none"]}
            elif t == "list":
                item = {"type": "string", "enum": list(spec["values"])} if spec.get("values") else {"type": "string"}
                settings_props[k] = {"type": "array", "items": item, "maxItems": 50}
            else:  # colour, string
                settings_props[k] = {"type": "string"}
        return {
            "type": "object",
            "properties": {
                "reply": {"type": "string"},
                "condition": {"type": "string", "enum": [c.name for c in group_cols] or [""]},
                "reference_level": {"type": "string", "enum": all_levels},
                "test_level": {"type": "string", "enum": all_levels},
                "covariates": {"type": "array", "items": {"type": "string", "enum": covariate_names or [""]}},
                "organism": {"type": "string", "enum": list(specs["organism"]["values"])},
                "run_name": {"type": "string"},
                "settings": {"type": "object", "properties": settings_props, "additionalProperties": False},
                "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            },
            "required": ["reply", "condition", "reference_level", "test_level", "covariates", "organism", "settings",
                         "questions"],
        }

    # ---- checking an answer ----------------------------------------------------------------------
    def check(self, out: dict) -> tuple[dict, list[str]]:
        """(setup, problems): the model's answer as settings, checked like any other setup."""
        defaults = codebank.defaults()
        adv = {k: v for k, v in (out.get("settings") or {}).items() if v != defaults.get(k)}
        if adv.get("exclude_samples") == []:
            adv.pop("exclude_samples")
        setup = {"condition": out.get("condition"), "reference_level": out.get("reference_level"),
                 "test_level": out.get("test_level"), "covariates": list(dict.fromkeys(out.get("covariates") or [])),
                 "organism": out.get("organism") or "auto",
                 "run_name": out.get("run_name") or "analysis", "advanced": adv}
        excluded = set(adv.get("exclude_samples") or [])
        keep = [i for i, r in enumerate(self.rows) if r[0] not in excluded]
        values = {c.name: [self._values(c.name)[i] for i in keep] for c in self.cols}
        problems = ExecutorAgent._check_setup(setup, self.by_name, values, len(keep))
        if not problems:
            n_ref = values[setup["condition"]].count(setup["reference_level"])
            n_test = values[setup["condition"]].count(setup["test_level"])
            if min(n_ref, n_test) < 2:
                problems.append("After leaving samples out, each compared group needs at least 2 samples.")
        try:
            s, p = validate(AnalysisSettings(**setup), columns=self.header)
            problems += p
            setup = s.model_dump()
        except ValueError as e:
            problems.append(str(e))
        return setup, problems

    def _group_columns(self):
        return [c for c in self.cols if len(c.levels) >= 2 and not c.unique_per_sample and not c.numeric and not c.n_missing]

    def _values(self, name: str) -> list[str]:
        j = self.header.index(name)
        return [r[j] if j < len(r) else "" for r in self.rows]


# ---- plain descriptions (shown with every answer, independent of the model's text) -----------------
LABELS = {"padj_cutoff": "adjusted p-value cutoff", "log2fc_cutoff": "minimum |log2 fold change|",
          "lfc_shrinkage": "fold-change shrinkage", "min_count": "minimum reads per gene",
          "run_enrichment": "pathway analysis", "gene_set_collections": "gene-set collections",
          "exclude_samples": "samples left out", "n_label_volcano": "genes labelled on the volcano plot",
          "volcano_label_genes": "genes to label on the volcano plot", "n_heatmap_genes": "genes in the heatmap",
          "plot_palette": "colour palette", "group_colours": "group colours", "up_colour": "colour for higher genes",
          "down_colour": "colour for lower genes", "pca_colour_by": "PCA coloured by", "pca_shape_by": "PCA shapes by",
          "pca_label_samples": "sample names on the PCA", "organism": "organism", "run_name": "analysis name"}


def _fmt(v) -> str:
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "none"
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def describe_setup(s: dict) -> str:
    out = f"Compare {s.get('test_level')} with {s.get('reference_level')} (column {s.get('condition')})"
    out += f", accounting for {', '.join(s['covariates'])}." if s.get("covariates") else ", with nothing else accounted for."
    if s.get("advanced"):
        out += " Changed settings: " + "; ".join(f"{LABELS.get(k, k)}: {_fmt(v)}" for k, v in s["advanced"].items()) + "."
    return out


def describe_changes(before: dict, after: dict) -> list[str]:
    out = []
    if (before.get("condition"), before.get("reference_level"), before.get("test_level")) != \
            (after.get("condition"), after.get("reference_level"), after.get("test_level")):
        out.append(f"Comparison: {after.get('test_level')} vs {after.get('reference_level')} ({after.get('condition')})")
    added = [c for c in after.get("covariates", []) if c not in before.get("covariates", [])]
    removed = [c for c in before.get("covariates", []) if c not in after.get("covariates", [])]
    if added:
        out.append("Accounting for " + ", ".join(added))
    if removed:
        out.append("No longer accounting for " + ", ".join(removed))
    for k in ("organism", "run_name"):
        if before.get(k) != after.get(k):
            out.append(f"{_cap(LABELS[k])}: {after.get(k)}")
    b, a = before.get("advanced") or {}, after.get("advanced") or {}
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            out.append(f"{_cap(LABELS.get(k, k))}: {_fmt(a[k]) if k in a else 'default'}")
    return out
