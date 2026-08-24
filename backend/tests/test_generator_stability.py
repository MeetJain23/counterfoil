"""The payments batch is frozen, and changing it has to be deliberate.

Every figure this project publishes is keyed to a seed. The generator draws
from one shared random stream, so inserting, removing or reordering a single
call silently produces a different batch for the same seed and quietly
invalidates every number in the README, with no test failing and no diff to
point at.

That happened once, adding the subscriptions surface (FAILURES.md 007). It was
caught only because two unrelated sensitivity tests happened to assert a
specific *loss*, and the loss disappeared. Relying on that again is not a plan.

So the batch is fingerprinted. If this test fails, either the change to the
generator was unintended, or it was intended and every published figure needs
regenerating along with the constant below. Both are fine. Doing it without
noticing is not.
"""

import hashlib
import json

import pytest

from counterfoil.domain.events import Surface
from counterfoil.synth import BatchSpec, generate

#: sha256 of the salient fields of a 200-event payments batch, first 32 chars.
GOLDEN_PAYMENTS = {
    7: "dbd0218cb7d2aad7b19f036bc1fee814",
    2026: "98436a7f96fa0b0b5b73cf92a5ee0409",
}


def fingerprint(cases) -> str:
    blob = json.dumps(
        [
            [
                c.event_id,
                c.true_cause.value,
                c.event.amount.paise,
                c.event.customer.ref,
                round(c.u_spontaneous, 12),
                c.ambiguous,
                c.event.context.get("attempts_so_far"),
            ]
            for c in cases
        ],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


@pytest.mark.parametrize("seed,expected", sorted(GOLDEN_PAYMENTS.items()))
def test_the_payments_batch_has_not_moved(seed, expected):
    assert fingerprint(generate(BatchSpec(size=200, seed=seed))) == expected, (
        "The payments batch changed. If that was deliberate, regenerate the "
        "published figures and update GOLDEN_PAYMENTS. If not, something "
        "reordered the random stream."
    )


def test_adding_a_surface_does_not_disturb_payments():
    """Generating subscriptions must not consume from the payments stream."""
    before = fingerprint(generate(BatchSpec(size=200, seed=2026)))
    generate(BatchSpec(size=500, seed=2026, surface=Surface.SUBSCRIPTIONS))
    generate(BatchSpec(size=50, seed=7, surface=Surface.SUBSCRIPTIONS))
    assert fingerprint(generate(BatchSpec(size=200, seed=2026))) == before


def test_each_surface_has_its_own_stream():
    """Same seed, different surface, genuinely different cases."""
    payments = generate(BatchSpec(size=100, seed=2026))
    subs = generate(BatchSpec(size=100, seed=2026, surface=Surface.SUBSCRIPTIONS))
    assert [c.true_cause for c in payments] != [c.true_cause for c in subs]
    assert all(c.event.surface is Surface.PAYMENTS for c in payments)
    assert all(c.event.surface is Surface.SUBSCRIPTIONS for c in subs)
