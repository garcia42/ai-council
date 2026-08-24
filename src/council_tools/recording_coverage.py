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
a quarter of rows, and inherited a false-accusation rate to match.

Deliberately not a gate.  Adoption is a number that starts low and improves, so
an exit code that goes red until it is perfect would be red for months and would
train its reader to ignore it.  Exit 0 means the report ran and can be trusted;
exit 3 means it cannot be.  There is no exit 1, because there is no accusation
to make: a row that records nothing is a gap in the record, not a change that
shipped unreviewed.
"""

from __future__ import annotations

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
OBJECT_NAMES_CANDIDATE = "object-names-candidate-commit"
OBJECT_PRECOMMIT = "object-precommit-review"
OBJECT_BASE_ONLY = "object-base-pointer-only"
FIELD_NULL = "field-null"
FIELD_ABSENT = "field-absent"
OTHER = "other"

SHAPES = (
    ARRAY_FULL_SHAS,
    ARRAY_ABBREVIATED,
    ARRAY_EMPTY,
    OBJECT_NAMES_CANDIDATE,
    OBJECT_PRECOMMIT,
    OBJECT_BASE_ONLY,
    FIELD_NULL,
    FIELD_ABSENT,
    OTHER,
)

#: Shapes that state which commits were read, so a later reconciler could join
#: on them.  Only these count toward adoption.
NAMES_COMMITS = frozenset(
    {ARRAY_FULL_SHAS, ARRAY_ABBREVIATED, OBJECT_NAMES_CANDIDATE}
)

SHAPE_DESCRIPTIONS = {
    ARRAY_FULL_SHAS: "array of full 40-character SHAs (the convention)",
    ARRAY_ABBREVIATED: "array of object names, at least one abbreviated",
    ARRAY_EMPTY: "empty array; indistinguishable from an unpopulated field",
    OBJECT_NAMES_CANDIDATE: "object naming the reviewed tip commit alongside its base",
    OBJECT_PRECOMMIT: "object recording a review of a staged or uncommitted tree",
    OBJECT_BASE_ONLY: "object naming only a branch point or production head, not what was read",
    FIELD_NULL: "field present and null",
    FIELD_ABSENT: "field absent",
    OTHER: "some other shape",
}

#: Keys and values that mark a row as having reviewed content that was not yet a
#: commit.  Those reviews were real; there was simply nothing to name.
_TREE_KEYS = frozenset({"stagedTree", "candidate_tree", "candidateTree"})
_PRECOMMIT_VALUE_PREFIX = "uncommitted"

#: Keys whose value names the reviewed *tip*. A reconciler can join on these:
#: with the row's base they bound exactly what was read. ``base`` is excluded --
#: it names the branch point, which is what the review started from, not what it
#: covered -- and so is ``prodHead``, which names production's HEAD and was never
#: a review boundary at all.
_REVIEWED_TIP_KEYS = ("candidate_commit", "candidateCommit", "candidate")

_HEX = frozenset("0123456789abcdef")


def _is_object_name(value: object, *, exact: int | None = None) -> bool:
    """True when a value is lowercase hex of a plausible object-name length."""

    if not isinstance(value, str):
        return False
    if exact is not None and len(value) != exact:
        return False
    if exact is None and not 7 <= len(value) <= 40:
        return False
    # Git resolves uppercase object names, so the convention bucket must not be
    # narrower than git itself.
    return bool(value) and set(value.lower()) <= _HEX


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
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        # A timestamp at the datetime range boundary overflows on conversion.
        # OverflowError is not a ValueError, so without this it escapes every
        # handler in this module and cli.main alike, and surfaces as a traceback
        # with exit 1 -- the one exit code this tool promises never to return.
        raise RecordingCoverageError(
            f"{field} cannot be converted to UTC: {text}"
        ) from exc


def classify_shape(row: Mapping[str, Any]) -> str:
    """Return the shape of one council row's ``commits`` field.

    The object branch inspects **values**, not key presence.  A row such as
    ``{"base": B, "candidate_commit": C, "candidate_tree": null}`` carries a
    tree key whose value is null and a candidate whose value is a real commit;
    keying off presence alone filed it as a tree review and dropped it from the
    adoption numerator.  When found, that mis-scored six live rows, understated
    adoption by a third and overstated tree reviews by three quarters.

    The key names themselves are not trustworthy either, which is the argument
    this whole module is making: one live row records a value under
    ``candidate_commit`` that another records under ``candidate_tree``.  Telling
    those apart would need the git join this module deliberately does not do, so
    the tree count is an upper bound on real tree reviews.
    """

    if "commits" not in row:
        return FIELD_ABSENT
    commits = row["commits"]
    if commits is None:
        return FIELD_NULL
    if isinstance(commits, list):
        if not commits:
            return ARRAY_EMPTY
        if not all(_is_object_name(item) for item in commits):
            # Not object names at all -- free text, numbers, nested structures.
            # The convention count is the headline number on a hand-typed field,
            # so anything that is not plausibly an object name is OTHER.
            return OTHER
        if all(_is_object_name(item, exact=40) for item in commits):
            return ARRAY_FULL_SHAS
        return ARRAY_ABBREVIATED
    if isinstance(commits, Mapping):
        # An explicit "uncommitted" declaration is the row stating outright what
        # it reviewed, so it outranks everything: a tip recorded beside it does
        # not undo the statement that the reviewed content was not a commit.
        if any(
            isinstance(value, str) and value.startswith(_PRECOMMIT_VALUE_PREFIX)
            for value in commits.values()
        ):
            return OBJECT_PRECOMMIT
        # Otherwise naming the reviewed tip is the stronger, joinable fact, so
        # it is tested before the tree keys: a row may carry both when a staged
        # tree was reviewed and then committed.
        if any(_is_object_name(commits.get(key)) for key in _REVIEWED_TIP_KEYS):
            return OBJECT_NAMES_CANDIDATE
        # A tree marker need not be a hash -- "dirty" is a legitimate value --
        # so unlike the tip keys this accepts any non-empty string. Do not
        # "fix" that into symmetry with the check above.
        if any(
            isinstance(commits.get(key), str) and commits.get(key)
            for key in _TREE_KEYS
        ):
            return OBJECT_PRECOMMIT
        # Only claim it names a branch point if something in it actually is an
        # object name. Asserting a positive fact the code never checked is what
        # made the presence-vs-value bug above possible; the array branch
        # already routes unrecognisable content to OTHER, and so does this.
        if any(_is_object_name(value) for value in commits.values()):
            return OBJECT_BASE_ONLY
        return OTHER
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
    excluded_for_ts = 0
    unrecognised_kind = 0
    considered = 0
    for _line_number, row in rows:
        if row.get("kind") not in COUNCIL_ROW_KINDS:
            # The ledger's first rows predate the `kind` field entirely. They
            # are excluded from the denominator, and counted for the same reason
            # the unreadable timestamps are: a denominator must not shrink
            # without saying so.
            if row.get("kind") is None:
                unrecognised_kind += 1
            continue
        row_ts = _row_timestamp(row)
        if row_ts is None:
            # Counted unconditionally, not only when a window is given. The
            # default invocation is the one anything automated runs, and it must
            # not report zero unreadable timestamps for a ledger that has them.
            unreadable_ts += 1
        if since is not None or until is not None:
            if row_ts is None:
                excluded_for_ts += 1
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
            f" (council rows excluded for an unreadable ts: {excluded_for_ts};"
            f" rows with no kind field: {unrecognised_kind})",
        )

    naming = sum(counts[shape] for shape in NAMES_COMMITS)
    result["determined"] = True
    result["councilRows"] = considered
    result["rowsWithUnreadableTs"] = unreadable_ts
    result["rowsExcludedForUnreadableTs"] = excluded_for_ts
    result["rowsWithNoKindField"] = unrecognised_kind
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
        "basis": (
            "council rows naming the commits they read (an array of object "
            "names, or an object naming the reviewed tip) / all rows with "
            "kind=council in the window"
        ),
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
        f"excluded_for_unreadable_ts={result['rowsExcludedForUnreadableTs']} "
        f"rows_with_no_kind_field={result['rowsWithNoKindField']} "
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
