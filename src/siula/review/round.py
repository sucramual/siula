"""siula round: keep the review-loop round state for one PR and tell the agent the next step.

Usage (each command prints one JSON object):
  siula round sync   <owner/repo> <pr> [--mode author|reviewer] [--max-rounds N] [--settle N]
                     [--interval N] [--ceiling N]
  siula round triage <owner/repo> <pr> --file F
  siula round findings <owner/repo> <pr> --file F
                     (reviewer mode: add your own findings to the inbox)
  siula round review <owner/repo> <pr> --event APPROVE|COMMENT|REQUEST_CHANGES --body-file F
                     (reviewer mode)
  siula round post   <owner/repo> <pr> (--thread COMMENT_ID | --issue) --body-file F [--resolve]
  siula round push   <owner/repo> <pr>
  siula round pause  <owner/repo> <pr> --reason TEXT
  siula round resume <owner/repo> <pr> --decision TEXT [--by NAME] [--max-rounds N]
  siula round show   <owner/repo> <pr>

Exit codes: 0 success; 1 refusal or tool error ("ERROR: ..." on stderr and nothing on stdout,
except that a post whose thread did not resolve also prints its result); 2 usage error.
Environment: REVIEW_PR_NOW sets the current time (%Y-%m-%dT%H:%M:%SZ), for tests.
The collector (siula.review.collect, imported) reads its test clock from REVIEW_PR_CLOCK.

See docs/review-loop.md for the design, the state fields, and the invariants.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from siula.review import collect, poster
from siula.review.test_integrity import MARKER, is_test_file

VERSION = 3  # 3: learning signals (items, proposals); a version-2 state is refused, not migrated
TIME_FORMAT = collect.TIME_FORMAT
PACKAGE_DIR = Path(__file__).resolve().parent
# resolve-thread.sh ships as package data; resolve() runs it with bash from its resource path.
RESOLVER = resources.files("siula.review").joinpath("resolve-thread.sh")
FILES_LIMIT = 3000  # GitHub lists at most this many files of a PR
ACTIVE = ("running", "needs_input", "blocked")
# disposition → the response it still asks of the run; the same table for every kind of item.
RESPONSES = {
    # until siula round push records its push and each thread it cites has a reply after it
    "Fix": "push",
    "Follow-up": None,  # settled
    "Already addressed": None,  # settled
    "No change": None,  # settled
    # until an answering stop decides it and triage applies it (sameAs)
    "Needs decision": "decision",
    "Noise": None,  # settled: a noise entry's reason says why its item asks for nothing
}
DISPOSITIONS = tuple(RESPONSES)
# Noise comes only from a noise entry.
CLAIM_DISPOSITIONS = tuple(d for d in DISPOSITIONS if d != "Noise")
# Never check (a red check needs attribution) or decision.
NOISE_KINDS = ("thread", "review", "issue", "finding")
PROPOSAL_ID = re.compile(r"P[1-9][0-9]*")  # Q3: one proposal per batch with a Needs decision entry
OPTION_ID = re.compile(r"[A-D]")  # a proposal's options, lettered by their order
OPTION_IDS = ("A", "B", "C", "D")
PROPOSAL_KEYS = ("summary", "options", "recommended")
PROPOSAL_SHAPE = (
    '{"proposal": {"summary": "<text>", "options": ["<one line>", "<one line>"], '
    '"recommended": "A"}}'
)
# Q4: characters kept of an archived body; the longest proposal summary; an option is one_line().
BODY_LIMIT = 8000
SUMMARY_LIMIT = 2000
# Closed lists for every field that code reads; a state file with any other value is refused.
STATUSES = ("running", "needs_input", "blocked", "succeeded", "failed")
CONDITIONS = (
    "decision_owed",  # a Needs decision claim has no answer
    "blocking_review",  # reviewDecision is CHANGES_REQUESTED
    "unknown_post",  # a post's result is unknown
    "misrouted_post",  # the post's receipt points to another repository or PR
    "max_rounds",  # the round cap was reached
    "repeat",  # T4: the same next for the same reason, with no new input
    "fix_returned",  # a claim came back after two fixes
    "pause",  # pause --reason
)
SOURCES = ("seen", "pushed")
CLASSES = ("design", "edges", None)
NEXT_STEPS = (
    "triage",
    "needs_input",
    "retrigger",
    "wait_reviewer",
    "wait_ci",
    "review",
    "done",
    "blocked",
)
ITEM_KINDS = ("thread", "review", "issue", "check", "decision", "finding")
MODES = ("author", "reviewer")
SEVERITIES = ("blocker", "high", "medium", "low")
REVIEW_EVENTS = ("APPROVE", "COMMENT", "REQUEST_CHANGES")
BLOCKING_SEVERITIES = ("blocker", "high")  # a Fix at these levels stops APPROVE
CHECK_KEY = r"check:[0-9a-f]{40}(?:[0-9a-f]{24})?:\S.*"  # check:<head sha>:<check name>
ANY_KEY = re.compile(rf"(?:thread|review|issue|finding):[0-9]+|{CHECK_KEY}|ledger:L[1-9][0-9]*")
LEDGER_ID = re.compile(r"L[1-9][0-9]*")
STOP_ID = re.compile(r"stop-[1-9][0-9]*")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # SHA-1, or SHA-256 repositories
PENDING_CONDITION = (
    "a post to {kind} {target} may or may not have happened; check the PR, then resume"
)
MAX_ROUNDS_CONDITION = "max rounds reached; raise the cap or stop"
# An observed item; ANY_KEY adds ledger:L<n>.
KEY = re.compile(rf"(?:thread|review|issue|finding):[0-9]+|{CHECK_KEY}")
COMMENT_KEY = re.compile(r"(thread|review|issue):([0-9]+)")  # the collector's numeric ids
THREAD_KEY = re.compile(r"thread:[0-9]+")
NO_CHECKS_GRACE = 120  # seconds a head may show no checks before none counts as green
CI_PENDING_LIMIT = 1800  # T4: seconds of wait_ci on one head before the run asks for input
# T2: check runs that a bot review adds are review, not CI. They never set ciSeen, and they hold
# only their own head while they run. On a private repository with no CI (2026-10-23), Copilot's
# automatic review added one to the first head only, outside the status rollup, so every later
# head waited 30 minutes.
BOT_REVIEW_CHECKS = ("copilot-pull-request-reviewer",)
THREAD_LOOKUP = """query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      author { login }
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          comments(first: 50) {
            pageInfo { hasNextPage }
            nodes { databaseId author { __typename login } }
          }
        }
      }
    }
  }
}"""

State = dict[str, Any]
Schema = dict[str, Any]


class Failure(Exception):
    """A refusal or tool error: printed as one ERROR line on stderr.

    ``output`` is still printed on stdout.
    """

    def __init__(self, message: str, output: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.output = output


class UsageError(Exception):
    """A bad combination of options: reported like an argparse error (exit 2)."""


def now() -> str:
    override = os.environ.get("REVIEW_PR_NOW")
    if not override:
        return collect.utc(int(time.time()))
    seconds = collect.epoch(override) if TIME.fullmatch(override) else None
    if seconds is None:
        raise Failure(f"REVIEW_PR_NOW must look like 2026-10-13T09:00:00Z (got: {override!r})")
    return collect.utc(seconds)


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# --- state schema -----------------------------------------------------------------
# The one declared schema of a version-3 state file, as data. A node names the JSON types that it
# accepts and at most one rule for its contents: "values" (a closed list), "pattern" (a typed id),
# "items" (each element of a list), "keys" and "entries" (a map), or "fields" (a record). A record
# field that is absent reads as null. Fields that code never reads, such as an inbox item's author
# or body, are free text and are not declared.
NULL = type(None)


def node(*types: type, **rule: Any) -> Schema:
    return {"types": types, **rule}


def closed(*values: Any) -> Schema:
    return node(*{type(value) for value in values}, values=values)


def ident(pattern: re.Pattern[str], *also: type) -> Schema:
    return node(str, *also, pattern=pattern)


def record(fields: dict[str, Schema], *also: type) -> Schema:
    return node(dict, *also, fields=fields)


def mapping(keys: re.Pattern[str], entries: Schema, label: str, sep: str, *also: type) -> Schema:
    """A JSON object keyed by typed ids.

    ``label`` names an entry ({path}, {key}), and ``sep`` joins it to its fields.
    """
    return node(dict, *also, keys=keys, entries=entries, label=label, sep=sep)


TEXT, INT, BOOL = node(str), node(int), node(bool)
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")  # TIME_FORMAT
STATE_SCHEMA = record(
    {
        "version": closed(VERSION),
        "repo": ident(re.compile(r"[^/\s]+/[^/\s]+")),
        "pr": INT,
        "mode": closed(*MODES, None),  # absent in older states: they load as author
        "status": closed(*STATUSES),
        "round": INT,
        "maxRounds": INT,
        "skillSha": TEXT,
        "handled": node(list, items=ident(ANY_KEY)),
        "inbox": node(
            list,
            items=record(  # the one item model of new_item()
                {
                    "key": ident(ANY_KEY),
                    "kind": closed(*ITEM_KINDS),
                    "id": node(int, str),
                    "head": ident(SHA, NULL),
                    "source": closed("collector", "ci", "resume", "findings"),
                }
            ),
        ),
        "head": ident(SHA, NULL),
        "heads": mapping(
            SHA, record({"firstSeen": ident(TIME), "source": closed(*SOURCES)}), "heads.{key}", "."
        ),
        "ciSeen": BOOL,
        "ledger": node(
            list,
            items=record(
                {
                    "id": ident(LEDGER_ID),
                    "round": INT,
                    "head": ident(SHA, NULL),
                    "claim": TEXT,
                    "class": closed(*CLASSES),
                    "disposition": closed(*DISPOSITIONS),
                    # reviewer mode: how much a Fix or decision blocks APPROVE
                    "severity": closed(*SEVERITIES, None),
                    "sameAs": ident(LEDGER_ID, NULL),
                    "sources": node(list, items=ident(KEY)),
                    # Q3: the batch's proposal of a Needs decision entry
                    "proposal": ident(PROPOSAL_ID, NULL),
                    # D3: the head that siula round push sent this Fix in
                    "pushed": ident(SHA, NULL),
                    # D3: thread key -> reply id
                    "replies": mapping(THREAD_KEY, INT, "{path}.{key}", ".", NULL),
                }
            ),
        ),
        "stops": mapping(
            STOP_ID,
            record(
                {
                    "condition": closed(*CONDITIONS),
                    "detail": node(str, NULL),
                    "claims": node(list, items=ident(ANY_KEY)),
                    "answer": record({"text": TEXT, "by": TEXT, "at": ident(TIME)}, NULL),
                }
            ),
            "{key}",
            " ",
        ),
        "pending": record(
            {
                "kind": closed("thread", "issue", "review"),
                "target": ident(SHA, int),  # a comment id, or the head a review was sent for
                "at": ident(TIME),
                "bodySha256": ident(re.compile(r"[0-9a-f]{64}")),
                "resumedAt": ident(TIME, NULL),
            },
            NULL,
        ),
        "last": record(
            {
                "head": ident(SHA),
                "next": closed(*NEXT_STEPS),
                "reason": node(str, NULL),
                "firstAt": ident(TIME),
            },
            NULL,
        ),
        "review": record({"id": INT, "event": closed(*REVIEW_EVENTS), "head": ident(SHA)}, NULL),
        "target": closed(None, "ready_for_human_merge", "ready_with_gaps", "reviewed"),
        # D1: the gaps of the last done; free text that code never reads
        "gaps": node(list, NULL, items=TEXT),
        # Q1: each item a triage covered, as it saw it; a reopened decision is never archived
        "items": mapping(
            KEY,
            record(
                {
                    "key": ident(KEY),
                    "kind": closed("thread", "review", "issue", "check", "finding"),
                    "id": node(int, str),
                    "head": ident(SHA, NULL),
                    "source": closed("collector", "ci", "findings"),
                    "truncated": BOOL,  # Q4: the body was cut to BODY_LIMIT characters
                }
            ),
            "items.{key}",
            ".",
        ),
        "proposals": mapping(  # Q3: summary and options are free text that code never reads
            PROPOSAL_ID,
            record(
                {
                    "round": INT,
                    "summary": TEXT,
                    "options": mapping(OPTION_ID, TEXT, "{path}.{key}", "."),
                    "recommended": ident(OPTION_ID),
                    "at": ident(TIME),
                }
            ),
            "proposals.{key}",
            ".",
        ),
    }
)

Problem = tuple[str, Any]


def schema_problems(schema: Schema, value: Any, path: str, sep: str = ".") -> list[Problem]:
    """Every (field, value) in ``value`` that does not fit ``schema``.

    The type is checked first; a value is iterated or read only after its type fits.
    """
    types = schema["types"]
    if (isinstance(value, bool) and bool not in types) or not isinstance(value, types):
        return [(path, value)]
    if value is None:
        return []
    if "values" in schema and value not in schema["values"]:
        return [(path, value)]
    if "pattern" in schema and isinstance(value, str) and not schema["pattern"].fullmatch(value):
        return [(path, value)]
    problems: list[Problem] = []
    for index, element in enumerate(value if "items" in schema else []):
        problems += schema_problems(schema["items"], element, f"{path}[{index}]")
    for key, entry in value.items() if "entries" in schema else ():
        if schema["keys"].fullmatch(key):
            problems += schema_problems(
                schema["entries"], entry, schema["label"].format(path=path, key=key), schema["sep"]
            )
        else:
            problems.append((f"{path} key", key))
    for name, field in schema.get("fields", {}).items():
        problems += schema_problems(field, value.get(name), f"{path}{sep}{name}" if path else name)
    return problems


def relation_problems(state: State) -> list[Problem]:
    """Rules between fields, checked only after every field fits STATE_SCHEMA."""
    ledger, stops = state["ledger"], state["stops"]
    problems: list[Problem] = []
    named: set[Any] = set()
    # Each claim is one linear chain: its latest entry is the tail.
    for index, entry in enumerate(ledger):
        target = entry["sameAs"]
        earlier_ids = [earlier["id"] for earlier in ledger[:index]]
        if target is not None and (target not in earlier_ids or target in named):
            # a later or missing entry, or a branch
            problems.append((f"ledger[{index}].sameAs", target))
        named.add(target)
    if state["head"] is not None and state["head"] not in state["heads"]:
        problems.append(("head", state["head"]))
    open_ids = [sid for sid, stop in stops.items() if stop["answer"] is None]
    if len(open_ids) > 1:
        problems.append(("open stops", open_ids))
    if (len(open_ids) == 1) != (state["status"] in ("needs_input", "blocked")):
        problems.append(("status", state["status"]))
    for index, entry in enumerate(ledger):
        # Q3: each Needs decision entry names a stored proposal; no other entry does.
        proposal = entry.get("proposal")
        allowed = state["proposals"] if entry["disposition"] == "Needs decision" else (None,)
        if proposal not in allowed:
            problems.append((f"ledger[{index}].proposal", proposal))
        missing = next(
            (n for n, key in enumerate(entry["sources"]) if key not in state["items"]), None
        )
        # Q1: every source has an archived item; triage archives it in the write that cites it.
        if missing is not None:
            problems.append((f"ledger[{index}].sources[{missing}]", entry["sources"][missing]))
    # Q3: 2-4 options A, B, ... in order; it recommends one.
    for pid, proposal in state["proposals"].items():
        letters = tuple(proposal["options"])
        if not 2 <= len(letters) <= len(OPTION_IDS) or letters != OPTION_IDS[: len(letters)]:
            problems.append((f"proposals.{pid}.options", proposal["options"]))
        elif proposal["recommended"] not in letters:
            problems.append((f"proposals.{pid}.recommended", proposal["recommended"]))
    for key, item in state["items"].items():  # Q1: an archived item sits under its own key
        if item["key"] != key:
            problems.append((f"items.{key}.key", item["key"]))
    return problems


def schema_problem(state: Any) -> str | None:
    """The first field whose value code cannot act on, as "<field> = <value>", or None.

    Code reads only closed lists and typed ids; free text (claim, detail, answer.text) is checked
    only for being a string. Store.load() and Store.save() are the only callers.
    """
    problems = schema_problems(STATE_SCHEMA, state, "") or relation_problems(state)
    return next((f"{field} = {value!r}" for field, value in problems), None)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class Store:
    """The state file, its lock, and the PR it belongs to."""

    def __init__(self, repo: str, pr: str) -> None:
        owner, name = repo.split("/")
        self.repo, self.pr, self.owner, self.name = repo, pr, owner, name
        self.label = f"{repo}#{pr}"
        # One directory per owner, repo, and PR: a joined name such as "a-b-c" is ambiguous.
        self.dir = Path.home() / ".local" / "state" / "siula" / "review" / owner / name / pr
        self.path = self.dir / "state.json"
        self._lock: Any = None

    def lock(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.dir / "state.lock", "a")  # held until the process exits
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise Failure(f"another siula round command is running for {self.label}") from None
        self._lock = handle

    def load(self) -> State | None:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise self.unreadable(error.strerror or str(error)) from None
        try:
            state = json.loads(text)
        except ValueError as error:
            raise self.unreadable(f"bad JSON: {error}") from None
        if not isinstance(state, dict):
            raise self.unreadable("not a JSON object")
        if state.get("version") != VERSION:
            raise self.unreadable(f"unknown version {state.get('version')!r}")
        problem = schema_problem(state)
        if problem is not None:
            raise self.unreadable(f"unknown value {problem}")
        if str(state.get("repo")).lower() != self.repo.lower() or state.get("pr") != int(self.pr):
            owner = f"{state.get('repo')}#{state.get('pr')}"
            raise Failure(
                f"state file {self.path} belongs to {owner}, not {self.label}; move it aside"
            )
        return state

    def require(self) -> State:
        state = self.load()
        if state is None:
            raise Failure(f"no state for {self.label}; run sync")
        return state

    def unreadable(self, why: str) -> Failure:
        return Failure(
            f"state file {self.path} is unreadable ({why}); move it aside to start again"
        )

    def save(self, state: State) -> None:
        problem = schema_problem(state)
        if problem is not None:  # a code bug: never write a state that load would refuse
            raise Failure(f"refusing to write {self.path}: unknown value {problem}")
        write_atomic(self.path, json.dumps(state, indent=2) + "\n")


def summary(state: State) -> dict[str, Any]:
    keys = ("repo", "pr", "status", "round", "maxRounds", "head", "pending")
    summary = {key: state[key] for key in keys}
    return {
        **summary,
        "resumeCondition": resume_condition(state),
        "ledgerEntries": len(state["ledger"]),
    }


def gh_json(store: Store, args: list[str]) -> Any:
    try:
        result = subprocess.run(["gh", *args], text=True, capture_output=True)
    except OSError as error:
        raise Failure(f"cannot run gh: {error}") from None
    what = f"gh {' '.join(args[:2])}"
    if result.returncode != 0:
        raise Failure(
            f"{what} failed for {store.label} (exit {result.returncode}): {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise Failure(f"{what} printed invalid JSON for {store.label}") from None


GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)
RECEIPT_URL = re.compile(
    r"github\.com/(?:repos/)?([^/]+)/([^/]+)/(?:pull|pulls|issues)/([0-9]+)(?:[#/?]|$)", re.I
)


def require_checkout_of(store: Store, found: str | None, detail: str = "") -> None:
    """Refuse unless the working directory is a checkout of <owner/repo> (compared without case)."""
    if found is None:
        raise Failure(
            f"the working directory is not a checkout of a GitHub repository ({detail}); "
            "run siula round from the PR's worktree"
        )
    if found.lower() != store.repo.lower():
        raise Failure(
            f"the working directory is in {found}, not {store.repo}; "
            "run siula round from the PR's worktree"
        )


def require_cwd_repo(store: Store) -> None:
    """Before a post only: the working directory must be a checkout of the PR's repository.

    The poster names the repository in its REST path, so the check does not route the post. It
    keeps a reply tied to the worktree that holds the fix it answers.
    """
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"], text=True, capture_output=True
    )
    try:
        found = json.loads(result.stdout)["nameWithOwner"]
    except (ValueError, KeyError, TypeError):
        found = None
    require_checkout_of(
        store, found, result.stderr.strip() or f"gh repo view exited {result.returncode}"
    )


def require_origin_repo(store: Store) -> None:
    """Before push: origin, as configured, must name <owner/repo>.

    git config prints the URL as written; git remote get-url would expand insteadOf rewrites and
    hide the GitHub name.
    """
    url = git(["config", "--get", "remote.origin.url"]).stdout.strip()
    match = GITHUB_REMOTE.search(url)
    require_checkout_of(
        store, f"{match[1]}/{match[2]}" if match else None, f"origin is {url or 'not set'}"
    )


def receipt_target(response: dict[str, Any]) -> str | None:
    """The <owner>/<repo>#<pr> that a posted comment's API response points to.

    None unless all its URLs agree.
    """
    urls = [response.get(key) for key in ("html_url", "issue_url", "pull_request_url")]
    matches = (RECEIPT_URL.search(u) for u in urls if isinstance(u, str))
    found = {f"{m[1]}/{m[2]}#{m[3]}".lower() for m in matches if m}
    return found.pop() if len(found) == 1 else None


def pr_view(store: Store, fields: str) -> dict[str, Any]:
    view = gh_json(store, ["pr", "view", store.pr, "--repo", store.repo, "--json", fields])
    if not isinstance(view, dict) or not isinstance(view.get("headRefOid"), str):
        raise Failure(f"gh pr view returned no head SHA for {store.label}")
    if not view["headRefOid"]:
        raise Failure(f"gh pr view returned no head SHA for {store.label}")
    return view


def record_head(state: State, sha: str, source: str = "seen") -> bool:
    """Make sha the current head. The first record of a head is never overwritten.

    Returns True on a change.
    """
    changed: bool = state["head"] != sha
    if sha not in state["heads"]:
        state["heads"][sha] = {"firstSeen": now(), "source": source}
        changed = True
    state["head"] = sha
    return changed


ANSWERING = ("decision_owed", "blocking_review")  # the only stops whose answer decides claims


def claim_keys(entry: dict[str, Any]) -> set[str]:
    """The keys that name a claim in a stop.

    Its source keys, or ledger:L<n> for a claim without sources.
    """
    return set(entry["sources"]) or {f"ledger:{entry['id']}"}


def open_stop(state: State) -> str | None:
    """The id of the stop without an answer; there is at most one."""
    sid: str | None = next(
        (sid for sid, stop in state["stops"].items() if stop["answer"] is None), None
    )
    return sid


def halt(state: State, status: str, condition: str, detail: str) -> None:
    """Stop the run (S5): record stop-<n> with its condition, detail, open claims, and no answer.

    The condition is one of CONDITIONS, and the detail is shown as the resumeCondition. The
    claims are the ones open now.
    """
    assert condition in CONDITIONS, condition
    undecided = (claim_keys(e) for e in open_decisions(state) if not decided(state, e))
    claims = sorted(set[str]().union(*undecided))
    stop = {"condition": condition, "detail": detail, "claims": claims, "answer": None}
    state["stops"][f"stop-{len(state['stops']) + 1}"] = stop
    state["status"] = status


def resume_condition(state: State) -> str | None:
    """The open stop's detail; the stops own it, so it is not stored twice."""
    sid = open_stop(state)
    detail: str | None = state["stops"][sid]["detail"] if sid else None
    return detail


def stop_for_pending(store: Store, state: State) -> str | None:
    """S4: while a post's result is unknown, set needs_input with the pending condition.

    Returns the condition. After resume (resumedAt set), the run stays running; only sync acts,
    and it reconciles the post first.
    """
    if state["pending"] is None or state["pending"].get("resumedAt"):
        return None
    condition = PENDING_CONDITION.format(**state["pending"])
    if resume_condition(state) != condition:
        halt(state, "needs_input", "unknown_post", condition)
        store.save(state)
    return condition


def require_running(store: Store, state: State, also: tuple[str, ...] = ()) -> None:
    """Refuse unless the run is running (or in ``also``). A pending post first stops the run."""
    condition = stop_for_pending(store, state)
    if condition is not None:
        raise Failure(f"status is needs_input: {condition}")
    pending = state["pending"]
    if pending is not None:  # resumed, not reconciled yet
        raise Failure(
            f"a post to {pending['kind']} {pending['target']} is unconfirmed; "
            "run sync to check the PR first"
        )
    status = state["status"]
    if status != "running" and status not in also:
        why = resume_condition(state) if status in ACTIVE else "run sync to begin a new run"
        raise Failure(f"status is {status}: {why or 'resume first'}")


def body_sha(text: str) -> str:
    """The hash that identifies a posted body: trimmed, with CRLF as LF.

    The collector removes the poster's footer from every body it returns, so a body read back
    from GitHub hashes like the text that was posted. The match is exact: a prefix match would
    also accept a longer comment that only starts with the same text.
    """
    return hashlib.sha256(text.replace("\r\n", "\n").strip().encode()).hexdigest()


def record_reply(state: State, thread: str, reply_id: int) -> None:
    """D3: a reply receipt on every ledger entry that cites ``thread``.

    A reply before a Fix's push does not count.
    """
    for entry in state["ledger"]:
        unpushed_fix = entry["disposition"] == "Fix" and entry.get("pushed") is None
        if thread in entry["sources"] and not unpushed_fix:
            entry["replies"] = {**(entry.get("replies") or {}), thread: reply_id}


EVENT_OF_STATE = {
    "APPROVED": "APPROVE",
    "COMMENTED": "COMMENT",
    "CHANGES_REQUESTED": "REQUEST_CHANGES",
}


def viewer_reviews(store: Store, viewer: str, head: str) -> list[dict[str, Any]]:
    """The viewer's reviews on ``head``, oldest first.

    The collector's reviews carry no commit, so this reads REST.
    """
    path = f"repos/{store.repo}/pulls/{store.pr}/reviews?per_page=100"
    pages = gh_json(store, ["api", path, "--paginate", "--slurp"])
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise Failure(f"gh api reviews printed an unexpected value for {store.label}")
    listed = [review for page in pages for review in page]
    return [
        r for r in listed
        if isinstance(r, dict) and collect.same_login((r.get("user") or {}).get("login"), viewer)
        and r.get("commit_id") == head
    ]  # fmt: skip


def record_review(state: State, review: dict[str, Any], head: str) -> str:
    """Record a review that is on GitHub: its id, event, and head.

    Its key joins handled, so sync never returns it.
    """
    raw = review.get("state") or review.get("event") or ""
    state["review"] = {"id": review["id"], "event": EVENT_OF_STATE.get(raw, raw), "head": head}
    key = f"review:{review['id']}"
    add_handled(state, [key])
    return key


def reconcile(
    store: Store, state: State, snapshot: dict[str, Any], author: str | None
) -> dict[str, Any]:
    """S4: find a resumed pending post on GitHub, and clear pending.

    It records the post's key, or records it as not posted.
    """
    pending = state["pending"]
    kind, target = pending["kind"], pending["target"]
    if kind == "review":
        found = [
            r for r in viewer_reviews(store, author or "", target)
            if body_sha(r.get("body") or "") == pending.get("bodySha256")
        ]  # fmt: skip
        key = record_review(state, found[0], target) if found else None
        state["pending"] = None
        return {
            "kind": kind,
            "target": target,
            "posted": key is not None,
            "key": key,
            "matches": len(found),
        }
    if kind == "thread":
        threads = [
            t["comments"]
            for t in snapshot["reviewThreads"]
            if any(c["id"] == target for c in t["comments"])
        ]
        candidates = threads[0] if threads else []
    else:
        candidates = snapshot["issueComments"]
    matches = sorted(
        (c for c in candidates
         if collect.same_login(c["author"], author)
         and collect.not_before(c["createdAt"], pending["at"])
         and body_sha(c["body"] or "") == pending.get("bodySha256")),
        key=lambda c: (c["createdAt"], c["id"]),
    )  # fmt: skip
    key = f"{kind}:{matches[0]['id']}" if matches else None
    if key is not None:
        add_handled(state, [key])
        if kind == "thread":
            record_reply(state, f"thread:{target}", matches[0]["id"])
    state["pending"] = None
    return {
        "kind": kind,
        "target": target,
        "posted": key is not None,
        "key": key,
        "matches": len(matches),
    }


def reviewers_path() -> Path:
    """The reviewers file.

    It is $XDG_CONFIG_HOME/siula/reviewers.json when XDG_CONFIG_HOME is set and absolute, and
    ~/.config/siula/reviewers.json otherwise.
    """
    config = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(config) if config and os.path.isabs(config) else Path.home() / ".config"
    return base / "siula" / "reviewers.json"


def configured_reviewer(repo: str) -> dict[str, Any] | None:
    """The reviewers file's entry for ``repo``, or None when no reviewer is configured.

    A missing file means no reviewer is configured. A file that exists but cannot be read or
    parsed, or a bad entry for ``repo``, raises collect.CollectorError.
    """
    path = reviewers_path()
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        raise collect.CollectorError(
            f"--reviewers file not found or not readable: '{path}' ({error.strerror})"
        ) from None
    return collect.reviewer_entry(path, repo)


def run_collector(
    store: Store, state: State, settle: int, interval: int, ceiling: int
) -> dict[str, Any]:
    """Collect in arrival mode for the current head.

    A failure leaves the inbox, handled keys, and round unchanged.
    """
    # The collector takes numeric ids. Two kinds can share a number, but a collision only
    # delays its early return; newItems are computed here from keys, so no item is hidden.
    # Not check: or ledger: keys.
    numeric = frozenset(m[2] for m in map(COMMENT_KEY.fullmatch, state["handled"]) if m)
    first_seen = collect.epoch(state["heads"][state["head"]]["firstSeen"])
    unchanged = "the inbox, handled keys, and round did not change"
    try:
        options = collect.Options(
            store.repo, store.pr, return_on_new=settle, poll_interval=interval, ceiling=ceiling
        )
        options.ignore_ids, options.first_seen = numeric, first_seen
        options.reviewer = configured_reviewer(store.repo)
        # No re-trigger in reviewer mode: a silent bot becomes a gap.
        if state.get("mode") == "reviewer" and options.reviewer:
            options.reviewer = {**options.reviewer, "retrigger": None}
        snapshot = collect.collect(options)  # its progress lines go to stderr
    except collect.CollectorError as error:
        raise Failure(f"the collector failed: {error}; {unchanged}") from None
    try:
        feedback(snapshot)
    except (KeyError, TypeError):
        raise Failure(f"the collector returned an incomplete snapshot; {unchanged}") from None
    if not isinstance(snapshot.get("headRefOid"), str):
        raise Failure(f"the collector snapshot has no headRefOid; {unchanged}")
    return snapshot


def new_item(
    kind: str,
    id: Any,
    head: str | None,
    source: str,
    key: str | None = None,
    **readable: Any,
) -> dict[str, Any]:
    """The one item model: {key, kind, id, head, source} plus the fields people read.

    source names the intake that observed it: collector (comments, reviews), ci (checks), or
    resume (a decided claim sent back to triage). key defaults to "<kind>:<id>".
    """
    return {
        "key": key or f"{kind}:{id}",
        "kind": kind,
        "id": id,
        "head": head,
        "source": source,
        **readable,
    }


def feedback(snapshot: dict[str, Any], head: str | None = None) -> list[dict[str, Any]]:
    """Intake for the collector: every thread comment, review, and issue comment, as items."""
    items: list[dict[str, Any]] = []
    for thread in snapshot["reviewThreads"]:
        place = {"threadId": thread["id"], "path": thread["path"], "line": thread["line"]}
        for comment in thread["comments"]:
            items.append(
                comment_item(
                    "thread",
                    comment,
                    head,
                    comment["createdAt"],
                    **place,
                    isResolved=thread["isResolved"],
                )
            )
    for review in snapshot["reviews"]:
        items.append(
            comment_item("review", review, head, review["submittedAt"], state=review["state"])
        )
    for comment in snapshot["issueComments"]:
        items.append(comment_item("issue", comment, head, comment["createdAt"]))
    return items


def comment_item(
    kind: str, source: dict[str, Any], head: str | None, created_at: str, **extra: Any
) -> dict[str, Any]:
    if not is_int(source["id"]):
        raise TypeError("feedback id is not an integer")
    readable = {"author": source["author"], "body": source["body"], "createdAt": created_at}
    return new_item(kind, source["id"], head, "collector", **readable, **extra)


def check_item(head: str, name: str) -> dict[str, Any]:
    """Intake for CI: a red check is one item per head, check:<head>:<name>."""
    return new_item("check", name, head, "ci", key=f"check:{head}:{name}")


def inbox_keys(state: State) -> set[str]:
    return {entry["key"] for entry in state["inbox"]}


def known_keys(state: State) -> set[str]:
    """S2: the inbox and handled keys.

    These are every key that sync returned, plus the run's own comments, which post marks
    handled. A thread reply needs one; a triage source outside them was never returned by sync.
    """
    return inbox_keys(state) | set(state["handled"])


def add_handled(state: State, keys: list[str]) -> None:
    handled = set(state["handled"])
    for key in keys:
        if key not in handled:
            state["handled"].append(key)
            handled.add(key)


# --- commands -----------------------------------------------------------------


def new_state(store: Store, max_rounds: int, mode: str = "author") -> State:
    git = subprocess.run(
        ["git", "-C", str(PACKAGE_DIR), "rev-parse", "HEAD"], text=True, capture_output=True
    )
    keys = ("version", "repo", "pr", "status", "round", "maxRounds", "skillSha", "handled")
    sha = git.stdout.strip() if git.returncode == 0 else "unknown"
    values: tuple[Any, ...] = (VERSION, store.repo, int(store.pr), "running", 0, max_rounds,
                               sha, [])  # fmt: skip
    rest: dict[str, Any] = {
        "inbox": [],
        "head": None,
        "heads": {},
        "ciSeen": False,
        "ledger": [],
        "stops": {},
    }
    return {
        **dict(zip(keys, values, strict=False)),
        **rest,
        "pending": None,
        "last": None,
        "target": None,
        "gaps": None,
        "items": {},
        "proposals": {},
        "mode": mode,
        "review": None,
    }


def age(stored: str) -> int:
    """Seconds from a stored time to now()."""
    start = collect.epoch(stored)
    if start is None:  # STATE_SCHEMA checks the form, but not the calendar (a 13th month)
        raise Failure(f"stored time {stored!r} is not a valid time")
    return (collect.epoch(now()) or start) - start


def check_list(store: Store, args: list[str]) -> list[dict[str, Any]] | None:
    """The checks that gh printed as JSON, [] for empty stdout, or None when stdout is not a list.

    stderr is not parsed: its wording differs between gh versions (2.102.0 prints "no checks
    reported" and exits 1 for a head without checks, even with --required). The exit code is
    only a hint; an empty or failed read is confirmed through REST before it counts as no checks.
    """
    result = subprocess.run(
        ["gh", "pr", "checks", store.pr, "--repo", store.repo, *args],
        text=True,
        capture_output=True,
    )
    checks: Any
    try:
        checks = json.loads(result.stdout) if result.stdout.strip() else []
    except ValueError:
        checks = None
    if isinstance(checks, list) and all(isinstance(check, dict) for check in checks):
        return checks
    return None


def rest_count(store: Store, path: str) -> int:
    data = gh_json(store, ["api", f"repos/{store.repo}/commits/{path}"])  # a failed read stops sync
    count = data.get("total_count") if isinstance(data, dict) else None
    if not isinstance(count, int) or isinstance(count, bool):
        raise Failure(f"gh api repos/{store.repo}/commits/{path} returned no total_count")
    return count


def running_suites(store: Store, head: str) -> list[str] | None:
    """D2: the apps whose check suites on the head have check runs and have not completed.

    None when the list cannot be read in full. A suite without check runs does not count: idle
    apps keep one queued forever.
    """
    try:
        data = gh_json(
            store, ["api", f"repos/{store.repo}/commits/{head}/check-suites?per_page=100"]
        )
    except Failure:
        return None
    suites = data.get("check_suites") if isinstance(data, dict) else None
    total = data.get("total_count") if isinstance(data, dict) else None
    if not isinstance(total, int) or isinstance(total, bool) or not isinstance(suites, list):
        return None
    if total > len(suites):
        return None
    running: set[str] = set()
    for suite in suites:
        if not isinstance(suite, dict):
            return None
        runs: Any = suite.get("latest_check_runs_count")
        status, app = suite.get("status"), suite.get("app")
        slug = app.get("slug") if isinstance(app, dict) else None
        if not is_int(runs) or not isinstance(status, str) or not isinstance(slug, str):
            return None
        if runs > 0 and status != "completed":
            running.add(slug)
    return sorted(running)


def ci_checks(store: Store, args: list[str]) -> list[dict[str, Any]]:
    """The checks that gh pr checks lists with ``args``.

    The checks of a bot review (BOT_REVIEW_CHECKS) are left out.
    """
    listed = check_list(store, args) or []
    return [check for check in listed if check.get("name") not in BOT_REVIEW_CHECKS]


def unlisted_ci(store: Store, head: str) -> tuple[dict[str, Any], bool]:
    """CI from REST when gh pr checks lists no check, as (ci, real); see ci_status.

    A check run that is not a bot review's (BOT_REVIEW_CHECKS), a run that REST counts but does
    not list, or a commit status is real CI, and CI is pending. A run that cannot be named counts
    as real CI. A bot-review run that has not completed is pending, but is not real CI. With no
    other run, CI is none. A failed read, or a check-run body without its count or its list,
    stops sync.
    """
    path = f"repos/{store.repo}/commits/{head}/check-runs"
    data = gh_json(store, ["api", f"{path}?per_page=100"])  # a failed read stops sync
    total = data.get("total_count") if isinstance(data, dict) else None
    runs = data.get("check_runs") if isinstance(data, dict) else None
    if not isinstance(total, int) or isinstance(total, bool) or not isinstance(runs, list):
        raise Failure(f"gh api {path} returned no total_count or no check_runs list")
    bots = [run for run in runs if isinstance(run, dict) and run.get("name") in BOT_REVIEW_CHECKS]
    if len(bots) < len(runs) or total > len(runs) or rest_count(store, f"{head}/status"):
        return {
            "state": "pending",
            "failing": [],
            "reason": "checks exist but were not listed",
        }, True
    running = sorted({run["name"] for run in bots if run.get("status") != "completed"})
    if running:
        reason = f"bot review still running: {', '.join(running)}"
        return {"state": "pending", "failing": [], "reason": reason}, False
    return {"state": "none", "failing": []}, False


def ci_status(store: Store, head: str) -> tuple[dict[str, Any], bool]:
    """CI as ({state, failing}, real).

    CI is the required checks, or every check on the head when none is required. ``real`` is
    true when the head has real CI; only real CI sets ciSeen (T2). A check that a bot review adds
    (BOT_REVIEW_CHECKS) is not CI, so it is dropped from the listed checks. "No checks" (state
    none) needs positive evidence from REST (unlisted_ci). Listed checks that all pass are green
    only when every check suite with check runs has completed (D2); a suite list that cannot be
    read is pending.
    """
    fields = ["--json", "name,bucket"]
    checks = ci_checks(store, [*fields, "--required"]) or ci_checks(store, fields)
    if not checks:
        return unlisted_ci(store, head)
    failing = [check.get("name") for check in checks if check.get("bucket") in ("fail", "cancel")]
    pending = any(
        check.get("bucket") not in ("pass", "skipping", "fail", "cancel") for check in checks
    )
    if failing or pending:
        return {"state": "failing" if failing else "pending", "failing": failing}, True
    running = running_suites(store, head)
    if running is None:
        reason = f"the check suites of {head[:7]} could not be read"
        return {"state": "pending", "failing": [], "reason": reason}, True
    if running:
        reason = f"check suites still running: {', '.join(running)}"
        return {"state": "pending", "failing": [], "reason": reason}, True
    return {"state": "green", "failing": []}, True


def stopped(state: State) -> dict[str, Any]:
    return {"next": state["status"], "resumeCondition": resume_condition(state)}


def latest_entries(state: State) -> list[dict[str, Any]]:
    """The latest entry of each claim: entries that no later entry names with sameAs."""
    superseded = {entry["sameAs"] for entry in state["ledger"] if entry["sameAs"] is not None}
    return [entry for entry in state["ledger"] if entry["id"] not in superseded]


def awaiting(state: State, response: str) -> list[dict[str, Any]]:
    """The claims whose latest disposition still asks for ``response``."""
    return [entry for entry in latest_entries(state) if RESPONSES[entry["disposition"]] == response]


def unreplied(entry: dict[str, Any]) -> list[str]:
    """D3: the threads that a pushed Fix cites and that have no reply receipt yet."""
    replies = entry.get("replies") or {}
    return [key for key in entry["sources"] if THREAD_KEY.fullmatch(key) and key not in replies]


def open_decisions(state: State) -> list[dict[str, Any]]:
    """Claims that still ask for a decision."""
    return awaiting(state, "decision")


def decided(state: State, entry: dict[str, Any]) -> bool:
    """S5: a claim is decided only by an answered stop that listed it and asked for a decision."""
    keys = claim_keys(entry)
    stops = state["stops"].values()
    return any(
        s["answer"] is not None and s["condition"] in ANSWERING and keys & set(s["claims"])
        for s in stops
    )


def state_mode(state: State) -> str:
    """A state written before reviewer mode has no mode; it is an author-mode state."""
    mode: str = state.get("mode") or "author"
    return mode


def viewer_login(store: Store) -> str:
    """The login that gh acts as.

    An unreadable login stops the run, because the authorship check needs it.
    """
    try:
        login = gh_json(store, ["api", "user"]).get("login")
    except (Failure, AttributeError):
        login = None
    if not isinstance(login, str) or not login:
        raise Failure("cannot read the viewer login (gh api user); fix gh auth and run sync again")
    return login


def cmd_sync(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    store.lock()
    state = store.load()
    if state is not None and args.mode and args.mode != state_mode(state):
        raise Failure(f"state is in {state_mode(state)} mode; cannot switch to {args.mode}")
    if state is not None and (
        stop_for_pending(store, state) or state["status"] in ("needs_input", "blocked")
    ):
        return stopped(state)
    mode = state_mode(state) if state is not None else (args.mode or "author")
    view = pr_view(store, "headRefOid,state,isCrossRepository,reviewDecision,author")
    if view.get("isCrossRepository"):
        raise Failure(
            f"{store.label} is a cross-repository PR; siula round handles only same-repository PRs"
        )
    if view.get("state") != "OPEN":
        raise Failure(f"{store.label} is {str(view.get('state')).lower()}, not open")
    viewer = viewer_login(store)
    pr_author = (view.get("author") or {}).get("login")
    if not isinstance(pr_author, str) or not pr_author:
        raise Failure(f"cannot read the PR author of {store.label}; fix gh auth and run sync again")
    if mode == "author" and not collect.same_login(pr_author, viewer):
        raise Failure(
            f"{pr_author or 'someone else'} owns this PR, not {viewer}; "
            "use --mode reviewer to review it"
        )
    if mode == "reviewer" and collect.same_login(pr_author, viewer):
        raise Failure(f"reviewer mode needs a PR that {viewer} does not own")
    if state is None:
        state = new_state(store, args.max_rounds or 8, mode)
    elif state["status"] != "running":  # succeeded or failed: a new run
        state.update(status="running", target=None, gaps=None)
    head = view["headRefOid"]
    record_head(state, head)
    store.save(state)
    settle, interval = (20 if args.settle is None else args.settle), args.interval or 30
    ceiling = 300 if args.ceiling is None else args.ceiling
    snapshot = run_collector(store, state, settle, interval, ceiling)
    # S4: a resumed post is reconciled against GitHub before anything else reads the snapshot.
    reconciled = reconcile(store, state, snapshot, viewer) if state["pending"] else None
    ci, real_ci = ci_status(store, head)
    # T1: the snapshot and CI must belong to the head that is current after the CI read.
    latest = pr_view(store, "headRefOid")["headRefOid"]
    head_changed = snapshot["headRefOid"] != head or latest != head
    reviewer = None if head_changed else snapshot.get("reviewer")
    if head_changed:  # the reviewer fields and CI may belong to an old head
        record_head(state, snapshot["headRefOid"])
        record_head(state, latest)
    sha7 = state["head"][:7]
    if real_ci:  # T2: a bot review's check run is not CI, so it never makes a later head wait
        state["ciSeen"] = True
    first_seen = state["heads"][state["head"]]["firstSeen"]
    if ci["state"] == "none" and (state["ciSeen"] or age(first_seen) < NO_CHECKS_GRACE):
        # Checks may not be registered yet.
        ci = {"state": "pending", "failing": [], "reason": "no checks yet"}
    handled, inbox = set(state["handled"]), state["inbox"]
    in_inbox = inbox_keys(state)
    # A red check is an item like a comment, once per head: check:<the head CI was read for>:<name>.
    # When the head moved during this pass, the red checks may be the old head's, so none is
    # taken in.
    failing = [] if head_changed else ci["failing"]
    observed = feedback(snapshot, state["head"]) + [check_item(head, name) for name in failing]
    fresh = [
        entry for entry in observed if entry["key"] not in handled and entry["key"] not in in_inbox
    ]
    gaps = []
    if reviewer and reviewer.get("timedOut"):
        gaps.append(f"{reviewer.get('login')} did not review {head[:7]}")
    if reviewer and reviewer.get("state") == "unconfigured":
        gaps.append(f"no reviewer configured for {store.repo}")
    if ci["state"] == "none":
        gaps.append(f"no CI checks on {sha7}")
    output = {
        "head": state["head"],
        "headChanged": head_changed,
        "round": state["round"],
        "newItems": inbox,
    }
    output.update(ci=ci, reviewer=reviewer, gaps=gaps)
    if reconciled is not None:
        output["reconciled"] = reconciled
    if fresh and state["round"] >= state["maxRounds"]:
        # The new batch stays out of the inbox, so it comes back after resume raises the cap.
        halt(state, "blocked", "max_rounds", MAX_ROUNDS_CONDITION)
        state["last"] = None
        store.save(state)
        return {
            **output,
            "reason": None,
            "next": "blocked",
            "resumeCondition": MAX_ROUNDS_CONDITION,
        }
    if fresh:  # a new batch counts once, however often the inbox is returned
        inbox.extend(fresh)
        state["round"] += 1
        output["round"] = state["round"]
    step, reason, repeat = next_step(state, snapshot, view, ci, reviewer, head_changed)
    # T4: the same unresolved step for the same reason on the same head, with no new input, is
    # bounded.
    last = state["last"]
    again = last is not None and not fresh and not head_changed
    again = again and (last["head"], last["next"], last["reason"]) == (state["head"], step, reason)
    if again and repeat is not None and not inbox:
        if step != "wait_ci" or age(last["firstAt"]) >= CI_PENDING_LIMIT:
            step = "needs_input"
            halt(state, "needs_input", "repeat", repeat)
            output["resumeCondition"] = repeat
    elif not again:
        state["last"] = {"head": state["head"], "next": step, "reason": reason, "firstAt": now()}
    if step == "needs_input" and "resumeCondition" not in output:
        output["resumeCondition"] = resume_condition(state)
    if step == "done":  # D1: a gap is never ready for a human merge
        if mode == "reviewer":
            output["target"] = "reviewed"
        else:
            output["target"] = "ready_with_gaps" if gaps else "ready_for_human_merge"
        state.update(status="succeeded", target=output["target"], gaps=gaps, last=None)
    store.save(state)
    return {**output, "reason": reason, "next": step}


Step = tuple[str, str | None, str | None]


def next_step(
    state: State,
    snapshot: dict[str, Any],
    view: dict[str, Any],
    ci: dict[str, Any],
    reviewer: dict[str, Any] | None,
    moved: bool,
) -> Step:
    """Return (next, reason, repeat condition or None) by the docstring's order.

    needs_input sets the status.
    """
    sha7 = state["head"][:7]
    if state["inbox"]:
        return "triage", "inbox", None  # the inbox repeats until triage handles it (crash recovery)
    claims = open_decisions(state)
    undecided = [entry["id"] for entry in claims if not decided(state, entry)]
    reviewing = state_mode(state) == "reviewer"
    # In reviewer mode a standing CHANGES_REQUESTED is the viewer's own review, so it never blocks.
    decision = snapshot.get("reviewDecision", view.get("reviewDecision"))
    blocking = not reviewing and decision == "CHANGES_REQUESTED"
    if undecided or blocking:
        condition = f"decision owed on {undecided[0]}" if undecided else "a blocking review stands"
        halt(state, "needs_input", "decision_owed" if undecided else "blocking_review", condition)
        return "needs_input", condition, None
    if claims:  # decided, but the claim was not triaged again with sameAs
        cid = claims[0]["id"]
        repeat = (
            f"claim {cid} is still Needs decision after its decision; triage it with sameAs {cid}"
        )
        return "triage", f"decision on {cid} not applied", repeat
    if reviewing:
        return reviewer_step(state, reviewer, ci, moved)
    # D3: a Fix waits for its push receipt, then for a reply receipt on each thread it cites,
    # whatever the head.
    fixes = awaiting(state, "push")
    unpushed = next((entry for entry in fixes if entry.get("pushed") is None), None)
    if unpushed is not None:
        fix, on = unpushed["id"], (unpushed["head"] or state["head"])[:7]
        repeat = f"fix {fix} on {on} was triaged but not pushed; push it or change the disposition"
        return "triage", f"unpushed fix {fix}", repeat
    owed = next(((entry["id"], unreplied(entry)[0]) for entry in fixes if unreplied(entry)), None)
    if owed is not None:
        fix, thread = owed
        repeat = (
            f"fix {fix} was pushed, but {thread} has no reply; post the reply with "
            f"siula round post --thread {thread.split(':')[1]}, "
            f"or triage it again with sameAs {fix}"
        )
        return "triage", f"unreplied fix {fix}", repeat
    if reviewer and reviewer.get("retriggerDue"):
        return "retrigger", "retrigger due", f"a re-trigger is due on {sha7} but was not posted"
    # Silence is not a review: only covered, unconfigured, or timed out (with its gap) lets the
    # reviewer pass.
    reviewed = (
        reviewer is not None
        and bool(reviewer)
        and (reviewer.get("state") in ("covered", "unconfigured") or reviewer.get("timedOut"))
    )
    if moved or not reviewed:
        return "wait_reviewer", "reviewer", None  # bounded by the collector's reviewer timeout
    if ci["state"] not in (
        "green",
        "none",
    ):  # T1: pending or red CI blocks done; T4 bounds the wait
        return "wait_ci", "ci not green", f"CI still {ci['state']} on {sha7} after 30 minutes"
    # T1: current head, CI green, reviewer covered or timed out, no unpushed fix, no open claim,
    # empty inbox.
    return "done", None, None


def reviewer_step(
    state: State, reviewer: dict[str, Any] | None, ci: dict[str, Any], moved: bool
) -> Step:
    """The reviewer-mode tail of next_step.

    A Fix is reported in the review, so no push or reply receipt is owed.
    """
    sha7 = state["head"][:7]
    posted = state.get("review")
    if posted and posted["head"] == state["head"]:
        return "done", None, None
    reviewed = (
        reviewer is not None
        and bool(reviewer)
        and (reviewer.get("state") in ("covered", "unconfigured") or reviewer.get("timedOut"))
    )
    if moved or not reviewed:
        # No re-trigger: the collector's timeout turns silence into a gap.
        return "wait_reviewer", "reviewer", None
    if ci["state"] not in ("green", "none"):
        return "wait_ci", "ci not green", f"CI still {ci['state']} on {sha7} after 30 minutes"
    # The claim count is in the reason, so a triage between two syncs is progress, not a repeat
    # (T4).
    return (
        "review",
        f"review due ({len(state['ledger'])} claims)",
        f"the review of {sha7} was due but not posted; post it with review",
    )


def one_line(value: Any) -> bool:
    """A non-empty single line of at most 200 characters.

    A claim summary, a noise reason, or a proposal option.
    """
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\n" not in value
        and "\r" not in value
        and len(value) <= 200
    )


def noise_problem(entry: dict[str, Any], inbox: dict[str, str]) -> str | None:
    """A noise entry {"noise": <inbox key>, "reason": <one line>}.

    ``inbox`` maps each inbox key to its kind.
    """
    unknown = sorted(set(entry) - {"noise", "reason"})
    if unknown:
        return f"unknown fields {unknown}"
    key = entry["noise"]
    if not one_line(entry.get("reason")):
        return "noise needs a reason: a non-empty single line of at most 200 characters"
    if not isinstance(key, str) or key not in inbox:
        return f"noise {key} is not in the inbox"
    if inbox[key] == "check":
        return f"noise {key}: a red check is never noise; triage it as a claim"
    if inbox[key] not in NOISE_KINDS:
        return (
            f"noise {key}: a reopened decision is never noise; "
            f"triage it with sameAs {key.split(':', 1)[1]}"
        )
    return None


def entry_problem(
    entry: Any, known: set[str], returned: set[str], citable: set[str], reviewing: bool = False
) -> str | None:
    """A claim entry.

    ``returned`` is known_keys(); ``citable`` is the inbox keys and the archived items.
    """
    if not isinstance(entry, dict):
        return "must be a JSON object"
    unknown = sorted(
        set(entry) - {"claim", "class", "disposition", "sources", "sameAs", "severity"}
    )
    if unknown:
        return f"unknown fields {unknown}"
    claim, klass = entry.get("claim"), entry.get("class", "missing")
    disposition = entry.get("disposition")
    if not one_line(claim):
        return "claim must be a non-empty single line of at most 200 characters"
    if klass not in ("design", "edges", None):
        return "class must be design, edges, or null"
    if disposition == "Noise":
        return (
            "disposition Noise is only for a noise entry: "
            '{"noise": "<inbox key>", "reason": "<one line>"}'
        )
    if disposition not in CLAIM_DISPOSITIONS:
        return f"disposition must be one of {', '.join(CLAIM_DISPOSITIONS)}"
    sources = entry.get("sources")
    if not isinstance(sources, list) or not all(
        isinstance(key, str) and KEY.fullmatch(key) for key in sources
    ):
        return "sources must be a list of item keys such as issue:123 (empty for a CI claim)"
    unknown_keys = [key for key in sources if key not in returned]
    if unknown_keys:  # S2: the ledger cites only feedback that sync returned
        return f"sources {unknown_keys} were never returned by sync"
    own = [key for key in sources if key not in citable]
    # S2/Q1: a handled key with no archived item is a comment that post added; sync never
    # returns it.
    if own:
        return (
            f"sources {own} are this run's own comments, which sync never returns; "
            "cite the feedback they answer"
        )
    if entry.get("sameAs") is not None and entry["sameAs"] not in known:
        return f"sameAs must name an existing ledger entry (got {entry['sameAs']!r})"
    if klass is None and disposition not in ("No change", "Already addressed"):
        return "class null is valid only with No change or Already addressed"
    if klass == "design" and disposition != "Needs decision":
        return "class design needs disposition Needs decision"
    severity = entry.get("severity")
    if severity not in (*SEVERITIES, None):
        return f"severity must be one of {', '.join(SEVERITIES)}"
    if reviewing and severity is None and disposition in ("Fix", "Needs decision"):
        return f"reviewer mode needs a severity ({', '.join(SEVERITIES)}) for {disposition}"
    return None


def proposal_problem(element: dict[str, Any]) -> str | None:
    """Q3: a proposal element {"proposal": {summary, options, recommended}}.

    It has 2-4 one-line options, lettered A-D.
    """
    unknown = sorted(set(element) - {"proposal"})
    if unknown:
        return f"unknown fields {unknown}; a proposal element has only the field proposal"
    proposal = element["proposal"]
    if not isinstance(proposal, dict):
        return f"proposal must be a JSON object with the keys {', '.join(PROPOSAL_KEYS)}"
    unknown = sorted(set(proposal) - set(PROPOSAL_KEYS))
    if unknown:
        return f"proposal has unknown keys {unknown}; allowed: {', '.join(PROPOSAL_KEYS)}"
    summary, options = proposal.get("summary"), proposal.get("options")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > SUMMARY_LIMIT:
        return f"proposal.summary must be non-empty text of at most {SUMMARY_LIMIT} characters"
    if (
        not isinstance(options, list)
        or not 2 <= len(options) <= len(OPTION_IDS)
        or not all(map(one_line, options))
    ):
        return (
            f"proposal.options must be a list of 2 to {len(OPTION_IDS)} options, "
            "each a non-empty single line of at most 200 characters"
        )
    letters = OPTION_IDS[: len(options)]
    if proposal.get("recommended") not in letters:
        return f"proposal.recommended must be the letter of an option: one of {', '.join(letters)}"
    return None


def archived(item: dict[str, Any]) -> dict[str, Any]:
    """Q1/Q4: an inbox item as triage archives it.

    A body over BODY_LIMIT characters is cut, and truncated says so.
    """
    body = item.get("body")
    if isinstance(body, str) and len(body) > BODY_LIMIT:
        return {**item, "body": body[:BODY_LIMIT], "truncated": True}
    return {**item, "truncated": False}


def cmd_triage(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    try:
        entries = json.loads(Path(args.file).read_text())
    except (OSError, ValueError) as error:
        raise Failure(f"cannot read the triage file {args.file}: {error}") from None
    if not isinstance(entries, list):
        raise Failure(f"the triage file {args.file} must hold a JSON array")
    store.lock()
    state = store.require()
    require_running(store, state)
    ledger = state["ledger"]
    known = {entry["id"] for entry in ledger}
    returned_keys = known_keys(state)
    # Q1: each source is archived in this write, or was before.
    citable = inbox_keys(state) | set(state["items"])
    inbox = {item["key"]: item["kind"] for item in state["inbox"]}
    design_batch = any(
        isinstance(entry, dict) and entry.get("class") == "design" for entry in entries
    )
    # sameAs must name the latest entry of its claim, so each claim stays one linear chain
    # (option A).
    named_by = {entry["sameAs"]: entry["id"] for entry in ledger if entry["sameAs"] is not None}
    named_in_batch: dict[str, int] = {}
    noise_at: dict[str, int] = {}  # noise key -> the index of its entry
    proposal_at: int | None = None  # Q3: the index of the batch's one proposal element
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and "noise" in entry:
            problem = noise_problem(entry, inbox)
            if problem is None and entry["noise"] in noise_at:
                problem = (
                    f"noise {entry['noise']} is already listed by triage entry "
                    f"{noise_at[entry['noise']]}"
                )
            if problem:
                raise Failure(f"triage entry {index}: {problem}; nothing was recorded")
            noise_at[entry["noise"]] = index
            continue
        if isinstance(entry, dict) and "proposal" in entry:
            problem = proposal_problem(entry)
            if problem is None and proposal_at is not None:
                problem = f"the batch already has a proposal in triage entry {proposal_at}"
            if problem:
                raise Failure(f"triage entry {index}: {problem}; nothing was recorded")
            proposal_at = index
            continue
        problem = entry_problem(
            entry, known, returned_keys, citable, state_mode(state) == "reviewer"
        )
        if (
            problem is None
            and design_batch
            and entry["class"] is not None
            and entry["disposition"] != "Needs decision"
        ):
            problem = "this batch has a design entry, so every classed entry must be Needs decision"
        target = entry.get("sameAs") if problem is None else None
        if target in named_by:
            latest = target
            while latest in named_by:
                latest = named_by[latest]
            problem = f"sameAs {target} is not the latest entry of its claim; use {latest}"
        elif target in named_in_batch:
            problem = (
                f"sameAs {target} is already continued by triage entry "
                f"{named_in_batch[target]} of this batch"
            )
        elif target is not None:
            named_in_batch[target] = index
        if problem:
            raise Failure(f"triage entry {index}: {problem}; nothing was recorded")
    claims = [
        index
        for index, entry in enumerate(entries)
        if "noise" not in entry and "proposal" not in entry
    ]
    deciding = [index for index in claims if entries[index]["disposition"] == "Needs decision"]
    if deciding and proposal_at is None:  # Q3: the proposal that the owner's answer is paired with
        need = f"disposition Needs decision needs a proposal element in the batch: {PROPOSAL_SHAPE}"
        raise Failure(f"triage entry {deciding[0]}: {need}; nothing was recorded")
    if proposal_at is not None and not deciding:
        need = "a proposal needs a Needs decision claim entry in the batch"
        raise Failure(f"triage entry {proposal_at}: {need}; nothing was recorded")
    # D4: every inbox key is cited by a claim (a ledger:L<n> item by sameAs L<n>) or listed as
    # noise, never both. A proposal element cites nothing.
    cited: dict[str, int] = {}
    for index in claims:
        for key in entries[index]["sources"]:
            cited.setdefault(key, index)
    for key, index in noise_at.items():
        if key in cited:
            source = f"a source of triage entry {cited[key]}"
            raise Failure(
                f"triage entry {index}: {key} is both noise and {source}; nothing was recorded"
            )
    covered = set(noise_at) | set(cited) | {f"ledger:{target}" for target in named_in_batch}
    uncovered = [key for key in inbox if key not in covered]
    if uncovered:
        raise Failure(
            f"triage leaves inbox keys uncovered: {uncovered}; cite each in a claim's sources "
            "(sameAs L<n> for ledger:L<n>) or list it as noise with a reason; nothing was recorded"
        )
    by_id = {entry["id"]: entry for entry in ledger}

    def root(entry: dict[str, Any]) -> str:
        while entry["sameAs"] is not None:
            entry = by_id[entry["sameAs"]]
        first: str = entry["id"]
        return first

    proposal_id = None
    if proposal_at is not None:  # Q3: stored once; each Needs decision entry of the batch names it
        given = entries[proposal_at]["proposal"]
        proposal_id = f"P{len(state['proposals']) + 1}"
        # 2-4 options take the first letters.
        options = dict(zip(OPTION_IDS, given["options"], strict=False))
        stored = {"round": state["round"], "summary": given["summary"], "options": options}
        state["proposals"][proposal_id] = {
            **stored,
            "recommended": given["recommended"],
            "at": now(),
        }
    returned: str | None = None
    for entry in entries:
        if "proposal" in entry:
            continue
        # D4: noise is a ledger entry, so the ledger says why the item asked for nothing.
        noise = entry.get("noise")
        record = {
            "id": f"L{len(ledger) + 1}",
            "round": state["round"],
            "head": state["head"],
            "claim": (entry["reason"] if noise else entry["claim"]).strip(),
            "class": None if noise else entry["class"],
            "disposition": "Noise" if noise else entry["disposition"],
            "severity": None if noise else entry.get("severity"),
            "sameAs": None if noise else entry.get("sameAs"),
            "sources": [noise] if noise else entry["sources"],
            "proposal": (
                proposal_id if not noise and entry["disposition"] == "Needs decision" else None
            ),
            "pushed": None,
            "replies": None,
        }
        if record["disposition"] == "Fix" and returned is None:
            first = root(record)
            fixes = [old for old in ledger if old["disposition"] == "Fix" and root(old) == first]
            returned = first if len(fixes) >= 2 else None
        ledger.append(record)
        by_id[record["id"]] = record
    if returned is not None:
        halt(state, "needs_input", "fix_returned", f"claim {returned} returned after two fixes")
    # Q1: the text each covered item had when triage saw it; never overwritten.
    for item in state["inbox"]:
        if item["kind"] != "decision":
            state["items"].setdefault(item["key"], archived(item))
    # S2: the batch covered every inbox key (D4).
    add_handled(state, [entry["key"] for entry in state["inbox"]])
    state["inbox"] = []
    store.save(state)
    this_round = [entry for entry in ledger if entry["round"] == state["round"]]
    classes = {entry["class"] for entry in this_round}
    verdict = "design" if "design" in classes else "edges" if "edges" in classes else None
    return {
        "round": state["round"],
        "verdict": verdict,
        "entries": this_round,
        "status": state["status"],
    }


def thread_starter(store: Store, comment_id: int) -> tuple[Any, Any, Any]:
    """Return (typename, login) of the first comment in the thread holding comment_id.

    The PR author's login comes third.
    """
    query = ["api", "graphql", "-f", f"query={THREAD_LOOKUP}", "-F", f"owner={store.owner}"]
    data = gh_json(store, [*query, "-F", f"name={store.name}", "-F", f"pr={store.pr}"])
    try:
        pull = data["data"]["repository"]["pullRequest"]
        threads = pull["reviewThreads"]
        if data.get("errors") or threads["pageInfo"]["hasNextPage"]:
            raise ValueError
        for thread in threads["nodes"]:
            comments = thread["comments"]
            if comments["pageInfo"]["hasNextPage"]:
                raise ValueError
            if any(comment["databaseId"] == comment_id for comment in comments["nodes"]):
                author = comments["nodes"][0]["author"] or {}
                pr_author = (pull["author"] or {}).get("login")
                return author.get("__typename"), author.get("login"), pr_author
    except (KeyError, TypeError, ValueError):
        raise Failure("GitHub returned an incomplete review-thread result") from None
    raise Failure(f"comment {comment_id} is not in a review thread of {store.label}")


def resolve(store: Store, comment_id: int, output: dict[str, Any]) -> dict[str, Any]:
    try:
        typename, login, pr_author = thread_starter(store, comment_id)
        if not (typename == "Bot" or (typename == "User" and collect.same_login(login, pr_author))):
            starter = f"{typename} {login}" if typename else "a deleted account"
            reason = (
                f"the thread was started by {starter}; only bot and PR-author threads are resolved"
            )
            return {**output, "resolved": False, "reason": reason}
        with resources.as_file(RESOLVER) as script:
            result = subprocess.run(
                ["bash", str(script), store.repo, store.pr, str(comment_id)],
                text=True,
                capture_output=True,
            )
        if result.returncode != 0:
            raise Failure(
                f"resolve-thread.sh failed (exit {result.returncode}): {result.stderr.strip()}"
            )
    except Failure as error:
        failed = {**output, "resolved": False, "reason": str(error)}
        raise Failure(
            f"reply {output['commentId']} was posted, but the thread was not resolved: {error}",
            failed,
        ) from None
    return {**output, "resolved": True}


def cmd_post(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    require_cwd_repo(store)  # the worktree must hold the PR's repository; checked before any write
    store.lock()
    state = store.require()
    if state_mode(state) == "reviewer":
        raise Failure("reviewer mode does not post replies; use review")
    if not (args.issue and state["status"] == "succeeded"):  # a final summary may follow done
        require_running(store, state)
    if args.thread is not None and f"thread:{args.thread}" not in known_keys(state):  # S2
        raise Failure(
            f"comment {args.thread} was never returned by sync; reply only to feedback it returned"
        )
    kind, target = ("thread", args.thread) if args.thread is not None else ("issue", int(store.pr))
    # The poster refuses a bad input before anything is recorded or posted.
    try:
        path = poster.api_path(store.repo, store.pr, str(args.thread) if kind == "thread" else None)
    except poster.PostInputError as error:
        raise Failure(f"cannot post to {store.label}: {error}; nothing was posted") from None
    # The hash, never the body, lets sync find the post on GitHub after resume (S4).
    pending = {"kind": kind, "target": target, "at": now(), "bodySha256": body_sha(args.body)}
    state["pending"] = pending
    store.save(state)
    printed, response, problem = b"", None, None
    try:
        printed = poster.post(path, poster.sign(args.body, poster.current_handle()))
        response = json.loads(printed)
    except poster.GitHubError as error:
        printed = error.stdout
        problem = f"{error}: {error.stderr}" if error.stderr else str(error)
    except ValueError:  # post() checks for a JSON id, so this is not expected
        problem = "the poster printed a response that is not JSON"
    reply_id: Any = response.get("id") if isinstance(response, dict) else None
    if not is_int(reply_id):
        # GitHub's response, if any, helps check the PR.
        sys.stderr.flush()
        sys.stderr.buffer.write(printed)
        sys.stderr.buffer.flush()
        if problem:
            print(problem, file=sys.stderr)
        stop_for_pending(store, state)
        raise Failure("the post result is unknown; check the PR, then resume")
    landed = receipt_target(response) if isinstance(response, dict) else None
    if landed != f"{store.repo}#{store.pr}".lower():  # never record a receipt for a post elsewhere
        condition = (
            f"the post to {kind} {target} on {store.label} landed on "
            f"{landed or 'an unknown target'}; delete it there, then resume"
        )
        state["pending"] = None
        halt(state, "needs_input", "misrouted_post", condition)
        store.save(state)
        raise Failure(condition)
    add_handled(state, [f"{kind}:{reply_id}"])  # S2: post adds only the key of its own comment
    if kind == "thread":
        record_reply(state, f"thread:{args.thread}", reply_id)
    state["pending"] = None
    store.save(state)
    output = {"commentId": reply_id}
    return resolve(store, args.thread, output) if args.resolve else output


def git(args: list[str]) -> subprocess.CompletedProcess[str]:
    """git in the working directory (the PR worktree), named explicitly."""
    try:
        return subprocess.run(["git", "-C", os.getcwd(), *args], text=True, capture_output=True)
    except OSError as error:
        raise Failure(f"cannot run git: {error}") from None


def cmd_push(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    existing = store.load()
    if existing is not None and state_mode(existing) == "reviewer":
        raise Failure("reviewer mode does not push")
    # push sends the cwd's HEAD to origin; checked before any write or fetch.
    require_origin_repo(store)
    status = git(["status", "--porcelain"])
    if status.returncode != 0 or status.stdout.strip():
        problem = (
            "has uncommitted changes"
            if status.returncode == 0
            else f"could not be read (exit {status.returncode})"
        )
        raise Failure(f"the worktree {problem}; commit or stash them, then push")
    store.lock()
    state = store.require()
    require_running(store, state)
    branch = pr_view(store, "headRefName,headRefOid").get("headRefName")
    if not isinstance(branch, str) or not branch:
        raise Failure(f"gh pr view returned no head branch for {store.label}")
    base = state["head"]
    fetch = git(["fetch", "origin", branch])
    if fetch.returncode != 0:
        raise Failure(
            f"git fetch origin {branch} failed (exit {fetch.returncode}): {fetch.stderr.strip()}"
        )
    remote = git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}"]).stdout.strip()
    if remote != base:
        raise Failure(
            f"origin/{branch} is at {remote[:7] or 'nothing'}, not the round's head {base[:7]}; "
            "run sync first"
        )
    local = git(["rev-parse", "--verify", "HEAD"]).stdout.strip()
    if local == base:
        raise Failure(f"nothing to push: HEAD is the round's head {base[:7]}")
    if git(["merge-base", "--is-ancestor", base, local]).returncode != 0:
        raise Failure(
            f"HEAD {local[:7]} does not descend from the round's head {base[:7]}; "
            "a push would rewrite it"
        )
    push = git(["push", "origin", f"HEAD:{branch}"])
    if push.returncode != 0:
        raise Failure(
            f"git push failed (exit {push.returncode}); nothing was recorded: {push.stderr.strip()}"
        )
    pushed_at = now()
    record_head(state, local, "pushed")  # S3: a head recorded before keeps its first record
    # D3: the push receipt of each Fix triaged on the round's head.
    for entry in latest_entries(state):
        if entry["disposition"] == "Fix" and entry.get("pushed") is None and entry["head"] == base:
            entry["pushed"] = local
    store.save(state)
    return {"head": local, "pushedAt": pushed_at}


def cmd_findings(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    """Reviewer mode: add the reviewer's own findings to the inbox as finding:<n>.

    Triage must cover them.
    """
    try:
        findings = json.loads(Path(args.file).read_text())
    except (OSError, ValueError) as error:
        raise Failure(f"cannot read --file: {error}") from None
    if not isinstance(findings, list):
        raise Failure("findings must be a JSON list")
    store.lock()
    state = store.require()
    if state_mode(state) != "reviewer":
        raise Failure("findings need reviewer mode")
    try:
        require_running(store, state)
    except Failure as error:
        lead = "the round is paused" if state["status"] in ACTIVE else "the round is not running"
        raise Failure(f"{lead}; {error}") from None
    live = pr_view(store, "headRefOid")["headRefOid"]
    if live != state["head"]:
        raise Failure(
            f"the head moved to {live[:7]} since the last sync ({state['head'][:7]}); "
            "run sync first"
        )
    for index, entry in enumerate(findings):
        problem = finding_problem(entry)
        if problem:
            raise Failure(f"finding {index}: {problem}")
    numbers = [
        int(key.split(":", 1)[1])
        for key in (*state["handled"], *inbox_keys(state))
        if re.fullmatch(r"finding:[0-9]+", key)
    ]
    added = []
    for number, entry in enumerate(findings, start=max(numbers, default=0) + 1):
        key = f"finding:{number}"
        readable = {name: entry[name] for name in ("severity", "file", "line", "body")}
        state["inbox"].append(
            new_item("finding", number, state["head"], "findings", key=key, **readable)
        )
        added.append(key)
    store.save(state)
    return {"added": added, "next": "triage"}


def finding_problem(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "must be a JSON object"
    unknown = sorted(set(entry) - {"severity", "file", "line", "body"})
    if unknown:
        return f"unknown fields {unknown}"
    if entry.get("severity") not in SEVERITIES:
        return f"severity must be one of {', '.join(SEVERITIES)}"
    for name in ("file", "body"):
        value = entry.get(name)
        if not isinstance(value, str) or not value.strip():
            return f"{name} is required"
    line = entry.get("line")
    if line is not None and not is_int(line):
        return "line must be an integer or null"
    return None


def marker_count(marker: re.Pattern[str], text: str) -> int:
    return len(marker.findall(text))


def patch_markers(marker: re.Pattern[str], patch: str) -> tuple[int, int]:
    """(markers in added lines, markers in removed lines) of a file's patch."""
    lines = patch.splitlines()
    added = marker_count(marker, "\n".join(line[1:] for line in lines if line.startswith("+")))
    removed = marker_count(marker, "\n".join(line[1:] for line in lines if line.startswith("-")))
    return added, removed


def raw_contents(store: Store, path: str, ref: str) -> str | None:
    """The file's text at ``ref``; None when it does not exist there (404).

    Any other failure raises Failure.
    """
    args = ["api", f"repos/{store.repo}/contents/{path}?ref={ref}"]
    args += ["-H", "Accept: application/vnd.github.raw"]
    try:
        result = subprocess.run(["gh", *args], text=True, capture_output=True)
    except OSError as error:
        raise Failure(f"cannot run gh: {error}") from None
    if result.returncode == 0:
        return result.stdout
    if "HTTP 404" in result.stderr:
        return None
    raise Failure(f"cannot read {path} at {ref[:7]}: {result.stderr.strip()}")


def test_integrity_blockers(store: Store, head: str) -> list[str]:
    """Static test-integrity blockers: a deleted or renamed test file, or a new skip/only/xfail.

    Green CI cannot see these, because the test no longer runs. Every unreadable input is a
    blocker. The rules are siula.review.test_integrity's is_test_file() and MARKER.
    """
    marker = MARKER
    try:
        view = pr_view(store, "headRefOid,baseRefOid,changedFiles")
        files = f"repos/{store.repo}/pulls/{store.pr}/files?per_page=100"
        pages = gh_json(store, ["api", files, "--paginate", "--slurp"])
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            return ["cannot read the changed files: gh api files printed an unexpected value"]
        listed = [entry for page in pages for entry in page]
        total = view.get("changedFiles")
        if not isinstance(total, int) or len(listed) < total or total > FILES_LIMIT:
            return [
                f"cannot read the changed files: {len(listed)} of {total} changed files were listed"
            ]
        blockers: list[str] = []
        merge_base: Any = None
        for entry in listed:
            path, status = (
                (entry.get("filename"), entry.get("status"))
                if isinstance(entry, dict)
                else (None, None)
            )
            if not isinstance(path, str) or not isinstance(status, str):
                blockers.append("cannot read the changed files: an entry has no filename or status")
                continue
            previous = entry.get("previous_filename")
            if status == "removed" and is_test_file(path):
                blockers.append(f"{path} is a removed test file")
            elif status == "renamed" and isinstance(previous, str) and is_test_file(previous):
                blockers.append(f"{previous} is a renamed test file")
            if status == "removed":
                continue
            patch = entry.get("patch")
            if isinstance(patch, str):
                added, removed = patch_markers(marker, patch)
                if added > removed:
                    blockers.append(f"{path} adds {added} test marker(s) and removes {removed}")
            elif entry.get("changes") != 0:  # no patch: GitHub omits it for binary and large diffs
                if merge_base is None:
                    compare_path = f"repos/{store.repo}/compare/{view['baseRefOid']}...{head}"
                    compare = gh_json(store, ["api", compare_path])
                    base_commit = (
                        compare.get("merge_base_commit") if isinstance(compare, dict) else None
                    )
                    merge_base = base_commit.get("sha") if isinstance(base_commit, dict) else None
                    if not isinstance(merge_base, str) or not merge_base:
                        return ["cannot read the changed files: the compare read has no merge base"]
                after_text = raw_contents(store, path, head)
                before_path = previous if isinstance(previous, str) else path
                before_text = raw_contents(store, before_path, merge_base)
                if after_text is None:
                    blockers.append(f"cannot read {path} at the head")
                    continue
                before = marker_count(marker, before_text or "")
                after = marker_count(marker, after_text)
                if after > before:
                    blockers.append(
                        f"{path} has {after} test markers at the head, {before} at the merge base"
                    )
    except Failure as failure:
        return [f"cannot read the changed files: {failure}"]
    return blockers


def approval_blockers(store: Store, state: State, head: str) -> list[str]:
    """The reasons APPROVE is refused on ``head``; empty when it may post."""
    blockers: list[str] = []
    ci, _ = ci_status(store, head)
    if ci["state"] != "green":
        blockers.append("CI is not green")
    blockers += [f"inbox item {key} is untriaged" for key in sorted(inbox_keys(state))]
    blockers += [f"{entry['id']} needs a decision" for entry in open_decisions(state)]
    blockers += [
        f"{entry['id']} is a {entry.get('severity')} Fix"
        for entry in latest_entries(state)
        if entry["disposition"] == "Fix" and entry.get("severity") in BLOCKING_SEVERITIES
    ]
    blockers += test_integrity_blockers(store, head)
    return blockers


def cmd_review(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    """Reviewer mode: post the one review, on the head the last sync recorded.

    The review is the only GitHub write.
    """
    store.lock()
    state = store.require()
    if state_mode(state) != "reviewer":
        raise Failure("review needs reviewer mode")
    condition = stop_for_pending(store, state)
    if condition is not None:
        raise Failure(f"status is needs_input: {condition}")
    pending = state["pending"]
    if pending is not None:  # resumed, not reconciled yet
        raise Failure(
            f"a review of {str(pending['target'])[:7]} is unconfirmed; "
            "run sync to check the PR first"
        )
    require_running(store, state)
    head = state["head"]
    live = pr_view(store, "headRefOid")["headRefOid"]
    if live != head:
        raise Failure(
            f"the head moved to {live[:7]} since the last sync ({head[:7]}); run sync first"
        )
    viewer = viewer_login(store)
    if viewer_reviews(store, viewer, head):
        raise Failure(f"{viewer} already reviewed {head[:7]}")
    if args.event == "APPROVE":
        blockers = approval_blockers(store, state, head)
        if blockers:
            raise Failure(f"cannot approve {head[:7]}: {'; '.join(blockers)}")
        # R4: the CI read may belong to a head pushed after the last sync, so the head is read
        # again.
        live = pr_view(store, "headRefOid")["headRefOid"]
        if live != head:
            raise Failure(
                f"the head moved to {live[:7]} during the CI read ({head[:7]}); run sync first"
            )
    body = poster.sign(args.body.rstrip(), viewer)  # never posted without the footer
    state["pending"] = {"kind": "review", "target": head, "at": now(), "bodySha256": body_sha(body)}
    store.save(state)  # S4: written before the post, so an unknown result can be found again
    response = None
    with contextlib.suppress(OSError, ValueError):
        result = subprocess.run(
            ["gh", "api", f"repos/{store.repo}/pulls/{store.pr}/reviews", "--method", "POST",
             "-f", f"commit_id={head}", "-f", f"event={args.event}", "-f", f"body={body}"],
            text=True, stdout=subprocess.PIPE,
        )  # fmt: skip
        response = json.loads(result.stdout) if result.returncode == 0 else None
    review_id = response.get("id") if isinstance(response, dict) else None
    if not is_int(review_id):
        stop_for_pending(store, state)
        raise Failure("the post result is unknown; check the PR, then resume")
    record_review(state, {"id": review_id, "event": args.event}, head)
    state["pending"] = None
    store.save(state)
    return {"id": review_id, "event": args.event, "head": head}


def cmd_pause(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    store.lock()
    state = store.require()
    condition = stop_for_pending(store, state)
    if condition is not None:
        raise Failure(f"status is needs_input: {condition}")
    # One open stop at a time: a blocked run already waits for an answer.
    if state["status"] != "running":
        raise Failure(f"status is {state['status']}; pause works only from running")
    halt(state, "needs_input", "pause", args.reason)
    store.save(state)
    return summary(state)


def cmd_resume(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    store.lock()
    state = store.require()
    sid = open_stop(state)
    if sid is None:
        raise Failure(
            f"there is no open stop to answer (status is {state['status']}); nothing was recorded"
        )
    if args.max_rounds is not None and args.max_rounds <= state["round"]:
        raise Failure(f"--max-rounds must be greater than the current round ({state['round']})")
    stop = state["stops"][sid]
    # Only the open stop gets the answer.
    stop["answer"] = decision = {"text": args.decision, "by": args.by, "at": now()}
    # T1: an answer to a decision stop sends its claims back to the inbox with it, so triage
    # applies it.
    claims = set(stop["claims"]) if stop["condition"] in ANSWERING else set()
    in_inbox = inbox_keys(state)
    for entry in open_decisions(state):
        if claim_keys(entry) & claims and f"ledger:{entry['id']}" not in in_inbox:
            # Q3: the proposal its stop was asked with, as a copy.
            proposal = state["proposals"].get(entry.get("proposal") or "")
            offered = {**proposal, "options": {**proposal["options"]}} if proposal else None
            readable = {
                "claim": entry["claim"],
                "sources": entry["sources"],
                "decision": decision,
                "proposal": offered,
            }
            key = f"ledger:{entry['id']}"
            reopened = new_item("decision", entry["id"], state["head"], "resume", key, **readable)
            state["inbox"].append(reopened)
    if state["pending"] is not None:  # S4: kept, and reconciled by the next sync
        state["pending"]["resumedAt"] = decision["at"]
    state.update(status="running", last=None)
    if args.max_rounds is not None:
        state["maxRounds"] = args.max_rounds
    store.save(state)
    return summary(state)


def cmd_show(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    return store.require()


# --- command line ---------------------------------------------------------------


def pr_number(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError(f"must be a positive integer (got: {value!r})")
    return value


def positive(value: str) -> int:
    return int(pr_number(value))


def seconds(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError(f"must be a whole number of seconds (got: {value!r})")
    return int(value)


def text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


Command = Callable[[argparse.Namespace, Store], dict[str, Any]]


def build_parser(prog: str = "siula round") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Keep the review-loop round state.")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, func: Command) -> argparse.ArgumentParser:
        child = sub.add_parser(name)
        child.add_argument("repo", help="owner/repo")
        child.add_argument("pr", type=pr_number, help="PR number")
        child.set_defaults(func=func)
        return child

    sync = command("sync", cmd_sync)
    sync.add_argument("--max-rounds", type=positive)
    sync.add_argument("--mode", choices=MODES)
    sync.add_argument("--interval", type=positive)
    for option in ("--settle", "--ceiling"):
        sync.add_argument(option, type=seconds)
    command("triage", cmd_triage).add_argument("--file", required=True)
    command("findings", cmd_findings).add_argument("--file", required=True)
    review = command("review", cmd_review)
    review.add_argument("--event", required=True, choices=REVIEW_EVENTS)
    review.add_argument("--body-file", required=True)
    post = command("post", cmd_post)
    where = post.add_mutually_exclusive_group(required=True)
    where.add_argument("--thread", type=positive, metavar="COMMENT_ID")
    where.add_argument("--issue", action="store_true")
    post.add_argument("--body-file", required=True)
    post.add_argument("--resolve", action="store_true")
    command("push", cmd_push)
    command("pause", cmd_pause).add_argument("--reason", required=True, type=text)
    resume = command("resume", cmd_resume)
    resume.add_argument("--decision", required=True, type=text)
    resume.add_argument("--by", default="owner", type=text)
    resume.add_argument("--max-rounds", type=positive)
    command("show", cmd_show)
    return parser


def check_usage(args: argparse.Namespace) -> None:
    # "." and ".." would move the state directory outside the siula review root.
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repo) or {".", ".."} & set(args.repo.split("/")):
        raise UsageError(f"repository must be <owner/repo> (got: {args.repo!r})")
    if args.command in ("post", "review"):
        if args.command == "post" and args.resolve and args.thread is None:
            raise UsageError("--resolve needs --thread")
        try:
            args.body = Path(args.body_file).read_text()
        except OSError as error:
            raise UsageError(f"cannot read --body-file: {error}") from None
        if not args.body.strip():
            raise UsageError(f"--body-file is empty: {args.body_file}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        check_usage(args)
    except UsageError as error:
        parser.error(str(error))
    try:
        output = args.func(args, Store(args.repo, args.pr))
    except Failure as error:
        if error.output is not None:
            print(json.dumps(error.output, indent=2))
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
