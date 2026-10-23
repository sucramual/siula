"""Black-box tests for ``siula round``, the review-loop engine, built around its invariants.

S1-S5 are the state rules, T1-T4 the stop rules, and R1-R4 the reviewer-mode rules in
docs/review-loop.md; each test's docstring starts with the rule it protects and names the bug it
catches. "#<n>" names a PR of the private project where the engine was first built; the tests for
its 2026-10-09/10 failures on PRs #150, #152, and #154 also name the PR.

Each test writes its own reviewers file (REVIEWERS) to the temp HOME's ~/.config/siula, and runs
the installed ``siula round`` (which imports siula.review.collect and posts through
siula.review.poster) and resolve-thread.sh on that temp HOME.

The PR is built with review_snapshot.pr_snapshot() (base_pr below); tests derive each change,
such as a new head, comment, or reaction, from it in code.

Fakes, controlled through files in ``ctl``. No real gh is on PATH (see conftest.IsolatedEnv).
- gh: ``api graphql`` serves ctl/pr.json; ``pr view`` reports its head plus ctl/view.json. REST
  ``commits/<sha>/<list>`` serves ctl/rest_<list>.json (check_runs, status, check_suites); the
  default check suites are one completed github-actions suite with one run.
  ``pr checks --required`` serves ctl/checks.json, and ``pr checks`` serves ctl/all_checks.json
  (or checks.json); a missing file fails as gh 2.102.0 does. ``repo view`` reports ctl/cwd_repo
  as the working directory's repository (default: the PR's). A call that names no repository
  acts on ctl/cwd_repo, and every call outside ctl/pr_repo fails, as on GitHub. A sha in
  ctl/move_head_on_checks becomes the head during the CI read. The resolve mutation marks the
  thread resolved. ctl/graphql_fail makes queries fail. REST ``pulls/<pr>/reviews`` lists
  ctl/rest_reviews.json and, with ``--method POST``, adds the viewer's review (ctl/review_fail:
  the post fails; ctl/review_postfail: it is accepted, then the call exits 1). ``api user``
  reports ctl/viewer, also with ``--jq .login`` (ctl/viewer_fail makes it fail). Calls are logged
  to ctl/gh.log.
- gh comment posts: ``api --method POST repos/<repo>/pulls/<pr>/comments/<id>/replies`` and
  ``.../issues/<pr>/comments`` with ``--input -`` read the poster's JSON body, log it to
  ctl/posts.log, add the comment by the viewer to ctl/pr.json, and print the id and the html_url,
  issue_url, or pull_request_url. FAKE_POST=fail exits 1 without posting; postfail posts, then
  exits 1; noid prints JSON without an id. FAKE_POST_REPO answers for another repository (a
  misroute): the comment does not reach the PR, and the receipt names that repository.
- git: logs to ctl/git.log, then runs the real git.
- clock: one clock in ctl/clock (epoch seconds), which the collector reads through REVIEW_PR_CLOCK.
  Its waits advance the clock, so they are instant and deterministic. siula round gets the same
  time through REVIEW_PR_NOW.
"""

import contextlib
import fcntl
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import SIULA, IsolatedEnv
from review_snapshot import comment, next_id, pr_snapshot, reaction_node, review_node, thread

TESTS = Path(__file__).resolve().parent
REPO = "octo-org/hello-world"
FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CODEX = "chatgpt-codex-connector[bot]"
RunTool = Callable[..., subprocess.CompletedProcess[str]]

FAKE_GH = r"""import json, os, re, sys
from pathlib import Path

ctl = Path(os.environ["FAKE_CTL"])
args = sys.argv[1:]
with open(ctl / "gh.log", "a") as log:
    log.write(json.dumps(args) + "\n")
cwd_repo = (ctl / "cwd_repo").read_text()
if args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": cwd_repo}))
    sys.exit(0)
if args[:2] == ["api", "user"]:
    # The viewer, as `gh api user` reports it; ctl/viewer_fail makes the call fail.
    if (ctl / "viewer_fail").exists():
        sys.exit("HTTP 502: Bad Gateway (user)")
    login = (ctl / "viewer").read_text()
    if args[2:] == ["--jq", ".login"]:
        print(login)
    elif args[2:] == []:
        print(json.dumps({"login": login}))
    else:
        sys.exit("fake gh: unexpected options for api user")
    sys.exit(0)
# Like gh: --repo/-R names the repository, GraphQL takes owner and name, and a REST call names it
# in its path; anything else uses the cwd's.
flags = dict(zip(args, args[1:]))
named = flags.get("--repo") or flags.get("-R")
fields = dict(arg.split("=", 1) for arg in args if "=" in arg and not arg.startswith("query="))
if args[:2] == ["api", "graphql"] and "owner" in fields:
    named = fields["owner"] + "/" + fields.get("repo", fields.get("name", ""))
rest = [arg for arg in args[1:] if arg.startswith("repos/")] if args[0] == "api" else []
api_path = rest[0] if rest else ""
if api_path:
    named = "/".join(api_path.split("/")[1:3])
if (named or cwd_repo).lower() != (ctl / "pr_repo").read_text().lower():
    sys.exit(f"GraphQL: Could not resolve to a PullRequest in {named or cwd_repo}")
method = flags.get("--method", "GET")
reply = re.fullmatch(r"repos/([^/]+/[^/]+)/pulls/([0-9]+)/comments/([0-9]+)/replies", api_path)
issue = re.fullmatch(r"repos/([^/]+/[^/]+)/issues/([0-9]+)/comments", api_path)
data = json.loads((ctl / "pr.json").read_text())
pr = data["data"]["repository"]["pullRequest"]
if args[:2] == ["pr", "view"]:
    files = ctl / "files_pages.json"
    pages = json.loads(files.read_text()) if files.exists() else [[]]
    listed_count = sum(len(page) for page in pages)
    served = {"headRefOid": pr["headRefOid"], "baseRefOid": "b" * 40, "changedFiles": listed_count,
              **json.loads((ctl / "view.json").read_text())}
    # Real gh prints only the requested fields.
    wanted = args[args.index("--json") + 1].split(",") if "--json" in args else list(served)
    print(json.dumps({name: served[name] for name in wanted if name in served}))
elif args[:2] == ["pr", "checks"]:
    move = ctl / "move_head_on_checks"
    if move.exists():
        pr["headRefOid"] = move.read_text().strip()
        (ctl / "pr.json").write_text(json.dumps(data))
        move.unlink()
    required, every = ctl / "checks.json", ctl / "all_checks.json"
    served = required if "--required" in args else (every if every.exists() else required)
    if not served.exists():
        # gh 2.102.0, observed 2026-10-14 on a sandbox PR: a head without checks
        # prints this on stderr, nothing on stdout, and exits 1, with or without --required.
        # The message for "checks, but none required" could not be observed; it is assumed here.
        none_required = "--required" in args and every.exists()
        reported = "no required checks" if none_required else "no checks"
        sys.exit(reported + " reported on the 'feature' branch")
    print(served.read_text())
elif method == "POST" and (reply or issue):
    # A comment post as siula.review.poster sends it: {"body": ...} on stdin with --input -.
    from datetime import datetime, timezone

    from review_snapshot import comment, next_id  # on PYTHONPATH from Round.run

    if args[args.index("--input") + 1] != "-":
        sys.exit("fake gh: a comment post reads its JSON body from stdin")
    body = json.loads(sys.stdin.read())["body"]
    with open(ctl / "posts.log", "a") as log:
        log.write(json.dumps({"path": api_path, "body": body}) + "\n")
    mode = os.environ.get("FAKE_POST", "ok")
    if mode == "fail":
        sys.exit("HTTP 502: Bad Gateway (" + api_path + ")")  # nothing was posted
    if mode == "noid":
        print('{"message": "Created"}')
        sys.exit(0)
    repo = os.environ.get("FAKE_POST_REPO") or named  # FAKE_POST_REPO stands in for a misroute
    number = (reply or issue)[2]
    new_id = next_id(pr)
    now = datetime.fromtimestamp(int((ctl / "clock").read_text()), timezone.utc)
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    node = comment(new_id, body, (ctl / "viewer").read_text(), at)
    if reply:
        target = int(reply[3])
        threads = pr["reviewThreads"]["nodes"]
        ids = [[c["databaseId"] for c in t["comments"]["nodes"]] for t in threads]
        thread = next(t for t, in_thread in zip(threads, ids) if target in in_thread)
        thread["comments"]["nodes"].append(node)
    else:
        pr["comments"]["nodes"].append(node)
    if repo.lower() == (ctl / "pr_repo").read_text().lower():
        (ctl / "pr.json").write_text(json.dumps(data))
    if mode == "postfail":
        sys.exit("the connection dropped after GitHub accepted the post")
    if reply:
        urls = {"html_url": f"https://github.com/{repo}/pull/{number}#discussion_r{new_id}",
                "pull_request_url": f"https://api.github.com/repos/{repo}/pulls/{number}"}
    else:
        urls = {"html_url": f"https://github.com/{repo}/pull/{number}#issuecomment-{new_id}",
                "issue_url": f"https://api.github.com/repos/{repo}/issues/{number}"}
    print(json.dumps({"id": new_id, **urls}))
elif args[0] == "api" and args[1].split("?", 1)[0].endswith("/reviews") and "/pulls/" in args[1]:
    # REST reviews of a PR: a GET lists ctl/rest_reviews.json (default: none); `--method POST`
    # creates a review by the viewer from the -f fields. ctl/review_fail fails before GitHub
    # accepts it; ctl/review_postfail exits 1 after it was accepted (the post result is unknown).
    listed = ctl / "rest_reviews.json"
    reviews = json.loads(listed.read_text()) if listed.exists() else []
    if method == "POST":
        if (ctl / "review_fail").exists():
            sys.exit("HTTP 502: Bad Gateway (" + args[1] + ")")
        created = {"id": 7000 + len(reviews) + 1, "user": {"login": (ctl / "viewer").read_text()},
                   "commit_id": fields["commit_id"], "state": fields["event"],
                   "body": fields["body"]}
        listed.write_text(json.dumps(reviews + [created]))
        if (ctl / "review_postfail").exists():
            sys.exit("the connection dropped after GitHub accepted the review")
        print(json.dumps(created))
    elif "--slurp" in args:
        # `--paginate --slurp` prints an array of page arrays; ctl/rest_review_pages.json sets
        # the pages.
        paged = ctl / "rest_review_pages.json"
        print(paged.read_text() if paged.exists() else json.dumps([reviews]))
    else:
        print(json.dumps(reviews))
elif args[0] == "api" and "/pulls/" in args[1] and args[1].split("?", 1)[0].endswith("/files"):
    # The PR's changed files; `--paginate --slurp` prints an array of page arrays
    # (ctl/files_pages.json).
    if (ctl / "files_fail").exists():
        sys.exit("HTTP 502: Bad Gateway (" + args[1] + ")")
    paged = ctl / "files_pages.json"
    print(paged.read_text() if paged.exists() else "[[]]")
elif args[0] == "api" and "/compare/" in args[1]:
    if (ctl / "compare_fail").exists():
        sys.exit("HTTP 502: Bad Gateway (" + args[1] + ")")
    print(json.dumps({"merge_base_commit": {"sha": "m" * 40}}))
elif args[0] == "api" and "/contents/" in args[1]:
    # Raw file text at a ref, from ctl/contents/<ref>/<path>; a missing file is a 404, and
    # ctl/contents_fail a 502.
    if "Accept: application/vnd.github.raw" not in args:
        sys.exit("the fake serves only the raw media type")
    path, ref = args[1].split("/contents/", 1)[1].split("?ref=", 1)
    served = ctl / "contents" / ref / path
    if (ctl / "contents_fail").exists():
        sys.exit("HTTP 502: Bad Gateway (" + args[1] + ")")
    if not served.exists():
        sys.exit("gh: Not Found (HTTP 404)")
    sys.stdout.write(served.read_text())
elif args[0] == "api" and args[1].startswith("repos/"):
    # REST: repos/<o>/<r>/commits/<sha>/check-runs, .../status, or .../check-suites?per_page=100;
    # the query is ignored, and a missing file is a failed call.
    listing = args[1].split("?", 1)[0].rsplit("/", 1)[1].replace("-", "_")
    served = ctl / ("rest_" + listing + ".json")
    if not served.exists():
        sys.exit("HTTP 502: Bad Gateway (" + args[1] + ")")
    print(served.read_text())
elif "resolveReviewThread(" in " ".join(args):
    thread_id = next(arg.split("=", 1)[1] for arg in args if arg.startswith("threadId="))
    for thread in pr["reviewThreads"]["nodes"]:
        thread["isResolved"] = thread["isResolved"] or thread["id"] == thread_id
    (ctl / "pr.json").write_text(json.dumps(data))
    print('{"data": {"resolveReviewThread": {"thread": {"isResolved": true}}}}')
elif (ctl / "graphql_fail").exists():
    sys.exit("HTTP 502: Bad Gateway")
else:
    print(json.dumps(data))
"""

REVIEWERS = {REPO: {"login": CODEX, "doneReaction": "+1", "retrigger": "@codex review"}}

GITHUB_URL = f"https://github.com/{REPO}.git"
CWD_CHECK = json.dumps(["repo", "view", "--json", "nameWithOwner"])
CHECKOUT_READS = (
    "config --get remote.origin.url",
    "config --get-all remote.origin.pushurl",
    "status --porcelain",
)
FAKE_GIT = (
    'if [[ -n "${FAKE_CTL:-}" ]]; then printf "%s\\n" "$*" >>"$FAKE_CTL/git.log"; fi\n'
    'exec {git} "$@"\n'
)
PASS = [{"name": "checks", "bucket": "pass"}]
PENDING_TEXT = "a post to thread {} may or may not have happened; check the PR, then resume"


def suite(app: str, status: str, runs: int) -> dict:
    """One check suite as REST lists it; an idle app keeps a queued suite with no check runs."""
    conclusion = "success" if status == "completed" else None
    return {
        "app": {"slug": app},
        "status": status,
        "conclusion": conclusion,
        "latest_check_runs_count": runs,
    }


def check_run(name: str, status: str, app: str = "github-actions") -> dict:
    """One check run as REST lists it under commits/<sha>/check-runs."""
    conclusion = "success" if status == "completed" else None
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"slug": app}}


# The check run that Copilot's automatic review adds (a private repository with no CI, 2026-10-23).
COPILOT = "copilot-pull-request-reviewer"


def epoch(text: str) -> int:
    return int(datetime.strptime(text, FORMAT).replace(tzinfo=UTC).timestamp())


def iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime(FORMAT)


def base_pr() -> dict:
    """The PR the tests start from, built from real nodes: two Codex review threads from #150, and
    three issue comments plus Codex's +1 reaction (at 08:56:44) from #154."""
    bot = "chatgpt-codex-connector"
    threads = [
        thread(
            "PRRT_kwDORd5rp86rCQ9L",
            [
                comment(
                    4236931467, "P2: exclude the agent's own replies", bot, "2026-10-10T07:54:17Z"
                )
            ],
            path="docs/review-loop.md",
            line=80,
        ),
        thread(
            "PRRT_kwDORd5rp86rCdqn",
            [
                comment(
                    4237011925, "P2: query once more at the ceiling", bot, "2026-10-10T08:19:14Z"
                )
            ],
            path="scripts/poll-and-collect.sh",
            line=313,
        ),
    ]
    review_round = "**Review round 4 (aab71a1, no push): edges**"
    comments = [
        comment(
            6095746489, "<!-- codex-pull-request-review-summary -->", bot, "2026-10-10T08:37:36Z"
        ),
        comment(6095816171, "### Codex Review", bot, "2026-10-10T08:46:57Z"),
        comment(6095880989, review_round, "octocat", "2026-10-10T08:55:55Z"),
    ]
    reactions = [reaction_node("THUMBS_UP", CODEX, "2026-10-10T08:56:44Z")]
    head = "aab71a1d1f34045997403a953db40b6ab2b38352"
    return pr_snapshot(head=head, threads=threads, comments=comments, reactions=reactions)


class Round:
    """siula round for octo-org/hello-world#<pr> on a temp HOME, with the fakes above."""

    def __init__(self, env: IsolatedEnv, run_tool: RunTool, start: str) -> None:
        self.root, self.run_tool, self.pr, self.isolated = env.root, run_tool, "154", env
        # Where siula reads the reviewers file when XDG_CONFIG_HOME is not set.
        self.reviewers = env.home / ".config" / "siula" / "reviewers.json"
        self.reviewers.parent.mkdir(parents=True)
        self.reviewers.write_text(json.dumps(REVIEWERS))
        self.ctl = env.root / "ctl"
        self.ctl.mkdir()
        (self.ctl / "pr.json").write_text(json.dumps(base_pr()))
        self.set_view(state="OPEN", isCrossRepository=False)
        self.set_checks(PASS)
        (self.ctl / "cwd_repo").write_text(REPO)
        (self.ctl / "viewer").write_text("octocat")
        (self.ctl / "pr_repo").write_text(REPO)
        self.set_rest_checks(check_runs=0, statuses=0)
        self.set_suites([suite("github-actions", "completed", 1)])
        (self.ctl / "clock").write_text(str(epoch(start)))
        real_git = shutil.which("git")
        fakes = {
            "gh": f"#!{sys.executable}\n{FAKE_GH}",
            "git": "#!/usr/bin/env bash\n" + FAKE_GIT.replace("{git}", str(real_git)),
        }
        for name, body in fakes.items():
            (env.bin / name).write_text(body)
            (env.bin / name).chmod(0o755)
        state_root = env.home / ".local" / "state" / "siula" / "review"
        self.state_dir = state_root / "octo-org" / "hello-world" / self.pr
        self.state_file = self.state_dir / "state.json"

    @property
    def now(self) -> str:
        return iso(int((self.ctl / "clock").read_text()))

    def advance(self, seconds: int) -> None:
        (self.ctl / "clock").write_text(str(epoch(self.now) + seconds))

    def set_files(self, files: list, pages: list | None = None) -> None:
        """The PR's changed files as REST lists them (one page unless ``pages`` splits them)."""
        (self.ctl / "files_pages.json").write_text(
            json.dumps(pages if pages is not None else [files])
        )

    def set_content(self, ref: str, path: str, text: str) -> None:
        """The raw text of ``path`` at ``ref``, as the contents API serves it."""
        served = self.ctl / "contents" / ref / path
        served.parent.mkdir(parents=True, exist_ok=True)
        served.write_text(text)

    def set_view(self, **fields: Any) -> None:
        (self.ctl / "view.json").write_text(json.dumps({"author": {"login": "octocat"}, **fields}))

    def set_rest_checks(self, check_runs: int | list | dict | None, statuses: int | None) -> None:
        """The head's check runs and commit statuses as REST reports them; None makes that call
        fail.

        ``check_runs`` is a count of completed runs named "checks", a list of run dicts, or a whole
        response body. ``statuses`` is the commit-status total.
        """
        if isinstance(check_runs, int):
            check_runs = [check_run("checks", "completed")] * check_runs
        if isinstance(check_runs, list):
            check_runs = {"total_count": len(check_runs), "check_runs": check_runs}
        status = {"total_count": statuses} if statuses is not None else None
        for name, body in (("rest_check_runs.json", check_runs), ("rest_status.json", status)):
            with contextlib.suppress(FileNotFoundError):
                (self.ctl / name).unlink()
            if body is not None:
                (self.ctl / name).write_text(json.dumps(body))

    def set_suites(
        self, suites: list | None, total: int | None = None, raw: str | None = None
    ) -> None:
        """The head's check suites as REST lists them: total_count defaults to the list length; raw
        replaces the whole response; None makes the call fail.
        """
        served = self.ctl / "rest_check_suites.json"
        with contextlib.suppress(FileNotFoundError):
            served.unlink()
        if raw is not None:
            served.write_text(raw)
        elif suites is not None:
            served.write_text(
                json.dumps(
                    {"total_count": len(suites) if total is None else total, "check_suites": suites}
                )
            )

    def set_checks(self, required: list | None, every: list | None = None) -> None:
        for name, checks in (("checks.json", required), ("all_checks.json", every)):
            with contextlib.suppress(FileNotFoundError):
                (self.ctl / name).unlink()
            if checks is not None:
                (self.ctl / name).write_text(json.dumps(checks))

    @contextlib.contextmanager
    def pr_data(self) -> Iterator[dict]:
        """Edit the PR that the fake gh serves."""
        data = json.loads((self.ctl / "pr.json").read_text())
        yield data["data"]["repository"]["pullRequest"]
        (self.ctl / "pr.json").write_text(json.dumps(data))

    def add_comment(self, body: str, author: str = "chatgpt-codex-connector") -> int:
        """A new PR comment from the reviewer at the current time. Returns its id."""
        with self.pr_data() as pr:
            new_id = next_id(pr)
            pr["comments"]["nodes"].append(comment(new_id, body, author, self.now))
        return new_id

    def edit_comment(self, database_id: int, body: str) -> None:
        """Edit a thread comment, review, or issue comment in place; GitHub keeps its id."""
        with self.pr_data() as pr:
            nodes = [c for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]]
            nodes += pr["reviews"]["nodes"] + pr["comments"]["nodes"]
            (node,) = [node for node in nodes if node["databaseId"] == database_id]
            node["body"] = body

    def add_review(self, body: str, author: str = "monalisa") -> int:
        """A COMMENTED review with a body on the current head, at the current time. Returns its
        id.
        """
        with self.pr_data() as pr:
            new_id = next_id(pr)
            pr["reviews"]["nodes"].append(
                review_node(new_id, pr["headRefOid"], author, body, self.now)
            )
        return new_id

    def react(self, content: str = "THUMBS_UP") -> None:
        with self.pr_data() as pr:
            pr["reactions"]["nodes"].append(reaction_node(content, CODEX, self.now))

    def set_head(self, sha: str) -> None:
        with self.pr_data() as pr:
            pr["headRefOid"] = sha

    def pr_branch(self) -> Path:
        """A bare origin whose PR branch "feature" has one commit, and a work clone on it."""
        origin, work = self.root / "origin.git", self.root / "work"
        self.isolated.git(self.root, "init", "-q", "--bare", str(origin))
        self.isolated.git(self.root, "clone", "-q", str(origin), str(work))
        # origin names the GitHub repository; insteadOf sends fetch and push to the local bare repo.
        self.isolated.git(work, "remote", "set-url", "origin", GITHUB_URL)
        self.isolated.git(work, "config", f"url.{origin}.insteadOf", GITHUB_URL)
        self.isolated.git(work, "switch", "-q", "-c", "feature")
        head = self.commit(work, "feat: first")
        self.isolated.git(work, "push", "-q", "origin", "feature")
        self.set_head(head)
        self.set_view(state="OPEN", isCrossRepository=False, headRefName="feature")
        return work

    def synced_branch(self) -> Path:
        """pr_branch(), then a first sync whose items a noise triage handled. Returns the work
        clone.
        """
        work = self.pr_branch()
        self.ok("sync")
        self.noise()
        return work

    def checkout(self) -> Path:
        """An empty git checkout whose origin names the PR's repository (enough for push's origin
        check).
        """
        path = self.root / "checkout"
        if not path.exists():
            self.isolated.git(self.root, "init", "-q", str(path))
            self.isolated.git(path, "remote", "add", "origin", GITHUB_URL)
        return path

    def commit(self, work: Path, message: str) -> str:
        """An empty commit in the work clone. Returns its SHA."""
        self.isolated.git(work, "commit", "-q", "--allow-empty", "-m", message)
        return self.isolated.git(work, "rev-parse", "HEAD")

    def origin_head(self) -> str:
        return self.isolated.git(self.root / "origin.git", "rev-parse", "feature")

    def run(
        self, command: str, *options: str, cwd: Path | None = None, repo: str = REPO, **env: Any
    ) -> subprocess.CompletedProcess[str]:
        args = [SIULA, "round", command, repo, self.pr, *options]
        clock = {"REVIEW_PR_NOW": self.now, "REVIEW_PR_CLOCK": str(self.ctl / "clock")}
        env = {"FAKE_CTL": str(self.ctl), **clock, "PYTHONPATH": str(TESTS), **env}
        return self.run_tool(args, cwd=cwd or self.root, **env)

    def ok(self, command: str, *options: str, cwd: Path | None = None, **env: Any) -> Any:
        result = self.run(command, *options, cwd=cwd, **env)
        assert result.returncode == 0, f"{command} {options} should succeed\n{result.stderr}"
        return json.loads(result.stdout)

    def refused(self, error: str, command: str, *options: str, **env: Any) -> None:
        """Run a command that must refuse with ``error``, and check that it called nothing."""
        before = self.calls()
        result = self.run(command, *options, **env)
        assert (result.returncode, result.stdout) == (1, ""), (
            f"{command} should refuse\n{result.stdout}"
        )
        assert f"ERROR: {error}" in result.stderr, (
            f"{command} should explain the refusal\n{result.stderr}"
        )
        assert self.calls() == before, "nothing should run after a refusal"

    def quiet_sync(self) -> dict:
        """Run sync where it must observe nothing: no gh, git, or comment-post call."""
        before = self.calls()
        output = self.ok("sync")
        assert self.calls() == before, "sync should observe nothing here"
        return output

    def calls(self) -> tuple[list, list, list]:
        """The logged gh calls, comment posts, and git calls; a comment post is also a gh call.

        The read-only checkout checks (``gh repo view`` before post; the origin URL, the origin
        push URLs, and ``status --porcelain`` before push) are left out, so "nothing ran" means
        nothing after them.
        """
        logs = (self.ctl / "gh.log", self.ctl / "posts.log", self.ctl / "git.log")
        gh, posts, git = (log.read_text().splitlines() if log.exists() else [] for log in logs)
        git = [c for c in git if not c.endswith(CHECKOUT_READS)]
        return [c for c in gh if c != CWD_CHECK], posts, git

    def resume_condition(self) -> str | None:
        """The open stop's detail, which siula round shows as the resumeCondition."""
        open_stops = [stop for stop in self.state()["stops"].values() if stop["answer"] is None]
        return open_stops[0]["detail"] if open_stops else None

    def state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def write(self, name: str, content: str) -> str:
        (self.root / name).write_text(content)
        return str(self.root / name)

    def triage(self, entries: list) -> Any:
        return self.ok("triage", "--file", self.write("triage.json", json.dumps(entries)))

    def noise(self) -> Any:
        """Triage every inbox item as noise: [] is valid only for an empty inbox (D4). Each item
        adds a ledger entry.
        """
        return self.triage(noise_for([item["key"] for item in self.state()["inbox"]]))

    def post(self, *options: str, body: str, **env: Any) -> subprocess.CompletedProcess[str]:
        return self.run("post", *options, "--body-file", self.write("body.md", body), **env)


@pytest.fixture
def make(isolated_env: IsolatedEnv, run_tool: RunTool) -> Callable[[str], Round]:
    return lambda start: Round(isolated_env, run_tool, start)


def synced(make: Callable[[str], Round], start: str, *options: str) -> Round:
    """A run whose first sync returned every existing item (round 1), and a noise triage handled
    them.

    The five noise entries are L1-L5, so a test's first claim is L6.
    """
    h = make(start)
    first = h.ok("sync", *options)
    assert (first["next"], first["round"]) == ("triage", 1), first
    h.noise()
    return h


COVERED = "2026-10-10T08:56:00Z"  # before the reviewer's +1 at 08:56:44, so the head is covered
WAITING = "2026-10-10T08:57:00Z"  # after the +1, so the head is not covered
NEW_HEAD = "f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0"
FIX = {
    "claim": "Guard the empty cursor",
    "class": "edges",
    "disposition": "Fix",
    "sources": ["issue:6095816171"],
}
DESIGN = {**FIX, "class": "design", "disposition": "Needs decision"}
NOT_FIXED = {"claim": FIX["claim"], "class": None, "disposition": "No change", "sources": []}
# Q3: a batch with a Needs decision entry holds one proposal element.
OPTIONS = ["Keep the cursor; guard the empty case", "Drop the cursor; read every page"]
PROPOSAL = {
    "proposal": {
        "summary": "Root: one cursor guards every page read.",
        "options": OPTIONS,
        "recommended": "A",
    }
}


# --- S1: one writer, atomic replace ---------------------------------------------------------


def test_S1_a_writer_is_refused_while_the_lock_is_held(make):
    """S1: two commands for one PR never write at once. Bug: no lock, so a second writer's state
    wins.
    """
    h = make(COVERED)
    h.ok("sync")
    before = h.state_file.read_bytes()
    with open(h.state_dir / "state.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        h.refused("another siula round command is running for octo-org/hello-world#154", "sync")
        h.refused(
            "another siula round command is running for octo-org/hello-world#154",
            "pause",
            "--reason",
            "x",
        )
        assert h.ok("show")["round"] == 1, "show takes no lock"
    assert h.state_file.read_bytes() == before


def test_S1_writes_leave_no_temp_file(make):
    """S1: each write replaces state.json atomically. Bug: a temp file is left behind (or written in
    place).
    """
    h = synced(make, COVERED)
    h.triage([FIX])
    h.ok("pause", "--reason", "check")
    h.ok("resume", "--decision", "go on")

    assert sorted(path.name for path in h.state_dir.iterdir()) == ["state.json", "state.lock"]


@pytest.mark.parametrize(
    "content", ["{not json", '{"version": 1, "status": "running", "decisions": []}']
)
@pytest.mark.parametrize("command", ["sync", "pause", "show"])
def test_S1_unreadable_state_is_never_overwritten(make, content: str, command: str):
    """S1: a corrupt or newer state file stays as it is. Bug: sync treats it as missing and replaces
    it.
    """
    h = make(COVERED)
    h.state_dir.mkdir(parents=True)
    h.state_file.write_text(content)

    h.refused(
        f"state file {h.state_file} is unreadable",
        command,
        *(["--reason", "x"] if command == "pause" else []),
    )

    assert h.state_file.read_text() == content


def test_show_without_state_is_refused(make):
    """show names the missing state. Bug: show prints an empty object and looks like a fresh run."""
    make(COVERED).refused("no state for octo-org/hello-world#154; run sync", "show")


# --- S2: ids enter the handled set only through sync ------------------------------------------


def test_S2_triage_and_post_refuse_ids_that_sync_never_returned(make):
    """S2: the ledger and replies cite only feedback that sync returned.

    Bug: a source id or reply target from the agent's memory is accepted, so the state gets ahead
    of the PR (a claim with no observed comment, or a reply to a comment that was never triaged).
    """
    h = synced(make, COVERED)
    before = h.state_file.read_bytes()

    h.refused(
        "triage entry 0: sources ['issue:999'] were never returned by sync",
        "triage",
        "--file",
        h.write("t.json", json.dumps([{**FIX, "sources": ["issue:999"]}])),
    )
    h.refused(
        "comment 999 was never returned by sync",
        "post",
        "--thread",
        "999",
        "--body-file",
        h.write("b.md", "Fixed."),
    )

    assert h.state_file.read_bytes() == before


def test_S2_triage_refuses_the_runs_own_comment_as_a_source(make):
    """S2 (Q1): a claim source is an inbox key or an archived item. The run's own comments, which
    post marks handled so that sync skips them, are refused by name, and nothing is recorded.

    Bug: triage accepted every handled key as a source, so a claim could cite the run's own reply;
    sync never returns it, so triage never archives it, and the ledger named a source with no item.
    """
    h = synced(make, COVERED)
    reply = h.post(
        "--thread", "4237011925", body="Fixed in 64a1289: the ceiling queries once more."
    )
    note = h.post("--issue", body="Review round 1: edges")
    assert (reply.returncode, note.returncode) == (0, 0), reply.stderr + note.stderr
    own = [
        f"thread:{json.loads(reply.stdout)['commentId']}",
        f"issue:{json.loads(note.stdout)['commentId']}",
    ]
    assert set(own) <= set(h.state()["handled"]), "post marks each of its own comments handled"
    before = h.state_file.read_bytes()

    error = (
        f"triage entry 0: sources {own} are this run's own comments, which sync never returns; "
        "cite the feedback they answer; nothing was recorded"
    )
    h.refused(
        error, "triage", "--file", h.write("t.json", json.dumps([{**NOT_FIXED, "sources": own}]))
    )

    assert h.state_file.read_bytes() == before, "the ledger, items, and handled keys are unchanged"


def test_S2_own_reply_is_handled_and_rounds_keep_counting(make):
    """S2 (#150): post adds only its own id, so the next sync neither returns it nor ends early.

    Bug: post does not record its id, so the next sync settles on the run's own reply (it comes
    back in newItems as triage work); or a new process resets the round count.
    """
    h = synced(make, WAITING)
    handled = h.state()["handled"]
    reply = h.post(
        "--thread", "4237011925", body="Fixed in 64a1289: the ceiling queries once more."
    )
    assert reply.returncode == 0, reply.stderr
    reply_id = json.loads(reply.stdout)["commentId"]
    assert h.state()["handled"] == [*handled, f"thread:{reply_id}"], "post adds only its own key"
    started = epoch(h.now)

    quiet = h.ok("sync", "--ceiling", "120")

    assert (quiet["newItems"], quiet["next"], quiet["round"]) == ([], "wait_reviewer", 1)
    assert epoch(h.now) - started >= 120, "the run's own reply must not end the wait early"
    late = h.add_comment("P2: the ceiling query can fail too.")
    again = h.ok("sync")
    assert ([item["id"] for item in again["newItems"]], again["round"]) == ([late], 2), (
        "rounds keep counting"
    )


def test_S2_returned_items_stay_in_the_inbox_until_triage(make):
    """S2/T1: sync returns the inbox, keyed by kind, until triage handles it, and counts it once.

    Bug: sync marked items handled when it returned them, so a crash between sync and triage lost
    them, and the next sync reached done on feedback nobody triaged.
    """
    h = make(COVERED)
    first = h.ok("sync")
    keys = [item["key"] for item in first["newItems"]]
    assert keys == [f"{item['kind']}:{item['id']}" for item in first["newItems"]]
    assert {key.split(":")[0] for key in keys} == {"thread", "issue"}, (
        "keys name the kind, so the id spaces cannot collide"
    )
    h.advance(60)

    again = h.ok("sync")  # a new process; the agent crashed before triage

    assert ([item["key"] for item in again["newItems"]], again["next"], again["round"]) == (
        keys,
        "triage",
        1,
    )
    assert h.state()["status"] == "running", "an inbox that was never triaged must not reach done"
    triaged = h.noise()
    assert triaged["status"] == "running"
    done = h.ok("sync")
    assert (done["newItems"], done["next"]) == ([], "done")
    assert set(keys) <= set(h.state()["handled"])


# The items of base_pr() in the order that the first sync returns them.
BASE_KEYS = [
    "thread:4236931467",
    "thread:4237011925",
    "issue:6095746489",
    "issue:6095816171",
    "issue:6095880989",
]


def noise_for(keys: list, reason: str = "a bot notice with no code claim") -> list:
    return [{"noise": key, "reason": reason} for key in keys]


@pytest.mark.parametrize("covered", [[], BASE_KEYS[:4]], ids=["empty-list", "one-key-left-out"])
def test_S2_a_triage_that_leaves_an_inbox_key_uncovered_is_refused(make, covered: list):
    """S2 (D4): every inbox key is cited by a claim or listed as noise; [] is valid only for an
    empty inbox.

    Bug (the engine before D4): any triage, even [], marked the whole inbox handled, so a real
    code claim was acknowledged with no ledger entry, no reply, and no decision.
    """
    h = make(COVERED)
    assert [item["key"] for item in h.ok("sync")["newItems"]] == BASE_KEYS
    before = h.state_file.read_bytes()
    uncovered = [key for key in BASE_KEYS if key not in covered]

    error = f"triage leaves inbox keys uncovered: {uncovered}"
    h.refused(error, "triage", "--file", h.write("t.json", json.dumps(noise_for(covered))))

    assert h.state_file.read_bytes() == before, (
        "the ledger, the handled keys, and the inbox are unchanged"
    )


# Noise for every base item except the one that FIX cites.
NOISE_BUT_FIX = noise_for([key for key in BASE_KEYS if key != FIX["sources"][0]])


@pytest.mark.parametrize(
    ("entries", "error"),
    [
        (
            [{"noise": BASE_KEYS[0]}, *noise_for(BASE_KEYS[1:])],
            "entry 0: noise needs a reason: a non-empty single line of at most 200 characters",
        ),
        (noise_for(BASE_KEYS, " "), "entry 0: noise needs a reason: a non-empty single line"),
        (
            noise_for(BASE_KEYS, "one\ntwo"),
            "entry 0: noise needs a reason: a non-empty single line",
        ),
        (noise_for(BASE_KEYS, "x" * 201), "entry 0: noise needs a reason: a non-empty single line"),
        (noise_for([*BASE_KEYS, "issue:999"]), "entry 5: noise issue:999 is not in the inbox"),
        (
            noise_for([*BASE_KEYS, BASE_KEYS[0]]),
            "entry 5: noise thread:4236931467 is already listed by triage entry 0",
        ),
        (
            [*noise_for(BASE_KEYS), FIX],
            "entry 3: issue:6095816171 is both noise and a source of triage entry 5",
        ),
        (
            [*NOISE_BUT_FIX, {**FIX, "class": None, "disposition": "Noise"}],
            "entry 4: disposition Noise is only for a noise entry",
        ),
        (
            [{**noise_for(BASE_KEYS)[0], "class": None}, *noise_for(BASE_KEYS[1:])],
            "entry 0: unknown fields ['class']",
        ),
    ],
    ids=[
        "no-reason",
        "blank-reason",
        "two-line-reason",
        "long-reason",
        "key-not-in-the-inbox",
        "noise-twice",
        "noise-and-cited",
        "claim-with-disposition-noise",
        "unknown-field",
    ],
)
def test_S2_triage_refuses_an_invalid_noise_entry(make, entries: list, error: str):
    """S2 (D4): a noise entry names one inbox key with a one-line reason, once, and no claim cites
    it.

    Bug: a noise entry without a reason, outside the inbox, listed twice, or also cited was
    accepted, so the ledger could not say why an item needed no response; or a claim entry used
    the Noise disposition and skipped the claim rules.
    """
    h = make(COVERED)
    h.ok("sync")
    before = h.state_file.read_bytes()

    h.refused(f"triage {error}", "triage", "--file", h.write("t.json", json.dumps(entries)))

    assert h.state_file.read_bytes() == before, (
        "the ledger, the handled keys, and the inbox are unchanged"
    )


def test_S2_a_red_check_is_never_noise(make):
    """S2 (D4): a check key cannot be listed as noise; a red check is always an attribution claim.

    Bug: a red check listed as noise was handled with no attribution, so nobody decided whether
    this PR caused it.
    """
    h = make(COVERED)
    h.set_checks([{"name": "checks", "bucket": "fail"}])
    first = h.ok("sync")
    key = f"check:{first['head']}:checks"
    assert [item["key"] for item in first["newItems"]] == [*BASE_KEYS, key]
    before = h.state_file.read_bytes()

    error = f"triage entry 5: noise {key}: a red check is never noise; triage it as a claim"
    h.refused(
        error, "triage", "--file", h.write("t.json", json.dumps(noise_for([*BASE_KEYS, key])))
    )

    assert h.state_file.read_bytes() == before, (
        "the ledger, the handled keys, and the inbox are unchanged"
    )


def test_S2_noise_is_a_ledger_entry_and_its_key_becomes_handled(make):
    """S2 (D4): each noise entry is stored as a ledger entry with its reason, and its key becomes
    handled.

    Bug: noise left no record, so the ledger could not show why an item got no reply; or the
    noise key stayed in the inbox, so sync returned it again.
    """
    h = make(COVERED)
    head = h.ok("sync")["head"]
    reasons = [f"progress notice {n}" for n in range(1, 6)]

    h.triage(
        [{"noise": key, "reason": reason} for key, reason in zip(BASE_KEYS, reasons, strict=False)]
    )

    state = h.state()
    fields = ("id", "round", "head", "claim", "class", "disposition", "sameAs", "sources")
    expected = [
        {
            "id": f"L{n}",
            "round": 1,
            "head": head,
            "claim": reason,
            "class": None,
            "disposition": "Noise",
            "sameAs": None,
            "sources": [key],
        }  # fmt: skip
        for n, (key, reason) in enumerate(zip(BASE_KEYS, reasons, strict=False), start=1)
    ]
    assert [{field: entry[field] for field in fields} for entry in state["ledger"]] == expected
    assert (state["handled"], state["inbox"]) == (BASE_KEYS, [])
    assert h.ok("sync")["next"] == "done", "noise asks for no response"


def test_S2_a_reopened_decision_is_covered_only_by_a_claim_with_its_sameAs(make):
    """S2 (D4): an inbox ledger:L<n> item is covered by a claim entry with sameAs L<n>, never by
    noise or by a claim that only cites the same sources.

    Bug: a decided claim was acknowledged by a new, unlinked entry or by noise, so its chain kept
    Needs decision as the latest entry and the decision was never applied.
    """
    h = make(COVERED)
    h.ok("sync")
    h.triage([{**DESIGN, "sources": BASE_KEYS}, PROPOSAL])
    assert h.ok("sync")["resumeCondition"] == "decision owed on L1"
    h.ok("resume", "--decision", "keep the cursor as it is")
    assert [item["key"] for item in h.ok("sync")["newItems"]] == ["ledger:L1"]
    before = h.state_file.read_bytes()
    applied = {
        "claim": DESIGN["claim"],
        "class": None,
        "disposition": "No change",
        "sources": BASE_KEYS,
    }

    h.refused(
        "triage leaves inbox keys uncovered: ['ledger:L1']",
        "triage",
        "--file",
        h.write("t.json", json.dumps([applied])),
    )
    error = (
        "triage entry 0: noise ledger:L1: a reopened decision is never noise; "
        "triage it with sameAs L1"
    )
    h.refused(error, "triage", "--file", h.write("t.json", json.dumps(noise_for(["ledger:L1"]))))

    assert h.state_file.read_bytes() == before, (
        "the ledger, the handled keys, and the inbox are unchanged"
    )
    h.triage([{**applied, "sameAs": "L1"}])
    assert h.ok("sync")["next"] == "done"


# --- S2: learning signals (Q1-Q4, the owner 2026-10-19) ------------------------------------------


def test_S2_triage_archives_each_covered_item_by_key(make):
    """S2 (Q1): triage stores each inbox item it covers in items, by key, as sync returned it, in
    the write that marks the key handled; every ledger source resolves to an item. A reopened
    decision (ledger:L<n>) is not archived: the ledger and the stops already hold it.

    Bug: triage cleared the inbox and kept only the keys, so the text behind a ledger entry was lost
    when a reviewer edited or deleted the comment on GitHub.
    """
    h = make(COVERED)
    returned = h.ok("sync")["newItems"]
    h.noise()

    state = h.state()
    archived = {item["key"]: {**item, "truncated": False} for item in returned}
    assert state.get("items") == archived, "each covered item is stored as sync returned it"
    assert {key for entry in state["ledger"] for key in entry["sources"]} == set(archived), (
        "each source resolves"
    )
    key = red_check(h)
    check = h.state()["inbox"][0]
    h.triage([{**DESIGN, "claim": "checks fail on a flaky runner", "sources": [key]}, PROPOSAL])
    assert h.ok("sync")["resumeCondition"] == "decision owed on L6"
    h.ok("resume", "--decision", "B, retry the runner later")
    assert [item["key"] for item in h.ok("sync")["newItems"]] == ["ledger:L6"], (
        "the decision is reopened"
    )
    h.triage(
        [{**NOT_FIXED, "claim": "checks fail on a flaky runner", "sameAs": "L6", "sources": [key]}]
    )

    items = h.state()["items"]
    assert items == {**archived, key: {**check, "truncated": False}}, (
        "a check is archived; ledger:L6 is not"
    )


def test_S2_a_long_body_is_archived_cut_to_8000_characters(make):
    """S2 (Q4): triage archives at most 8,000 characters of a body, and truncated says whether it
    cut.

    Bug: one long bot comment grew the state file without limit; or a cut body looked complete;
    or a body of exactly 8,000 characters was marked as cut.
    """
    h = make(COVERED)
    long_id = h.add_comment("x" * 8999 + "y")
    exact_id = h.add_comment("z" * 8000)
    short_id = h.add_comment("P2: short")
    h.ok("sync")
    h.noise()

    items = h.state().get("items") or {}
    stored = {n: items.get(f"issue:{n}", {}) for n in (long_id, exact_id, short_id)}
    assert {n: (item.get("body"), item.get("truncated")) for n, item in stored.items()} == {
        long_id: ("x" * 8000, True),
        exact_id: ("z" * 8000, False),
        short_id: ("P2: short", False),
    }


# A finding that a reviewer adds to a comment by editing it; GitHub keeps the comment's id.
EDITED = "P1: the cursor also skips the last page; guard it too."
THREAD_ID, ISSUE_ID, STATUS_ID = 4237011925, 6095816171, 6095746489


@pytest.mark.parametrize("kind", ["thread", "issue", "review"])
def test_S2_an_edited_comment_returns_to_triage_as_a_revision(make, kind: str):
    """S2 (Q1): sync compares each comment's body with the latest text the run holds for it. A
    changed body enters the inbox as <key>@<n>, triage covers it, and the archive keeps every text
    under its own key. A body that did not change adds nothing.

    Bug (Codex review of #4): sync dropped every observed key that was handled or in the inbox, so
    a finding that a reviewer added by an edit never reached triage, and a covered head with green
    CI went to done.
    """
    h = make(COVERED)
    ids = {"thread": THREAD_ID, "issue": ISSUE_ID}
    if kind == "review":
        ids["review"] = h.add_review("Please guard the empty cursor.")
    h.ok("sync")
    h.noise()
    key = f"{kind}:{ids[kind]}"
    first_text = h.state()["items"][key]["body"]
    edited_text = f"{first_text}\n\n{EDITED}"
    h.advance(60)
    h.edit_comment(ids[kind], edited_text)

    edited = h.ok("sync", "--ceiling", "60")

    assert ([item["key"] for item in edited["newItems"]], edited["next"]) == (
        [f"{key}@2"],
        "triage",
    ), edited
    assert (edited["ci"]["state"], edited["reviewer"]["state"]) == ("green", "covered"), (
        "every gate holds, so only the revision keeps the run from done"
    )
    revision = edited["newItems"][0]
    assert (revision["kind"], revision["id"], revision["body"]) == (kind, ids[kind], edited_text)
    assert revision["revises"] == key
    claim = {**NOT_FIXED, "claim": "The last page is already guarded", "sources": [f"{key}@2"]}
    h.triage([claim])
    items = h.state()["items"]
    assert (items[key]["body"], items[f"{key}@2"]["body"]) == (first_text, edited_text), (
        "the archive keeps both texts under their own keys"
    )
    unchanged = h.ok("sync", "--ceiling", "60")
    assert (unchanged["newItems"], unchanged["next"]) == ([], "done"), (
        "an unchanged body adds nothing"
    )
    h.advance(60)
    h.edit_comment(ids[kind], first_text)  # back to the first text: new against the latest one
    reverted = h.ok("sync", "--ceiling", "60")
    assert [item["key"] for item in reverted["newItems"]] == [f"{key}@3"], reverted
    assert h.state()["items"][key]["body"] == first_text, "the first archive entry never changes"


def test_S2_a_comment_edited_while_its_item_waits_in_the_inbox_is_triaged_in_both_texts(make):
    """S2 (Q1): an edit while the item waits in the inbox adds <key>@2 next to it. Triage must
    cover both keys, and the archive keeps the text that each one had.

    Bug: the inbox kept the text from the first sync, so triage never saw the edit, and the
    archive stored the old text as if it were the whole comment.
    """
    h = make(COVERED)
    first = h.ok("sync")
    first_keys = [item["key"] for item in first["newItems"]]
    key = f"thread:{THREAD_ID}"
    old = next(item for item in first["newItems"] if item["key"] == key)["body"]
    new = f"{old}\n\n{EDITED}"
    h.advance(60)
    h.edit_comment(THREAD_ID, new)

    second = h.ok("sync", "--ceiling", "60")

    assert [item["key"] for item in second["newItems"]] == [*first_keys, f"{key}@2"], second
    assert second["newItems"][-1]["body"] == new
    assert next(item for item in second["newItems"] if item["key"] == key)["body"] == old
    error = f"triage leaves inbox keys uncovered: ['{key}@2']"
    h.refused(error, "triage", "--file", h.write("t.json", json.dumps(noise_for(first_keys))))
    h.noise()
    items = h.state()["items"]
    assert (items[key]["body"], items[f"{key}@2"]["body"]) == (old, new)


def test_D3_a_fix_that_cites_a_thread_revision_waits_for_a_reply_on_its_thread(make):
    """D3: a revision of a thread comment belongs to its thread. A pushed Fix that cites
    thread:<id>@<n> needs a reply receipt on thread:<id>, and post --thread <id> records it.

    Bug: the reply check matched only thread:<id> keys, so a Fix that cited a revision reached
    done with no reply on the reviewer's thread; or post --thread <id> recorded no receipt on it.
    """
    h = make(COVERED)
    work = h.synced_branch()
    h.advance(60)
    h.edit_comment(THREAD_ID, EDITED)
    revised = h.ok("sync", "--ceiling", "60")
    assert [item["key"] for item in revised["newItems"]] == [f"thread:{THREAD_ID}@2"], revised
    h.triage([{**FIX, "sources": [f"thread:{THREAD_ID}@2"]}])
    new_head = h.commit(work, "fix: guard the last page")
    h.ok("push", cwd=work)
    h.set_head(new_head)
    h.react()

    unreplied = h.ok("sync")
    reply = h.post("--thread", str(THREAD_ID), body=f"Fixed in {new_head[:7]}.")
    done = h.ok("sync")

    assert (unreplied["next"], unreplied["reason"]) == ("triage", "unreplied fix L6"), unreplied
    assert reply.returncode == 0, reply.stderr
    entry = h.state()["ledger"][-1]
    reply_id = json.loads(reply.stdout)["commentId"]
    assert (entry["id"], entry["sources"], entry.get("replies")) == (
        "L6",
        [f"thread:{THREAD_ID}@2"],
        {f"thread:{THREAD_ID}": reply_id},
    )
    assert done["next"] == "done", done


def test_S2_codexs_edited_status_comment_is_a_revision_that_noise_settles(make):
    """S2: Codex edits its status comment in place on each review. The edit is a revision that
    triage lists as noise, and the run then reaches done.

    Bug: the edit was dropped (a finding added that way never reached triage), or a noise revision
    stopped the run.
    """
    h = synced(make, COVERED)
    h.advance(60)
    status = "<!-- codex-pull-request-review-summary -->\nCodex reviewed the latest commit."
    h.edit_comment(STATUS_ID, status)

    edited = h.ok("sync", "--ceiling", "60")

    assert ([item["key"] for item in edited["newItems"]], edited["next"]) == (
        [f"issue:{STATUS_ID}@2"],
        "triage",
    ), edited
    h.noise()
    done = h.ok("sync", "--ceiling", "60")
    assert (done["newItems"], done["next"]) == ([], "done"), done
    state = h.state()
    assert (state["status"], state["target"]) == ("succeeded", "ready_for_human_merge")
    assert state["ledger"][-1]["disposition"] == "Noise"


def test_S2_an_edit_to_the_runs_own_comment_adds_no_revision(make):
    """S2: the run's own comments stay out of triage, edited or not; post marks them handled and
    sync never archives them.

    Bug: a revision check that treated every handled key as triaged feedback returned the run's
    own comment to triage after an edit.
    """
    h = synced(make, COVERED)
    posted = h.post("--issue", body="Round 1: nothing to fix.")
    assert posted.returncode == 0, posted.stderr
    own = json.loads(posted.stdout)["commentId"]
    h.advance(60)
    h.edit_comment(own, "Round 1: nothing to fix, as the owner edited it.")

    after = h.ok("sync", "--ceiling", "60")

    assert f"issue:{own}" in h.state()["handled"], "the own comment is handled"
    assert (after["newItems"], after["next"]) == ([], "done"), after


@pytest.mark.parametrize("edit", ["unchanged", "edited-after-the-cut"])
def test_S2_a_long_body_is_compared_in_full_not_by_its_archived_cut(make, edit: str):
    """S2 (Q4): the archive keeps 8,000 characters of a body, so sync compares the hash of the
    whole body (bodySha256), not the cut text.

    Bugs: an unchanged long body looked edited on every sync, because the full text never equals
    the cut; or an edit after character 8,000 was missed.
    """
    h = make(COVERED)
    long_id = h.add_comment("x" * 8999 + "y")
    h.ok("sync")
    h.noise()
    assert h.state()["items"][f"issue:{long_id}"]["truncated"] is True
    h.advance(60)
    if edit == "edited-after-the-cut":
        h.edit_comment(long_id, "x" * 8999 + "z")

    after = h.ok("sync", "--ceiling", "60")

    expected = [f"issue:{long_id}@2"] if edit == "edited-after-the-cut" else []
    assert [item["key"] for item in after["newItems"]] == expected, after


@pytest.mark.parametrize("case", ["unchanged-short", "edited-short", "unchanged-long"])
def test_S2_an_archive_from_before_body_hashes_still_loads_and_compares_its_text(make, case: str):
    """S2: a state file written before bodySha256 existed loads; sync then compares the archived
    text, cut the same way when the archive was cut.

    Bug: an archive entry without a hash failed to load, or every unchanged long body looked
    edited.
    """
    h = make(COVERED)
    long_id = h.add_comment("x" * 8999 + "y")
    h.ok("sync")
    h.noise()
    old = h.state()
    for item in old["items"].values():
        del item["bodySha256"]
    h.state_file.write_text(json.dumps(old))
    h.advance(60)
    if case == "edited-short":
        h.edit_comment(THREAD_ID, EDITED)

    after = h.ok("sync", "--ceiling", "60")

    expected = [f"thread:{THREAD_ID}@2"] if case == "edited-short" else []
    assert [item["key"] for item in after["newItems"]] == expected, after
    assert h.state()["items"][f"issue:{long_id}"]["truncated"] is True


@pytest.mark.parametrize(
    ("entries", "error"),
    [
        (
            [DESIGN],
            "entry 0: disposition Needs decision needs a proposal element in the batch: "
            '{"proposal": {"summary": "<text>", "options": ["<one line>", "<one line>"], '
            '"recommended": "A"}}',
        ),
        ([FIX, PROPOSAL], "entry 1: a proposal needs a Needs decision claim entry in the batch"),
        (
            [DESIGN, PROPOSAL, PROPOSAL],
            "entry 2: the batch already has a proposal in triage entry 1",
        ),
        *[
            (
                [DESIGN, {"proposal": {**PROPOSAL["proposal"], "options": options}}],
                "entry 1: proposal.options must be a list of 2 to 4 options, "
                "each a non-empty single line of at most 200 characters",
            )
            for options in (
                OPTIONS[:1],
                [*OPTIONS, "c", "d", "e"],
                [OPTIONS[0], "one\ntwo"],
                [OPTIONS[0], "x" * 201],
            )
        ],
        (
            [DESIGN, {"proposal": {**PROPOSAL["proposal"], "recommended": "C"}}],
            "entry 1: proposal.recommended must be the letter of an option: one of A, B",
        ),
        (
            [DESIGN, {"proposal": {**PROPOSAL["proposal"], "summary": "x" * 2001}}],
            "entry 1: proposal.summary must be non-empty text of at most 2000 characters",
        ),
        (
            [DESIGN, {"proposal": {**PROPOSAL["proposal"], "chosen": "B"}}],
            "entry 1: proposal has unknown keys ['chosen']; allowed: summary, options, recommended",
        ),
        (
            [DESIGN, {**PROPOSAL, "sources": ["issue:6095816171"]}],
            "entry 1: unknown fields ['sources']; a proposal element has only the field proposal",
        ),
    ],
    ids=[
        "needs-decision-without-a-proposal",
        "a-proposal-without-needs-decision",
        "two-proposals",
        "one-option",
        "five-options",
        "a-two-line-option",
        "a-long-option",
        "recommended-outside-the-options",
        "a-long-summary",
        "an-unknown-proposal-key",
        "a-proposal-element-with-sources",
    ],
)
def test_S2_triage_refuses_an_invalid_proposal(make, entries: list, error: str):
    """S2 (Q3): a batch with a Needs decision entry holds exactly one proposal element, and only
    such a batch: 2 to 4 one-line options and a recommended letter. Each refusal records nothing.

    Bug: a decision stop was opened with no record of what the agent proposed, so the owner's answer
    could not be paired with it; or a proposal element cited a source and covered an inbox key.
    """
    h = synced(make, COVERED)
    before = h.state_file.read_bytes()

    h.refused(
        f"triage {error}; nothing was recorded",
        "triage",
        "--file",
        h.write("t.json", json.dumps(entries)),
    )

    assert h.state_file.read_bytes() == before, (
        "the ledger, items, proposals, and inbox are unchanged"
    )


def test_S2_a_proposal_is_paired_with_the_answer_to_its_decision_stop(make):
    """S2/S5 (Q3): triage stores the batch's proposal once as P<n>, and each Needs decision entry of
    the batch names it. The decision stop lists those claims, resume stores the owner's words
    verbatim, and each reopened claim carries the proposal, so the agent can apply "B".

    Bug: the proposal lived only in the round summary and the chat, so no record joined the options
    the agent offered to the answer the owner gave.
    """
    h = synced(make, COVERED)
    other = {**DESIGN, "claim": "Split the cursor", "sources": ["issue:6095880989"]}
    settled = {
        "claim": "the notice is stale",
        "class": None,
        "disposition": "No change",
        "sources": [],
    }

    h.triage([DESIGN, other, settled, PROPOSAL])

    options = dict(zip("AB", OPTIONS, strict=True))
    proposal = {"round": 1, "summary": PROPOSAL["proposal"]["summary"], "options": options}
    proposal.update(recommended="A", at=h.now)
    shown = h.ok("show")
    assert shown.get("proposals") == {"P1": proposal}
    assert [(e["id"], e["proposal"]) for e in shown["ledger"][5:]] == [
        ("L6", "P1"),
        ("L7", "P1"),
        ("L8", None),
    ]
    owed = h.ok("sync")
    assert (owed["next"], owed["resumeCondition"]) == ("needs_input", "decision owed on L6")
    assert h.state()["stops"]["stop-1"]["claims"] == ["issue:6095816171", "issue:6095880989"]
    h.ok("resume", "--decision", "B, keep the flag")
    assert h.state()["stops"]["stop-1"]["answer"]["text"] == "B, keep the flag", (
        "the answer is stored verbatim"
    )
    reopened = h.ok("sync")["newItems"]
    assert [(item["key"], item["proposal"]) for item in reopened] == [
        ("ledger:L6", proposal),
        ("ledger:L7", proposal),
    ]


# --- S3: a head's time is written once -----------------------------------------------------


def test_S3_a_pushed_head_keeps_its_push_record_through_later_syncs(make):
    """S3: push records {firstSeen: push time, source: pushed}, and no later sync overwrites it.

    Bug caught (Codex P1 4267611111 on #152): the push time was reset by the next round, after the
    reviewer's +1 that came 16 seconds after the push, so the covered head looked unreviewed.
    """
    h = make(COVERED)
    work = h.synced_branch()
    new_head = h.commit(work, "fix: guard the cursor")
    h.advance(60)
    pushed_at = h.now

    assert h.ok("push", cwd=work) == {"head": new_head, "pushedAt": pushed_at}

    assert h.origin_head() == new_head
    record = {"firstSeen": pushed_at, "source": "pushed"}
    h.set_head(new_head)
    h.advance(16)
    h.react("THUMBS_UP")
    h.advance(14)
    first = h.ok("sync")
    assert (first["reviewer"]["state"], first["next"]) == ("covered", "done"), first
    h.advance(60)
    h.ok("sync")
    assert h.state()["heads"][new_head] == record, "two later syncs keep the push record"


def test_S3_a_force_pushed_head_gets_its_own_first_seen_time(make):
    """S3 (#152): a new head is timed from when sync first saw it, not from an older head or commit.

    Bug: the new head reuses an old time, so a re-trigger posted for the old head counts and the
    reviewer times out at the first poll (done with a gap) instead of waiting 600 s for the new
    head.
    """
    h = synced(make, WAITING)
    old_head = h.state()["head"]
    old_record = h.state()["heads"][old_head]
    assert h.post("--issue", body="@codex review\n").returncode == 0, (
        "a re-trigger for the old head"
    )
    h.advance(3600)
    new_head = NEW_HEAD
    h.set_head(new_head)
    seen_at = h.now

    result = h.ok("sync", "--ceiling", "900")

    assert h.state()["heads"] == {
        old_head: old_record,
        new_head: {"firstSeen": seen_at, "source": "seen"},
    }
    assert result["next"] == "retrigger", (
        f"the new head should wait for its own re-trigger\n{result}"
    )
    assert result["reviewer"]["timedOut"] is False
    assert epoch(h.now) - epoch(seen_at) >= 600, "the re-trigger is due 600 s after the new head"


# --- S4: a pending post is reconciled first ------------------------------------------------


@pytest.mark.parametrize("command", ["sync", "triage", "post", "push", "pause"])
def test_S4_a_pending_post_stops_every_writer(make, command: str):
    """S4: while a post's result is unknown, each writer (not resume) stops and sets needs_input.

    Bug: a command acts on top of an unknown post (a process died after writing pending), so a
    reply can be posted twice or a push can follow a reply that may not exist.
    """
    h = synced(make, WAITING)
    state = h.state()
    state["pending"] = {"kind": "thread", "target": 4237011925, "at": h.now, "bodySha256": "0" * 64}
    h.state_file.write_text(json.dumps(state))
    condition = PENDING_TEXT.format(4237011925)
    options = {
        "triage": ["--file", h.write("t.json", json.dumps([FIX]))],
        "post": ["--issue", "--body-file", h.write("b.md", "Summary")],
        "pause": ["--reason", "x"],
    }.get(command, [])

    if command == "sync":
        assert h.quiet_sync() == {"next": "needs_input", "resumeCondition": condition}
    else:
        h.refused(f"status is needs_input: {condition}", command, *options, cwd=h.checkout())

    assert (h.state()["status"], h.resume_condition()) == ("needs_input", condition)
    assert h.ok("show")["pending"]["target"] == 4237011925, "show still reads"


@pytest.mark.parametrize("mode", ["fail", "noid"])
def test_T3_an_unknown_post_result_needs_input(make, mode: str):
    """T3 (#150/#154): a failed or id-less post keeps pending and stops the run with a
    resumeCondition.

    Bug: the poster's failure is hidden (as `post-reply | jq` did), so the run goes on and can post
    the reply twice.
    """
    h = synced(make, WAITING)
    result = h.post("--thread", "4237011925", body="Fixed in 64a1289.", FAKE_POST=mode)

    assert (result.returncode, result.stdout) == (1, "")
    assert "ERROR: the post result is unknown; check the PR, then resume" in result.stderr
    pending = h.state()["pending"]
    assert {key: pending[key] for key in ("kind", "target", "at")} == {
        "kind": "thread",
        "target": 4237011925,
        "at": h.now,
    }
    assert h.quiet_sync() == {
        "next": "needs_input",
        "resumeCondition": PENDING_TEXT.format(4237011925),
    }
    assert len(h.calls()[1]) == 1, "only the first post reached GitHub"


# --- S5: a decision before leaving needs_input ---------------------------------------------


def test_S5_only_resume_with_a_decision_leaves_needs_input(make):
    """S5 (#154/#155): the decision and the status change land together, and state survives
    processes.

    Bug: state in shell variables or agent context is lost by a new process; sync quietly resumes a
    paused run; or the status leaves needs_input without a recorded decision.
    """
    h = synced(make, COVERED)
    h.triage([FIX])
    h.advance(60)
    h.ok("pause", "--reason", "ask the owner whether the cursor may skip")
    paused = h.state_file.read_bytes()

    assert h.quiet_sync() == {
        "next": "needs_input",
        "resumeCondition": "ask the owner whether the cursor may skip",
    }
    no_decision = h.run("resume")
    assert no_decision.returncode == 2 and "--decision" in no_decision.stderr
    assert h.state_file.read_bytes() == paused, "neither sync nor a bare resume changes the state"

    resumed = h.ok("resume", "--decision", "keep the cursor; add a test")

    state = h.ok("show")
    assert (resumed["status"], resumed["resumeCondition"], state["status"]) == (
        "running",
        None,
        "running",
    )
    answer = {"text": "keep the cursor; add a test", "by": "owner", "at": h.now}
    detail = "ask the owner whether the cursor may skip"
    assert state["stops"] == {
        "stop-1": {"condition": "pause", "detail": detail, "claims": [], "answer": answer}
    }
    claims = [
        (entry["id"], entry["claim"])
        for entry in state["ledger"]
        if entry["disposition"] != "Noise"
    ]
    assert claims == [("L6", FIX["claim"])]
    assert {"issue:6095746489", "issue:6095816171", "issue:6095880989"} <= set(state["handled"])


# --- T1: done only when every condition holds ----------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("all-hold", "done"),
        ("head-moved-during-ci", "wait_reviewer"),
        ("ci-failing", "triage"),
        ("ci-pending", "wait_ci"),
        ("reviewer-silent", "wait_reviewer"),
        ("unpushed-fix", "triage"),
        ("decision-owed", "needs_input"),
        ("blocking-review", "needs_input"),
        ("unhandled-item", "triage"),
    ],
)
def test_T1_done_only_when_every_condition_holds(make, case: str, expected: str):
    """T1: with everything else holding, one failed condition keeps next from done.

    Bugs: done on a head that moved during the CI read; on failing or pending CI; on a head the
    reviewer never covered; with an unpushed fix or an owed decision; or (#152) on a stale snapshot,
    so feedback that arrived after the last sync is missed.
    """
    h = synced(make, COVERED)
    h.advance(60)
    late, moved = None, "a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0"
    if case == "head-moved-during-ci":
        (h.ctl / "move_head_on_checks").write_text(moved)
    elif case in ("ci-failing", "ci-pending"):
        h.set_checks(
            [{"name": "checks", "bucket": case.removeprefix("ci-").replace("failing", "fail")}]
        )
    elif case == "reviewer-silent":
        with h.pr_data() as pr:
            pr["reactions"]["nodes"] = []
    elif case == "unpushed-fix":
        h.triage([FIX])
    elif case == "decision-owed":
        h.triage([DESIGN, PROPOSAL])
    elif case == "blocking-review":
        with h.pr_data() as pr:
            pr["reviewDecision"] = "CHANGES_REQUESTED"
    elif case == "unhandled-item":
        late = h.add_comment("P1: the cursor skips a comment.")

    result = h.ok("sync", "--ceiling", "60")

    assert result["next"] == expected, result
    done = (h.state()["status"], h.state()["target"]) == ("succeeded", "ready_for_human_merge")
    assert done is (case == "all-hold")
    if case == "head-moved-during-ci":
        assert (result["headChanged"], result["head"], h.state()["heads"][moved]["source"]) == (
            True,
            moved,
            "seen",
        )
    if case == "ci-failing":
        assert (result["ci"], result["round"]) == ({"state": "failing", "failing": ["checks"]}, 2)
        assert [item["key"] for item in result["newItems"]] == [f"check:{result['head']}:checks"]
    if case in ("decision-owed", "blocking-review"):
        condition = "decision owed on L6" if case == "decision-owed" else "a blocking review stands"
        assert (result["resumeCondition"], h.state()["status"]) == (condition, "needs_input")
    if late is not None:
        assert [item["id"] for item in result["newItems"]] == [late]


@pytest.mark.parametrize(
    ("every", "expected", "ci"),
    [
        (
            [{"name": "checks", "bucket": "fail"}],
            "triage",
            {"state": "failing", "failing": ["checks"]},
        ),
        ([{"name": "checks", "bucket": "pending"}], "wait_ci", {"state": "pending", "failing": []}),
    ],
    ids=["failing", "pending"],
)
def test_T1_without_required_checks_every_check_on_the_head_counts(
    make, every, expected: str, ci: dict
):
    """T1: with no required checks, CI is judged on every check on the head.

    Bug: CI counted green while the checks workflow failed (the source repository has no required
    checks).
    """
    h = synced(make, COVERED)
    h.set_checks(None, every)

    result = h.ok("sync")

    assert (result["next"], result["ci"], result["gaps"]) == (expected, ci, [])


# Check-run conclusions that fail; only success, neutral, and skipped pass a completed run.
FAILING_CONCLUSIONS = [
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
]


@pytest.mark.parametrize("conclusion", FAILING_CONCLUSIONS)
def test_T1_an_unlisted_failing_check_run_is_never_green_without_required_checks(
    make, conclusion: str
):
    """T1: with no required checks, a completed check run on the head that gh pr checks does not
    list still counts. A conclusion other than success, neutral, or skipped makes CI failing and
    names the run, so sync never reaches done.

    Bug (found in review, 2026-10-23): with no required checks, CI was every check that gh pr
    checks listed, and gh omits some runs on the head, such as a workflow_dispatch run. A
    completed, failing, unlisted run read as green, so sync returned done with
    ready_for_human_merge, and reviewer mode allowed APPROVE.
    """
    h = synced(make, COVERED)
    h.set_checks(None, PASS)
    failed = {**check_run("e2e", "completed"), "conclusion": conclusion}
    h.set_rest_checks([check_run("checks", "completed"), failed], 0)

    result = h.ok("sync")

    head = result["head"]
    assert (result["next"], result["ci"]) == (
        "triage",
        {"state": "failing", "failing": ["e2e"]},
    ), result
    assert [item["key"] for item in result["newItems"]] == [f"check:{head}:e2e"]


def test_T1_an_unlisted_running_check_run_keeps_ci_pending_without_required_checks(make):
    """T1: with no required checks, an unlisted check run on the head that has not completed keeps
    CI pending.

    Bug (found in review, 2026-10-23): CI was green from the listed checks alone, so done came
    while an unlisted run on the head was still running.
    """
    h = synced(make, COVERED)
    h.set_checks(None, PASS)
    h.set_rest_checks([check_run("checks", "completed"), check_run("e2e", "in_progress")], 0)

    result = h.ok("sync")

    reason = "check runs still running: e2e"
    assert (result["next"], result["ci"]) == (
        "wait_ci",
        {"state": "pending", "failing": [], "reason": reason},
    ), result


@pytest.mark.parametrize(
    "run",
    [
        {**check_run("e2e", "completed"), "conclusion": "neutral"},
        {**check_run("e2e", "completed"), "conclusion": "skipped"},
        check_run("e2e", "completed"),
        {**check_run(COPILOT, "completed"), "conclusion": "failure"},
    ],
    ids=["neutral", "skipped", "success", "bot-review-failure"],
)
def test_T1_unlisted_runs_that_pass_or_belong_to_a_bot_review_leave_ci_green(make, run: dict):
    """T1: with no required checks, an unlisted run that completed with success, neutral, or skipped
    does not hold CI, and a bot review's run (BOT_REVIEW_CHECKS) is not CI.

    Bug: the check-run read made every unlisted run block done, so a head whose runs all passed, or
    whose only red run was Copilot's review, never became green.
    """
    h = synced(make, COVERED)
    h.set_checks(None, PASS)
    h.set_rest_checks([check_run("checks", "completed"), run], 0)

    result = h.ok("sync")

    assert (result["next"], result["ci"]) == ("done", {"state": "green", "failing": []}), result


@pytest.mark.parametrize(
    "runs",
    [
        {"fail": True},
        {"raw": "{not json"},
        {"body": {"check_runs": [check_run("checks", "completed")]}},
        {"body": {"total_count": 1, "check_runs": {"name": "checks"}}},
        {"body": {"total_count": 101, "check_runs": [check_run("checks", "completed")]}},
        {"body": {"total_count": 1, "check_runs": [{"status": "completed"}]}},
    ],
    ids=[
        "call-fails",
        "bad-json",
        "no-total-count",
        "runs-not-a-list",
        "more-runs-than-listed",
        "run-without-a-name",
    ],
)
def test_T1_unreadable_check_runs_are_never_green_without_required_checks(make, runs: dict):
    """T1: with no required checks, when the head's check runs cannot be read in full, CI is
    pending, never green, and T4 bounds the wait.

    Bug (found in review, 2026-10-23): a failed or partial check-run read would leave the listed
    checks as the only evidence, so done could pass a failing run that gh pr checks did not list.
    """
    h = synced(make, COVERED)
    h.set_checks(None, PASS)
    if "raw" in runs:
        (h.ctl / "rest_check_runs.json").write_text(runs["raw"])
    else:
        h.set_rest_checks(None if runs.get("fail") else runs["body"], 0)

    first = h.ok("sync")
    h.advance(1800)
    last = h.ok("sync")

    head7 = first["head"][:7]
    reason = f"the check runs of {head7} could not be read"
    assert (first["next"], first["ci"]) == (
        "wait_ci",
        {"state": "pending", "failing": [], "reason": reason},
    ), first
    condition = f"CI still pending on {head7} after 30 minutes"
    assert (last["next"], last["resumeCondition"], h.state()["status"]) == (
        "needs_input",
        condition,
        "needs_input",
    )


@pytest.mark.parametrize(
    "earlier_checks", [False, True], ids=["no-head-had-checks", "an-earlier-head-had-checks"]
)
def test_T2_no_checks_just_after_a_push_is_not_green(make, earlier_checks: bool):
    """T2: a head with no checks counts as green only 120 s after it was first seen, and only if no
    earlier head of the PR had a check; until then CI is pending with "no checks yet".

    Bug: just after a push GitHub has not registered a check suite yet, so "no checks" counted as
    green and the run finished before CI ran on the new head.
    """
    h = make(COVERED)
    h.reviewers.write_text("{}")
    h.set_checks(None, PASS if earlier_checks else None)
    work = h.synced_branch()
    h.commit(work, "fix: guard the cursor")
    h.advance(300)
    new_head = h.ok("push", cwd=work)["head"]
    h.set_head(new_head)
    h.set_checks(None, None)

    fresh = h.ok("sync", "--ceiling", "0")
    h.advance(120)
    later = h.ok("sync", "--ceiling", "0")

    assert (fresh["next"], fresh["ci"]) == (
        "wait_ci",
        {"state": "pending", "failing": [], "reason": "no checks yet"},
    )
    if earlier_checks:
        assert later["next"] == "wait_ci", (
            "a head that lost the checks an earlier head had is not green"
        )
    else:
        assert (later["next"], later["ci"]["state"]) == ("done", "none")
        assert later["gaps"] == [
            "no reviewer configured for octo-org/hello-world",
            f"no CI checks on {new_head[:7]}",
        ]


def test_T2_a_bot_review_check_on_an_earlier_head_does_not_hold_a_head_without_checks(make):
    """T2: a check run that a bot review adds to one head is not CI, so it does not keep a later
    head without checks pending after its 120 s.

    Bug (a private repository with no CI, 2026-10-23): the repository has no CI. Copilot's automatic
    review added a copilot-pull-request-reviewer check run to the first head only. Every fix head
    then waited with "no checks yet" until T4 stopped the run with needs_input after 30 minutes.
    GitHub leaves that run out of the status rollup, so gh pr checks listed nothing and only REST
    saw it. The first sync of that head (08:45:06Z) came before the run completed (08:48:48Z), so
    the running bot run set ciSeen. The fix head had only the queued suites of idle apps, with no
    check runs.
    """
    h = make(COVERED)
    h.reviewers.write_text("{}")
    h.set_checks(None, None)
    h.set_rest_checks([check_run(COPILOT, "in_progress")], 0)
    work = h.synced_branch()
    h.set_rest_checks([check_run(COPILOT, "completed")], 0)
    h.commit(work, "fix: guard the cursor")
    h.advance(300)
    new_head = h.ok("push", cwd=work)["head"]
    h.set_head(new_head)
    h.set_rest_checks(check_runs=0, statuses=0)
    idle = ("dbt-cloud", "cloudflare-workers-and-pages", "cursor", "claude")
    h.set_suites([suite(app, "queued", 0) for app in idle])

    fresh = h.ok("sync", "--ceiling", "0")
    h.advance(120)
    later = h.ok("sync", "--ceiling", "0")

    assert (fresh["next"], fresh["ci"]) == (
        "wait_ci",
        {"state": "pending", "failing": [], "reason": "no checks yet"},
    )
    assert (later["next"], later["ci"]) == ("done", {"state": "none", "failing": []}), later
    assert later["gaps"] == [
        "no reviewer configured for octo-org/hello-world",
        f"no CI checks on {new_head[:7]}",
    ]
    assert (later.get("target"), h.state()["target"]) == ("ready_with_gaps", "ready_with_gaps")


def test_T2_a_bot_review_check_holds_only_its_own_head_while_it_runs(make):
    """T2: a check run that a bot review adds keeps its head pending while it runs. After it
    completes, the head counts as a head with no checks: done with the gap, 120 s after its head
    time.

    Bug (a private repository with no CI, 2026-10-23): REST counted the
    copilot-pull-request-reviewer run as a check that gh pr checks did not list, so the first head
    stayed pending with "checks exist but were not listed" after the review ended, and could never
    reach done.
    """
    h = make(COVERED)
    h.reviewers.write_text("{}")
    h.set_checks(None, None)
    h.set_rest_checks([check_run(COPILOT, "in_progress")], 0)
    h.ok("sync")
    h.noise()

    running = h.ok("sync", "--ceiling", "0")
    h.set_rest_checks([check_run(COPILOT, "completed")], 0)
    h.advance(120)
    done = h.ok("sync", "--ceiling", "0")

    reason = f"bot review still running: {COPILOT}"
    assert (running["next"], running["ci"]) == (
        "wait_ci",
        {"state": "pending", "failing": [], "reason": reason},
    )
    assert (done["next"], done["ci"]) == ("done", {"state": "none", "failing": []}), done
    assert done["gaps"] == [
        "no reviewer configured for octo-org/hello-world",
        f"no CI checks on {done['head'][:7]}",
    ]
    assert (done.get("target"), h.state()["target"]) == ("ready_with_gaps", "ready_with_gaps")


def test_T1_an_unconfigured_reviewer_allows_done_with_a_gap(make):
    """T1 (approved interpretation): with no reviewer configured, CI decides, and the gap is
    reported. D1: that done is ready_with_gaps, never ready_for_human_merge.

    Bug: an unconfigured repo waits for a reviewer forever, or finishes without saying no one
    reviewed; or (D1) the controller merged a PR that no reviewer ever looked at.
    """
    h = synced(make, COVERED)
    h.reviewers.write_text("{}")
    h.set_checks([{"name": "checks", "bucket": "pending"}])

    pending = h.ok("sync", "--ceiling", "60")
    h.set_checks(PASS)
    done = h.ok("sync", "--ceiling", "60")

    assert (pending["next"], pending["gaps"]) == (
        "wait_ci",
        ["no reviewer configured for octo-org/hello-world"],
    )
    assert (done["next"], done["gaps"]) == (
        "done",
        ["no reviewer configured for octo-org/hello-world"],
    )
    shown = h.ok("show")
    assert (done.get("target"), shown["target"]) == ("ready_with_gaps", "ready_with_gaps")
    assert shown.get("gaps") == ["no reviewer configured for octo-org/hello-world"]


def test_T1_a_covered_head_with_green_ci_is_ready_for_human_merge(make):
    """T1 (D1): a done with no gap sets target ready_for_human_merge, in the sync output and the
    state, and stores no gaps.

    Bug: the done output does not say which target it set, so the controller has to guess; or a
    clean done stores a gap list that shows gaps it does not have.
    """
    h = synced(make, COVERED)

    done = h.ok("sync")

    shown = h.ok("show")
    assert (done["next"], done["gaps"], done.get("target")) == ("done", [], "ready_for_human_merge")
    assert (shown["status"], shown["target"], shown.get("gaps")) == (
        "succeeded",
        "ready_for_human_merge",
        [],
    )


def codex_receipt(sha: str) -> str:
    """The receipt issue comment Codex posted on #183 (2026-10-23).

    Codex posted it instead of a 👍 or a review.
    """
    return (
        "<!-- codex-review-receipt:v1 -->\n"
        "Codex Review: Didn't find any major issues. Breezy!\n\n"
        f"**Reviewed commit:** `{sha[:10]}`"
    )


@pytest.mark.parametrize(
    ("author", "reviewed", "state"),
    [
        ("chatgpt-codex-connector", "head", "covered"),
        ("octocat", "head", "waiting"),
        ("chatgpt-codex-connector", "old-head", "waiting"),
    ],
    ids=["codex-on-the-head", "another-login", "an-older-head"],
)
def test_T1_a_codex_review_receipt_covers_only_the_head_it_names(
    make, author: str, reviewed: str, state: str
):
    """T1 (D1): a review receipt comment from the configured reviewer that names the head covers it.

    Bug caught (#183, 2026-10-23): Codex posted a receipt comment for f011183d5c and no 👍 or
    review, so the run waited 600 s and ended ready_with_gaps with "did not review f011183". A
    receipt from another login, or one for an older head, must not cover the head.
    """
    h = synced(make, WAITING)
    head = h.state()["head"]
    h.advance(60)
    h.add_comment(codex_receipt(head if reviewed == "head" else NEW_HEAD), author=author)

    first = h.ok("sync", "--ceiling", "60")
    h.noise()
    result = h.ok("sync", "--ceiling", "60")

    assert first["next"] == "triage", "the receipt is still an issue comment for triage"
    assert result["reviewer"]["state"] == state, result
    if state == "covered":
        assert result["reviewer"]["evidence"].endswith(f"on {head[:7]}"), result["reviewer"]
        assert (result["next"], result["gaps"], result.get("target")) == (
            "done",
            [],
            "ready_for_human_merge",
        )
    else:
        assert (result["next"], result["reviewer"]["evidence"]) == ("wait_reviewer", None), result
        assert h.state()["status"] == "running", "a spoofed or stale receipt must not end the run"


def test_T1_a_new_run_clears_the_gaps_and_the_target(make):
    """T1 (D1): the gaps and the target belong to one done; a new run clears both.

    Bug: a new run kept the old run's ready_with_gaps target or gaps, so show reported a target
    for a run that had not finished.
    """
    h = synced(make, COVERED)
    h.reviewers.write_text("{}")
    assert h.ok("sync")["next"] == "done"
    assert h.ok("show").get("gaps") == ["no reviewer configured for octo-org/hello-world"]
    h.add_comment("P2: one more case.")

    again = h.ok("sync")

    shown = h.ok("show")
    assert again["next"] == "triage"
    assert (shown["status"], shown["target"], shown.get("gaps", "absent")) == (
        "running",
        None,
        None,
    )


# --- T2: never done on silence ------------------------------------------------------------


def test_T2_silence_waits_then_retriggers_then_times_out_with_a_gap(make):
    """T2: a quiet PR is never done until the reviewer covers it or times out after a re-trigger.
    D1: a timeout's done is ready_with_gaps, never ready_for_human_merge.

    Bug: the ceiling with no feedback and no reviewer signal is read as "nothing left", so done
    comes before the reviewer looked; or a timeout finishes without reporting the missing review;
    or (D1, #161) "Review complete" came a minute before Codex reviewed the head.
    """
    h = synced(make, WAITING)
    head = h.state()["head"]

    quiet = h.ok("sync", "--ceiling", "120")
    h.advance(600)
    due = h.ok("sync", "--ceiling", "120")
    assert h.post("--issue", body="@codex review").returncode == 0
    h.advance(600)
    timed_out = h.ok("sync", "--ceiling", "120")

    assert (quiet["next"], quiet["newItems"]) == ("wait_reviewer", [])
    assert due["next"] == "retrigger"
    assert (timed_out["next"], timed_out["gaps"]) == (
        "done",
        [f"{CODEX} did not review {head[:7]}"],
    )
    shown = h.ok("show")
    assert (timed_out.get("target"), shown["target"]) == ("ready_with_gaps", "ready_with_gaps")
    assert shown.get("gaps") == [f"{CODEX} did not review {head[:7]}"]


# --- T3: needs_input and blocked carry a resumeCondition ---------------------------------------


def test_T3_a_design_verdict_lets_the_round_post_then_needs_input(make):
    """T3: triage of a design batch does not stop the run; the next sync asks for the decision.

    Bug: triage stops at once, so the design round cannot post its replies and summary; or sync
    goes on without the decision.
    """
    h = synced(make, COVERED)
    other = {**DESIGN, "claim": "Split the cursor", "sources": ["issue:6095880989"]}

    triaged = h.triage([DESIGN, other, PROPOSAL])
    posted = h.post("--issue", body="Review round 1: design")
    result = h.ok("sync")

    assert (triaged["verdict"], triaged["status"]) == ("design", "running")
    assert posted.returncode == 0, posted.stderr
    assert (result["next"], result["resumeCondition"]) == ("needs_input", "decision owed on L6")


def test_T3_max_rounds_blocks_until_resume_raises_the_cap(make):
    """T3: the round cap stops the loop with a resumeCondition until a human raises it.

    Bug: sync keeps returning work past maxRounds, or the work found at the cap is lost.
    """
    h = synced(make, COVERED, "--max-rounds", "1")
    late = h.add_comment("P2: one more thing.")

    blocked = h.ok("sync")

    condition = "max rounds reached; raise the cap or stop"
    assert (blocked["next"], blocked["resumeCondition"], blocked["round"]) == (
        "blocked",
        condition,
        1,
    )
    assert h.quiet_sync() == {"next": "blocked", "resumeCondition": condition}
    h.refused(
        "--max-rounds must be greater than the current round (1)",
        "resume",
        "--decision",
        "go",
        "--max-rounds",
        "1",
    )
    assert (
        h.ok("resume", "--decision", "one more round", "--max-rounds", "2")["status"] == "running"
    )
    resumed = h.ok("sync")
    assert ([item["id"] for item in resumed["newItems"]], resumed["round"]) == ([late], 2)


# --- T4: a repeated next without new input is bounded -------------------------------------


def give_new_input(h: Round, new_input: str | None) -> None:
    """New input resets T4's repeat tracking: a new inbox item, or a new head."""
    if new_input == "new-item":
        h.add_comment("P2: one more case.")
    elif new_input == "new-head":
        h.set_head(NEW_HEAD)


NEW_INPUT = pytest.mark.parametrize("new_input", [None, "new-item", "new-head"])


@NEW_INPUT
def test_T4_a_fix_that_is_never_pushed_stops_the_run(make, new_input: str | None):
    """T4: the same unpushed fix on the same head, with no new input, asks for a decision.

    Bug: an endless loop; every sync returns triage for a fix that was triaged but never pushed.
    A new item or a new head is new input, so the run goes on.
    """
    h = synced(make, COVERED)
    h.triage([FIX])
    h.advance(60)
    first = h.ok("sync")
    h.advance(60)
    give_new_input(h, new_input)

    second = h.ok("sync", "--ceiling", "60")

    assert (first["next"], first["reason"]) == ("triage", "unpushed fix L6")
    if new_input is None:
        condition = (
            f"fix L6 on {first['head'][:7]} was triaged but not pushed; "
            "push it or change the disposition"
        )
        assert (second["next"], second["resumeCondition"], h.state()["status"]) == (
            "needs_input",
            condition,
            "needs_input",
        )
    else:
        assert (second["next"], h.state()["status"]) != ("needs_input", "needs_input"), second
        assert h.state()["status"] == "running"


def test_T1_a_fix_is_not_settled_when_another_actor_moves_the_head(make):
    """T1 (D3): a Fix settles only on a siula round push receipt, not on any head move.

    Bug (the engine before D3): a Fix triaged on one head settled as soon as someone else
    pushed another head, so done came with the fix never pushed.
    """
    h = synced(make, COVERED)
    fix_head = h.state()["head"]
    h.triage([FIX])
    h.set_head(NEW_HEAD)  # another actor pushed; siula round push never ran
    h.react()  # the reviewer covers the new head, and CI is green

    moved = h.ok("sync")
    h.advance(60)
    again = h.ok("sync")

    assert (moved["head"], moved["next"], moved["reason"]) == (
        NEW_HEAD,
        "triage",
        "unpushed fix L6",
    )
    condition = (
        f"fix L6 on {fix_head[:7]} was triaged but not pushed; push it or change the disposition"
    )
    assert (again["next"], again["resumeCondition"]) == ("needs_input", condition), (
        "T4 bounds it; never done"
    )
    state = h.state()
    fix = state["ledger"][-1]
    assert (state["target"], fix["id"], fix["disposition"], fix.get("pushed", "absent")) == (
        None,
        "L6",
        "Fix",
        None,
    )


def pushed_thread_fix(h: Round) -> tuple:
    """A Fix that cites thread 4237011925, a reply posted before its push, then siula round push.
    Returns (new head, the early reply's id)."""
    work = h.synced_branch()
    h.triage([{**FIX, "sources": ["thread:4237011925"]}])
    early = h.post("--thread", "4237011925", body="Fixing the ceiling query now.")
    assert early.returncode == 0, early.stderr
    entry = h.state()["ledger"][-1]
    assert (entry["id"], entry.get("replies")) == ("L6", None), (
        "a reply before the push does not count"
    )
    new_head = h.commit(work, "fix: query once more at the ceiling")
    h.ok("push", cwd=work)
    h.set_head(new_head)
    h.react()  # the reviewer covers the pushed head, and CI is green
    return new_head, json.loads(early.stdout)["commentId"]


def test_T1_a_pushed_fix_waits_for_a_reply_to_each_thread_it_cites(make):
    """T1 (D3): a pushed Fix still asks for a reply receipt on each thread it cites; a reply
    posted before the push does not count.

    Bug: done came after the push while the reviewer's thread had no Fixed reply; or a reply
    posted before the fix was pushed counted as its answer.
    """
    h = make(COVERED)
    new_head, _ = pushed_thread_fix(h)

    unreplied = h.ok("sync")
    reply = h.post(
        "--thread", "4237011925", body=f"Fixed in {new_head[:7]}: the ceiling queries once more."
    )
    done = h.ok("sync")

    assert (unreplied["head"], unreplied["next"], unreplied["reason"]) == (
        new_head,
        "triage",
        "unreplied fix L6",
    )
    assert reply.returncode == 0, reply.stderr
    entry = h.state()["ledger"][-1]
    assert (entry["id"], entry.get("pushed")) == ("L6", new_head), (
        "push records its receipt on the Fix"
    )
    assert entry.get("replies") == {"thread:4237011925": json.loads(reply.stdout)["commentId"]}
    assert done["next"] == "done"


def test_T4_a_pushed_fix_that_is_never_replied_to_stops_the_run(make):
    """T4 (D3): the same unreplied Fix on the same head, with no new input, asks for input.

    Bug: an endless loop; every sync returns triage for a pushed fix whose reply was never posted.
    """
    h = make(COVERED)
    pushed_thread_fix(h)

    first = h.ok("sync")
    h.advance(60)
    second = h.ok("sync")

    assert (first["next"], first["reason"]) == ("triage", "unreplied fix L6")
    condition = (
        "fix L6 was pushed, but thread:4237011925 has no reply; post the reply with "
        "siula round post --thread 4237011925, or triage it again with sameAs L6"
    )
    assert (second["next"], second["resumeCondition"], h.state()["status"]) == (
        "needs_input",
        condition,
        "needs_input",
    )


RED = [{"name": "checks", "bucket": "fail"}]


def red_check(h: Round) -> str:
    """Fail the required check on the current head, sync it into the inbox, and return its key."""
    h.set_checks(RED)
    first = h.ok("sync")
    key = f"check:{first['head']}:checks"
    assert ([item["key"] for item in first["newItems"]], first["next"]) == ([key], "triage"), first
    return key


def test_T1_a_red_check_waiting_on_a_decision_can_be_decided(make):
    """T1/S5: a failing check is an item; triaged Needs decision, it reaches decision_owed like a
    comment.

    Bug: a CI claim waiting on a decision could never be decided. Red CI always returned triage, so
    T4 opened a repeat stop, and an answer to a repeat stop decides no claim.
    """
    h = synced(make, COVERED)
    key = red_check(h)
    h.triage(
        [
            {
                "claim": "checks fail on a flaky runner",
                "class": "design",
                "disposition": "Needs decision",
                "sources": [key],
            },
            PROPOSAL,
        ]
    )

    owed = h.ok("sync")
    h.ok("resume", "--decision", "retry the runner later; not caused by this PR")
    reopened = h.ok("sync")
    applied = {
        "claim": "checks fail on a flaky runner",
        "class": None,
        "disposition": "No change",
        "sameAs": "L6",
    }
    h.triage([{**applied, "sources": [key]}])
    after = h.ok("sync")

    assert (owed["next"], owed["resumeCondition"]) == ("needs_input", "decision owed on L6")
    assert h.state()["stops"]["stop-1"]["claims"] == [key], "the stop names the check"
    assert ([item["key"] for item in reopened["newItems"]], reopened["next"]) == (
        ["ledger:L6"],
        "triage",
    )
    assert after["next"] == "wait_ci", "applied; the red check still blocks done"


def test_T1_a_red_check_triaged_no_change_waits_for_ci(make):
    """T1: a red check triaged No change needs nothing more as an item; the run waits on CI.

    Bug: red CI sent the agent back to triage on every pass, then stopped the run as a repeat
    ("checks still fail after triage"), or let done pass a red check.
    """
    h = synced(make, COVERED)
    key = red_check(h)
    h.triage(
        [
            {
                "claim": "checks also fail on main",
                "class": None,
                "disposition": "No change",
                "sources": [key],
            }
        ]
    )

    passes = []
    for _ in range(2):
        h.advance(60)
        passes.append(h.ok("sync"))

    assert [(p["next"], p["newItems"]) for p in passes] == [("wait_ci", []), ("wait_ci", [])]
    assert (h.state()["status"], h.state()["stops"]) == ("running", {}), (
        "no repeat stop, never done"
    )


def test_T1_a_red_check_triaged_fix_is_pushed(make):
    """T1: a red check triaged Fix takes the unpushed-fix path, then the push gives a new head.

    Bug: red CI hid the unpushed fix behind wait_ci, so the fix was never pushed and the run stopped
    after 30 minutes of waiting on the CI it was meant to repair.
    """
    h = make(COVERED)
    work = h.synced_branch()
    key = red_check(h)
    h.triage([{**FIX, "claim": "a test fails", "sources": [key]}])

    unpushed = h.ok("sync")
    new_head = h.commit(work, "fix: repair the failing test")
    h.ok("push", cwd=work)
    h.set_head(new_head)
    h.set_checks(PASS)
    waiting = h.ok("sync", "--ceiling", "60")

    assert (unpushed["next"], unpushed["reason"]) == ("triage", "unpushed fix L6")
    assert (waiting["head"], waiting["next"]) == (new_head, "wait_reviewer"), (
        "the new head waits for its review"
    )


def test_T1_an_old_heads_red_check_is_not_taken_in_when_the_head_moves(make):
    """T1: when the head moves during sync, the red checks read for the old head are not taken in.

    Bug: an old head's failing check entered the inbox and cost a round.
    """
    h = synced(make, COVERED)
    h.set_checks(RED)
    (h.ctl / "move_head_on_checks").write_text(NEW_HEAD)

    moved = h.ok("sync", "--ceiling", "60")

    assert (moved["headChanged"], moved["newItems"], moved["round"]) == (True, [], 1)
    assert (moved["next"], h.state()["inbox"]) == ("wait_reviewer", [])


def test_T1_a_new_failure_on_a_new_head_enters_the_inbox_again(make):
    """T1: check keys name the head, so a failure on a new head is a new item.

    Bug: a check handled on one head hid the same check failing again on the next head.
    """
    h = synced(make, COVERED)
    key = red_check(h)
    h.triage(
        [
            {
                "claim": "checks also fail on main",
                "class": None,
                "disposition": "No change",
                "sources": [key],
            }
        ]
    )
    h.set_head(NEW_HEAD)

    again = h.ok("sync", "--ceiling", "60")

    assert ([item["key"] for item in again["newItems"]], again["next"]) == (
        [f"check:{NEW_HEAD}:checks"],
        "triage",
    )


@NEW_INPUT
def test_T4_ci_pending_for_30_minutes_stops_the_run(make, new_input: str | None):
    """T4: wait_ci on one head is bounded at 30 minutes from the first wait_ci.

    Bug: an endless loop; a stuck CI run keeps the agent polling forever. New input restarts the
    clock.
    """
    h = synced(make, COVERED)
    h.set_checks([{"name": "checks", "bucket": "pending"}])
    assert h.ok("sync")["next"] == "wait_ci"
    h.advance(1000)
    assert h.ok("sync")["next"] == "wait_ci"
    if new_input == "new-item":
        h.add_comment("P2: one more case.")
        assert h.ok("sync")["next"] == "triage"
        h.noise()
    elif new_input == "new-head":
        h.set_head(NEW_HEAD)
        assert h.ok("sync", "--ceiling", "30")["next"] == "wait_reviewer"
        h.react()
    h.advance(800)

    last = h.ok("sync")

    if new_input is None:
        condition = f"CI still pending on {last['head'][:7]} after 30 minutes"
        assert (last["next"], last["resumeCondition"]) == ("needs_input", condition)
    else:
        assert (last["next"], h.state()["status"]) == ("wait_ci", "running"), (
            "new input restarts the 30 minutes"
        )


# --- decisions and changes across pause and resume ------------------------------------------


def test_T1_a_decided_claim_is_triaged_again_before_done(make):
    """T1: resume sends each open Needs decision claim back to triage with the decision attached.

    Bug: resume counted the decision as applied, so on an unchanged, covered, green head done fired
    while the claim was still Needs decision and its thread stayed open.
    """
    h = synced(make, COVERED)
    h.triage([DESIGN, PROPOSAL])
    assert h.ok("sync")["resumeCondition"] == "decision owed on L6"
    h.ok("resume", "--decision", "keep the cursor as it is")

    reopened = h.ok("sync")

    assert (reopened["next"], h.state()["status"]) == ("triage", "running")
    [item] = reopened["newItems"]
    assert (item["key"], item["kind"], item["sources"]) == (
        "ledger:L6",
        "decision",
        DESIGN["sources"],
    )
    assert (item["decision"]["text"], item["decision"]["by"]) == (
        "keep the cursor as it is",
        "owner",
    )
    applied = {
        "claim": DESIGN["claim"],
        "class": None,
        "disposition": "No change",
        "sameAs": "L6",
        "sources": [],
    }
    h.triage([applied])
    assert h.ok("sync")["next"] == "done"


@pytest.mark.parametrize("posted", [True, False], ids=["found-on-github", "not-posted"])
def test_S4_resume_reconciles_an_unknown_post_with_github(make, posted: bool):
    """S4: after resume, sync looks for the unknown post on GitHub before it does anything else.

    Bug: resume cleared pending without checking, so a post that did reach GitHub came back as new
    feedback (or was posted twice), and a post that did not was never retried.
    """
    h = synced(make, WAITING)
    body = "Fixed in 64a1289: the ceiling queries once more."
    result = h.post("--thread", "4237011925", body=body, FAKE_POST="postfail" if posted else "fail")
    assert result.returncode == 1 and "Fixed in" not in h.state_file.read_text(), (
        "pending keeps a hash, not the body"
    )
    assert h.quiet_sync()["next"] == "needs_input"
    h.ok("resume", "--decision", "checked the PR")
    unconfirmed = "a post to thread 4237011925 is unconfirmed; run sync to check the PR first"
    h.refused(unconfirmed, "triage", "--file", h.write("t.json", "[]"))

    reconciled = h.ok("sync", "--ceiling", "60")

    with h.pr_data() as pr:
        replies = [
            c["databaseId"] for c in pr["reviewThreads"]["nodes"][1]["comments"]["nodes"][1:]
        ]
    key = f"thread:{replies[0]}" if posted else None
    assert reconciled["reconciled"] == {
        "kind": "thread", "target": 4237011925, "posted": posted, "key": key,
        "matches": 1 if posted else 0,
    }  # fmt: skip
    assert (reconciled["newItems"], h.state()["pending"]) == ([], None)
    assert len(h.calls()[1]) == 1, "nothing is posted twice"
    assert h.ok("sync", "--ceiling", "60")["newItems"] == [], "the found post is not new feedback"


def test_S4_a_reconciled_thread_post_records_a_reply_receipt(make):
    """S4 (D3): a thread post that sync finds on GitHub after resume records the same reply receipts
    as post, and skips a Fix that was not pushed yet.

    Bug: reconcile marked the comment handled but recorded no receipt, so a claim that the reply
    answered still asked for one, and the agent posted it twice.
    """
    h = synced(make, WAITING)
    addressed = {"claim": "query once more", "class": None, "disposition": "Already addressed"}
    h.triage(
        [{**addressed, "sources": ["thread:4237011925"]}, {**FIX, "sources": ["thread:4237011925"]}]
    )
    body = "Already addressed: the ceiling queries once more."
    assert h.post("--thread", "4237011925", body=body, FAKE_POST="postfail").returncode == 1
    h.ok("resume", "--decision", "checked the PR")

    reconciled = h.ok("sync", "--ceiling", "60")

    assert reconciled["reconciled"]["posted"] is True
    reply_id = int(reconciled["reconciled"]["key"].split(":")[1])
    settled, fix = h.state()["ledger"][-2:]
    assert (settled["id"], settled.get("replies")) == ("L6", {"thread:4237011925": reply_id})
    assert (fix["id"], fix["disposition"], fix.get("replies")) == ("L7", "Fix", None), (
        "no receipt before the push"
    )


def test_T1_changes_made_while_paused_are_picked_up_after_resume(make):
    """T1: a head pushed by hand and a comment added while paused come in with the next sync.

    Bug: the run trusted its state from before the pause, so the new head counted as reviewed and
    done fired before the reviewer covered it, and the new comment was never triaged.
    """
    h = make(COVERED)
    work = h.synced_branch()
    h.ok("pause", "--reason", "the owner reworks the cursor")
    new_head = h.commit(work, "fix: rework the cursor")
    h.isolated.git(work, "push", "-q", "origin", "feature")
    h.set_head(new_head)
    h.advance(120)
    mine = h.add_comment("I reworked the cursor; please look again.", author="octocat")
    h.ok("resume", "--decision", "reworked the cursor by hand")
    seen_at = h.now

    resumed = h.ok("sync")
    h.noise()
    waiting = h.ok("sync", "--ceiling", "60")
    h.react()
    covered = h.ok("sync")

    assert h.state()["heads"][new_head] == {"firstSeen": seen_at, "source": "seen"}
    assert ([item["key"] for item in resumed["newItems"]], resumed["next"]) == (
        [f"issue:{mine}"],
        "triage",
    )
    assert waiting["next"] == "wait_reviewer", "done waits until the reviewer covers the new head"
    assert covered["next"] == "done"


# --- the working directory and CI as gh reports them -----------------------------------------


@pytest.mark.parametrize("command", ["post-issue", "post-thread", "push"])
def test_post_and_push_refuse_a_checkout_of_another_repository(make, command: str):
    """post (which still checks the cwd's repository, as when its helper posted there) and push
    (which pushes the cwd's HEAD) run only from a checkout of <owner/repo>, before any write or
    call.

    Bug: a reply posted to the wrong repository, reported as success. During the 2026-10-14 smoke,
    a run from another checkout posted to PR 4 of another repository.
    """
    h = make(COVERED)
    work = h.synced_branch()
    (h.ctl / "cwd_repo").write_text("octo-org/elsewhere")
    h.isolated.git(work, "remote", "set-url", "origin", "git@github.com:octo-org/elsewhere.git")
    options = {
        "post-issue": ["--issue", "--body-file", h.write("b.md", "Summary")],
        "post-thread": ["--thread", "4237011925", "--body-file", h.write("b.md", "Fixed.")],
    }.get(command, [])
    before, state_before = h.calls(), h.state_file.read_bytes()

    result = h.run(command.split("-")[0], *options, cwd=work)

    error = (
        "the working directory is in octo-org/elsewhere, not octo-org/hello-world; "
        "run siula round from the PR's worktree"
    )
    assert (result.returncode, result.stdout) == (1, "")
    assert f"ERROR: {error}" in result.stderr, result.stderr
    gh_after, posts_after, git_after = h.calls()
    assert (gh_after, posts_after) == before[:2], "no gh call and no post ran"
    assert not any(
        " fetch " in f" {call} " or " push " in f" {call} " for call in git_after[len(before[2]) :]
    )
    assert h.state_file.read_bytes() == state_before


def test_sync_reads_the_named_repository_from_any_working_directory(make):
    """sync names <owner/repo> on every gh call, so the working directory does not matter.

    Bug: sync read the working directory's repository; from another checkout, every call fails or
    reads another PR.
    """
    h = synced(make, COVERED)
    (h.ctl / "cwd_repo").write_text("octo-org/elsewhere")

    result = h.ok("sync")

    assert (result["next"], result["head"]) == ("done", "aab71a1d1f34045997403a953db40b6ab2b38352")


def test_post_records_no_receipt_for_a_misrouted_post(make):
    """post records a receipt only when the response points to <owner/repo> and PR <n>.

    Bug: a misrouted post was recorded as a receipt: its id became handled, the claim got a reply,
    and the thread was resolved, although the reply landed in another repository.
    """
    h = synced(make, WAITING)
    h.triage([{**NOT_FIXED, "sources": ["thread:4237011925"]}])
    before = h.state()

    result = h.post(
        "--thread", "4237011925", "--resolve", body="Fixed.", FAKE_POST_REPO="octo-org/elsewhere"
    )

    condition = (
        "the post to thread 4237011925 on octo-org/hello-world#154 landed on octo-org/elsewhere#154"
    )
    assert (result.returncode, result.stdout) == (1, "")
    assert f"ERROR: {condition}; delete it there, then resume" in result.stderr, result.stderr
    after = h.state()
    assert (after["status"], h.resume_condition()) == (
        "needs_input",
        f"{condition}; delete it there, then resume",
    )
    assert (after["handled"], after["pending"]) == (before["handled"], None)
    assert (after["ledger"][-1]["id"], after["ledger"][-1]["replies"]) == ("L6", None), (
        "no receipt on the claim"
    )
    resolver_calls = [
        call for call in h.calls()[0] if "resolveReviewThread" in call or "__typename" in call
    ]
    assert resolver_calls == [], "no thread lookup or resolver ran"


def test_post_refuses_a_target_that_the_poster_cannot_name_and_records_nothing(make):
    """post builds the poster's REST path before it records a pending post. An input error refuses,
    and nothing is recorded or posted.

    Bug: the pending record was written first, so a repository name that GitHub cannot have (an
    underscore in the owner) stopped the run as an unknown post, which asks a human to look for a
    post that was never sent.
    """
    repo = "octo_org/hello-world"
    h = make(COVERED)
    for name in ("pr_repo", "cwd_repo"):
        (h.ctl / name).write_text(repo)
    state_root = h.isolated.home / ".local" / "state" / "siula" / "review"
    state_file = state_root / "octo_org" / "hello-world" / h.pr / "state.json"
    assert h.ok("sync", repo=repo)["next"] == "triage"
    before, state_before = h.calls(), state_file.read_bytes()

    result = h.run("post", "--issue", "--body-file", h.write("b.md", "Summary"), repo=repo)

    assert (result.returncode, result.stdout) == (1, ""), result.stdout
    error = (
        f"cannot post to {repo}#154: --repo must be OWNER/NAME, got '{repo}'; nothing was posted"
    )
    assert f"ERROR: {error}" in result.stderr, result.stderr
    assert h.calls() == before, "no handle read and no post ran"
    assert state_file.read_bytes() == state_before, "no pending post is recorded"


# --- the reviewers file -------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["file", "config-directory"])
def test_a_missing_reviewers_file_means_no_reviewer_is_configured(make, missing: str):
    """T1 (D1): with no reviewers file, no reviewer is configured, and done reports that gap.

    Bug: a missing ~/.config/siula/reviewers.json failed the collector ("--reviewers file not
    found"), so sync failed on every machine that had not written one, and no run could start.
    """
    h = make(COVERED)
    if missing == "file":
        h.reviewers.unlink()
    else:
        shutil.rmtree(h.isolated.home / ".config")

    first = h.ok("sync")
    h.noise()
    done = h.ok("sync")

    assert first["reviewer"]["state"] == "unconfigured", first["reviewer"]
    assert (done["next"], done["gaps"]) == ("done", [f"no reviewer configured for {REPO}"])
    assert (done.get("target"), h.state()["target"]) == ("ready_with_gaps", "ready_with_gaps")


@pytest.mark.parametrize("where", ["home-config", "xdg-config-home", "relative-xdg-config-home"])
def test_a_reviewers_file_that_cannot_be_parsed_stops_sync(make, where: str):
    """A reviewers file that exists but cannot be parsed stops sync before the round changes. The
    file is $XDG_CONFIG_HOME/siula/reviewers.json when XDG_CONFIG_HOME is set and absolute, and
    ~/.config/siula/reviewers.json otherwise.

    Bug: a broken file read as "no reviewer configured", so the run ended ready_with_gaps without
    the reviewer that the owner configured; or siula read another file and never saw the break.
    """
    h = synced(make, COVERED)
    before = h.state()
    broken, env = h.reviewers, {}
    if where == "xdg-config-home":  # the valid file in ~/.config must not be read
        broken = h.root / "xdg" / "siula" / "reviewers.json"
        env["XDG_CONFIG_HOME"] = str(h.root / "xdg")
    elif where == "relative-xdg-config-home":  # a valid file at the relative path must not be read
        valid = h.root / "xdg" / "siula" / "reviewers.json"
        valid.parent.mkdir(parents=True)
        valid.write_text(json.dumps(REVIEWERS))
        env["XDG_CONFIG_HOME"] = "xdg"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not json")

    result = h.run("sync", **env)

    assert (result.returncode, result.stdout) == (1, ""), result.stdout
    assert (
        "ERROR: the collector failed: --reviewers file must hold one JSON object" in result.stderr
    )
    assert f"'{broken}'" in result.stderr, f"the error names the file it read\n{result.stderr}"
    after = h.state()
    keys = ("round", "handled", "inbox", "status")
    assert [after[key] for key in keys] == [before[key] for key in keys]


def test_sync_handles_a_head_without_checks(make):
    """sync reads "no checks" from gh's JSON output, not from its stderr wording. D1: a done on a
    head without checks is ready_with_gaps.

    Bug: sync failed on every head without checks; gh 2.102.0 prints "no checks reported" and
    exits 1 even with --required, which did not match the expected "no required checks reported";
    or (D1) a PR whose head no CI ran on was reported ready for a human merge.
    """
    h = make(COVERED)
    h.set_checks(None, None)

    first = h.ok("sync")
    h.noise()
    h.advance(120)
    later = h.ok("sync")

    assert (first["next"], first["ci"]["state"], first["ci"].get("reason")) == (
        "triage",
        "pending",
        "no checks yet",
    )
    assert (later["next"], later["ci"]["state"], later["gaps"]) == (
        "done",
        "none",
        [f"no CI checks on {later['head'][:7]}"],
    )
    shown = h.ok("show")
    assert (later.get("target"), shown["target"]) == ("ready_with_gaps", "ready_with_gaps")
    assert shown.get("gaps") == [f"no CI checks on {later['head'][:7]}"]


# --- Codex review 5515439321 on #159: guards need positive evidence --------------------------


def test_S5_an_answer_to_another_stop_decides_no_claim(make):
    """S5: only an answer to a decision_owed or blocking_review stop decides the claims it listed.

    Bug: an answer to another stop decided an open claim. A design verdict left L1 owed; the design
    summary's post result was unknown; resuming that stop with "it posted" counted as the decision,
    so done could pass the open claim.
    """
    h = synced(make, COVERED)
    h.triage([DESIGN, PROPOSAL])
    assert h.post("--issue", body="Review round 1: design", FAKE_POST="postfail").returncode == 1
    h.ok("resume", "--decision", "it posted")

    owed = h.ok("sync", "--ceiling", "60")

    assert owed["reconciled"]["posted"] is True
    assert (owed["next"], owed["resumeCondition"], owed["newItems"]) == (
        "needs_input",
        "decision owed on L6",
        [],
    )
    stops = h.state()["stops"]
    assert [(sid, stop["condition"], stop["claims"]) for sid, stop in stops.items()] == [
        ("stop-1", "unknown_post", ["issue:6095816171"]),
        ("stop-2", "decision_owed", ["issue:6095816171"]),
    ]
    assert stops["stop-1"]["answer"]["text"] == "it posted" and stops["stop-2"]["answer"] is None
    h.ok("resume", "--decision", "keep the cursor as it is")
    assert [item["key"] for item in h.ok("sync")["newItems"]] == ["ledger:L6"], (
        "now the claim is decided"
    )


def test_S5_an_answer_decides_only_the_ledger_entries_its_stop_listed(make):
    """S5 (Q3): an answered decision stop decides only the ledger entries it listed. A later Needs
    decision claim on the same comment, with its own proposal, gets its own stop and its own answer.

    Bug (found in review, 2026-10-23): decided() matched a stop to a claim by source keys. After L6
    (proposal P1) was decided and settled, a new Needs decision claim with proposal P2 that cited
    the same issue comment read as decided by the old answer, so sync asked the agent to apply a
    decision that the owner never gave for P2.
    """
    h = synced(make, COVERED)
    h.triage([DESIGN, PROPOSAL])
    assert h.ok("sync")["resumeCondition"] == "decision owed on L6"
    h.ok("resume", "--decision", "A")
    assert [item["key"] for item in h.ok("sync")["newItems"]] == ["ledger:L6"]
    h.triage([{**NOT_FIXED, "sameAs": "L6"}])
    h.advance(60)
    late = h.add_comment("Should the cursor be public too?", author="monalisa")
    assert [item["key"] for item in h.ok("sync")["newItems"]] == [f"issue:{late}"]
    sources = [f"issue:{late}", "issue:6095816171"]
    second = {**DESIGN, "claim": "Make the cursor public", "sources": sources}
    summary = "Root: who may read the cursor."
    h.triage([second, {"proposal": {**PROPOSAL["proposal"], "summary": summary}}])

    owed = h.ok("sync")

    state = h.state()
    assert [(e["id"], e["disposition"], e["proposal"]) for e in state["ledger"][5:]] == [
        ("L6", "Needs decision", "P1"),
        ("L7", "No change", None),
        ("L8", "Needs decision", "P2"),
    ]
    assert (owed["next"], owed["reason"], owed.get("resumeCondition")) == (
        "needs_input",
        "decision owed on L8",
        "decision owed on L8",
    )
    stop = state["stops"]["stop-2"]
    assert (stop["condition"], stop["claims"], stop["entries"], stop["answer"]) == (
        "decision_owed",
        sorted(sources),
        ["L8"],
        None,
    )
    h.ok("resume", "--decision", "B")
    [item] = h.ok("sync")["newItems"]
    assert (item["key"], item["proposal"]["summary"]) == ("ledger:L8", summary), (
        "the answer reopens the claim it was asked about, with its proposal"
    )


def test_S5_a_decision_stop_without_entries_decides_nothing_and_asks_again(make):
    """S5: a decision stop written before stops named their ledger entries still loads, but its
    answer decides no claim. The claim is asked again in a new stop that names it, so the run
    fails safe.

    Bug guarded: an old stop names only source keys, which a later claim on the same comment can
    share, so reading its answer as a decision could decide a claim that nobody asked about.
    """
    h = synced(make, COVERED)
    h.triage([DESIGN, PROPOSAL])
    assert h.ok("sync")["resumeCondition"] == "decision owed on L6"
    old = h.state()
    del old["stops"]["stop-1"]["entries"]
    h.state_file.write_text(json.dumps(old))
    h.ok("resume", "--decision", "A")

    again = h.ok("sync")

    assert (again["next"], again["resumeCondition"], again["newItems"]) == (
        "needs_input",
        "decision owed on L6",
        [],
    )
    stops = h.state()["stops"]
    assert [
        (sid, stop["condition"], stop.get("entries"), stop["answer"] is None)
        for sid, stop in stops.items()
    ] == [("stop-1", "decision_owed", None, False), ("stop-2", "decision_owed", ["L6"], True)]
    h.ok("resume", "--decision", "A")
    assert [item["key"] for item in h.ok("sync")["newItems"]] == ["ledger:L6"]


def test_S5_a_resume_answers_only_the_open_stop(make):
    """S5: each resume writes one answer, into the stop that is open; older answers stay.

    Bug: a resume rewrote an older stop's answer.
    """
    h = synced(make, COVERED)
    h.ok("pause", "--reason", "first question")
    h.ok("resume", "--decision", "first answer")
    h.ok("pause", "--reason", "second question")
    h.ok("resume", "--decision", "second answer", "--by", "Alex")

    stops = h.state()["stops"]

    assert {
        sid: (stop["detail"], stop["answer"]["text"], stop["answer"]["by"])
        for sid, stop in stops.items()
    } == {
        "stop-1": ("first question", "first answer", "owner"),
        "stop-2": ("second question", "second answer", "Alex"),
    }


def test_S5_a_resume_with_no_open_stop_is_refused(make):
    """S5: resume needs an open stop to answer; otherwise it writes nothing.

    Bug: a decision was recorded with nothing to answer, and a later claim read it as its decision.
    """
    h = synced(make, COVERED)
    before = h.state_file.read_bytes()

    h.refused(
        "there is no open stop to answer (status is running); nothing was recorded",
        "resume",
        "--decision",
        "x",
    )

    assert h.state_file.read_bytes() == before


def full_state(h: Round) -> dict:
    """A valid synced state with one record in every container.

    Every nested field can then be broken.
    """
    state = h.state()
    head, at = state["head"], h.now
    item = {
        "key": "thread:11",
        "kind": "thread",
        "id": 11,
        "head": head,
        "source": "collector",
        "body": "b",
        "bodySha256": "0" * 64,
    }
    entry = {
        "id": "L1",
        "round": 1,
        "head": head,
        "claim": "c",
        "class": "edges",
        "disposition": "Fix",
    }
    entry.update(sameAs=None, sources=["thread:11"], pushed=head, replies={"thread:11": 12})
    entry.update(proposal=None)
    stop = {
        "condition": "pause",
        "detail": "x",
        "claims": ["thread:11"],
        "entries": ["L1"],
        "answer": {"text": "go", "by": "M", "at": at},
    }
    pending = {"kind": "thread", "target": 11, "at": at, "bodySha256": "0" * 64, "resumedAt": at}
    last = {"head": head, "next": "wait_ci", "reason": "ci not green", "firstAt": at}
    proposal = {
        "round": 1,
        "summary": "s",
        "options": {"A": "keep", "B": "drop"},
        "recommended": "A",
        "at": at,
    }
    state.update(handled=["thread:11"], inbox=[item], ledger=[entry], stops={"stop-1": stop})
    state.update(
        status="running", pending=pending, last=last, target=None, gaps=["no CI checks on aab71a1"]
    )
    state.update(items={"thread:11": {**item, "truncated": False}}, proposals={"P1": proposal})
    return state


def test_S1_the_full_state_fixture_loads(make):
    """The base of the schema table loads, so each refusal in the table comes from its one broken
    field.
    """
    h = synced(make, COVERED)
    h.state_file.write_text(json.dumps(full_state(h)))

    assert h.ok("show")["inbox"][0]["key"] == "thread:11"


NOT_LIST, NOT_MAP = (True, 1, "x", {}, None), (True, 1, "x", [], None)
NOT_LIST_OR_NULL, NOT_MAP_OR_NULL = (True, 1, "x", {}), (True, 1, "x", [])
NOT_RECORD, NOT_RECORD_OR_NULL = (True, 1, "x", [], None), (True, 1, "x", [])
NOT_TEXT, NOT_TEXT_OR_NULL = (True, 1, [], {}, None), (True, 1, [], {})
NOT_INT, NOT_BOOL = (True, "x", [], {}, None, 1.5), (1, "x", [], {}, None)
NOT_ID, NOT_ID_OR_NULL = (
    (True, 1, "x", [], {}, None),
    (True, 1, "x", [], {}),
)  # "x" is not a typed id
NOT_CLOSED, NOT_CLOSED_OR_NULL = (True, 1, "x", [], {}, None), (True, 1, "x", [], {})
# (path into the state, the field the error names, wrong values); HEAD is the head SHA
SCHEMA_TABLE = [
    (("repo",), "repo", NOT_ID),
    (("pr",), "pr", NOT_INT),
    (("status",), "status", NOT_CLOSED),
    (("round",), "round", NOT_INT),
    (("maxRounds",), "maxRounds", NOT_INT),
    (("skillSha",), "skillSha", NOT_TEXT),
    (("handled",), "handled", NOT_LIST),
    (("handled", 0), "handled[0]", NOT_ID),
    (("handled", 0), "handled[0]", ("thread:11@1", "thread:11@02", "finding:3@2")),
    (("inbox",), "inbox", NOT_LIST),
    (("inbox", 0), "inbox[0]", NOT_RECORD),
    (("inbox", 0, "key"), "inbox[0].key", NOT_ID),
    (("inbox", 0, "kind"), "inbox[0].kind", NOT_CLOSED),
    (("inbox", 0, "id"), "inbox[0].id", (True, None, [], {}, 1.5)),
    (("inbox", 0, "head"), "inbox[0].head", NOT_ID_OR_NULL),
    (("inbox", 0, "source"), "inbox[0].source", NOT_CLOSED),
    (("inbox", 0, "bodySha256"), "inbox[0].bodySha256", NOT_ID_OR_NULL),
    (("head",), "head", NOT_ID_OR_NULL),
    (("heads",), "heads", NOT_MAP),
    (("heads", "HEAD"), "heads.HEAD", NOT_RECORD),
    (("heads", "HEAD", "firstSeen"), "heads.HEAD.firstSeen", NOT_ID),
    (("heads", "HEAD", "source"), "heads.HEAD.source", NOT_CLOSED),
    (("ciSeen",), "ciSeen", NOT_BOOL),
    (("ledger",), "ledger", NOT_LIST),
    (("ledger", 0), "ledger[0]", NOT_RECORD),
    (("ledger", 0, "id"), "ledger[0].id", NOT_ID),
    (("ledger", 0, "round"), "ledger[0].round", NOT_INT),
    (("ledger", 0, "head"), "ledger[0].head", NOT_ID_OR_NULL),
    (("ledger", 0, "claim"), "ledger[0].claim", NOT_TEXT),
    (("ledger", 0, "class"), "ledger[0].class", NOT_CLOSED_OR_NULL),
    (("ledger", 0, "disposition"), "ledger[0].disposition", NOT_CLOSED),
    (("ledger", 0, "sameAs"), "ledger[0].sameAs", NOT_ID_OR_NULL),
    (("ledger", 0, "sources"), "ledger[0].sources", NOT_LIST),
    (("ledger", 0, "sources", 0), "ledger[0].sources[0]", NOT_ID),
    (("ledger", 0, "pushed"), "ledger[0].pushed", NOT_ID_OR_NULL),
    (("ledger", 0, "replies"), "ledger[0].replies", NOT_MAP_OR_NULL),
    (("ledger", 0, "replies", "thread:11"), "ledger[0].replies.thread:11", NOT_INT),
    (("stops",), "stops", NOT_MAP),
    (("stops", "stop-1"), "stop-1", NOT_RECORD),
    (("stops", "stop-1", "condition"), "stop-1 condition", NOT_CLOSED),
    (("stops", "stop-1", "detail"), "stop-1 detail", NOT_TEXT_OR_NULL),
    (("stops", "stop-1", "claims"), "stop-1 claims", NOT_LIST),
    (("stops", "stop-1", "claims", 0), "stop-1 claims[0]", NOT_ID),
    (("stops", "stop-1", "entries"), "stop-1 entries", NOT_LIST_OR_NULL),
    (("stops", "stop-1", "entries", 0), "stop-1 entries[0]", NOT_ID),
    (("stops", "stop-1", "answer"), "stop-1 answer", NOT_RECORD_OR_NULL),
    (("stops", "stop-1", "answer", "text"), "stop-1 answer.text", NOT_TEXT),
    (("stops", "stop-1", "answer", "by"), "stop-1 answer.by", NOT_TEXT),
    (("stops", "stop-1", "answer", "at"), "stop-1 answer.at", NOT_ID),
    (("pending",), "pending", NOT_RECORD_OR_NULL),
    (("pending", "kind"), "pending.kind", NOT_CLOSED),
    (("pending", "target"), "pending.target", NOT_INT),
    (("pending", "at"), "pending.at", NOT_ID),
    (("pending", "bodySha256"), "pending.bodySha256", NOT_ID),
    (("pending", "resumedAt"), "pending.resumedAt", NOT_ID_OR_NULL),
    (("last",), "last", NOT_RECORD_OR_NULL),
    (("last", "head"), "last.head", NOT_ID),
    (("last", "next"), "last.next", NOT_CLOSED),
    (("last", "reason"), "last.reason", NOT_TEXT_OR_NULL),
    (("last", "firstAt"), "last.firstAt", NOT_ID),
    (("target",), "target", NOT_CLOSED_OR_NULL),
    (("gaps",), "gaps", NOT_LIST_OR_NULL),
    (("gaps", 0), "gaps[0]", NOT_TEXT),
    # Q1-Q5: the learning signals are strict, because no version-3 state predates them.
    (("ledger", 0, "proposal"), "ledger[0].proposal", NOT_ID_OR_NULL),
    (("items",), "items", NOT_MAP),
    (("items", "thread:11"), "items.thread:11", NOT_RECORD),
    (("items", "thread:11", "key"), "items.thread:11.key", NOT_ID),
    (("items", "thread:11", "kind"), "items.thread:11.kind", (*NOT_CLOSED, "decision")),
    (("items", "thread:11", "id"), "items.thread:11.id", (True, None, [], {}, 1.5)),
    (("items", "thread:11", "head"), "items.thread:11.head", NOT_ID_OR_NULL),
    (("items", "thread:11", "source"), "items.thread:11.source", (*NOT_CLOSED, "resume")),
    (("items", "thread:11", "truncated"), "items.thread:11.truncated", NOT_BOOL),
    (("items", "thread:11", "bodySha256"), "items.thread:11.bodySha256", NOT_ID_OR_NULL),
    (("proposals",), "proposals", NOT_MAP),
    (("proposals", "P1"), "proposals.P1", NOT_RECORD),
    (("proposals", "P1", "round"), "proposals.P1.round", NOT_INT),
    (("proposals", "P1", "summary"), "proposals.P1.summary", NOT_TEXT),
    (("proposals", "P1", "options"), "proposals.P1.options", NOT_MAP),
    (("proposals", "P1", "options", "A"), "proposals.P1.options.A", NOT_TEXT),
    (("proposals", "P1", "recommended"), "proposals.P1.recommended", NOT_ID),
    (("proposals", "P1", "at"), "proposals.P1.at", NOT_ID),
]


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [(path, field, value) for path, field, values in SCHEMA_TABLE for value in values],
    ids=lambda part: json.dumps(part) if not isinstance(part, tuple) else ".".join(map(str, part)),
)
def test_S1_a_wrong_type_in_any_state_field_is_refused_without_a_traceback(
    make, path: tuple, field: str, value: Any
):
    """S1: the declared state schema checks every field's type before code reads inside it.

    Bug: a malformed state file crashed with a traceback instead of failing closed ("pending": []
    and "inbox": true reached .get or iteration before their type was checked).
    """
    h = synced(make, COVERED)
    state = full_state(h)
    head = state["head"]
    *parents, last = [head if part == "HEAD" else part for part in path]
    target = state
    for part in parents:
        target = target[part]
    target[last] = value
    h.state_file.write_text(json.dumps(state))
    before = h.state_file.read_bytes()
    named = field.replace("HEAD", head)

    result = h.run("show")

    assert (result.returncode, result.stdout) == (1, ""), result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    unreadable = f"(unknown value {named} = {value!r}); move it aside to start again"
    assert result.stderr == f"ERROR: state file {h.state_file} is unreadable {unreadable}\n", (
        result.stderr
    )
    assert h.state_file.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [("condition", "decision owed"), ("status", "paused"), ("source", "guessed")],
)
@pytest.mark.parametrize("command", ["sync", "resume"])
def test_S1_an_unknown_value_in_the_state_file_is_refused(
    make, field: str, value: str, command: str
):
    """S1: every field that code reads is a closed list or a typed id; any other value is refused on
    load.

    Bug: an unknown value in the state file was acted on (a hand-edited condition decided claims,
    or an unknown status was treated as running).
    """
    h = synced(make, COVERED)
    h.ok("pause", "--reason", "check")
    state = h.state()
    if field == "condition":
        state["stops"]["stop-1"]["condition"], named = value, "stop-1 condition"
    elif field == "status":
        state["status"], named = value, "status"
    else:
        state["heads"][state["head"]]["source"], named = value, f"heads.{state['head']}.source"
    h.state_file.write_text(json.dumps(state))
    before = h.state_file.read_bytes()

    error = (
        f"state file {h.state_file} is unreadable (unknown value {named} = {value!r}); "
        "move it aside to start again"
    )
    h.refused(error, command, *(["--decision", "go"] if command == "resume" else []))

    assert h.state_file.read_bytes() == before


NOISE = {"disposition": "Noise", "class": None}
STORED_PROPOSAL = {
    "round": 1,
    "summary": "s",
    "recommended": "A",
    "at": COVERED,
}  # plus its options
ITEM_11 = {
    "key": "thread:11",
    "kind": "thread",
    "id": 11,
    "head": None,
    "source": "collector",
    "truncated": False,
}
# (where, the change, the field the error names, its value); full_state's L1 is a Fix
RELATION_BREAKS = [
    (("ledger", 0), {**NOISE, "proposal": "P1"}, "ledger[0].proposal", "P1"),
    (("ledger", 0), {"proposal": "P1"}, "ledger[0].proposal", "P1"),
    (
        ("ledger", 0),
        {"disposition": "Needs decision", "proposal": "P2"},
        "ledger[0].proposal",
        "P2",
    ),
    (("ledger", 0), {"disposition": "Needs decision"}, "ledger[0].proposal", None),
    (("ledger", 0), {"sources": ["thread:11", "issue:13"]}, "ledger[0].sources[1]", "issue:13"),
    (("proposals", "P1"), {"options": {"A": "keep"}}, "proposals.P1.options", {"A": "keep"}),
    (
        ("proposals", "P1"),
        {"options": {"B": "keep", "A": "drop"}},
        "proposals.P1.options",
        {"B": "keep", "A": "drop"},
    ),
    (
        ("proposals", "P1"),
        {"options": {"A": "keep", "C": "drop"}},
        "proposals.P1.options",
        {"A": "keep", "C": "drop"},
    ),
    (("proposals", "P1", "options"), {"E": "five"}, "proposals.P1.options key", "E"),
    (("proposals", "P1"), {"recommended": "C"}, "proposals.P1.recommended", "C"),
    (
        ("proposals",),
        {"X1": {**STORED_PROPOSAL, "options": {"A": "a", "B": "b"}}},
        "proposals key",
        "X1",
    ),
    (("items", "thread:11"), {"key": "thread:12"}, "items.thread:11.key", "thread:12"),
    (("items",), {"ledger:L1": {**ITEM_11, "key": "ledger:L1"}}, "items key", "ledger:L1"),
]


@pytest.mark.parametrize(
    ("where", "change", "field", "value"),
    RELATION_BREAKS,
    ids=[
        "noise-with-a-proposal",
        "a-proposal-on-a-fix",
        "a-proposal-that-is-not-stored",
        "a-needs-decision-entry-without-a-proposal",
        "a-source-that-is-not-archived",
        "one-option",
        "options-out-of-order",
        "an-option-letter-skipped",
        "a-fifth-option",
        "recommended-outside-the-options",
        "a-proposal-id-that-is-not-P-n",
        "an-item-under-another-key",
        "an-archived-decision",
    ],
)
def test_S1_a_broken_learning_signal_in_the_state_file_is_refused(
    make, where: tuple, change: dict, field: str, value
):
    """S1 (Q1-Q3): load checks the rules between the new fields: a noise entry has no proposal; a
    proposal id names a stored proposal of a Needs decision entry, and every Needs decision entry
    names one; a proposal has 2 to 4 options A, B, ... in order and recommends one; every ledger
    source has an archived item; an archived item sits under its own key, which is never a
    ledger:L<n> key.

    Bug: a hand-edited or buggy state paired an answer with a proposal that was never offered; or
    resume reopened a decision without its proposal; or a ledger entry cited a source whose text was
    never archived.
    """
    h = synced(make, COVERED)
    state = full_state(h)
    target = state
    for part in where:
        target = target[part]
    target.update(change)
    h.state_file.write_text(json.dumps(state))
    before = h.state_file.read_bytes()

    result = h.run("show")

    assert (result.returncode, result.stdout) == (1, ""), result.stderr
    unreadable = f"(unknown value {field} = {value!r}); move it aside to start again"
    assert result.stderr == f"ERROR: state file {h.state_file} is unreadable {unreadable}\n", (
        result.stderr
    )
    assert h.state_file.read_bytes() == before


@pytest.mark.parametrize("command", ["sync", "triage", "post", "push", "pause", "resume", "show"])
def test_S1_a_version_2_state_is_refused_by_every_command(make, command: str):
    """S1 (Q5): a version-2 state file is refused, not migrated, and stays as it is: the learning
    signals are required fields, and a version-2 file has none.

    Bug: a version-2 file loaded without items or proposals, so one run mixed
    ledger entries with and without the signals, and the next write added them to an old file.
    """
    h = synced(make, COVERED)
    state = h.state()
    state["version"] = 2
    for container in ("items", "proposals"):
        state.pop(container, None)
    for entry in state["ledger"]:
        entry.pop("proposal", None)
    h.state_file.write_text(json.dumps(state, indent=2) + "\n")
    before = h.state_file.read_bytes()
    options = {
        "triage": ["--file", h.write("t.json", "[]")],
        "post": ["--issue", "--body-file", h.write("b.md", "Summary")],
        "pause": ["--reason", "x"],
        "resume": ["--decision", "go"],
    }.get(command, [])

    error = (
        f"state file {h.state_file} is unreadable (unknown version 2); move it aside to start again"
    )
    h.refused(error, command, *options, cwd=h.checkout())

    assert h.state_file.read_bytes() == before


@pytest.mark.parametrize(
    ("check_runs", "statuses", "expected"),
    [
        (None, 0, "error"),
        (0, None, "error"),
        ({"total_count": 1}, 0, "error"),
        (1, 0, "wait_ci"),
        (0, 1, "wait_ci"),
        ({"total_count": 2, "check_runs": [check_run(COPILOT, "completed")]}, 0, "wait_ci"),
    ],
    ids=[
        "check-runs-call-fails",
        "status-call-fails",
        "no-check-run-list",
        "one-check-run",
        "one-status",
        "a-bot-review-run-and-an-unlisted-run",
    ],
)
def test_T1_no_checks_needs_positive_evidence_from_rest(make, check_runs, statuses, expected: str):
    """T1/T2: when gh pr checks lists nothing, "no checks" holds only if REST reports zero check
    runs and zero commit statuses for the head. A failed REST call or a check-run body without its
    list stops sync. Any other count above zero is pending, also when the only listed run is a bot
    review's: a run that REST counts but does not list may be real CI.

    Bug: done without ever reading CI: both gh pr checks reads failed with empty output, and the
    empty result counted as a head with no checks.
    """
    h = make(COVERED)
    h.set_checks(None, None)
    h.set_rest_checks(check_runs, statuses)

    first = h.run("sync")
    h.advance(120)
    later = h.run("sync")

    if expected == "error":
        for result in (first, later):
            assert (result.returncode, result.stdout) == (1, ""), result.stdout
            assert "ERROR: gh api repos/octo-org/hello-world/commits/" in result.stderr, (
                result.stderr
            )
    else:
        h.noise()
        result = h.ok("sync")
        reason = "checks exist but were not listed"
        assert (result["next"], result["ci"]) == (
            "wait_ci",
            {"state": "pending", "failing": [], "reason": reason},
        )
    assert h.state()["status"] == "running", "never done"


def test_T1_ci_is_not_green_while_a_check_suite_with_runs_is_still_running(make):
    """T1 (D2, #166): CI is green only when every check suite on the head that has check runs has
    completed. A suite without runs does not count: idle apps keep one queued forever.

    Bug (a work monorepo PR, 2026-10-18): gh pr checks --required listed only passing checks while
    the github-actions suite was still running, so done came before a required job had a run.
    """
    h = synced(make, COVERED)
    idle = suite("cursor", "queued", 0)
    h.set_suites([suite("github-actions", "in_progress", 18), idle])

    running = h.ok("sync")
    h.set_suites([suite("github-actions", "completed", 18), idle])
    done = h.ok("sync")

    reason = "check suites still running: github-actions"
    assert (running["next"], running["ci"]) == (
        "wait_ci",
        {"state": "pending", "failing": [], "reason": reason},
    )
    assert (done["next"], done["ci"]["state"]) == ("done", "green"), (
        "the idle app's queued suite does not block done"
    )


@pytest.mark.parametrize(
    "suites",
    [
        {},
        {"raw": "{not json"},
        {
            "raw": json.dumps(
                {"total_count": 1, "check_suites": {"app": {"slug": "github-actions"}}}
            )
        },
        {"raw": json.dumps({"check_suites": []})},
        {"suites": [{"app": {"slug": "github-actions"}, "status": "in_progress"}]},
        {"suites": [suite("github-actions", "completed", 1)], "total": 101},
    ],
    ids=[
        "call-fails",
        "bad-json",
        "suites-not-a-list",
        "no-total-count",
        "no-run-count",
        "more-suites-than-listed",
    ],
)
def test_T1_unreadable_check_suites_are_never_green(make, suites: dict):
    """T1 (D2): when the head's check suites cannot be read in full, CI is pending, never green, and
    T4 bounds the wait.

    Bug: a failed or partial suite read counted as "no running suites", so done passed a head
    whose CI was never confirmed.
    """
    h = synced(make, COVERED)
    h.set_suites(suites.get("suites"), suites.get("total"), suites.get("raw"))

    first = h.ok("sync")
    h.advance(1800)
    last = h.ok("sync")

    head7 = first["head"][:7]
    reason = f"the check suites of {head7} could not be read"
    assert (first["next"], first["ci"]) == (
        "wait_ci",
        {"state": "pending", "failing": [], "reason": reason},
    )
    condition = f"CI still pending on {head7} after 30 minutes"
    assert (last["next"], last["resumeCondition"], h.state()["status"]) == (
        "needs_input",
        condition,
        "needs_input",
    )


def test_S1_state_is_kept_per_owner_repo_and_pr(make):
    """S1: each owner, repo, and PR has its own state directory, and a state file names its owner.

    Bug: two PRs shared one state file: a-b/c#154 and a/b-c#154 both mapped to a-b-c-154.
    """
    h = make(COVERED)
    root = h.isolated.home / ".local" / "state" / "siula" / "review"
    for repo in ("a-b/c", "a/b-c"):
        for name in ("pr_repo", "cwd_repo"):
            (h.ctl / name).write_text(repo)
        assert h.ok("sync", repo=repo)["round"] == 1, f"{repo} starts its own run"

    stored = {
        repo: json.loads((root / repo / "154" / "state.json").read_text())["repo"]
        for repo in ("a-b/c", "a/b-c")
    }
    assert stored == {"a-b/c": "a-b/c", "a/b-c": "a/b-c"}
    wrong = root / "a" / "b-c" / "154" / "state.json"
    shutil.copy(root / "a-b" / "c" / "154" / "state.json", wrong)
    result = h.run("show", repo="a/b-c")
    assert (result.returncode, result.stdout) == (1, "")
    assert (
        f"ERROR: state file {wrong} belongs to a-b/c#154, not a/b-c#154; move it aside"
        in result.stderr
    )


def test_T1_a_fix_superseded_by_no_change_does_not_block_done(make):
    """T1/T4: the unpushed-fix check reads only the latest entry of each claim's sameAs chain.

    Bug: a Fix that a later triage changed to No change kept the run in triage, then stopped it as
    "fix L1 ... was triaged but not pushed", although nothing was left to push.
    """
    h = synced(make, COVERED)
    h.triage([FIX])
    h.triage([{**NOT_FIXED, "sameAs": "L6"}])

    assert h.ok("sync")["next"] == "done"


def test_T1_a_linear_sameAs_chain_reaches_done_through_its_tail(make):
    """T1: Fix, then Fix, then No change on one claim is one chain, and only its tail counts.

    Bug: a branched sameAs chain left an obsolete entry as latest and blocked done.
    """
    h = synced(make, COVERED)
    h.triage([FIX])
    h.triage([{**FIX, "sameAs": "L6"}])
    h.triage([{**NOT_FIXED, "sameAs": "L7"}])

    assert h.ok("sync")["next"] == "done"


def test_triage_refuses_a_sameAs_that_is_not_the_latest_entry_of_its_claim(make):
    """Option A (the owner, 2026-10-14): sameAs must name the tail of its claim's chain, and the
    error names it.

    Bug: a branched sameAs chain left an obsolete entry as latest and blocked done.
    """
    h = synced(make, COVERED)
    h.triage([FIX])
    h.triage([{**FIX, "sameAs": "L6"}])
    h.triage([{**NOT_FIXED, "sameAs": "L7"}])
    before = h.state_file.read_bytes()

    error = (
        "triage entry 0: sameAs L6 is not the latest entry of its claim; use L8; "
        "nothing was recorded"
    )
    h.refused(
        error, "triage", "--file", h.write("triage.json", json.dumps([{**FIX, "sameAs": "L6"}]))
    )

    assert h.state_file.read_bytes() == before, "the ledger is unchanged"


def test_triage_refuses_a_batch_that_branches_a_claim(make):
    """Two entries of one batch cannot continue the same entry; the whole batch is refused.

    Bug: a branched sameAs chain left an obsolete entry as latest and blocked done.
    """
    h = synced(make, COVERED)
    h.triage([FIX])
    before = h.state_file.read_bytes()
    branch = [{**NOT_FIXED, "sameAs": "L6"}, {**FIX, "sameAs": "L6"}]

    error = (
        "triage entry 1: sameAs L6 is already continued by triage entry 0 of this batch; "
        "nothing was recorded"
    )
    h.refused(error, "triage", "--file", h.write("triage.json", json.dumps(branch)))

    assert h.state_file.read_bytes() == before, "neither entry is recorded"


def test_S1_a_stored_branched_ledger_is_refused(make):
    """S1: load refuses a ledger in which two entries name the same entry, so a hand edit cannot
    bring a branch back.

    Bug: a branched sameAs chain left an obsolete entry as latest and blocked done.
    """
    h = synced(make, COVERED)
    state = full_state(h)
    later = [
        {**state["ledger"][0], "id": "L2", "sameAs": "L1"},
        {**state["ledger"][0], "id": "L3", "sameAs": "L1"},
    ]
    state["ledger"] += later
    h.state_file.write_text(json.dumps(state))
    before = h.state_file.read_bytes()

    result = h.run("show")

    assert (result.returncode, result.stdout) == (1, ""), result.stderr
    unreadable = "(unknown value ledger[2].sameAs = 'L1'); move it aside to start again"
    assert result.stderr == f"ERROR: state file {h.state_file} is unreadable {unreadable}\n", (
        result.stderr
    )
    assert h.state_file.read_bytes() == before


def test_push_refuses_a_dirty_worktree(make):
    """push refuses while git status --porcelain is not empty, before any fetch or push.

    Bug: push sent HEAD while the worktree held uncommitted changes, so the pushed head was not the
    code the agent had tested and described in its replies.
    """
    h = make(COVERED)
    work = h.synced_branch()
    h.commit(work, "fix: guard the cursor")
    (work / "notes.txt").write_text("work in progress\n")
    before, state_before, origin_before = h.calls(), h.state_file.read_bytes(), h.origin_head()

    result = h.run("push", cwd=work)

    assert (result.returncode, result.stdout) == (1, "")
    assert (
        "ERROR: the worktree has uncommitted changes; commit or stash them, then push"
        in result.stderr
    )
    assert h.calls() == before, "no fetch or push ran"
    assert (h.origin_head(), h.state_file.read_bytes()) == (origin_before, state_before)


# --- other command behaviour ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("entries", "error"),
    [
        ([{**FIX, "claim": " "}], "entry 0: claim must be a non-empty single line"),
        ([{**FIX, "claim": "one\ntwo"}], "entry 0: claim must be a non-empty single line"),
        ([{**FIX, "claim": "x" * 201}], "entry 0: claim must be a non-empty single line"),
        ([FIX, {**FIX, "class": "major"}], "entry 1: class must be design, edges, or null"),
        ([{**FIX, "disposition": "Won't fix"}], "entry 0: disposition must be one of"),
        (
            [{**FIX, "sources": [6095816171]}],
            "entry 0: sources must be a list of item keys such as issue:123",
        ),
        (
            [{**FIX, "sources": ["comment:6095816171"]}],
            "entry 0: sources must be a list of item keys",
        ),
        ([{**FIX, "sameAs": "L9"}], "entry 0: sameAs must name an existing ledger entry"),
        (
            [{**FIX, "class": None}],
            "entry 0: class null is valid only with No change or Already addressed",
        ),
        ([{**FIX, "class": "design"}], "entry 0: class design needs disposition Needs decision"),
        (
            [DESIGN, FIX],
            "entry 1: this batch has a design entry, so every classed entry must be Needs decision",
        ),
    ],
)
def test_triage_refuses_an_invalid_batch(make, entries: list, error: str):
    """Triage records all entries or none. Bug: a rule is not checked, or valid entries before a bad
    one are kept.
    """
    h = synced(make, COVERED)
    before = h.state()

    h.refused(f"triage {error}", "triage", "--file", h.write("triage.json", json.dumps(entries)))

    assert (h.state()["ledger"], h.state()["status"]) == (before["ledger"], "running")


def test_triage_refuses_a_claim_that_still_sends_grade_and_evidence(make):
    """Claim entries have no grade or evidence (the owner, 2026-10-23). A batch written for the old
    format is refused by field name, and nothing is recorded.

    Bug: triage accepted the old fields and dropped them in silence, so the agent kept spending
    effort on a grade that no one stored.
    """
    h = synced(make, COVERED)
    before = h.state_file.read_bytes()
    grade = {
        "validity": "Valid",
        "intent": "Required",
        "proportion": "Cheap and safe",
        "fit": "Improves this PR",
    }
    old = {**FIX, "grade": grade, "evidence": "round.py:1 reads the cursor before it guards it"}

    h.refused("triage entry 0: unknown fields ['evidence', 'grade']; nothing was recorded",
              "triage", "--file", h.write("triage.json", json.dumps([old])))  # fmt: skip

    assert h.state_file.read_bytes() == before


def test_triage_stops_a_claim_after_two_fixes(make):
    """Ledger ids are global, and a claim fixed twice that returns needs a human.

    Bug: ids restart per batch, or the third Fix for one claim keeps the loop running.
    """
    h = synced(make, COVERED)
    no_change = {
        "claim": "Rename the flag",
        "class": None,
        "disposition": "No change",
        "sources": [],
    }
    first = h.triage([FIX, no_change])
    assert [
        (e["id"], e["head"], e["pushed"], e["replies"])
        for e in first["entries"]
        if e["disposition"] != "Noise"
    ] == [
        ("L6", h.state()["head"], None, None),
        ("L7", h.state()["head"], None, None),
    ]
    assert h.triage([{**FIX, "sameAs": "L6"}])["status"] == "running"

    third = h.triage([{**FIX, "sameAs": "L8"}])

    claims = [entry["id"] for entry in third["entries"] if entry["disposition"] != "Noise"]
    assert claims == ["L6", "L7", "L8", "L9"], "the entry is still recorded"
    assert (third["status"], h.resume_condition()) == (
        "needs_input",
        "claim L6 returned after two fixes",
    )


@pytest.mark.parametrize(
    ("starter", "skipped_because"),
    [
        ({"__typename": "Bot", "login": "chatgpt-codex-connector"}, None),
        ({"__typename": "User", "login": "octocat"}, None),
        ({"__typename": "User", "login": "Octocat"}, None),
        ({"__typename": "User", "login": "alice"}, "started by User alice"),
        (None, "started by a deleted account"),
        ({"__typename": "Mannequin", "login": "ghost-import"}, "started by Mannequin ghost-import"),
    ],
    ids=["bot", "pr-author", "pr-author-other-case", "other-human", "deleted-account", "mannequin"],
)
def test_post_resolve_leaves_another_humans_thread_open(
    make, starter: dict | None, skipped_because: str | None
):
    """--resolve closes only threads that a Bot or the PR author (a User) started; any other starter
    is skipped with a reason. The reply is linked to its ledger entry either way.

    Bugs: a thread opened by a deleted account was resolved (a null author passed the guard); the
    run resolves a thread a human reviewer or an imported Mannequin opened; or it loses which reply
    answered a claim.
    """
    resolved = skipped_because is None
    h = synced(make, WAITING)
    with h.pr_data() as pr:
        pr["author"] = {"login": "octocat"}
        thread = pr["reviewThreads"]["nodes"][0]
        thread["isResolved"] = False
        thread["comments"]["nodes"][0]["author"] = starter
    h.triage([{**NOT_FIXED, "sources": ["thread:4236931467"]}])

    result = h.post("--thread", "4236931467", "--resolve", body="Keeping the current approach.")

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["resolved"] is resolved
    if skipped_because is not None:
        assert skipped_because in output["reason"], output
    with h.pr_data() as pr:
        assert pr["reviewThreads"]["nodes"][0]["isResolved"] is resolved
    assert h.state()["ledger"][-1]["replies"] == {"thread:4236931467": output["commentId"]}


def test_post_issue_works_after_done(make):
    """post --issue may follow done, so the final summary can list the gaps; the status stays
    succeeded.

    Bug: post refuses after done, so the gaps never reach the PR; or the summary restarts the run.
    """
    h = synced(make, COVERED)
    assert h.ok("sync")["next"] == "done"

    result = h.post("--issue", body="Ready for a human merge. Gaps: none.")

    assert result.returncode == 0, result.stderr
    reply_id = json.loads(result.stdout)["commentId"]
    state = h.state()
    assert (state["status"], state["pending"], state["handled"][-1]) == (
        "succeeded",
        None,
        f"issue:{reply_id}",
    )
    h.refused(
        "status is succeeded: run sync to begin a new run",
        "post",
        "--thread",
        "6095816171",
        "--body-file",
        h.write("b.md", "x"),
    )


def test_sync_collector_failure_keeps_the_round_but_records_the_new_head(make):
    """A failed observation changes nothing but the head record. Bug: ids or the round move without
    a snapshot.
    """
    h = synced(make, COVERED)
    before = h.state()
    h.advance(60)
    h.set_head("e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0")
    (h.ctl / "graphql_fail").touch()
    seen_at = h.now

    result = h.run("sync", "--ceiling", "60")

    assert (result.returncode, result.stdout) == (1, "")
    assert "ERROR: the collector failed" in result.stderr
    after = h.state()
    assert [after[key] for key in ("round", "handled", "inbox", "status")] == [
        before[key] for key in ("round", "handled", "inbox", "status")
    ]
    assert after["heads"]["e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0"] == {
        "firstSeen": seen_at,
        "source": "seen",
    }


@pytest.mark.parametrize(
    ("view", "error"),
    [
        ({"state": "MERGED"}, "is merged, not open"),
        ({"isCrossRepository": True}, "is a cross-repository PR"),
    ],
)
def test_sync_refuses_a_closed_or_cross_repository_pr(make, view: dict, error: str):
    """sync creates state only for an open same-repository PR. Bug: the loop starts on a merged or
    fork PR.
    """
    h = make(COVERED)
    h.set_view(**{"state": "OPEN", "isCrossRepository": False, **view})

    result = h.run("sync")

    assert (result.returncode, result.stdout) == (1, "")
    assert f"ERROR: octo-org/hello-world#154 {error}" in result.stderr
    assert not h.state_file.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["sync", "--settle", "soon"],
        ["post", "--issue", "--resolve", "--body-file", "body.md"],
        ["post", "--thread", "11", "--body-file", "empty.md"],
        ["pause"],
        ["start"],
        ["collect"],
        ["check"],
    ],
)
def test_usage_errors_exit_2_before_any_call(make, args: list):
    """Bad options and the removed start/collect/check are usage errors. Bug: they run with a
    default anyway.
    """
    h = make(COVERED)
    h.write("body.md", "text")
    h.write("empty.md", " \n")

    result = h.run(*args)

    assert result.returncode == 2, result.stderr
    assert h.calls() == ([], [], []) and not h.state_file.exists()


@pytest.mark.parametrize("repo", ["../hello-world", "octo-org/..", "./hello-world", "octo-org/."])
def test_S1_a_dot_component_in_the_repository_is_a_usage_error(make, repo: str):
    """S1: the state directory stays under the state root.

    Bug: "../foo" passed the <owner/repo> check, and sync locked a directory outside that root.
    """
    h = make(COVERED)
    home = h.isolated.home
    before = sorted(home.rglob("*"))

    result = h.run("sync", repo=repo)

    assert (result.returncode, result.stdout) == (2, ""), result.stderr
    assert "repository must be <owner/repo>" in result.stderr, result.stderr
    assert h.calls() == ([], [], [])
    assert sorted(home.rglob("*")) == before, "nothing is created or locked"


@pytest.mark.parametrize("case", ["remote-moved", "nothing-to-push"])
def test_push_refuses_unless_it_fast_forwards_the_rounds_head(make, case: str):
    """push only moves origin forward from the round's head.

    Bug: without the guard, push races a commit someone else pushed (the round then judges a head
    it never observed), or records a "pushed" head when nothing was pushed.
    """
    h = make(COVERED)
    work = h.synced_branch()
    base = h.state()["head"]
    error = f"nothing to push: HEAD is the round's head {base[:7]}"
    if case == "remote-moved":
        other = h.root / "other"
        h.isolated.git(
            h.root, "clone", "-q", "--branch", "feature", str(h.root / "origin.git"), str(other)
        )
        h.commit(other, "fix: someone else")
        h.isolated.git(other, "push", "-q", "origin", "feature")
        h.commit(work, "fix: ours")
        error = (
            f"origin/feature is at {h.origin_head()[:7]}, not the round's head {base[:7]}; "
            "run sync first"
        )
    origin_before, state_before = h.origin_head(), h.state_file.read_bytes()

    result = h.run("push", cwd=work)

    assert (result.returncode, result.stdout) == (1, ""), result.stderr
    assert f"ERROR: {error}" in result.stderr
    assert h.origin_head() == origin_before, "no push should run"
    assert h.state_file.read_bytes() == state_before, "a refused push records nothing"


def push_urls(h: Round, work: Path, urls: list[str], target: Path) -> None:
    """Set remote.origin.pushurl to ``urls``, as written. insteadOf sends each URL other than
    GITHUB_URL (which pr_branch already sends to the local origin) to the local bare repository
    ``target``, so no push leaves the machine.
    """
    for url in urls:
        h.isolated.git(work, "config", "--add", "remote.origin.pushurl", url)
        if url != GITHUB_URL:
            h.isolated.git(work, "config", "--add", f"url.{target}.insteadOf", url)


@pytest.mark.parametrize(
    ("urls", "named"),
    [
        (["git@github.com:octo-cat/hello-world.git"], "octo-cat/hello-world"),
        ([GITHUB_URL, "https://github.com/octo-cat/hello-world.git"], "octo-cat/hello-world"),
        (["https://git.example.com/octo-org/hello-world.git"], "no GitHub repository"),
    ],
    ids=["a-git-fork", "one-of-two-push-urls", "not-github"],
)
def test_S6_push_refuses_a_push_url_that_names_another_repository(
    make, urls: list[str], named: str
):
    """S6: git push origin sends to every remote.origin.pushurl, so push refuses unless each one,
    as written, names <owner/repo>. It refuses before any fetch or push and records nothing.

    Bug: push checked only remote.origin.url. With a push URL to a Git fork, git push sent the fix
    there, push recorded the Fix receipt and the new head, and the next sync read the unchanged PR
    head; nothing checked that the fix landed, so the run could reach done.
    """
    h = make(COVERED)
    work = h.synced_branch()
    h.triage([FIX])
    fork = h.root / "fork.git"
    h.isolated.git(h.root, "init", "-q", "--bare", str(fork))
    push_urls(h, work, urls, fork)
    new_head = h.commit(work, "fix: guard the cursor")
    base = h.state()["head"]
    entry = h.state()["ledger"][-1]
    assert (entry["disposition"], entry["head"], entry.get("pushed")) == ("Fix", base, None)
    before, state_before, origin_before = h.calls(), h.state_file.read_bytes(), h.origin_head()

    result = h.run("push", cwd=work)

    error = (
        f"remote.origin.pushurl names {named}, not {REPO}; git push origin would send the fix "
        "there. Correct or remove it, then push again; nothing was pushed"
    )
    assert (result.returncode, result.stdout) == (1, ""), result.stderr
    assert f"ERROR: {error}" in result.stderr, result.stderr
    git_after = h.calls()[2][len(before[2]) :]
    assert not any(" push " in f" {call} " for call in git_after), git_after
    assert h.calls() == before, "no fetch or push ran after the checkout checks"
    assert h.origin_head() == origin_before
    forked = h.isolated.git(fork, "for-each-ref", "--format=%(refname)")
    assert forked == "", "nothing reached the fork"
    state = h.state()
    assert state["ledger"][-1]["id"] == entry["id"]
    assert state["ledger"][-1].get("pushed") is None, "no push receipt"
    assert (state["head"], new_head in state["heads"]) == (base, False), "no new head"
    assert h.state_file.read_bytes() == state_before


@pytest.mark.parametrize(
    "urls",
    [
        [GITHUB_URL],
        ["git@github.com:octo-org/hello-world.git"],
        ["https://github.com/Octo-Org/Hello-World/", GITHUB_URL],
    ],
    ids=["the-same-url", "ssh-form", "two-urls-one-in-other-case"],
)
def test_S6_push_goes_ahead_when_each_push_url_names_the_prs_repository(make, urls: list[str]):
    """S6: a push URL that names <owner/repo> (compared without case) does not change push.

    Bug: a push URL check that compares the URL text, or refuses any push URL, stops a push that
    goes to the PR's own repository.
    """
    h = make(COVERED)
    work = h.synced_branch()
    h.triage([FIX])
    push_urls(h, work, urls, h.root / "origin.git")
    new_head = h.commit(work, "fix: guard the cursor")
    h.advance(60)
    pushed_at = h.now

    assert h.ok("push", cwd=work) == {"head": new_head, "pushedAt": pushed_at}

    assert h.origin_head() == new_head
    state = h.state()
    assert (state["ledger"][-1]["disposition"], state["ledger"][-1].get("pushed")) == (
        "Fix",
        new_head,
    )
    assert state["heads"][new_head] == {"firstSeen": pushed_at, "source": "pushed"}


# --- Reviewer mode: R1 no replies or pushes, R2 who may review, R3 one review per head, R4 approval

MONALISA = {"login": "monalisa"}
FOOTER = "\n\n---\nagent on behalf of octocat\n"
REVIEW_BODY = "Two things to change before merge."
HIGH_FIX = {**FIX, "severity": "high"}
MEDIUM_FIX = {**FIX, "severity": "medium"}
LOW_FIX = {**FIX, "severity": "low"}
BLOCKER_DESIGN = {**DESIGN, "severity": "blocker"}


def reviewing(make: Callable[[str], Round], start: str) -> Round:
    """A reviewer-mode run on monalisa's PR: the first sync returned every item (round 1), and noise
    covered them.
    """
    h = make(start)
    h.set_view(author=MONALISA, state="OPEN", isCrossRepository=False)
    first = h.ok("sync", "--mode", "reviewer")
    assert (first["next"], first["round"]) == ("triage", 1), first
    h.noise()
    return h


def writes(h: Round) -> tuple[list, list, list]:
    """What a run changed outside its state file: gh writes (review and comment posts, thread
    resolves), the comment posts alone, and git."""
    gh, posts, git = h.calls()
    gh_writes = [call for call in gh if '"POST"' in call or "resolveReviewThread" in call]
    return gh_writes, posts, git


def refused_untouched(
    h: Round, error: str, command: str, *options: str, **env: Any
) -> subprocess.CompletedProcess[str]:
    """A command that must refuse with ``error``: empty stdout, the state bytes unchanged, and no
    write after it.
    """
    state_before = h.state_file.read_bytes() if h.state_file.exists() else None
    writes_before = writes(h)
    result = h.run(command, *options, **env)
    assert (result.returncode, result.stdout) == (1, ""), (
        f"{command} should refuse\n{result.stdout}"
    )
    assert f"ERROR: {error}" in result.stderr, (
        f"{command} should explain the refusal\n{result.stderr}"
    )
    assert (h.state_file.read_bytes() if h.state_file.exists() else None) == state_before, (
        "the state is unchanged"
    )
    assert writes(h) == writes_before, "nothing is written after a refusal"
    return result


def send_review(
    h: Round, event: str = "COMMENT", body: str = REVIEW_BODY, **env: Any
) -> subprocess.CompletedProcess[str]:
    return h.run("review", "--event", event, "--body-file", h.write("review.md", body), **env)


def github_reviews(h: Round) -> list:
    listed = h.ctl / "rest_reviews.json"
    return json.loads(listed.read_text()) if listed.exists() else []


def review_ready(make: Callable[[str], Round], *entries: dict) -> Round:
    """A reviewer-mode run with the given triage entries recorded, running, and ready to review."""
    h = reviewing(make, COVERED)
    if entries:
        h.triage(list(entries))
    h.advance(60)
    assert h.ok("sync")["next"] == "review"
    return h


@pytest.mark.parametrize(
    "command",
    [
        ["push"],
        ["post", "--thread", "4237011925"],
        ["post", "--thread", "4237011925", "--resolve"],
        ["post", "--issue"],
    ],
    ids=["push", "thread-reply", "thread-reply-and-resolve", "issue-comment"],
)
def test_R1_reviewer_mode_replies_to_no_thread_and_pushes_no_branch(make, command: list):
    """R1: in reviewer mode the only GitHub write is review. push and post are refused, with nothing
    written.

    Bug: a reviewer-mode run answers the author's thread or pushes to the author's branch, because
    the author-mode writers do not check the mode.
    """
    h = reviewing(make, COVERED)
    options = [
        *command[1:],
        *(["--body-file", h.write("body.md", "Fixed.")] if command[0] == "post" else []),
    ]
    error = (
        "reviewer mode does not push"
        if command[0] == "push"
        else "reviewer mode does not post replies; use review"
    )

    refused_untouched(h, error, command[0], *options)

    state = h.state()
    assert (state["mode"], state["status"], state["pending"]) == ("reviewer", "running", None)
    gh_writes, comment_posts, _ = writes(
        h
    )  # the git log holds the skill-SHA read of the first sync
    assert (gh_writes, comment_posts) == ([], [])


def test_R2_author_mode_is_refused_on_another_persons_pr(make):
    """R2: the default mode is author, and the author must be the viewer; nothing is created on
    refusal.

    Bug: the shepherd pushes fixes and replies on a PR that its viewer does not own.
    """
    h = make(COVERED)
    h.set_view(author=MONALISA, state="OPEN", isCrossRepository=False)

    result = refused_untouched(h, "monalisa owns this PR", "sync")

    assert "use --mode reviewer" in result.stderr
    assert not h.state_file.exists()


def test_R2_reviewer_mode_is_refused_on_the_viewers_own_pr(make):
    """R2: the viewer cannot review their own PR.

    Bug: a reviewer-mode run on the viewer's own PR posts a review that GitHub refuses, or pretends
    to approve it.
    """
    h = make(COVERED)

    refused_untouched(
        h, "reviewer mode needs a PR that octocat does not own", "sync", "--mode", "reviewer"
    )

    assert not h.state_file.exists()


@pytest.mark.parametrize("mode", [[], ["--mode", "reviewer"]], ids=["author", "reviewer"])
def test_R2_an_unreadable_viewer_login_is_refused(make, mode: list):
    """R2: when `gh api user` fails, the authorship check cannot run, so nothing starts.

    Bug: an unreadable login counts as "not the author", or as "the author", and the run starts in
    the wrong mode.
    """
    h = make(COVERED)
    (h.ctl / "viewer_fail").write_text("")

    refused_untouched(h, "cannot read the viewer login", "sync", *mode)

    assert not h.state_file.exists()


@pytest.mark.parametrize(("first", "second"), [("author", "reviewer"), ("reviewer", "author")])
def test_R2_a_state_cannot_change_its_mode(make, first: str, second: str):
    """R2: the mode is fixed when the state is created; an explicit other mode is refused.

    Bug: a switch turns a run that posted replies into one that must not, or the reverse, without a
    new round.
    """
    h = make(COVERED)
    if first == "reviewer":
        h.set_view(author=MONALISA, state="OPEN", isCrossRepository=False)
    h.ok("sync", "--mode", first)

    refused_untouched(
        h, f"state is in {first} mode; cannot switch to {second}", "sync", "--mode", second
    )

    assert h.state()["mode"] == first
    assert h.ok("sync")["round"] == 1, "a sync with no --mode keeps the state's mode"
    assert h.state()["mode"] == first


def test_R2_a_state_without_a_mode_loads_as_author(make):
    """R2/S1: states written before reviewer mode have no mode field; they stay valid author-mode
    states.

    Bug: the closed schema rejects the old field set, so every run in progress breaks.
    """
    h = synced(make, COVERED)
    state = h.state()
    assert "mode" in state, "the state records its mode"
    del state["mode"]
    h.state_file.write_text(json.dumps(state))

    assert h.ok("show")["status"] == "running"
    refused_untouched(
        h, "state is in author mode; cannot switch to reviewer", "sync", "--mode", "reviewer"
    )
    assert h.ok("sync")["next"] == "done"


def test_R2_a_bad_mode_is_a_usage_error(make):
    """R2: --mode takes author or reviewer only; any other value exits 2 before any call."""
    h = make(COVERED)

    result = h.run("sync", "--mode", "owner")

    assert (result.returncode, result.stdout) == (2, ""), result.stderr
    assert h.calls() == ([], [], []) and not h.state_file.exists()


def test_R3_a_review_is_posted_on_the_synced_head_with_the_agent_footer(make):
    """R3: review posts one GitHub review on the head sync recorded, ends it with the agent line,
    and records it.

    Bug: the review lands on a newer head than the one that was examined, or without the line that
    says an agent wrote it on the viewer's behalf, or it is not recorded so the run never reaches
    done.
    """
    h = review_ready(make, MEDIUM_FIX)
    head = h.state()["head"]

    posted = h.ok(
        "review",
        "--event",
        "REQUEST_CHANGES",
        "--body-file",
        h.write("review.md", REVIEW_BODY + "\n"),
    )

    [review] = github_reviews(h)
    assert (review["commit_id"], review["state"], review["body"]) == (
        head,
        "REQUEST_CHANGES",
        REVIEW_BODY + FOOTER,
    )
    assert posted == {"id": review["id"], "event": "REQUEST_CHANGES", "head": head}
    state = h.state()
    assert state["review"] == {"id": review["id"], "event": "REQUEST_CHANGES", "head": head}
    assert state["pending"] is None and f"review:{review['id']}" in state["handled"]
    done = h.ok("sync")
    assert (done["next"], done["newItems"], h.state()["status"], h.state()["target"]) == (
        "done", [], "succeeded", "reviewed",
    )  # fmt: skip
    assert len(github_reviews(h)) == 1


def test_R3_a_review_is_refused_when_the_head_moved_after_the_last_sync(make):
    """R3: review compares the live head with the head sync recorded.

    Bug: the review is posted for code that the agent never read, with commit_id of the new head.
    """
    h = review_ready(make)
    recorded = h.state()["head"]
    h.set_head(NEW_HEAD)

    refused_untouched(
        h, f"the head moved to {NEW_HEAD[:7]} since the last sync ({recorded[:7]})", "review",
        "--event", "COMMENT", "--body-file", h.write("review.md", REVIEW_BODY),
    )  # fmt: skip

    assert github_reviews(h) == []


@pytest.mark.parametrize("earlier", ["by-this-run", "by-the-viewer-on-github"])
def test_R3_a_head_gets_one_review_from_the_viewer(make, earlier: str):
    """R3: a second review by the viewer on the same commit is refused, whether this run or GitHub
    shows it.

    Bug: a repeated `review` call, or a rerun after the state was lost, posts the same verdict
    twice.
    """
    h = review_ready(make)
    head = h.state()["head"]
    if earlier == "by-this-run":
        assert send_review(h).returncode == 0
    else:
        mine = {
            "id": 91,
            "user": {"login": "octocat"},
            "commit_id": head,
            "state": "COMMENT",
            "body": "earlier",
        }
        (h.ctl / "rest_reviews.json").write_text(json.dumps([mine]))
    before = github_reviews(h)

    refused_untouched(h, f"octocat already reviewed {head[:7]}", "review", "--event", "COMMENT",
                      "--body-file", h.write("review.md", "again"))  # fmt: skip

    assert github_reviews(h) == before and len(before) == 1


def test_R3_the_viewers_review_on_a_later_page_still_blocks_a_second_review(make):
    """R3: the duplicate check reads every page of reviews.

    Bug: only the first 100 reviews are read, so a review on page 2 is missed and the verdict posts
    twice.
    """
    h = review_ready(make)
    head = h.state()["head"]
    filler = [
        {"id": n, "user": {"login": "monalisa"}, "commit_id": "0" * 40, "state": "COMMENTED"}
        for n in range(1, 4)
    ]
    mine = {
        "id": 91,
        "user": {"login": "octocat"},
        "commit_id": head,
        "state": "COMMENTED",
        "body": "earlier",
    }
    (h.ctl / "rest_review_pages.json").write_text(json.dumps([filler, [mine]]))

    refused_untouched(h, f"octocat already reviewed {head[:7]}", "review", "--event", "COMMENT",
                      "--body-file", h.write("review.md", "again"))  # fmt: skip

    assert not any('"POST"' in call for call in h.calls()[0]), "no review was posted"


def test_R3_a_reviews_listing_that_is_not_a_list_of_pages_is_refused(make):
    """R3: an unexpected shape from the paginated read refuses. Bug: a non-list value is treated as
    no reviews.
    """
    h = review_ready(make)
    (h.ctl / "rest_review_pages.json").write_text(json.dumps({"message": "oops"}))

    refused_untouched(h, "gh api reviews printed an unexpected value", "review", "--event",
                      "COMMENT", "--body-file", h.write("review.md", "x"))  # fmt: skip


@pytest.mark.parametrize(
    "author", [None, {}, {"login": ""}], ids=["null", "empty-object", "empty-login"]
)
@pytest.mark.parametrize("mode", [[], ["--mode", "reviewer"]], ids=["author", "reviewer"])
def test_R2_an_unreadable_pr_author_is_refused(make, mode: list, author: Any):
    """R2: a PR view without an author login cannot pass the authorship check.

    Bug: in reviewer mode `same_login(None, viewer)` is false, so the run starts on a PR of unknown
    ownership.
    """
    h = make(COVERED)
    (h.ctl / "view.json").write_text(
        json.dumps({"state": "OPEN", "isCrossRepository": False, "author": author})
    )

    refused_untouched(h, "cannot read the PR author", "sync", *mode)

    assert not h.state_file.exists()
    assert h.calls()[1] == [] and not any("pr checks" in call for call in h.calls()[0]), (
        "nothing ran after it"
    )


def test_R3_another_persons_review_of_the_head_does_not_block_the_viewers(make):
    """R3: the duplicate check is per viewer. Bug: any review on the commit blocks, so the reviewer
    never posts.
    """
    h = review_ready(make)
    head = h.state()["head"]
    theirs = {
        "id": 92,
        "user": {"login": "monalisa"},
        "commit_id": head,
        "state": "COMMENT",
        "body": "mine",
    }
    (h.ctl / "rest_reviews.json").write_text(json.dumps([theirs]))

    posted = h.ok("review", "--event", "COMMENT", "--body-file", h.write("review.md", REVIEW_BODY))

    assert [review["user"]["login"] for review in github_reviews(h)] == ["monalisa", "octocat"]
    assert posted["head"] == head


@pytest.mark.parametrize("event", ["approve", "LGTM", ""])
def test_R3_review_accepts_only_the_three_review_events(make, event: str):
    """R3: --event is APPROVE, COMMENT, or REQUEST_CHANGES.

    Anything else exits 2 before any call.
    """
    h = review_ready(make)
    before = writes(h)

    result = h.run("review", "--event", event, "--body-file", h.write("review.md", REVIEW_BODY))

    assert (result.returncode, result.stdout) == (2, ""), result.stderr
    assert writes(h) == before and github_reviews(h) == []


def test_R3_an_empty_review_body_is_a_usage_error(make):
    """R3: the footer alone is not a review. Bug: an empty body posts only the agent line."""
    h = review_ready(make)

    result = h.run("review", "--event", "COMMENT", "--body-file", h.write("review.md", " \n"))

    assert (result.returncode, result.stdout) == (2, ""), result.stderr
    assert github_reviews(h) == []


def test_R3_a_review_is_refused_in_author_mode(make):
    """R3: review belongs to reviewer mode. Bug: an author-mode run reviews its own PR, which GitHub
    refuses late.
    """
    h = synced(make, COVERED)

    refused_untouched(h, "review needs reviewer mode", "review", "--event", "COMMENT",
                      "--body-file", h.write("review.md", REVIEW_BODY))  # fmt: skip

    assert github_reviews(h) == []


@pytest.mark.parametrize("failure", ["review_fail", "review_postfail"])
def test_R3_S4_an_unknown_review_result_needs_input_and_resume_reconciles_it(make, failure: str):
    """R3/S4: pending holds a hash, never the body; an unknown result stops the run; after resume,
    sync looks for the viewer's review of that head on GitHub, so nothing is posted twice and a lost
    post is not assumed.

    Bug: a post that reached GitHub is posted again after resume, or a post that failed is taken as
    done.
    """
    posted = failure == "review_postfail"
    h = review_ready(make)
    head = h.state()["head"]
    (h.ctl / failure).write_text("")

    result = send_review(h, body="Please add a test for the empty cursor.")

    assert (result.returncode, result.stdout) == (1, ""), result.stderr
    assert "the post result is unknown; check the PR, then resume" in result.stderr
    pending = h.state()["pending"]
    assert (pending["kind"], pending["target"]) == ("review", head)
    assert "empty cursor" not in h.state_file.read_text(), "pending keeps a hash, not the body"
    assert h.quiet_sync()["next"] == "needs_input"
    h.ok("resume", "--decision", "checked the PR")
    refused_untouched(
        h,
        "a review of",
        "review",
        "--event",
        "COMMENT",
        "--body-file",
        h.write("review.md", "again"),
    )

    reconciled = h.ok("sync")

    [kept] = github_reviews(h) if posted else [None]
    key = f"review:{kept['id']}" if posted else None
    assert reconciled["reconciled"] == {
        "kind": "review", "target": head, "posted": posted, "key": key,
        "matches": 1 if posted else 0,
    }  # fmt: skip
    assert h.state()["pending"] is None and reconciled["newItems"] == []
    assert (h.state()["review"] is not None) is posted
    assert len(github_reviews(h)) == (1 if posted else 0), "nothing is posted twice"
    assert reconciled["next"] == ("done" if posted else "review")


@pytest.mark.parametrize(
    "blocker", ["ci-failing", "no-checks", "inbox", "needs-decision", "fix-high", "fix-blocker"]
)
def test_R4_approve_is_refused_until_every_condition_holds(make, blocker: str):
    """R4: APPROVE needs green CI on the head, an empty inbox, no open decision, and no blocker or
    high Fix. The refusal names each blocking ledger ID or inbox key, and nothing is posted.

    Bug: the agent approves a head with red or missing CI, with feedback it never triaged, or with a
    claim it rated blocker or high; or it hides which entry blocks.
    """
    h = review_ready(
        make,
        *(
            [HIGH_FIX]
            if blocker == "fix-high"
            else [{**FIX, "severity": "blocker"}]
            if blocker == "fix-blocker"
            else []
        ),
    )
    names = ["L6"] if blocker in ("fix-high", "fix-blocker") else []
    if blocker == "ci-failing":
        h.set_checks([{"name": "checks", "bucket": "fail"}])
    elif blocker == "no-checks":
        h.set_checks(None)
    elif blocker == "inbox":
        late = h.add_comment("P1: the cursor skips a comment.")
        assert [item["key"] for item in h.ok("sync")["newItems"]] == [f"issue:{late}"]
        names = [f"issue:{late}"]
    elif blocker == "needs-decision":
        h.triage([BLOCKER_DESIGN, PROPOSAL])
        h.advance(60)
        assert h.ok("sync")["next"] == "needs_input"
        h.ok("resume", "--decision", "keep the cursor as it is")
        names = ["L6"]

    result = refused_untouched(h, "cannot approve", "review", "--event", "APPROVE",
                               "--body-file", h.write("review.md", REVIEW_BODY))  # fmt: skip

    assert all(name in result.stderr for name in names), result.stderr
    if blocker in ("ci-failing", "no-checks"):
        assert "CI is not green" in result.stderr
    assert github_reviews(h) == []


def test_R4_approve_is_refused_when_the_head_moves_during_the_ci_read(make):
    """R4: the approval CI read can see a head pushed after the last sync. The review refuses and
    posts nothing.

    Bug: APPROVE pins the old commit but uses the new head's green checks, so an unreviewed commit
    is approved.
    """
    h = review_ready(make)
    old_head = h.state()["head"]
    (h.ctl / "move_head_on_checks").write_text(NEW_HEAD)

    refused_untouched(
        h, f"the head moved to {NEW_HEAD[:7]} during the CI read ({old_head[:7]})", "review",
        "--event", "APPROVE", "--body-file", h.write("review.md", REVIEW_BODY),
    )  # fmt: skip

    assert github_reviews(h) == []


@pytest.mark.parametrize("fix", [MEDIUM_FIX, LOW_FIX], ids=["medium", "low"])
def test_R4_a_medium_or_low_fix_does_not_block_approval(make, fix: dict):
    """R4: only blocker and high Fix entries block APPROVE. Bug: any Fix blocks, so a nit stops
    every approval.
    """
    h = review_ready(make, fix)

    posted = h.ok(
        "review", "--event", "APPROVE", "--body-file", h.write("review.md", "Approved with a note.")
    )

    assert [review["state"] for review in github_reviews(h)] == ["APPROVE"]
    assert posted["event"] == "APPROVE"


def test_R4_approval_follows_the_sameAs_chain_to_the_latest_claim(make):
    """R4: a high Fix that a later entry of its chain lowers to medium, or closes, no longer blocks.

    Bug: the check reads every ledger entry, so a claim that the reviewer already settled blocks
    forever.
    """
    h = review_ready(make, HIGH_FIX)
    refused_untouched(
        h,
        "cannot approve",
        "review",
        "--event",
        "APPROVE",
        "--body-file",
        h.write("review.md", "ok"),
    )
    h.triage([{**MEDIUM_FIX, "sameAs": "L6"}])
    h.advance(60)
    assert h.ok("sync")["next"] == "review"

    h.ok(
        "review", "--event", "APPROVE", "--body-file", h.write("review.md", "Approved with a note.")
    )

    assert [review["state"] for review in github_reviews(h)] == ["APPROVE"]


@pytest.mark.parametrize("event", ["COMMENT", "REQUEST_CHANGES"])
def test_R4_other_events_are_not_gated_by_ci_or_the_ledger(make, event: str):
    """R4: only APPROVE is gated.

    Bug: a reviewer cannot request changes on a head whose CI is red.
    """
    h = review_ready(make, HIGH_FIX)
    h.set_checks([{"name": "checks", "bucket": "fail"}])

    h.ok("review", "--event", event, "--body-file", h.write("review.md", REVIEW_BODY))

    assert [review["state"] for review in github_reviews(h)] == [event]


def changed(path: str, status: str = "modified", **fields: Any) -> dict:
    """One entry of the PR files list, as GitHub reports it."""
    return {"filename": path, "status": status, "changes": 1, **fields}


def approve(h: Round) -> subprocess.CompletedProcess[str]:
    return send_review(h, "APPROVE", "Looks right.")


def refused_approval(h: Round, *needles: str) -> None:
    """APPROVE refuses as a gate.

    Checks the exit code, every needle in the error, and no review on GitHub.
    """
    result = approve(h)
    assert (result.returncode, result.stdout) == (1, ""), result.stdout
    assert "cannot approve" in result.stderr, result.stderr
    assert all(needle in result.stderr for needle in needles), result.stderr
    assert github_reviews(h) == [] and writes(h)[0] == [], "nothing is posted after a refusal"


def test_R4_a_removed_test_file_refuses_approval_but_not_a_comment(make):
    """R4: a PR that deletes a test file leaves CI green, because the test no longer runs.

    Bug: approval checks only CI, so a PR that deletes the test that guards a behaviour gets
    approved.
    """
    h = review_ready(make)
    h.set_files([changed("tests/test_x.py", "removed"), changed("src/x.py")])

    refused_approval(h, "tests/test_x.py is a removed test file")
    h.ok("review", "--event", "COMMENT", "--body-file", h.write("review.md", REVIEW_BODY))
    assert [review["state"] for review in github_reviews(h)] == ["COMMENT"]


def test_R4_a_renamed_test_file_refuses_approval(make):
    """R4: renaming a test file out of the test paths hides it from the runner.

    Bug: only status removed is checked, so tests/test_x.py renamed to notes/x.txt passes.
    """
    h = review_ready(make)
    h.set_files([changed("notes/x.txt", "renamed", previous_filename="tests/test_x.py", changes=0)])

    refused_approval(h, "tests/test_x.py is a renamed test file")


def test_R4_a_removed_non_test_file_does_not_block_approval(make):
    """R4: only test files count. Bug: any removed file blocks approval."""
    h = review_ready(make)
    pure_rename = changed("notes/a.txt", "renamed", previous_filename="notes/b.txt", changes=0)
    h.set_files([changed("src/old.py", "removed"), pure_rename])

    h.ok("review", "--event", "APPROVE", "--body-file", h.write("review.md", "Looks right."))

    assert [review["state"] for review in github_reviews(h)] == ["APPROVE"]


@pytest.mark.parametrize(
    ("path", "added"),
    [
        ("tests/test_x.py", "+@pytest.mark.skip\n+def test_a():"),
        ("web/x.test.ts", "+  it.only('a', () => {})"),
    ],
    ids=["pytest-skip", "it-only"],
)
def test_R4_a_patch_that_adds_a_skip_marker_refuses_approval(make, path: str, added: str):
    """R4: a new skip or only marker disables tests while CI stays green.

    Bug: approval ignores the diff, so a PR that skips the failing test gets approved.
    """
    h = review_ready(make)
    h.set_files([changed(path, patch=f"@@ -1,2 +1,3 @@\n context\n{added}")])

    refused_approval(h, f"{path} adds 1 test marker", "removes 0")


def test_R4_a_patch_that_adds_a_second_marker_to_a_line_that_has_one_refuses_approval(make):
    """R4: markers are counted as matches, as siula.review.test_integrity counts them. Bug: counting
    lines misses the second marker.
    """
    h = review_ready(make)
    patch = (
        '@@ -1,2 +1,2 @@\n-@pytest.mark.skip\n+@pytest.mark.skip; pytest.skip("x")\n def test_a():'
    )
    h.set_files([changed("tests/test_x.py", patch=patch)])

    refused_approval(h, "tests/test_x.py adds 2 test marker", "removes 1")


def test_R4_a_patch_that_moves_a_marker_does_not_block_approval(make):
    """R4: a marker that moves is not a new marker. Bug: any added marker line blocks, even a moved
    one.
    """
    h = review_ready(make)
    patch = (
        "@@ -1,3 +1,3 @@\n-@pytest.mark.skip\n def test_a():\n+@pytest.mark.skip\n def test_b():"
    )
    h.set_files([changed("tests/test_x.py", patch=patch)])

    h.ok("review", "--event", "APPROVE", "--body-file", h.write("review.md", "Looks right."))

    assert [review["state"] for review in github_reviews(h)] == ["APPROVE"]


def test_R4_a_file_without_a_patch_is_compared_at_the_merge_base_and_the_head(make):
    """R4: GitHub omits the patch of a large or binary diff. The marker counts at the two commits
    decide.

    Bug: a file with no patch counts as zero markers, so a huge test file that gains a skip passes.
    """
    h = review_ready(make)
    head = h.state()["head"]
    h.set_files([changed("tests/test_big.py")])
    h.set_content("m" * 40, "tests/test_big.py", "def test_a():\n    pass\n")
    h.set_content(head, "tests/test_big.py", "@pytest.mark.skip\ndef test_a():\n    pass\n")

    refused_approval(h, "tests/test_big.py has 1 test markers at the head, 0 at the merge base")


def test_R4_a_file_without_a_patch_and_equal_counts_does_not_block_approval(make):
    """R4: equal counts at the base and head are not new markers. A file new at the head has a base
    count of 0.
    """
    h = review_ready(make)
    head = h.state()["head"]
    h.set_files([changed("tests/test_big.py"), changed("tests/test_new.py", "added")])
    for ref in ("m" * 40, head):
        h.set_content(ref, "tests/test_big.py", "@pytest.mark.skip\ndef test_a():\n    pass\n")
    h.set_content(head, "tests/test_new.py", "def test_n():\n    pass\n")

    h.ok("review", "--event", "APPROVE", "--body-file", h.write("review.md", "Looks right."))

    assert [review["state"] for review in github_reviews(h)] == ["APPROVE"]


def test_R4_a_file_without_a_patch_that_cannot_be_read_refuses_approval(make):
    """R4: no evidence is not a pass. Bug: a failed contents read counts as zero markers and
    approves.
    """
    h = review_ready(make)
    h.set_files([changed("tests/test_big.py")])
    (h.ctl / "contents_fail").write_text("")

    refused_approval(h, "tests/test_big.py")


def test_R4_a_file_list_shorter_than_the_changed_file_count_refuses_approval(make):
    """R4: GitHub lists at most 3000 files.

    Bug: the files that were not listed are taken as clean.
    """
    h = review_ready(make)
    h.set_files([changed("src/x.py")])
    h.set_view(author=MONALISA, state="OPEN", isCrossRepository=False, changedFiles=3200)

    refused_approval(h, "1 of 3200 changed files")


def test_R4_a_failed_file_list_read_refuses_approval(make):
    """R4: an unreadable file list is a refusal. Bug: the failed read is taken as an empty PR."""
    h = review_ready(make)
    (h.ctl / "files_fail").write_text("")

    refused_approval(h, "cannot read the changed files")


# --- Reviewer mode: findings and severity ---------------------------------------------------------


def finding(**fields: Any) -> dict:
    return {
        "severity": "high",
        "file": "src/siula/review/round.py",
        "line": 12,
        "body": "Guard the cursor.",
        **fields,
    }


def test_findings_adds_numbered_inbox_items_that_triage_must_cover(make):
    """Findings: the reviewer's own findings enter the inbox as finding:<n>, and triage covers them
    like any item.

    Bug: findings live outside the ledger, so a run reaches review with a finding no triage entry
    decided, or numbering restarts and two findings share a key.
    """
    h = reviewing(make, COVERED)
    file = h.write(
        "findings.json", json.dumps([finding(), finding(severity="low", line=None, body="Nit.")])
    )

    first = h.ok("findings", "--file", file)
    second = h.ok(
        "findings", "--file", h.write("more.json", json.dumps([finding(body="Another.")]))
    )

    assert first == {"added": ["finding:1", "finding:2"], "next": "triage"}
    assert second["added"] == ["finding:3"]
    inbox = {item["key"]: item for item in h.state()["inbox"]}
    assert sorted(inbox) == ["finding:1", "finding:2", "finding:3"]
    assert {item["kind"] for item in inbox.values()} == {"finding"}
    claim = {**HIGH_FIX, "sources": ["finding:1"]}
    h.refused("triage leaves inbox keys uncovered: ['finding:2', 'finding:3']", "triage", "--file",
              h.write("t.json", json.dumps([claim])))  # fmt: skip
    h.triage([claim, *noise_for(["finding:2", "finding:3"], reason="a duplicate of finding 1")])
    state = h.state()
    assert state["inbox"] == [] and {"finding:1", "finding:2", "finding:3"} <= set(state["handled"])
    assert [
        (entry["id"], entry["severity"])
        for entry in state["ledger"]
        if entry["disposition"] == "Fix"
    ] == [("L6", "high")]


def test_S2_reviewer_mode_triage_archives_a_cited_finding_and_the_state_loads_again(make):
    """S2 (Q1/Q4): in reviewer mode a finding is an inbox item like a comment, so triage archives it
    in items, cut to 8,000 characters, and a later claim may cite it again by sameAs. The state then
    loads again.

    Bug: the items schema accepted only collector and CI items, so the first triage that cited a
    finding refused to write (items.finding:1.kind = 'finding') and the reviewer-mode run could not
    continue.
    """
    h = reviewing(make, COVERED)
    h.ok(
        "findings", "--file", h.write("findings.json", json.dumps([finding(body="x" * 8999 + "y")]))
    )
    [item] = h.state()["inbox"]

    h.triage([{**HIGH_FIX, "sources": ["finding:1"]}])
    h.triage([{**HIGH_FIX, "sameAs": "L6", "sources": ["finding:1"]}])

    shown = h.ok("show")
    assert shown["items"]["finding:1"] == {**item, "body": "x" * 8000, "truncated": True}
    assert [entry["sources"] for entry in shown["ledger"][-2:]] == [["finding:1"], ["finding:1"]]


@pytest.mark.parametrize(
    ("case", "entries", "error"),
    [
        (
            "bad-severity",
            [finding(severity="urgent")],
            "finding 0: severity must be one of blocker, high, medium, low",
        ),
        ("string-line", [finding(line="12")], "finding 0: line must be an integer or null"),
        (
            "missing-file",
            [{k: v for k, v in finding().items() if k != "file"}],
            "finding 0: file is required",
        ),
        ("missing-body", [finding(body=" ")], "finding 0: body is required"),
        ("not-a-list", {"findings": []}, "findings must be a JSON list"),
    ],
)
def test_findings_refuses_a_malformed_finding_and_records_nothing(
    make, case: str, entries: Any, error: str
):
    """Findings: a malformed entry refuses the whole file. Bug: valid entries are kept, or a bad
    severity is stored.
    """
    h = reviewing(make, COVERED)

    refused_untouched(h, error, "findings", "--file", h.write("findings.json", json.dumps(entries)))

    assert h.state()["inbox"] == [] and h.state()["mode"] == "reviewer"


@pytest.mark.parametrize("case", ["author-mode", "head-moved", "paused"])
def test_findings_refuses_outside_a_running_reviewer_round_on_the_current_head(make, case: str):
    """Findings: they belong to reviewer mode, the head sync recorded, and a running status.

    Bug: findings about an old head enter the inbox of a newer head, or an author-mode run grows
    reviewer items.
    """
    if case == "author-mode":
        h = synced(make, COVERED)
        error = "findings need reviewer mode"
    else:
        h = reviewing(make, COVERED)
        error = "the head moved" if case == "head-moved" else "the round is paused"
        if case == "head-moved":
            h.set_head(NEW_HEAD)
        else:
            h.ok("pause", "--reason", "waiting for the owner")

    refused_untouched(
        h, error, "findings", "--file", h.write("findings.json", json.dumps([finding()]))
    )

    assert h.state()["inbox"] == []


@pytest.mark.parametrize("disposition", [FIX, DESIGN], ids=["fix", "needs-decision"])
def test_severity_is_required_for_fix_and_needs_decision_in_reviewer_mode(make, disposition: dict):
    """Severity: in reviewer mode a Fix or Needs decision claim carries a severity, or triage is
    refused.

    Bug: an unrated claim reaches the review, so APPROVE cannot tell a nit from a blocker.
    """
    h = reviewing(make, COVERED)

    error = (
        "triage entry 0: reviewer mode needs a severity (blocker, high, medium, low) "
        f"for {disposition['disposition']}"
    )
    refused_untouched(h, error, "triage", "--file", h.write("t.json", json.dumps([disposition])))

    assert len(h.state()["ledger"]) == 5, "only the five noise entries exist"


def test_severity_must_be_one_of_the_four_levels(make):
    """Severity: any other value is refused. Bug: a free-text severity is stored and APPROVE never
    matches it.
    """
    h = reviewing(make, COVERED)

    refused_untouched(
        h, "triage entry 0: severity must be one of blocker, high, medium, low", "triage",
        "--file", h.write("t.json", json.dumps([{**FIX, "severity": "urgent"}])),
    )  # fmt: skip


def test_severity_is_stored_in_the_ledger_and_noise_has_none(make):
    """Severity: the ledger keeps the severity of a claim, and noise has none.

    Bug: the field is accepted and dropped, so the APPROVE gate reads nothing.
    """
    h = reviewing(make, COVERED)

    h.triage([HIGH_FIX])

    ledger = {entry["id"]: entry for entry in h.state()["ledger"]}
    assert ledger["L6"]["severity"] == "high" and ledger["L6"]["disposition"] == "Fix"
    assert {entry["severity"] for key, entry in ledger.items() if key != "L6"} == {None}


def test_author_mode_triage_still_loads_and_accepts_a_state_without_severity(make):
    """Severity: author mode needs no severity, and ledger entries without the field (older states)
    load.

    Bug: the closed schema rejects old ledgers, or author mode starts to demand a severity.
    """
    h = synced(make, COVERED)
    state = h.state()
    for entry in state["ledger"]:
        entry.pop("severity", None)
    h.state_file.write_text(json.dumps(state))

    h.triage([FIX])

    assert h.ok("show")["status"] == "running"
    assert {entry["id"]: entry["disposition"] for entry in h.state()["ledger"]}["L6"] == "Fix"


# --- Reviewer mode: what sync says next -----------------------------------------------------------


def test_reviewer_mode_goes_from_triage_to_review_to_done_without_a_push(make):
    """Flow: after triage and green CI on a covered head, next is review (an unpushed Fix does not
    stop it), and done (target reviewed) comes only after the review exists on the current head.

    Bug: reviewer mode asks for a push or a thread reply that it must not do, or finishes before it
    reviewed.
    """
    h = reviewing(make, COVERED)
    h.triage([HIGH_FIX])
    h.advance(60)

    ready = h.ok("sync")
    assert (ready["next"], h.state()["status"]) == ("review", "running")
    assert send_review(h, "REQUEST_CHANGES").returncode == 0
    done = h.ok("sync")

    assert (done["next"], h.state()["target"], h.state()["status"]) == (
        "done",
        "reviewed",
        "succeeded",
    )
    gh_writes, comment_posts, _ = writes(h)
    assert len(gh_writes) == 1 and "reviews" in gh_writes[0], "the review is the only GitHub write"
    assert comment_posts == [], "no comment post"


def test_reviewer_mode_never_retriggers_and_times_out_with_a_gap(make):
    """Flow: a silent bot reviewer is waited for, never re-triggered, and the 1200 s timeout is a
    gap.

    Bug: reviewer mode posts "@codex review" on the PR, or waits for the bot forever.
    """
    h = reviewing(make, WAITING)
    head = h.state()["head"]

    waiting = h.ok("sync", "--ceiling", "120")
    h.advance(600)
    still = h.ok("sync", "--ceiling", "120")
    h.advance(660)
    timed_out = h.ok("sync", "--ceiling", "120")

    assert (waiting["next"], still["next"]) == ("wait_reviewer", "wait_reviewer")
    assert (timed_out["next"], timed_out["gaps"]) == (
        "review",
        [f"{CODEX} did not review {head[:7]}"],
    )
    assert writes(h)[:2] == ([], []), "nothing is posted to ask the bot again"


def test_reviewer_mode_waits_for_ci_before_review(make):
    """Flow: pending CI gives wait_ci, and the review comes after it. Bug: review is offered on
    unfinished CI.
    """
    h = reviewing(make, COVERED)
    h.set_checks([{"name": "checks", "bucket": "pending"}])
    h.advance(60)

    assert h.ok("sync", "--ceiling", "60")["next"] == "wait_ci"


def test_T4_reviewer_mode_a_review_that_is_never_posted_stops_the_run(make):
    """T4: the same due review on the same head with no new input asks for a decision.

    Bug: an endless loop; every sync returns review while the agent never posts it.
    """
    h = review_ready(make)
    h.advance(60)

    second = h.ok("sync", "--ceiling", "60")

    head = h.state()["head"]
    condition = f"the review of {head[:7]} was due but not posted; post it with review"
    assert (second["next"], second["resumeCondition"], h.state()["status"]) == (
        "needs_input",
        condition,
        "needs_input",
    )


def test_a_head_moved_after_the_review_starts_a_new_round(make):
    """Flow: a push after the review means the review is for an old head, so the run is not done.

    Bug: done stays true for a head that the review never covered.
    """
    h = review_ready(make)
    assert send_review(h).returncode == 0
    assert h.ok("sync")["next"] == "done"
    reviewed = h.state()["review"]["head"]
    h.advance(60)
    h.set_head(NEW_HEAD)

    moved = h.ok("sync")

    assert moved["head"] == NEW_HEAD
    assert (moved["next"], h.state()["status"]) != ("done", "succeeded")
    assert h.state()["review"]["head"] == reviewed != NEW_HEAD
