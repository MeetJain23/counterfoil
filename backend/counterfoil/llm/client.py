"""The model boundary.

Everything above this line deals in ``Ask`` and ``Answer``. Nothing above it
imports the Anthropic SDK, so the provider is a swap rather than a rewrite, and
the tests never need a network.

The important property is narrowness. A model reached through this interface
can return structured data and nothing else: it cannot call a tool, cannot
reach the executor, and cannot influence the policy engine except by supplying
a classification that the policy engine is then free to refuse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from .budget import price


@dataclass(frozen=True)
class Ask:
    #: Stable across calls so it can be cached at the provider. Put nothing
    #: volatile in here: one changed byte invalidates the cached prefix.
    system: str
    user: str
    schema: dict[str, Any]
    max_tokens: int = 640


@dataclass(frozen=True)
class Answer:
    data: dict[str, Any]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    raw_text: str = ""


class LLMError(RuntimeError):
    """Anything that stopped us getting a usable structured answer."""


class LLMClient(Protocol):
    def ask(self, request: Ask) -> Answer: ...


class AnthropicClient:
    """Claude via the official SDK, constrained to a JSON schema."""

    def __init__(self, model: str = "claude-haiku-4-5", api_key: str | None = None):
        import anthropic  # imported here so the SDK is optional at import time

        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._errors = anthropic

    def ask(self, request: Ask) -> Answer:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": request.system,
                        # The taxonomy and rules of engagement are identical on
                        # every call, so they are worth caching: reads bill at
                        # roughly a tenth of the input rate.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": request.user}],
                output_config={"format": {"type": "json_schema", "schema": request.schema}},
            )
        except self._errors.APIStatusError as exc:
            raise LLMError(f"{exc.status_code} from the API: {exc}") from exc
        except self._errors.APIConnectionError as exc:
            raise LLMError(f"could not reach the API: {exc}") from exc

        try:
            text = next(b.text for b in response.content if b.type == "text")
        except StopIteration:
            raise LLMError("response contained no text block") from None

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"response was not valid JSON: {exc}") from exc

        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

        return Answer(
            data=data,
            model=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=price(usage.input_tokens, usage.output_tokens, cache_read, cache_write),
            raw_text=text,
        )


@dataclass
class ScriptedClient:
    """A deterministic stand-in for tests and for offline development.

    Not a mock of the SDK: it implements the same narrow interface the rest of
    the system uses, so a test exercising the diagnoser exercises the real
    parsing, validation and refusal paths rather than a shortcut around them.
    """

    answers: list[dict[str, Any]] = field(default_factory=list)
    #: Raised instead of answering, to exercise the degraded path.
    fail_with: Exception | None = None
    calls: int = 0
    seen: list[Ask] = field(default_factory=list)

    def ask(self, request: Ask) -> Answer:
        self.calls += 1
        self.seen.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        if not self.answers:
            raise LLMError("ScriptedClient ran out of scripted answers")
        data = self.answers[(self.calls - 1) % len(self.answers)]
        return Answer(
            data=data,
            model="scripted",
            input_tokens=900,
            output_tokens=90,
            cost_usd=price(900, 90),
            raw_text=json.dumps(data),
        )
