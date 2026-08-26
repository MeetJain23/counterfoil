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
from datetime import UTC, datetime, timedelta

from ..domain.diagnosis import RootCause
from ..domain.events import Customer, RiskEvent, RiskKind, Surface
from ..domain.money import Money
from . import profiles
from .profiles import AMBIGUOUS_RATE, SEGMENTS

#: How many sequential recovery attempts a case has pre-drawn randomness for.
#: The policy engine caps real runs well below this.
MAX_DRAWS = 12

EMAIL_DOMAINS = ("gmail.com", "outlook.com", "yahoo.in", "rediffmail.com", "proton.me")

#: B2B counterparties are companies, not consumers.
BUSINESS_DOMAINS = (
    "acmelogistics.in", "northbridge.co.in", "vertexretail.com",
    "sundarammills.in", "clearpathtech.io", "meridianfoods.in",
)


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
    #: When money actually arrives in this customer's account, in hours from
    #: the failure. Set on subscriptions, where a failed debit is not a moment
    #: but a position in a month: the charge lands on the 28th and the salary
    #: on the 1st. Retrying before this is close to worthless no matter how
    #: many attempts you spend, and retrying just after it is close to free.
    ripens_after_hours: float | None = None

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
    starts_at: datetime = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)


def _weighted_choice(rng: random.Random, weights: dict) -> object:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _amount_paise(rng: random.Random) -> int:
    """Indian consumer transaction sizes: mostly small, with a long right tail."""
    rupees = min(250_000, max(29, int(rng.lognormvariate(6.9, 1.15))))
    return rupees * 100


def _signals(
    rng: random.Random, cause: RootCause, ambiguous: bool, surface: Surface
) -> tuple[dict[str, str], bool]:
    """Build the provider payload the diagnoser is allowed to read."""
    vague = profiles.SURFACE_AMBIGUOUS[surface]
    if ambiguous and cause in vague:
        return {
            "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "payment_failed",
            "error_source": "gateway",
            "error_step": "payment_authorization",
            "error_description": rng.choice(vague[cause]),
        }, True

    template = profiles.SURFACE_SIGNALS[surface][cause]
    return {
        "error_code": template.error_code,
        "error_reason": template.error_reason,
        "error_source": template.error_source,
        "error_step": template.error_step,
        "error_description": rng.choice(template.descriptions),
    }, False


def _method(rng: random.Random) -> str:
    return _weighted_choice(rng, {"upi": 0.46, "card": 0.31, "netbanking": 0.15, "wallet": 0.08})


#: Recurring plans are priced, not sampled: a subscription book is a handful of
#: tiers repeated thousands of times, not a lognormal spread.
PLAN_PRICES_PAISE: dict[int, float] = {
    9900: 0.24,      # Rs 99
    19900: 0.21,     # Rs 199
    49900: 0.19,     # Rs 499
    99900: 0.16,     # Rs 999
    149900: 0.11,    # Rs 1,499
    299900: 0.06,    # Rs 2,999
    599900: 0.03,    # Rs 5,999
}

#: Balance-driven causes ripen when money arrives, not after a fixed backoff.
BALANCE_CAUSES = frozenset({RootCause.MANDATE_BALANCE_LOW, RootCause.INSUFFICIENT_FUNDS})

#: B2B invoice sizes. Two orders of magnitude above consumer payments, which is
#: why the value floor never bites here and the cost of a wrong move does.
def _invoice_paise(rng: random.Random) -> int:
    rupees = min(2_500_000, max(4_000, int(rng.lognormvariate(11.6, 1.05))))
    return rupees * 100


def _thread(rng: random.Random, cause: RootCause) -> tuple[str, bool]:
    """What the buyer wrote back, and whether it is trying to manipulate us."""
    if rng.random() < profiles.ADVERSARIAL_RATE:
        return rng.choice(profiles.ADVERSARIAL_THREADS), True
    return rng.choice(profiles.RECEIVABLES_THREADS[cause]), False


def _hours_until_payday(rng: random.Random) -> float:
    """How long until this customer's account is funded again.

    Indian salary credits cluster in the first few days of the month, and
    mandate debits are commonly set for month end. Most of these customers are
    therefore days rather than hours from being able to pay, and a small tail
    is much longer: contract workers, delayed payroll, a genuinely empty
    account.
    """
    if rng.random() < 0.82:
        return rng.uniform(18, 120)      # under a week: the ordinary case
    return rng.uniform(120, 500)         # up to three weeks


def generate(spec: BatchSpec) -> list[LatentCase]:
    """Deterministic for a given (size, seed, surface). Same inputs, same batch."""
    if spec.surface not in profiles.SURFACE_MIX:
        raise NotImplementedError(
            f"{spec.surface.value} generation lands with its surface adapter"
        )

    rng = random.Random(spec.seed)
    mix = profiles.SURFACE_MIX[spec.surface]
    subscriptions = spec.surface is Surface.SUBSCRIPTIONS
    receivables = spec.surface is Surface.RECEIVABLES
    cases: list[LatentCase] = []

    if receivables:
        return _generate_receivables(spec, rng, mix)

    for i in range(spec.size):
        cause: RootCause = _weighted_choice(rng, mix)  # type: ignore[assignment]
        segment = rng.choices(
            [s.name for s in SEGMENTS], weights=[s.weight for s in SEGMENTS], k=1
        )[0]

        ambiguous_roll = rng.random() < AMBIGUOUS_RATE
        signals, ambiguous = _signals(rng, cause, ambiguous_roll, spec.surface)
        signals["method"] = "emandate" if subscriptions else _method(rng)

        occurred_at = spec.starts_at + timedelta(
            seconds=rng.randrange(spec.window_hours * 3600)
        )

        # Draw order below is load-bearing and must not be rearranged:
        # amount, then customer, then context. Every published figure is keyed
        # to a seed, so reordering these calls silently produces a different
        # batch for the same seed and quietly invalidates every number in the
        # README. Adding the subscriptions surface did exactly that once
        # already; see FAILURES.md 007.
        amount = Money(
            _weighted_choice(rng, PLAN_PRICES_PAISE)  # type: ignore[arg-type]
            if subscriptions
            else _amount_paise(rng)
        )

        customer = Customer(
            ref=f"cus_{rng.randrange(10**9):09d}",
            phone_last4=f"{rng.randrange(10000):04d}",
            email_domain=rng.choice(EMAIL_DOMAINS),
            segment=segment,
        )

        if subscriptions:
            kind = RiskKind.MANDATE_CHARGE_FAILED
            context = {
                # A mandate charge is presented by us, not attempted by the
                # customer, so it always arrives on its first attempt.
                "attempts_so_far": 0,
                "contacts_last_7d": _weighted_choice(rng, {0: 0.93, 1: 0.06, 2: 0.01}),
                "actions_taken": 0,
                "cycle_number": rng.randrange(1, 30),
                # A mandate that has paid twenty times and just failed is a
                # different proposition from one failing on its second cycle.
                "consecutive_failures": _weighted_choice(rng, {1: 0.79, 2: 0.16, 3: 0.05}),
            }
            ripens = _hours_until_payday(rng) if cause in BALANCE_CAUSES else None
        else:
            kind = RiskKind.PAYMENT_FAILED
            context = {
                # A minority of failures are already a second or third try by
                # the customer before we ever see them.
                "attempts_so_far": _weighted_choice(rng, {0: 0.78, 1: 0.16, 2: 0.06}),
                "contacts_last_7d": _weighted_choice(rng, {0: 0.88, 1: 0.09, 2: 0.03}),
                "actions_taken": 0,
            }
            ripens = None

        event = RiskEvent(
            event_id=f"evt_{spec.seed}_{i:05d}",
            surface=spec.surface,
            kind=kind,
            occurred_at=occurred_at,
            amount=amount,
            customer=customer,
            provider_signals=signals,
            context=context,
        )

        cases.append(
            LatentCase(
                event=event,
                true_cause=cause,
                segment=segment,
                u_spontaneous=rng.random(),
                draws=tuple(rng.random() for _ in range(MAX_DRAWS)),
                ambiguous=ambiguous,
                ripens_after_hours=ripens,
            )
        )

    return cases


def _generate_receivables(spec: BatchSpec, rng: random.Random, mix: dict) -> list[LatentCase]:
    """Overdue B2B invoices, where the signal is correspondence rather than codes.

    Kept as its own function rather than a third branch in the main loop: the
    field it produces barely overlap with the transactional surfaces, and the
    main loop's draw order is load-bearing for the payments figures. A separate
    stream cannot disturb it.
    """
    cases: list[LatentCase] = []

    for i in range(spec.size):
        cause: RootCause = _weighted_choice(rng, mix)  # type: ignore[assignment]
        segment = rng.choices(
            [s.name for s in SEGMENTS], weights=[s.weight for s in SEGMENTS], k=1
        )[0]

        amount = Money(_invoice_paise(rng))
        thread, adversarial = _thread(rng, cause)
        days_overdue = rng.randrange(1, 95)
        occurred_at = spec.starts_at + timedelta(seconds=rng.randrange(spec.window_hours * 3600))

        # A stated payment date is the one piece of information that should
        # stop an agent chasing, so it is modelled explicitly: the money
        # arrives then, and contact before then achieves close to nothing.
        promised_in_hours = rng.uniform(24, 340) if cause is RootCause.PROMISE_TO_PAY else None

        event = RiskEvent(
            event_id=f"evt_{spec.seed}_{i:05d}",
            surface=Surface.RECEIVABLES,
            kind=RiskKind.INVOICE_OVERDUE,
            occurred_at=occurred_at,
            amount=amount,
            customer=Customer(
                ref=f"acct_{rng.randrange(10**6):06d}",
                email_domain=rng.choice(BUSINESS_DOMAINS),
                segment=segment,
            ),
            provider_signals={
                # No error_reason anywhere: there is nothing for a rule table
                # to match on, which is the point of this surface.
                "invoice_number": f"INV-{spec.seed}-{i:05d}",
                "days_overdue": str(days_overdue),
                "payment_terms": rng.choice(("NET15", "NET30", "NET45", "NET60")),
                "thread": thread,
            },
            context={
                "attempts_so_far": 0,
                "contacts_last_7d": _weighted_choice(rng, {0: 0.71, 1: 0.22, 2: 0.07}),
                "actions_taken": 0,
                "days_overdue": days_overdue,
                "adversarial_thread": adversarial,
            },
        )

        cases.append(
            LatentCase(
                event=event,
                true_cause=cause,
                segment=segment,
                u_spontaneous=rng.random(),
                draws=tuple(rng.random() for _ in range(MAX_DRAWS)),
                # Every thread is ambiguous to a rule table by construction.
                ambiguous=True,
                ripens_after_hours=promised_in_hours,
            )
        )

    return cases


def truth_table(cases: list[LatentCase]) -> dict[str, RootCause]:
    """Held-back labels, for scoring diagnosis accuracy after a run."""
    return {c.event_id: c.true_cause for c in cases}
