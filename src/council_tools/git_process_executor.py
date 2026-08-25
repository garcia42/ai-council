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

* **No time bound.**  A child that blocks does so for as long as it likes, and
  this executor waits.  Deadlines, process-group confinement, group termination
  and output bounding are the separate outcome on issue 54, and until that lands
  this executor must not be relied on anywhere a child could block without end.
* **No working directory policy.**  The child inherits the caller's, which is
  harmless for the runners because they always pass an explicit repository
  binding, but it is not a guarantee this module makes.
* **No interpretation.**  An exit status is reported, never judged, and
  :class:`GitCommandResult` refuses truthiness precisely so a caller cannot skip
  looking at it.
"""

from __future__ import annotations

import subprocess
from typing import Any

from council_tools.git_process_contract import (
    EXECUTOR_OBLIGATIONS,
    GitCommand,
    GitCommandResult,
)

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

    Satisfies the ``GitExecutor`` protocol.  Holds no state between calls, so a
    single instance may serve any number of commands.
    """

    #: Recorded so a reader can see which obligations this class answers for.
    obligations: tuple[str, ...] = EXECUTOR_OBLIGATIONS

    def execute(self, command: GitCommand) -> GitCommandResult:
        """Execute ``command`` and return what the child produced.

        Raises :class:`GitExecutorError` for a command that cannot be executed
        faithfully.  Returns a result carrying ``local_failure`` and no exit
        status when a well-formed command could not be started.
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
            stdout, stderr = child.communicate(input=checked.stdin)

        return GitCommandResult(
            exit_status=child.returncode,
            stdout=stdout,
            stderr=stderr,
            local_failure=None,
        )


__all__ = [
    "STANDARD_STREAM_DESCRIPTORS",
    "GitExecutorError",
    "GitSubprocessExecutor",
]
