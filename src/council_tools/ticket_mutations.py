"""Pure policy for declared ticket mutations and normalized Git diff entries.

This module compares already-normalized, caller-supplied records.  It does not
parse Git output, inspect a repository, or authorize execution.  In particular,
``MutationDecision.allowed`` is only a structural policy result; boolean
coercion is refused so it cannot silently become an authorization check.

Case-collision checks cover only paths present in one request.  Collisions with
paths elsewhere in a repository require a later adapter with protected access
to the actual tree.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from council_tools.ticket_contracts import (
    MAX_LIST_ITEMS,
    MAX_PATH_LENGTH,
    PATH_KINDS,
    AllowedPath,
    TicketContract,
)


MAX_MUTATIONS = MAX_LIST_ITEMS
MAX_DIFF_ENTRIES = MAX_LIST_ITEMS
MAX_FINDINGS = MAX_LIST_ITEMS

MUTATION_OPERATIONS = frozenset(
    {"create", "modify", "delete", "rename", "copy"}
)
DIFF_STATUSES = frozenset({"A", "M", "D", "R", "C", "T"})
ENTRY_KINDS = frozenset({"regular", "symlink", "gitlink"})

REASON_CODES = (
    "invalid-contract",
    "invalid-request",
    "target-branch-mismatch",
    "base-commit-malformed",
    "base-commit-mismatch",
    "too-many-mutations",
    "too-many-diff-entries",
    "unknown-diff-status",
    "invalid-diff-fields",
    "unsupported-entry-kind",
    "type-change-forbidden",
    "path-not-allowed",
    "source-equals-destination",
    "duplicate-path-mutation",
    "case-collision",
    "undeclared-diff-entry",
    "unsubstantiated-mutation",
    "findings-truncated",
)
_REASON_RANK = {code: index for index, code in enumerate(REASON_CODES)}
_BASE_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class NormalizedMutation:
    """One caller-normalized intended repository mutation."""

    op: str
    path: str
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedDiffEntry:
    """One caller-normalized Git diff entry; no raw Git parsing occurs here."""

    status: str
    old_path: str | None
    new_path: str | None
    old_kind: str | None
    new_kind: str | None


@dataclass(frozen=True, slots=True)
class MutationRequest:
    """One point-in-time structural comparison request."""

    target_branch: str
    base_commit: str
    mutations: tuple[NormalizedMutation, ...]
    diff_entries: tuple[NormalizedDiffEntry, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    """One stable, non-reflective policy finding."""

    code: str
    path: str = ""
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class MutationDecision:
    """A structural policy result that is deliberately not authorization."""

    allowed: bool
    findings: tuple[Finding, ...]

    def __bool__(self) -> bool:
        raise TypeError("mutation policy result is not authorization")


@dataclass(frozen=True, slots=True)
class _ContractView:
    target_branch: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class _RequestView:
    target_branch: str
    base_commit: str
    mutations: tuple[NormalizedMutation, ...]
    diff_entries: tuple[NormalizedDiffEntry, ...]


@dataclass(frozen=True, slots=True)
class _DiffView:
    status: str
    old_path: str | None
    new_path: str | None
    old_kind: str | None
    new_kind: str | None


MutationKey = tuple[str, str, str]


def _contract_view(value: Any) -> _ContractView | None:
    if type(value) is not TicketContract:
        return None
    try:
        target_branch = value.target_branch
        base_commit = value.base_commit
        allowed_paths = value.allowed_paths
    except Exception:
        return None
    if type(target_branch) is not str or type(base_commit) is not str:
        return None
    if not _BASE_COMMIT_RE.fullmatch(base_commit):
        return None
    if (
        type(allowed_paths) is not tuple
        or not allowed_paths
        or len(allowed_paths) > MAX_LIST_ITEMS
    ):
        return None
    try:
        for scope in allowed_paths:
            if (
                type(scope) is not AllowedPath
                or type(scope.kind) is not str
                or scope.kind not in PATH_KINDS
                or type(scope.path) is not str
            ):
                return None
    except Exception:
        return None
    return _ContractView(
        target_branch=target_branch,
        base_commit=base_commit,
    )


def _request_view(value: Any) -> _RequestView | None:
    if type(value) is not MutationRequest:
        return None
    try:
        target_branch = value.target_branch
        base_commit = value.base_commit
        mutations = value.mutations
        diff_entries = value.diff_entries
    except Exception:
        return None
    if type(target_branch) is not str or type(base_commit) is not str:
        return None
    if type(mutations) is not tuple or type(diff_entries) is not tuple:
        return None
    return _RequestView(
        target_branch=target_branch,
        base_commit=base_commit,
        mutations=mutations,
        diff_entries=diff_entries,
    )


def _mutation_keys(
    values: tuple[NormalizedMutation, ...],
) -> tuple[list[MutationKey], list[str]] | None:
    keys: list[MutationKey] = []
    paths: list[str] = []
    try:
        for value in values:
            if type(value) is not NormalizedMutation:
                return None
            op = value.op
            path = value.path
            source_path = value.source_path
            if (
                type(op) is not str
                or op not in MUTATION_OPERATIONS
                or type(path) is not str
                or type(source_path) is not str
            ):
                return None
            if (op in {"rename", "copy"}) != bool(source_path):
                return None
            keys.append((op, path, source_path))
            paths.append(path)
            if source_path:
                paths.append(source_path)
    except Exception:
        return None
    return keys, paths


def _diff_views(
    values: tuple[NormalizedDiffEntry, ...],
) -> tuple[list[_DiffView], list[str]] | None:
    result: list[_DiffView] = []
    paths: list[str] = []
    try:
        for value in values:
            if type(value) is not NormalizedDiffEntry:
                return None
            status = value.status
            old_path = value.old_path
            new_path = value.new_path
            old_kind = value.old_kind
            new_kind = value.new_kind
            if type(status) is not str:
                return None
            if old_path is not None and type(old_path) is not str:
                return None
            if new_path is not None and type(new_path) is not str:
                return None
            if old_kind is not None and type(old_kind) is not str:
                return None
            if new_kind is not None and type(new_kind) is not str:
                return None
            result.append(
                _DiffView(
                    status=status,
                    old_path=old_path,
                    new_path=new_path,
                    old_kind=old_kind,
                    new_kind=new_kind,
                )
            )
            if old_path is not None:
                paths.append(old_path)
            if new_path is not None:
                paths.append(new_path)
    except Exception:
        return None
    return result, paths


def _anchor(entry: _DiffView) -> tuple[str, str]:
    path = entry.new_path if entry.new_path is not None else entry.old_path
    source = ""
    if (
        entry.old_path is not None
        and entry.new_path is not None
        and entry.old_path != entry.new_path
    ):
        source = entry.old_path
    return path or "", source


def _valid_diff_shape(entry: _DiffView) -> bool:
    if entry.status == "A":
        return (
            entry.old_path is None
            and entry.old_kind is None
            and entry.new_path is not None
            and entry.new_kind is not None
        )
    if entry.status == "D":
        return (
            entry.old_path is not None
            and entry.old_kind is not None
            and entry.new_path is None
            and entry.new_kind is None
        )
    if entry.status == "M":
        return (
            entry.old_path is not None
            and entry.new_path is not None
            and entry.old_path == entry.new_path
            and entry.old_kind is not None
            and entry.new_kind is not None
        )
    if entry.status in {"R", "C"}:
        return (
            entry.old_path is not None
            and entry.new_path is not None
            and entry.old_kind is not None
            and entry.new_kind is not None
        )
    return False


def _projection(entry: _DiffView) -> MutationKey:
    if entry.status == "A":
        return ("create", entry.new_path or "", "")
    if entry.status == "M":
        return ("modify", entry.new_path or "", "")
    if entry.status == "D":
        return ("delete", entry.old_path or "", "")
    if entry.status == "R":
        return ("rename", entry.new_path or "", entry.old_path or "")
    return ("copy", entry.new_path or "", entry.old_path or "")


def _applicable_fields(
    entry: _DiffView,
) -> tuple[tuple[str, str], ...]:
    if entry.status == "A":
        return ((entry.new_path or "", entry.new_kind or ""),)
    if entry.status == "D":
        return ((entry.old_path or "", entry.old_kind or ""),)
    if entry.status == "M":
        return (
            (entry.new_path or "", entry.old_kind or ""),
            (entry.new_path or "", entry.new_kind or ""),
        )
    return (
        (entry.old_path or "", entry.old_kind or ""),
        (entry.new_path or "", entry.new_kind or ""),
    )


def _allowed_path(contract: TicketContract, path: str) -> bool | None:
    try:
        result = TicketContract.allows_path(contract, path)
    except Exception:
        return None
    return result if type(result) is bool else None


def _finding_sort_key(finding: Finding) -> tuple[int, str, str]:
    return (_REASON_RANK[finding.code], finding.path, finding.source_path)


def _decision(findings: Iterable[Finding]) -> MutationDecision:
    ordered = sorted(set(findings), key=_finding_sort_key)
    if len(ordered) > MAX_FINDINGS:
        ordered = ordered[: MAX_FINDINGS - 1]
        ordered.append(Finding("findings-truncated"))
    return MutationDecision(allowed=not ordered, findings=tuple(ordered))


def evaluate_ticket_mutations(
    contract: Any, request: Any
) -> MutationDecision:
    """Return one total structural policy decision for normalized local inputs."""

    contract_view = _contract_view(contract)
    if contract_view is None:
        return _decision((Finding("invalid-contract"),))
    request_view = _request_view(request)
    if request_view is None:
        return _decision((Finding("invalid-request"),))

    findings: list[Finding] = []
    if request_view.target_branch != contract_view.target_branch:
        findings.append(Finding("target-branch-mismatch"))
    if not _BASE_COMMIT_RE.fullmatch(request_view.base_commit):
        findings.append(Finding("base-commit-malformed"))
    elif request_view.base_commit != contract_view.base_commit:
        findings.append(Finding("base-commit-mismatch"))

    too_many_mutations = len(request_view.mutations) > MAX_MUTATIONS
    too_many_diff_entries = len(request_view.diff_entries) > MAX_DIFF_ENTRIES
    if too_many_mutations:
        findings.append(Finding("too-many-mutations"))
    if too_many_diff_entries:
        findings.append(Finding("too-many-diff-entries"))
    if too_many_mutations or too_many_diff_entries:
        return _decision(findings)

    mutation_data = _mutation_keys(request_view.mutations)
    diff_data = _diff_views(request_view.diff_entries)
    if mutation_data is None or diff_data is None:
        return _decision((Finding("invalid-request"),))
    mutation_keys, mutation_paths = mutation_data
    diff_entries, diff_paths = diff_data

    for path, count in Counter(key[1] for key in mutation_keys).items():
        if count > 1:
            findings.append(Finding("duplicate-path-mutation", path=path))

    case_groups: dict[str, set[str]] = {}
    for path in mutation_paths + diff_paths:
        if len(path) <= MAX_PATH_LENGTH:
            case_groups.setdefault(path.casefold(), set()).add(path)
    for paths in case_groups.values():
        if len(paths) > 1:
            for path in paths:
                findings.append(Finding("case-collision", path=path))

    diff_keys: list[MutationKey] = []
    for entry in diff_entries:
        path, source_path = _anchor(entry)
        if entry.status not in DIFF_STATUSES:
            findings.append(
                Finding(
                    "unknown-diff-status",
                    path=path,
                    source_path=source_path,
                )
            )
            continue
        if entry.status == "T":
            findings.append(
                Finding(
                    "type-change-forbidden",
                    path=path,
                    source_path=source_path,
                )
            )
            continue
        if not _valid_diff_shape(entry):
            findings.append(
                Finding(
                    "invalid-diff-fields",
                    path=path,
                    source_path=source_path,
                )
            )
            continue

        diff_keys.append(_projection(entry))
        if (
            entry.status in {"R", "C"}
            and entry.old_path == entry.new_path
        ):
            findings.append(
                Finding(
                    "source-equals-destination",
                    path=entry.new_path or "",
                    source_path=entry.old_path or "",
                )
            )

        checked_paths: set[str] = set()
        for applicable_path, kind in _applicable_fields(entry):
            if kind not in ENTRY_KINDS or kind != "regular":
                findings.append(
                    Finding(
                        "unsupported-entry-kind",
                        path=applicable_path,
                        source_path=(
                            entry.old_path or ""
                            if entry.status in {"R", "C"}
                            else ""
                        ),
                    )
                )
            if applicable_path in checked_paths:
                continue
            checked_paths.add(applicable_path)
            allowed = _allowed_path(contract, applicable_path)
            if allowed is None:
                return _decision((Finding("invalid-contract"),))
            if not allowed:
                findings.append(
                    Finding(
                        "path-not-allowed",
                        path=applicable_path,
                        source_path=(
                            entry.old_path or ""
                            if entry.status in {"R", "C"}
                            else ""
                        ),
                    )
                )

    declared = Counter(mutation_keys)
    observed = Counter(diff_keys)
    for key in observed.keys() - declared.keys():
        findings.append(
            Finding("undeclared-diff-entry", path=key[1], source_path=key[2])
        )
    for key in declared.keys() - observed.keys():
        findings.append(
            Finding("unsubstantiated-mutation", path=key[1], source_path=key[2])
        )
    for key in observed.keys() & declared.keys():
        if observed[key] > declared[key]:
            findings.append(
                Finding(
                    "undeclared-diff-entry", path=key[1], source_path=key[2]
                )
            )
        elif declared[key] > observed[key]:
            findings.append(
                Finding(
                    "unsubstantiated-mutation",
                    path=key[1],
                    source_path=key[2],
                )
            )

    return _decision(findings)


__all__ = [
    "DIFF_STATUSES",
    "ENTRY_KINDS",
    "MAX_DIFF_ENTRIES",
    "MAX_FINDINGS",
    "MAX_MUTATIONS",
    "MUTATION_OPERATIONS",
    "REASON_CODES",
    "Finding",
    "MutationDecision",
    "MutationRequest",
    "NormalizedDiffEntry",
    "NormalizedMutation",
    "evaluate_ticket_mutations",
]
