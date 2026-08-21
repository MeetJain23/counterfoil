"""A hard ceiling on model spend, enforced in code rather than in discipline.

Counterfoil runs its eval hundreds of times during development. A loop that
accidentally calls the model once per event instead of once per *distinct*
event is an easy mistake and an expensive one, and it does not announce itself:
the run simply takes longer and the bill arrives later.

So spend is metered per call and the meter refuses once the cap is reached.
Refusal degrades diagnosis rather than raising, because a batch that stops
halfway leaves an audit trail full of half-processed cases, whereas a batch
that finishes with the tail marked ``degraded`` is honest and still readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: USD per million tokens, Claude Haiku 4.5. Cache reads bill at roughly a
#: tenth of the input rate; cache writes at 1.25x for the 5 minute TTL.
INPUT_PER_MTOK = 1.00
OUTPUT_PER_MTOK = 5.00
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def price(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    return (
        input_tokens * INPUT_PER_MTOK
        + cache_read_tokens * INPUT_PER_MTOK * CACHE_READ_MULTIPLIER
        + cache_write_tokens * INPUT_PER_MTOK * CACHE_WRITE_MULTIPLIER
        + output_tokens * OUTPUT_PER_MTOK
    ) / 1_000_000


class BudgetExhausted(RuntimeError):
    """Raised only when a caller explicitly asks for strict enforcement."""


@dataclass
class Budget:
    cap_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    #: Calls that never reached the API because the answer was already known.
    saved_calls: int = 0
    _history: list[float] = field(default_factory=list)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.cap_usd

    def can_afford(self, estimate_usd: float = 0.0) -> bool:
        return (self.spent_usd + estimate_usd) < self.cap_usd

    def charge(self, usd: float) -> None:
        self.spent_usd += usd
        self.calls += 1
        self._history.append(usd)

    def note_cache_hit(self) -> None:
        self.saved_calls += 1

    def require(self, estimate_usd: float = 0.0) -> None:
        if not self.can_afford(estimate_usd):
            raise BudgetExhausted(
                f"spend cap of ${self.cap_usd:.2f} reached (${self.spent_usd:.4f} used "
                f"over {self.calls} calls)"
            )

    @property
    def mean_call_usd(self) -> float:
        return sum(self._history) / len(self._history) if self._history else 0.0

    def summary(self) -> str:
        hit_rate = (
            self.saved_calls / (self.saved_calls + self.calls)
            if (self.saved_calls + self.calls)
            else 0.0
        )
        return (
            f"{self.calls} model calls, {self.saved_calls} served from cache "
            f"({hit_rate:.0%} hit rate), ${self.spent_usd:.4f} of ${self.cap_usd:.2f} spent"
        )
