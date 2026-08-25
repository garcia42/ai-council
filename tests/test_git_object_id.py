"""Exhaustive rejection and value-semantics proofs for Sha1ObjectId.

The contract for this module requires every rejection rule to be proved by
example rather than by construction, because the type is the operand defence
that the pinned Git binary's option terminator cannot provide.
"""

from __future__ import annotations

import copy
import dataclasses
import pickle
import unittest

from council_tools.git_object_id import (
    SHA1_HEX_LENGTH,
    GitObjectIdError,
    Sha1ObjectId,
)


VALID = "0dee7875f2f930495d890b8fce0ee0c6229fb23380e034cd86ce4075a1046b4b"[:40]
OTHER = "5005007fe89e082c994a45a1d527cfc136c35cc9"


class WireTextSubclass(str):
    """A str subclass, which must not be accepted as the wire text."""


class Sha1ObjectIdAcceptanceTests(unittest.TestCase):
    def test_accepts_exact_lowercase_forty_hex(self) -> None:
        self.assertEqual(Sha1ObjectId(OTHER).wire_text, OTHER)

    def test_wire_text_round_trips_to_the_exact_input(self) -> None:
        for text in (OTHER, "0" * 40, "f" * 40, VALID):
            with self.subTest(text=text):
                identifier = Sha1ObjectId(text)
                self.assertEqual(identifier.wire_text, text)
                self.assertIs(type(identifier.wire_text), str)

    def test_all_hex_digits_are_accepted(self) -> None:
        text = ("0123456789abcdef" * 3)[:40]
        self.assertEqual(len(text), SHA1_HEX_LENGTH)
        self.assertEqual(Sha1ObjectId(text).wire_text, text)


class Sha1ObjectIdTypeRejectionTests(unittest.TestCase):
    def test_rejects_non_string_types(self) -> None:
        for value in (None, True, False, 0, 1, 3.5, b"a" * 40, bytearray(b"a" * 40),
                      ["a" * 40], ("a" * 40,), {"wire_text": "a" * 40}):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(GitObjectIdError) as caught:
                    Sha1ObjectId(value)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.code, "invalid-type")

    def test_rejects_str_subclass_even_when_it_spells_a_valid_identifier(self) -> None:
        with self.assertRaises(GitObjectIdError) as caught:
            Sha1ObjectId(WireTextSubclass(OTHER))
        self.assertEqual(caught.exception.code, "invalid-type")

    def test_bool_is_rejected_as_a_type_not_coerced(self) -> None:
        # bool is an int subclass; it must not slip through any numeric path.
        with self.assertRaises(GitObjectIdError) as caught:
            Sha1ObjectId(True)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid-type")


class Sha1ObjectIdShapeRejectionTests(unittest.TestCase):
    def assert_code(self, text: str, code: str) -> None:
        with self.assertRaises(GitObjectIdError) as caught:
            Sha1ObjectId(text)
        self.assertEqual(caught.exception.code, code, f"for {text!r}")

    def test_rejects_control_characters_anywhere(self) -> None:
        for character in ("\x00", "\r", "\n"):
            for text in (character + OTHER[1:], OTHER[:20] + character + OTHER[21:],
                         OTHER + character):
                with self.subTest(text=repr(text)):
                    self.assert_code(text, "control-character")

    def test_rejects_whitespace_leading_trailing_and_interior(self) -> None:
        for text in (" " + OTHER, OTHER + " ", OTHER[:20] + " " + OTHER[21:],
                     "\t" + OTHER, OTHER + "\t", OTHER[:20] + "\x0b" + OTHER[21:]):
            with self.subTest(text=repr(text)):
                self.assert_code(text, "contains-whitespace")

    def test_rejects_a_leading_dash_so_it_cannot_be_read_as_an_option(self) -> None:
        for text in ("-" + OTHER[1:], "--" + OTHER[2:], "-h", "--help",
                     "--upload-pack=evil"):
            with self.subTest(text=text):
                self.assert_code(text, "leading-dash")

    def test_a_value_violating_several_rules_is_still_rejected(self) -> None:
        # Rules are ordered, so a value that breaks more than one is reported
        # against the first that fires.  Which code wins is an implementation
        # detail; that it is rejected is the guarantee.
        for text in ("--upload-pack=touch /tmp/pwned", "-- HEAD^", "\n--help"):
            with self.subTest(text=repr(text)):
                with self.assertRaises(GitObjectIdError):
                    Sha1ObjectId(text)

    def test_rejects_path_syntax(self) -> None:
        for text in ("../" + OTHER[3:], "./" + OTHER[2:], ".git", "a/b",
                     OTHER[:20] + "/" + OTHER[21:], OTHER[:20] + "\\" + OTHER[21:]):
            with self.subTest(text=text):
                self.assert_code(text, "path-syntax")

    def test_rejects_revision_syntax_including_a_valid_identifier_with_a_suffix(self) -> None:
        for text in (OTHER + "^", OTHER + "~1", OTHER + ":path", OTHER + "^{tree}",
                     OTHER + "@{0}", OTHER[:20] + "^" + OTHER[21:], OTHER + "[a]",
                     OTHER + "*", OTHER + "?"):
            with self.subTest(text=text):
                self.assert_code(text, "revision-syntax")

    def test_rejects_wrong_length(self) -> None:
        for text in ("", "a", "0" * 39, "0" * 41, "0" * 64, "HEAD".lower(), "abc"):
            with self.subTest(text=text):
                self.assert_code(text, "invalid-length")

    def test_rejects_uppercase_hex_distinctly_from_non_hex(self) -> None:
        self.assert_code(OTHER.upper(), "uppercase-hex")
        self.assert_code("A" + OTHER[1:], "uppercase-hex")
        self.assert_code(OTHER[:39] + "F", "uppercase-hex")

    def test_rejects_non_hex_characters_at_the_right_length(self) -> None:
        for text in ("g" * 40, "z" + OTHER[1:], OTHER[:39] + "g", "-" .join(("0" * 20, "0" * 19))):
            with self.subTest(text=text):
                with self.assertRaises(GitObjectIdError) as caught:
                    Sha1ObjectId(text)
                self.assertIn(caught.exception.code, {"non-hex-character", "leading-dash"})

    def test_rejects_a_reference_name_and_head(self) -> None:
        for text in ("HEAD", "refs/heads/main", "main", "origin/main"):
            with self.subTest(text=text):
                with self.assertRaises(GitObjectIdError):
                    Sha1ObjectId(text)

    def test_rejects_range_forms_by_naming_the_seam(self) -> None:
        # A range form is rejected for being revision syntax, not incidentally
        # for being the wrong length, so the code stays diagnostic.
        for text in (f"{OTHER}..{OTHER}", f"{OTHER}...{OTHER}", "a..b",
                     OTHER[:20] + "." + OTHER[21:]):
            with self.subTest(text=text):
                self.assert_code(text, "revision-syntax")

    def test_a_leading_dot_is_path_syntax_not_revision_syntax(self) -> None:
        # Ordering matters: a leading dot is a path seam and is reported as one.
        self.assert_code("." + OTHER[1:], "path-syntax")
        self.assert_code("../etc/passwd", "path-syntax")


class Sha1ObjectIdErrorContractTests(unittest.TestCase):
    def test_error_is_typed_and_carries_a_stable_code_and_field(self) -> None:
        with self.assertRaises(GitObjectIdError) as caught:
            Sha1ObjectId("nope")
        error = caught.exception
        self.assertEqual(error.code, "invalid-length")
        self.assertEqual(error.field, "wire_text")
        self.assertIn("invalid-length", str(error))
        self.assertIn("wire_text", str(error))

    def test_error_is_a_value_error_subclass_not_a_bare_value_error(self) -> None:
        self.assertTrue(issubclass(GitObjectIdError, ValueError))
        with self.assertRaises(GitObjectIdError):
            Sha1ObjectId("nope")

    def test_a_type_failure_raises_the_typed_error_not_type_error(self) -> None:
        try:
            Sha1ObjectId(b"0" * 40)  # type: ignore[arg-type]
        except GitObjectIdError as error:
            self.assertEqual(error.code, "invalid-type")
        except TypeError:  # pragma: no cover - the failure this test exists to catch
            self.fail("a bare TypeError reached the caller")


class Sha1ObjectIdValueSemanticsTests(unittest.TestCase):
    def test_is_immutable(self) -> None:
        identifier = Sha1ObjectId(OTHER)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            identifier.wire_text = VALID  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            del identifier.wire_text  # type: ignore[misc]

    def test_compares_by_value(self) -> None:
        self.assertEqual(Sha1ObjectId(OTHER), Sha1ObjectId(OTHER))
        self.assertNotEqual(Sha1ObjectId(OTHER), Sha1ObjectId(VALID))
        self.assertNotEqual(Sha1ObjectId(OTHER), OTHER)

    def test_is_hashable_and_usable_as_a_mapping_key(self) -> None:
        first, second = Sha1ObjectId(OTHER), Sha1ObjectId(OTHER)
        self.assertEqual(hash(first), hash(second))
        mapping = {first: "one"}
        mapping[second] = "two"
        self.assertEqual(mapping, {first: "two"})
        self.assertEqual(len({first, second, Sha1ObjectId(VALID)}), 2)

    def test_survives_copy_and_pickle_by_value(self) -> None:
        identifier = Sha1ObjectId(OTHER)
        self.assertEqual(copy.copy(identifier), identifier)
        self.assertEqual(copy.deepcopy(identifier), identifier)
        self.assertEqual(pickle.loads(pickle.dumps(identifier)), identifier)

    def test_replace_revalidates_rather_than_bypassing_the_rules(self) -> None:
        identifier = Sha1ObjectId(OTHER)
        self.assertEqual(dataclasses.replace(identifier, wire_text=VALID).wire_text, VALID)
        with self.assertRaises(GitObjectIdError):
            dataclasses.replace(identifier, wire_text="HEAD")

    def test_defines_no_implicit_string_conversion(self) -> None:
        # Command construction must reach for .wire_text explicitly, so that an
        # unvalidated value cannot enter an argument vector by stringification.
        self.assertIs(type(Sha1ObjectId(OTHER)).__str__, object.__str__)


class Sha1ObjectIdPurityTests(unittest.TestCase):
    def test_module_imports_no_io_capable_module(self) -> None:
        import council_tools.git_object_id as module

        forbidden = {"os", "io", "sys", "subprocess", "pathlib", "socket", "shutil", "tempfile"}
        present = forbidden.intersection(vars(module))
        self.assertEqual(present, set(), f"module exposes IO-capable names: {present}")

    def test_asserts_nothing_about_existence(self) -> None:
        # An identifier for an object that certainly does not exist is still valid:
        # the type is a wire-format claim, never a repository claim.
        self.assertEqual(Sha1ObjectId("d" * 40).wire_text, "d" * 40)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
