"""Reproducible synthetic batches, with the ground truth held back.

A ``LatentCase`` pairs the ``RiskEvent`` the system is allowed to see with the
truth it is not: the actual root cause, the customer's disposition, and the
random draws that decide what happens next.

Those draws are fixed at generation time rather than sampled during a run. That
makes every arm face *the same* world: when the control arm and the agent arm
disagree about an event, the difference is the intervention and nothing else.
Sampling fresh randomness per arm would mean measuring the agent against a
different universe than the one the control arm saw, and would need far larger
batches to say anything at all.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..domain.diagnosis import RootCause
from ..domain.events import Customer, RiskEvent, RiskKind, Surface
from ..domain.money import Money
from . import profiles
from .profiles import AMBIGUOUS_DESCRIPTIONS, AMBIGUOUS_RATE, CLEAR_SIGNALS, SEGMENTS

#: How many sequential recovery attempts a case has pre-drawn randomness for.
#: The policy engine caps real runs well below this.
MAX_DRAWS = 12

EMAIL_DOMAINS = ("gmail.com", "outlook.com", "yahoo.in", "rediffmail.com", "proton.me")


@dataclass(frozen=True)
class LatentCase:
    """One at-risk item: what the system sees, plus what is actually true."""

    event: RiskEvent
    true_cause: RootCause
    segment: str
    #: Decides whether the money arrives with no intervention at all.
    u_spontaneous: float
    #: Consumed positionally, one per attempt, so arms stay comparable.
    draws: tuple[float, ...]
    #: True when the provider gave a generic code and prose instead of a
    #: resolvable reason. These are the cases a rule table cannot close.
    ambiguous: bool

    @property
    def event_id(self) -> str:
        return self.event.event_id


@dataclass(frozen=True)
class BatchSpec:
    size: int
    seed: int
    surface: Surface = Surface.PAYMENTS
    #: Batch covers failures arriving across this many hours.
    window_hours: int = 72
    starts_at: datetime = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)


def _weighted_choice(rng: random.Random, weights: dict) -> object:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _amount_paise(rng: random.Random) -> int:
    """Indian consumer transaction sizes: mostly small, with a long right tail."""
    rupees = min(250_000, max(29, int(rng.lognormvariate(6.9, 1.15))))
    return rupees * 100


def _signals(rng: random.Random, cause: RootCause, ambiguous: bool) -> tuple[dict[str, str], bool]:
    """Build the provider payload the diagnoser is allowed to read."""
    if ambiguous and cause in AMBIGUOUS_DESCRIPTIONS:
        return {
            "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "payment_failed",
            "error_source": "gateway",
            "error_step": "payment_authorization",
            "error_description": rng.choice(AMBIGUOUS_DESCRIPTIONS[cause]),
        }, True

    template = CLEAR_SIGNALS[cause]
    return {
        "error_code": template.error_code,
        "error_reason": template.error_reason,
        "error_source": template.error_source,
        "error_step": template.error_step,
        "error_description": rng.choice(template.descriptions),
    }, False


def _method(rng: random.Random) -> str:
    return _weighted_choice(rng, {"upi": 0.46, "card": 0.31, "netbanking": 0.15, "wallet": 0.08})


def generate(spec: BatchSpec) -> list[LatentCase]:
    """Deterministic for a given (size, seed, surface). Same inputs, same batch."""
    if spec.surface is not Surface.PAYMENTS:
        raise NotImplementedError(
            f"{spec.surface.value} generation lands with its surface adapter"
        )

    rng = random.Random(spec.seed)
    mix = profiles.SURFACE_MIX[spec.surface]
    cases: list[LatentCase] = []

    for i in range(spec.size):
        cause: RootCause = _weighted_choice(rng, mix)  # type: ignore[assignment]
        segment = rng.choices(
            [s.name for s in SEGMENTS], weights=[s.weight for s in SEGMENTS], k=1
        )[0]

        ambiguous_roll = rng.random() < AMBIGUOUS_RATE
        signals, ambiguous = _signals(rng, cause, ambiguous_roll)
        signals["method"] = _method(rng)

        occurred_at = spec.starts_at + timedelta(
            seconds=rng.randrange(spec.window_hours * 3600)
        )

        event = RiskEvent(
            event_id=f"evt_{spec.seed}_{i:05d}",
            surface=spec.surface,
            kind=RiskKind.PAYMENT_FAILED,
            occurred_at=occurred_at,
            amount=Money(_amount_paise(rng)),
            customer=Customer(
                ref=f"cus_{rng.randrange(10**9):09d}",
                phone_last4=f"{rng.randrange(10000):04d}",
                email_domain=rng.choice(EMAIL_DOMAINS),
                segment=segment,
            ),
            provider_signals=signals,
            context={
                # A minority of failures are already a second or third try by
                # the customer before we ever see them.
                "attempts_so_far": _weighted_choice(rng, {0: 0.78, 1: 0.16, 2: 0.06}),
                "contacts_last_7d": _weighted_choice(rng, {0: 0.88, 1: 0.09, 2: 0.03}),
                "actions_taken": 0,
            },
        )

        cases.append(
            LatentCase(
                event=event,
                true_cause=cause,
                segment=segment,
                u_spontaneous=rng.random(),
                draws=tuple(rng.random() for _ in range(MAX_DRAWS)),
                ambiguous=ambiguous,
            )
        )

    return cases


def truth_table(cases: list[LatentCase]) -> dict[str, RootCause]:
    """Held-back labels, for scoring diagnosis accuracy after a run."""
    return {c.event_id: c.true_cause for c in cases}
