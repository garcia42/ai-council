"""Pure v1 validation for issue-bound implementation ticket contracts.

The review-reference digest is an integrity binding: it proves that a review
reference names these exact contract fields.  It is not authorization and does
not prove that a council or human actually approved the ticket.  Later adapters
must verify that trust decision against their own protected evidence source.

Use :func:`load_ticket_envelope_json` at JSON-text boundaries.  It rejects
duplicate keys and non-strict encodings before calling the mapping validator.
Call :func:`validate_ticket_envelope` directly only for already-parsed trusted
objects whose parser applied equivalent duplicate-key controls.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_ISSUE_NUMBER = 2**63 - 1
MAX_CONTRACT_BYTES = 65_536
MAX_TICKET_JSON_BYTES = 2 * MAX_CONTRACT_BYTES
MAX_LIST_ITEMS = 64
MAX_PATH_LENGTH = 1_024
MAX_REPOSITORY_LENGTH = 256
MAX_RUN_ID_LENGTH = 256
MAX_TARGET_BRANCH_LENGTH = 255
MAX_TEST_COMMAND_LENGTH = 4_096
MAX_TEXT_LENGTH = 8_192
WORK_TYPES = frozenset({"bug", "change", "investigation"})
PRIORITIES = frozenset({"P0", "P1"})
PATH_KINDS = frozenset({"file", "directory"})
ENVELOPE_KEYS = frozenset({"contract", "reviewRef"})
CONTRACT_KEYS = frozenset(
    {
        "schemaVersion",
        "repository",
        "issueNumber",
        "targetBranch",
        "baseCommit",
        "workType",
        "priority",
        "points",
        "problemStatement",
        "acceptanceCriteria",
        "testCommands",
        "allowedPaths",
        "outOfScope",
        "dependencies",
        "rollbackPlan",
    }
)
REVIEW_REF_KEYS = frozenset({"runId", "contractSha256"})
ALLOWED_PATH_KEYS = frozenset({"kind", "path"})

_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_BASE_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:\Z")


class TicketContractError(ValueError):
    """A stable, field-addressed v1 contract validation failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"ticket contract {code} at {field}")


@dataclass(frozen=True)
class AllowedPath:
    """One exact file or segment-bounded directory scope."""

    kind: str
    path: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}

    def allows(self, candidate: str) -> bool:
        if not _is_repository_path(candidate):
            return False
        if self.kind == "file":
            return candidate == self.path
        if self.kind == "directory":
            return candidate.startswith(self.path + "/")
        return False


@dataclass(frozen=True)
class TicketContract:
    """Immutable normalized ticket data; normalization changes containers only."""

    schema_version: int
    repository: str
    issue_number: int
    target_branch: str
    base_commit: str
    work_type: str
    priority: str
    points: int
    problem_statement: str
    acceptance_criteria: tuple[str, ...]
    test_commands: tuple[str, ...]
    allowed_paths: tuple[AllowedPath, ...]
    out_of_scope: tuple[str, ...]
    dependencies: tuple[int, ...]
    rollback_plan: str

    def allows_path(self, candidate: str) -> bool:
        """Return the single normative, case-sensitive v1 scope decision."""

        return any(scope.allows(candidate) for scope in self.allowed_paths)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "repository": self.repository,
            "issueNumber": self.issue_number,
            "targetBranch": self.target_branch,
            "baseCommit": self.base_commit,
            "workType": self.work_type,
            "priority": self.priority,
            "points": self.points,
            "problemStatement": self.problem_statement,
            "acceptanceCriteria": list(self.acceptance_criteria),
            "testCommands": list(self.test_commands),
            "allowedPaths": [scope.as_dict() for scope in self.allowed_paths],
            "outOfScope": list(self.out_of_scope),
            "dependencies": list(self.dependencies),
            "rollbackPlan": self.rollback_plan,
        }


@dataclass(frozen=True)
class TicketReviewRef:
    """An external review reference plus its contract-integrity binding."""

    run_id: str
    contract_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "runId": self.run_id,
            "contractSha256": self.contract_sha256,
        }


@dataclass(frozen=True)
class TicketEnvelope:
    """A validated immutable contract and its integrity-only review reference."""

    contract: TicketContract
    review_ref: TicketReviewRef

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract.as_dict(),
            "reviewRef": self.review_ref.as_dict(),
        }


def canonical_contract_bytes(contract: Mapping[str, Any]) -> bytes:
    """Return the normative v1 canonical JSON bytes for a raw contract object.

    Canonical v1 JSON is UTF-8 encoded, uses NFC values enforced by validation,
    sorts object keys by Python's Unicode code-point ordering, uses compact
    separators, emits Unicode directly, and rejects NaN, infinity, or non-JSON
    types.  The review reference is deliberately outside this digest.
    """

    if not isinstance(contract, Mapping):
        raise TicketContractError("non-canonical-json", "contract")
    try:
        encoded = json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        canonical = encoded.encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TicketContractError("non-canonical-json", "contract") from exc
    if len(canonical) > MAX_CONTRACT_BYTES:
        raise TicketContractError("contract-too-large", "contract")
    return canonical


def contract_sha256(contract: Mapping[str, Any]) -> str:
    """Return the lowercase SHA-256 digest of canonical raw contract bytes."""

    return hashlib.sha256(canonical_contract_bytes(contract)).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TicketContractError("duplicate-json-key", "$")
        result[key] = value
    return result


def _reject_json_constant(_name: str) -> None:
    raise TicketContractError("non-finite-json-number", "$")


def _mapping_with_exact_keys(
    value: Any,
    expected: frozenset[str],
    *,
    code: str,
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TicketContractError(code, field)
    return value


def _canonical_text(value: Any, *, max_length: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= max_length
        and value == value.strip()
        and "\x00" not in value
        and not any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        and unicodedata.normalize("NFC", value) == value
    )


def _require_text(
    value: Any, *, max_length: int, code: str, field: str
) -> str:
    if not _canonical_text(value, max_length=max_length):
        raise TicketContractError(code, field)
    return value


def _require_exact_int(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    code: str,
    field: str,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise TicketContractError(code, field)
    return value


def _require_text_list(
    value: Any,
    *,
    max_item_length: int,
    code: str,
    field: str,
) -> tuple[str, ...]:
    if type(value) is not list or not value or len(value) > MAX_LIST_ITEMS:
        raise TicketContractError(code, field)
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if (
            not _canonical_text(item, max_length=max_item_length)
            or item in seen
        ):
            raise TicketContractError(code, field)
        seen.add(item)
        result.append(item)
    return tuple(result)


def _is_repository_path(value: Any) -> bool:
    if not _canonical_text(value, max_length=MAX_PATH_LENGTH):
        return False
    if value.startswith("/") or value.endswith("/") or "\\" in value:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    pieces = value.split("/")
    if any(
        piece in {"", ".", ".."} or piece.casefold() == ".git"
        for piece in pieces
    ):
        return False
    if _WINDOWS_DRIVE_RE.fullmatch(pieces[0]):
        return False
    return True


def _allowed_paths(value: Any) -> tuple[AllowedPath, ...]:
    field = "contract.allowedPaths"
    if type(value) is not list or not value or len(value) > MAX_LIST_ITEMS:
        raise TicketContractError("invalid-allowed-paths", field)
    result: list[AllowedPath] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        item_field = f"{field}[{index}]"
        item = _mapping_with_exact_keys(
            item,
            ALLOWED_PATH_KEYS,
            code="invalid-allowed-path-keys",
            field=item_field,
        )
        kind = item["kind"]
        if type(kind) is not str or kind not in PATH_KINDS:
            raise TicketContractError(
                "invalid-allowed-path-kind", f"{item_field}.kind"
            )
        path = item["path"]
        if not _is_repository_path(path):
            raise TicketContractError("invalid-allowed-path", f"{item_field}.path")
        identity = (kind, path)
        if identity in seen:
            raise TicketContractError("duplicate-allowed-path", item_field)
        seen.add(identity)
        result.append(AllowedPath(kind=kind, path=path))
    return tuple(result)


def _dependencies(value: Any, issue_number: int) -> tuple[int, ...]:
    field = "contract.dependencies"
    if type(value) is not list or len(value) > MAX_LIST_ITEMS:
        raise TicketContractError("invalid-dependencies", field)
    result: list[int] = []
    seen: set[int] = set()
    for index, dependency in enumerate(value):
        item_field = f"{field}[{index}]"
        if type(dependency) is not int or not 1 <= dependency <= MAX_ISSUE_NUMBER:
            raise TicketContractError("invalid-dependency", item_field)
        if dependency == issue_number:
            raise TicketContractError("self-dependency", item_field)
        if dependency in seen:
            raise TicketContractError("duplicate-dependency", item_field)
        seen.add(dependency)
        result.append(dependency)
    return tuple(result)


def _is_target_branch(value: Any) -> bool:
    if not _canonical_text(value, max_length=MAX_TARGET_BRANCH_LENGTH):
        return False
    if (
        value == "@"
        or value.startswith(("/", "-"))
        or value.endswith(("/", "."))
    ):
        return False
    if ".." in value or "//" in value or "@{" in value:
        return False
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in " ~^:?*[\\"
        for character in value
    ):
        return False
    pieces = value.split("/")
    return not any(
        not piece or piece.startswith(".") or piece.endswith(".lock")
        for piece in pieces
    )


def validate_ticket_envelope(value: Mapping[str, Any]) -> TicketEnvelope:
    """Validate a parsed v1 envelope and return a deeply immutable value.

    Checks run in fixed field order, so equivalent mappings fail with the same
    stable code and field regardless of caller key insertion order.
    """

    envelope = _mapping_with_exact_keys(
        value, ENVELOPE_KEYS, code="invalid-envelope-keys", field="$"
    )
    raw_contract_candidate = envelope["contract"]
    if not isinstance(raw_contract_candidate, Mapping):
        raise TicketContractError("invalid-contract-keys", "contract")
    if "schemaVersion" in raw_contract_candidate:
        schema_version = raw_contract_candidate["schemaVersion"]
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise TicketContractError(
                "unsupported-schema-version", "contract.schemaVersion"
            )

    raw_contract = _mapping_with_exact_keys(
        raw_contract_candidate,
        CONTRACT_KEYS,
        code="invalid-contract-keys",
        field="contract",
    )

    schema_version = raw_contract["schemaVersion"]

    repository = _require_text(
        raw_contract["repository"],
        max_length=MAX_REPOSITORY_LENGTH,
        code="invalid-repository",
        field="contract.repository",
    )
    if (
        not _REPOSITORY_RE.fullmatch(repository)
        or any(piece in {".", ".."} for piece in repository.split("/"))
    ):
        raise TicketContractError("invalid-repository", "contract.repository")

    issue_number = _require_exact_int(
        raw_contract["issueNumber"],
        minimum=1,
        maximum=MAX_ISSUE_NUMBER,
        code="invalid-issue-number",
        field="contract.issueNumber",
    )

    target_branch = raw_contract["targetBranch"]
    if not _is_target_branch(target_branch):
        raise TicketContractError(
            "invalid-target-branch", "contract.targetBranch"
        )

    base_commit = raw_contract["baseCommit"]
    if type(base_commit) is not str or not _BASE_COMMIT_RE.fullmatch(base_commit):
        raise TicketContractError("invalid-base-commit", "contract.baseCommit")

    work_type = raw_contract["workType"]
    if type(work_type) is not str or work_type not in WORK_TYPES:
        raise TicketContractError("invalid-work-type", "contract.workType")

    priority = raw_contract["priority"]
    if type(priority) is not str or priority not in PRIORITIES:
        raise TicketContractError("invalid-priority", "contract.priority")

    points = _require_exact_int(
        raw_contract["points"],
        minimum=1,
        maximum=3,
        code="invalid-points",
        field="contract.points",
    )

    problem_statement = _require_text(
        raw_contract["problemStatement"],
        max_length=MAX_TEXT_LENGTH,
        code="invalid-problem-statement",
        field="contract.problemStatement",
    )
    acceptance_criteria = _require_text_list(
        raw_contract["acceptanceCriteria"],
        max_item_length=MAX_TEXT_LENGTH,
        code="invalid-acceptance-criteria",
        field="contract.acceptanceCriteria",
    )
    test_commands = _require_text_list(
        raw_contract["testCommands"],
        max_item_length=MAX_TEST_COMMAND_LENGTH,
        code="invalid-test-commands",
        field="contract.testCommands",
    )
    allowed_paths = _allowed_paths(raw_contract["allowedPaths"])
    out_of_scope = _require_text_list(
        raw_contract["outOfScope"],
        max_item_length=MAX_TEXT_LENGTH,
        code="invalid-out-of-scope",
        field="contract.outOfScope",
    )
    dependencies = _dependencies(raw_contract["dependencies"], issue_number)
    rollback_plan = _require_text(
        raw_contract["rollbackPlan"],
        max_length=MAX_TEXT_LENGTH,
        code="invalid-rollback-plan",
        field="contract.rollbackPlan",
    )

    raw_review_ref = _mapping_with_exact_keys(
        envelope["reviewRef"],
        REVIEW_REF_KEYS,
        code="invalid-review-ref-keys",
        field="reviewRef",
    )
    run_id = _require_text(
        raw_review_ref["runId"],
        max_length=MAX_RUN_ID_LENGTH,
        code="invalid-run-id",
        field="reviewRef.runId",
    )
    digest = raw_review_ref["contractSha256"]
    if type(digest) is not str or not _SHA256_RE.fullmatch(digest):
        raise TicketContractError(
            "invalid-contract-sha256", "reviewRef.contractSha256"
        )
    if contract_sha256(raw_contract) != digest:
        raise TicketContractError(
            "contract-sha256-mismatch", "reviewRef.contractSha256"
        )

    return TicketEnvelope(
        contract=TicketContract(
            schema_version=schema_version,
            repository=repository,
            issue_number=issue_number,
            target_branch=target_branch,
            base_commit=base_commit,
            work_type=work_type,
            priority=priority,
            points=points,
            problem_statement=problem_statement,
            acceptance_criteria=acceptance_criteria,
            test_commands=test_commands,
            allowed_paths=allowed_paths,
            out_of_scope=out_of_scope,
            dependencies=dependencies,
            rollback_plan=rollback_plan,
        ),
        review_ref=TicketReviewRef(
            run_id=run_id,
            contract_sha256=digest,
        ),
    )


def load_ticket_envelope_json(document: Any) -> TicketEnvelope:
    """Strictly decode and validate one v1 ticket-envelope JSON document.

    Bytes are decoded as plain strict UTF-8 before JSON parsing, so Python's
    more permissive bytes autodetection cannot admit BOM-stripped UTF-16/32 or
    surrogate-pass input.  Loader failures use fixed non-reflective codes at
    ``$``; field-specific contract errors pass through unchanged.
    """

    if type(document) is str:
        if len(document) > MAX_TICKET_JSON_BYTES:
            raise TicketContractError("ticket-json-too-large", "$")
        try:
            encoded_length = len(document.encode("utf-8"))
        except UnicodeEncodeError:
            raise TicketContractError("invalid-json-encoding", "$") from None
        if encoded_length > MAX_TICKET_JSON_BYTES:
            raise TicketContractError("ticket-json-too-large", "$")
        text = document
    elif type(document) is bytes or type(document) is bytearray:
        if len(document) > MAX_TICKET_JSON_BYTES:
            raise TicketContractError("ticket-json-too-large", "$")
        try:
            text = bytes(document).decode("utf-8")
        except UnicodeDecodeError:
            raise TicketContractError("invalid-json-encoding", "$") from None
    else:
        raise TicketContractError("invalid-json-type", "$")

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except TicketContractError:
        raise
    except RecursionError:
        raise TicketContractError("ticket-json-too-deep", "$") from None
    except ValueError:
        raise TicketContractError("invalid-json", "$") from None

    if type(parsed) is not dict:
        raise TicketContractError("invalid-json-top-level", "$")
    return validate_ticket_envelope(parsed)


__all__ = [
    "AllowedPath",
    "MAX_CONTRACT_BYTES",
    "MAX_ISSUE_NUMBER",
    "MAX_LIST_ITEMS",
    "MAX_PATH_LENGTH",
    "MAX_REPOSITORY_LENGTH",
    "MAX_RUN_ID_LENGTH",
    "MAX_TARGET_BRANCH_LENGTH",
    "MAX_TICKET_JSON_BYTES",
    "MAX_TEST_COMMAND_LENGTH",
    "MAX_TEXT_LENGTH",
    "TicketContract",
    "TicketContractError",
    "TicketEnvelope",
    "TicketReviewRef",
    "canonical_contract_bytes",
    "contract_sha256",
    "load_ticket_envelope_json",
    "validate_ticket_envelope",
]
