import json
import unittest
from dataclasses import FrozenInstanceError
from itertools import combinations

import council_tools.ticket_contracts as ticket_contracts
import council_tools.ticket_policy as ticket_policy
from council_tools.ticket_contracts import TicketContractError, contract_sha256
from council_tools.ticket_policy import (
    MAX_ISSUE_BODY_BYTES,
    MAX_ISSUE_BODY_CHARACTERS,
    TICKET_POLICY_V1,
    TicketPolicyError,
    parse_ticket_issue_body,
    parse_ticket_labels,
)


def contract():
    return {
        "schemaVersion": 1,
        "repository": "garcia42/ai-council",
        "issueNumber": 5,
        "targetBranch": "main",
        "baseCommit": "6d9b20451c0d9b8f41989c21b1af50a239bd2dac",
        "workType": "change",
        "priority": "P0",
        "points": 1,
        "problemStatement": "Publish one shared ticket policy.",
        "acceptanceCriteria": ["Every consumer parses the same policy."],
        "testCommands": [
            "PYTHONPATH=src:. python3 -m unittest tests.test_ticket_policy -v"
        ],
        "allowedPaths": [
            {"kind": "file", "path": "src/council_tools/ticket_policy.py"},
            {"kind": "file", "path": "tests/test_ticket_policy.py"},
        ],
        "outOfScope": ["GitHub mutation"],
        "dependencies": [2, 4],
        "rollbackPlan": "Revert the issue commit.",
    }


def review_ref(raw_contract=None):
    raw_contract = raw_contract if raw_contract is not None else contract()
    return {
        "runId": "claude-opus-5:review-5",
        "contractSha256": contract_sha256(raw_contract),
    }


def issue_body(
    *,
    raw_contract=None,
    raw_review_ref=None,
    newline="\n",
    prefix="Ticket prose before the machine blocks.",
    suffix="Ticket prose after the machine blocks.",
):
    raw_contract = raw_contract if raw_contract is not None else contract()
    raw_review_ref = (
        raw_review_ref if raw_review_ref is not None else review_ref(raw_contract)
    )
    policy = TICKET_POLICY_V1
    lines = [
        prefix,
        policy.contract_start_marker,
        json.dumps(raw_contract, ensure_ascii=False, indent=2),
        policy.contract_end_marker,
        policy.review_ref_start_marker,
        json.dumps(raw_review_ref, ensure_ascii=False, indent=2),
        policy.review_ref_end_marker,
        suffix,
    ]
    return newline.join(lines)


class TextSubclass(str):
    pass


class ListSubclass(list):
    pass


class TicketPolicyTest(unittest.TestCase):
    def assertPolicyError(self, call, code, field):
        with self.assertRaises(TicketPolicyError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.field, field)
        self.assertEqual(str(caught.exception), f"ticket policy {code} at {field}")

    def test_policy_object_is_frozen_versioned_and_cross_module_consistent(self):
        policy = TICKET_POLICY_V1
        self.assertEqual(policy.policy_version, 1)
        self.assertEqual(policy.policy_version, ticket_contracts.SCHEMA_VERSION)
        self.assertEqual(
            policy.priority_labels, frozenset({"priority:P0", "priority:P1"})
        )
        self.assertEqual(
            policy.size_labels, frozenset({"size:1", "size:2", "size:3"})
        )
        self.assertEqual(
            policy.agent_state_labels,
            frozenset({"agent:ready", "agent:claimed", "agent:blocked"}),
        )
        self.assertEqual(
            policy.work_type_labels,
            frozenset({"work:bug", "work:change", "work:investigation"}),
        )
        self.assertEqual(policy.needs_split_label, "needs-split")
        self.assertEqual(
            {label.removeprefix(policy.priority_prefix) for label in policy.priority_labels},
            ticket_contracts.PRIORITIES,
        )
        self.assertEqual(
            {label.removeprefix(policy.work_type_prefix) for label in policy.work_type_labels},
            ticket_contracts.WORK_TYPES,
        )
        for marker in (
            policy.contract_start_marker,
            policy.contract_end_marker,
            policy.review_ref_start_marker,
            policy.review_ref_end_marker,
        ):
            self.assertTrue(marker.isascii())
            self.assertIn(":v1:", marker)
        with self.assertRaises(FrozenInstanceError):
            policy.policy_version = 2

    def test_labels_parse_to_optional_contract_shaped_values(self):
        empty = parse_ticket_labels([])
        self.assertIsNone(empty.priority)
        self.assertIsNone(empty.points)
        self.assertIsNone(empty.agent_state)
        self.assertIsNone(empty.work_type)
        self.assertFalse(empty.needs_split)

        parsed = parse_ticket_labels(
            (
                "documentation",
                "priority:P1",
                "size:3",
                "agent:claimed",
                "work:investigation",
            )
        )
        self.assertEqual(parsed.priority, "P1")
        self.assertEqual(parsed.points, 3)
        self.assertEqual(parsed.agent_state, "claimed")
        self.assertEqual(parsed.work_type, "investigation")
        self.assertFalse(parsed.needs_split)

    def test_zero_or_one_label_per_governed_group_is_structurally_valid(self):
        for label in sorted(
            TICKET_POLICY_V1.priority_labels
            | TICKET_POLICY_V1.size_labels
            | TICKET_POLICY_V1.agent_state_labels
            | TICKET_POLICY_V1.work_type_labels
        ):
            with self.subTest(label=label):
                parse_ticket_labels([label])

        for agent_state in (None, "agent:blocked"):
            labels = ["needs-split"]
            if agent_state is not None:
                labels.append(agent_state)
            with self.subTest(agent_state=agent_state):
                parsed = parse_ticket_labels(labels)
                self.assertTrue(parsed.needs_split)

    def test_multiple_labels_in_each_governed_group_fail_deterministically(self):
        groups = (
            (TICKET_POLICY_V1.priority_labels, "multiple-priority-labels"),
            (TICKET_POLICY_V1.size_labels, "multiple-size-labels"),
            (TICKET_POLICY_V1.agent_state_labels, "multiple-agent-state-labels"),
            (TICKET_POLICY_V1.work_type_labels, "multiple-work-type-labels"),
        )
        for group, code in groups:
            ordered_group = sorted(group)
            for count in range(2, len(ordered_group) + 1):
                for labels in combinations(ordered_group, count):
                    for ordered in (labels, tuple(reversed(labels))):
                        with self.subTest(labels=ordered):
                            self.assertPolicyError(
                                lambda ordered=ordered: parse_ticket_labels(ordered),
                                code,
                                "labels",
                            )

    def test_every_pair_of_agent_states_is_rejected(self):
        states = sorted(TICKET_POLICY_V1.agent_state_labels)
        for left_index, left in enumerate(states):
            for right in states[left_index + 1 :]:
                with self.subTest(left=left, right=right):
                    self.assertPolicyError(
                        lambda left=left, right=right: parse_ticket_labels(
                            [left, right]
                        ),
                        "multiple-agent-state-labels",
                        "labels",
                    )

    def test_needs_split_rejects_eligibility_states_only(self):
        for state in ("agent:ready", "agent:claimed"):
            with self.subTest(state=state):
                self.assertPolicyError(
                    lambda state=state: parse_ticket_labels(["needs-split", state]),
                    "needs-split-eligible",
                    "labels",
                )

        for labels in (["needs-split"], ["needs-split", "agent:blocked"]):
            self.assertTrue(parse_ticket_labels(labels).needs_split)

    def test_label_collection_types_duplicates_and_text_subclasses_are_strict(self):
        invalid_collections = (None, set(), frozenset(), "priority:P0", ListSubclass())
        for labels in invalid_collections:
            with self.subTest(labels=type(labels).__name__):
                self.assertPolicyError(
                    lambda labels=labels: parse_ticket_labels(labels),
                    "invalid-label-collection",
                    "labels",
                )

        for labels in ([1], [TextSubclass("priority:P0")]):
            with self.subTest(labels=labels):
                self.assertPolicyError(
                    lambda labels=labels: parse_ticket_labels(labels),
                    "invalid-label-type",
                    "labels",
                )

        for labels in (
            ["priority:P0", "priority:P0"],
            ["unrelated", "unrelated"],
        ):
            with self.subTest(labels=labels):
                self.assertPolicyError(
                    lambda labels=labels: parse_ticket_labels(labels),
                    "duplicate-label",
                    "labels",
                )

    def test_unknown_or_miscased_governed_labels_fail_but_unrelated_labels_pass(self):
        cases = (
            ("priority:P2", "unknown-priority-label"),
            ("Priority:P0", "unknown-priority-label"),
            ("size:4", "unknown-size-label"),
            ("SIZE:1", "unknown-size-label"),
            ("agent:paused", "unknown-agent-state-label"),
            ("Agent:ready", "unknown-agent-state-label"),
            ("work:feature", "unknown-work-type-label"),
            ("WORK:bug", "unknown-work-type-label"),
            ("Needs-Split", "unknown-needs-split-label"),
        )
        for label, code in cases:
            with self.subTest(label=label):
                self.assertPolicyError(
                    lambda label=label: parse_ticket_labels([label]),
                    code,
                    "labels",
                )
        self.assertEqual(parse_ticket_labels(["release:P0"]), parse_ticket_labels([]))

    def test_error_selection_does_not_depend_on_github_label_order(self):
        labels = ["work:feature", "size:1", "priority:P2", "agent:ready"]
        for ordered in (labels, list(reversed(labels))):
            with self.subTest(ordered=ordered):
                self.assertPolicyError(
                    lambda ordered=ordered: parse_ticket_labels(ordered),
                    "unknown-priority-label",
                    "labels",
                )

    def test_golden_issue_body_accepts_lf_and_crlf(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                parsed = parse_ticket_issue_body(issue_body(newline=newline))
                self.assertEqual(parsed.contract.issue_number, 5)
                self.assertEqual(parsed.contract.points, 1)
                self.assertEqual(parsed.review_ref.run_id, "claude-opus-5:review-5")

    def test_issue_body_requires_exact_plain_utf8_text_with_bounded_size(self):
        for body in (None, b"{}", TextSubclass(issue_body())):
            with self.subTest(body_type=type(body).__name__):
                self.assertPolicyError(
                    lambda body=body: parse_ticket_issue_body(body),
                    "invalid-issue-body-type",
                    "body",
                )
        self.assertPolicyError(
            lambda: parse_ticket_issue_body("\ud800"),
            "invalid-issue-body-encoding",
            "body",
        )
        self.assertPolicyError(
            lambda: parse_ticket_issue_body("x" * (MAX_ISSUE_BODY_CHARACTERS + 1)),
            "issue-body-too-large",
            "body",
        )
        multibyte = "😀" * ((MAX_ISSUE_BODY_BYTES // 4) + 1)
        self.assertLessEqual(len(multibyte), MAX_ISSUE_BODY_CHARACTERS)
        self.assertPolicyError(
            lambda: parse_ticket_issue_body(multibyte),
            "issue-body-too-large",
            "body",
        )

    def test_each_marker_is_required_exactly_once(self):
        body = issue_body()
        markers = (
            (TICKET_POLICY_V1.contract_start_marker, "contractStartMarker"),
            (TICKET_POLICY_V1.contract_end_marker, "contractEndMarker"),
            (TICKET_POLICY_V1.review_ref_start_marker, "reviewRefStartMarker"),
            (TICKET_POLICY_V1.review_ref_end_marker, "reviewRefEndMarker"),
        )
        for marker, field in markers:
            with self.subTest(marker=marker, mode="missing"):
                self.assertPolicyError(
                    lambda marker=marker: parse_ticket_issue_body(
                        body.replace(marker, "", 1)
                    ),
                    "missing-marker",
                    field,
                )
            with self.subTest(marker=marker, mode="duplicate"):
                self.assertPolicyError(
                    lambda marker=marker: parse_ticket_issue_body(body + "\n" + marker),
                    "duplicate-marker",
                    field,
                )

    def test_marker_case_spacing_and_full_order_fail_closed(self):
        body = issue_body()
        start = TICKET_POLICY_V1.contract_start_marker
        for replacement in (start.upper(), start.replace(" -->", "-->")):
            with self.subTest(replacement=replacement):
                self.assertPolicyError(
                    lambda replacement=replacement: parse_ticket_issue_body(
                        body.replace(start, replacement)
                    ),
                    "missing-marker",
                    "contractStartMarker",
                )

        policy = TICKET_POLICY_V1
        fragments = {
            "contract": json.dumps(contract()),
            "review": json.dumps(review_ref()),
        }
        invalid_orders = (
            (
                policy.contract_end_marker,
                fragments["contract"],
                policy.contract_start_marker,
                policy.review_ref_start_marker,
                fragments["review"],
                policy.review_ref_end_marker,
            ),
            (
                policy.contract_start_marker,
                policy.review_ref_start_marker,
                fragments["contract"],
                policy.contract_end_marker,
                fragments["review"],
                policy.review_ref_end_marker,
            ),
            (
                policy.review_ref_start_marker,
                fragments["review"],
                policy.review_ref_end_marker,
                policy.contract_start_marker,
                fragments["contract"],
                policy.contract_end_marker,
            ),
        )
        for parts in invalid_orders:
            with self.subTest(parts=parts):
                self.assertPolicyError(
                    lambda parts=parts: parse_ticket_issue_body("\n".join(parts)),
                    "invalid-marker-order",
                    "body",
                )

    def test_non_ascii_boundary_whitespace_and_fragment_injection_are_rejected(self):
        marker = TICKET_POLICY_V1.contract_start_marker
        body = issue_body().replace(marker + "\n", marker + "\u00a0", 1)
        self.assertPolicyError(
            lambda: parse_ticket_issue_body(body),
            "invalid-contract-json",
            "contractBlock",
        )

        policy = TICKET_POLICY_V1
        injection = "\n".join(
            (
                policy.contract_start_marker,
                '{"k":{"a":1',
                policy.contract_end_marker,
                policy.review_ref_start_marker,
                '{"b":2}}',
                policy.review_ref_end_marker,
            )
        )
        self.assertPolicyError(
            lambda: parse_ticket_issue_body(injection),
            "invalid-contract-json",
            "contractBlock",
        )

    def test_each_fragment_must_be_one_complete_json_object(self):
        policy = TICKET_POLICY_V1
        cases = (
            ("contract", "", "invalid-contract-json", "contractBlock"),
            ("contract", "[]", "invalid-contract-json-object", "contractBlock"),
            ("contract", "{} {}", "invalid-contract-json", "contractBlock"),
            ("review", "null", "invalid-review-ref-json-object", "reviewRefBlock"),
            ("review", "{", "invalid-review-ref-json", "reviewRefBlock"),
        )
        valid_contract = json.dumps(contract())
        valid_review = json.dumps(review_ref())
        for target, payload, code, field in cases:
            contract_payload = payload if target == "contract" else valid_contract
            review_payload = payload if target == "review" else valid_review
            body = "\n".join(
                (
                    policy.contract_start_marker,
                    contract_payload,
                    policy.contract_end_marker,
                    policy.review_ref_start_marker,
                    review_payload,
                    policy.review_ref_end_marker,
                )
            )
            with self.subTest(target=target, payload=payload):
                self.assertPolicyError(
                    lambda body=body: parse_ticket_issue_body(body), code, field
                )

    def test_strict_envelope_errors_propagate_for_both_fragments(self):
        policy = TICKET_POLICY_V1
        raw_contract = json.dumps(contract())
        raw_review = json.dumps(review_ref())
        cases = (
            (
                raw_contract.replace(
                    '"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'
                ),
                raw_review,
                "duplicate-json-key",
            ),
            (
                raw_contract,
                raw_review.replace(
                    '"runId": "claude-opus-5:review-5"',
                    '"runId": "claude-opus-5:review-5", "extra": NaN',
                ),
                "non-finite-json-number",
            ),
        )
        for contract_payload, review_payload, code in cases:
            body = "\n".join(
                (
                    policy.contract_start_marker,
                    contract_payload,
                    policy.contract_end_marker,
                    policy.review_ref_start_marker,
                    review_payload,
                    policy.review_ref_end_marker,
                )
            )
            with self.subTest(code=code):
                with self.assertRaises(TicketContractError) as caught:
                    parse_ticket_issue_body(body)
                self.assertEqual(caught.exception.code, code)
                self.assertIsInstance(caught.exception, ValueError)

    def test_strict_loader_is_called_exactly_once_with_a_synthetic_envelope(self):
        calls = []
        sentinel = object()
        original = ticket_contracts.load_ticket_envelope_json

        def spy(document):
            calls.append(document)
            return sentinel

        ticket_contracts.load_ticket_envelope_json = spy
        try:
            self.assertIs(parse_ticket_issue_body(issue_body()), sentinel)
        finally:
            ticket_contracts.load_ticket_envelope_json = original

        self.assertEqual(len(calls), 1)
        parsed = json.loads(calls[0])
        self.assertEqual(parsed, {"contract": contract(), "reviewRef": review_ref()})

    def test_composed_envelope_retains_the_strict_loader_size_boundary(self):
        policy = TICKET_POLICY_V1
        oversized_contract = contract()
        oversized_contract["problemStatement"] = "😀" * 33_000
        contract_payload = json.dumps(
            oversized_contract, ensure_ascii=False, separators=(",", ":")
        )
        review_payload = json.dumps(
            {
                "runId": "review",
                "contractSha256": "0" * 64,
            },
            separators=(",", ":"),
        )
        body = "\n".join(
            (
                policy.contract_start_marker,
                contract_payload,
                policy.contract_end_marker,
                policy.review_ref_start_marker,
                review_payload,
                policy.review_ref_end_marker,
            )
        )
        self.assertLess(len(body.encode("utf-8")), MAX_ISSUE_BODY_BYTES)
        with self.assertRaises(TicketContractError) as caught:
            parse_ticket_issue_body(body)
        self.assertEqual(caught.exception.code, "ticket-json-too-large")

    def test_policy_module_has_no_external_io_dependencies(self):
        for name in (
            "os",
            "pathlib",
            "requests",
            "subprocess",
            "time",
            "urllib",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(ticket_policy, name))


if __name__ == "__main__":
    unittest.main()
