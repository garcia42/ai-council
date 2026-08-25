"""Pinned-vector and closed-surface proofs for the two local write builders.

The rendered vectors are pinned exactly, because the value of a closed builder
is that the set of commands it can emit is knowable, and a test that only checks
"contains --bare" would not establish that.
"""

from __future__ import annotations

import dataclasses
import inspect
import unittest

from council_tools.git_local_write_operations import (
    FORBIDDEN_FLAG_PREFIXES,
    GitWriteOperationError,
    InitializeBareRepository,
    WriteCanonicalBlob,
    assert_no_ambient_behaviour,
)
from council_tools.git_process_contract import RenderedInvocation


class InitializeTests(unittest.TestCase):
    def test_rendered_vector_is_pinned_exactly(self):
        self.assertEqual(
            InitializeBareRepository().render().argv(),
            ("init", "--bare", "--template=", "--object-format=sha1"),
        )

    def test_object_format_is_explicit_not_defaulted(self):
        # The protocol identifies objects by name, so an inherited default could
        # change stored names underneath it.
        self.assertIn("--object-format=sha1", InitializeBareRepository().render().argv())

    def test_template_is_empty_so_no_hook_is_installed(self):
        # A populated template directory installs hooks, which then execute.
        self.assertIn("--template=", InitializeBareRepository().render().argv())

    def test_takes_no_caller_parameters_at_all(self):
        # Nothing to supply means nothing to smuggle.
        self.assertEqual(
            [f.name for f in dataclasses.fields(InitializeBareRepository)], []
        )

    def test_renders_no_stdin_and_no_identity(self):
        rendered = InitializeBareRepository().render()
        self.assertEqual(rendered.stdin, b"")
        self.assertEqual(rendered.identity, {})

    def test_renders_no_global_options(self):
        self.assertEqual(InitializeBareRepository().render().global_options, ())

    def test_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            InitializeBareRepository().anything = 1  # type: ignore[attr-defined]


class WriteBlobTests(unittest.TestCase):
    def test_rendered_vector_is_pinned_exactly(self):
        self.assertEqual(
            WriteCanonicalBlob(b"payload").render().argv(),
            ("hash-object", "-w", "--stdin", "--"),
        )

    def test_content_crosses_as_stdin_bytes(self):
        self.assertEqual(WriteCanonicalBlob(b"payload").render().stdin, b"payload")

    def test_empty_content_is_permitted(self):
        self.assertEqual(WriteCanonicalBlob(b"").render().stdin, b"")

    def test_the_vector_does_not_change_with_content(self):
        # Content must never reach the argument vector, only stdin.
        first = WriteCanonicalBlob(b"a").render().argv()
        second = WriteCanonicalBlob(b"--upload-pack=evil").render().argv()
        self.assertEqual(first, second)

    def test_non_bytes_content_is_refused(self):
        for value in ("text", None, 1, bytearray(b"x"), memoryview(b"x"), True):
            with self.subTest(value=repr(value)[:30]):
                with self.assertRaises(GitWriteOperationError) as caught:
                    WriteCanonicalBlob(value)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.code, "invalid-content")

    def test_exposes_no_filename_path_or_mode_parameter(self):
        names = {f.name for f in dataclasses.fields(WriteCanonicalBlob)}
        self.assertEqual(names, {"content"})

    def test_is_frozen(self):
        blob = WriteCanonicalBlob(b"x")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            blob.content = b"y"  # type: ignore[misc]


class ClosedSurfaceTests(unittest.TestCase):
    def test_neither_builder_accepts_argv_options_or_a_repository_selector(self):
        forbidden = {
            "argv", "args", "options", "global_options", "subcommand", "config",
            "repository", "repository_path", "cwd", "git_dir", "environment", "flags",
        }
        for builder in (InitializeBareRepository, WriteCanonicalBlob):
            with self.subTest(builder=builder.__name__):
                names = {f.name for f in dataclasses.fields(builder)}
                self.assertEqual(names & forbidden, set())
                signature = inspect.signature(builder.render)
                self.assertEqual(list(signature.parameters), ["self"])

    def test_render_returns_the_shared_invocation_type(self):
        for builder in (InitializeBareRepository(), WriteCanonicalBlob(b"x")):
            with self.subTest(builder=type(builder).__name__):
                self.assertIs(type(builder.render()), RenderedInvocation)

    def test_no_rendered_vector_enables_ambient_behaviour(self):
        for builder in (InitializeBareRepository(), WriteCanonicalBlob(b"x")):
            with self.subTest(builder=type(builder).__name__):
                assert_no_ambient_behaviour(builder.render(), field="rendered")

    def test_the_guard_actually_catches_a_forbidden_flag(self):
        # Proving the guard is live, not vacuous.
        hostile = RenderedInvocation(
            subcommand="init", subcommand_args=("--template=/etc/hostile",)
        )
        with self.assertRaises(GitWriteOperationError) as caught:
            assert_no_ambient_behaviour(hostile, field="rendered")
        self.assertEqual(caught.exception.code, "forbidden-flag")

    def test_the_guard_catches_each_forbidden_prefix(self):
        for prefix in FORBIDDEN_FLAG_PREFIXES:
            if prefix == "--template":
                continue  # covered above; the empty spelling is the permitted one
            with self.subTest(prefix=prefix):
                hostile = RenderedInvocation(subcommand="init", subcommand_args=(prefix,))
                with self.assertRaises(GitWriteOperationError):
                    assert_no_ambient_behaviour(hostile, field="rendered")

    def test_the_empty_template_spelling_is_the_only_permitted_one(self):
        permitted = RenderedInvocation(subcommand="init", subcommand_args=("--template=",))
        assert_no_ambient_behaviour(permitted, field="rendered")


class PurityTests(unittest.TestCase):
    def test_module_exposes_no_spawn_or_io_capable_name(self):
        import council_tools.git_local_write_operations as module

        forbidden = {"subprocess", "os", "pathlib", "shutil", "socket", "io", "sys"}
        self.assertEqual(forbidden.intersection(vars(module)), set())

    def test_errors_are_typed_with_stable_codes_and_fields(self):
        with self.assertRaises(GitWriteOperationError) as caught:
            WriteCanonicalBlob("text")  # type: ignore[arg-type]
        self.assertTrue(issubclass(GitWriteOperationError, ValueError))
        self.assertEqual(caught.exception.code, "invalid-content")
        self.assertEqual(caught.exception.field, "content")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
