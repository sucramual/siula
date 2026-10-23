#!/usr/bin/env bash
# resolve-thread.sh — mark a PR review thread as resolved
#
# Looks up the GraphQL thread node ID for a given REST review-comment ID,
# then calls resolveReviewThread. Idempotent: no-ops if already resolved
# or if the comment isn't on this PR.
#
# Usage:
#   resolve-thread.sh <owner/repo> <pr-number> <comment-id>
#
# Exit codes:
#   0  resolved, was already resolved, or comment not found (idempotent no-op)
#   1  GraphQL error or invalid argument

set -euo pipefail

REPO="${1:?Usage: resolve-thread.sh <owner/repo> <pr-number> <comment-id>}"
PR="${2:?Usage: resolve-thread.sh <owner/repo> <pr-number> <comment-id>}"
COMMENT_ID="${3:?Usage: resolve-thread.sh <owner/repo> <pr-number> <comment-id>}"

# Validate numeric — protects the jq filter from empty / non-int interpolation.
if ! [[ "$COMMENT_ID" =~ ^[0-9]+$ ]]; then
  echo "ERROR: comment-id must be a positive integer (got: '${COMMENT_ID}')" >&2
  exit 1
fi
if ! [[ "$PR" =~ ^[0-9]+$ ]]; then
  echo "ERROR: pr-number must be a positive integer (got: '${PR}')" >&2
  exit 1
fi

OWNER="${REPO%%/*}"
NAME="${REPO##*/}"

# Find the thread containing this comment. databaseId on a PullRequestReviewComment
# corresponds to the REST API id. Uses --argjson so jq compares typed numbers.
RESULT=$(gh api graphql -f query='
  query($owner: String!, $name: String!, $pr: Int!) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            id
            isResolved
            comments(first: 50) {
              pageInfo { hasNextPage }
              nodes { databaseId }
            }
          }
        }
      }
    }
  }
' -F owner="$OWNER" -F name="$NAME" -F pr="$PR")

if ! jq -e '
  (.errors == null)
  and (.data.repository.pullRequest != null)
  and ((.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage // false) | not)
  and (any(.data.repository.pullRequest.reviewThreads.nodes[]?; .comments.pageInfo.hasNextPage // false) | not)
' <<< "$RESULT" >/dev/null; then
  echo "ERROR: GitHub returned an incomplete review-thread result" >&2
  exit 1
fi

THREAD_ID=$(jq -r --argjson commentId "$COMMENT_ID" '
      .data.repository.pullRequest.reviewThreads.nodes[]
      | select(.comments.nodes[].databaseId == $commentId)
      | select(.isResolved == false)
      | .id' <<< "$RESULT" | head -1)

if [[ -z "$THREAD_ID" ]]; then
  # Either the comment doesn't exist on this PR or the thread is already resolved.
  # Both are no-ops; exit success.
  exit 0
fi

gh api graphql -f query='
  mutation($threadId: ID!) {
    resolveReviewThread(input: {threadId: $threadId}) {
      thread { isResolved }
    }
  }
' -F threadId="$THREAD_ID" --jq '.data.resolveReviewThread.thread.isResolved' >/dev/null
