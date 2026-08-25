"""Turn a validated claim request into one Git object, in order.

Every part of the local write path exists separately and nothing joins them:
a validated request that renders canonical bytes, four write builders, a lease,
a runner that composes a builder with that lease, and an executor that runs the
result under a deadline.

The sequence is the missing piece, and the sequence is where the guarantees are
won or lost:

* **Ordering.**  The four writes are ordered and each consumes the previous
  one's output -- the tree names the blob, the commit names the tree.  A caller
  assembling them by hand chooses which identifier flows where, and because
  every identifier has the same forty-hex shape, passing the wrong one is
  invisible.  Here nothing is a parameter: each input is derived from the
  previous result.
* **Result discipline.**  Each write returns a result carrying *either* an exit
  status *or* a local failure.  ``GitCommandResult`` refuses truthiness for
  exactly this reason.  A result read without checking that distinction turns a
  command that never ran into an object identifier that is really an error.
* **Typed parsing.**  Each command's output is text a child produced.  It is
  parsed into the exact :class:`Sha1ObjectId` the next builder requires and
  refused otherwise, rather than forwarded as whatever bytes arrived.

**Producing an object is not owning a claim.**  No compare-and-set happens here
and none is implied.  The identifier returned is the *input* to that decision,
not its result: an object exists in a repository, and whether it owns anything
is decided by a ref operation this module does not perform.
"""

from __future__ import annotations

from typing import Any

from council_tools.git_claim_request import ClaimRequest
from council_tools.git_local_write_operations import (
    CreateClaimTree,
    CreateZeroParentCommit,
    InitializeBareRepository,
    WriteCanonicalBlob,
)
from council_tools.git_object_id import GitObjectIdError, Sha1ObjectId
from council_tools.git_repository_lease import BareRepositoryLease
from council_tools.git_repository_write_runner import run_write_operation

#: The ordered steps, named so a refusal says which one failed rather than that
#: something did.
STEP_INITIALIZE = "initialize"
STEP_BLOB = "write-blob"
STEP_TREE = "create-tree"
STEP_COMMIT = "create-commit"

MATERIALIZATION_STEPS: tuple[str, ...] = (
    STEP_INITIALIZE,
    STEP_BLOB,
    STEP_TREE,
    STEP_COMMIT,
)


class ClaimMaterializationError(ValueError):
    """A stable, step-addressed materialization failure."""

    def __init__(self, code: str, step: str):
        self.code = code
        self.step = step
        super().__init__(f"claim materialization {code} at {step}")


def _require_success(result: Any, step: str) -> bytes:
    """Return the step's stdout, having checked both failure channels.

    The two are checked separately and reported separately.  Collapsing them
    would lose the distinction between a command that ran and failed and one
    that never ran at all, which are different problems with different fixes.
    """

    # Not ``if not result``: the result type refuses truthiness precisely so
    # this check cannot be written that way.
    if result.local_failure is not None:
        raise ClaimMaterializationError("local-failure", step)
    if result.exit_status != 0:
        raise ClaimMaterializationError("nonzero-exit", step)
    return result.stdout


def _parse_object_id(stdout: bytes, step: str) -> Sha1ObjectId:
    """Parse one object name from a step's output, or refuse it.

    Git writes exactly ``<oid>\n``.  Exactly that one trailing newline is
    removed, and nothing else is normalised.  ``strip()`` would silently accept
    leading spaces, trailing spaces and a doubled newline -- output this command
    does not produce -- and output that differs from what the command produces
    is a reason to stop rather than something to tidy up.
    """

    try:
        text = stdout.decode("ascii")
    except UnicodeDecodeError:
        raise ClaimMaterializationError("malformed-output", step) from None
    if text.endswith("\n"):
        text = text[:-1]
    try:
        return Sha1ObjectId(text)
    except GitObjectIdError:
        raise ClaimMaterializationError("malformed-output", step) from None


def materialize_claim_object(
    request: Any,
    lease: Any,
    executor: Any,
) -> Sha1ObjectId:
    """Write ``request`` into ``lease`` and return its root commit identifier.

    The lease is supplied already held; acquiring, revalidating and removing one
    belong to its owner.  Nothing here decides whether the resulting object owns
    anything.
    """

    if type(request) is not ClaimRequest:
        raise ClaimMaterializationError("not-a-claim-request", "request")
    if type(lease) is not BareRepositoryLease:
        raise ClaimMaterializationError("not-a-lease", "lease")
    if not callable(getattr(executor, "execute", None)):
        raise ClaimMaterializationError("not-an-executor", "executor")

    # Step one produces no identifier; it is still checked, because a failed
    # init would otherwise surface as a confusing failure two steps later.
    _require_success(
        run_write_operation(InitializeBareRepository(), lease, executor),
        STEP_INITIALIZE,
    )

    blob_id = _parse_object_id(
        _require_success(
            run_write_operation(
                WriteCanonicalBlob(content=request.payload_bytes()), lease, executor
            ),
            STEP_BLOB,
        ),
        STEP_BLOB,
    )

    # The tree's only input is the blob identifier this sequence just produced.
    tree_id = _parse_object_id(
        _require_success(
            run_write_operation(CreateClaimTree(blob_id=blob_id), lease, executor),
            STEP_TREE,
        ),
        STEP_TREE,
    )

    return _parse_object_id(
        _require_success(
            run_write_operation(
                CreateZeroParentCommit(
                    tree_id=tree_id,
                    message=request.message_bytes(),
                    **request.commit_identity(),
                ),
                lease,
                executor,
            ),
            STEP_COMMIT,
        ),
        STEP_COMMIT,
    )


__all__ = [
    "MATERIALIZATION_STEPS",
    "STEP_BLOB",
    "STEP_COMMIT",
    "STEP_INITIALIZE",
    "STEP_TREE",
    "ClaimMaterializationError",
    "materialize_claim_object",
]
