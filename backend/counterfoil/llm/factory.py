"""Building the diagnoser from configuration.

The point of the LLMClient interface is that changing provider is a line of
config, not a rewrite. This is the one place that knows which concrete client
goes with which provider name, and everything above it stays provider-blind.
"""

from __future__ import annotations

from ..config import Settings
from .budget import Budget
from .cache import FixtureStore
from .client import LLMClient


def build_client(settings: Settings) -> LLMClient | None:
    """The client for the configured provider, or None when we cannot call out.

    Returning None is not an error: replay mode with committed fixtures is the
    normal way this project runs, and it needs no key at all.
    """
    if not settings.can_call_llm:
        return None

    if settings.llm_provider == "gemini":
        from .gemini_client import GeminiClient

        return GeminiClient(api_key=settings.gemini_api_key, model=settings.llm_model)

    if settings.llm_provider == "anthropic":
        from .client import AnthropicClient

        return AnthropicClient(model=settings.llm_model, api_key=settings.anthropic_api_key)

    raise ValueError(f"no client for provider {settings.llm_provider!r}")


def build_diagnoser(settings: Settings, *, mode: str | None = None):
    """A ready diagnoser: client, fixture store and budget wired together."""
    from ..kernel.diagnose.llm import LLMDiagnoser

    store = FixtureStore(settings.llm_fixture_dir, mode=mode or settings.llm_mode)
    return LLMDiagnoser(
        client=build_client(settings) if store.may_call_api else None,
        fixtures=store,
        budget=Budget(cap_usd=settings.spend_cap_usd),
    )
