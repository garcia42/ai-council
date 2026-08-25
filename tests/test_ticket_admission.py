import copy
import json
import unittest
from dataclasses import FrozenInstanceError, replace

import council_tools.ticket_admission as ticket_admission
import council_tools.ticket_policy as ticket_policy
import council_tools.ticket_review as ticket_review
from council_tools.ticket_admission import (
    MAX_ADMISSION_CHANGED_PATHS,
    MAX_ADMISSION_EVIDENCE,
    REASON_CODES,
    evaluate_ticket_admission,
)
from council_tools.ticket_contracts import (
    contract_sha256,
    sizing_projection_sha256,
)


BASE_COMMIT = "d8b53357087a4baa478851f156c148792ae9ca0f"
RUN_ID = "review-run-admission"


def contract():
    return {
        "schemaVersion": 1,
        "repository": "garcia42/ai-council",
        "issueNumber": 77,
        "targetBranch": "main",
        "baseCommit": BASE_COMMIT,
        "workType": "change",
        "priority": "P0",
        "points": 3,
        "problemStatement": "Evaluate one pure structural admission decision.",
        "acceptanceCriteria": ["All structural inputs agree."],
        "testCommands": [
            "PYTHONPATH=src:. python3 -m unittest tests.test_ticket_admission -v"
        ],
        "allowedPaths": [
            {"kind": "file", "path": "src/council_tools/ticket_admission.py"},
            {"kind": "file", "path": "tests/test_ticket_admission.py"},
        ],
        "outOfScope": ["Authorization"],
        "dependencies": [2, 5],
        "rollbackPlan": "Revert the issue commit.",
    }


def raw_review(raw_contract=None, *, days=(2, 3), priorities=("P1", "P0")):
    raw_contract = raw_contract if raw_contract is not None else contract()
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
                "engineerDays": days[0],
                "singleOutcome": True,
                "splitReasons": [],
                "priority": priorities[0],
                "confidence": 80,
            },
            {
                "seatId": "codex",
                "status": "submitted",
                "engineerDays": days[1],
                "singleOutcome": True,
                "splitReasons": [],
                "priority": priorities[1],
                "confidence": 70,
            },
        ],
    }


def normalized_review(raw_contract=None, **kwargs):
    raw_contract = raw_contract if raw_contract is not None else contract()
    digest = contract_sha256(raw_contract)
    return ticket_review.validate_ticket_review(
        raw_review(raw_contract, **kwargs), expected_contract_sha256=digest
    )


def body(raw_contract=None, *, run_id=RUN_ID):
    raw_contract = raw_contract if raw_contract is not None else contract()
    digest = contract_sha256(raw_contract)
    policy = ticket_policy.TICKET_POLICY_V1
    return "\n".join(
        (
            "Human prose before.",
            policy.contract_start_marker,
            json.dumps(raw_contract, ensure_ascii=False, indent=2),
            policy.contract_end_marker,
            policy.review_ref_start_marker,
            json.dumps(
                {"runId": run_id, "contractSha256": digest}, indent=2
            ),
            policy.review_ref_end_marker,
            "Human prose after.",
        )
    )


def valid_inputs(raw_contract=None):
    raw_contract = copy.deepcopy(
        raw_contract if raw_contract is not None else contract()
    )
    snapshot = {
        "repository": raw_contract["repository"],
        "issueNumber": raw_contract["issueNumber"],
        "state": "open",
        "labels": [
            f'priority:{raw_contract["priority"]}',
            f'size:{raw_contract["points"]}',
            "agent:ready",
            f'work:{raw_contract["workType"]}',
            "unrelated-label",
        ],
        "body": body(raw_contract),
    }
    context = {
        "repository": raw_contract["repository"],
        "issueNumber": raw_contract["issueNumber"],
        "targetBranch": raw_contract["targetBranch"],
        "baseCommit": raw_contract["baseCommit"],
        # The base commit is identical here, so the only self-consistent
        # evidence is "ancestor of itself, nothing changed".
        "baseCommitEvidence": {
            "contractBaseIsAncestor": True,
            "changedPaths": [],
        },
        "dependencyClosure": [
            {"issueNumber": issue_number, "state": "closed"}
            for issue_number in raw_contract["dependencies"]
        ],
    }
    evidence = [normalized_review(raw_contract)]
    return snapshot, context, evidence


class DictSubclass(dict):
    pass


class ListSubclass(list):
    pass


class TextSubclass(str):
    pass


class ExplodingKey:
    def __hash__(self):
        return hash("repository")

    def __eq__(self, other):
        raise RuntimeError("untrusted key equality must not run")


class TicketAdmissionTest(unittest.TestCase):
    def assertReasons(self, snapshot, context, evidence, expected):
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertFalse(result.structurally_eligible)
        self.assertEqual(result.reasons, tuple(expected))
        self.assertIsNone(result.envelope)
        self.assertIsNone(result.labels)
        self.assertIsNone(result.review)
        return result

    def test_golden_result_is_structural_only_and_refuses_boolean_coercion(self):
        snapshot, context, evidence = valid_inputs()
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertTrue(result.structurally_eligible)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.envelope.contract.issue_number, 77)
        self.assertEqual(result.labels.points, 3)
        self.assertEqual(result.review.state, "eligible")
        self.assertFalse(hasattr(result, "eligible"))
        self.assertFalse(hasattr(result, "authorization"))
        with self.assertRaisesRegex(TypeError, "structural admission is not authorization"):
            bool(result)
        with self.assertRaises(FrozenInstanceError):
            result.structurally_eligible = False

    def test_reason_code_order_is_frozen_complete_and_unique(self):
        expected = (
            "invalid-snapshot",
            "invalid-context",
            "invalid-labels",
            "invalid-ticket-body",
            "issue-not-open",
            "missing-priority",
            "missing-size",
            "missing-work-type",
            "agent-not-ready",
            "needs-split",
            "repository-mismatch",
            "issue-number-mismatch",
            "target-branch-mismatch",
            "base-commit-mismatch",
            "base-commit-not-descendant",
            "base-commit-scope-changed",
            "base-commit-read-dependency-changed",
            "priority-mismatch",
            "size-mismatch",
            "work-type-mismatch",
            "invalid-dependency-closure",
            "dependency-not-closed",
            "invalid-review-evidence",
            "missing-review-evidence",
            "ambiguous-review-evidence",
            "review-not-eligible",
            "review-size-mismatch",
            "review-priority-mismatch",
            "review-projection-mismatch",
        )
        self.assertEqual(REASON_CODES, expected)
        self.assertEqual(len(REASON_CODES), len(set(REASON_CODES)))

    def test_arbitrary_top_level_garbage_returns_and_never_raises(self):
        invalid_values = (
            None,
            1,
            "value",
            [],
            (),
            {},
            DictSubclass(),
            object(),
        )
        valid_snapshot, valid_context, valid_evidence = valid_inputs()
        for value in invalid_values:
            evidence_reason = (
                "missing-review-evidence"
                if type(value) is list
                else "invalid-review-evidence"
            )
            cases = (
                (value, valid_context, valid_evidence, "invalid-snapshot"),
                (valid_snapshot, value, valid_evidence, "invalid-context"),
                (valid_snapshot, valid_context, value, evidence_reason),
            )
            for snapshot, context, evidence, reason in cases:
                with self.subTest(value=type(value).__name__, reason=reason):
                    result = evaluate_ticket_admission(snapshot, context, evidence)
                    self.assertIn(reason, result.reasons)
                    self.assertFalse(result.structurally_eligible)

    def test_hostile_plain_dict_keys_never_escape_shape_validation(self):
        snapshot, context, evidence = valid_inputs()
        hostile = {ExplodingKey(): None}
        self.assertReasons(
            hostile, context, evidence, ("invalid-snapshot",)
        )
        self.assertReasons(
            snapshot, hostile, evidence, ("invalid-context",)
        )

        hostile_closure = copy.deepcopy(context)
        hostile_closure["dependencyClosure"] = [hostile]
        self.assertReasons(
            snapshot, hostile_closure, evidence, ("invalid-context",)
        )

    def test_snapshot_shape_is_exact_bounded_and_read_before_parsing(self):
        snapshot, context, evidence = valid_inputs()
        cases = []
        for key in tuple(snapshot):
            candidate = copy.deepcopy(snapshot)
            del candidate[key]
            cases.append(candidate)
        extra = copy.deepcopy(snapshot)
        extra["extra"] = True
        cases.extend(
            (
                extra,
                DictSubclass(snapshot),
                {**snapshot, "repository": 1},
                {**snapshot, "issueNumber": True},
                {**snapshot, "state": "reopened"},
                {**snapshot, "labels": ListSubclass(snapshot["labels"])},
                {**snapshot, "labels": [TextSubclass("priority:P0")]},
                {**snapshot, "body": TextSubclass(snapshot["body"])},
            )
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertReasons(
                    candidate, context, evidence, ("invalid-snapshot",)
                )

        too_many = copy.deepcopy(snapshot)
        too_many["labels"] = [
            f"unrelated-{index}"
            for index in range(ticket_admission.MAX_ADMISSION_LABELS + 1)
        ]
        self.assertReasons(too_many, context, evidence, ("invalid-snapshot",))

    def test_context_and_dependency_entry_shapes_are_exact(self):
        snapshot, context, evidence = valid_inputs()
        cases = []
        for key in tuple(context):
            candidate = copy.deepcopy(context)
            del candidate[key]
            cases.append(candidate)
        extra = copy.deepcopy(context)
        extra["extra"] = True
        cases.extend(
            (
                extra,
                DictSubclass(context),
                {**context, "repository": 1},
                {**context, "issueNumber": True},
                {**context, "targetBranch": TextSubclass("main")},
                {**context, "baseCommit": "bad"},
                {**context, "dependencyClosure": ListSubclass()},
                {**context, "dependencyClosure": [None]},
                {
                    **context,
                    "dependencyClosure": [{"issueNumber": 2, "state": "done"}],
                },
                {
                    **context,
                    "dependencyClosure": [
                        {"issueNumber": True, "state": "closed"}
                    ],
                },
            )
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertReasons(
                    snapshot, candidate, evidence, ("invalid-context",)
                )

        too_many = copy.deepcopy(context)
        too_many["dependencyClosure"] = [
            {"issueNumber": index + 1, "state": "closed"}
            for index in range(ticket_admission.MAX_ADMISSION_DEPENDENCIES + 1)
        ]
        self.assertReasons(snapshot, too_many, evidence, ("invalid-context",))

    def test_canonical_parsers_are_called_at_most_once_each(self):
        snapshot, context, evidence = valid_inputs()
        calls = {"labels": 0, "body": 0}
        original_labels = ticket_policy.parse_ticket_labels
        original_body = ticket_policy.parse_ticket_issue_body

        def label_spy(value):
            calls["labels"] += 1
            return original_labels(value)

        def body_spy(value):
            calls["body"] += 1
            return original_body(value)

        ticket_policy.parse_ticket_labels = label_spy
        ticket_policy.parse_ticket_issue_body = body_spy
        try:
            result = evaluate_ticket_admission(snapshot, context, evidence)
        finally:
            ticket_policy.parse_ticket_labels = original_labels
            ticket_policy.parse_ticket_issue_body = original_body
        self.assertTrue(result.structurally_eligible)
        self.assertEqual(calls, {"labels": 1, "body": 1})

    def test_invalid_labels_and_body_are_isolated_parse_reasons(self):
        snapshot, context, evidence = valid_inputs()
        invalid_labels = copy.deepcopy(snapshot)
        invalid_labels["labels"].append("priority:P1")
        self.assertReasons(
            invalid_labels, context, evidence, ("invalid-labels",)
        )

        invalid_body = copy.deepcopy(snapshot)
        invalid_body["body"] = "not a ticket body"
        self.assertReasons(
            invalid_body, context, evidence, ("invalid-ticket-body",)
        )

        both = copy.deepcopy(snapshot)
        both["labels"].append("priority:P1")
        both["body"] = "not a ticket body"
        self.assertReasons(
            both,
            context,
            evidence,
            ("invalid-labels", "invalid-ticket-body"),
        )

    def test_every_incomplete_or_nonready_label_state_is_explicit(self):
        snapshot, context, evidence = valid_inputs()
        cases = (
            ("priority:P0", "missing-priority"),
            ("size:3", "missing-size"),
            ("work:change", "missing-work-type"),
            ("agent:ready", "agent-not-ready"),
        )
        for removed, reason in cases:
            candidate = copy.deepcopy(snapshot)
            candidate["labels"].remove(removed)
            with self.subTest(removed=removed):
                self.assertReasons(candidate, context, evidence, (reason,))

        for state in ("agent:blocked", "agent:claimed"):
            candidate = copy.deepcopy(snapshot)
            candidate["labels"].remove("agent:ready")
            candidate["labels"].append(state)
            with self.subTest(state=state):
                self.assertReasons(
                    candidate, context, evidence, ("agent-not-ready",)
                )

        split = copy.deepcopy(snapshot)
        split["labels"].remove("agent:ready")
        split["labels"].append("agent:blocked")
        split["labels"].append("needs-split")
        self.assertReasons(
            split,
            context,
            evidence,
            ("agent-not-ready", "needs-split"),
        )

        contradictory = copy.deepcopy(snapshot)
        contradictory["labels"].append("needs-split")
        self.assertReasons(
            contradictory, context, evidence, ("invalid-labels",)
        )

    def test_closed_issue_is_never_structurally_admitted(self):
        snapshot, context, evidence = valid_inputs()
        snapshot["state"] = "closed"
        self.assertReasons(snapshot, context, evidence, ("issue-not-open",))

    def test_snapshot_contract_and_context_identity_must_all_agree(self):
        fields = (
            ("repository", "other/repository", "repository-mismatch"),
            ("issueNumber", 78, "issue-number-mismatch"),
            ("targetBranch", "release", "target-branch-mismatch"),
        )
        for field, value, reason in fields:
            snapshot, context, evidence = valid_inputs()
            context[field] = value
            with self.subTest(field=field):
                self.assertReasons(snapshot, context, evidence, (reason,))

        snapshot, context, evidence = valid_inputs()
        snapshot["repository"] = "other/repository"
        snapshot["issueNumber"] = 78
        self.assertReasons(
            snapshot,
            context,
            evidence,
            ("repository-mismatch", "issue-number-mismatch"),
        )

    def test_identical_base_commits_still_admit(self):
        snapshot, context, evidence = valid_inputs()
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertTrue(result.structurally_eligible)
        self.assertEqual(result.reasons, ())

    def test_a_descendant_that_leaves_the_allowed_paths_alone_is_admitted(self):
        # This is the whole point of the rule: an unrelated commit must not
        # invalidate a qualification whose own files did not move.
        snapshot, context, evidence = valid_inputs()
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": ["docs/UNRELATED.md", "src/council_tools/elsewhere.py"],
        }
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertTrue(result.structurally_eligible)
        self.assertEqual(result.reasons, ())

    def test_a_descendant_touching_a_file_scope_reports_a_scope_change(self):
        snapshot, context, evidence = valid_inputs()
        touched = contract()["allowedPaths"][0]["path"]
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": ["docs/UNRELATED.md", touched],
        }
        self.assertReasons(
            snapshot, context, evidence, ("base-commit-scope-changed",)
        )

    def test_a_descendant_touching_a_directory_scope_reports_a_scope_change(self):
        raw = contract()
        raw["allowedPaths"] = [
            {"kind": "directory", "path": "src/council_tools"}
        ]
        snapshot, context, evidence = valid_inputs(raw)
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": ["src/council_tools/anything.py"],
        }
        self.assertReasons(
            snapshot, context, evidence, ("base-commit-scope-changed",)
        )

    def test_a_non_ancestor_fails_closed_and_is_distinguishable(self):
        snapshot, context, evidence = valid_inputs()
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": False,
            "changedPaths": [],
        }
        self.assertReasons(
            snapshot, context, evidence, ("base-commit-not-descendant",)
        )

    def test_a_non_ancestor_that_also_changes_scope_reports_both(self):
        snapshot, context, evidence = valid_inputs()
        touched = contract()["allowedPaths"][0]["path"]
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": False,
            "changedPaths": [touched],
        }
        self.assertReasons(
            snapshot,
            context,
            evidence,
            ("base-commit-not-descendant", "base-commit-scope-changed"),
        )

    def test_evidence_contradicting_identical_commits_fails_closed(self):
        # Identical commits admit nothing to have changed.  Evidence saying
        # otherwise is self-contradictory and must not be read permissively.
        for contradiction in (
            {"contractBaseIsAncestor": True, "changedPaths": ["docs/x.md"]},
            {"contractBaseIsAncestor": False, "changedPaths": []},
            {"contractBaseIsAncestor": False, "changedPaths": ["docs/x.md"]},
        ):
            snapshot, context, evidence = valid_inputs()
            context["baseCommitEvidence"] = contradiction
            with self.subTest(evidence=contradiction):
                self.assertReasons(
                    snapshot, context, evidence, ("base-commit-mismatch",)
                )

    def test_omitted_evidence_is_an_invalid_context_not_an_absent_constraint(self):
        snapshot, context, evidence = valid_inputs()
        del context["baseCommitEvidence"]
        self.assertReasons(snapshot, context, evidence, ("invalid-context",))

    def test_malformed_evidence_is_an_invalid_context(self):
        cases = (
            None,
            [],
            "ancestor",
            {},
            {"contractBaseIsAncestor": True},
            {"changedPaths": []},
            {"contractBaseIsAncestor": True, "changedPaths": [], "extra": 1},
            # 1 and 0 must not stand in for the boolean claim.
            {"contractBaseIsAncestor": 1, "changedPaths": []},
            {"contractBaseIsAncestor": 0, "changedPaths": []},
            {"contractBaseIsAncestor": "true", "changedPaths": []},
            {"contractBaseIsAncestor": True, "changedPaths": "docs/x.md"},
            {"contractBaseIsAncestor": True, "changedPaths": ListSubclass()},
            {"contractBaseIsAncestor": True, "changedPaths": [None]},
            {"contractBaseIsAncestor": True, "changedPaths": [""]},
            {"contractBaseIsAncestor": True, "changedPaths": ["/absolute/path"]},
            {"contractBaseIsAncestor": True, "changedPaths": ["trailing/"]},
            {"contractBaseIsAncestor": True, "changedPaths": ["back\\slash"]},
            {"contractBaseIsAncestor": True, "changedPaths": [DictSubclass()]},
        )
        for candidate in cases:
            snapshot, context, evidence = valid_inputs()
            context["baseCommitEvidence"] = candidate
            with self.subTest(candidate=repr(candidate)[:60]):
                self.assertReasons(snapshot, context, evidence, ("invalid-context",))

    def test_an_over_long_changed_path_set_is_refused(self):
        snapshot, context, evidence = valid_inputs()
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": [
                f"docs/file{index}.md"
                for index in range(MAX_ADMISSION_CHANGED_PATHS + 1)
            ],
        }
        self.assertReasons(snapshot, context, evidence, ("invalid-context",))

    def test_the_bound_itself_is_accepted(self):
        snapshot, context, evidence = valid_inputs()
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": [
                f"docs/file{index}.md"
                for index in range(MAX_ADMISSION_CHANGED_PATHS)
            ],
        }
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertTrue(result.structurally_eligible)

    def test_labels_must_agree_with_each_present_contract_field(self):
        cases = (
            ("priority:P0", "priority:P1", "priority-mismatch"),
            ("size:3", "size:2", "size-mismatch"),
            ("work:change", "work:bug", "work-type-mismatch"),
        )
        for old, new, reason in cases:
            snapshot, context, evidence = valid_inputs()
            snapshot["labels"].remove(old)
            snapshot["labels"].append(new)
            with self.subTest(new=new):
                self.assertReasons(snapshot, context, evidence, (reason,))

    def test_dependency_closure_shape_correspondence_and_state_have_clear_seams(self):
        snapshot, context, evidence = valid_inputs()
        cases = (
            ([{"issueNumber": 2, "state": "closed"}], "invalid-dependency-closure"),
            (
                context["dependencyClosure"]
                + [{"issueNumber": 9, "state": "closed"}],
                "invalid-dependency-closure",
            ),
            (
                [
                    {"issueNumber": 2, "state": "closed"},
                    {"issueNumber": 2, "state": "closed"},
                ],
                "invalid-dependency-closure",
            ),
            (
                [
                    {"issueNumber": 2, "state": "open"},
                    {"issueNumber": 5, "state": "closed"},
                ],
                "dependency-not-closed",
            ),
        )
        for closure, reason in cases:
            candidate = copy.deepcopy(context)
            candidate["dependencyClosure"] = closure
            with self.subTest(closure=closure):
                self.assertReasons(snapshot, candidate, evidence, (reason,))

        no_dependencies = contract()
        no_dependencies["dependencies"] = []
        snapshot, context, evidence = valid_inputs(no_dependencies)
        self.assertTrue(
            evaluate_ticket_admission(
                snapshot, context, evidence
            ).structurally_eligible
        )

    def test_review_evidence_collection_is_exact_bounded_and_unpoisoned(self):
        snapshot, context, evidence = valid_inputs()
        for candidate in (None, (), ListSubclass(evidence), [object()], evidence + [object()]):
            with self.subTest(candidate=type(candidate).__name__):
                self.assertReasons(
                    snapshot,
                    context,
                    candidate,
                    ("invalid-review-evidence",),
                )

        too_many = evidence * (MAX_ADMISSION_EVIDENCE + 1)
        self.assertReasons(
            snapshot,
            context,
            too_many,
            ("invalid-review-evidence",),
        )

        malformed_match = replace(evidence[0], seat_reviews=(object(),))
        self.assertReasons(
            snapshot,
            context,
            [malformed_match],
            ("invalid-review-evidence",),
        )

        uninitialized = object.__new__(ticket_review.TicketReview)
        self.assertReasons(
            snapshot,
            context,
            [uninitialized],
            ("invalid-review-evidence",),
        )

    def test_review_evidence_must_match_exactly_once(self):
        snapshot, context, evidence = valid_inputs()
        self.assertReasons(
            snapshot, context, [], ("missing-review-evidence",)
        )
        other = replace(evidence[0], run_id="other-run")
        self.assertReasons(
            snapshot,
            context,
            [other],
            ("missing-review-evidence",),
        )
        self.assertReasons(
            snapshot,
            context,
            [evidence[0], evidence[0]],
            ("ambiguous-review-evidence",),
        )

    def test_matched_review_is_revalidated_and_forged_derived_fields_do_not_pass(self):
        raw_contract = contract()
        digest = contract_sha256(raw_contract)
        oversized = raw_review(raw_contract, days=(4, 3))
        normalized = ticket_review.validate_ticket_review(
            oversized, expected_contract_sha256=digest
        )
        forged = replace(
            normalized,
            state="eligible",
            points=3,
            priority="P0",
            reasons=(),
        )
        snapshot, context, _ = valid_inputs(raw_contract)
        result = self.assertReasons(
            snapshot, context, [forged], ("review-not-eligible",)
        )
        self.assertIsNone(result.review)

    def test_matched_review_validation_is_called_once(self):
        snapshot, context, evidence = valid_inputs()
        calls = []
        original = ticket_review.validate_ticket_review

        def spy(value, *, expected_contract_sha256):
            calls.append((value, expected_contract_sha256))
            return original(
                value, expected_contract_sha256=expected_contract_sha256
            )

        ticket_review.validate_ticket_review = spy
        try:
            result = evaluate_ticket_admission(snapshot, context, evidence)
        finally:
            ticket_review.validate_ticket_review = original
        self.assertTrue(result.structurally_eligible)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][1], result.envelope.review_ref.contract_sha256
        )

    def test_review_state_size_and_priority_are_independently_checked(self):
        raw_contract = contract()
        cases = (
            ((2, 2), ("P1", "P0"), "review-size-mismatch"),
            ((2, 3), ("P1", "P1"), "review-priority-mismatch"),
        )
        for days, priorities, reason in cases:
            snapshot, context, _ = valid_inputs(raw_contract)
            evidence = [
                normalized_review(
                    raw_contract, days=days, priorities=priorities
                )
            ]
            with self.subTest(reason=reason):
                self.assertReasons(snapshot, context, evidence, (reason,))

    def test_layered_reason_aggregation_is_fixed_and_prerequisite_gated(self):
        snapshot, context, evidence = valid_inputs()
        snapshot["state"] = "closed"
        snapshot["labels"].remove("priority:P0")
        snapshot["labels"].remove("size:3")
        snapshot["repository"] = "other/repository"
        context["issueNumber"] = 78
        context["targetBranch"] = "release"
        context["dependencyClosure"][0]["state"] = "open"
        evidence = []
        self.assertReasons(
            snapshot,
            context,
            evidence,
            (
                "issue-not-open",
                "missing-priority",
                "missing-size",
                "repository-mismatch",
                "issue-number-mismatch",
                "target-branch-mismatch",
                "dependency-not-closed",
                "missing-review-evidence",
            ),
        )

        invalid_body = copy.deepcopy(snapshot)
        invalid_body["body"] = "bad"
        result = evaluate_ticket_admission(invalid_body, context, evidence)
        self.assertNotIn("target-branch-mismatch", result.reasons)
        self.assertNotIn("missing-review-evidence", result.reasons)

    def test_mapping_and_list_order_do_not_change_reason_order(self):
        snapshot, context, evidence = valid_inputs()
        snapshot["state"] = "closed"
        snapshot["labels"].remove("priority:P0")
        context["dependencyClosure"][0]["state"] = "open"
        expected = evaluate_ticket_admission(snapshot, context, evidence)

        reordered_snapshot = dict(reversed(list(snapshot.items())))
        reordered_context = dict(reversed(list(context.items())))
        reordered_context["dependencyClosure"] = list(
            reversed(reordered_context["dependencyClosure"])
        )
        actual = evaluate_ticket_admission(
            reordered_snapshot, reordered_context, evidence
        )
        self.assertEqual(actual, expected)

    def test_ineligible_results_never_retain_partially_validated_objects(self):
        snapshot, context, evidence = valid_inputs()
        snapshot["state"] = "closed"
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertFalse(result.structurally_eligible)
        self.assertIsNone(result.envelope)
        self.assertIsNone(result.labels)
        self.assertIsNone(result.review)

    def test_admission_module_has_no_external_io_dependencies(self):
        for name in (
            "os",
            "pathlib",
            "requests",
            "subprocess",
            "time",
            "urllib",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(ticket_admission, name))


class ProjectionBindingTest(unittest.TestCase):
    """A review is bound to the content its seats were shown, not just to a digest."""

    def _admit(self, raw_contract, review_contract=None):
        # review_contract is what the seats reviewed; raw_contract is what was
        # finally published.  They differ in the tamper cases below.
        reviewed = review_contract if review_contract is not None else raw_contract
        raw = raw_review(reviewed)
        raw["contractSha256"] = contract_sha256(raw_contract)
        raw["sizingProjectionSha256"] = sizing_projection_sha256(reviewed)
        review = ticket_review.validate_ticket_review(
            raw, expected_contract_sha256=contract_sha256(raw_contract)
        )
        snapshot, context, _ = valid_inputs(raw_contract)
        return evaluate_ticket_admission(snapshot, context, [review])

    def test_untampered_contract_is_admitted(self):
        self.assertTrue(self._admit(contract()).structurally_eligible)

    def test_editing_any_reviewed_field_after_review_fails_admission(self):
        # Seat results are held fixed and the contract digest is recomputed, so
        # only the projection binding can detect these.
        edits = {
            "problemStatement": "A different problem entirely.",
            "acceptanceCriteria": ["Something nobody sized."],
            "testCommands": ["true"],
            "allowedPaths": [{"kind": "directory", "path": "src"}],
            "outOfScope": ["Nothing at all"],
            "rollbackPlan": "Leave it broken.",
            "workType": "bug",
        }
        for key, value in edits.items():
            reviewed = contract()
            published = contract()
            published[key] = value
            with self.subTest(edited=key):
                result = self._admit(published, review_contract=reviewed)
                self.assertFalse(result.structurally_eligible)
                self.assertIn("review-projection-mismatch", result.reasons)

    def test_editing_only_the_derived_fields_still_passes(self):
        # points and priority are what the seats derived; changing them to the
        # derived values is exactly what phase two does.
        reviewed = contract()
        reviewed["points"] = 1
        reviewed["priority"] = "P1"
        published = contract()
        self.assertEqual(published["points"], 3)
        self.assertEqual(published["priority"], "P0")
        result = self._admit(published, review_contract=reviewed)
        self.assertTrue(result.structurally_eligible, result.reasons)

    def test_projection_mismatch_is_reported_with_a_stable_reason_code(self):
        raw = raw_review(contract())
        raw["sizingProjectionSha256"] = "c" * 64
        review = ticket_review.validate_ticket_review(
            raw, expected_contract_sha256=contract_sha256(contract())
        )
        snapshot, context, _ = valid_inputs(contract())
        result = evaluate_ticket_admission(snapshot, context, [review])
        self.assertFalse(result.structurally_eligible)
        self.assertEqual(result.reasons, ("review-projection-mismatch",))



if __name__ == "__main__":
    unittest.main()


class ReadDependencyAdmissionTest(unittest.TestCase):
    """A change to a declared read dependency invalidates a qualification.

    Before this field existed the invalidated set was computed from write scope
    alone, which is a proxy: a ticket that reads an interface it must not write
    stayed admissible when that interface moved. #88 was the measured case.
    """

    # Deliberately OUTSIDE the fixture's allowedPaths, which already grant write
    # access to ticket_admission.py. A path in both scopes is a contradiction the
    # contract refuses, and using one here would test that refusal by accident
    # instead of testing the admission rule.
    READ_PATH = "src/council_tools/ticket_contracts.py"

    def _contract_with_read_path(self):
        raw = copy.deepcopy(contract())
        raw["readPaths"] = [{"kind": "file", "path": self.READ_PATH}]
        return raw

    def test_a_change_to_a_read_dependency_refuses_with_its_own_reason(self):
        snapshot, context, evidence = valid_inputs(self._contract_with_read_path())
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": [self.READ_PATH],
        }
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertFalse(result.structurally_eligible)
        self.assertIn("base-commit-read-dependency-changed", result.reasons)
        # Reported distinctly from a write-scope change: the first says the
        # ticket's own files moved, the second says something it reads did.
        self.assertNotIn("base-commit-scope-changed", result.reasons)

    def test_a_change_elsewhere_still_admits(self):
        snapshot, context, evidence = valid_inputs(self._contract_with_read_path())
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": ["docs/UNRELATED.md"],
        }
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertTrue(result.structurally_eligible)

    def test_without_the_field_a_read_dependency_change_still_admits(self):
        # The behaviour every already-published contract keeps: this is what
        # makes the field additive rather than a silent re-qualification of the
        # entire backlog.
        snapshot, context, evidence = valid_inputs()
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": [self.READ_PATH],
        }
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertTrue(result.structurally_eligible)

    def test_both_reasons_can_be_reported_together(self):
        raw = self._contract_with_read_path()
        snapshot, context, evidence = valid_inputs(raw)
        context["baseCommit"] = "b" * 40
        context["baseCommitEvidence"] = {
            "contractBaseIsAncestor": True,
            "changedPaths": [self.READ_PATH, raw["allowedPaths"][0]["path"]],
        }
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertIn("base-commit-read-dependency-changed", result.reasons)
        self.assertIn("base-commit-scope-changed", result.reasons)

    def test_the_read_dependency_grants_no_write_permission(self):
        snapshot, context, evidence = valid_inputs(self._contract_with_read_path())
        result = evaluate_ticket_admission(snapshot, context, evidence)
        self.assertTrue(result.structurally_eligible)
        envelope = ticket_policy.parse_ticket_issue_body(snapshot["body"])
        self.assertTrue(envelope.contract.reads_path(self.READ_PATH))
        self.assertFalse(envelope.contract.allows_path(self.READ_PATH))
