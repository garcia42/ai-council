"""Rejection and end-to-end proofs for the GitHub payload normalizer.

The module's whole purpose is to close the boundary at which the pure admission
predicate's guarantees begin, so every rejection rule is proved by example and
the success path is proved by feeding the real predicate.
"""

from __future__ import annotations

import json
import unittest

import council_tools.ticket_admission as ticket_admission
import council_tools.ticket_policy as ticket_policy
import council_tools.ticket_review as ticket_review
from council_tools.github_ticket_snapshot import (
    GitHubSnapshotError,
    build_admission_context,
    build_issue_snapshot,
    extract_label_names,
    normalize_issue_state,
)
from council_tools.ticket_admission import evaluate_ticket_admission
from council_tools.ticket_contracts import contract_sha256, sizing_projection_sha256

REPOSITORY = "garcia42/ai-council"
ISSUE_NUMBER = 77
BASE_COMMIT = "a" * 40
RUN_ID = "claude-opus-5:11111111-2222-3333-4444-555555555555"


def contract():
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "issueNumber": ISSUE_NUMBER,
        "targetBranch": "main",
        "baseCommit": BASE_COMMIT,
        "workType": "change",
        "priority": "P0",
        "points": 3,
        "problemStatement": "Normalize one acquired payload into admission inputs.",
        "acceptanceCriteria": ["The emitted shapes are exact."],
        "testCommands": ["PYTHONPATH=src:. python3 -m pytest tests/ -q"],
        "allowedPaths": [
            {"kind": "file", "path": "src/council_tools/github_ticket_snapshot.py"}
        ],
        "outOfScope": ["Authorization"],
        "dependencies": [7],
        "rollbackPlan": "Revert the issue commit.",
    }


def review_record(raw_contract):
    return {
        "schemaVersion": 2,
        "runId": RUN_ID,
        "contractSha256": contract_sha256(raw_contract),
        "sizingProjectionSha256": sizing_projection_sha256(raw_contract),
        "requiredSeats": ["claude", "codex"],
        "seatReviews": [
            {
                "seatId": "claude",
                "status": "submitted",
                "engineerDays": 2,
                "singleOutcome": True,
                "splitReasons": [],
                "priority": "P1",
                "confidence": 80,
            },
            {
                "seatId": "codex",
                "status": "submitted",
                "engineerDays": 3,
                "singleOutcome": True,
                "splitReasons": [],
                "priority": "P0",
                "confidence": 70,
            },
        ],
    }


def issue_body(raw_contract):
    policy = ticket_policy.TICKET_POLICY_V1
    return "\n".join(
        (
            "Human prose before.",
            policy.contract_start_marker,
            json.dumps(raw_contract, ensure_ascii=False, indent=2),
            policy.contract_end_marker,
            policy.review_ref_start_marker,
            json.dumps(
                {"runId": RUN_ID, "contractSha256": contract_sha256(raw_contract)},
                indent=2,
            ),
            policy.review_ref_end_marker,
            "Human prose after.",
        )
    )


def payload(raw_contract=None, **overrides):
    """A payload shaped the way the GitHub CLI actually returns one."""

    raw_contract = raw_contract if raw_contract is not None else contract()
    base = {
        "number": ISSUE_NUMBER,
        # GitHub reports state in upper case.  That is the point of the fold.
        "state": "OPEN",
        "labels": [
            {"id": "LA_1", "name": "priority:P0", "color": "D93F0B"},
            {"id": "LA_2", "name": "size:3", "color": "000000"},
            {"id": "LA_3", "name": "agent:ready", "color": "111111"},
            {"id": "LA_4", "name": "work:change", "color": "222222"},
        ],
        "body": issue_body(raw_contract),
    }
    base.update(overrides)
    return base


class DictSubclass(dict):
    pass


class ListSubclass(list):
    pass


class StateNormalizationTests(unittest.TestCase):
    def test_upper_case_state_is_folded(self):
        self.assertEqual(normalize_issue_state("OPEN"), "open")
        self.assertEqual(normalize_issue_state("CLOSED"), "closed")

    def test_already_lower_case_state_is_preserved(self):
        for state in sorted(ticket_admission.ISSUE_STATES):
            with self.subTest(state=state):
                self.assertEqual(normalize_issue_state(state), state)

    def test_unknown_state_is_refused_rather_than_defaulted(self):
        for state in ("merged", "OPENED", "", "Draft", "open ", " open"):
            with self.subTest(state=repr(state)):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    normalize_issue_state(state)
                self.assertEqual(caught.exception.code, "unknown-issue-state")

    def test_non_text_state_is_refused(self):
        for state in (None, 1, True, ["open"], {"state": "open"}):
            with self.subTest(state=repr(state)):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    normalize_issue_state(state)
                self.assertEqual(caught.exception.code, "invalid-text")


class LabelExtractionTests(unittest.TestCase):
    def test_names_are_extracted_in_payload_order(self):
        names = extract_label_names(
            [{"name": "b"}, {"name": "a"}, {"name": "c"}]
        )
        self.assertEqual(names, ["b", "a", "c"])

    def test_extra_label_fields_are_ignored(self):
        self.assertEqual(
            extract_label_names([{"id": "x", "name": "priority:P0", "color": "f"}]),
            ["priority:P0"],
        )

    def test_unknown_and_repeated_labels_are_passed_through_unadjudicated(self):
        # Adjudication belongs to the predicate.  Proving the normalizer stays
        # out of it is the point: it must not pre-empt ``invalid-labels``.
        names = extract_label_names(
            [
                {"name": "priority:P0"},
                {"name": "priority:P1"},
                {"name": "totally-unknown"},
                {"name": "size:9"},
            ]
        )
        self.assertEqual(
            names, ["priority:P0", "priority:P1", "totally-unknown", "size:9"]
        )
        # And the predicate does adjudicate them, so the rule still exists.
        with self.assertRaises(ticket_policy.TicketPolicyError):
            ticket_policy.parse_ticket_labels(names)

    def test_malformed_label_collections_are_refused(self):
        cases = (
            (None, "invalid-labels"),
            ({"name": "a"}, "invalid-labels"),
            ("priority:P0", "invalid-labels"),
            (ListSubclass(), "invalid-labels"),
        )
        for value, code in cases:
            with self.subTest(value=repr(value)):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    extract_label_names(value)
                self.assertEqual(caught.exception.code, code)

    def test_malformed_label_entries_are_refused(self):
        cases = (
            ([None], "invalid-payload"),
            (["priority:P0"], "invalid-payload"),
            ([DictSubclass(name="a")], "invalid-payload"),
            ([{}], "missing-label-name"),
            ([{"color": "f"}], "missing-label-name"),
            ([{"name": None}], "invalid-text"),
            ([{"name": 1}], "invalid-text"),
            ([{1: "a", "name": "b"}], "invalid-payload-key"),
        )
        for value, code in cases:
            with self.subTest(value=repr(value)):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    extract_label_names(value)
                self.assertEqual(caught.exception.code, code)


class SnapshotShapeTests(unittest.TestCase):
    def test_emitted_key_set_is_exactly_the_predicate_snapshot_key_set(self):
        snapshot = build_issue_snapshot(
            payload(), repository=REPOSITORY, issue_number=ISSUE_NUMBER
        )
        self.assertEqual(set(snapshot), set(ticket_admission.SNAPSHOT_KEYS))

    def test_state_is_folded_and_labels_are_plain_strings(self):
        snapshot = build_issue_snapshot(
            payload(), repository=REPOSITORY, issue_number=ISSUE_NUMBER
        )
        self.assertEqual(snapshot["state"], "open")
        self.assertTrue(all(type(name) is str for name in snapshot["labels"]))

    def test_payload_identity_is_checked_not_trusted(self):
        with self.assertRaises(GitHubSnapshotError) as caught:
            build_issue_snapshot(
                payload(number=ISSUE_NUMBER + 1),
                repository=REPOSITORY,
                issue_number=ISSUE_NUMBER,
            )
        self.assertEqual(caught.exception.code, "issue-number-mismatch")

    def test_a_redirected_repository_is_refused(self):
        with self.assertRaises(GitHubSnapshotError) as caught:
            build_issue_snapshot(
                payload(repository="someone/else"),
                repository=REPOSITORY,
                issue_number=ISSUE_NUMBER,
            )
        self.assertEqual(caught.exception.code, "repository-mismatch")

    def test_missing_payload_fields_are_refused(self):
        for field in ("number", "state", "labels", "body"):
            candidate = payload()
            del candidate[field]
            with self.subTest(field=field):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    build_issue_snapshot(
                        candidate, repository=REPOSITORY, issue_number=ISSUE_NUMBER
                    )
                self.assertEqual(caught.exception.code, "missing-payload-field")

    def test_malformed_payloads_are_refused(self):
        for candidate in (None, [], "issue", DictSubclass(), 1):
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    build_issue_snapshot(
                        candidate, repository=REPOSITORY, issue_number=ISSUE_NUMBER
                    )
                self.assertEqual(caught.exception.code, "invalid-payload")

    def test_requested_identity_is_validated(self):
        with self.assertRaises(GitHubSnapshotError) as caught:
            build_issue_snapshot(payload(), repository=None, issue_number=ISSUE_NUMBER)
        self.assertEqual(caught.exception.code, "invalid-text")
        for number in (0, -1, True, "77", None):
            with self.subTest(number=repr(number)):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    build_issue_snapshot(
                        payload(), repository=REPOSITORY, issue_number=number
                    )
                self.assertEqual(caught.exception.code, "invalid-issue-number")


class ContextShapeTests(unittest.TestCase):
    def build(self, **overrides):
        kwargs = {
            "repository": REPOSITORY,
            "issue_number": ISSUE_NUMBER,
            "target_branch": "main",
            "base_commit": BASE_COMMIT,
            "dependency_closure": [{"issueNumber": 7, "state": "closed"}],
            "base_commit_evidence": {
                "contractBaseIsAncestor": True,
                "changedPaths": [],
            },
        }
        kwargs.update(overrides)
        return build_admission_context(**kwargs)

    def test_emitted_key_set_is_exactly_the_predicate_context_key_set(self):
        self.assertEqual(set(self.build()), set(ticket_admission.CONTEXT_KEYS))

    def test_the_key_set_is_read_from_the_predicate_not_hard_coded(self):
        # This is the guard the ticket exists to add.  When CONTEXT_KEYS gained
        # baseCommitEvidence, an earlier contract went silently stale; a builder
        # that reads the predicate's own declaration fails loudly instead.
        self.assertEqual(
            set(self.build()),
            set(ticket_admission.CONTEXT_KEYS),
            "the emitted context key set must track ticket_admission.CONTEXT_KEYS",
        )
        self.assertIn("baseCommitEvidence", ticket_admission.CONTEXT_KEYS)

    def test_dependency_closure_entries_are_validated_not_resolved(self):
        cases = (
            (None, "invalid-dependency-closure"),
            ("closed", "invalid-dependency-closure"),
            ([{"issueNumber": 7}], "invalid-dependency-closure"),
            ([{"issueNumber": 7, "state": "closed", "x": 1}], "invalid-dependency-closure"),
            ([{"issueNumber": 7, "state": "done"}], "unknown-issue-state"),
            ([{"issueNumber": 0, "state": "closed"}], "invalid-issue-number"),
            ([{"issueNumber": True, "state": "closed"}], "invalid-issue-number"),
            ([None], "invalid-payload"),
        )
        for value, code in cases:
            with self.subTest(value=repr(value)):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    self.build(dependency_closure=value)
                self.assertEqual(caught.exception.code, code)

    def test_base_commit_evidence_is_validated_not_resolved(self):
        cases = (
            (None, "invalid-payload"),
            ({}, "invalid-base-commit-evidence"),
            ({"contractBaseIsAncestor": True}, "invalid-base-commit-evidence"),
            ({"changedPaths": []}, "invalid-base-commit-evidence"),
            (
                {"contractBaseIsAncestor": True, "changedPaths": [], "x": 1},
                "invalid-base-commit-evidence",
            ),
            ({"contractBaseIsAncestor": 1, "changedPaths": []}, "invalid-base-commit-evidence"),
            ({"contractBaseIsAncestor": 0, "changedPaths": []}, "invalid-base-commit-evidence"),
            (
                {"contractBaseIsAncestor": "true", "changedPaths": []},
                "invalid-base-commit-evidence",
            ),
            (
                {"contractBaseIsAncestor": True, "changedPaths": "a"},
                "invalid-base-commit-evidence",
            ),
            ({"contractBaseIsAncestor": True, "changedPaths": [None]}, "invalid-text"),
        )
        for value, code in cases:
            with self.subTest(value=repr(value)):
                with self.assertRaises(GitHubSnapshotError) as caught:
                    self.build(base_commit_evidence=value)
                self.assertEqual(caught.exception.code, code)

    def test_changed_paths_are_carried_through_unresolved(self):
        context = self.build(
            base_commit_evidence={
                "contractBaseIsAncestor": True,
                "changedPaths": ["docs/A.md", "src/b.py"],
            }
        )
        self.assertEqual(
            context["baseCommitEvidence"]["changedPaths"], ["docs/A.md", "src/b.py"]
        )


class EndToEndAdmissionTests(unittest.TestCase):
    def test_the_produced_shapes_are_admitted_by_the_real_predicate(self):
        raw = contract()
        snapshot = build_issue_snapshot(
            payload(raw), repository=REPOSITORY, issue_number=ISSUE_NUMBER
        )
        context = build_admission_context(
            repository=REPOSITORY,
            issue_number=ISSUE_NUMBER,
            target_branch="main",
            base_commit=BASE_COMMIT,
            dependency_closure=[{"issueNumber": 7, "state": "closed"}],
            base_commit_evidence={
                "contractBaseIsAncestor": True,
                "changedPaths": [],
            },
        )
        review = ticket_review.validate_ticket_review(
            review_record(raw), expected_contract_sha256=contract_sha256(raw)
        )
        result = evaluate_ticket_admission(snapshot, context, [review])
        self.assertTrue(result.structurally_eligible)
        self.assertEqual(result.reasons, ())

    def test_the_predicate_still_owns_label_adjudication(self):
        # The normalizer passes a bad label set through; the predicate rejects
        # it.  This proves the rule lives in exactly one place.
        raw = contract()
        bad = payload(raw)
        bad["labels"] = bad["labels"] + [{"name": "priority:P1"}]
        snapshot = build_issue_snapshot(
            bad, repository=REPOSITORY, issue_number=ISSUE_NUMBER
        )
        context = build_admission_context(
            repository=REPOSITORY,
            issue_number=ISSUE_NUMBER,
            target_branch="main",
            base_commit=BASE_COMMIT,
            dependency_closure=[{"issueNumber": 7, "state": "closed"}],
            base_commit_evidence={"contractBaseIsAncestor": True, "changedPaths": []},
        )
        review = ticket_review.validate_ticket_review(
            review_record(raw), expected_contract_sha256=contract_sha256(raw)
        )
        result = evaluate_ticket_admission(snapshot, context, [review])
        self.assertFalse(result.structurally_eligible)
        self.assertIn("invalid-labels", result.reasons)

    def test_a_closed_issue_is_rejected_by_the_predicate_not_the_normalizer(self):
        raw = contract()
        snapshot = build_issue_snapshot(
            payload(raw, state="CLOSED"), repository=REPOSITORY, issue_number=ISSUE_NUMBER
        )
        self.assertEqual(snapshot["state"], "closed")
        context = build_admission_context(
            repository=REPOSITORY,
            issue_number=ISSUE_NUMBER,
            target_branch="main",
            base_commit=BASE_COMMIT,
            dependency_closure=[{"issueNumber": 7, "state": "closed"}],
            base_commit_evidence={"contractBaseIsAncestor": True, "changedPaths": []},
        )
        review = ticket_review.validate_ticket_review(
            review_record(raw), expected_contract_sha256=contract_sha256(raw)
        )
        result = evaluate_ticket_admission(snapshot, context, [review])
        self.assertIn("issue-not-open", result.reasons)


class PurityTests(unittest.TestCase):
    def test_module_exposes_no_io_capable_name(self):
        import council_tools.github_ticket_snapshot as module

        forbidden = {
            "os", "io", "sys", "subprocess", "pathlib", "socket",
            "shutil", "tempfile", "urllib", "requests", "http",
        }
        present = forbidden.intersection(vars(module))
        self.assertEqual(present, set(), f"module exposes IO-capable names: {present}")

    def test_errors_are_typed_with_stable_codes_and_fields(self):
        with self.assertRaises(GitHubSnapshotError) as caught:
            normalize_issue_state("nope")
        self.assertTrue(issubclass(GitHubSnapshotError, ValueError))
        self.assertEqual(caught.exception.code, "unknown-issue-state")
        self.assertEqual(caught.exception.field, "issue.state")
        self.assertIn("unknown-issue-state", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
