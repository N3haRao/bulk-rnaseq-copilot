import os

from .base import LLMClient, LLMUnavailable
from .ollama import OllamaClient


class NoLLM:
    """Used when EXECUTOR_LLM=none: every agent step falls back to its rule-based version."""

    name = "none"

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        raise LLMUnavailable("No language model is configured (EXECUTOR_LLM=none).")

    def status(self) -> dict:
        return {"available": False, "model": self.name, "detail": "No language model is configured."}


_override: LLMClient | None = None


def set_executor_llm(client: LLMClient | None) -> None:
    """Replace the model (tests and the evaluation script use this)."""
    global _override
    _override = client


def executor_llm() -> LLMClient:
    """The open-weight model shared by the executor and (later) the report agent."""
    if _override is not None:
        return _override
    kind = os.environ.get("EXECUTOR_LLM", "ollama").lower()
    if kind == "none":
        return NoLLM()
    return OllamaClient(model=os.environ.get("OLLAMA_MODEL", "qwen3:8b"),
                        url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
                        timeout=float(os.environ.get("OLLAMA_TIMEOUT", "180")))
