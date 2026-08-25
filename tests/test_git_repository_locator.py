"""Rejection and value-semantics proofs for BareRepositoryLocator.

Every rejection rule is proved by example, because the custody design binds
execution to an inode obtained from this location and a location that denotes
more than one place would undermine that binding before it starts.
"""

from __future__ import annotations

import copy
import dataclasses
import pickle
import unicodedata
import unittest

from council_tools.git_repository_locator import (
    MAX_REPOSITORY_PATH_LENGTH,
    BareRepositoryLocator,
    GitRepositoryLocatorError,
)

VALID = "/srv/ai-council/claims/claim.git"


class PathSubclass(str):
    """A str subclass, which must not stand in for the path text."""


class AcceptanceTests(unittest.TestCase):
    def test_accepts_a_normalized_absolute_path(self):
        self.assertEqual(BareRepositoryLocator(VALID).path, VALID)

    def test_path_round_trips_to_the_exact_input(self):
        for text in (VALID, "/a", "/a/b", "/srv/x.git", "/tmp/ai-council/one.git"):
            with self.subTest(text=text):
                locator = BareRepositoryLocator(text)
                self.assertEqual(locator.path, text)
                self.assertIs(type(locator.path), str)

    def test_accepts_non_ascii_that_is_already_nfc(self):
        text = "/srv/café/claim.git"
        self.assertEqual(unicodedata.normalize("NFC", text), text)
        self.assertEqual(BareRepositoryLocator(text).path, text)

    def test_accepts_a_segment_containing_dots_that_is_not_a_dot_segment(self):
        for text in ("/srv/a.b/claim.git", "/srv/...hidden/claim.git", "/srv/..a/x"):
            with self.subTest(text=text):
                self.assertEqual(BareRepositoryLocator(text).path, text)

    def test_accepts_the_length_bound(self):
        text = "/" + "a" * (MAX_REPOSITORY_PATH_LENGTH - 1)
        self.assertEqual(len(text), MAX_REPOSITORY_PATH_LENGTH)
        self.assertEqual(BareRepositoryLocator(text).path, text)


class RejectionTests(unittest.TestCase):
    def assert_code(self, text, code):
        with self.assertRaises(GitRepositoryLocatorError) as caught:
            BareRepositoryLocator(text)
        self.assertEqual(caught.exception.code, code, f"for {text!r}")

    def test_rejects_non_string_types(self):
        for value in (None, True, False, 0, 1, 3.5, b"/srv/x", bytearray(b"/srv/x"),
                      ["/srv/x"], ("/srv/x",), {"path": "/srv/x"}):
            with self.subTest(value=repr(value)[:40]):
                self.assert_code(value, "invalid-type")

    def test_rejects_a_str_subclass_even_when_it_spells_a_valid_path(self):
        self.assert_code(PathSubclass(VALID), "invalid-type")

    def test_rejects_empty_text(self):
        self.assert_code("", "empty-path")

    def test_rejects_text_over_the_length_bound(self):
        self.assert_code("/" + "a" * MAX_REPOSITORY_PATH_LENGTH, "path-too-long")

    def test_rejects_control_characters_anywhere(self):
        for character in ("\x00", "\r", "\n"):
            for text in (character + VALID, "/srv/" + character + "x", VALID + character):
                with self.subTest(text=repr(text)):
                    self.assert_code(text, "control-character")

    def test_rejects_an_alternate_separator(self):
        for text in ("/srv\\claims\\claim.git", "/srv/claims\\claim.git", "\\srv\\x"):
            with self.subTest(text=text):
                self.assert_code(text, "alternate-separator")

    def test_rejects_relative_paths(self):
        for text in ("srv/claim.git", "./claim.git", "../claim.git", "claim.git", "a"):
            with self.subTest(text=text):
                self.assert_code(text, "relative-path")

    def test_rejects_the_root_path_itself(self):
        self.assert_code("/", "root-path")

    def test_rejects_a_trailing_separator(self):
        for text in ("/srv/claims/", "/srv/", VALID + "/"):
            with self.subTest(text=text):
                self.assert_code(text, "trailing-separator")

    def test_rejects_a_doubled_separator(self):
        for text in ("//srv/claim.git", "/srv//claims/claim.git", "/srv/claims//claim.git"):
            with self.subTest(text=text):
                self.assert_code(text, "doubled-separator")

    def test_rejects_dot_and_dot_dot_segments(self):
        for text in ("/srv/./claim.git", "/srv/../claim.git", "/./x", "/../x",
                     "/srv/claims/..", "/srv/claims/."):
            with self.subTest(text=text):
                self.assert_code(text, "dot-segment")

    def test_rejects_text_that_is_not_nfc(self):
        # Same visible name, decomposed: it must not be accepted alongside the
        # composed spelling, or one repository would have two valid names.
        decomposed = "/srv/café/claim.git"
        self.assertNotEqual(unicodedata.normalize("NFC", decomposed), decomposed)
        self.assert_code(decomposed, "not-nfc")

    def test_rejects_text_that_cannot_round_trip_the_filesystem_encoding(self):
        # A lone surrogate cannot be encoded, so it could never be handed to a
        # syscall as the bytes it appears to spell.
        self.assert_code("/srv/\ud800/claim.git", "not-round-trippable")


class ErrorContractTests(unittest.TestCase):
    def test_error_is_typed_and_carries_a_stable_code_and_field(self):
        with self.assertRaises(GitRepositoryLocatorError) as caught:
            BareRepositoryLocator("relative")
        error = caught.exception
        self.assertEqual(error.code, "relative-path")
        self.assertEqual(error.field, "path")
        self.assertIn("relative-path", str(error))
        self.assertIn("path", str(error))

    def test_error_is_a_value_error_subclass_not_a_bare_value_error(self):
        self.assertTrue(issubclass(GitRepositoryLocatorError, ValueError))

    def test_a_type_failure_raises_the_typed_error_not_type_error(self):
        try:
            BareRepositoryLocator(b"/srv/x")
        except GitRepositoryLocatorError as error:
            self.assertEqual(error.code, "invalid-type")
        except TypeError:  # pragma: no cover - the failure this test exists to catch
            self.fail("a bare TypeError reached the caller")


class ValueSemanticsTests(unittest.TestCase):
    def test_is_immutable(self):
        locator = BareRepositoryLocator(VALID)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            locator.path = "/srv/other.git"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            del locator.path  # type: ignore[misc]

    def test_compares_by_value(self):
        self.assertEqual(BareRepositoryLocator(VALID), BareRepositoryLocator(VALID))
        self.assertNotEqual(BareRepositoryLocator(VALID), BareRepositoryLocator("/srv/b.git"))
        self.assertNotEqual(BareRepositoryLocator(VALID), VALID)

    def test_is_hashable_and_usable_as_a_mapping_key(self):
        first, second = BareRepositoryLocator(VALID), BareRepositoryLocator(VALID)
        self.assertEqual(hash(first), hash(second))
        mapping = {first: "one"}
        mapping[second] = "two"
        self.assertEqual(mapping, {first: "two"})
        self.assertEqual(len({first, second, BareRepositoryLocator("/srv/b.git")}), 2)

    def test_survives_copy_and_pickle_by_value(self):
        locator = BareRepositoryLocator(VALID)
        self.assertEqual(copy.copy(locator), locator)
        self.assertEqual(copy.deepcopy(locator), locator)
        self.assertEqual(pickle.loads(pickle.dumps(locator)), locator)

    def test_replace_revalidates_rather_than_bypassing_the_rules(self):
        locator = BareRepositoryLocator(VALID)
        self.assertEqual(
            dataclasses.replace(locator, path="/srv/other.git").path, "/srv/other.git"
        )
        with self.assertRaises(GitRepositoryLocatorError):
            dataclasses.replace(locator, path="../escape")


class PurityTests(unittest.TestCase):
    def test_module_exposes_no_io_capable_name(self):
        import council_tools.git_repository_locator as module

        forbidden = {"os", "io", "sys", "subprocess", "pathlib", "socket", "shutil", "tempfile"}
        present = forbidden.intersection(vars(module))
        self.assertEqual(present, set(), f"module exposes IO-capable names: {present}")

    def test_asserts_nothing_about_the_filesystem(self):
        # A path that certainly does not exist is still a valid locator: the
        # type is a naming claim, never a filesystem claim.
        self.assertEqual(
            BareRepositoryLocator("/nonexistent/nowhere/never.git").path,
            "/nonexistent/nowhere/never.git",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
