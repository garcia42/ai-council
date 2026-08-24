import json
import unicodedata
import unittest
from dataclasses import FrozenInstanceError

import council_tools.ticket_contracts as ticket_contracts
import council_tools.ticket_review as ticket_review
from council_tools.ticket_review import (
    MAX_ENGINEER_DAYS,
    MAX_REVIEW_JSON_BYTES,
    REQUIRED_SEATS,
    TicketReviewError,
    canonical_review_bytes,
    load_ticket_review_json,
    review_sha256,
    validate_ticket_review,
)


CONTRACT_SHA256 = "a" * 64
GOLDEN_REVIEW_SHA256 = "e0b3dc3912293f9ba957f4c9c7b9d445d03ec837798532ff21c4c6c8cdcd3c14"


def submitted(
    seat_id,
    *,
    days,
    priority,
    confidence,
    single_outcome=True,
    split_reasons=None,
):
    if split_reasons is None:
        split_reasons = [] if single_outcome else ["Split independent outcome."]
    return {
        "seatId": seat_id,
        "status": "submitted",
        "engineerDays": days,
        "singleOutcome": single_outcome,
        "splitReasons": split_reasons,
        "priority": priority,
        "confidence": confidence,
    }


def unavailable(seat_id, reason="Seat was unavailable for this review."):
    return {
        "seatId": seat_id,
        "status": "unavailable",
        "reason": reason,
    }


def record(*, claude=None, codex=None):
    return {
        "schemaVersion": 1,
        "runId": "review-run-6",
        "contractSha256": CONTRACT_SHA256,
        "requiredSeats": ["claude", "codex"],
        "seatReviews": [
            claude
            if claude is not None
            else submitted("claude", days=2, priority="P1", confidence=80),
            codex
            if codex is not None
            else submitted("codex", days=3, priority="P0", confidence=60),
        ],
    }


class TextSubclass(str):
    pass


class IntegerSubclass(int):
    pass


class TicketReviewTest(unittest.TestCase):
    def assertReviewError(self, raw, code, field, *, expected=CONTRACT_SHA256):
        with self.assertRaises(TicketReviewError) as caught:
            validate_ticket_review(raw, expected_contract_sha256=expected)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.field, field)
        self.assertEqual(str(caught.exception), f"ticket review {code} at {field}")

    def assertJsonError(self, document, code):
        with self.assertRaises(TicketReviewError) as caught:
            load_ticket_review_json(
                document, expected_contract_sha256=CONTRACT_SHA256
            )
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.field, "$")
        self.assertEqual(str(caught.exception), f"ticket review {code} at $")

    def test_golden_eligible_review_derives_conservative_decision_and_digest(self):
        normalized = validate_ticket_review(
            record(), expected_contract_sha256=CONTRACT_SHA256
        )
        self.assertEqual(normalized.schema_version, 1)
        self.assertEqual(normalized.run_id, "review-run-6")
        self.assertEqual(normalized.contract_sha256, CONTRACT_SHA256)
        self.assertEqual(normalized.required_seats, REQUIRED_SEATS)
        self.assertEqual(normalized.state, "eligible")
        self.assertEqual(normalized.points, 3)
        self.assertEqual(normalized.priority, "P0")
        self.assertEqual(normalized.confidence, 60)
        self.assertEqual(normalized.reasons, ())

        expected = (
            '{"contractSha256":"'
            + CONTRACT_SHA256
            + '","requiredSeats":["claude","codex"],"runId":"review-run-6",'
            '"schemaVersion":1,"seatReviews":[{"confidence":80,'
            '"engineerDays":2,"priority":"P1","seatId":"claude",'
            '"singleOutcome":true,"splitReasons":[],"status":"submitted"},'
            '{"confidence":60,"engineerDays":3,"priority":"P0",'
            '"seatId":"codex","singleOutcome":true,"splitReasons":[],'
            '"status":"submitted"}]}'
        ).encode("utf-8")
        self.assertEqual(canonical_review_bytes(normalized), expected)
        self.assertEqual(normalized.review_sha256, GOLDEN_REVIEW_SHA256)
        self.assertEqual(review_sha256(normalized), GOLDEN_REVIEW_SHA256)

    def test_normalized_review_is_deeply_immutable_and_detached(self):
        raw = record()
        normalized = validate_ticket_review(
            raw, expected_contract_sha256=CONTRACT_SHA256
        )
        raw["requiredSeats"].reverse()
        raw["seatReviews"][0]["confidence"] = 0
        raw["seatReviews"][0]["splitReasons"].append("Mutated later.")
        self.assertEqual(normalized.required_seats, ("claude", "codex"))
        self.assertEqual(normalized.seat_reviews[0].confidence, 80)
        self.assertEqual(normalized.seat_reviews[0].split_reasons, ())
        with self.assertRaises(FrozenInstanceError):
            normalized.state = "needs-split"

    def test_schema_version_dispatches_before_exact_key_validation(self):
        for version in (0, 2, True, "1"):
            raw = {"schemaVersion": version, "future": "shape"}
            with self.subTest(version=version):
                self.assertReviewError(
                    raw,
                    "unsupported-schema-version",
                    "review.schemaVersion",
                )
        self.assertReviewError({}, "invalid-review-keys", "review")

    def test_top_level_and_variant_keys_are_exact(self):
        raw = record()
        raw["extra"] = True
        self.assertReviewError(raw, "invalid-review-keys", "review")

        submitted_extra = record()
        submitted_extra["seatReviews"][0]["reason"] = "not allowed"
        self.assertReviewError(
            submitted_extra,
            "invalid-submitted-review-keys",
            "review.seatReviews[0]",
        )

        unavailable_extra = record(claude=unavailable("claude"))
        unavailable_extra["seatReviews"][0]["priority"] = "P0"
        self.assertReviewError(
            unavailable_extra,
            "invalid-unavailable-review-keys",
            "review.seatReviews[0]",
        )

    def test_expected_contract_digest_is_required_validated_and_compared(self):
        with self.assertRaises(TypeError):
            validate_ticket_review(record())
        for expected in (None, "A" * 64, "a" * 63, TextSubclass("a" * 64)):
            with self.subTest(expected=expected):
                self.assertReviewError(
                    record(),
                    "invalid-expected-contract-sha256",
                    "expectedContractSha256",
                    expected=expected,
                )
        self.assertReviewError(
            record(),
            "contract-sha256-mismatch",
            "review.contractSha256",
            expected="b" * 64,
        )

        raw = record()
        raw["contractSha256"] = "A" * 64
        self.assertReviewError(
            raw, "invalid-contract-sha256", "review.contractSha256"
        )

    def test_run_id_rules_match_ticket_contracts_on_adversarial_corpus(self):
        values = (
            "",
            " padded",
            "padded ",
            "nul\x00byte",
            "\ud800",
            unicodedata.normalize("NFD", "José"),
            "x" * ticket_contracts.MAX_RUN_ID_LENGTH,
            "x" * (ticket_contracts.MAX_RUN_ID_LENGTH + 1),
            "review-run-6",
        )
        for value in values:
            expected = ticket_contracts._canonical_text(
                value, max_length=ticket_contracts.MAX_RUN_ID_LENGTH
            )
            with self.subTest(value=repr(value)):
                self.assertEqual(ticket_review._canonical_run_id(value), expected)

    def test_required_seats_and_reviews_are_exact_ordered_and_complete(self):
        for required_seats in (
            ["codex", "claude"],
            ["claude"],
            ["claude", "codex", "other"],
            [TextSubclass("claude"), "codex"],
            ("claude", "codex"),
        ):
            raw = record()
            raw["requiredSeats"] = required_seats
            with self.subTest(required_seats=required_seats):
                self.assertReviewError(
                    raw, "invalid-required-seats", "review.requiredSeats"
                )

        cases = (
            (
                [record()["seatReviews"][0]],
                "invalid-seat-reviews",
                "review.seatReviews",
            ),
            (
                list(reversed(record()["seatReviews"])),
                "invalid-seat-id",
                "review.seatReviews[0].seatId",
            ),
            (
                record()["seatReviews"] + [unavailable("other")],
                "invalid-seat-reviews",
                "review.seatReviews",
            ),
        )
        for reviews, code, field in cases:
            raw = record()
            raw["seatReviews"] = reviews
            with self.subTest(reviews=reviews):
                self.assertReviewError(raw, code, field)

        raw = record()
        raw["seatReviews"][0]["seatId"] = "codex"
        self.assertReviewError(
            raw, "invalid-seat-id", "review.seatReviews[0].seatId"
        )
        raw = record()
        raw["seatReviews"][0]["seatId"] = TextSubclass("claude")
        self.assertReviewError(
            raw, "invalid-seat-id", "review.seatReviews[0].seatId"
        )

    def test_unknown_status_and_non_mapping_seats_fail_closed(self):
        raw = record()
        raw["seatReviews"][0] = {"seatId": "claude", "status": "abstained"}
        self.assertReviewError(
            raw, "invalid-seat-status", "review.seatReviews[0].status"
        )
        raw = record()
        raw["seatReviews"][0] = "submitted"
        self.assertReviewError(
            raw, "invalid-seat-review", "review.seatReviews[0]"
        )

    def test_unavailable_and_all_unavailable_fail_closed_without_aggregates(self):
        one = validate_ticket_review(
            record(claude=unavailable("claude")),
            expected_contract_sha256=CONTRACT_SHA256,
        )
        self.assertEqual(one.state, "needs-split")
        self.assertIsNone(one.points)
        self.assertIsNone(one.priority)
        self.assertIsNone(one.confidence)
        self.assertEqual(
            tuple((reason.code, reason.seat_id) for reason in one.reasons),
            (("seat-unavailable", "claude"),),
        )

        all_unavailable = validate_ticket_review(
            record(
                claude=unavailable("claude", "Abstained from review."),
                codex=unavailable("codex"),
            ),
            expected_contract_sha256=CONTRACT_SHA256,
        )
        self.assertEqual(all_unavailable.state, "needs-split")
        self.assertIsNone(all_unavailable.points)
        self.assertIsNone(all_unavailable.priority)
        self.assertIsNone(all_unavailable.confidence)
        self.assertEqual(
            tuple((reason.code, reason.seat_id) for reason in all_unavailable.reasons),
            (
                ("seat-unavailable", "claude"),
                ("seat-unavailable", "codex"),
            ),
        )

    def test_unavailable_reason_is_canonical_and_bounded(self):
        for reason in ("", " padded", "nul\x00byte", "\ud800", 1):
            raw = record(claude=unavailable("claude", reason))
            with self.subTest(reason=repr(reason)):
                self.assertReviewError(
                    raw,
                    "invalid-unavailable-reason",
                    "review.seatReviews[0].reason",
                )

    def test_engineer_day_boundaries_and_strict_integer_type(self):
        for days in (1, 2, 3):
            normalized = validate_ticket_review(
                record(
                    claude=submitted(
                        "claude", days=days, priority="P1", confidence=80
                    ),
                    codex=submitted(
                        "codex", days=days, priority="P1", confidence=70
                    ),
                ),
                expected_contract_sha256=CONTRACT_SHA256,
            )
            with self.subTest(days=days):
                self.assertEqual(normalized.state, "eligible")
                self.assertEqual(normalized.points, days)

        for days in (4, MAX_ENGINEER_DAYS):
            normalized = validate_ticket_review(
                record(
                    claude=submitted(
                        "claude", days=days, priority="P1", confidence=80
                    )
                ),
                expected_contract_sha256=CONTRACT_SHA256,
            )
            with self.subTest(days=days):
                self.assertEqual(normalized.state, "needs-split")
                self.assertIsNone(normalized.points)
                self.assertEqual(normalized.priority, "P0")
                self.assertEqual(normalized.confidence, 60)
                self.assertEqual(
                    (normalized.reasons[0].code, normalized.reasons[0].seat_id),
                    ("estimate-over-three", "claude"),
                )

        for days in (0, MAX_ENGINEER_DAYS + 1, True, 1.0, IntegerSubclass(2)):
            raw = record()
            raw["seatReviews"][0]["engineerDays"] = days
            with self.subTest(days=days):
                self.assertReviewError(
                    raw,
                    "invalid-engineer-days",
                    "review.seatReviews[0].engineerDays",
                )

    def test_single_outcome_and_split_reasons_must_agree_exactly(self):
        cases = (
            (True, ["Contradiction."], "inconsistent-single-outcome"),
            (False, [], "inconsistent-single-outcome"),
            (1, [], "invalid-single-outcome"),
        )
        for single_outcome, reasons, code in cases:
            raw = record()
            raw["seatReviews"][0]["singleOutcome"] = single_outcome
            raw["seatReviews"][0]["splitReasons"] = reasons
            with self.subTest(single_outcome=single_outcome, reasons=reasons):
                self.assertReviewError(
                    raw, code, "review.seatReviews[0].singleOutcome"
                )

        normalized = validate_ticket_review(
            record(
                codex=submitted(
                    "codex",
                    days=2,
                    priority="P1",
                    confidence=75,
                    single_outcome=False,
                    split_reasons=["Ship parser and adapter separately."],
                )
            ),
            expected_contract_sha256=CONTRACT_SHA256,
        )
        self.assertEqual(normalized.state, "needs-split")
        self.assertIsNone(normalized.points)
        self.assertEqual(normalized.priority, "P1")
        self.assertEqual(normalized.confidence, 75)
        self.assertEqual(
            (normalized.reasons[0].code, normalized.reasons[0].seat_id),
            ("multiple-outcomes", "codex"),
        )

    def test_split_reasons_are_strict_canonical_unique_text(self):
        invalid = (
            "one reason",
            [""],
            [" padded"],
            ["duplicate", "duplicate"],
            [1],
            ["\ud800"],
            ["x" * (ticket_review.MAX_REASON_LENGTH + 1)],
            [f"reason-{index}" for index in range(ticket_review.MAX_SPLIT_REASONS + 1)],
        )
        for reasons in invalid:
            raw = record()
            raw["seatReviews"][0]["singleOutcome"] = False
            raw["seatReviews"][0]["splitReasons"] = reasons
            with self.subTest(reasons=reasons):
                self.assertReviewError(
                    raw,
                    "invalid-split-reasons",
                    "review.seatReviews[0].splitReasons",
                )

    def test_canonical_review_record_has_an_independent_size_bound(self):
        reasons = [
            ("x" * (ticket_review.MAX_REASON_LENGTH - 1)) + chr(65 + index)
            for index in range(ticket_review.MAX_SPLIT_REASONS)
        ]
        raw = record(
            claude=submitted(
                "claude",
                days=2,
                priority="P1",
                confidence=80,
                single_outcome=False,
                split_reasons=reasons,
            ),
            codex=submitted(
                "codex",
                days=2,
                priority="P1",
                confidence=80,
                single_outcome=False,
                split_reasons=reasons,
            ),
        )
        self.assertReviewError(raw, "review-too-large", "review")

    def test_priority_is_strict_and_p0_wins_submitted_disagreement(self):
        for priority in ("P2", "p0", 0, TextSubclass("P0")):
            raw = record()
            raw["seatReviews"][0]["priority"] = priority
            with self.subTest(priority=priority):
                self.assertReviewError(
                    raw,
                    "invalid-priority",
                    "review.seatReviews[0].priority",
                )

        normalized = validate_ticket_review(
            record(), expected_contract_sha256=CONTRACT_SHA256
        )
        self.assertEqual(normalized.priority, "P0")

    def test_confidence_is_strict_self_report_and_minimum_wins(self):
        for confidence in (0, 100):
            raw = record()
            raw["seatReviews"][0]["confidence"] = confidence
            normalized = validate_ticket_review(
                raw, expected_contract_sha256=CONTRACT_SHA256
            )
            with self.subTest(confidence=confidence):
                self.assertEqual(normalized.confidence, min(confidence, 60))

        for confidence in (-1, 101, True, 50.0, IntegerSubclass(50)):
            raw = record()
            raw["seatReviews"][0]["confidence"] = confidence
            with self.subTest(confidence=confidence):
                self.assertReviewError(
                    raw,
                    "invalid-confidence",
                    "review.seatReviews[0].confidence",
                )

    def test_reason_order_is_canonical_seat_then_rule_order(self):
        normalized = validate_ticket_review(
            record(
                claude=submitted(
                    "claude",
                    days=4,
                    priority="P1",
                    confidence=50,
                    single_outcome=False,
                    split_reasons=["Multiple outcomes."],
                ),
                codex=submitted(
                    "codex", days=5, priority="P0", confidence=40
                ),
            ),
            expected_contract_sha256=CONTRACT_SHA256,
        )
        self.assertEqual(
            tuple((reason.code, reason.seat_id) for reason in normalized.reasons),
            (
                ("estimate-over-three", "claude"),
                ("multiple-outcomes", "claude"),
                ("estimate-over-three", "codex"),
            ),
        )

    def test_content_address_is_computed_only_after_full_validation(self):
        calls = []
        original = ticket_review.hashlib.sha256

        def spy(data=b""):
            calls.append(data)
            return original(data)

        ticket_review.hashlib.sha256 = spy
        try:
            invalid = record()
            invalid["seatReviews"][0]["confidence"] = 101
            self.assertReviewError(
                invalid,
                "invalid-confidence",
                "review.seatReviews[0].confidence",
            )
            self.assertEqual(calls, [])
            validate_ticket_review(
                record(), expected_contract_sha256=CONTRACT_SHA256
            )
            self.assertEqual(len(calls), 1)
        finally:
            ticket_review.hashlib.sha256 = original

    def test_record_mapping_order_does_not_change_content_address(self):
        original = record()
        reordered = dict(reversed(list(original.items())))
        reordered["seatReviews"] = [
            dict(reversed(list(seat.items()))) for seat in reordered["seatReviews"]
        ]
        left = validate_ticket_review(
            original, expected_contract_sha256=CONTRACT_SHA256
        )
        right = validate_ticket_review(
            reordered, expected_contract_sha256=CONTRACT_SHA256
        )
        self.assertEqual(left, right)
        self.assertEqual(left.review_sha256, right.review_sha256)

    def test_canonical_helpers_accept_only_validated_review_values(self):
        for value in (record(), None, "review"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TicketReviewError) as caught:
                    canonical_review_bytes(value)
                self.assertEqual(caught.exception.code, "invalid-normalized-review")

    def test_strict_json_loader_accepts_text_bytes_and_bytearray(self):
        document = json.dumps(record(), ensure_ascii=False, indent=2)
        expected = validate_ticket_review(
            record(), expected_contract_sha256=CONTRACT_SHA256
        )
        for value in (document, document.encode("utf-8"), bytearray(document, "utf-8")):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(
                    load_ticket_review_json(
                        value, expected_contract_sha256=CONTRACT_SHA256
                    ),
                    expected,
                )

    def test_strict_json_loader_rejects_duplicates_constants_and_malformed(self):
        document = json.dumps(record())
        duplicate = document.replace(
            '"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'
        )
        nested_duplicate = document.replace(
            '"seatId": "claude"', '"seatId": "claude", "seatId": "claude"'
        )
        nonfinite = document.replace('"confidence": 80', '"confidence": NaN')
        for value, code in (
            (duplicate, "duplicate-json-key"),
            (nested_duplicate, "duplicate-json-key"),
            (nonfinite, "non-finite-json-number"),
            ("{", "invalid-review-json"),
            ("[]", "invalid-review-json-top-level"),
        ):
            with self.subTest(code=code):
                self.assertJsonError(value, code)

    def test_strict_json_loader_enforces_type_encoding_size_and_recursion(self):
        for value in (None, {}, TextSubclass("{}")):
            with self.subTest(value=type(value).__name__):
                self.assertJsonError(value, "invalid-review-json-type")
        self.assertJsonError(b"\xff", "invalid-review-json-encoding")
        self.assertJsonError("\ud800", "invalid-review-json-encoding")
        self.assertJsonError("\ufeff{}", "invalid-review-json")
        self.assertJsonError(
            "x" * (MAX_REVIEW_JSON_BYTES + 1), "review-json-too-large"
        )
        deeply_nested = "[" * 1_100 + "0" + "]" * 1_100
        self.assertJsonError(deeply_nested, "review-json-too-deep")

    def test_strict_loader_delegates_once_with_expected_digest(self):
        calls = []
        sentinel = object()
        original = ticket_review.validate_ticket_review

        def spy(value, *, expected_contract_sha256):
            calls.append((value, expected_contract_sha256))
            return sentinel

        ticket_review.validate_ticket_review = spy
        try:
            result = load_ticket_review_json(
                json.dumps(record()), expected_contract_sha256=CONTRACT_SHA256
            )
        finally:
            ticket_review.validate_ticket_review = original
        self.assertIs(result, sentinel)
        self.assertEqual(calls, [(record(), CONTRACT_SHA256)])

    def test_rejection_is_deterministic_across_mapping_order(self):
        raw = record()
        raw["contractSha256"] = "bad"
        raw["requiredSeats"] = []
        reordered = dict(reversed(list(raw.items())))
        for candidate in (raw, reordered):
            with self.subTest(candidate=candidate):
                self.assertReviewError(
                    candidate,
                    "invalid-contract-sha256",
                    "review.contractSha256",
                )

    def test_review_module_has_no_external_io_dependencies(self):
        for name in ("os", "pathlib", "requests", "subprocess", "time", "urllib"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(ticket_review, name))


if __name__ == "__main__":
    unittest.main()
