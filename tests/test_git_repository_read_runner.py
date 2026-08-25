"""Composition proofs for the repository-bound verification-read runner.

A read that runs against the wrong repository returns bytes that look like an
answer, so these assertions are about the exact command that would have been
spawned, and about the *position* of both defences rather than their presence.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path

from council_tools.git_local_read_operations import (
    NO_REPLACE_OBJECTS,
    SELECTOR_READS,
    ListTree,
    ObserveObjectFormat,
    ReadBlobBytes,
    ReadCommitBytes,
    ReadObjectType,
)
from council_tools.git_local_write_operations import InitializeBareRepository
from council_tools.git_object_id import Sha1ObjectId
from council_tools.git_process_contract import (
    GitCommand,
    GitCommandResult,
    RenderedInvocation,
)
from council_tools.git_repository_lease import GitRepositoryLeaseError, acquire_lease
from council_tools.git_repository_read_runner import (
    GIT_DIR_OPTION,
    GitReadRunnerError,
    run_read_operation,
)

OID = Sha1ObjectId("5005007fe89e082c994a45a1d527cfc136c35cc9")


class RecordingExecutor:
    def __init__(self, result=None, raises=None):
        self.commands: list[GitCommand] = []
        self._result = result if result is not None else GitCommandResult(exit_status=0)
        self._raises = raises

    def execute(self, command: GitCommand) -> GitCommandResult:
        self.commands.append(command)
        if self._raises is not None:
            raise self._raises
        return self._result

    @property
    def called(self) -> bool:
        return bool(self.commands)


class ReadRunnerTestCase(unittest.TestCase):
    def setUp(self):
        if not Path("/proc/self/fd").is_dir():  # pragma: no cover - environment guard
            self.skipTest("procfs is required")
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.lease = acquire_lease(self.root, "claim.git")
        self.addCleanup(self._close_quietly)

    def _close_quietly(self):
        try:
            os.close(self.lease.descriptor)
        except OSError:
            pass


class BindingTests(ReadRunnerTestCase):
    def test_the_binding_leads_the_git_global_region(self):
        executor = RecordingExecutor()
        run_read_operation(ReadBlobBytes(OID), self.lease, executor)
        options = executor.commands[0].invocation.global_options
        self.assertEqual(options[0], GIT_DIR_OPTION.format(selector=self.lease.selector))

    def test_both_defences_stay_ahead_of_the_subcommand(self):
        # Position is the whole point for each of them: the binding and the
        # replacement defence are read only from the Git-global region.
        executor = RecordingExecutor()
        run_read_operation(ReadBlobBytes(OID), self.lease, executor)
        command = executor.commands[0]
        argv = command.argv
        subcommand_at = argv.index(command.invocation.subcommand)
        self.assertLess(argv.index(GIT_DIR_OPTION.format(selector=self.lease.selector)), subcommand_at)
        self.assertLess(argv.index(NO_REPLACE_OBJECTS), subcommand_at)

    def test_the_binding_names_the_descriptor_not_a_path(self):
        executor = RecordingExecutor()
        run_read_operation(ReadBlobBytes(OID), self.lease, executor)
        selector = executor.commands[0].invocation.global_options[0]
        self.assertIn("/proc/self/fd/", selector)
        self.assertNotIn(str(self.root), selector)

    def test_every_selector_read_carries_the_replacement_defence(self):
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                executor = RecordingExecutor()
                run_read_operation(builder(OID), self.lease, executor)
                self.assertIn(
                    NO_REPLACE_OBJECTS, executor.commands[0].invocation.global_options
                )

    def test_the_operation_vector_is_preserved_after_the_binding(self):
        executor = RecordingExecutor()
        run_read_operation(ListTree(OID), self.lease, executor)
        self.assertEqual(
            executor.commands[0].invocation.subcommand_args, ("-z", OID.wire_text, "--")
        )

    def test_only_the_lease_descriptor_is_inherited(self):
        executor = RecordingExecutor()
        run_read_operation(ReadCommitBytes(OID), self.lease, executor)
        self.assertEqual(
            executor.commands[0].inherited_descriptors, (self.lease.descriptor,)
        )

    def test_the_object_format_read_is_bound_but_carries_no_replacement_flag(self):
        # It reads no object, so it neither needs the flag nor is expected to
        # carry one; the runner must not demand it.
        executor = RecordingExecutor()
        run_read_operation(ObserveObjectFormat(), self.lease, executor)
        options = executor.commands[0].invocation.global_options
        self.assertEqual(options[0], GIT_DIR_OPTION.format(selector=self.lease.selector))
        self.assertNotIn(NO_REPLACE_OBJECTS, options)

    def test_the_result_is_returned_unchanged(self):
        expected = GitCommandResult(exit_status=0, stdout=b"blob")
        self.assertIs(
            run_read_operation(ReadBlobBytes(OID), self.lease, RecordingExecutor(result=expected)),
            expected,
        )


class ReplacementDefenceAssertionTests(ReadRunnerTestCase):
    def test_an_object_read_that_lost_the_defence_is_refused_before_spawn(self):
        # Proves the assertion is live rather than decorative: the defence is
        # checked, not assumed to have survived composition.
        class Stripped(ReadBlobBytes):
            def render(self):
                original = super().render()
                return dataclasses.replace(original, global_options=())

        executor = RecordingExecutor()
        with self.assertRaises(GitReadRunnerError) as caught:
            run_read_operation(Stripped(OID), self.lease, executor)
        # A subclass is not in the branded family, so it is refused even earlier.
        self.assertIn(
            caught.exception.code, {"not-a-read-operation", "missing-replacement-defence"}
        )
        self.assertFalse(executor.called)

    def test_the_assertion_helper_rejects_a_stripped_invocation(self):
        from council_tools.git_repository_read_runner import _assert_replacement_defence

        stripped = RenderedInvocation(subcommand="cat-file", subcommand_args=("blob",))
        with self.assertRaises(GitReadRunnerError) as caught:
            _assert_replacement_defence(ReadBlobBytes(OID), stripped)
        self.assertEqual(caught.exception.code, "missing-replacement-defence")

    def test_the_assertion_helper_exempts_the_selector_free_read(self):
        from council_tools.git_repository_read_runner import _assert_replacement_defence

        _assert_replacement_defence(
            ObserveObjectFormat(), ObserveObjectFormat().render()
        )


class RefusalBeforeSpawnTests(ReadRunnerTestCase):
    def assert_refused(self, code, operation, lease):
        executor = RecordingExecutor()
        with self.assertRaises(GitReadRunnerError) as caught:
            run_read_operation(operation, lease, executor)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(executor.called, "the executor must not have been called")

    def test_a_write_family_operation_is_refused(self):
        self.assert_refused("not-a-read-operation", InitializeBareRepository(), self.lease)

    def test_a_raw_command_is_refused(self):
        raw = GitCommand(
            executable="/usr/bin/git",
            invocation=ReadBlobBytes(OID).render(),
            environment={},
        )
        self.assert_refused("not-a-read-operation", raw, self.lease)

    def test_a_raw_argument_vector_is_refused(self):
        self.assert_refused("not-a-read-operation", ("cat-file", "blob"), self.lease)

    def test_a_non_lease_value_is_refused(self):
        for value in (None, self.lease.path, self.lease.descriptor, object()):
            with self.subTest(value=repr(value)[:40]):
                self.assert_refused("not-a-lease", ReadBlobBytes(OID), value)

    def test_a_non_executor_is_refused(self):
        with self.assertRaises(GitReadRunnerError) as caught:
            run_read_operation(ReadBlobBytes(OID), self.lease, object())
        self.assertEqual(caught.exception.code, "not-an-executor")

    def test_a_drifted_lease_is_refused_before_spawn(self):
        executor = RecordingExecutor()
        drifted = dataclasses.replace(
            self.lease,
            identity=dataclasses.replace(
                self.lease.identity, inode=self.lease.identity.inode + 1
            ),
        )
        with self.assertRaises(GitRepositoryLeaseError):
            run_read_operation(ReadBlobBytes(OID), drifted, executor)
        self.assertFalse(executor.called)


class BorrowLifetimeTests(ReadRunnerTestCase):
    def test_the_borrow_is_held_across_the_executor_call(self):
        seen = []

        class Observer(RecordingExecutor):
            def execute(inner, command):  # noqa: N805
                seen.append(self.lease.borrow_count)
                return super().execute(command)

        run_read_operation(ReadObjectType(OID), self.lease, Observer())
        self.assertEqual(seen, [1])

    def test_the_borrow_is_released_on_both_paths(self):
        run_read_operation(ReadObjectType(OID), self.lease, RecordingExecutor())
        self.assertEqual(self.lease.borrow_count, 0)

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            run_read_operation(
                ReadObjectType(OID), self.lease, RecordingExecutor(raises=Boom())
            )
        self.assertEqual(self.lease.borrow_count, 0)


class ErrorContractTests(ReadRunnerTestCase):
    def test_errors_are_typed_with_stable_codes_and_fields(self):
        self.assertTrue(issubclass(GitReadRunnerError, ValueError))
        with self.assertRaises(GitReadRunnerError) as caught:
            run_read_operation(None, self.lease, RecordingExecutor())
        self.assertEqual(caught.exception.code, "not-a-read-operation")
        self.assertEqual(caught.exception.field, "operation")

    def test_the_runner_parses_nothing_and_spawns_nothing(self):
        import council_tools.git_repository_read_runner as module

        for name in ("subprocess", "Popen", "os", "json"):
            with self.subTest(name=name):
                self.assertNotIn(name, vars(module))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
