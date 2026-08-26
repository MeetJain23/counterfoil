# ADR-002: Recovery is reported incremental, never gross

**Status:** accepted · **Date:** 2026-08-21

## Context

The obvious way to score a recovery agent is to add up the money that arrived
after it acted. It is also close to meaningless, because a large share of that
money was going to arrive anyway. Customers retry failed payments themselves.
Invoices get paid because somebody finally opened the inbox. An agent that
counts those is measuring its own existence rather than its effect.

This is not a subtle bias. On the payments surface, doing nothing at all
recovers ₹3,09,834 of a ₹17.9L book. Any agent that reports gross recovery
starts from that number for free.

The problem is that in production you cannot observe the counterfactual. You
sent the message; you cannot know what would have happened if you had not.

## Decision

Every case carries a spontaneous recovery draw fixed at generation time, and a
control arm that takes no action at all exists to subtract it. Recovery is
always reported as incremental over control.

Two supporting choices follow from it.

**Randomness is drawn at generation time, not during a run.** Every arm faces
the same customers, the same banks and the same luck, so a difference between
arms is the intervention rather than the sample. Sampling fresh randomness per
arm would compare the agent against a different universe than the control arm
saw, and would need a far larger batch to say anything.

**Attribution requires evidence.** A `RECOVERED` outcome cannot be constructed
without a provider reference, enforced in `__post_init__`. And a recovery that
would have happened anyway is never marked attributable, even when the agent
also acted on it.

## Consequences

**Every headline number gets smaller.** Gross recovery on the payments batch is
₹11,44,130. Incremental is ₹8,66,520. The second is the one reported.

**The confidence interval is a paired bootstrap.** Both arms saw identical
cases, so resampling them independently would reinstate exactly the variance
that common random numbers were introduced to remove.

**A false-positive cost becomes visible.** Money spent chasing a customer who
was already going to pay is tracked as wasted spend. Gross figures hide this
entirely; it is the closest thing these surfaces have to a precision metric.

**It only works on synthetic data.** The per-case counterfactual is the one
thing this system knows that no production system can. That is stated plainly in
the README rather than glossed, and it is the honest limit of what the eval
proves: that the system measures its own effect correctly, not that the effect
would be this size in production.

## What would change our mind

Nothing about the principle. In production the same shape is achievable with a
holdout: withhold treatment from a random slice and compare. That is strictly
better evidence than what is here, and it is what this design is meant to be a
rehearsal for.
