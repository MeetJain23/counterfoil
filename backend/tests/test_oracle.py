"""Tests for the perfect-diagnosis ceiling.

The oracle exists to make "was the model worth adding" answerable in rupees
rather than in accuracy percentages. That only works if the oracle is genuinely
a ceiling and genuinely still bound by policy, so both are asserted here.
"""

import pytest

from counterfoil.domain.diagnosis import DiagnosisPath, RootCause
from counterfoil.eval import OracleDiagnoser, measure_contribution, run_batch
from counterfoil.eval.oracle import Contribution
from counterfoil.kernel.diagnose.llm import LLMDiagnoser
from counterfoil.llm import Budget, FixtureStore
from counterfoil.synth import BatchSpec, generate

SPEC = BatchSpec(size=300, seed=2026)


@pytest.fixture(scope="module")
def cases():
    return generate(SPEC)


@pytest.fixture(scope="module")
def oracle(cases):
    return OracleDiagnoser.from_cases(cases)


@pytest.fixture(scope="module")
def model_diagnoser():
    return LLMDiagnoser(
        client=None,
        fixtures=FixtureStore("llm_fixtures", mode="replay"),
        budget=Budget(cap_usd=0.0),
    )


# --------------------------------------------------------------------- #
# the oracle is actually an oracle                                      #
# --------------------------------------------------------------------- #


def test_the_oracle_is_never_wrong(cases, oracle):
    for case in cases:
        assert oracle(case.event).cause is case.true_cause


def test_the_oracle_withholds_on_an_event_it_has_no_label_for(cases, oracle):
    stranger = generate(BatchSpec(size=1, seed=999))[0]
    result = oracle(stranger.event)
    assert result.path is DiagnosisPath.DEGRADED
    assert result.cause is RootCause.UNKNOWN


def test_the_oracle_is_not_exempt_from_the_confidence_floor(oracle, cases):
    """Confidence of exactly 1.0 appears nowhere else, and a clause comparing
    against it would treat the oracle arm differently by accident."""
    assert oracle(cases[0].event).confidence < 1.0


def test_the_oracle_still_obeys_policy(cases, oracle):
    report = run_batch(SPEC, diagnoser=oracle)
    assert report.agent.total_violations == 0
    assert sum(report.agent.refusals.values()) > 0


def test_the_oracle_never_retries_a_dead_instrument(cases, oracle):
    from counterfoil.domain.decision import Intervention

    report = run_batch(SPEC, diagnoser=oracle)
    terminal = {
        RootCause.EXPIRED_INSTRUMENT,
        RootCause.ISSUER_DECLINE_HARD,
        RootCause.INTERNATIONAL_BLOCKED,
    }
    for r in report.agent.per_case:
        if r.diagnosis and r.diagnosis.cause in terminal:
            assert Intervention.RETRY_SAME_RAIL not in {a.intervention for a in r.actions}


# --------------------------------------------------------------------- #
# the ceiling behaves like a ceiling                                    #
# --------------------------------------------------------------------- #


def test_perfect_diagnosis_beats_rules_alone(model_diagnoser):
    c = measure_contribution(SPEC, diagnoser=model_diagnoser)
    assert c.oracle_paise > c.rules_only_paise


def test_the_model_lands_between_rules_and_perfect(model_diagnoser):
    """The claim the README makes, asserted rather than asserted at."""
    c = measure_contribution(SPEC, diagnoser=model_diagnoser)
    assert c.rules_only_paise <= c.with_model_paise <= c.oracle_paise


def test_the_model_captures_a_real_share_of_the_headroom(model_diagnoser):
    c = measure_contribution(SPEC, diagnoser=model_diagnoser)
    assert c.model_adds_paise > 0
    assert 0.0 < c.gap_closed <= 1.0


def test_what_is_left_on_the_table_is_reported(model_diagnoser):
    c = measure_contribution(SPEC, diagnoser=model_diagnoser)
    assert c.left_on_the_table_paise == c.oracle_paise - c.with_model_paise
    assert c.left_on_the_table_paise >= 0


# --------------------------------------------------------------------- #
# the arithmetic                                                        #
# --------------------------------------------------------------------- #


def test_gap_closed_is_zero_when_there_is_no_headroom():
    c = Contribution(rules_only_paise=100, with_model_paise=100, oracle_paise=100)
    assert c.gap_closed == 0.0
    assert c.headroom_paise == 0


def test_gap_closed_is_one_when_the_model_matches_the_oracle():
    c = Contribution(rules_only_paise=100, with_model_paise=200, oracle_paise=200)
    assert c.gap_closed == 1.0
    assert c.left_on_the_table_paise == 0


def test_a_model_that_helps_nothing_scores_zero():
    c = Contribution(rules_only_paise=100, with_model_paise=100, oracle_paise=300)
    assert c.gap_closed == 0.0
    assert c.model_adds_paise == 0


def test_the_oracle_is_an_evaluation_instrument_not_a_product_component():
    """Nothing in the kernel may import it: it reads labels no real system has."""
    import ast
    from pathlib import Path

    kernel = Path(__file__).resolve().parents[1] / "counterfoil" / "kernel"
    for path in kernel.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            assert not any("oracle" in n.lower() for n in names), path
