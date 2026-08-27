"""Proof that a needs-split round becomes a durable, attributed, anchor-free record.

The interesting assertions are the two negatives: the submitted counts must NOT
appear, and a round nobody wrote a reason for must say so in words rather than
render an empty section.
"""

from __future__ import annotations

import unittest

from council_tools import ticket_policy
from council_tools.ticket_review import (
    REQUIRED_SEATS,
    TicketReview,
    validate_ticket_review,
)
from council_tools.ticket_split_record import (
    HEADING,
    NO_REASONS_NOTICE,
    SPLIT_STATE,
    TicketSplitRecordError,
    render_split_round,
)

# All-letter digests on purpose: the record is scanned below for digits
# that must NOT appear, and a digest carrying some would make that vacuous.
DIGEST = "cd" * 32
OTHER_DIGEST = "ab" * 32

CLAUDE_REASON = "The descriptive report is independently shippable without the test."
CODEX_REASON_A = "The distribution-free test is a separate statistical outcome."
CODEX_REASON_B = "The status classification is a separate governance outcome."


def submitted(seat, days, single, reasons=(), priority="P1", confidence=80):
    return {
        "seatId": seat,
        "status": "submitted",
        "engineerDays": days,
        "singleOutcome": single,
        "splitReasons": list(reasons),
        "priority": priority,
        "confidence": confidence,
    }


def unavailable(seat, reason="The required seat could not complete the review."):
    return {"seatId": seat, "status": "unavailable", "reason": reason}


def review(seat_reviews, digest=DIGEST):
    record = {
        "schemaVersion": 2,
        "runId": "claude-opus-5:00000000-0000-4000-8000-000000000000",
        "contractSha256": digest,
        "sizingProjectionSha256": digest,
        "requiredSeats": list(REQUIRED_SEATS),
        "seatReviews": seat_reviews,
    }
    return validate_ticket_review(record, expected_contract_sha256=digest)


BOTH_WROTE = [
    submitted("claude", 6, False, [CLAUDE_REASON], confidence=73),
    submitted("codex", 17, False, [CODEX_REASON_A, CODEX_REASON_B], confidence=91),
]
ONE_WROTE = [
    submitted("claude", 3, True),
    submitted("codex", 8, False, [CODEX_REASON_A]),
]
# The ceiling alone: both seats judged the work single and it split anyway.
CEILING_ONLY = [
    submitted("claude", 3, True),
    submitted("codex", 4, True),
]


class StateTest(unittest.TestCase):
    def test_the_split_state_spelling_matches_what_the_validator_derives(self):
        # One spelling, two modules. Pinned against a review the validator
        # actually produced so it cannot drift from its owner in silence.
        self.assertEqual(review(CEILING_ONLY).state, SPLIT_STATE)

    def test_an_eligible_round_is_refused(self):
        eligible = review(
            [submitted("claude", 2, True), submitted("codex", 2, True)]
        )
        self.assertEqual(eligible.state, "eligible")
        with self.assertRaises(TicketSplitRecordError) as caught:
            render_split_round(eligible, projection_sha256=DIGEST)
        self.assertEqual(caught.exception.code, "not-a-split-round")

    def test_something_that_is_not_a_validated_review_is_refused(self):
        class Impostor:
            state = SPLIT_STATE
            reasons = ()
            seat_reviews = ()

        for value in (None, {}, "needs-split", Impostor()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TicketSplitRecordError) as caught:
                    render_split_round(value, projection_sha256=DIGEST)
                self.assertEqual(caught.exception.code, "invalid-review")

    def test_a_non_canonical_digest_is_refused(self):
        for value in ("", "abc", DIGEST.upper(), DIGEST + "0", None, 3, DIGEST[:-1] + "g"):
            with self.subTest(value=repr(value)[:20]):
                with self.assertRaises(TicketSplitRecordError) as caught:
                    render_split_round(review(BOTH_WROTE), projection_sha256=value)
                self.assertEqual(caught.exception.code, "invalid-projection-digest")


class ContentTest(unittest.TestCase):
    def test_every_reason_appears_verbatim_and_attributed(self):
        text = render_split_round(review(BOTH_WROTE), projection_sha256=DIGEST)
        for reason in (CLAUDE_REASON, CODEX_REASON_A, CODEX_REASON_B):
            self.assertIn(reason, text)
        claude_at = text.index("**claude**")
        codex_at = text.index("**codex**")
        self.assertLess(claude_at, codex_at)
        self.assertLess(claude_at, text.index(CLAUDE_REASON))
        self.assertLess(codex_at, text.index(CODEX_REASON_A))
        self.assertLess(text.index(CODEX_REASON_A), text.index(CODEX_REASON_B))

    def test_seat_order_is_the_validators_guarantee_not_this_modules(self):
        # The renderer walks the record as given, because a record whose seats
        # arrive in any other order never validates. Re-sorting here would be a
        # second copy of a rule `validate_ticket_review` owns.
        with self.assertRaises(Exception) as caught:
            review(list(reversed(BOTH_WROTE)))
        self.assertEqual(getattr(caught.exception, "code", None), "invalid-seat-id")
        self.assertEqual(REQUIRED_SEATS, ("claude", "codex"))

    def test_no_submitted_value_reaches_the_record(self):
        # The point of the whole record: a later round is a first opinion over a
        # new projection, and these are the values it must derive for itself.
        text = render_split_round(review(BOTH_WROTE), projection_sha256=DIGEST)
        # Field names, and the submitted values themselves. The values are
        # distinctive and the digest carries no digits, so a hit here is real.
        for forbidden in ("engineerDays", "confidence", "singleOutcome", "priority",
                          "6", "17", "73", "91", "P1"):
            with self.subTest(value=forbidden):
                self.assertNotIn(forbidden, text)

    def test_the_derived_outputs_do_reach_the_record(self):
        text = render_split_round(review(BOTH_WROTE), projection_sha256=DIGEST)
        self.assertIn(DIGEST, text)
        self.assertIn(SPLIT_STATE, text)
        self.assertIn("estimate-over-three", text)
        self.assertIn("multiple-outcomes", text)

    def test_a_seat_that_wrote_nothing_says_so_and_the_other_still_renders(self):
        text = render_split_round(review(ONE_WROTE), projection_sha256=DIGEST)
        self.assertIn("**claude** — no reason authored.", text)
        self.assertIn(CODEX_REASON_A, text)
        self.assertNotIn(NO_REASONS_NOTICE, text)

    def test_a_ceiling_only_round_says_no_seat_wrote_anything(self):
        text = render_split_round(review(CEILING_ONLY), projection_sha256=DIGEST)
        self.assertIn(NO_REASONS_NOTICE, text)
        self.assertIn("estimate-over-three", text)

    def test_an_unavailable_seats_reason_is_carried_and_attributed(self):
        text = render_split_round(
            review([submitted("claude", 2, True), unavailable("codex")]),
            projection_sha256=DIGEST,
        )
        self.assertIn("**codex** — unavailable: The required seat could not", text)
        self.assertNotIn(NO_REASONS_NOTICE, text)

    def test_the_heading_and_digest_are_present_once(self):
        text = render_split_round(review(BOTH_WROTE), projection_sha256=DIGEST)
        self.assertEqual(text.count(HEADING), 1)
        self.assertEqual(text.count(DIGEST), 1)
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))


class MarkerTest(unittest.TestCase):
    def markers(self):
        policy = ticket_policy.TICKET_POLICY_V1
        return (
            policy.contract_start_marker,
            policy.contract_end_marker,
            policy.review_ref_start_marker,
            policy.review_ref_end_marker,
        )

    def test_a_reason_carrying_any_governed_marker_is_refused(self):
        for marker in self.markers():
            with self.subTest(marker=marker):
                poisoned = review(
                    [
                        submitted("claude", 4, False, [f"a reason {marker} and more"]),
                        submitted("codex", 2, True),
                    ]
                )
                with self.assertRaises(TicketSplitRecordError) as caught:
                    render_split_round(poisoned, projection_sha256=DIGEST)
                self.assertEqual(caught.exception.code, "reason-contains-marker")
                self.assertEqual(caught.exception.detail, marker)

    def test_an_unavailable_reason_carrying_a_marker_is_refused_too(self):
        marker = self.markers()[0]
        poisoned = review(
            [submitted("claude", 4, False, ["ordinary"]), unavailable("codex", f"x {marker} y")]
        )
        with self.assertRaises(TicketSplitRecordError) as caught:
            render_split_round(poisoned, projection_sha256=DIGEST)
        self.assertEqual(caught.exception.code, "reason-contains-marker")

    def test_an_ordinary_reason_mentioning_html_comments_is_not_refused(self):
        ok = review(
            [
                submitted("claude", 4, False, ["<!-- an ordinary comment --> is fine"]),
                submitted("codex", 2, True),
            ]
        )
        self.assertIn("an ordinary comment", render_split_round(ok, projection_sha256=DIGEST))


class DeterminismTest(unittest.TestCase):
    def test_the_same_round_renders_byte_identically(self):
        first = render_split_round(review(BOTH_WROTE), projection_sha256=DIGEST)
        second = render_split_round(review(BOTH_WROTE), projection_sha256=DIGEST)
        self.assertEqual(first, second)

    def test_a_different_projection_renders_differently(self):
        # Guards against a digest that is accepted and then not used.
        self.assertNotEqual(
            render_split_round(review(BOTH_WROTE), projection_sha256=DIGEST),
            render_split_round(review(BOTH_WROTE), projection_sha256=OTHER_DIGEST),
        )
