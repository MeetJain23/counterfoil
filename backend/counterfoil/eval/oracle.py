"""A diagnoser that cannot be wrong, for measuring what being wrong costs.

This is an evaluation instrument and never part of the product. It reads the
generator's held-back labels directly, which no real system can do, and exists
only to establish the ceiling: what the agent would recover if diagnosis were
perfect.

That ceiling is what makes the model's contribution measurable in money rather
than in accuracy percentages. Three points define the line:

    rules only      what the provider's own error codes can close
    rules + model   what reading the prose adds
    oracle          what perfect diagnosis would have achieved

The interesting figure is not the model's accuracy. It is the share of the gap
between the first and third points that the second one closes, because that is
the only version of "was the model worth adding" a merchant would care about.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from ..domain.events import RiskEvent
from ..synth.generator import LatentCase


@dataclass
class OracleDiagnoser:
    """Answers from the generator's labels. Only valid on synthetic batches."""

    truth: dict[str, RootCause]
    #: The promised payment date, in days, for the cases that have one. Perfect
    #: diagnosis of "they said they would pay" includes knowing when they said.
    #: Omitting it made the oracle lose to the model on receivables, because the
    #: model reads the date out of the buyer's reply and the oracle was left
    #: chasing from the failure date. A ceiling that a real system can exceed is
    #: not a ceiling; see FAILURES.md 009.
    promised_days: dict[str, int] = field(default_factory=dict)
    #: Not 1.0. Perfect knowledge still has to clear the same policy clauses as
    #: everything else, and a confidence of exactly 1.0 nowhere else in the
    #: system would make the oracle arm quietly exempt from the confidence floor.
    confidence: float = 0.99

    @classmethod
    def from_cases(cls, cases: list[LatentCase]) -> OracleDiagnoser:
        return cls(
            truth={c.event_id: c.true_cause for c in cases},
            promised_days={
                c.event_id: max(0, round(c.ripens_after_hours / 24))
                for c in cases
                if c.true_cause is RootCause.PROMISE_TO_PAY and c.ripens_after_hours
            },
        )

    def __call__(self, event: RiskEvent) -> Diagnosis:
        cause = self.truth.get(event.event_id)
        if cause is None:
            return Diagnosis(
                RootCause.UNKNOWN,
                0.0,
                DiagnosisPath.DEGRADED,
                "oracle has no label for this event",
            )
        evidence = {"path": "oracle"}
        promised = self.promised_days.get(event.event_id)
        if promised is not None:
            evidence["promised_within_days"] = str(promised)

        return Diagnosis(
            cause=cause,
            confidence=self.confidence,
            path=DiagnosisPath.LLM,
            rationale="oracle: the generator's held-back label",
            evidence=evidence,
        )


@dataclass(frozen=True)
class Contribution:
    """What each diagnosis layer is worth, in rupees."""

    rules_only_paise: int
    with_model_paise: int
    oracle_paise: int

    @property
    def model_adds_paise(self) -> int:
        return self.with_model_paise - self.rules_only_paise

    @property
    def headroom_paise(self) -> int:
        """What perfect diagnosis would add over rules alone."""
        return self.oracle_paise - self.rules_only_paise

    @property
    def gap_closed(self) -> float:
        """The share of achievable headroom the model actually captured."""
        if self.headroom_paise <= 0:
            return 0.0
        return self.model_adds_paise / self.headroom_paise

    @property
    def left_on_the_table_paise(self) -> int:
        return self.oracle_paise - self.with_model_paise


def measure_contribution(spec, *, diagnoser) -> Contribution:
    """Run the same batch under three diagnosis regimes."""
    from ..synth.generator import generate
    from .harness import run_batch

    cases = generate(spec)

    rules_only = run_batch(spec)
    with_model = run_batch(spec, diagnoser=diagnoser)
    oracle = run_batch(spec, diagnoser=OracleDiagnoser.from_cases(cases))

    return Contribution(
        rules_only_paise=rules_only.incremental_paise(rules_only.agent),
        with_model_paise=with_model.incremental_paise(with_model.agent),
        oracle_paise=oracle.incremental_paise(oracle.agent),
    )
