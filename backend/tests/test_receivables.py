"""The receivables surface, and the inversion it exists to demonstrate.

A failed card payment arrives with a machine-readable reason code, so rules
close 83% of that surface for free and the model is a useful supplement. An
overdue invoice arrives with an email thread, so rules close none of it and the
model is the product. Same kernel, same policy engine, same ledger; only the
share of decisions a model touches moves, and it moves from a sixth to all.

The other thing this surface forces is triage. Human review is the one resource
that does not scale, and once it is budgeted, "escalate everything" stops being
an answer and diagnosis starts being worth something.
"""

from collections import Counter
from datetime import timedelta

import pytest

from counterfoil.domain.decision import Channel, Intervention, Proposal
from counterfoil.domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from counterfoil.domain.events import Surface
from counterfoil.eval import OracleDiagnoser, run_batch
from counterfoil.kernel.diagnose import rules
from counterfoil.kernel.diagnose.llm import (
    RECEIVABLE_CAUSES,
    build_system_prompt,
    build_user_prompt,
    case_fingerprint,
    schema_for,
)
from counterfoil.kernel.policy import PolicyEngine
from counterfoil.kernel.policy.engine import Capacity
from counterfoil.kernel.propose import playbook_for
from counterfoil.synth import BatchSpec, generate

SPEC = BatchSpec(size=400, seed=2026, surface=Surface.RECEIVABLES)


@pytest.fixture(scope="module")
def cases():
    return generate(SPEC)


@pytest.fixture(scope="module")
def report(cases):
    return run_batch(SPEC, diagnoser=OracleDiagnoser.from_cases(cases))


@pytest.fixture
def engine():
    return PolicyEngine()


def invoice(cases, cause):
    return next(c for c in cases if c.true_cause is cause)


def dx(cause, confidence=0.9, **evidence):
    return Diagnosis(cause, confidence, DiagnosisPath.LLM, "test", evidence=evidence)


# --------------------------------------------------------------------- #
# the inversion                                                         #
# --------------------------------------------------------------------- #


def test_rules_resolve_nothing_at_all_here(cases):
    """The whole point: there are no error codes to match on."""
    assert all(rules.diagnose(c.event) is None for c in cases)


def test_an_invoice_carries_prose_rather_than_codes(cases):
    for case in cases:
        signals = case.event.provider_signals
        assert "thread" in signals
        assert "error_reason" not in signals
        assert "error_code" not in signals


def test_the_model_is_offered_only_causes_an_invoice_can_have():
    enum = schema_for(Surface.RECEIVABLES)["properties"]["cause"]["enum"]
    assert set(enum) == {c.value for c in RECEIVABLE_CAUSES}
    assert "card_expired" not in enum
    assert "bank_downtime" not in enum


def test_the_prompt_asks_for_a_payment_date_only_here():
    assert "promised_within_days" in schema_for(Surface.RECEIVABLES)["properties"]
    assert "promised_within_days" not in schema_for(Surface.PAYMENTS)["properties"]
    assert "promised_within_days" in build_system_prompt(Surface.RECEIVABLES)
    assert "promised_within_days" not in build_system_prompt(Surface.PAYMENTS)


def test_identical_threads_are_one_question_regardless_of_age(cases):
    """Days overdue is context for the model and noise in a cache key.

    Keying on it turned 23 real questions into 4,085 paid ones.
    """
    by_thread = {}
    for case in cases:
        by_thread.setdefault(case.event.provider_signals["thread"], set()).add(
            case_fingerprint(case.event)
        )
    assert all(len(keys) == 1 for keys in by_thread.values())
    assert len({k for keys in by_thread.values() for k in keys}) < 30


def test_days_overdue_still_reaches_the_model(cases):
    assert "days_overdue" in build_user_prompt(cases[0].event)


# --------------------------------------------------------------------- #
# honouring a stated payment date                                       #
# --------------------------------------------------------------------- #


def test_chasing_before_the_promised_date_is_blocked(engine, cases):
    case = invoice(cases, RootCause.PROMISE_TO_PAY)
    d = engine.evaluate(
        case.event,
        dx(RootCause.PROMISE_TO_PAY, promised_within_days="10"),
        Proposal(
            Intervention.INVOICE_REMINDER,
            scheduled_for=case.event.occurred_at + timedelta(days=3),
            channel=Channel.EMAIL,
        ),
    )
    assert not d.allowed
    assert "receivables.honour_promise_to_pay" in d.citation
    assert "too early" in next(
        c.detail for c in d.clauses if c.clause_id == "receivables.honour_promise_to_pay"
    )


def test_following_up_after_the_promised_date_is_permitted(engine, cases):
    case = invoice(cases, RootCause.PROMISE_TO_PAY)
    d = engine.evaluate(
        case.event,
        dx(RootCause.PROMISE_TO_PAY, promised_within_days="10"),
        Proposal(
            Intervention.INVOICE_REMINDER,
            scheduled_for=case.event.occurred_at + timedelta(days=12),
            channel=Channel.EMAIL,
        ),
    )
    assert d.allowed


def test_no_promise_means_the_clause_does_not_apply(engine, cases):
    case = invoice(cases, RootCause.INVOICE_CASHFLOW_DELAY)
    d = engine.evaluate(
        case.event,
        dx(RootCause.INVOICE_CASHFLOW_DELAY),
        Proposal(
            Intervention.INVOICE_REMINDER,
            scheduled_for=case.event.occurred_at + timedelta(days=2),
            channel=Channel.EMAIL,
        ),
    )
    assert all(c.clause_id != "receivables.honour_promise_to_pay" for c in d.clauses)


def test_the_playbook_anchors_follow_up_to_the_promised_date(cases):
    from counterfoil.kernel.propose import next_proposal

    case = invoice(cases, RootCause.PROMISE_TO_PAY)
    proposal = next_proposal(
        case.event, dx(RootCause.PROMISE_TO_PAY, promised_within_days="10"), 0
    )
    gap = (proposal.scheduled_for - case.event.occurred_at).total_seconds() / 86400
    assert 10 < gap < 12, "follow-up should land just after the date they gave"


def test_a_disputed_invoice_goes_to_a_person_and_is_never_chased(engine, cases):
    case = invoice(cases, RootCause.INVOICE_DISPUTED)
    steps = playbook_for(case.event, dx(RootCause.INVOICE_DISPUTED))
    assert [s.intervention for s in steps] == [Intervention.ESCALATE_HUMAN]

    chase = engine.evaluate(
        case.event,
        dx(RootCause.INVOICE_DISPUTED),
        Proposal(Intervention.INVOICE_REMINDER, channel=Channel.EMAIL),
    )
    assert not chase.allowed


# --------------------------------------------------------------------- #
# human attention is finite, so it has to be triaged                    #
# --------------------------------------------------------------------- #


def test_escalation_is_refused_once_the_team_is_out_of_hours(cases):
    engine = PolicyEngine(capacity=Capacity(limit=2))
    case = invoice(cases, RootCause.INVOICE_DISPUTED)
    proposal = Proposal(Intervention.ESCALATE_HUMAN)

    assert engine.evaluate(case.event, dx(RootCause.INVOICE_DISPUTED), proposal).allowed
    engine.capacity.consume()
    engine.capacity.consume()
    blocked = engine.evaluate(case.event, dx(RootCause.INVOICE_DISPUTED), proposal)
    assert not blocked.allowed
    assert "escalation.capacity" in blocked.citation


def test_a_small_invoice_is_not_worth_a_person(engine, cases):
    small = min(cases, key=lambda c: c.event.amount.paise)
    d = engine.evaluate(small.event, dx(RootCause.INVOICE_DISPUTED), Proposal(Intervention.ESCALATE_HUMAN))
    if small.event.amount.paise < 2_500_000:
        assert not d.allowed
        assert "escalation.min_value" in d.citation


def test_a_run_never_exceeds_its_review_budget(report):
    escalated = sum(
        1
        for r in report.agent.per_case
        if any(a.intervention is Intervention.ESCALATE_HUMAN for a in r.actions)
    )
    assert escalated <= round(SPEC.size * 8 / 100)


def test_each_arm_gets_its_own_budget(cases):
    """Sharing one would measure running order rather than policy."""
    report = run_batch(SPEC, diagnoser=OracleDiagnoser.from_cases(cases))
    for arm in (report.naive, report.agent):
        escalated = sum(
            1
            for r in arm.per_case
            if any(a.intervention is Intervention.ESCALATE_HUMAN for a in r.actions)
        )
        assert escalated <= round(SPEC.size * 8 / 100)


def test_what_a_human_achieves_depends_on_what_they_were_handed():
    """A flat escalation success rate makes escalating everything optimal."""
    from counterfoil.synth.profiles import RECEIVABLES_BEHAVIOUR

    rates = {c: b.escalation_success for c, b in RECEIVABLES_BEHAVIOUR.items()}
    assert len(set(rates.values())) > 1
    # A person can genuinely resolve a dispute; they cannot make a buyer who
    # already committed to Friday pay on Tuesday.
    assert rates[RootCause.INVOICE_DISPUTED] > rates[RootCause.PROMISE_TO_PAY]


# --------------------------------------------------------------------- #
# diagnosis is the product on this surface                              #
# --------------------------------------------------------------------- #


def test_diagnosis_is_worth_far_more_here_than_on_payments(cases):
    """The honest answer to "where did the model earn its place".

    Without diagnosis the agent spends its entire review budget on whichever
    invoices it happened to see first. With it, the same 48 reviews go to the
    cases that need them. Same budget, same batch, different outcome.
    """
    blind = run_batch(SPEC)
    seeing = run_batch(SPEC, diagnoser=OracleDiagnoser.from_cases(cases))
    assert seeing.net_at(seeing.agent, 0) > blind.net_at(blind.agent, 0) * 3


def test_without_diagnosis_the_agent_can_only_escalate(cases):
    blind = run_batch(SPEC)
    taken = Counter(
        a.intervention for r in blind.agent.per_case for a in r.actions
    )
    assert set(taken) <= {Intervention.ESCALATE_HUMAN}


def test_the_agent_commits_no_breaches_while_the_naive_arm_does(report):
    assert report.agent.total_violations == 0
    assert report.naive.total_violations > 1000


def test_adversarial_threads_are_present_and_labelled(cases):
    hostile = [c for c in cases if c.event.context["adversarial_thread"]]
    assert hostile, "the surface should exercise its own injection defence"
    for case in hostile:
        assert case.event.provider_signals["thread"]
