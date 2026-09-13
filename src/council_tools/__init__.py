"""Council forecast ledger tools."""

import sys

MINIMUM_PYTHON = (3, 11)


def _assert_supported_interpreter(version: tuple = None) -> None:
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

    Exit status 2, not 1, deliberately: 1 is this CLI's code for a ledger in invalid
    state, which is the false claim being eliminated here. This is an environment fault
    and must not be reported as a data fault.
    """
    if version is None:
        version = sys.version_info
    if tuple(version[:2]) >= MINIMUM_PYTHON:
        return
    found = ".".join(str(part) for part in version[:3])
    required = ".".join(str(part) for part in MINIMUM_PYTHON)
    raise SystemExit(
        f"council-tools requires Python >= {required}; this interpreter is {found} "
        f"({sys.executable}). The ledger is not the problem. Note that `python3` and "
        f"`python3.11` on PATH may both be older builds -- use an absolute path to a "
        f"real {required}, e.g. /usr/bin/python3.11."
    )


_assert_supported_interpreter()
