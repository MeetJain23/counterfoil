#!/usr/bin/env python3
"""Run Counterfoil against the real Razorpay test-mode API.

    python tools/live_lane.py

Everything the eval reports is synthetic. This is the separate, smaller lane
that shows the integration is real rather than modelled, and it is deliberately
kept apart: no figure produced here is ever added to a recovery number.

It proves four things and refuses to imply a fifth:

  1. authentication against the real API
  2. reading real objects out of a real sandbox
  3. writing one, an order, which moves no money on its own
  4. that a real payload normalises into the same RiskEvent the kernel already
     handles, with contact details reduced at the boundary

It cannot prove a recovery rate. A failed payment needs a customer at a checkout
page entering a test card, which is not reachable from a script, so the
recovery figures stay synthetic and stay in the other lane.

Requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env. The client refuses to
build against anything but an rzp_test_ key.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from counterfoil.config import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from counterfoil.config import load_settings  # noqa: E402
from counterfoil.domain.money import Money  # noqa: E402
from counterfoil.kernel.diagnose import rules  # noqa: E402
from counterfoil.ledger import Ledger, Stage  # noqa: E402
from counterfoil.surfaces.razorpay import (  # noqa: E402
    NotTestMode,
    RazorpayClient,
    RazorpayError,
    sign_webhook,
    to_risk_event,
    verify_webhook_signature,
)

RULE = "=" * 78
LEDGER = Path("data/runs/live_lane.jsonl")


def ok(label: str, detail: str = "") -> None:
    print(f"  [ok]   {label}" + (f"  {detail}" if detail else ""))


def no(label: str, detail: str = "") -> None:
    print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=2, help="test orders to create")
    args = parser.parse_args()

    settings = load_settings()
    print(RULE)
    print("COUNTERFOIL LIVE LANE  |  real Razorpay test mode")
    print(RULE)

    if not settings.has_razorpay:
        print("\nRAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not both set in .env.")
        return 2

    try:
        client = RazorpayClient(settings.razorpay_key_id, settings.razorpay_key_secret)
    except NotTestMode as exc:
        no("refuses non-test credentials", str(exc))
        return 2

    print(f"\nkey: {settings.razorpay_key_id[:13]}... (test mode)\n")

    failures = 0

    # 1. read ----------------------------------------------------------- #
    print("reading the sandbox")
    try:
        payments = client.payments(count=25)
        ok("GET /payments", f"{len(payments)} payment(s)")
        orders = client.orders(count=25)
        ok("GET /orders", f"{len(orders)} order(s)")
    except RazorpayError as exc:
        no("read", str(exc))
        return 1

    # 2. write ---------------------------------------------------------- #
    print("\ncreating test orders (an order is an intent, not a charge)")
    created = []
    for i in range(args.orders):
        try:
            order = client.create_order(
                amount_paise=49900 + i * 10000,
                receipt=f"counterfoil-{int(time.time())}-{i}",
            )
            created.append(order)
            ok(order["id"], f"{Money(order['amount'])} · {order['status']}")
        except RazorpayError as exc:
            no("POST /orders", str(exc))
            failures += 1

    # 3. normalise ------------------------------------------------------ #
    print("\nnormalising real payloads into the kernel's RiskEvent")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    if LEDGER.exists():
        LEDGER.unlink()
    ledger = Ledger(LEDGER, run_id="live_lane")

    if not payments:
        print("     no payments in this sandbox yet, which is expected on a fresh")
        print("     account: a payment requires a customer at a checkout page, and")
        print("     cannot be created from a script. Normalisation is covered by")
        print("     backend/tests/test_razorpay_live_lane.py against real-shaped")
        print("     payloads instead.")
    for payment in payments:
        event = to_risk_event(payment)
        diagnosis = rules.diagnose(event)
        ledger.append(
            event_id=event.event_id,
            stage=Stage.DETECTED,
            payload={
                "customer": event.customer.redacted,
                "amount_paise": event.amount.paise,
                "signals": event.provider_signals,
                "source": "razorpay-test-mode",
            },
        )
        ok(
            event.event_id,
            f"{event.amount} · "
            + (f"diagnosed {diagnosis.cause.value}" if diagnosis else "deferred to model"),
        )

    if payments:
        broken = ledger.verify()
        (ok if broken is None else no)(
            "audit ledger of real payments verifies",
            "" if broken is None else str(broken),
        )
        if broken:
            failures += 1

    # 4. webhook signature ---------------------------------------------- #
    print("\nwebhook signature verification")
    secret = settings.razorpay_webhook_secret or "whsec_local_sample"
    body = json.dumps(
        {"event": "payment.failed", "payload": {"payment": {"entity": {"id": "pay_sample"}}}}
    ).encode()
    good = sign_webhook(body, secret)

    checks = [
        ("a correctly signed body is accepted", verify_webhook_signature(body, good, secret)),
        ("a tampered body is rejected",
         not verify_webhook_signature(body.replace(b"pay_sample", b"pay_evil"), good, secret)),
        ("a signature from another secret is rejected",
         not verify_webhook_signature(body, sign_webhook(body, "wrong"), secret)),
        ("an unconfigured secret rejects everything",
         not verify_webhook_signature(body, good, "")),
    ]
    for label, passed in checks:
        (ok if passed else no)(label)
        failures += 0 if passed else 1

    if not settings.razorpay_webhook_secret:
        print("     using a local sample secret; set RAZORPAY_WEBHOOK_SECRET to")
        print("     verify against the one Razorpay actually signs with.")

    # summary ------------------------------------------------------------ #
    print()
    print(RULE)
    print("what this lane established")
    print(RULE)
    print("  authenticated against the real API      yes")
    print(f"  real objects read                       {len(payments)} payments, {len(orders)} orders")
    print(f"  real objects created                    {len(created)} orders")
    print(f"  payloads normalised and audited         {len(payments)}")
    print(f"  webhook signature checks passed         {sum(1 for _, p in checks if p)}/4")
    print()
    print("  what it does NOT establish: any recovery figure. Those stay in the")
    print("  synthetic lane and the two are never combined.")

    if failures:
        print(f"\n{failures} check(s) failed.")
        return 1
    print("\nLive lane clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
