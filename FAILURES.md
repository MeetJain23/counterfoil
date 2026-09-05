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

---

## 005: The recorder had no idea rate limits existed

**Date:** 2026-08-21 · **Area:** model provider

**Expected:** Record 56 answers once, commit them, never pay for them again.

**What happened:** 9 recorded, 47 refused. The free tier caps requests per
minute and the client fired as fast as the loop could go, so 84% of the run
collected `429`s. The whole design goal of the fixture store is to make model
calls a one-off, and the recorder could not complete a single pass.

Worth noting what did not happen: nothing crashed, no half-written fixture was
saved, and the 9 good answers persisted. Every 429 became a `DEGRADED`
diagnosis, exactly like an unreachable API, because that path was already
built. Re-running simply resumed. The failure was in throughput, not in
correctness, which is the cheap kind.

**Cost:** One wasted run and about ten minutes.

**Fix:** The client now paces itself and retries. On a retryable status it
sleeps and tries again, obeying the provider's own `retryDelay` when the body
carries one and falling back to exponential backoff when it does not. Guessing
a shorter wait than the provider asked for is how a rate limit becomes a longer
rate limit. `429`, `500`, `502`, `503` and `504` are retried; `400`, `401`,
`403` and `404` are not, because repeating a malformed request just wastes the
quota that the real requests need.

Two things surfaced while testing it:

The first run of the new suite took 123 seconds instead of 3.5, because an
existing test raised a 429 and the retry logic dutifully slept through 8, 16,
32 and 64 seconds of real backoff. `sleep` and `clock` are now injected, so
every timing test asserts against a recorded list of durations and waits for
nothing.

The pacing test then failed for a better reason. `_last_call_at` used `0.0` as
its "no call yet" sentinel and checked it for truthiness, so with a clock
reading zero the guard disabled pacing entirely. A monotonic clock is allowed
to return zero. The sentinel is `None` now.

**What it taught us:** the degradation path built for "the model is down"
covered "the model is rate limiting" for free, without a line written for it.
Designing one honest failure mode bought a second one nobody had thought about.

---

## 006: Guessing the rate limit cost two hours

**Date:** 2026-08-22 · **Area:** model provider

**Expected:** After adding retry and pacing (see 005), recording 56 fixtures at
8 requests per minute should take about seven minutes.

**What happened:** Forty-five minutes in, 20 of 56 recorded and the process
still running. A direct probe against the same API succeeded instantly, so it
was not a quota exhaustion, an outage, or a hang.

The pacing figure was a guess, and it was too fast for this model. Nearly every
call hit a 429 and then paid a full retry chain of 8, 16, 32 and 64 seconds
before the next question even began. Two minutes per question, 56 questions, on
its way to nearly two hours.

The retry logic worked exactly as designed. That was the problem: retrying
correctly at the wrong cadence is still the wrong cadence, and backing off the
individual request does nothing about a rate that is structurally too high.

**Cost:** Two wasted runs and about an hour of wall clock. No money, since the
tier is free, and no lost work, because fixtures are written as they succeed
and re-running resumes.

**Fix:** The client now backs off the *interval*, not just the request. Each 429
widens the gap between calls by 1.6x up to a 45 second ceiling, so a batch
converges on the real limit within a few calls instead of paying a full retry
chain on every one of them. The published requests-per-minute figure differs by
model and changes without notice, so discovering it is more reliable than
configuring it.

Rewriting the tests exposed a second thing. They counted `sleep` calls to
assert on retry behaviour, which stopped meaning anything once pacing could
insert sleeps of its own. Retries are now counted through the `on_retry`
callback, and the fake clock advances when the fake sleep is called, the way
real time does. Asserting on a side effect rather than on the event itself is
what made those tests fragile.

**What it taught us:** a correct retry policy and a wrong rate are
indistinguishable from the inside. The system reported no errors, lost no data
and produced no bad output; it was simply going to take two hours to do seven
minutes of work, and nothing but a wall clock would have said so.

---

## 007: One DNS blip cost 36 questions, because network errors were not retried

**Date:** 2026-08-22 · **Area:** model provider

**Expected:** With adaptive pacing in place (006), the recorder finishes the
remaining 36 fixtures unattended.

**What happened:** It finished, having recorded nothing:

```
[50/56] withheld: model unavailable: could not reach Gemini: [Errno 11001] getaddrinfo failed
...
recorded : 20   withheld : 36
```

The connection dropped partway through. By the time it came back the run was
over. A direct DNS lookup a minute later resolved instantly, so the outage was
seconds long and the batch was already past every remaining question.

The retry logic covered HTTP status codes and nothing else. A dropped
connection is the most transient failure there is and the most obviously worth
retrying, and it was the one case that raised immediately. That gap was not
visible while the only failures being exercised were 429s.

**Cost:** One 25 minute run producing zero new fixtures. Nothing lost, since
recorded answers persist and re-running resumes.

**Fix:** `URLError` and `TimeoutError` now retry on the same backoff as a 5xx.
They do not widen the pacing interval, because pacing answers rate limits and a
dropped connection is not one. Four tests cover it, including a transport that
fails twice and then succeeds.

Then the suite went from 5 seconds to 128, for the second time, because a test
built a client with the real `time.sleep` and the new retry path slept through
a genuine 8, 16, 32, 64 second chain. Getting this wrong twice made it a
pattern rather than an accident, so it is now structural:

- `sleep` defaults to a module-level `_sleep` wrapper rather than `time.sleep`
  directly. A dataclass default binds at class-definition time, so the original
  form captured the real function and ignored any later patch.
- An autouse fixture in `conftest.py` replaces `time.sleep` with something that
  raises, naming the fix in the message. A test that waits now fails instead of
  merely being slow.

**What it taught us:** the second occurrence is the useful one. The first looks
like a mistake and invites a local fix; the same mistake twice says the design
permits it, and that is worth a guard rather than another patch.

---

## 008: The model's only mistake was my mistake

**Date:** 2026-08-24 · **Area:** model diagnosis / synthetic data

**Expected:** Measure how accurately the model classifies the ambiguous
failures the rule table cannot close, and report the number honestly whatever
it turned out to be.

**What happened:** 88.6% accuracy across 810 ambiguous cases, and every single
one of the 92 errors was the same confusion: a true `insufficient_funds`
classified as `issuer_decline_soft`. Not scattered noise. One systematic error.

Broken down by the description text the model actually reads, thirteen of the
fourteen variants were classified **100% correctly**. The entire error was one
sentence:

> Transaction could not be completed. Please check with your bank and try again.

That sentence contains no signal about a balance. I wrote it into the
generator's `insufficient_funds` pool, but nothing in the text says
insufficient funds, and `issuer_decline_soft` is arguably the better reading of
"the issuer refused and gave no reason". A human expert reading only that line
would fail it too. The ground truth was wrong, not the answer.

**Cost:** None, and it nearly went the other way. The obvious move on seeing
88.6% was to report it as the model's accuracy ceiling. The obvious move on
discovering the cause was to relabel the description and report 100%. Both are
wrong: the first blames the model for my data, the second launders a data fix
into a model result.

**Fix:** The description stays where it is. Gateways really do return
uninformative text for a genuine balance failure, so the case is realistic and
the ambiguity is irreducible. What changed is that the number is now reported
with the breakdown attached, because "88.6%, and here is the one sentence that
accounts for all of it" is a more useful sentence than either 88.6% or 100%
alone.

The more interesting finding is what the model did rather than what it got
wrong. The system prompt tells it explicitly to answer `unknown` when the
evidence does not separate two causes. Faced with a sentence that genuinely
does not, it answered confidently instead, at 0.85 to 0.98. Instructing a model
to express uncertainty is not the same as getting uncertainty, and the
confidence floor cannot catch an error delivered at 0.95.

What does catch it is the architecture. Both causes are non-terminal, so the
misdiagnosis changes retry *timing*, 8 hours instead of 26, rather than the
class of action taken. The terminal-cause clause, the retry caps and the
contact limits all still hold. A confident wrong answer costs money here; it
does not cost safety, and that distinction is the entire reason the model was
given no authority.

**What it taught us:** an accuracy figure with no error analysis behind it is
close to worthless. The single most useful thing measured this week was not
88.6%, it was the per-variant breakdown that turned one number into a specific
sentence in a specific file that I had written myself.

---

## 009: Adding a surface silently rewrote every published payments number

**Date:** 2026-08-24 · **Area:** synthetic generator

**Expected:** Adding subscriptions is additive. The payments surface has its
own cause mix, its own behaviour table and its own seeds, so nothing about it
should move.

**What happened:** Every payments batch changed. Same seed, different cases,
different amounts, different customers, different outcomes.

The generator draws from one shared random stream. Restructuring the loop to
support two surfaces moved the `context` dictionary construction ahead of the
`Customer` construction, so two `_weighted_choice` calls now happened before
three `rng.randrange` calls instead of after. That is enough. Every figure in
the README, every fixture fingerprint and every sensitivity result was keyed to
a stream that no longer existed.

Nothing failed loudly. The suite went green except for two tests, and those two
were the ones asserting the agent *loses* under a handicap: the loss had
quietly become a win.

**Cost:** Twenty minutes, because those two tests existed. Without them this
would have shipped, and the published numbers would have been unreproducible
from the published code with nothing to indicate why.

**Fix:** The draw order is restored and now carries a comment saying it is
load-bearing and must not be rearranged. More usefully, the batch is
fingerprinted: `test_generator_stability.py` hashes 200 events across two seeds
and fails if either moves. A deliberate change to the generator now means
updating a constant and regenerating the figures, which is a decision someone
has to make on purpose rather than a side effect they never see.

**What it taught us:** the test I nearly deleted as redundant is the one that
caught this. `test_the_win_is_a_timing_win_and_we_say_so` asserts a *negative*
result, that the agent loses when timing stops mattering, and it looked like an
oddly specific thing to lock down. Asserting the shape of a result, including
the parts that are unflattering, catches classes of error that asserting
success cannot.

---

## 0010: Complying with the mandate rules costs Rs 76,716, and we are reporting it

**Date:** 2026-08-24 · **Area:** subscriptions surface

**Expected:** The subscriptions surface would repeat the payments result. The
agent times its retries around salary dates, the naive arm hammers the mandate
on a fixed schedule, the agent wins on money and on restraint.

**What happened:** The naive arm won on money, by a lot.

```
                incremental   discretionary   notices   actions   breaches
naive           Rs 5,03,748             359         0      3246       5015
agent           Rs 4,27,032             404       614      1741          0
```

The mechanism is not subtle once measured. NPCI e-mandate rules require the
customer to be notified before a recurring debit is presented, and the policy
engine enforces a 24 hour notice period. So the agent sends a notice and cannot
present until a day later. The naive arm presents immediately and again every
day after, and simply does not send the notice: 2,887 pre-debit notice breaches
across the batch.

More presentations inside the same window means more recoveries. The agent gets
two shots where the naive arm takes four. That is the whole Rs 76,716.

**Cost:** None. This is the system working.

**Fix:** None, and that is the point. The obvious moves were to shorten the
notice period, or exempt the first retry, or quietly drop the clause on the
grounds that the eval looks better without it. All three amount to deciding
that a rule is optional when it is expensive, which is the exact failure the
policy engine exists to prevent.

What changed instead is the accounting. A pre-debit notice was being counted as
a customer contact, which made the compliant arm look noisier than the one
breaking the rule: 1,018 contacts against 359. Legally required notices are now
tracked separately from discretionary ones, and the break-even calculation uses
only the discretionary count. On the corrected numbers the two arms send
comparable discretionary messages, 404 against 359, and the agent additionally
sends 614 notices it is obliged to send.

**What it taught us:** compliance has a price and it is measurable, which is
more useful than pretending it is free. The honest headline for this surface is
not that Counterfoil recovers more. It is that an ungoverned agent recovers
Rs 76,716 more by committing 5,015 policy breaches, 2,887 of them regulatory,
and that a merchant cannot bank that money because it arrives attached to a
compliance finding. Reporting the loss and naming what bought it is a stronger
claim than a win would have been.

---

## 0011: The ceiling was not a ceiling, and the model went through it

**Date:** 2026-08-25 · **Area:** eval / oracle

**Expected:** With the fixtures finally complete, the model-contribution table
would fill in. The oracle reads the generator's held-back labels, so it is by
construction the best any diagnosis could do, and the model should land
somewhere below it.

**What happened:** On receivables the model captured **100.9%** of the oracle's
headroom. It returned more money than perfect diagnosis.

That is not a triumph, it is a contradiction, and the first instinct was the
wrong one: the number looked good, the surface had just started working, and it
would have been easy to write it up as "the model matches the theoretical
ceiling" and move on.

The cause took two passes to find. It was not the genuinely-unknown invoices;
oracle and model handled those identically. It was the cases where **both**
diagnosed `promise_to_pay` correctly. On those the model recovered ₹8.4L more.

The oracle knew the *cause* and not the *date*. It read `true_cause` from the
generator and stopped, so the playbook's promise-anchored follow-up fell back to
scheduling from the failure date and chased buyers before they had said they
would pay. The real model reads "give it three working days to land" out of the
buyer's reply and waits. On this surface the date is most of the value, so
cause-only knowledge is not perfect knowledge.

**Cost:** An hour, and it nearly cost the credibility of the entire
model-contribution table. Every percentage in it is measured against this
ceiling.

**Fix:** The oracle now supplies the promised date as well as the cause, taken
from the same held-back labels. Receivables drops to 97.6%, which is a number
that can be true. Payments and subscriptions were unaffected, because neither
has a date to extract.

A parametrised test now asserts, on all three surfaces, that the model never
returns more than the oracle. That invariant is what the entire contribution
metric rests on, and it had never been checked.

**What it taught us:** a result that is too good is a bug report. The useful
habit is not scepticism about bad numbers, which is easy, but scepticism about
good ones. "The model matched the theoretical maximum" was available, flattering,
and false, and nothing in the test suite would have contradicted it.
