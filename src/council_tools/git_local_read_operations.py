"""Closed builders for the four typed object reads used to verify a claim.

A read that can be redirected is worse than no read at all, because it returns
an answer that looks correct.  Two redirection routes were measured on the
pinned ``git 2.39.5`` and recorded in ``design/git-fd-binding-spike.md``:

* **Replacement references (Row 8).**  With a ``refs/replace/<oid>`` ref present,
  reading an object returns the *replacement* content while the object name still
  appears correct.  ``--no-replace-objects`` defends it, and only from the
  Git-global position — which is why it is rendered there on every read here and
  not as a subcommand flag.
* **The option terminator (Row 9).**  ``--end-of-options`` is not a Git-global
  option on that binary at all, and it fails before ``cat-file``'s type argument.
  It therefore cannot be the operand defence.  Exact ``Sha1ObjectId`` validation
  is: a forty-character lowercase hex string cannot be parsed as an option, a
  revision expression, or a pathspec.

A builder that accepted raw arguments, revision expressions, or path
specifications would reopen both routes at once, so this one accepts neither.
Object selectors cross only as the typed identifier.

The tree listing requests NUL-delimited output because an entry name may itself
contain whitespace or a path separator, and a human-readable listing cannot be
parsed back unambiguously.

Rendering only: no subprocess, no filesystem, and no parsing of results.
"""

from __future__ import annotations

from dataclasses import dataclass

from council_tools.git_object_id import Sha1ObjectId
from council_tools.git_process_contract import RenderedInvocation


#: Rendered in the Git-global region, before the subcommand.  Position is not
#: cosmetic: measured on the pinned binary, the flag defends only from there.
NO_REPLACE_OBJECTS = "--no-replace-objects"

CAT_FILE = "cat-file"
LS_TREE = "ls-tree"
REV_PARSE = "rev-parse"


class GitReadOperationError(ValueError):
    """A stable, field-addressed read-operation construction failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"git read operation {code} at {field}")


def _require_object_id(value: object, field: str) -> Sha1ObjectId:
    # Only the typed identifier crosses.  A raw string would reopen the operand
    # seam that the unavailable terminator cannot close.
    if type(value) is not Sha1ObjectId:
        raise GitReadOperationError("invalid-object-id", field)
    return value


@dataclass(frozen=True)
class _TypedObjectRead:
    """Common shape: one typed selector, no other caller input."""

    object_id: Sha1ObjectId

    def __post_init__(self) -> None:
        _require_object_id(self.object_id, "object_id")


@dataclass(frozen=True)
class ReadObjectType(_TypedObjectRead):
    """Report the type of one object."""

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(NO_REPLACE_OBJECTS,),
            subcommand=CAT_FILE,
            # Trailing ``--`` is accepted by cat-file on the pinned binary.
            subcommand_args=("-t", "--", self.object_id.wire_text),
        )


@dataclass(frozen=True)
class ReadCommitBytes(_TypedObjectRead):
    """Read one commit object's raw bytes."""

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(NO_REPLACE_OBJECTS,),
            subcommand=CAT_FILE,
            subcommand_args=("commit", "--", self.object_id.wire_text),
        )


@dataclass(frozen=True)
class ReadBlobBytes(_TypedObjectRead):
    """Read one blob object's raw bytes."""

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(NO_REPLACE_OBJECTS,),
            subcommand=CAT_FILE,
            subcommand_args=("blob", "--", self.object_id.wire_text),
        )


@dataclass(frozen=True)
class ListTree(_TypedObjectRead):
    """List one tree in full, with NUL-delimited machine-readable output."""

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(NO_REPLACE_OBJECTS,),
            subcommand=LS_TREE,
            # ``-z`` is what makes the output parseable when an entry name
            # contains whitespace or a separator.  The exact form ``ls-tree -z
            # <tree> --`` is the one the spike measured; no additional flag is
            # rendered, because an unmeasured flag on the pinned binary is a
            # claim this module cannot support.
            subcommand_args=("-z", self.object_id.wire_text, "--"),
        )


@dataclass(frozen=True)
class ObserveObjectFormat:
    """Observe the format this repository names objects in.

    This is the one read in the family that takes **no object selector**, and it
    is deliberately shaped differently from the rest for a measured reason.

    On the pinned ``git 2.39.5`` the documented option terminator is *consumed as
    an operand* by this query: ``rev-parse --end-of-options --show-object-format``
    treats the terminator as the thing being asked about, so a builder written on
    the assumption that the terminator separates flags from operands renders a
    command that asks the wrong question.  It is therefore absent here on
    purpose, not by oversight (spike Row 9).

    Because there is no selector, there is also no typed operand carrying the
    operand defence the way ``Sha1ObjectId`` does for the reads above.  This
    operation's safety rests entirely on accepting **nothing** from a caller, so
    it takes no parameters at all.

    It observes and does not judge: comparing the result to a required format
    belongs to whichever caller holds that expectation.
    """

    def render(self) -> RenderedInvocation:
        return RenderedInvocation(
            global_options=(),
            subcommand=REV_PARSE,
            subcommand_args=("--show-object-format",),
        )


#: Every read this module can emit.  Used by tests to assert the family is
#: closed rather than checked case by case.
SELECTOR_READS = (ReadObjectType, ReadCommitBytes, ReadBlobBytes, ListTree)
READ_OPERATIONS = SELECTOR_READS + (ObserveObjectFormat,)


__all__ = [
    "CAT_FILE",
    "LS_TREE",
    "NO_REPLACE_OBJECTS",
    "READ_OPERATIONS",
    "REV_PARSE",
    "SELECTOR_READS",
    "ObserveObjectFormat",
    "GitReadOperationError",
    "ListTree",
    "ReadBlobBytes",
    "ReadCommitBytes",
    "ReadObjectType",
]
