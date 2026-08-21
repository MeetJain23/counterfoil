# What broke, and how we got out

Kept live, from day one, rather than reconstructed at submission time. Newest last.

Format: what we expected, what actually happened, what it cost, what we changed.

---

## 001 — The ledger would have happily recorded customer phone numbers

**Date:** 2026-08-21 · **Area:** audit ledger

**Expected:** The audit trail records evidence for every decision, so the more
provider payload we keep verbatim, the more explainable the system is.

**What happened:** Writing the first ledger test, the "evidence" dict for a
failed payment naturally contained the customer's contact details — because
that is exactly what Razorpay's payload carries and what a diagnosis rests on.
The ledger is the one artefact designed to be published, exported and shown to
a judge. We were one careless `payload=raw_event` away from a public repo
containing a file full of phone numbers and email addresses.

**Cost:** None yet — caught before any data existed. Would have been severe.

**Fix:** The ledger now scans every payload before writing and raises
`PiiInLedgerError` on anything shaped like a full phone number, email address,
or card number. The refusal is at the write boundary, not in a code-review
convention, because conventions do not hold at 2am on day 12. `Customer`
carries only `phone_last4` and `email_domain`, and exposes a `redacted`
rendering that the ledger accepts. Eight tests cover it, including nested
payloads.

**What it taught us:** the auditability requirement and the privacy requirement
pull in opposite directions, and the resolution is not "be careful" — it is to
make the unsafe thing impossible to express.

---

## 002 — The naive baseline beat our agent's premise

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
surfaced on 4 September instead of day 1, or — worse — if we had never run the
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
