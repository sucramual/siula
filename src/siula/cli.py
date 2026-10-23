"""Command-line entry point: inspect snapshots and run the pieces of a review loop."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError

from siula.evaluator import inspect
from siula.model import Snapshot
from siula.review import poster
from siula.review.brief import BriefError, build_brief
from siula.review.findings import parse_findings


def _fail(error: str, details: list[dict[str, object]]) -> None:
    print(json.dumps({"error": error, "details": details}), file=sys.stderr)


def _details(exc: Exception) -> list[dict[str, object]]:
    if isinstance(exc, ValidationError):
        return [dict(item) for item in exc.errors(include_url=False, include_context=False)]
    return [{"message": str(exc)}]


def _inspect(args: argparse.Namespace) -> int:
    try:
        raw = args.snapshot.read_text(encoding="utf-8")
        snapshot = Snapshot.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        _fail("invalid_snapshot", _details(exc))
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


def _read(path: Path) -> str:
    """Read a UTF-8 file exactly, without newline translation."""
    return path.read_bytes().decode("utf-8")


def _write(data: bytes) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _check_findings(args: argparse.Namespace) -> int:
    try:
        findings = parse_findings(_read(args.findings))
    except (OSError, UnicodeError, ValidationError) as exc:
        _fail("invalid_findings", _details(exc))
        return 2
    print(json.dumps([finding.model_dump() for finding in findings], indent=2))
    return 0


def _review_brief(args: argparse.Namespace) -> int:
    try:
        brief = build_brief(_read(args.diff_file), _read(args.intent_file))
    except (OSError, UnicodeError, BriefError) as exc:
        _fail("invalid_input", _details(exc))
        return 2
    _write(brief.encode("utf-8"))
    return 0


def _post_reply(args: argparse.Namespace) -> int:
    try:
        path = poster.api_path(args.repo, args.pr, args.thread)
        if args.body_file == "-":
            raw = sys.stdin.buffer.read().decode("utf-8")
        else:
            raw = _read(Path(args.body_file))
        body = poster.require_body(raw)
    except (OSError, UnicodeError, poster.PostInputError) as exc:
        _fail("invalid_input", _details(exc))
        return 2
    try:
        handle = poster.current_handle()
        response = poster.post(path, poster.sign(body, handle))
    except poster.GitHubError as exc:
        if exc.stdout:
            _write(exc.stdout)
        detail: dict[str, object] = {"message": str(exc)}
        if exc.stderr:
            detail["stderr"] = exc.stderr
        _fail("github_error", [detail])
        return 1
    _write(response)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["round"]:
        # The review-loop engine owns its command line, exit codes, and error format.
        from siula.review import round as review_round

        return review_round.main(argv[1:])

    parser = argparse.ArgumentParser(prog="siula")
    commands = parser.add_subparsers(dest="command", required=True)
    # Listed for --help only: main() hands "siula round ..." to siula.review.round before parsing.
    commands.add_parser(
        "round",
        help="keep the review-loop round state for one PR (siula round --help)",
        add_help=False,
    )

    inspect_parser = commands.add_parser("inspect", help="inspect a declared snapshot")
    inspect_parser.add_argument("snapshot", type=Path)
    inspect_parser.set_defaults(handler=_inspect)

    findings_parser = commands.add_parser(
        "check-findings", help="validate a reviewer's findings array and print it normalized"
    )
    findings_parser.add_argument("findings", type=Path, metavar="PATH")
    findings_parser.set_defaults(handler=_check_findings)

    brief_parser = commands.add_parser(
        "review-brief", help="print the prompt for a read-only pull request reviewer"
    )
    brief_parser.add_argument("--diff-file", type=Path, required=True, metavar="PATH")
    brief_parser.add_argument("--intent-file", type=Path, required=True, metavar="PATH")
    brief_parser.set_defaults(handler=_review_brief)

    post_parser = commands.add_parser(
        "post-reply", help="post a signed reply to a review thread, or a signed PR comment"
    )
    post_parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    post_parser.add_argument("--pr", required=True, metavar="N")
    target = post_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--thread", metavar="COMMENT_ID", help="reply to this review comment")
    target.add_argument("--issue", action="store_true", help="post a top-level PR comment")
    post_parser.add_argument(
        "--body-file", required=True, metavar="PATH", help='"-" reads standard input'
    )
    post_parser.set_defaults(handler=_post_reply)

    args = parser.parse_args()
    result: int = args.handler(args)
    return result
