"""Execute one already-built Git command, and report what happened.

Both repository runners assemble a complete :class:`GitCommand` and hand it to an
injected executor.  Nothing implemented that protocol, so a bound command could
be built but never run, and the only executors in the tree were test fakes.

This module supplies one.  Its whole job is to start a child faithfully and
describe the outcome without embellishment, so the interesting content is which
of the process record's :data:`EXECUTOR_OBLIGATIONS` it discharges and *how*:

* ``preserve-descriptor-numbers``, ``clear-cloexec-for-allowlist-only`` and
  ``close-other-non-standard-descriptors`` are discharged by the spawning
  primitive.  ``pass_fds`` keeps each listed descriptor at its original number
  and clears close-on-exec on it even when the parent set that flag, and
  ``close_fds`` closes every other non-standard descriptor.  Measured on this
  platform rather than assumed: a listed descriptor answers ``F_GETFD`` in the
  child at the same number, and an unlisted inheritable one raises ``EBADF``.

  Two details of that primitive are worth recording, because both make a test
  read stronger than it is.  A non-empty ``pass_fds`` forces ``close_fds`` on
  regardless of what was asked for, so passing it explicitly changes the outcome
  only when the allowlist is empty -- which is exactly when nothing else would
  close anything.  And Python sets close-on-exec on descriptors it creates
  (PEP 446), so a descriptor left at that default is closed at exec whether or
  not this executor asks for it; only an explicitly inheritable one measures the
  closure.
* ``never-alias-standard-streams`` is **not** discharged that way, and this is
  the one obligation that needs code here.  ``GitCommand`` validates
  ``inherited_descriptors`` only for type, uniqueness and non-negativity, so a
  standard stream number passes it.  The spawning primitive then accepts that
  number without complaint while the standard streams are separately redirected
  onto pipes.  A ``--git-dir=/proc/self/fd/<n>`` selector built from such a
  descriptor names whichever stream won, and a selector resolving to the wrong
  object returns bytes that look like an answer instead of an error.  So a
  command listing a standard stream is refused here, before anything is spawned.

Refusal and local failure are deliberately different channels.  A command that
cannot be described faithfully is a caller error and raises, exactly as the
runners raise on the inputs they refuse.  A command that was well-formed but
could not be started produces a result carrying ``local_failure`` and **no**
exit status, because a failure to launch is not something the command reported.
The two are never combined: a caller reading an exit status is always reading
one a child actually returned.

What this node does not do, stated so nothing over-reads it:

* **Bounded in time, not in volume.**  Every execution is bounded by one total
  deadline covering the whole call.  What is *not* bounded is how much a child
  writes: a deadline limits how long it may write, never how fast, so a child
  that floods its pipes inside the deadline is a resource concern this module
  does not answer.

  Expiry terminates the whole spawned group rather than the immediate child,
  and that is a requirement rather than tidiness.  A bound operation runs inside
  a borrow of the repository lease, and cleanup must refuse while a borrow is
  outstanding, because removing a leased tree while a bound child is live breaks
  that child and every later bound command -- measured for the pinned Git build
  in ``design/git-fd-binding-spike.md`` Row 6.  Git starts helpers, so a helper
  that survives its parent keeps descriptors open and keeps writing.  Signalling
  only the process the caller knows about would leave a descendant holding the
  very borrow that cleanup is waiting on, and one stuck command would become a
  lease that can never be reclaimed.

  The child is therefore placed in its own session at spawn, and the group is
  confirmed to be led by the child immediately before it is signalled: a group
  the child does not lead is not this command's group, and signalling it would
  reach unrelated processes.
* **No working directory policy.**  The child inherits the caller's, which is
  harmless for the runners because they always pass an explicit repository
  binding, but it is not a guarantee this module makes.
* **No interpretation.**  An exit status is reported, never judged, and
  :class:`GitCommandResult` refuses truthiness precisely so a caller cannot skip
  looking at it.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
from typing import Any

from council_tools.git_process_contract import (
    EXECUTOR_OBLIGATIONS,
    GitCommand,
    GitCommandResult,
)

#: The default total bound on one execution, in seconds.  It is a default and
#: not a policy: a caller with a slower or faster expectation supplies its own.
DEFAULT_DEADLINE_SECONDS: float = 30.0

#: How long the post-termination drain may take before it is abandoned.
#: Draining is a courtesy, not a requirement -- the output of an expired command
#: is discarded either way -- so it must never be able to outlast the deadline it
#: is cleaning up after.  See :meth:`GitSubprocessExecutor.execute`.
TERMINATION_DRAIN_SECONDS: float = 2.0

#: The descriptors the executor redirects itself.  A command may not also claim
#: one as inherited; see the module docstring for why that is refused and not
#: merely discouraged.
STANDARD_STREAM_DESCRIPTORS: frozenset[int] = frozenset({0, 1, 2})


class GitExecutorError(ValueError):
    """A stable, field-addressed refusal raised before anything is spawned."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"git executor {code} at {field}")


def _require_command(command: Any) -> GitCommand:
    # Membership, not duck typing: an object that merely exposes ``argv`` and
    # ``environment`` has passed none of the record's validation.
    if type(command) is not GitCommand:
        raise GitExecutorError("not-a-command", "command")
    for index, descriptor in enumerate(command.inherited_descriptors):
        if descriptor in STANDARD_STREAM_DESCRIPTORS:
            raise GitExecutorError(
                "standard-stream-descriptor",
                f"command.inherited_descriptors[{index}]",
            )
    return command


class GitSubprocessExecutor:
    """Run a :class:`GitCommand` in a child process and report the outcome.

    Satisfies the ``GitExecutor`` protocol.  The deadline is held on the
    instance rather than passed to :meth:`execute`, so the protocol's single
    argument is unchanged and the bound is fixed before any command is seen.
    """

    #: Recorded so a reader can see which obligations this class answers for.
    obligations: tuple[str, ...] = EXECUTOR_OBLIGATIONS

    def __init__(self, *, deadline_seconds: float = DEFAULT_DEADLINE_SECONDS):
        # ``bool`` is an ``int`` subclass and is not a duration; ``type`` rather
        # than ``isinstance`` keeps it out.
        if type(deadline_seconds) not in (int, float):
            raise GitExecutorError("invalid-deadline", "deadline_seconds")
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise GitExecutorError("invalid-deadline", "deadline_seconds")
        self.deadline_seconds = float(deadline_seconds)

    @staticmethod
    def _terminate_group(child: subprocess.Popen) -> None:
        """Signal the whole group, but only if the child leads it.

        ``start_new_session`` makes the child a session and group leader, so the
        expected case is that its group id equals its pid.  If that does not
        hold, the group is not this command's to signal and only the child
        itself is killed: reaching unrelated processes would be a worse failure
        than leaving a descendant behind.
        """

        try:
            group = os.getpgid(child.pid)
        except OSError:
            # Already reaped or gone; there is nothing left to signal.
            return
        if group == child.pid:
            try:
                os.killpg(group, signal.SIGKILL)
                return
            except OSError:
                pass
        try:
            child.kill()
        except OSError:
            pass

    def execute(self, command: GitCommand) -> GitCommandResult:
        """Execute ``command`` and return what the child produced.

        Raises :class:`GitExecutorError` for a command that cannot be executed
        faithfully.  Returns a result carrying ``local_failure`` and no exit
        status when a well-formed command could not be started, or when it did
        not finish within the deadline.
        """

        checked = _require_command(command)
        try:
            child = subprocess.Popen(
                [checked.executable, *checked.argv],
                executable=checked.executable,
                env=dict(checked.environment),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # The listed descriptors survive at their own numbers; every
                # other non-standard descriptor the parent holds is closed.
                pass_fds=tuple(checked.inherited_descriptors),
                close_fds=True,
                # Makes the child a session leader, so it and everything it
                # starts share one group that can be signalled as a unit.
                start_new_session=True,
            )
        except OSError as exc:
            # Nothing was started, so there is nothing to reap and no status to
            # report.  ``errno`` names the failure without quoting a path back.
            return GitCommandResult(
                exit_status=None,
                local_failure=f"start-failed:{exc.errno}",
            )

        # ``Popen`` as a context manager closes the three capture descriptors and
        # waits for the child, so neither leaks on the exception path either.
        with child:
            try:
                stdout, stderr = child.communicate(
                    input=checked.stdin, timeout=self.deadline_seconds
                )
            except subprocess.TimeoutExpired:
                # The timeout does not stop anything by itself: the child is
                # still running, and so is whatever it started.
                self._terminate_group(child)
                # Drain and reap, but under a bound.  When the group was
                # killed nothing is left holding the capture pipes, so this
                # returns at once.  When it was *not* -- the fallback below
                # kills only the child -- a surviving descendant still holds the
                # write ends open, and an unbounded drain would wait for an EOF
                # that never comes, turning a deadline into a permanent hang.
                try:
                    child.communicate(timeout=TERMINATION_DRAIN_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    # Whatever still holds those pipes is not this command's to
                    # wait for.  The context manager closes them and reaps the
                    # child.
                    pass
                # A child killed on expiry has a status describing the signal.
                # Reporting it would let a caller read a timeout as something
                # the command returned, so it is discarded.
                return GitCommandResult(
                    exit_status=None,
                    local_failure="deadline-expired",
                )

        return GitCommandResult(
            exit_status=child.returncode,
            stdout=stdout,
            stderr=stderr,
            local_failure=None,
        )


__all__ = [
    "DEFAULT_DEADLINE_SECONDS",
    "TERMINATION_DRAIN_SECONDS",
    "STANDARD_STREAM_DESCRIPTORS",
    "GitExecutorError",
    "GitSubprocessExecutor",
]
