import unittest

from council_tools.forecasts import LedgerError, make_attempt, make_completion
from council_tools.reviewed_record import (
    NON_COMMIT_STATES,
    ReviewedRecordError,
    validate_reviewed_record,
)


SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "c" * 64


class CommitArrayTest(unittest.TestCase):
    def test_an_array_of_full_object_names_is_a_commit_review(self):
        self.assertEqual(validate_reviewed_record([SHA, OTHER_SHA]), "commits")

    def test_uppercase_object_names_are_accepted_because_git_resolves_them(self):
        self.assertEqual(validate_reviewed_record([SHA.upper()]), "commits")

    def test_an_empty_array_is_refused_because_it_cannot_be_told_from_unset(self):
        with self.assertRaises(ReviewedRecordError) as caught:
            validate_reviewed_record([])
        self.assertIn("no-diff", str(caught.exception))

    def test_an_abbreviation_is_refused(self):
        with self.assertRaises(ReviewedRecordError) as caught:
            validate_reviewed_record([SHA[:7]])
        self.assertIn("rewritten history", str(caught.exception))

    def test_a_repeated_object_name_is_refused(self):
        with self.assertRaises(ReviewedRecordError):
            validate_reviewed_record([SHA, SHA])

    def test_non_object_names_are_refused_with_their_index(self):
        with self.assertRaises(ReviewedRecordError) as caught:
            validate_reviewed_record([SHA, "not a sha"])
        self.assertIn("commits[1]", str(caught.exception))

    def test_a_bare_string_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(ReviewedRecordError):
            validate_reviewed_record(SHA)


class NonCommitObjectTest(unittest.TestCase):
    def test_content_states_require_a_digest_of_what_was_read(self):
        for state in ("uncommitted", "staged"):
            with self.subTest(state=state):
                self.assertEqual(
                    validate_reviewed_record(
                        {"state": state, "contentSha256": DIGEST, "base": SHA}
                    ),
                    "non-commit",
                )
                with self.assertRaises(ReviewedRecordError) as caught:
                    validate_reviewed_record({"state": state})
                self.assertIn("but not what", str(caught.exception))

    def test_contentless_states_must_not_carry_an_arbitrary_digest(self):
        for state in ("no-diff", "decision-only"):
            with self.subTest(state=state):
                self.assertEqual(
                    validate_reviewed_record({"state": state}), "non-commit"
                )
                with self.assertRaises(ReviewedRecordError):
                    validate_reviewed_record({"state": state, "contentSha256": DIGEST})

    def test_a_bare_base_pointer_is_refused(self):
        with self.assertRaises(ReviewedRecordError) as caught:
            validate_reviewed_record({"base": SHA})
        self.assertIn("does not say what was reviewed", str(caught.exception))

    def test_the_state_vocabulary_is_closed(self):
        with self.assertRaises(ReviewedRecordError):
            validate_reviewed_record({"state": "uncommitted-untracked"})
        self.assertEqual(
            set(NON_COMMIT_STATES),
            {"uncommitted", "staged", "no-diff", "decision-only"},
        )

    def test_unknown_keys_are_refused_so_the_vocabulary_cannot_drift(self):
        # `base`/`candidate`/`candidate_tree`/`prodHead`/`stagedTree`/`branch`
        # all accumulated in the real ledger because nothing rejected them.
        for extra in ("candidate_tree", "prodHead", "stagedTree", "branch"):
            with self.subTest(extra=extra):
                with self.assertRaises(ReviewedRecordError) as caught:
                    validate_reviewed_record({"state": "no-diff", extra: SHA})
                self.assertIn("unknown keys", str(caught.exception))

    def test_a_malformed_digest_or_base_is_refused(self):
        with self.assertRaises(ReviewedRecordError):
            validate_reviewed_record({"state": "staged", "contentSha256": "short"})
        with self.assertRaises(ReviewedRecordError):
            validate_reviewed_record(
                {"state": "staged", "contentSha256": DIGEST, "base": "nope"}
            )

    def test_a_note_must_be_real_text_when_present(self):
        self.assertEqual(
            validate_reviewed_record({"state": "no-diff", "note": "policy only"}),
            "non-commit",
        )
        with self.assertRaises(ReviewedRecordError):
            validate_reviewed_record({"state": "no-diff", "note": "   "})

    def test_shapes_that_are_neither_are_refused(self):
        for value in (None, 7, True, [[SHA]], {"state": None}):
            with self.subTest(value=value):
                with self.assertRaises(ReviewedRecordError):
                    validate_reviewed_record(value)


class CompletionEnforcementTest(unittest.TestCase):
    def attempt(self):
        return make_attempt(
            question="Should we deploy?",
            expected_seats=["code"],
            claim="The change completes its window without rollback",
            resolution_date="2027-03-31",
            resolved_by="Inspect the deployment record",
            decision_link="change-1",
            materiality="A rollback would reject the decision",
            action_if_true="Retain",
            action_if_false="Revert",
            evidence_cutoff_at="2027-03-01T12:00:00Z",
            ts="2027-03-01T12:00:00Z",
            related_outcome_ids=[],
        )

    def complete(self, council_fields):
        return make_completion(
            attempt=self.attempt(),
            council_fields=council_fields,
            seat_states={"code": "submitted"},
            probabilities={"code": 60},
            ts="2027-03-01T12:10:00Z",
        )

    def test_a_completion_without_a_reviewed_record_is_refused(self):
        with self.assertRaises(LedgerError) as caught:
            self.complete({"verdicts": {"code": "APPROVE"}})
        self.assertIn("must record what was reviewed", str(caught.exception))

    def test_an_unreadable_reviewed_record_is_refused_as_a_ledger_error(self):
        # The shape error has to surface as the error type the CLI already
        # catches, or it becomes a traceback at the public boundary.
        with self.assertRaises(LedgerError):
            self.complete({"commits": {"base": SHA}})
        with self.assertRaises(LedgerError):
            self.complete({"commits": []})

    def test_both_valid_shapes_seal_a_completion(self):
        row = self.complete({"commits": [SHA]})
        self.assertEqual(row["commits"], [SHA])
        row = self.complete({"commits": {"state": "no-diff"}})
        self.assertEqual(row["commits"], {"state": "no-diff"})

    def test_the_record_cannot_be_smuggled_past_as_a_protected_key(self):
        with self.assertRaises(LedgerError):
            self.complete({"commits": [SHA], "predictions": []})


if __name__ == "__main__":
    unittest.main()
