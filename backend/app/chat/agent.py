"""Interpreter agent (Claude API): a chat about one analysis run.

A conversation lives in data/jobs/<job>/chat/<conversation>/:
    messages.json   the API conversation, stored exactly as sent and received (append-only)
    transcript.json what the user sees: their messages, answers, code that ran, files, proposals
    workspace/      sandbox (see sandbox.py): a copy of the run, the outputs, every script run

`send()` takes the user's message and yields events for the chat window while
Claude answers, calling tools as often as it needs (up to MAX_TOOL_ROUNDS).
"""

import json
import secrets
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .. import codebank, guardrails
from ..agents.reporter import build_facts
from .model import ChatModel, ChatModelError
from .sandbox import Workspace
from .tools import TOOLS, ToolBox

MAX_TOOL_ROUNDS = 30
PROMPT = Path(__file__).parent / "system_prompt.md"
_locks: dict[str, threading.Lock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Conversation:
    def __init__(self, job_dir: Path, run_dir: Path, job: dict, conv_id: str) -> None:
        if not conv_id or any(ch not in "0123456789abcdef-" for ch in conv_id):
            raise KeyError(conv_id)
        self.dir = job_dir / "chat" / conv_id
        self.id, self.job, self.run_dir = conv_id, job, run_dir
        if not (self.dir / "messages.json").exists():
            raise KeyError(conv_id)

    # ---- storage -----------------------------------------------------------------------------
    @classmethod
    def create(cls, job_dir: Path, run_dir: Path, job: dict) -> "Conversation":
        conv_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        d = job_dir / "chat" / conv_id
        d.mkdir(parents=True)
        (d / "messages.json").write_text("[]")
        (d / "transcript.json").write_text("[]")
        (d / "meta.json").write_text(json.dumps({"id": conv_id, "created": _now(), "title": None}))
        Workspace(d / "workspace", run_dir)  # copy the run now, so the first answer is not slowed down
        return cls(job_dir, run_dir, job, conv_id)

    @staticmethod
    def all_for(job_dir: Path) -> list[dict]:
        base = job_dir / "chat"
        if not base.exists():
            return []
        return [json.loads((d / "meta.json").read_text()) for d in sorted(base.iterdir(), reverse=True)
                if (d / "meta.json").exists()]

    def _load(self, name: str) -> list:
        return json.loads((self.dir / name).read_text())

    def _save(self, name: str, data) -> None:
        tmp = self.dir / f"{name}.tmp"
        tmp.write_text(json.dumps(data, indent=1, default=str))
        tmp.replace(self.dir / name)

    def transcript(self) -> list[dict]:
        return self._load("transcript.json")

    @property
    def workspace(self) -> Workspace:
        return Workspace(self.dir / "workspace", self.run_dir)

    # ---- the system prompt ---------------------------------------------------------------------------
    def system(self) -> list[dict]:
        facts, _ = build_facts(self.run_dir)
        manifest = codebank.manifest()
        files = {name: [o for o in (step.get("outputs") or {})] for name, step in
                 ((s["name"], s) for s in manifest["steps"])}
        guide = {
            "run_folder_layout": {f"run/{k}/": v for k, v in files.items()},
            "where_answers_live": {q: [f"run/{x}" for x in v] for q, v in (manifest.get("answers") or {}).items()},
        }
        s = self.job["settings"]
        context = {
            "dataset": self.job.get("dataset"),
            "run_id": self.run_dir.name,
            "settings_chosen_by_user": {k: s.get(k) for k in ("condition", "reference_level", "test_level",
                                                             "covariates", "organism")} | {"advanced": s.get("advanced")},
            "setup_suggested_by": self.job.get("setup_origin"),
            "code_bank": self.job.get("codebank"),
        }
        text = (PROMPT.read_text()
                + "\n\n# This analysis\n\n" + json.dumps(context, indent=1, default=str)
                + "\n\n# Main results (from the run's files)\n\n" + json.dumps(facts, indent=1, default=str)
                + "\n\n# Files\n\n" + json.dumps(guide, indent=1)
                + "\n\n# Methods policy (from the " + guardrails.policy_source() + ")\n\n" + guardrails.describe_for_prompt())
        # stable for the whole conversation, so it is cached after the first request
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    # ---- one user message ----------------------------------------------------------------------------
    def send(self, text: str, model: ChatModel) -> Iterator[dict]:
        lock = _locks.setdefault(str(self.dir), threading.Lock())
        if not lock.acquire(blocking=False):
            yield {"type": "error", "text": "This conversation is still answering. Wait for it to finish."}
            return
        try:
            yield from self._send(text, model)
        finally:
            lock.release()

    def _send(self, text: str, model: ChatModel) -> Iterator[dict]:
        messages = self._load("messages.json")
        transcript = self._load("transcript.json")
        start_len = len(messages)
        messages.append({"role": "user", "content": text})
        transcript.append({"role": "user", "text": text, "at": _now()})
        self._save("transcript.json", transcript)
        if start_len == 0:
            meta = json.loads((self.dir / "meta.json").read_text())
            meta["title"] = text[:80]
            (self.dir / "meta.json").write_text(json.dumps(meta))
        tools = ToolBox(self.workspace, self.run_dir, self.job)
        system = self.system()
        json_retries = 0

        for _round in range(MAX_TOOL_ROUNDS):
            answer = {"role": "assistant", "text": "", "at": _now()}
            final = None
            try:
                for kind, value in model.stream_turn(system, messages, TOOLS):
                    if kind == "text":
                        answer["text"] += value
                        yield {"type": "text", "text": value}
                    elif kind == "thinking":
                        yield {"type": "thinking"}
                    elif kind == "final":
                        final = value
            except ChatModelError as e:
                if str(e).startswith("retry:") and json_retries < 2:
                    json_retries += 1
                    continue
                yield from self._fail(transcript, messages, start_len, str(e))
                return
            json_retries = 0
            stop = final["stop_reason"]
            if stop == "refusal":
                yield from self._fail(transcript, messages, start_len,
                                      "Claude declined to answer this request. Try rephrasing it.")
                return
            messages.append({"role": "assistant", "content": final["content"]})
            if answer["text"].strip():
                transcript.append(answer)
            tool_uses = [b for b in final["content"] if b.get("type") == "tool_use"]
            if stop == "max_tokens" and tool_uses:
                messages.pop()
                yield from self._fail(transcript, messages, start_len,
                                      "The answer was too long and was cut off. Ask for a smaller step.")
                return
            if not tool_uses:
                self._save("messages.json", messages)
                self._save("transcript.json", transcript)
                yield {"type": "done"}
                return

            results = []
            for b in tool_uses:
                yield {"type": "tool_start", "id": b["id"], "name": b["name"],
                       "title": (b.get("input") or {}).get("description") or b["name"].replace("_", " "),
                       "code": (b.get("input") or {}).get("code"), "language": _lang(b["name"])}
                content, event = self._call(tools, b)
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": content,
                                **({"is_error": True} if event.get("error") else {})})
                item = {"role": "tool", "name": b["name"], **event, "at": _now()}
                transcript.append(item)
                yield {"type": "tool_end", "id": b["id"], **item}
            # all results of one turn go back in one user message
            messages.append({"role": "user", "content": results})
            self._save("messages.json", messages)
            self._save("transcript.json", transcript)

        yield from self._fail(transcript, messages, start_len,
                              f"Stopped after {MAX_TOOL_ROUNDS} steps. Ask again to continue.", keep=True)

    def _call(self, tools: ToolBox, block: dict) -> tuple[list[dict] | str, dict]:
        args = block.get("input")
        if not isinstance(args, dict):
            return "Invalid tool input: expected a JSON object.", {"title": block["name"], "error": True, "ok": False}
        try:
            return tools.call(block["name"], args)
        except TypeError as e:  # missing or unexpected arguments
            return f"Invalid arguments for {block['name']}: {e}", {"title": block["name"], "error": True, "ok": False,
                                                                   "output": str(e)}
        except (ValueError, KeyError, OSError) as e:
            return f"Error: {e}", {"title": block["name"].replace("_", " "), "error": True, "ok": False, "output": str(e)}

    def _fail(self, transcript, messages, start_len, text, keep: bool = False) -> Iterator[dict]:
        """Report a problem; unless `keep`, take this exchange back out of the API history."""
        if not keep:
            del messages[start_len:]
        transcript.append({"role": "error", "text": text, "at": _now()})
        self._save("messages.json", messages)
        self._save("transcript.json", transcript)
        yield {"type": "error", "text": text}


def _lang(tool: str) -> str | None:
    return {"run_python": "python", "run_r": "r"}.get(tool)
