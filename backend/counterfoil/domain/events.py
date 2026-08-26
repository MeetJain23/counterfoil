"""The normalised unit of work: something that put money at risk.

Every surface adapter (payments, subscriptions, receivables) converts its own
provider-shaped payload into a RiskEvent. The kernel below this line never
knows which surface it is looking at.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .money import Money


class Surface(str, enum.Enum):
    PAYMENTS = "payments"
    SUBSCRIPTIONS = "subscriptions"
    RECEIVABLES = "receivables"


class RiskKind(str, enum.Enum):
    """What went wrong, in provider-neutral terms."""

    PAYMENT_FAILED = "payment_failed"
    CHECKOUT_ABANDONED = "checkout_abandoned"
    MANDATE_CHARGE_FAILED = "mandate_charge_failed"
    MANDATE_EXPIRED = "mandate_expired"
    INVOICE_OVERDUE = "invoice_overdue"
    PROMISE_BROKEN = "promise_broken"


@dataclass(frozen=True)
class Customer:
    """Contact handles are stored redacted-ready; the ledger never writes them raw."""

    ref: str
    phone_last4: str | None = None
    email_domain: str | None = None
    segment: str = "unknown"

    @property
    def redacted(self) -> str:
        bits = [self.ref]
        if self.phone_last4:
            bits.append(f"ph:****{self.phone_last4}")
        if self.email_domain:
            bits.append(f"em:***@{self.email_domain}")
        return " ".join(bits)


@dataclass(frozen=True)
class RiskEvent:
    event_id: str
    surface: Surface
    kind: RiskKind
    occurred_at: datetime
    amount: Money
    customer: Customer
    # Provider-native fields, kept verbatim so diagnosis is explainable and auditable.
    provider_signals: dict[str, Any] = field(default_factory=dict)
    # Merchant/account context the policy engine may need (e.g. prior attempts).
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("RiskEvent.occurred_at must be timezone-aware")

    @property
    def attempts_so_far(self) -> int:
        return int(self.context.get("attempts_so_far", 0))

    @property
    def contacts_last_7d(self) -> int:
        return int(self.context.get("contacts_last_7d", 0))

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return (now - self.occurred_at).total_seconds() / 3600.0
