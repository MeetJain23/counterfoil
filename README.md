# Counterfoil

**A bounded revenue recovery agent that proves what it recovered.**

Razorpay AI Buildathon · Track 03, AI Revenue Recovery

A counterfoil is the stub of a receipt you keep after tearing off the other
half: the retained proof that something happened. That is what this system is
built around. Recovering money is the easy part. Being able to show, per
decision, what was tried, what was refused, and what it was actually worth, is
the part most recovery tooling never does.

---

## The problem

Revenue rarely disappears in one clean step. A payment degrades at the gateway.
A mandate is presented on the 28th against a salary that arrives on the 1st. An
invoice sits in someone's approval queue for forty days.

The tempting fix is an agent that retries everything and messages everyone.
That agent demos well and is a disaster in production: it burns gateway fees on
cards that can never succeed, presents mandate debits without the notice the
regulator requires, dunns customers who are in an active billing dispute, and
texts people at 2am. It will also report a large number for "revenue recovered"
which is mostly revenue that would have arrived anyway.

Counterfoil is that agent's opposite, and it is measured against it directly.

---

## Results

Three loss surfaces, 1,000 items each, seed 2026. Every figure below is
reproducible with one command.

| surface | at risk | agent net | naive net | agent breaches | naive breaches |
|---|---|---|---|---|---|
| payments | ₹17.9L | **₹8,56,102** | ₹6,23,439 | **0** | 2,976 |
| subscriptions | ₹8.1L | ₹3,82,630 | **₹4,96,466** | **0** | 5,015 |
| receivables | ₹18.4cr | ₹32,13,238 | **₹1,83,25,297** | **0** | 11,606 |

**Counterfoil loses on two of three surfaces, and that is the most interesting
thing in this repository.**

On subscriptions the ungoverned arm recovers ₹95,568 more. It buys that by
presenting mandate debits without a pre-debit notice 2,887 times, which is a
regulatory breach and not revenue a merchant can bank. On receivables it looks
far worse, and the reason is stated plainly below: the model layer that surface
depends on is not yet wired to real recordings.

Where the numbers are unflattering they are reported unflattering. The full
account of each is in [FAILURES.md](FAILURES.md).

---

## How the measurement works

Three arms run over identical cases:

- **control** does nothing at all
- **naive** retries hard and fast and messages everyone, with the policy engine
  running in shadow mode so its breaches are counted rather than prevented
- **agent** is Counterfoil

Four things make the comparison mean something.

**Randomness is drawn at generation time, not during a run.** Every arm faces
the same customers, the same banks and the same luck, so a difference between
arms is the intervention rather than the sample.

**Recovery is reported incremental, never gross.** Customers pay on their own.
An agent that counts those is measuring its own existence. Each case carries a
spontaneous recovery draw, and the control arm exists to subtract it. The
per-case counterfactual, *would this have recovered anyway*, is the number every
recovery product quietly needs and none can observe in production.

**Policy breaches are counted, not asserted.** Both arms face the identical
policy engine. Counterfoil obeys it; the naive arm has it evaluated and then
ignored, so "the ungoverned version breaks these rules this many times" is
measured.

**The result is attacked on purpose.** A sensitivity pass re-runs the batch with
the agent's advantages removed:

| variant | agent | naive | |
|---|---|---|---|
| baseline | ₹3,95,455 | ₹3,48,809 | agent wins |
| no right-intervention bonus | ₹3,84,160 | ₹3,48,809 | agent wins |
| timing barely matters | ₹3,95,455 | ₹4,54,727 | **agent loses** |
| both handicaps | ₹3,84,160 | ₹4,54,727 | **agent loses** |

The win survives removing the reward the world grants for matching the
intervention to the cause, which was the most obviously self-serving thing in
the model. It does not survive flattening the timing curve. **So the agent's
advantage is a timing advantage:** knowing the cause matters mostly because it
tells you *when* to act, not *what* to do. A test asserts the agent loses under
that handicap, so a future change that quietly makes it win everywhere fails the
suite.

The payments win holds across six seeds, ₹33,056 to ₹1,25,681.

---

## Where AI is used, and where it is not

The question is not whether a model is present. It is what it is worth, and the
answer differs by an order of magnitude across surfaces.

| surface | rules resolve | what the model is for |
|---|---|---|
| payments | 83.2% | the 17% where the provider returned prose instead of a code |
| subscriptions | 88.2% | same, narrower |
| receivables | **0%** | everything: the signal is an email thread |

A failed card arrives with a machine-readable reason code, so a lookup table
closes most of that surface for free and paying a model to rediscover that a
card expired would be slower and less reliable. An overdue invoice arrives with
a reply from a person. *We are waiting on the PO*, *we will pay on the 15th*,
*you billed us for twelve licences and we have eight*, and silence are four
situations that look identical in a dashboard and demand opposite responses.
Separating them is reading comprehension.

**Model value, measured in rupees against a ceiling.** An oracle arm reads the
generator's held-back labels, which no real system can do, purely to establish
what perfect diagnosis would achieve:

| surface | rules only | rules + model | perfect diagnosis | captured |
|---|---|---|---|---|
| payments | ₹7,67,100 | ₹8,66,520 | ₹9,07,612 | **70.8%** |
| subscriptions | ₹4,08,180 | ₹4,08,180 | ₹4,41,116 | 0% |
| receivables | ₹32,77,238 | ₹32,77,238 | ₹2,79,39,324 | 0% |

**The two zeroes are honest, not broken.** Recorded model answers exist for the
payments surface only. Subscription and receivables fixtures are still being
recorded against a free-tier daily quota, so on those surfaces every ambiguous
case currently degrades to human escalation, which is the correct behaviour and
not a useful one. Until they exist, the receivables oracle figure is what better
diagnosis *could* be worth there, not what this model *has* delivered.

That gap is the honest headline for receivables. With human review budgeted at
8 reviews per 100 invoices, an agent with no diagnosis spends that budget on
whatever it saw first; an agent with perfect diagnosis spends the same 48
reviews on the cases that need them and nets roughly eighteen times more. The
whole difference is knowing which forty-eight.

**Diagnosis accuracy on the ambiguous payments cases is 88.6%,** and the error
analysis matters more than the number: every one of the 92 errors is the same
confusion, and thirteen of fourteen description variants classify perfectly. The
entire error is one sentence written into the generator that carries no balance
signal while being labelled `insufficient_funds`. The ground truth was wrong,
not the answer. It has been left as it is, because gateways really do return
uninformative text for real balance failures, and relabelling it would launder a
data fix into a model result.

---

## The model has no authority

Everything above the `LLMClient` interface deals in `Ask` and `Answer`. Nothing
above it imports a provider SDK, so switching providers is a line of config, and
the tests never touch a network.

The important property is narrowness:

- **The model classifies; it cannot act.** The only object that leaves the
  diagnoser is a `Diagnosis`. There is no tool, no executor reference, and no
  path from a model output to a side effect that does not pass through the
  policy engine. A test parses the module's imports to enforce it.
- **Untrusted text is fenced and labelled.** Provider payloads and buyer email
  threads are wrapped in `<provider_payload>` tags, and the system prompt states
  that content inside is data which is never an instruction.
- **The customer is never sent to the model.** Only provider error fields and
  correspondence go, so there is nothing to leak.
- **Every failure degrades rather than propagates.** Unparseable JSON, an
  unknown cause, a non-numeric confidence, an unreachable API, an exhausted
  budget and a replay-mode cache miss all produce a `DEGRADED` diagnosis, which
  policy routes only to a human. Guessing is never the fallback.

A test grants the attacker a total win at the model layer, feeding a compromised
response carrying `action`, `override_policy` and `skip_confidence_floor`, and
shows those fields simply do not exist on the resulting domain object and the
diagnosis still faces every clause. The receivables generator includes
adversarial email threads so the defence is exercised rather than claimed.

The one place a model output becomes a policy *input* rather than a label is
`promised_within_days`, extracted from a buyer's own reply. It feeds a clause
that blocks contact until the date they committed to has passed.

---

## Policy is the whole of the agent's authority

[`policies.yaml`](backend/counterfoil/kernel/policy/policies.yaml) is 17 clauses
and it is the entire thing Counterfoil is allowed to do. A `Proposal` is inert;
only the deterministic policy engine may act. A `Decision` cannot be constructed
without the clauses that produced it and cannot be `allowed` while any clause
blocks it, both enforced in `__post_init__`.

Clauses cover retry caps per surface, causes that can never succeed on a retry,
bank-outage holds, TRAI quiet hours, contact frequency, value floors, confidence
floors, disputed-invoice protection, NPCI pre-debit notice, consecutive-failure
stops, promise-to-pay, and the two that make human attention behave like the
finite resource it is.

Three that are worth calling out:

**Quiet hours defer rather than drop.** A nudge blocked at 23:40 IST is
rescheduled to 09:00, but only when quiet hours are the *sole* blocker. Timing
problems delay revenue; permission problems forfeit it, and the engine
distinguishes them.

**Pre-debit notice is not a tunable.** NPCI e-mandate rules require notifying a
customer before presenting a recurring debit. Enforcing it costs ₹95,568 on the
subscriptions batch. Shortening the period or exempting the first retry would
each amount to deciding a rule is optional when it is expensive.

**Human review is budgeted.** A collections desk works a single-digit percentage
of the ledger by hand however much is overdue. Without that constraint,
"escalate everything" is the optimal policy on receivables, which is a helpdesk
rather than a recovery agent. With it, escalation is a choice between cases, and
that is what makes diagnosis worth doing.

---

## The audit trail

Every decision is written to an append-only ledger where each entry commits to
the hash of the entry before it. Editing or removing any past record breaks
verification at that point and every point after it, so the trail is
tamper-evident rather than merely written down. Tests prove both.

It is JSONL on purpose: a reader can open the file and re-verify the chain in
twenty lines of Python. Auditability that needs a running database to inspect is
not really auditability.

A run over 1,000 payments writes about 6,200 entries. Each records the event,
the diagnosis with its confidence and reasoning path, every clause evaluated
with its verdict, the action executed, and the observed outcome with the
evidence for it. The ledger also **refuses to record personal data**: payloads
are scanned at the write boundary and a phone number, email address or card
number raises rather than persisting.

The dashboard renders any event's full trail as a timeline:

```
DETECTED   #0   cus_841140765 ph:****9380 · ₹1,555.00 at risk
DIAGNOSED  #1   insufficient_funds at 0.98 via rule
DECIDED    #2   proposed retry_same_rail, allowed
                [PASS] retry.not_terminal_cause: can succeed on retry
                [PASS] retry.insufficient_funds_delay: retry 26.0h after failure
EXECUTED   #3   retry_same_rail · cost ₹2.50 · did not land
DECIDED    #4   proposed customer_nudge, blocked
                [BLOCK] contact.quiet_hours: 00:39 IST falls inside 21:00-09:00
```

---

## Run it

Python 3.12, no services required.

```bash
pip install -r backend/requirements.txt
python -m pytest backend/tests -q
```

Reproduce every figure above:

```bash
python tools/run_eval.py --size 1000 --audit
```

Add `--surface subscriptions` or `--surface receivables`, and
`--model-contribution` for the oracle comparison. `--audit` writes a ledger and
verifies the chain. Everything is seeded: two runs of the same command produce
identical figures.

The dashboard:

```bash
python -m uvicorn counterfoil.api.main:app --app-dir backend --port 8000
```

Read-only by construction. The single write endpoint runs a seeded simulation;
nothing reachable over HTTP can move money or message anyone.

---

## What is synthetic, and what that means

All figures come from a seeded synthetic world. The behavioural model behind it
lives in [`profiles.py`](backend/counterfoil/synth/profiles.py) as explicit
numbers with the reasoning attached, rather than buried in generator code,
because an eval on generated data is only as honest as the process that
generated it.

Those figures are informed estimates, not measurements. **Nothing here is a
claim about real Razorpay recovery rates.** What the eval demonstrates is that
the system measures its own effect correctly, including when the answer is
unflattering. Recorded model answers are committed to
[`llm_fixtures/`](llm_fixtures/), so anyone can clone this and reproduce the
numbers with no API key and no spend.

The live Razorpay sandbox lane is not yet wired. When it is, it will be a
separate, smaller set of cases, reported separately, and it will not be blended
into these figures.

---

## Why it is built this way

Six decisions the rest of the code is downstream of, in
[docs/decisions](docs/decisions/). Each records what was traded off, what it
cost, and what would change the answer.

Several of them look wrong from the outside until you know what the alternative
did. The policy engine refusing to act on the model's output looks like distrust
of the model, and is what makes the model safe to use at all. Reporting recovery
as incremental makes every headline smaller, and is the only version of the
number that means anything. Modelling human review as a budget looks like an
artificial constraint, and is what stops "escalate everything" being the optimal
policy.

## What broke

[FAILURES.md](FAILURES.md) is kept live rather than reconstructed. Eight entries
so far, including the ledger that would have happily recorded customer phone
numbers, the day the naive baseline beat the whole premise, the model retirement
that a diagnostic tool caught and then hid the fix for, the surface addition that
silently rewrote every published number, and the discovery that complying with
the mandate rules costs ₹95,568.

The most useful thing built this week was not any single number. It was the
per-variant error breakdown that turned "88.6% accurate" into one specific
sentence in one specific file.
