"""Run a batch through all three arms and report what happened."""

from __future__ import annotations

from ..domain.outcome import Arm
from ..kernel.policy import PolicyEngine
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
    """Every arm sees the same cases in the same order, by construction."""
    engine = engine or PolicyEngine()
    cases = generate(spec)

    control = ArmResult(Arm.CONTROL)
    naive = ArmResult(Arm.NAIVE)
    agent = ArmResult(Arm.AGENT)

    for case in cases:
        control.add(run_control(case, engine=engine))
        naive.add(run_naive(case, engine=engine))
        agent.add(run_agent(case, engine=engine, diagnoser=diagnoser, ledger=ledger))

    return BatchReport(control=control, naive=naive, agent=agent)
