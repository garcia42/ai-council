"""Proof that one claim attempt is exact, single, and never over-read.

The vector and the refusals are checked against a recording executor, because
what this module adds is *which bytes are sent* and *what is refused before
anything is sent*. Every outcome is then driven against a real bare repository
at a `file://` URL, with the pushed object produced by the merged materialization
module rather than by hand: the operation this module exists to run needs local
objects to send, and a test that avoids them is a test of the case that needs
least.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from council_tools.git_claim_attempt import (
    _CONSISTENT_STATUS,
    ATTEMPT_ALREADY_CURRENT,
    ATTEMPT_CREATED,
    ATTEMPT_INCONSISTENT,
    ATTEMPT_REJECTED,
    ATTEMPT_TRANSPORT_FAILED,
    ATTEMPT_UNREADABLE,
    FORBIDDEN_ATTEMPT_ARGUMENTS,
    PUSH_SUBCOMMAND,
    REQUIRED_OPTIONS,
    ClaimAttemptOutcome,
    GitClaimAttemptError,
    attempt_claim,
    build_attempt_arguments,
)
from council_tools.git_claim_materialization import materialize_claim_object
from council_tools.git_claim_request import ClaimRequest
from council_tools.git_object_id import Sha1ObjectId
from council_tools.git_process_contract import BASE_CHILD_ENVIRONMENT, GitCommandResult
from council_tools.git_process_executor import GitSubprocessExecutor
from council_tools.git_repository_lease import acquire_lease, release_descriptor
from council_tools.git_transport_execution import GitTransportExecutionError
from council_tools.git_transport_identity import validate_remote_url

GIT = shutil.which("git") or "/usr/bin/git"

OBJECT_ID = Sha1ObjectId("a1" * 20)
OTHER_OBJECT_ID = Sha1ObjectId("b2" * 20)
OBJECT_TEXT = OBJECT_ID.wire_text
OTHER_OBJECT_TEXT = OTHER_OBJECT_ID.wire_text
REFERENCE = "refs/heads/ai-council/claims/58"

REQUEST = ClaimRequest(
    repository="garcia42/ai-council",
    issue_number=58,
    contract_sha256="c" * 64,
    holder="session-a",
    issued_at="@0 +0000",
)


def target(url="file:///tmp/claim.git"):
    return validate_remote_url(url)


class RecordingExecutor:
    def __init__(self, result=None):
        self.commands = []
        self._result = GitCommandResult(exit_status=0) if result is None else result

    def execute(self, command):
        self.commands.append(command)
        return self._result


def porcelain(flag, object_id, reference, summary, trailer=True):
    lines = [f"To {target().url}", f"{flag}\t{object_id}:{reference}\t{summary}"]
    if trailer:
        lines.append("Done")
    return ("\n".join(lines) + "\n").encode()


class VectorTest(unittest.TestCase):
    def test_the_argument_vector_is_exactly_this(self):
        self.assertEqual(
            build_attempt_arguments(OBJECT_ID, REFERENCE),
            (
                "--porcelain",
                "--no-verify",
                f"--force-with-lease={REFERENCE}:",
                f"{OBJECT_TEXT}:{REFERENCE}",
            ),
        )

    def test_the_expected_value_is_empty_and_names_the_full_reference(self):
        # The empty value after the colon is what says create-only. A bare
        # --force-with-lease is a different and much weaker request.
        guard = build_attempt_arguments(OBJECT_ID, REFERENCE)[2]
        self.assertTrue(guard.endswith(":"))
        self.assertEqual(guard, f"--force-with-lease={REFERENCE}:")

    def test_the_refspec_is_explicit_and_names_the_same_reference(self):
        refspec = build_attempt_arguments(OBJECT_ID, REFERENCE)[3]
        source, _, destination = refspec.rpartition(":")
        self.assertEqual(source, OBJECT_TEXT)
        self.assertEqual(destination, REFERENCE)

    def test_no_force_option_is_present(self):
        arguments = build_attempt_arguments(OBJECT_ID, REFERENCE)
        self.assertNotIn("--force", arguments)
        self.assertNotIn("-f", arguments)

    def test_the_vector_reaches_the_child_through_the_transport_module(self):
        executor = RecordingExecutor()
        root = Path(tempfile.mkdtemp(prefix="attempt-vector-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        lease = acquire_lease(root, "src.git")
        self.addCleanup(release_descriptor, lease)
        attempt_claim(
            executor, GIT, target(), OBJECT_ID, REFERENCE, repository=lease
        )
        command = executor.commands[0]
        self.assertEqual(
            command.invocation.argv(),
            (
                f"--git-dir={lease.selector}",
                PUSH_SUBCOMMAND,
                target().url,
                *REQUIRED_OPTIONS,
                f"--force-with-lease={REFERENCE}:",
                f"{OBJECT_TEXT}:{REFERENCE}",
            ),
        )
        self.assertEqual(command.inherited_descriptors, (lease.descriptor,))


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="attempt-refusal-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lease = acquire_lease(self.root, "src.git")
        self.addCleanup(release_descriptor, self.lease)

    def attempt(self, **kwargs):
        kwargs.setdefault("repository", self.lease)
        return attempt_claim(
            RecordingExecutor(), GIT, target(), OBJECT_ID, REFERENCE, **kwargs
        )

    def test_every_forbidden_argument_is_refused_bare_and_with_a_value(self):
        for forbidden in FORBIDDEN_ATTEMPT_ARGUMENTS:
            for spelling in (forbidden, f"{forbidden}=x"):
                with self.subTest(argument=spelling):
                    with self.assertRaises(GitClaimAttemptError) as caught:
                        self.attempt(extra_arguments=(spelling,))
                    self.assertEqual(caught.exception.code, "forbidden-argument")
                    self.assertEqual(caught.exception.detail, forbidden)

    def test_a_second_refspec_is_refused(self):
        with self.assertRaises(GitClaimAttemptError) as caught:
            self.attempt(extra_arguments=(f"{OTHER_OBJECT_TEXT}:refs/heads/other",))
        self.assertEqual(caught.exception.code, "second-refspec")

    def test_a_reference_that_is_not_full_is_refused(self):
        for value in ("main", "heads/main", "ai-council/claims/58"):
            with self.subTest(reference=value):
                with self.assertRaises(GitClaimAttemptError) as caught:
                    build_attempt_arguments(OBJECT_ID, value)
                self.assertEqual(caught.exception.code, "reference-not-full")

    def test_a_colon_in_the_reference_is_refused(self):
        with self.assertRaises(GitClaimAttemptError) as caught:
            build_attempt_arguments(OBJECT_ID, "refs/heads/a:b")
        self.assertEqual(caught.exception.code, "colon-in-argument")
        self.assertEqual(caught.exception.field, "reference")

    def test_an_object_name_that_is_not_the_typed_one_is_refused(self):
        # Text is refused rather than parsed. `str()` on the wrong object yields
        # a repr, which Git accepts as far as reporting an unmatched refspec --
        # a confusing failure a long way from the mistake.
        class Impostor:
            wire_text = OBJECT_TEXT

        for value in (OBJECT_TEXT, None, 1, b"x", ["a"], Impostor()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(GitClaimAttemptError) as caught:
                    build_attempt_arguments(value, REFERENCE)
                self.assertEqual(caught.exception.code, "invalid-object-id")
                self.assertEqual(caught.exception.field, "object_id")

    def test_a_non_canonical_reference_is_refused(self):
        for value in ("", " ", " refs/heads/x", "refs/heads/x "):
            with self.subTest(value=repr(value)):
                with self.assertRaises(GitClaimAttemptError) as caught:
                    build_attempt_arguments(OBJECT_ID, value)
                self.assertIn(caught.exception.code, ("non-canonical", "invalid-type"))

    def test_a_non_text_reference_is_refused(self):
        for value in (None, 1, b"x", ["a"]):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(GitClaimAttemptError) as caught:
                    build_attempt_arguments(OBJECT_ID, value)
                self.assertEqual(caught.exception.code, "invalid-type")

    def test_extra_arguments_that_are_not_a_sequence_are_refused(self):
        for value in ("--porcelain", b"x", 3, None):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(GitClaimAttemptError) as caught:
                    self.attempt(extra_arguments=value)
                self.assertEqual(caught.exception.code, "invalid-arguments")

    def test_a_remote_given_as_a_name_is_refused_by_the_transport_module(self):
        # Delegated, not re-decided here: what a remote may be is one module's
        # rule, and a copy of it is what drifts.
        with self.assertRaises(GitTransportExecutionError) as caught:
            attempt_claim(
                RecordingExecutor(), GIT, "origin", OBJECT_ID, REFERENCE,
                repository=self.lease,
            )
        self.assertEqual(caught.exception.code, "invalid-target")

    def test_an_absent_repository_is_refused_by_this_module(self):
        # The transport module reads None as "needs no local objects", which is
        # true of ls-remote and false of every attempt, so the requirement is
        # this module's own rather than a borrowed one.
        with self.assertRaises(GitClaimAttemptError) as caught:
            attempt_claim(
                RecordingExecutor(), GIT, target(), OBJECT_ID, REFERENCE,
                repository=None,
            )
        self.assertEqual(caught.exception.code, "repository-required")

    def test_a_repository_that_is_not_a_lease_is_refused_by_the_transport_module(self):
        for value in (str(self.root), self.lease.selector, 3, object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(GitTransportExecutionError) as caught:
                    attempt_claim(
                        RecordingExecutor(), GIT, target(), OBJECT_ID, REFERENCE,
                        repository=value,
                    )
                self.assertEqual(caught.exception.code, "invalid-repository")

    def test_the_newer_repository_check_does_not_preempt_the_operand_refusals(self):
        # Both wrong at once: the narrower, older message is the one that fires.
        with self.assertRaises(GitClaimAttemptError) as caught:
            attempt_claim(
                RecordingExecutor(), GIT, target(), OBJECT_TEXT, REFERENCE,
                repository=None,
            )
        self.assertEqual(caught.exception.code, "invalid-object-id")

    def test_a_refused_attempt_never_reaches_the_executor(self):
        executor = RecordingExecutor()
        with self.assertRaises(GitClaimAttemptError):
            attempt_claim(
                executor, GIT, target(), OBJECT_ID, REFERENCE,
                repository=self.lease, extra_arguments=("--force",),
            )
        self.assertEqual(executor.commands, [])


class SingleAttemptTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="attempt-single-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lease = acquire_lease(self.root, "src.git")
        self.addCleanup(release_descriptor, self.lease)

    def test_exactly_one_command_is_run_whatever_the_outcome(self):
        cases = {
            "created": GitCommandResult(
                exit_status=0,
                stdout=porcelain("*", OBJECT_TEXT, REFERENCE, "[new branch]"),
            ),
            "rejected": GitCommandResult(
                exit_status=1,
                stdout=porcelain("!", OBJECT_TEXT, REFERENCE, "[rejected] (stale info)"),
            ),
            "unreadable": GitCommandResult(exit_status=128, stdout=b""),
            "transport": GitCommandResult(
                exit_status=None, local_failure="deadline-exceeded"
            ),
        }
        for name, result in cases.items():
            with self.subTest(case=name):
                executor = RecordingExecutor(result)
                attempt_claim(
                    executor, GIT, target(), OBJECT_ID, REFERENCE,
                    repository=self.lease,
                )
                self.assertEqual(len(executor.commands), 1)

    def test_there_is_no_retry_parameter(self):
        import inspect

        parameters = inspect.signature(attempt_claim).parameters
        for name in parameters:
            self.assertNotIn("retry", name)
            self.assertNotIn("attempts", name)


class OutcomeReconciliationTest(unittest.TestCase):
    """The status and the observation together, never either alone."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="attempt-outcome-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lease = acquire_lease(self.root, "src.git")
        self.addCleanup(release_descriptor, self.lease)

    def run_with(self, **kwargs):
        return attempt_claim(
            RecordingExecutor(GitCommandResult(**kwargs)),
            GIT, target(), OBJECT_ID, REFERENCE, repository=self.lease,
        )

    def test_exit_zero_alone_does_not_decide_between_two_answers(self):
        created = self.run_with(
            exit_status=0, stdout=porcelain("*", OBJECT_TEXT, REFERENCE, "[new branch]")
        )
        current = self.run_with(
            exit_status=0, stdout=porcelain("=", OBJECT_TEXT, REFERENCE, "[up to date]")
        )
        self.assertEqual(created.exit_status, current.exit_status)
        self.assertEqual(created.outcome, ATTEMPT_CREATED)
        self.assertEqual(current.outcome, ATTEMPT_ALREADY_CURRENT)

    def test_a_refusal_is_read_from_the_flag_whatever_the_summary(self):
        for summary in (
            "[rejected] (stale info)",
            "[remote rejected] (failed to update ref)",
        ):
            with self.subTest(summary=summary):
                outcome = self.run_with(
                    exit_status=1,
                    stdout=porcelain("!", OBJECT_TEXT, REFERENCE, summary),
                )
                self.assertEqual(outcome.outcome, ATTEMPT_REJECTED)
                self.assertEqual(outcome.observed_summary, summary)

    def test_a_status_that_disagrees_with_the_observation_is_its_own_answer(self):
        # Every server answer, each paired with a status it may not carry. The
        # disagreement is reported as itself, never resolved in favour of either
        # half: there is no reason to trust the status over the line or the line
        # over the status.
        cases = (
            ("created", "*", "[new branch]", 1),
            ("already-current", "=", "[up to date]", 1),
            ("rejected", "!", "[rejected] (stale info)", 0),
        )
        for observed, flag, summary, status in cases:
            with self.subTest(observed=observed, exit_status=status):
                outcome = self.run_with(
                    exit_status=status,
                    stdout=porcelain(flag, OBJECT_TEXT, REFERENCE, summary),
                )
                self.assertEqual(outcome.outcome, ATTEMPT_INCONSISTENT)
                self.assertEqual(outcome.detail, observed)

    def test_each_server_answer_permits_exactly_one_status(self):
        # Pins the table itself, so widening any entry fails here rather than
        # silently making a disagreement readable as agreement.
        self.assertEqual(
            {answer: sorted(statuses) for answer, statuses in _CONSISTENT_STATUS.items()},
            {"created": [0], "already-current": [0], "rejected": [1]},
        )

    def test_an_unreadable_observation_is_its_own_answer(self):
        outcome = self.run_with(exit_status=128, stdout=b"")
        self.assertEqual(outcome.outcome, ATTEMPT_UNREADABLE)
        self.assertEqual(outcome.detail, "empty-output")

    def test_a_local_failure_is_its_own_answer_and_is_read_first(self):
        # Read before the observation: a run the deadline killed may still have
        # written a status line, and that line is not a server decision.
        outcome = self.run_with(
            exit_status=None,
            stdout=porcelain("*", OBJECT_TEXT, REFERENCE, "[new branch]"),
            local_failure="deadline-exceeded",
        )
        self.assertEqual(outcome.outcome, ATTEMPT_TRANSPORT_FAILED)
        self.assertEqual(outcome.detail, "deadline-exceeded")

    def test_a_status_line_for_another_reference_is_not_read_as_this_one(self):
        outcome = self.run_with(
            exit_status=0,
            stdout=porcelain("*", OBJECT_TEXT, "refs/heads/other", "[new branch]"),
        )
        self.assertEqual(outcome.outcome, ATTEMPT_UNREADABLE)
        self.assertEqual(outcome.detail, "reference-not-reported")

    def test_truncated_output_is_unreadable_rather_than_a_decision(self):
        outcome = self.run_with(
            exit_status=0,
            stdout=porcelain(
                "*", OBJECT_TEXT, REFERENCE, "[new branch]", trailer=False
            ),
        )
        self.assertEqual(outcome.outcome, ATTEMPT_UNREADABLE)
        self.assertEqual(outcome.detail, "missing-trailer")

    def test_the_outcome_refuses_truthiness(self):
        outcome = self.run_with(
            exit_status=0, stdout=porcelain("*", OBJECT_TEXT, REFERENCE, "[new branch]")
        )
        self.assertIsInstance(outcome, ClaimAttemptOutcome)
        with self.assertRaises(TypeError):
            bool(outcome)

    def test_the_diagnostics_arrive_already_redacted(self):
        outcome = self.run_with(
            exit_status=1,
            stderr=b"fatal: could not read from https://user:secret@h/x\n",
            stdout=porcelain("!", OBJECT_TEXT, REFERENCE, "[rejected] (stale info)"),
        )
        self.assertNotIn("secret", outcome.diagnostics)
        self.assertIn("[redacted]", outcome.diagnostics)


class RealRepositoryTest(unittest.TestCase):
    """Every outcome driven against a real server decision."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="attempt-real-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.remote = self.root / "remote.git"
        subprocess.run(
            [GIT, "init", "--bare", "-q", str(self.remote)],
            check=True, env=dict(BASE_CHILD_ENVIRONMENT),
        )
        self.target = validate_remote_url(f"file://{self.remote}")
        self.executor = GitSubprocessExecutor()
        self.lease = acquire_lease(self.root, "claim.git")
        self.addCleanup(release_descriptor, self.lease)
        # The object comes from the merged materialization module, not by hand:
        # this is the object the protocol actually pushes.
        self.object_id = materialize_claim_object(REQUEST, self.lease, self.executor)

    def attempt(self, object_id=None, repository=None):
        return attempt_claim(
            self.executor, GIT, self.target,
            object_id or self.object_id, REFERENCE,
            repository=repository or self.lease,
        )

    def remote_refs(self):
        listed = subprocess.run(
            [GIT, "ls-remote", self.target.url],
            capture_output=True, check=True, env=dict(BASE_CHILD_ENVIRONMENT),
        )
        return listed.stdout.decode()

    def test_the_first_attempt_creates_the_reference(self):
        outcome = self.attempt()
        self.assertEqual(outcome.outcome, ATTEMPT_CREATED)
        self.assertEqual(outcome.exit_status, 0)
        # Read back from the server rather than inferred from the outcome.
        self.assertIn(f"{self.object_id.wire_text}\t{REFERENCE}", self.remote_refs())

    def test_the_same_object_again_is_already_current_not_created(self):
        self.assertEqual(self.attempt().outcome, ATTEMPT_CREATED)
        second = self.attempt()
        self.assertEqual(second.outcome, ATTEMPT_ALREADY_CURRENT)
        self.assertEqual(second.exit_status, 0)

    def test_a_different_object_is_refused_and_the_reference_is_unchanged(self):
        self.assertEqual(self.attempt().outcome, ATTEMPT_CREATED)
        other_lease = acquire_lease(self.root, "other.git")
        self.addCleanup(release_descriptor, other_lease)
        other = materialize_claim_object(
            ClaimRequest(
                repository=REQUEST.repository,
                issue_number=REQUEST.issue_number,
                contract_sha256=REQUEST.contract_sha256,
                holder="session-b",
                issued_at=REQUEST.issued_at,
            ),
            other_lease,
            self.executor,
        )
        self.assertNotEqual(other, self.object_id)
        refused = self.attempt(object_id=other, repository=other_lease)
        self.assertEqual(refused.outcome, ATTEMPT_REJECTED)
        self.assertEqual(refused.exit_status, 1)
        self.assertIn(f"{self.object_id.wire_text}\t{REFERENCE}", self.remote_refs())

    def test_a_missing_remote_is_unreadable_rather_than_a_refusal(self):
        absent = validate_remote_url(f"file://{self.root / 'nowhere.git'}")
        outcome = attempt_claim(
            self.executor, GIT, absent, self.object_id, REFERENCE,
            repository=self.lease,
        )
        self.assertEqual(outcome.outcome, ATTEMPT_UNREADABLE)
        self.assertEqual(outcome.exit_status, 128)

    def test_an_object_absent_from_the_bound_repository_is_unreadable(self):
        # Measured: the server reports this local fault with the REFUSING flag
        # and `[remote rejected] (unpacker error)`, and writes no trailer. Under
        # #151's rule the flag is honoured whatever summary follows it, so the
        # trailer is the only thing between a local fault and a false report
        # that another session holds the claim.
        empty = acquire_lease(self.root, "empty.git")
        self.addCleanup(release_descriptor, empty)
        subprocess.run(
            [GIT, f"--git-dir={empty.selector}", "init", "--bare", "-q",
             "--object-format=sha1"],
            check=True, pass_fds=(empty.descriptor,),
            env=dict(BASE_CHILD_ENVIRONMENT),
        )
        outcome = self.attempt(repository=empty)
        self.assertEqual(outcome.outcome, ATTEMPT_UNREADABLE)
        self.assertEqual(outcome.detail, "missing-trailer")
        self.assertNotEqual(outcome.outcome, ATTEMPT_REJECTED)
        self.assertEqual(self.remote_refs().strip(), "")
