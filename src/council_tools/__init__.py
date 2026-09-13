"""Council forecast ledger tools."""

# Deferred so the annotation below is never evaluated at import. A PEP 604 union on a
# builtin generic needs 3.10, and an interpreter that cannot evaluate it dies with an
# uncaught TypeError -- exit 1, this CLI's code for an invalid ledger, which is the
# false verdict the guard below exists to prevent. The guard must outlive its own
# syntax.
from __future__ import annotations

import sys

MINIMUM_PYTHON = (3, 11)

# Not 1: this CLI reserves 1 for "the ledger is in invalid state, stop the council",
# which is precisely the wrong conclusion to draw from an environment fault. 3 is taken
# by grading debt. 2 is the closest available meaning -- the operator invoked the tool
# wrongly -- and the accompanying contract text says so.
UNSUPPORTED_INTERPRETER_EXIT = 2


def _assert_supported_interpreter(version: tuple[int, ...] | None = None) -> None:
    """Refuse an interpreter older than the one pyproject declares.

    This package sets requires-python = ">=3.11" and depends on it at runtime.
    Python 3.10's datetime.fromisoformat rejects fractional seconds longer than six
    digits, and four council-attempt rows in the live ledger carry nanosecond
    evidenceCutoffAt values, so under 3.10 the READER rejects rows the APPENDER wrote:
    `report` exits 1 with "evidenceCutoffAt must be an ISO-8601 timestamp", which the
    council skill defines as invalid state that STOPS the council.

    On 2026-09-13 that reported a healthy ledger as corrupt and cost an hour before the
    interpreter was suspected. The trap is that `python3` AND `python3.11` both resolve
    to a 3.10 build here -- reaching deliberately for "the 3.11 one" does not save you --
    so the failure looks like data corruption and the documented remedies do not apply:
    repair-tail refuses anything but a torn final line, the rows are mid-file and parse
    fine under 3.11, and hand-editing the JSONL is forbidden. An operator who trusts the
    exit code is either blocked or "repairs" a healthy ledger.

    Exits with UNSUPPORTED_INTERPRETER_EXIT (2), not 1, deliberately: 1 is this CLI's
    code for a ledger in invalid state, which is the false claim being eliminated here.
    This is an environment fault and must not be reported as a data fault. The status is
    pinned by a subprocess test, because the obvious assertion -- that SystemExit.code is
    truthy -- is vacuous: .code is the message string when SystemExit carries one.
    """
    if version is None:
        version = sys.version_info
    if tuple(version[:2]) >= MINIMUM_PYTHON:
        return
    found = ".".join(str(part) for part in version[:3])
    required = ".".join(str(part) for part in MINIMUM_PYTHON)
    # print + SystemExit(int), NOT SystemExit(str): SystemExit carrying a string prints it
    # and exits 1, and 1 is this CLI's code for a ledger in invalid state -- the false
    # claim this guard exists to eliminate. The first version of this guard did exactly
    # that and was therefore a no-op on the failure it targeted.
    print(
        f"council-tools requires Python >= {required}; this interpreter is {found} "
        f"({sys.executable}). The ledger is not the problem. Note that `python3` and "
        f"`python3.11` on PATH may both be older builds -- use an absolute path to a "
        f"real {required}, e.g. /usr/bin/python3.11.",
        file=sys.stderr,
    )
    raise SystemExit(UNSUPPORTED_INTERPRETER_EXIT)


_assert_supported_interpreter()
