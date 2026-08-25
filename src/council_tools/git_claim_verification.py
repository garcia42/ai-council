"""Verify that a commit object says what a claim would say.

An object identifier proves that some object was *named*.  It says nothing
about what that object contains.  When a claim is **found** rather than made,
the only question that matters is whether the object says what a claim by this
holder for this issue would say -- and every part of that question is a place a
mismatch can hide:

* the named object might not be a commit at all;
* a commit might carry a **parent**, which makes it a step in a history rather
  than the standalone assertion a claim is;
* the tree might hold a **second entry**, or one entry under a different name,
  mode or type, and a reader that inspected only the entry it expected would
  never notice the others;
* the payload might differ from the canonical rendering in a way no reader
  comparing *parsed fields* would see, because two different byte strings can
  parse to equal values -- and it is the **bytes** that were hashed;
* the identity recorded on the commit might name a different holder or issuance
  time than the payload does, so the object would disagree with itself.

A verifier that checks some of these and not others produces a positive result
weaker than it appears, and a positive result is what a later decision acts on.
So every check is performed and each has its own refusal reason.

Verification is against a :class:`ClaimRequest`, not against loose expected
values, so the thing verified against is the same validated, canonically
rendered record the write path uses and the two cannot drift apart.

**A positive result says what the object contains.**  It does not say its holder
owns anything.  Ownership is decided by a ref, and this module reads none.

One measured detail, because assuming it would make the verifier reject every
real claim: a request records its issuance time as ``@<seconds> <offset>``, and
Git normalises that to ``<seconds> <offset>`` when it writes the commit.  The
leading ``@`` is therefore removed before comparison.  Confirmed on the pinned
binary for a zero, a negative and a positive offset.
"""

from __future__ import annotations

from typing import Any

from council_tools.git_claim_request import ClaimRequest
from council_tools.git_local_read_operations import (
    ListTree,
    ReadBlobBytes,
    ReadCommitBytes,
    ReadObjectType,
)
from council_tools.git_local_write_operations import (
    CLAIM_ENTRY_MODE,
    CLAIM_ENTRY_NAME,
    CLAIM_ENTRY_TYPE,
)
from council_tools.git_object_id import GitObjectIdError, Sha1ObjectId
from council_tools.git_repository_lease import BareRepositoryLease
from council_tools.git_repository_read_runner import run_read_operation

COMMIT_OBJECT_TYPE = "commit"

#: Named so a refusal says which question failed rather than that something did.
CHECK_OBJECT_TYPE = "object-type"
CHECK_COMMIT_SHAPE = "commit-shape"
CHECK_COMMIT_IDENTITY = "commit-identity"
CHECK_COMMIT_MESSAGE = "commit-message"
CHECK_TREE_SHAPE = "tree-shape"
CHECK_PAYLOAD = "payload"

VERIFICATION_CHECKS: tuple[str, ...] = (
    CHECK_OBJECT_TYPE,
    CHECK_COMMIT_SHAPE,
    CHECK_COMMIT_IDENTITY,
    CHECK_COMMIT_MESSAGE,
    CHECK_TREE_SHAPE,
    CHECK_PAYLOAD,
)


class ClaimVerificationError(ValueError):
    """A stable, check-addressed verification refusal."""

    def __init__(self, code: str, check: str):
        self.code = code
        self.check = check
        super().__init__(f"claim verification {code} at {check}")


def _require_success(result: Any, check: str) -> bytes:
    # The result type refuses truthiness, so the two failure channels are
    # checked separately and reported separately: a command that ran and failed
    # and one that never ran are different problems.
    if result.local_failure is not None:
        raise ClaimVerificationError("local-failure", check)
    if result.exit_status != 0:
        raise ClaimVerificationError("nonzero-exit", check)
    return result.stdout


def _object_id(text: str, check: str) -> Sha1ObjectId:
    try:
        return Sha1ObjectId(text)
    except GitObjectIdError:
        raise ClaimVerificationError("malformed-output", check) from None


def _commit_date(issued_at: str) -> str:
    """The form Git records, derived from the form a request carries."""

    return issued_at[1:] if issued_at.startswith("@") else issued_at


def _parse_commit(raw: bytes) -> tuple[dict[str, list[str]], bytes]:
    """Split a commit into its headers and its message.

    A header may legally repeat (``parent`` in particular), so values are kept
    as lists.  Collapsing them to one value per key is what would let a second
    parent disappear.
    """

    separator = raw.find(b"\n\n")
    if separator == -1:
        raise ClaimVerificationError("malformed-output", CHECK_COMMIT_SHAPE)
    try:
        header_text = raw[:separator].decode("utf-8")
    except UnicodeDecodeError:
        raise ClaimVerificationError("malformed-output", CHECK_COMMIT_SHAPE) from None
    headers: dict[str, list[str]] = {}
    for line in header_text.split("\n"):
        key, _, value = line.partition(" ")
        headers.setdefault(key, []).append(value)
    return headers, raw[separator + 2 :]


def _verify_commit_headers(
    headers: dict[str, list[str]], request: ClaimRequest
) -> Sha1ObjectId:
    if len(headers.get("tree", [])) != 1:
        raise ClaimVerificationError("not-exactly-one-tree", CHECK_COMMIT_SHAPE)
    if headers.get("parent"):
        # A claim is a standalone assertion, not a step in a history.
        raise ClaimVerificationError("commit-has-parent", CHECK_COMMIT_SHAPE)

    expected = (
        f"{request.holder} <{request.holder_email()}> {_commit_date(request.issued_at)}"
    )
    for field in ("author", "committer"):
        values = headers.get(field, [])
        if len(values) != 1:
            raise ClaimVerificationError(
                f"not-exactly-one-{field}", CHECK_COMMIT_IDENTITY
            )
        if values[0] != expected:
            raise ClaimVerificationError(f"{field}-mismatch", CHECK_COMMIT_IDENTITY)

    return _object_id(headers["tree"][0], CHECK_COMMIT_SHAPE)


def _verify_tree(raw: bytes) -> Sha1ObjectId:
    """Require exactly one entry, with the fixed mode, type and name.

    ``ls-tree -z`` NUL-*terminates* each entry, so the split leaves a trailing
    empty element.  Only that one is dropped: dropping every empty element would
    hide an entry with an empty name.
    """

    parts = raw.split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    if len(parts) != 1:
        # An extra entry is refused as firmly as a wrong one: a reader that
        # inspected only the entry it expected would not notice the others.
        raise ClaimVerificationError("not-exactly-one-entry", CHECK_TREE_SHAPE)
    try:
        entry = parts[0].decode("utf-8")
    except UnicodeDecodeError:
        raise ClaimVerificationError("malformed-output", CHECK_TREE_SHAPE) from None

    metadata, tab, name = entry.partition("\t")
    if not tab:
        raise ClaimVerificationError("malformed-output", CHECK_TREE_SHAPE)
    fields = metadata.split(" ")
    if len(fields) != 3:
        raise ClaimVerificationError("malformed-output", CHECK_TREE_SHAPE)
    mode, object_type, object_name = fields
    if mode != CLAIM_ENTRY_MODE:
        raise ClaimVerificationError("entry-mode-mismatch", CHECK_TREE_SHAPE)
    if object_type != CLAIM_ENTRY_TYPE:
        raise ClaimVerificationError("entry-type-mismatch", CHECK_TREE_SHAPE)
    if name != CLAIM_ENTRY_NAME:
        raise ClaimVerificationError("entry-name-mismatch", CHECK_TREE_SHAPE)
    return _object_id(object_name, CHECK_TREE_SHAPE)


def verify_claim_object(
    commit_id: Any,
    request: Any,
    lease: Any,
    executor: Any,
) -> bool:
    """Return ``True`` when ``commit_id`` says exactly what ``request`` says.

    Raises :class:`ClaimVerificationError` naming the check that refused.  The
    object is expected to be present already in ``lease``; fetching one from
    anywhere is a separate outcome.
    """

    if type(commit_id) is not Sha1ObjectId:
        raise ClaimVerificationError("not-an-object-id", "commit_id")
    if type(request) is not ClaimRequest:
        raise ClaimVerificationError("not-a-claim-request", "request")
    if type(lease) is not BareRepositoryLease:
        raise ClaimVerificationError("not-a-lease", "lease")
    if not callable(getattr(executor, "execute", None)):
        raise ClaimVerificationError("not-an-executor", "executor")

    observed = (
        _require_success(
            run_read_operation(ReadObjectType(commit_id), lease, executor),
            CHECK_OBJECT_TYPE,
        )
        .decode("ascii", "replace")
        .strip()
    )
    if observed != COMMIT_OBJECT_TYPE:
        # Refused before reading it as a commit, so a tree or blob is never
        # parsed as one.
        raise ClaimVerificationError("not-a-commit", CHECK_OBJECT_TYPE)

    raw_commit = _require_success(
        run_read_operation(ReadCommitBytes(commit_id), lease, executor),
        CHECK_COMMIT_SHAPE,
    )
    headers, message = _parse_commit(raw_commit)
    tree_id = _verify_commit_headers(headers, request)

    if message != request.message_bytes():
        raise ClaimVerificationError("message-mismatch", CHECK_COMMIT_MESSAGE)

    blob_id = _verify_tree(
        _require_success(
            run_read_operation(ListTree(tree_id), lease, executor), CHECK_TREE_SHAPE
        )
    )

    payload = _require_success(
        run_read_operation(ReadBlobBytes(blob_id), lease, executor), CHECK_PAYLOAD
    )
    if payload != request.payload_bytes():
        # Byte comparison, not field comparison: two different byte strings can
        # parse to equal values, and it is the bytes that were hashed.
        raise ClaimVerificationError("payload-mismatch", CHECK_PAYLOAD)

    return True


__all__ = [
    "CHECK_COMMIT_IDENTITY",
    "CHECK_COMMIT_MESSAGE",
    "CHECK_COMMIT_SHAPE",
    "CHECK_OBJECT_TYPE",
    "CHECK_PAYLOAD",
    "CHECK_TREE_SHAPE",
    "COMMIT_OBJECT_TYPE",
    "VERIFICATION_CHECKS",
    "ClaimVerificationError",
    "verify_claim_object",
]
