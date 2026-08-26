"""Order a decomposition's children by their sibling dependencies.

A council decomposition names its children by a **local key**, because at the
moment it is written none of them has an issue number.  ``ticket_contracts``
validates a contract's ``dependencies`` as integer issue numbers and rejects
anything else, so a child that depends on a sibling cannot state that dependency
in its own contract at planning time.  The relation travels beside the contract,
and this module is what turns it into an order.

Three failure modes each produce a plausible-looking result unless they are
refused:

* a reference to a key the set never defines is a dependency on nothing, and
  reading it as satisfied orders a child ahead of a sibling that does not exist;
* a key that appears twice makes every reference to it ambiguous, and silently
  keeping either definition chooses for the author;
* a cycle admits no order at all, and no later assignment of issue numbers will
  produce one, so refusing the set when it is read beats discovering it when
  half the tickets already exist.

**The order is decided from the relations alone.**  A depth-first walk emits an
order that depends on which child the set happened to list first, so two
documents naming the same children and the same relations in a different
sequence would produce different orders and therefore different plans, neither
diffable against the other.  Ties are broken by sorting the ready set, which
makes the result a function of the graph rather than of the input sequence.

A local key is the author's own text rather than a validated identifier, so what
a key may be is settled here.  Non-text, empty, and whitespace-padded keys are
**refused rather than normalized**: normalizing would silently merge two keys an
author wrote as distinct.

Everything here is pure: no network, no GitHub, no filesystem, no subprocess.
This module decides nothing about labels, bodies, admission or issue numbers.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from council_tools.ticket_contracts import MAX_LIST_ITEMS


class TicketOrderingError(ValueError):
    """A stable, field-addressed ordering failure."""

    def __init__(self, code: str, field: str, detail: str = ""):
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(
            f"ticket ordering {code} at {field}" + (f": {detail}" if detail else "")
        )


def _canonical_key(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TicketOrderingError("invalid-key", field)
    if not value or value != value.strip():
        raise TicketOrderingError("non-canonical-key", field, repr(value))
    return value


def order_children(children: Any) -> tuple[str, ...]:
    """Return the order in which children may be created.

    ``children`` maps each child's local key to the local keys of the siblings
    it depends on.  The returned order places every child after every sibling it
    depends on.

    Ties are broken lexicographically among the children that are ready at the
    same time, so the result is a function of the relations and not of the order
    the caller happened to supply.
    """

    dependencies = _normalize(children)
    return _kahn(dependencies)


def _normalize(children: Any) -> dict[str, tuple[str, ...]]:
    if isinstance(children, Mapping):
        items: Iterable[tuple[Any, Any]] = children.items()
    else:
        raise TicketOrderingError("invalid-children", "children")

    normalized: dict[str, tuple[str, ...]] = {}
    for raw_key, raw_deps in items:
        key = _canonical_key(raw_key, "children")
        if key in normalized:
            # Unreachable through a dict, whose keys are already unique, but a
            # Mapping is a protocol and a custom one may yield a key twice.
            raise TicketOrderingError("duplicate-key", "children", key)
        if isinstance(raw_deps, (str, bytes)) or not isinstance(raw_deps, Iterable):
            raise TicketOrderingError("invalid-dependencies", f"children[{key}]")
        seen: list[str] = []
        for raw_dep in raw_deps:
            dep = _canonical_key(raw_dep, f"children[{key}]")
            if dep == key:
                # Distinct from a multi-child cycle: the author error differs.
                raise TicketOrderingError("self-dependency", f"children[{key}]", key)
            if dep in seen:
                raise TicketOrderingError("duplicate-dependency", f"children[{key}]", dep)
            seen.append(dep)
        normalized[key] = tuple(seen)

    if not normalized:
        raise TicketOrderingError("empty-children", "children")
    if len(normalized) > MAX_LIST_ITEMS:
        raise TicketOrderingError("too-many-children", "children", str(len(normalized)))

    for key, deps in normalized.items():
        for dep in deps:
            if dep not in normalized:
                raise TicketOrderingError("unknown-key", f"children[{key}]", dep)
    return normalized


def _kahn(dependencies: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    remaining = {key: set(deps) for key, deps in dependencies.items()}
    order: list[str] = []

    while remaining:
        # Sorting the ready set is what makes the result independent of the
        # caller's sequence.  Without it this is still a valid order and no
        # longer a reproducible one.
        ready = sorted(key for key, deps in remaining.items() if not deps)
        if not ready:
            raise TicketOrderingError("dependency-cycle", "children", _cycle(remaining))
        for key in ready:
            del remaining[key]
        for deps in remaining.values():
            deps.difference_update(ready)
        order.extend(ready)

    return tuple(order)


def _cycle(remaining: Mapping[str, set[str]]) -> str:
    """Name one cycle deterministically, so the message is reproducible."""

    start = min(remaining)
    path: list[str] = []
    seen: set[str] = set()
    node = start
    while node not in seen:
        seen.add(node)
        path.append(node)
        candidates = sorted(dep for dep in remaining[node] if dep in remaining)
        if not candidates:
            break
        node = candidates[0]
    if node in path:
        cycle = path[path.index(node):]
        return " -> ".join([*cycle, node])
    return " -> ".join(path)
