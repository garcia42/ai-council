"""Proof that transport diagnostics are bounded, stable, and redacted."""

from __future__ import annotations

import unittest

from council_tools.git_transport_diagnostics import (
    LINE_TRUNCATION_MARKER,
    LINES_OMITTED_MARKER,
    MAX_LINE_LENGTH,
    MAX_LINES,
    MAX_TOTAL_LENGTH,
    REDACTION_MARKER,
    TOTAL_TRUNCATION_MARKER,
    redact_transport_diagnostics,
)


class ControlCharacterTests(unittest.TestCase):
    def test_every_control_character_except_the_separator_is_removed(self) -> None:
        for code in list(range(0x00, 0x20)) + [0x7F] + list(range(0x80, 0xA0)):
            character = chr(code)
            with self.subTest(code=hex(code)):
                result = redact_transport_diagnostics(f"a{character}b".encode())
                if character == "\n":
                    self.assertEqual(result, "a\nb")
                else:
                    self.assertEqual(result, "ab")

    def test_an_ansi_escape_sequence_cannot_survive(self) -> None:
        hostile = b"\x1b[31mfatal: remote said this\x1b[0m\n"
        result = redact_transport_diagnostics(hostile)
        self.assertNotIn("\x1b", result)
        self.assertIn("fatal: remote said this", result)

    def test_carriage_returns_from_progress_output_are_removed(self) -> None:
        result = redact_transport_diagnostics(b"Counting: 1\rCounting: 2\rdone\n")
        self.assertNotIn("\r", result)
        self.assertEqual(result, "Counting: 1Counting: 2done")


class DecodingTests(unittest.TestCase):
    def test_undecodable_bytes_do_not_raise(self) -> None:
        result = redact_transport_diagnostics(b"\xff\xfe fatal: bad\n")
        self.assertIn("fatal: bad", result)

    def test_a_non_bytes_input_is_rejected(self) -> None:
        for value in ("already text", None, 42, bytearray(b"x")):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError):
                    redact_transport_diagnostics(value)

    def test_empty_input_gives_empty_output(self) -> None:
        self.assertEqual(redact_transport_diagnostics(b""), "")


class BoundTests(unittest.TestCase):
    def test_a_long_line_is_truncated_visibly(self) -> None:
        result = redact_transport_diagnostics(b"x" * (MAX_LINE_LENGTH * 3))
        self.assertEqual(len(result), MAX_LINE_LENGTH)
        self.assertTrue(result.endswith(LINE_TRUNCATION_MARKER))

    def test_excess_lines_are_dropped_and_the_count_is_reported(self) -> None:
        raw = ("line\n" * (MAX_LINES + 7)).encode()
        result = redact_transport_diagnostics(raw)
        lines = result.split("\n")
        self.assertEqual(len(lines), MAX_LINES + 1)
        self.assertEqual(lines[-1], LINES_OMITTED_MARKER.format(count=7))

    def test_the_overall_length_is_bounded_visibly(self) -> None:
        # Many lines, each under the per-line bound, exceeding the total.
        raw = (("y" * 400) + "\n").encode() * (MAX_LINES)
        result = redact_transport_diagnostics(raw)
        self.assertLessEqual(len(result), MAX_TOTAL_LENGTH)
        if len(raw) > MAX_TOTAL_LENGTH:
            self.assertTrue(result.endswith(TOTAL_TRUNCATION_MARKER))

    def test_no_input_can_exceed_any_bound(self) -> None:
        for raw in (b"z" * 100_000, ("a" * 900 + "\n").encode() * 500, b"\n" * 5_000):
            with self.subTest(size=len(raw)):
                result = redact_transport_diagnostics(raw)
                self.assertLessEqual(len(result), MAX_TOTAL_LENGTH)
                for line in result.split("\n"):
                    self.assertLessEqual(len(line), MAX_LINE_LENGTH)

    def test_truncation_is_never_silent(self) -> None:
        result = redact_transport_diagnostics(b"q" * (MAX_LINE_LENGTH + 1))
        self.assertIn(LINE_TRUNCATION_MARKER, result)


class RedactionTests(unittest.TestCase):
    def assertWithheld(self, raw: bytes, secret: str) -> str:
        result = redact_transport_diagnostics(raw)
        self.assertNotIn(secret, result, "the secret survived redaction")
        self.assertIn(REDACTION_MARKER, result, "nothing marked the withholding")
        return result

    def test_url_userinfo_is_replaced_and_the_host_survives(self) -> None:
        # Deliberately NOT a recognised token shape: if the secret also matched
        # a credential pattern, this would pass with userinfo redaction removed
        # entirely, which is exactly what it did before.
        secret = "correcthorsebatterystaple"
        result = self.assertWithheld(
            f"fatal: could not read from https://user:{secret}@github.com/x/y.git\n".encode(),
            secret,
        )
        self.assertNotIn("user", result.split("github.com")[0].replace("could not", ""))
        # The host is the useful part of the message and must not be lost.
        self.assertIn("github.com/x/y.git", result)
        self.assertIn("https://", result)

    def test_each_recognised_credential_shape_is_replaced(self) -> None:
        cases = {
            "classic github token": "ghp_" + "B" * 36,
            "github oauth token": "gho_" + "C" * 36,
            "fine grained pat": "github_pat_" + "D" * 30,
            "aws access key": "AKIA" + "E" * 16,
        }
        for name, secret in cases.items():
            with self.subTest(shape=name):
                self.assertWithheld(f"remote: rejected {secret}\n".encode(), secret)

    def test_a_credential_run_together_with_other_text_is_replaced(self) -> None:
        """No word boundary before the token.

        A leading ``\\b`` fails here, because there is no boundary between a
        letter and the token's own first letter -- and a credential run together
        with surrounding text is exactly the case a reader would not spot.
        """

        secret = "ghp_" + "Y" * 36
        self.assertWithheld(f"remote:denied{secret}now\n".encode(), secret)
        self.assertWithheld(f"x{'AKIA' + 'B' * 16}y\n".encode(), "AKIA" + "B" * 16)

    def test_header_and_assignment_shapes_are_replaced(self) -> None:
        cases = {
            "authorization header": (b"Authorization: Bearer abcdef1234567890\n",
                                     "abcdef1234567890"),
            "bearer scheme": (b"sent bearer abcdef1234567890 upstream\n",
                              "abcdef1234567890"),
            "password assignment": (b"password=hunter2hunter2\n", "hunter2hunter2"),
            "api key assignment": (b"api_key: zyxwvut987654\n", "zyxwvut987654"),
        }
        for name, (raw, secret) in cases.items():
            with self.subTest(shape=name):
                self.assertWithheld(raw, secret)

    def test_a_private_key_header_is_replaced(self) -> None:
        result = redact_transport_diagnostics(b"-----BEGIN OPENSSH PRIVATE KEY-----\n")
        self.assertIn(REDACTION_MARKER, result)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", result)

    def test_redaction_replaces_rather_than_deletes(self) -> None:
        # A sentence with a hole in it reads as if nothing was there.
        result = redact_transport_diagnostics(b"token=abcdefgh12345678 was rejected\n")
        self.assertIn(REDACTION_MARKER, result)
        self.assertIn("was rejected", result)

    def test_ordinary_text_is_left_alone(self) -> None:
        raw = b"fatal: repository 'https://github.com/x/y.git' not found\n"
        self.assertEqual(
            redact_transport_diagnostics(raw),
            "fatal: repository 'https://github.com/x/y.git' not found",
        )

    def test_a_secret_cannot_be_recovered_from_the_result(self) -> None:
        secret = "ghp_" + "F" * 36
        result = redact_transport_diagnostics(
            f"https://x:{secret}@h/r.git and again {secret}\n".encode()
        )
        for length in (len(secret), 20, 12):
            self.assertNotIn(secret[:length], result)


class RedactionOrderingTests(unittest.TestCase):
    """Redaction must run before per-line truncation, not after.

    If truncation ran first, a credential straddling the per-line bound would be
    cut into a prefix short enough that no pattern matches it, and that prefix
    would then survive into the output as plaintext. Redacting first replaces
    the whole value before anything is cut.
    """

    def test_a_credential_straddling_the_line_bound_leaves_no_prefix(self) -> None:
        secret = "ghp_" + "Z" * 36
        # Truncation keeps MAX_LINE_LENGTH - len(marker) characters, so the
        # secret starts ten characters before that point: exactly ten of it
        # would survive a cut -- fewer than any pattern's minimum, so a
        # post-truncation pass would miss the remnant.
        prefix_length = MAX_LINE_LENGTH - len(LINE_TRUNCATION_MARKER) - 10
        raw = ("p" * prefix_length + secret + "\n").encode()
        result = redact_transport_diagnostics(raw)
        self.assertNotIn(secret, result)
        self.assertNotIn(secret[:10], result)
        self.assertNotIn("ghp_", result)


class BacktrackingTests(unittest.TestCase):
    """A remote must not be able to buy CPU time with a long line.

    The URL pattern originally used an unbounded scheme repetition, so the
    engine consumed the whole line at every start position and backtracked out
    again: 8.8s for one 160,000-character line, growing with the square. That is
    a denial of service in the module whose job is making untrusted output safe.

    The bound below is ~1000x the fixed cost, so this is not a race with the
    scheduler: it fails only if the quadratic behaviour returns.
    """

    def test_a_long_hostile_line_is_processed_promptly(self) -> None:
        import time

        raw = (b"z" * 200_000) + b"\n"
        started = time.monotonic()
        result = redact_transport_diagnostics(raw)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, "redaction time is superlinear again")
        self.assertLessEqual(len(result), MAX_TOTAL_LENGTH)

    def test_many_long_lines_are_processed_promptly(self) -> None:
        import time

        raw = ((b"a" * 50_000) + b"\n") * 200
        started = time.monotonic()
        redact_transport_diagnostics(raw)
        self.assertLess(time.monotonic() - started, 5.0)


class StabilityTests(unittest.TestCase):
    def test_identical_input_gives_identical_output(self) -> None:
        raw = b"fatal: could not read from https://u:p@h/r.git\nremote: denied\n"
        results = {redact_transport_diagnostics(raw) for _ in range(25)}
        self.assertEqual(len(results), 1)

    def test_output_is_stable_across_a_process_boundary(self) -> None:
        import os
        import subprocess
        import sys

        raw = b"remote: token=abcdefgh12345678\nfatal: \x1b[31mdenied\x1b[0m\n"
        source = (
            "import sys\n"
            "from council_tools.git_transport_diagnostics import "
            "redact_transport_diagnostics\n"
            f"sys.stdout.write(redact_transport_diagnostics({raw!r}))\n"
        )
        child = subprocess.run(
            [sys.executable, "-c", source],
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": os.pathsep.join(sys.path),
                "PYTHONHASHSEED": "424242",
                "LC_ALL": "tr_TR.UTF-8",
                "TZ": "Asia/Tokyo",
            },
        )
        self.assertEqual(child.returncode, 0, child.stderr.decode())
        self.assertEqual(child.stdout.decode(), redact_transport_diagnostics(raw))


class HonestyTests(unittest.TestCase):
    def test_the_module_states_that_redaction_is_incomplete(self) -> None:
        """The contract requires the limitation to be stated, so it is asserted.

        A caller who believes redaction is sufficient stops maintaining the
        control that actually prevents a leak, so this sentence is load-bearing
        rather than decorative.
        """

        from council_tools import git_transport_diagnostics

        text = git_transport_diagnostics.__doc__ or ""
        self.assertIn("second line of defence", text)
        self.assertIn("never complete", text)
        # And it must name the control that is the first line.
        self.assertIn("BASE_CHILD_ENVIRONMENT", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
