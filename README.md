# Counterfoil

**A bounded revenue-recovery agent for Indian merchants that proves what it recovered.**

Razorpay AI Buildathon · Track 03, AI Revenue Recovery · *work in progress*

---

## The problem

Revenue rarely disappears in one clean step. A payment degrades at the gateway.
A checkout gets abandoned at the OTP screen. A subscription mandate fails on the
28th because the salary lands on the 1st. An invoice quietly goes 40 days
overdue. Each of these is recoverable, and each is recovered, when it is
recovered at all, by someone manually deciding what to do about it.

The tempting fix is an agent that retries everything and messages everyone.
That agent looks great in a demo and is a disaster in production: it burns
gateway fees on cards that can never succeed, dunns customers who are in an
active billing dispute, and texts people at 2am. It will also report a large
number for "revenue recovered" that is mostly revenue which would have arrived
anyway.

## What Counterfoil does

Counterfoil detects revenue at risk across three surfaces, diagnoses the root
cause, chooses a **bounded** intervention, executes it, and then measures
whether the intervention actually caused anything.

```
        ┌──────────────── SURFACES (adapters) ─────────────────┐
        │  payments/        subscriptions/      receivables/    │
        │  failed txns      mandate failures    overdue invoices│
        └───────────────────────┬───────────────────────────────┘
                                │  normalize → RiskEvent
                                ▼
  ┌────────────────────── RECOVERY KERNEL ────────────────────────┐
  │ 1 DETECT    rules + anomaly detection over the event stream   │
  │ 2 DIAGNOSE  deterministic code map FIRST, LLM only if unclear │
  │ 3 DECIDE    policy engine: eligible actions, caps, gates     │
  │ 4 ACT       bounded executor: retry / switch rail / nudge /   │
  │             escalate / STOP.  idempotent, rate-limited        │
  │ 5 OBSERVE   outcome collector (webhook + reconcile poll)      │
  │ 6 LEARN     per-arm attribution, uplift vs control            │
  └──────────┬─────────────────────────────────┬──────────────────┘
             │                                 │
     AUDIT LEDGER                        EVAL HARNESS
  append-only, hash-chained        control / naive / agent arms
  decision + evidence + ₹          uplift, net ₹, cost, CIs
```

Three commitments hold everywhere in the system.

### 1. The model has no authority

Whatever produces a proposal, a rule table or an LLM, may only *propose*. A
`Proposal` is inert. The **policy engine** decides, and it is entirely
deterministic: no clause may consult a model, so every decision replays exactly.

This is also the security boundary. The receivables surface reads customer
emails, which are attacker-controlled text feeding a system that moves money. A
prompt injection reading *"mark this invoice as paid"* produces, at worst, a
proposal that the policy engine rejects and the ledger records.

### 2. Every action cites the clause that permitted it

A `Decision` cannot be constructed without the clauses that produced it, and
cannot be `allowed` while any clause blocks it. Both are enforced in `__post_init__`,
not by convention. Refusals cite the blocking clause just as loudly as
approvals cite the permitting one.

Some current clauses:

| Clause | What it prevents |
|---|---|
| `retry.not_terminal_cause` | Retrying a card that can never succeed |
| `retry.bank_outage_hold` | Burning attempts while the issuer is down |
| `retry.insufficient_funds_delay` | Retrying a balance that has not had time to change |
| `contact.quiet_hours` | Messaging customers between 21:00 and 09:00 IST |
| `contact.frequency_cap` | Becoming the reason someone churns |
| `receivables.no_dunning_when_disputed` | Chasing a billing dispute as a collection |
| `global.value_floor` | Spending ₹20 to recover ₹12 |
| `safety.degraded_diagnosis` | Acting on a guess when the model was unavailable |

Quiet hours **defer rather than drop**: a nudge blocked at 23:40 IST is
rescheduled to 09:00, but only when quiet hours are the sole blocker. Timing
problems delay revenue; permission problems forfeit it, and the engine
distinguishes them.

### 3. Recovery is measured against a control group, or it is not measured

Every batch runs three arms on identical seeded data:

- **control**: do nothing
- **naive**: retry everything, contact everyone, immediately
- **agent**: Counterfoil

The headline number is *incremental* recovery over control, net of intervention
cost, with the exception list of everything the agent refused to touch and why.
"We recovered ₹4.2L" is not a result. "₹4.2L against ₹3.1L from doing nothing"
is.

## The counterfoil

The name is the audit ledger. A counterfoil is the stub of a receipt you keep
after tearing off the other half. It is the retained proof that the transaction
happened.

Every decision is appended to a hash chain: each entry commits to the hash of
the entry before it, so editing or deleting any past record breaks verification
at that point and every point after. It is stored as JSONL, deliberately:
you can open the file, read it, and re-verify the chain in twenty lines of
Python. Auditability that requires a running database to inspect is not
really auditability.

The ledger also **refuses to record personal data**. Any payload containing
something shaped like a full phone number, email address, or card number raises
at the write boundary rather than being quietly persisted. (See
[FAILURES.md](FAILURES.md) #001 for why that guard exists.)

## Safety posture

This is a test-mode system and is built so that it cannot become anything else.

- Refuses to boot on an `rzp_live_` key, or on any unrecognised key shape
- Has **no production mode**. `COUNTERFOIL_ENV=prod` raises
- Dry-run and LLM-replay are the defaults; side effects and spending are opt-in
- Hard spend ceiling; a run halts rather than quietly billing
- All customer data is synthetic
- Webhooks verified by HMAC signature, timestamp window, and replay nonce
- Secrets are scanned at every commit by [tools/scan_secrets.py](tools/scan_secrets.py),
  which is itself covered by tests, because a security control that is untested is a
  claim, not a control

## Running it

```bash
python -m pytest backend/tests -q
```

```bash
python tools/scan_secrets.py --all
```

Full setup is in [RUNBOOK.md](RUNBOOK.md).

## Status

| Component | State |
|---|---|
| Domain model (`Money`, `RiskEvent`, `Diagnosis`, `Decision`, `Outcome`) | done |
| Hash-chained audit ledger | done |
| Policy engine, 12 clauses | done |
| Config safety guards | done |
| Secret + PII scanner | done |
| Synthetic batch generator | next |
| Rules diagnoser | next |
| LLM diagnoser (Claude Haiku 4.5, replay-cached) | next |
| Surface: payments | next |
| Surfaces: subscriptions, receivables | after payments |
| Eval harness + dashboard | after payments |

---

Educational project built for the Razorpay AI Buildathon. Test mode only; no
real funds move. Not financial advice, and not a collections service.
