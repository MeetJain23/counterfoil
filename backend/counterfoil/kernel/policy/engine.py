"""The policy engine: the only thing in Counterfoil with authority to act.

Diagnosis (whether by rule or by model) produces a Proposal. A Proposal is
inert. This engine evaluates it against every applicable clause and returns a
Decision that carries, in writing, the clauses that permitted or blocked it.

Design constraints:
  * No clause may consult an LLM. Policy must be deterministic and replayable.
  * A blocked contact is rescheduled where possible rather than dropped, so
    quiet hours delay revenue rather than forfeit it.
  * A degraded diagnosis can only ever reach a human.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from ...domain.decision import (
    BILLABLE,
    CONTACTING,
    ClauseEval,
    Decision,
    Intervention,
    Proposal,
)
from ...domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from ...domain.events import RiskEvent

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_POLICY_PATH = Path(__file__).with_name("policies.yaml")

#: Interventions that are always permissible: they neither spend money nor
#: contact a customer, so no clause needs to gate them.
ALWAYS_SAFE = frozenset({Intervention.NO_ACTION, Intervention.ESCALATE_HUMAN})


class PolicyEngine:
    def __init__(self, config: dict[str, Any] | None = None, path: Path | None = None):
        if config is None:
            config = yaml.safe_load((path or DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
        self.cfg = config
        self.version = config.get("version", 0)

    # ------------------------------------------------------------------ #
    # clauses: each returns None when it does not apply to this proposal  #
    # ------------------------------------------------------------------ #

    def _c_degraded_reaches_human_only(self, ev, dx, pr) -> ClauseEval | None:
        if dx.path is not DiagnosisPath.DEGRADED:
            return None
        ok = pr.intervention in ALWAYS_SAFE
        return ClauseEval(
            "safety.degraded_diagnosis",
            ok,
            "degraded diagnosis routed to a human"
            if ok
            else "diagnosis is degraded; only escalation or no-action is permitted",
        )

    def _c_confidence_floor(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention in ALWAYS_SAFE:
            return None
        floor = float(self.cfg["global"]["min_confidence"])
        return ClauseEval(
            "global.confidence_floor",
            dx.confidence >= floor,
            f"confidence {dx.confidence:.2f} vs floor {floor:.2f}",
        )

    def _c_value_floor(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention in ALWAYS_SAFE:
            return None
        floor = int(self.cfg["global"]["min_recoverable_paise"])
        ok = ev.amount.paise >= floor
        return ClauseEval(
            "global.value_floor",
            ok,
            f"at-risk {ev.amount} vs floor Rs {floor / 100:,.2f}: "
            + ("worth pursuing" if ok else "recovery would cost more than it returns"),
        )

    def _c_total_action_cap(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention in ALWAYS_SAFE:
            return None
        cap = int(self.cfg["global"]["max_total_actions"])
        used = int(ev.context.get("actions_taken", 0))
        return ClauseEval(
            "global.max_total_actions",
            used < cap,
            f"{used} of {cap} lifetime actions used",
        )

    def _c_retry_attempts(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention not in BILLABLE:
            return None
        cap = int(self.cfg["retry"]["max_attempts"][ev.surface.value])
        return ClauseEval(
            "retry.max_attempts",
            ev.attempts_so_far < cap,
            f"{ev.attempts_so_far} of {cap} retries used on {ev.surface.value}",
        )

    def _c_retry_not_terminal(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention is not Intervention.RETRY_SAME_RAIL:
            return None
        ok = not dx.is_terminal
        return ClauseEval(
            "retry.not_terminal_cause",
            ok,
            f"cause {dx.cause.value} "
            + ("can succeed on retry" if ok else "can never succeed on the same instrument"),
        )

    def _c_bank_outage_hold(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention not in BILLABLE or dx.cause is not RootCause.BANK_DOWNTIME:
            return None
        hold = int(self.cfg["retry"]["bank_outage_hold_minutes"])
        when = pr.scheduled_for or ev.occurred_at
        elapsed = (when - ev.occurred_at).total_seconds() / 60.0
        return ClauseEval(
            "retry.bank_outage_hold",
            elapsed >= hold,
            f"retry scheduled {elapsed:.0f}min after outage, hold is {hold}min",
        )

    def _c_insufficient_funds_delay(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention not in BILLABLE or dx.cause is not RootCause.INSUFFICIENT_FUNDS:
            return None
        hours = int(self.cfg["retry"]["insufficient_funds_retry_after_hours"])
        when = pr.scheduled_for or ev.occurred_at
        elapsed = (when - ev.occurred_at).total_seconds() / 3600.0
        return ClauseEval(
            "retry.insufficient_funds_delay",
            elapsed >= hours,
            f"retry {elapsed:.1f}h after failure, balance needs {hours}h to change",
        )

    def _c_contact_frequency(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention not in CONTACTING:
            return None
        cap = int(self.cfg["contact"]["max_per_7d"])
        return ClauseEval(
            "contact.frequency_cap",
            ev.contacts_last_7d < cap,
            f"{ev.contacts_last_7d} of {cap} contacts used in trailing 7 days",
        )

    def _c_quiet_hours(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention not in CONTACTING:
            return None
        when = (pr.scheduled_for or ev.occurred_at).astimezone(IST)
        start = int(self.cfg["contact"]["quiet_hours_start_ist"])
        end = int(self.cfg["contact"]["quiet_hours_end_ist"])
        in_quiet = when.hour >= start or when.hour < end
        window = f"{start:02d}:00-{end:02d}:00"
        return ClauseEval(
            "contact.quiet_hours",
            not in_quiet,
            f"{when:%H:%M} IST "
            + (f"falls inside quiet hours {window}" if in_quiet else f"is outside quiet hours {window}"),
        )

    def _c_channel_allowed(self, ev, dx, pr) -> ClauseEval | None:
        if pr.intervention not in CONTACTING:
            return None
        allowed = set(self.cfg["contact"]["allowed_channels"])
        ok = pr.channel.value in allowed
        return ClauseEval(
            "contact.channel_allowed",
            ok,
            f"channel {pr.channel.value} " + ("permitted" if ok else f"not in {sorted(allowed)}"),
        )

    def _c_no_dunning_when_disputed(self, ev, dx, pr) -> ClauseEval | None:
        if not self.cfg["receivables"].get("block_dunning_when_disputed", True):
            return None
        if pr.intervention not in CONTACTING:
            return None
        ok = dx.cause is not RootCause.INVOICE_DISPUTED
        return ClauseEval(
            "receivables.no_dunning_when_disputed",
            ok,
            "invoice is not disputed"
            if ok
            else "invoice is disputed; this is a support matter, not a collections one",
        )

    CLAUSES = (
        "_c_degraded_reaches_human_only",
        "_c_confidence_floor",
        "_c_value_floor",
        "_c_total_action_cap",
        "_c_retry_attempts",
        "_c_retry_not_terminal",
        "_c_bank_outage_hold",
        "_c_insufficient_funds_delay",
        "_c_contact_frequency",
        "_c_quiet_hours",
        "_c_channel_allowed",
        "_c_no_dunning_when_disputed",
    )

    # ------------------------------------------------------------------ #
    # evaluation                                                          #
    # ------------------------------------------------------------------ #

    def evaluate(self, event: RiskEvent, diagnosis: Diagnosis, proposal: Proposal) -> Decision:
        clauses: list[ClauseEval] = []
        for name in self.CLAUSES:
            result = getattr(self, name)(event, diagnosis, proposal)
            if result is not None:
                clauses.append(result)

        if not clauses:
            clauses.append(
                ClauseEval(
                    "global.no_gate_applies",
                    True,
                    f"{proposal.intervention.value} neither spends money nor contacts anyone",
                )
            )

        if all(c.passed for c in clauses):
            return Decision(proposal, True, clauses)

        return Decision(
            proposal,
            False,
            clauses,
            substituted_with=self._substitute(event, clauses, proposal),
        )

    def _substitute(
        self, event: RiskEvent, clauses: list[ClauseEval], proposal: Proposal
    ) -> Proposal | None:
        """Recover what can be recovered from a blocked proposal.

        Quiet hours are a timing problem, not a permission problem: the correct
        response is to send it when the customer is awake, not to give up on
        the money.
        """
        blocked = {c.clause_id for c in clauses if not c.passed}
        if blocked != {"contact.quiet_hours"}:
            return None

        when = (proposal.scheduled_for or event.occurred_at).astimezone(IST)
        start = int(self.cfg["contact"]["quiet_hours_start_ist"])
        end = int(self.cfg["contact"]["quiet_hours_end_ist"])
        target = when.replace(hour=end, minute=0, second=0, microsecond=0)
        if when.hour >= start:
            target += timedelta(days=1)
        return Proposal(
            proposal.intervention,
            scheduled_for=target.astimezone(timezone.utc),
            channel=proposal.channel,
            message_hint=proposal.message_hint,
            params={**proposal.params, "deferred_for": "quiet_hours"},
            proposed_by="policy:substitution",
        )
