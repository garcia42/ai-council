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


from council_tools.git_local_write_operations import (  # noqa: E402
    CLAIM_ENTRY_MODE,
    CLAIM_ENTRY_NAME,
    CLAIM_ENTRY_TYPE,
    WRITE_OPERATIONS,
    CreateClaimTree,
    CreateZeroParentCommit,
)
from council_tools.git_object_id import Sha1ObjectId  # noqa: E402

BLOB = Sha1ObjectId("47d05ff6403c8e6c3cf635ea6eb9263738432773")
TREE = Sha1ObjectId("b439cdd182c0e51b28fa858fadcb3e9f71efb415")
DATE = "@1234567890 +0000"


def commit(**overrides):
    kwargs = {
        "tree_id": TREE, "message": b"claim",
        "author_name": "AI Council", "author_email": "ai@example.invalid",
        "author_date": DATE, "committer_name": "AI Council",
        "committer_email": "ai@example.invalid", "committer_date": DATE,
    }
    kwargs.update(overrides)
    return CreateZeroParentCommit(**kwargs)


class ClaimTreeTests(unittest.TestCase):
    def test_rendered_vector_is_pinned_and_carries_no_operand(self):
        self.assertEqual(CreateClaimTree(BLOB).render().argv(), ("mktree",))

    def test_the_entry_line_is_built_internally_from_fixed_values(self):
        self.assertEqual(
            CreateClaimTree(BLOB).entry_line(),
            b"100644 blob 47d05ff6403c8e6c3cf635ea6eb9263738432773\tclaim.json\n",
        )

    def test_the_only_caller_input_is_the_blob_identifier(self):
        # A caller able to choose the mode, name, type or entry count could
        # choose what the claim contains, which is the whole of it.
        self.assertEqual([f.name for f in dataclasses.fields(CreateClaimTree)], ["blob_id"])

    def test_no_caller_value_can_add_or_alter_an_entry(self):
        line = CreateClaimTree(BLOB).entry_line().decode()
        self.assertEqual(line.count("\n"), 1)
        self.assertIn(CLAIM_ENTRY_MODE, line)
        self.assertIn(CLAIM_ENTRY_TYPE, line)
        self.assertIn(CLAIM_ENTRY_NAME, line)

    def test_only_the_identifier_varies_between_renders(self):
        other = Sha1ObjectId("b" * 40)
        first, second = CreateClaimTree(BLOB).entry_line(), CreateClaimTree(other).entry_line()
        self.assertEqual(first.replace(BLOB.wire_text.encode(), b"X"),
                         second.replace(other.wire_text.encode(), b"X"))

    def test_a_raw_identifier_is_refused(self):
        with self.assertRaises(GitWriteOperationError) as caught:
            CreateClaimTree(BLOB.wire_text)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid-object-id")


class ZeroParentCommitTests(unittest.TestCase):
    def test_rendered_vector_is_pinned_exactly(self):
        self.assertEqual(
            commit().render().argv(), ("commit-tree", TREE.wire_text, "--")
        )

    def test_message_crosses_on_stdin_not_in_argv(self):
        # A message in argv could be read as an option; on stdin it cannot.
        hostile = commit(message=b"--upload-pack=evil")
        self.assertEqual(hostile.render().argv(), commit().render().argv())
        self.assertEqual(hostile.render().stdin, b"--upload-pack=evil")

    def test_no_parent_flag_and_no_parent_parameter(self):
        self.assertNotIn("-p", commit().render().argv())
        self.assertNotIn("parent", {f.name for f in dataclasses.fields(CreateZeroParentCommit)})

    def test_identity_travels_in_the_invocation_identity_mapping(self):
        identity = commit().render().identity
        self.assertEqual(
            set(identity),
            {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
             "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"},
        )

    def test_identical_inputs_render_byte_identical_commands(self):
        # A claim's object name must depend on what it says, not on when it was
        # made, so nothing here may read a clock or the environment.
        first, second = commit().render(), commit().render()
        self.assertEqual(first.argv(), second.argv())
        self.assertEqual(first.stdin, second.stdin)
        self.assertEqual(first.identity, second.identity)

    def test_the_measured_raw_date_form_is_required(self):
        # Measured on the pinned binary: "0 +0000" is rejected by git itself
        # ("invalid date format") while "@0 +0000" is accepted.
        for good in ("@0 +0000", "@1234567890 +0000", "@1234567890 -0500"):
            with self.subTest(date=good):
                self.assertEqual(commit(author_date=good).author_date, good)
        for bad in ("0 +0000", "1234567890 +0000", "now", "", "@0", "@abc +0000"):
            with self.subTest(date=bad):
                with self.assertRaises(GitWriteOperationError) as caught:
                    commit(author_date=bad)
                self.assertEqual(caught.exception.code, "invalid-identity-date")

    def test_identity_values_that_could_break_out_of_their_field_are_refused(self):
        for value in ("A\nB", "A\rB", "A\x00B", "A<b", "A>b", ""):
            for field_name in ("author_name", "author_email", "committer_name", "committer_email"):
                with self.subTest(value=repr(value), field=field_name):
                    with self.assertRaises(GitWriteOperationError) as caught:
                        commit(**{field_name: value})
                    self.assertEqual(caught.exception.code, "invalid-identity")

    def test_a_non_bytes_message_is_refused(self):
        with self.assertRaises(GitWriteOperationError) as caught:
            commit(message="text")
        self.assertEqual(caught.exception.code, "invalid-message")

    def test_a_raw_tree_identifier_is_refused(self):
        with self.assertRaises(GitWriteOperationError) as caught:
            commit(tree_id=TREE.wire_text)
        self.assertEqual(caught.exception.code, "invalid-object-id")


class WriteFamilyTests(unittest.TestCase):
    def test_the_family_is_exactly_four_operations(self):
        self.assertEqual(
            set(WRITE_OPERATIONS),
            {InitializeBareRepository, WriteCanonicalBlob, CreateClaimTree, CreateZeroParentCommit},
        )

    def test_no_new_operation_enables_ambient_behaviour(self):
        for built in (CreateClaimTree(BLOB), commit()):
            with self.subTest(operation=type(built).__name__):
                assert_no_ambient_behaviour(built.render(), field="rendered")

    def test_no_new_operation_accepts_argv_options_or_a_repository_selector(self):
        forbidden = {"argv", "args", "options", "global_options", "subcommand",
                     "config", "repository", "cwd", "git_dir", "flags", "parent", "mode"}
        for builder in (CreateClaimTree, CreateZeroParentCommit):
            with self.subTest(builder=builder.__name__):
                names = {f.name for f in dataclasses.fields(builder)}
                self.assertEqual(names & forbidden, set())
