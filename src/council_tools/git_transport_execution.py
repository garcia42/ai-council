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

**A repository binding is inserted here or it does not exist.**  A remote
operation that sends objects needs a local repository, and a caller cannot supply
one: ``--git-dir`` is refused above, for the reason that a caller-supplied
binding re-points the operation at a repository the identity policy never
validated.  Refusing it left no sanctioned way to bind one at all, so the module
could run only the subset of Git needing no local objects -- which is the subset
every test of it happened to use.  It now takes an optional
:class:`~council_tools.git_repository_lease.BareRepositoryLease`, the same handle
both local runners take, and inserts the binding itself.  The caller refusal is
unchanged and stays the primary control.

Two measured facts decide how that binding is placed, on the pinned ``git
2.39.5``:

* **Git resolves the last ``--git-dir``, not the first.**  ``--git-dir=A
  --git-dir=B`` reads B and ``--git-dir=B --git-dir=A`` reads A.  So the binding
  goes **last** in the Git-global region, where nothing a caller supplied can
  override it.  Both local runners insert theirs *first*; that position is
  correct only because the refusal in front of it is total, and it is not the
  position that would win on its own.
* **The selector and the descriptor travel together.**
  ``--git-dir=/proc/self/fd/N`` without ``N`` in
  :attr:`~council_tools.git_process_contract.GitCommand.inherited_descriptors`
  gives ``fatal: not a git repository: '/proc/self/fd/3'``, so passing the
  selector alone binds nothing and a test that passes it alone measures nothing.

The lease is **borrowed for the whole operation**, as the local runners borrow
theirs: identity is revalidated as the borrow is taken, and cleanup waits on the
borrow count, so the repository cannot be removed from under a running child.

**The two output streams are not the same kind of thing, and are not treated the
same way.**  Standard error carries prose a remote chose.  It reaches a log or a
report unexamined, so it is bounded and redacted by
:func:`~council_tools.git_transport_diagnostics.redact_transport_diagnostics`
exactly as before.  Standard output, for the operations this module exists to
run, is **protocol data**.  Redaction strips control characters -- written as a
keep-list on purpose, so it strips TAB -- and TAB is what a push status line is
separated by.  Measured::

    in  : "*\t<oid>:refs/heads/x\t[new branch]"
    out : "*<oid>:refs/heads/x[new branch]"

:func:`~council_tools.git_claim_observation.observe_claim_push` splits on TAB and
requires exactly three fields, so redacting that stream made **every** claim
attempt come back ``unparseable-line``.  Removing the separators does not make
the stream safer; the parser already treats it as hostile, bounding it in lines
and bytes before parsing, requiring its trailer, refusing a diagnostic offered as
a status line, ignoring a status line for a reference nobody asked about, and
refusing two lines for the one requested.  Removing them only makes every answer
unreadable, and an unreadable answer about who holds a claim is the failure the
apparatus exists to avoid.

**So a caller that records standard output is recording bytes a remote chose,
and bounding and redacting them at the point of record is that caller's
obligation** -- this module no longer does it for that stream.  It is still
decoded with ``errors="replace"``, for the reason the diagnostics module uses the
same policy: undecodable bytes are a thing a remote can send, and failing on them
would let it suppress its own answer.  How much a child may produce at all is a
separate question, and it is issue #126 rather than this module's.

This module spawns nothing itself: every command goes through the executor, whose
deadline and process-group termination are inherited unchanged.  It contacts no
network host, obtains no credential, and reads no credential material.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Mapping

from council_tools.git_process_contract import (
    GitCommand,
    GitCommandResult,
    GitExecutor,
    RenderedInvocation,
)
from council_tools.git_repository_lease import BareRepositoryLease
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

#: How a repository binding is spelled.  The two local runners each define this
#: identically; a test pins all three together so a change to one cannot
#: silently diverge from the others.
GIT_DIR_OPTION = "--git-dir={selector}"


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
    """What a remote operation produced.

    ``stderr`` is bounded and redacted.  ``stdout`` is **not**: it is the
    child's bytes decoded and otherwise untouched, because for the operations
    this module runs it is protocol data whose separators redaction destroys.
    Recording it safely is the caller's obligation; see the module docstring.
    """

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
    repository: Any = None,
) -> GitCommand:
    """Build the command for one remote operation.

    ``target`` must be a validated :class:`RemoteTarget`; text is refused, so the
    string placed in the vector is the string that was validated.

    ``repository`` is an optional lease on a local repository.  Omitting it
    builds exactly the vector this built before the option existed, so an
    operation needing no local objects is unchanged.
    """

    if not isinstance(target, RemoteTarget):
        raise GitTransportExecutionError("invalid-target", "target")
    if not isinstance(subcommand, str) or not subcommand or subcommand != subcommand.strip():
        raise GitTransportExecutionError("invalid-subcommand", "subcommand")

    lease = _checked_repository(repository)
    checked_arguments = _checked_arguments(arguments, "arguments")
    checked_globals = _checked_arguments(global_options, "global_options")

    for index, argument in enumerate(checked_arguments):
        if argument == target.url:
            raise GitTransportExecutionError(
                "argument-repeats-remote", f"arguments[{index}]"
            )

    # Last, not first: Git resolves the last ``--git-dir`` in the vector, so a
    # binding placed ahead of a caller-supplied one would be overridden by it.
    # The caller refusal above already makes that unreachable; this is the
    # position that would still hold if it were not.
    bound_globals = checked_globals
    descriptors: tuple[int, ...] = ()
    if lease is not None:
        bound_globals = (*checked_globals, GIT_DIR_OPTION.format(selector=lease.selector))
        # Without the descriptor the selector resolves to nothing in the child.
        descriptors = (lease.descriptor,)

    child_environment = transport_child_environment(environment)
    invocation = RenderedInvocation(
        global_options=bound_globals,
        subcommand=subcommand,
        subcommand_args=(target.url, *checked_arguments),
    )
    return GitCommand(
        executable=executable,
        invocation=invocation,
        environment=child_environment,
        inherited_descriptors=descriptors,
    )


def _checked_repository(repository: Any) -> BareRepositoryLease | None:
    """Accept a lease or nothing; membership in the type, not duck typing.

    An object that merely has ``selector`` and ``descriptor`` could name any
    directory at all, and the whole point of a lease is that custody was
    established when it was acquired and is revalidated when it is borrowed.
    """

    if repository is None:
        return None
    if type(repository) is not BareRepositoryLease:
        raise GitTransportExecutionError("invalid-repository", "repository")
    return repository


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


def _decode_protocol_stream(raw: Any) -> str:
    """Decode a machine-readable stream without altering a byte of it.

    ``errors="replace"`` for the same reason the diagnostics module uses it:
    undecodable bytes are a thing a remote can send, and raising on them would
    let a remote suppress its own answer.  The type check mirrors that module's,
    so an executor returning something other than bytes fails the same way on
    either stream rather than silently producing a different kind of value.
    """

    if type(raw) is not bytes:
        raise TypeError("transport output must be bytes")
    return raw.decode("utf-8", errors="replace")


def run_remote_operation(
    executor: GitExecutor,
    executable: str,
    target: Any,
    subcommand: Any,
    arguments: Any = (),
    *,
    environment: Mapping[str, str] | None = None,
    global_options: Any = (),
    repository: Any = None,
) -> RemoteOperationResult:
    """Build the command, run it through ``executor``, and make its output safe.

    The deadline and the process-group termination belong to the executor and are
    inherited unchanged; nothing is spawned here.

    A ``repository`` lease is held for the whole operation, the way both local
    runners hold theirs: identity is revalidated as the borrow is taken, and
    cleanup waits on the borrow count, so the repository cannot be removed from
    under a running child.
    """

    lease = _checked_repository(repository)
    # One build-and-execute path whether or not there is a lease: a second path
    # is where the borrow gets forgotten.
    holder = nullcontext(None) if lease is None else lease.borrow()
    with holder as bound:
        command = build_remote_command(
            executable,
            target,
            subcommand,
            arguments,
            environment=environment,
            global_options=global_options,
            repository=bound,
        )
        result = executor.execute(command)
    if not isinstance(result, GitCommandResult):
        raise GitTransportExecutionError("invalid-result", "executor")
    return RemoteOperationResult(
        exit_status=result.exit_status,
        # Protocol data: decoded, and otherwise exactly what the child wrote.
        # Redacting it here strips the TAB the porcelain is separated by.
        stdout=_decode_protocol_stream(result.stdout),
        # Prose a remote chose, which reaches a log unexamined.
        stderr=redact_transport_diagnostics(result.stderr),
        local_failure=result.local_failure,
    )
