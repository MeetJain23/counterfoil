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
