"""The release agent: an open-weight coding model (or any LLMClient) that updates the staging copy of the site.

One JSON action per turn (Ollama's JSON-schema output works with local models that have no native tool calls).
The history of actions and their results is sent back each turn, trimmed to fit the model's context.
Every action goes through Workspace, which confines it to the staging copy and a fixed menu of checks.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..llm.base import LLMError, LLMUnavailable
from .workspace import CHECKS, ToolError, Workspace

PROMPT = (Path(__file__).resolve().parents[1] / "agents" / "prompts" / "release_agent.md").read_text()

ACTIONS = ["list_files", "read_file", "read_codebank", "search", "replace_in_file", "write_file", "run_check", "finish"]

SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "description": "One or two sentences: why this action."},
        "action": {"type": "string", "enum": ACTIONS},
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "query": {"type": "string"},
        "old": {"type": "string"},
        "new": {"type": "string"},
        "content": {"type": "string"},
        "check": {"type": "string", "enum": list(CHECKS)},
        "summary": {"type": "string"},
        "manual_review": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["thought", "action"],
}


@dataclass
class AgentResult:
    finished: bool
    summary: str
    manual_review: list[str]
    changed: list[str]
    steps: list[dict] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    stopped_because: str | None = None


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n // 2] + f"\n... ({len(s) - n} characters left out) ...\n" + s[-n // 2:]


class ReleaseAgent:
    def __init__(self, llm, ws: Workspace, max_steps: int = 40, history_chars: int = 60_000, log: Path | None = None) -> None:
        self.llm, self.ws, self.max_steps, self.history_chars, self.log = llm, ws, max_steps, history_chars, log

    def run(self, task: str) -> AgentResult:
        steps: list[dict] = []
        checks: dict[str, bool] = {}
        for i in range(self.max_steps):
            user = self._user(task, steps, i)
            try:
                a = self.llm.chat_json(PROMPT, user, SCHEMA)
            except (LLMUnavailable, LLMError) as e:
                return self._result(False, f"The model stopped answering: {e}", [], steps, checks, "model_error")
            act = a.get("action")
            t0 = time.time()
            try:
                result = self._do(a, checks)
            except ToolError as e:
                result = f"ERROR: {e}"
            except Exception as e:  # noqa: BLE001 - the agent sees the error and can correct itself
                result = f"ERROR: {type(e).__name__}: {e}"
            step = {"n": i + 1, "action": act, "thought": a.get("thought", ""),
                    "args": {k: (v if k not in ("content", "new", "old") else f"<{len(str(v))} characters>")
                             for k, v in a.items() if k not in ("action", "thought")},
                    "result": result, "seconds": round(time.time() - t0, 1)}
            steps.append(step)
            self._log(step, a)
            if act == "finish":
                return self._result(True, a.get("summary") or "", a.get("manual_review") or [], steps, checks, None)
        return self._result(False, f"Stopped after {self.max_steps} actions without finishing.", [], steps, checks, "max_steps")

    def _do(self, a: dict, checks: dict) -> str:
        act, ws = a.get("action"), self.ws
        if act == "list_files":
            return ws.list_files(a.get("path") or ".")
        if act == "read_file":
            return ws.read_file(a.get("path") or "", a.get("start_line") or 1)
        if act == "read_codebank":
            return ws.read_codebank(a.get("path") or "")
        if act == "search":
            return ws.search(a.get("query") or "", a.get("path") or ".")
        if act == "replace_in_file":
            return ws.replace_in_file(a.get("path") or "", a.get("old") or "", a.get("new", ""))
        if act == "write_file":
            return ws.write_file(a.get("path") or "", a.get("content") or "")
        if act == "run_check":
            ok, out = ws.run_check(a.get("check") or "")
            checks[a["check"]] = ok
            return ("PASSED\n" if ok else "FAILED\n") + out
        if act == "finish":
            return "Finished."
        raise ToolError(f"Unknown action '{act}' (known: {', '.join(ACTIONS)}).")

    def _user(self, task: str, steps: list[dict], i: int) -> str:
        hist = []
        budget = self.history_chars
        # newest first while trimming, so the latest results are always complete
        for s in reversed(steps):
            entry = (f"### Step {s['n']}: {s['action']} {json.dumps(s['args'])}\nThought: {s['thought']}\nResult:\n"
                     + _clip(str(s["result"]), 12000 if s is steps[-1] else 3000))
            if len(entry) > budget:
                hist.append(f"### Steps 1-{s['n']}: (left out to save space)")
                break
            hist.append(entry)
            budget -= len(entry)
        files = ", ".join(sorted(self.ws.changed)) or "none"
        return (f"{task}\n\n## Your actions so far ({i} of at most {self.max_steps}; files changed: {files})\n\n"
                + ("\n\n".join(reversed(hist)) if hist else "(none yet)") + "\n\nYour next action (JSON):")

    def _log(self, step: dict, raw: dict) -> None:
        if self.log:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log, "a") as f:
                f.write(json.dumps({**step, "raw": raw}, default=str) + "\n")

    def _result(self, finished, summary, review, steps, checks, why) -> AgentResult:
        return AgentResult(finished=finished, summary=summary, manual_review=review, changed=sorted(self.ws.changed),
                           steps=steps, checks=checks, stopped_because=why)
