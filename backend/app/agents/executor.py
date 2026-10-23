"""Executor agent (open-weight model): the judgment steps of the analysis workflow.

The workflow itself is fixed code (uploads.py, jobs.py). The model is asked
for three things only:

  propose_setup         which column to compare, control group, test group, covariates
  explain_checks        plain-language explanation and fix for each input-check problem
  explain_run_warnings  what each warning of a finished run means for the user

Every answer is constrained by a JSON schema built from the actual data (the
model can only name columns and groups that exist), then checked by code. A
proposal that fails the checks is retried once with the problems listed; after
that, or when no model is available, the rule-based result is used instead.
The user always gets an answer, and can always see which one it was.

Every call is recorded (prompt, raw answer, checks, outcome) next to the data
it was about, so any suggestion shown on the site can be traced.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ..llm import LLMClient, LLMError, LLMUnavailable, executor_llm
from .grounding import numbers_ok
from ..setup_proposal import _confounded, _design_rank_ok, describe_columns, propose

PROMPTS = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


class _Recorder:
    def __init__(self, log_dir: Path | None, task: str, model: str) -> None:
        self.log_dir, self.task = log_dir, task
        self.rec: dict = {"task": task, "model": model, "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "attempts": []}
        self.t0 = time.monotonic()

    def attempt(self, user: str, raw: dict | None, problems: list[str], error: str | None = None) -> None:
        self.rec["attempts"].append({"user_message": user, "answer": raw, "problems": problems, "error": error})

    def finish(self, result: dict) -> dict:
        self.rec.update(seconds=round(time.monotonic() - self.t0, 2), result=result)
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            (self.log_dir / f"{stamp}_{self.task}.json").write_text(json.dumps(self.rec, indent=2, default=str))
        return result


class ExecutorAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or executor_llm()

    # ------------------------------------------------------------------------------
    # 1. Analysis setup
    # ------------------------------------------------------------------------------
    def propose_setup(self, header: list[str], rows: list[list[str]], *, files: dict | None = None,
                      condition: str | None = None, log_dir: Path | None = None) -> dict:
        sample_col, cols = describe_columns(header, rows)
        _, rules = propose(header, rows, condition=condition)
        rule_result = {
            "source": "rules", "model": None,
            "condition": rules.condition, "reference_level": rules.reference_level, "test_level": rules.test_level,
            "covariates": rules.covariates, "explanation": " ".join(rules.reasons), "reasons": rules.reasons,
            "questions": ["Please check that the control group is right."] if rules.reference_guessed else [],
            "warnings": rules.warnings,
        }
        values = {c.name: _values(header, rows, c.name) for c in cols}
        group_cols = [c for c in cols if len(c.levels) >= 2 and not c.unique_per_sample and not c.numeric and not c.n_missing]
        if condition:
            group_cols = [c for c in group_cols if c.name == condition]
        rec = _Recorder(log_dir, "propose_setup", self.llm.name)
        if not group_cols:
            return rec.finish({**rule_result, "fallback_reason": "No column with groups to compare."})

        facts = {
            "n_samples": len(rows),
            "sample_id_column": sample_col,
            "files": files or {},
            "columns": [{
                "name": c.name,
                "values": c.levels if not c.numeric else {"numeric, e.g.": list(c.levels)[:6]},
                "numeric": c.numeric,
                "different_for_every_sample": c.unique_per_sample,
                "missing_values": c.n_missing,
                "lines_up_with": [o.name for o in cols if o.name != c.name and len(c.levels) > 1 and len(o.levels) > 1
                                  and not c.unique_per_sample and not o.unique_per_sample
                                  and _confounded(values[c.name], values[o.name])],
            } for c in cols],
            "rule_based_guess_may_be_wrong": {k: rule_result[k] for k in ("condition", "reference_level", "test_level", "covariates")},
        }
        if condition:
            facts["user_chose_condition"] = condition
        all_levels = sorted({lv for c in group_cols for lv in c.levels})
        covariate_names = [c.name for c in cols if len(c.levels) >= 2 and not c.unique_per_sample and not c.n_missing]
        schema = {
            "type": "object",
            "properties": {
                "condition": {"type": "string", "enum": [c.name for c in group_cols]},
                "reference_level": {"type": "string", "enum": all_levels},
                "test_level": {"type": "string", "enum": all_levels},
                "covariates": {"type": "array", "items": {"type": "string", "enum": covariate_names or [""]}},
                "explanation": {"type": "string"},
                "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
                "warnings": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            },
            "required": ["condition", "reference_level", "test_level", "covariates", "explanation", "questions", "warnings"],
        }
        facts_text = json.dumps(facts, indent=1)
        user = "Facts about the uploaded sample table:\n" + facts_text
        by_name = {c.name: c for c in cols}

        for attempt in range(2):
            try:
                out = self.llm.chat_json(_prompt("propose_setup"), user, schema)
            except LLMUnavailable as e:
                rec.attempt(user, None, [], error=str(e))
                return rec.finish({**rule_result, "fallback_reason": str(e)})
            except LLMError as e:
                rec.attempt(user, None, [], error=str(e))
                continue
            problems = self._check_setup(out, by_name, values, len(rows))
            rec.attempt(user, out, problems)
            if not problems:
                text_ok = all(numbers_ok(t, facts_text) for t in [out["explanation"], *out["questions"], *out["warnings"]])
                covs = list(dict.fromkeys(out["covariates"]))
                return rec.finish({
                    "source": "model", "model": self.llm.name,
                    "condition": out["condition"], "reference_level": out["reference_level"],
                    "test_level": out["test_level"], "covariates": covs,
                    # text with numbers the model was not given is not shown; the rule-based reasons are
                    "explanation": out["explanation"] if text_ok else rule_result["explanation"],
                    "reasons": rules.reasons,
                    "questions": out["questions"] if text_ok else [],
                    "warnings": list(dict.fromkeys((out["warnings"] if text_ok else []) + rules.warnings)),
                    "differs_from_rules": (out["condition"], out["reference_level"], out["test_level"], sorted(covs))
                    != (rules.condition, rules.reference_level, rules.test_level, sorted(rules.covariates)),
                })
            user = ("Facts about the uploaded sample table:\n" + facts_text +
                    "\n\nYour previous answer had these problems:\n- " + "\n- ".join(problems) +
                    "\nAnswer again, fixing them.")
        return rec.finish({**rule_result, "fallback_reason": "The model's suggestion did not pass the checks twice."})

    @staticmethod
    def _check_setup(out: dict, by_name: dict, values: dict, n: int) -> list[str]:
        problems = []
        cond = by_name.get(out.get("condition"))
        if cond is None:
            return [f"'{out.get('condition')}' is not a column with groups."]
        ref, test = out.get("reference_level"), out.get("test_level")
        for label, lv in (("reference_level", ref), ("test_level", test)):
            if lv not in cond.levels:
                problems.append(f"{label} '{lv}' is not a value of column '{cond.name}' (values: {', '.join(cond.levels)}).")
        if ref == test:
            problems.append("reference_level and test_level must be different groups.")
        covs = list(dict.fromkeys(out.get("covariates") or []))
        for c in covs:
            col = by_name.get(c)
            if col is None or c == cond.name:
                problems.append(f"Covariate '{c}' is not allowed (it is missing or is the condition column).")
            elif col.unique_per_sample:
                problems.append(f"Covariate '{c}' has a different value for every sample.")
            elif _confounded(values[c], values[cond.name]):
                problems.append(f"Covariate '{c}' lines up exactly with '{cond.name}'; it cannot be adjusted for.")
        if not problems and covs and not _design_rank_ok(values[cond.name], [values[c] for c in covs]):
            problems.append(f"With covariates {', '.join(covs)} there are too few samples to estimate the comparison; "
                            "use fewer covariates.")
        return problems

    # ------------------------------------------------------------------------------
    # 2. Input-check problems
    # ------------------------------------------------------------------------------
    def explain_checks(self, checks: list[dict], *, context: dict | None = None,
                       log_dir: Path | None = None) -> dict:
        problems = [{"id": c["id"], "status": c["status"], "message": c["message"]}
                    for c in checks if c.get("status") in ("fail", "warning")]
        rec = _Recorder(log_dir, "explain_checks", self.llm.name)
        if not problems:
            return rec.finish({"source": "rules", "model": None, "items": []})
        facts = {"problems": problems, "context": context or {}}
        facts_text = json.dumps(facts, indent=1)
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string", "enum": [p["id"] for p in problems]},
                               "explanation": {"type": "string"}, "how_to_fix": {"type": "string"}},
                "required": ["id", "explanation", "how_to_fix"]}}},
            "required": ["items"],
        }
        user = "Facts:\n" + facts_text
        try:
            out = self.llm.chat_json(_prompt("explain_checks"), user, schema)
        except (LLMUnavailable, LLMError) as e:
            rec.attempt(user, None, [], error=str(e))
            return rec.finish({"source": "rules", "model": None, "items": [], "fallback_reason": str(e)})
        items, dropped = [], []
        for it in out.get("items", []):
            if it.get("id") in {p["id"] for p in problems} and numbers_ok(it["explanation"] + " " + it["how_to_fix"], facts_text):
                items.append({"id": it["id"], "explanation": it["explanation"].strip(), "how_to_fix": it["how_to_fix"].strip()})
            else:
                dropped.append(it.get("id"))
        rec.attempt(user, out, [f"dropped (unknown id or unsupported numbers): {d}" for d in dropped])
        return rec.finish({"source": "model", "model": self.llm.name, "items": list({i["id"]: i for i in items}.values())})

    # ------------------------------------------------------------------------------
    # 3. Warnings of a finished run
    # ------------------------------------------------------------------------------
    def explain_run_warnings(self, summary: dict, *, log_dir: Path | None = None) -> dict:
        warnings = [re.sub(r"^\[[^\]]+\]\s*", "", w) for w in summary.get("warnings") or []]
        rec = _Recorder(log_dir, "explain_run_warnings", self.llm.name)
        fallback = {"source": "rules", "model": None, "headline": None,
                    "items": [{"index": i, "title": None, "explanation": w, "what_to_do": None, "severity": "caution"}
                              for i, w in enumerate(warnings)]}
        if not warnings:
            return rec.finish({**fallback, "headline": "No warnings were raised."})
        facts = {"comparison": summary.get("comparison"), "group_sizes": summary.get("group_sizes"),
                 "n_samples": summary.get("n_samples"),
                 "warnings": [{"index": i, "text": w} for i, w in enumerate(warnings)]}
        facts_text = json.dumps(facts, indent=1)
        schema = {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"index": {"type": "integer", "enum": list(range(len(warnings)))},
                                   "title": {"type": "string"}, "explanation": {"type": "string"},
                                   "what_to_do": {"type": "string"},
                                   "severity": {"type": "string", "enum": ["info", "caution", "serious"]}},
                    "required": ["index", "title", "explanation", "what_to_do", "severity"]}},
            },
            "required": ["headline", "items"],
        }
        user = "Facts:\n" + facts_text
        try:
            out = self.llm.chat_json(_prompt("explain_run_warnings"), user, schema)
        except (LLMUnavailable, LLMError) as e:
            rec.attempt(user, None, [], error=str(e))
            return rec.finish({**fallback, "fallback_reason": str(e)})
        by_index = {}
        for it in out.get("items", []):
            text = " ".join(str(it.get(k, "")) for k in ("title", "explanation", "what_to_do"))
            if isinstance(it.get("index"), int) and 0 <= it["index"] < len(warnings) and numbers_ok(text, facts_text):
                by_index[it["index"]] = {**it, "original": warnings[it["index"]]}
        # any warning the model skipped or got wrong is still shown, in its original words
        items = [by_index.get(i) or {**fallback["items"][i], "original": warnings[i]} for i in range(len(warnings))]
        headline = out.get("headline") if numbers_ok(out.get("headline", ""), facts_text) else None
        rec.attempt(user, out, [])
        return rec.finish({"source": "model", "model": self.llm.name, "headline": headline, "items": items})


def _values(header: list[str], rows: list[list[str]], name: str) -> list[str]:
    j = header.index(name)
    return [r[j] if j < len(r) else "" for r in rows]
