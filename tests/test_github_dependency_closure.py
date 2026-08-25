"""Proofs for the dependency-closure builder.

Every failure mode is proved by example, because each of them produces a
structurally valid closure carrying a wrong answer when it is not caught, and
the predicate cannot detect the difference.
"""

from __future__ import annotations

import json
import unittest

import council_tools.ticket_admission as ticket_admission
import council_tools.ticket_policy as ticket_policy
import council_tools.ticket_review as ticket_review
from council_tools.github_dependency_closure import (
    DependencyClosureError,
    build_dependency_closure,
    declared_dependencies,
)
from council_tools.ticket_admission import evaluate_ticket_admission
from council_tools.ticket_contracts import contract_sha256, sizing_projection_sha256

REPOSITORY = "garcia42/ai-council"
TICKET = 77
BASE_COMMIT = "a" * 40
RUN_ID = "claude-opus-5:11111111-2222-3333-4444-555555555555"


def contract(dependencies=(7, 8), issue_number=TICKET):
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "issueNumber": issue_number,
        "targetBranch": "main",
        "baseCommit": BASE_COMMIT,
        "workType": "change",
        "priority": "P0",
        "points": 3,
        "problemStatement": "Resolve one declared dependency set from acquired payloads.",
        "acceptanceCriteria": ["The closure corresponds to the declaration."],
        "testCommands": ["PYTHONPATH=src:. python3 -m pytest tests/ -q"],
        "allowedPaths": [
            {"kind": "file", "path": "src/council_tools/github_dependency_closure.py"}
        ],
        "outOfScope": ["Authorization"],
        "dependencies": list(dependencies),
        "rollbackPlan": "Revert the issue commit.",
    }


def body(raw_contract):
    policy = ticket_policy.TICKET_POLICY_V1
    return "\n".join(
        (
            "Prose.",
            policy.contract_start_marker,
            json.dumps(raw_contract, ensure_ascii=False, indent=2),
            policy.contract_end_marker,
            policy.review_ref_start_marker,
            json.dumps(
                {"runId": RUN_ID, "contractSha256": contract_sha256(raw_contract)}, indent=2
            ),
            policy.review_ref_end_marker,
            "More prose.",
        )
    )


def lookup(ticket_contract=None, states=None, extra=None):
    ticket_contract = ticket_contract if ticket_contract is not None else contract()
    states = states if states is not None else {7: "closed", 8: "closed"}
    table = {TICKET: {"state": "open", "body": body(ticket_contract)}}
    for number, state in states.items():
        table[number] = {"state": state, "body": "A plain issue with no contract."}
    if extra:
        table.update(extra)
    return table


class DeclaredDependencyTests(unittest.TestCase):
    def test_reads_the_declared_set_from_the_published_contract(self):
        self.assertEqual(declared_dependencies(body(contract()), field="b"), (7, 8))

    def test_a_body_with_no_contract_declares_nothing_rather_than_failing(self):
        # A dependency may be a plain issue.  That is not an error.
        self.assertEqual(declared_dependencies("Just prose.", field="b"), ())

    def test_an_unparseable_ticket_body_is_reported_not_treated_as_empty(self):
        # "No contract" and "a broken contract" look identical downstream and
        # mean opposite things, so the broken one must be named.
        policy = ticket_policy.TICKET_POLICY_V1
        broken = "\n".join((policy.contract_start_marker, "{not json", policy.contract_end_marker))
        with self.assertRaises(DependencyClosureError) as caught:
            declared_dependencies(broken, field="b")
        self.assertEqual(caught.exception.code, "unparseable-ticket-body")

    def test_a_non_text_body_is_refused(self):
        with self.assertRaises(DependencyClosureError) as caught:
            declared_dependencies(None, field="b")
        self.assertEqual(caught.exception.code, "invalid-issue-body")


class ClosureTests(unittest.TestCase):
    def test_one_entry_per_declared_dependency_in_declared_order(self):
        closure = build_dependency_closure(TICKET, lookup())
        self.assertEqual(
            closure,
            [
                {"issueNumber": 7, "state": "closed"},
                {"issueNumber": 8, "state": "closed"},
            ],
        )

    def test_entry_key_set_is_exactly_the_predicate_closure_key_set(self):
        for entry in build_dependency_closure(TICKET, lookup()):
            with self.subTest(entry=entry):
                self.assertEqual(set(entry), set(ticket_admission.CLOSURE_KEYS))
                self.assertIn(entry["state"], ticket_admission.ISSUE_STATES)

    def test_output_is_byte_identical_across_runs(self):
        table = lookup()
        first = json.dumps(build_dependency_closure(TICKET, table), sort_keys=True)
        second = json.dumps(build_dependency_closure(TICKET, table), sort_keys=True)
        self.assertEqual(first, second)

    def test_upper_case_states_are_folded(self):
        closure = build_dependency_closure(TICKET, lookup(states={7: "CLOSED", 8: "OPEN"}))
        self.assertEqual([e["state"] for e in closure], ["closed", "open"])

    def test_an_open_dependency_is_reported_not_hidden(self):
        closure = build_dependency_closure(TICKET, lookup(states={7: "closed", 8: "open"}))
        self.assertEqual(closure[1], {"issueNumber": 8, "state": "open"})

    def test_a_ticket_declaring_nothing_yields_an_empty_closure(self):
        self.assertEqual(build_dependency_closure(TICKET, lookup(contract(dependencies=()))), [])

    def test_a_callable_lookup_is_accepted(self):
        table = lookup()
        self.assertEqual(len(build_dependency_closure(TICKET, table.get)), 2)


class FailureTests(unittest.TestCase):
    def assert_code(self, code, *args, **kwargs):
        with self.assertRaises(DependencyClosureError) as caught:
            build_dependency_closure(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_an_absent_dependency_is_named_not_omitted(self):
        # Omission is the dangerous one: the predicate never learns the
        # dependency existed, so it cannot notice it is missing.
        self.assert_code("dependency-not-available", TICKET, lookup(states={7: "closed"}))

    def test_a_lookup_that_raises_is_an_absent_dependency(self):
        def raising(number):
            if number == TICKET:
                return {"state": "open", "body": body(contract())}
            raise RuntimeError("network")

        self.assert_code("dependency-not-available", TICKET, raising)

    def test_a_self_declared_dependency_is_refused_upstream_at_parse(self):
        # ticket_contracts already rejects this with `self-dependency`, so a body
        # carrying it cannot be parsed at all.  Re-checking it here would be
        # unreachable, and a second copy of a rule is what drifts.  What this
        # module owes is that the upstream refusal is surfaced, not swallowed.
        self.assert_code("unparseable-ticket-body", TICKET, lookup(contract(dependencies=(TICKET,))))

    def test_the_upstream_rule_is_the_one_that_fires(self):
        from council_tools.ticket_contracts import TicketContractError

        with self.assertRaises(TicketContractError) as caught:
            ticket_policy.parse_ticket_issue_body(body(contract(dependencies=(TICKET,))))
        self.assertEqual(caught.exception.code, "self-dependency")

    def test_a_duplicated_declaration_is_refused_upstream_at_parse(self):
        self.assert_code("unparseable-ticket-body", TICKET, lookup(contract(dependencies=(7, 7))))

        from council_tools.ticket_contracts import TicketContractError

        with self.assertRaises(TicketContractError) as caught:
            ticket_policy.parse_ticket_issue_body(body(contract(dependencies=(7, 7))))
        self.assertEqual(caught.exception.code, "duplicate-dependency")

    def test_an_over_large_declared_set_is_refused_upstream_at_parse(self):
        from council_tools.ticket_contracts import MAX_LIST_ITEMS, TicketContractError

        oversized = contract(dependencies=tuple(range(100, 100 + MAX_LIST_ITEMS + 1)))
        with self.assertRaises(TicketContractError) as caught:
            ticket_policy.parse_ticket_issue_body(body(oversized))
        self.assertEqual(caught.exception.code, "invalid-dependencies")
        # And the module surfaces that refusal rather than truncating.
        self.assert_code("unparseable-ticket-body", TICKET, lookup(oversized))

    def test_the_module_bound_is_not_looser_than_the_upstream_bound(self):
        # If upstream ever widens, the assertion in the module fires instead of
        # silently producing a closure the predicate would refuse.
        from council_tools.ticket_contracts import MAX_LIST_ITEMS

        self.assertLessEqual(
            ticket_admission.MAX_ADMISSION_DEPENDENCIES, MAX_LIST_ITEMS
        )

    def test_an_unparseable_dependency_declaration_is_refused(self):
        policy = ticket_policy.TICKET_POLICY_V1
        table = {TICKET: {"state": "open", "body": policy.contract_start_marker + "\n{bad\n" + policy.contract_end_marker}}
        self.assert_code("unparseable-ticket-body", TICKET, table)

    def test_a_malformed_payload_is_refused(self):
        self.assert_code("invalid-payload", TICKET, {TICKET: "not a payload"})

    def test_an_unknown_dependency_state_is_refused(self):
        self.assert_code("unknown-issue-state", TICKET, lookup(states={7: "merged", 8: "closed"}))

    def test_a_missing_state_is_refused(self):
        table = lookup()
        table[7] = {"body": "no state here"}
        self.assert_code("invalid-state", TICKET, table)

    def test_an_invalid_requested_issue_number_is_refused(self):
        for number in (0, -1, True, "77", None):
            with self.subTest(number=repr(number)):
                self.assert_code("invalid-issue-number", number, lookup())

    def test_a_non_callable_lookup_is_refused(self):
        self.assert_code("invalid-lookup", TICKET, 17)


class PredicateAcceptanceTests(unittest.TestCase):
    def test_a_produced_closure_is_accepted_by_the_real_predicate(self):
        raw = contract()
        closure = build_dependency_closure(TICKET, lookup(raw))
        snapshot = {
            "repository": REPOSITORY,
            "issueNumber": TICKET,
            "state": "open",
            "labels": ["priority:P0", "size:3", "agent:ready", "work:change"],
            "body": body(raw),
        }
        context = {
            "repository": REPOSITORY,
            "issueNumber": TICKET,
            "targetBranch": "main",
            "baseCommit": BASE_COMMIT,
            "baseCommitEvidence": {"contractBaseIsAncestor": True, "changedPaths": []},
            "dependencyClosure": closure,
        }
        review = ticket_review.validate_ticket_review(
            {
                "schemaVersion": 2,
                "runId": RUN_ID,
                "contractSha256": contract_sha256(raw),
                "sizingProjectionSha256": sizing_projection_sha256(raw),
                "requiredSeats": ["claude", "codex"],
                "seatReviews": [
                    {"seatId": "claude", "status": "submitted", "engineerDays": 2,
                     "singleOutcome": True, "splitReasons": [], "priority": "P1", "confidence": 80},
                    {"seatId": "codex", "status": "submitted", "engineerDays": 3,
                     "singleOutcome": True, "splitReasons": [], "priority": "P0", "confidence": 70},
                ],
            },
            expected_contract_sha256=contract_sha256(raw),
        )
        result = evaluate_ticket_admission(snapshot, context, [review])
        self.assertTrue(result.structurally_eligible)
        self.assertEqual(result.reasons, ())

    def test_an_open_dependency_is_the_predicate_s_rejection_not_ours(self):
        raw = contract()
        closure = build_dependency_closure(TICKET, lookup(raw, states={7: "closed", 8: "open"}))
        snapshot = {
            "repository": REPOSITORY, "issueNumber": TICKET, "state": "open",
            "labels": ["priority:P0", "size:3", "agent:ready", "work:change"],
            "body": body(raw),
        }
        context = {
            "repository": REPOSITORY, "issueNumber": TICKET, "targetBranch": "main",
            "baseCommit": BASE_COMMIT,
            "baseCommitEvidence": {"contractBaseIsAncestor": True, "changedPaths": []},
            "dependencyClosure": closure,
        }
        review = ticket_review.validate_ticket_review(
            {
                "schemaVersion": 2, "runId": RUN_ID,
                "contractSha256": contract_sha256(raw),
                "sizingProjectionSha256": sizing_projection_sha256(raw),
                "requiredSeats": ["claude", "codex"],
                "seatReviews": [
                    {"seatId": "claude", "status": "submitted", "engineerDays": 2,
                     "singleOutcome": True, "splitReasons": [], "priority": "P1", "confidence": 80},
                    {"seatId": "codex", "status": "submitted", "engineerDays": 3,
                     "singleOutcome": True, "splitReasons": [], "priority": "P0", "confidence": 70},
                ],
            },
            expected_contract_sha256=contract_sha256(raw),
        )
        result = evaluate_ticket_admission(snapshot, context, [review])
        self.assertIn("dependency-not-closed", result.reasons)


class PurityTests(unittest.TestCase):
    def test_module_exposes_no_io_capable_name(self):
        import council_tools.github_dependency_closure as module

        forbidden = {"os", "io", "sys", "subprocess", "pathlib", "socket", "urllib", "requests"}
        self.assertEqual(forbidden.intersection(vars(module)), set())

    def test_errors_are_typed_with_stable_codes(self):
        self.assertTrue(issubclass(DependencyClosureError, ValueError))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
