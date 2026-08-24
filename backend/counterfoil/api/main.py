"""The HTTP surface, and the single page that reads it.

Read-only by design. Every endpoint either runs a seeded simulation or reads
back what one produced, so there is nothing here that can move money, message a
customer, or mutate a record. That is not a limitation of the demo; it is the
same boundary the rest of the system enforces, expressed at the edge.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from ..config import load_settings
from ..kernel.policy import PolicyEngine
from ..domain.events import Surface
from . import service
from .service import MAX_BATCH, RunStore

STATIC = Path(__file__).parent / "static"
LEDGER_DIR = Path("data/runs")

app = FastAPI(
    title="Counterfoil",
    description="A bounded revenue recovery agent that proves what it recovered.",
    version="0.1.0",
)

store = RunStore()
engine = PolicyEngine()


def _run_or_404(run_id: str | None):
    run = store.get(run_id) if run_id else store.latest()
    if run is None:
        raise HTTPException(404, "no such run; POST /api/runs to create one")
    return run


@app.get("/api/health")
def health() -> dict:
    settings = load_settings()
    return {
        "status": "ok",
        "dry_run": settings.dry_run,
        "llm_provider": settings.llm_provider,
        "llm_mode": settings.llm_mode,
        "razorpay": "test-mode" if settings.has_razorpay else "not configured",
        "runs_held": len(store.ids()),
    }


@app.post("/api/runs")
def create_run(
    size: int = Query(1000, ge=1, le=MAX_BATCH),
    seed: int = Query(2026),
    surface: str = Query("payments", pattern="^(payments|subscriptions|receivables)$"),
    with_model: bool = Query(False, description="use recorded model fixtures"),
) -> dict:
    diagnoser = None
    if with_model:
        from ..llm.factory import build_diagnoser

        diagnoser = build_diagnoser(load_settings(), mode="replay")

    try:
        run = service.execute(
            service.spec_for(Surface(surface), size, seed),
            ledger_dir=LEDGER_DIR,
            diagnoser=diagnoser,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    store.add(run)
    return service.run_summary(run)


@app.get("/api/runs")
def list_runs() -> dict:
    return {"runs": store.ids()}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return service.run_summary(_run_or_404(run_id))


@app.get("/api/runs/{run_id}/events")
def get_events(
    run_id: str,
    arm: str = Query("agent", pattern="^(control|naive|agent)$"),
    state: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    return service.event_rows(
        _run_or_404(run_id), arm=arm, state=state, limit=limit, offset=offset
    )


@app.get("/api/runs/{run_id}/events/{event_id}")
def get_event(run_id: str, event_id: str) -> dict:
    detail = service.event_detail(_run_or_404(run_id), event_id)
    if detail is None:
        raise HTTPException(404, f"no ledger entries for {event_id}")
    return detail


@app.get("/api/runs/{run_id}/ledger/verify")
def verify_ledger(run_id: str) -> dict:
    run = _run_or_404(run_id)
    broken = run.ledger.verify()
    return {
        "run_id": run.run_id,
        "entries": sum(1 for _ in run.ledger.entries()),
        "intact": broken is None,
        "break": None if broken is None else {"seq": broken.seq, "reason": broken.reason},
        "head": run.ledger.head,
    }


@app.get("/api/runs/{run_id}/sensitivity")
def get_sensitivity(run_id: str) -> list[dict]:
    return service.sensitivity_rows(_run_or_404(run_id).spec)


@app.get("/api/policy")
def get_policy() -> dict:
    return service.policy_view(engine)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.ico")
def favicon() -> JSONResponse:
    return JSONResponse({}, status_code=204)
