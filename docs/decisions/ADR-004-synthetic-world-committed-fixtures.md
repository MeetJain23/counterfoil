# ADR-004: The world is seeded and the model answers are committed

**Status:** accepted · **Date:** 2026-08-22

## Context

Every figure this project publishes needs to be checkable by someone who did not
write it. Two things get in the way.

The data is synthetic, so the behavioural model behind it decides the answers.
An eval on generated data is only as honest as the process that generated it,
and a generator buried in code is a generator nobody audits.

The model calls cost money and are rate limited. If reproducing the README
requires an API key and a working quota, almost nobody reproduces it, and the
figures become an assertion.

## Decision

**The behavioural model is explicit and readable.** `profiles.py` holds the
spontaneous recovery rate, retry success, ripening time and escalation success
for every cause, as numbers with the reasoning attached in comments. A reader
who disagrees with a figure can find it and see what it does.

**Batches are seeded and fingerprinted.** The same seed produces the same batch,
and `test_generator_stability.py` hashes 200 events across two seeds and fails
if either moves. A deliberate change means updating a constant and regenerating
the figures, which is a decision somebody makes rather than a side effect
nobody sees.

**Model answers are recorded once and committed.** `llm_fixtures/` holds one
JSON file per distinct question. Clone the repo, run the eval, get the published
numbers with no API key and no spend. The fixtures are evidence, so they belong
in version control next to the code that produced them.

**The cache key is the situation, not the prompt.** Rewording a system prompt
does not invalidate a recording, because the question did not change. Amount,
attempt count and days overdue are excluded: a gateway timeout is the same
question at ₹200 and at ₹20,000, and an invoice stuck in approval says the same
thing at 12 days overdue and at 80.

## Consequences

**Reproduction costs nothing.** CI regenerates the figures on every push with no
credentials.

**Recording is cheap.** Across six seeds of 1,000 events on three surfaces, the
whole corpus collapses to 85 distinct questions.

**Fixtures are a liability if the key is wrong.** Keying on `days_overdue` once
turned 23 real questions into 4,085 paid ones. Keying on too little would serve
one answer for two genuinely different situations. The split between what the
model is shown and what makes two cases identical is now explicit.

**Recorded answers can go stale.** `SCHEMA_VERSION` invalidates them when the
taxonomy changes. Payments keys deliberately predate the multi-surface split and
were left unrenamed, because invalidating recorded evidence costs a day against
a rate limit and changes published figures for no gain.

**The claim is narrow, and the README says so.** These numbers demonstrate that
the system measures its own effect correctly. They are not a claim about real
recovery rates.

## What would change our mind

Real data. The moment there is a live sandbox lane, it gets reported separately
and never blended into these figures, because two things measured differently
should not be added together.
