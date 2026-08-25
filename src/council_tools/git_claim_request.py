"""One validated claim request, and its canonical rendering.

The write builders that construct a claim's blob, tree and commit take their
inputs already validated and already canonical, and nothing produces those
inputs.  :class:`~council_tools.git_local_write_operations.WriteCanonicalBlob`
accepts arbitrary ``content: bytes``.  :class:`CreateZeroParentCommit` accepts a
message and six identity strings and checks only their shape.

So the bytes that decide what a claim *says* -- and therefore what its object
name *is* -- have no origin.  Two callers can assemble them differently and get
different objects for the same claim.  That defeats the reason the commit builder
refuses to read a clock or an environment: a claim's object name must depend on
what the claim says, not on when or where it was made.  Two agents that agree on
the facts have to compute the same name, or a compare-and-set on that name means
nothing.

This module is that origin.  Three properties carry the weight:

* **Exactness.**  Every field ends up either inside the hashed payload or inside
  an argument vector, so validation is a barrier rather than advice.  It runs in
  ``__post_init__``, which means there is no unchecked way to build a request.
* **Determinism.**  No clock, no environment, no filesystem, no repository.
  Every varying value is a caller-supplied parameter, so identical inputs give
  byte-identical output in any process, in any order, on any host.
* **Closure.**  The payload's key set is fixed and the renderer builds it from
  the record's own fields.  A payload assembled from a caller-supplied mapping
  would be a payload whose meaning a caller can extend after the fact.

**It decides nothing about ownership.**  Whether a claim may be taken, whether
one already exists, and what happens when two are attempted belong to the
protocol above this node.  Answering any of them here would bury an
authorization decision inside a value constructor, where nothing can see it.
Constructing a :class:`ClaimRequest` is not evidence that anything is claimed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

#: The payload's key set is fixed here and nowhere else.  The renderer builds
#: exactly these keys from the record's own fields, so there is no parameter
#: through which a caller could add one.
CLAIM_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"schemaVersion", "repository", "issueNumber", "contractSha256", "holder", "issuedAt"}
)

CLAIM_PAYLOAD_SCHEMA_VERSION = 1

#: Git's own bound on an issue number is far larger than anything meaningful
#: here; this bound exists so a value cannot be arbitrarily long in the payload.
MAX_ISSUE_NUMBER = 1_000_000
MAX_TEXT_LENGTH = 256

#: ``@<seconds> <offset>`` is the form the zero-parent commit builder accepts.
#: A bare ``0 +0000`` is rejected by the pinned binary; the leading ``@`` is not
#: decoration.  Kept identical to the builder's rule rather than looser, so a
#: request that validates here cannot fail there.
_ISSUED_AT_RE = re.compile(r"\A@[0-9]{1,20} [+-][0-9]{4}\Z")

_CONTRACT_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

#: ``<`` and ``>`` delimit an identity in Git's own commit format, and the
#: control characters would terminate or split a line.  Rejected rather than
#: escaped: an escaped identity is one whose rendering depends on the escaping.
_FORBIDDEN_CHARACTERS = ("\x00", "\r", "\n", "<", ">")

#: A holder is rendered into both the payload and the commit identity, so it
#: carries a fixed address rather than a caller-chosen one.  The domain is
#: reserved for exactly this use by RFC 2606.
HOLDER_EMAIL_DOMAIN = "claims.invalid"


class ClaimRequestError(ValueError):
    """A stable, field-addressed claim-request validation failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"claim request {code} at {field}")


def _require_text(value: Any, field: str) -> str:
    if type(value) is not str:
        raise ClaimRequestError("invalid-type", field)
    if not value:
        raise ClaimRequestError("empty", field)
    if len(value) > MAX_TEXT_LENGTH:
        raise ClaimRequestError("too-long", field)
    if any(character in value for character in _FORBIDDEN_CHARACTERS):
        raise ClaimRequestError("forbidden-character", field)
    if value.strip() != value:
        raise ClaimRequestError("surrounding-whitespace", field)
    return value


@dataclass(frozen=True)
class ClaimRequest:
    """What is being claimed, validated on construction and nothing more.

    Frozen and compares by value, so two agents holding the same facts hold
    equal requests and render identical bytes.
    """

    repository: str
    issue_number: int
    contract_sha256: str
    holder: str
    issued_at: str

    def __post_init__(self) -> None:
        _require_text(self.repository, "repository")
        # ``bool`` is an ``int`` subclass and is not an issue number.
        if type(self.issue_number) is not int:
            raise ClaimRequestError("invalid-type", "issue_number")
        if self.issue_number < 1 or self.issue_number > MAX_ISSUE_NUMBER:
            raise ClaimRequestError("out-of-range", "issue_number")
        digest = _require_text(self.contract_sha256, "contract_sha256")
        if _CONTRACT_SHA256_RE.fullmatch(digest) is None:
            # Lowercase and exact: an uppercase or truncated digest would render
            # a different payload for the same contract.
            raise ClaimRequestError("invalid-digest", "contract_sha256")
        _require_text(self.holder, "holder")
        issued = _require_text(self.issued_at, "issued_at")
        if _ISSUED_AT_RE.fullmatch(issued) is None:
            raise ClaimRequestError("invalid-issued-at", "issued_at")

    # -- rendering -------------------------------------------------------

    def payload_mapping(self) -> dict[str, Any]:
        """The payload's fields, built from this record and from nothing else."""

        mapping = {
            "schemaVersion": CLAIM_PAYLOAD_SCHEMA_VERSION,
            "repository": self.repository,
            "issueNumber": self.issue_number,
            "contractSha256": self.contract_sha256,
            "holder": self.holder,
            "issuedAt": self.issued_at,
        }
        # A guard on the module's own renderer, not on caller input: adding a
        # field to the record without adding it to CLAIM_PAYLOAD_KEYS would
        # silently change every claim's object name.
        if frozenset(mapping) != CLAIM_PAYLOAD_KEYS:
            raise ClaimRequestError("payload-key-set-changed", "payload")
        return mapping

    def payload_bytes(self) -> bytes:
        """The canonical payload: sorted keys, no whitespace, UTF-8, no newline.

        ``ensure_ascii`` is left at its default so any non-ASCII character is
        escaped rather than emitted, which keeps the bytes identical regardless
        of the encoding a reader assumes.
        """

        return json.dumps(
            self.payload_mapping(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def message_bytes(self) -> bytes:
        """The commit message, derived rather than supplied.

        A caller-supplied message would be a second place the claim's meaning
        could differ while the payload stayed equal.
        """

        return f"claim {self.repository}#{self.issue_number}\n".encode("utf-8")

    def holder_email(self) -> str:
        return f"{self.holder}@{HOLDER_EMAIL_DOMAIN}"

    def commit_identity(self) -> Mapping[str, str]:
        """The six values the zero-parent commit builder requires.

        Author and committer are identical: a claim has one party, and letting
        them differ would admit a distinction the protocol does not have.
        """

        return {
            "author_name": self.holder,
            "author_email": self.holder_email(),
            "author_date": self.issued_at,
            "committer_name": self.holder,
            "committer_email": self.holder_email(),
            "committer_date": self.issued_at,
        }


__all__ = [
    "CLAIM_PAYLOAD_KEYS",
    "CLAIM_PAYLOAD_SCHEMA_VERSION",
    "HOLDER_EMAIL_DOMAIN",
    "MAX_ISSUE_NUMBER",
    "MAX_TEXT_LENGTH",
    "ClaimRequest",
    "ClaimRequestError",
]
