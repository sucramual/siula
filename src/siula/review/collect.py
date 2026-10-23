"""One read of a PR's review feedback and reviewer state, with quiescence polling.

One GraphQL query feeds both the polling-phase event count and the final output, so there is no
separate "collect" pass. ``siula round`` imports this module and calls collect(); the command
prints the same object.

Usage:
  python -m siula.review.collect <owner/repo> <pr-number> [options]

Options:
  --since <ISO8601>             Only count/return events after this time.
  --floor <seconds>             Don't stop before this (default 90).
  --idle-window <seconds>       Stop when no new events for this long (default 60).
  --ceiling <seconds>           Hard timeout (default 300).
  --poll-interval <seconds>     Polling cadence (default 15).
  --skip-author <login>         Append to default skip-list (repeatable).
  --skip-pattern <regex>        Append to default skip-pattern list (repeatable).
  --no-skip-defaults            Replace defaults with only what's passed via --skip-*.
  --skip-scope <scope>          all | comments-only (default).
  --return-on-new <seconds>     Arrival mode. Stop when new feedback exists and no more new
                                feedback arrived for this many seconds (the settle time), or at
                                the ceiling. --floor and --idle-window do not apply in this mode.
  --ignore-ids <file>           Arrival mode only. A file with one numeric id per line (the
                                output's REST databaseId); blank lines are allowed. Events with
                                these ids do not count as new feedback for the early return,
                                but still appear in the output unchanged. Outside arrival mode
                                the file is still validated but does not change the stop rule.
  --reviewers <file>            A JSON object keyed by owner/repo (matched case-insensitively).
                                Each entry names one reviewer and has only these keys:
                                  login         non-empty string (required)
                                  retrigger     non-empty string, the comment text that asks for
                                                a new review (optional)
                                  doneReaction  one of +1 -1 laugh hooray confused heart rocket
                                                eyes (optional)
                                Any other key, or a bad value, exits non-zero before any query.
                                Without this option, or with no entry for the repo,
                                reviewer.state is unconfigured.
  --first-seen <ISO8601>        The time the caller first saw the current head SHA, for example
                                2026-10-13T09:00:00Z. The collector does not infer it from
                                GitHub. Without it, headPushedAt is null: doneReaction does not
                                match and no re-trigger or timeout fires. The round can stop
                                late, never early.

Output: JSON object {headRefOid, reviewDecision, reviewThreads, reviews, issueComments,
                     headPushedAt, reviewer, stopReason}.
  Every body is the text as written: the poster's footer ("---" / "agent on behalf of
  <handle>") is removed.
  reviewThreads[] = {id, isResolved, isOutdated, path, line, comments[]}
    comments[]    = {id, body, author, createdAt}     (id is REST databaseId)
  reviews[]       = {id, body, state, author, submittedAt}
  issueComments[] = {id, body, author, createdAt}
  headPushedAt    = the --first-seen time in UTC, or null without --first-seen.
  reviewer        = {login, state, evidence, retrigger, retriggeredAt, retriggerDue, timedOut}
    state         = covered | waiting | unconfigured. covered means a review by login whose
                    commit is the head, an issue comment by login with a line
                    "**Reviewed commit:** `<sha>`" whose sha (7+ hex) is a prefix of the head,
                    or a doneReaction on the PR by login created at or after headPushedAt.
                    Logins compare by same_login().
    evidence      = the matching signal, "review 123 on abc1234", "receipt 456 on abc1234",
                    or "reaction +1 at <time>";
                    null when waiting
    retriggeredAt = earliest issue comment after headPushedAt, not by the reviewer, whose body
                    equals the retrigger text (both trimmed, after removing the poster's footer
                    "---" / "agent on behalf of <handle>"); a comment that quotes it does not count
    retriggerDue  = waiting, retrigger configured and not posted, >= RETRIGGER_AFTER (600s)
                    after headPushedAt
    timedOut      = waiting, and: after a retrigger (retriggeredAt set),
                    >= TIMEOUT_AFTER_RETRIGGER (600s) after retriggeredAt; with no retrigger
                    configured, >= REVIEWER_TIMEOUT (1200s) after headPushedAt. A reviewer with
                    a retrigger configured does not time out before the retrigger is posted.
  The reviewer fields use the raw query result, before --since and the skip-lists.
  stopReason      = "covered", "settled", "timeout", or "retrigger" (arrival mode returned
                    early), "ceiling", or "quiescent" (the floor/idle-window rule). At the
                    ceiling the collector queries once more and returns that snapshot; if that
                    query fails, it exits non-zero.

Exit codes: 0 success; 1 a bad option or a failed or incomplete GitHub read ("ERROR: ..." on
stderr and nothing on stdout). Progress lines go to stderr.
Environment: REVIEW_PR_CLOCK names a file that holds epoch seconds, for tests. The collector reads
the time from it, and each wait adds the whole seconds of the wait to it instead of sleeping.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# siula.review.poster appends "\n\n---\nagent on behalf of <handle>\n" to every post. FOOTER is
# its regex for that footer; unsigned() removes it, once, for both the output bodies and the
# re-trigger match. ``as FOOTER`` re-exports it for callers that read collect.FOOTER.
from siula.review.poster import FOOTER as FOOTER
from siula.review.poster import unsign

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
RETRIGGER_AFTER = 600  # seconds after the head time before a re-trigger is due
# seconds after the head time before a reviewer with no retrigger times out
REVIEWER_TIMEOUT = 1200
TIMEOUT_AFTER_RETRIGGER = 600  # seconds after the re-trigger comment before the reviewer times out

SKIP_AUTHORS_DEFAULT = ("github-actions", "nx-cloud-bot", "linear", "vercel", "dependabot")
SKIP_PATTERNS_DEFAULT = (".*-deploy-preview", ".*-preview-bot")
SKIP_SCOPES = ("all", "comments-only")

REVIEWER_KEYS = ("login", "retrigger", "doneReaction")
# reviewers.json uses the REST reaction names; GraphQL reports the enum on the right.
REACTIONS = {"+1": "THUMBS_UP", "-1": "THUMBS_DOWN", "laugh": "LAUGH", "hooray": "HOORAY",
             "confused": "CONFUSED", "heart": "HEART", "rocket": "ROCKET",
             "eyes": "EYES"}  # fmt: skip
UNCONFIGURED = {"login": None, "state": "unconfigured", "evidence": None, "retrigger": None,
                "retriggeredAt": None, "retriggerDue": False, "timedOut": False}  # fmt: skip

# GitHub times end in "Z"; a UTC offset such as "+02:00" is also accepted. Fractions are ignored.
ISO_TIME = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})"
)
# A review receipt: Codex posts an issue comment with this line, not a review or a reaction, when
# it finds nothing (seen on 2026-10-23). The SHA is short, so a receipt names the head when it is
# a prefix of it.
RECEIPT = re.compile(r"^\*\*Reviewed commit:\*\* `([0-9a-fA-F]{7,40})`", re.MULTILINE)

# One query feeds both the polling signal and the final output. `databaseId` on comments and
# reviews is the numeric REST id that the reply and resolve tools expect. Connections are capped
# at 100; a result with more pages is refused, never returned in part.
QUERY = """query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      headRefOid
      reviewDecision
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            pageInfo { hasNextPage }
            nodes {
              databaseId
              body
              author { login }
              createdAt
            }
          }
        }
      }
      reviews(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          databaseId
          body
          state
          author { login }
          submittedAt
          commit { oid }
        }
      }
      comments(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          databaseId
          body
          author { login }
          createdAt
        }
      }
      reactions(first: 100) {
        pageInfo { hasNextPage }
        nodes { content createdAt user { login } }
      }
    }
  }
}"""


class CollectorError(Exception):
    """A bad option or a failed or incomplete read. Each line is printed as "ERROR: <line>"."""

    def __init__(self, *lines: str) -> None:
        super().__init__("; ".join(lines))
        self.lines = lines


class UsageError(CollectorError):
    """A malformed command line, printed as is."""


# --- shared rules: siula round uses these too ---------------------------------------


def epoch(text: Any) -> int | None:
    """An ISO-8601 time as epoch seconds, or None for anything that is not one."""
    match = ISO_TIME.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    try:
        seconds = calendar.timegm(datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S").timetuple())
    except ValueError:
        return None
    zone = match[3].replace(":", "")
    if zone == "Z":
        return seconds
    offset = int(zone[1:3]) * 3600 + int(zone[3:5]) * 60
    return seconds - offset if zone[0] == "+" else seconds + offset


def utc(seconds: int) -> str:
    """Epoch seconds in GitHub's form, TIME_FORMAT."""
    return time.strftime(TIME_FORMAT, time.gmtime(seconds))


def not_before(text: Any, start: str | int) -> bool:
    """True when ``text`` is a time at or after ``start`` (a time or epoch seconds).

    Offsets compare as UTC.
    """
    at, since = epoch(text), (start if isinstance(start, int) else epoch(start))
    return at is not None and since is not None and at >= since


def same_login(a: Any, b: Any) -> bool:
    """GitHub logins compare without case and without a trailing "[bot]".

    An empty login matches none. GraphQL review and comment authors drop the "[bot]" suffix, and
    reaction users keep it.
    """
    return isinstance(a, str) and isinstance(b, str) and bool(norm(a)) and norm(a) == norm(b)


def norm(login: str) -> str:
    login = login.lower()
    return login[: -len("[bot]")] if login.endswith("[bot]") else login


def unsigned(body: Any) -> Any:
    """Comment text as its author wrote it, without the poster's footer."""
    return unsign(body) if isinstance(body, str) else body


# --- options ----------------------------------------------------------------------


@dataclass
class Options:
    repo: str
    pr: str
    since: int | None = None  # epoch seconds
    floor: int = 90
    idle_window: int = 60
    ceiling: int = 300
    poll_interval: float = 15
    skip_authors: tuple[str, ...] = SKIP_AUTHORS_DEFAULT
    skip_patterns: tuple[str, ...] = SKIP_PATTERNS_DEFAULT
    skip_scope: str = "comments-only"
    return_on_new: int | None = None  # the settle time; None is the floor/idle-window mode
    ignore_ids: frozenset[str] = field(default_factory=frozenset)  # ids as written, such as "11"
    reviewer: dict[str, Any] | None = None  # the repo's reviewers.json entry, from reviewer_entry()
    first_seen: int | None = None  # epoch seconds


def reviewer_entry(path: Path, repo: str) -> dict[str, Any] | None:
    """The reviewers.json entry for ``repo``, or None when it has none.

    Only the matched entry is validated, so a bad entry for another repo does not block this one.
    Raises CollectorError before any query when the file or the entry is bad.
    """
    if not (path.is_file() and os.access(path, os.R_OK)):
        raise CollectorError(f"--reviewers file not found or not readable: '{path}'")
    try:
        reviewers = json.loads(path.read_text())
    except (ValueError, UnicodeDecodeError):
        reviewers = None
    if not isinstance(reviewers, dict):
        raise CollectorError(
            f"--reviewers file must hold one JSON object keyed by owner/repo: '{path}'"
        )
    hits = [(key, value) for key, value in reviewers.items() if key.lower() == repo.lower()]
    if not hits:
        return None
    errors = []
    if len(hits) > 1:
        errors.append(f"more than one entry matches {repo}")
    else:
        key, entry = hits[0]
        where = f"entry {tojson(key)}"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
        else:
            known = ", ".join(REVIEWER_KEYS)
            errors += [
                f"{where}: unknown key {tojson(k)} (known keys: {known})"
                for k in entry
                if k not in REVIEWER_KEYS
            ]
            if not (isinstance(entry.get("login"), str) and entry["login"]):
                errors.append(f"{where}: login must be a non-empty string")
            retrigger = entry.get("retrigger")
            if not (retrigger is None or (isinstance(retrigger, str) and retrigger)):
                errors.append(f"{where}: retrigger must be a non-empty string")
            reaction = entry.get("doneReaction")
            if not (reaction is None or (isinstance(reaction, str) and reaction in REACTIONS)):
                errors.append(
                    f"{where}: doneReaction {tojson(reaction)} is not one of {' '.join(REACTIONS)}"
                )
    if errors:
        raise CollectorError(
            *(f"--reviewers file {tojson(str(path))}: {error}" for error in errors)
        )
    found: dict[str, Any] = hits[0][1]
    return found


def ignore_ids(path: Path) -> frozenset[str]:
    """The ids in an --ignore-ids file: one numeric id per line, blank lines allowed."""
    if not (path.is_file() and os.access(path, os.R_OK)):
        raise CollectorError(f"--ignore-ids file not found or not readable: '{path}'")
    lines = path.read_text().split("\n")
    if lines and lines[-1] == "":  # the newline that ends the last line
        lines.pop()
    for number, line in enumerate(lines, start=1):
        if not re.fullmatch(r"[0-9]*", line):
            raise CollectorError(
                f"--ignore-ids file must hold one numeric id per line (line {number})"
            )
    return frozenset(line for line in lines if line)


def whole_seconds(option: str, value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise CollectorError(f"{option} must be a whole number of seconds (got: '{value}')")
    return int(value)


VALUED = ("--since", "--floor", "--idle-window", "--ceiling", "--poll-interval", "--skip-author",
          "--skip-pattern", "--skip-scope", "--return-on-new", "--ignore-ids", "--reviewers",
          "--first-seen")  # fmt: skip


def parse_args(argv: list[str]) -> Options:
    """The command line as Options, checked in the same order as the options are documented."""
    if len(argv) < 2 or not argv[0] or not argv[1]:
        raise UsageError("Usage: python -m siula.review.collect <owner/repo> <pr-number> [options]")
    values: dict[str, str] = {}
    authors: list[str] = []
    patterns: list[str] = []
    defaults = True
    rest = argv[2:]
    while rest:
        option = rest.pop(0)
        if option == "--no-skip-defaults":
            defaults = False
            continue
        if option not in VALUED:
            raise UsageError(f"Unknown option: {option}")
        if not rest:
            raise CollectorError(f"{option} needs a value")
        value = rest.pop(0)
        if option == "--skip-author":
            authors.append(value)
        elif option == "--skip-pattern":
            patterns.append(value)
        else:
            values[option] = value

    options = Options(repo=argv[0], pr=argv[1])
    for option, name in (("--since", "since"), ("--first-seen", "first_seen")):
        if option in values:
            setattr(options, name, epoch(values[option]))
            if getattr(options, name) is None:
                example = "2026-10-13T09:00:00Z"
                raise CollectorError(
                    f"{option} must be an ISO 8601 time such as {example} (got: '{values[option]}')"
                )
    options.skip_scope = values.get("--skip-scope", options.skip_scope)
    if options.skip_scope not in SKIP_SCOPES:
        raise CollectorError(
            f"--skip-scope must be 'all' or 'comments-only' (got: '{options.skip_scope}')"
        )
    if values.get("--return-on-new"):  # an empty value leaves arrival mode off
        options.return_on_new = whole_seconds("--return-on-new", values["--return-on-new"])
    for option, name in (
        ("--floor", "floor"),
        ("--idle-window", "idle_window"),
        ("--ceiling", "ceiling"),
    ):
        if option in values:
            setattr(options, name, whole_seconds(option, values[option]))
    if "--poll-interval" in values:
        try:
            options.poll_interval = float(values["--poll-interval"])
        except ValueError:
            options.poll_interval = -1.0
        if not 0 <= options.poll_interval < float("inf"):
            raise CollectorError(
                f"--poll-interval must be a number of seconds (got: '{values['--poll-interval']}')"
            )
    if "--ignore-ids" in values:
        options.ignore_ids = ignore_ids(Path(values["--ignore-ids"]))
    if "--reviewers" in values:
        options.reviewer = reviewer_entry(Path(values["--reviewers"]), options.repo)
    options.skip_authors = tuple(authors) if not defaults else (*SKIP_AUTHORS_DEFAULT, *authors)
    options.skip_patterns = tuple(patterns) if not defaults else (*SKIP_PATTERNS_DEFAULT, *patterns)
    for pattern in options.skip_patterns:
        try:
            re.compile(pattern)
        except re.error as error:
            raise CollectorError(
                f"--skip-pattern {pattern!r} is not a valid regular expression: {error}"
            ) from None
    return options


# --- one read ---------------------------------------------------------------------


class Clock:
    """Real time in whole seconds, or the REVIEW_PR_CLOCK test clock, which a wait advances."""

    def __init__(self) -> None:
        path = os.environ.get("REVIEW_PR_CLOCK")
        self.path = Path(path) if path else None

    def now(self) -> int:
        return int(self.path.read_text()) if self.path else int(time.time())

    def sleep(self, seconds: float) -> None:
        if self.path:
            self.path.write_text(str(self.now() + int(seconds)))
        else:
            time.sleep(seconds)


def query(options: Options) -> Any:
    """The raw GraphQL result for the PR, or None when gh fails. The repo is named on every call.

    The result is any JSON value; snapshot() refuses one that is not a complete result.
    """
    owner, _, name = options.repo.partition("/")
    args = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}"]
    args += ["-F", f"repo={name}"]
    try:
        result = subprocess.run([*args, "-F", f"pr={options.pr}"], text=True, capture_output=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {}  # not JSON: refused as incomplete


INCOMPLETE = "GitHub returned an incomplete pull request result"


def snapshot(raw: Any, options: Options, now: int) -> dict[str, Any]:
    """The output object for one result, without stopReason.

    Raises CollectorError for an incomplete result.
    """
    try:
        if raw.get("errors") is not None:
            raise CollectorError(INCOMPLETE)
        pr = raw["data"]["repository"]["pullRequest"]
        if not isinstance(pr, dict):
            raise CollectorError(INCOMPLETE)
        connections = [pr[name] for name in ("reviewThreads", "reviews", "comments", "reactions")]
        connections += [thread["comments"] for thread in pr["reviewThreads"]["nodes"]]
        if any((connection.get("pageInfo") or {}).get("hasNextPage") for connection in connections):
            raise CollectorError(
                "GitHub result exceeds collector pagination limits; refusing a partial snapshot"
            )
        return {**shape(pr, options), **coverage(pr, options, now)}
    except (AttributeError, KeyError, TypeError):
        raise CollectorError(INCOMPLETE) from None


def shape(pr: dict[str, Any], options: Options) -> dict[str, Any]:
    """The feedback fields, after --since and the skip-lists.

    Skip rules apply to issueComments only by default; --skip-scope all extends them to thread
    comments and reviews too. Empty-body reviews are dropped, so an empty reviews[] means "no
    actionable review feedback".
    """

    def skipped(node: dict[str, Any]) -> bool:
        login = login_of(node)
        return login in options.skip_authors or any(
            re.search(p, login) for p in options.skip_patterns
        )

    def kept(nodes: list[Any]) -> list[Any]:
        return (
            [node for node in nodes if not skipped(node)] if options.skip_scope == "all" else nodes
        )

    def after(nodes: list[Any], key: str) -> list[Any]:
        """--since keeps a node whose time is later, or cannot be read.

        So the filter never hides feedback.
        """
        if options.since is None:
            return nodes
        times = ((node, epoch(node.get(key))) for node in nodes)
        return [node for node, at in times if at is None or at > options.since]

    def comment(node: dict[str, Any]) -> dict[str, Any]:
        return {"id": node.get("databaseId"), "body": unsigned(node.get("body")),
                "author": login_of(node), "createdAt": node.get("createdAt")}  # fmt: skip

    threads = []
    for thread in pr["reviewThreads"]["nodes"]:
        comments = after(thread["comments"]["nodes"], "createdAt")
        if options.since is not None and not comments:  # --since drops a thread with no new comment
            continue
        place = {key: thread.get(key) for key in ("id", "isResolved", "isOutdated", "path", "line")}
        threads.append({**place, "comments": [comment(node) for node in kept(comments)]})
    reviews = [
        {"id": node.get("databaseId"), "body": unsigned(node.get("body")),
         "state": node.get("state"), "author": login_of(node),
         "submittedAt": node.get("submittedAt")}
        for node in kept(after(pr["reviews"]["nodes"], "submittedAt"))
        if re.search(r"\S", node.get("body") or "")
    ]  # fmt: skip
    issue_comments = [
        comment(node) for node in after(pr["comments"]["nodes"], "createdAt") if not skipped(node)
    ]
    return {"headRefOid": pr.get("headRefOid"), "reviewDecision": pr.get("reviewDecision"),
            "reviewThreads": threads, "reviews": reviews,
            "issueComments": issue_comments}  # fmt: skip


def coverage(pr: dict[str, Any], options: Options, now: int) -> dict[str, Any]:
    """{headPushedAt, reviewer} from the raw result.

    The raw result is used so that --since and the skip-lists cannot hide a signal.
    """
    pushed, config = options.first_seen, options.reviewer
    head_time = {"headPushedAt": None if pushed is None else utc(pushed)}
    if config is None:
        return {**head_time, "reviewer": dict(UNCONFIGURED)}
    login, retrigger = config["login"], config.get("retrigger")
    reaction = config.get("doneReaction")
    head = pr.get("headRefOid")
    sha7 = jq_text(head[:7] if isinstance(head, str) else head)
    signals = [
        f"review {jq_text(node.get('databaseId'))} on {sha7}"
        for node in pr["reviews"].get("nodes") or []
        if same_login(login_of(node), login) and (node.get("commit") or {}).get("oid") == head
    ]
    signals += [
        f"receipt {jq_text(node.get('databaseId'))} on {sha7}"
        for node in pr["comments"].get("nodes") or []
        if same_login(login_of(node), login) and receipt_names(node.get("body"), head)
    ]
    if reaction is not None and pushed is not None:
        signals += [
            f"reaction {reaction} at {jq_text(node.get('createdAt'))}"
            for node in pr["reactions"].get("nodes") or []
            if same_login(login_of(node, "user"), login)
            and node.get("content") == REACTIONS[reaction]
            and not_before(node.get("createdAt"), pushed)
        ]
    evidence = signals[0] if signals else None
    waiting = evidence is None
    retriggered = None
    if retrigger is not None and pushed is not None:
        # Exact match only: a round summary that quotes the text is not a re-trigger.
        times = [
            epoch(node.get("createdAt"))
            for node in pr["comments"].get("nodes") or []
            if not same_login(login_of(node), login)
            and unsigned(node.get("body") or "").strip() == retrigger.strip()
        ]
        retriggered = min((t for t in times if t is not None and t > pushed), default=None)
    age = None if pushed is None else now - pushed
    # After a retrigger, the wait ends TIMEOUT_AFTER_RETRIGGER after it. With no retrigger
    # configured, it ends REVIEWER_TIMEOUT after the head time. A reviewer with a retrigger
    # configured is not timed out before the retrigger is posted.
    if retriggered is not None:
        timed_out = now - retriggered >= TIMEOUT_AFTER_RETRIGGER
    else:
        timed_out = retrigger is None and age is not None and age >= REVIEWER_TIMEOUT
    due = waiting and retrigger is not None and retriggered is None
    return {
        **head_time,
        "reviewer": {
            "login": login,
            "state": "waiting" if waiting else "covered",
            "evidence": evidence,
            "retrigger": retrigger,
            "retriggeredAt": None if retriggered is None else utc(retriggered),
            "retriggerDue": due and age is not None and age >= RETRIGGER_AFTER,
            "timedOut": waiting and timed_out,
        },
    }


def receipt_names(body: Any, head: Any) -> bool:
    """True when a review receipt body names ``head``.

    Its reviewed commit must be a prefix of the head SHA.
    """
    found = RECEIPT.search(body) if isinstance(body, str) else None
    return found is not None and isinstance(head, str) and head.lower().startswith(found[1].lower())


def login_of(node: dict[str, Any], key: str = "author") -> Any:
    """The node's login, or "<deleted>" for a deleted account."""
    login = (node.get(key) or {}).get("login")
    return "<deleted>" if login is None or login is False else login


def jq_text(value: Any) -> str:
    """A value as jq string interpolation writes it: strings as is, anything else as JSON."""
    return value if isinstance(value, str) else json.dumps(value)


def tojson(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def count_events(output: dict[str, Any], ignored: frozenset[str]) -> int:
    """New feedback in the output: what the consumer sees.

    So skipped authors and empty-body reviews never reset the quiescence timer. Ids in
    ``ignored`` do not count.
    """
    nodes = [c for t in output["reviewThreads"] for c in t["comments"]]
    nodes += output["reviews"] + output["issueComments"]
    return sum(1 for node in nodes if jq_text(node["id"]) not in ignored)


# --- polling ----------------------------------------------------------------------


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def collect(options: Options, clock: Clock | None = None) -> dict[str, Any]:
    """Poll the PR until a stop rule holds, and return the output object.

    The high-water count marks the last change; idle is the time since it. Stop when elapsed >=
    floor and idle >= idle window, or at the ceiling. In arrival mode (return_on_new = SETTLE),
    floor and idle window do not apply. Stop on the first true rule, in this order: reviewer
    covered and idle >= SETTLE (covered); count > 0 and idle >= SETTLE (settled); reviewer
    timedOut (timeout); reviewer retriggerDue (retrigger); elapsed >= ceiling (ceiling). timeout
    and retrigger wait while new feedback is still settling (count > 0 and idle < SETTLE), so
    they never cut a review batch short. The reviewer rules are stateless, so a later round on
    the same head returns at its first poll. Raises CollectorError instead of returning a
    partial or stale snapshot.
    """
    clock = clock or Clock()
    settle = options.return_on_new
    ignored = options.ignore_ids if settle is not None else frozenset()
    start = clock.now()
    last_change, prev_count = start, 0
    last: dict[str, Any] | None = None
    while True:
        # With --ceiling 0 the first pass ends at the ceiling, so it queries at once instead of
        # waiting.
        if options.ceiling != 0:
            clock.sleep(options.poll_interval)
        # Elapsed is checked first, so persistent query failures cannot loop past the ceiling.
        now = clock.now()
        elapsed = now - start
        if elapsed >= options.ceiling:
            log(f"ceiling reached ({elapsed}s ≥ {options.ceiling}s) — collecting")
            stop = "ceiling"
            break
        raw = query(options)
        if raw is None:
            log("poll: GraphQL query failed; retrying")
            continue
        last = snapshot(raw, options, clock.now())
        count, reviewer = count_events(last, ignored), last["reviewer"]
        if count > prev_count:
            last_change, prev_count = now, count
        idle = now - last_change
        log(f"poll: elapsed={elapsed}s count={count} idle={idle}s reviewer={reviewer['state']}")
        if settle is not None:
            settling = count > 0 and idle < settle
            if reviewer["state"] == "covered" and idle >= settle:
                log(
                    f"reviewer covered the head (idle {idle}s ≥ settle {settle}s, "
                    f"elapsed {elapsed}s) — collecting"
                )
                stop = "covered"
                break
            if count > 0 and idle >= settle:
                log(
                    f"new feedback settled (count {count}, idle {idle}s ≥ settle {settle}s, "
                    f"elapsed {elapsed}s) — collecting"
                )
                stop = "settled"
                break
            if reviewer["timedOut"] and not settling:
                log(
                    f"reviewer wait expired ({TIMEOUT_AFTER_RETRIGGER}s after the re-trigger, "
                    f"or {REVIEWER_TIMEOUT}s after the head time with no re-trigger configured)"
                    " — collecting"
                )
                stop = "timeout"
                break
            if reviewer["retriggerDue"] and not settling:
                log(
                    f"reviewer re-trigger due ({RETRIGGER_AFTER}s after the head time, "
                    "no signal) — collecting"
                )
                stop = "retrigger"
                break
        elif elapsed >= options.floor and idle >= options.idle_window:
            log(
                f"quiescent (idle {idle}s ≥ {options.idle_window}s, elapsed {elapsed}s ≥ "
                f"floor {options.floor}s) — collecting"
            )
            stop = "quiescent"
            break

    # The last poll can be up to one poll interval old at the ceiling, so query once more and
    # return that snapshot. If the final query fails or is incomplete, fail closed: never the
    # older snapshot.
    if stop == "ceiling":
        raw = query(options)
        if raw is None:
            if last is None:
                raise CollectorError(
                    "no successful GitHub query during polling; refusing an empty snapshot"
                )
            raise CollectorError(
                "final GitHub query at the ceiling failed; refusing a stale snapshot"
            )
        last = snapshot(raw, options, clock.now())
    if last is None:
        raise CollectorError(
            "no successful GitHub query during polling; refusing an empty snapshot"
        )
    return {**last, "stopReason": stop}


def main(argv: list[str]) -> int:
    try:
        output = collect(parse_args(argv))
    except CollectorError as error:
        for line in error.lines:
            print(line if isinstance(error, UsageError) else f"ERROR: {line}", file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
