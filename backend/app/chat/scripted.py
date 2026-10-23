"""A stand-in for Claude with the same interface (tests and offline demos).

Each scripted turn is a list of content blocks the "model" returns; a turn with
tool_use blocks gets stop_reason "tool_use", otherwise "end_turn". Text blocks
are streamed in small pieces like the real model.
"""

import itertools


class ScriptedChat:
    def __init__(self, turns: list[list[dict]], name: str = "scripted") -> None:
        self.turns = list(turns)
        self.name = name
        self.requests: list[dict] = []
        self._ids = itertools.count(1)

    def stream_turn(self, system, messages, tools):
        import copy
        self.requests.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools})
        if not self.turns:
            blocks = [{"type": "text", "text": "(no more scripted answers)"}]
        else:
            blocks = self.turns.pop(0)
        content = []
        for b in blocks:
            if b["type"] == "text":
                for i in range(0, len(b["text"]), 12):
                    yield "text", b["text"][i:i + 12]
                content.append({"type": "text", "text": b["text"]})
            elif b["type"] == "tool_use":
                content.append({"type": "tool_use", "id": f"toolu_{next(self._ids):04d}", "name": b["name"],
                                "input": b["input"]})
            else:
                content.append(b)
        stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
        yield "final", {"content": content, "stop_reason": b.get("stop_reason", stop) if blocks else stop, "usage": {}}
