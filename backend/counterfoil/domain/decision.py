"""What we propose to do, and whether policy lets us do it.

Two rules hold everywhere in Counterfoil:

1. Whatever produced the proposal (rules or an LLM) has no authority. It may
   only *propose*. The policy engine decides.
2. Nothing executes without citing the clause that permitted it, and nothing is
   refused without citing the clause that blocked it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class Intervention(str, enum.Enum):
    RETRY_SAME_RAIL = "retry_same_rail"
    RETRY_ALTERNATE_RAIL = "retry_alternate_rail"
    REQUEST_UPDATED_INSTRUMENT = "request_updated_instrument"
    SEND_PAYMENT_LINK = "send_payment_link"
    CUSTOMER_NUDGE = "customer_nudge"
    MANDATE_REAUTH = "mandate_reauth"
    PRE_DEBIT_NOTICE = "pre_debit_notice"
    INVOICE_REMINDER = "invoice_reminder"
    ESCALATE_HUMAN = "escalate_human"
    NO_ACTION = "no_action"


#: Interventions that contact the customer. These are the ones that carry
#: reputational cost and are governed by quiet hours and contact frequency caps.
CONTACTING: frozenset[Intervention] = frozenset({
    Intervention.REQUEST_UPDATED_INSTRUMENT,
    Intervention.SEND_PAYMENT_LINK,
    Intervention.CUSTOMER_NUDGE,
    Intervention.MANDATE_REAUTH,
    Intervention.INVOICE_REMINDER,
    Intervention.PRE_DEBIT_NOTICE,
})

#: Telling a customer a recurring debit is coming is required before presenting
#: it, so it is not discretionary contact in the way a reminder is. It still
#: counts against quiet hours, because a 3am notice is not a notice.
MANDATORY_NOTICE: frozenset[Intervention] = frozenset({Intervention.PRE_DEBIT_NOTICE})

#: Interventions that spend money at the gateway when they run.
BILLABLE: frozenset[Intervention] = frozenset({
    Intervention.RETRY_SAME_RAIL,
    Intervention.RETRY_ALTERNATE_RAIL,
})


class Channel(str, enum.Enum):
    SMS = "sms"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    NONE = "none"


@dataclass(frozen=True)
class Proposal:
    """A suggestion. Carries no authority until policy signs off."""

    intervention: Intervention
    scheduled_for: datetime | None = None
    channel: Channel = Channel.NONE
    #: Free-text only ever shown to a human or sent after policy approval.
    message_hint: str = ""
    params: dict[str, str] = field(default_factory=dict)
    proposed_by: str = "rules"


@dataclass(frozen=True)
class ClauseEval:
    """One policy clause, evaluated against one proposal."""

    clause_id: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'BLOCK'}] {self.clause_id}: {self.detail}"


@dataclass(frozen=True)
class Decision:
    """The policy engine's ruling on a proposal. Immutable and fully explained."""

    proposal: Proposal
    allowed: bool
    clauses: list[ClauseEval]
    #: Set when a proposal is blocked but a weaker fallback is permitted instead.
    substituted_with: Proposal | None = None

    def __post_init__(self) -> None:
        if not self.clauses:
            raise ValueError("a decision with no evaluated clauses is not auditable")
        if self.allowed and any(not c.passed for c in self.clauses):
            raise ValueError("cannot allow an action while a clause blocks it")
        if not self.allowed and all(c.passed for c in self.clauses):
            raise ValueError("cannot block an action without a blocking clause")

    @property
    def blocking_clauses(self) -> list[ClauseEval]:
        return [c for c in self.clauses if not c.passed]

    @property
    def citation(self) -> str:
        """One line naming exactly why this went the way it did."""
        if self.allowed:
            return "permitted by " + ", ".join(c.clause_id for c in self.clauses)
        return "blocked by " + ", ".join(c.clause_id for c in self.blocking_clauses)

    @property
    def effective(self) -> Proposal:
        if self.allowed:
            return self.proposal
        if self.substituted_with is not None:
            return self.substituted_with
        return Proposal(Intervention.NO_ACTION, proposed_by="policy")
