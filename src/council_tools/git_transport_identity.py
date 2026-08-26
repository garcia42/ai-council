"""Decide what a remote Git child may be given, and which remotes it may reach.

:mod:`council_tools.git_process_contract` builds a child environment from
scratch and never copies the ambient one, because the descriptor-binding spike
measured that a hostile ``~/.gitconfig`` reaches the child unless global, system
and XDG configuration are suppressed *together*.  Beside that base set sits the
set of keys a caller may add, and it holds only the six authorship and date
keys.  That module names the gap this one fills:

    ``SSH_AUTH_SOCK`` and every other transport key are deliberately absent: a
    later, separately reviewed transport policy extends this set rather than
    editing this module.

Until now nothing had written that policy, so the only two ways to run a remote
operation were both wrong.  Passing an ambient environment reintroduces exactly
the configuration routes the base set exists to close, and hands the child
whatever agent, helper and proxy state happens to be exported.  Widening the
permitted set inline at a call site puts the decision about what a
credential-bearing child may see in whichever function needed it that day, with
no single place to review it and no way to tell afterwards what was permitted.

**Userinfo in a remote URL is refused, not stripped.**  This was measured on the
pinned ``git 2.39.5``: ``git ls-remote "file://user:pass@<path>"`` exits 0 and
lists the ref.  Git neither warns nor fails, so a URL carrying credentials
*works* — while the secret sits in the argument vector, visible in a process
listing on the host, and is precisely the material
:mod:`council_tools.git_transport_diagnostics` has to redact after the fact.
That module states that redaction is the second line of defence and cannot be
the first.  Stripping the userinfo would silently change which identity the
operation uses; accepting it puts a secret in argv.  Refusing is the only option
that surprises nobody.

**Every scp-like remote is refused, and a scheme allow-list alone does not do
it.**  ``git@host:path`` carries no scheme, so the allow-list refuses it.  But
``host:path`` parses as *scheme* ``host``, and — measured on the pinned
``git 2.39.5`` — ``file:remote.git`` is read by ``urlsplit`` as a file URL with a
relative path while **git reads it as scp-like and tries SSH to a host named
"file"**.  A validator that disagreed with git about which host a credential
reaches is the exact failure this module exists to prevent, so a remote must use
the ``scheme://`` authority form and everything else is refused as scp-like.

This module **connects to nothing**.  It returns an environment and a validated
remote.  It spawns no process, opens no socket, reads no credential, and
performs no network, filesystem or subprocess access.  Executing through it is a
separate outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import council_tools.git_process_contract as git_process_contract
from council_tools.git_process_contract import (
    MAX_ARGUMENT_LENGTH,
    GitProcessError,
    GitProcessPolicy,
)

#: The transport keys a remote Git child may additionally receive.
#:
#: ``SSH_AUTH_SOCK`` lets an already-running agent answer a key challenge without
#: this process ever holding the key.  ``GIT_SSH_COMMAND`` pins which SSH binary
#: and options are used rather than inheriting a user's.  Both are named here so
#: the decision is reviewable in one place instead of at a call site.
#:
#: Nothing that names a credential *file*, a helper, a proxy or a trace sink is
#: on this list.  A helper would let the child obtain a secret; a proxy would let
#: it reach a different host than the URL names; a trace sink would let it write
#: the exchange somewhere unreviewed.
TRANSPORT_ENVIRONMENT_KEYS: frozenset[str] = frozenset(
    {
        "SSH_AUTH_SOCK",
        "GIT_SSH_COMMAND",
    }
)

#: The schemes a remote may use.
#:
#: ``file`` is what the local qualification suites drive, and it exercises the
#: real server-side ref decision.  ``ssh`` and ``https`` are the two
#: authenticated transports.  ``git://`` and ``http://`` are absent deliberately:
#: neither authenticates the server, so a claim verified over one proves nothing
#: about who answered.
PERMITTED_SCHEMES: frozenset[str] = frozenset({"file", "ssh", "https"})

#: A ``file`` URL has no host; every other permitted scheme must name one.
_HOSTLESS_SCHEMES: frozenset[str] = frozenset({"file"})


class GitTransportIdentityError(ValueError):
    """A stable, field-addressed transport-identity failure."""

    def __init__(self, code: str, field: str, detail: str = ""):
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(
            f"git transport identity {code} at {field}"
            + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class RemoteTarget:
    """One remote URL that policy has accepted."""

    url: str
    scheme: str


def transport_policy() -> GitProcessPolicy:
    """Return the process policy a remote operation runs under.

    The permitted set is the process contract's default widened by exactly
    :data:`TRANSPORT_ENVIRONMENT_KEYS`.  Whether any of those shadows the base
    child environment is not re-decided here: ``GitProcessPolicy`` already
    refuses that with ``explicit-key-shadows-base``, and a second copy of the
    rule is what drifts.
    """

    return GitProcessPolicy(
        explicit_keys=frozenset(
            git_process_contract.DEFAULT_EXPLICIT_ENVIRONMENT_KEYS
            | TRANSPORT_ENVIRONMENT_KEYS
        )
    )


def validate_remote_url(url: Any) -> RemoteTarget:
    """Accept one remote URL, or refuse it with a code naming why."""

    if not isinstance(url, str):
        raise GitTransportIdentityError("invalid-type", "url")
    if not url or url != url.strip():
        raise GitTransportIdentityError("non-canonical-url", "url")
    if len(url) > MAX_ARGUMENT_LENGTH:
        raise GitTransportIdentityError("url-too-long", "url", str(len(url)))
    if any(character < " " or character == "\x7f" for character in url):
        raise GitTransportIdentityError("control-character", "url")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise GitTransportIdentityError("unparseable-url", "url") from exc

    scheme = parts.scheme.lower()
    if not scheme:
        # An scp-like remote lands here, and refusing it is the intent.
        raise GitTransportIdentityError("missing-scheme", "url")
    # Before the scheme check, because a remote that is not a URL at all is a
    # more fundamental refusal than one naming a scheme policy does not allow.
    #
    # Measured on git 2.39.5, and it is why this rule exists: `urlsplit` reads
    # "file:remote.git" as scheme "file" with a relative path, while **git reads
    # the same string as scp-like syntax** and tries to reach a host named
    # "file" over SSH ("ssh: Could not resolve hostname file").  Validating a
    # string one way while git resolves it another is precisely what this module
    # exists to prevent, so the authority form is required and every scp-like
    # remote is refused with one code.
    if url[: len(scheme) + 3].lower() != f"{scheme}://":
        raise GitTransportIdentityError("scp-like-remote", "url", scheme)
    if scheme not in PERMITTED_SCHEMES:
        raise GitTransportIdentityError("scheme-not-permitted", "url", scheme)

    # Before the host check, because a URL carrying a credential must be refused
    # for carrying it rather than for whatever else is also wrong with it.
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise GitTransportIdentityError("url-carries-userinfo", "url", scheme)

    if scheme in _HOSTLESS_SCHEMES:
        if not parts.path:
            raise GitTransportIdentityError("empty-path", "url", scheme)
    elif not parts.hostname:
        raise GitTransportIdentityError("empty-host", "url", scheme)

    return RemoteTarget(url=url, scheme=scheme)


def transport_child_environment(
    explicit: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a fresh environment for a remote child.

    The base child environment plus only the permitted transport keys the caller
    supplied.  Nothing is read from the ambient environment, and the returned
    mapping aliases nothing the caller passed.
    """

    if explicit is None:
        return transport_policy().child_environment()
    if not isinstance(explicit, Mapping):
        raise GitTransportIdentityError("invalid-type", "environment")

    for key in explicit:
        if key in git_process_contract.DEFAULT_EXPLICIT_ENVIRONMENT_KEYS:
            continue
        if key not in TRANSPORT_ENVIRONMENT_KEYS:
            raise GitTransportIdentityError(
                "transport-key-not-permitted", "environment", str(key)
            )
    try:
        return transport_policy().child_environment(dict(explicit))
    except GitProcessError as exc:
        raise GitTransportIdentityError(exc.code, exc.field) from exc
