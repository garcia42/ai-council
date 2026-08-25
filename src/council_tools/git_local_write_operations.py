"""Closed builders for the two local write commands the claim protocol needs.

The code that builds a Git command decides what commands can exist.  If it
accepts a raw argument vector, a subcommand name, a configuration pair, or a
repository selector from a caller, the set of commands it can emit is unbounded
and no review of it can establish what it does.

Both operations here take no object operand at all: initialization is
parameterless beyond the object format, and the payload write reads its content
from byte standard input.  That makes the surface closable by construction, and
it is closed: there is no parameter through which a caller can add a flag.

Two properties are decided rather than defaulted.

* **Object format is requested explicitly.**  The protocol identifies objects by
  name, so inheriting whatever the binary happens to default to would let stored
  object names change underneath it.
* **No template, hook, signing, or automatic maintenance behaviour is enabled.**
  Each of those would run code or rewrite state the protocol never asked for, and
  a repository created with a populated template directory executes whatever
  hooks that directory contained.

Rendering only.  This module calls no subprocess, touches no filesystem, and
parses no result.  Repository binding is added by the runner, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from council_tools.git_process_contract import RenderedInvocation


#: Rejected in a rendered vector by :func:`assert_no_ambient_behaviour`.  Each
#: entry names a behaviour the protocol never asks for and must not acquire by
#: default.
FORBIDDEN_FLAG_PREFIXES = (
    "--template",      # a populated template directory installs hooks
    "--separate-git-dir",
    "--shared",
    "--gpg-sign",
    "-S",
    "--path",
    "--literally",
)

INIT_SUBCOMMAND = "init"
HASH_OBJECT_SUBCOMMAND = "hash-object"

#: An empty template directory is the documented way to install nothing.  It is
#: rendered by this module, never supplied by a caller.
_EMPTY_TEMPLATE = "--template="


class GitWriteOperationError(ValueError):
    """A stable, field-addressed write-operation construction failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"git write operation {code} at {field}")


@dataclass(frozen=True)
class InitializeBareRepository:
    """Initialize an already-created empty directory as a SHA-1 bare repository.

    Takes no caller parameters at all.  That is the point: there is nothing to
    supply, so there is nothing to smuggle.
    """

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(),
            subcommand=INIT_SUBCOMMAND,
            subcommand_args=("--bare", _EMPTY_TEMPLATE, "--object-format=sha1"),
            stdin=b"",
            identity={},
        )


@dataclass(frozen=True)
class WriteCanonicalBlob:
    """Write one payload blob, whose content is the only input.

    The content crosses as bytes on standard input rather than as a path, so
    there is no filename, no mode, and no way for a caller to name a file the
    protocol did not produce.
    """

    content: bytes

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise GitWriteOperationError("invalid-content", "content")

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(),
            subcommand=HASH_OBJECT_SUBCOMMAND,
            # ``-w`` writes the object; ``--stdin`` takes content from stdin.
            # The trailing ``--`` is accepted by hash-object on the pinned
            # binary (spike Row 9).  It is NOT available on every required
            # command, so its presence here is deliberate and local, not a
            # pattern other builders can assume.
            subcommand_args=("-w", "--stdin", "--"),
            stdin=self.content,
            identity={},
        )


def assert_no_ambient_behaviour(invocation: RenderedInvocation, *, field: str) -> None:
    """Refuse a rendered vector that would enable behaviour we never asked for.

    ``--template=`` with an empty value is the one permitted spelling, because
    it is how "install nothing" is written.
    """

    for argument in invocation.argv():
        if argument == _EMPTY_TEMPLATE:
            continue
        for prefix in FORBIDDEN_FLAG_PREFIXES:
            if argument == prefix or argument.startswith(prefix + "="):
                raise GitWriteOperationError("forbidden-flag", field)


__all__ = [
    "FORBIDDEN_FLAG_PREFIXES",
    "GitWriteOperationError",
    "InitializeBareRepository",
    "WriteCanonicalBlob",
    "assert_no_ambient_behaviour",
]
