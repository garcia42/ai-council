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

import re
from dataclasses import dataclass, field
from typing import Mapping

from council_tools.git_object_id import Sha1ObjectId
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
MKTREE_SUBCOMMAND = "mktree"
COMMIT_TREE_SUBCOMMAND = "commit-tree"

#: The claim tree holds exactly one entry, and every part of that entry except
#: the blob identifier is fixed here.  A caller that could choose the mode, the
#: name, or the object type could choose what the claim contains, which is the
#: whole of it.
CLAIM_ENTRY_MODE = "100644"
CLAIM_ENTRY_TYPE = "blob"
CLAIM_ENTRY_NAME = "claim.json"

#: Git identity fields are delimited by ``<``, ``>`` and whitespace, so a value
#: containing one could terminate its own field and open another.
_IDENTITY_FORBIDDEN = ("\x00", "\r", "\n", "<", ">")
#: Git's explicit raw-date form, ``@<seconds> <zone>``.  Measured on the pinned
#: binary: a bare ``0 +0000`` is rejected outright ("invalid date format") while
#: ``@0 +0000`` is accepted, so requiring the ``@`` prefix is what makes the
#: whole range including the epoch expressible.  An earlier draft accepted the
#: bare form and would have rendered commands the binary refuses at run time.
_DATE_RE = re.compile(r"\A@[0-9]{1,20} [+-][0-9]{4}\Z")

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


def _require_identity_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise GitWriteOperationError("invalid-identity", field_name)
    if not value:
        raise GitWriteOperationError("invalid-identity", field_name)
    if any(character in value for character in _IDENTITY_FORBIDDEN):
        raise GitWriteOperationError("invalid-identity", field_name)
    return value


def _require_identity_date(value: object, field_name: str) -> str:
    if type(value) is not str or _DATE_RE.fullmatch(value) is None:
        raise GitWriteOperationError("invalid-identity-date", field_name)
    return value


@dataclass(frozen=True)
class CreateClaimTree:
    """Create the fixed one-entry tree holding the claim payload.

    ``mktree`` reads its entry list from standard input.  A builder that let a
    caller supply those bytes would let the caller choose the filename, the
    mode, the object type and the number of entries -- which is the entire
    content of the claim.  So the entry line is built here from the typed blob
    identifier and three fixed values, and there is no parameter through which
    anything else can enter.
    """

    blob_id: Sha1ObjectId

    def __post_init__(self) -> None:
        if type(self.blob_id) is not Sha1ObjectId:
            raise GitWriteOperationError("invalid-object-id", "blob_id")

    def entry_line(self) -> bytes:
        """The one entry, in the fixed grammar the spike measured."""

        return (
            f"{CLAIM_ENTRY_MODE} {CLAIM_ENTRY_TYPE} {self.blob_id.wire_text}"
            f"\t{CLAIM_ENTRY_NAME}\n"
        ).encode("ascii")

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(),
            subcommand=MKTREE_SUBCOMMAND,
            subcommand_args=(),
            stdin=self.entry_line(),
            identity={},
        )


@dataclass(frozen=True)
class CreateZeroParentCommit:
    """Create the claim commit: one tree, no parent, deterministic identity.

    The message crosses on standard input rather than through ``-m``, so message
    bytes never reach the argument vector and cannot be read as an option.

    Identity and dates are caller-supplied and validated; nothing is read from
    the ambient environment and no clock is consulted.  Identical inputs
    therefore render an identical command, which matters because a claim's
    object name would otherwise depend on when it was made rather than on what
    it says.  The values travel in the invocation's identity mapping, which the
    process policy already restricts to exactly these keys.
    """

    tree_id: Sha1ObjectId
    message: bytes
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str

    def __post_init__(self) -> None:
        if type(self.tree_id) is not Sha1ObjectId:
            raise GitWriteOperationError("invalid-object-id", "tree_id")
        if type(self.message) is not bytes:
            raise GitWriteOperationError("invalid-message", "message")
        _require_identity_text(self.author_name, "author_name")
        _require_identity_text(self.author_email, "author_email")
        _require_identity_date(self.author_date, "author_date")
        _require_identity_text(self.committer_name, "committer_name")
        _require_identity_text(self.committer_email, "committer_email")
        _require_identity_date(self.committer_date, "committer_date")

    def identity(self) -> Mapping[str, str]:
        return {
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_AUTHOR_DATE": self.author_date,
            "GIT_COMMITTER_NAME": self.committer_name,
            "GIT_COMMITTER_EMAIL": self.committer_email,
            "GIT_COMMITTER_DATE": self.committer_date,
        }

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(),
            subcommand=COMMIT_TREE_SUBCOMMAND,
            # No ``-p``: the claim commit has no parent, and there is no
            # parameter through which one could be supplied.
            subcommand_args=(self.tree_id.wire_text, "--"),
            stdin=self.message,
            identity=dict(self.identity()),
        )


#: Every write this module can emit.
WRITE_OPERATIONS = (
    InitializeBareRepository,
    WriteCanonicalBlob,
    CreateClaimTree,
    CreateZeroParentCommit,
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
    "CLAIM_ENTRY_MODE",
    "CLAIM_ENTRY_NAME",
    "CLAIM_ENTRY_TYPE",
    "COMMIT_TREE_SUBCOMMAND",
    "MKTREE_SUBCOMMAND",
    "WRITE_OPERATIONS",
    "CreateClaimTree",
    "CreateZeroParentCommit",
    "FORBIDDEN_FLAG_PREFIXES",
    "GitWriteOperationError",
    "InitializeBareRepository",
    "WriteCanonicalBlob",
    "assert_no_ambient_behaviour",
]
