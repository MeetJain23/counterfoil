"""Deterministic diagnosis from provider error codes.

Most payment failures arrive well-labelled. The provider already knows the card
expired; asking a language model to rediscover that from prose is slower, more
expensive, and less reliable than a lookup. This table handles those, and
returns ``None`` for anything it cannot resolve honestly — which is the signal
that a case genuinely needs the model.

**This table is written from the provider's error taxonomy, not from the
synthetic generator.** Nothing here imports ``counterfoil.synth``, and a test
enforces that. If the rules were derived from the generator they would agree
with it by construction, and the reported diagnosis accuracy would be measuring
nothing at all.
"""

from __future__ import annotations

from ...domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from ...domain.events import RiskEvent

#: error_reason -> (cause, confidence). Confidence reflects how much the code
#: actually pins down: "card_expired" is unambiguous, while a generic issuer
#: decline leaves real room for the reason to be something else.
REASON_MAP: dict[str, tuple[RootCause, float]] = {
    "card_expired": (RootCause.EXPIRED_INSTRUMENT, 0.98),
    "insufficient_funds": (RootCause.INSUFFICIENT_FUNDS, 0.96),
    "international_transaction_not_allowed": (RootCause.INTERNATIONAL_BLOCKED, 0.96),
    "card_blocked": (RootCause.ISSUER_DECLINE_HARD, 0.94),
    "card_disabled_for_online": (RootCause.ISSUER_DECLINE_HARD, 0.92),
    "payment_cancelled_by_customer": (RootCause.CUSTOMER_ABANDONED, 0.93),
    "bank_technical_error": (RootCause.BANK_DOWNTIME, 0.90),
    "gateway_technical_error": (RootCause.TECHNICAL_GATEWAY, 0.90),
    "payment_timed_out": (RootCause.TECHNICAL_GATEWAY, 0.78),
    "payment_authentication_failed": (RootCause.AUTHENTICATION_DROPOFF, 0.86),
    "invalid_card_expiry": (RootCause.EXPIRED_INSTRUMENT, 0.88),
    "payment_declined_by_issuer": (RootCause.ISSUER_DECLINE_SOFT, 0.80),
    "mandate_revoked": (RootCause.MANDATE_REVOKED, 0.95),
    "mandate_insufficient_balance": (RootCause.MANDATE_BALANCE_LOW, 0.93),
}

#: Reasons the provider emits when it does not actually know. Matching one of
#: these is not a diagnosis; it is the absence of one.
GENERIC_REASONS: frozenset[str] = frozenset({
    "payment_failed",
    "",
})

#: Weak corroboration. Never enough to diagnose on its own, but enough to nudge
#: confidence when the reason code already points somewhere.
SOURCE_CORROBORATION: dict[tuple[str, RootCause], float] = {
    ("issuer", RootCause.INSUFFICIENT_FUNDS): +0.02,
    ("issuer", RootCause.ISSUER_DECLINE_SOFT): +0.03,
    ("issuer", RootCause.ISSUER_DECLINE_HARD): +0.03,
    ("bank", RootCause.BANK_DOWNTIME): +0.04,
    ("gateway", RootCause.TECHNICAL_GATEWAY): +0.04,
    ("customer", RootCause.AUTHENTICATION_DROPOFF): +0.05,
    ("customer", RootCause.CUSTOMER_ABANDONED): +0.04,
    # A "customer cancelled" that the gateway reports is less trustworthy than
    # one the customer's own session reported.
    ("gateway", RootCause.CUSTOMER_ABANDONED): -0.06,
}


def diagnose(event: RiskEvent) -> Diagnosis | None:
    """Resolve a cause from provider codes, or return None to defer.

    Returning None is a first-class outcome, not a failure. It routes the case
    to the model, and the share of a batch that lands here is exactly what the
    reported LLM-usage figure measures.
    """
    signals = event.provider_signals
    reason = str(signals.get("error_reason", "")).strip().lower()

    if reason in GENERIC_REASONS or reason not in REASON_MAP:
        return None

    cause, confidence = REASON_MAP[reason]

    source = str(signals.get("error_source", "")).strip().lower()
    confidence = min(0.99, confidence + SOURCE_CORROBORATION.get((source, cause), 0.0))

    evidence = {
        k: str(signals[k])
        for k in ("error_code", "error_reason", "error_source", "error_step", "method")
        if k in signals
    }

    return Diagnosis(
        cause=cause,
        confidence=confidence,
        path=DiagnosisPath.RULE,
        rationale=(
            f"provider reported error_reason={reason!r} from source={source or 'unspecified'}, "
            f"which maps to {cause.value}"
        ),
        evidence=evidence,
    )
