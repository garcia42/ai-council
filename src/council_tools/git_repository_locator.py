"""Lexically normalized absolute paths naming one bare repository.

The ticket-claim protocol has to name a repository on disk, and any string can
stand in for one unless something refuses the ambiguous spellings.  A path that
is relative, that contains a dot or dot-dot segment, that carries a trailing or
doubled separator, that uses an alternate separator, or that embeds a control
character will be read differently by different consumers, and some of those
readings resolve outside the directory the caller meant.

The custody design that follows binds execution to an inode obtained from this
location, so the location itself has to denote exactly one place.  Everything
rejected here is rejected because it denotes more than one, or because it
denotes one that cannot be written down unambiguously.

This type asserts **nothing about the filesystem**.  Whether the path exists,
what it points at, who owns it, its mode, its inode, and whether it is a
repository at all are questions the custody node answers by looking.  A value
type that implied any of them would invite callers to skip the checks that
actually answer them, so it implies none.

Normalization here is *lexical*: no symlink is resolved and no directory is
read, because both require touching the filesystem and neither is this type's
job.  A path that is already normalized is accepted as written; one that is not
is refused rather than rewritten, so the accepted text is always the caller's
own text.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


SEPARATOR = "/"
MAX_REPOSITORY_PATH_LENGTH = 4_096

_CONTROL_CHARACTERS = ("\x00", "\r", "\n")
_ALTERNATE_SEPARATORS = ("\\",)
_REJECTED_SEGMENTS = frozenset({"", ".", ".."})


class GitRepositoryLocatorError(ValueError):
    """A stable, field-addressed repository-path validation failure."""

    def __init__(self, code: str, field: str = "path"):
        self.code = code
        self.field = field
        super().__init__(f"git repository locator {code} at {field}")


def _validate(value: object, field: str) -> None:
    # ``type() is not str`` rather than ``isinstance``: a str subclass can
    # override comparison or slicing and need not behave as the text it spells.
    if type(value) is not str:
        raise GitRepositoryLocatorError("invalid-type", field)
    if not value:
        raise GitRepositoryLocatorError("empty-path", field)
    if len(value) > MAX_REPOSITORY_PATH_LENGTH:
        raise GitRepositoryLocatorError("path-too-long", field)
    if any(character in value for character in _CONTROL_CHARACTERS):
        raise GitRepositoryLocatorError("control-character", field)
    if any(character in value for character in _ALTERNATE_SEPARATORS):
        raise GitRepositoryLocatorError("alternate-separator", field)
    if not value.startswith(SEPARATOR):
        raise GitRepositoryLocatorError("relative-path", field)
    if value == SEPARATOR:
        raise GitRepositoryLocatorError("root-path", field)
    if value.endswith(SEPARATOR):
        raise GitRepositoryLocatorError("trailing-separator", field)

    # The leading separator is structural; every remaining segment must name
    # something.  An empty segment is a doubled separator, and the dot segments
    # denote a directory other than the one the text spells.
    for segment in value[1:].split(SEPARATOR):
        if segment == "":
            raise GitRepositoryLocatorError("doubled-separator", field)
        if segment in _REJECTED_SEGMENTS:
            raise GitRepositoryLocatorError("dot-segment", field)

    if unicodedata.normalize("NFC", value) != value:
        raise GitRepositoryLocatorError("not-nfc", field)
    # Text that cannot round-trip the filesystem encoding cannot be handed to a
    # syscall unchanged, so two spellings could otherwise both be "accepted"
    # while naming different bytes.
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise GitRepositoryLocatorError("not-round-trippable", field) from error
    if encoded.decode("utf-8", "strict") != value:  # pragma: no cover - defensive
        raise GitRepositoryLocatorError("not-round-trippable", field)


@dataclass(frozen=True)
class BareRepositoryLocator:
    """One lexically normalized absolute POSIX path naming a bare repository.

    Frozen and hashable, so it is safe as a mapping key and compares by value.
    Validation runs in ``__post_init__``, so every construction path is checked
    and there is no unvalidated way to build one.

    The name says ``bare`` because that is what the protocol will place there.
    The type does not verify it, and cannot: that is a filesystem claim.
    """

    path: str

    def __post_init__(self) -> None:
        _validate(self.path, "path")


__all__ = [
    "BareRepositoryLocator",
    "GitRepositoryLocatorError",
    "MAX_REPOSITORY_PATH_LENGTH",
    "SEPARATOR",
]
