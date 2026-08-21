"""The three arms.

The control arm is what happens with no product at all. The naive arm is the
product a weekend hackathon produces: retry hard, retry fast, then text
everyone. Counterfoil has to beat both, and the naive arm is deliberately built
to be *good*, because a baseline chosen to lose proves nothing.
"""

from __future__ import annotations

from datetime import timedelta

from ..domain.decision import Channel, Intervention, Proposal
from ..domain.outcome import Arm
from ..kernel.policy import PolicyEngine
from ..kernel.runner import CaseResult, run_case
from ..ledger import Ledger
from ..synth.generator import LatentCase

#: What the naive arm does to every failure, regardless of why it failed:
#: hammer the same instrument, then message the customer.
NAIVE_PLAN = (
    (Intervention.RETRY_SAME_RAIL, 5 / 60, Channel.NONE),
    (Intervention.RETRY_SAME_RAIL, 20 / 60, Channel.NONE),
    (Intervention.RETRY_SAME_RAIL, 1.0, Channel.NONE),
    (Intervention.CUSTOMER_NUDGE, 1.5, Channel.SMS),
)


def _naive_plan(event, diagnosis, step):
    if step >= len(NAIVE_PLAN):
        return None
    intervention, after_hours, channel = NAIVE_PLAN[step]
    return Proposal(
        intervention=intervention,
        scheduled_for=event.occurred_at + timedelta(hours=after_hours),
        channel=channel,
        message_hint="retry hard and fast, ask questions later",
        params={"step": str(step + 1)},
        proposed_by="naive",
    )


def run_control(case: LatentCase, *, engine: PolicyEngine, ledger: Ledger | None = None) -> CaseResult:
    return run_case(case, Arm.CONTROL, engine=engine, ledger=ledger, max_steps=0)


def run_naive(case: LatentCase, *, engine: PolicyEngine, ledger: Ledger | None = None) -> CaseResult:
    """Ungoverned. Policy runs in shadow mode so its breaches are counted."""
    return run_case(
        case,
        Arm.NAIVE,
        engine=engine,
        enforce=False,
        plan=_naive_plan,
        ledger=ledger,
        max_steps=len(NAIVE_PLAN),
    )


def run_agent(
    case: LatentCase,
    *,
    engine: PolicyEngine,
    diagnoser=None,
    ledger: Ledger | None = None,
) -> CaseResult:
    return run_case(case, Arm.AGENT, engine=engine, diagnoser=diagnoser, ledger=ledger)
