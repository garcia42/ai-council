import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from council_tools.git_claim_observation import (
    ALREADY_CURRENT,
    CONTENDED_REJECTION_SUMMARY,
    CREATED,
    MAX_PORCELAIN_BYTES,
    MAX_PORCELAIN_LINES,
    REJECTED,
    REJECTION_FLAG,
    SEQUENTIAL_REJECTION_SUMMARY,
    ClaimPushObservation,
    GitClaimObservationError,
    observe_claim_push,
)

REF = "refs/heads/ai-council/claims/9"
OID = "ec06a3d23ddcfb0719bf079dfd707847fd3937ef"
OTHER = "5a195c361596b5d6f8e737e56e8cbdba0cc240f7"
URL = "file:///tmp/claim.git"
GIT = shutil.which("git") or "/usr/bin/git"

# The exact measured bytes, tabs included.
CREATED_OUTPUT = f"To {URL}\n*\t{OID}:{REF}\t[new branch]\nDone\n"
CURRENT_OUTPUT = f"To {URL}\n=\t{OID}:{REF}\t[up to date]\nDone\n"
REJECTED_OUTPUT = f"To {URL}\n!\t{OTHER}:{REF}\t[rejected] (stale info)\nDone\n"


class MeasuredOutcomeTest(unittest.TestCase):
    def test_the_created_case(self):
        observation = observe_claim_push(CREATED_OUTPUT, expected_ref=REF)
        self.assertEqual(observation.outcome, CREATED)
        self.assertEqual(observation.flag, "*")
        self.assertEqual(observation.source, OID)
        self.assertEqual(observation.destination, REF)
        self.assertEqual(observation.summary, "[new branch]")

    def test_the_already_current_case(self):
        observation = observe_claim_push(CURRENT_OUTPUT, expected_ref=REF)
        self.assertEqual(observation.outcome, ALREADY_CURRENT)
        self.assertEqual(observation.flag, "=")

    def test_the_rejected_case(self):
        observation = observe_claim_push(REJECTED_OUTPUT, expected_ref=REF)
        self.assertEqual(observation.outcome, REJECTED)
        self.assertEqual(observation.flag, "!")
        self.assertEqual(observation.source, OTHER)

    def test_created_and_already_current_are_distinguished(self):
        """Both exit 0, and reading the status alone cannot tell them apart."""
        self.assertNotEqual(
            observe_claim_push(CREATED_OUTPUT, expected_ref=REF).outcome,
            observe_claim_push(CURRENT_OUTPUT, expected_ref=REF).outcome,
        )

    def test_the_separator_really_is_a_tab(self):
        spaced = CREATED_OUTPUT.replace("\t", " ")
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(spaced, expected_ref=REF)
        self.assertEqual(caught.exception.code, "unparseable-line")

    def test_a_summary_containing_spaces_survives_intact(self):
        self.assertEqual(
            observe_claim_push(REJECTED_OUTPUT, expected_ref=REF).summary,
            "[rejected] (stale info)",
        )


class RealGitPorcelainTest(unittest.TestCase):
    """The measured bytes above, taken from a real push rather than transcribed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="claim-porcelain-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"
        self._git("init", "--bare", "-q", "--object-format=sha1", str(self.remote))
        self._git("init", "-q", "--object-format=sha1", str(self.work))
        self.first = self._commit("one")
        self.second = self._commit("two")

    def _git(self, *args, cwd=None):
        return subprocess.run(
            [GIT, *args], cwd=cwd, capture_output=True, text=True, check=True
        )

    def _commit(self, message):
        self._git(
            "-c", "user.name=t", "-c", "user.email=t@e",
            "commit", "-q", "--allow-empty", "-m", message, cwd=self.work,
        )
        return self._git("rev-parse", "HEAD", cwd=self.work).stdout.strip()

    def _push(self, oid):
        return subprocess.run(
            [
                GIT, "push", "--porcelain", "--no-verify", f"file://{self.remote}",
                f"--force-with-lease={REF}:", f"{oid}:{REF}",
            ],
            cwd=self.work, capture_output=True, text=True,
        )

    def test_a_real_create_parses_as_created(self):
        pushed = self._push(self.first)
        self.assertEqual(pushed.returncode, 0)
        observation = observe_claim_push(pushed.stdout, expected_ref=REF)
        self.assertEqual(observation.outcome, CREATED)
        self.assertEqual(observation.source, self.first)

    def test_a_real_repeat_parses_as_already_current_and_also_exits_zero(self):
        self._push(self.first)
        pushed = self._push(self.first)
        self.assertEqual(pushed.returncode, 0)
        self.assertEqual(
            observe_claim_push(pushed.stdout, expected_ref=REF).outcome, ALREADY_CURRENT
        )

    def test_a_real_foreign_object_parses_as_rejected(self):
        self._push(self.first)
        pushed = self._push(self.second)
        self.assertEqual(pushed.returncode, 1)
        self.assertEqual(
            observe_claim_push(pushed.stdout, expected_ref=REF).outcome, REJECTED
        )

    def test_the_diagnostic_really_is_on_the_other_stream(self):
        self._push(self.first)
        pushed = self._push(self.second)
        self.assertIn("error:", pushed.stderr)
        self.assertNotIn("error:", pushed.stdout)

    def test_racing_processes_produce_exactly_one_created_reference(self):
        """The contended path, which every sequential test misses.

        This is the case the protocol exists to decide, and the summary its
        losers report is not the one a sequential second push produces.
        """
        import concurrent.futures

        racers = 6
        workspaces = []
        for index in range(racers):
            work = self.root / f"racer{index}"
            self._git("init", "-q", "--object-format=sha1", str(work))
            self._git(
                "-c", "user.name=r", "-c", f"user.email=r{index}@e",
                "commit", "-q", "--allow-empty", "-m", f"racer-{index}", cwd=work,
            )
            oid = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
            workspaces.append((work, oid))

        def attempt(pair):
            work, oid = pair
            return subprocess.run(
                [
                    GIT, "push", "--porcelain", "--no-verify", f"file://{self.remote}",
                    f"--force-with-lease={REF}:", f"{oid}:{REF}",
                ],
                cwd=work, capture_output=True, text=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=racers) as pool:
            pushed = list(pool.map(attempt, workspaces))

        outcomes = [
            observe_claim_push(result.stdout, expected_ref=REF).outcome
            for result in pushed
        ]
        self.assertEqual(
            outcomes.count(CREATED), 1, f"expected exactly one winner, got {outcomes}"
        )
        for outcome in outcomes:
            self.assertIn(outcome, (CREATED, ALREADY_CURRENT, REJECTED))
        self.assertEqual(
            outcomes.count(REJECTED) + outcomes.count(ALREADY_CURRENT), racers - 1
        )

        listed = self._git("ls-remote", f"file://{self.remote}").stdout.strip().splitlines()
        self.assertEqual(len(listed), 1, listed)

    def test_a_contended_loser_reports_a_summary_the_old_table_refused(self):
        """Pins the regression: this is the byte sequence that broke the observer."""
        import concurrent.futures

        workspaces = []
        for index in range(4):
            work = self.root / f"loser{index}"
            self._git("init", "-q", "--object-format=sha1", str(work))
            self._git(
                "-c", "user.name=r", "-c", f"user.email=l{index}@e",
                "commit", "-q", "--allow-empty", "-m", f"loser-{index}", cwd=work,
            )
            workspaces.append((work, self._git("rev-parse", "HEAD", cwd=work).stdout.strip()))

        def attempt(pair):
            work, oid = pair
            return subprocess.run(
                [
                    GIT, "push", "--porcelain", "--no-verify", f"file://{self.remote}",
                    f"--force-with-lease={REF}:", f"{oid}:{REF}",
                ],
                cwd=work, capture_output=True, text=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pushed = list(pool.map(attempt, workspaces))

        losers = [
            observe_claim_push(result.stdout, expected_ref=REF)
            for result in pushed
            if result.returncode != 0
        ]
        self.assertTrue(losers, "no loser: the pushes did not contend at all")

        # Both measured summaries are pinned. Which one a loser gets depends on
        # whether it lost the ref lock (contended) or found the ref already
        # written (serialised), and that is a property of the machine, not of
        # this module -- so either is accepted and anything else is a change in
        # Git's wording that should be seen rather than absorbed by the flag rule.
        measured = {CONTENDED_REJECTION_SUMMARY, SEQUENTIAL_REJECTION_SUMMARY}
        for loser in losers:
            with self.subTest(summary=loser.summary):
                self.assertEqual(loser.outcome, REJECTED)
                self.assertIn(
                    loser.summary,
                    measured,
                    f"unmeasured rejection summary on this binary: {loser.summary!r}",
                )

    def test_a_missing_remote_produces_no_porcelain_at_all(self):
        pushed = subprocess.run(
            [
                GIT, "push", "--porcelain", "--no-verify",
                f"file://{self.root}/absent.git",
                f"--force-with-lease={REF}:", f"{self.first}:{REF}",
            ],
            cwd=self.work, capture_output=True, text=True,
        )
        self.assertEqual(pushed.returncode, 128)
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(pushed.stdout, expected_ref=REF)
        self.assertEqual(caught.exception.code, "empty-output")


class TrailerTest(unittest.TestCase):
    def test_output_without_the_trailer_is_incomplete(self):
        truncated = f"To {URL}\n*\t{OID}:{REF}\t[new branch]\n"
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(truncated, expected_ref=REF)
        self.assertEqual(caught.exception.code, "missing-trailer")

    def test_a_truncated_prefix_carrying_a_status_line_is_still_refused(self):
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(f"To {URL}\n*\t{OID}:{REF}\t[new branch]", expected_ref=REF)
        self.assertEqual(caught.exception.code, "missing-trailer")

    def test_a_trailer_that_is_not_last_does_not_count(self):
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(
                f"To {URL}\nDone\n*\t{OID}:{REF}\t[new branch]\n", expected_ref=REF
            )
        self.assertEqual(caught.exception.code, "missing-trailer")

    def test_empty_output_is_refused(self):
        for value in ("", "\n", "   \n" .replace("   ", "")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(GitClaimObservationError) as caught:
                    observe_claim_push(value, expected_ref=REF)
                self.assertEqual(caught.exception.code, "empty-output")


class HostileOutputTest(unittest.TestCase):
    def test_a_status_line_for_another_reference_is_ignored(self):
        output = (
            f"To {URL}\n"
            f"*\t{OID}:refs/heads/somebody-else\t[new branch]\n"
            f"*\t{OID}:{REF}\t[new branch]\n"
            "Done\n"
        )
        self.assertEqual(observe_claim_push(output, expected_ref=REF).outcome, CREATED)

    def test_output_reporting_only_another_reference_is_refused(self):
        output = f"To {URL}\n*\t{OID}:refs/heads/other\t[new branch]\nDone\n"
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(output, expected_ref=REF)
        self.assertEqual(caught.exception.code, "reference-not-reported")

    def test_two_status_lines_for_the_requested_reference_are_refused(self):
        output = (
            f"To {URL}\n"
            f"*\t{OID}:{REF}\t[new branch]\n"
            f"=\t{OID}:{REF}\t[up to date]\n"
            "Done\n"
        )
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(output, expected_ref=REF)
        self.assertEqual(caught.exception.code, "reference-reported-more-than-once")

    def test_a_diagnostic_offered_as_porcelain_is_refused(self):
        for line in ("error: failed to push some refs", "fatal: not a repository"):
            with self.subTest(line=line):
                output = f"To {URL}\n{line}\n*\t{OID}:{REF}\t[new branch]\nDone\n"
                with self.assertRaises(GitClaimObservationError) as caught:
                    observe_claim_push(output, expected_ref=REF)
                self.assertEqual(caught.exception.code, "diagnostic-on-stdout")

    def test_a_granting_flag_with_an_unfamiliar_summary_is_still_refused(self):
        """Reading something as created or already-current asserts ownership."""
        for flag, summary in (
            ("*", "[up to date]"),
            ("=", "[new branch]"),
            ("*", "[new tag]"),
            ("=", "[everything up-to-date]"),
            ("+", "[forced update]"),
            ("-", "[deleted]"),
        ):
            with self.subTest(flag=flag, summary=summary):
                output = f"To {URL}\n{flag}\t{OID}:{REF}\t{summary}\nDone\n"
                with self.assertRaises(GitClaimObservationError) as caught:
                    observe_claim_push(output, expected_ref=REF)
                self.assertEqual(caught.exception.code, "unrecognised-outcome")

    def test_a_rejection_is_recognised_whatever_summary_the_server_chose(self):
        """Declining to claim is the recoverable direction, so the flag suffices."""
        for summary in (
            SEQUENTIAL_REJECTION_SUMMARY,
            CONTENDED_REJECTION_SUMMARY,
            "[remote rejected] pre-receive hook declined",
            "[remote rejected] (cannot lock ref)",
            "[rejected] (non-fast-forward)",
            "[remote rejected] (refusing to update protected ref)",
            "[rejected] (something no one has seen)",
        ):
            with self.subTest(summary=summary):
                output = f"To {URL}\n!\t{OID}:{REF}\t{summary}\nDone\n"
                observation = observe_claim_push(output, expected_ref=REF)
                self.assertEqual(observation.outcome, REJECTED)

    def test_the_server_summary_is_retained_exactly_as_written(self):
        output = f"To {URL}\n!\t{OID}:{REF}\t{CONTENDED_REJECTION_SUMMARY}\nDone\n"
        self.assertEqual(
            observe_claim_push(output, expected_ref=REF).summary,
            CONTENDED_REJECTION_SUMMARY,
        )

    def test_the_measured_contended_summary_is_pinned(self):
        """So a change to it is visible rather than absorbed by the flag rule."""
        self.assertEqual(
            CONTENDED_REJECTION_SUMMARY, "[remote rejected] (failed to update ref)"
        )
        self.assertEqual(SEQUENTIAL_REJECTION_SUMMARY, "[rejected] (stale info)")
        self.assertEqual(REJECTION_FLAG, "!")

    def test_a_reference_containing_a_colon_splits_on_the_last_one(self):
        weird = "refs/heads/ai-council/claims/9"
        output = f"To {URL}\n*\ta:b:{weird}\t[new branch]\nDone\n"
        observation = observe_claim_push(output, expected_ref=weird)
        self.assertEqual(observation.source, "a:b")
        self.assertEqual(observation.destination, weird)

    def test_an_empty_source_side_is_kept(self):
        output = f"To {URL}\n*\t:{REF}\t[new branch]\nDone\n"
        self.assertEqual(observe_claim_push(output, expected_ref=REF).source, "")

    def test_a_line_with_no_colon_is_refused(self):
        output = f"To {URL}\n*\tnocolon\t[new branch]\nDone\n"
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(output, expected_ref=REF)
        self.assertEqual(caught.exception.code, "unparseable-reference")

    def test_a_line_with_the_wrong_field_count_is_refused(self):
        for line in (f"*\t{OID}:{REF}", f"*\t{OID}:{REF}\t[new branch]\textra"):
            with self.subTest(line=line):
                with self.assertRaises(GitClaimObservationError) as caught:
                    observe_claim_push(f"To {URL}\n{line}\nDone\n", expected_ref=REF)
                self.assertEqual(caught.exception.code, "unparseable-line")


class BoundsTest(unittest.TestCase):
    def test_output_over_the_line_bound_is_refused(self):
        filler = "\n".join(f"*\t{OID}:refs/heads/x{i}\t[new branch]" for i in range(MAX_PORCELAIN_LINES + 1))
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push(f"To {URL}\n{filler}\nDone\n", expected_ref=REF)
        self.assertEqual(caught.exception.code, "too-many-lines")

    def test_output_over_the_byte_bound_is_refused(self):
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push("x" * (MAX_PORCELAIN_BYTES + 1), expected_ref=REF)
        self.assertEqual(caught.exception.code, "output-too-large")

    def test_the_byte_bound_is_checked_before_the_line_bound(self):
        """Otherwise a huge single line is split before it is measured."""
        with self.assertRaises(GitClaimObservationError) as caught:
            observe_claim_push("\n" * (MAX_PORCELAIN_BYTES + 1), expected_ref=REF)
        self.assertEqual(caught.exception.code, "output-too-large")


class NotAuthorizationTest(unittest.TestCase):
    def test_the_observation_refuses_boolean_coercion(self):
        observation = observe_claim_push(CREATED_OUTPUT, expected_ref=REF)
        self.assertIsInstance(observation, ClaimPushObservation)
        with self.assertRaises(TypeError):
            bool(observation)

    def test_even_a_rejection_refuses_boolean_coercion(self):
        with self.assertRaises(TypeError):
            bool(observe_claim_push(REJECTED_OUTPUT, expected_ref=REF))

    def test_the_observation_carries_no_success_field(self):
        observation = observe_claim_push(CREATED_OUTPUT, expected_ref=REF)
        for name in ("success", "ok", "owned", "authorized", "claimed"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(observation, name))

    def test_the_observation_is_frozen(self):
        import dataclasses

        observation = observe_claim_push(CREATED_OUTPUT, expected_ref=REF)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            observation.outcome = "created"


class InputTest(unittest.TestCase):
    def test_non_text_stdout_is_refused(self):
        for value in (None, 1, CREATED_OUTPUT.encode()):
            with self.subTest(value=value):
                with self.assertRaises(GitClaimObservationError) as caught:
                    observe_claim_push(value, expected_ref=REF)
                self.assertEqual(caught.exception.code, "invalid-stdout")

    def test_a_blank_or_padded_expected_reference_is_refused(self):
        for value in ("", " " + REF, REF + " ", None, 1):
            with self.subTest(value=value):
                with self.assertRaises(GitClaimObservationError) as caught:
                    observe_claim_push(CREATED_OUTPUT, expected_ref=value)
                self.assertEqual(caught.exception.code, "invalid-expected-ref")

    def test_error_is_a_value_error_with_a_stable_code_and_field(self):
        with self.assertRaises(ValueError) as caught:
            observe_claim_push("", expected_ref=REF)
        self.assertEqual(caught.exception.code, "empty-output")
        self.assertEqual(caught.exception.field, "stdout")


if __name__ == "__main__":
    unittest.main()
