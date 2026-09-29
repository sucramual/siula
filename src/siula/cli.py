"""Read one JSON snapshot and print its advisory inspection."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError

from siula.evaluator import inspect
from siula.model import Snapshot


def main() -> int:
    parser = argparse.ArgumentParser(prog="siula")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect", help="inspect a declared snapshot")
    inspect_parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    try:
        raw = args.snapshot.read_text(encoding="utf-8")
        snapshot = Snapshot.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            details: list[dict[str, object]] = [
                dict(item) for item in exc.errors(include_url=False, include_context=False)
            ]
        else:
            details = [{"message": str(exc)}]
        print(json.dumps({"error": "invalid_snapshot", "details": details}), file=sys.stderr)
        return 2

    report = inspect(snapshot)
    output = asdict(report)
    output["trust_boundary"] = {
        "declared_only": True,
        "authority_authenticated": False,
        "external_evidence_verified": False,
        "effects_enforced": False,
    }
    print(json.dumps(output, indent=2))
    return 0
