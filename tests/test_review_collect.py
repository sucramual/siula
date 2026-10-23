"""Black-box tests for siula.review.collect, run as ``python -m siula.review.collect``.

One fake ``gh`` serves PR snapshots built with review_snapshot.pr_snapshot(). It appends one
line per call to GH_CALLS and prints the MOCK_GH_SNAPSHOT file, or MOCK_GH_LATER from the second
call on. MOCK_GH_MODE=failure fails every call; fail-after-first fails every call after the first.
The fake is the only gh on PATH (see conftest.IsolatedEnv). The reviewer-coverage tests build
their snapshots with times relative to now.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import IsolatedEnv
from review_snapshot import HEAD, comment, pr_snapshot, reaction_node, review_node, thread

COLLECT = (sys.executable, "-m", "siula.review.collect")
RunTool = Callable[..., subprocess.CompletedProcess[str]]

FAKE_GH = r"""set -euo pipefail
echo call >>"$MOCK_GH_CALLS"
calls=$(wc -l <"$MOCK_GH_CALLS")
case "${MOCK_GH_MODE:-ok}" in
  failure) exit 1 ;;
  fail-after-first) [[ $calls -eq 1 ]] || exit 1 ;;
esac
if [[ -n "${MOCK_GH_LATER:-}" && $calls -gt 1 ]]; then
  cat "$MOCK_GH_LATER"
else
  cat "$MOCK_GH_SNAPSHOT"
fi
"""

# The file, under the test root, where the fake gh records each call.
GH_CALLS = "gh-calls"


def basic_pr(*late: dict[str, Any]) -> dict[str, Any]:
    """A PR with one thread comment (11) and one issue comment (12), plus any ``late`` issue
    comments.
    """
    first = comment(11, "Guard the missing value", "review-bot", "2026-08-21T00:00:00Z")
    found = comment(12, "Concrete top-level finding", "review-bot", "2026-08-21T00:00:01Z")
    threads = [thread("thread-1", [first], path="src/example.ts", line=7)]
    return pr_snapshot(
        head="abc123", review_decision="CHANGES_REQUESTED", threads=threads, comments=[found, *late]
    )


def overflowing_pr() -> dict[str, Any]:
    """basic_pr() whose thread page reports more pages, which the collector must refuse."""
    snapshot = basic_pr()
    snapshot["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]["hasNextPage"] = True
    return snapshot


LATE_COMMENT = comment(14, "Late finding", "review-bot", "2026-08-23T00:00:00Z")


@pytest.fixture
def gh(isolated_env: IsolatedEnv, fake_exe: Callable[..., Path]) -> Callable[..., dict[str, str]]:
    """Install the fake gh.

    Return ``serve(snapshot, later=None) -> env`` for the run's environment.
    """
    fake_exe("gh", FAKE_GH)

    def serve(snapshot: dict[str, Any], later: dict[str, Any] | None = None) -> dict[str, str]:
        env = {"MOCK_GH_CALLS": str(isolated_env.root / GH_CALLS)}
        for name, data in (("MOCK_GH_SNAPSHOT", snapshot), ("MOCK_GH_LATER", later)):
            if data is not None:
                path = isolated_env.root / f"{name.lower()}.json"
                path.write_text(json.dumps(data))
                env[name] = str(path)
        return env

    return serve


@pytest.fixture
def collect(isolated_env: IsolatedEnv, gh: Callable[..., dict[str, str]], run_tool: RunTool):
    """Return ``collect(ceiling, mode=None)``, which runs the collector on basic_pr() with no
    waiting.

    ``mode`` is "overflow" (serve overflowing_pr()) or "failure" (every query fails).
    """

    def run(ceiling: str, mode: str | None = None) -> subprocess.CompletedProcess[str]:
        env = gh(overflowing_pr() if mode == "overflow" else basic_pr())
        if mode == "failure":
            env["MOCK_GH_MODE"] = "failure"
        args = [*COLLECT, "owner/repo", "7", "--floor", "0", "--idle-window", "0"]
        return run_tool(
            [*args, "--ceiling", ceiling, "--poll-interval", "0"], cwd=isolated_env.root, **env
        )

    return run


@pytest.fixture
def collect_args(isolated_env: IsolatedEnv, gh: Callable[..., dict[str, str]], run_tool: RunTool):
    """Return ``run(*options, later=None, **extra_env) -> (result, seconds)`` on basic_pr().

    ``later`` is the snapshot served from the second query on.
    """

    def run(
        *options: str, later: dict[str, Any] | None = None, **extra_env: str
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        env = {**gh(basic_pr(), later), **extra_env}
        start = time.monotonic()
        result = run_tool([*COLLECT, "owner/repo", "7", *options], cwd=isolated_env.root, **env)
        return result, time.monotonic() - start

    return run


# A --since value after every fixture timestamp, so no event counts as new feedback.
AFTER_ALL_FIXTURES = "2026-08-22T00:00:00Z"


def emits_complete_state(stdout: str) -> bool:
    """The same checks as the bash test's ``jq -e`` filter; bad JSON or a missing field counts as
    false.
    """
    try:
        snapshot: Any = json.loads(stdout)
        return (
            snapshot["headRefOid"] == "abc123"
            and snapshot["reviewDecision"] == "CHANGES_REQUESTED"
            and snapshot["reviewThreads"][0]["comments"][0]["id"] == 11
            and snapshot["issueComments"][0]["id"] == 12
        )
    except (ValueError, KeyError, IndexError, TypeError):
        return False


def test_emits_complete_pr_state(collect):
    result = collect("2")
    assert result.returncode == 0, (
        f"collector should succeed on a complete snapshot\n{result.stderr}"
    )

    assert emits_complete_state(result.stdout), (
        "collector should emit complete PR state and actionable comment sources"
    )


def test_fails_closed_on_pagination_overflow(collect):
    result = collect("2", mode="overflow")

    assert result.returncode != 0, "collector should fail closed on pagination overflow"
    assert "refusing a partial snapshot" in result.stderr, (
        "overflow failure should explain the incomplete snapshot"
    )


def test_fails_closed_when_no_query_succeeds(collect):
    result = collect("0", mode="failure")

    assert result.returncode != 0, "collector should fail closed when no query succeeds"
    assert "refusing an empty snapshot" in result.stderr, (
        "query failure should not look like a quiet PR"
    )


def test_arrival_mode_returns_when_new_feedback_settles(collect_args):
    result, seconds = collect_args(
        "--return-on-new", "0", "--poll-interval", "0", "--ceiling", "30"
    )

    assert result.returncode == 0, f"arrival mode should succeed\n{result.stderr}"
    assert emits_complete_state(result.stdout), "arrival mode should emit the new feedback"
    assert seconds < 10, (
        "arrival mode should return soon after feedback arrives, "
        f"not at the ceiling ({seconds:.1f}s)"
    )
    assert "new feedback settled" in result.stderr, "arrival mode should log why it stopped"
    assert json.loads(result.stdout)["stopReason"] == "settled", (
        "an early return should report stopReason settled"
    )


def test_arrival_mode_without_new_feedback_waits_for_the_ceiling(collect_args):
    result, seconds = collect_args(
        "--since",
        AFTER_ALL_FIXTURES,
        "--return-on-new",
        "0",
        "--poll-interval",
        "0",
        "--ceiling",
        "3",
    )

    assert result.returncode == 0, f"a quiet arrival round should still succeed\n{result.stderr}"
    assert seconds >= 1.5, f"a quiet arrival round should last until the ceiling ({seconds:.1f}s)"
    assert "ceiling reached" in result.stderr, "a quiet arrival round should stop at the ceiling"
    snapshot = json.loads(result.stdout)
    assert snapshot["headRefOid"] == "abc123", (
        "a quiet arrival round should still emit the PR state"
    )
    assert snapshot["issueComments"] == [], "a quiet arrival round should emit no new feedback"
    assert snapshot["stopReason"] == "ceiling", (
        "a quiet arrival round should report stopReason ceiling"
    )


def test_ceiling_zero_queries_once_without_waiting(collect_args, isolated_env):
    """--ceiling 0 returns the first snapshot at once. Bug: --ceiling 0 still waited one poll
    interval.

    The REVIEW_PR_CLOCK test clock records each wait instead of sleeping, so machine load cannot
    change the result.
    """
    clock = isolated_env.root / "clock"
    clock.write_text("1000")

    result, _ = collect_args(
        "--return-on-new", "0", "--poll-interval", "5", "--ceiling", "0", REVIEW_PR_CLOCK=str(clock)
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    assert clock.read_text() == "1000", (
        f"--ceiling 0 should not wait the 5 s poll interval (clock {clock.read_text()})"
    )
    assert (isolated_env.root / GH_CALLS).read_text().count("call") == 1, (
        "--ceiling 0 should query GitHub once"
    )
    assert json.loads(result.stdout)["stopReason"] == "ceiling"


def test_arrival_mode_rejects_a_settle_time_that_is_not_whole_seconds(collect_args):
    result, _ = collect_args("--return-on-new", "20s", "--ceiling", "0")

    assert result.returncode != 0, "a non-integer --return-on-new should be rejected"
    assert "--return-on-new must be a whole number" in result.stderr, (
        "the error should explain the expected value"
    )


def test_reports_quiescent_stop_reason_in_the_old_mode(collect):
    result = collect("30")

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    assert json.loads(result.stdout)["stopReason"] == "quiescent", (
        "the floor/idle-window rule should report quiescent"
    )


def test_ignored_ids_do_not_end_an_arrival_round(collect_args, isolated_env):
    ignore = isolated_env.root / "seen-ids"
    ignore.write_text("11\n\n12\n")

    result, seconds = collect_args(
        "--ignore-ids",
        str(ignore),
        "--return-on-new",
        "0",
        "--poll-interval",
        "0",
        "--ceiling",
        "3",
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    assert seconds >= 1.5, f"only ignored events should not end the round early ({seconds:.1f}s)"
    snapshot = json.loads(result.stdout)
    assert snapshot["stopReason"] == "ceiling", (
        "a round with only ignored events should run to the ceiling"
    )
    assert emits_complete_state(result.stdout), "ignored events should still appear in the output"


def test_unignored_feedback_still_ends_an_arrival_round(collect_args, isolated_env):
    ignore = isolated_env.root / "seen-ids"
    ignore.write_text("11\n")

    result, seconds = collect_args(
        "--ignore-ids",
        str(ignore),
        "--return-on-new",
        "0",
        "--poll-interval",
        "0",
        "--ceiling",
        "30",
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    assert seconds < 10, f"feedback that is not ignored should end the round early ({seconds:.1f}s)"
    assert json.loads(result.stdout)["stopReason"] == "settled", (
        "an early return should report stopReason settled"
    )
    assert emits_complete_state(result.stdout), "the output should hold both ignored and new events"


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("11\nnot-an-id\n", "one numeric id per line (line 2)"),
        (None, "file not found or not readable"),
    ],
)
def test_rejects_a_bad_ignore_ids_file(collect_args, isolated_env, content, error):
    ignore = isolated_env.root / "seen-ids"
    if content is not None:
        ignore.write_text(content)

    result, _ = collect_args("--ignore-ids", str(ignore), "--return-on-new", "0", "--ceiling", "0")

    assert result.returncode != 0, "a bad --ignore-ids file should be rejected"
    assert error in result.stderr, "the error should explain what is wrong with the file"


@pytest.mark.parametrize(
    "mode_options",
    [
        pytest.param(("--return-on-new", "30"), id="arrival"),
        pytest.param(("--floor", "30", "--idle-window", "30"), id="quiescence"),
    ],
)
def test_ceiling_returns_a_fresh_snapshot(collect_args, isolated_env, mode_options):
    result, _ = collect_args(
        "--since",
        AFTER_ALL_FIXTURES,
        *mode_options,
        "--poll-interval",
        "1.5",
        "--ceiling",
        "3",
        later=basic_pr(LATE_COMMENT),
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    assert "count=0" in result.stderr, "an earlier poll should have seen no new feedback"
    snapshot = json.loads(result.stdout)
    assert snapshot["stopReason"] == "ceiling", "the round should end at the ceiling"
    assert [comment["id"] for comment in snapshot["issueComments"]] == [14], (
        "the ceiling result should come from a final query, not the older poll"
    )


def test_fails_closed_when_the_final_ceiling_query_fails(collect_args, isolated_env):
    result, _ = collect_args(
        "--since",
        AFTER_ALL_FIXTURES,
        "--return-on-new",
        "30",
        "--poll-interval",
        "0",
        "--ceiling",
        "3",
        MOCK_GH_MODE="fail-after-first",
    )

    assert result.returncode != 0, "a failed final query should not return the older snapshot"
    assert "refusing a stale snapshot" in result.stderr, (
        "the error should explain the failed final query"
    )
    assert result.stdout == "", "a failed final query should emit no snapshot"


# --- Reviewer coverage (--reviewers) ---
#
# Each test builds its snapshot with times relative to now, because the collector compares them
# with the clock.

# siula ships no reviewers file. This is the example in docs/review-loop.md: the two bot
# reviewers that the engine was built against, each on a neutral repository.
EXAMPLE_REVIEWERS: dict[str, Any] = {
    "octo-org/hello-world": {
        "login": "chatgpt-codex-connector[bot]",
        "doneReaction": "+1",
        "retrigger": "@codex review",
    },
    "octo-org/monorepo": {"login": "claude[bot]", "retrigger": "@claude review"},
}
OLD_SHA = "0000000aaaaaaabbbbbbbcccccccdddddddeeee"
TEST_REPO = "owner/repo"
# The config login keeps "[bot]"; GraphQL review authors drop it, reaction users keep it.
TEST_LOGIN = "test-reviewer[bot]"
TEST_REVIEWERS: dict[str, Any] = {
    TEST_REPO: {"login": TEST_LOGIN, "doneReaction": "+1", "retrigger": "@test-reviewer review"}
}
# Arrival-mode options that make every early return happen at the first poll.
ARRIVAL = ("--return-on-new", "0", "--poll-interval", "0", "--ceiling", "30")
# Non-arrival options that stop at the first poll, only to read the reviewer state.
ONE_POLL = ("--floor", "0", "--idle-window", "0", "--poll-interval", "0", "--ceiling", "30")


def ago(seconds: float) -> str:
    """The UTC time `seconds` before now, in GitHub's ISO-8601 form."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def review(oid: str, login: str = "test-reviewer", body: str = "") -> dict[str, Any]:
    return review_node(21, oid, login, body, ago(30))


def reaction(content: str, seconds_ago: float, login: str = TEST_LOGIN) -> dict[str, Any]:
    return reaction_node(content, login, ago(seconds_ago))


def issue_comment(
    body: str, seconds_ago: float, login: str = "pr-author", comment_id: int = 31
) -> dict[str, Any]:
    return comment(comment_id, body, login, ago(seconds_ago))


@pytest.fixture
def coverage(isolated_env: IsolatedEnv, gh: Callable[..., dict[str, str]], run_tool: RunTool):
    """Return ``run(snapshot, *options, reviewers=TEST_REVIEWERS, repo=TEST_REPO, first_seen=360)``.

    It returns ``(result, seconds)``. ``reviewers`` is a dict written to a temp file, or a Path used
    as is. ``first_seen`` is a number of seconds ago or a raw string for ``--first-seen``; None
    omits the option. The fake gh appends one line per call to ``GH_CALLS``.
    """

    def run(
        snapshot: dict[str, Any],
        *options: str,
        reviewers: dict[str, Any] | Path = TEST_REVIEWERS,
        repo: str = TEST_REPO,
        first_seen: float | str | None = 360,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        if isinstance(reviewers, Path):
            reviewers_file = reviewers
        else:
            reviewers_file = isolated_env.root / "reviewers.json"
            reviewers_file.write_text(json.dumps(reviewers))
        args = [*COLLECT, repo, "7", "--reviewers", str(reviewers_file), *options]
        if first_seen is not None:
            args += ["--first-seen", first_seen if isinstance(first_seen, str) else ago(first_seen)]
        start = time.monotonic()
        result = run_tool(args, cwd=isolated_env.root, **gh(snapshot))
        return result, time.monotonic() - start

    return run


def reviewer_of(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    return json.loads(result.stdout)["reviewer"]


@pytest.mark.parametrize(("oid", "state"), [(HEAD, "covered"), (OLD_SHA, "waiting")])
def test_a_review_covers_only_the_head_it_names(coverage, oid, state):
    """A review by the bot covers the head it names, though GraphQL drops "[bot]" from its author.

    Guards a real GitHub shape seen on PRs 150 and 152 of the project this engine came from: the
    review author is "chatgpt-codex-connector" while the config login is
    "chatgpt-codex-connector[bot]". Bug: the review is never matched, so the run waits for a review
    that already exists.
    """
    result, _ = coverage(pr_snapshot(reviews=[review(oid)]), *ONE_POLL)

    reviewer = reviewer_of(result)
    assert reviewer["state"] == state, f"a review on {oid[:7]} should leave the reviewer {state}"
    if state == "covered":
        assert reviewer["evidence"] == f"review 21 on {HEAD[:7]}", (
            "evidence should name the review and head"
        )
        assert reviewer["login"] == TEST_LOGIN, "the output should name the configured login"
    else:
        assert reviewer["evidence"] is None, "a waiting reviewer has no evidence"


@pytest.mark.parametrize(("seconds_ago", "state"), [(400, "waiting"), (60, "covered")])
def test_a_done_reaction_counts_only_after_first_seen(coverage, seconds_ago, state):
    """A done reaction counts from the head's first-seen time; its user login keeps "[bot]".

    Guards a real GitHub shape seen on PRs 150 and 152 of the project this engine came from:
    reaction users are "...[bot]", like the config login. Bug: the login comparison strips or adds
    "[bot]" on one side only, so the done reaction is missed.
    """
    snapshot = pr_snapshot(reactions=[reaction("THUMBS_UP", seconds_ago)])

    result, _ = coverage(snapshot, *ONE_POLL, first_seen=360)

    reviewer = reviewer_of(result)
    assert reviewer["state"] == state, (
        f"a +1 from {seconds_ago}s ago (head first seen 360s ago) should be {state}"
    )
    if state == "covered":
        assert reviewer["evidence"].startswith("reaction +1 at "), (
            "evidence should name the reaction"
        )


def test_a_reaction_from_another_user_does_not_count(coverage):
    snapshot = pr_snapshot(reactions=[reaction("THUMBS_UP", 60, login="someone-else")])

    result, _ = coverage(snapshot, *ONE_POLL)

    assert reviewer_of(result)["state"] == "waiting", "only the configured login can cover the head"


def test_another_reaction_from_the_reviewer_does_not_cover_the_head(coverage):
    """Protects: only the configured doneReaction covers the head.

    Bug: any reaction by the reviewer counts as done, so Codex's eyes reaction, which only means
    "started", ends the wait before the review exists.
    """
    snapshot = pr_snapshot(reactions=[reaction("EYES", 60)])

    result, _ = coverage(snapshot, *ONE_POLL)

    reviewer = reviewer_of(result)
    assert reviewer["state"] == "waiting", "an eyes reaction is not the doneReaction"
    assert reviewer["evidence"] is None, "a waiting reviewer has no evidence"


def test_without_a_done_reaction_only_a_review_covers_the_head(coverage):
    """Protects: an entry without doneReaction is covered only by a review on the head.

    Bug: a missing doneReaction matches any reaction (or a +1 by default), so claude[bot] would
    count as done from a reaction it never uses as a done signal.
    """
    reviewers = {TEST_REPO: {"login": TEST_LOGIN, "retrigger": "@test-reviewer review"}}
    snapshot = pr_snapshot(reactions=[reaction("THUMBS_UP", 60)])

    result, _ = coverage(snapshot, *ONE_POLL, reviewers=reviewers)

    assert reviewer_of(result)["state"] == "waiting", (
        "without doneReaction, a +1 should not cover the head"
    )


def test_no_signal_after_ten_minutes_makes_a_retrigger_due(coverage):
    """Protects: a re-trigger is due RETRIGGER_AFTER (600s) after the head time with no done signal.

    Bug: the re-trigger is never due, so a reviewer that missed the push is never asked again.
    """
    result, _ = coverage(pr_snapshot(), *ONE_POLL, first_seen=660)

    reviewer = reviewer_of(result)
    assert reviewer["state"] == "waiting", "no signal should leave the reviewer waiting"
    assert reviewer["retriggerDue"] is True, (
        "no signal 11 minutes after the push should make a re-trigger due"
    )
    assert reviewer["retrigger"] == "@test-reviewer review", (
        "the output should carry the retrigger text"
    )
    assert reviewer["retriggeredAt"] is None, "nothing was re-triggered yet"


def test_no_retrigger_is_due_before_ten_minutes(coverage):
    """Protects: the re-trigger waits 600s, now that no busy signal shows a running review.

    Bug: RETRIGGER_AFTER stays at 300s, so a review that Codex started (8 to 13 minutes, with
    no done signal yet) gets a needless second "@codex review" at 5 minutes.
    """
    result, _ = coverage(pr_snapshot(), *ONE_POLL, first_seen=540)

    assert reviewer_of(result)["retriggerDue"] is False, (
        "a re-trigger should wait 10 minutes after the push"
    )


@pytest.mark.parametrize(
    ("comment", "posted"),
    [
        (issue_comment("@test-reviewer review", 60), True),
        (issue_comment("@test-reviewer review", 700), False),
        (issue_comment("@test-reviewer review", 60, login="test-reviewer"), False),
        (issue_comment("  @test-reviewer review\n", 60), True),
        (issue_comment("@test-reviewer review\n\n---\nagent on behalf of pr-author\n", 60), True),
        (
            issue_comment("**Review round 3**: re-triggered with `@test-reviewer review`.", 60),
            False,
        ),
    ],
    ids=[
        "after-push",
        "before-push",
        "by-reviewer",
        "exact-with-whitespace",
        "with-agent-footer",
        "quoted-in-summary",
    ],
)
def test_an_existing_retrigger_comment_after_the_push_is_not_repeated(coverage, comment, posted):
    """Only a comment that is exactly the re-trigger text (trimmed) counts as a re-trigger.

    Bug: the collector matched any comment that contains the text, so a round summary quoting
    "@codex review" started the timeout clock although no re-trigger was posted.
    """
    result, _ = coverage(pr_snapshot(comments=[comment]), *ONE_POLL, first_seen=660)

    reviewer = reviewer_of(result)
    if posted:
        assert reviewer["retriggeredAt"] == comment["createdAt"], (
            "retriggeredAt should be the comment time"
        )
        assert reviewer["retriggerDue"] is False, "a posted re-trigger should not be due again"
    else:
        assert reviewer["retriggeredAt"] is None, (
            "only a non-reviewer comment after the push is a re-trigger"
        )
        assert reviewer["retriggerDue"] is True, "the re-trigger should still be due"


def test_arrival_mode_returns_retrigger_when_one_is_due(coverage):
    result, seconds = coverage(pr_snapshot(), *ARRIVAL, first_seen=660)

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    assert json.loads(result.stdout)["stopReason"] == "retrigger", (
        "a due re-trigger should end the round"
    )
    assert seconds < 10, f"the round should return at once, not at the ceiling ({seconds:.1f}s)"


def test_no_timeout_before_the_retrigger_is_posted(coverage):
    result, seconds = coverage(pr_snapshot(), *ARRIVAL, first_seen=1260)

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    output = json.loads(result.stdout)
    assert output["reviewer"]["timedOut"] is False, (
        "with a retrigger configured, 21 minutes alone should not time out"
    )
    assert output["reviewer"]["retriggerDue"] is True, (
        "21 minutes with no retrigger comment should make it due"
    )
    assert output["stopReason"] == "retrigger", (
        "the round should return to post the retrigger, not to time out"
    )
    assert seconds < 10, f"the round should return at once, not at the ceiling ({seconds:.1f}s)"


@pytest.mark.parametrize(("retriggered_ago", "timed_out"), [(660, True), (540, False)])
def test_the_timeout_runs_from_the_retrigger_comment(
    coverage, isolated_env, retriggered_ago, timed_out
):
    # The agent records its own retrigger post as seen, so it is not new feedback.
    seen = isolated_env.root / "seen-ids"
    seen.write_text("31\n")
    snapshot = pr_snapshot(comments=[issue_comment("@test-reviewer review", retriggered_ago)])

    result, seconds = coverage(
        snapshot,
        "--return-on-new",
        "0",
        "--poll-interval",
        "0",
        "--ceiling",
        "3",
        "--ignore-ids",
        str(seen),
        first_seen=900,
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    output = json.loads(result.stdout)
    assert output["reviewer"]["timedOut"] is timed_out, (
        f"a retrigger {retriggered_ago}s ago should {'' if timed_out else 'not '}time out "
        "(wait is 10 minutes)"
    )
    assert output["reviewer"]["retriggerDue"] is False, "a posted retrigger should not be due again"
    if timed_out:
        assert output["stopReason"] == "timeout", (
            "arrival mode should return when the reviewer wait expires"
        )
        assert seconds < 10, f"the round should return at once, not at the ceiling ({seconds:.1f}s)"
    else:
        assert output["stopReason"] == "ceiling", "a recent retrigger should not end the round"


@pytest.mark.parametrize(
    ("retriggered_ago", "timed_out"),
    [(540, False), (660, True)],
    ids=["9m-after-retrigger", "11m-after-retrigger"],
)
def test_after_a_retrigger_the_timeout_fires_ten_minutes_after_it_not_earlier(
    coverage, retriggered_ago, timed_out
):
    """Protects: after a re-trigger, the wait ends TIMEOUT_AFTER_RETRIGGER (600s) after it.

    The head time is 1300s ago, past REVIEWER_TIMEOUT. Bugs: the head timeout still applies after a
    re-trigger and cuts the reviewer off 9 minutes after it; or the wait after a re-trigger is still
    REVIEWER_TIMEOUT - RETRIGGER_AFTER, so 11 minutes after it does not time out. The test above has
    a head time inside REVIEWER_TIMEOUT, so it cannot see the first bug.
    """
    snapshot = pr_snapshot(comments=[issue_comment("@test-reviewer review", retriggered_ago)])

    result, _ = coverage(snapshot, *ONE_POLL, first_seen=1300)

    reviewer = reviewer_of(result)
    assert reviewer["retriggeredAt"] is not None, "the retrigger comment should be found"
    assert reviewer["timedOut"] is timed_out, (
        f"a retrigger {retriggered_ago}s ago should {'' if timed_out else 'not '}time out "
        "(wait is 10 minutes)"
    )


NO_RETRIGGER_REVIEWERS = {
    TEST_REPO: {k: v for k, v in TEST_REVIEWERS[TEST_REPO].items() if k != "retrigger"}
}


@pytest.mark.parametrize(
    ("first_seen", "timed_out"),
    [(1260, True), (1140, False), (660, False)],
    ids=["21m", "19m", "11m"],
)
def test_without_a_retrigger_the_wait_expires_twenty_minutes_after_first_seen(
    coverage, first_seen, timed_out
):
    """Protects: with no retrigger configured, the reviewer times out REVIEWER_TIMEOUT (1200s) after
    the head time.

    Bugs: the timeout stays at 600s, so an 8 to 13 minute review is cut off at 11 minutes; or it
    never fires, so the round waits for a reviewer that will not come.
    """
    result, _ = coverage(
        pr_snapshot(), *ONE_POLL, reviewers=NO_RETRIGGER_REVIEWERS, first_seen=first_seen
    )

    reviewer = reviewer_of(result)
    assert reviewer["timedOut"] is timed_out, (
        f"no retrigger and {first_seen}s since first seen: timedOut {timed_out}"
    )
    assert reviewer["retriggerDue"] is False, "a reviewer without a retrigger is never due one"


def test_arrival_mode_returns_timeout_without_a_retrigger(coverage):
    result, seconds = coverage(
        pr_snapshot(), *ARRIVAL, reviewers=NO_RETRIGGER_REVIEWERS, first_seen=1260
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    assert json.loads(result.stdout)["stopReason"] == "timeout", (
        "an expired wait should end the round"
    )
    assert seconds < 10, f"the round should return at once, not at the ceiling ({seconds:.1f}s)"


@pytest.mark.parametrize(
    ("first_seen", "stop_reason"),
    [(660, "retrigger"), (1260, "timeout")],
    ids=["retrigger-due", "timed-out"],
)
def test_reviewer_returns_wait_for_new_feedback_to_settle(coverage, first_seen, stop_reason):
    # The comment is new feedback seen at the first poll, so it settles 3 seconds later.
    snapshot = pr_snapshot(comments=[issue_comment("One more finding", 30)])
    reviewers = TEST_REVIEWERS if stop_reason == "retrigger" else NO_RETRIGGER_REVIEWERS
    options = ("--return-on-new", "3", "--poll-interval", "0.5", "--ceiling", "30")

    result, seconds = coverage(snapshot, *options, reviewers=reviewers, first_seen=first_seen)

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    output = json.loads(result.stdout)
    assert output["stopReason"] == "settled", (
        f"a {stop_reason} return should wait until new feedback settles"
    )
    # The collector clock has whole seconds, so a 3-second settle can end after about 2 seconds.
    assert seconds >= 2, f"the round should wait for the settle time ({seconds:.1f}s)"
    assert [comment["id"] for comment in output["issueComments"]] == [31], (
        "the settled batch should be returned"
    )
    reviewer = output["reviewer"]
    assert reviewer["retriggerDue" if stop_reason == "retrigger" else "timedOut"] is True, (
        f"the reviewer {stop_reason} state should still be reported after the batch settles"
    )


def test_arrival_mode_returns_covered_when_the_reviewer_covers_the_head(coverage):
    snapshot = pr_snapshot(reviews=[review(HEAD, body="One finding")])

    result, seconds = coverage(snapshot, *ARRIVAL, first_seen=1260)

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    output = json.loads(result.stdout)
    assert output["stopReason"] == "covered", (
        "covering the head should end the round, even after 20 minutes"
    )
    assert output["reviewer"]["timedOut"] is False, "a covered reviewer never times out"
    assert output["reviews"][0]["body"] == "One finding", (
        "the round should still carry the review feedback"
    )
    assert seconds < 10, f"the round should return at once, not at the ceiling ({seconds:.1f}s)"


def test_unconfigured_repo_does_not_end_an_arrival_round_on_reviewer_grounds(coverage):
    snapshot = pr_snapshot()

    result, seconds = coverage(
        snapshot,
        "--return-on-new",
        "0",
        "--poll-interval",
        "0",
        "--ceiling",
        "3",
        repo="owner/other",
        first_seen=660,
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    output = json.loads(result.stdout)
    assert output["reviewer"] == {
        "login": None,
        "state": "unconfigured",
        "evidence": None,
        "retrigger": None,
        "retriggeredAt": None,
        "retriggerDue": False,
        "timedOut": False,
    }, "a repo without an entry should report an unconfigured reviewer"
    assert output["stopReason"] == "ceiling", (
        "an unconfigured reviewer should not end the round early"
    )
    assert seconds >= 1.5, f"the round should run to the ceiling ({seconds:.1f}s)"


def test_repo_key_matches_case_insensitively(coverage):
    reviewers = {"Owner/Repo": TEST_REVIEWERS[TEST_REPO]}

    result, _ = coverage(pr_snapshot(reviews=[review(HEAD)]), *ONE_POLL, reviewers=reviewers)

    assert reviewer_of(result)["state"] == "covered", (
        "the repo key should match without regard to case"
    )


# --- Head time (--first-seen) ---


@pytest.mark.parametrize(("seconds_ago", "state"), [(400, "waiting"), (60, "covered")])
def test_an_offset_time_compares_in_utc(coverage, seconds_ago, state):
    """A time with a UTC offset (here --first-seen as +02:00) compares with Z times as the same
    instant.

    Guards the ISO-8601 forms seen on PRs 150 and 152 of the project this engine came from (Z times
    from GitHub, offset times from local clocks). Bug: an offset time is parsed as UTC or rejected,
    so a reaction counts too early or never.
    """
    plus_two = timezone(timedelta(hours=2))
    first_seen = (datetime.now(plus_two) - timedelta(seconds=360)).strftime(
        "%Y-%m-%dT%H:%M:%S+02:00"
    )

    result, _ = coverage(
        pr_snapshot(reactions=[reaction("THUMBS_UP", seconds_ago)]),
        *ONE_POLL,
        first_seen=first_seen,
    )

    assert reviewer_of(result)["state"] == state, (
        f"a +1 {seconds_ago}s ago (head first seen 360s ago) is {state}"
    )


@pytest.mark.parametrize(
    ("first_seen", "expected"),
    [
        ("2026-10-13T09:00:00Z", "2026-10-13T09:00:00Z"),
        ("2026-10-10T09:45:56+02:00", "2026-10-10T07:45:56Z"),
    ],
    ids=["utc", "offset"],
)
def test_head_pushed_at_is_the_first_seen_time(coverage, first_seen, expected):
    result, _ = coverage(pr_snapshot(), *ONE_POLL, first_seen=first_seen)

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    assert json.loads(result.stdout)["headPushedAt"] == expected, (
        "headPushedAt should be --first-seen in UTC"
    )


def test_without_first_seen_no_reaction_retrigger_or_timeout_applies(coverage):
    snapshot = pr_snapshot(reactions=[reaction("THUMBS_UP", 60)])

    result, seconds = coverage(
        snapshot, "--return-on-new", "0", "--poll-interval", "0", "--ceiling", "3", first_seen=None
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    output = json.loads(result.stdout)
    assert output["headPushedAt"] is None, "without --first-seen there is no head time"
    reviewer = output["reviewer"]
    assert reviewer["state"] == "waiting", (
        "without a head time, a +1 reaction should not cover the head"
    )
    assert reviewer["retriggerDue"] is False, "without a head time, no retrigger should be due"
    assert reviewer["timedOut"] is False, "without a head time, the reviewer should not time out"
    assert output["stopReason"] == "ceiling", (
        "without a head time, the round should not end on reviewer grounds"
    )
    assert seconds >= 1.5, f"the round should run to the ceiling ({seconds:.1f}s)"


@pytest.mark.parametrize("value", ["2026-10-13 09:00:00", "yesterday", "2026-13-45T99:99:99Z", ""])
def test_rejects_a_malformed_first_seen(coverage, isolated_env, value):
    result, _ = coverage(pr_snapshot(), *ONE_POLL, first_seen=value)

    assert result.returncode != 0, "a malformed --first-seen should be rejected"
    assert "--first-seen must be an ISO 8601 time" in result.stderr, (
        f"the error should explain the value\n{result.stderr}"
    )
    assert result.stdout == "", "a rejected --first-seen should emit no snapshot"
    assert not (isolated_env.root / GH_CALLS).exists(), (
        "a rejected --first-seen should not query GitHub"
    )


def test_fails_closed_when_reactions_overflow(coverage):
    """Protects: a PR with more reactions than one page refuses the snapshot.

    Bug: the reactions page limit is not checked, so the reviewer's +1 on page two is missed and
    the round waits, re-triggers, or times out on a head that is already covered.
    """
    snapshot = pr_snapshot()
    snapshot["data"]["repository"]["pullRequest"]["reactions"]["pageInfo"]["hasNextPage"] = True

    result, _ = coverage(snapshot, *ONE_POLL)

    assert result.returncode != 0, "collector should fail closed when reactions overflow"
    assert "refusing a partial snapshot" in result.stderr, (
        "the failure should explain the incomplete snapshot"
    )


@pytest.mark.parametrize("repo", sorted(EXAMPLE_REVIEWERS))
def test_the_example_reviewers_file_is_valid_for_each_repo(coverage, repo):
    result, _ = coverage(pr_snapshot(), *ONE_POLL, reviewers=EXAMPLE_REVIEWERS, repo=repo)

    reviewer = reviewer_of(result)
    assert reviewer["state"] != "unconfigured", f"{repo} should have a reviewer in the example"


def test_the_example_codex_reviewer_is_covered_by_a_thumbs_up(coverage):
    repo = "octo-org/hello-world"
    login = EXAMPLE_REVIEWERS[repo]["login"]
    snapshot = pr_snapshot(
        reactions=[reaction("EYES", 200, login), reaction("THUMBS_UP", 60, login)]
    )

    result, seconds = coverage(
        snapshot, *ARRIVAL, reviewers=EXAMPLE_REVIEWERS, repo=repo, first_seen=240
    )

    assert result.returncode == 0, f"collector should succeed\n{result.stderr}"
    output = json.loads(result.stdout)
    assert output["reviewer"]["state"] == "covered", "a +1 after the push should cover the head"
    assert output["stopReason"] == "covered", (
        "arrival mode should return once the reviewer covered the head"
    )
    assert seconds < 10, f"the round should return at once ({seconds:.1f}s)"


@pytest.mark.parametrize(
    ("content", "error"),
    [
        pytest.param(None, "--reviewers file not found or not readable", id="missing-file"),
        pytest.param("not json", "must hold one JSON object keyed by owner/repo", id="not-json"),
        pytest.param("[]", "must hold one JSON object keyed by owner/repo", id="not-an-object"),
        pytest.param(
            '{"owner/repo": []}', 'entry "owner/repo" must be an object', id="entry-not-an-object"
        ),
        pytest.param(
            '{"owner/repo": {"retrigger": "x"}}',
            "login must be a non-empty string",
            id="missing-login",
        ),
        pytest.param(
            '{"owner/repo": {"login": ""}}', "login must be a non-empty string", id="empty-login"
        ),
        pytest.param(
            '{"owner/repo": {"login": "x", "doneReaction": "THUMBS_UP"}}',
            'doneReaction "THUMBS_UP" is not one of +1 -1 laugh hooray confused heart rocket eyes',
            id="unknown-done-reaction",
        ),
        pytest.param(
            '{"owner/repo": {"login": "x", "doneReaction": 1}}',
            "doneReaction 1 is not one of",
            id="non-string-reaction",
        ),
        pytest.param(
            '{"owner/repo": {"login": "x", "retrigger": 1}}',
            "retrigger must be a non-empty string",
            id="non-string-retrigger",
        ),
        pytest.param(
            '{"owner/repo": {"login": "x", "busy": ["reaction:eyes"]}}',
            'unknown key "busy"',
            id="unknown-key-busy",
        ),
        pytest.param(
            '{"owner/repo": {"login": "x", "done": ["review"]}}',
            'unknown key "done"',
            id="unknown-key-done",
        ),
        pytest.param(
            '{"owner/repo": {"login": "x"}, "OWNER/REPO": {"login": "y"}}',
            "more than one entry matches",
            id="duplicate-entry",
        ),
    ],
)
def test_rejects_a_bad_reviewers_file(isolated_env, gh, run_tool, content, error):
    """Protects: a bad --reviewers file stops the collector with a clear error before any gh call.

    Bugs: a typo such as "THUMBS_UP" (the GraphQL name, not the REST name) or a leftover "busy"
    key is accepted and silently never matches, so the round waits for a signal that cannot come;
    or the check runs only after a query, so a bad file still spends a GitHub call.
    """
    reviewers = isolated_env.root / "reviewers.json"
    if content is not None:
        reviewers.write_text(content)
    calls = isolated_env.root / GH_CALLS

    result = run_tool(
        [*COLLECT, TEST_REPO, "7", "--reviewers", str(reviewers), *ONE_POLL],
        cwd=isolated_env.root,
        **gh(pr_snapshot()),
    )

    assert result.returncode != 0, "a bad --reviewers file should be rejected"
    assert "ERROR: --reviewers file" in result.stderr and error in result.stderr, (
        f"the error should explain what is wrong with the file\n{result.stderr}"
    )
    assert result.stdout == "", "a rejected --reviewers file should emit no snapshot"
    assert not calls.exists(), "a rejected --reviewers file should not query GitHub"
