"""The live lane: real Razorpay test-mode API, kept apart from the synthetic world.

What this exists to prove is narrow and worth stating precisely, because the
temptation is to imply more.

It proves the integration is real: that Counterfoil authenticates against the
actual API, creates and reads real objects in a real sandbox, normalises real
provider payloads into the same ``RiskEvent`` the kernel already understands,
and verifies webhook signatures the way Razorpay actually signs them.

It does **not** prove any recovery figure. A failed payment cannot be produced
from the server side; it needs a customer at a checkout page entering a test
card. So the recovery numbers in the README stay synthetic, this lane stays
separate, and the two are never added together.

Two safety properties hold here that do not hold in the synthetic world, because
here the payloads are real:

* **Nothing is ever charged.** The client refuses to construct itself against
  anything but an ``rzp_test_`` key, and the only write it performs is creating
  an order, which moves no money on its own.
* **Real payloads carry real contact details.** ``email`` and ``contact`` arrive
  populated, and ``to_risk_event`` reduces them to a domain and last four digits
  before anything downstream sees them. That is FAILURES 001 applied to data
  that actually has something to leak.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..domain.events import Customer, RiskEvent, RiskKind, Surface
from ..domain.money import Money

API = "https://api.razorpay.com/v1"


class RazorpayError(RuntimeError):
    """Anything that stopped us getting a usable answer from the API."""


class NotTestMode(RuntimeError):
    """Raised rather than ever touching a live key."""


def _http(url: str, method: str, headers: dict, body: bytes | None, timeout: float) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


@dataclass
class RazorpayClient:
    """Stdlib only, and injectable, so the tests need no network and no key."""

    key_id: str
    key_secret: str
    timeout: float = 25.0
    transport: Callable[[str, str, dict, bytes | None, float], dict] | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.key_id.startswith("rzp_test_"):
            raise NotTestMode(
                f"refusing to build a client for {self.key_id[:9]!r}. Counterfoil "
                "only ever talks to test mode."
            )

    @property
    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.key_id}:{self.key_secret}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path))
        send = self.transport or _http
        payload = json.dumps(body).encode() if body is not None else None
        try:
            return send(f"{API}{path}", method, self._headers, payload, self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RazorpayError(f"{exc.code} from Razorpay: {detail}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise RazorpayError(f"could not reach Razorpay: {exc}") from exc

    # --- reads ---------------------------------------------------------- #

    def payments(self, count: int = 25) -> list[dict[str, Any]]:
        return self._call("GET", f"/payments?count={count}").get("items", [])

    def payment(self, payment_id: str) -> dict[str, Any]:
        return self._call("GET", f"/payments/{payment_id}")

    def orders(self, count: int = 25) -> list[dict[str, Any]]:
        return self._call("GET", f"/orders?count={count}").get("items", [])

    # --- the one write, and it moves nothing ---------------------------- #

    def create_order(self, amount_paise: int, receipt: str) -> dict[str, Any]:
        """Create a test order. An order is an intent, not a charge."""
        return self._call(
            "POST",
            "/orders",
            {"amount": amount_paise, "currency": "INR", "receipt": receipt},
        )


# --------------------------------------------------------------------- #
# normalising a real payload                                            #
# --------------------------------------------------------------------- #

#: Fields worth carrying from a real payment. Everything else in the payload is
#: either irrelevant to diagnosis or is contact detail we do not want.
SIGNAL_FIELDS = (
    "error_code",
    "error_reason",
    "error_source",
    "error_step",
    "error_description",
    "method",
)


def to_risk_event(payment: dict[str, Any]) -> RiskEvent:
    """Turn a real Razorpay payment into the same RiskEvent the kernel uses.

    This is the whole point of the adapter pattern: below this function nothing
    knows whether a case came from the sandbox or the generator, so the policy
    engine, the ledger and the eval all work unchanged on real data.
    """
    contact = str(payment.get("contact") or "")
    email = str(payment.get("email") or "")

    created = payment.get("created_at")
    occurred_at = (
        datetime.fromtimestamp(int(created), tz=UTC)
        if created
        else datetime.now(UTC)
    )

    return RiskEvent(
        event_id=str(payment.get("id") or "pay_unknown"),
        surface=Surface.PAYMENTS,
        kind=RiskKind.PAYMENT_FAILED,
        occurred_at=occurred_at,
        amount=Money(int(payment.get("amount") or 0)),
        customer=Customer(
            # Razorpay has no stable customer ref on a bare payment, so the
            # order is the closest thing to one.
            ref=str(payment.get("order_id") or payment.get("id") or "unknown"),
            # Reduced here, at the boundary, so nothing downstream ever holds
            # a full number or address.
            phone_last4=contact[-4:] if len(contact) >= 4 else None,
            email_domain=email.split("@", 1)[1] if "@" in email else None,
        ),
        provider_signals={
            k: str(payment[k]) for k in SIGNAL_FIELDS if payment.get(k) is not None
        },
        context={"attempts_so_far": 0, "contacts_last_7d": 0, "actions_taken": 0,
                 "source": "razorpay-test-mode"},
    )


# --------------------------------------------------------------------- #
# webhooks                                                              #
# --------------------------------------------------------------------- #


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Exactly how Razorpay signs a webhook: HMAC-SHA256 over the raw body.

    Compared with compare_digest rather than ==, because a timing-variable
    comparison on a signature is a real weakness and a free one to avoid. The
    raw body must be the bytes as received: re-serialising the parsed JSON
    changes key order and whitespace, and the signature stops matching for
    reasons that look like an attack.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign_webhook(raw_body: bytes, secret: str) -> str:
    """The same computation, for tests and for generating a sample locally."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
