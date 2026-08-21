"""Which assumption is the result actually resting on?

A single favourable number proves very little when the world it was measured in
was written by the same person as the agent. This module attacks the result on
purpose: it re-runs the batch with the agent's advantages deliberately removed
and reports whether the conclusion survives.

Two of the handicaps below are aimed squarely at the ways this eval could be
rigged:

``no_intervention_fit``
    The world grants a bonus when an intervention suits the cause, and the
    playbooks were written to collect it. Both are the author's assertions, so
    the honest question is whether the agent still wins without the bonus.

``timing_barely_matters``
    The agent's whole thesis is that *when* you act dominates *what* you do.
    Flattening the ripeness curve tests that directly, and it is the one
    handicap the agent does not survive. Saying so is the point.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from .. import synth
from ..synth import world
from ..synth.generator import BatchSpec
from .harness import run_batch


@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    note: str


VARIANTS: tuple[Variant, ...] = (
    Variant("baseline", "baseline", "the world as modelled in profiles.py"),
    Variant(
        "no_intervention_fit",
        "no right-intervention bonus",
        "removes the reward for matching the intervention to the cause",
    ),
    Variant(
        "timing_barely_matters",
        "timing barely matters",
        "acting immediately becomes 70% as good as acting at the right moment",
    ),
    Variant(
        "both",
        "both handicaps at once",
        "the agent keeps only its stopping rules and its refusals",
    ),
)


@contextmanager
def _handicap(key: str):
    saved_fit = world.INTERVENTION_FIT
    saved_ripeness = world._ripeness

    def flat(ripens_after_hours: float, delay_hours: float) -> float:
        if ripens_after_hours <= 0:
            return 1.0
        return 0.70 + 0.30 * min(1.0, max(0.0, delay_hours) / ripens_after_hours)

    try:
        if key in {"no_intervention_fit", "both"}:
            world.INTERVENTION_FIT = {}
        if key in {"timing_barely_matters", "both"}:
            world._ripeness = flat
        yield
    finally:
        world.INTERVENTION_FIT = saved_fit
        world._ripeness = saved_ripeness


@dataclass(frozen=True)
class VariantResult:
    variant: Variant
    agent_incremental_paise: int
    naive_incremental_paise: int
    agent_contacts: int
    naive_contacts: int
    agent_violations: int
    naive_violations: int

    @property
    def delta_paise(self) -> int:
        return self.agent_incremental_paise - self.naive_incremental_paise

    @property
    def agent_wins(self) -> bool:
        return self.delta_paise > 0


def run_sensitivity(spec: BatchSpec) -> list[VariantResult]:
    results = []
    for variant in VARIANTS:
        with _handicap(variant.key):
            report = run_batch(spec)
            results.append(
                VariantResult(
                    variant=variant,
                    agent_incremental_paise=report.incremental_paise(report.agent),
                    naive_incremental_paise=report.incremental_paise(report.naive),
                    agent_contacts=report.agent.contacts,
                    naive_contacts=report.naive.contacts,
                    agent_violations=report.agent.total_violations,
                    naive_violations=report.naive.total_violations,
                )
            )
    return results


def run_across_seeds(
    size: int, seeds: tuple[int, ...]
) -> list[tuple[int, int, int]]:
    """(seed, agent_incremental, naive_incremental). Guards against seed luck."""
    out = []
    for seed in seeds:
        report = run_batch(BatchSpec(size=size, seed=seed))
        out.append(
            (
                seed,
                report.incremental_paise(report.agent),
                report.incremental_paise(report.naive),
            )
        )
    return out
