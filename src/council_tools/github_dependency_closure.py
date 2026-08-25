"""Build the dependency closure the admission predicate actually compares against.

``evaluate_ticket_admission`` requires the dependency states it is given to
correspond exactly to what a ticket declares: it compares the set of issue
numbers in the supplied closure against the set in the contract, rejects any
difference as ``invalid-dependency-closure``, and only then checks that every
entry is closed.  Nothing produced that evidence, so it was assembled by hand.

Each step of a hand-rolled assembly has a failure mode that yields a
structurally valid closure carrying a wrong answer.  A dependency absent from the
available payloads becomes a silent omission — which the predicate cannot detect,
because it never learns the dependency existed — or an invented state.  An
unparseable payload becomes the same.  A ticket that declares itself can never be
satisfied, and reporting it as merely unclosed hides that it never will be.

**One hop, not a traversal.**  Because the predicate compares against the
declared set directly, a wider transitive closure would be rejected outright.
Walking further would produce entries the predicate refuses, so this module
resolves exactly what the ticket declares and nothing more.  That is not a
simplification: it is the only shape the predicate accepts.

**What this module does not re-check.**  A self-declared dependency, a duplicate,
an out-of-range issue number and an over-long dependency list are all rejected by
``ticket_contracts`` when the body is parsed, with the codes ``self-dependency``,
``duplicate-dependency``, ``invalid-dependency`` and ``invalid-dependencies``.  A
published body carrying any of them cannot be parsed at all, so a copy of those
rules here would be unreachable, and a second copy of a rule is what drifts.  The
guarantee is delegated, not dropped, and the tests prove the delegation holds.

This module decides nothing about admission.  It performs no input-output; the
payloads arrive from the caller.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

import council_tools.ticket_admission as ticket_admission
import council_tools.ticket_policy as ticket_policy


class DependencyClosureError(ValueError):
    """A stable, field-addressed dependency-closure resolution failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"dependency closure {code} at {field}")


def declared_dependencies(body: Any, *, field: str) -> tuple[int, ...]:
    """Read the declared dependency set out of a published ticket body.

    An issue whose body carries no published contract is **not** an error: a
    dependency may be a plain issue with no governed body, and such an issue
    simply declares nothing.  Only a body that fails to parse *as* a ticket is
    a failure, and it is reported rather than treated as an empty declaration,
    because those two look identical downstream and mean opposite things.
    """

    if type(body) is not str:
        raise DependencyClosureError("invalid-issue-body", field)
    if ticket_policy.TICKET_POLICY_V1.contract_start_marker not in body:
        return ()
    try:
        envelope = ticket_policy.parse_ticket_issue_body(body)
    except ValueError as error:
        raise DependencyClosureError("unparseable-ticket-body", field) from error
    return tuple(envelope.contract.dependencies)


def build_dependency_closure(
    issue_number: int,
    lookup: Callable[[int], Any] | Mapping[int, Any],
    *,
    max_dependencies: int = ticket_admission.MAX_ADMISSION_DEPENDENCIES,
) -> list[dict[str, Any]]:
    """Return one closure entry per declared dependency, in declared order.

    ``lookup`` maps an issue number to that issue's payload, which must carry at
    least ``state`` and ``body``.  It is supplied by the caller because fetching
    is a separate outcome.
    """

    if type(issue_number) is not int or issue_number <= 0:
        raise DependencyClosureError("invalid-issue-number", "issueNumber")

    resolve = lookup.get if isinstance(lookup, Mapping) else lookup
    if not callable(resolve):
        raise DependencyClosureError("invalid-lookup", "lookup")

    ticket = _payload(resolve, issue_number, field="issue")
    declared = declared_dependencies(ticket.get("body"), field="issue.body")

    # The declared set arrives already validated: parsing rejected a
    # self-dependency, a duplicate, an out-of-range number and an over-long
    # list before this line could run.  The bound is asserted rather than
    # re-decided, so a future widening upstream surfaces here instead of
    # silently producing a closure the predicate will refuse.
    if len(declared) > max_dependencies:  # pragma: no cover - upstream bound is tighter
        raise DependencyClosureError("too-many-dependencies", "contract.dependencies")

    closure: list[dict[str, Any]] = []
    for dependency in declared:
        payload = _payload(resolve, dependency, field=f"dependency.{dependency}")
        closure.append(
            {
                "issueNumber": dependency,
                "state": _state(payload.get("state"), field=f"dependency.{dependency}.state"),
            }
        )
    return closure


def _payload(resolve: Callable[[int], Any], issue_number: int, *, field: str) -> Mapping[str, Any]:
    try:
        payload = resolve(issue_number)
    except Exception as error:  # noqa: BLE001 - a lookup failure is a missing dependency
        raise DependencyClosureError("dependency-not-available", field) from error
    if payload is None:
        raise DependencyClosureError("dependency-not-available", field)
    if type(payload) is not dict:
        raise DependencyClosureError("invalid-payload", field)
    return payload


def _state(value: Any, *, field: str) -> str:
    if type(value) is not str:
        raise DependencyClosureError("invalid-state", field)
    folded = value.lower()
    if folded not in ticket_admission.ISSUE_STATES:
        raise DependencyClosureError("unknown-issue-state", field)
    return folded


__all__ = [
    "DependencyClosureError",
    "build_dependency_closure",
    "declared_dependencies",
]
