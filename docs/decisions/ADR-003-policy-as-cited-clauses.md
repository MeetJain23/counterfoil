# ADR-003: Every decision cites the clauses that produced it

**Status:** accepted · **Date:** 2026-08-21

## Context

The track brief asks for bounded actions and an audit trail. The cheap way to
satisfy that is to log what the agent did. The useful way is to make an
unexplainable action impossible to construct in the first place.

There is also a practical problem. Recovery aggression is exactly the kind of
thing a merchant wants to tune, and a system where "how many retries" is spread
across five functions cannot be tuned by anyone except its author.

## Decision

Policy is a single declarative file, and a decision cannot exist without the
clauses that produced it.

`policies.yaml` holds all 17 clauses and is the whole of the agent's authority.
A `Proposal` is inert. The engine evaluates every applicable clause, and the
resulting `Decision` carries the full list of `ClauseEval` results with a
`PASS` or `BLOCK` and a human-readable reason for each.

Two invariants are enforced in `__post_init__` rather than by review:

- a `Decision` with no evaluated clauses raises, because it is not auditable
- a `Decision` cannot be `allowed` while any clause blocks it

Clauses may not consult a model. Policy has to be deterministic and replayable,
or an audit trail cannot be re-derived from the record.

## Consequences

**Refusals are as legible as actions.** The agent's restraint is countable:
`contact.quiet_hours` refused 385 times on a payments batch, `escalation.capacity`
914 times on receivables. Those are product facts, not log noise.

**The naive arm becomes measurable rather than rhetorical.** It runs through the
same engine in shadow mode, where every clause is evaluated, recorded, and then
ignored. "The ungoverned version breaks these rules 2,976 times" is a count
rather than a claim.

**Tuning does not require reading the code.** Changing retry aggression, quiet
hours, or the value floor is a diff in one YAML file, and the reasoning for each
number sits in a comment next to it.

**Blocked is not always the end.** A nudge refused for quiet hours is
rescheduled to 09:00 rather than dropped, but only when quiet hours are the
*sole* blocker. Timing problems delay revenue; permission problems forfeit it,
and conflating them would either lose money or break a rule.

**It is verbose.** A single case with four actions produces around a dozen
ledger entries. That is the intended trade: the ledger is the product.

## What would change our mind

If clause evaluation became a performance problem at a volume this design was
never meant for. Even then the fix would be to evaluate lazily, not to stop
recording why.
