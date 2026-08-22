"""Gemini via the REST API, for the free tier.

Deliberately stdlib only. Adding an SDK to make one HTTP POST would pull a
dependency tree into a project whose whole model surface is a single
request-response, and the raw payload is easier to debug when a provider
changes its schema dialect under you.

Gemini's structured-output schema is an OpenAPI subset rather than plain JSON
Schema, so ``to_gemini_schema`` translates ours across: types are uppercased,
``additionalProperties`` is dropped because it is unsupported, and an explicit
property ordering is supplied since the field order in a Python dict is not
something the API is willing to infer.

Note on the free tier: Google may use free-tier prompts to improve their
products. Everything Counterfoil sends is a synthetic provider error string
with no customer data in it, so there is nothing here to be careful about, but
it is worth knowing before pointing this at anything real.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import Answer, Ask, LLMError

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"

#: Google retires models by name and says so in the 404 body. Turning that
#: sentence into an actionable suggestion is cheaper than reading release notes.
_REPLACEMENT = re.compile(r"use\s+models/([A-Za-z0-9.\-]+)")


def suggested_replacement(message: str) -> str | None:
    """The model Google recommends instead, pulled out of an error body."""
    match = _REPLACEMENT.search(message)
    return match.group(1) if match else None


#: Worth trying again: rate limits and transient server faults.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

BACKOFF_BASE = 8.0

#: How much wider the gap gets after each rate limit, and the ceiling it
#: will not pass. Converging is the point; crawling is not.
PACING_GROWTH = 1.6
MAX_INTERVAL_SECONDS = 45.0

#: A 429 body carries a RetryInfo detail saying how long to wait. Obeying it
#: beats guessing, and guessing short is how you get rate limited for longer.
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def retry_after_seconds(body: str) -> float | None:
    match = _RETRY_DELAY.search(body)
    return float(match.group(1)) if match else None


def _sleep(seconds: float) -> None:
    """Indirection so a test can patch ``time.sleep`` and be obeyed.

    A dataclass default binds at class-definition time, so ``sleep: Callable =
    time.sleep`` would capture the real function and quietly ignore any later
    patch. Twice that turned a new retry path into a suite that slept through
    two minutes of genuine backoff.
    """
    time.sleep(seconds)


#: The free tier bills nothing, so cost is reported as zero rather than
#: estimated. The budget meter still counts calls, which is what the rate limit
#: actually cares about.
FREE_TIER_COST_USD = 0.0

_TYPE_MAP = {
    "object": "OBJECT",
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
}

#: Keys Gemini rejects outright rather than ignoring.
_UNSUPPORTED = {"additionalProperties", "$schema", "definitions", "$defs"}


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate a JSON Schema into Gemini's OpenAPI subset."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED:
            continue
        if key == "type" and isinstance(value, str):
            out["type"] = _TYPE_MAP.get(value.lower(), value.upper())
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: to_gemini_schema(v) for k, v in value.items()}
            out.setdefault("propertyOrdering", list(value))
        elif key == "items" and isinstance(value, dict):
            out["items"] = to_gemini_schema(value)
        elif key in {"minimum", "maximum"}:
            # Accepted on numbers but silently ignored; kept for documentation
            # value rather than enforcement. Our own validation is the real gate.
            out[key] = value
        else:
            out[key] = value
    return out


def _post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass
class GeminiClient:
    """Same narrow interface as the Anthropic client: ask, get structured data.

    ``transport`` is injectable so the parsing, error handling and schema
    translation are all testable without a network or a key.
    """

    api_key: str
    model: str = "gemini-3.6-flash"
    timeout: float = 30.0
    transport: Callable[[str, dict, float], dict] | None = None
    #: Minimum gap between requests. The free tier is rate limited per minute,
    #: so a batch that fires as fast as it can will spend most of its calls
    #: collecting 429s. Pacing is cheaper than retrying.
    min_interval_seconds: float = 0.0
    max_retries: int = 4
    #: Injected so tests do not actually wait.
    sleep: Callable[[float], None] = _sleep
    clock: Callable[[], float] = time.monotonic
    #: Called before each retry, for progress output.
    on_retry: Callable[[int, float, str], None] | None = None
    #: None means no call has been made yet. Not 0.0: a monotonic clock is
    #: allowed to read zero, and a falsy sentinel would silently disable pacing
    #: on the one call where it matters most.
    _last_call_at: float | None = field(default=None, repr=False)

    def _slow_down(self) -> None:
        """Widen the gap after a rate limit, rather than guessing it up front.

        A published requests-per-minute figure is a moving target and differs
        per model, so the first run of a batch inevitably guesses wrong. Backing
        off on the interval rather than only on the individual request means the
        batch converges on the real limit within a few calls instead of paying
        a full retry chain on every one of them.
        """
        if self.min_interval_seconds <= 0:
            self.min_interval_seconds = BACKOFF_BASE
        else:
            self.min_interval_seconds = min(
                MAX_INTERVAL_SECONDS, self.min_interval_seconds * PACING_GROWTH
            )

    def _pace(self) -> None:
        if self.min_interval_seconds <= 0 or self._last_call_at is None:
            return
        elapsed = self.clock() - self._last_call_at
        if elapsed < self.min_interval_seconds:
            self.sleep(self.min_interval_seconds - elapsed)

    def _send(self, url: str, body: dict) -> dict:
        send = self.transport or _post

        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                result = send(url, body, self.timeout)
                self._last_call_at = self.clock()
                return result
            except urllib.error.HTTPError as exc:
                self._last_call_at = self.clock()
                detail = exc.read().decode("utf-8", "replace")
                if exc.code in RETRYABLE_STATUS and attempt < self.max_retries:
                    delay = retry_after_seconds(detail) or BACKOFF_BASE * (2**attempt)
                    if exc.code == 429:
                        self._slow_down()
                    if self.on_retry:
                        self.on_retry(attempt + 1, delay, f"{exc.code}")
                    self.sleep(delay)
                    continue
                raise LLMError(f"{exc.code} from Gemini: {detail[:300]}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                # A dropped connection or a DNS blip is the most transient
                # failure there is, and the first version of this did not retry
                # it at all: one hiccup mid-batch cost 36 questions.
                self._last_call_at = self.clock()
                reason = getattr(exc, "reason", exc)
                if attempt < self.max_retries:
                    delay = BACKOFF_BASE * (2**attempt)
                    if self.on_retry:
                        self.on_retry(attempt + 1, delay, "network")
                    self.sleep(delay)
                    continue
                raise LLMError(f"could not reach Gemini: {reason}") from exc

        raise LLMError(f"Gemini still rate limiting after {self.max_retries} retries")

    def ask(self, request: Ask) -> Answer:
        url = f"{ENDPOINT}/models/{self.model}:generateContent?key={self.api_key}"
        body = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": to_gemini_schema(request.schema),
                "maxOutputTokens": request.max_tokens,
                # Classification, not composition. Determinism makes the
                # recorded fixtures mean something.
                "temperature": 0.0,
            },
        }

        payload = self._send(url, body)

        if "error" in payload:
            raise LLMError(f"Gemini returned an error: {payload['error']}")

        candidates = payload.get("candidates") or []
        if not candidates:
            reason = payload.get("promptFeedback", {}).get("blockReason", "no candidates")
            raise LLMError(f"Gemini returned nothing usable: {reason}")

        finish = candidates[0].get("finishReason")
        if finish not in (None, "STOP"):
            raise LLMError(f"Gemini stopped early: {finish}")

        try:
            text = candidates[0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected Gemini response shape: {exc}") from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Gemini response was not valid JSON: {exc}") from exc

        usage = payload.get("usageMetadata", {})
        return Answer(
            data=data,
            model=self.model,
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
            cost_usd=FREE_TIER_COST_USD,
            raw_text=text,
        )

    def list_models(self) -> list[str]:
        """What this key can actually reach.

        Model names move faster than any hard-coded default survives, so the
        setup check asks rather than assumes.
        """
        url = f"{ENDPOINT}/models?key={self.api_key}"
        send = self.transport or (lambda u, b, t: _get(u, t))
        try:
            payload = send(url, {}, self.timeout) if self.transport else _get(url, self.timeout)
        except urllib.error.HTTPError as exc:
            raise LLMError(f"{exc.code} listing Gemini models") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"could not reach Gemini: {exc.reason}") from exc

        return [
            m["name"].removeprefix("models/")
            for m in payload.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]


def _get(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
