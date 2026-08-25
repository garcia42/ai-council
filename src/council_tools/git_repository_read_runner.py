"""Compose a read operation, a lease, and an executor into one bound command.

Verification reads need the same composition the writes need, and for a sharper
reason: a read that runs against the wrong repository returns bytes that *look
like an answer*.  Every guarantee the read builders establish is therefore only
as good as the binding that carries them to a process.

The builders already put the replacement-object defence in the Git-global
position and accept only typed object selectors.  But a caller assembling the
binding by hand can still name a path instead of the leased descriptor, skip
revalidation, hold no borrow, or pass a raw command carrying none of that.

Because this node renders internally, it can also **assert** that what it is
about to execute still carries those defences rather than trusting that nothing
downstream removed them.  That check is cheap and it is the difference between a
defence that is present and a defence that is merely intended.

It does not spawn.  The executor is injected through the ``GitExecutor``
protocol; implementing one is a separate outcome.
"""

from __future__ import annotations

from typing import Any

from council_tools.git_local_read_operations import (
    NO_REPLACE_OBJECTS,
    READ_OPERATIONS,
    SELECTOR_READS,
)
from council_tools.git_process_contract import (
    GitCommand,
    GitCommandResult,
    GitProcessPolicy,
    RenderedInvocation,
)
from council_tools.git_repository_lease import BareRepositoryLease

GIT_DIR_OPTION = "--git-dir={selector}"
DEFAULT_GIT_EXECUTABLE = "/usr/bin/git"


class GitReadRunnerError(ValueError):
    """A stable, field-addressed composition refusal."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"git read runner {code} at {field}")


def _require_read_operation(operation: Any) -> Any:
    if type(operation) not in READ_OPERATIONS:
        raise GitReadRunnerError("not-a-read-operation", "operation")
    return operation


def _require_lease(lease: Any) -> BareRepositoryLease:
    if type(lease) is not BareRepositoryLease:
        raise GitReadRunnerError("not-a-lease", "lease")
    return lease


def _assert_replacement_defence(operation: Any, rendered: RenderedInvocation) -> None:
    """Refuse to execute an object read that lost its replacement defence.

    Only the selector-taking reads read an object; the object-format query does
    not, so it neither needs the flag nor is expected to carry one.
    """

    if type(operation) not in SELECTOR_READS:
        return
    if NO_REPLACE_OBJECTS not in rendered.global_options:
        raise GitReadRunnerError("missing-replacement-defence", "operation")


def run_read_operation(
    operation: Any,
    lease: Any,
    executor: Any,
    *,
    policy: GitProcessPolicy | None = None,
    executable: str = DEFAULT_GIT_EXECUTABLE,
) -> GitCommandResult:
    """Bind ``operation`` to ``lease`` and hand the result to ``executor``."""

    _require_read_operation(operation)
    bound_lease = _require_lease(lease)
    if not callable(getattr(executor, "execute", None)):
        raise GitReadRunnerError("not-an-executor", "executor")

    active_policy = policy if policy is not None else GitProcessPolicy()
    if type(active_policy) is not GitProcessPolicy:
        raise GitReadRunnerError("not-a-policy", "policy")

    with bound_lease.borrow():
        rendered = operation.render()
        if type(rendered) is not RenderedInvocation:
            raise GitReadRunnerError("not-a-rendered-invocation", "operation")
        _assert_replacement_defence(operation, rendered)

        command = GitCommand(
            executable=executable,
            invocation=RenderedInvocation(
                # The binding leads, and the operation's own Git-global options
                # -- including the replacement defence -- follow it.  Both stay
                # ahead of the subcommand, which is the only position Git reads
                # either from.
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
            inherited_descriptors=(bound_lease.descriptor,),
        )
        return executor.execute(command)


__all__ = [
    "DEFAULT_GIT_EXECUTABLE",
    "GIT_DIR_OPTION",
    "GitReadRunnerError",
    "run_read_operation",
]
