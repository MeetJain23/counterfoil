from datetime import UTC, datetime, timedelta

import pytest

from counterfoil.domain.decision import Channel, Intervention, Proposal
from counterfoil.domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from counterfoil.domain.events import Customer, RiskEvent, RiskKind, Surface
from counterfoil.domain.money import Money
from counterfoil.kernel.policy import IST, PolicyEngine

# A Tuesday, 14:30 IST -- comfortably outside quiet hours.
BASE = datetime(2026, 8, 18, 14, 30, tzinfo=IST).astimezone(UTC)


@pytest.fixture
def engine():
    return PolicyEngine()


def event(**over):
    fields = dict(
        event_id="evt_1",
        surface=Surface.PAYMENTS,
        kind=RiskKind.PAYMENT_FAILED,
        occurred_at=BASE,
        amount=Money.rupees(2499),
        customer=Customer("cus_1", phone_last4="4412"),
        provider_signals={},
        context={},
    )
    fields.update(over)
    return RiskEvent(**fields)


def dx(cause=RootCause.TECHNICAL_GATEWAY, confidence=0.9, path=DiagnosisPath.RULE):
    return Diagnosis(cause, confidence, path, "test diagnosis")


def clause(decision, clause_id):
    return next(c for c in decision.clauses if c.clause_id == clause_id)


# --------------------------------------------------------------------- #
# the happy path                                                        #
# --------------------------------------------------------------------- #


def test_a_clean_retry_is_permitted_and_cites_its_clauses(engine):
    d = engine.evaluate(event(), dx(), Proposal(Intervention.RETRY_SAME_RAIL))
    assert d.allowed
    assert d.citation.startswith("permitted by")
    assert "retry.max_attempts" in d.citation


def test_no_action_needs_no_gate(engine):
    d = engine.evaluate(event(), dx(), Proposal(Intervention.NO_ACTION))
    assert d.allowed
    assert clause(d, "global.no_gate_applies").passed


# --------------------------------------------------------------------- #
# stopping rules                                                        #
# --------------------------------------------------------------------- #


def test_retry_stops_at_the_attempt_cap(engine):
    d = engine.evaluate(
        event(context={"attempts_so_far": 3}), dx(), Proposal(Intervention.RETRY_SAME_RAIL)
    )
    assert not d.allowed
    assert clause(d, "retry.max_attempts").passed is False
    assert d.effective.intervention is Intervention.NO_ACTION


def test_terminal_causes_are_never_retried(engine):
    d = engine.evaluate(
        event(), dx(cause=RootCause.ISSUER_DECLINE_HARD), Proposal(Intervention.RETRY_SAME_RAIL)
    )
    assert not d.allowed
    assert "retry.not_terminal_cause" in d.citation


def test_lifetime_action_cap_halts_everything(engine):
    d = engine.evaluate(
        event(context={"actions_taken": 5}), dx(), Proposal(Intervention.CUSTOMER_NUDGE, channel=Channel.SMS)
    )
    assert not d.allowed
    assert clause(d, "global.max_total_actions").passed is False


def test_low_value_recovery_is_not_worth_chasing(engine):
    d = engine.evaluate(event(amount=Money.rupees(12)), dx(), Proposal(Intervention.RETRY_SAME_RAIL))
    assert not d.allowed
    assert "would cost more than it returns" in clause(d, "global.value_floor").detail


def test_low_confidence_blocks_spending_but_not_escalation(engine):
    weak = dx(confidence=0.30)
    blocked = engine.evaluate(event(), weak, Proposal(Intervention.RETRY_SAME_RAIL))
    assert not blocked.allowed
    assert "global.confidence_floor" in blocked.citation

    escalate = engine.evaluate(event(), weak, Proposal(Intervention.ESCALATE_HUMAN))
    assert escalate.allowed


# --------------------------------------------------------------------- #
# timing                                                                #
# --------------------------------------------------------------------- #


def test_bank_outage_holds_the_retry_back(engine):
    ev, cause = event(), dx(cause=RootCause.BANK_DOWNTIME)
    too_soon = engine.evaluate(ev, cause, Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(minutes=10)))
    assert not too_soon.allowed
    assert "retry.bank_outage_hold" in too_soon.citation

    after_hold = engine.evaluate(ev, cause, Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(minutes=120)))
    assert after_hold.allowed


def test_insufficient_funds_waits_a_day_before_retrying(engine):
    ev, cause = event(), dx(cause=RootCause.INSUFFICIENT_FUNDS)
    immediate = engine.evaluate(ev, cause, Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(hours=1)))
    assert not immediate.allowed

    next_day = engine.evaluate(ev, cause, Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(hours=26)))
    assert next_day.allowed


# --------------------------------------------------------------------- #
# customer contact                                                      #
# --------------------------------------------------------------------- #


def test_contact_frequency_cap(engine):
    d = engine.evaluate(
        event(context={"contacts_last_7d": 2}), dx(), Proposal(Intervention.CUSTOMER_NUDGE, channel=Channel.SMS)
    )
    assert not d.allowed
    assert clause(d, "contact.frequency_cap").passed is False


def test_quiet_hours_defer_rather_than_drop(engine):
    at_2340 = datetime(2026, 8, 18, 23, 40, tzinfo=IST).astimezone(UTC)
    d = engine.evaluate(
        event(occurred_at=at_2340),
        dx(),
        Proposal(Intervention.CUSTOMER_NUDGE, channel=Channel.SMS),
    )
    assert not d.allowed
    assert d.substituted_with is not None

    sent_at = d.effective.scheduled_for.astimezone(IST)
    assert (sent_at.hour, sent_at.minute) == (9, 0)
    assert sent_at.date() == at_2340.astimezone(IST).date() + timedelta(days=1)
    assert d.effective.params["deferred_for"] == "quiet_hours"

    # ...and the deferred proposal now passes.
    assert engine.evaluate(event(occurred_at=at_2340), dx(), d.effective).allowed


def test_early_morning_defers_to_the_same_day(engine):
    at_0400 = datetime(2026, 8, 18, 4, 0, tzinfo=IST).astimezone(UTC)
    d = engine.evaluate(
        event(occurred_at=at_0400), dx(), Proposal(Intervention.CUSTOMER_NUDGE, channel=Channel.SMS)
    )
    sent_at = d.effective.scheduled_for.astimezone(IST)
    assert (sent_at.hour, sent_at.date()) == (9, at_0400.astimezone(IST).date())


def test_multiple_blocks_are_not_papered_over_by_substitution(engine):
    """Substitution only applies when quiet hours are the *sole* blocker."""
    at_2340 = datetime(2026, 8, 18, 23, 40, tzinfo=IST).astimezone(UTC)
    d = engine.evaluate(
        event(occurred_at=at_2340, context={"contacts_last_7d": 5}),
        dx(),
        Proposal(Intervention.CUSTOMER_NUDGE, channel=Channel.SMS),
    )
    assert not d.allowed
    assert d.substituted_with is None
    assert d.effective.intervention is Intervention.NO_ACTION


# --------------------------------------------------------------------- #
# receivables and degradation                                           #
# --------------------------------------------------------------------- #


def test_disputed_invoices_are_never_dunned(engine):
    ev = event(surface=Surface.RECEIVABLES, kind=RiskKind.INVOICE_OVERDUE, amount=Money.rupees(180000))
    d = engine.evaluate(
        ev, dx(cause=RootCause.INVOICE_DISPUTED), Proposal(Intervention.INVOICE_REMINDER, channel=Channel.EMAIL)
    )
    assert not d.allowed
    assert "support matter" in clause(d, "receivables.no_dunning_when_disputed").detail

    # A human may still be asked to look at it.
    assert engine.evaluate(ev, dx(cause=RootCause.INVOICE_DISPUTED), Proposal(Intervention.ESCALATE_HUMAN)).allowed


def test_degraded_diagnosis_can_only_reach_a_human(engine):
    degraded = Diagnosis(RootCause.UNKNOWN, 0.0, DiagnosisPath.DEGRADED, "model unavailable")
    for intervention in (Intervention.RETRY_SAME_RAIL, Intervention.CUSTOMER_NUDGE, Intervention.SEND_PAYMENT_LINK):
        d = engine.evaluate(event(), degraded, Proposal(intervention, channel=Channel.SMS))
        assert not d.allowed
        assert "safety.degraded_diagnosis" in d.citation

    assert engine.evaluate(event(), degraded, Proposal(Intervention.ESCALATE_HUMAN)).allowed


def test_every_decision_is_explainable(engine):
    """No decision may exist without a clause naming why it went that way."""
    cases = [
        (event(), dx(), Proposal(Intervention.RETRY_SAME_RAIL)),
        (event(context={"attempts_so_far": 9}), dx(), Proposal(Intervention.RETRY_SAME_RAIL)),
        (event(), dx(cause=RootCause.INVOICE_DISPUTED), Proposal(Intervention.INVOICE_REMINDER, channel=Channel.EMAIL)),
        (event(), dx(), Proposal(Intervention.NO_ACTION)),
    ]
    for ev, diagnosis, proposal in cases:
        d = engine.evaluate(ev, diagnosis, proposal)
        assert d.clauses
        assert d.citation and d.citation != "permitted by "
