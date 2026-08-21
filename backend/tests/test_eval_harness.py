"""Tests for the thing that produces the headline number.

If the eval is wrong, everything downstream of it is decoration, so these
lean hard on the properties the claims actually rest on: that arms are
comparable, that attribution is conservative, and that the agent's advantage
is not an artefact of how the world was written.
"""

import pytest

from counterfoil.domain.decision import Intervention
from counterfoil.domain.diagnosis import DiagnosisPath, RootCause
from counterfoil.domain.outcome import Arm, OutcomeState
from counterfoil.eval import run_batch
from counterfoil.eval.arms import run_agent, run_control, run_naive
from counterfoil.eval.sensitivity import run_across_seeds, run_sensitivity
from counterfoil.kernel.policy import PolicyEngine
from counterfoil.ledger import Ledger
from counterfoil.synth import BatchSpec, generate


@pytest.fixture(scope="module")
def report():
    return run_batch(BatchSpec(size=600, seed=2026))


# --------------------------------------------------------------------- #
# the arms are actually comparable                                      #
# --------------------------------------------------------------------- #


def test_every_arm_sees_every_case(report):
    assert report.control.n == report.naive.n == report.agent.n == 600
    ids = {r.event_id for r in report.control.per_case}
    assert {r.event_id for r in report.naive.per_case} == ids
    assert {r.event_id for r in report.agent.per_case} == ids


def test_the_counterfactual_is_identical_across_arms(report):
    by_id = {r.event_id: r for r in report.control.per_case}
    for r in report.agent.per_case:
        assert r.would_have_recovered_anyway == by_id[r.event_id].would_have_recovered_anyway


def test_control_does_nothing_and_costs_nothing(report):
    assert report.control.actions == 0
    assert report.control.contacts == 0
    assert report.control.direct_cost.paise == 0
    assert report.control.n_attributable == 0
    assert report.control.gross.paise > 0      # but money still arrives


def test_the_run_is_reproducible():
    a = run_batch(BatchSpec(size=200, seed=5))
    b = run_batch(BatchSpec(size=200, seed=5))
    assert a.agent.gross_recovered_paise == b.agent.gross_recovered_paise
    assert a.agent.contacts == b.agent.contacts
    assert a.naive.total_violations == b.naive.total_violations


# --------------------------------------------------------------------- #
# attribution stays conservative                                        #
# --------------------------------------------------------------------- #


def test_attributable_recovery_never_exceeds_gross(report):
    for arm in (report.naive, report.agent):
        assert arm.attributable_paise <= arm.gross_recovered_paise
        assert arm.n_attributable <= arm.n_recovered


def test_self_served_recoveries_are_never_claimed(report):
    for arm in (report.naive, report.agent):
        for r in arm.per_case:
            if r.would_have_recovered_anyway:
                assert not r.attributable


def test_an_arm_cannot_be_credited_without_evidence(report):
    for arm in (report.control, report.naive, report.agent):
        for r in arm.per_case:
            if r.outcome.state is OutcomeState.RECOVERED:
                assert r.outcome.evidence


def test_incremental_is_measured_against_control(report):
    assert report.incremental_paise(report.agent) == (
        report.agent.gross_recovered_paise - report.control.gross_recovered_paise
    )
    assert report.incremental_paise(report.control) == 0


# --------------------------------------------------------------------- #
# governance is enforced for the agent and merely observed for naive    #
# --------------------------------------------------------------------- #


def test_the_agent_never_violates_policy(report):
    assert report.agent.total_violations == 0
    assert not report.agent.violations


def test_the_naive_arm_violates_policy_a_lot(report):
    assert report.naive.total_violations > 500
    assert "retry.not_terminal_cause" in report.naive.violations
    assert "contact.quiet_hours" in report.naive.violations


def test_the_agent_refuses_and_records_why(report):
    assert report.agent.total_violations == 0
    assert sum(report.agent.refusals.values()) > 0


def test_the_agent_never_retries_a_dead_instrument(report):
    terminal = {
        RootCause.EXPIRED_INSTRUMENT,
        RootCause.ISSUER_DECLINE_HARD,
        RootCause.INTERNATIONAL_BLOCKED,
    }
    for r in report.agent.per_case:
        if r.diagnosis and r.diagnosis.cause in terminal:
            assert Intervention.RETRY_SAME_RAIL not in {a.intervention for a in r.actions}


def test_undiagnosed_cases_only_ever_reach_a_human(report):
    for r in report.agent.per_case:
        if r.diagnosis and r.diagnosis.path is DiagnosisPath.DEGRADED:
            taken = {a.intervention for a in r.actions}
            assert taken <= {Intervention.ESCALATE_HUMAN}


def test_the_agent_does_less_work_than_the_naive_arm(report):
    assert report.agent.actions < report.naive.actions


# --------------------------------------------------------------------- #
# the break-even machinery                                              #
# --------------------------------------------------------------------- #


def test_net_falls_as_contact_cost_rises(report):
    nets = [report.net_at(report.agent, c) for c in (0, 500, 1000, 5000)]
    assert nets == sorted(nets, reverse=True)


def test_sweep_covers_the_range_and_is_monotone(report):
    sweep = report.sweep(max_contact_cost_paise=5000, points=11)
    assert sweep[0][0] == 0
    assert sweep[-1][0] <= 5000
    assert [s[1] for s in sweep] == sorted([s[1] for s in sweep], reverse=True)


def test_break_even_is_zero_when_the_agent_already_wins(report):
    """The agent wins before goodwill is priced at all, so no churn number is
    needed to support the headline claim."""
    assert report.net_at(report.agent, 0) > report.net_at(report.naive, 0)
    assert report.break_even_contact_cost_paise() == 0.0


def test_bootstrap_interval_brackets_the_point_estimate(report):
    lo, hi = report.bootstrap_incremental(report.agent, iterations=400)
    assert lo < report.incremental_paise(report.agent) < hi
    assert lo > 0          # the interval excludes zero: the effect is real


# --------------------------------------------------------------------- #
# the result is not an artefact of how the world was written            #
# --------------------------------------------------------------------- #


def test_the_win_survives_removing_the_intervention_bonus():
    results = {v.variant.key: v for v in run_sensitivity(BatchSpec(size=400, seed=2026))}
    assert results["baseline"].agent_wins
    assert results["no_intervention_fit"].agent_wins


def test_the_win_is_a_timing_win_and_we_say_so():
    """The honest finding: flatten the timing curve and the agent loses.

    This test exists to fail loudly if someone later 'fixes' the world so that
    the agent wins under every variant. That would mean the handicap stopped
    being a real handicap.
    """
    results = {v.variant.key: v for v in run_sensitivity(BatchSpec(size=400, seed=2026))}
    assert not results["timing_barely_matters"].agent_wins
    assert not results["both"].agent_wins


def test_the_win_holds_across_seeds():
    for seed, agent, naive in run_across_seeds(300, (7, 11, 23, 99)):
        assert agent > naive, f"seed {seed}"


def test_handicaps_do_not_leak_into_later_runs():
    before = run_batch(BatchSpec(size=150, seed=42)).agent.gross_recovered_paise
    run_sensitivity(BatchSpec(size=150, seed=42))
    after = run_batch(BatchSpec(size=150, seed=42)).agent.gross_recovered_paise
    assert before == after


# --------------------------------------------------------------------- #
# the audit trail                                                       #
# --------------------------------------------------------------------- #


def test_a_run_writes_a_verifiable_ledger(tmp_path):
    engine = PolicyEngine()
    ledger = Ledger(tmp_path / "audit.jsonl", run_id="run_test")
    cases = generate(BatchSpec(size=25, seed=3))
    for case in cases:
        run_agent(case, engine=engine, ledger=ledger)

    assert ledger.verify() is None
    entries = list(ledger.entries())
    assert len(entries) > len(cases) * 2

    stages = {e.stage for e in entries}
    assert {"detected", "diagnosed", "observed"} <= stages


def test_every_executed_action_has_a_recorded_decision(tmp_path):
    engine = PolicyEngine()
    ledger = Ledger(tmp_path / "audit.jsonl", run_id="run_test")
    for case in generate(BatchSpec(size=40, seed=8)):
        run_agent(case, engine=engine, ledger=ledger)

    decided = sum(1 for e in ledger.entries() if e.stage == "decided")
    executed = sum(1 for e in ledger.entries() if e.stage == "executed")
    assert decided == executed
    assert executed > 0


def test_every_decision_entry_cites_its_clauses(tmp_path):
    engine = PolicyEngine()
    ledger = Ledger(tmp_path / "audit.jsonl", run_id="run_test")
    for case in generate(BatchSpec(size=30, seed=12)):
        run_agent(case, engine=engine, ledger=ledger)

    for entry in ledger.entries():
        if entry.stage == "decided":
            assert entry.payload["clauses"]
            assert entry.payload["citation"]


def test_control_and_naive_can_share_a_ledger_without_confusion(tmp_path):
    engine = PolicyEngine()
    ledger = Ledger(tmp_path / "audit.jsonl", run_id="run_test")
    case = generate(BatchSpec(size=1, seed=1))[0]
    run_control(case, engine=engine, ledger=ledger)
    run_naive(case, engine=engine, ledger=ledger)
    run_agent(case, engine=engine, ledger=ledger)

    assert ledger.verify() is None
    arms = {e.arm for e in ledger.entries()}
    assert arms == {"control", "naive", "agent"}
