"""Inspect a bare repository for state that redirects reads.

A repository can redirect reads without any object name changing.

* An **alternates file** names other object databases whose contents then become
  visible as if they were local, so an object the repository never received can
  be read out of it.
* A **replacement reference** makes a read of one object return the bytes of
  another, while the object name still looks correct.

``design/git-fd-binding-spike.md`` measured the second on the pinned Git binary
and found the defending flag works only from the Git-global position.  It did
**not** exercise alternates at all.  So this preflight is required rather than
superseded, and for the alternates half its own behaviour is the only evidence
there is: nothing here may borrow credibility from the spike for that.

Inspection is conservative.  An alternates file that exists but cannot be read,
a reference namespace that cannot be listed, or a directory that does not have
the structure of the bare repository the caller expected are all conditions
under which the correct answer is **refusal**, not a report of nothing found.
The difference matters because "clean" and "could not tell" are indistinguishable
downstream and mean opposite things.

Filesystem inspection only: no Git subprocess is run and no Git command is built.
This module reports; the caller decides what a finding means.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Standard locations inside a bare repository.
ALTERNATES_FILE = ("objects", "info", "alternates")
ALTERNATE_OBJECT_DIRECTORY = ("objects", "info", "alternates")
LOOSE_REPLACE_NAMESPACE = ("refs", "replace")
PACKED_REFS_FILE = ("packed-refs",)
REQUIRED_BARE_ENTRIES = ("objects", "refs", "HEAD")

_PACKED_REPLACE_PREFIX = "refs/replace/"


class GitRepositoryHygieneError(ValueError):
    """A stable, field-addressed hygiene-inspection refusal."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"git repository hygiene {code} at {field}")


@dataclass(frozen=True)
class HygieneReport:
    """Exactly what was found, named rather than summarised."""

    alternates: tuple[str, ...] = ()
    replacement_refs: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.alternates and not self.replacement_refs


def _read_text(path: Path, *, field_name: str) -> str | None:
    """Return the file's text, ``None`` if absent, or refuse if unreadable.

    A file that exists and cannot be read is the case this function exists for:
    treating it as absent would report clean on a repository whose alternates
    could not be checked.
    """

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise GitRepositoryHygieneError("unreadable", field_name) from error


def _require_bare_repository(root: Path) -> None:
    if not root.is_dir():
        raise GitRepositoryHygieneError("not-a-directory", "repository")
    for entry in REQUIRED_BARE_ENTRIES:
        if not (root / entry).exists():
            # Reporting "nothing found" about a directory that is not the
            # repository the caller meant would be true and useless.
            raise GitRepositoryHygieneError("not-a-bare-repository", f"repository.{entry}")


def _alternates(root: Path) -> tuple[str, ...]:
    text = _read_text(root.joinpath(*ALTERNATES_FILE), field_name="objects.info.alternates")
    if text is None:
        return ()
    return tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _loose_replacements(root: Path) -> tuple[str, ...]:
    namespace = root.joinpath(*LOOSE_REPLACE_NAMESPACE)
    if not namespace.exists():
        return ()
    found: list[str] = []
    try:
        for current, _directories, files in os.walk(namespace, onerror=_raise_walk_error):
            for name in files:
                relative = Path(current, name).relative_to(root)
                found.append(relative.as_posix())
    except OSError as error:
        raise GitRepositoryHygieneError("unreadable", "refs.replace") from error
    return tuple(sorted(found))


def _raise_walk_error(error: OSError) -> None:
    raise error


def _packed_replacements(root: Path) -> tuple[str, ...]:
    """Find replacements recorded in packed form.

    A replacement can live loose or packed, and a preflight that checked only
    one would report clean on a repository carrying the other.
    """

    text = _read_text(root.joinpath(*PACKED_REFS_FILE), field_name="packed-refs")
    if text is None:
        return ()
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("^"):
            continue
        parts = stripped.split(" ", 1)
        if len(parts) == 2 and parts[1].startswith(_PACKED_REPLACE_PREFIX):
            found.append(parts[1])
    return tuple(sorted(found))


def inspect_repository(repository_path: str | os.PathLike[str]) -> HygieneReport:
    """Report alternate and replacement state, or refuse if it cannot be told."""

    if isinstance(repository_path, (bytes, bytearray)):
        raise GitRepositoryHygieneError("invalid-path", "repository")
    try:
        root = Path(repository_path)
    except TypeError as error:
        raise GitRepositoryHygieneError("invalid-path", "repository") from error

    _require_bare_repository(root)
    replacements = _loose_replacements(root) + _packed_replacements(root)
    return HygieneReport(
        alternates=_alternates(root),
        replacement_refs=tuple(sorted(set(replacements))),
    )


__all__ = [
    "ALTERNATES_FILE",
    "LOOSE_REPLACE_NAMESPACE",
    "PACKED_REFS_FILE",
    "REQUIRED_BARE_ENTRIES",
    "GitRepositoryHygieneError",
    "HygieneReport",
    "inspect_repository",
]
