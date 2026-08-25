"""Composition proofs for the repository-bound write runner.

A recording executor captures the command it is handed, so the assertions are
about the exact command that would have been spawned rather than about the
runner's intentions.  Every refusal is proved to happen *before* the executor is
called, because a refusal after spawning is not a refusal.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from council_tools.git_local_read_operations import ReadBlobBytes
from council_tools.git_local_write_operations import (
    CreateClaimTree,
    InitializeBareRepository,
    WriteCanonicalBlob,
)
from council_tools.git_object_id import Sha1ObjectId
from council_tools.git_process_contract import GitCommand, GitCommandResult
from council_tools.git_repository_lease import (
    GitRepositoryLeaseError,
    acquire_lease,
)
from council_tools.git_repository_write_runner import (
    GIT_DIR_OPTION,
    GitWriteRunnerError,
    run_write_operation,
)

OID = Sha1ObjectId("5005007fe89e082c994a45a1d527cfc136c35cc9")


class RecordingExecutor:
    """Captures the command and reports whether it was ever called."""

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


class RunnerTestCase(unittest.TestCase):
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


class BindingTests(RunnerTestCase):
    def test_the_binding_is_present_and_in_the_git_global_region(self):
        executor = RecordingExecutor()
        run_write_operation(InitializeBareRepository(), self.lease, executor)
        command = executor.commands[0]
        expected = GIT_DIR_OPTION.format(selector=self.lease.selector)
        self.assertEqual(command.invocation.global_options[0], expected)
        self.assertNotIn(expected, command.invocation.subcommand_args)

    def test_the_binding_precedes_the_subcommand_in_the_vector(self):
        executor = RecordingExecutor()
        run_write_operation(InitializeBareRepository(), self.lease, executor)
        argv = executor.commands[0].argv
        expected = GIT_DIR_OPTION.format(selector=self.lease.selector)
        self.assertLess(argv.index(expected), argv.index("init"))

    def test_the_binding_names_the_descriptor_not_a_path(self):
        # A selector naming a path would follow a name an attacker can replace.
        executor = RecordingExecutor()
        run_write_operation(InitializeBareRepository(), self.lease, executor)
        selector = executor.commands[0].invocation.global_options[0]
        self.assertIn("/proc/self/fd/", selector)
        self.assertNotIn(str(self.root), selector)

    def test_the_operation_vector_is_preserved_after_the_binding(self):
        executor = RecordingExecutor()
        run_write_operation(WriteCanonicalBlob(b"payload"), self.lease, executor)
        argv = executor.commands[0].argv
        self.assertEqual(argv[1:], ("hash-object", "-w", "--stdin", "--"))

    def test_stdin_and_identity_survive_composition(self):
        executor = RecordingExecutor()
        run_write_operation(CreateClaimTree(OID), self.lease, executor)
        command = executor.commands[0]
        self.assertEqual(command.stdin, CreateClaimTree(OID).entry_line())

    def test_only_the_lease_descriptor_is_inherited(self):
        executor = RecordingExecutor()
        run_write_operation(InitializeBareRepository(), self.lease, executor)
        self.assertEqual(
            executor.commands[0].inherited_descriptors, (self.lease.descriptor,)
        )

    def test_the_environment_is_the_suppressed_child_environment(self):
        executor = RecordingExecutor()
        run_write_operation(InitializeBareRepository(), self.lease, executor)
        environment = executor.commands[0].environment
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(environment["HOME"], "/nonexistent")

    def test_the_result_is_returned_unchanged(self):
        expected = GitCommandResult(exit_status=0, stdout=b"abc")
        executor = RecordingExecutor(result=expected)
        self.assertIs(
            run_write_operation(InitializeBareRepository(), self.lease, executor), expected
        )


class RefusalBeforeSpawnTests(RunnerTestCase):
    def assert_refused_before_spawn(self, code, *args, **kwargs):
        executor = kwargs.pop("executor", None) or RecordingExecutor()
        with self.assertRaises(GitWriteRunnerError) as caught:
            run_write_operation(*args, executor, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(executor.called, "the executor must not have been called")

    def test_a_raw_command_is_refused(self):
        raw = GitCommand(
            executable="/usr/bin/git",
            invocation=InitializeBareRepository().render(),
            environment={},
        )
        self.assert_refused_before_spawn("not-a-write-operation", raw, self.lease)

    def test_a_raw_argument_vector_is_refused(self):
        self.assert_refused_before_spawn(
            "not-a-write-operation", ("init", "--bare"), self.lease
        )

    def test_a_read_family_operation_is_refused(self):
        # The closed command set only means something if the family is checked.
        self.assert_refused_before_spawn(
            "not-a-write-operation", ReadBlobBytes(OID), self.lease
        )

    def test_a_duck_typed_object_with_render_is_refused(self):
        class Impostor:
            def render(self):  # pragma: no cover - must never be called
                raise AssertionError("render must not run")

        self.assert_refused_before_spawn("not-a-write-operation", Impostor(), self.lease)

    def test_a_non_lease_value_is_refused(self):
        for value in (None, self.lease.path, self.lease.descriptor, object()):
            with self.subTest(value=repr(value)[:40]):
                self.assert_refused_before_spawn(
                    "not-a-lease", InitializeBareRepository(), value
                )

    def test_a_non_executor_is_refused(self):
        with self.assertRaises(GitWriteRunnerError) as caught:
            run_write_operation(InitializeBareRepository(), self.lease, object())
        self.assertEqual(caught.exception.code, "not-an-executor")

    def test_a_drifted_lease_identity_is_refused_before_spawn(self):
        import dataclasses

        executor = RecordingExecutor()
        drifted = dataclasses.replace(
            self.lease,
            identity=dataclasses.replace(
                self.lease.identity, inode=self.lease.identity.inode + 1
            ),
        )
        with self.assertRaises(GitRepositoryLeaseError):
            run_write_operation(InitializeBareRepository(), drifted, executor)
        self.assertFalse(executor.called)


class BorrowLifetimeTests(RunnerTestCase):
    def test_the_borrow_is_held_across_the_executor_call(self):
        seen = []

        class Observer(RecordingExecutor):
            def execute(inner, command):  # noqa: N805 - inner self
                seen.append(self.lease.borrow_count)
                return super().execute(command)

        run_write_operation(InitializeBareRepository(), self.lease, Observer())
        self.assertEqual(seen, [1], "the lease must be borrowed while executing")

    def test_the_borrow_is_released_on_the_returning_path(self):
        run_write_operation(InitializeBareRepository(), self.lease, RecordingExecutor())
        self.assertEqual(self.lease.borrow_count, 0)

    def test_the_borrow_is_released_when_the_executor_raises(self):
        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            run_write_operation(
                InitializeBareRepository(), self.lease, RecordingExecutor(raises=Boom())
            )
        self.assertEqual(self.lease.borrow_count, 0)


class ErrorContractTests(RunnerTestCase):
    def test_errors_are_typed_with_stable_codes_and_fields(self):
        self.assertTrue(issubclass(GitWriteRunnerError, ValueError))
        with self.assertRaises(GitWriteRunnerError) as caught:
            run_write_operation(None, self.lease, RecordingExecutor())
        self.assertEqual(caught.exception.code, "not-a-write-operation")
        self.assertEqual(caught.exception.field, "operation")

    def test_the_runner_spawns_nothing_itself(self):
        import council_tools.git_repository_write_runner as module

        for name in ("subprocess", "Popen", "os"):
            with self.subTest(name=name):
                self.assertNotIn(name, vars(module))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
