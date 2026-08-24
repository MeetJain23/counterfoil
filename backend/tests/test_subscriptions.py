"""The subscriptions surface.

The same kernel on a different calendar. What distinguishes it is that a
recurring debit may not lawfully be presented without prior notice, which makes
this the one surface where the compliant arm can lose on money and still be the
right answer. These tests pin that behaviour down rather than papering over it.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from counterfoil.domain.decision import Channel, Intervention, Proposal
from counterfoil.domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from counterfoil.domain.events import Customer, RiskEvent, RiskKind, Surface
from counterfoil.domain.money import Money
from counterfoil.eval import run_batch
from counterfoil.kernel.diagnose import rules
from counterfoil.kernel.policy import IST, PolicyEngine
from counterfoil.kernel.propose import next_proposal, playbook_for
from counterfoil.synth import BatchSpec, generate
from counterfoil.synth.profiles import SUBSCRIPTION_CAUSE_MIX

SPEC = BatchSpec(size=600, seed=2026, surface=Surface.SUBSCRIPTIONS, window_hours=48)
BASE = datetime(2026, 8, 18, 10, 0, tzinfo=IST).astimezone(timezone.utc)


@pytest.fixture(scope="module")
def cases():
    return generate(SPEC)


@pytest.fixture(scope="module")
def report():
    return run_batch(SPEC)


@pytest.fixture
def engine():
    return PolicyEngine()


def mandate_event(**over):
    fields = dict(
        event_id="evt_sub",
        surface=Surface.SUBSCRIPTIONS,
        kind=RiskKind.MANDATE_CHARGE_FAILED,
        occurred_at=BASE,
        amount=Money(49900),
        customer=Customer("cus_1", phone_last4="4412"),
        provider_signals={"error_reason": "mandate_insufficient_balance"},
        context={"consecutive_failures": 1},
    )
    fields.update(over)
    return RiskEvent(**fields)


def dx(cause=RootCause.MANDATE_BALANCE_LOW, confidence=0.93):
    return Diagnosis(cause, confidence, DiagnosisPath.RULE, "test")


# --------------------------------------------------------------------- #
# generation                                                            #
# --------------------------------------------------------------------- #


def test_the_batch_looks_like_a_subscription_book(cases):
    assert all(c.event.surface is Surface.SUBSCRIPTIONS for c in cases)
    assert all(c.event.kind is RiskKind.MANDATE_CHARGE_FAILED for c in cases)
    assert all(c.event.provider_signals["method"] == "emandate" for c in cases)
    # Plans are priced, not sampled: a handful of tiers, repeated.
    assert len({c.event.amount.paise for c in cases}) <= 8


def test_the_cause_mix_matches_the_declared_distribution(cases):
    seen = Counter(c.true_cause for c in cases)
    for cause, expected in SUBSCRIPTION_CAUSE_MIX.items():
        assert abs(seen[cause] / len(cases) - expected) < 0.05, cause


def test_a_mandate_charge_always_arrives_on_its_first_attempt(cases):
    """We present the debit; the customer does not retry it themselves."""
    assert all(c.event.attempts_so_far == 0 for c in cases)


def test_balance_cases_carry_a_payday_and_others_do_not(cases):
    balance = {RootCause.MANDATE_BALANCE_LOW, RootCause.INSUFFICIENT_FUNDS}
    for c in cases:
        if c.true_cause in balance:
            assert c.ripens_after_hours is not None
            assert 18 <= c.ripens_after_hours <= 500
        else:
            assert c.ripens_after_hours is None


def test_paydays_vary_across_the_book(cases):
    """The point of the surface: customers are funded on different dates."""
    paydays = [c.ripens_after_hours for c in cases if c.ripens_after_hours]
    assert max(paydays) - min(paydays) > 200


# --------------------------------------------------------------------- #
# diagnosis and playbooks                                               #
# --------------------------------------------------------------------- #


def test_mandate_reason_codes_resolve_without_a_model():
    for reason, expected in (
        ("mandate_insufficient_balance", RootCause.MANDATE_BALANCE_LOW),
        ("mandate_revoked", RootCause.MANDATE_REVOKED),
    ):
        d = rules.diagnose(mandate_event(provider_signals={"error_reason": reason}))
        assert d is not None and d.cause is expected


def test_the_same_cause_gets_a_different_plan_on_each_surface():
    """Insufficient funds on a checkout is not insufficient funds on a mandate."""
    checkout = RiskEvent(
        event_id="e", surface=Surface.PAYMENTS, kind=RiskKind.PAYMENT_FAILED,
        occurred_at=BASE, amount=Money(49900), customer=Customer("c"),
    )
    diagnosis = dx(RootCause.INSUFFICIENT_FUNDS)
    on_checkout = playbook_for(checkout, diagnosis)
    on_mandate = playbook_for(mandate_event(), diagnosis)

    assert on_checkout != on_mandate
    assert on_checkout[0].intervention is Intervention.RETRY_SAME_RAIL
    assert on_mandate[0].intervention is Intervention.PRE_DEBIT_NOTICE
    # And the mandate plan waits days where the checkout plan waits hours.
    assert on_mandate[1].after_hours > on_checkout[0].after_hours * 2


def test_every_mandate_plan_that_retries_sends_notice_first():
    for cause in (
        RootCause.MANDATE_BALANCE_LOW,
        RootCause.INSUFFICIENT_FUNDS,
        RootCause.BANK_DOWNTIME,
        RootCause.TECHNICAL_GATEWAY,
    ):
        steps = playbook_for(mandate_event(), dx(cause))
        retries = [i for i, s in enumerate(steps) if s.intervention is Intervention.RETRY_SAME_RAIL]
        notices = [i for i, s in enumerate(steps) if s.intervention is Intervention.PRE_DEBIT_NOTICE]
        assert notices, cause
        assert min(notices) < min(retries), cause


def test_a_revoked_mandate_is_never_presented_again():
    steps = playbook_for(mandate_event(), dx(RootCause.MANDATE_REVOKED))
    assert Intervention.RETRY_SAME_RAIL not in {s.intervention for s in steps}
    assert steps[0].intervention is Intervention.MANDATE_REAUTH


# --------------------------------------------------------------------- #
# the pre-debit notice clause                                           #
# --------------------------------------------------------------------- #


def test_presenting_without_notice_is_blocked(engine):
    d = engine.evaluate(
        mandate_event(), dx(),
        Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(hours=74)),
    )
    assert not d.allowed
    assert "subscriptions.pre_debit_notice" in d.citation


def test_presenting_too_soon_after_notice_is_blocked(engine):
    ev = mandate_event(context={"pre_debit_notice_at": BASE, "consecutive_failures": 1})
    d = engine.evaluate(
        ev, dx(), Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(hours=6))
    )
    assert not d.allowed
    assert "subscriptions.pre_debit_notice" in d.citation


def test_presenting_after_proper_notice_is_permitted(engine):
    ev = mandate_event(context={"pre_debit_notice_at": BASE, "consecutive_failures": 1})
    d = engine.evaluate(
        ev, dx(), Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(hours=26))
    )
    assert d.allowed


def test_the_notice_clause_does_not_apply_to_one_off_payments(engine):
    checkout = RiskEvent(
        event_id="e", surface=Surface.PAYMENTS, kind=RiskKind.PAYMENT_FAILED,
        occurred_at=BASE, amount=Money(49900), customer=Customer("c"),
    )
    d = engine.evaluate(
        checkout, dx(RootCause.TECHNICAL_GATEWAY),
        Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(hours=1)),
    )
    assert all(c.clause_id != "subscriptions.pre_debit_notice" for c in d.clauses)


def test_sending_the_notice_itself_needs_no_prior_notice(engine):
    d = engine.evaluate(
        mandate_event(), dx(),
        Proposal(Intervention.PRE_DEBIT_NOTICE, scheduled_for=BASE + timedelta(hours=2),
                 channel=Channel.SMS),
    )
    assert d.allowed


def test_repeated_failed_cycles_stop_the_agent(engine):
    ev = mandate_event(context={"consecutive_failures": 3, "pre_debit_notice_at": BASE})
    d = engine.evaluate(
        ev, dx(), Proposal(Intervention.RETRY_SAME_RAIL, scheduled_for=BASE + timedelta(hours=26))
    )
    assert not d.allowed
    assert "subscriptions.consecutive_failure_stop" in d.citation


def test_a_small_plan_that_keeps_failing_stops_rather_than_reaching_a_person(engine):
    """Stopping is the whole answer for a plan too small to be worth an analyst.

    A person costs about Rs 800 of loaded time. Handing them a Rs 499
    subscription loses money on the handoff before they do anything, so the
    correct end state is no action at all rather than a queued review.
    """
    ev = mandate_event(context={"consecutive_failures": 3})
    d = engine.evaluate(ev, dx(), Proposal(Intervention.ESCALATE_HUMAN))
    assert not d.allowed
    assert "escalation.min_value" in d.citation


def test_a_plan_worth_a_persons_time_does_reach_one(engine):
    ev = mandate_event(amount=Money(599900), context={"consecutive_failures": 3})
    assert engine.evaluate(ev, dx(), Proposal(Intervention.ESCALATE_HUMAN)).allowed


# --------------------------------------------------------------------- #
# the measured result, including the part where we lose                 #
# --------------------------------------------------------------------- #


def test_the_agent_commits_no_breaches_and_the_naive_arm_commits_many(report):
    assert report.agent.total_violations == 0
    assert report.naive.violations["subscriptions.pre_debit_notice"] > 500


def test_the_agent_sends_the_notices_and_the_naive_arm_sends_none(report):
    assert report.agent.mandatory_notices > 0
    assert report.naive.mandatory_notices == 0


def test_mandatory_notices_are_excluded_from_discretionary_contact_counts(report):
    """Otherwise obeying the rule makes the compliant arm look like the spammer."""
    assert report.agent.contacts > report.agent.discretionary_contacts
    assert (
        report.agent.discretionary_contacts
        == report.agent.contacts - report.agent.mandatory_notices
    )


def test_compliance_costs_money_on_this_surface(report):
    """The honest finding, asserted so nobody can quietly tune it away.

    The naive arm recovers more here. It buys that with thousands of regulatory
    breaches, and if a future change makes the compliant arm win outright, the
    likely explanation is that the notice requirement stopped being enforced
    rather than that the agent got cleverer.
    """
    assert report.incremental_paise(report.naive) > report.incremental_paise(report.agent)
    assert report.naive.total_violations > 1000


def test_the_agent_still_does_far_less_work(report):
    assert report.agent.actions < report.naive.actions
