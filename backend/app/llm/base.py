from typing import Protocol


class LLMError(RuntimeError):
    """The model answered, but not with usable JSON."""


class LLMUnavailable(RuntimeError):
    """No model is configured, or it cannot be reached."""


class LLMClient(Protocol):
    name: str  # shown to the user and recorded with every answer, e.g. "ollama:qwen3:8b"

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        """Return one JSON object that follows `schema`. Raises LLMError or LLMUnavailable."""
        ...

    def status(self) -> dict:
        """{"available": bool, "model": str, "detail": str} without running the model."""
        ...
