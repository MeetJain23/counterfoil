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
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from counterfoil.config import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from counterfoil.config import load_settings  # noqa: E402
from counterfoil.domain.events import Surface  # noqa: E402
from counterfoil.kernel.diagnose import rules  # noqa: E402
from counterfoil.kernel.diagnose.llm import LLMDiagnoser, case_fingerprint  # noqa: E402
from counterfoil.llm import Budget, FixtureStore, build_client  # noqa: E402
from counterfoil.synth import BatchSpec, generate  # noqa: E402

FIXTURES = Path("llm_fixtures")

#: Measured from the prompt: about 900 input tokens of taxonomy and payload,
#: about 90 output. At Haiku 4.5 rates, and most of the input cached after the
#: first call.
ESTIMATE_PER_CALL_USD = 0.0006


def distinct_questions(
    sizes: tuple[int, ...], seeds: tuple[int, ...], surfaces: tuple[Surface, ...]
):
    """Every situation across every batch we intend to publish numbers for.

    Collapsed by fingerprint first. A thousand invoices produce a few dozen
    distinct questions, and paying for the other nine hundred would be paying
    to be told the same thing again.
    """
    seen: dict[str, object] = {}
    for surface in surfaces:
        for size in sizes:
            for seed in seeds:
                spec = BatchSpec(
                    size=size,
                    seed=seed,
                    surface=surface,
                    window_hours=48 if surface is Surface.SUBSCRIPTIONS else 72,
                )
                for case in generate(spec):
                    if rules.diagnose(case.event) is not None:
                        continue
                    seen.setdefault(case_fingerprint(case.event), case.event)
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually call the API")
    parser.add_argument("--cap-usd", type=float, default=0.50)
    parser.add_argument("--model", default=None, help="overrides the .env setting")
    parser.add_argument("--rpm", type=float, default=5.0,
                        help="requests per minute; the free tier is capped")
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000])
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 11, 23, 42, 99, 2026])
    parser.add_argument("--surfaces", nargs="+", default=[s.value for s in Surface],
                        choices=[s.value for s in Surface])
    args = parser.parse_args()

    settings = load_settings()
    model = args.model or settings.llm_model

    surfaces = tuple(Surface(s) for s in args.surfaces)
    questions = distinct_questions(tuple(args.sizes), tuple(args.seeds), surfaces)
    store = FixtureStore(FIXTURES, mode="record" if args.confirm else "replay")

    already = sum(1 for key in questions if (FIXTURES / f"{key}.json").exists())
    todo = len(questions) - already

    print(f"batches      : sizes {args.sizes}, seeds {args.seeds}")
    print(f"surfaces     : {', '.join(s.value for s in surfaces)}")
    print(f"questions    : {len(questions)} distinct, {already} already recorded")
    print(f"to record    : {todo}")
    print(f"provider     : {settings.llm_provider} / {model}")
    cost = (
        "free tier, no charge"
        if settings.llm_provider == "gemini"
        else f"${todo * ESTIMATE_PER_CALL_USD:.4f}"
    )
    print(f"est. cost    : {cost}")
    print(f"spend cap    : ${args.cap_usd:.2f}")

    if not args.confirm:
        print()
        print("Dry run. Nothing was called and nothing was spent.")
        print("Re-run with --confirm to record.")
        return 0

    client = build_client(replace(settings, llm_mode="live", llm_model=model))
    if client is None:
        key = "GEMINI_API_KEY" if settings.llm_provider == "gemini" else "ANTHROPIC_API_KEY"
        print(f"\n{key} is not set. Put it in .env and try again.", file=sys.stderr)
        return 2

    if hasattr(client, "min_interval_seconds"):
        client.min_interval_seconds = 60.0 / args.rpm if args.rpm > 0 else 0.0
        client.on_retry = lambda n, delay, code: print(
            f"    rate limited ({code}), waiting {delay:.0f}s before retry {n}; "
            f"pacing now {client.min_interval_seconds:.0f}s between calls",
            flush=True,
        )
        print(f"pacing       : {args.rpm:.0f} requests/minute, "
              f"about {len(questions) * 60 / args.rpm / 60:.1f} minutes")

    budget = Budget(cap_usd=args.cap_usd)
    diagnoser = LLMDiagnoser(client=client, fixtures=store, budget=budget)

    print()
    recorded = degraded = cached = 0
    for i, event in enumerate(questions.values(), 1):
        hits_before = store.hits
        result = diagnoser(event)
        served_from_cache = store.hits > hits_before
        if result.path.value == "llm":
            if served_from_cache:
                cached += 1
                label = "already recorded"
            else:
                recorded += 1
                label = "recorded"
            print(f"  [{i}/{len(questions)}] {label}: {result.cause.value} "
                  f"({result.confidence:.2f})", flush=True)
        else:
            degraded += 1
            # Not truncated. A shortened error message has hidden the actual
            # cause of a failed run three separate times on this project.
            print(f"  [{i}/{len(questions)}] withheld: {result.rationale}", flush=True)
        if budget.exhausted:
            print(f"\nSpend cap reached after {i} questions. Re-run to continue.")
            break

    print()
    print(f"newly recorded : {recorded}")
    print(f"already had    : {cached}")
    print(f"withheld       : {degraded}")
    if degraded and not recorded:
        print()
        print("Nothing new was recorded. Every attempt was refused, which at this")
        print("pacing means a daily quota rather than a per-minute one. Wait for the")
        print("reset, or pass --model with a different model from tools/check_llm.py.")
    print(budget.summary())
    print(store.stats())
    print(f"\nFixtures are in {FIXTURES}/. Commit them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
