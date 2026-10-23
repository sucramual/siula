import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from siula.review.poster import ATTRIBUTION, FOOTER, sign, unsign

REPO = "octo-org/hello.world"

# A stand-in for the GitHub CLI. It parses its arguments as `gh api` does, so the tests read the
# method, path, and request body of each call, whatever the flag order. Environment variables:
#   FAKE_GH_LOG          file that gets one JSON line per call
#   FAKE_GH_LOGIN        login that `gh api user --jq .login` prints (default: octocat)
#   FAKE_GH_USER_EXIT    non-zero exit code for `gh api user`
#   FAKE_GH_POST_EXIT    exit code for a POST (default: 0)
#   FAKE_GH_POST_STDOUT  raw stdout for a POST, in place of the response built from the path
FAKE_GH = r"""
import argparse
import json
import os
import re
import sys

parser = argparse.ArgumentParser(prog="gh", add_help=False)
parser.add_argument("command")
parser.add_argument("path")
parser.add_argument("-X", "--method", default="GET")
parser.add_argument("--input")
parser.add_argument("-q", "--jq")
args = parser.parse_args()
stdin = sys.stdin.read() if args.input == "-" else None
method = args.method.upper()
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    record = {"argv": sys.argv[1:], "stdin": stdin, "method": method, "path": args.path}
    log.write(json.dumps(record) + "\n")

if args.command != "api":
    sys.exit(f"fake gh: unexpected command {args.command}")
if method == "GET" and args.path == "user":
    if int(os.environ.get("FAKE_GH_USER_EXIT", "0")):
        print("gh: Bad credentials (HTTP 401)", file=sys.stderr)
        sys.exit(int(os.environ["FAKE_GH_USER_EXIT"]))
    if args.jq != ".login":
        sys.exit("fake gh: expected --jq .login")
    print(os.environ.get("FAKE_GH_LOGIN", "octocat"))
    sys.exit(0)
if method != "POST":
    sys.exit(f"fake gh: unexpected {method} {args.path}")

code = int(os.environ.get("FAKE_GH_POST_EXIT", "0"))
if "FAKE_GH_POST_STDOUT" in os.environ:
    sys.stdout.write(os.environ["FAKE_GH_POST_STDOUT"])
    if code:
        print("gh: Validation Failed (HTTP 422)", file=sys.stderr)
    sys.exit(code)
thread = re.fullmatch(r"repos/([^/]+/[^/]+)/pulls/(\d+)/comments/\d+/replies", args.path)
issue = re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)/comments", args.path)
if code or not (thread or issue):
    print(json.dumps({"message": "Not Found", "status": "404"}))
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(code or 1)
match = thread or issue
repo, pr = match.group(1), match.group(2)
anchor = "discussion_r9001" if thread else "issuecomment-9001"
print(json.dumps({"id": 9001, "html_url": f"https://github.com/{repo}/pull/{pr}#{anchor}"}))
"""


@dataclass
class FakeGh:
    log: Path
    env: dict[str, str]

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def requests(self) -> list[tuple[str, str]]:
        return [(call["method"], call["path"]) for call in self.calls()]

    def posts(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path in self.requests() if method == "POST"]

    def posted_bodies(self) -> list[Any]:
        return [json.loads(call["stdin"]) for call in self.calls() if call["method"] == "POST"]


@pytest.fixture
def gh(tmp_path: Path) -> FakeGh:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "gh-calls.jsonl"
    # The fake is the only PATH entry. When an exec fails, subprocess tries the next PATH entry,
    # so a real gh later in PATH could post to GitHub.
    return FakeGh(log=log, env={**os.environ, "PATH": str(bin_dir), "FAKE_GH_LOG": str(log)})


def run_cli(
    gh: FakeGh, *args: str, stdin: str | None = None, **env: str
) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).parent / "siula"
    stream: dict[str, Any] = (
        {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    )
    return subprocess.run(
        [str(executable), "post-reply", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**gh.env, **env},
        check=False,
        **stream,
    )


def body_file(tmp_path: Path, body: str = "Fixed in abc1234.") -> str:
    path = tmp_path / "body.md"
    path.write_text(body, encoding="utf-8", newline="")
    return str(path)


def thread_args(tmp_path: Path, body: str = "Fixed in abc1234.") -> list[str]:
    return [
        "--repo",
        REPO,
        "--pr",
        "12",
        "--thread",
        "345",
        "--body-file",
        body_file(tmp_path, body),
    ]


def error_of(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert "Traceback" not in result.stderr
    error: dict[str, Any] = json.loads(result.stderr)
    return error


# Footer contract: a collector finds the agent's own comments by these exact bytes.


def test_footer_contract_sign_is_byte_exact():
    assert sign("Fixed in abc1234.", "octocat") == (
        "Fixed in abc1234.\n\n---\nagent on behalf of octocat\n"
    )
    assert ATTRIBUTION == "agent on behalf of"


def test_footer_contract_cli_posts_the_exact_signed_body(gh: FakeGh, tmp_path: Path):
    result = run_cli(gh, *thread_args(tmp_path))

    assert result.returncode == 0, result.stderr
    assert gh.posted_bodies() == [
        {"body": "Fixed in abc1234.\n\n---\nagent on behalf of octocat\n"}
    ]


BODIES = [
    "Fixed in abc1234.",
    "Done.\n",
    "  Indented, with trailing spaces.  ",
    "Line one\r\nline two\r\n",
    "Quoting another reply:\n\n---\nagent on behalf of someone\nand more text after it.",
    "Ends with a footer by someone else.\n\n---\nagent on behalf of someone\n",
]


@pytest.mark.parametrize("body", BODIES)
def test_sign_appends_the_footer_to_the_body_as_given(body: str):
    assert sign(body, "octocat") == body + "\n\n---\nagent on behalf of octocat\n"


@pytest.mark.parametrize("body", BODIES)
def test_unsign_removes_exactly_the_footer_that_sign_added(body: str):
    signed = sign(body, "octocat")
    assert FOOTER.search(signed) is not None
    assert unsign(signed) == body


@pytest.mark.parametrize("body", BODIES)
def test_sign_is_idempotent(body: str):
    signed = sign(body, "octocat")
    assert sign(signed, "octocat") == signed


def test_sign_leaves_a_body_that_ends_with_its_footer_without_a_newline():
    body = "Fixed.\n\n---\nagent on behalf of octocat"
    assert sign(body, "octocat") == body


# Posting


def test_thread_reply_posts_to_the_review_comment_replies_path(gh: FakeGh, tmp_path: Path):
    result = run_cli(gh, *thread_args(tmp_path))

    assert result.returncode == 0, result.stderr
    assert gh.posts() == [("POST", f"repos/{REPO}/pulls/12/comments/345/replies")]
    response = json.loads(result.stdout)
    assert response["id"] == 9001
    assert response["html_url"] == f"https://github.com/{REPO}/pull/12#discussion_r9001"


def test_issue_comment_posts_to_the_issue_comments_path(gh: FakeGh, tmp_path: Path):
    args = ["--repo", REPO, "--pr", "12", "--issue", "--body-file", body_file(tmp_path, "Thanks.")]

    result = run_cli(gh, *args)

    assert result.returncode == 0, result.stderr
    assert gh.posts() == [("POST", f"repos/{REPO}/issues/12/comments")]
    assert gh.posted_bodies() == [{"body": "Thanks.\n\n---\nagent on behalf of octocat\n"}]
    assert (
        json.loads(result.stdout)["html_url"]
        == f"https://github.com/{REPO}/pull/12#issuecomment-9001"
    )


def test_body_file_dash_reads_standard_input(gh: FakeGh):
    args = ["--repo", REPO, "--pr", "12", "--thread", "345", "--body-file", "-"]

    result = run_cli(gh, *args, stdin="From stdin.\n")

    assert result.returncode == 0, result.stderr
    assert gh.posted_bodies() == [{"body": "From stdin.\n\n\n---\nagent on behalf of octocat\n"}]


def test_handle_comes_from_the_authenticated_user(gh: FakeGh, tmp_path: Path):
    result = run_cli(gh, *thread_args(tmp_path), FAKE_GH_LOGIN="hubot\n")

    assert result.returncode == 0, result.stderr
    assert gh.posted_bodies() == [{"body": "Fixed in abc1234.\n\n---\nagent on behalf of hubot\n"}]


def test_success_prints_the_github_response_unchanged(gh: FakeGh, tmp_path: Path):
    response = '{"id": 77,   "html_url": "https://github.com/octo-org/hello.world/pull/12"}\n'

    result = run_cli(gh, *thread_args(tmp_path), FAKE_GH_POST_STDOUT=response)

    assert result.returncode == 0, result.stderr
    assert result.stdout == response


# Refusals before any call to GitHub


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--repo", "octo-org"),
        ("--repo", "octo-org/hello/world"),
        ("--repo", "octo org/hello"),
        ("--repo", "octo_org/hello"),
        ("--repo", "octo-org/hello\n"),
        ("--repo", "/hello"),
        ("--repo", "octo-org/"),
        ("--repo", "octo-org/.."),
        ("--repo", "{owner}/{repo}"),
        ("--pr", "0"),
        ("--pr", "-1"),
        ("--pr", "abc"),
        ("--pr", "1.5"),
        ("--pr", "012"),
        ("--pr", " 12"),
        ("--pr", "12\n"),
        ("--pr", "\uff11\uff12"),
        ("--thread", "0"),
        ("--thread", "-3"),
        ("--thread", "1e3"),
    ],
)
def test_bad_target_is_refused_before_any_github_call(
    gh: FakeGh, tmp_path: Path, option: str, value: str
):
    args = thread_args(tmp_path)
    args[args.index(option) + 1] = value

    result = run_cli(gh, *args)

    assert result.returncode == 2
    assert result.stdout == ""
    error = error_of(result)
    assert error["error"] == "invalid_input"
    assert option in error["details"][0]["message"]
    assert gh.calls() == []


@pytest.mark.parametrize("body", ["", " ", "\n", " \t\r\n"])
def test_blank_body_is_refused_before_any_github_call(gh: FakeGh, tmp_path: Path, body: str):
    result = run_cli(gh, *thread_args(tmp_path, body))

    assert result.returncode == 2
    assert result.stdout == ""
    error = error_of(result)
    assert error["error"] == "invalid_input"
    assert "empty" in error["details"][0]["message"]
    assert gh.calls() == []


def test_blank_body_on_standard_input_is_refused(gh: FakeGh):
    args = ["--repo", REPO, "--pr", "12", "--issue", "--body-file", "-"]

    result = run_cli(gh, *args, stdin="  \n")

    assert result.returncode == 2
    assert error_of(result)["error"] == "invalid_input"
    assert gh.calls() == []


def test_unreadable_body_file_is_refused_before_any_github_call(gh: FakeGh, tmp_path: Path):
    args = ["--repo", REPO, "--pr", "12", "--issue", "--body-file", str(tmp_path / "gone.md")]

    result = run_cli(gh, *args)

    assert result.returncode == 2
    assert result.stdout == ""
    error = error_of(result)
    assert error["error"] == "invalid_input"
    assert "gone.md" in error["details"][0]["message"]
    assert gh.calls() == []


def test_body_file_that_is_not_utf8_is_refused(gh: FakeGh, tmp_path: Path):
    path = tmp_path / "body.md"
    path.write_bytes(b"caf\xe9\n")

    result = run_cli(gh, "--repo", REPO, "--pr", "12", "--issue", "--body-file", str(path))

    assert result.returncode == 2
    assert error_of(result)["error"] == "invalid_input"
    assert gh.calls() == []


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (["--thread", "345", "--issue"], "not allowed with argument"),
        ([], "one of the arguments --thread --issue is required"),
    ],
)
def test_exactly_one_of_thread_or_issue(
    gh: FakeGh, tmp_path: Path, target: list[str], message: str
):
    args = ["--repo", REPO, "--pr", "12", *target, "--body-file", body_file(tmp_path)]

    result = run_cli(gh, *args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert message in result.stderr
    assert gh.calls() == []


# Refusals after a GitHub call


@pytest.mark.parametrize(
    "env",
    [
        {"FAKE_GH_USER_EXIT": "1"},
        {"FAKE_GH_LOGIN": ""},
        {"FAKE_GH_LOGIN": "  "},
        {"FAKE_GH_LOGIN": "octo cat"},
    ],
)
def test_bad_handle_posts_nothing(gh: FakeGh, tmp_path: Path, env: dict[str, str]):
    result = run_cli(gh, *thread_args(tmp_path), **env)

    assert result.returncode == 1
    assert result.stdout == ""
    error = error_of(result)
    assert error["error"] == "github_error"
    assert "handle" in error["details"][0]["message"]
    assert gh.requests() == [("GET", "user")]


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"FAKE_GH_POST_EXIT": "1", "FAKE_GH_POST_STDOUT": ""}, id="gh-fails-silently"),
        pytest.param(
            {"FAKE_GH_POST_EXIT": "1", "FAKE_GH_POST_STDOUT": '{"message": "Validation Failed"}\n'},
            id="gh-fails-with-body",
        ),
        pytest.param({"FAKE_GH_POST_STDOUT": ""}, id="empty-response"),
        pytest.param({"FAKE_GH_POST_STDOUT": "not json"}, id="not-json"),
        pytest.param({"FAKE_GH_POST_STDOUT": '[{"id": 9001}]'}, id="array"),
        pytest.param({"FAKE_GH_POST_STDOUT": '{"html_url": "https://github.com/x"}'}, id="no-id"),
        pytest.param({"FAKE_GH_POST_STDOUT": '{"id": true}'}, id="bool-id"),
        pytest.param({"FAKE_GH_POST_STDOUT": '{"id": "9001"}'}, id="string-id"),
        pytest.param({"FAKE_GH_POST_STDOUT": '{"id": 9001.0}'}, id="float-id"),
    ],
)
def test_failed_post_reports_github_error_and_relays_raw_output(
    gh: FakeGh, tmp_path: Path, env: dict[str, str]
):
    result = run_cli(gh, *thread_args(tmp_path), **env)

    assert result.returncode == 1
    assert result.stdout == env["FAKE_GH_POST_STDOUT"]
    assert error_of(result)["error"] == "github_error"
    assert len(gh.posts()) == 1


def test_missing_gh_is_a_github_error(gh: FakeGh, tmp_path: Path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()

    result = run_cli(gh, *thread_args(tmp_path), PATH=str(empty))

    assert result.returncode == 1
    assert result.stdout == ""
    error = error_of(result)
    assert error["error"] == "github_error"
    assert "gh" in error["details"][0]["message"]
    assert gh.calls() == []
