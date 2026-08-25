"""Exact SHA-1 object identifier values for Git operand construction.

Every Git object operand in the ticket-claim protocol is an operand that Git's
own argument parser will read.  On the pinned ``git 2.39.5`` there is no
universal option terminator to lean on: ``--end-of-options`` is not a Git-global
option, it fails before ``cat-file``'s type argument, and ``rev-parse`` consumes
it as an operand.  Trailing ``--`` is accepted by ``hash-object``, ``cat-file``,
``ls-tree`` and ``commit-tree`` and by none of the other required commands.  The
measurement is recorded in ``design/git-fd-binding-spike.md``.

With no terminator to rely on, the accepted value is itself the barrier.  Exactly
forty lowercase hexadecimal characters cannot be parsed as an option, as a
revision expression, or as a pathspec, so a validated value closes the operand
seam that the terminator cannot.

This type is deliberately **non-authorizing**.  It asserts that its text is
well-formed, and nothing else: not that the object exists, is reachable, has any
particular type, or may be acted upon.  It performs no input-output, spawns no
process, and touches no filesystem.

There is intentionally no ``__str__``.  Command construction must reach for
:attr:`Sha1ObjectId.wire_text` explicitly, so an unvalidated value can never
reach an argument vector through implicit stringification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


SHA1_HEX_LENGTH = 40

_WIRE_RE = re.compile(r"[0-9a-f]{40}\Z")
_ANY_CASE_HEX_RE = re.compile(r"[0-9a-fA-F]{40}\Z")

# Rejected before the length rule, so that a well-formed identifier carrying a
# revision or path suffix reports the seam it was exploiting rather than a
# length that hides it.
_CONTROL_CHARACTERS = ("\x00", "\r", "\n")
_PATH_CHARACTERS = ("/", "\\")
_REVISION_CHARACTERS = ("^", "~", ":", "@", "{", "}", "?", "*", "[", "]", ".")
# ``.`` is in that set for range forms such as ``a..b`` and ``a...b``.  A
# well-formed identifier never contains one, so catching it costs nothing and
# names the seam instead of reporting an incidental length failure.


class GitObjectIdError(ValueError):
    """A stable, field-addressed object identifier validation failure."""

    def __init__(self, code: str, field: str = "objectId"):
        self.code = code
        self.field = field
        super().__init__(f"git object id {code} at {field}")


def _validate(value: object, field: str) -> None:
    # ``type() is not str`` rather than ``isinstance``: a str subclass can
    # override comparison, hashing or slicing, so it must not stand in for the
    # wire text even when it currently spells the same characters.
    if type(value) is not str:
        raise GitObjectIdError("invalid-type", field)
    if any(character in value for character in _CONTROL_CHARACTERS):
        raise GitObjectIdError("control-character", field)
    if any(character.isspace() for character in value):
        raise GitObjectIdError("contains-whitespace", field)
    if value.startswith("-"):
        raise GitObjectIdError("leading-dash", field)
    if value.startswith(".") or any(c in value for c in _PATH_CHARACTERS):
        raise GitObjectIdError("path-syntax", field)
    if any(character in value for character in _REVISION_CHARACTERS):
        raise GitObjectIdError("revision-syntax", field)
    if len(value) != SHA1_HEX_LENGTH:
        raise GitObjectIdError("invalid-length", field)
    if _WIRE_RE.fullmatch(value) is None:
        if _ANY_CASE_HEX_RE.fullmatch(value) is not None:
            raise GitObjectIdError("uppercase-hex", field)
        raise GitObjectIdError("non-hex-character", field)


@dataclass(frozen=True)
class Sha1ObjectId:
    """One exact lowercase forty-character hexadecimal SHA-1 object name.

    Frozen and hashable, so it is safe as a mapping key and compares by value.
    Validation runs in ``__post_init__``, so direct construction is validated on
    every path and there is no unchecked way to build one.
    """

    wire_text: str

    def __post_init__(self) -> None:
        _validate(self.wire_text, "wire_text")


__all__ = [
    "GitObjectIdError",
    "SHA1_HEX_LENGTH",
    "Sha1ObjectId",
]
