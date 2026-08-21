# Recorded model answers

One JSON file per distinct question the rule table could not close. These are
committed on purpose.

They make the eval reproducible: clone the repo, run `python tools/run_eval.py
--model-contribution`, and you get the published numbers with no API key and no
spend. They are also the evidence behind those numbers, so they belong in
version control next to the code that produced them.

The filename is a fingerprint of the *situation*, not of the prompt. Rewording
the system prompt does not invalidate a recording, because the question did not
change. Changing the cause taxonomy does, which is what `SCHEMA_VERSION` in
`backend/counterfoil/kernel/diagnose/llm.py` is for.

To record or top up:

    python tools/record_fixtures.py              # dry run, shows the cost
    python tools/record_fixtures.py --confirm    # spends money

Nothing here contains customer data. The diagnoser never sends the customer to
the model, only provider error fields, so there is nothing to redact.
