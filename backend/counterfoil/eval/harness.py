"""Run a batch through all three arms and report what happened."""

from __future__ import annotations

from ..domain.outcome import Arm
from ..kernel.policy import PolicyEngine
from ..kernel.policy.engine import Capacity
from ..ledger import Ledger
from ..synth.generator import BatchSpec, generate
from .arms import run_agent, run_control, run_naive
from .metrics import ArmResult, BatchReport


def run_batch(
    spec: BatchSpec,
    *,
    engine: PolicyEngine | None = None,
    diagnoser=None,
    ledger: Ledger | None = None,
) -> BatchReport:
    """Every arm sees the same cases in the same order, by construction.

    Each arm gets its own human-review budget, sized from the batch. Sharing one
    across arms would let whichever ran first starve the others, which would
    measure running order rather than policy.
    """
    base = engine or PolicyEngine()
    cases = generate(spec)

    control = ArmResult(Arm.CONTROL)
    naive = ArmResult(Arm.NAIVE)
    agent = ArmResult(Arm.AGENT)

    for arm_result, runner in (
        (control, run_control),
        (naive, run_naive),
        (agent, run_agent),
    ):
        arm_engine = PolicyEngine(config=base.cfg, capacity=_capacity_for(base.cfg, spec))
        for case in cases:
            if runner is run_agent:
                arm_result.add(
                    run_agent(case, engine=arm_engine, diagnoser=diagnoser, ledger=ledger)
                )
            else:
                arm_result.add(runner(case, engine=arm_engine))

    return BatchReport(control=control, naive=naive, agent=agent)


def _capacity_for(cfg: dict, spec: BatchSpec) -> Capacity | None:
    """Human reviews available for a batch this size, or None where unlimited."""
    per_hundred = cfg.get(spec.surface.value, {}).get("escalations_per_100_invoices")
    if per_hundred is None:
        return None
    return Capacity(limit=max(1, round(spec.size * float(per_hundred) / 100)))
