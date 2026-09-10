"""Pure initiative-level scope controls for bounded implementation programs.

Ticket sizing limits one independently shippable change.  It cannot prevent a
large architecture from being decomposed into an unbounded sequence of valid
small tickets.  This module adds the missing aggregate boundary: a reviewed
initiative envelope and caller-observed cumulative progress.

The values here establish structural scope, not authority.  In particular, a
digest proves which envelope was assessed; it does not authenticate the person
who approved that envelope or the adapter that measured cumulative progress.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_TEXT_LENGTH = 8_192
MAX_LIST_ITEMS = 64
MAX_COUNTER = 2**63 - 1

RUNTIME_COMPONENT_KINDS = frozenset(
    {
        "daemon",
        "systemd-unit",
        "schema",
        "journal",
        "ipc-protocol",
        "general-framework",
        "runtime-dependency",
    }
)
FINDING_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
FINDING_CLASSIFICATIONS = frozenset(
    {
        "DIRECT",
        "INDUCED_BY_DESIGN",
        "GOVERNANCE",
        "POST_CANARY_HARDENING",
    }
)
FINDING_DISPOSITIONS = frozenset(
    {
        "BLOCKS_CANARY",
        "ACCEPTED_RESIDUAL_RISK",
        "POST_CANARY_BACKLOG",
        "REJECTED",
        "REQUIRES_PRINCIPAL_SCOPE_CHANGE",
    }
)

INITIATIVE_SCOPE_KEYS = frozenset(
    {
        "schemaVersion",
        "initiativeId",
        "scopeRevision",
        "objective",
        "canarySuccess",
        "nonGoals",
        "maxProductionLinesAdded",
        "maxProductionFilesChanged",
        "maxTickets",
        "maxEngineerDays",
        "allowedNewRuntimeComponents",
    }
)
SCOPE_FINDING_KEYS = frozenset(
    {
        "findingId",
        "severity",
        "classification",
        "observedEvidence",
        "canaryFailure",
        "maximumConsequence",
        "existingControlsGap",
        "smallestMitigation",
        "acceptanceTest",
        "disposition",
    }
)
INITIATIVE_PROGRESS_KEYS = frozenset(
    {
        "initiativeId",
        "scopeRevision",
        "initiativeScopeSha256",
        "cumulativeTickets",
        "cumulativeEngineerDays",
        "cumulativeProductionLinesAdded",
        "cumulativeProductionFilesChanged",
        "newRuntimeComponents",
        "blockersOpened",
        "blockersClosed",
        "consecutiveGrowingCheckpoints",
        "findings",
    }
)

SCOPE_REASON_CODES = (
    "initiative-identity-mismatch",
    "initiative-scope-digest-mismatch",
    "initiative-ticket-budget-exceeded",
    "initiative-engineer-days-budget-exceeded",
    "initiative-production-lines-budget-exceeded",
    "initiative-production-files-budget-exceeded",
    "initiative-runtime-component-not-allowed",
    "initiative-critical-path-growing",
    "initiative-principal-scope-change-required",
)


class InitiativeScopeError(ValueError):
    """Stable field-addressed validation failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"initiative scope {code} at {field}")


@dataclass(frozen=True)
class InitiativeScope:
    schema_version: int
    initiative_id: str
    scope_revision: int
    objective: str
    canary_success: tuple[str, ...]
    non_goals: tuple[str, ...]
    max_production_lines_added: int
    max_production_files_changed: int
    max_tickets: int
    max_engineer_days: int
    allowed_new_runtime_components: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "initiativeId": self.initiative_id,
            "scopeRevision": self.scope_revision,
            "objective": self.objective,
            "canarySuccess": list(self.canary_success),
            "nonGoals": list(self.non_goals),
            "maxProductionLinesAdded": self.max_production_lines_added,
            "maxProductionFilesChanged": self.max_production_files_changed,
            "maxTickets": self.max_tickets,
            "maxEngineerDays": self.max_engineer_days,
            "allowedNewRuntimeComponents": list(
                self.allowed_new_runtime_components
            ),
        }


@dataclass(frozen=True)
class ScopeFinding:
    finding_id: str
    severity: str
    classification: str
    observed_evidence: str
    canary_failure: str
    maximum_consequence: str
    existing_controls_gap: str
    smallest_mitigation: str
    acceptance_test: str
    disposition: str

    def as_dict(self) -> dict[str, str]:
        return {
            "findingId": self.finding_id,
            "severity": self.severity,
            "classification": self.classification,
            "observedEvidence": self.observed_evidence,
            "canaryFailure": self.canary_failure,
            "maximumConsequence": self.maximum_consequence,
            "existingControlsGap": self.existing_controls_gap,
            "smallestMitigation": self.smallest_mitigation,
            "acceptanceTest": self.acceptance_test,
            "disposition": self.disposition,
        }


@dataclass(frozen=True)
class InitiativeProgress:
    initiative_id: str
    scope_revision: int
    initiative_scope_sha256: str
    cumulative_tickets: int
    cumulative_engineer_days: int
    cumulative_production_lines_added: int
    cumulative_production_files_changed: int
    new_runtime_components: tuple[str, ...]
    blockers_opened: int
    blockers_closed: int
    consecutive_growing_checkpoints: int
    findings: tuple[ScopeFinding, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "initiativeId": self.initiative_id,
            "scopeRevision": self.scope_revision,
            "initiativeScopeSha256": self.initiative_scope_sha256,
            "cumulativeTickets": self.cumulative_tickets,
            "cumulativeEngineerDays": self.cumulative_engineer_days,
            "cumulativeProductionLinesAdded": (
                self.cumulative_production_lines_added
            ),
            "cumulativeProductionFilesChanged": (
                self.cumulative_production_files_changed
            ),
            "newRuntimeComponents": list(self.new_runtime_components),
            "blockersOpened": self.blockers_opened,
            "blockersClosed": self.blockers_closed,
            "consecutiveGrowingCheckpoints": (
                self.consecutive_growing_checkpoints
            ),
            "findings": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class InitiativeScopeDecision:
    """A scope decision that deliberately cannot authorize work."""

    scope_review_required: bool
    reasons: tuple[str, ...]

    def __bool__(self) -> bool:
        raise TypeError("initiative scope assessment is not authorization")


def _canonical_text(value: Any) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= MAX_TEXT_LENGTH
        and value == value.strip()
        and "\x00" not in value
        and not any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        and unicodedata.normalize("NFC", value) == value
    )


def _text(value: Any, field: str) -> str:
    if not _canonical_text(value):
        raise InitiativeScopeError("invalid-text", field)
    return value


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_COUNTER:
        raise InitiativeScopeError("invalid-positive-integer", field)
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise InitiativeScopeError("invalid-nonnegative-integer", field)
    return value


def _text_list(value: Any, field: str, *, permit_empty: bool = False) -> tuple[str, ...]:
    if (
        type(value) is not list
        or len(value) > MAX_LIST_ITEMS
        or (not permit_empty and not value)
    ):
        raise InitiativeScopeError("invalid-list", field)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item = _text(item, f"{field}[{index}]")
        if item in seen:
            raise InitiativeScopeError("duplicate-list-item", f"{field}[{index}]")
        seen.add(item)
        result.append(item)
    return tuple(result)


def _exact_mapping(value: Any, keys: frozenset[str], field: str) -> Mapping[str, Any]:
    if (
        type(value) is not dict
        or len(value) != len(keys)
        or any(type(key) is not str or key not in keys for key in value)
    ):
        raise InitiativeScopeError("invalid-keys", field)
    return value


def validate_initiative_scope(value: Any) -> InitiativeScope:
    raw = _exact_mapping(value, INITIATIVE_SCOPE_KEYS, "initiativeScope")
    if raw["schemaVersion"] != SCHEMA_VERSION or type(raw["schemaVersion"]) is not int:
        raise InitiativeScopeError(
            "unsupported-schema-version", "initiativeScope.schemaVersion"
        )
    components = _text_list(
        raw["allowedNewRuntimeComponents"],
        "initiativeScope.allowedNewRuntimeComponents",
        permit_empty=True,
    )
    if any(component not in RUNTIME_COMPONENT_KINDS for component in components):
        raise InitiativeScopeError(
            "unknown-runtime-component",
            "initiativeScope.allowedNewRuntimeComponents",
        )
    return InitiativeScope(
        schema_version=SCHEMA_VERSION,
        initiative_id=_text(raw["initiativeId"], "initiativeScope.initiativeId"),
        scope_revision=_positive_int(
            raw["scopeRevision"], "initiativeScope.scopeRevision"
        ),
        objective=_text(raw["objective"], "initiativeScope.objective"),
        canary_success=_text_list(
            raw["canarySuccess"], "initiativeScope.canarySuccess"
        ),
        non_goals=_text_list(raw["nonGoals"], "initiativeScope.nonGoals"),
        max_production_lines_added=_positive_int(
            raw["maxProductionLinesAdded"],
            "initiativeScope.maxProductionLinesAdded",
        ),
        max_production_files_changed=_positive_int(
            raw["maxProductionFilesChanged"],
            "initiativeScope.maxProductionFilesChanged",
        ),
        max_tickets=_positive_int(raw["maxTickets"], "initiativeScope.maxTickets"),
        max_engineer_days=_positive_int(
            raw["maxEngineerDays"], "initiativeScope.maxEngineerDays"
        ),
        allowed_new_runtime_components=components,
    )


def canonical_initiative_scope_bytes(scope: InitiativeScope) -> bytes:
    if type(scope) is not InitiativeScope:
        raise InitiativeScopeError("invalid-scope", "initiativeScope")
    return json.dumps(
        scope.as_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def initiative_scope_sha256(scope: InitiativeScope) -> str:
    return hashlib.sha256(canonical_initiative_scope_bytes(scope)).hexdigest()


def _finding(value: Any, index: int) -> ScopeFinding:
    field = f"initiativeScopeEvidence.findings[{index}]"
    raw = _exact_mapping(value, SCOPE_FINDING_KEYS, field)
    severity = raw["severity"]
    classification = raw["classification"]
    disposition = raw["disposition"]
    if type(severity) is not str or severity not in FINDING_SEVERITIES:
        raise InitiativeScopeError("invalid-finding-severity", f"{field}.severity")
    if (
        type(classification) is not str
        or classification not in FINDING_CLASSIFICATIONS
    ):
        raise InitiativeScopeError(
            "invalid-finding-classification", f"{field}.classification"
        )
    if type(disposition) is not str or disposition not in FINDING_DISPOSITIONS:
        raise InitiativeScopeError(
            "invalid-finding-disposition", f"{field}.disposition"
        )
    if disposition == "BLOCKS_CANARY" and severity not in {"P0", "P1"}:
        raise InitiativeScopeError(
            "nonblocking-severity-blocks-canary", f"{field}.disposition"
        )
    if disposition == "BLOCKS_CANARY" and classification in {
        "INDUCED_BY_DESIGN",
        "POST_CANARY_HARDENING",
    }:
        raise InitiativeScopeError(
            "classification-cannot-block-canary", f"{field}.disposition"
        )
    if (
        classification == "POST_CANARY_HARDENING"
        and disposition not in {"POST_CANARY_BACKLOG", "REJECTED"}
    ):
        raise InitiativeScopeError(
            "hardening-not-backlogged", f"{field}.disposition"
        )
    return ScopeFinding(
        finding_id=_text(raw["findingId"], f"{field}.findingId"),
        severity=severity,
        classification=classification,
        observed_evidence=_text(raw["observedEvidence"], f"{field}.observedEvidence"),
        canary_failure=_text(raw["canaryFailure"], f"{field}.canaryFailure"),
        maximum_consequence=_text(
            raw["maximumConsequence"], f"{field}.maximumConsequence"
        ),
        existing_controls_gap=_text(
            raw["existingControlsGap"], f"{field}.existingControlsGap"
        ),
        smallest_mitigation=_text(
            raw["smallestMitigation"], f"{field}.smallestMitigation"
        ),
        acceptance_test=_text(raw["acceptanceTest"], f"{field}.acceptanceTest"),
        disposition=disposition,
    )


def validate_initiative_progress(value: Any) -> InitiativeProgress:
    raw = _exact_mapping(
        value, INITIATIVE_PROGRESS_KEYS, "initiativeScopeEvidence"
    )
    digest = raw["initiativeScopeSha256"]
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise InitiativeScopeError(
            "invalid-scope-sha256", "initiativeScopeEvidence.initiativeScopeSha256"
        )
    components = _text_list(
        raw["newRuntimeComponents"],
        "initiativeScopeEvidence.newRuntimeComponents",
        permit_empty=True,
    )
    if any(component not in RUNTIME_COMPONENT_KINDS for component in components):
        raise InitiativeScopeError(
            "unknown-runtime-component",
            "initiativeScopeEvidence.newRuntimeComponents",
        )
    findings_value = raw["findings"]
    if type(findings_value) is not list or len(findings_value) > MAX_LIST_ITEMS:
        raise InitiativeScopeError("invalid-findings", "initiativeScopeEvidence.findings")
    findings = tuple(_finding(item, index) for index, item in enumerate(findings_value))
    finding_ids = [finding.finding_id for finding in findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise InitiativeScopeError(
            "duplicate-finding-id", "initiativeScopeEvidence.findings"
        )
    blockers_opened = _nonnegative_int(
        raw["blockersOpened"], "initiativeScopeEvidence.blockersOpened"
    )
    blockers_closed = _nonnegative_int(
        raw["blockersClosed"], "initiativeScopeEvidence.blockersClosed"
    )
    if blockers_closed > blockers_opened:
        raise InitiativeScopeError(
            "closed-blockers-exceed-opened",
            "initiativeScopeEvidence.blockersClosed",
        )
    return InitiativeProgress(
        initiative_id=_text(raw["initiativeId"], "initiativeScopeEvidence.initiativeId"),
        scope_revision=_positive_int(
            raw["scopeRevision"], "initiativeScopeEvidence.scopeRevision"
        ),
        initiative_scope_sha256=digest,
        cumulative_tickets=_nonnegative_int(
            raw["cumulativeTickets"], "initiativeScopeEvidence.cumulativeTickets"
        ),
        cumulative_engineer_days=_nonnegative_int(
            raw["cumulativeEngineerDays"],
            "initiativeScopeEvidence.cumulativeEngineerDays",
        ),
        cumulative_production_lines_added=_nonnegative_int(
            raw["cumulativeProductionLinesAdded"],
            "initiativeScopeEvidence.cumulativeProductionLinesAdded",
        ),
        cumulative_production_files_changed=_nonnegative_int(
            raw["cumulativeProductionFilesChanged"],
            "initiativeScopeEvidence.cumulativeProductionFilesChanged",
        ),
        new_runtime_components=components,
        blockers_opened=blockers_opened,
        blockers_closed=blockers_closed,
        consecutive_growing_checkpoints=_nonnegative_int(
            raw["consecutiveGrowingCheckpoints"],
            "initiativeScopeEvidence.consecutiveGrowingCheckpoints",
        ),
        findings=findings,
    )


def evaluate_initiative_scope(
    scope: InitiativeScope, progress: InitiativeProgress
) -> InitiativeScopeDecision:
    if type(scope) is not InitiativeScope:
        raise InitiativeScopeError("invalid-scope", "initiativeScope")
    if type(progress) is not InitiativeProgress:
        raise InitiativeScopeError("invalid-progress", "initiativeScopeEvidence")

    found: set[str] = set()
    if (
        progress.initiative_id != scope.initiative_id
        or progress.scope_revision != scope.scope_revision
    ):
        found.add("initiative-identity-mismatch")
    if progress.initiative_scope_sha256 != initiative_scope_sha256(scope):
        found.add("initiative-scope-digest-mismatch")
    if progress.cumulative_tickets > scope.max_tickets:
        found.add("initiative-ticket-budget-exceeded")
    if progress.cumulative_engineer_days > scope.max_engineer_days:
        found.add("initiative-engineer-days-budget-exceeded")
    if progress.cumulative_production_lines_added > scope.max_production_lines_added:
        found.add("initiative-production-lines-budget-exceeded")
    if (
        progress.cumulative_production_files_changed
        > scope.max_production_files_changed
    ):
        found.add("initiative-production-files-budget-exceeded")
    if not set(progress.new_runtime_components) <= set(
        scope.allowed_new_runtime_components
    ):
        found.add("initiative-runtime-component-not-allowed")
    if progress.consecutive_growing_checkpoints >= 2:
        found.add("initiative-critical-path-growing")
    if any(
        finding.disposition == "REQUIRES_PRINCIPAL_SCOPE_CHANGE"
        for finding in progress.findings
    ):
        found.add("initiative-principal-scope-change-required")

    reasons = tuple(code for code in SCOPE_REASON_CODES if code in found)
    return InitiativeScopeDecision(
        scope_review_required=bool(reasons),
        reasons=reasons,
    )


__all__ = [
    "FINDING_CLASSIFICATIONS",
    "FINDING_DISPOSITIONS",
    "FINDING_SEVERITIES",
    "INITIATIVE_PROGRESS_KEYS",
    "INITIATIVE_SCOPE_KEYS",
    "InitiativeProgress",
    "InitiativeScope",
    "InitiativeScopeDecision",
    "InitiativeScopeError",
    "RUNTIME_COMPONENT_KINDS",
    "SCOPE_FINDING_KEYS",
    "SCOPE_REASON_CODES",
    "ScopeFinding",
    "canonical_initiative_scope_bytes",
    "evaluate_initiative_scope",
    "initiative_scope_sha256",
    "validate_initiative_progress",
    "validate_initiative_scope",
]
