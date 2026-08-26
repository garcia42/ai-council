import copy
import json
import unittest

from council_tools.ticket_ordering import TicketOrderingError
from council_tools.ticket_planning import (
    PLAN_SCHEMA_VERSION,
    TicketPlan,
    TicketPlanError,
    canonical_plan_bytes,
    plan_tickets,
)

RUN_ID = "planning-run"
BASE = "d" * 40


def contract(issue_number, *, work_type="change"):
    return {
        "schemaVersion": 1,
        "repository": "garcia42/ai-council",
        "issueNumber": issue_number,
        "targetBranch": "main",
        "baseCommit": BASE,
        "workType": work_type,
        "priority": "P1",
        "points": 1,
        "problemStatement": "One proposed child of a decomposition needs a plan entry.",
        "acceptanceCriteria": ["The plan records what this child would become."],
        "testCommands": ["pytest tests/ -q"],
        "allowedPaths": [{"kind": "file", "path": f"src/council_tools/m{issue_number}.py"}],
        "outOfScope": ["Anything the sibling children own."],
        "dependencies": [],
        "rollbackPlan": "Revert the commit.",
    }


def submitted(seat, *, days=2, single=True, reasons=(), priority="P1", confidence=80):
    return {
        "seatId": seat,
        "status": "submitted",
        "engineerDays": days,
        "singleOutcome": single,
        "splitReasons": list(reasons),
        "priority": priority,
        "confidence": confidence,
    }


def eligible_seats(days=2):
    return [submitted("claude", days=days), submitted("codex", days=days)]


def needs_split_seats():
    return [
        submitted("claude", days=7, single=False, reasons=["two outcomes"]),
        submitted("codex", days=2),
    ]


def child(issue_number, *, seats=None, depends_on=(), work_type="change", prose="Prose."):
    return {
        "prose": prose,
        "contract": contract(issue_number, work_type=work_type),
        "seatReviews": list(seats if seats is not None else eligible_seats()),
        "dependsOn": list(depends_on),
    }


def document(children):
    return {
        "repository": "garcia42/ai-council",
        "targetBranch": "main",
        "baseCommit": BASE,
        "children": children,
    }


def mixed_document():
    return document(
        {
            "second": child(201, depends_on=["first"]),
            "first": child(200),
            "split": child(202, seats=needs_split_seats(), depends_on=["first"]),
        }
    )


class ShapeTest(unittest.TestCase):
    def test_both_child_shapes_appear_in_one_plan(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        by_key = {c.local_key: c for c in plan.children}
        self.assertEqual(by_key["first"].decision, "eligible")
        self.assertEqual(by_key["split"].decision, "needs-split")

    def test_an_eligible_child_is_sealed_and_rendered(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        first = next(c for c in plan.children if c.local_key == "first")
        self.assertIsNotNone(first.contract)
        self.assertIsNotNone(first.body)
        self.assertEqual(len(first.contract_sha256), 64)
        self.assertEqual(first.reasons, ())

    def test_a_needs_split_child_has_no_contract_and_no_body(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        split = next(c for c in plan.children if c.local_key == "split")
        self.assertIsNone(split.contract)
        self.assertIsNone(split.contract_sha256)
        self.assertIsNone(split.body)
        self.assertTrue(split.reasons)

    def test_a_needs_split_child_records_the_review_reason_codes(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        split = next(c for c in plan.children if c.local_key == "split")
        self.assertIn("estimate-over-three:claude", split.reasons)
        self.assertIn("multiple-outcomes:claude", split.reasons)

    def test_labels_are_derived_not_declared(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        by_key = {c.local_key: c for c in plan.children}
        self.assertEqual(
            by_key["first"].labels,
            ("agent:blocked", "priority:P1", "size:2", "work:change"),
        )
        self.assertEqual(
            by_key["split"].labels,
            ("agent:blocked", "needs-split", "priority:P1", "work:change"),
        )

    def test_a_plan_whose_children_all_have_one_shape(self):
        plan = plan_tickets(document({"a": child(300), "b": child(301)}), run_id=RUN_ID)
        self.assertTrue(all(c.contract is not None for c in plan.children))
        self.assertTrue(all(c.body for c in plan.children))

    def test_a_plan_of_only_needs_split_children(self):
        doc = document({"a": child(310, seats=needs_split_seats())})
        plan = plan_tickets(doc, run_id=RUN_ID)
        self.assertTrue(all(c.contract is None for c in plan.children))

    def test_the_plan_records_the_document_identity(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        self.assertEqual(plan.schema_version, PLAN_SCHEMA_VERSION)
        self.assertEqual(plan.repository, "garcia42/ai-council")
        self.assertEqual(plan.target_branch, "main")
        self.assertEqual(plan.base_commit, BASE)
        self.assertEqual(plan.run_id, RUN_ID)

    def test_the_projection_digest_is_the_one_the_seats_would_have_seen(self):
        """Asserting only the length lets any 64-character string through."""
        from council_tools.ticket_qualification import phase_one_material

        doc = mixed_document()
        plan = plan_tickets(doc, run_id=RUN_ID)
        for planned in plan.children:
            with self.subTest(key=planned.local_key, decision=planned.decision):
                expected = phase_one_material(
                    doc["children"][planned.local_key]["contract"]
                ).projection_sha256
                self.assertEqual(planned.projection_sha256, expected)


class OrderTest(unittest.TestCase):
    def test_children_appear_in_creation_order(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        keys = [c.local_key for c in plan.children]
        self.assertEqual(keys[0], "first")
        self.assertLess(keys.index("first"), keys.index("second"))
        self.assertLess(keys.index("first"), keys.index("split"))

    def test_dependencies_are_carried_through(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        second = next(c for c in plan.children if c.local_key == "second")
        self.assertEqual(second.depends_on, ("first",))


class DeterminismTest(unittest.TestCase):
    def test_two_runs_serialize_byte_identically(self):
        self.assertEqual(
            canonical_plan_bytes(plan_tickets(mixed_document(), run_id=RUN_ID)),
            canonical_plan_bytes(plan_tickets(mixed_document(), run_id=RUN_ID)),
        )

    def test_input_sequence_does_not_change_the_serialized_plan(self):
        pairs = list(mixed_document()["children"].items())
        expected = canonical_plan_bytes(plan_tickets(mixed_document(), run_id=RUN_ID))
        for rotation in range(1, len(pairs)):
            rotated = document(dict(pairs[rotation:] + pairs[:rotation]))
            with self.subTest(rotation=rotation):
                self.assertEqual(
                    canonical_plan_bytes(plan_tickets(rotated, run_id=RUN_ID)), expected
                )

    def test_canonical_bytes_sort_keys_and_keep_child_order(self):
        plan = plan_tickets(mixed_document(), run_id=RUN_ID)
        loaded = json.loads(canonical_plan_bytes(plan).decode("utf-8"))
        self.assertEqual(list(loaded), sorted(loaded))
        self.assertEqual(
            [c["localKey"] for c in loaded["children"]],
            [c.local_key for c in plan.children],
        )

    def test_the_document_is_not_mutated(self):
        doc = mixed_document()
        before = copy.deepcopy(doc)
        plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(doc, before)

    def test_canonical_bytes_refuse_a_non_plan(self):
        with self.assertRaises(TicketPlanError) as caught:
            canonical_plan_bytes({"schemaVersion": 1})
        self.assertEqual(caught.exception.code, "invalid-plan")


class DelegationTest(unittest.TestCase):
    """Relation refusals belong to order_children and must reach the caller."""

    def test_a_cycle_is_reported_by_the_ordering_module(self):
        doc = document({"a": child(400, depends_on=["b"]), "b": child(401, depends_on=["a"])})
        with self.assertRaises(TicketOrderingError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "dependency-cycle")

    def test_an_unknown_sibling_key_is_reported_by_the_ordering_module(self):
        doc = document({"a": child(402, depends_on=["ghost"])})
        with self.assertRaises(TicketOrderingError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "unknown-key")

    def test_an_empty_child_set_is_reported_by_the_ordering_module(self):
        with self.assertRaises(TicketOrderingError) as caught:
            plan_tickets(document({}), run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "empty-children")

    def test_a_relation_refusal_is_not_pre_empted_by_a_child_disagreement(self):
        """Ordering runs first, so its narrower message survives."""
        broken = child(403, depends_on=["b"])
        broken["contract"]["repository"] = "someone/else"
        doc = document({"a": broken, "b": child(404, depends_on=["a"])})
        with self.assertRaises(TicketOrderingError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "dependency-cycle")


class DisagreementTest(unittest.TestCase):
    def test_repository_disagreement_is_refused(self):
        c = child(500)
        c["contract"]["repository"] = "someone/else"
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(document({"a": c}), run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "repository-mismatch")

    def test_target_branch_disagreement_is_refused(self):
        c = child(501)
        c["contract"]["targetBranch"] = "release"
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(document({"a": c}), run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "target-branch-mismatch")

    def test_two_children_claiming_one_issue_number_are_refused(self):
        doc = document({"a": child(600), "b": child(600)})
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "duplicate-issue-number")
        self.assertIn("600", caught.exception.detail)

    def test_a_duplicate_issue_number_across_both_shapes_is_refused(self):
        doc = document({"a": child(601), "b": child(601, seats=needs_split_seats())})
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "duplicate-issue-number")

    def test_the_cross_child_check_does_not_pre_empt_a_per_child_refusal(self):
        a = child(700)
        b = child(700)
        b["contract"]["targetBranch"] = "release"
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(document({"a": a, "b": b}), run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "target-branch-mismatch")


class DocumentShapeTest(unittest.TestCase):
    def test_a_non_mapping_document_is_refused(self):
        for value in (None, [], "doc", 1):
            with self.subTest(value=value):
                with self.assertRaises(TicketPlanError) as caught:
                    plan_tickets(value, run_id=RUN_ID)
                self.assertEqual(caught.exception.code, "invalid-document")

    def test_unknown_or_missing_document_keys_are_refused(self):
        doc = mixed_document()
        doc["extra"] = 1
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "unknown-keys")
        missing = mixed_document()
        del missing["baseCommit"]
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(missing, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "unknown-keys")

    def test_unknown_or_missing_child_keys_are_refused(self):
        doc = document({"a": child(800)})
        doc["children"]["a"]["extra"] = 1
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "unknown-keys")

    def test_a_non_mapping_child_is_refused(self):
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(document({"a": ["not", "a", "child"]}), run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "invalid-child")

    def test_blank_or_non_text_identity_fields_are_refused(self):
        for field in ("repository", "targetBranch", "baseCommit"):
            for value in ("", " main", None, 1):
                with self.subTest(field=field, value=value):
                    doc = mixed_document()
                    doc[field] = value
                    with self.assertRaises(TicketPlanError) as caught:
                        plan_tickets(doc, run_id=RUN_ID)
                    self.assertEqual(caught.exception.code, "invalid-text")

    def test_a_blank_run_id_is_refused(self):
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(mixed_document(), run_id="  ")
        self.assertEqual(caught.exception.code, "invalid-text")

    def test_non_text_prose_is_refused(self):
        doc = document({"a": child(900)})
        doc["children"]["a"]["prose"] = None
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "invalid-prose")

    def test_non_sequence_seat_reviews_are_refused(self):
        doc = document({"a": child(901)})
        doc["children"]["a"]["seatReviews"] = "claude"
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "invalid-seat-reviews")

    def test_a_non_mapping_contract_is_refused(self):
        doc = document({"a": child(902)})
        doc["children"]["a"]["contract"] = "contract"
        with self.assertRaises(TicketPlanError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "invalid-contract")

    def test_error_is_a_value_error_with_a_stable_code_and_field(self):
        with self.assertRaises(ValueError) as caught:
            plan_tickets(None, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "invalid-document")
        self.assertEqual(caught.exception.field, "document")


class SealBindingTest(unittest.TestCase):
    """The rendered body must carry the contract the seal produced."""

    def test_the_body_embeds_the_sealed_contract_digest(self):
        plan = plan_tickets(document({"a": child(1000)}), run_id=RUN_ID)
        planned = plan.children[0]
        self.assertIn(planned.contract_sha256, planned.body)

    def test_the_sealed_contract_carries_the_derived_values(self):
        plan = plan_tickets(document({"a": child(1001, seats=eligible_seats(days=3))}), run_id=RUN_ID)
        planned = plan.children[0]
        self.assertEqual(planned.contract["points"], 3)
        self.assertEqual(planned.contract["priority"], "P1")
        self.assertIn("size:3", planned.labels)

    def test_the_projection_digest_is_unmoved_by_the_seal(self):
        doc = document({"a": child(1002)})
        plan = plan_tickets(doc, run_id=RUN_ID)
        from council_tools.ticket_qualification import phase_one_material

        unsealed = phase_one_material(doc["children"]["a"]["contract"])
        self.assertEqual(plan.children[0].projection_sha256, unsealed.projection_sha256)

    def test_prose_containing_a_marker_is_refused_by_the_renderer(self):
        doc = document({"a": child(1003, prose="<!-- ai-council:ticket-contract:v1:start -->")})
        with self.assertRaises(ValueError) as caught:
            plan_tickets(doc, run_id=RUN_ID)
        self.assertEqual(caught.exception.code, "prose-contains-marker")


if __name__ == "__main__":
    unittest.main()
