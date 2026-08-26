"""The recovery loop: detect, diagnose, decide, act, observe, stop.

One case is played forward a step at a time. After every action the world is
consulted, the context is updated, and the next decision is made with what just
happened taken into account. That matters for more than realism: an agent that
submits a plan of five actions up front cannot honour a stopping rule, because
by the time the rule would fire the actions are already gone.

The naive arm runs through the same executor with the policy engine in shadow
mode: every clause is still evaluated and recorded, and then ignored. That is
what makes "the ungoverned version breaks these rules this many times" a
measured number rather than an assertion.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..domain.decision import (
    BILLABLE,
    CONTACTING,
    MANDATORY_NOTICE,
    Decision,
    Intervention,
    Proposal,
)
from ..domain.diagnosis import Diagnosis, DiagnosisPath, RootCause
from ..domain.money import Money
from ..domain.outcome import Arm, Outcome, OutcomeState
from ..ledger import Ledger, Stage
from ..synth.generator import MAX_DRAWS, LatentCase
from ..synth.world import TakenAction, attempt, spontaneous_probability
from .diagnose import rules
from .policy import PolicyEngine
from .propose import next_proposal

#: A hard ceiling independent of policy, so a misconfigured policy file cannot
#: produce an unbounded loop. Policy should always bite first.
ABSOLUTE_STEP_CEILING = 8


@dataclass
class CaseResult:
    event_id: str
    arm: Arm
    diagnosis: Diagnosis | None
    actions: list[TakenAction] = field(default_factory=list)
    outcome: Outcome | None = None
    attributable: bool = False
    would_have_recovered_anyway: bool = False
    closed_by: Intervention | None = None
    #: Clauses that blocked an action the arm took anyway. Empty for the agent
    #: by construction; the naive arm is measured by this.
    violations: list[str] = field(default_factory=list)
    #: Clauses that stopped the agent from acting. This is the agent's restraint
    #: made countable.
    refusals: list[str] = field(default_factory=list)
    #: Contacts the regulator requires, which are not discretionary and must not
    #: be compared against an arm that simply skips them.
    mandatory_notices: int = 0
    llm_calls: int = 0

    @property
    def recovered(self) -> Money:
        return self.outcome.recovered if self.outcome else Money.zero()

    @property
    def direct_cost(self) -> Money:
        return self.outcome.intervention_cost if self.outcome else Money.zero()

    @property
    def contacts(self) -> int:
        return self.outcome.contacts_made if self.outcome else 0

    @property
    def discretionary_contacts(self) -> int:
        """Messages the merchant chose to send.

        A pre-debit notice is not one. Counting it alongside a dunning reminder
        makes a compliant arm look noisier than one that breaks the rule, which
        is precisely backwards.
        """
        return max(0, self.contacts - self.mandatory_notices)


def _diagnose(event, diagnoser) -> tuple[Diagnosis, int]:
    """Rules first, model only for what rules refuse to answer."""
    resolved = rules.diagnose(event)
    if resolved is not None:
        return resolved, 0

    if diagnoser is None:
        # No model wired up. Withhold rather than guess: a degraded diagnosis
        # can only ever reach a human, which the policy engine enforces.
        return (
            Diagnosis(
                RootCause.UNKNOWN,
                0.0,
                DiagnosisPath.DEGRADED,
                "provider gave a generic code and no model is available to read it",
                evidence={
                    k: str(v) for k, v in event.provider_signals.items()
                    if k.startswith("error")
                },
            ),
            0,
        )

    return diagnoser(event), 1


def run_case(
    case: LatentCase,
    arm: Arm,
    *,
    engine: PolicyEngine,
    enforce: bool = True,
    diagnoser=None,
    ledger: Ledger | None = None,
    max_steps: int = ABSOLUTE_STEP_CEILING,
    plan=None,
) -> CaseResult:
    """Play one case forward under one arm.

    ``enforce=False`` puts the policy engine in shadow mode: clauses are still
    evaluated and recorded, but a blocked action executes anyway. That is the
    naive arm.

    ``plan`` overrides the proposer with a fixed sequence, which is how the
    naive arm expresses "retry immediately, three times, then text them".
    """
    event = case.event
    result = CaseResult(event_id=case.event_id, arm=arm, diagnosis=None)

    if ledger:
        ledger.append(
            event_id=case.event_id,
            arm=arm.value,
            stage=Stage.DETECTED,
            payload={
                "surface": event.surface.value,
                "kind": event.kind.value,
                "amount_paise": event.amount.paise,
                "customer": event.customer.redacted,
                "signals": {k: str(v) for k, v in event.provider_signals.items()},
            },
        )

    diagnosis, llm_calls = _diagnose(event, diagnoser)
    result.diagnosis = diagnosis
    result.llm_calls = llm_calls

    if ledger:
        ledger.append(
            event_id=case.event_id,
            arm=arm.value,
            stage=Stage.DIAGNOSED,
            payload={
                "cause": diagnosis.cause.value,
                "confidence": round(diagnosis.confidence, 3),
                "path": diagnosis.path.value,
                "rationale": diagnosis.rationale,
                "evidence": diagnosis.evidence,
            },
        )

    working = event
    contacts = 0
    spent = 0
    closed_by: Intervention | None = None
    last_at = event.occurred_at

    for step in range(min(max_steps, MAX_DRAWS)):
        proposal = (
            plan(working, diagnosis, step) if plan else next_proposal(working, diagnosis, step)
        )
        if proposal is None or proposal.intervention is Intervention.NO_ACTION:
            break

        decision = engine.evaluate(working, diagnosis, proposal)
        chosen: Proposal | None

        if decision.allowed:
            chosen = decision.effective
        elif enforce:
            substituted = decision.substituted_with
            result.refusals.extend(c.clause_id for c in decision.blocking_clauses)
            if substituted is None:
                _log_halt(ledger, case, arm, decision)
                break
            chosen = substituted
        else:
            # Shadow mode: record the breach, do it anyway.
            result.violations.extend(c.clause_id for c in decision.blocking_clauses)
            chosen = proposal

        if ledger:
            ledger.append(
                event_id=case.event_id,
                arm=arm.value,
                stage=Stage.DECIDED,
                payload={
                    "step": step + 1,
                    "proposed": proposal.intervention.value,
                    "proposed_by": proposal.proposed_by,
                    "allowed": decision.allowed,
                    "citation": decision.citation,
                    "clauses": [str(c) for c in decision.clauses],
                    "enforced": enforce,
                    "executing": chosen.intervention.value,
                },
            )

        action = TakenAction(
            chosen.intervention,
            chosen.scheduled_for or working.occurred_at,
            chosen.channel,
        )
        contacts_before = contacts
        spent += action.cost_paise
        if action.intervention in CONTACTING:
            contacts += 1
        if action.intervention in MANDATORY_NOTICE:
            result.mandatory_notices += 1
        if action.intervention is Intervention.ESCALATE_HUMAN and engine.capacity:
            # Consumed on execution rather than on approval, so the naive
            # arm running in shadow mode also draws down the same finite
            # team it is pretending not to have.
            engine.capacity.consume()
        last_at = action.at

        landed, probability = attempt(case, action, step, contacts_before)

        if ledger:
            ledger.append(
                event_id=case.event_id,
                arm=arm.value,
                stage=Stage.EXECUTED,
                payload={
                    "step": step + 1,
                    "intervention": action.intervention.value,
                    "channel": action.channel.value,
                    "at": action.at.isoformat(),
                    "cost_paise": action.cost_paise,
                    "landed": landed,
                },
            )

        result.actions.append(action)

        if landed:
            closed_by = action.intervention
            break

        updated = {
            **working.context,
            "attempts_so_far": working.attempts_so_far
            + (1 if action.intervention in BILLABLE else 0),
            "contacts_last_7d": working.contacts_last_7d
            + (1 if action.intervention in CONTACTING else 0),
            "actions_taken": int(working.context.get("actions_taken", 0)) + 1,
        }
        if action.intervention is Intervention.PRE_DEBIT_NOTICE:
            # Records that notice was given, which is what unlocks presenting
            # the mandate again. Written from the executed action rather than
            # the proposal, so a notice that was deferred out of quiet hours
            # moves the earliest lawful retry with it.
            updated["pre_debit_notice_at"] = action.at
        working = replace(working, context=updated)

    would_anyway = case.u_spontaneous < spontaneous_probability(case)
    recovered_now = closed_by is not None or would_anyway

    if recovered_now:
        state = OutcomeState.RECOVERED
        recovered = event.amount
        evidence = {
            "payment_id": f"pay_SYN{case.event_id[-8:]}",
            "source": "synthetic-world",
            "closed_by": closed_by.value if closed_by else "self_serve",
        }
    else:
        state = (
            OutcomeState.NOT_ATTEMPTED
            if not result.actions
            else OutcomeState.ESCALATED
            if result.actions[-1].intervention is Intervention.ESCALATE_HUMAN
            else OutcomeState.STILL_AT_RISK
        )
        recovered = Money.zero()
        evidence = {"source": "synthetic-world"}

    result.outcome = Outcome(
        event_id=case.event_id,
        arm=arm,
        state=state,
        observed_at=last_at,
        recovered=recovered,
        intervention_cost=Money(spent),
        contacts_made=contacts,
        evidence=evidence,
    )
    result.closed_by = closed_by
    result.would_have_recovered_anyway = would_anyway
    result.attributable = closed_by is not None and not would_anyway

    if ledger:
        ledger.append(
            event_id=case.event_id,
            arm=arm.value,
            stage=Stage.OBSERVED,
            payload={
                "state": state.value,
                "recovered_paise": recovered.paise,
                "cost_paise": spent,
                "contacts": contacts,
                "closed_by": closed_by.value if closed_by else None,
                "attributable": result.attributable,
                "evidence": evidence,
            },
        )

    return result


def _log_halt(ledger: Ledger | None, case: LatentCase, arm: Arm, decision: Decision) -> None:
    if not ledger:
        return
    ledger.append(
        event_id=case.event_id,
        arm=arm.value,
        stage=Stage.HALTED,
        payload={
            "citation": decision.citation,
            "blocked": [str(c) for c in decision.blocking_clauses],
            "proposed": decision.proposal.intervention.value,
        },
    )
