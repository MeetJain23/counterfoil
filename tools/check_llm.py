#!/usr/bin/env python3
"""Verify the model layer end to end before spending anything on it.

    python tools/check_llm.py

Prints the resolved configuration, lists the models the key can actually
reach, and runs one real classification against a known payload so you can see
the answer rather than trust it. One call, so on the Gemini free tier it costs
nothing and on Anthropic it costs a fraction of a cent.

Model names move faster than a hard-coded default survives, so this asks the
provider what exists rather than assuming.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from counterfoil.config import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from dataclasses import replace  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from counterfoil.config import load_settings  # noqa: E402
from counterfoil.domain.events import Customer, RiskEvent, RiskKind, Surface  # noqa: E402
from counterfoil.domain.money import Money  # noqa: E402
from counterfoil.kernel.diagnose.llm import LLMDiagnoser, build_user_prompt  # noqa: E402
from counterfoil.llm import Budget, FixtureStore, LLMError, build_client  # noqa: E402
from counterfoil.llm.gemini_client import suggested_replacement  # noqa: E402

PROBE = RiskEvent(
    event_id="evt_probe",
    surface=Surface.PAYMENTS,
    kind=RiskKind.PAYMENT_FAILED,
    occurred_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
    amount=Money.rupees(1899),
    customer=Customer("cus_probe"),
    provider_signals={
        "error_code": "BAD_REQUEST_ERROR",
        "error_reason": "payment_failed",
        "error_source": "gateway",
        "error_step": "payment_authorization",
        "error_description": "The remitter bank declined to process the request right now.",
        "method": "upi",
    },
)

#: What a competent classifier should say about the probe. Not asserted, just
#: shown, because the point is for a human to look at the answer.
EXPECTED = "bank_downtime"


def main() -> int:
    settings = load_settings()

    print(f"provider : {settings.llm_provider}")
    print(f"model    : {settings.llm_model}")
    print(f"mode     : {settings.llm_mode}")
    print(f"key      : {'set' if settings.api_key else 'MISSING'}")
    print(f"fixtures : {settings.llm_fixture_dir}")

    if not settings.api_key:
        key_name = "GEMINI_API_KEY" if settings.llm_provider == "gemini" else "ANTHROPIC_API_KEY"
        print(f"\nNo key found. Put {key_name} in .env and try again.", file=sys.stderr)
        return 2

    # A check is a deliberate live call even when the project is set to replay.
    client = build_client(replace(settings, llm_mode="live"))
    if client is None:
        print("\nCould not build a client for this configuration.", file=sys.stderr)
        return 2

    if hasattr(client, "list_models"):
        print()
        try:
            models = client.list_models()
        except LLMError as exc:
            print(f"could not list models: {exc}", file=sys.stderr)
            return 1
        # Print all of them. Truncating this list once hid the very model the
        # API was telling us to switch to.
        print(f"models listed for this key ({len(models)}):")
        for name in models:
            marker = "  <- configured" if name == settings.llm_model else ""
            print(f"    {name}{marker}")
        if settings.llm_model not in models:
            print(f"\n  WARNING: {settings.llm_model} is not in the list above.")
            print("  Set COUNTERFOIL_LLM_MODEL in .env to one that is.")
        print()
        print("  Being listed does not mean being callable: models can be retired")
        print("  for new keys while still appearing here. The probe below is the")
        print("  only thing that actually settles it.")

    print()
    print("probe payload:")
    for line in build_user_prompt(PROBE).splitlines():
        print(f"    {line}")

    diagnoser = LLMDiagnoser(
        client=client,
        fixtures=FixtureStore(settings.llm_fixture_dir, mode="live"),
        budget=Budget(cap_usd=0.05),
    )
    result = diagnoser(PROBE)

    print()
    print(f"cause      : {result.cause.value}  (expected {EXPECTED})")
    print(f"confidence : {result.confidence:.2f}")
    print(f"path       : {result.path.value}")
    print(f"rationale  : {result.rationale}")
    print(f"cost       : ${result.llm_cost_usd:.6f}")

    if result.path.value == "degraded":
        print("\nThe call did not produce a usable classification. The message above")
        print("says why. Nothing downstream would act on this; it would go to a human.")

        replacement = suggested_replacement(result.rationale)
        if replacement and replacement != settings.llm_model:
            print()
            print(f"  The provider named a replacement: {replacement}")
            print(f"  Set this in .env, then re-run:")
            print(f"      COUNTERFOIL_LLM_MODEL={replacement}")
        return 1

    print("\nWorking. Run tools/record_fixtures.py to record the full set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
