# ADR-005: Human review is a budget, not an escape hatch

**Status:** accepted · **Date:** 2026-08-24

## Context

Escalating to a person is the safe answer to uncertainty, and the policy engine
leans on it: a degraded diagnosis is allowed to reach a human precisely because
that is never the wrong thing to do with a case you do not understand.

Modelled naively, it is also the *optimal* answer to everything. The first
version of the receivables surface priced escalation at ₹40 with a flat 42%
success rate. Against invoices averaging ₹1.75L that makes "put every invoice in
front of a person" strictly dominant, and the agent escalated 552 of 600 cases
for an apparent ₹3.15 crore. That is not a recovery agent. It is a ticket
router with a good opinion of itself.

The reason real collections teams do not do this is not that it fails. It is
that they have three people.

## Decision

Human attention is modelled as the finite resource it is, in three parts.

**What a human achieves depends on what they were handed.** `escalation_success`
is a property of the cause rather than a constant. A person can genuinely
resolve a billing dispute and get it paid, at 55%. They can do very little about
a buyer who already said the cheque goes out on Friday, at 18%. A single rate
made triage worthless by making every case equally worth escalating.

**Escalation is priced at what it costs.** Twenty minutes of loaded analyst
time, ₹800. The most expensive action available and the only one that does not
scale.

**Capacity is a batch-level budget.** `Capacity` is the only shared mutable
state in the policy engine, and it has to be shared: every other clause asks a
question about one case, while "can a person look at this" is a question about
the whole queue. `escalations_per_100_invoices` sets it at 8 for receivables and
3 for the transactional surfaces. Each arm gets its own budget, because sharing
one would let whichever ran first starve the others and measure running order.

A value floor sits alongside it. Handing someone a ₹499 subscription loses money
on the handoff before they do anything.

## Consequences

**Diagnosis becomes worth something.** With 48 reviews for 600 invoices,
escalating is a choice between cases rather than a free answer to any case. An
agent with no diagnosis spends that budget on whatever it saw first and nets
₹9,11,831; the same budget triaged by perfect diagnosis nets ₹1,69,99,280. Same
48 reviews, same batch. The entire difference is knowing which forty-eight.

**Some cases correctly end in nothing at all.** A ₹499 plan that has failed
three cycles is refused a retry by the stop rule and refused a human by the
value floor. The right end state is no action, and a test asserts it.

**The numbers moved when this landed.** Repricing escalation changed every
surface, including payments. That is the correct direction: the earlier figures
were flattered by an action that was effectively free.

## What would change our mind

A merchant whose escalation path is not a person. If the handoff were to
self-serve tooling that genuinely scales, the capacity clause would be wrong and
the value floor would be the only gate that still made sense.
