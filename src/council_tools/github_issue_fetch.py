"""Acquire one GitHub issue payload, failing closed when it describes the wrong thing.

Two modules already turn acquired payloads into the evidence the admission
predicate consumes.  Nothing acquired them, so callers did it by hand, and a
hand-rolled acquisition has failure modes that each yield a payload which parses
cleanly and describes something other than what was asked for:

* a **renamed repository** answers through a redirect, so the response describes
  a different repository while the request carried the requested name;
* a response arriving **after an edit** means a body read in one call and labels
  read in another describe two different versions of one issue, and the
  combination describes neither;
* a **rate-limited or malformed** response reads as an absence rather than as a
  refusal.

The downstream normalizers cannot catch any of these.  They validate *shape*,
and every one of these produces a well-shaped payload.  So each is refused here.

This module performs no network access.  The transport is a caller-supplied
callable, which is also what makes every failure mode above testable without
one.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

#: The fields an observation is composed from.  ``updatedAt`` is what identity
#: is pinned on: GitHub advances it whenever the issue changes, so a reread that
#: returns a different value proves the issue moved between the calls.
#:
#: What it does **not** prove: that nothing changed when the value is equal.
#: Two edits within the timestamp's resolution, or a change to something the
#: field does not track, would both leave it unmoved.  It detects motion; it
#: does not certify stillness, and no field GitHub offers here would.
IDENTITY_FIELD = "updatedAt"

REQUIRED_FIELDS = ("number", "state", "labels", "body", IDENTITY_FIELD)


class GitHubFetchError(ValueError):
    """A stable, field-addressed acquisition refusal."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"github issue fetch {code} at {field}")


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise GitHubFetchError("malformed-response", field)
    return value


def _require_text(value: Any, field: str) -> str:
    if type(value) is not str:
        raise GitHubFetchError("malformed-response", field)
    return value


def fetch_issue(
    transport: Callable[[str, int], Any],
    *,
    repository: str,
    issue_number: int,
) -> dict[str, Any]:
    """Return one consistent raw payload for ``repository``/``issue_number``.

    ``transport`` is called with the requested repository and issue number and
    must return the raw response mapping.  It is called twice: once to read the
    observation, once to reread the identity value.
    """

    requested_repository = _require_text(repository, "repository")
    if type(issue_number) is not int or issue_number <= 0:
        raise GitHubFetchError("invalid-issue-number", "issueNumber")
    if not callable(transport):
        raise GitHubFetchError("invalid-transport", "transport")

    first = _observe(transport, requested_repository, issue_number, field="response")
    second = _observe(transport, requested_repository, issue_number, field="reread")

    if first[IDENTITY_FIELD] != second[IDENTITY_FIELD]:
        # Returning either version would hand downstream a body and a label set
        # that may belong to different versions of the issue.
        raise GitHubFetchError("issue-changed-during-read", IDENTITY_FIELD)

    return dict(first)


def _observe(
    transport: Callable[[str, int], Any],
    repository: str,
    issue_number: int,
    *,
    field: str,
) -> Mapping[str, Any]:
    try:
        response = transport(repository, issue_number)
    except GitHubFetchError:
        raise
    except Exception as error:  # noqa: BLE001 - any transport failure is a refusal
        raise GitHubFetchError("transport-failed", field) from error

    payload = _require_mapping(response, field)

    # A rate-limit answer is a refusal, not an absence.  Read as an absence it
    # becomes "this issue has no labels" or "this issue does not exist".
    if payload.get("rateLimited") or payload.get("status") in (403, 429):
        raise GitHubFetchError("rate-limited", field)

    if payload.get("redirected") or payload.get("movedTo"):
        raise GitHubFetchError("repository-redirected", field)

    for name in REQUIRED_FIELDS:
        if name not in payload:
            raise GitHubFetchError("malformed-response", f"{field}.{name}")

    reported_repository = payload.get("repository")
    if reported_repository is not None:
        if _require_text(reported_repository, f"{field}.repository") != repository:
            # The payload's self-report is checked, not trusted: a redirect that
            # did not announce itself looks exactly like the response wanted.
            raise GitHubFetchError("repository-mismatch", f"{field}.repository")

    number = payload["number"]
    if type(number) is not int or number != issue_number:
        raise GitHubFetchError("issue-number-mismatch", f"{field}.number")

    _require_text(payload[IDENTITY_FIELD], f"{field}.{IDENTITY_FIELD}")
    return payload


__all__ = [
    "IDENTITY_FIELD",
    "REQUIRED_FIELDS",
    "GitHubFetchError",
    "fetch_issue",
]
