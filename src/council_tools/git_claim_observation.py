"""Read what the server said about a claim push, without deciding what it means.

The claim protocol decides which of several sessions owns an issue by asking a
server to create one ref and refuse every later attempt.  The server states its
answer in the porcelain of the push.  Until now nothing read it, so the only
signal a caller had was the process exit status — and **that status cannot
distinguish the answers that matter.**

Measured on the pinned ``git 2.39.5`` against a bare repository at a ``file://``
URL, with ``push --porcelain --no-verify <url> --force-with-lease=<ref>:
<oid>:<ref>`` (``\\t`` is a literal TAB):

===========================  ====  ==========================================
outcome                      exit  status line on **stdout**
===========================  ====  ==========================================
created                         0  ``*\\t<oid>:<ref>\\t[new branch]``
already current, same object    0  ``=\\t<oid>:<ref>\\t[up to date]``
rejected, different object      1  ``!\\t<oid>:<ref>\\t[rejected] (stale info)``
remote absent                 128  *(no porcelain at all)*
===========================  ====  ==========================================

**Exit 0 covers the first two**, and they are different facts about who owns the
claim: one says this caller created it, the other says it already existed and
happens to hold the same object.

Reading the line exactly is where the false successes live, and each of these was
measured rather than assumed:

* the porcelain is on **stdout** while ``error:`` and ``fatal:`` are on stderr, so
  a parser fed the merged streams sees lines the format does not describe;
* each line is **TAB-separated**, and the summary contains spaces and brackets
  (``[rejected] (stale info)``), so splitting on whitespace destroys it;
* a ref name may contain a colon and the source side may be empty, so the pair is
  split on the **last** colon rather than the first;
* the ``Done`` trailer is the only thing distinguishing output the command
  finished writing from output truncated by a deadline or a killed process;
* every byte is attacker-influenced, because a remote chooses its own strings and
  can emit a line shaped like a status line for a ref nobody asked about.

**None of this is authorization.**  Observing that a server created a ref is
evidence about server state *at one moment*, not proof that this session owns the
claim: the object still has to be read back from a fresh repository before anyone
acts on it.  So :class:`ClaimPushObservation` refuses boolean coercion and carries
no success field, the same refusal ``GitCommandResult`` and the admission result
already make.

This module performs no push, spawns no process, and performs no network,
filesystem or subprocess access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The trailer Git writes once it has finished reporting.  Absent output is
#: incomplete rather than empty, and the distinction is the whole point.
PORCELAIN_TRAILER = "Done"

#: The header naming the remote.  Recognised so it is not mistaken for a status
#: line, never parsed for meaning.
PORCELAIN_HEADER_PREFIX = "To "

#: Bounds applied *before* parsing.  A remote decides how much it writes, so the
#: volume has to be capped by this side.
MAX_PORCELAIN_LINES = 256
MAX_PORCELAIN_BYTES = 64 * 1024

#: The measured outcomes, keyed by ``(flag, summary)``.  Both halves are required:
#: a flag alone is not specific enough, and matching a summary loosely is how an
#: unfamiliar answer becomes the nearest familiar one.
CREATED = "created"
ALREADY_CURRENT = "already-current"
REJECTED = "rejected"

_MEASURED_OUTCOMES: dict[tuple[str, str], str] = {
    ("*", "[new branch]"): CREATED,
    ("=", "[up to date]"): ALREADY_CURRENT,
    ("!", "[rejected] (stale info)"): REJECTED,
}


class GitClaimObservationError(ValueError):
    """A stable, field-addressed observation failure."""

    def __init__(self, code: str, field: str, detail: str = ""):
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(
            f"git claim observation {code} at {field}" + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class ClaimPushObservation:
    """What the server said, at one moment. Not a statement about ownership.

    The name says ``observation`` rather than ``result`` deliberately, and the
    type refuses ``bool()``, because the only safe use of this value is as an
    input to verification from a fresh repository.
    """

    outcome: str
    flag: str
    source: str
    destination: str
    summary: str

    def __bool__(self) -> bool:
        raise TypeError(
            "a claim push observation is server state, not proof of ownership"
        )


def observe_claim_push(stdout: Any, *, expected_ref: Any) -> ClaimPushObservation:
    """Read the porcelain of one claim push.

    ``stdout`` is the **standard output** of the push and nothing else: passing
    merged streams, or standard error, is refused rather than parsed.
    """

    if not isinstance(expected_ref, str) or not expected_ref or expected_ref != expected_ref.strip():
        raise GitClaimObservationError("invalid-expected-ref", "expected_ref")
    if not isinstance(stdout, str):
        raise GitClaimObservationError("invalid-stdout", "stdout")
    if len(stdout.encode("utf-8", "surrogatepass")) > MAX_PORCELAIN_BYTES:
        raise GitClaimObservationError("output-too-large", "stdout")

    lines = stdout.split("\n")
    if len(lines) > MAX_PORCELAIN_LINES:
        raise GitClaimObservationError("too-many-lines", "stdout", str(len(lines)))

    body = [line for line in lines if line != ""]
    if not body:
        raise GitClaimObservationError("empty-output", "stdout")

    # The trailer first: without it the rest may be a truncated prefix, and a
    # prefix that happens to contain a status line is the worst case of all.
    if body[-1] != PORCELAIN_TRAILER:
        raise GitClaimObservationError("missing-trailer", "stdout")

    matches: list[ClaimPushObservation] = []
    for index, line in enumerate(body[:-1]):
        if line.startswith(PORCELAIN_HEADER_PREFIX):
            continue
        # An `error:` or `fatal:` line belongs on the other stream. Seeing one
        # here means the caller merged the streams, and parsing on would let a
        # diagnostic become a reference.
        if line.startswith("error:") or line.startswith("fatal:"):
            raise GitClaimObservationError("diagnostic-on-stdout", f"stdout[{index}]")

        fields = line.split("\t")
        if len(fields) != 3:
            raise GitClaimObservationError("unparseable-line", f"stdout[{index}]")
        flag, pair, summary = fields
        if ":" not in pair:
            raise GitClaimObservationError("unparseable-reference", f"stdout[{index}]")
        # Last colon, not first: a ref name may contain one and the source side
        # may be empty.
        source, _, destination = pair.rpartition(":")
        if destination != expected_ref:
            continue
        outcome = _MEASURED_OUTCOMES.get((flag, summary))
        if outcome is None:
            raise GitClaimObservationError(
                "unrecognised-outcome", f"stdout[{index}]", f"{flag} {summary}"
            )
        matches.append(
            ClaimPushObservation(
                outcome=outcome,
                flag=flag,
                source=source,
                destination=destination,
                summary=summary,
            )
        )

    if not matches:
        raise GitClaimObservationError("reference-not-reported", "stdout", expected_ref)
    if len(matches) > 1:
        raise GitClaimObservationError(
            "reference-reported-more-than-once", "stdout", expected_ref
        )
    return matches[0]
