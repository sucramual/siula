"""Post a signed reply to a pull request review thread, or a signed top-level comment.

Every post ends with an attribution footer. A collector finds the agent's own comments by the
footer bytes, so sign() and FOOTER live together here and must change together. If the bytes
drift, the agent's own replies read as new feedback.

The repository is always named by the caller. Nothing here reads it from a working directory.
"""

import json
import re
import subprocess

ATTRIBUTION = "agent on behalf of"
FOOTER = re.compile(r"\n\n---\nagent on behalf of [^\n]*\s*\Z")

_REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_POSITIVE = re.compile(r"[1-9][0-9]*")


class PostInputError(ValueError):
    """An input that the poster refuses before it calls GitHub."""


class GitHubError(Exception):
    """A failed GitHub call. stdout holds whatever gh printed, which can name a created comment."""

    def __init__(self, message: str, stdout: bytes = b"", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


def sign(body: str, handle: str) -> str:
    """Append the attribution footer to body exactly as given. A signed body stays unchanged."""
    tail = f"\n\n---\n{ATTRIBUTION} {handle}"
    trimmed = body[:-1] if body.endswith("\n") else body
    if trimmed.endswith(tail):
        return body
    return f"{body}{tail}\n"


def unsign(body: str) -> str:
    """Remove one attribution footer from the end of body."""
    return FOOTER.sub("", body, count=1)


def api_path(repo: str, pr: str, thread: str | None) -> str:
    """Return the REST path for a thread reply, or for an issue comment when thread is None."""
    owner_name = _REPO.fullmatch(repo)
    if owner_name is None or repo.split("/")[1] in (".", ".."):
        raise PostInputError(f"--repo must be OWNER/NAME, got {repo!r}")
    if _POSITIVE.fullmatch(pr) is None:
        raise PostInputError(f"--pr must be a positive integer, got {pr!r}")
    if thread is None:
        return f"repos/{repo}/issues/{pr}/comments"
    if _POSITIVE.fullmatch(thread) is None:
        raise PostInputError(f"--thread must be a positive integer comment id, got {thread!r}")
    return f"repos/{repo}/pulls/{pr}/comments/{thread}/replies"


def require_body(body: str) -> str:
    if not body.strip():
        raise PostInputError("the body is empty or whitespace-only")
    return body


def _gh(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["gh", *args],
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise GitHubError(f"cannot run gh: {exc}") from exc


def _stderr(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stderr.decode("utf-8", "replace").strip()


def current_handle() -> str:
    """Return the login of the user that gh is authenticated as."""
    result = _gh(["api", "user", "--jq", ".login"])
    if result.returncode != 0:
        raise GitHubError(
            f"cannot read the GitHub handle: gh exited {result.returncode}", stderr=_stderr(result)
        )
    try:
        handle = result.stdout.decode("utf-8").strip()
    except UnicodeError as exc:
        raise GitHubError(f"cannot read the GitHub handle: {exc}") from exc
    if not handle or any(character.isspace() for character in handle):
        raise GitHubError(f"the GitHub handle is empty or has whitespace: {handle!r}")
    return handle


def post(path: str, body: str) -> bytes:
    """POST {"body": body} to path and return gh's stdout, a JSON object with an integer id."""
    payload = json.dumps({"body": body}).encode("utf-8")
    result = _gh(["api", "--method", "POST", path, "--input", "-"], stdin=payload)
    if result.returncode != 0:
        raise GitHubError(
            f"gh api POST {path} exited {result.returncode}",
            stdout=result.stdout,
            stderr=_stderr(result),
        )
    try:
        response = json.loads(result.stdout)
    except ValueError:
        response = None
    comment_id = response.get("id") if isinstance(response, dict) else None
    if not isinstance(comment_id, int) or isinstance(comment_id, bool):
        raise GitHubError(
            f"gh api POST {path} returned no integer comment id",
            stdout=result.stdout,
            stderr=_stderr(result),
        )
    return result.stdout
