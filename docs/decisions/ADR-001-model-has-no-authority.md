# ADR-001: The model proposes, the policy engine disposes

**Status:** accepted · **Date:** 2026-08-21

## Context

Counterfoil reads text it does not control and then spends money. On the
payments surface that text is a gateway error description. On receivables it is
an email written by the counterparty, who has an obvious incentive to influence
what happens next.

The default shape for an agent in 2026 is a model with tools: give it a
`retry_payment` function and a `send_message` function and let it decide. That
design makes the model's output an instruction. Every guard then has to live
inside the prompt, and prompt injection becomes a direct path from attacker text
to a side effect.

The alternative is to give the model no tools at all and let something
deterministic decide what happens.

## Decision

The model classifies. It cannot act.

The only object that leaves the diagnoser is a `Diagnosis`: a cause, a
confidence, a rationale, and quoted evidence. There is no tool definition, no
executor reference, and no import path from the diagnosis module to anything
that performs an action. A test parses the module's AST to enforce that, rather
than trusting a convention.

What the model produces then feeds a `Proposal`, which is inert, and the
deterministic policy engine decides whether the proposal executes. Untrusted
text is fenced in `<provider_payload>` tags and the system prompt states that
content inside is data which is never an instruction.

## Consequences

**A successful prompt injection buys a wrong label.** The worst an attacker
achieves is a confident misclassification. That still faces the confidence
floor, the terminal-cause clause, the retry caps, the contact limits, and every
other clause. A test grants the attacker a total win at the model layer, feeding
a response carrying `action`, `override_policy` and `skip_confidence_floor`, and
shows those fields simply do not exist on the resulting domain object.

**Every model failure has one shape.** Unparseable JSON, an unknown cause, a
non-numeric confidence, an unreachable API, an exhausted budget and a cache miss
all produce `DEGRADED`, which policy routes only to a human. There is no
fallback that guesses.

**We give up the model choosing novel actions.** It cannot invent an
intervention we did not write down. On these surfaces that is not much of a
loss: once you know a card is expired, "ask for a new card" is a lookup rather
than a judgement call, and the playbooks are a page of code.

**One exception is deliberate.** On receivables the model extracts
`promised_within_days` from a buyer's reply, and that number becomes a policy
*input* rather than a label: it drives the clause that blocks chasing before the
promised date. It is still not authority. A malformed value falls back to
scheduling from the failure date, and the clause it feeds only ever restricts
what the agent may do.

## What would change our mind

A surface where the right action genuinely cannot be enumerated in advance. If
recovery required composing a novel payment plan per debtor, a proposer that can
only pick from a fixed list would be the wrong tool, and the honest response
would be to widen what the model may propose while keeping policy as the thing
that approves it.
