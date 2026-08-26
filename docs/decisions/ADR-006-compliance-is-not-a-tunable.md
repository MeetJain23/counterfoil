# ADR-006: Regulatory clauses are not parameters

**Status:** accepted · **Date:** 2026-08-24

## Context

Most of `policies.yaml` encodes business preference. How many retries, how long
to wait, how much an invoice must be worth before it is chased: reasonable
people set these differently, and a merchant should be able to.

A few clauses are not like that. NPCI e-mandate rules require notifying a
customer before presenting a recurring debit. TRAI restricts commercial
communication outside set hours. These are not positions on the aggression
dial.

The distinction stopped being academic when it was measured. Enforcing the
pre-debit notice costs ₹95,568 on a 1,000-mandate batch. The ungoverned arm
presents four times inside the window where Counterfoil presents twice, and it
manages that by skipping the notice 2,887 times.

An eval that reports Counterfoil losing on that surface is a worse-looking
result than one that quietly relaxes the notice period to twelve hours, or
exempts the first retry, or drops the clause on the reasonable-sounding grounds
that it is only a demo.

## Decision

The clause stays, at 24 hours, and the loss is reported.

Compliance clauses are enforced regardless of what they cost, and the cost is
published rather than absorbed. `FAILURES.md` 008 carries the number and the
mechanism, the README leads with the loss rather than burying it, and the
dashboard renders it in the verdict panel where a viewer cannot miss it:

> Counterfoil nets ₹1,13,835.90 less than the ungoverned arm, which bought that
> with 5015 policy breaches.

A test asserts the agent loses on this surface. If it ever starts winning
outright, the likely explanation is that the notice requirement stopped being
enforced, and someone has to explain that rather than enjoy it.

## Consequences

**Two of three surfaces show Counterfoil behind on money.** That is the honest
result and it is a better claim than a win. An ungoverned agent recovering more
by committing 5,015 breaches is not producing revenue a merchant can bank; it is
producing revenue attached to a compliance finding.

**One measurement had to be fixed to keep the comparison fair.** A pre-debit
notice was being counted as customer contact, which made the compliant arm look
noisier than the one skipping the rule, 1,018 messages against 359. Mandatory
notices are now tracked separately and the break-even calculation uses only
discretionary contact. That is a correction to the instrument, not a thumb on
the scale: the corrected numbers still show the loss.

**Someone reviewing this will see a losing number first.** That is the intended
trade. A submission that only ever shows itself winning invites the question of
what it is not showing.

## What would change our mind

Being wrong about the rule. If the notice requirement does not apply the way
this models it, the clause should change to match reality, and the figures
should be regenerated and republished. Changing it because it is expensive is a
different thing entirely, and is the one move this decision exists to rule out.
