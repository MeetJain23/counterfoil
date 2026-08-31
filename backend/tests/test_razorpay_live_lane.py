"""The Razorpay adapter, tested without a network or a key.

The transport is injected and the payloads below are shaped like real ones, so
everything here exercises the code that will run against the sandbox. What it
cannot cover is whether Razorpay still returns this shape, which is what
`tools/live_lane.py` is for.
"""

import json
import urllib.error

import pytest

from counterfoil.domain.events import RiskKind, Surface
from counterfoil.surfaces.razorpay import (
    NotTestMode,
    RazorpayClient,
    RazorpayError,
    sign_webhook,
    to_risk_event,
    verify_webhook_signature,
)

#: Shaped after a real failed payment, contact details and all.
FAILED_PAYMENT = {
    "id": "pay_TWO9ClVpDftrlr",
    "entity": "payment",
    "amount": 49900,
    "currency": "INR",
    "status": "failed",
    "order_id": "order_TWO9ClVpDftrlr",
    "method": "card",
    "email": "arjun.mehta@gmail.com",
    "contact": "+919876543210",
    "created_at": 1787000000,
    "error_code": "BAD_REQUEST_ERROR",
    "error_description": "Your account does not have sufficient balance.",
    "error_source": "issuer",
    "error_step": "payment_authorization",
    "error_reason": "insufficient_funds",
}


def canned(payload, capture=None):
    def transport(url, method, headers, body, timeout):
        if capture is not None:
            capture.update({"url": url, "method": method, "headers": headers, "body": body})
        return payload

    return transport


def client(payload=None, capture=None, **kw):
    return RazorpayClient(
        key_id="rzp_test_FAKEKEYFORTESTS",
        key_secret="shhh",
        transport=canned(payload if payload is not None else {}, capture),
        **kw,
    )


# --------------------------------------------------------------------- #
# it cannot be pointed at live                                          #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["rzp_live_ABCDEFGHIJKL", "sk_test_something", ""])
def test_only_a_test_key_builds_a_client(key):
    with pytest.raises(NotTestMode):
        RazorpayClient(key_id=key, key_secret="x")


def test_a_test_key_builds_fine():
    assert client().key_id.startswith("rzp_test_")


def test_credentials_go_in_the_header_and_not_the_url():
    seen = {}
    client({"items": []}, seen).payments()
    assert "shhh" not in seen["url"]
    assert seen["headers"]["Authorization"].startswith("Basic ")


# --------------------------------------------------------------------- #
# reads and the one write                                               #
# --------------------------------------------------------------------- #


def test_payments_are_unwrapped_from_the_items_envelope():
    assert client({"count": 1, "items": [FAILED_PAYMENT]}).payments() == [FAILED_PAYMENT]


def test_an_empty_sandbox_is_not_an_error():
    assert client({"count": 0, "items": []}).payments() == []


def test_creating_an_order_sends_the_amount_in_paise():
    seen = {}
    client({"id": "order_x", "status": "created"}, seen).create_order(49900, "receipt-1")
    body = json.loads(seen["body"])
    assert seen["method"] == "POST"
    assert body == {"amount": 49900, "currency": "INR", "receipt": "receipt-1"}


def test_http_errors_become_razorpay_errors():
    def refused(url, method, headers, body, timeout):
        err = urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        err.read = lambda: b'{"error":{"description":"bad key"}}'
        raise err

    c = RazorpayClient(key_id="rzp_test_x", key_secret="y", transport=refused)
    with pytest.raises(RazorpayError, match="401"):
        c.payments()


def test_network_failures_become_razorpay_errors():
    def dead(url, method, headers, body, timeout):
        raise urllib.error.URLError("offline")

    c = RazorpayClient(key_id="rzp_test_x", key_secret="y", transport=dead)
    with pytest.raises(RazorpayError, match="could not reach"):
        c.payments()


# --------------------------------------------------------------------- #
# normalising a real payload, including the part that carries PII       #
# --------------------------------------------------------------------- #


def test_a_real_payment_becomes_the_same_event_the_kernel_understands():
    event = to_risk_event(FAILED_PAYMENT)
    assert event.surface is Surface.PAYMENTS
    assert event.kind is RiskKind.PAYMENT_FAILED
    assert event.event_id == "pay_TWO9ClVpDftrlr"
    assert event.amount.paise == 49900
    assert event.provider_signals["error_reason"] == "insufficient_funds"


def test_the_normaliser_strips_contact_details_at_the_boundary():
    """Real payloads carry a full phone number and email. Synthetic ones do not,
    so this is the first place that reduction actually matters."""
    event = to_risk_event(FAILED_PAYMENT)
    blob = json.dumps(
        {
            "customer": event.customer.redacted,
            "ref": event.customer.ref,
            "signals": event.provider_signals,
            "context": event.context,
        }
    )
    assert "9876543210" not in blob
    assert "arjun.mehta" not in blob
    assert event.customer.phone_last4 == "3210"
    assert event.customer.email_domain == "gmail.com"


def test_the_normalised_event_is_acceptable_to_the_ledger(tmp_path):
    """The end-to-end privacy property: a real payment can be audited safely."""
    from counterfoil.ledger import Ledger, Stage

    event = to_risk_event(FAILED_PAYMENT)
    ledger = Ledger(tmp_path / "audit.jsonl", run_id="live")
    ledger.append(
        event_id=event.event_id,
        stage=Stage.DETECTED,
        payload={
            "customer": event.customer.redacted,
            "amount_paise": event.amount.paise,
            "signals": event.provider_signals,
        },
    )
    assert ledger.verify() is None


def test_a_real_payment_is_diagnosable_by_the_existing_rule_table():
    """Nothing below the adapter needed changing to handle real data."""
    from counterfoil.domain.diagnosis import RootCause
    from counterfoil.kernel.diagnose import rules

    diagnosis = rules.diagnose(to_risk_event(FAILED_PAYMENT))
    assert diagnosis is not None
    assert diagnosis.cause is RootCause.INSUFFICIENT_FUNDS


def test_a_payment_with_no_contact_details_still_normalises():
    sparse = {"id": "pay_x", "amount": 1000, "created_at": 1787000000}
    event = to_risk_event(sparse)
    assert event.customer.phone_last4 is None
    assert event.customer.email_domain is None
    assert event.amount.paise == 1000


def test_a_payment_with_no_timestamp_still_normalises():
    event = to_risk_event({"id": "pay_x", "amount": 1000})
    assert event.occurred_at.tzinfo is not None


# --------------------------------------------------------------------- #
# webhook signatures                                                    #
# --------------------------------------------------------------------- #


SECRET = "whsec_counterfoil_test"
BODY = b'{"event":"payment.failed","payload":{"payment":{"entity":{"id":"pay_x"}}}}'


def test_a_correctly_signed_webhook_verifies():
    assert verify_webhook_signature(BODY, sign_webhook(BODY, SECRET), SECRET)


def test_a_tampered_body_does_not_verify():
    signature = sign_webhook(BODY, SECRET)
    assert not verify_webhook_signature(BODY.replace(b"pay_x", b"pay_y"), signature, SECRET)


def test_a_signature_from_the_wrong_secret_does_not_verify():
    assert not verify_webhook_signature(BODY, sign_webhook(BODY, "someone-elses"), SECRET)


@pytest.mark.parametrize("signature", ["", "deadbeef", "0" * 64])
def test_a_missing_or_junk_signature_does_not_verify(signature):
    assert not verify_webhook_signature(BODY, signature, SECRET)


def test_no_secret_configured_means_nothing_verifies():
    """Failing closed. An unconfigured webhook secret must not accept everything."""
    assert not verify_webhook_signature(BODY, sign_webhook(BODY, SECRET), "")


def test_reserialising_the_body_breaks_the_signature():
    """Why the raw bytes must be kept: json round-tripping changes them."""
    reserialised = json.dumps(json.loads(BODY)).encode()
    assert not verify_webhook_signature(reserialised, sign_webhook(BODY, SECRET), SECRET)
