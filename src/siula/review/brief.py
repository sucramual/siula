"""Build the prompt for a read-only pull request reviewer.

The brief joins the packaged reviewer instructions and test-integrity checklist with the pull
request's description and diff. The description and diff are untrusted, so each sits inside a
fenced code block that no line of its content can close.
"""

import re
from importlib.resources import files

INTENT_HEADING = "## Intent: pull request description (untrusted)"
DIFF_HEADING = "## Diff: pinned head against base (untrusted)"
NO_DESCRIPTION = "The pull request has no description."


class BriefError(ValueError):
    """An input that cannot make a meaningful brief."""


def _fence(content: str) -> str:
    """Return a backtick fence one longer than the longest backtick run in content, at least 3."""
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def _fenced(content: str, info: str = "") -> str:
    fence = _fence(content)
    if not content.endswith("\n"):
        content += "\n"
    return f"{fence}{info}\n{content}{fence}\n"


def _ending_with_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def build_brief(diff: str, intent: str) -> str:
    """Return the reviewer brief. Raise BriefError when the diff is empty or whitespace-only."""
    if not diff.strip():
        raise BriefError("the diff is empty: a review of nothing must not look like a clean review")
    package = files("siula.review")
    reviewer = package.joinpath("reviewer.md").read_text(encoding="utf-8")
    checklist = package.joinpath("checklist.md").read_text(encoding="utf-8")
    intent_body = _fenced(intent) if intent.strip() else NO_DESCRIPTION + "\n"
    parts = [
        reviewer,
        checklist,
        f"{INTENT_HEADING}\n\n{intent_body}",
        f"{DIFF_HEADING}\n\n{_fenced(diff, 'diff')}",
    ]
    return "\n".join(_ending_with_newline(part) for part in parts)
