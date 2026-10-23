"""Score the executor agent's setup proposals against eval/setup_cases.yaml.

Usage (from backend/):
    python -m eval.run_setup_eval                    # model from EXECUTOR_LLM / OLLAMA_MODEL (default qwen3:8b)
    python -m eval.run_setup_eval --rules            # the rule-based fallback alone, for comparison
    python -m eval.run_setup_eval --model qwen3:14b  # another Ollama model

Writes eval/results/<date>_<model>.json (every answer) and prints a table.
A case counts as correct only when condition, both groups and the covariate
set are all acceptable.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from app.agents.executor import ExecutorAgent
from app.llm.factory import NoLLM, executor_llm
from app.llm.ollama import OllamaClient

HERE = Path(__file__).parent


def as_options(v) -> list:
    return v if isinstance(v, list) else [v]


def score(expect: dict, got: dict) -> dict:
    cov_options = [sorted(c) for c in expect.get("covariates", [[]])]
    return {
        "condition": got["condition"] == expect["condition"],
        "reference_level": got["reference_level"] in as_options(expect["reference_level"]),
        "test_level": got["test_level"] in as_options(expect["test_level"]),
        "covariates": sorted(got["covariates"] or []) in cov_options,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", action="store_true", help="score the rule-based fallback only")
    ap.add_argument("--model", help="Ollama model name (default: OLLAMA_MODEL or qwen3:8b)")
    ap.add_argument("--cases", default=str(HERE / "setup_cases.yaml"))
    args = ap.parse_args()

    llm = NoLLM() if args.rules else (OllamaClient(args.model) if args.model else executor_llm())
    if not args.rules:
        st = llm.status()
        if not st["available"]:
            print(f"Model not available: {st['detail']}", file=sys.stderr)
            return 2
    agent = ExecutorAgent(llm)
    cases = yaml.safe_load(Path(args.cases).read_text())["cases"]
    rows, details = [], []
    for case in cases:
        lines = [ln.split(",") for ln in case["table"].strip().splitlines()]
        t0 = time.monotonic()
        got = agent.propose_setup(lines[0], lines[1:], condition=case.get("user_condition"))
        secs = time.monotonic() - t0
        s = score(case["expect"], got)
        ok = all(s.values())
        rows.append((case["name"], ok, s, got["source"], secs))
        details.append({"case": case["name"], "correct": ok, "fields": s, "seconds": round(secs, 1),
                        "expected": case["expect"], "answer": got})
        mark = "ok  " if ok else "MISS"
        wrong = ", ".join(k for k, v in s.items() if not v)
        print(f"{mark} {case['name'][:58]:58} {got['source']:6} {secs:5.1f}s {wrong}")

    n = len(rows)
    correct = sum(r[1] for r in rows)
    by_field = {f: sum(r[2][f] for r in rows) for f in ("condition", "reference_level", "test_level", "covariates")}
    fallbacks = sum(r[3] == "rules" for r in rows) if not args.rules else 0
    print(f"\n{llm.name}: {correct}/{n} cases fully correct ({100 * correct / n:.0f}%)")
    print("  per field: " + ", ".join(f"{k} {v}/{n}" for k, v in by_field.items()))
    if not args.rules:
        print(f"  fell back to rules: {fallbacks}/{n}; mean time {sum(r[4] for r in rows) / n:.1f} s per case")

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M}_{llm.name.replace(':', '-').replace('/', '-')}.json"
    out.write_text(json.dumps({"model": llm.name, "correct": correct, "n": n, "by_field": by_field,
                               "fallbacks": fallbacks, "cases": details}, indent=2, default=str))
    print(f"  details: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
