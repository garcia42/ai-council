"""Reconcile shipped commits against council rows to measure review coverage.

The forecast ledger can say how often a seated council changed a decision.  It
cannot say how often a change shipped with no council at all, because a skipped
review writes nothing.  That makes the kill criterion a rate over councils that
happened rather than over decisions that were made.  This module narrows that
gap: it joins ``git log`` over an explicit window to the ``commits`` field of
council rows and classifies every commit in that window.

It narrows the gap rather than closing it, and the difference matters:

* The unit is **commits**, not decisions.  A commit count moves with branch and
  squash conventions, which have nothing to do with review.
* ``COVERED`` means a council row *names* the commit.  Nothing in this evidence
  establishes that the review happened **before** the commit reached the branch
  being measured.  Per-commit ``namedByRowAt`` and the ``coveredWithRowAfterCommit``
  count expose the lag so an after-the-fact recording is visible, but git does
  not durably record merge time and this module does not invent one.
* The ``commits`` field is written by hand by the same session that runs the
  council.  ``COVERED`` is therefore an **upper bound** on reviewed: the tool can
  prove under-review, it cannot prove review.

Four states, each defined:

``COVERED``
    A ``council`` row's ``commits`` array names this commit -- by object name
    (full or abbreviated, resolved against this repository), or by patch
    identity, so that a reviewed commit which was later rebased or cherry-picked
    still counts.

``EXEMPT``
    The commit message's **trailer block** carries ``Council-Exempt: <reason>``
    with a real reason.  Only an explicit, author-written exemption counts.
    There is no presumed exemption: a commit is never excused for looking
    mechanical, and a message that merely quotes the convention in its body does
    not exempt itself.

``UNKNOWN``
    A council row plausibly reviewed the commit but does not say so.  Rows
    written before the array convention recorded shapes such as
    ``{"base": "<sha>"}``, which names the branch point and not the reviewed
    commits.  When such a row names an object that resolves here, is an ancestor
    of the commit, and was written after the commit existed, the record cannot
    distinguish "reviewed" from "appended afterwards".

``UNCOVERED``
    None of the above.  The commit shipped and no row claims it.

A wrong denominator is worse than none, so the reconciler refuses to print a
rate at all -- and no counts, and no classification -- when the ledger cannot be
read in full, when git cannot answer in full, when no row attributable to *this*
repository has used the array convention, or when the window starts before the
first row that did.  The skip rate before that convention is not recoverable
from this evidence and is reported as such rather than estimated.

Every refusal path, and every path that measures nothing, is reported as
"cannot determine".  This tool must never say "clean" because it looked at
nothing: that is the single failure mode it exists to prevent.

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
from datetime import datetime, timezone
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
#: An object name as a council row may spell it.  Seven hex characters is git's
#: own default abbreviation floor; requiring it keeps branch names and free text
#: such as ``uncommitted-untracked`` out of the join entirely.
_OBJECT_NAME = re.compile(r"^[0-9a-f]{7,40}$")
#: A documentation placeholder such as ``<reason>`` is not a reason.
_PLACEHOLDER = re.compile(r"^<[^>]*>$")

_GIT_TIMEOUT_SECONDS = 120


class CoverageError(ValueError):
    """The reconciler was asked for something it cannot compute."""


class GitError(CoverageError):
    """Git could not answer a question about the repository under review.

    Derived from :class:`CoverageError` so it shares the package-wide
    ``ValueError`` base.  Every other error class in ``council_tools`` does, and
    ``cli.main`` catches that base; a bare ``RuntimeError`` escaping any call
    site would produce a traceback instead of a stable exit code.
    """


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
    is_merge: bool


@dataclass(frozen=True)
class LedgerEvidence:
    rows_read: int
    #: ``(ts, object names)`` for each council row whose ``commits`` is an array
    #: of full 40-character SHAs.  Only these establish that the convention was
    #: in force, which is what the instrumentation epoch means.
    convention_rows: tuple[tuple[datetime, tuple[str, ...]], ...]
    #: ``(ts, object names)`` for every council row whose ``commits`` is an
    #: array, including arrays spelled with abbreviations.  An array is always a
    #: statement of what was read, whether or not it obeys the convention.
    named_rows: tuple[tuple[datetime | None, tuple[str, ...]], ...]
    #: ``(ts, object names)`` for rows whose ``commits`` is present but is not
    #: an array -- the pre-convention ``{"base": ...}`` shape.
    ambiguous_bases: tuple[tuple[datetime | None, tuple[str, ...]], ...]
    ambiguous_rows: int
    council_rows_without_commits: int


def _text_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(
    value: str, *, field: str, require_timezone: bool = False
) -> datetime:
    """Parse an ISO-8601 instant, or a bare date read as UTC midnight.

    Window bounds are deliberately UTC rather than America/New_York.  Git
    records an explicit offset per commit, so normalizing both sides to UTC
    keeps the join independent of the operator's local zone.

    ``require_timezone`` is used when reading a ledger row's ``ts``.  The other
    modules that read that same field (``forecasts``, ``capture_schema``) reject
    a naive timestamp, and a row those refuse to read must not be able to
    silently establish this tool's instrumentation epoch.
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
        if require_timezone:
            raise CoverageError(f"{field} must include a timezone")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _object_names(value: Any) -> tuple[str, ...]:
    """Collect the object-name-shaped strings a value mentions, in order."""

    if isinstance(value, str):
        candidates: list[str] = [value]
    elif isinstance(value, Mapping):
        candidates = [item for item in value.values() if isinstance(item, str)]
    elif isinstance(value, (list, tuple)):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return ()
    return tuple(dict.fromkeys(item for item in candidates if _OBJECT_NAME.match(item)))


def read_ledger_evidence(log_path: str | Path) -> LedgerEvidence:
    """Summarize what the ledger claims about reviewed commits.

    Raises :class:`~council_tools.forecasts.LedgerError` when any line is
    unparseable, or when a council row records ``commits`` without a readable
    timezone-qualified ``ts``.  The caller turns that into a refusal rather than
    a rate: a ledger that cannot be read in full has an unknown numerator, and
    dropping a row whose ``ts`` cannot be read would move the epoch later and
    hide exactly the window this tool exists to measure.
    """

    rows = load_jsonl(log_path)
    label = Path(log_path).name
    convention: list[tuple[datetime, tuple[str, ...]]] = []
    named: list[tuple[datetime | None, tuple[str, ...]]] = []
    ambiguous: list[tuple[datetime | None, tuple[str, ...]]] = []
    ambiguous_rows = 0
    without_commits = 0

    for line_number, row in rows:
        is_council = row.get("kind") in COUNCIL_ROW_KINDS
        if "commits" not in row:
            if is_council:
                without_commits += 1
            continue
        commits = row.get("commits")
        raw_ts = row.get("ts")
        row_ts: datetime | None = None
        if isinstance(raw_ts, str):
            try:
                row_ts = parse_timestamp(raw_ts, field="ts", require_timezone=True)
            except CoverageError:
                row_ts = None

        if is_council and isinstance(commits, list):
            # An array is a statement of what was read, whether or not it obeys
            # the full-SHA convention.  Abbreviations are resolved later against
            # the repository; they must not be mistaken for ancestry bases.
            named.append((row_ts, _object_names(commits)))
            if all(isinstance(item, str) and _FULL_SHA.match(item) for item in commits):
                if row_ts is None:
                    raise LedgerError(
                        f"{label} line {line_number}: council row records commits "
                        "but has no readable timezone-qualified ts"
                    )
                convention.append((row_ts, tuple(commits)))
            continue

        ambiguous_rows += 1
        names = _object_names(commits)
        if names:
            ambiguous.append((row_ts, names))

    return LedgerEvidence(
        rows_read=len(rows),
        convention_rows=tuple(convention),
        named_rows=tuple(named),
        ambiguous_bases=tuple(ambiguous),
        ambiguous_rows=ambiguous_rows,
        council_rows_without_commits=without_commits,
    )


def _git(repo: Path, *arguments: str, stdin: str | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            text=True,
            capture_output=True,
            check=False,
            input=stdin,
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
    from being read as a git option.  A name that is ambiguous between several
    objects does not resolve, which fails toward UNCOVERED -- loud -- rather
    than toward COVERED.
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


def _trailer_block(message: str) -> list[str]:
    """Return the lines of the message's final paragraph.

    Git defines trailers as living in the last paragraph, so that is the only
    place this looks.  Scanning the whole message would let a commit that merely
    documents the convention -- or a revert quoting the original message, or a
    squash concatenating one exempt body -- exempt itself, which is fail-open
    inside a design whose stated principle is fail-closed.
    """

    paragraphs = [block for block in re.split(r"\n[ \t]*\n", message) if block.strip()]
    if not paragraphs:
        return []
    return paragraphs[-1].splitlines()


def _exempt_reason(message: str) -> str | None:
    """Return the reason from a ``Council-Exempt:`` trailer, if it has a real one.

    An exemption with an empty reason, or with a bare ``<placeholder>``, is not
    an exemption.  Failing closed here means a malformed trailer surfaces as
    UNCOVERED rather than silently removing the commit from the denominator.
    """

    for line in _trailer_block(message):
        # A trailer is not indented; an indented line is quoted prose.
        if line != line.lstrip():
            continue
        if not line.lower().startswith(EXEMPT_TRAILER.lower()):
            continue
        reason = line[len(EXEMPT_TRAILER) :].strip()
        if reason and not _PLACEHOLDER.match(reason):
            return reason
    return None


def _read_commits(repo: Path, *, ref: str) -> list[Commit]:
    """List every commit reachable from ``ref``, with the fields the join needs.

    The window is applied in Python, never by ``git log --since``.  That option
    is a *traversal cutoff*, not a filter: a commit older than the bound is
    marked uninteresting and its parents are pruned, so a single backdated
    commit anywhere in the walk silently drops in-window ancestors.  A slack
    margin does not bound that, because committer-date skew is unbounded.  A
    full walk costs milliseconds even on a few thousand commits, and a
    denominator that can silently empty itself is not worth the saving.
    """

    arguments = [
        # One NUL terminates each record and the first three newline-separated
        # lines are the structured fields.  A commit message cannot contain a
        # NUL, and it is the last field, so it may contain anything else.  A
        # NUL *between* fields would be ambiguous instead: a root commit has an
        # empty %P, which would collide with any multi-NUL record separator.
        "log",
        "--format=%H%n%cI%n%P%n%B%x00",
        "--end-of-options",
        ref,
        "--",
    ]
    raw = _git(repo, *arguments)

    commits: list[Commit] = []
    for record in raw.split("\x00"):
        if not record.strip("\n"):
            continue
        fields = record.lstrip("\n").split("\n", 3)
        if len(fields) < 4:
            raise GitError("git log returned a malformed record")
        sha, committed_raw, parents, message = fields
        if not _FULL_SHA.match(sha):
            raise GitError(f"git log returned a malformed object name: {sha!r}")
        subject = message.strip().splitlines()[0].strip() if message.strip() else ""
        commits.append(
            Commit(
                sha=sha,
                committed_at=parse_timestamp(committed_raw, field="committer date"),
                subject=subject,
                exempt_reason=_exempt_reason(message),
                is_merge=len(parents.split()) > 1,
            )
        )
    return commits


def _patch_ids(repo: Path, shas: Sequence[str]) -> dict[str, str]:
    """Map commit SHA to stable patch id, for the SHAs that have one.

    Batched through ``diff-tree --stdin`` so the cost is two git invocations
    regardless of how many commits are compared.  A commit with no diff -- an
    empty commit, or a merge under default diff rules -- produces no patch and
    is simply absent from the result, so it can never match anything.
    """

    if not shas:
        return {}
    # The trailing newline is required: `diff-tree --stdin` silently drops a
    # final line that is not terminated, which would omit one commit's patch
    # and make a genuine cherry-pick read as UNCOVERED.
    stdin = "".join(f"{sha}\n" for sha in shas)
    diffs = _git(repo, "diff-tree", "-p", "--no-color", "--stdin", stdin=stdin)
    if not diffs.strip():
        return {}
    output = _git(repo, "patch-id", "--stable", stdin=diffs)
    mapping: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and _FULL_SHA.match(parts[1]):
            mapping[parts[1]] = parts[0]
    return mapping


def _reviewed_index(
    repo: Path, evidence: LedgerEvidence
) -> tuple[dict[str, datetime | None], int, int]:
    """Resolve every object a council row named into ``sha -> earliest row ts``.

    Returns the index, the number of distinct names seen, and the number that
    resolved here.  The two counts are the wrong-repository tripwire: a window
    measured against a repository the councils were not about shows names seen
    but few or none resolved.
    """

    resolved: dict[str, str | None] = {}
    index: dict[str, datetime | None] = {}
    for row_ts, names in evidence.named_rows:
        for name in names:
            if name not in resolved:
                resolved[name] = _resolve_object(repo, name)
            sha = resolved[name]
            if sha is None:
                continue
            if sha not in index:
                index[sha] = row_ts
            elif row_ts is not None and (index[sha] is None or row_ts < index[sha]):
                index[sha] = row_ts
    return index, len(resolved), len({v for v in resolved.values() if v is not None})


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

    A git failure here is **not** swallowed.  Dropping a base would move its
    commits from UNKNOWN to UNCOVERED and turn "cannot determine" into a
    positive accusation, which is the opposite of this module's contract.
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
                # Both revisions are full SHAs validated above, so no
                # caller-controlled text reaches git's option parser here;
                # `--end-of-options` cannot be used because `--not` must
                # still be read as an option.
                reachable = _git(
                    repo, "rev-list", "--ancestry-path", ref_sha, "--not", base, "--"
                )
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
    reviewed: Mapping[str, datetime | None],
    patch_matched: Mapping[str, datetime | None],
    ambiguous: set[str],
) -> tuple[str, str, datetime | None]:
    if commit.sha in reviewed:
        return COVERED, "named in a council row commits array", reviewed[commit.sha]
    if commit.sha in patch_matched:
        return (
            COVERED,
            "patch-identical to a commit named in a council row",
            patch_matched[commit.sha],
        )
    if commit.exempt_reason:
        return EXEMPT, f"{EXEMPT_TRAILER} {commit.exempt_reason}", None
    if commit.sha in ambiguous:
        return (
            UNKNOWN,
            "a pre-convention council row names an ancestor but not this commit",
            None,
        )
    return UNCOVERED, "no council row names this commit", None


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

    repo_path = Path(repo).resolve()
    result: dict[str, Any] = {
        "tool": "council-coverage",
        "determined": False,
        "refusals": [],
        "window": {
            "repo": str(repo_path),
            "ref": ref,
            "refSha": None,
            "since": _text_timestamp(since),
            "until": _text_timestamp(until),
            "timezone": "UTC",
            "includesMerges": include_merges,
        },
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

    if not Path(log_path).exists():
        return refuse(
            "ledger-unreadable",
            f"ledger file does not exist: {log_path}; this is a path problem, "
            "not a missing convention",
        )
    try:
        evidence = read_ledger_evidence(log_path)
    except LedgerError as exc:
        return refuse("ledger-unreadable", str(exc))
    except OSError as exc:
        return refuse("ledger-unreadable", f"cannot read {log_path}: {exc}")

    result["ledger"] = {
        "rowsRead": evidence.rows_read,
        "conventionCouncilRows": len(evidence.convention_rows),
        "councilRowsNamingCommits": len(evidence.named_rows),
        "councilRowsWithoutCommits": evidence.council_rows_without_commits,
        "ambiguousCommitRows": evidence.ambiguous_rows,
    }

    try:
        if _git(repo_path, "rev-parse", "--is-inside-work-tree").strip() != "true":
            return refuse(
                "git-unavailable", f"{repo_path} is not inside a git work tree"
            )
        if _git(repo_path, "rev-parse", "--is-shallow-repository").strip() == "true":
            # Truncated history is the git-side equivalent of a ledger that
            # cannot be read in full: the denominator is missing commits and
            # nothing in the output would say so.
            return refuse(
                "shallow-repository",
                f"{repo_path} is a shallow clone, so its history is truncated and "
                "the denominator would silently omit commits; fetch full history",
            )
        ref_sha = _resolve_object(repo_path, ref)
        if ref_sha is None:
            return refuse("git-unavailable", f"ref does not resolve to a commit: {ref}")
        result["window"]["refSha"] = ref_sha

        reviewed, names_seen, names_resolved = _reviewed_index(repo_path, evidence)
        result["ledger"]["reviewedNamesSeen"] = names_seen
        result["ledger"]["reviewedNamesResolvedInRepo"] = names_resolved

        # The epoch must be attributable to the repository under review.  The
        # shared ledger carries no repo key, so a council about a *different*
        # repository would otherwise open the gate here: the earliest convention
        # row in the live ledger is a Truth-and-Reconciliation council, and
        # accepting it would assert instrumentation this repository never had.
        attributable = [
            row_ts
            for row_ts, names in evidence.convention_rows
            if any(_resolve_object(repo_path, name) is not None for name in names)
        ]
        epoch = instrumented_since or (min(attributable) if attributable else None)
        result["instrumentation"]["epoch"] = (
            _text_timestamp(epoch) if epoch is not None else None
        )
        result["instrumentation"]["attributableConventionRows"] = len(attributable)
        if epoch is None:
            if evidence.convention_rows:
                return refuse(
                    "no-repo-attributable-instrumentation",
                    "no council row using the commits-array convention names any "
                    f"object that resolves in {repo_path}; the ledger is shared "
                    "across repositories and carries no repo field, so this "
                    "repository has no known denominator",
                )
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
                f"using the commits-array convention for this repository is "
                f"{_text_timestamp(epoch)}; the skip rate before that convention "
                "is unrecoverable and is not estimated. Re-run with "
                f"--since {_text_timestamp(epoch)} for the measurable sub-window",
            )

        reachable = _read_commits(repo_path, ref=ref)
        in_window = [
            commit for commit in reachable if since <= commit.committed_at < until
        ]
        merges_excluded = 0
        if not include_merges:
            merges_excluded = sum(1 for commit in in_window if commit.is_merge)
            in_window = [commit for commit in in_window if not commit.is_merge]

        if not in_window:
            return refuse(
                "empty-window",
                f"no commits in {_text_timestamp(since)}..{_text_timestamp(until)} "
                f"on {ref} in {repo_path} (merge commits excluded: {merges_excluded}); "
                "measuring nothing is not the same as measuring full coverage",
            )

        # Patch identity, only for what object identity could not settle. A
        # reviewed commit that was rebased or cherry-picked onto the measured
        # ref is a different object with the same patch; recorded SHAs that
        # matched nothing are exactly the candidates.
        patch_matched: dict[str, datetime | None] = {}
        unmatched_commits = [c.sha for c in in_window if c.sha not in reviewed]
        unmatched_reviewed = [sha for sha in reviewed if sha not in {c.sha for c in in_window}]
        if unmatched_commits and unmatched_reviewed:
            reviewed_patches = _patch_ids(repo_path, unmatched_reviewed)
            by_patch: dict[str, datetime | None] = {}
            for sha, patch in reviewed_patches.items():
                row_ts = reviewed[sha]
                if patch not in by_patch or (
                    row_ts is not None
                    and (by_patch[patch] is None or row_ts < by_patch[patch])
                ):
                    by_patch[patch] = row_ts
            for sha, patch in _patch_ids(repo_path, unmatched_commits).items():
                if patch in by_patch:
                    patch_matched[sha] = by_patch[patch]

        ambiguous, resolved_bases = _ambiguously_covered(
            repo_path, evidence=evidence, commits=in_window, ref_sha=ref_sha
        )
    except (GitError, CoverageError) as exc:
        # A git failure means this tool cannot answer, never that a commit is
        # unreviewed.  Swallowing one would convert UNKNOWN into UNCOVERED and
        # exit 3 into exit 1 -- a false alarm dressed as a finding.
        return refuse("git-unavailable", str(exc))

    result["ledger"]["ambiguousBasesResolvedInRepo"] = resolved_bases

    classified = []
    counts = {state: 0 for state in STATE_PRECEDENCE}
    recorded_after = 0
    for commit in sorted(in_window, key=lambda item: (item.committed_at, item.sha)):
        state, reason, row_ts = _classify(
            commit,
            reviewed=reviewed,
            patch_matched=patch_matched,
            ambiguous=ambiguous,
        )
        counts[state] += 1
        entry = {
            "sha": commit.sha,
            "committedAt": _text_timestamp(commit.committed_at),
            "subject": commit.subject,
            "state": state,
            "reason": reason,
            "namedByRowAt": _text_timestamp(row_ts) if row_ts is not None else None,
        }
        if state == COVERED and row_ts is not None and row_ts > commit.committed_at:
            recorded_after += 1
        classified.append(entry)

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
        "coveredWithRowAfterCommit": recorded_after,
    }
    # Ordered so the actionable states are read first, not buried among a long
    # chronological run of UNKNOWNs.
    order = {state: index for index, state in enumerate(STATE_PRECEDENCE)}
    result["commits"] = sorted(
        classified,
        key=lambda item: (-order[item["state"]], item["committedAt"], item["sha"]),
    )
    if eligible <= 0:
        result["rateNote"] = (
            "every commit in the window is author-exempted; there is no eligible "
            "population and no rate"
        )
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
    """0 clean, 1 something shipped unreviewed, 3 cannot determine.

    Nothing returns 0 unless an eligible population was actually measured and
    every commit in it was placed.  "I looked at nothing" and "everything was
    exempted by its own author" are both cannot-determine, never clean.
    """

    if not result.get("determined"):
        return 3
    counts = result.get("counts", {})
    if counts.get("uncovered", 0) > 0:
        return 1
    if counts.get("unknown", 0) > 0:
        return 3
    if counts.get("eligible", 0) <= 0:
        return 3
    return 0


def format_coverage(result: Mapping[str, Any]) -> str:
    """Render the reconciliation as stable ``key=value`` lines."""

    window = result["window"]
    instrumentation = result["instrumentation"]
    lines = [
        f"repo={window['repo']} ref={window['ref']} "
        f"ref_sha={(window.get('refSha') or 'unresolved')[:12]}",
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
            f"convention_rows={ledger['conventionCouncilRows']} "
            f"rows_naming_commits={ledger['councilRowsNamingCommits']} "
            f"council_rows_without_commits={ledger['councilRowsWithoutCommits']} "
            f"ambiguous_commit_rows={ledger['ambiguousCommitRows']}"
        )
        if "reviewedNamesResolvedInRepo" in ledger:
            lines.append(
                f"reviewed_names_resolved_in_repo="
                f"{ledger['reviewedNamesResolvedInRepo']}/{ledger['reviewedNamesSeen']}"
                "  (a low ratio means this ledger's councils were about another repo)"
            )
    if not result["determined"]:
        for refusal in result["refusals"]:
            lines.append(f"REFUSED code={refusal['code']} detail={refusal['detail']}")
        lines.append("rate=UNAVAILABLE  (cannot determine; exit 3)")
        return "\n".join(lines)

    counts = result["counts"]
    lines.append(
        f"commits={counts['total']} covered={counts['covered']} "
        f"exempt={counts['exempt']} unknown={counts['unknown']} "
        f"uncovered={counts['uncovered']} eligible={counts['eligible']} "
        f"merge_commits_excluded={counts['mergeCommitsExcluded']} "
        f"covered_with_row_after_commit={counts['coveredWithRowAfterCommit']}"
    )
    rate = result.get("rate")
    if rate is None:
        lines.append(f"rate=NONE note={result.get('rateNote', 'unavailable')}")
    elif rate["exact"]:
        lines.append(f"covered_rate={rate['lower']:.4f} basis={rate['basis']}")
    else:
        lines.append(
            f"covered_rate_band={rate['lower']:.4f}..{rate['upper']:.4f} "
            f"basis={rate['basis']} (band width is the UNKNOWN commits; "
            "read the lower bound, not the midpoint)"
        )
    for commit in result["commits"]:
        if commit["state"] == COVERED:
            continue
        lines.append(
            f"{commit['state']} {commit['sha'][:12]} {commit['committedAt']} "
            f"{commit['subject']} :: {commit['reason']}"
        )
    return "\n".join(lines)
