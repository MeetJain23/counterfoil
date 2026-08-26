from datetime import UTC, datetime

import pytest

from counterfoil.domain.diagnosis import DiagnosisPath, RootCause
from counterfoil.domain.events import Customer, RiskEvent, RiskKind, Surface
from counterfoil.domain.money import Money
from counterfoil.kernel.diagnose import rules
from counterfoil.synth import BatchSpec, generate


def event(**signals):
    return RiskEvent(
        event_id="evt_1",
        surface=Surface.PAYMENTS,
        kind=RiskKind.PAYMENT_FAILED,
        occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        amount=Money.rupees(1200),
        customer=Customer("cus_1"),
        provider_signals=signals,
    )


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("card_expired", RootCause.EXPIRED_INSTRUMENT),
        ("insufficient_funds", RootCause.INSUFFICIENT_FUNDS),
        ("card_blocked", RootCause.ISSUER_DECLINE_HARD),
        ("bank_technical_error", RootCause.BANK_DOWNTIME),
        ("gateway_technical_error", RootCause.TECHNICAL_GATEWAY),
        ("payment_authentication_failed", RootCause.AUTHENTICATION_DROPOFF),
        ("payment_cancelled_by_customer", RootCause.CUSTOMER_ABANDONED),
        ("international_transaction_not_allowed", RootCause.INTERNATIONAL_BLOCKED),
    ],
)
def test_clear_codes_resolve(reason, expected):
    d = rules.diagnose(event(error_reason=reason))
    assert d is not None
    assert d.cause is expected
    assert d.path is DiagnosisPath.RULE
    assert d.confidence >= 0.78


def test_generic_reason_defers_instead_of_guessing():
    assert rules.diagnose(event(error_reason="payment_failed")) is None


def test_unknown_reason_defers_instead_of_guessing():
    assert rules.diagnose(event(error_reason="some_code_we_have_never_seen")) is None


def test_missing_reason_defers():
    assert rules.diagnose(event(error_code="BAD_REQUEST_ERROR")) is None


def test_source_corroborates_confidence():
    plain = rules.diagnose(event(error_reason="bank_technical_error"))
    corroborated = rules.diagnose(
        event(error_reason="bank_technical_error", error_source="bank")
    )
    assert corroborated.confidence > plain.confidence


def test_a_mismatched_source_lowers_confidence():
    from_customer = rules.diagnose(
        event(error_reason="payment_cancelled_by_customer", error_source="customer")
    )
    from_gateway = rules.diagnose(
        event(error_reason="payment_cancelled_by_customer", error_source="gateway")
    )
    assert from_gateway.confidence < from_customer.confidence


def test_diagnosis_carries_its_evidence():
    d = rules.diagnose(
        event(
            error_code="BAD_REQUEST_ERROR",
            error_reason="insufficient_funds",
            error_source="issuer",
            error_step="payment_authorization",
            method="upi",
        )
    )
    assert d.evidence["error_reason"] == "insufficient_funds"
    assert d.evidence["method"] == "upi"
    assert "insufficient_funds" in d.rationale


def test_reason_matching_is_case_and_whitespace_tolerant():
    d = rules.diagnose(event(error_reason="  Card_Expired  "))
    assert d is not None and d.cause is RootCause.EXPIRED_INSTRUMENT


# --------------------------------------------------------------------- #
# integrity: the rules must not be derived from the generator           #
# --------------------------------------------------------------------- #


def test_the_rules_module_does_not_import_the_generator():
    """Rules derived from the generator would agree with it by construction.

    Then "diagnosis accuracy" would measure nothing. The table must stand on
    the provider's published error taxonomy alone.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(rules.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
            imported.update(a.name for a in node.names)

    assert not any("synth" in name or "profiles" in name for name in imported), imported


# --------------------------------------------------------------------- #
# measured behaviour on a real batch                                    #
# --------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def batch():
    return generate(BatchSpec(size=600, seed=23))


def test_rules_close_the_large_majority_of_a_batch(batch):
    resolved = [c for c in batch if rules.diagnose(c.event) is not None]
    coverage = len(resolved) / len(batch)
    assert 0.74 < coverage < 0.92, f"rule coverage {coverage:.1%}"


def test_rules_defer_exactly_on_the_ambiguous_cases(batch):
    for case in batch:
        deferred = rules.diagnose(case.event) is None
        assert deferred == case.ambiguous, case.event.provider_signals


def test_rules_are_accurate_where_they_do_answer(batch):
    answered = [(c, rules.diagnose(c.event)) for c in batch]
    answered = [(c, d) for c, d in answered if d is not None]
    correct = sum(d.cause is c.true_cause for c, d in answered)
    accuracy = correct / len(answered)
    assert accuracy > 0.97, f"rule accuracy {accuracy:.1%}"
