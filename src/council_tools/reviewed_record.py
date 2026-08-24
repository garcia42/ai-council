"""Validate what a council row says it reviewed.

``complete`` used to splat ``councilFields`` into the row after checking only
that it did not collide with a protected key, so ``commits`` -- the single field
that says what a council actually read -- was never validated at all.  Seven
distinct shapes accumulated across the ledger, and a quarter of rows ended up
able to name what they reviewed.  Everything a reader could do with that field
was downstream of a key that mostly was not there.

This module closes the write side.  It defines exactly two valid shapes and
rejects everything else, so the ledger stops accumulating shapes nothing can
read.

**A commit review** is a JSON array of full 40-character object names::

    ["7d1a...", "9f30..."]

**A review of something that is not a commit** is an object stating what it was::

    {"state": "uncommitted", "contentSha256": "<64 hex>", "base": "<sha>"}

The second shape exists because a fifth of real councils review a staged or
uncommitted tree -- this project's own convention is *commit first, then
convene*, and it is honoured less than it is stated.  Those reviews are real and
there is no commit to name, so forcing them into an array would make the record
lie.  ``contentSha256`` is the digest of exactly what the seats read, which is
what a later reconciler can match a commit against.

Deliberately rejected, though all appear in history:

``[]``
    An empty array is indistinguishable from a field nobody populated.  A
    council that genuinely read no diff says so with ``state: "no-diff"``.
``{"base": "<sha>"}``
    A branch point is where the work started, not what was reviewed.
``null``, absent, or any other shape
    Nothing can be read from them.

This validates; it does not derive.  Deriving the array inside ``complete``
would put git in the one code path that must not acquire new ways to fail
mid-transaction, and the caller already computes the range it briefed the seats
on.  Validation makes an unreadable claim impossible to append; it cannot make a
false one impossible, and nothing here should be read as proving a review
happened.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


class ReviewedRecordError(ValueError):
    """A council row does not say what it reviewed in a readable shape."""


_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_OBJECT_NAME = re.compile(r"^[0-9a-fA-F]{7,40}$")

#: What a review that is not of a commit was actually of.  Closed, because an
#: open vocabulary is how the seven historical shapes happened.
NON_COMMIT_STATES = {
    "uncommitted": "working-tree content that was never committed",
    "staged": "content staged in the index",
    "no-diff": "a decision with no diff to read",
    "decision-only": "a decision, document, or policy rather than code",
}

#: States whose review had content a digest can be taken of.  ``no-diff`` and
#: ``decision-only`` had none, so requiring a digest of them would only invite a
#: digest of something arbitrary.
_STATES_REQUIRING_DIGEST = {"uncommitted", "staged"}

_ALLOWED_OBJECT_KEYS = {"state", "contentSha256", "base", "note"}


def _shapes_message() -> str:
    return (
        "commits must be either a non-empty array of full 40-character object "
        "names, or an object {\"state\": <"
        + "|".join(sorted(NON_COMMIT_STATES))
        + ">, \"contentSha256\": <64 hex, required for "
        + "|".join(sorted(_STATES_REQUIRING_DIGEST))
        + ">, \"base\": <optional object name>, \"note\": <optional text>}"
    )


def validate_reviewed_record(value: Any) -> str:
    """Return the record's kind, or raise :class:`ReviewedRecordError`.

    The returned kind is ``"commits"`` or ``"non-commit"``.  Callers use it to
    say which shape was accepted; nothing about it asserts that a review
    actually took place.
    """

    if isinstance(value, Mapping):
        return _validate_non_commit(value)
    if isinstance(value, str) or not isinstance(value, Sequence):
        # A bare string is a single object name someone forgot to wrap, and is
        # rejected rather than guessed at; everything else is not a record.
        raise ReviewedRecordError(_shapes_message())
    return _validate_commit_array(value)


def _validate_commit_array(value: Sequence[Any]) -> str:
    if not value:
        raise ReviewedRecordError(
            "commits must not be an empty array: it cannot be told apart from a "
            "field nobody populated. A council that read no diff records "
            '{"state": "no-diff"}.'
        )
    for index, item in enumerate(value):
        if not isinstance(item, str) or not _FULL_SHA.match(item):
            raise ReviewedRecordError(
                f"commits[{index}] must be a full 40-character object name; "
                "an abbreviation does not survive a rewritten history and "
                f"cannot be resolved from the ledger alone: {item!r}"
            )
    if len(set(value)) != len(value):
        raise ReviewedRecordError("commits must not repeat an object name")
    return "commits"


def _validate_non_commit(value: Mapping[str, Any]) -> str:
    unknown = sorted(set(value) - _ALLOWED_OBJECT_KEYS)
    if unknown:
        # An open vocabulary is exactly how `base`/`candidate`/`candidate_tree`/
        # `prodHead`/`stagedTree`/`branch`/`state` accumulated, each meaning
        # something slightly different and none of them validated.
        raise ReviewedRecordError(
            f"commits object has unknown keys: {unknown}. " + _shapes_message()
        )
    state = value.get("state")
    if state not in NON_COMMIT_STATES:
        raise ReviewedRecordError(
            "commits object must carry state as one of "
            f"{sorted(NON_COMMIT_STATES)}; got {state!r}. A branch point alone "
            "does not say what was reviewed."
        )
    digest = value.get("contentSha256")
    if state in _STATES_REQUIRING_DIGEST:
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise ReviewedRecordError(
                f"commits state {state!r} reviewed content, so contentSha256 "
                "must be its 64-character SHA-256; without it the row records "
                "that something was reviewed but not what"
            )
    elif digest is not None:
        raise ReviewedRecordError(
            f"commits state {state!r} had no content to digest, so "
            "contentSha256 must be omitted rather than filled with something "
            "arbitrary"
        )
    base = value.get("base")
    if base is not None and (not isinstance(base, str) or not _OBJECT_NAME.match(base)):
        raise ReviewedRecordError(
            f"commits base must be an object name when present: {base!r}"
        )
    note = value.get("note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise ReviewedRecordError("commits note must be non-empty text when present")
    return "non-commit"
