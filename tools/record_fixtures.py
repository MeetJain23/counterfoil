#!/usr/bin/env python3
"""Record model answers once, so every later run is free and reproducible.

    python tools/record_fixtures.py                 # dry run: what it would cost
    python tools/record_fixtures.py --confirm       # actually spend money

Only the cases the rule table refuses to answer are sent, and identical
situations are collapsed to one question first, so this costs a few US cents
rather than a few dollars. The recorded answers are committed to the repository:
they are the evidence behind the numbers in the README, and they let anyone
clone the repo and reproduce the eval with no API key and no spend.

Requires ANTHROPIC_API_KEY in the environment. Never pass a key on the command
line, where it lands in your shell history.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from counterfoil.config import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from counterfoil.kernel.diagnose import rules  # noqa: E402
from counterfoil.kernel.diagnose.llm import LLMDiagnoser, case_fingerprint  # noqa: E402
from counterfoil.llm import AnthropicClient, Budget, FixtureStore  # noqa: E402
from counterfoil.synth import BatchSpec, generate  # noqa: E402

FIXTURES = Path("llm_fixtures")

#: Measured from the prompt: about 900 input tokens of taxonomy and payload,
#: about 90 output. At Haiku 4.5 rates, and most of the input cached after the
#: first call.
ESTIMATE_PER_CALL_USD = 0.0006


def distinct_questions(sizes: tuple[int, ...], seeds: tuple[int, ...]):
    """Every situation across every batch we intend to publish numbers for."""
    seen: dict[str, object] = {}
    for size in sizes:
        for seed in seeds:
            for case in generate(BatchSpec(size=size, seed=seed)):
                if rules.diagnose(case.event) is not None:
                    continue
                key = case_fingerprint(case.event)
                seen.setdefault(key, case.event)
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually call the API")
    parser.add_argument("--cap-usd", type=float, default=0.50)
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000])
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 11, 23, 42, 99, 2026])
    args = parser.parse_args()

    questions = distinct_questions(tuple(args.sizes), tuple(args.seeds))
    store = FixtureStore(FIXTURES, mode="record" if args.confirm else "replay")

    already = sum(1 for key in questions if (FIXTURES / f"{key}.json").exists())
    todo = len(questions) - already

    print(f"batches      : sizes {args.sizes}, seeds {args.seeds}")
    print(f"questions    : {len(questions)} distinct, {already} already recorded")
    print(f"to record    : {todo}")
    print(f"est. cost    : ${todo * ESTIMATE_PER_CALL_USD:.4f} at {args.model} rates")
    print(f"spend cap    : ${args.cap_usd:.2f}")

    if not args.confirm:
        print()
        print("Dry run. Nothing was called and nothing was spent.")
        print("Re-run with --confirm to record.")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\nANTHROPIC_API_KEY is not set. Export it and try again.", file=sys.stderr)
        return 2

    budget = Budget(cap_usd=args.cap_usd)
    diagnoser = LLMDiagnoser(
        client=AnthropicClient(model=args.model), fixtures=store, budget=budget
    )

    print()
    recorded = degraded = 0
    for i, (key, event) in enumerate(questions.items(), 1):
        result = diagnoser(event)
        if result.path.value == "llm":
            recorded += 1
        else:
            degraded += 1
            print(f"  [{i}/{len(questions)}] withheld: {result.rationale[:70]}")
        if budget.exhausted:
            print(f"\nSpend cap reached after {i} questions. Re-run to continue.")
            break

    print()
    print(f"recorded  : {recorded}")
    print(f"withheld  : {degraded}")
    print(budget.summary())
    print(store.stats())
    print(f"\nFixtures are in {FIXTURES}/. Commit them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
