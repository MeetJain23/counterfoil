"""The synthetic world is the measuring instrument, so it gets calibrated.

If the generator is biased or the world model is incoherent, every number the
eval reports downstream is decoration. These tests check the properties the
eval actually depends on.
"""

from collections import Counter
from datetime import timedelta

import pytest

from counterfoil.domain.decision import Channel, Intervention
from counterfoil.domain.diagnosis import RootCause
from counterfoil.domain.events import Surface
from counterfoil.domain.outcome import Arm, OutcomeState
from counterfoil.synth import BatchSpec, generate, resolve, spontaneous_probability
from counterfoil.synth.profiles import PAYMENT_CAUSE_MIX
from counterfoil.synth.world import TakenAction


@pytest.fixture(scope="module")
def batch():
    return generate(BatchSpec(size=800, seed=11))


def act(case, intervention, after_hours=0.0, channel=Channel.NONE):
    return TakenAction(intervention, case.event.occurred_at + timedelta(hours=after_hours), channel)


# --------------------------------------------------------------------- #
# reproducibility                                                       #
# --------------------------------------------------------------------- #


def test_same_seed_produces_an_identical_batch():
    a = generate(BatchSpec(size=120, seed=3))
    b = generate(BatchSpec(size=120, seed=3))
    assert [c.event_id for c in a] == [c.event_id for c in b]
    assert [c.true_cause for c in a] == [c.true_cause for c in b]
    assert [c.u_spontaneous for c in a] == [c.u_spontaneous for c in b]
    assert [c.draws for c in a] == [c.draws for c in b]


def test_different_seeds_produce_different_batches():
    a = generate(BatchSpec(size=120, seed=3))
    b = generate(BatchSpec(size=120, seed=4))
    assert [c.true_cause for c in a] != [c.true_cause for c in b]


def test_unimplemented_surfaces_fail_loudly():
    with pytest.raises(NotImplementedError):
        generate(BatchSpec(size=5, seed=1, surface=Surface.RECEIVABLES))


# --------------------------------------------------------------------- #
# the batch resembles what it claims to model                           #
# --------------------------------------------------------------------- #


def test_cause_mix_matches_the_declared_distribution(batch):
    seen = Counter(c.true_cause for c in batch)
    for cause, expected in PAYMENT_CAUSE_MIX.items():
        assert abs(seen[cause] / len(batch) - expected) < 0.04, cause


def test_amounts_are_plausible_and_never_negative(batch):
    paise = [c.event.amount.paise for c in batch]
    assert min(paise) > 0
    assert all(p % 100 == 0 for p in paise)
    median = sorted(paise)[len(paise) // 2]
    assert 20_000 < median < 500_000          # Rs 200 - Rs 5,000
    assert max(paise) > median * 8            # a real right tail exists


def test_the_visible_event_never_carries_the_label(batch):
    for case in batch:
        assert not hasattr(case.event, "true_cause")
        assert "true_cause" not in case.event.context
        assert "true_cause" not in case.event.provider_signals


def test_a_meaningful_minority_is_ambiguous(batch):
    share = sum(c.ambiguous for c in batch) / len(batch)
    assert 0.10 < share < 0.26


def test_ambiguous_cases_are_genuinely_unresolvable_from_codes(batch):
    """If the cause were readable off the codes, the LLM path would be theatre."""
    ambiguous = [c for c in batch if c.ambiguous]
    assert ambiguous
    for case in ambiguous:
        signals = case.event.provider_signals
        assert signals["error_reason"] == "payment_failed"
        assert signals["error_description"]
        codes = f"{signals['error_code']} {signals['error_reason']} {signals['error_source']}".lower()
        assert case.true_cause.value not in codes


# --------------------------------------------------------------------- #
# the world model behaves the way the profiles claim                    #
# --------------------------------------------------------------------- #


def test_doing_nothing_still_recovers_some_money(batch):
    """The control arm must be non-trivial, or every uplift number is a lie."""
    recovered = [resolve(c, [], Arm.CONTROL) for c in batch]
    rate = sum(r.outcome.state is OutcomeState.RECOVERED for r in recovered) / len(batch)
    assert 0.10 < rate < 0.35
    assert all(not r.attributable for r in recovered)
    assert all(r.outcome.intervention_cost.paise == 0 for r in recovered)


def test_retrying_a_terminal_cause_never_works(batch):
    terminal = [c for c in batch if c.true_cause in {
        RootCause.EXPIRED_INSTRUMENT,
        RootCause.ISSUER_DECLINE_HARD,
        RootCause.INTERNATIONAL_BLOCKED,
    }]
    assert terminal
    for case in terminal:
        r = resolve(case, [act(case, Intervention.RETRY_SAME_RAIL, 2)], Arm.AGENT)
        assert r.closed_by is not Intervention.RETRY_SAME_RAIL


def test_waiting_makes_an_insufficient_funds_retry_work_better(batch):
    cases = [c for c in batch if c.true_cause is RootCause.INSUFFICIENT_FUNDS]
    assert len(cases) > 50

    def rate(hours):
        hits = [
            resolve(c, [act(c, Intervention.RETRY_SAME_RAIL, hours)], Arm.AGENT).closed_by
            for c in cases
        ]
        return sum(h is Intervention.RETRY_SAME_RAIL for h in hits) / len(cases)

    assert rate(30) > rate(1) * 2


def test_waiting_out_a_bank_outage_beats_retrying_into_it(batch):
    cases = [c for c in batch if c.true_cause is RootCause.BANK_DOWNTIME]
    assert len(cases) > 30

    def rate(hours):
        hits = [
            resolve(c, [act(c, Intervention.RETRY_SAME_RAIL, hours)], Arm.AGENT).closed_by
            for c in cases
        ]
        return sum(h is Intervention.RETRY_SAME_RAIL for h in hits) / len(cases)

    assert rate(3) > rate(0.05)


def test_the_right_intervention_beats_the_wrong_one(batch):
    """An expired card needs a new card, not another attempt at the old one."""
    cases = [c for c in batch if c.true_cause is RootCause.EXPIRED_INSTRUMENT]
    assert len(cases) > 20

    retried = sum(
        resolve(c, [act(c, Intervention.RETRY_SAME_RAIL, 4)], Arm.AGENT).attributable
        for c in cases
    )
    asked = sum(
        resolve(c, [act(c, Intervention.REQUEST_UPDATED_INSTRUMENT, 4, Channel.SMS)], Arm.AGENT).attributable
        for c in cases
    )
    assert retried == 0          # nothing on a dead card ever works
    assert asked > 0


def test_contact_fatigue_makes_the_third_message_weaker(batch):
    cases = [c for c in batch if c.true_cause is RootCause.AUTHENTICATION_DROPOFF]
    assert len(cases) > 50

    # Three nudges in a row: count how often each position is the one that lands.
    landed_at = Counter()
    for case in cases:
        actions = [act(case, Intervention.CUSTOMER_NUDGE, 2 + i, Channel.SMS) for i in range(3)]
        r = resolve(case, actions, Arm.AGENT)
        if r.closed_by is not None:
            landed_at[len(r.notes) - 1] += 1
    assert landed_at[0] > landed_at[2]


# --------------------------------------------------------------------- #
# attribution: the part everything downstream depends on                #
# --------------------------------------------------------------------- #


def test_recovery_that_would_have_happened_anyway_is_not_attributed(batch):
    would_anyway = [c for c in batch if c.u_spontaneous < spontaneous_probability(c)]
    assert would_anyway
    for case in would_anyway[:60]:
        r = resolve(case, [act(case, Intervention.RETRY_SAME_RAIL, 26)], Arm.AGENT)
        assert r.outcome.state is OutcomeState.RECOVERED
        assert r.would_have_recovered_anyway
        assert not r.attributable


def test_chasing_a_customer_who_would_have_paid_is_recorded_as_waste(batch):
    would_anyway = [c for c in batch if c.u_spontaneous < spontaneous_probability(c)]
    case = would_anyway[0]
    r = resolve(case, [act(case, Intervention.CUSTOMER_NUDGE, 3, Channel.SMS)], Arm.AGENT)
    assert r.wasted_spend.paise > 0
    assert r.wasted_spend == r.outcome.intervention_cost


def test_cost_accrues_for_every_action_even_when_nothing_works(batch):
    stubborn = [c for c in batch if c.true_cause is RootCause.ISSUER_DECLINE_HARD][0]
    actions = [act(stubborn, Intervention.RETRY_SAME_RAIL, h) for h in (1, 2, 3)]
    r = resolve(stubborn, actions, Arm.NAIVE)
    assert r.outcome.intervention_cost.paise == 750
    assert r.outcome.state is OutcomeState.STILL_AT_RISK


def test_no_actions_means_not_attempted(batch):
    r = resolve([c for c in batch if not (c.u_spontaneous < spontaneous_probability(c))][0], [], Arm.CONTROL)
    assert r.outcome.state is OutcomeState.NOT_ATTEMPTED


def test_an_absurd_action_count_is_rejected(batch):
    case = batch[0]
    with pytest.raises(ValueError, match="pre-drawn"):
        resolve(case, [act(case, Intervention.CUSTOMER_NUDGE, i) for i in range(20)], Arm.NAIVE)


def test_arms_see_the_same_world(batch):
    """The point of pre-drawn randomness: differences are the intervention."""
    for case in batch[:80]:
        control = resolve(case, [], Arm.CONTROL)
        agent = resolve(case, [act(case, Intervention.RETRY_SAME_RAIL, 26)], Arm.AGENT)
        assert control.would_have_recovered_anyway == agent.would_have_recovered_anyway
