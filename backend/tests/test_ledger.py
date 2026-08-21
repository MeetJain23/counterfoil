import json

import pytest

from counterfoil.ledger import ChainBreak, Ledger, PiiInLedgerError, Stage


def make_ledger(tmp_path, run_id="run_test"):
    return Ledger(tmp_path / "audit.jsonl", run_id=run_id)


def test_chain_verifies_when_intact(tmp_path):
    led = make_ledger(tmp_path)
    for i in range(5):
        led.append(event_id=f"evt_{i}", stage=Stage.DETECTED, payload={"i": i})
    assert led.verify() is None


def test_first_entry_links_to_genesis(tmp_path):
    led = make_ledger(tmp_path)
    entry = led.append(event_id="evt_0", stage=Stage.DETECTED, payload={})
    assert entry.prev_hash == "0" * 64
    assert entry.seq == 0


def test_editing_a_past_entry_breaks_the_chain(tmp_path):
    led = make_ledger(tmp_path)
    for i in range(4):
        led.append(event_id=f"evt_{i}", stage=Stage.DECIDED, payload={"amount_paise": 100 * i})
    assert led.verify() is None

    # Tamper: someone rewrites the recovered amount on entry 1.
    path = tmp_path / "audit.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["payload"]["amount_paise"] = 999_999
    path.write_text("\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows) + "\n", encoding="utf-8")

    break_ = Ledger(path, run_id="run_test").verify()
    assert isinstance(break_, ChainBreak)
    assert break_.seq == 1
    assert "hash" in break_.reason


def test_deleting_an_entry_breaks_the_chain(tmp_path):
    led = make_ledger(tmp_path)
    for i in range(4):
        led.append(event_id=f"evt_{i}", stage=Stage.DETECTED, payload={"i": i})
    path = tmp_path / "audit.jsonl"
    rows = path.read_text(encoding="utf-8").splitlines()
    del rows[2]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    break_ = Ledger(path, run_id="run_test").verify()
    assert isinstance(break_, ChainBreak)
    assert break_.seq == 3


def test_reopening_resumes_the_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = Ledger(path, run_id="run_a")
    first.append(event_id="evt_0", stage=Stage.DETECTED, payload={})
    head = first.head

    second = Ledger(path, run_id="run_b")
    entry = second.append(event_id="evt_1", stage=Stage.DETECTED, payload={})
    assert entry.prev_hash == head
    assert entry.seq == 1
    assert second.verify() is None


def test_timeline_filters_to_one_event(tmp_path):
    led = make_ledger(tmp_path)
    led.append(event_id="evt_a", stage=Stage.DETECTED, payload={})
    led.append(event_id="evt_b", stage=Stage.DETECTED, payload={})
    led.append(event_id="evt_a", stage=Stage.DECIDED, payload={})
    assert [e.stage for e in led.timeline("evt_a")] == ["detected", "decided"]


@pytest.mark.parametrize(
    "payload",
    [
        {"note": "customer email is arjun.mehta@gmail.com"},
        {"contact": "9876543210"},
        {"contact": "+91 9876543210"},
        {"instrument": "4111 1111 1111 1111"},
        {"nested": [{"deep": "reach me at ops@merchant.co.in"}]},
    ],
)
def test_ledger_refuses_to_record_pii(tmp_path, payload):
    led = make_ledger(tmp_path)
    with pytest.raises(PiiInLedgerError):
        led.append(event_id="evt_0", stage=Stage.OBSERVED, payload=payload)


def test_redacted_forms_are_accepted(tmp_path):
    led = make_ledger(tmp_path)
    entry = led.append(
        event_id="evt_0",
        stage=Stage.OBSERVED,
        payload={"customer": "cus_9 ph:****3210 em:***@gmail.com"},
    )
    assert entry.entry_hash
    assert led.verify() is None
