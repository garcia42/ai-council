"""Parametrised outcome checks for the three claim shapes councils issue most.

Run as a script, never imported by the runtime: it uses the standard library
only, so a check grades the same whichever interpreter ``resolve-due`` finds and
cannot be refused by the package's interpreter guard. Every check takes
``--claim``, the outcome's claim text verbatim, and prints it first, so the claim
and the predicate that grades it are one argv and one evidence record.

Exit codes follow the ``check`` schema: 10 TRUE, 11 FALSE, and 12 when the
evidence cannot decide, which leaves the outcome for a human. Every rule below
grades TRUE or FALSE only from evidence about the state at the end of the
deadline (America/New_York); "it is true now" stands in for "it was true then"
only where the shape makes that sound, and the docstring of each check says
where that is.

    claim_checks.py spec <shape> --claim TEXT ... [--timeout-seconds N]

prints the ``sharedOutcome.check`` object for an attempt spec, after parsing
the arguments with the very parser the check will run with.

    merged-by          --repo DIR --sha SHA --branch NAME [--remote NAME]
    release-active-by  --repo DIR --sha SHA --pointer PATH [--pointer PATH ...]
    round-sealed       (--url URL | --json-file PATH | --jsonl-file PATH)
                       [--items DOTTED] --match K=V ... --require K=V ...
                       [--require-prefix K=P ...] [--at-field K]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

EXIT_TRUE = 10
EXIT_FALSE = 11
EXIT_UNDETERMINED = 12
#: The interpreter a generated check names. Absolute, as the schema requires,
#: and the system one: a venv can be deleted, /usr/bin/python3 cannot quietly be.
CHECK_PYTHON = "/usr/bin/python3"
#: Where the installed runtime keeps this file. ``spec`` names it by default so a
#: check never points into a session worktree that will be deleted.
INSTALLED_SCRIPT = "/home/trader/council-tools/src/council_tools/claim_checks.py"
DEADLINE_ZONE = ZoneInfo("America/New_York")
GIT_TIMEOUT_SECONDS = 60
SOURCE_LIMIT_BYTES = 16 * 1024 * 1024
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
#: A commit id standing alone, not 40 hex digits out of a 64-hex SHA-256 digest.
NAMED_SHA_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])")
DEFAULT_TIMEOUTS = {"merged-by": 180, "release-active-by": 60, "round-sealed": 60}


class Undetermined(Exception):
    """The evidence available cannot decide the claim; a human grades it."""


# --------------------------------------------------------------------- helpers


def deadline_end(deadline: date) -> datetime:
    """The first instant after the deadline day, in New York, as UTC."""

    start = datetime.combine(deadline + timedelta(days=1), datetime.min.time())
    return start.replace(tzinfo=DEADLINE_ZONE).astimezone(timezone.utc)


def git_env() -> dict[str, str]:
    """The caller's environment without one GIT_* variable.

    A check can be launched from inside a git hook or another git command, whose
    GIT_DIR/GIT_INDEX_FILE/GIT_WORK_TREE would silently point every child git at
    the wrong repository.
    """

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


def git(repo: str, *args: str, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            env=git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Undetermined(f"git {args[0]} could not run: {exc}") from exc
    if result.returncode not in ok:
        raise Undetermined(
            f"git {' '.join(args)} exited {result.returncode}: {result.stderr.strip()[:500]}"
        )
    return result


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    code = git(repo, "merge-base", "--is-ancestor", ancestor, descendant, ok=(0, 1)).returncode
    return code == 0


def resolve_commit(repo: str, rev: str) -> str:
    result = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", ok=(0, 1))
    sha = result.stdout.strip()
    if result.returncode != 0 or not FULL_SHA_RE.fullmatch(sha):
        raise Undetermined(f"{rev} is not a commit in {repo}")
    return sha


def key_value(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key, value


def absolute_path(text: str) -> str:
    if not text.startswith("/"):
        raise argparse.ArgumentTypeError(f"{text!r} must be an absolute path")
    return text


def commit_id(text: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{7,40}", text):
        raise argparse.ArgumentTypeError(f"{text!r} must be a hex commit id")
    return text


def require_full_sha(sha: str) -> str:
    """A check names one commit for ever; ``spec`` expands short ids, so a short one here is a hand edit."""

    if not FULL_SHA_RE.fullmatch(sha):
        raise Undetermined(f"{sha} is not a full 40-hex commit id")
    return sha


def iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} must be YYYY-MM-DD") from exc


# ------------------------------------------------------------------ merged-by


def _reflog(repo: str, ref: str) -> list[tuple[str, int]]:
    """(sha, unix time) for every reflog entry of ``ref``; none if it has no log."""

    result = git(repo, "log", "-g", "--date=unix", "--format=%H %gd", ref, "--", ok=(0, 128))
    entries = []
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"([0-9a-f]{40}) \S+@\{(\d+)\}", line.strip())
        if match:
            entries.append((match.group(1), int(match.group(2))))
    return entries


def _introducing_commit(repo: str, sha: str, tip: str) -> str:
    """The first-parent commit of ``tip`` through which ``sha`` entered the branch."""

    first_parent = git(repo, "rev-list", "--first-parent", tip).stdout.split()
    if sha in first_parent:
        return sha
    descendants = set(git(repo, "rev-list", "--ancestry-path", f"{sha}..{tip}").stdout.split())
    for commit in reversed(first_parent):
        if commit in descendants:
            return commit
    raise Undetermined(f"no first-parent commit of {tip} introduces {sha}")


def merged_by(args: argparse.Namespace) -> tuple[int, str]:
    """TRUE iff ``--sha`` was on ``--remote``/``--branch`` by the end of the deadline.

    The remote branch is read with ``ls-remote`` (and its tip fetched if absent),
    so a stale local clone cannot hide a merge.

    * Not an ancestor of the remote tip now: FALSE. A merge is not undone short
      of a force-push, which this check does not try to see.
    * TRUE needs positive evidence from before the deadline ended: a reflog entry
      of ``refs/remotes/<remote>/<branch>`` or ``refs/heads/<branch>`` stamped no
      later than the deadline whose commit contains the sha (a push or fetch from
      this clone saw it there), or a merge commit that GitHub itself created
      before the deadline.
    * FALSE also when the first-parent commit that brought the sha into the
      branch was committed after the deadline: it cannot have entered sooner.
    * Anything else, for example a fast-forward pushed from another machine and
      never fetched here before the deadline, is undetermined. A commit's own date
      says when it was written, not when it was merged.
    """

    repo, remote, branch, sha = args.repo, args.remote, args.branch, require_full_sha(args.sha)
    end = int(deadline_end(args.deadline).timestamp())
    listed = git(repo, "ls-remote", "--exit-code", remote, f"refs/heads/{branch}", ok=(0, 2))
    if listed.returncode == 2 or not listed.stdout.strip():
        raise Undetermined(f"{remote} has no branch {branch}")
    tip = listed.stdout.split()[0]
    if git(repo, "cat-file", "-e", f"{tip}^{{commit}}", ok=(0, 1, 128)).returncode != 0:
        git(repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", remote,
            f"refs/heads/{branch}")
    if git(repo, "cat-file", "-e", f"{sha}^{{commit}}", ok=(0, 1, 128)).returncode != 0:
        raise Undetermined(f"{sha} is not a commit in {repo}")
    if not is_ancestor(repo, sha, tip):
        return EXIT_FALSE, f"{sha} is not on {remote}/{branch} (tip {tip})"
    for ref in (f"refs/remotes/{remote}/{branch}", f"refs/heads/{branch}"):
        before = [entry for entry in _reflog(repo, ref) if entry[1] <= end]
        for seen, stamp in before:
            if git(repo, "cat-file", "-e", f"{seen}^{{commit}}", ok=(0, 1, 128)).returncode:
                continue
            if is_ancestor(repo, sha, seen):
                when = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                return EXIT_TRUE, f"{ref} held {seen}, which contains {sha}, at {when}"
    introducing = _introducing_commit(repo, sha, tip)
    shown = git(repo, "show", "-s", "--format=%P%x09%ce%x09%ct", introducing).stdout.strip()
    parents, committer, committed = shown.split("\t")
    if int(committed) > end:
        return EXIT_FALSE, f"{sha} entered {branch} through {introducing}, committed after the deadline"
    if len(parents.split()) > 1 and committer == "noreply@github.com":
        return EXIT_TRUE, f"GitHub merged {sha} into {branch} as {introducing} before the deadline"
    raise Undetermined(
        f"{sha} is on {remote}/{branch} now, but nothing recorded here shows it there "
        "before the deadline ended"
    )


# ---------------------------------------------------------- release-active-by


def _pointer_state(path: str) -> tuple[list[str], float]:
    """The commits a pointer names, and when it last changed."""

    try:
        info = os.lstat(path)
        if os.path.islink(path):
            text = os.readlink(path)
        else:
            with open(path, "rb") as handle:
                text = handle.read(SOURCE_LIMIT_BYTES).decode("utf-8", errors="replace")
    except OSError as exc:
        raise Undetermined(f"cannot read pointer {path}: {exc}") from exc
    commits = sorted(set(NAMED_SHA_RE.findall(text)))
    if not commits:
        raise Undetermined(f"pointer {path} names no full commit id")
    return commits, info.st_mtime


def release_active_by(args: argparse.Namespace) -> tuple[int, str]:
    """TRUE iff every ``--pointer`` named only releases containing ``--sha`` at the deadline.

    A pointer is a file or symlink that names the active release by its full
    commit id: an installed shim's ``EXPECTED_COMMIT``, a unit file's
    ``releases/<sha>`` path, a ``current`` symlink. Every 40-hex id it contains
    must have ``--sha`` as an ancestor in ``--repo``.

    The state now is the state at the deadline only for a pointer that has not
    changed since (its mtime, or a symlink's own lstat mtime, is no later than the
    deadline's end). So: any unchanged pointer that lacks the sha is FALSE; all
    pointers unchanged and all containing it is TRUE; a pointer rewritten after
    the deadline, by a later install say, leaves the claim to a human.
    """

    require_full_sha(args.sha)
    end = deadline_end(args.deadline).timestamp()
    unchanged_ok, changed = [], []
    for path in args.pointer:
        commits, mtime = _pointer_state(path)
        for commit in commits:
            resolve_commit(args.repo, commit)
        contains = all(is_ancestor(args.repo, args.sha, commit) for commit in commits)
        if mtime > end:
            changed.append(path)
        elif not contains:
            return EXIT_FALSE, f"{path} names {commits}, without {args.sha}, since before the deadline"
        else:
            unchanged_ok.append(path)
    if changed:
        raise Undetermined(f"rewritten after the deadline, so silent about it: {changed}")
    return EXIT_TRUE, f"{unchanged_ok} named releases containing {args.sha} since before the deadline"


# --------------------------------------------------------------- round-sealed


def _load_source(args: argparse.Namespace) -> list:
    try:
        if args.url:
            with urllib.request.urlopen(args.url, timeout=30) as response:
                raw = response.read(SOURCE_LIMIT_BYTES + 1)
        else:
            with open(args.json_file or args.jsonl_file, "rb") as handle:
                raw = handle.read(SOURCE_LIMIT_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise Undetermined(f"cannot read the source: {exc}") from exc
    if len(raw) > SOURCE_LIMIT_BYTES:
        raise Undetermined("source is larger than the check will read")
    text = raw.decode("utf-8", errors="replace")
    try:
        if args.jsonl_file:
            items = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            value = json.loads(text)
            for part in filter(None, (args.items or "").split(".")):
                value = value[part]
            items = value if isinstance(value, list) else [value]
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise Undetermined(f"source is not the expected shape: {exc}") from exc
    return items


def _field(item, dotted: str):
    value = item
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _text(value) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def _timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def round_sealed(args: argparse.Namespace) -> tuple[int, str]:
    """TRUE iff a record selected by ``--match`` met every ``--require`` by the deadline.

    Records come from a local JSON API (``--url``), a JSON file, or a JSONL
    ledger, ``--items`` naming the list inside a JSON document. ``--match K=V``
    selects the records the claim is about; ``--require K=V`` and
    ``--require-prefix K=P`` say what "sealed" means for them. Keys may be dotted.

    * The source cannot be read, or nothing matches: undetermined. A selector
      that matches nothing is far likelier a mistake than a fact.
    * With ``--at-field``, a matched record passes only if that timestamp is no
      later than the deadline: TRUE if one does, otherwise FALSE.
    * Without it, sealing is taken to be permanent: none sealed now is FALSE,
      and one sealed now is TRUE only within ``--max-lateness-hours`` of the
      deadline, since nothing then shows it was sealed in time.
    """

    end = deadline_end(args.deadline)
    matched = [
        item
        for item in _load_source(args)
        if all(_text(_field(item, key)) == value for key, value in args.match)
    ]
    if not matched:
        raise Undetermined("no record matches the selector")

    def sealed(item) -> bool:
        return all(_text(_field(item, key)) == value for key, value in args.require) and all(
            (_text(_field(item, key)) or "").startswith(prefix)
            for key, prefix in args.require_prefix
        )

    passing = [item for item in matched if sealed(item)]
    if args.at_field:
        stamps = [_timestamp(_field(item, args.at_field)) for item in passing]
        if any(stamp is None for stamp in stamps):
            raise Undetermined(f"a sealed record has no timezone-aware {args.at_field}")
        if any(stamp <= end for stamp in stamps):
            return EXIT_TRUE, f"{len(matched)} matched; one sealed at or before {end.isoformat()}"
        return EXIT_FALSE, f"{len(matched)} matched; none sealed by {end.isoformat()}"
    if not passing:
        return EXIT_FALSE, f"{len(matched)} matched; none sealed now, so none by the deadline"
    lateness = datetime.now(timezone.utc) - end
    if lateness > timedelta(hours=args.max_lateness_hours):
        raise Undetermined(
            f"sealed now, but checked {lateness} after the deadline with no --at-field to date it"
        )
    return EXIT_TRUE, f"{len(matched)} matched; sealed when checked {lateness} after the deadline"


# ------------------------------------------------------------------- the CLI

SHAPES = {
    "merged-by": merged_by,
    "release-active-by": release_active_by,
    "round-sealed": round_sealed,
}


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="claim_checks.py", description=__doc__.split("\n\n")[0])
    shapes = top.add_subparsers(dest="shape", required=True)

    def shape(name: str) -> argparse.ArgumentParser:
        sub = shapes.add_parser(name)
        sub.add_argument("--claim", required=True)
        sub.add_argument("--deadline", required=True, type=iso_date,
                         help="the outcome's resolutionDate")
        return sub

    merged = shape("merged-by")
    merged.add_argument("--repo", required=True, type=absolute_path)
    merged.add_argument("--sha", required=True, type=commit_id)
    merged.add_argument("--branch", required=True)
    merged.add_argument("--remote", default="origin")

    release = shape("release-active-by")
    release.add_argument("--repo", required=True, type=absolute_path)
    release.add_argument("--sha", required=True, type=commit_id)
    release.add_argument("--pointer", required=True, action="append", type=absolute_path)

    rounds = shape("round-sealed")
    source = rounds.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--json-file", type=absolute_path)
    source.add_argument("--jsonl-file", type=absolute_path)
    rounds.add_argument("--items")
    rounds.add_argument("--match", action="append", type=key_value, required=True)
    rounds.add_argument("--require", action="append", type=key_value, default=[])
    rounds.add_argument("--require-prefix", action="append", type=key_value, default=[])
    rounds.add_argument("--at-field")
    rounds.add_argument("--max-lateness-hours", type=int, default=24)
    return top


def run(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    if args.shape == "round-sealed" and not (args.require or args.require_prefix):
        print("round-sealed needs at least one --require or --require-prefix", file=sys.stderr)
        return 2
    print(f"claim: {args.claim}")
    try:
        code, reason = SHAPES[args.shape](args)
    except Undetermined as exc:
        code, reason = EXIT_UNDETERMINED, str(exc)
    verdict = {EXIT_TRUE: "TRUE", EXIT_FALSE: "FALSE"}.get(code, "UNDETERMINED")
    print(f"{verdict}: {reason}")
    return code


def spec(argv: list[str]) -> dict:
    """The ``sharedOutcome.check`` object that runs ``argv`` as a check.

    Parsed now with the check's own parser, so a spec that prints will parse
    when ``resolve-due`` runs it. Short commit ids are expanded here, while the
    author can still see which commit they name.
    """

    front = argparse.ArgumentParser(add_help=False)
    front.add_argument("--timeout-seconds", type=int)
    front.add_argument("--script", default=INSTALLED_SCRIPT, type=absolute_path)
    front.add_argument("--python", default=CHECK_PYTHON, type=absolute_path)
    known, rest = front.parse_known_args(argv)
    args = parser().parse_args(rest)
    if getattr(args, "sha", None) and not FULL_SHA_RE.fullmatch(args.sha):
        try:
            full = resolve_commit(args.repo, args.sha)
        except Undetermined as exc:
            raise SystemExit(f"spec: {exc}") from exc
        positions = [i for i in range(1, len(rest)) if rest[i - 1] == "--sha"]
        if len(positions) != 1:
            raise SystemExit("spec: pass the commit as a separate argument: --sha <id>")
        rest[positions[0]] = full
    timeout = known.timeout_seconds or DEFAULT_TIMEOUTS[args.shape]
    if not 1 <= timeout <= 600:
        raise SystemExit("spec: --timeout-seconds must be from 1 to 600")
    return {
        "type": "command",
        "argv": [known.python, known.script, *rest],
        "cwd": "/",
        "timeoutSeconds": timeout,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["spec"]:
        print(json.dumps(spec(argv[1:]), indent=2))
        return 0
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
