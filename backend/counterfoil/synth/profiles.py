"""The behavioural model behind the synthetic world.

Read this before believing any number Counterfoil reports on synthetic data.

An eval on generated data can only be as honest as the process that generated
it, so that process lives here, in the open, as explicit numbers rather than
buried inside a generator function. Two properties matter:

**Spontaneous recovery is non-zero.** Customers retry failed payments on their
own. Invoices get paid because someone finally looked at the inbox. A recovery
agent that takes credit for these is measuring its own existence, not its
effect, and it is the single easiest way to produce an impressive and
meaningless number. Every cause below therefore carries a ``spontaneous``
rate, and the control arm exists to subtract it.

**Interventions are not uniformly useful.** Retrying an expired card succeeds
zero percent of the time no matter how many times you try. Retrying an
insufficient-funds failure works reasonably well, but only after the balance
has had time to change. Nudging someone who has already ignored two messages
does close to nothing. Encoding this is what makes the naive arm lose.

These figures are informed estimates, not measurements. They are deliberately
conservative about how much an agent can help. Nothing here is a claim about
real Razorpay recovery rates; what the eval demonstrates is that the system
measures its own effect correctly, and the live sandbox lane is what shows the
integration is real. Both claims are kept separate on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.diagnosis import RootCause
from ..domain.events import Surface


@dataclass(frozen=True)
class CauseBehaviour:
    """How one root cause responds to time, retries and contact."""

    #: p(money arrives with no intervention at all, inside the observation window)
    spontaneous: float
    #: p(a retry on the same instrument succeeds), once any waiting period is met
    retry_success: float
    #: Hours before a retry is worth attempting. Retrying earlier scales the
    #: success rate down sharply rather than to zero.
    retry_ripens_after_hours: float
    #: p(success) when moved to a different payment method or rail
    alt_rail_success: float
    #: Additive lift on the *next* action from having contacted the customer
    nudge_lift: float
    #: Nothing on the same instrument can ever work
    terminal: bool = False


#: Payment-surface behaviour. Ordered roughly from most to least recoverable.
PAYMENT_BEHAVIOUR: dict[RootCause, CauseBehaviour] = {
    # A gateway blip. Retrying shortly after works most of the time, and many
    # customers retry themselves within minutes.
    RootCause.TECHNICAL_GATEWAY: CauseBehaviour(
        spontaneous=0.34, retry_success=0.72, retry_ripens_after_hours=0.1,
        alt_rail_success=0.66, nudge_lift=0.05,
    ),
    # The bank is down. Retrying during the outage is near-worthless; retrying
    # after it clears is excellent. This is the cause where timing is the whole
    # game, and where the naive arm burns its attempts.
    RootCause.BANK_DOWNTIME: CauseBehaviour(
        spontaneous=0.28, retry_success=0.78, retry_ripens_after_hours=1.5,
        alt_rail_success=0.71, nudge_lift=0.03,
    ),
    # The balance has to change before anything can succeed. Salary cycles, not
    # exponential backoff.
    RootCause.INSUFFICIENT_FUNDS: CauseBehaviour(
        spontaneous=0.19, retry_success=0.46, retry_ripens_after_hours=24.0,
        alt_rail_success=0.24, nudge_lift=0.14,
    ),
    # Customer bailed at the OTP screen. Retrying the charge does nothing --
    # they have to come back. This is a contact problem, not a retry problem.
    RootCause.AUTHENTICATION_DROPOFF: CauseBehaviour(
        spontaneous=0.22, retry_success=0.08, retry_ripens_after_hours=0.0,
        alt_rail_success=0.31, nudge_lift=0.27,
    ),
    RootCause.CUSTOMER_ABANDONED: CauseBehaviour(
        spontaneous=0.15, retry_success=0.05, retry_ripens_after_hours=0.0,
        alt_rail_success=0.22, nudge_lift=0.24,
    ),
    # A soft decline: risk engine said no this time, might say yes later.
    RootCause.ISSUER_DECLINE_SOFT: CauseBehaviour(
        spontaneous=0.16, retry_success=0.33, retry_ripens_after_hours=6.0,
        alt_rail_success=0.48, nudge_lift=0.09,
    ),
    # --- terminal: the instrument itself is the problem ---
    RootCause.ISSUER_DECLINE_HARD: CauseBehaviour(
        spontaneous=0.04, retry_success=0.0, retry_ripens_after_hours=0.0,
        alt_rail_success=0.39, nudge_lift=0.21, terminal=True,
    ),
    RootCause.EXPIRED_INSTRUMENT: CauseBehaviour(
        spontaneous=0.06, retry_success=0.0, retry_ripens_after_hours=0.0,
        alt_rail_success=0.44, nudge_lift=0.29, terminal=True,
    ),
    RootCause.INTERNATIONAL_BLOCKED: CauseBehaviour(
        spontaneous=0.05, retry_success=0.0, retry_ripens_after_hours=0.0,
        alt_rail_success=0.35, nudge_lift=0.18, terminal=True,
    ),
}

#: How often each cause occurs on the payments surface.
PAYMENT_CAUSE_MIX: dict[RootCause, float] = {
    RootCause.INSUFFICIENT_FUNDS: 0.22,
    RootCause.AUTHENTICATION_DROPOFF: 0.20,
    RootCause.ISSUER_DECLINE_SOFT: 0.14,
    RootCause.BANK_DOWNTIME: 0.12,
    RootCause.TECHNICAL_GATEWAY: 0.10,
    RootCause.ISSUER_DECLINE_HARD: 0.09,
    RootCause.EXPIRED_INSTRUMENT: 0.06,
    RootCause.CUSTOMER_ABANDONED: 0.04,
    RootCause.INTERNATIONAL_BLOCKED: 0.03,
}


@dataclass(frozen=True)
class SegmentProfile:
    """Customer segments differ in how reachable and how reliable they are."""

    name: str
    weight: float
    #: Multiplier on spontaneous recovery
    self_serve: float
    #: Multiplier on the lift any contact produces
    responsiveness: float


SEGMENTS: tuple[SegmentProfile, ...] = (
    SegmentProfile("engaged", 0.34, self_serve=1.35, responsiveness=1.30),
    SegmentProfile("casual", 0.41, self_serve=1.00, responsiveness=1.00),
    SegmentProfile("lapsing", 0.18, self_serve=0.62, responsiveness=0.55),
    SegmentProfile("dormant", 0.07, self_serve=0.28, responsiveness=0.22),
)


# --------------------------------------------------------------------------- #
# Provider signal catalogue                                                    #
# --------------------------------------------------------------------------- #
#
# Modelled on Razorpay's documented error shape: error_code, error_reason,
# error_source, error_step and a human-readable error_description.
#
# TODO(live-lane): reconcile these against real sandbox failures once test keys
# are wired up, and record any divergence in FAILURES.md rather than silently
# editing the table.


@dataclass(frozen=True)
class SignalTemplate:
    error_code: str
    error_reason: str
    error_source: str
    error_step: str
    descriptions: tuple[str, ...] = field(default_factory=tuple)


#: Unambiguous failures: the provider tells you exactly what happened, and a
#: rule table resolves them for free. This is the majority of real traffic, and
#: the reason spending model tokens on every event would be wasteful.
CLEAR_SIGNALS: dict[RootCause, SignalTemplate] = {
    RootCause.INSUFFICIENT_FUNDS: SignalTemplate(
        "BAD_REQUEST_ERROR", "insufficient_funds", "issuer", "payment_authorization",
        ("Your account does not have sufficient balance to complete this transaction.",),
    ),
    RootCause.EXPIRED_INSTRUMENT: SignalTemplate(
        "BAD_REQUEST_ERROR", "card_expired", "issuer", "payment_authentication",
        ("The card used has expired.",),
    ),
    RootCause.ISSUER_DECLINE_HARD: SignalTemplate(
        "BAD_REQUEST_ERROR", "card_blocked", "issuer", "payment_authorization",
        ("The card has been blocked by the issuing bank.",),
    ),
    RootCause.ISSUER_DECLINE_SOFT: SignalTemplate(
        "BAD_REQUEST_ERROR", "payment_declined_by_issuer", "issuer", "payment_authorization",
        ("The payment was declined by the issuing bank.",),
    ),
    RootCause.INTERNATIONAL_BLOCKED: SignalTemplate(
        "BAD_REQUEST_ERROR", "international_transaction_not_allowed", "issuer",
        "payment_authorization",
        ("International transactions are not enabled on this card.",),
    ),
    RootCause.BANK_DOWNTIME: SignalTemplate(
        "GATEWAY_ERROR", "bank_technical_error", "bank", "payment_authorization",
        ("The bank is currently unable to process this request.",),
    ),
    RootCause.TECHNICAL_GATEWAY: SignalTemplate(
        "GATEWAY_ERROR", "gateway_technical_error", "gateway", "payment_initiation",
        ("The payment gateway encountered a temporary error.",),
    ),
    RootCause.AUTHENTICATION_DROPOFF: SignalTemplate(
        "BAD_REQUEST_ERROR", "payment_authentication_failed", "customer",
        "payment_authentication",
        ("Authentication was not completed.",),
    ),
    RootCause.CUSTOMER_ABANDONED: SignalTemplate(
        "BAD_REQUEST_ERROR", "payment_cancelled_by_customer", "customer",
        "payment_authentication",
        ("The payment was cancelled by the customer.",),
    ),
}

#: Failures where the provider gives you a generic code and a sentence of prose.
#: A rule table cannot resolve these, and guessing a cause from an unmapped code
#: is exactly how a recovery agent ends up retrying a dead card twelve times.
#: These are the cases that justify a language model, and the share of the batch
#: that lands here is what the reported LLM-usage percentage measures.
AMBIGUOUS_DESCRIPTIONS: dict[RootCause, tuple[str, ...]] = {
    RootCause.INSUFFICIENT_FUNDS: (
        "Transaction could not be completed. Please check with your bank and try again.",
        "Payment unsuccessful - limit or balance issue reported by the issuer.",
    ),
    RootCause.BANK_DOWNTIME: (
        "Unable to reach the issuing bank at this time.",
        "Upstream did not respond within the expected window.",
        "The remitter bank declined to process the request right now.",
    ),
    RootCause.AUTHENTICATION_DROPOFF: (
        "The session ended before verification finished.",
        "OTP page was closed before submission.",
        "Customer did not complete the additional verification step.",
    ),
    RootCause.ISSUER_DECLINE_SOFT: (
        "Payment was not approved. No further detail was provided by the issuer.",
        "The transaction was refused; the issuer did not supply a reason code.",
    ),
    RootCause.ISSUER_DECLINE_HARD: (
        "The instrument cannot be used for this transaction.",
        "Issuer permanently refused this instrument.",
    ),
    RootCause.TECHNICAL_GATEWAY: (
        "Something went wrong while processing the payment.",
        "The request failed at the processor and was not retried.",
    ),
}

#: Fraction of generated events that carry an ambiguous signal instead of a
#: clear one. Set from the observation that most real failures are well-coded
#: and a meaningful minority are not.
AMBIGUOUS_RATE: float = 0.18

#: Cost of an intervention, in paise. Retries cost gateway fees; messages cost
#: per-send. Both are charged against recovered revenue so the net number is
#: honest rather than flattering.
INTERVENTION_COST_PAISE: dict[str, int] = {
    "retry_same_rail": 250,
    "retry_alternate_rail": 250,
    "customer_nudge": 18,
    "send_payment_link": 18,
    "request_updated_instrument": 18,
    "mandate_reauth": 45,
    "invoice_reminder": 18,
    "escalate_human": 4000,   # a human minute is the most expensive thing here
    "no_action": 0,
}

SURFACE_MIX: dict[Surface, dict[RootCause, float]] = {
    Surface.PAYMENTS: PAYMENT_CAUSE_MIX,
}

SURFACE_BEHAVIOUR: dict[Surface, dict[RootCause, CauseBehaviour]] = {
    Surface.PAYMENTS: PAYMENT_BEHAVIOUR,
}
