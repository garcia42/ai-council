"""Source-pinned study paths; selection does not authorize a live mutation.

The CLI must separately enforce the selected study's collection state. These
checks reject pathname aliases at selection time; the existing pinned writer
transactions remain responsible for filesystem identity at mutation time.
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class StudyRouteError(ValueError):
    """The selected study or explicitly supplied store path is invalid."""


@dataclass(frozen=True)
class StudyRoute:
    study_id: str
    collection_state: str
    log: str
    v1_events: str
    v2_events: str
    artifact_root: str
    control_store: str
    coordination_lock: str
    capture_log_prefix_bytes: int | None = None
    capture_log_prefix_sha256: str | None = None


_STORE_FIELDS = (
    "log",
    "v1_events",
    "v2_events",
    "artifact_root",
    "control_store",
    "coordination_lock",
)
_STUDIES = ("council-legacy", "council-fresh-20260910")
LEGACY_ARTIFACT_ROOT = "/var/lib/ai-council-evidence/live-capture-20260907/artifacts"
FRESH_EVIDENCE_ROOT = "/var/lib/ai-council-evidence/fresh-capture-20260910-v2"
LEGACY_CAPTURE_PREFIX_BYTES = 4_589_514
LEGACY_CAPTURE_PREFIX_SHA256 = (
    "f9ccbfd5808a86888f84f0d228a38e586510560e411304f43c33392c5c855ba0"
)


def _canonical_path(raw: str, field: str) -> None:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise StudyRouteError(f"invalid study path: {field}")
    path = Path(raw)
    if not path.is_absolute() or str(path) != raw or ".." in path.parts:
        raise StudyRouteError(f"noncanonical study path: {field}")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise StudyRouteError(f"cannot resolve study path: {field}") from exc
    if resolved != path:
        raise StudyRouteError(f"aliased study path: {field}")


def resolve_study_route(
    study_id: str, supplied_paths: Mapping[str, str] | None = None
) -> StudyRoute:
    """Select fixed paths and reject conflicting explicit store arguments.

    Omitted paths are bound to the route. Supplied paths must be the exact
    canonical strings for that route, including when a supplied value equals
    another study's default. No directories, locks or evidence are created.
    """

    if not isinstance(study_id, str) or study_id not in _STUDIES:
        raise StudyRouteError("missing or unknown council study")
    if supplied_paths is None:
        supplied_paths = {}
    if not isinstance(supplied_paths, Mapping):
        raise StudyRouteError("study paths must be a mapping")
    if any(field not in _STORE_FIELDS for field in supplied_paths):
        raise StudyRouteError("unknown study path field")

    home_text = pwd.getpwuid(os.getuid()).pw_dir
    _canonical_path(home_text, "account_home")
    home = Path(home_text)
    knowledge = home / ".claude/knowledge"
    runtime = home / ".local/state/council-tools"
    lock = str(runtime / "evidence.lock")
    if study_id == "council-legacy":
        route = StudyRoute(
            study_id=study_id,
            collection_state="v1-only",
            log=str(knowledge / "futures-panel-log.jsonl"),
            v1_events=str(knowledge / "council-eval/predictions_resolved.jsonl"),
            v2_events=str(knowledge / "council-eval/capture_resolved.jsonl"),
            artifact_root=LEGACY_ARTIFACT_ROOT,
            control_store=str(runtime / "capture-control"),
            coordination_lock=lock,
            capture_log_prefix_bytes=LEGACY_CAPTURE_PREFIX_BYTES,
            capture_log_prefix_sha256=LEGACY_CAPTURE_PREFIX_SHA256,
        )
    else:
        study_knowledge = knowledge / "council-eval/studies" / study_id
        study_evidence = Path(FRESH_EVIDENCE_ROOT)
        route = StudyRoute(
            study_id=study_id,
            collection_state="closed",
            log=str(study_knowledge / "panel.jsonl"),
            v1_events=str(study_knowledge / "predictions_resolved.jsonl"),
            v2_events=str(study_knowledge / "capture_resolved.jsonl"),
            artifact_root=str(study_evidence / "artifacts"),
            control_store=str(study_evidence / "controls"),
            coordination_lock=lock,
        )

    for field in _STORE_FIELDS:
        expected = getattr(route, field)
        _canonical_path(expected, field)
        if field in supplied_paths:
            supplied = supplied_paths[field]
            _canonical_path(supplied, field)
            if supplied != expected:
                raise StudyRouteError(f"path conflicts with selected study: {field}")
    return route
