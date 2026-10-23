"""A stand-in model that returns prepared answers (for tests and offline demos)."""

from collections.abc import Callable

from .base import LLMUnavailable


class ScriptedLLM:
    def __init__(self, answers: list[dict | Exception] | Callable[[str, str, dict], dict], name: str = "scripted") -> None:
        self.answers = answers
        self.name = name
        self.calls: list[dict] = []

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append({"system": system, "user": user, "schema": schema})
        if callable(self.answers):
            return self.answers(system, user, schema)
        if not self.answers:
            raise LLMUnavailable("The scripted model has no answers left.")
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def status(self) -> dict:
        return {"available": True, "model": self.name, "detail": "scripted"}
