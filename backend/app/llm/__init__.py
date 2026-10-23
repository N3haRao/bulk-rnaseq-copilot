"""Language-model clients used by the agents.

Agents only call `chat_json(system, user, schema)`: one request, one JSON object
back, constrained by a JSON schema. No tools, no shell, no free text that is
executed. Which model runs is configuration (EXECUTOR_LLM, OLLAMA_MODEL).
"""

from .base import LLMClient, LLMError, LLMUnavailable
from .factory import executor_llm

__all__ = ["LLMClient", "LLMError", "LLMUnavailable", "executor_llm"]
