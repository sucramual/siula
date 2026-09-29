import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from siula import Snapshot, inspect


def snapshot() -> dict:
    return {
        "schema_version": 1,
        "revision": "abc123",
        "environment": "local",
        "authority_ref": "decision-1",
        "policy_id": "policy-v1",
        "coordinator_id": "agent-a",
        "actor_id": "agent-a",
        "action_budget": 4,
        "actions_used": 0,
        "policy": {
            "authority_ref": "decision-1",
            "policy_id": "policy-v1",
            "coordinator_id": "agent-a",
            "allowed_scopes": ["package", "docs"],
            "allowed_effects": ["local_write"],
        },
        "actions": [
            {
                "id": "build",
                "capability": "verify",
                "scope": "package",
                "effects": [],
                "depends_on": [],
                "required_evidence_kind": "build",
            },
            {
                "id": "tests",
                "capability": "verify",
                "scope": "package",
                "effects": [],
                "depends_on": ["build"],
                "required_evidence_kind": "test",
            },
            {
                "id": "docs",
                "capability": "implement",
                "scope": "docs",
                "effects": ["local_write"],
                "depends_on": [],
                "required_evidence_kind": "other",
            },
        ],
        "outcomes": [],
        "evidence": [],
        "criteria": [
            {
                "id": "suite",
                "required": True,
                "expected_evidence_kind": "test",
                "evidence_refs": [],
                "unresolved": True,
            },
            {
                "id": "review",
                "required": True,
                "expected_evidence_kind": "review",
                "evidence_refs": [],
                "unresolved": True,
            },
        ],
    }


def evidence(kind="build", revision="abc123", counts=None):
    item = {
        "id": "e-build",
        "kind": kind,
        "command": "uv build",
        "exit_code": 0,
        "executed_at": "2026-09-28T12:00:00Z",
        "revision": revision,
        "environment": "local",
        "result": "passed",
    }
    if counts is not None:
        item["counts"] = counts
    return item


def run_cli(tmp_path: Path, data: dict) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data))
    executable = Path(sys.executable).parent / "siula"
    return subprocess.run(
        [str(executable), "inspect", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def deep_chain(length: int = 1100) -> dict:
    data = snapshot()
    data["actions"] = [
        {
            "id": f"action-{index}",
            "capability": "verify",
            "scope": "package",
            "effects": [],
            "depends_on": [f"action-{index + 1}"] if index + 1 < length else [],
            "required_evidence_kind": "other",
        }
        for index in range(length)
    ]
    data["action_budget"] = length
    return data


def test_cli_success_and_no_embedded_command_execution(tmp_path: Path):
    data = snapshot()
    marker = tmp_path / "must-not-exist"
    data["evidence"] = [evidence() | {"command": f"touch {marker}"}]
    data["outcomes"] = [
        {
            "action_id": "build",
            "status": "succeeded",
            "revision": "abc123",
            "environment": "local",
            "evidence_refs": ["e-build"],
        }
    ]
    result = run_cli(tmp_path, data)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["actions"]["build"]["state"] == "completed"
    assert output["actions"]["tests"]["state"] == "eligible"
    assert output["complete"] is False
    assert output["trust_boundary"]["declared_only"] is True
    assert not marker.exists()


def test_cli_scoped_blocker_leaves_independent_action(tmp_path: Path):
    data = snapshot()
    data["outcomes"] = [
        {
            "action_id": "build",
            "status": "blocked",
            "reason": "build service unavailable",
            "affected_scope": "package",
            "owner": "agent-a",
            "resume_condition": "build service restored",
            "revision": "abc123",
            "environment": "local",
            "evidence_refs": [],
        }
    ]
    result = run_cli(tmp_path, data)
    assert result.returncode == 0, result.stderr
    actions = json.loads(result.stdout)["actions"]
    assert actions["build"]["state"] == "blocked"
    assert "dependency build" in actions["tests"]["reasons"][0]
    assert actions["docs"]["state"] == "eligible"


def test_cli_malformed_graph_is_structured_error(tmp_path: Path):
    data = snapshot()
    data["actions"][0]["depends_on"] = ["tests"]
    result = run_cli(tmp_path, data)
    assert result.returncode != 0
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["error"] == "invalid_snapshot"
    assert "cycle" in str(error["details"]).lower()
    assert "Traceback" not in result.stderr


def test_cli_stale_evidence_blocks_dependency(tmp_path: Path):
    data = snapshot()
    data["evidence"] = [evidence(revision="old")]
    data["outcomes"] = [
        {
            "action_id": "build",
            "status": "succeeded",
            "revision": "abc123",
            "environment": "local",
            "evidence_refs": ["e-build"],
        }
    ]
    result = run_cli(tmp_path, data)
    assert result.returncode == 0
    actions = json.loads(result.stdout)["actions"]
    assert actions["build"]["state"] == "blocked"
    assert actions["tests"]["state"] == "blocked"
    assert actions["docs"]["state"] == "eligible"


def test_cli_accepts_deep_reverse_ordered_chain(tmp_path: Path):
    result = run_cli(tmp_path, deep_chain())
    assert result.returncode == 0, result.stderr
    actions = json.loads(result.stdout)["actions"]
    assert len(actions) == 1100
    assert actions["action-1099"]["state"] == "eligible"
    assert actions["action-0"]["state"] == "blocked"
    assert "dependency action-1" in actions["action-0"]["reasons"][0]


def test_cli_rejects_deep_cycle_without_traceback(tmp_path: Path):
    data = deep_chain()
    data["actions"][-1]["depends_on"] = ["action-0"]
    result = run_cli(tmp_path, data)
    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["error"] == "invalid_snapshot"
    assert "cycle" in str(error["details"]).lower()
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["actions"][0].update(capability="deploy"),
        lambda d: d["actions"].append(d["actions"][0].copy()),
        lambda d: d["actions"][0].update(depends_on=["missing"]),
        lambda d: d["outcomes"].append(
            {
                "action_id": "missing",
                "status": "succeeded",
                "revision": "abc123",
                "environment": "local",
                "evidence_refs": [],
            }
        ),
        lambda d: d["criteria"].clear(),
        lambda d: d["outcomes"].append(
            {
                "action_id": "build",
                "status": "blocked",
                "reason": "missing service",
                "revision": "abc123",
                "environment": "local",
                "evidence_refs": [],
            }
        ),
        lambda d: d["evidence"].append(evidence() | {"exit_code": 1}),
    ],
)
def test_invalid_contracts_rejected(change):
    data = snapshot()
    change(data)
    with pytest.raises(ValidationError):
        Snapshot.model_validate(data)


@pytest.mark.parametrize(
    "counts",
    [
        {"collected": 3, "executed": 4, "skipped": 0, "approved_skips": 0},
        {"collected": 3, "executed": 2, "skipped": 1, "approved_skips": 0},
        {"collected": 0, "executed": 0, "skipped": 0, "approved_skips": 0},
    ],
)
def test_bad_test_counts_do_not_complete(counts):
    data = snapshot()
    data["evidence"] = [evidence(kind="test", counts=counts)]
    data["criteria"] = [
        {
            "id": "suite",
            "required": True,
            "expected_evidence_kind": "test",
            "evidence_refs": ["e-build"],
            "unresolved": False,
        }
    ]
    result = inspect(Snapshot.model_validate(data))
    assert result.complete is False
    assert "suite" in result.unmet_criteria


def test_valid_evidence_and_resolved_criteria_complete():
    data = snapshot()
    data["evidence"] = [
        evidence(
            kind="test", counts={"collected": 3, "executed": 3, "skipped": 0, "approved_skips": 0}
        )
    ]
    data["criteria"] = [
        {
            "id": "suite",
            "required": True,
            "expected_evidence_kind": "test",
            "evidence_refs": ["e-build"],
            "unresolved": False,
        }
    ]
    result = inspect(Snapshot.model_validate(data))
    assert result.complete is True


def test_wrong_evidence_kind_cannot_satisfy_review():
    data = snapshot()
    data["evidence"] = [evidence(kind="build")]
    data["criteria"] = [
        {
            "id": "review",
            "required": True,
            "expected_evidence_kind": "review",
            "evidence_refs": ["e-build"],
            "unresolved": False,
        }
    ]
    result = inspect(Snapshot.model_validate(data))
    assert result.unmet_criteria == ("review",)


def test_completed_build_stays_completed_after_budget_is_used():
    data = snapshot()
    data["actions_used"] = 4
    data["evidence"] = [evidence()]
    data["outcomes"] = [
        {
            "action_id": "build",
            "status": "succeeded",
            "revision": "abc123",
            "environment": "local",
            "evidence_refs": ["e-build"],
        }
    ]
    result = inspect(Snapshot.model_validate(data))
    assert result.actions["build"].state == "completed"
    assert "budget_exhausted" in result.actions["tests"].reasons


def test_authority_and_budget_fail_closed():
    data = snapshot()
    data["actor_id"] = "agent-b"
    data["actions_used"] = 4
    data["actions"][2]["scope"] = "other"
    result = inspect(Snapshot.model_validate(data))
    assert all(action.state == "blocked" for action in result.actions.values())
    assert "coordinator_mismatch" in result.actions["build"].reasons
    assert "budget_exhausted" in result.actions["build"].reasons
    assert "scope_not_allowed" in result.actions["docs"].reasons
