# Siula

Flexible paths. Verifiable outcomes.

Named after Siula Grande, inspired by a trek through the Huayhuash range.

Siula's first slice inspects a declared delivery snapshot. It lists each action as `eligible`, `blocked`, or `completed`, gives block reasons, and reports unmet required criteria. It does not select or run an action. The host model remains the coordinator. The capability names `implement`, `verify`, `review`, and `publish` describe possible actions; this package does not implement them.

## Run

Use Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run siula inspect examples/snapshot.json
```

The equivalent typed Python API is:

```python
import json

from siula import Snapshot, inspect

with open("examples/snapshot.json", encoding="utf-8") as source:
    snapshot = Snapshot.model_validate(json.load(source))

report = inspect(snapshot)
print(report.actions["suite"].state, report.unmet_criteria)
```

## Declaration contract

The snapshot names one revision and environment. It includes an authority reference, policy identity, coordinator assignment, and action-count budget. Its policy lists exact allowed scopes and effects. Each action declares a known capability, one scope, effects, dependencies, and the evidence kind needed for a successful outcome. Dependencies form a directed graph. An action completes only when its outcome succeeds and references passing evidence from the current revision and environment. A blocked action blocks its dependents, while independent actions remain eligible.

Evidence records a command, exit code, execution time, revision, environment, result, and kind. Test evidence also needs collected, executed, skipped, and approved skip counts. A test record passes only when it executed tests, the counts reconcile, and every skip is declared approved. Other evidence kinds do not carry test counts. Each required criterion names its expected evidence kind and evidence references. Completion needs at least one required criterion, and every required criterion must be resolved with current passing evidence. A successful action alone cannot complete the snapshot.

The CLI reads one JSON file and writes a JSON report to standard output. Invalid input returns a structured JSON error on standard error and exit status 2. It never runs a command from the file, writes a checkpoint, contacts a service, or spawns a worker.

## Trust boundary

**This is advisory inspection of supplied declarations.** Siula does not authenticate an approval or policy, verify external evidence, enforce a sandbox, or inspect live Git, Linear, or GitHub state. Matching strings in a snapshot cannot grant real authority. The output carries this limit in `trust_boundary`. The action budget is a declared count check; it does not enforce time, cost, or retry limits. The caller must obtain and verify the declarations through its own trusted controls before any effect.
