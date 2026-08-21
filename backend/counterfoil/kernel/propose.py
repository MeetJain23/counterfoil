"""Choosing what to try next, given what we believe went wrong.

This is the agent's domain knowledge, and it is deliberately a lookup rather
than a model call. Once you know the card expired, "ask for a new card" is not
a judgement call, and paying a language model to rediscover it every time would
be slower and less predictable than writing it down.

The proposer is stateless and has no authority. It returns a Proposal, which
the policy engine is free to refuse. Timing is chosen to be *plausible* rather
than aggressive: proposing a retry two minutes after a bank outage would simply
be blocked, and a proposer that spends its attempts getting refused is not
clever, it is broken.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..domain.decision import Channel, Intervention, Proposal
from ..domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from ..domain.events import RiskEvent


@dataclass(frozen=True)
class Step:
    intervention: Intervention
    #: Hours after the original failure, not after the previous step. Recovery
    #: windows are anchored to when the money first went at risk.
    after_hours: float
    channel: Channel = Channel.NONE
    why: str = ""


#: The playbooks. Each is an ordered escalation, cheapest and least intrusive
#: first, stopping before it becomes harassment.
PLAYBOOKS: dict[RootCause, tuple[Step, ...]] = {
    # A blip. Wait out the minimum backoff and try again; most of these close
    # on the first retry and never need to bother the customer at all.
    RootCause.TECHNICAL_GATEWAY: (
        Step(Intervention.RETRY_SAME_RAIL, 0.75, why="transient processor error, retry after backoff"),
        Step(Intervention.RETRY_ALTERNATE_RAIL, 4, why="second failure suggests the rail, not the moment"),
        Step(Intervention.SEND_PAYMENT_LINK, 24, Channel.SMS, why="two silent failures, hand it back to the customer"),
    ),
    # Timing is the entire intervention. Retrying into a live outage wastes an
    # attempt for close to nothing.
    RootCause.BANK_DOWNTIME: (
        Step(Intervention.RETRY_SAME_RAIL, 2, why="past the outage hold, bank likely recovered"),
        Step(Intervention.RETRY_SAME_RAIL, 8, why="outage may be longer than typical"),
        Step(Intervention.RETRY_ALTERNATE_RAIL, 20, why="bank still failing, route around it"),
    ),
    # The balance has to change. Retry on the human calendar, not a machine one.
    RootCause.INSUFFICIENT_FUNDS: (
        Step(Intervention.RETRY_SAME_RAIL, 26, why="give the balance a day to change"),
        Step(Intervention.CUSTOMER_NUDGE, 50, Channel.SMS, why="tell them before trying a third time"),
        Step(Intervention.RETRY_SAME_RAIL, 74, why="third attempt across a likely credit cycle"),
    ),
    # Retrying the charge does nothing; they abandoned the OTP screen. The only
    # thing that works is getting them back to a checkout.
    RootCause.AUTHENTICATION_DROPOFF: (
        Step(Intervention.SEND_PAYMENT_LINK, 1, Channel.SMS, why="drop-off is a return-to-checkout problem"),
        Step(Intervention.RETRY_ALTERNATE_RAIL, 12, why="the authentication step itself may be the obstacle"),
        Step(Intervention.CUSTOMER_NUDGE, 48, Channel.EMAIL, why="one last low-cost reminder"),
    ),
    RootCause.CUSTOMER_ABANDONED: (
        Step(Intervention.SEND_PAYMENT_LINK, 2, Channel.SMS, why="make returning to the cart one tap"),
        Step(Intervention.CUSTOMER_NUDGE, 48, Channel.EMAIL, why="second and final touch"),
    ),
    # Might succeed later; the issuer's risk view is not permanent.
    RootCause.ISSUER_DECLINE_SOFT: (
        Step(Intervention.RETRY_SAME_RAIL, 8, why="soft decline, issuer risk view may have moved"),
        Step(Intervention.RETRY_ALTERNATE_RAIL, 20, why="try a different instrument entirely"),
        Step(Intervention.CUSTOMER_NUDGE, 60, Channel.SMS, why="ask the customer to intervene with their bank"),
    ),
    # --- terminal instruments: never retry, always ask for a new one ---
    RootCause.EXPIRED_INSTRUMENT: (
        Step(Intervention.REQUEST_UPDATED_INSTRUMENT, 1, Channel.SMS, why="the card is dead, only a new one can work"),
        Step(Intervention.CUSTOMER_NUDGE, 72, Channel.EMAIL, why="single follow-up on the update request"),
    ),
    RootCause.ISSUER_DECLINE_HARD: (
        Step(Intervention.REQUEST_UPDATED_INSTRUMENT, 1, Channel.SMS, why="instrument is blocked at the issuer"),
        Step(Intervention.CUSTOMER_NUDGE, 72, Channel.EMAIL, why="single follow-up"),
    ),
    RootCause.INTERNATIONAL_BLOCKED: (
        Step(Intervention.REQUEST_UPDATED_INSTRUMENT, 1, Channel.SMS, why="card cannot transact here at all"),
        Step(Intervention.ESCALATE_HUMAN, 48, why="cross-border blocks often need a person"),
    ),
}

#: When we do not know, we do not act. A human sees it instead.
UNKNOWN_PLAYBOOK: tuple[Step, ...] = (
    Step(Intervention.ESCALATE_HUMAN, 2, why="no confident diagnosis; a person should look"),
)


def playbook_for(diagnosis: Diagnosis) -> tuple[Step, ...]:
    if diagnosis.path is DiagnosisPath.DEGRADED or diagnosis.cause is RootCause.UNKNOWN:
        return UNKNOWN_PLAYBOOK
    return PLAYBOOKS.get(diagnosis.cause, UNKNOWN_PLAYBOOK)


def next_proposal(
    event: RiskEvent, diagnosis: Diagnosis, step_index: int
) -> Proposal | None:
    """The next thing worth trying, or None when the playbook is exhausted.

    Running out of playbook is a real answer. It means we have tried what makes
    sense for this cause and further attempts would be noise, which is exactly
    when most recovery tooling starts sending a fourth message.
    """
    steps = playbook_for(diagnosis)
    if step_index >= len(steps):
        return None

    step = steps[step_index]
    return Proposal(
        intervention=step.intervention,
        scheduled_for=event.occurred_at + timedelta(hours=step.after_hours),
        channel=step.channel,
        message_hint=step.why,
        params={"step": str(step_index + 1), "of": str(len(steps))},
        proposed_by=f"playbook:{diagnosis.cause.value}",
    )
