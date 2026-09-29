"""Advisory inspection of supplied actions and acceptance evidence."""

from dataclasses import dataclass

from siula.model import Evidence, Snapshot


@dataclass(frozen=True)
class ActionInspection:
    state: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Inspection:
    actions: dict[str, ActionInspection]
    complete: bool
    unmet_criteria: tuple[str, ...]


def _valid_evidence(item: Evidence, snapshot: Snapshot) -> bool:
    if (
        item.revision != snapshot.revision
        or item.environment != snapshot.environment
        or item.result != "passed"
    ):
        return False
    if item.kind != "test":
        return item.counts is None
    counts = item.counts
    if counts is None:
        return False
    return (
        counts.collected > 0
        and counts.executed > 0
        and counts.collected == counts.executed + counts.skipped
        and counts.skipped == counts.approved_skips
    )


def inspect(snapshot: Snapshot) -> Inspection:
    """Inspect declarations only; this function performs no effects or authentication."""
    evidence = {item.id: item for item in snapshot.evidence}
    outcomes = {item.action_id: item for item in snapshot.outcomes}
    actions: dict[str, ActionInspection] = {}
    for action in snapshot.dependency_order():
        reasons: list[str] = []
        policy = snapshot.policy
        if snapshot.authority_ref != policy.authority_ref:
            reasons.append("authority_mismatch")
        if snapshot.policy_id != policy.policy_id:
            reasons.append("policy_mismatch")
        if (
            snapshot.coordinator_id != policy.coordinator_id
            or snapshot.actor_id != policy.coordinator_id
        ):
            reasons.append("coordinator_mismatch")
        if action.scope not in policy.allowed_scopes:
            reasons.append("scope_not_allowed")
        if not set(action.effects) <= set(policy.allowed_effects):
            reasons.append("effect_not_allowed")
        for dependency in action.depends_on:
            if actions[dependency].state != "completed":
                reasons.append(f"dependency {dependency} not completed with current evidence")

        outcome = outcomes.get(action.id)
        if outcome is not None:
            if outcome.revision != snapshot.revision or outcome.environment != snapshot.environment:
                reasons.append("stale_outcome")
            elif outcome.status == "succeeded":
                if not outcome.evidence_refs or not all(
                    _valid_evidence(evidence[ref], snapshot) for ref in outcome.evidence_refs
                ):
                    reasons.append("invalid_outcome_evidence")
                elif not any(
                    evidence[ref].kind == action.required_evidence_kind
                    for ref in outcome.evidence_refs
                ):
                    reasons.append("missing_required_evidence_kind")
                elif not reasons:
                    result = ActionInspection("completed", ())
                    actions[action.id] = result
                    continue
            else:
                reasons.append(f"outcome_{outcome.status}: {outcome.reason}")

        if outcome is None and snapshot.actions_used >= snapshot.action_budget:
            reasons.append("budget_exhausted")
        result = ActionInspection("blocked" if reasons else "eligible", tuple(reasons))
        actions[action.id] = result

    unmet = tuple(
        criterion.id
        for criterion in snapshot.criteria
        if criterion.required
        and (
            criterion.unresolved
            or not criterion.evidence_refs
            or not all(
                _valid_evidence(evidence[ref], snapshot)
                and evidence[ref].kind == criterion.expected_evidence_kind
                for ref in criterion.evidence_refs
            )
        )
    )
    return Inspection(actions=actions, complete=not unmet, unmet_criteria=unmet)
