"""What actually happened after we acted, and what it was worth.

``recovered`` is only ever set from an observed provider state change, never
inferred from the fact that we took an action. Attribution requires evidence.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from .money import Money


class OutcomeState(str, enum.Enum):
    PENDING = "pending"
    RECOVERED = "recovered"
    STILL_AT_RISK = "still_at_risk"
    WRITTEN_OFF = "written_off"
    ESCALATED = "escalated"
    NOT_ATTEMPTED = "not_attempted"


class Arm(str, enum.Enum):
    """Experiment arm. Everything is measured against these, never in isolation."""

    CONTROL = "control"   # do nothing at all
    NAIVE = "naive"       # retry everything, contact everyone, immediately
    AGENT = "agent"       # Counterfoil


@dataclass(frozen=True)
class Outcome:
    event_id: str
    arm: Arm
    state: OutcomeState
    observed_at: datetime
    recovered: Money
    #: What the attempt cost us: gateway retry fees, SMS/WhatsApp send cost.
    intervention_cost: Money
    #: Number of times we contacted the customer to get here.
    contacts_made: int = 0
    #: Provider evidence for the state change (payment id, webhook event id).
    evidence: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state is OutcomeState.RECOVERED and self.recovered.paise <= 0:
            raise ValueError("RECOVERED outcome must carry a positive amount")
        if self.state is OutcomeState.RECOVERED and not self.evidence:
            raise ValueError("RECOVERED requires provider evidence; no evidence, no credit")

    @property
    def net(self) -> Money:
        return self.recovered - self.intervention_cost
