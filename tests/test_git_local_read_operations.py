"""Pinned-vector and closed-selector proofs for the four typed object reads.

Vectors are pinned exactly, and the replacement-object flag's *position* is
asserted rather than its mere presence, because the spike measured that it
defends only from the Git-global region.
"""

from __future__ import annotations

import dataclasses
import inspect
import unittest

from council_tools.git_local_read_operations import (
    NO_REPLACE_OBJECTS,
    READ_OPERATIONS,
    SELECTOR_READS,
    ObserveObjectFormat,
    GitReadOperationError,
    ListTree,
    ReadBlobBytes,
    ReadCommitBytes,
    ReadObjectType,
)
from council_tools.git_object_id import Sha1ObjectId
from council_tools.git_process_contract import RenderedInvocation

OID = Sha1ObjectId("5005007fe89e082c994a45a1d527cfc136c35cc9")
WIRE = "5005007fe89e082c994a45a1d527cfc136c35cc9"


class PinnedVectorTests(unittest.TestCase):
    def test_each_rendered_vector_is_pinned_exactly(self):
        expected = {
            ReadObjectType: (NO_REPLACE_OBJECTS, "cat-file", "-t", "--", WIRE),
            ReadCommitBytes: (NO_REPLACE_OBJECTS, "cat-file", "commit", "--", WIRE),
            ReadBlobBytes: (NO_REPLACE_OBJECTS, "cat-file", "blob", "--", WIRE),
            ListTree: (NO_REPLACE_OBJECTS, "ls-tree", "-z", WIRE, "--"),
        }
        for builder, vector in expected.items():
            with self.subTest(builder=builder.__name__):
                self.assertEqual(builder(OID).render().argv(), vector)

    def test_the_selector_family_is_exactly_four_reads(self):
        self.assertEqual(
            set(SELECTOR_READS), {ReadObjectType, ReadCommitBytes, ReadBlobBytes, ListTree}
        )

    def test_the_full_family_adds_only_the_selector_free_read(self):
        self.assertEqual(set(READ_OPERATIONS) - set(SELECTOR_READS), {ObserveObjectFormat})


class ReplacementObjectDefenceTests(unittest.TestCase):
    def test_every_read_carries_the_flag(self):
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                self.assertIn(NO_REPLACE_OBJECTS, builder(OID).render().argv())

    def test_the_flag_is_in_the_git_global_region_not_a_subcommand_flag(self):
        # Position is the whole point: measured on the pinned binary, the flag
        # defends only from the Git-global region.
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                rendered = builder(OID).render()
                self.assertEqual(rendered.global_options, (NO_REPLACE_OBJECTS,))
                self.assertNotIn(NO_REPLACE_OBJECTS, rendered.subcommand_args)

    def test_the_flag_precedes_the_subcommand_in_the_vector(self):
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                argv = builder(OID).render().argv()
                self.assertLess(argv.index(NO_REPLACE_OBJECTS), argv.index(builder(OID).render().subcommand))


class ClosedSelectorTests(unittest.TestCase):
    def test_a_raw_string_selector_is_refused(self):
        # The unavailable terminator cannot close the operand seam, so the
        # typed identifier has to.
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(GitReadOperationError) as caught:
                    builder(WIRE)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.code, "invalid-object-id")

    def test_hostile_selectors_are_refused_before_they_can_be_typed(self):
        for hostile in ("HEAD", WIRE + "^", "--upload-pack=evil", "../etc/passwd",
                        WIRE + ":path", "refs/heads/main", WIRE + ".." + WIRE):
            with self.subTest(hostile=hostile):
                # They cannot even become a Sha1ObjectId, which is the defence.
                with self.assertRaises(ValueError):
                    Sha1ObjectId(hostile)
                with self.assertRaises(GitReadOperationError):
                    ReadObjectType(hostile)  # type: ignore[arg-type]

    def test_non_identifier_types_are_refused(self):
        for value in (None, 1, True, b"a" * 40, ["a" * 40], object()):
            with self.subTest(value=repr(value)[:30]):
                with self.assertRaises(GitReadOperationError):
                    ReadBlobBytes(value)  # type: ignore[arg-type]

    def test_no_builder_accepts_argv_options_formats_or_pathspecs(self):
        forbidden = {
            "argv", "args", "options", "global_options", "subcommand", "config",
            "repository", "cwd", "git_dir", "format", "output_format",
            "separator", "pathspec", "paths", "flags", "revision",
        }
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                names = {f.name for f in dataclasses.fields(builder)}
                self.assertEqual(names, {"object_id"})
                self.assertEqual(list(inspect.signature(builder.render).parameters), ["self"])

    def test_the_vector_depends_only_on_the_selector(self):
        other = Sha1ObjectId("b" * 40)
        first, second = ReadBlobBytes(OID).render().argv(), ReadBlobBytes(other).render().argv()
        self.assertEqual(len(first), len(second))
        self.assertEqual(first[:-1], second[:-1])


class TreeListingTests(unittest.TestCase):
    def test_output_is_nul_delimited(self):
        # An entry name may contain whitespace or a separator, so a
        # human-readable listing cannot be parsed back unambiguously.
        self.assertIn("-z", ListTree(OID).render().argv())

    def test_renders_only_the_measured_form(self):
        # No flag beyond what the spike measured on the pinned binary.
        self.assertEqual(
            ListTree(OID).render().subcommand_args, ("-z", WIRE, "--")
        )


class ValueSemanticsTests(unittest.TestCase):
    def test_builders_are_frozen_and_compare_by_value(self):
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                one, two = builder(OID), builder(OID)
                self.assertEqual(one, two)
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    one.object_id = Sha1ObjectId("b" * 40)  # type: ignore[misc]

    def test_render_returns_the_shared_invocation_type(self):
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                self.assertIs(type(builder(OID).render()), RenderedInvocation)

    def test_no_read_sends_anything_on_standard_input(self):
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                self.assertEqual(builder(OID).render().stdin, b"")


class PurityTests(unittest.TestCase):
    def test_module_exposes_no_spawn_or_io_capable_name(self):
        import council_tools.git_local_read_operations as module

        forbidden = {"subprocess", "os", "pathlib", "shutil", "socket", "io", "sys"}
        self.assertEqual(forbidden.intersection(vars(module)), set())

    def test_no_selector_read_observes_the_object_format(self):
        # The selector reads and the format read stay distinct commands.
        for builder in SELECTOR_READS:
            with self.subTest(builder=builder.__name__):
                self.assertNotIn("rev-parse", builder(OID).render().argv())

    def test_errors_are_typed_with_stable_codes_and_fields(self):
        with self.assertRaises(GitReadOperationError) as caught:
            ReadObjectType("nope")  # type: ignore[arg-type]
        self.assertTrue(issubclass(GitReadOperationError, ValueError))
        self.assertEqual(caught.exception.code, "invalid-object-id")
        self.assertEqual(caught.exception.field, "object_id")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ObjectFormatReadTests(unittest.TestCase):
    def test_rendered_vector_is_pinned_exactly(self):
        self.assertEqual(
            ObserveObjectFormat().render().argv(), ("rev-parse", "--show-object-format")
        )

    def test_takes_no_caller_parameters_at_all(self):
        # There is no selector here, so no typed operand carries the defence.
        # Accepting nothing is what makes it safe.
        self.assertEqual([f.name for f in dataclasses.fields(ObserveObjectFormat)], [])
        self.assertEqual(
            list(inspect.signature(ObserveObjectFormat.render).parameters), ["self"]
        )

    def test_the_option_terminator_is_absent_on_purpose(self):
        # Measured on the pinned binary: rev-parse consumes --end-of-options as
        # an operand, so including it would ask a different question.
        argv = ObserveObjectFormat().render().argv()
        self.assertNotIn("--end-of-options", argv)
        self.assertNotIn("--", argv)

    def test_carries_no_replacement_flag_because_it_reads_no_object(self):
        self.assertNotIn(NO_REPLACE_OBJECTS, ObserveObjectFormat().render().argv())

    def test_sends_nothing_on_standard_input(self):
        self.assertEqual(ObserveObjectFormat().render().stdin, b"")

    def test_compares_by_value_and_is_frozen(self):
        self.assertEqual(ObserveObjectFormat(), ObserveObjectFormat())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ObserveObjectFormat().anything = 1  # type: ignore[attr-defined]

    def test_does_not_compare_the_format_to_an_expectation(self):
        # Observation only: the comparison belongs to whoever holds the
        # expectation, and this module holds none.
        import council_tools.git_local_read_operations as module

        self.assertNotIn("sha1", inspect.getsource(module.ObserveObjectFormat.render))
