"""Why the money is at risk, and which reasoning path decided that.

``path`` is deliberately part of the record. Being able to report "the LLM
touched 18% of decisions and here is which ones" is the point, not a footnote.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class RootCause(str, enum.Enum):
    # --- bank / gateway side ---
    BANK_DOWNTIME = "bank_downtime"
    TECHNICAL_GATEWAY = "technical_gateway"
    # --- issuer decisions ---
    INSUFFICIENT_FUNDS = "insufficient_funds"
    ISSUER_DECLINE_SOFT = "issuer_decline_soft"
    ISSUER_DECLINE_HARD = "issuer_decline_hard"
    EXPIRED_INSTRUMENT = "expired_instrument"
    INTERNATIONAL_BLOCKED = "international_blocked"
    # --- customer behaviour ---
    AUTHENTICATION_DROPOFF = "authentication_dropoff"
    CUSTOMER_ABANDONED = "customer_abandoned"
    # --- mandates ---
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_BALANCE_LOW = "mandate_balance_low"
    # --- receivables ---
    INVOICE_DISPUTED = "invoice_disputed"
    INVOICE_AWAITING_APPROVAL = "invoice_awaiting_approval"
    INVOICE_CASHFLOW_DELAY = "invoice_cashflow_delay"
    PROMISE_TO_PAY = "promise_to_pay"
    # --- the honest one ---
    UNKNOWN = "unknown"


class DiagnosisPath(str, enum.Enum):
    RULE = "rule"           # deterministic map from provider codes
    LLM = "llm"             # model was consulted because rules were insufficient
    DEGRADED = "degraded"   # LLM was needed but unavailable: we withhold, never fabricate


#: Causes where retrying the same instrument can never succeed. Retrying these
#: burns gateway fees and customer patience for a guaranteed zero.
TERMINAL_CAUSES: frozenset[RootCause] = frozenset({
    RootCause.ISSUER_DECLINE_HARD,
    RootCause.EXPIRED_INSTRUMENT,
    RootCause.MANDATE_REVOKED,
    RootCause.INTERNATIONAL_BLOCKED,
    RootCause.INVOICE_DISPUTED,
})


@dataclass(frozen=True)
class Diagnosis:
    cause: RootCause
    confidence: float
    path: DiagnosisPath
    rationale: str
    #: Verbatim provider fields the conclusion rests on. Shown in the audit UI.
    evidence: dict[str, str] = field(default_factory=dict)
    #: Populated only when path is LLM, for cost attribution.
    llm_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        if self.path is DiagnosisPath.DEGRADED and self.cause is not RootCause.UNKNOWN:
            raise ValueError("a degraded diagnosis must report UNKNOWN, not a guess")

    @property
    def is_terminal(self) -> bool:
        return self.cause in TERMINAL_CAUSES
