"""Compose a write operation, a lease, and an executor into one bound command.

Every piece needed to run a claim write exists separately and nothing composes
them, so a caller would assemble the repository binding by hand at each call
site.  That assembly is exactly where the guarantees are lost:

* a caller can pass a selector naming a **path** rather than the leased
  descriptor, and the binding then follows a name an attacker can replace;
* a caller can forget to **revalidate**, and the operation runs against a
  descriptor whose identity has already drifted;
* a caller can hold no **borrow**, and a concurrent removal breaks the operation
  in flight -- which the spike measured as a hard failure, not an untidiness;
* a caller can pass a **raw command**, and the closed set of commands the
  operation builders were written to guarantee becomes unbounded again.

This runner removes each of those by accepting only branded write operations and
a live lease, deriving the binding internally, and refusing everything else
*before* a process is spawned.

It does not spawn.  The executor is injected and conforms to the
``GitExecutor`` protocol; implementing one that actually starts a process is a
separate outcome.  This node's job is to make sure that whatever executes is a
command it built.
"""

from __future__ import annotations

from typing import Any

from council_tools.git_local_write_operations import WRITE_OPERATIONS
from council_tools.git_process_contract import (
    GitCommand,
    GitCommandResult,
    GitProcessPolicy,
    RenderedInvocation,
)
from council_tools.git_repository_lease import BareRepositoryLease

#: The binding goes in the Git-global region, ahead of the subcommand, because
#: that is the only position Git reads it from.
GIT_DIR_OPTION = "--git-dir={selector}"

DEFAULT_GIT_EXECUTABLE = "/usr/bin/git"


class GitWriteRunnerError(ValueError):
    """A stable, field-addressed composition refusal."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"git write runner {code} at {field}")


def _require_write_operation(operation: Any) -> Any:
    # Membership in the branded family, not duck typing: an object that merely
    # has ``render`` could render anything at all.
    if type(operation) not in WRITE_OPERATIONS:
        raise GitWriteRunnerError("not-a-write-operation", "operation")
    return operation


def _require_lease(lease: Any) -> BareRepositoryLease:
    if type(lease) is not BareRepositoryLease:
        raise GitWriteRunnerError("not-a-lease", "lease")
    return lease


def run_write_operation(
    operation: Any,
    lease: Any,
    executor: Any,
    *,
    policy: GitProcessPolicy | None = None,
    executable: str = DEFAULT_GIT_EXECUTABLE,
) -> GitCommandResult:
    """Bind ``operation`` to ``lease`` and hand the result to ``executor``.

    Every refusal below happens before the executor is called, so a rejected
    input never reaches a process.
    """

    _require_write_operation(operation)
    bound_lease = _require_lease(lease)
    if not callable(getattr(executor, "execute", None)):
        raise GitWriteRunnerError("not-an-executor", "executor")

    active_policy = policy if policy is not None else GitProcessPolicy()
    if type(active_policy) is not GitProcessPolicy:
        raise GitWriteRunnerError("not-a-policy", "policy")

    # The borrow revalidates identity as it is taken and is held until the
    # executor returns, so a concurrent close or removal cannot undercut the
    # operation while it runs.
    with bound_lease.borrow():
        rendered = operation.render()
        if type(rendered) is not RenderedInvocation:
            raise GitWriteRunnerError("not-a-rendered-invocation", "operation")

        command = GitCommand(
            executable=executable,
            invocation=RenderedInvocation(
                global_options=(
                    GIT_DIR_OPTION.format(selector=bound_lease.selector),
                    *rendered.global_options,
                ),
                subcommand=rendered.subcommand,
                subcommand_args=rendered.subcommand_args,
                stdin=rendered.stdin,
                identity=dict(rendered.identity),
            ),
            environment=active_policy.child_environment(dict(rendered.identity)),
            # Only the lease descriptor: nothing else the parent holds open
            # should reach the child.
            inherited_descriptors=(bound_lease.descriptor,),
        )
        return executor.execute(command)


__all__ = [
    "DEFAULT_GIT_EXECUTABLE",
    "GIT_DIR_OPTION",
    "GitWriteRunnerError",
    "run_write_operation",
]
