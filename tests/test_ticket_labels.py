import dataclasses
import unittest

import council_tools.ticket_policy as ticket_policy
from council_tools.ticket_labels import (
    DERIVED_AGENT_STATE,
    TicketLabelError,
    derive_ticket_labels,
)
from council_tools.ticket_policy import parse_ticket_labels
from council_tools.ticket_review import (
    REQUIRED_SEATS,
    REVIEW_SCHEMA_VERSION,
    validate_ticket_review,
)

DIGEST = "a" * 64


def submitted(seat, *, days, single=True, reasons=(), priority="P1", confidence=80):
    return {
        "seatId": seat,
        "status": "submitted",
        "engineerDays": days,
        "singleOutcome": single,
        "splitReasons": list(reasons),
        "priority": priority,
        "confidence": confidence,
    }


def unavailable(seat):
    return {
        "seatId": seat,
        "status": "unavailable",
        "reason": "The required seat could not complete the review.",
    }


def review(seat_reviews):
    record = {
        "schemaVersion": REVIEW_SCHEMA_VERSION,
        "runId": "run",
        "contractSha256": DIGEST,
        "sizingProjectionSha256": DIGEST,
        "requiredSeats": list(REQUIRED_SEATS),
        "seatReviews": list(seat_reviews),
    }
    return validate_ticket_review(record, expected_contract_sha256=DIGEST)


def eligible_review(*, days=2, priority="P1"):
    return review(
        [
            submitted("claude", days=days, priority=priority),
            submitted("codex", days=days, priority=priority),
        ]
    )


def needs_split_review():
    """Both seats submitted, one over the ceiling: a priority IS derived."""
    return review(
        [
            submitted("claude", days=5, single=False, reasons=["two outcomes"], priority="P1"),
            submitted("codex", days=2, priority="P0"),
        ]
    )


def needs_split_without_priority():
    """An unavailable seat leaves priority and confidence absent."""
    return review(
        [
            submitted("claude", days=5, single=False, reasons=["two outcomes"]),
            unavailable("codex"),
        ]
    )


class DerivedStateTest(unittest.TestCase):
    def test_eligible_derives_size_priority_work_type_and_blocked(self):
        labels = derive_ticket_labels(eligible_review(days=2, priority="P1"), "change")
        self.assertEqual(
            labels, ("agent:blocked", "priority:P1", "size:2", "work:change")
        )

    def test_eligible_carries_no_split_flag(self):
        labels = derive_ticket_labels(eligible_review(), "change")
        self.assertNotIn(ticket_policy.NEEDS_SPLIT_LABEL, labels)

    def test_derived_size_tracks_the_review_not_the_caller(self):
        for days in (1, 2, 3):
            with self.subTest(days=days):
                labels = derive_ticket_labels(eligible_review(days=days), "change")
                self.assertIn(f"size:{days}", labels)

    def test_p0_wins_when_seats_disagree(self):
        record = review(
            [
                submitted("claude", days=2, priority="P0"),
                submitted("codex", days=2, priority="P1"),
            ]
        )
        self.assertIn("priority:P0", derive_ticket_labels(record, "change"))

    def test_needs_split_derives_split_flag_and_no_size(self):
        labels = derive_ticket_labels(needs_split_review(), "change")
        self.assertEqual(
            labels, ("agent:blocked", "needs-split", "priority:P0", "work:change")
        )
        self.assertFalse([label for label in labels if label.startswith("size:")])

    def test_needs_split_without_a_derived_priority_omits_the_label(self):
        record = needs_split_without_priority()
        self.assertIsNone(record.priority)
        labels = derive_ticket_labels(record, "change")
        self.assertEqual(labels, ("agent:blocked", "needs-split", "work:change"))
        self.assertFalse([label for label in labels if label.startswith("priority:")])

    def test_every_work_type_the_policy_governs_is_derivable(self):
        for work_type in ("bug", "change", "investigation"):
            with self.subTest(work_type=work_type):
                labels = derive_ticket_labels(eligible_review(), work_type)
                self.assertIn(f"work:{work_type}", labels)


class AgentStateTest(unittest.TestCase):
    def test_never_ready_or_claimed_in_either_state(self):
        for record in (eligible_review(), needs_split_review(), needs_split_without_priority()):
            with self.subTest(state=record.state):
                labels = derive_ticket_labels(record, "change")
                self.assertIn(f"agent:{DERIVED_AGENT_STATE}", labels)
                self.assertNotIn("agent:ready", labels)
                self.assertNotIn("agent:claimed", labels)

    def test_derived_agent_state_is_blocked(self):
        self.assertEqual(DERIVED_AGENT_STATE, "blocked")


class RoundTripTest(unittest.TestCase):
    def test_round_trip_returns_the_derived_values(self):
        cases = (
            (eligible_review(days=3, priority="P0"), "bug"),
            (needs_split_review(), "investigation"),
            (needs_split_without_priority(), "change"),
        )
        for record, work_type in cases:
            with self.subTest(state=record.state, work_type=work_type):
                labels = derive_ticket_labels(record, work_type)
                parsed = parse_ticket_labels(list(labels))
                self.assertEqual(parsed.priority, record.priority)
                self.assertEqual(
                    parsed.points, record.points if record.state == "eligible" else None
                )
                self.assertEqual(parsed.agent_state, DERIVED_AGENT_STATE)
                self.assertEqual(parsed.work_type, work_type)
                self.assertEqual(parsed.needs_split, record.state != "eligible")

    def test_a_needs_split_set_is_a_structurally_valid_combination(self):
        labels = derive_ticket_labels(needs_split_review(), "change")
        parsed = parse_ticket_labels(list(labels))
        self.assertTrue(parsed.needs_split)
        self.assertIn(parsed.agent_state, (None, "blocked"))


class RefusalTest(unittest.TestCase):
    def test_a_raw_mapping_is_refused_rather_than_read(self):
        record = {
            "schemaVersion": REVIEW_SCHEMA_VERSION,
            "runId": "run",
            "contractSha256": DIGEST,
            "sizingProjectionSha256": DIGEST,
            "requiredSeats": list(REQUIRED_SEATS),
            "seatReviews": [submitted("claude", days=2), submitted("codex", days=2)],
        }
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(record, "change")
        self.assertEqual(caught.exception.code, "invalid-review")

    def test_none_review_is_refused(self):
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(None, "change")
        self.assertEqual(caught.exception.code, "invalid-review")

    def test_ungoverned_work_type_is_refused(self):
        for work_type in ("chore", "Change", "", "work:change"):
            with self.subTest(work_type=work_type):
                with self.assertRaises(TicketLabelError) as caught:
                    derive_ticket_labels(eligible_review(), work_type)
                self.assertEqual(caught.exception.code, "unknown-work-type")

    def test_non_text_work_type_is_refused(self):
        for work_type in (None, 1, ["change"]):
            with self.subTest(work_type=work_type):
                with self.assertRaises(TicketLabelError) as caught:
                    derive_ticket_labels(eligible_review(), work_type)
                self.assertEqual(caught.exception.code, "invalid-work-type")

    def test_error_is_a_value_error_with_a_stable_code_and_field(self):
        with self.assertRaises(ValueError) as caught:
            derive_ticket_labels(eligible_review(), "chore")
        self.assertEqual(caught.exception.code, "unknown-work-type")
        self.assertEqual(caught.exception.field, "workType")


class DeterminismTest(unittest.TestCase):
    def test_two_derivations_are_identical(self):
        record = eligible_review()
        self.assertEqual(
            derive_ticket_labels(record, "change"),
            derive_ticket_labels(record, "change"),
        )

    def test_returned_order_is_sorted(self):
        for record in (eligible_review(), needs_split_review()):
            with self.subTest(state=record.state):
                labels = derive_ticket_labels(record, "change")
                self.assertEqual(list(labels), sorted(labels))

    def test_the_review_is_not_mutated(self):
        record = eligible_review()
        before = (record.state, record.points, record.priority, record.seat_reviews)
        derive_ticket_labels(record, "change")
        self.assertEqual(
            before, (record.state, record.points, record.priority, record.seat_reviews)
        )

    def test_the_policy_is_not_mutated(self):
        before = (
            ticket_policy.TICKET_POLICY_V1.size_labels,
            ticket_policy.TICKET_POLICY_V1.priority_labels,
            ticket_policy.TICKET_POLICY_V1.work_type_labels,
        )
        derive_ticket_labels(eligible_review(), "change")
        self.assertEqual(
            before,
            (
                ticket_policy.TICKET_POLICY_V1.size_labels,
                ticket_policy.TICKET_POLICY_V1.priority_labels,
                ticket_policy.TICKET_POLICY_V1.work_type_labels,
            ),
        )


class SpellingOwnershipTest(unittest.TestCase):
    def test_every_derived_label_is_one_the_policy_declares(self):
        governed = (
            set(ticket_policy.TICKET_POLICY_V1.priority_labels)
            | set(ticket_policy.TICKET_POLICY_V1.size_labels)
            | set(ticket_policy.TICKET_POLICY_V1.agent_state_labels)
            | set(ticket_policy.TICKET_POLICY_V1.work_type_labels)
            | {ticket_policy.TICKET_POLICY_V1.needs_split_label}
        )
        for record in (eligible_review(), needs_split_review(), needs_split_without_priority()):
            with self.subTest(state=record.state):
                for label in derive_ticket_labels(record, "change"):
                    self.assertIn(label, governed)

    def test_the_module_hardcodes_no_label_spelling(self):
        """The spellings belong to ticket_policy; a copy here is what drifts."""
        import pathlib

        import council_tools.ticket_labels as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        body = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith(("#", '"', "*"))
        )
        for spelling in ("priority:", "size:", "agent:", "work:", "needs-split"):
            self.assertNotIn(f'"{spelling}', body)


class LoadBearingRoundTripTest(unittest.TestCase):
    """Prove the round-trip assertion actually carries weight.

    Every other test derives a *correct* set, so the assertion never fires and
    deleting it changes nothing observable.  These feed the deriver a policy
    whose spellings are internally inconsistent, so the set it builds parses to
    something other than what it was derived from.  Each case is arranged to be
    caught by exactly one clause, which is what stops the assertion decaying
    into a comment.
    """

    def _policy(self, **overrides):
        return dataclasses.replace(ticket_policy.TICKET_POLICY_V1, **overrides)

    def test_a_set_the_parser_rejects_is_refused(self):
        # A mis-cased governed label: the parser rejects it outright.
        policy = self._policy(needs_split_label="Needs-Split")
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(needs_split_review(), "change", policy=policy)
        self.assertEqual(caught.exception.code, "underived-labels")

    def test_a_size_label_that_parses_to_nothing_is_refused(self):
        # Only the points clause can catch this one.
        policy = self._policy(size_prefix="", size_labels=frozenset({"1", "2", "3"}))
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(eligible_review(days=2), "change", policy=policy)
        self.assertEqual(caught.exception.code, "underived-labels")

    def test_a_work_type_label_that_parses_to_nothing_is_refused(self):
        # Only the work-type clause can catch this one.
        policy = self._policy(
            work_type_prefix="",
            work_type_labels=frozenset({"bug", "change", "investigation"}),
        )
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(eligible_review(), "change", policy=policy)
        self.assertEqual(caught.exception.code, "underived-labels")

    def test_a_split_flag_the_parser_does_not_govern_is_refused(self):
        # Parses cleanly as an ungoverned label, so only the needs_split clause
        # can catch it: the set would claim the ticket is implementable.
        policy = self._policy(needs_split_label="split-needed")
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(needs_split_review(), "change", policy=policy)
        self.assertEqual(caught.exception.code, "underived-labels")

    def test_an_agent_state_that_parses_to_nothing_is_refused(self):
        policy = self._policy(
            agent_state_prefix="",
            agent_state_labels=frozenset({"ready", "claimed", "blocked"}),
        )
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(eligible_review(), "change", policy=policy)
        self.assertEqual(caught.exception.code, "underived-labels")


class BrokenReviewInvariantTest(unittest.TestCase):
    """An eligible review always carries both derived values.

    ``ticket_review`` guarantees it, so these objects cannot arise from
    validation — they are built by replacing a field on the frozen record.  The
    guards exist so a broken invariant fails closed with a stable code instead
    of building a label out of ``None``.
    """

    def test_eligible_without_points_is_refused(self):
        record = dataclasses.replace(eligible_review(), points=None)
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(record, "change")
        self.assertEqual(caught.exception.code, "missing-points")

    def test_eligible_without_priority_is_refused(self):
        record = dataclasses.replace(eligible_review(), priority=None)
        with self.assertRaises(TicketLabelError) as caught:
            derive_ticket_labels(record, "change")
        self.assertEqual(caught.exception.code, "missing-priority")


if __name__ == "__main__":
    unittest.main()
