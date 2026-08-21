#!/usr/bin/env python3
"""Reproduce every number Counterfoil claims.

    python tools/run_eval.py                  # the headline batch
    python tools/run_eval.py --size 2000      # a bigger one
    python tools/run_eval.py --audit          # also write a verifiable ledger

Everything is seeded. Two runs of the same command produce the same figures.
"""

from __future__ import annotations

import argparse
from collections import Counter
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from counterfoil.domain.money import Money  # noqa: E402
from counterfoil.eval import run_batch  # noqa: E402
from counterfoil.eval.sensitivity import run_across_seeds, run_sensitivity  # noqa: E402
from counterfoil.ledger import Ledger  # noqa: E402
from counterfoil.synth import BatchSpec  # noqa: E402

RULE = "=" * 78


def rs(paise: int) -> str:
    return Money(int(paise)).as_rupees_str


def headline(spec: BatchSpec, ledger: Ledger | None) -> None:
    report = run_batch(spec, ledger=ledger)

    print(RULE)
    print(f"COUNTERFOIL  |  {spec.size} failed payments, seed {spec.seed}")
    print(RULE)

    rows = (("control", report.control), ("naive", report.naive), ("agent", report.agent))
    print(f"{'arm':8s} {'gross':>13s} {'incremental':>13s} {'cost':>10s} "
          f"{'recovered':>10s} {'caused':>7s} {'actions':>8s} {'contacts':>9s} {'breaches':>9s}")
    for name, arm in rows:
        print(f"{name:8s} {rs(arm.gross_recovered_paise):>13s} "
              f"{rs(report.incremental_paise(arm)):>13s} {rs(arm.direct_cost_paise):>10s} "
              f"{arm.n_recovered:>10d} {arm.n_attributable:>7d} {arm.actions:>8d} "
              f"{arm.contacts:>9d} {arm.total_violations:>9d}")

    lo, hi = report.bootstrap_incremental(report.agent)
    print()
    print(f"agent incremental recovery : Rs {rs(report.incremental_paise(report.agent))}")
    print(f"  95% CI (paired bootstrap): Rs {rs(lo)} to Rs {rs(hi)}")
    print(f"  vs naive                 : Rs {rs(report.incremental_paise(report.agent) - report.incremental_paise(report.naive))} better")

    be = report.break_even_contact_cost_paise()
    print()
    if be == 0.0:
        print("break-even contact cost   : Rs 0.00")
        print("  The agent nets more than the naive arm before any goodwill cost is")
        print("  priced in at all, so the headline never depends on a churn estimate.")
    elif be is None:
        print("break-even contact cost   : not reachable (the agent contacts more people)")
    else:
        print(f"break-even contact cost   : Rs {be / 100:.2f} per unwanted message")

    print()
    print("what the naive arm broke to get its number:")
    for clause, n in report.naive.violations.most_common():
        print(f"    {n:5d}  {clause}")

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
        ("withheld, no model wired yet", "degraded"),
    ):
        if paths.get(key):
            print(f"    {paths[key]:5d}  {paths[key] / total:6.1%}  {label}")
    print(f"    {report.agent.llm_calls} model calls made, "
          f"{report.agent.llm_calls * 0.0003:.4f} USD estimated")


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
    args = parser.parse_args()

    spec = BatchSpec(size=args.size, seed=args.seed)

    ledger = None
    if args.audit:
        path = Path("data/runs") / f"audit_{args.seed}.jsonl"
        if path.exists():
            path.unlink()
        ledger = Ledger(path, run_id=f"run_{args.seed}")

    headline(spec, ledger)

    if ledger:
        print()
        print(RULE)
        broken = ledger.verify()
        entries = sum(1 for _ in ledger.entries())
        print(f"AUDIT LEDGER  |  {entries} entries at {ledger.path}")
        print(f"  chain verification: {'INTACT' if broken is None else f'BROKEN at {broken.seq}: {broken.reason}'}")

    if not args.quick:
        sensitivity(BatchSpec(size=min(args.size, 500), seed=args.seed))
        across_seeds(min(args.size, 400))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
