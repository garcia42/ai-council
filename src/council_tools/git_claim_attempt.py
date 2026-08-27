"""Make the one exact create-only claim attempt, and read what the server decided.

Four pieces of the compare-and-set are merged and nothing joined them.
:mod:`council_tools.git_transport_execution` builds and runs a remote command
through the frozen identity policy, binds it to a local repository, and returns
its machine-readable stream untouched.
:mod:`council_tools.git_claim_observation` reads the status lines into a
provisional observation.  :mod:`council_tools.git_claim_materialization` writes
the claim object into a leased repository and returns its identifier.  What was
missing is the attempt itself -- the step where the mutual exclusion actually
happens, because the **server** decides it, and it decides on a single exact
argument vector.

That vector has to be exact in ways ordinary Git usage is not:

* an **empty expected value naming the full reference** is what says *create
  only*; a bare ``--force-with-lease`` is a different and much weaker request;
* **one explicit refspec** naming the object and the same full reference, because
  an implicit refspec pushes whatever the current branch happens to be;
* **no force**, because force and an empty expected value are contradictory and
  which one wins is not visible at the call site;
* **exactly one reference**, because a second refspec makes the exit status
  describe two operations at once and the status lines stop answering one
  question;
* **exactly one attempt**, because a retry after a refusal is either meaningless
  or a second race, and a retry after a *transport* failure can succeed against a
  server that already accepted the first attempt without reporting it.

Get any of these wrong and the command pushes successfully while meaning
something the protocol cannot use.

**The exit status is not the answer.**  Measured on the pinned ``git 2.39.5``: a
create and a push of the identical object *both exit 0*, and those are different
facts about who holds the claim.  A refusal exits 1.  A missing remote exits 128
with no status lines at all.  So the outcome is derived from the status and the
parsed observation **together**, and the cases where they disagree -- or where
the observation cannot be read because the run was killed or timed out -- are
their own answers rather than being folded into the nearest one.

**One measured case is a local fault wearing the shape of a refusal.**  When the
bound repository does not hold the object being sent, the server reports::

    !<TAB><oid>:<ref><TAB>[remote rejected] (unpacker error)     (no `Done`)
    fatal: bad object <oid>

Under #151's rule the ``!`` flag is honoured whatever summary follows it, so the
only thing separating that from "another session holds the claim" is the absent
trailer, which :func:`~council_tools.git_claim_observation.observe_claim_push`
checks before it classifies anything.  It is therefore an **unreadable
observation**, never a refusal, and that is pinned by a test rather than left to
the trailer rule being remembered.

**Nothing is re-decided here.**  The remote is validated, the environment built,
the repository bound and the two output streams treated by the transport module;
the status lines are parsed by the observer; the deadline and process-group
termination belong to the executor.  This module owns the vector, the single
attempt, and the reconciliation of status with observation -- and nothing else.
In particular it neither redacts nor decodes anything: standard output arrives as
protocol data and standard error arrives bounded and redacted, which is the whole
reason the composition works now and did not before.

Whether a server serialises simultaneous attempts is a separate question about
the server, and it is another ticket's outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from council_tools.git_claim_observation import (
    ALREADY_CURRENT,
    CREATED,
    REJECTED,
    GitClaimObservationError,
    observe_claim_push,
)
from council_tools.git_object_id import Sha1ObjectId
from council_tools.git_transport_execution import run_remote_operation

#: The subcommand.  Named here so a caller cannot substitute another.
PUSH_SUBCOMMAND = "push"

#: The options that make the attempt what it is.  ``--porcelain`` is what makes
#: the answer machine-readable at all; ``--no-verify`` keeps a local hook from
#: deciding a question the server owns.
REQUIRED_OPTIONS: tuple[str, ...] = ("--porcelain", "--no-verify")

#: Arguments that would change what the attempt means.  Refused rather than
#: filtered, because a caller that passed one wanted something this cannot do.
#:
#: This is a prefix set, so ``--force-with-lease=<anything>`` is refused by its
#: bare prefix: the only expected value that may reach a child is the empty one
#: this module builds.
FORBIDDEN_ATTEMPT_ARGUMENTS: tuple[str, ...] = (
    "--force",
    "-f",
    "--force-with-lease",
    "--force-if-includes",
    "--mirror",
    "--all",
    "--tags",
    "--follow-tags",
    "--delete",
    "-d",
    "--atomic",
    "--set-upstream",
    "-u",
)

#: What one attempt established.  The first three mirror what the server said;
#: the last three say that no server answer could be established at all, which
#: is a different thing from the server saying no.
ATTEMPT_CREATED = "created"
ATTEMPT_ALREADY_CURRENT = "already-current"
ATTEMPT_REJECTED = "rejected"
ATTEMPT_TRANSPORT_FAILED = "transport-failed"
ATTEMPT_UNREADABLE = "unreadable-observation"
ATTEMPT_INCONSISTENT = "status-observation-mismatch"

#: What the exit status is *permitted* to be for each server answer.  A pair
#: outside this table is reported as inconsistent rather than resolved in favour
#: of either side, because there is no reason to trust one over the other.
_CONSISTENT_STATUS: dict[str, frozenset[int]] = {
    CREATED: frozenset({0}),
    ALREADY_CURRENT: frozenset({0}),
    REJECTED: frozenset({1}),
}

_OBSERVED_TO_ATTEMPT: dict[str, str] = {
    CREATED: ATTEMPT_CREATED,
    ALREADY_CURRENT: ATTEMPT_ALREADY_CURRENT,
    REJECTED: ATTEMPT_REJECTED,
}


class GitClaimAttemptError(ValueError):
    """A stable, field-addressed claim-attempt failure."""

    def __init__(self, code: str, field: str, detail: str = ""):
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(
            f"git claim attempt {code} at {field}" + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class ClaimAttemptOutcome:
    """What one attempt established, which is never ownership.

    The name says ``attempt`` rather than ``result`` deliberately.  A positive
    outcome is server state at one moment; the object still has to be read back
    from a fresh repository before anyone acts on it.  So this refuses ``bool()``
    and carries no success field, the same refusal the observation, the command
    result and the admission result already make.
    """

    outcome: str
    exit_status: int | None
    reference: str
    object_id: str
    observed_summary: str | None = None
    diagnostics: str = ""
    detail: str | None = None

    def __bool__(self) -> bool:
        raise TypeError(
            "a claim attempt outcome is provisional, not proof of ownership"
        )


def build_attempt_arguments(object_id: Any, reference: Any) -> tuple[str, ...]:
    """Return the exact arguments for one create-only attempt.

    ``object_id`` is a :class:`~council_tools.git_object_id.Sha1ObjectId` and
    **not** text.  The spike found ``--end-of-options`` unusable on the pinned
    binary, so a typed exact object name is the operand defence: it is validated
    as forty lowercase hexadecimal characters on construction, so nothing that
    could be read as an option, a path or a second refspec can reach the vector.
    Text is refused rather than parsed, which also means the identifier the
    materialization module returned is the identifier that is pushed.
    """

    # Membership in the type, not duck typing: something that merely renders as
    # a name renders as whatever it likes, and `str()` on the wrong object
    # produces a repr Git then reports as an unmatched refspec.
    if type(object_id) is not Sha1ObjectId:
        raise GitClaimAttemptError("invalid-object-id", "object_id")
    checked_reference = _canonical(reference, "reference")
    if not checked_reference.startswith("refs/"):
        raise GitClaimAttemptError("reference-not-full", "reference", checked_reference)
    # A colon would make the refspec parse as a different pair. The object name
    # cannot contain one; the reference can.
    if ":" in checked_reference:
        raise GitClaimAttemptError("colon-in-argument", "reference")
    return (
        *REQUIRED_OPTIONS,
        # The empty value after the colon is what says create-only. Without it
        # this is an ordinary push that would overwrite an existing claim.
        f"--force-with-lease={checked_reference}:",
        f"{object_id.wire_text}:{checked_reference}",
    )


def attempt_claim(
    executor: Any,
    executable: str,
    target: Any,
    object_id: Any,
    reference: Any,
    *,
    repository: Any,
    extra_arguments: Any = (),
) -> ClaimAttemptOutcome:
    """Attempt the claim exactly once and report what could be established.

    ``object_id`` is a typed exact object name; see
    :func:`build_attempt_arguments`.

    ``repository`` is a held lease on the repository holding ``object_id``, and
    it has neither a default nor an accepted ``None``: an attempt with nothing to send is not a weaker attempt,
    it is one the server answers with a refusal shaped like somebody else's
    claim.  It is passed straight to the transport module, which owns what a
    repository binding may be and refuses anything else.

    There is deliberately **no retry parameter and no retry path**.
    """

    arguments = (
        *build_attempt_arguments(object_id, reference),
        *_checked_extra(extra_arguments),
    )
    # Checked AFTER the operands, so this newer requirement does not pre-empt
    # the narrower message an invalid object name or reference would give.
    #
    # `repository` has no default and None is not one either. The transport
    # module treats an absent repository as "this operation needs no local
    # objects", which is true of `ls-remote` and false of every attempt: unbound,
    # the server answers with the REFUSING flag and `(unpacker error)`, a local
    # fault wearing the shape of somebody else's claim. So the requirement is
    # this module's, and it is stated here rather than borrowed from a module
    # for which the value really is optional.
    if repository is None:
        raise GitClaimAttemptError("repository-required", "repository")
    # Through `run_remote_operation`, not around it: it owns the identity
    # policy, the repository binding, and the treatment of the two output
    # streams. Re-deciding any of that here is how a guarantee goes missing at a
    # call site that reads as protected.
    identifier = object_id.wire_text
    result = run_remote_operation(
        executor,
        executable,
        target,
        PUSH_SUBCOMMAND,
        arguments,
        repository=repository,
    )

    if result.local_failure is not None:
        # No server answer was established at all; the run did not complete.
        return _outcome(
            ATTEMPT_TRANSPORT_FAILED, result, reference, identifier,
            detail=result.local_failure,
        )

    try:
        observation = observe_claim_push(result.stdout, expected_ref=reference)
    except GitClaimObservationError as error:
        # A server answer that cannot be read is not a server answer. This
        # covers the missing remote (exit 128, no status lines at all), a run the
        # deadline truncated, and the local fault the server reports with the
        # refusing flag and no trailer.
        return _outcome(
            ATTEMPT_UNREADABLE, result, reference, identifier, detail=error.code,
        )

    if result.exit_status not in _CONSISTENT_STATUS[observation.outcome]:
        # Neither half is trusted over the other: an answer whose two halves
        # disagree is its own result, not the nearest recognised one.
        return _outcome(
            ATTEMPT_INCONSISTENT, result, reference, identifier,
            observed_summary=observation.summary, detail=observation.outcome,
        )

    return _outcome(
        _OBSERVED_TO_ATTEMPT[observation.outcome], result, reference, identifier,
        observed_summary=observation.summary,
    )


def _outcome(
    outcome: str,
    result: Any,
    reference: str,
    object_id: str,
    *,
    observed_summary: str | None = None,
    detail: str | None = None,
) -> ClaimAttemptOutcome:
    return ClaimAttemptOutcome(
        outcome=outcome,
        exit_status=result.exit_status,
        reference=reference,
        object_id=object_id,
        observed_summary=observed_summary,
        # Already bounded and redacted by the transport module; carried so an
        # operator can see what a failure said without it being re-treated here.
        diagnostics=result.stderr,
        detail=detail,
    )


def _canonical(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise GitClaimAttemptError("invalid-type", field)
    if not value or value != value.strip():
        raise GitClaimAttemptError("non-canonical", field)
    return value


def _checked_extra(extra_arguments: Any) -> tuple[str, ...]:
    if isinstance(extra_arguments, (str, bytes)) or not isinstance(
        extra_arguments, (tuple, list)
    ):
        raise GitClaimAttemptError("invalid-arguments", "extra_arguments")
    checked: list[str] = []
    for index, argument in enumerate(extra_arguments):
        field = f"extra_arguments[{index}]"
        if not isinstance(argument, str):
            raise GitClaimAttemptError("invalid-argument", field)
        for forbidden in FORBIDDEN_ATTEMPT_ARGUMENTS:
            if argument == forbidden or argument.startswith(f"{forbidden}="):
                raise GitClaimAttemptError("forbidden-argument", field, forbidden)
        if ":" in argument and not argument.startswith("-"):
            # A bare `a:b` is a refspec, and a second refspec makes the exit
            # status describe two operations at once.
            raise GitClaimAttemptError("second-refspec", field, argument)
        checked.append(argument)
    return tuple(checked)


__all__ = [
    "ATTEMPT_ALREADY_CURRENT",
    "ATTEMPT_CREATED",
    "ATTEMPT_INCONSISTENT",
    "ATTEMPT_REJECTED",
    "ATTEMPT_TRANSPORT_FAILED",
    "ATTEMPT_UNREADABLE",
    "FORBIDDEN_ATTEMPT_ARGUMENTS",
    "PUSH_SUBCOMMAND",
    "REQUIRED_OPTIONS",
    "ClaimAttemptOutcome",
    "GitClaimAttemptError",
    "attempt_claim",
    "build_attempt_arguments",
]
