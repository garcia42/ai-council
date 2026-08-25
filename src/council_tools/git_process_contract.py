"""Frozen records describing exactly what a Git child process receives.

Running a Git child safely means deciding, in one place, what that process gets:
which executable, which argument vector, which environment, which open file
descriptors, and which bytes on standard input.  Without a shared record each
call site answers those questions for itself and the answers diverge.

Two of them are security-relevant and were measured on the pinned ``git 2.39.5``
in ``design/git-fd-binding-spike.md``:

* **Environment.** With a hostile ``~/.gitconfig`` reachable, an identity value
  in the child came back attacker-controlled.  It was unset only once global,
  system and XDG configuration were all suppressed *together* (Row 7).  So the
  environment here is built from scratch and never copied: an inherited value
  cannot leak through a policy that starts empty.
* **Descriptors.** A child inherits whatever the parent left open unless someone
  says otherwise, so the inherited set is named explicitly rather than accepted
  (Row 2).

This module is a **non-authorizing** process record.  It validates structure and
nothing else.  It makes no isolation, repository, Git-operation, transport, or
approval claim, and its public constructibility is emphatically *not* evidence
that an operation is approved: building a ``GitCommand`` is as easy as building
any other dataclass, and the approval controls live elsewhere.

It spawns nothing.  There is no subprocess call, no filesystem access, no procfs
access, no command rendering and no deadline behaviour here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


MAX_ARGUMENT_COUNT = 1_024
MAX_ARGUMENT_LENGTH = 4_096
MAX_ENVIRONMENT_ENTRIES = 256
MAX_STDIN_BYTES = 8 * 1024 * 1024

#: Suppression must be applied as a set; the spike measured that omitting any one
#: of these still let a hostile configuration reach the child.
BASE_CHILD_ENVIRONMENT: Mapping[str, str] = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "XDG_CONFIG_HOME": "/nonexistent",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
    "LC_ALL": "C",
    "LANG": "C",
    "TZ": "UTC",
}

#: The shipped default carries only deterministic authorship and date keys.
#: ``SSH_AUTH_SOCK`` and every other transport key are deliberately absent: a
#: later, separately reviewed transport policy extends this set rather than
#: editing this module.
DEFAULT_EXPLICIT_ENVIRONMENT_KEYS: frozenset[str] = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
    }
)

_CONTROL_CHARACTERS = ("\x00",)


class GitProcessError(ValueError):
    """A stable, field-addressed process-record validation failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"git process {code} at {field}")


def _require_text(value: Any, field: str, *, max_length: int = MAX_ARGUMENT_LENGTH) -> str:
    if type(value) is not str:
        raise GitProcessError("invalid-type", field)
    if len(value) > max_length:
        raise GitProcessError("too-long", field)
    if any(character in value for character in _CONTROL_CHARACTERS):
        raise GitProcessError("control-character", field)
    return value


def _require_absolute_executable(value: Any, field: str) -> str:
    text = _require_text(value, field)
    if not text.startswith("/"):
        raise GitProcessError("executable-not-absolute", field)
    if text.endswith("/"):
        raise GitProcessError("executable-not-absolute", field)
    return text


@dataclass(frozen=True)
class RenderedInvocation:
    """The invocation in named parts, so nothing is spliced blind.

    Operation builders populate these fields and a repository binding is
    inserted into ``global_options`` at a known position.  Keeping the parts
    named is what lets a runner add its binding without an opaque tuple splice,
    and what lets a reviewer see where each argument came from.
    """

    global_options: tuple[str, ...] = ()
    subcommand: str = ""
    subcommand_args: tuple[str, ...] = ()
    stdin: bytes = b""
    identity: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for index, option in enumerate(self.global_options):
            _require_text(option, f"invocation.global_options[{index}]")
        _require_text(self.subcommand, "invocation.subcommand")
        if not self.subcommand:
            raise GitProcessError("empty-subcommand", "invocation.subcommand")
        for index, argument in enumerate(self.subcommand_args):
            _require_text(argument, f"invocation.subcommand_args[{index}]")
        if type(self.stdin) is not bytes:
            raise GitProcessError("invalid-type", "invocation.stdin")
        if len(self.stdin) > MAX_STDIN_BYTES:
            raise GitProcessError("too-long", "invocation.stdin")
        if type(self.identity) is not dict:
            raise GitProcessError("invalid-type", "invocation.identity")

    def argv(self) -> tuple[str, ...]:
        """The complete post-executable argument vector, in order."""

        return (*self.global_options, self.subcommand, *self.subcommand_args)


@dataclass(frozen=True)
class GitProcessPolicy:
    """Builds a child environment from scratch; never reads the ambient one.

    ``explicit_keys`` is a constructor input rather than a module constant so a
    separately reviewed transport policy can widen it without editing this
    module, which is the only place the suppression set is defined.
    """

    explicit_keys: frozenset[str] = DEFAULT_EXPLICIT_ENVIRONMENT_KEYS

    def __post_init__(self) -> None:
        if type(self.explicit_keys) is not frozenset:
            raise GitProcessError("invalid-type", "policy.explicit_keys")
        for key in self.explicit_keys:
            _require_text(key, "policy.explicit_keys")
            if key in BASE_CHILD_ENVIRONMENT:
                # Permitting an override would let a caller re-enable exactly the
                # configuration routes the base set exists to close.
                raise GitProcessError("explicit-key-shadows-base", "policy.explicit_keys")

    def child_environment(self, explicit: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return a fresh environment: the base set plus permitted extras only."""

        environment = dict(BASE_CHILD_ENVIRONMENT)
        if explicit is None:
            return environment
        if type(explicit) is not dict:
            raise GitProcessError("invalid-type", "environment")
        if len(explicit) > MAX_ENVIRONMENT_ENTRIES:
            raise GitProcessError("too-long", "environment")
        for key, value in explicit.items():
            _require_text(key, "environment.key")
            if key not in self.explicit_keys:
                raise GitProcessError("environment-key-not-permitted", f"environment.{key}")
            environment[key] = _require_text(value, f"environment.{key}")
        return environment


@dataclass(frozen=True)
class GitCommand:
    """One complete description of a child process, ready to execute.

    ``executable`` is separate from ``argv`` so no consumer prepends an unseen
    prefix: the vector a reviewer inspects is the vector that runs.
    """

    executable: str
    invocation: RenderedInvocation
    environment: Mapping[str, str]
    inherited_descriptors: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _require_absolute_executable(self.executable, "command.executable")
        if type(self.invocation) is not RenderedInvocation:
            raise GitProcessError("invalid-type", "command.invocation")
        if type(self.environment) is not dict:
            raise GitProcessError("invalid-type", "command.environment")
        for key, value in self.environment.items():
            _require_text(key, "command.environment.key")
            _require_text(value, f"command.environment.{key}")
        if type(self.inherited_descriptors) is not tuple:
            raise GitProcessError("invalid-type", "command.inherited_descriptors")
        seen: set[int] = set()
        for index, descriptor in enumerate(self.inherited_descriptors):
            field_name = f"command.inherited_descriptors[{index}]"
            # bool is an int subclass and is not a descriptor.
            if type(descriptor) is not int or descriptor < 0:
                raise GitProcessError("invalid-descriptor", field_name)
            if descriptor in seen:
                raise GitProcessError("duplicate-descriptor", field_name)
            seen.add(descriptor)
        if len(self.argv) > MAX_ARGUMENT_COUNT:
            raise GitProcessError("too-long", "command.argv")

    @property
    def argv(self) -> tuple[str, ...]:
        return self.invocation.argv()

    @property
    def stdin(self) -> bytes:
        return self.invocation.stdin


@dataclass(frozen=True)
class GitCommandResult:
    """What a child produced, which is never a success signal by itself."""

    exit_status: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    local_failure: str | None = None

    def __bool__(self) -> bool:
        # A result read by truthiness is a result read without looking at the
        # exit status, which is how a failed Git call becomes a silent success.
        raise TypeError("a git command result is not a success signal")


class GitExecutor(Protocol):
    """The only protocol method: execute one already-complete command."""

    def execute(self, command: GitCommand) -> GitCommandResult:
        ...  # pragma: no cover - protocol declaration


#: An executor implementing :class:`GitExecutor` must, as a documented
#: obligation this module cannot enforce:
#:
#: 1. preserve the numbers of ``command.inherited_descriptors`` in the child;
#: 2. clear close-on-exec for exactly that set and no other descriptor;
#: 3. close every other non-standard descriptor before exec;
#: 4. never alias the standard streams onto an inherited descriptor.
#:
#: Points 1 and 4 are what make a ``/proc/self/fd/<n>`` selector meaningful in
#: the child; point 3 is what stops the parent's open files leaking into it.
EXECUTOR_OBLIGATIONS = (
    "preserve-descriptor-numbers",
    "clear-cloexec-for-allowlist-only",
    "close-other-non-standard-descriptors",
    "never-alias-standard-streams",
)


__all__ = [
    "BASE_CHILD_ENVIRONMENT",
    "DEFAULT_EXPLICIT_ENVIRONMENT_KEYS",
    "EXECUTOR_OBLIGATIONS",
    "MAX_ARGUMENT_COUNT",
    "MAX_ARGUMENT_LENGTH",
    "MAX_ENVIRONMENT_ENTRIES",
    "MAX_STDIN_BYTES",
    "GitCommand",
    "GitCommandResult",
    "GitExecutor",
    "GitProcessError",
    "GitProcessPolicy",
    "RenderedInvocation",
]
