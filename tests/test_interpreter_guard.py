"""The interpreter guard must refuse 3.10, say why, and exit with the right status.

Written because the absence of this guard produced a false "the council ledger is
invalid" conclusion on 2026-09-13, and because the first version of these tests failed
to catch two defects in the guard they were written for:

  * `assert excinfo.value.code != 0` was vacuous -- SystemExit carrying a string puts the
    string in `.code`, so the assertion passed while the guard exited 1, the very code
    that means "invalid ledger". `.code` is now an int and is asserted exactly.
  * Nothing tested that the guard was ARMED. Deleting the module-level call left all
    tests green with the guard completely dead. `test_guard_is_armed_at_import` reads the
    module's AST so that mutation now fails on any machine, with no 3.10 needed.

The subprocess test is the end-to-end proof and needs a real sub-3.11 interpreter; it
skips explicitly rather than silently when none is present.
"""

import ast
import os
import pathlib
import subprocess
import sys
import tomllib

import pytest

import council_tools
from council_tools import (
    MINIMUM_PYTHON,
    UNSUPPORTED_INTERPRETER_EXIT,
    _assert_supported_interpreter,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_supported_interpreter_is_accepted():
    _assert_supported_interpreter((3, 11, 2))
    _assert_supported_interpreter((3, 12, 0))
    _assert_supported_interpreter(sys.version_info)


def test_the_interpreter_that_broke_the_ledger_read_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        _assert_supported_interpreter((3, 10, 18))
    # The literal 2 only. `== UNSUPPORTED_INTERPRETER_EXIT` compared the constant to
    # itself: with that constant mutated to 1 it PASSED while this line failed, which is
    # the same vacuous-assertion class already removed from this file. Exit 1 would mean
    # "the ledger is in invalid state", the false conclusion this guard exists to prevent.
    assert excinfo.value.code == 2


def test_refusal_blames_the_interpreter_and_not_the_ledger(capsys):
    with pytest.raises(SystemExit):
        _assert_supported_interpreter((3, 10, 18))
    message = capsys.readouterr().err
    assert "3.10.18" in message
    assert "requires Python" in message
    # The failure this replaces claimed the ledger was invalid. Say the opposite.
    assert "ledger is not the problem" in message.lower()


def test_guard_is_armed_at_import():
    """The module must CALL the guard, not merely define it.

    Deleting the module-level call is invisible to every test that exercises the function
    directly, and that mutation shipped once already.
    """
    source = pathlib.Path(council_tools.__file__).read_text()
    module = ast.parse(source)
    armed = [
        node
        for node in module.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_assert_supported_interpreter"
    ]
    assert armed, "council_tools/__init__.py defines the guard but never calls it"


def test_minimum_matches_the_declared_requirement():
    """MINIMUM_PYTHON must track pyproject, not restate it.

    The previous version compared the constant to a literal, so bumping requires-python
    would have gone unnoticed -- which is the only drift this test exists to catch.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    declared = pyproject["project"]["requires-python"]
    assert declared.startswith(">="), f"unexpected requires-python form: {declared!r}"
    wanted = tuple(int(part) for part in declared[2:].strip().split("."))
    # Compared whole, not sliced by MINIMUM_PYTHON itself: slicing by the value under
    # test let MINIMUM_PYTHON = (3,) satisfy the assertion named for exactly that drift.
    assert MINIMUM_PYTHON == wanted


def test_the_guard_can_speak_on_interpreters_older_than_it_requires():
    """The guard must not need the interpreter it is refusing in order to refuse it.

    `_assert_supported_interpreter` is annotated `tuple[int, ...] | None`.  PEP 604
    unions on builtin generics are a 3.10 feature, and without
    `from __future__ import annotations` that expression is evaluated when the `def`
    executes -- at import, before the guard can run.  On 3.9 or older the module would
    therefore raise an uncaught TypeError, and an uncaught exception exits 1: the exact
    status that means "the ledger is in invalid state" and the exact false conclusion
    this whole guard exists to prevent.

    Deferring annotations costs nothing and keeps the refusal working on any interpreter
    old enough to reach it.  Asserted against the AST rather than by running an old
    interpreter because this host has none below 3.10, so a runtime check would skip
    and prove nothing.
    """
    source = pathlib.Path(council_tools.__file__).read_text()
    module = ast.parse(source)
    deferred = [
        node
        for node in module.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
    ]
    assert deferred, (
        "council_tools/__init__.py evaluates its annotations at import time, so an "
        "interpreter older than the annotation syntax fails with an uncaught TypeError "
        "(exit 1) instead of the guard's exit 2"
    )


_UNSUPPORTED_CANDIDATES = (
    "/home/trader/pysystemtrade/venv/bin/python3",
    "/usr/bin/python3.10",
    "/usr/bin/python3.9",
)


def _unsupported_interpreter():
    """Any interpreter on this host older than the declared minimum, or None."""
    for candidate in _UNSUPPORTED_CANDIDATES:
        path = pathlib.Path(candidate)
        if not path.exists():
            continue
        probe = subprocess.run(
            [str(path), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            continue
        try:
            version = tuple(int(part) for part in probe.stdout.strip().split("."))
        except ValueError:
            continue
        # (3, 11) literal, NOT MINIMUM_PYTHON: deriving the probe's threshold from the
        # constant under test let `MINIMUM_PYTHON = (3, 10)` make this search find
        # nothing, so the end-to-end proof skipped itself out of existence under the
        # one mutation it exists to catch.
        if version < (3, 11):
            return str(path)
    return None


def test_subprocess_import_on_an_unsupported_interpreter_exits_two():
    """End-to-end: the real import, on a real old interpreter, with the real status.

    This is the test that pins both defects at once -- it fails if the guard is disarmed
    and it fails if the status reverts to 1.
    """
    interpreter = _unsupported_interpreter()
    if interpreter is None:
        # FAIL, do not skip.  This is the only out-of-process proof of the refusal and it
        # kills most of the mutations the guard is reviewed against, but every candidate it
        # searches lives in another project's virtualenv -- and PATH's `python3`,
        # `python3.11` and `python` all resolve into that same venv, so they vanish
        # together when it is rebuilt on 3.11.  A skip would then go green and silent among
        # the skips the suite already has, exactly the blind spot tests/test_rehearse.py
        # names and anchors for its own neighbour suite.
        # Set COUNCIL_ALLOW_NO_OLD_INTERPRETER=1 to opt out deliberately and visibly.
        if os.environ.get("COUNCIL_ALLOW_NO_OLD_INTERPRETER") == "1":
            pytest.skip("opted out: COUNCIL_ALLOW_NO_OLD_INTERPRETER=1")
        pytest.fail(
            "no sub-3.11 interpreter on this host, so the end-to-end refusal is unproven; "
            f"searched {_UNSUPPORTED_CANDIDATES}. Install one, or set "
            "COUNCIL_ALLOW_NO_OLD_INTERPRETER=1 to accept the loss of this proof."
        )
    result = subprocess.run(
        [interpreter, "-c", "import council_tools"],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    # The literal 2, not the constant: asserting against UNSUPPORTED_INTERPRETER_EXIT is
    # constant-relative, so redefining that constant to 1 left this test PASSING -- and the
    # sealed shared outcome names "exit status EXACTLY 2", out of process.
    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}; stderr={result.stderr!r}"
    )
    assert result.returncode == UNSUPPORTED_INTERPRETER_EXIT
    assert "requires Python" in result.stderr
    assert "ledger is not the problem" in result.stderr.lower()
