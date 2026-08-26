"""Run a remote Git operation only through the frozen transport identity policy.

:mod:`council_tools.git_transport_identity` decides *what* a remote child may be
given and *which* remotes it may reach.
:mod:`council_tools.git_process_executor` runs an already-complete command.
Nothing joined them, so a caller assembled the command — and each part of that
assembly can drop a guarantee without looking wrong at the call site:

* build the environment from the policy and then **add a key afterwards**, which
  is indistinguishable from asking the policy for it and is how a proxy or a
  credential-helper variable gets in;
* validate a URL and then **pass a different string** as the argument, because
  validation returns a target while the command takes text and nothing checks
  they are the same;
* place the remote **where validation never looked**, because Git accepts a
  remote in more than one argument position.

Each produces a child running with the policy apparently applied and one of its
guarantees missing, which is worse than no policy at all, because the call site
reads as protected.

So this module takes a validated :class:`~council_tools.git_transport_identity.RemoteTarget`
rather than text, builds the environment only through the policy, and places the
remote at one known position it controls.  There is no parameter for extra
environment keys beyond the ones the policy permits, and none for arguments that
could name a second remote.

**The isolation is proven against a real child, not asserted.**  The base
environment is built from scratch, and the descriptor-binding spike measured that
global, system and XDG configuration must be suppressed *together*.  That was
measured against local commands by the spike rather than by the suite, and an
isolation claim nobody has reproduced against a real child is a comment.  The
tests plant a hostile configuration and export an agent socket, a credential
helper and an askpass hook, then assert through one observation channel that the
child reports **neither** the hostile identity **nor** anything ambient, while
still reporting a value the policy explicitly permitted — so a test that observed
nothing at all could not pass.

This module spawns nothing itself: every command goes through the executor, whose
deadline and process-group termination are inherited unchanged.  It contacts no
network host, obtains no credential, and reads no credential material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from council_tools.git_process_contract import (
    GitCommand,
    GitCommandResult,
    GitExecutor,
    RenderedInvocation,
)
from council_tools.git_transport_diagnostics import redact_transport_diagnostics
from council_tools.git_transport_identity import (
    RemoteTarget,
    transport_child_environment,
)

#: Where the remote goes in the vector.  It is the first subcommand argument, and
#: it is placed here rather than by a caller so validation cannot be bypassed by
#: putting a second remote somewhere else.
REMOTE_ARGUMENT_INDEX = 0

#: Arguments a caller may not supply, because each would introduce a second
#: remote, redirect the first, or reintroduce configuration the policy suppressed.
#:
#: This is a prefix set: ``--upload-pack=...`` is refused by its ``--upload-pack``
#: prefix, and a bare ``-c`` is refused because the value follows separately.
FORBIDDEN_ARGUMENT_PREFIXES: tuple[str, ...] = (
    "--upload-pack",
    "--receive-pack",
    "--exec",
    "-c",
    "--config-env",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--upload-archive",
)


class GitTransportExecutionError(ValueError):
    """A stable, field-addressed remote-execution failure."""

    def __init__(self, code: str, field: str, detail: str = ""):
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(
            f"git transport execution {code} at {field}"
            + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class RemoteOperationResult:
    """What a remote operation produced, with its output already made safe."""

    exit_status: int | None
    stdout: str
    stderr: str
    local_failure: str | None = None

    def __bool__(self) -> bool:
        # Same refusal the underlying result makes, and for the same reason: a
        # result read by truthiness is one read without looking at the status.
        raise TypeError("a remote operation result is not a success signal")


def build_remote_command(
    executable: str,
    target: Any,
    subcommand: Any,
    arguments: Any = (),
    *,
    environment: Mapping[str, str] | None = None,
    global_options: Any = (),
) -> GitCommand:
    """Build the command for one remote operation.

    ``target`` must be a validated :class:`RemoteTarget`; text is refused, so the
    string placed in the vector is the string that was validated.
    """

    if not isinstance(target, RemoteTarget):
        raise GitTransportExecutionError("invalid-target", "target")
    if not isinstance(subcommand, str) or not subcommand or subcommand != subcommand.strip():
        raise GitTransportExecutionError("invalid-subcommand", "subcommand")

    checked_arguments = _checked_arguments(arguments, "arguments")
    checked_globals = _checked_arguments(global_options, "global_options")

    for index, argument in enumerate(checked_arguments):
        if argument == target.url:
            raise GitTransportExecutionError(
                "argument-repeats-remote", f"arguments[{index}]"
            )

    child_environment = transport_child_environment(environment)
    invocation = RenderedInvocation(
        global_options=checked_globals,
        subcommand=subcommand,
        subcommand_args=(target.url, *checked_arguments),
    )
    return GitCommand(
        executable=executable,
        invocation=invocation,
        environment=child_environment,
    )


def _checked_arguments(arguments: Any, field: str) -> tuple[str, ...]:
    if isinstance(arguments, (str, bytes)) or not isinstance(arguments, (tuple, list)):
        raise GitTransportExecutionError("invalid-arguments", field)
    checked: list[str] = []
    for index, argument in enumerate(arguments):
        if not isinstance(argument, str):
            raise GitTransportExecutionError("invalid-argument", f"{field}[{index}]")
        for prefix in FORBIDDEN_ARGUMENT_PREFIXES:
            if argument == prefix or argument.startswith(f"{prefix}="):
                raise GitTransportExecutionError(
                    "forbidden-argument", f"{field}[{index}]", prefix
                )
        checked.append(argument)
    return tuple(checked)


def run_remote_operation(
    executor: GitExecutor,
    executable: str,
    target: Any,
    subcommand: Any,
    arguments: Any = (),
    *,
    environment: Mapping[str, str] | None = None,
    global_options: Any = (),
) -> RemoteOperationResult:
    """Build the command, run it through ``executor``, and make its output safe.

    The deadline and the process-group termination belong to the executor and are
    inherited unchanged; nothing is spawned here.
    """

    command = build_remote_command(
        executable,
        target,
        subcommand,
        arguments,
        environment=environment,
        global_options=global_options,
    )
    result = executor.execute(command)
    if not isinstance(result, GitCommandResult):
        raise GitTransportExecutionError("invalid-result", "executor")
    return RemoteOperationResult(
        exit_status=result.exit_status,
        # A remote chooses these bytes, so they are bounded and redacted before
        # anyone can record or print them.
        stdout=redact_transport_diagnostics(result.stdout),
        stderr=redact_transport_diagnostics(result.stderr),
        local_failure=result.local_failure,
    )
