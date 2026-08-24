"""Report how councils record what they reviewed, from the ledger alone.

Before anything can measure *review* coverage, it has to be possible to tell
which changes a council read.  That depends entirely on what the ledger records,
and today it mostly does not record it: the ``commits`` field has no producer,
so it is typed by hand by the same session that ran the council, in whatever
shape that session chose.

This module answers the precondition question -- *is the recording convention
being adopted* -- and nothing else.  It reads the ledger and classifies each
council row's ``commits`` field by shape.  It never invokes git, never
classifies a commit, and never says a change went unreviewed.  That is the
point: the reader that did join against git had to work from a key present in
16% of rows, and inherited a false-accusation rate to match.

Deliberately not a gate.  Adoption is a number that starts low and improves, so
an exit code that goes red until it is perfect would be red for months and would
train its reader to ignore it.  Exit 0 means the report ran and can be trusted;
exit 3 means it cannot be.  There is no exit 1, because there is no accusation
to make: a row that records nothing is a gap in the record, not a change that
shipped unreviewed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .forecasts import LedgerError, load_jsonl


COUNCIL_ROW_KINDS = frozenset({"council"})

#: Shapes in adoption order: the first names commits in the defined convention,
#: the last cannot name anything at all.
ARRAY_FULL_SHAS = "array-full-shas"
ARRAY_ABBREVIATED = "array-abbreviated"
ARRAY_EMPTY = "array-empty"
OBJECT_PRECOMMIT = "object-precommit-review"
OBJECT_BASE_ONLY = "object-base-pointer-only"
FIELD_NULL = "field-null"
FIELD_ABSENT = "field-absent"
OTHER = "other"

SHAPES = (
    ARRAY_FULL_SHAS,
    ARRAY_ABBREVIATED,
    ARRAY_EMPTY,
    OBJECT_PRECOMMIT,
    OBJECT_BASE_ONLY,
    FIELD_NULL,
    FIELD_ABSENT,
    OTHER,
)

#: Shapes that state which commits were read, so a later reconciler could join
#: on them.  Only these count toward adoption.
NAMES_COMMITS = frozenset({ARRAY_FULL_SHAS, ARRAY_ABBREVIATED})

SHAPE_DESCRIPTIONS = {
    ARRAY_FULL_SHAS: "array of full 40-character SHAs (the convention)",
    ARRAY_ABBREVIATED: "array of object names, at least one abbreviated",
    ARRAY_EMPTY: "empty array; indistinguishable from an unpopulated field",
    OBJECT_PRECOMMIT: "object recording a review of a staged or uncommitted tree",
    OBJECT_BASE_ONLY: "object naming a branch point, not what was read",
    FIELD_NULL: "field present and null",
    FIELD_ABSENT: "field absent",
    OTHER: "some other shape",
}

#: Keys and values that mark a row as having reviewed content that was not yet a
#: commit.  Those reviews were real; there was simply nothing to name.
_PRECOMMIT_KEYS = frozenset({"stagedTree", "candidate_tree", "candidateTree"})
_PRECOMMIT_VALUE_PREFIX = "uncommitted"


class RecordingCoverageError(ValueError):
    """The report was asked for something it cannot compute."""


def parse_timestamp(value: str, *, field: str) -> datetime:
    """Parse an ISO-8601 instant, or a bare date read as UTC midnight."""

    if not isinstance(value, str) or not value.strip():
        raise RecordingCoverageError(f"{field} must be a non-empty ISO-8601 timestamp")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RecordingCoverageError(
            f"{field} is not an ISO-8601 date or timestamp: {text}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_shape(row: Mapping[str, Any]) -> str:
    """Return the shape of one council row's ``commits`` field."""

    if "commits" not in row:
        return FIELD_ABSENT
    commits = row["commits"]
    if commits is None:
        return FIELD_NULL
    if isinstance(commits, list):
        if not commits:
            return ARRAY_EMPTY
        if not all(isinstance(item, str) and item for item in commits):
            return OTHER
        if all(len(item) == 40 for item in commits):
            return ARRAY_FULL_SHAS
        return ARRAY_ABBREVIATED
    if isinstance(commits, Mapping):
        if _PRECOMMIT_KEYS & set(commits):
            return OBJECT_PRECOMMIT
        for value in commits.values():
            if isinstance(value, str) and value.startswith(_PRECOMMIT_VALUE_PREFIX):
                return OBJECT_PRECOMMIT
        return OBJECT_BASE_ONLY
    return OTHER


def _row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    raw = row.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return parse_timestamp(raw, field="ts")
    except RecordingCoverageError:
        return None


def report_recording_coverage(
    *,
    log_path: str | Path,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Classify every council row's ``commits`` field, or refuse and say why."""

    if since is not None and until is not None and since >= until:
        raise RecordingCoverageError("--since must be strictly before --until")

    result: dict[str, Any] = {
        "tool": "council-recording-coverage",
        "determined": False,
        "refusals": [],
        "window": {
            "ledgerPath": str(log_path),
            "since": since.isoformat().replace("+00:00", "Z") if since else None,
            "until": until.isoformat().replace("+00:00", "Z") if until else None,
            "timezone": "UTC",
        },
        "adoption": None,
    }

    def refuse(code: str, detail: str) -> dict[str, Any]:
        result["refusals"] = [{"code": code, "detail": detail}]
        return result

    if not Path(log_path).exists():
        return refuse(
            "ledger-unreadable", f"ledger file does not exist: {log_path}"
        )
    try:
        rows = load_jsonl(log_path)
    except LedgerError as exc:
        # A ledger that cannot be read in full has an unknown numerator; the
        # same discipline `report` applies.
        return refuse("ledger-unreadable", str(exc))
    except OSError as exc:
        return refuse("ledger-unreadable", f"cannot read {log_path}: {exc}")

    counts = {shape: 0 for shape in SHAPES}
    unreadable_ts = 0
    considered = 0
    for _line_number, row in rows:
        if row.get("kind") not in COUNCIL_ROW_KINDS:
            continue
        if since is not None or until is not None:
            row_ts = _row_timestamp(row)
            if row_ts is None:
                # Counted, never silently dropped: a row that cannot be placed
                # in time is a gap in the record, and the window's denominator
                # would otherwise shrink without saying so.
                unreadable_ts += 1
                continue
            if since is not None and row_ts < since:
                continue
            if until is not None and row_ts >= until:
                continue
        considered += 1
        counts[classify_shape(row)] += 1

    if considered == 0:
        return refuse(
            "no-council-rows",
            "no council rows in the window, so there is no adoption denominator"
            f" (rows with an unreadable ts: {unreadable_ts})",
        )

    naming = sum(counts[shape] for shape in NAMES_COMMITS)
    result["determined"] = True
    result["councilRows"] = considered
    result["rowsWithUnreadableTs"] = unreadable_ts
    result["shapes"] = {
        shape: {
            "rows": counts[shape],
            "share": counts[shape] / considered,
            "description": SHAPE_DESCRIPTIONS[shape],
            "namesCommits": shape in NAMES_COMMITS,
        }
        for shape in SHAPES
    }
    result["adoption"] = {
        "rowsNamingCommits": naming,
        "rowsUnableToNameCommits": considered - naming,
        "share": naming / considered,
        "basis": "council rows recording an array of object names / all council rows",
    }
    result["precommitReviews"] = counts[OBJECT_PRECOMMIT]
    return result


def recording_exit_code(result: Mapping[str, Any]) -> int:
    """0 the report ran and can be trusted, 3 it cannot.

    There is deliberately no exit 1.  Low adoption is the finding this report
    exists to show, not an error state -- an exit code that stayed red until
    adoption was perfect would be red for months and would teach its reader to
    ignore it.
    """

    return 0 if result.get("determined") else 3


def format_recording_coverage(result: Mapping[str, Any]) -> str:
    """Render the report as stable ``key=value`` lines plus a shape table."""

    window = result["window"]
    lines = [
        f"ledger={window['ledgerPath']} "
        f"window={window['since'] or 'all'}..{window['until'] or 'now'} "
        f"timezone={window['timezone']}"
    ]
    if not result["determined"]:
        for refusal in result["refusals"]:
            lines.append(f"REFUSED code={refusal['code']} detail={refusal['detail']}")
        lines.append("adoption=UNAVAILABLE  (cannot determine; exit 3)")
        return "\n".join(lines)

    adoption = result["adoption"]
    lines.append(
        f"council_rows={result['councilRows']} "
        f"rows_with_unreadable_ts={result['rowsWithUnreadableTs']} "
        f"precommit_reviews={result['precommitReviews']}"
    )
    lines.append(
        f"adoption={adoption['share']:.4f} "
        f"names_commits={adoption['rowsNamingCommits']} "
        f"cannot_name_commits={adoption['rowsUnableToNameCommits']} "
        f"basis={adoption['basis']}"
    )
    for shape in SHAPES:
        entry = result["shapes"][shape]
        if not entry["rows"]:
            continue
        marker = "names-commits" if entry["namesCommits"] else "             "
        lines.append(
            f"  {entry['rows']:4d}  {entry['share'] * 100:5.1f}%  {marker}  "
            f"{shape}: {entry['description']}"
        )
    return "\n".join(lines)
