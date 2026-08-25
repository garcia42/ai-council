"""Proof that the claim write sequence is ordered, checked, and typed.

The executor is a recording fake rather than a real process: what this module
adds is the *sequence*, so what has to be observed is the order of commands and
what flowed between them. A real Git child is exercised in the end-to-end test
at the bottom, which is where "these commands actually work" belongs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from council_tools.git_claim_materialization import (
    MATERIALIZATION_STEPS,
    STEP_BLOB,
    STEP_COMMIT,
    STEP_INITIALIZE,
    STEP_TREE,
    ClaimMaterializationError,
    materialize_claim_object,
)
from council_tools.git_claim_request import ClaimRequest
from council_tools.git_process_contract import GitCommand, GitCommandResult
from council_tools.git_process_executor import GitSubprocessExecutor
from council_tools.git_object_id import Sha1ObjectId
from council_tools.git_repository_lease import acquire_lease, remove_lease

REQUEST = ClaimRequest(
    repository="garcia42/ai-council",
    issue_number=55,
    contract_sha256="a" * 64,
    holder="session-a",
    issued_at="@0 +0000",
)

# Letters, not just digits: an all-digit identifier is equal to its own
# uppercase form, which would make the lowercase-rejection case vacuous.
BLOB_ID = "a1" * 20
TREE_ID = "b2" * 20
COMMIT_ID = "c3" * 20


class RecordingExecutor:
    """Records every command and replies with scripted output."""

    def __init__(self, outputs=None, failures=None):
        self.commands: list[GitCommand] = []
        self._outputs = outputs or [b"", BLOB_ID + "\n", TREE_ID + "\n", COMMIT_ID + "\n"]
        self._failures = failures or {}

    def execute(self, command: GitCommand) -> GitCommandResult:
        index = len(self.commands)
        self.commands.append(command)
        if index in self._failures:
            return self._failures[index]
        out = self._outputs[index]
        return GitCommandResult(
            exit_status=0,
            stdout=out.encode() if isinstance(out, str) else out,
        )

    def subcommands(self) -> list[str]:
        return [c.invocation.subcommand for c in self.commands]


def _lease():
    root = tempfile.mkdtemp(prefix="claim-mat-")
    lease = acquire_lease(root, "claim.git")
    return lease, root


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.lease, self.root = _lease()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self._drop)

    def _drop(self):
        try:
            remove_lease(self.lease)
        except Exception:
            pass

    def test_the_four_writes_happen_in_order(self) -> None:
        executor = RecordingExecutor()
        materialize_claim_object(REQUEST, self.lease, executor)
        self.assertEqual(
            executor.subcommands(), ["init", "hash-object", "mktree", "commit-tree"]
        )
        self.assertEqual(len(MATERIALIZATION_STEPS), 4)

    def test_each_step_output_becomes_the_next_step_input(self) -> None:
        executor = RecordingExecutor()
        result = materialize_claim_object(REQUEST, self.lease, executor)
        blob_cmd, tree_cmd, commit_cmd = executor.commands[1:]
        # The blob's content is the request's payload, not a caller parameter.
        self.assertEqual(blob_cmd.stdin, REQUEST.payload_bytes())
        # The tree entry names the blob the previous step returned.
        self.assertIn(BLOB_ID.encode(), tree_cmd.stdin)
        # The commit names the tree the previous step returned.
        self.assertIn(TREE_ID, commit_cmd.argv)
        self.assertNotIn(BLOB_ID, commit_cmd.argv)
        self.assertEqual(commit_cmd.stdin, REQUEST.message_bytes())
        self.assertEqual(result, Sha1ObjectId(COMMIT_ID))

    def test_the_commit_carries_the_requests_identity(self) -> None:
        executor = RecordingExecutor()
        materialize_claim_object(REQUEST, self.lease, executor)
        environment = executor.commands[3].environment
        self.assertEqual(environment["GIT_AUTHOR_NAME"], REQUEST.holder)
        self.assertEqual(environment["GIT_AUTHOR_DATE"], REQUEST.issued_at)
        self.assertEqual(environment["GIT_COMMITTER_DATE"], REQUEST.issued_at)

    def test_the_same_request_materializes_to_the_same_identifier(self) -> None:
        first = materialize_claim_object(REQUEST, self.lease, RecordingExecutor())
        second = materialize_claim_object(REQUEST, self.lease, RecordingExecutor())
        self.assertEqual(first, second)


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.lease, self.root = _lease()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_local_failure_at_any_step_is_refused_by_name(self) -> None:
        for index, step in enumerate(MATERIALIZATION_STEPS):
            with self.subTest(step=step):
                executor = RecordingExecutor(
                    failures={index: GitCommandResult(
                        exit_status=None, local_failure="deadline-expired")}
                )
                with self.assertRaises(ClaimMaterializationError) as caught:
                    materialize_claim_object(REQUEST, self.lease, executor)
                self.assertEqual(caught.exception.code, "local-failure")
                self.assertEqual(caught.exception.step, step)
                # Nothing after the failing step ran.
                self.assertEqual(len(executor.commands), index + 1)

    def test_a_nonzero_exit_at_any_step_is_refused_by_name(self) -> None:
        for index, step in enumerate(MATERIALIZATION_STEPS):
            with self.subTest(step=step):
                executor = RecordingExecutor(
                    failures={index: GitCommandResult(exit_status=128, stderr=b"fatal")}
                )
                with self.assertRaises(ClaimMaterializationError) as caught:
                    materialize_claim_object(REQUEST, self.lease, executor)
                self.assertEqual(caught.exception.code, "nonzero-exit")
                self.assertEqual(caught.exception.step, step)

    def test_the_two_failure_channels_are_reported_distinctly(self) -> None:
        # A command that never ran and one that ran and failed are different
        # problems; collapsing them would lose which happened.
        local = RecordingExecutor(
            failures={1: GitCommandResult(exit_status=None, local_failure="start-failed:2")})
        exited = RecordingExecutor(
            failures={1: GitCommandResult(exit_status=1)})
        codes = []
        for executor in (local, exited):
            with self.assertRaises(ClaimMaterializationError) as caught:
                materialize_claim_object(REQUEST, self.lease, executor)
            codes.append(caught.exception.code)
        self.assertEqual(codes, ["local-failure", "nonzero-exit"])

    def test_malformed_output_is_refused_rather_than_forwarded(self) -> None:
        for bad in ("", "not-an-oid\n", "z" * 40 + "\n", BLOB_ID.upper() + "\n",
                    BLOB_ID + "\nsecond\n", BLOB_ID[:39] + "\n",
                    # Git writes exactly "<oid>\n". These are what separate
                    # removing that one newline from strip()ing whatever
                    # arrived: strip() would accept all three.
                    " " + BLOB_ID + "\n", BLOB_ID + " \n", BLOB_ID + "\n\n"):
            with self.subTest(output=bad[:16]):
                executor = RecordingExecutor(
                    outputs=[b"", bad, TREE_ID + "\n", COMMIT_ID + "\n"])
                with self.assertRaises(ClaimMaterializationError) as caught:
                    materialize_claim_object(REQUEST, self.lease, executor)
                self.assertEqual(caught.exception.code, "malformed-output")
                self.assertEqual(caught.exception.step, STEP_BLOB)

    def test_non_ascii_output_is_refused(self) -> None:
        executor = RecordingExecutor(
            outputs=[b"", "café\n".encode(), TREE_ID + "\n", COMMIT_ID + "\n"])
        with self.assertRaises(ClaimMaterializationError) as caught:
            materialize_claim_object(REQUEST, self.lease, executor)
        self.assertEqual(caught.exception.code, "malformed-output")


class ArgumentRefusalTests(unittest.TestCase):
    def setUp(self):
        self.lease, self.root = _lease()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_wrong_typed_arguments_are_refused_before_anything_runs(self) -> None:
        executor = RecordingExecutor()
        cases = [
            (("not-a-request", self.lease, executor), "not-a-claim-request"),
            ((REQUEST, "not-a-lease", executor), "not-a-lease"),
            ((REQUEST, self.lease, object()), "not-an-executor"),
        ]
        for args, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ClaimMaterializationError) as caught:
                    materialize_claim_object(*args)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(executor.commands, [], "nothing may run on a refused call")


@unittest.skipUnless(
    os.path.isfile("/usr/bin/git") and os.path.isdir("/proc/self/fd"),
    "needs Linux with procfs and git",
)
class EndToEndTests(unittest.TestCase):
    """One real repository, real Git children, through the real executor."""

    def test_a_claim_materializes_to_a_readable_commit(self) -> None:
        root = tempfile.mkdtemp(prefix="claim-mat-e2e-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        lease = acquire_lease(root, "claim.git")
        try:
            commit = materialize_claim_object(
                REQUEST, lease, GitSubprocessExecutor(deadline_seconds=30)
            )
            self.assertIsInstance(commit, Sha1ObjectId)
            repository = os.path.join(root, "claim.git")
            shown = subprocess.run(
                ["/usr/bin/git", f"--git-dir={repository}", "--no-replace-objects",
                 "cat-file", "commit", commit.wire_text],
                capture_output=True,
            )
            self.assertEqual(shown.returncode, 0, shown.stderr.decode())
            self.assertIn(b"claim garcia42/ai-council#55", shown.stdout)
            self.assertIn(b"session-a", shown.stdout)
            self.assertNotIn(b"parent ", shown.stdout)

            payload = subprocess.run(
                ["/usr/bin/git", f"--git-dir={repository}", "--no-replace-objects",
                 "cat-file", "blob", f"{commit.wire_text}:claim.json"],
                capture_output=True,
            )
            self.assertEqual(payload.returncode, 0, payload.stderr.decode())
            self.assertEqual(payload.stdout, REQUEST.payload_bytes())
        finally:
            remove_lease(lease)

    def test_the_same_request_yields_the_same_commit_in_a_fresh_repository(self) -> None:
        """Determinism end to end, which is what a compare-and-set will rest on."""

        names = []
        for index in range(2):
            root = tempfile.mkdtemp(prefix=f"claim-mat-det{index}-")
            self.addCleanup(shutil.rmtree, root, ignore_errors=True)
            lease = acquire_lease(root, "claim.git")
            try:
                names.append(
                    materialize_claim_object(
                        REQUEST, lease, GitSubprocessExecutor(deadline_seconds=30)
                    )
                )
            finally:
                remove_lease(lease)
        self.assertEqual(names[0], names[1])


class NonOwnershipTests(unittest.TestCase):
    def test_no_ref_command_is_ever_issued(self) -> None:
        lease, root = _lease()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        executor = RecordingExecutor()
        materialize_claim_object(REQUEST, lease, executor)
        for command in executor.commands:
            self.assertNotIn(command.invocation.subcommand,
                             {"update-ref", "push", "symbolic-ref", "fetch"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
