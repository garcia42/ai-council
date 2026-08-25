"""Refusal proofs for the acquisition boundary.

Every failure mode here produces a payload the downstream normalizers would
accept, because they validate shape.  So each is proved by example against a
mocked transport, and the success path is carried all the way through the
snapshot builder into the real admission predicate.
"""

from __future__ import annotations

import json
import unittest

import council_tools.ticket_policy as ticket_policy
import council_tools.ticket_review as ticket_review
from council_tools.github_issue_fetch import (
    IDENTITY_FIELD,
    GitHubFetchError,
    fetch_issue,
)
from council_tools.github_ticket_snapshot import build_issue_snapshot
from council_tools.ticket_admission import evaluate_ticket_admission
from council_tools.ticket_contracts import contract_sha256, sizing_projection_sha256

REPOSITORY = "garcia42/ai-council"
ISSUE = 77
BASE_COMMIT = "a" * 40
RUN_ID = "claude-opus-5:11111111-2222-3333-4444-555555555555"


def contract():
    return {
        "schemaVersion": 1, "repository": REPOSITORY, "issueNumber": ISSUE,
        "targetBranch": "main", "baseCommit": BASE_COMMIT, "workType": "change",
        "priority": "P0", "points": 3,
        "problemStatement": "Acquire one payload without describing the wrong thing.",
        "acceptanceCriteria": ["Every wrong-thing response is refused."],
        "testCommands": ["PYTHONPATH=src:. python3 -m pytest tests/ -q"],
        "allowedPaths": [{"kind": "file", "path": "src/council_tools/github_issue_fetch.py"}],
        "outOfScope": ["Authorization"], "dependencies": [7],
        "rollbackPlan": "Revert the issue commit.",
    }


def body(raw):
    policy = ticket_policy.TICKET_POLICY_V1
    return "\n".join((
        "Prose.", policy.contract_start_marker,
        json.dumps(raw, ensure_ascii=False, indent=2), policy.contract_end_marker,
        policy.review_ref_start_marker,
        json.dumps({"runId": RUN_ID, "contractSha256": contract_sha256(raw)}, indent=2),
        policy.review_ref_end_marker, "More prose.",
    ))


def payload(**overrides):
    base = {
        "number": ISSUE, "state": "OPEN", "repository": REPOSITORY,
        "labels": [{"name": "priority:P0"}, {"name": "size:3"},
                   {"name": "agent:ready"}, {"name": "work:change"}],
        "body": body(contract()), IDENTITY_FIELD: "2026-08-25T00:00:00Z",
    }
    base.update(overrides)
    return base


def transport_returning(*responses):
    """A transport handing back each response in turn."""

    queue = list(responses)

    def transport(repository, issue_number):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return transport


class SuccessPathTests(unittest.TestCase):
    def test_a_consistent_observation_is_returned(self):
        result = fetch_issue(transport_returning(payload()), repository=REPOSITORY, issue_number=ISSUE)
        self.assertEqual(result["number"], ISSUE)
        self.assertEqual(result["state"], "OPEN")

    def test_the_result_feeds_the_snapshot_builder_and_the_real_predicate(self):
        raw = contract()
        acquired = fetch_issue(
            transport_returning(payload(body=body(raw))), repository=REPOSITORY, issue_number=ISSUE
        )
        snapshot = build_issue_snapshot(acquired, repository=REPOSITORY, issue_number=ISSUE)
        context = {
            "repository": REPOSITORY, "issueNumber": ISSUE, "targetBranch": "main",
            "baseCommit": BASE_COMMIT,
            "baseCommitEvidence": {"contractBaseIsAncestor": True, "changedPaths": []},
            "dependencyClosure": [{"issueNumber": 7, "state": "closed"}],
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
        self.assertTrue(result.structurally_eligible)
        self.assertEqual(result.reasons, ())

    def test_the_transport_receives_what_was_requested(self):
        seen = []

        def transport(repository, issue_number):
            seen.append((repository, issue_number))
            return payload()

        fetch_issue(transport, repository=REPOSITORY, issue_number=ISSUE)
        self.assertEqual(set(seen), {(REPOSITORY, ISSUE)})


class MutationDetectionTests(unittest.TestCase):
    def test_an_issue_edited_between_calls_is_refused(self):
        # Returning either version would hand downstream a body and a label set
        # that may belong to different versions of the issue.
        moved = payload(**{IDENTITY_FIELD: "2026-08-25T00:00:01Z", "body": "edited"})
        with self.assertRaises(GitHubFetchError) as caught:
            fetch_issue(
                transport_returning(payload(), moved), repository=REPOSITORY, issue_number=ISSUE
            )
        self.assertEqual(caught.exception.code, "issue-changed-during-read")

    def test_an_unchanged_identity_value_is_accepted(self):
        same = payload()
        fetch_issue(transport_returning(same, same), repository=REPOSITORY, issue_number=ISSUE)


class RefusalTests(unittest.TestCase):
    def assert_code(self, code, *responses):
        with self.assertRaises(GitHubFetchError) as caught:
            fetch_issue(transport_returning(*responses), repository=REPOSITORY, issue_number=ISSUE)
        self.assertEqual(caught.exception.code, code)

    def test_a_rate_limited_response_is_a_refusal_not_an_absence(self):
        # Read as an absence it becomes "this issue has no labels".
        for marker in ({"rateLimited": True}, {"status": 403}, {"status": 429}):
            with self.subTest(marker=marker):
                self.assert_code("rate-limited", {**payload(), **marker})

    def test_a_redirect_is_refused(self):
        for marker in ({"redirected": True}, {"movedTo": "someone/else"}):
            with self.subTest(marker=marker):
                self.assert_code("repository-redirected", {**payload(), **marker})

    def test_a_repository_self_report_that_disagrees_is_refused(self):
        # A redirect that did not announce itself looks exactly like the
        # response that was wanted, so the self-report is checked not trusted.
        self.assert_code("repository-mismatch", payload(repository="someone/else"))

    def test_an_issue_number_that_disagrees_is_refused(self):
        self.assert_code("issue-number-mismatch", payload(number=ISSUE + 1))
        self.assert_code("issue-number-mismatch", payload(number="77"))

    def test_a_malformed_response_is_refused(self):
        for response in (None, [], "issue", 1):
            with self.subTest(response=repr(response)):
                self.assert_code("malformed-response", response)

    def test_a_response_missing_any_required_field_is_refused(self):
        for field in ("number", "state", "labels", "body", IDENTITY_FIELD):
            candidate = payload()
            del candidate[field]
            with self.subTest(field=field):
                self.assert_code("malformed-response", candidate)

    def test_a_non_text_identity_value_is_refused(self):
        self.assert_code("malformed-response", payload(**{IDENTITY_FIELD: 1}))

    def test_a_transport_that_raises_is_a_refusal(self):
        def raising(repository, issue_number):
            raise RuntimeError("connection reset")

        with self.assertRaises(GitHubFetchError) as caught:
            fetch_issue(raising, repository=REPOSITORY, issue_number=ISSUE)
        self.assertEqual(caught.exception.code, "transport-failed")

    def test_invalid_arguments_are_refused(self):
        with self.assertRaises(GitHubFetchError) as caught:
            fetch_issue(transport_returning(payload()), repository=None, issue_number=ISSUE)
        self.assertEqual(caught.exception.code, "malformed-response")
        for number in (0, -1, True, "77"):
            with self.subTest(number=repr(number)):
                with self.assertRaises(GitHubFetchError) as caught:
                    fetch_issue(transport_returning(payload()), repository=REPOSITORY, issue_number=number)
                self.assertEqual(caught.exception.code, "invalid-issue-number")
        with self.assertRaises(GitHubFetchError) as caught:
            fetch_issue(17, repository=REPOSITORY, issue_number=ISSUE)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid-transport")


class NoNetworkAndNoWriteTests(unittest.TestCase):
    def test_the_module_imports_no_network_client(self):
        import council_tools.github_issue_fetch as module

        forbidden = {"requests", "urllib", "http", "socket", "subprocess", "os"}
        self.assertEqual(forbidden.intersection(vars(module)), set())

    def test_the_public_surface_exposes_no_write_parameter(self):
        import inspect

        signature = inspect.signature(fetch_issue)
        self.assertEqual(
            set(signature.parameters), {"transport", "repository", "issue_number"}
        )

    def test_errors_are_typed_with_stable_codes(self):
        self.assertTrue(issubclass(GitHubFetchError, ValueError))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
