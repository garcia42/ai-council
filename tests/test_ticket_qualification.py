import copy
import json
import unittest
from unittest import mock

import council_tools.ticket_qualification as ticket_qualification
from council_tools.ticket_contracts import (
    SIZING_DERIVED_KEYS,
    contract_sha256,
    sizing_projection_sha256,
)
from council_tools.ticket_policy import TICKET_POLICY_V1, parse_ticket_issue_body
from council_tools.ticket_qualification import (
    TicketQualificationError,
    phase_one_material,
    render_ticket_body,
    seal_qualification,
)


RUN_ID = "claude-opus-5:11111111-2222-3333-4444-555555555555"


def contract(**overrides):
    base = {
        "schemaVersion": 1,
        "repository": "garcia42/ai-council",
        "issueNumber": 83,
        "targetBranch": "main",
        "baseCommit": "38775291259e26232dd5859caaed2873c374e7ce",
        "workType": "change",
        # Placeholders: phase one never shows these to a seat.
        "priority": "P1",
        "points": 1,
        "problemStatement": "Compose the governance primitives into the two-phase seal.",
        "acceptanceCriteria": ["The seal is a library function, not a script."],
        "testCommands": ["PYTHONPATH=src:. python3 -m pytest tests/ -q"],
        "allowedPaths": [
            {"kind": "file", "path": "src/council_tools/ticket_qualification.py"},
            {"kind": "file", "path": "tests/test_ticket_qualification.py"},
        ],
        "outOfScope": ["Any GitHub API call."],
        "dependencies": [],
        "rollbackPlan": "Revert the commit.",
    }
    base.update(overrides)
    return base


def submitted(seat_id, *, days=2, priority="P1", confidence=80):
    return {
        "seatId": seat_id,
        "status": "submitted",
        "engineerDays": days,
        "singleOutcome": True,
        "splitReasons": [],
        "priority": priority,
        "confidence": confidence,
    }


def split(seat_id, *, days=2, reasons=("Two independent outcomes.",)):
    return {
        "seatId": seat_id,
        "status": "submitted",
        "engineerDays": days,
        "singleOutcome": False,
        "splitReasons": list(reasons),
        "priority": "P1",
        "confidence": 80,
    }


def eligible_seats():
    return [submitted("claude", days=2), submitted("codex", days=3)]


class PhaseOneTest(unittest.TestCase):
    def test_material_is_the_projection_and_its_digest(self):
        material = phase_one_material(contract())
        self.assertEqual(
            material.projection_sha256, sizing_projection_sha256(contract())
        )
        for derived in SIZING_DERIVED_KEYS:
            with self.subTest(derived=derived):
                self.assertNotIn(derived, material.projection)

    def test_material_is_identical_for_every_placeholder(self):
        baseline = phase_one_material(contract()).projection_sha256
        for points, priority in ((1, "P1"), (3, "P0"), (2, "P0")):
            with self.subTest(points=points, priority=priority):
                material = phase_one_material(
                    contract(points=points, priority=priority)
                )
                self.assertEqual(material.projection_sha256, baseline)

    def test_material_rejects_a_non_mapping(self):
        for bad in (None, [], "contract", 3):
            with self.subTest(candidate=type(bad).__name__):
                with self.assertRaises(Exception):
                    phase_one_material(bad)


class SealQualificationTest(unittest.TestCase):
    def test_seal_records_the_derived_values(self):
        sealed = seal_qualification(contract(), eligible_seats(), run_id=RUN_ID)
        self.assertEqual(sealed.contract["points"], 3)  # max of 2 and 3
        self.assertEqual(sealed.contract["priority"], "P1")
        self.assertEqual(sealed.review.points, 3)
        self.assertEqual(sealed.review.state, "eligible")

    def test_p0_from_either_seat_wins(self):
        seats = [submitted("claude", priority="P1"), submitted("codex", priority="P0")]
        sealed = seal_qualification(contract(), seats, run_id=RUN_ID)
        self.assertEqual(sealed.contract["priority"], "P0")

    def test_seal_binds_both_digests(self):
        before = phase_one_material(contract()).projection_sha256
        sealed = seal_qualification(contract(), eligible_seats(), run_id=RUN_ID)
        # The reviewed content is unchanged by recording the derived values ...
        self.assertEqual(sealed.projection_sha256, before)
        self.assertEqual(sealed.review_record["sizingProjectionSha256"], before)
        # ... while the contract digest binds the sealed contract.
        self.assertEqual(sealed.contract_sha256, contract_sha256(sealed.contract))
        self.assertEqual(sealed.review_record["contractSha256"], sealed.contract_sha256)
        self.assertNotEqual(sealed.contract_sha256, before)

    def test_review_ref_matches_the_sealed_contract(self):
        sealed = seal_qualification(contract(), eligible_seats(), run_id=RUN_ID)
        self.assertEqual(
            sealed.review_ref(),
            {"runId": RUN_ID, "contractSha256": sealed.contract_sha256},
        )

    def test_seal_does_not_mutate_the_caller_contract(self):
        original = contract()
        snapshot = copy.deepcopy(original)
        seal_qualification(original, eligible_seats(), run_id=RUN_ID)
        self.assertEqual(original, snapshot)

    def test_seal_refuses_a_decision_that_is_not_eligible(self):
        cases = {
            "estimate-over-three": [
                submitted("claude", days=2),
                submitted("codex", days=4),
            ],
            "multiple-outcomes": [submitted("claude"), split("codex")],
            "seat-unavailable": [
                submitted("claude"),
                {
                    "seatId": "codex",
                    "status": "unavailable",
                    "reason": "The seat could not complete the review.",
                },
            ],
        }
        for expected, seats in cases.items():
            with self.subTest(reason=expected):
                with self.assertRaises(TicketQualificationError) as caught:
                    seal_qualification(contract(), seats, run_id=RUN_ID)
                self.assertEqual(caught.exception.code, "review-not-eligible")
                self.assertIn(expected, caught.exception.detail)

    def test_seal_refuses_if_writing_the_derived_values_moves_the_projection(self):
        # Cannot happen while the derived fields are excluded, which is exactly
        # why the guard is verified rather than assumed.
        real = sizing_projection_sha256
        calls = []

        def drifting(value):
            digest = real(value)
            calls.append(digest)
            return digest if len(calls) < 2 else "f" * 64

        with mock.patch.object(
            ticket_qualification, "sizing_projection_sha256", drifting
        ):
            with self.assertRaises(TicketQualificationError) as caught:
                seal_qualification(contract(), eligible_seats(), run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "projection-moved")

    def test_seal_rejects_malformed_inputs(self):
        seats = eligible_seats()
        cases = (
            ("invalid-contract", lambda: seal_qualification(None, seats, run_id=RUN_ID)),
            ("invalid-run-id", lambda: seal_qualification(contract(), seats, run_id="")),
            (
                "invalid-run-id",
                lambda: seal_qualification(contract(), seats, run_id=" padded "),
            ),
            (
                "invalid-seat-reviews",
                lambda: seal_qualification(contract(), "claude", run_id=RUN_ID),
            ),
        )
        for code, call in cases:
            with self.subTest(code=code):
                with self.assertRaises(TicketQualificationError) as caught:
                    call()
                self.assertEqual(caught.exception.code, code)


class RenderTicketBodyTest(unittest.TestCase):
    def sealed(self):
        return seal_qualification(contract(), eligible_seats(), run_id=RUN_ID)

    def test_body_round_trips_through_the_policy_parser(self):
        sealed = self.sealed()
        body = render_ticket_body("## Outcome\n\nSome prose.", sealed.contract, sealed.review_ref())
        envelope = parse_ticket_issue_body(body)
        self.assertEqual(envelope.contract.as_dict(), dict(sealed.contract))
        self.assertEqual(envelope.review_ref.as_dict(), sealed.review_ref())

    def test_each_marker_appears_exactly_once_and_in_order(self):
        sealed = self.sealed()
        body = render_ticket_body("Prose.", sealed.contract, sealed.review_ref())
        markers = (
            TICKET_POLICY_V1.contract_start_marker,
            TICKET_POLICY_V1.contract_end_marker,
            TICKET_POLICY_V1.review_ref_start_marker,
            TICKET_POLICY_V1.review_ref_end_marker,
        )
        positions = []
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertEqual(body.count(marker), 1)
            positions.append(body.index(marker))
        self.assertEqual(positions, sorted(positions))

    def test_prose_is_preserved_ahead_of_the_blocks(self):
        sealed = self.sealed()
        body = render_ticket_body("## Outcome\n\nKeep me.", sealed.contract, sealed.review_ref())
        self.assertTrue(body.startswith("## Outcome\n\nKeep me."))

    def test_marker_text_in_prose_is_refused(self):
        sealed = self.sealed()
        for marker in (
            TICKET_POLICY_V1.contract_start_marker,
            TICKET_POLICY_V1.contract_end_marker,
            TICKET_POLICY_V1.review_ref_start_marker,
            TICKET_POLICY_V1.review_ref_end_marker,
        ):
            with self.subTest(marker=marker):
                with self.assertRaises(TicketQualificationError) as caught:
                    render_ticket_body(
                        f"Discussing {marker} inline.",
                        sealed.contract,
                        sealed.review_ref(),
                    )
                self.assertEqual(caught.exception.code, "prose-contains-marker")

    def test_non_text_prose_is_refused(self):
        sealed = self.sealed()
        with self.assertRaises(TicketQualificationError) as caught:
            render_ticket_body(None, sealed.contract, sealed.review_ref())
        self.assertEqual(caught.exception.code, "invalid-prose")

    def test_a_body_that_would_not_parse_is_refused_rather_than_returned(self):
        sealed = self.sealed()
        broken = dict(sealed.contract)
        broken["points"] = 99  # invalid, and no longer matches the digest
        with self.assertRaises(TicketQualificationError) as caught:
            render_ticket_body("Prose.", broken, sealed.review_ref())
        self.assertEqual(caught.exception.code, "unparseable-body")

    def test_non_renderable_json_is_refused(self):
        sealed = self.sealed()
        broken = dict(sealed.contract)
        broken["problemStatement"] = object()
        with self.assertRaises(TicketQualificationError) as caught:
            render_ticket_body("Prose.", broken, sealed.review_ref())
        self.assertEqual(caught.exception.code, "non-renderable-json")


class ModuleSurfaceTest(unittest.TestCase):
    def test_module_performs_no_external_io(self):
        for forbidden in ("os", "socket", "subprocess", "urllib", "requests", "pathlib"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(hasattr(ticket_qualification, forbidden))

    def test_public_surface_is_exported(self):
        for name in (
            "PhaseOneMaterial",
            "SealedQualification",
            "TicketQualificationError",
            "phase_one_material",
            "render_ticket_body",
            "seal_qualification",
        ):
            with self.subTest(name=name):
                self.assertIn(name, ticket_qualification.__all__)


if __name__ == "__main__":
    unittest.main()
