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

import contextlib
from dataclasses import dataclass
from datetime import timedelta

from ..domain.decision import Channel, Intervention, Proposal
from ..domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from ..domain.events import RiskEvent, Surface


@dataclass(frozen=True)
class Step:
    intervention: Intervention
    #: Hours after the original failure, not after the previous step. Recovery
    #: windows are anchored to when the money first went at risk.
    after_hours: float
    channel: Channel = Channel.NONE
    why: str = ""
    #: Schedule from the date the buyer promised rather than from the failure.
    #: A promise of "the 15th" and a promise of "Friday" want the same follow-up
    #: shape anchored to different days, and only the model knows which.
    from_promise: bool = False


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

#: Subscriptions are the same kernel with a different calendar.
#:
#: Two things make a mandate failure unlike a card failure. The debit is
#: presented by us rather than attempted by the customer, so nothing happens
#: unless we act. And the money arrives on a salary date, not after a backoff,
#: so every plan below waits days rather than hours and opens with the notice
#: the regulator requires before a debit may be presented again.
SUBSCRIPTION_PLAYBOOKS: dict[RootCause, tuple[Step, ...]] = {
    # The debit landed before the salary did. Telling them it is coming is both
    # legally required and the single most effective thing available: a customer
    # who knows a Rs 499 debit lands tomorrow will often fund the account.
    RootCause.MANDATE_BALANCE_LOW: (
        Step(Intervention.PRE_DEBIT_NOTICE, 2, Channel.SMS,
             why="required before re-presenting, and it prompts them to top up"),
        Step(Intervention.RETRY_SAME_RAIL, 74,
             why="three days out, past the salary date for most customers"),
        Step(Intervention.CUSTOMER_NUDGE, 122, Channel.SMS,
             why="two failed cycles; ask before presenting a third time"),
        Step(Intervention.RETRY_SAME_RAIL, 170,
             why="a week out, across a second credit opportunity"),
    ),
    RootCause.INSUFFICIENT_FUNDS: (
        Step(Intervention.PRE_DEBIT_NOTICE, 2, Channel.SMS,
             why="required before re-presenting, and it prompts them to top up"),
        Step(Intervention.RETRY_SAME_RAIL, 74, why="give the balance three days"),
        Step(Intervention.CUSTOMER_NUDGE, 122, Channel.EMAIL, why="low-cost reminder"),
        Step(Intervention.RETRY_SAME_RAIL, 170, why="final presentation"),
    ),
    RootCause.BANK_DOWNTIME: (
        Step(Intervention.PRE_DEBIT_NOTICE, 1, Channel.SMS, why="notice before re-presenting"),
        Step(Intervention.RETRY_SAME_RAIL, 26, why="sponsor bank should have recovered"),
        Step(Intervention.RETRY_SAME_RAIL, 50, why="second presentation a day later"),
    ),
    RootCause.TECHNICAL_GATEWAY: (
        Step(Intervention.PRE_DEBIT_NOTICE, 1, Channel.SMS, why="notice before re-presenting"),
        Step(Intervention.RETRY_SAME_RAIL, 26, why="present again once the processor is healthy"),
    ),
    # --- terminal: the mandate itself is gone, and presenting it again is both
    # useless and, on a revoked mandate, the thing that generates a complaint.
    RootCause.MANDATE_REVOKED: (
        Step(Intervention.MANDATE_REAUTH, 2, Channel.SMS,
             why="the standing instruction is cancelled; only a new one can work"),
        Step(Intervention.ESCALATE_HUMAN, 96,
             why="no re-authorisation after four days; a person should decide"),
    ),
    RootCause.EXPIRED_INSTRUMENT: (
        Step(Intervention.REQUEST_UPDATED_INSTRUMENT, 2, Channel.SMS,
             why="the card behind the mandate is dead; only a new one can work"),
        Step(Intervention.CUSTOMER_NUDGE, 72, Channel.EMAIL, why="single follow-up"),
    ),
}

#: Receivables. Almost every plan here is a message or a person, because there
#: is nothing to retry: an invoice does not fail, it waits.
RECEIVABLE_PLAYBOOKS: dict[RootCause, tuple[Step, ...]] = {
    # Stuck in their process. A reminder aimed at unblocking it is genuinely
    # useful, and two is the most anyone should send.
    RootCause.INVOICE_AWAITING_APPROVAL: (
        Step(Intervention.INVOICE_REMINDER, 24, Channel.EMAIL,
             why="it is in their queue; a nudge moves it along"),
        Step(Intervention.INVOICE_REMINDER, 192, Channel.EMAIL,
             why="a week on and still unapproved"),
        Step(Intervention.ESCALATE_HUMAN, 384,
             why="two weeks in their process; someone should call them"),
    ),
    # They told us when. The plan is to wait, then follow up once, and the
    # policy engine enforces the waiting.
    RootCause.PROMISE_TO_PAY: (
        Step(Intervention.INVOICE_REMINDER, 24, Channel.EMAIL, from_promise=True,
             why="a day past the date they gave; ask only if it did not arrive"),
        Step(Intervention.INVOICE_REMINDER, 168, Channel.EMAIL, from_promise=True,
             why="a week past a broken promise"),
        Step(Intervention.ESCALATE_HUMAN, 336, from_promise=True,
             why="the commitment was not kept twice; a person should take it"),
    ),
    # They intend to pay and cannot. Pressure is the wrong tool.
    RootCause.INVOICE_CASHFLOW_DELAY: (
        Step(Intervention.INVOICE_REMINDER, 48, Channel.EMAIL,
             why="acknowledge and ask for a date they can commit to"),
        Step(Intervention.INVOICE_REMINDER, 288, Channel.EMAIL,
             why="second and final reminder before a human takes it"),
        Step(Intervention.ESCALATE_HUMAN, 600,
             why="this needs a payment plan, which is not an agent decision"),
    ),
    # Contested. Chasing converts a billing correction into a lost account, and
    # the dunning clause blocks it anyway.
    RootCause.INVOICE_DISPUTED: (
        Step(Intervention.ESCALATE_HUMAN, 2,
             why="a dispute is a support matter; stop collections and route it"),
    ),
}

BY_SURFACE: dict[Surface, dict[RootCause, tuple[Step, ...]]] = {
    Surface.PAYMENTS: PLAYBOOKS,
    Surface.SUBSCRIPTIONS: SUBSCRIPTION_PLAYBOOKS,
    Surface.RECEIVABLES: RECEIVABLE_PLAYBOOKS,
}

#: When we do not know, we do not act. A human sees it instead.
UNKNOWN_PLAYBOOK: tuple[Step, ...] = (
    Step(Intervention.ESCALATE_HUMAN, 2, why="no confident diagnosis; a person should look"),
)


def playbook_for(event: RiskEvent, diagnosis: Diagnosis) -> tuple[Step, ...]:
    """The plan for this cause on this surface.

    Keyed on both, because the same cause wants opposite handling depending on
    where it happened. Insufficient funds on a checkout is a 26 hour retry; on
    a mandate it is a notice, then three days, then a salary date. Collapsing
    them onto one table would mean one of the two surfaces is always wrong.
    """
    if diagnosis.path is DiagnosisPath.DEGRADED or diagnosis.cause is RootCause.UNKNOWN:
        return UNKNOWN_PLAYBOOK
    table = BY_SURFACE.get(event.surface, PLAYBOOKS)
    return table.get(diagnosis.cause, UNKNOWN_PLAYBOOK)


def next_proposal(
    event: RiskEvent, diagnosis: Diagnosis, step_index: int
) -> Proposal | None:
    """The next thing worth trying, or None when the playbook is exhausted.

    Running out of playbook is a real answer. It means we have tried what makes
    sense for this cause and further attempts would be noise, which is exactly
    when most recovery tooling starts sending a fourth message.
    """
    steps = playbook_for(event, diagnosis)
    if step_index >= len(steps):
        return None

    step = steps[step_index]

    anchor = event.occurred_at
    if step.from_promise:
        promised = diagnosis.evidence.get("promised_within_days")
        if promised is not None:
            # A malformed promise falls back to scheduling from the failure
            # date. The model is not trusted to have produced a number.
            with contextlib.suppress(TypeError, ValueError):
                anchor = anchor + timedelta(days=int(promised))

    return Proposal(
        intervention=step.intervention,
        scheduled_for=anchor + timedelta(hours=step.after_hours),
        channel=step.channel,
        message_hint=step.why,
        params={"step": str(step_index + 1), "of": str(len(steps))},
        proposed_by=f"playbook:{diagnosis.cause.value}",
    )
