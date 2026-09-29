"""Strict declarations supplied to the read-only inspector."""

from collections import deque
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(min_length=1)]
Capability = Literal["implement", "verify", "review", "publish"]
Status = Literal["succeeded", "needs_input", "blocked", "failed"]


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Policy(Declaration):
    authority_ref: Name
    policy_id: Name
    coordinator_id: Name
    allowed_scopes: list[Name]
    allowed_effects: list[Name]


class Action(Declaration):
    id: Name
    capability: Capability
    scope: Name
    effects: list[Name]
    depends_on: list[Name]
    required_evidence_kind: Literal["build", "test", "review", "integration", "other"]


class Outcome(Declaration):
    action_id: Name
    status: Status
    revision: Name
    environment: Name
    evidence_refs: list[Name]
    reason: Name | None = None
    affected_scope: Name | None = None
    owner: Name | None = None
    resume_condition: Name | None = None

    @model_validator(mode="after")
    def require_failure_reason(self) -> "Outcome":
        if self.status != "succeeded" and any(
            value is None
            for value in (self.reason, self.affected_scope, self.owner, self.resume_condition)
        ):
            raise ValueError(
                "non-success outcome requires reason, affected scope, owner, and resume condition"
            )
        return self


class TestCounts(Declaration):
    collected: int = Field(ge=0)
    executed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    approved_skips: int = Field(ge=0)


class Evidence(Declaration):
    id: Name
    kind: Literal["build", "test", "review", "integration", "other"]
    command: Name
    exit_code: int
    executed_at: datetime = Field(strict=False)
    revision: Name
    environment: Name
    result: Literal["passed", "failed"]
    counts: TestCounts | None = None

    @model_validator(mode="after")
    def validate_command_result(self) -> "Evidence":
        if (self.result == "passed") != (self.exit_code == 0):
            raise ValueError("evidence result contradicts command exit code")
        return self


class Criterion(Declaration):
    id: Name
    required: bool
    evidence_refs: list[Name]
    expected_evidence_kind: Literal["build", "test", "review", "integration", "other"]
    unresolved: bool


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label} IDs")


class Snapshot(Declaration):
    schema_version: Literal[1]
    revision: Name
    environment: Name
    authority_ref: Name
    policy_id: Name
    coordinator_id: Name
    actor_id: Name
    action_budget: int = Field(ge=0)
    actions_used: int = Field(ge=0)
    policy: Policy
    actions: list[Action]
    outcomes: list[Outcome]
    evidence: list[Evidence]
    criteria: list[Criterion]

    @model_validator(mode="after")
    def validate_graph_and_references(self) -> "Snapshot":
        action_ids = [item.id for item in self.actions]
        evidence_ids = [item.id for item in self.evidence]
        _unique(action_ids, "action")
        _unique(evidence_ids, "evidence")
        _unique([item.id for item in self.criteria], "criterion")
        _unique([item.action_id for item in self.outcomes], "outcome action")
        if not any(item.required for item in self.criteria):
            raise ValueError("at least one required criterion is needed")
        known_actions = set(action_ids)
        known_evidence = set(evidence_ids)
        for action in self.actions:
            _unique(action.depends_on, "dependency")
            if not set(action.depends_on) <= known_actions:
                raise ValueError(f"dangling dependency for action {action.id}")
        for outcome in self.outcomes:
            if outcome.action_id not in known_actions:
                raise ValueError(f"dangling outcome action {outcome.action_id}")
            if not set(outcome.evidence_refs) <= known_evidence:
                raise ValueError(f"dangling outcome evidence for {outcome.action_id}")
        for criterion in self.criteria:
            if not set(criterion.evidence_refs) <= known_evidence:
                raise ValueError(f"dangling criterion evidence for {criterion.id}")

        self.dependency_order()
        return self

    def dependency_order(self) -> list[Action]:
        """Return dependencies before dependents without using the Python call stack."""
        actions = {action.id: action for action in self.actions}
        remaining = {action.id: len(action.depends_on) for action in self.actions}
        dependents: dict[str, list[str]] = {action.id: [] for action in self.actions}
        for action in self.actions:
            for dependency in action.depends_on:
                dependents[dependency].append(action.id)

        ready = deque(action_id for action_id, count in remaining.items() if count == 0)
        ordered: list[Action] = []
        while ready:
            action_id = ready.popleft()
            ordered.append(actions[action_id])
            for dependent in dependents[action_id]:
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    ready.append(dependent)
        if len(ordered) != len(self.actions):
            raise ValueError("dependency cycle")
        return ordered
