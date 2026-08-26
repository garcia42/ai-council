"""Derive one governed label set from a sizing decision.

``ticket_policy.parse_ticket_labels`` reads a label set.  Nothing was its
inverse, so every label on every ticket opened so far was typed from a document
rather than derived from the review that decided it, and a later audit had to
strip thirteen size labels and six split flags from issues whose reviews had
never derived them.  Each one asserted a review that did not happen.  A derived
set cannot make that mistake, because the value it writes is the value the seats
returned.

The two review states owe opposite label sets:

* an ``eligible`` review owes the derived size and the derived priority, and no
  split flag;
* a ``needs-split`` review owes the split flag, owes no size label whatever the
  review carries, and owes a priority **only when the review derived one**.

That last distinction is measured rather than assumed.  Two submitted seats that
disagree still derive a priority, because the more severe wins; a seat recorded
``unavailable`` leaves both priority and confidence absent, and a label written
from an absent value would be invented.

Neither state may carry ``agent:ready`` or ``agent:claimed``.  Assigning the
ready state is a later admission control's decision, not a sizing review's, and
the policy already declares the split flag beside either of those structurally
invalid.

**Where the values and the spellings come from are two different modules.**  The
state, points and priority are :mod:`council_tools.ticket_review`'s; the
spellings, prefixes and split flag are :mod:`council_tools.ticket_policy`'s.
This module reads each from its owner and restates neither — copying the
spellings is precisely the duplication that would drift.  ``ticket_policy`` does
not import ``ticket_review``, which is why the composition lives here rather
than beside the parser it inverts.

The check that this holds is one assertion rather than a rule apiece:
:func:`derive_ticket_labels` feeds its own output back through
``parse_ticket_labels`` and refuses to return a set that does not parse to the
values it was derived from.  A wrong spelling, a missing namespace and a
duplicated group all fail there.

Everything here is pure: no network, no GitHub, no filesystem, no subprocess.
Deriving a label set is an integrity operation, not an authorization one.
"""

from __future__ import annotations

from typing import Any

import council_tools.ticket_policy as ticket_policy
from council_tools.ticket_review import TicketReview


#: The agent state a derived set always carries.  A sizing review never assigns
#: ``ready``: that is a later admission control's decision, and ``claimed`` is a
#: lifecycle state no review can reach.
DERIVED_AGENT_STATE = "blocked"

#: The review state that earns a size label.  Every other state derives the
#: split flag instead.  ``ticket_review`` owns which states exist.
ELIGIBLE_STATE = "eligible"


class TicketLabelError(ValueError):
    """A stable, field-addressed label-derivation failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"ticket label {code} at {field}")


def _work_type_label(
    work_type: Any, policy: ticket_policy.TicketPolicy
) -> str:
    if not isinstance(work_type, str):
        raise TicketLabelError("invalid-work-type", "workType")
    label = f"{policy.work_type_prefix}{work_type}"
    if label not in policy.work_type_labels:
        raise TicketLabelError("unknown-work-type", "workType")
    return label


def _priority_label(priority: str, policy: ticket_policy.TicketPolicy) -> str:
    label = f"{policy.priority_prefix}{priority}"
    if label not in policy.priority_labels:
        raise TicketLabelError("unknown-priority", "priority")
    return label


def _size_label(points: Any, policy: ticket_policy.TicketPolicy) -> str:
    label = f"{policy.size_prefix}{points}"
    if label not in policy.size_labels:
        raise TicketLabelError("unknown-size", "points")
    return label


def _agent_state_label(policy: ticket_policy.TicketPolicy) -> str:
    label = f"{policy.agent_state_prefix}{DERIVED_AGENT_STATE}"
    if label not in policy.agent_state_labels:
        raise TicketLabelError("unknown-agent-state", "agentState")
    return label


def derive_ticket_labels(
    review: Any,
    work_type: Any,
    *,
    policy: ticket_policy.TicketPolicy = ticket_policy.TICKET_POLICY_V1,
) -> tuple[str, ...]:
    """Return the exact governed label set one sizing decision authorizes.

    ``review`` must be a validated :class:`~council_tools.ticket_review.TicketReview`.
    A raw mapping is refused rather than read: the derived state, points and
    priority are only trustworthy because that object validated them, and
    re-deriving any of them here would put one rule in two places.

    The returned order is **sorted and therefore deterministic**, so two
    derivations from one review are identical.  The order carries no meaning:
    GitHub label sets are unordered, and ``parse_ticket_labels`` reads them as a
    set.  It is sorted so the value can be compared and recorded, not because
    anything downstream depends on the sequence.
    """

    if not isinstance(review, TicketReview):
        raise TicketLabelError("invalid-review", "review")

    labels = [_work_type_label(work_type, policy), _agent_state_label(policy)]

    if review.state == ELIGIBLE_STATE:
        # An eligible review always carries both derived values; ticket_review
        # guarantees it, so their absence is a contract breach rather than a
        # shape this module accommodates.
        if review.points is None:
            raise TicketLabelError("missing-points", "review.points")
        if review.priority is None:
            raise TicketLabelError("missing-priority", "review.priority")
        labels.append(_size_label(review.points, policy))
        labels.append(_priority_label(review.priority, policy))
    else:
        labels.append(policy.needs_split_label)
        # No size label, whatever the review carries.  A priority only when the
        # review actually derived one: an unavailable seat leaves it absent, and
        # a label written from an absent value would be invented.
        if review.priority is not None:
            labels.append(_priority_label(review.priority, policy))

    derived = tuple(sorted(labels))
    _assert_round_trip(derived, review, work_type, policy)
    return derived


def _assert_round_trip(
    labels: tuple[str, ...],
    review: TicketReview,
    work_type: str,
    policy: ticket_policy.TicketPolicy,
) -> None:
    """Refuse a set that does not parse back to what it was derived from.

    This is the whole correctness argument in one place.  A wrong spelling, a
    missing namespace, a duplicated group and a label the parser rejects all
    fail here rather than needing a rule each.
    """

    try:
        parsed = ticket_policy.parse_ticket_labels(list(labels))
    except ticket_policy.TicketPolicyError as exc:
        raise TicketLabelError("underived-labels", "labels") from exc

    expected_points = review.points if review.state == ELIGIBLE_STATE else None
    if (
        parsed.priority != review.priority
        or parsed.points != expected_points
        or parsed.agent_state != DERIVED_AGENT_STATE
        or parsed.work_type != work_type
        or parsed.needs_split != (review.state != ELIGIBLE_STATE)
    ):
        raise TicketLabelError("underived-labels", "labels")
