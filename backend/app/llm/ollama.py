"""Open-weight models served by Ollama on the same machine (https://ollama.com).

Uses Ollama's /api/chat with `format` set to a JSON schema, so the model's
output is constrained to valid JSON of the expected shape (Ollama 0.5+).
"""

import json

import httpx

from .base import LLMError, LLMUnavailable


class OllamaClient:
    def __init__(self, model: str, url: str = "http://localhost:11434", timeout: float = 180.0,
                 keep_alive: str = "30m") -> None:
        self.model = model
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.name = f"ollama:{model}"

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema,
            "stream": False,
            "think": False,          # Qwen3: answer directly, no hidden reasoning tokens
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "seed": 42, "num_ctx": 8192},
        }
        try:
            r = httpx.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        except httpx.ConnectError as e:
            raise LLMUnavailable(f"Ollama is not running at {self.url}. Start the Ollama app or run 'ollama serve'.") from e
        except httpx.TimeoutException as e:
            raise LLMUnavailable(f"The model took longer than {self.timeout:.0f} s to answer.") from e
        if r.status_code == 404:
            raise LLMUnavailable(f"Model '{self.model}' is not installed. Run: ollama pull {self.model}")
        if r.status_code >= 400:
            raise LLMUnavailable(f"Ollama error {r.status_code}: {r.text[:300]}")
        content = r.json().get("message", {}).get("content", "")
        try:
            out = json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"The model did not return valid JSON: {content[:200]!r}") from e
        if not isinstance(out, dict):
            raise LLMError("The model did not return a JSON object.")
        return out

    def status(self) -> dict:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
        except (httpx.HTTPError, OSError):
            return {"available": False, "model": self.name,
                    "detail": f"Ollama is not running at {self.url}. Start the Ollama app or run 'ollama serve'."}
        names = {m.get("name") for m in r.json().get("models", [])}
        wanted = {self.model, f"{self.model}:latest"}
        if not names & wanted:
            return {"available": False, "model": self.name,
                    "detail": f"Model '{self.model}' is not installed. Run: ollama pull {self.model}"}
        return {"available": True, "model": self.name, "detail": "ready"}
