"""Resolving what actually happened, given what an arm chose to do.

The world is deterministic given a ``LatentCase``: all randomness was drawn at
generation time. Two arms that take different actions on the same case are
therefore compared against the same underlying customer, the same bank, and the
same luck.

The one thing this module exposes that no real system can is
``would_have_recovered_anyway``, whether the money would have arrived with no
intervention at all. That is the number every recovery product quietly needs
and none can observe in production. Having it here is the whole reason the eval
can report incremental rather than gross recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..domain.decision import CONTACTING, Channel, Intervention
from ..domain.money import Money
from ..domain.outcome import Arm, Outcome, OutcomeState
from .generator import MAX_DRAWS, LatentCase
from .profiles import INTERVENTION_COST_PAISE, SEGMENTS, SURFACE_BEHAVIOUR

_SEGMENT_BY_NAME = {s.name: s for s in SEGMENTS}

#: Each additional message to the same customer lands with less force.
CONTACT_FATIGUE = 0.55

#: A human working the case. Effective, and by far the most expensive option,
#: which is why the policy engine treats escalation as a last resort.
ESCALATION_SUCCESS = 0.42

#: Interventions that specifically address a broken instrument, and the causes
#: they actually address. Encoding this is what separates choosing the right
#: intervention from simply choosing one.
INTERVENTION_FIT: dict[Intervention, tuple[frozenset, float]] = {
    Intervention.REQUEST_UPDATED_INSTRUMENT: (
        frozenset({"expired_instrument", "issuer_decline_hard", "international_blocked"}),
        1.65,
    ),
    Intervention.SEND_PAYMENT_LINK: (
        frozenset({"authentication_dropoff", "customer_abandoned"}),
        1.45,
    ),
    # Telling somebody a debit is coming tomorrow is the one message that
    # reliably makes them fund the account, which is why the regulator made it
    # mandatory and why it is worth more than a reminder after the fact.
    Intervention.PRE_DEBIT_NOTICE: (
        frozenset({"mandate_balance_low", "insufficient_funds"}),
        1.70,
    ),
    Intervention.MANDATE_REAUTH: (
        frozenset({"mandate_revoked", "expired_instrument"}),
        1.55,
    ),
}


@dataclass(frozen=True)
class TakenAction:
    intervention: Intervention
    at: datetime
    channel: Channel = Channel.NONE

    @property
    def cost_paise(self) -> int:
        return INTERVENTION_COST_PAISE.get(self.intervention.value, 0)


@dataclass(frozen=True)
class SimResult:
    outcome: Outcome
    #: True when recovery is genuinely caused by an action rather than by the
    #: customer getting there on their own.
    attributable: bool
    #: The counterfactual: what the control arm saw on this exact case.
    would_have_recovered_anyway: bool
    #: Which action closed it, if any.
    closed_by: Intervention | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def wasted_spend(self) -> Money:
        """Money spent chasing someone who was going to pay regardless.

        Gross recovery figures hide this entirely. It is the closest thing this
        surface has to a false-positive cost.
        """
        if self.would_have_recovered_anyway:
            return self.outcome.intervention_cost
        return Money.zero()


def _ripeness(ripens_after_hours: float, delay_hours: float) -> float:
    """How much of an intervention's power is available this early.

    Acting immediately is not worthless, but on causes that need time to
    change - a balance, an outage - it is worth about a sixth of acting once
    the condition has actually had a chance to resolve.
    """
    if ripens_after_hours <= 0:
        return 1.0
    return 0.15 + 0.85 * min(1.0, max(0.0, delay_hours) / ripens_after_hours)


def behaviour_for(case: LatentCase):
    return SURFACE_BEHAVIOUR[case.event.surface][case.true_cause]


def ripens_after(case: LatentCase) -> float:
    """When this case becomes worth retrying.

    A per-case override beats the per-cause default, because on subscriptions
    the answer is not "after N hours" but "after this particular customer gets
    paid", and those differ by two weeks across a book.
    """
    if case.ripens_after_hours is not None:
        return case.ripens_after_hours
    return behaviour_for(case).retry_ripens_after_hours


def _success_probability(
    case: LatentCase, action: TakenAction, contacts_before: int
) -> float:
    behaviour = behaviour_for(case)
    segment = _SEGMENT_BY_NAME[case.segment]
    delay_hours = (action.at - case.event.occurred_at).total_seconds() / 3600.0
    iv = action.intervention

    if iv is Intervention.NO_ACTION:
        return 0.0

    if iv is Intervention.ESCALATE_HUMAN:
        return ESCALATION_SUCCESS * segment.responsiveness ** 0.5

    if iv is Intervention.RETRY_SAME_RAIL:
        if behaviour.terminal:
            return 0.0
        return behaviour.retry_success * _ripeness(ripens_after(case), delay_hours)

    if iv is Intervention.RETRY_ALTERNATE_RAIL:
        ripeness = _ripeness(ripens_after(case), delay_hours)
        return behaviour.alt_rail_success * (0.55 + 0.45 * ripeness)

    if iv in CONTACTING:
        p = behaviour.nudge_lift * segment.responsiveness
        fit_causes, bonus = INTERVENTION_FIT.get(iv, (frozenset(), 1.0))
        if case.true_cause.value in fit_causes:
            p *= bonus
        return p * (CONTACT_FATIGUE ** contacts_before)

    return 0.0


def spontaneous_probability(case: LatentCase) -> float:
    behaviour = behaviour_for(case)
    segment = _SEGMENT_BY_NAME[case.segment]
    return min(0.95, behaviour.spontaneous * segment.self_serve)


def attempt(
    case: LatentCase, action: TakenAction, draw_index: int, contacts_before: int
) -> tuple[bool, float]:
    """Play a single action and report whether it landed.

    The step-wise entry point. A real recovery agent does not commit to a plan
    of five actions up front; it tries one, sees what happened, and decides
    again with that knowledge. The runner drives this one action at a time so
    the audit trail records a sequence of decisions rather than a batch
    submission, and so a stopping rule can actually stop something.

    Returns (landed, probability) so callers can record the probability that
    was faced, not just the coin flip that came back.
    """
    if draw_index >= MAX_DRAWS:
        raise ValueError(
            f"attempt {draw_index} exceeds the {MAX_DRAWS} pre-drawn outcomes; "
            "a policy this permissive is the bug, not the draw budget"
        )
    p = _success_probability(case, action, contacts_before)
    return case.draws[draw_index] < p, p


def resolve(case: LatentCase, actions: list[TakenAction], arm: Arm) -> SimResult:
    """Play one case forward under one arm's chosen actions."""
    if len(actions) > MAX_DRAWS:
        raise ValueError(
            f"{len(actions)} actions exceeds the {MAX_DRAWS} pre-drawn outcomes; "
            "a policy this permissive is the bug, not the draw budget"
        )

    would_recover_anyway = case.u_spontaneous < spontaneous_probability(case)
    notes: list[str] = []

    spent = 0
    contacts = 0
    closed_by: Intervention | None = None
    attributable = False

    for index, action in enumerate(actions):
        contacts_before = contacts
        spent += action.cost_paise
        if action.intervention in CONTACTING:
            contacts += 1

        landed, p = attempt(case, action, index, contacts_before)
        if landed:
            closed_by = action.intervention
            attributable = not would_recover_anyway
            notes.append(
                f"{action.intervention.value} closed the case (p={p:.2f})"
                + ("" if attributable else "; customer would have paid regardless")
            )
            break
        notes.append(f"{action.intervention.value} did not land (p={p:.2f})")

    recovered_now = closed_by is not None or would_recover_anyway
    if closed_by is None and would_recover_anyway:
        notes.append("recovered with no successful intervention: customer self-served")

    if recovered_now:
        state = OutcomeState.RECOVERED
        recovered = case.event.amount
        evidence = {
            "payment_id": f"pay_SYN{case.event.event_id[-8:]}",
            "source": "synthetic-world",
            "closed_by": closed_by.value if closed_by else "self_serve",
        }
    else:
        state = (
            OutcomeState.NOT_ATTEMPTED
            if not actions
            else OutcomeState.ESCALATED
            if actions[-1].intervention is Intervention.ESCALATE_HUMAN
            else OutcomeState.STILL_AT_RISK
        )
        recovered = Money.zero()
        evidence = {"source": "synthetic-world"}

    outcome = Outcome(
        event_id=case.event_id,
        arm=arm,
        state=state,
        observed_at=actions[-1].at if actions else case.event.occurred_at,
        recovered=recovered,
        intervention_cost=Money(spent),
        contacts_made=contacts,
        evidence=evidence,
    )

    return SimResult(
        outcome=outcome,
        attributable=attributable,
        would_have_recovered_anyway=would_recover_anyway,
        closed_by=closed_by,
        notes=notes,
    )
