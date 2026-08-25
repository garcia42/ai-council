"""Proof that the executor discharges its obligations, measured not asserted."""

from __future__ import annotations

import errno
import fcntl
import os
import sys
import unittest

from council_tools.git_process_contract import (
    GitCommand,
    GitCommandResult,
    RenderedInvocation,
)
from council_tools.git_process_executor import (
    STANDARD_STREAM_DESCRIPTORS,
    GitExecutorError,
    GitSubprocessExecutor,
)


def _python_command(source: str, *, descriptors: tuple[int, ...] = (), stdin: bytes = b"") -> GitCommand:
    """A command that runs ``source`` under this interpreter.

    The executor is indifferent to which binary it starts, so the tests use one
    that can report on its own descriptors rather than a Git build whose answer
    would have to be inferred from its output.
    """

    return GitCommand(
        executable=sys.executable,
        invocation=RenderedInvocation(
            global_options=("-c", source),
            subcommand="--",
            subcommand_args=(),
            stdin=stdin,
            identity={},
        ),
        environment={"LC_ALL": "C"},
        inherited_descriptors=descriptors,
    )


class StandardStreamRefusalTests(unittest.TestCase):
    def test_every_standard_stream_number_is_refused(self) -> None:
        for descriptor in sorted(STANDARD_STREAM_DESCRIPTORS):
            with self.subTest(descriptor=descriptor):
                command = _python_command("pass", descriptors=(descriptor,))
                with self.assertRaises(GitExecutorError) as caught:
                    GitSubprocessExecutor().execute(command)
                self.assertEqual(caught.exception.code, "standard-stream-descriptor")
                self.assertEqual(
                    caught.exception.field, "command.inherited_descriptors[0]"
                )

    def test_refusal_happens_before_anything_is_spawned(self) -> None:
        # A nonexistent executable would produce a local failure if it were ever
        # reached, so a refusal here proves nothing was started.
        command = GitCommand(
            executable="/nonexistent/git",
            invocation=RenderedInvocation(subcommand="status"),
            environment={},
            inherited_descriptors=(2,),
        )
        with self.assertRaises(GitExecutorError) as caught:
            GitSubprocessExecutor().execute(command)
        self.assertEqual(caught.exception.code, "standard-stream-descriptor")

    def test_a_non_command_is_refused(self) -> None:
        with self.assertRaises(GitExecutorError) as caught:
            GitSubprocessExecutor().execute(object())
        self.assertEqual(caught.exception.code, "not-a-command")


class DescriptorInheritanceTests(unittest.TestCase):
    """Measure the child's descriptors instead of trusting the primitive."""

    SOURCE = (
        "import fcntl,os,sys\n"
        "def alive(fd):\n"
        "    try:\n"
        "        fcntl.fcntl(fd, fcntl.F_GETFD)\n"
        "        return True\n"
        "    except OSError:\n"
        "        return False\n"
        "listed, unlisted = (int(x) for x in os.environ['GIT_AUTHOR_NAME'].split(','))\n"
        "sys.stdout.write('%s,%s' % (alive(listed), alive(unlisted)))\n"
    )

    def _run_with_two_descriptors(self) -> tuple[GitCommandResult, int, int]:
        listed = os.open(os.devnull, os.O_RDONLY)
        unlisted = os.open(os.devnull, os.O_RDONLY)
        # Close-on-exec is set here deliberately: the primitive must clear it for
        # the listed descriptor, and a test that left it clear would prove nothing.
        os.set_inheritable(listed, False)
        # And the unlisted one is made explicitly inheritable, because Python
        # sets close-on-exec on descriptors it creates (PEP 446). Left at the
        # default it would be closed at exec whatever the executor did, and the
        # assertion below would hold against an executor that closed nothing.
        os.set_inheritable(unlisted, True)
        try:
            command = GitCommand(
                executable=sys.executable,
                invocation=RenderedInvocation(
                    global_options=("-c", self.SOURCE),
                    subcommand="--",
                    identity={},
                ),
                environment={"GIT_AUTHOR_NAME": f"{listed},{unlisted}"},
                inherited_descriptors=(listed,),
            )
            return GitSubprocessExecutor().execute(command), listed, unlisted
        finally:
            os.close(listed)
            os.close(unlisted)

    def test_listed_descriptor_survives_and_unlisted_one_is_closed(self) -> None:
        result, listed, unlisted = self._run_with_two_descriptors()
        self.assertEqual(result.exit_status, 0, result.stderr)
        self.assertEqual(
            result.stdout.decode(),
            "True,False",
            f"listed fd {listed} must survive at its own number; "
            f"unlisted fd {unlisted} must be closed",
        )

    def test_an_inheritable_descriptor_is_closed_when_nothing_is_listed(self) -> None:
        """The empty-allowlist case, where ``close_fds`` is the only defence.

        A non-empty ``pass_fds`` forces ``close_fds`` on by itself, so this is
        the one path where passing it explicitly changes the outcome.
        """

        leaked = os.open(os.devnull, os.O_RDONLY)
        os.set_inheritable(leaked, True)
        try:
            source = (
                "import fcntl,os,sys\n"
                "fd = int(os.environ['GIT_AUTHOR_NAME'])\n"
                "try:\n"
                "    fcntl.fcntl(fd, fcntl.F_GETFD)\n"
                "    sys.stdout.write('open')\n"
                "except OSError:\n"
                "    sys.stdout.write('closed')\n"
            )
            command = GitCommand(
                executable=sys.executable,
                invocation=RenderedInvocation(
                    global_options=("-c", source), subcommand="--", identity={}
                ),
                environment={"GIT_AUTHOR_NAME": str(leaked)},
                inherited_descriptors=(),
            )
            result = GitSubprocessExecutor().execute(command)
        finally:
            os.close(leaked)
        self.assertEqual(result.exit_status, 0, result.stderr)
        self.assertEqual(result.stdout, b"closed")

    def test_the_capture_pipes_do_not_alias_an_inherited_descriptor(self) -> None:
        # The child reports which of its own descriptors are open; the standard
        # streams must be the pipes this executor created, never the lease.
        result, listed, _ = self._run_with_two_descriptors()
        self.assertNotIn(listed, STANDARD_STREAM_DESCRIPTORS)
        self.assertEqual(result.exit_status, 0)


class OutcomeReportingTests(unittest.TestCase):
    def test_a_completed_command_reports_status_and_both_streams(self) -> None:
        source = (
            "import sys\n"
            "sys.stdout.write('out')\n"
            "sys.stderr.write('err')\n"
            "sys.exit(7)\n"
        )
        result = GitSubprocessExecutor().execute(_python_command(source))
        self.assertEqual(result.exit_status, 7)
        self.assertEqual(result.stdout, b"out")
        self.assertEqual(result.stderr, b"err")
        self.assertIsNone(result.local_failure)

    def test_recorded_stdin_bytes_reach_the_child(self) -> None:
        source = "import sys\nsys.stdout.write(sys.stdin.read())\n"
        payload = b"100644 blob 0123456789abcdef\tclaim.json\n"
        result = GitSubprocessExecutor().execute(
            _python_command(source, stdin=payload)
        )
        self.assertEqual(result.exit_status, 0, result.stderr)
        self.assertEqual(result.stdout, payload)

    def test_a_start_failure_reports_no_exit_status(self) -> None:
        command = GitCommand(
            executable="/nonexistent/git",
            invocation=RenderedInvocation(subcommand="status"),
            environment={},
        )
        result = GitSubprocessExecutor().execute(command)
        self.assertIsNone(result.exit_status)
        self.assertEqual(result.local_failure, f"start-failed:{errno.ENOENT}")
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_a_local_failure_and_an_exit_status_never_appear_together(self) -> None:
        started = GitSubprocessExecutor().execute(_python_command("pass"))
        failed = GitSubprocessExecutor().execute(
            GitCommand(
                executable="/nonexistent/git",
                invocation=RenderedInvocation(subcommand="status"),
                environment={},
            )
        )
        for result in (started, failed):
            with self.subTest(result=result.local_failure):
                self.assertNotEqual(
                    result.exit_status is None, result.local_failure is None
                )

    def test_a_result_is_not_a_success_signal(self) -> None:
        result = GitSubprocessExecutor().execute(_python_command("pass"))
        with self.assertRaises(TypeError):
            bool(result)


class LeakTests(unittest.TestCase):
    def _open_descriptors(self) -> set[int]:
        found = set()
        for entry in os.listdir("/proc/self/fd"):
            descriptor = int(entry)
            try:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            except OSError:
                continue
            found.add(descriptor)
        return found

    def test_no_capture_descriptor_is_left_open_by_any_outcome(self) -> None:
        executor = GitSubprocessExecutor()
        executor.execute(_python_command("pass"))
        before = self._open_descriptors()
        for _ in range(5):
            executor.execute(_python_command("import sys; sys.stderr.write('x')"))
            executor.execute(
                GitCommand(
                    executable="/nonexistent/git",
                    invocation=RenderedInvocation(subcommand="status"),
                    environment={},
                )
            )
        self.assertEqual(self._open_descriptors() - before, set())

    def _zombie_children(self) -> list[int]:
        """Zombie children of *this* process only.

        Deliberately not ``waitpid(-1, WNOHANG)``: that reports on any child at
        all, so an unrelated subprocess left by another test would decide this
        one. Scoping the check to our own parent pid keeps it a measurement of
        this executor rather than of what else happens to be running.
        """

        zombies = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8") as handle:
                    stat = handle.read()
            except OSError:
                continue
            # The command field may contain spaces, so parse after its closing
            # parenthesis: the next two fields are state and parent pid.
            fields = stat[stat.rindex(")") + 2 :].split()
            if fields[0] == "Z" and int(fields[1]) == os.getpid():
                zombies.append(int(entry))
        return zombies

    def test_no_child_is_left_unreaped(self) -> None:
        executor = GitSubprocessExecutor()
        for _ in range(5):
            executor.execute(_python_command("pass"))
        self.assertEqual(self._zombie_children(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
