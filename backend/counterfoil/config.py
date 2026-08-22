"""Configuration, and the guards that make a misconfiguration fail loudly.

Counterfoil is a system that retries payments and messages customers. The two
ways it could do real damage are (a) pointing at live Razorpay credentials and
(b) running with side effects enabled when someone thought it was dry. Both are
checked here, at boot, before anything else can run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class UnsafeConfigError(RuntimeError):
    """Raised when configuration would let the process touch something real."""


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> int:
    """Read a .env file into the environment. Deliberately dependency-free.

    Real environment variables win by default, so CI and a shell export both
    still beat the file. Values are taken literally: no shell expansion, no
    command substitution, nothing that could execute anything.
    """
    file = Path(path)
    if not file.is_file():
        return 0

    loaded = 0
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or (not override and key in os.environ):
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


#: Default model per provider, so switching provider does not also require
#: remembering to change the model name.
DEFAULT_MODEL = {
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-2.5-flash",
}


@dataclass(frozen=True)
class Settings:
    env: str
    dry_run: bool
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    anthropic_api_key: str
    gemini_api_key: str
    llm_provider: str
    llm_model: str
    llm_mode: str
    llm_fixture_dir: Path
    spend_cap_usd: float

    @property
    def has_razorpay(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def api_key(self) -> str:
        return {
            "anthropic": self.anthropic_api_key,
            "gemini": self.gemini_api_key,
        }.get(self.llm_provider, "")

    @property
    def can_call_llm(self) -> bool:
        return self.llm_mode in {"record", "live"} and bool(self.api_key)


def _resolve_provider() -> str:
    """Explicit setting wins; otherwise infer from whichever key is present."""
    declared = _env("COUNTERFOIL_LLM_PROVIDER").lower()
    if declared:
        return declared
    if _env("GEMINI_API_KEY"):
        return "gemini"
    if _env("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "gemini"


def load_settings(*, allow_missing_credentials: bool = True) -> Settings:
    provider = _resolve_provider()
    settings = Settings(
        env=_env("COUNTERFOIL_ENV", "dev"),
        dry_run=_flag("COUNTERFOIL_DRY_RUN", True),
        razorpay_key_id=_env("RAZORPAY_KEY_ID"),
        razorpay_key_secret=_env("RAZORPAY_KEY_SECRET"),
        razorpay_webhook_secret=_env("RAZORPAY_WEBHOOK_SECRET"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        gemini_api_key=_env("GEMINI_API_KEY"),
        llm_provider=provider,
        llm_model=_env("COUNTERFOIL_LLM_MODEL") or DEFAULT_MODEL.get(provider, ""),
        llm_mode=_env("COUNTERFOIL_LLM_MODE", "replay"),
        llm_fixture_dir=Path(_env("COUNTERFOIL_LLM_FIXTURE_DIR", "llm_fixtures")),
        spend_cap_usd=float(_env("COUNTERFOIL_SPEND_CAP_USD", "2.00")),
    )
    assert_safe(settings, allow_missing_credentials=allow_missing_credentials)
    return settings


def assert_safe(settings: Settings, *, allow_missing_credentials: bool = True) -> None:
    """Refuse to run under any configuration that could touch live money."""

    key = settings.razorpay_key_id
    if key:
        if key.startswith("rzp_live_"):
            raise UnsafeConfigError(
                "RAZORPAY_KEY_ID is a LIVE key. Counterfoil never runs against live "
                "credentials. Replace it with an rzp_test_ key."
            )
        if not key.startswith("rzp_test_"):
            raise UnsafeConfigError(
                f"RAZORPAY_KEY_ID must start with 'rzp_test_', got {key[:9]!r}. "
                "If this is a live or unrecognised credential, stop and rotate it."
            )
    elif not allow_missing_credentials:
        raise UnsafeConfigError("RAZORPAY_KEY_ID is required for this operation")

    if settings.llm_provider not in DEFAULT_MODEL:
        raise UnsafeConfigError(
            f"COUNTERFOIL_LLM_PROVIDER must be one of {sorted(DEFAULT_MODEL)}, "
            f"got {settings.llm_provider!r}"
        )

    if settings.llm_mode not in {"replay", "record", "live"}:
        raise UnsafeConfigError(
            f"COUNTERFOIL_LLM_MODE must be replay|record|live, got {settings.llm_mode!r}"
        )

    if settings.spend_cap_usd <= 0:
        raise UnsafeConfigError("COUNTERFOIL_SPEND_CAP_USD must be positive")

    if settings.env == "prod":
        raise UnsafeConfigError(
            "Counterfoil has no production mode. It is a test-mode recovery agent."
        )
