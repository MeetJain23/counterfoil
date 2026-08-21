"""Scoring a batch, without asserting the number that decides the answer.

The awkward fact about revenue recovery is that the interesting comparison
depends on a figure nobody can measure: what it costs you when a customer is
messaged and did not want to be. Set that to zero and the correct strategy is
to message everyone constantly. Set it high and restraint wins by definition.

So this module never picks a value. It reports the agent's net across the whole
range and solves for the **break-even contact cost**: the point at which the
careful agent overtakes the aggressive one. A reader can then decide whether a
message costs their business more or less than that, which is a question they
can actually answer about their own customers.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field

from ..domain.money import Money
from ..domain.outcome import Arm, OutcomeState
from ..kernel.runner import CaseResult


@dataclass
class ArmResult:
    arm: Arm
    n: int = 0
    gross_recovered_paise: int = 0
    n_recovered: int = 0
    #: Recoveries the arm actually caused, as opposed to ones it happened to be
    #: standing next to.
    n_attributable: int = 0
    attributable_paise: int = 0
    #: Money spent that turned out to be chasing someone already on their way.
    wasted_paise: int = 0
    direct_cost_paise: int = 0
    contacts: int = 0
    actions: int = 0
    llm_calls: int = 0
    violations: Counter = field(default_factory=Counter)
    refusals: Counter = field(default_factory=Counter)
    per_case: list[CaseResult] = field(default_factory=list)

    def add(self, r: CaseResult) -> None:
        self.n += 1
        self.per_case.append(r)
        self.actions += len(r.actions)
        self.contacts += r.contacts
        self.direct_cost_paise += r.direct_cost.paise
        self.llm_calls += r.llm_calls
        self.violations.update(r.violations)
        self.refusals.update(r.refusals)
        if r.outcome and r.outcome.state is OutcomeState.RECOVERED:
            self.n_recovered += 1
            self.gross_recovered_paise += r.recovered.paise
            if r.attributable:
                self.n_attributable += 1
                self.attributable_paise += r.recovered.paise
        if r.would_have_recovered_anyway:
            self.wasted_paise += r.direct_cost.paise

    # --- money -------------------------------------------------------- #

    @property
    def gross(self) -> Money:
        return Money(self.gross_recovered_paise)

    @property
    def direct_cost(self) -> Money:
        return Money(self.direct_cost_paise)

    @property
    def wasted(self) -> Money:
        return Money(self.wasted_paise)

    @property
    def contacts_per_recovery(self) -> float:
        return self.contacts / self.n_attributable if self.n_attributable else float("inf")

    @property
    def total_violations(self) -> int:
        return sum(self.violations.values())


@dataclass
class BatchReport:
    control: ArmResult
    naive: ArmResult
    agent: ArmResult

    def incremental_paise(self, arm: ArmResult) -> int:
        """Recovery above what doing nothing would have produced."""
        return arm.gross_recovered_paise - self.control.gross_recovered_paise

    def net_at(self, arm: ArmResult, contact_cost_paise: int) -> int:
        """Net value at an assumed goodwill cost per customer contact."""
        return (
            self.incremental_paise(arm)
            - arm.direct_cost_paise
            - arm.contacts * contact_cost_paise
        )

    def sweep(
        self, max_contact_cost_paise: int = 5000, points: int = 26
    ) -> list[tuple[int, int, int]]:
        """(contact_cost, agent_net, naive_net) across the plausible range."""
        step = max_contact_cost_paise // (points - 1)
        return [
            (c, self.net_at(self.agent, c), self.net_at(self.naive, c))
            for c in range(0, max_contact_cost_paise + 1, max(step, 1))
        ]

    def break_even_contact_cost_paise(self) -> float | None:
        """What one unwanted message must cost before restraint pays.

        Below this the aggressive arm nets more; above it the agent does. The
        whole argument for Counterfoil is that this number lands well below
        what a message actually costs a real merchant, and stating it this way
        means a reader can check that claim against their own churn data
        instead of taking ours.
        """
        contact_gap = self.naive.contacts - self.agent.contacts
        if contact_gap <= 0:
            return None
        value_gap = (
            self.net_at(self.naive, 0) - self.net_at(self.agent, 0)
        )
        if value_gap <= 0:
            return 0.0          # the agent already wins with contacts free
        return value_gap / contact_gap

    # --- uncertainty --------------------------------------------------- #

    def bootstrap_incremental(
        self, arm: ArmResult, *, iterations: int = 2000, seed: int = 17
    ) -> tuple[int, int]:
        """95% CI on incremental recovery, resampling cases in pairs.

        Paired because both arms saw the same cases: resampling them
        independently would inflate the interval with variance that the common
        random numbers already removed.
        """
        rng = random.Random(seed)
        by_id = {r.event_id: r for r in self.control.per_case}
        deltas = [
            r.recovered.paise - by_id[r.event_id].recovered.paise for r in arm.per_case
        ]
        n = len(deltas)
        if n == 0:
            return (0, 0)

        totals = []
        for _ in range(iterations):
            totals.append(sum(deltas[rng.randrange(n)] for _ in range(n)))
        totals.sort()
        return (totals[int(0.025 * iterations)], totals[int(0.975 * iterations)])
