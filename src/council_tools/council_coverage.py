"""Reconcile shipped commits against council rows to measure review coverage.

The forecast ledger can say how often a seated council changed a decision.  It
cannot say how often a change shipped with no council at all, because a skipped
review writes nothing.  That makes the kill criterion a rate over councils that
happened rather than over decisions that were made, which is the gap this
module closes: it joins ``git log`` over an explicit window to the ``commits``
field of council rows and classifies every commit in that window.

Four states, each defined:

``COVERED``
    The commit's full SHA appears in the ``commits`` array of a ``council`` row.

``EXEMPT``
    The commit message carries a ``Council-Exempt: <reason>`` trailer with a
    non-empty reason.  Only an explicit, author-written exemption counts.  There
    is no presumed exemption: a commit is never excused for looking mechanical.

``UNKNOWN``
    A council row plausibly reviewed the commit but does not say so.  Rows
    written before the array convention recorded shapes such as
    ``{"base": "<sha>"}``, which names the branch point and not the reviewed
    commits.  When such a row names an object name that resolves in this
    repository, is an ancestor of the commit, and was written after the commit
    was made, the commit could have been reviewed by that council or could have
    been appended afterwards.  The record cannot distinguish the two.

``UNCOVERED``
    None of the above.  The commit shipped and no row claims it.

A wrong denominator is worse than none, so the reconciler refuses to print a
rate at all when the ledger cannot be read, when no row has ever used the array
convention, or when the requested window starts before the first row that did.
The skip rate before that convention is not recoverable from this evidence and
is reported as such rather than estimated.

Exit codes belong to the caller, but the mapping this module is written for is
0 clean, 1 something shipped unreviewed, and 3 cannot determine.  2 is
deliberately unused: argparse and the interpreter both exit 2, and a coverage
rate must never be confusable with a command that did not run.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .forecasts import LedgerError, load_jsonl


#: Row kinds whose ``commits`` array is a statement that a council read them.
#: ``council-attempt`` is deliberately excluded: it is the price-free record
#: written *before* the seats run, so it cannot attest that anything was read.
COUNCIL_ROW_KINDS = frozenset({"council"})

COVERED = "COVERED"
EXEMPT = "EXEMPT"
UNKNOWN = "UNKNOWN"
UNCOVERED = "UNCOVERED"

#: Ordered most to least informative.  A commit that qualifies for several
#: states is reported as the first one it qualifies for.
STATE_PRECEDENCE = (COVERED, EXEMPT, UNKNOWN, UNCOVERED)

EXEMPT_TRAILER = "Council-Exempt:"

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
#: An abbreviated object name in a pre-convention row.  Seven hex characters is
#: git's own default abbreviation floor; requiring it keeps branch names and
#: free text such as ``uncommitted-untracked`` out of the ambiguity join.
_ABBREV_SHA = re.compile(r"^[0-9a-f]{7,40}$")

_GIT_TIMEOUT_SECONDS = 120


class CoverageError(ValueError):
    """The reconciler was asked for something it cannot compute."""


@dataclass(frozen=True)
class Refusal:
    """A named reason the reconciler will not report a rate."""

    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class Commit:
    sha: str
    committed_at: datetime
    subject: str
    exempt_reason: str | None


@dataclass(frozen=True)
class LedgerEvidence:
    rows_read: int
    conforming_rows: int
    ambiguous_rows: int
    reviewed_shas: frozenset[str]
    #: ``(row timestamp or None, object names named by the row)`` for every row
    #: that carries ``commits`` in a shape the convention does not define.
    ambiguous_bases: tuple[tuple[datetime | None, tuple[str, ...]], ...]
    epoch: datetime | None


def _text_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, *, field: str) -> datetime:
    """Parse an ISO-8601 instant, or a bare date read as UTC midnight.

    Window bounds are deliberately UTC rather than America/New_York.  Git
    records an explicit offset per commit, so normalizing both sides to UTC
    keeps the join independent of the operator's local zone.
    """

    if not isinstance(value, str) or not value.strip():
        raise CoverageError(f"{field} must be a non-empty ISO-8601 timestamp")
    text = value.strip()
    # Ledger timestamps and git's %cI both spell UTC as a trailing Z on some
    # interpreters and as +00:00 on others.  Normalizing here keeps the parse
    # from depending on which one wrote the string.
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CoverageError(
            f"{field} is not an ISO-8601 date or timestamp: {text}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    """Parse a row's ``ts``, or None when the row does not carry a usable one.

    Callers decide what None means.  For a conforming row it is fatal, because
    the instrumentation epoch is derived from those timestamps.  For a
    pre-convention row it means the ambiguity it creates is unbounded in time.
    """

    raw = row.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return parse_timestamp(raw, field="ts")
    except CoverageError:
        return None


def _conforming_commits(row: Mapping[str, Any]) -> list[str] | None:
    """Return the reviewed SHAs when the row states them in the defined shape.

    The shape is a JSON array of full 40-character SHAs.  An empty array is
    conforming: it is how a council that reviewed a decision with no diff
    records that it read no commits, and it still proves the writer was using
    the convention.  Anything else -- an object, a null, a bare string, an
    array carrying an abbreviation -- is not conforming, because it does not
    say which commits were read.
    """

    if row.get("kind") not in COUNCIL_ROW_KINDS:
        return None
    commits = row.get("commits")
    if not isinstance(commits, list):
        return None
    if not all(isinstance(item, str) and _FULL_SHA.match(item) for item in commits):
        return None
    return list(commits)


def _named_object_names(value: Any) -> tuple[str, ...]:
    """Collect the object-name-shaped strings a non-conforming row mentions."""

    if isinstance(value, str):
        candidates: list[str] = [value]
    elif isinstance(value, Mapping):
        candidates = [item for item in value.values() if isinstance(item, str)]
    elif isinstance(value, Sequence):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return ()
    return tuple(
        dict.fromkeys(item for item in candidates if _ABBREV_SHA.match(item))
    )


def read_ledger_evidence(log_path: str | Path) -> LedgerEvidence:
    """Summarize what the ledger claims about reviewed commits.

    Raises :class:`~council_tools.forecasts.LedgerError` when any line is
    unparseable.  The caller turns that into a refusal rather than a rate: a
    ledger that cannot be read in full has an unknown numerator.
    """

    rows = load_jsonl(log_path)
    reviewed: set[str] = set()
    ambiguous: list[tuple[datetime | None, tuple[str, ...]]] = []
    conforming_rows = 0
    ambiguous_rows = 0
    epoch: datetime | None = None
    for _line_number, row in rows:
        conforming = _conforming_commits(row)
        if conforming is not None:
            conforming_rows += 1
            reviewed.update(conforming)
            row_ts = _row_timestamp(row)
            if row_ts is None:
                # The epoch is derived from these timestamps.  Dropping a row
                # whose ts cannot be read would move the epoch later and hide
                # exactly the window this tool exists to measure.
                raise LedgerError(
                    f"{Path(log_path).name} line {_line_number}: council row "
                    "records commits but has no readable ts"
                )
            if epoch is None or row_ts < epoch:
                epoch = row_ts
            continue
        if "commits" not in row:
            continue
        ambiguous_rows += 1
        names = _named_object_names(row.get("commits"))
        if names:
            ambiguous.append((_row_timestamp(row), names))
    return LedgerEvidence(
        rows_read=len(rows),
        conforming_rows=conforming_rows,
        ambiguous_rows=ambiguous_rows,
        reviewed_shas=frozenset(reviewed),
        ambiguous_bases=tuple(ambiguous),
        epoch=epoch,
    )


class GitError(RuntimeError):
    """Git could not answer a question about the repository under review."""


def _git(repo: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git {' '.join(arguments)} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise GitError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _resolve_object(repo: Path, name: str) -> str | None:
    """Return the full SHA a name resolves to, or None when it does not.

    ``--end-of-options`` keeps a caller-supplied name that begins with a dash
    from being read as a git option.
    """

    try:
        output = _git(
            repo,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{name}^{{commit}}",
        )
    except GitError:
        return None
    resolved = output.strip()
    return resolved if _FULL_SHA.match(resolved) else None


def _exempt_reason(message: str) -> str | None:
    """Return the reason from a ``Council-Exempt:`` trailer, if it has one.

    An exemption with an empty reason is not an exemption.  Failing closed here
    means a malformed trailer surfaces as UNCOVERED rather than silently
    removing the commit from the denominator.
    """

    for line in message.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith(EXEMPT_TRAILER.lower()):
            continue
        reason = stripped[len(EXEMPT_TRAILER) :].strip()
        if reason:
            return reason
    return None


def _read_commits(
    repo: Path,
    *,
    ref: str,
    since: datetime,
    until: datetime,
    include_merges: bool,
) -> tuple[list[Commit], int]:
    """List commits reachable from ``ref`` whose committer date is in-window.

    Git's own ``--since``/``--until`` are used only as a loose pre-filter, wide
    by a day on each side, and the window is then re-applied here against the
    parsed ``%cI``.  The reported bounds therefore mean exactly one thing --
    half-open ``[since, until)`` on committer date in UTC -- rather than
    whatever a given git version makes of a date string at the boundary.

    Merge commits are read in the same pass and partitioned here, so the count
    of what a ``--no-merges`` run left out is exact and always reported.  A
    merge introduces no content of its own; the commits it brings in are
    classified on their own SHAs.
    """

    slack = timedelta(days=1)
    arguments = [
        # One NUL terminates each record and the first three newline-separated
        # lines are the structured fields.  A commit message cannot contain a
        # NUL, and it is the last field, so it may contain anything else.  A
        # NUL *between* fields would be ambiguous instead: a root commit has an
        # empty %P, which would collide with any multi-NUL record separator.
        "log",
        "--format=%H%n%cI%n%P%n%B%x00",
        f"--since={_text_timestamp(since - slack)}",
        f"--until={_text_timestamp(until + slack)}",
        "--end-of-options",
        ref,
        "--",
    ]
    raw = _git(repo, *arguments)

    commits: list[Commit] = []
    merges_excluded = 0
    for record in raw.split("\x00"):
        if not record.strip("\n"):
            continue
        fields = record.lstrip("\n").split("\n", 3)
        if len(fields) < 4:
            raise GitError("git log returned a malformed record")
        sha, committed_raw, parents, message = fields
        if not _FULL_SHA.match(sha):
            raise GitError(f"git log returned a malformed object name: {sha!r}")
        committed_at = parse_timestamp(committed_raw, field="committer date")
        if not since <= committed_at < until:
            continue
        if len(parents.split()) > 1 and not include_merges:
            merges_excluded += 1
            continue
        subject = message.strip().splitlines()[0].strip() if message.strip() else ""
        commits.append(
            Commit(
                sha=sha,
                committed_at=committed_at,
                subject=subject,
                exempt_reason=_exempt_reason(message),
            )
        )
    return commits, merges_excluded


def _ambiguously_covered(
    repo: Path,
    *,
    evidence: LedgerEvidence,
    commits: Sequence[Commit],
    ref_sha: str,
) -> tuple[set[str], int]:
    """Map pre-convention rows onto the commits they might have reviewed.

    A row that names base ``B`` at time ``T`` could only have read commits that
    descend from ``B`` and already existed at ``T``.  ``--ancestry-path`` gives
    exactly the descendants of ``B`` that are ancestors of the tip, in one call
    per distinct base rather than one per (base, commit) pair.

    The result is deliberately a superset.  A row such as
    ``{"base": B, "candidate": C}`` reviewed some commits between ``B`` and
    ``C``, and this treats every descendant of ``B`` up to ``T`` as possibly
    reviewed.  Over-reporting UNKNOWN keeps ambiguity visible; under-reporting
    it would quietly convert a guess into a rate.
    """

    if not commits:
        return set(), 0
    by_sha = {commit.sha: commit for commit in commits}
    ambiguous: set[str] = set()
    resolved: dict[str, str | None] = {}
    descendants: dict[str, set[str]] = {}
    for row_ts, names in evidence.ambiguous_bases:
        for name in names:
            if name not in resolved:
                resolved[name] = _resolve_object(repo, name)
            base = resolved[name]
            if base is None:
                continue
            if base not in descendants:
                try:
                    # Both revisions are full SHAs validated above, so no
                    # caller-controlled text reaches git's option parser here;
                    # `--end-of-options` cannot be used because `--not` must
                    # still be read as an option.
                    reachable = _git(
                        repo,
                        "rev-list",
                        "--ancestry-path",
                        ref_sha,
                        "--not",
                        base,
                        "--",
                    )
                except GitError:
                    descendants[base] = set()
                else:
                    descendants[base] = set(reachable.split())
            for sha in descendants[base] & by_sha.keys():
                # A council cannot have read a commit that did not exist yet.
                # A row with an unreadable timestamp is treated as unbounded,
                # which keeps the ambiguity rather than resolving it silently.
                if row_ts is None or by_sha[sha].committed_at <= row_ts:
                    ambiguous.add(sha)
    return ambiguous, len({base for base in resolved.values() if base is not None})


def _classify(
    commit: Commit,
    *,
    reviewed: frozenset[str],
    ambiguous: set[str],
) -> tuple[str, str]:
    if commit.sha in reviewed:
        return COVERED, "named in a council row commits array"
    if commit.exempt_reason:
        return EXEMPT, f"{EXEMPT_TRAILER} {commit.exempt_reason}"
    if commit.sha in ambiguous:
        return (
            UNKNOWN,
            "a pre-convention council row names an ancestor but not this commit",
        )
    return UNCOVERED, "no council row names this commit"


def reconcile_coverage(
    *,
    repo: str | Path,
    log_path: str | Path,
    since: datetime,
    until: datetime,
    ref: str = "HEAD",
    include_merges: bool = False,
    instrumented_since: datetime | None = None,
) -> dict[str, Any]:
    """Classify every in-window commit, or refuse and say why.

    The returned mapping always carries ``determined`` and ``refusals``.  Counts
    and a rate appear only when ``determined`` is true.
    """

    if since >= until:
        raise CoverageError("window --since must be strictly before --until")

    repo_path = Path(repo)
    window = {
        "repo": str(repo_path),
        "ref": ref,
        "since": _text_timestamp(since),
        "until": _text_timestamp(until),
        "timezone": "UTC",
        "includesMerges": include_merges,
    }
    result: dict[str, Any] = {
        "tool": "council-coverage",
        "determined": False,
        "refusals": [],
        "window": window,
        "instrumentation": {
            "ledgerPath": str(log_path),
            "epoch": None,
            "source": "explicit" if instrumented_since else "ledger-derived",
        },
        "rate": None,
    }

    def refuse(code: str, detail: str) -> dict[str, Any]:
        result["refusals"] = [Refusal(code, detail).as_dict()]
        return result

    try:
        evidence = read_ledger_evidence(log_path)
    except LedgerError as exc:
        return refuse("ledger-unreadable", str(exc))
    except OSError as exc:
        return refuse("ledger-unreadable", f"cannot read {log_path}: {exc}")

    result["ledger"] = {
        "rowsRead": evidence.rows_read,
        "conformingCouncilRows": evidence.conforming_rows,
        "ambiguousCommitRows": evidence.ambiguous_rows,
        "reviewedShas": len(evidence.reviewed_shas),
    }

    epoch = instrumented_since or evidence.epoch
    result["instrumentation"]["epoch"] = (
        _text_timestamp(epoch) if epoch is not None else None
    )
    if epoch is None:
        return refuse(
            "no-commit-instrumentation",
            "no council row records commits as an array of full SHAs, so no "
            "window has a known denominator; the historical skip rate is "
            "unrecoverable from this ledger",
        )
    if since < epoch:
        return refuse(
            "window-predates-commit-instrumentation",
            f"window starts {_text_timestamp(since)} but the first council row "
            f"using the commits-array convention is {_text_timestamp(epoch)}; "
            "the skip rate before that convention is unrecoverable and is not "
            "estimated",
        )

    try:
        if _git(repo_path, "rev-parse", "--is-inside-work-tree").strip() != "true":
            return refuse(
                "git-unavailable", f"{repo_path} is not inside a git work tree"
            )
        ref_sha = _resolve_object(repo_path, ref)
        if ref_sha is None:
            return refuse("git-unavailable", f"ref does not resolve to a commit: {ref}")
        commits, merges_excluded = _read_commits(
            repo_path,
            ref=ref,
            since=since,
            until=until,
            include_merges=include_merges,
        )
        ambiguous, resolved_bases = _ambiguously_covered(
            repo_path, evidence=evidence, commits=commits, ref_sha=ref_sha
        )
    except (GitError, CoverageError) as exc:
        # CoverageError here means git answered with something this tool cannot
        # read (a committer date it cannot parse).  That is "cannot determine",
        # not "shipped unreviewed".
        return refuse("git-unavailable", str(exc))

    result["ledger"]["ambiguousBasesResolvedInRepo"] = resolved_bases

    classified = []
    counts = {state: 0 for state in STATE_PRECEDENCE}
    for commit in sorted(commits, key=lambda item: (item.committed_at, item.sha)):
        state, reason = _classify(
            commit, reviewed=evidence.reviewed_shas, ambiguous=ambiguous
        )
        counts[state] += 1
        classified.append(
            {
                "sha": commit.sha,
                "committedAt": _text_timestamp(commit.committed_at),
                "subject": commit.subject,
                "state": state,
                "reason": reason,
            }
        )

    total = len(classified)
    eligible = total - counts[EXEMPT]
    result["determined"] = True
    result["counts"] = {
        "total": total,
        "covered": counts[COVERED],
        "exempt": counts[EXEMPT],
        "unknown": counts[UNKNOWN],
        "uncovered": counts[UNCOVERED],
        "eligible": eligible,
        "mergeCommitsExcluded": merges_excluded,
    }
    result["commits"] = classified
    if eligible <= 0:
        result["rateNote"] = "no eligible commits in window"
    else:
        # The band, not a point estimate: every UNKNOWN commit is a commit this
        # evidence cannot place on either side, so the true covered fraction
        # lies between counting them as uncovered and counting them as covered.
        result["rate"] = {
            "basis": "covered / (total - exempt)",
            "lower": counts[COVERED] / eligible,
            "upper": (counts[COVERED] + counts[UNKNOWN]) / eligible,
            "exact": counts[UNKNOWN] == 0,
        }
    return result


def coverage_exit_code(result: Mapping[str, Any]) -> int:
    """0 clean, 1 something shipped unreviewed, 3 cannot determine."""

    if not result.get("determined"):
        return 3
    counts = result.get("counts", {})
    if counts.get("uncovered", 0) > 0:
        return 1
    if counts.get("unknown", 0) > 0:
        return 3
    return 0


def format_coverage(result: Mapping[str, Any]) -> str:
    """Render the reconciliation as stable ``key=value`` lines."""

    window = result["window"]
    instrumentation = result["instrumentation"]
    lines = [
        f"repo={window['repo']} ref={window['ref']}",
        f"window={window['since']}..{window['until']} timezone={window['timezone']} "
        f"includes_merges={str(window['includesMerges']).lower()}",
        f"ledger={instrumentation['ledgerPath']} "
        f"instrumentation_epoch={instrumentation['epoch'] or 'none'} "
        f"epoch_source={instrumentation['source']}",
    ]
    ledger = result.get("ledger")
    if ledger:
        lines.append(
            f"rows_read={ledger['rowsRead']} "
            f"conforming_council_rows={ledger['conformingCouncilRows']} "
            f"ambiguous_commit_rows={ledger['ambiguousCommitRows']} "
            f"reviewed_shas={ledger['reviewedShas']}"
        )
    if not result["determined"]:
        for refusal in result["refusals"]:
            lines.append(f"REFUSED code={refusal['code']} detail={refusal['detail']}")
        lines.append("rate=UNAVAILABLE")
        return "\n".join(lines)

    counts = result["counts"]
    lines.append(
        f"commits={counts['total']} covered={counts['covered']} "
        f"exempt={counts['exempt']} unknown={counts['unknown']} "
        f"uncovered={counts['uncovered']} eligible={counts['eligible']} "
        f"merge_commits_excluded={counts['mergeCommitsExcluded']}"
    )
    rate = result.get("rate")
    if rate is None:
        lines.append(f"rate=NONE note={result.get('rateNote', 'unavailable')}")
    elif rate["exact"]:
        lines.append(f"covered_rate={rate['lower']:.4f} basis={rate['basis']}")
    else:
        lines.append(
            f"covered_rate_band={rate['lower']:.4f}..{rate['upper']:.4f} "
            f"basis={rate['basis']} (band width is the UNKNOWN commits)"
        )
    for commit in result["commits"]:
        if commit["state"] == COVERED:
            continue
        lines.append(
            f"{commit['state']} {commit['sha'][:12]} {commit['committedAt']} "
            f"{commit['subject']} :: {commit['reason']}"
        )
    return "\n".join(lines)
