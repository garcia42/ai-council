"""Render what the seats wrote when a round found the work was not one outcome.

``seal_qualification`` refuses any decision that is not ``eligible``, so a round
deriving ``needs-split`` has no sealed contract and no ``reviewRef`` -- and
:func:`~council_tools.ticket_qualification.render_ticket_body` therefore has
nothing to render it against.  The seats' ``splitReasons`` are the entire product
of such a round, and until now they existed only wherever the session that
collected them happened to leave them.  They reached an issue when an operator
remembered to paste them, which is a habit rather than a mechanism, and the seats'
most useful output has consistently been those sentences rather than the counts.

Two properties decide what this record may carry, and they cut in opposite
directions.

**Verbatim and attributed.**  A reason belongs to the seat that wrote it.  A
reason an operator has summarized is the operator's sentence, and after the fact
nothing distinguishes the two.  So the text is reproduced exactly, under the seat
that submitted it, in the required seat order.

**The submitted values are left out.**  This makes the record *narrower* than the
one an operator writes by hand today, and it is the point rather than an
omission.  ``runtime/ticket-sizing-contract.md``:

    If a seat is re-run, re-run it against the same projection digest.  A new
    projection digest is different work: its estimate is a first opinion on that
    work, not a second opinion on the old work.

A re-contract produces a new projection, so the next round is a first opinion --
and putting the previous round's submitted estimate, priority or confidence where
the next round's brief gets written from is exactly the anchor-and-adjust that
withholding those fields from reviewed content exists to prevent.  What *is*
carried is different in kind: the derived state, its reason codes and the
projection digest are computed by the validator rather than offered by a seat,
and the digest is what ties the reasons to the content they were written about.

**A round can reach this outcome with no seat reason at all.**  Any estimate over
three derives ``needs-split`` on its own, and ``splitReasons`` is empty exactly
when a seat judged the work single, so both seats can call it one outcome and the
ceiling still splits it.  Rendering an empty section there would read as seats
having had nothing to say, rather than as an outcome no seat asked for, so that
case is stated in words.

Nothing here publishes anything: this returns text.  No filesystem, no network,
no subprocess, no clock and no randomness, so one round renders to one string.
"""

from __future__ import annotations

import re
from typing import Any

from council_tools import ticket_policy
from council_tools.ticket_review import TicketReview

#: The derived state this renders.  Held as a constant here and pinned by a test
#: against a review the validator actually derived, so the spelling cannot drift
#: from the module that owns it without something failing.
SPLIT_STATE = "needs-split"

SUBMITTED_STATUS = "submitted"
UNAVAILABLE_STATUS = "unavailable"

_DIGEST = re.compile(r"[0-9a-f]{64}")

HEADING = "## Sizing round — no single outcome"

#: Said in words rather than shown as an empty list.  Which derived reasons
#: produced the outcome is stated above it, so this claims nothing about *why*
#: beyond the fact that no seat wrote it.
NO_REASONS_NOTICE = (
    "No seat authored a reason. This outcome follows from the derived reasons "
    "above rather than from anything a seat wrote."
)


class TicketSplitRecordError(ValueError):
    """A stable, field-addressed split-record failure."""

    def __init__(self, code: str, field: str, detail: str = ""):
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(
            f"ticket split record {code} at {field}" + (f": {detail}" if detail else "")
        )


def render_split_round(
    review: Any,
    *,
    projection_sha256: Any,
    policy: ticket_policy.TicketPolicy = ticket_policy.TICKET_POLICY_V1,
) -> str:
    """Render one ``needs-split`` round as durable, attributed text."""

    if type(review) is not TicketReview:
        raise TicketSplitRecordError("invalid-review", "review")
    if review.state != SPLIT_STATE:
        # An eligible round has a sealed contract and a reviewRef, and renders
        # through the governed body renderer instead.
        raise TicketSplitRecordError("not-a-split-round", "review.state", review.state)
    if not isinstance(projection_sha256, str) or _DIGEST.fullmatch(projection_sha256) is None:
        raise TicketSplitRecordError("invalid-projection-digest", "projection_sha256")

    lines = [
        HEADING,
        "",
        f"Projection reviewed: `{projection_sha256}`",
        "",
        f"Derived state: `{review.state}`",
        _derived_reasons_line(review),
        "",
        "### What the seats wrote",
        "",
    ]

    wrote_something = False
    # In record order, which `validate_ticket_review` has already pinned to the
    # required seat order -- it refuses a record whose seats arrive in any other
    # one. Re-sorting here would be a second copy of a rule it owns.
    for seat in review.seat_reviews:
        rendered, authored = _seat_section(seat, policy)
        lines.extend(rendered)
        wrote_something = wrote_something or authored
    if not wrote_something:
        lines.append(NO_REASONS_NOTICE)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _derived_reasons_line(review: TicketReview) -> str:
    if not review.reasons:
        return "Derived reasons: none recorded"
    rendered = ", ".join(
        f"`{reason.code}` ({reason.seat_id})" for reason in review.reasons
    )
    return f"Derived reasons: {rendered}"


def _seat_section(seat: Any, policy: ticket_policy.TicketPolicy) -> tuple[list[str], bool]:
    if seat.status == UNAVAILABLE_STATUS:
        text = seat.unavailable_reason
        if not text:
            return ([f"**{seat.seat_id}** — unavailable, with no reason recorded.", ""], False)
        _refuse_markers(text, policy, f"seat_reviews[{seat.seat_id}].reason")
        return ([f"**{seat.seat_id}** — unavailable: {text}", ""], True)

    if not seat.split_reasons:
        return ([f"**{seat.seat_id}** — no reason authored.", ""], False)

    section = [f"**{seat.seat_id}**", ""]
    for index, reason in enumerate(seat.split_reasons):
        _refuse_markers(reason, policy, f"seat_reviews[{seat.seat_id}].splitReasons[{index}]")
        section.append(f"{index + 1}. {reason}")
    section.append("")
    return (section, True)


def _refuse_markers(text: str, policy: ticket_policy.TicketPolicy, field: str) -> None:
    """Refuse text carrying a governed marker.

    The spellings are read from the policy that owns them rather than restated.
    A marker inside prose makes that marker non-unique and fails the body parser
    closed, so a record carrying one would break the issue it was written for.
    """

    for marker in (
        policy.contract_start_marker,
        policy.contract_end_marker,
        policy.review_ref_start_marker,
        policy.review_ref_end_marker,
    ):
        if marker in text:
            raise TicketSplitRecordError("reason-contains-marker", field, marker)


__all__ = [
    "HEADING",
    "NO_REASONS_NOTICE",
    "SPLIT_STATE",
    "TicketSplitRecordError",
    "render_split_round",
]
