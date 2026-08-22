"""API tests.

The dashboard is a demonstration surface, so the property that matters most is
that it is genuinely read-only: nothing reachable over HTTP can move money,
message anyone, or alter a record. The rest is making sure the numbers a viewer
sees are the same ones the eval computes.
"""

import pytest
from fastapi.testclient import TestClient

from counterfoil.api.main import app

client = TestClient(app)


@pytest.fixture(scope="module")
def run():
    return client.post("/api/runs?size=120&seed=2026").json()


# --------------------------------------------------------------------- #
# the surface is read-only                                              #
# --------------------------------------------------------------------- #


def test_the_only_write_verb_creates_a_simulation():
    """Nothing here reaches the outside world; POST /api/runs runs a seeded batch."""
    writes = [
        (route.path, sorted(route.methods - {"HEAD", "OPTIONS"}))
        for route in app.routes
        if getattr(route, "methods", None) and route.methods - {"GET", "HEAD", "OPTIONS"}
    ]
    assert writes == [("/api/runs", ["POST"])]


def test_health_reports_the_safety_posture():
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["dry_run"] is True
    assert body["llm_mode"] in {"replay", "record", "live"}


def test_the_page_is_served_and_self_contained():
    html = client.get("/").text
    assert "<title>Counterfoil</title>" in html
    # No CDN, no external anything: it has to work from a clean clone offline.
    for offender in ("http://", "https://", "cdn.", "unpkg", "jsdelivr"):
        assert offender not in html, offender


# --------------------------------------------------------------------- #
# runs                                                                  #
# --------------------------------------------------------------------- #


def test_a_run_returns_all_three_arms(run):
    assert {a["arm"] for a in run["arms"]} == {"control", "naive", "agent"}
    assert run["size"] == 120
    assert run["amount_at_risk_paise"] > 0


def test_the_api_agrees_with_the_eval(run):
    from counterfoil.eval import run_batch
    from counterfoil.synth import BatchSpec

    direct = run_batch(BatchSpec(size=120, seed=2026))
    agent = next(a for a in run["arms"] if a["arm"] == "agent")
    assert agent["gross_paise"] == direct.agent.gross_recovered_paise
    assert agent["incremental_paise"] == direct.incremental_paise(direct.agent)
    assert agent["violations"] == direct.agent.total_violations


def test_the_agent_reports_no_breaches_and_naive_reports_many(run):
    arms = {a["arm"]: a for a in run["arms"]}
    assert arms["agent"]["violations"] == 0
    assert arms["naive"]["violations"] > 0
    assert arms["control"]["actions"] == 0


def test_absurd_batch_sizes_are_refused():
    assert client.post("/api/runs?size=0").status_code == 422
    assert client.post("/api/runs?size=999999").status_code == 422


def test_an_unknown_run_is_a_404():
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404


def test_a_run_is_reproducible_over_http():
    a = client.post("/api/runs?size=80&seed=5").json()
    b = client.post("/api/runs?size=80&seed=5").json()
    assert a["arms"] == b["arms"]


# --------------------------------------------------------------------- #
# events and the audit trail                                            #
# --------------------------------------------------------------------- #


def test_events_can_be_filtered_by_outcome(run):
    all_rows = client.get(f"/api/runs/{run['run_id']}/events").json()
    recovered = client.get(f"/api/runs/{run['run_id']}/events?state=recovered").json()
    assert 0 < recovered["total"] < all_rows["total"]
    assert all(r["state"] == "recovered" for r in recovered["rows"])


def test_an_event_timeline_is_ordered_and_complete(run):
    rows = client.get(f"/api/runs/{run['run_id']}/events").json()["rows"]
    event_id = rows[0]["event_id"]
    detail = client.get(f"/api/runs/{run['run_id']}/events/{event_id}").json()

    stages = [e["stage"] for e in detail["timeline"]]
    assert stages[0] == "detected"
    assert "diagnosed" in stages
    assert stages[-1] == "observed"
    assert [e["seq"] for e in detail["timeline"]] == sorted(e["seq"] for e in detail["timeline"])


def test_every_decision_in_the_timeline_carries_its_clauses(run):
    rows = client.get(f"/api/runs/{run['run_id']}/events?state=recovered").json()["rows"]
    for row in rows[:15]:
        detail = client.get(f"/api/runs/{run['run_id']}/events/{row['event_id']}").json()
        for entry in detail["timeline"]:
            if entry["stage"] == "decided":
                assert entry["payload"]["clauses"]
                assert entry["payload"]["citation"]


def test_the_timeline_never_exposes_a_customer(run):
    """The ledger refuses PII at the write boundary; this checks the read side.

    Scanned over payload values rather than the raw response body: a 64
    character hex hash reliably contains a ten digit run bounded by letters,
    which matches a phone number pattern and means nothing.
    """
    import json
    import re

    phone = re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)")
    email = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

    rows = client.get(f"/api/runs/{run['run_id']}/events").json()["rows"]
    assert rows
    for row in rows[:20]:
        detail = client.get(f"/api/runs/{run['run_id']}/events/{row['event_id']}").json()
        for entry in detail["timeline"]:
            blob = json.dumps(entry["payload"])
            assert not phone.search(blob), entry
            assert not email.search(blob), entry
        # And the redacted form is what actually reaches the browser.
        detected = next(e for e in detail["timeline"] if e["stage"] == "detected")
        assert "****" in detected["payload"]["customer"]


def test_the_ledger_verifies_over_http(run):
    body = client.get(f"/api/runs/{run['run_id']}/ledger/verify").json()
    assert body["intact"] is True
    assert body["entries"] > run["size"]
    assert len(body["head"]) == 64


def test_a_missing_event_is_a_404(run):
    assert client.get(f"/api/runs/{run['run_id']}/events/evt_nope").status_code == 404


# --------------------------------------------------------------------- #
# the honest bits are served, not just computed                         #
# --------------------------------------------------------------------- #


def test_the_break_even_and_sweep_are_exposed(run):
    assert run["break_even_contact_cost_paise"] is not None
    assert len(run["sweep"]) > 5
    nets = [p["agent_net_paise"] for p in run["sweep"]]
    assert nets == sorted(nets, reverse=True)


def test_the_confidence_interval_brackets_the_estimate(run):
    lo, hi = run["confidence_interval_paise"]
    agent = next(a for a in run["arms"] if a["arm"] == "agent")
    assert lo < agent["incremental_paise"] < hi


def test_sensitivity_reports_the_variant_the_agent_loses(run):
    rows = client.get(f"/api/runs/{run['run_id']}/sensitivity").json()
    by_key = {r["key"]: r for r in rows}
    assert by_key["baseline"]["agent_wins"]
    assert not by_key["timing_barely_matters"]["agent_wins"]


def test_the_policy_is_served_in_full():
    """The agent's whole authority is readable from the browser."""
    body = client.get("/api/policy").json()
    assert body["version"] >= 1
    assert set(body["config"]) >= {"global", "retry", "contact", "receivables"}
    assert body["config"]["contact"]["quiet_hours_start_ist"] == 21


def test_diagnosis_paths_are_reported(run):
    assert sum(run["diagnosis_paths"].values()) == run["size"]
    assert "rule" in run["diagnosis_paths"]
