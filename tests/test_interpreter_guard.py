"""The interpreter guard must fail on 3.10 and say so in the message.

Written because the absence of this guard produced a false "the council ledger is
invalid" conclusion on 2026-09-13. A test that only asserted "raises" would pass on any
error; these assert the message names the interpreter rather than the data.
"""

import pytest

from council_tools import MINIMUM_PYTHON, _assert_supported_interpreter


def test_supported_interpreter_is_accepted():
    _assert_supported_interpreter((3, 11, 2))
    _assert_supported_interpreter((3, 12, 0))


def test_the_interpreter_that_broke_the_ledger_read_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        _assert_supported_interpreter((3, 10, 18))
    assert excinfo.value.code != 0


def test_refusal_blames_the_interpreter_and_not_the_ledger():
    with pytest.raises(SystemExit) as excinfo:
        _assert_supported_interpreter((3, 10, 18))
    message = str(excinfo.value)
    assert "3.10.18" in message
    assert "requires Python" in message
    # The failure this replaces claimed the ledger was invalid. Say the opposite.
    assert "ledger is not the problem" in message.lower()


def test_minimum_matches_the_declared_requirement():
    assert MINIMUM_PYTHON == (3, 11)
