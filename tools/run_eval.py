#!/usr/bin/env python3
"""Reproduce every number Counterfoil claims.

    python tools/run_eval.py                  # the headline batch
    python tools/run_eval.py --size 2000      # a bigger one
    python tools/run_eval.py --audit          # also write a verifiable ledger

Everything is seeded. Two runs of the same command produce the same figures.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from counterfoil.config import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from counterfoil.domain.events import Surface  # noqa: E402
from counterfoil.domain.money import Money  # noqa: E402
from counterfoil.eval import run_batch  # noqa: E402
from counterfoil.eval.sensitivity import run_across_seeds, run_sensitivity  # noqa: E402
from counterfoil.ledger import Ledger  # noqa: E402
from counterfoil.synth import BatchSpec  # noqa: E402

RULE = "=" * 78


def rs(paise: int) -> str:
    return Money(int(paise)).as_rupees_str


def default_diagnoser():
    """The recorded model answers, which are committed and cost nothing.

    The model layer is part of the product, so the default run has to include
    it or the published figures cannot be reproduced by the published command.
    Replaying fixtures needs no API key and no network.
    """
    from counterfoil.kernel.diagnose.llm import LLMDiagnoser
    from counterfoil.llm import Budget, FixtureStore

    return LLMDiagnoser(
        client=None,
        fixtures=FixtureStore(Path("llm_fixtures"), mode="replay"),
        budget=Budget(cap_usd=0.0),
    )


def headline(spec: BatchSpec, ledger: Ledger | None, diagnoser=None) -> None:
    report = run_batch(spec, ledger=ledger, diagnoser=diagnoser)

    print(RULE)
    noun = "mandate charges" if spec.surface is Surface.SUBSCRIPTIONS else "failed payments"
    print(f"COUNTERFOIL  |  {spec.size} {noun}, seed {spec.seed}")
    print(RULE)

    rows = (("control", report.control), ("naive", report.naive), ("agent", report.agent))
    print(f"{'arm':8s} {'gross':>13s} {'incremental':>13s} {'cost':>10s} "
          f"{'recovered':>10s} {'caused':>7s} {'actions':>8s} {'msgs':>6s} {'notices':>8s} {'breaches':>9s}")
    for name, arm in rows:
        print(f"{name:8s} {rs(arm.gross_recovered_paise):>13s} "
              f"{rs(report.incremental_paise(arm)):>13s} {rs(arm.direct_cost_paise):>10s} "
              f"{arm.n_recovered:>10d} {arm.n_attributable:>7d} {arm.actions:>8d} "
              f"{arm.discretionary_contacts:>6d} {arm.mandatory_notices:>8d} "
              f"{arm.total_violations:>9d}")

    lo, hi = report.bootstrap_incremental(report.agent)
    print()
    print(f"agent incremental recovery : Rs {rs(report.incremental_paise(report.agent))}")
    print(f"  95% CI (paired bootstrap): Rs {rs(lo)} to Rs {rs(hi)}")
    delta = report.incremental_paise(report.agent) - report.incremental_paise(report.naive)
    verdict = "ahead of" if delta >= 0 else "BEHIND"
    print(f"  vs naive                 : Rs {rs(abs(delta))} {verdict} the ungoverned arm")
    if delta < 0:
        print(f"  ...which it bought with {report.naive.total_violations} policy breaches")

    be = report.break_even_contact_cost_paise()
    print()
    if be == 0.0:
        print("break-even contact cost   : Rs 0.00")
        print("  The agent nets more than the naive arm before any goodwill cost is")
        print("  priced in at all, so the headline never depends on a churn estimate.")
    elif be is None:
        print("break-even contact cost   : not reachable on this surface")
        print("  The agent sends more discretionary messages than the naive arm, so no")
        print("  goodwill price makes restraint the cheaper option. Where the agent also")
        print("  recovers less, that gap is what obeying the rules costs, and the breach")
        print("  counts below are what the other arm spent to avoid paying it.")
    else:
        print(f"break-even contact cost   : Rs {be / 100:.2f} per unwanted message")

    print()
    print("what the naive arm broke to get its number:")
    for clause, n in report.naive.violations.most_common():
        print(f"    {n:5d}  {clause}")

    if report.agent.mandatory_notices:
        print()
        print(f"required notices sent      : {report.agent.mandatory_notices}")
        print("  Counted apart from discretionary messages. A pre-debit notice is")
        print("  obligatory, so scoring it as customer contact would make the arm that")
        print("  obeys the rule look noisier than the arm that skips it.")

    print()
    print("what the agent refused to do:")
    for clause, n in report.agent.refusals.most_common():
        print(f"    {n:5d}  {clause}")

    print()
    paths = Counter(
        r.diagnosis.path.value for r in report.agent.per_case if r.diagnosis
    )
    total = sum(paths.values()) or 1
    print("diagnosis path:")
    for label, key in (
        ("resolved by rules", "rule"),
        ("resolved by model", "llm"),
        ("withheld, nothing could resolve it", "degraded"),
    ):
        if paths.get(key):
            print(f"    {paths[key]:5d}  {paths[key] / total:6.1%}  {label}")
    print(f"    {report.agent.llm_calls} live model calls "
          f"(recorded answers replay for free)")


def model_contribution(spec: BatchSpec) -> None:
    """Did the model earn its place, or is it decoration?

    The rules-only agent is a complete, working product: it recovers money and
    escalates what it cannot read. The question this answers is whether adding
    a language model on top of it recovers anything the rules could not, which
    is a narrower and more honest claim than "we used AI".
    """
    from counterfoil.eval import measure_contribution
    from counterfoil.kernel.diagnose.llm import LLMDiagnoser
    from counterfoil.llm import Budget, FixtureStore

    store = FixtureStore(Path("llm_fixtures"), mode="replay")
    budget = Budget(cap_usd=0.0)
    diagnoser = LLMDiagnoser(client=None, fixtures=store, budget=budget)

    print()
    print(RULE)
    print("MODEL CONTRIBUTION  |  what the language model adds over rules alone")
    print(RULE)

    if store.hits == 0 and not any(Path("llm_fixtures").glob("*.json")):
        print("No fixtures recorded, so the model arm would degrade to escalation on")
        print("every ambiguous case. Run tools/record_fixtures.py --confirm first.")
        return

    c = measure_contribution(spec, diagnoser=diagnoser)

    print(f"{'rules only':22s} {rs(c.rules_only_paise):>13s}")
    print(f"{'rules plus model':22s} {rs(c.with_model_paise):>13s}"
          f"   +{rs(c.model_adds_paise)}")
    print(f"{'perfect diagnosis':22s} {rs(c.oracle_paise):>13s}"
          f"   +{rs(c.headroom_paise)} headroom over rules")
    print()
    print(f"The model captures {c.gap_closed:.1%} of the headroom that better diagnosis")
    print(f"could possibly reach, leaving Rs {rs(c.left_on_the_table_paise)} on the table.")
    print()
    print("The third row is an oracle reading the generator's held-back labels. No")
    print("real system can do that; it is here so the model's value is a rupee")
    print("figure against a ceiling rather than an accuracy percentage against")
    print("nothing.")
    print()
    print(store.stats())

    provenance = store.provenance()
    if len(provenance) > 1:
        print()
        print("recorded across more than one model, because the free tier caps a")
        print("single model at twenty requests a day:")
        for model, n in provenance.items():
            print(f"    {n:4d}  {model}")


def _escalations(arm) -> int:
    from counterfoil.domain.decision import Intervention

    return sum(
        1
        for r in arm.per_case
        if any(a.intervention is Intervention.ESCALATE_HUMAN for a in r.actions)
    )


def sensitivity(spec: BatchSpec) -> None:
    print()
    print(RULE)
    print("SENSITIVITY  |  does the conclusion survive its own assumptions?")
    print(RULE)
    for r in run_sensitivity(spec):
        verdict = "agent wins" if r.agent_wins else "AGENT LOSES"
        print(f"{r.variant.label:34s} agent={rs(r.agent_incremental_paise):>12s}  "
              f"naive={rs(r.naive_incremental_paise):>12s}  "
              f"delta={rs(r.delta_paise):>12s}   {verdict}")
        print(f"{'':34s} {r.variant.note}")

    print()
    print("The agent's advantage is a timing advantage. Knowing the cause matters")
    print("mostly because it tells you when to act, not what to do. Flatten the")
    print("timing curve and the careful arm loses, which is stated here rather than")
    print("left for a reader to discover.")


def across_seeds(size: int) -> None:
    print()
    print(RULE)
    print("SEED STABILITY  |  is the result luck?")
    print(RULE)
    for seed, agent, naive in run_across_seeds(size, (7, 11, 23, 42, 99, 2026)):
        print(f"seed {seed:<6d} agent={rs(agent):>12s}  naive={rs(naive):>12s}  "
              f"delta={rs(agent - naive):>12s}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--audit", action="store_true", help="write a verifiable ledger")
    parser.add_argument("--quick", action="store_true", help="skip sensitivity and seeds")
    parser.add_argument("--no-model", action="store_true",
                        help="rules only, to see what the model is worth")
    parser.add_argument("--model-contribution", action="store_true",
                        help="compare rules-only against rules plus model")
    parser.add_argument("--surface", default="payments",
                        choices=[s.value for s in Surface],
                        help="which loss surface to run")
    args = parser.parse_args()

    surface = Surface(args.surface)
    spec = BatchSpec(
        size=args.size,
        seed=args.seed,
        surface=surface,
        # A mandate book fails on a billing date, not across three days of
        # checkout traffic, so the arrival window is tighter.
        window_hours=48 if surface is Surface.SUBSCRIPTIONS else 72,
    )

    ledger = None
    if args.audit:
        path = Path("data/runs") / f"audit_{args.seed}.jsonl"
        if path.exists():
            path.unlink()
        ledger = Ledger(path, run_id=f"run_{args.seed}")

    diagnoser = None if args.no_model else default_diagnoser()
    headline(spec, ledger, diagnoser)

    if ledger:
        print()
        print(RULE)
        broken = ledger.verify()
        entries = sum(1 for _ in ledger.entries())
        print(f"AUDIT LEDGER  |  {entries} entries at {ledger.path}")
        print(f"  chain verification: {'INTACT' if broken is None else f'BROKEN at {broken.seq}: {broken.reason}'}")

    if args.model_contribution:
        model_contribution(spec)

    if not args.quick:
        sensitivity(BatchSpec(size=min(args.size, 500), seed=args.seed))
        across_seeds(min(args.size, 400))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
