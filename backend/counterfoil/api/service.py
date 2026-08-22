"""Turning a run into something a browser can page through.

A batch is cheap to compute and expensive to keep re-computing per request, so
a run is executed once, held in memory under an id, and served from there. This
is a demonstration surface for a submission, not a multi-tenant service, and
pretending otherwise would add a database migration nobody needs to look at.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..domain.decision import Intervention
from ..domain.money import Money
from ..domain.outcome import OutcomeState
from ..eval.harness import run_batch
from ..eval.metrics import ArmResult, BatchReport
from ..eval.sensitivity import run_sensitivity
from ..kernel.policy import PolicyEngine
from ..ledger import Ledger
from ..synth.generator import BatchSpec, generate

#: Refuse absurd batch sizes rather than letting a URL parameter decide how
#: long the process blocks for.
MAX_BATCH = 5000


@dataclass
class Run:
    run_id: str
    spec: BatchSpec
    report: BatchReport
    ledger: Ledger
    created_at: datetime
    amount_at_risk_paise: int
    _by_event: dict[str, Any] = field(default_factory=dict)


class RunStore:
    """The last few runs, newest first. Deliberately bounded and in memory."""

    def __init__(self, keep: int = 5) -> None:
        self._runs: dict[str, Run] = {}
        self._order: list[str] = []
        self._keep = keep
        self._lock = threading.Lock()

    def add(self, run: Run) -> None:
        with self._lock:
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            while len(self._order) > self._keep:
                self._runs.pop(self._order.pop(0), None)

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def latest(self) -> Run | None:
        return self._runs.get(self._order[-1]) if self._order else None

    def ids(self) -> list[str]:
        return list(reversed(self._order))


def execute(spec: BatchSpec, *, ledger_dir, diagnoser=None) -> Run:
    if not 1 <= spec.size <= MAX_BATCH:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH}")

    run_id = f"run_{spec.seed}_{spec.size}"
    path = ledger_dir / f"{run_id}.jsonl"
    if path.exists():
        path.unlink()

    ledger = Ledger(path, run_id=run_id)
    report = run_batch(spec, engine=PolicyEngine(), diagnoser=diagnoser, ledger=ledger)
    at_risk = sum(c.event.amount.paise for c in generate(spec))

    return Run(
        run_id=run_id,
        spec=spec,
        report=report,
        ledger=ledger,
        created_at=datetime.now(timezone.utc),
        amount_at_risk_paise=at_risk,
    )


# --------------------------------------------------------------------- #
# serialisation                                                         #
# --------------------------------------------------------------------- #


def arm_summary(report: BatchReport, arm: ArmResult) -> dict[str, Any]:
    return {
        "arm": arm.arm.value,
        "gross_paise": arm.gross_recovered_paise,
        "incremental_paise": report.incremental_paise(arm),
        "cost_paise": arm.direct_cost_paise,
        "wasted_paise": arm.wasted_paise,
        "recovered": arm.n_recovered,
        "attributable": arm.n_attributable,
        "actions": arm.actions,
        "contacts": arm.contacts,
        "violations": arm.total_violations,
        "violation_detail": dict(arm.violations.most_common()),
        "refusal_detail": dict(arm.refusals.most_common()),
        "llm_calls": arm.llm_calls,
    }


def run_summary(run: Run) -> dict[str, Any]:
    report = run.report
    lo, hi = report.bootstrap_incremental(report.agent, iterations=800)
    break_even = report.break_even_contact_cost_paise()

    paths: dict[str, int] = {}
    for case in report.agent.per_case:
        if case.diagnosis:
            key = case.diagnosis.path.value
            paths[key] = paths.get(key, 0) + 1

    return {
        "run_id": run.run_id,
        "seed": run.spec.seed,
        "size": run.spec.size,
        "created_at": run.created_at.isoformat(),
        "amount_at_risk_paise": run.amount_at_risk_paise,
        "arms": [
            arm_summary(report, report.control),
            arm_summary(report, report.naive),
            arm_summary(report, report.agent),
        ],
        "confidence_interval_paise": [lo, hi],
        "break_even_contact_cost_paise": break_even,
        "sweep": [
            {"contact_cost_paise": c, "agent_net_paise": a, "naive_net_paise": n}
            for c, a, n in report.sweep(points=21)
        ],
        "diagnosis_paths": paths,
        "ledger_entries": sum(1 for _ in run.ledger.entries()),
        "ledger_intact": run.ledger.verify() is None,
    }


def event_rows(run: Run, *, arm: str = "agent", state: str | None = None,
               limit: int = 200, offset: int = 0) -> dict[str, Any]:
    source = {
        "control": run.report.control,
        "naive": run.report.naive,
        "agent": run.report.agent,
    }[arm]

    rows = []
    for case in source.per_case:
        if state and case.outcome and case.outcome.state.value != state:
            continue
        rows.append(
            {
                "event_id": case.event_id,
                "cause": case.diagnosis.cause.value if case.diagnosis else None,
                "confidence": round(case.diagnosis.confidence, 2) if case.diagnosis else None,
                "path": case.diagnosis.path.value if case.diagnosis else None,
                "state": case.outcome.state.value if case.outcome else None,
                "recovered_paise": case.recovered.paise,
                "cost_paise": case.direct_cost.paise,
                "actions": [a.intervention.value for a in case.actions],
                "closed_by": case.closed_by.value if case.closed_by else None,
                "attributable": case.attributable,
                "would_have_recovered_anyway": case.would_have_recovered_anyway,
                "refusals": case.refusals,
                "violations": case.violations,
            }
        )

    return {"total": len(rows), "rows": rows[offset : offset + limit]}


def event_detail(run: Run, event_id: str) -> dict[str, Any] | None:
    entries = run.ledger.timeline(event_id)
    if not entries:
        return None

    case = next(
        (c for c in run.report.agent.per_case if c.event_id == event_id), None
    )

    return {
        "event_id": event_id,
        "summary": {
            "cause": case.diagnosis.cause.value if case and case.diagnosis else None,
            "path": case.diagnosis.path.value if case and case.diagnosis else None,
            "state": case.outcome.state.value if case and case.outcome else None,
            "recovered_paise": case.recovered.paise if case else 0,
            "cost_paise": case.direct_cost.paise if case else 0,
            "attributable": case.attributable if case else False,
            "would_have_recovered_anyway": case.would_have_recovered_anyway if case else False,
        },
        "timeline": [
            {
                "seq": e.seq,
                "ts": e.ts,
                "arm": e.arm,
                "stage": e.stage,
                "payload": e.payload,
                "prev_hash": e.prev_hash,
                "entry_hash": e.entry_hash,
            }
            for e in entries
        ],
    }


def sensitivity_rows(spec: BatchSpec) -> list[dict[str, Any]]:
    capped = BatchSpec(size=min(spec.size, 400), seed=spec.seed)
    return [
        {
            "key": r.variant.key,
            "label": r.variant.label,
            "note": r.variant.note,
            "agent_paise": r.agent_incremental_paise,
            "naive_paise": r.naive_incremental_paise,
            "delta_paise": r.delta_paise,
            "agent_wins": r.agent_wins,
        }
        for r in run_sensitivity(capped)
    ]


def policy_view(engine: PolicyEngine) -> dict[str, Any]:
    """The agent's entire authority, as served to the browser."""
    return {"version": engine.version, "config": engine.cfg}


def rupees(paise: int) -> str:
    return Money(int(paise)).as_rupees_str


__all__ = [
    "MAX_BATCH",
    "Run",
    "RunStore",
    "arm_summary",
    "event_detail",
    "event_rows",
    "execute",
    "policy_view",
    "rupees",
    "run_summary",
    "sensitivity_rows",
]
