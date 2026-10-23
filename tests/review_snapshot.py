"""PR snapshots in the GraphQL response shape that siula.review.collect reads.

Shared by test_review_collect.py and test_review_round.py. Times are ISO-8601 UTC strings
(GitHub's form); callers pass them, so a test can use the real clock or a fake one.
"""

from typing import Any

HEAD = "1111111aaaaaaabbbbbbbcccccccdddddddeeee"


def page(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """A complete connection: the collector refuses a snapshot whose pages are not all read."""
    return {"pageInfo": {"hasNextPage": False}, "nodes": nodes}


def comment(database_id: int, body: str, login: str, at: str) -> dict[str, Any]:
    """A review-thread comment or an issue comment."""
    return {"databaseId": database_id, "body": body, "author": {"login": login}, "createdAt": at}


def thread(
    thread_id: str, comments: list[dict[str, Any]], *, path: str = "README.md", line: int | None = 1
) -> dict[str, Any]:
    """An unresolved review thread."""
    return {
        "id": thread_id,
        "isResolved": False,
        "isOutdated": False,
        "path": path,
        "line": line,
        "comments": page(comments),
    }


def review_node(database_id: int, oid: str, login: str, body: str, at: str) -> dict[str, Any]:
    return {
        "databaseId": database_id,
        "body": body,
        "state": "COMMENTED",
        "author": {"login": login},
        "submittedAt": at,
        "commit": {"oid": oid},
    }


def reaction_node(content: str, login: str, at: str) -> dict[str, Any]:
    return {"content": content, "createdAt": at, "user": {"login": login}}


def next_id(pull: dict[str, Any]) -> int:
    """An id above every thread comment, review, and issue comment of a pullRequest object."""
    nodes = [c for t in pull["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]]
    reviews, comments = pull["reviews"]["nodes"], pull["comments"]["nodes"]
    return max(node["databaseId"] for node in nodes + reviews + comments) + 1


def pr_snapshot(
    *,
    head: str = HEAD,
    review_decision: str | None = None,
    threads: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    reactions: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A complete GraphQL result for a PR whose head is ``head``."""
    pull = {
        "headRefOid": head,
        "reviewDecision": review_decision,
        "reviewThreads": page(threads or []),
    }
    pull.update(
        reviews=page(reviews or []), comments=page(comments or []), reactions=page(reactions or [])
    )
    return {"data": {"repository": {"pullRequest": pull}}}
