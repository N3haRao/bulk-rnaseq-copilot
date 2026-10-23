"""The chat assistant's model: Claude through the Anthropic API, streamed.

`stream_turn` yields ("text", piece) while Claude writes, then one
("final", message) with the whole assistant message as plain dicts, ready to be
stored and sent back unchanged on the next request (thinking blocks included).
A scripted stand-in with the same interface is used in tests.
"""

import os
from collections.abc import Iterator
from typing import Protocol

DEFAULT_MODEL = "claude-opus-5-5"


class ChatModelError(RuntimeError):
    """The API could not be reached or refused the request (a message for the user)."""


class ChatModel(Protocol):
    name: str

    def stream_turn(self, system: list[dict], messages: list[dict], tools: list[dict]) -> Iterator[tuple[str, object]]:
        ...


_override: "ChatModel | None" = None


def set_chat_model(model: "ChatModel | None") -> None:
    """Replace the model (tests use a scripted one)."""
    global _override
    _override = model


def chat_model() -> "ChatModel":
    return _override if _override is not None else ClaudeChat()


def configured() -> dict:
    """Whether a key is set, without calling the API."""
    if _override is not None:
        return {"configured": True, "model": _override.name, "detail": "ready"}
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return {"configured": has_key, "model": os.environ.get("CHAT_MODEL", DEFAULT_MODEL),
            "detail": "ready" if has_key else
            "No Anthropic API key found. Put ANTHROPIC_API_KEY=... in the .env file next to start.sh and restart."}


class ClaudeChat:
    def __init__(self, model: str | None = None, effort: str | None = None) -> None:
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=3)
        self.name = model or os.environ.get("CHAT_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.environ.get("CHAT_EFFORT", "high")

    def stream_turn(self, system, messages, tools):
        a = self.anthropic
        params = dict(
            model=self.name,
            max_tokens=64000,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        # On a policy decline, let the API retry on Anthropic's recommended fallback model
        if self.name in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"):
            params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            with self.client.beta.messages.stream(**params) as stream:
                for event in stream:
                    if event.type == "text":
                        yield "text", event.text
                    elif event.type == "content_block_start" and event.content_block.type == "thinking":
                        yield "thinking", ""
                msg = stream.get_final_message()
        except ValueError as e:
            # tool input JSON the SDK could not parse at all: the caller re-issues the turn
            raise ChatModelError(f"retry: unreadable tool input ({e})") from e
        except a.AuthenticationError as e:
            raise ChatModelError("The Anthropic API key was not accepted. Check ANTHROPIC_API_KEY in .env.") from e
        except a.PermissionDeniedError as e:
            raise ChatModelError(f"The API key is not allowed to use {self.name}.") from e
        except a.NotFoundError as e:
            raise ChatModelError(f"Model '{self.name}' was not found. Check CHAT_MODEL.") from e
        except a.RateLimitError as e:
            raise ChatModelError("The Anthropic API is rate-limiting requests. Wait a minute and try again.") from e
        except a.APIStatusError as e:
            raise ChatModelError(f"The Anthropic API returned an error ({e.status_code}): {e.message}") from e
        except a.APIConnectionError as e:
            raise ChatModelError("Could not reach the Anthropic API. Check the internet connection.") from e
        yield "final", {"content": [b.to_dict() for b in msg.content], "stop_reason": msg.stop_reason,
                        "usage": msg.usage.to_dict() if msg.usage else {}}
