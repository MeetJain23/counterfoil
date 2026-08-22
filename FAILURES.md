# What broke, and how we got out

Kept live, from day one, rather than reconstructed at submission time. Newest last.

Format: what we expected, what actually happened, what it cost, what we changed.

---

## 001: The ledger would have happily recorded customer phone numbers

**Date:** 2026-08-21 · **Area:** audit ledger

**Expected:** The audit trail records evidence for every decision, so the more
provider payload we keep verbatim, the more explainable the system is.

**What happened:** Writing the first ledger test, the "evidence" dict for a
failed payment naturally contained the customer's contact details, because
that is exactly what Razorpay's payload carries and what a diagnosis rests on.
The ledger is the one artefact designed to be published, exported and shown to
a judge. We were one careless `payload=raw_event` away from a public repo
containing a file full of phone numbers and email addresses.

**Cost:** None yet. Caught before any data existed. Would have been severe.

**Fix:** The ledger now scans every payload before writing and raises
`PiiInLedgerError` on anything shaped like a full phone number, email address,
or card number. The refusal is at the write boundary, not in a code-review
convention, because conventions do not hold at 2am on day 12. `Customer`
carries only `phone_last4` and `email_domain`, and exposes a `redacted`
rendering that the ledger accepts. Eight tests cover it, including nested
payloads.

**What it taught us:** the auditability requirement and the privacy requirement
pull in opposite directions, and the resolution is not "be careful". It is to
make the unsafe thing impossible to express.

---

## 002: The naive baseline beat our agent's premise

**Date:** 2026-08-21 · **Area:** eval / world model

**Expected:** The whole thesis of Counterfoil is that a bounded, diagnosing
agent beats "retry everything, message everyone". The first end-to-end run over
a 1,000-event batch was supposed to show the naive arm burning money for
mediocre returns.

**What happened:** The naive arm returned ₹6,30,202 of gross uplift over
control for ₹6,762 of cost. It recovered 507 of 1,000 items. In this world,
spraying retries is an *excellent* strategy, and a careful agent has almost
no room to beat it on rupees.

The reason is that the world model prices only direct costs. A retry costs
₹2.50 against an average at-risk amount of ₹1,789. A message costs ₹0.18. When
an action is effectively free and sometimes works, doing it constantly is
correct. The model had no way to represent the actual cost of sending 694
messages to 1,000 customers.

**Cost:** None in money. Would have been fatal to the project if it had
surfaced on 4 September instead of day 1, or worse, if we had never run the
naive arm and simply reported the agent's gross recovery as a win.

**Fix:** Not to tune the numbers until our agent wins. That is the exact failure
this project exists to argue against, and it would be trivially detectable by
anyone reading the commit history. Instead:

1. **Price the contact externality.** Over-messaging causes churn, and churn
   costs future lifetime value. This is the dominant real cost of the naive
   strategy and the model currently omits it entirely. It goes in with the
   parameter stated openly in `profiles.py` and a sensitivity analysis across a
   range, so a reader can see exactly how much of the agent's advantage depends
   on that assumption.
2. **Count policy violations as a first-class metric.** The naive arm texts
   customers at 3am and dunns disputed invoices. Those are not modelling
   opinions, they are compliance breaches, and they are reportable as counts
   regardless of what any cost parameter is set to.
3. **Report gross recovery per arm regardless**, including when naive wins on
   it. If the honest answer is "naive recovers marginally more rupees and burns
   4x the customer goodwill to do it", that is the finding, and it is a more
   interesting one than a rigged victory.

**What it taught us:** we nearly shipped an eval whose baseline was too weak to
be informative. A control arm proves the agent does something; a *strong* naive
arm is what proves the agent does something worth doing. The uncomfortable
result on day 1 was the most useful output the system has produced so far.

---

## 003: The .env file I shipped could not be read by the loader I shipped

**Date:** 2026-08-21 · **Area:** configuration

**Expected:** Copy `.env.example` to `.env`, paste in an API key, run
`tools/check_llm.py`. That is the first thing anyone cloning this repo does,
including a judge.

**What happened:** It crashed immediately:

```
UnsafeConfigError: COUNTERFOIL_LLM_MODE must be replay|record|live,
got 'replay          # replay | record | live'
```

The `.env` loader was written to take values literally, deliberately, so that
nothing in the file could ever be expanded or executed. I took that too far: it
did not strip trailing comments either. And `.env.example`, which is the file
everybody copies, had a helpful inline comment on that exact line. The two
pieces I wrote in the same commit were incompatible with each other.

**Cost:** A few minutes. It surfaced on the very first real use, because the
config guard refused to boot rather than accepting a nonsense value and failing
somewhere less obvious later.

**Fix:** Values now end at the first whitespace-preceded `#`, which is the
convention every `.env` file in the wild is written against. A quoted value is
still taken whole, so a secret containing a `#` survives intact, and
`secret#value` unquoted keeps its hash because there is no whitespace before
it. Still no expansion and no substitution: the result is always a plain
string. Eleven parametrised cases cover the boundaries.

The example file also had its inline comments moved onto their own lines, so it
does not depend on the loader being lenient, and a test now loads
`.env.example` itself and asserts the settings it produces are valid. The
shipped example being parseable is not something to verify by hand each time.

**What it taught us:** the config guard earned its place. A validator that
refuses a malformed value at boot turned a silent misconfiguration into a
one-line error message naming the exact field. The bug was mine; the thing that
caught it was also mine, written on day 1 before there was anything to
misconfigure.

---

## 004: The model we defaulted to had been retired, and our own tool hid the fix

**Date:** 2026-08-21 · **Area:** model provider

**Expected:** `tools/check_llm.py` was written precisely because model names
move faster than a hard-coded default survives. It lists what the key can reach
before running a probe, so drift is caught in setup rather than in a batch run.

**What happened:** It caught the drift, then obscured the answer.

The listing showed `gemini-2.5-flash` present and configured, so the "is the
model available" check passed. The probe then failed:

```
404: This model models/gemini-2.5-flash is no longer available to new users.
Please update your code to use models/gemini-3.6-flash
```

Two separate mistakes:

1. **Being listed is not being callable.** `models.list` returns models that
   `generateContent` will refuse for a new key. The check conflated the two, so
   its reassuring "configured model found" line was worthless.
2. **The output truncated at 20 of 37 models**, a limit added for tidiness, and
   `gemini-3.6-flash` was below the cut. The tool printed the exact error naming
   the replacement, and simultaneously hid that replacement from the list right
   above it.

**Cost:** One confusing run. The system behaved correctly throughout: the
diagnoser degraded, nothing was retried, nothing was messaged, and the probe
would have been escalated to a human.

**Fix:** The default is now `gemini-3.6-flash`. The listing prints in full and
carries a line saying outright that being listed does not mean being callable
and only the probe settles it. And `suggested_replacement` parses the model name
out of the provider's own 404 body, so the tool ends with the literal line to
paste into `.env` rather than leaving it to be spotted in a stack trace. Four
parametrised cases plus one end-to-end test that a retirement notice survives
into the diagnosis rationale intact.

**What it taught us:** a diagnostic tool that reports a green check next to a
broken thing is worse than no tool, because it spends the reader's trust. The
check was right to exist and wrong to summarise. Where it now cannot be certain,
it says so.
