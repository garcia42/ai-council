"""Structural proofs for the Git process records.

The two rules with measured motivation — an environment built from scratch and
an explicitly named descriptor set — are proved directly, because both were
demonstrated to leak on the pinned binary when left implicit.
"""

from __future__ import annotations

import dataclasses
import os
import unittest

from council_tools.git_process_contract import (
    BASE_CHILD_ENVIRONMENT,
    DEFAULT_EXPLICIT_ENVIRONMENT_KEYS,
    EXECUTOR_OBLIGATIONS,
    MAX_ARGUMENT_COUNT,
    MAX_STDIN_BYTES,
    GitCommand,
    GitCommandResult,
    GitProcessError,
    GitProcessPolicy,
    RenderedInvocation,
)

GIT = "/usr/bin/git"


def invocation(**overrides):
    kwargs = {
        "global_options": ("--no-replace-objects",),
        "subcommand": "cat-file",
        "subcommand_args": ("-t", "a" * 40),
        "stdin": b"",
        "identity": {},
    }
    kwargs.update(overrides)
    return RenderedInvocation(**kwargs)


def command(**overrides):
    kwargs = {
        "executable": GIT,
        "invocation": invocation(),
        "environment": dict(BASE_CHILD_ENVIRONMENT),
        "inherited_descriptors": (),
    }
    kwargs.update(overrides)
    return GitCommand(**kwargs)


class EnvironmentIsBuiltFromScratchTests(unittest.TestCase):
    def test_child_environment_contains_no_ambient_value(self):
        # The measured failure: a child inheriting the ambient environment read
        # a hostile user configuration.  Starting empty makes that unreachable.
        os.environ["AI_COUNCIL_AMBIENT_PROBE"] = "leaked"
        try:
            environment = GitProcessPolicy().child_environment()
        finally:
            del os.environ["AI_COUNCIL_AMBIENT_PROBE"]
        self.assertNotIn("AI_COUNCIL_AMBIENT_PROBE", environment)
        self.assertEqual(set(environment), set(BASE_CHILD_ENVIRONMENT))

    def test_the_whole_suppression_set_is_present(self):
        # Row 7 of the spike: omitting any one of these still leaked.
        environment = GitProcessPolicy().child_environment()
        for key in (
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM",
            "HOME",
            "XDG_CONFIG_HOME",
        ):
            with self.subTest(key=key):
                self.assertIn(key, environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(environment["GIT_CONFIG_SYSTEM"], "/dev/null")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")

    def test_locale_and_prompting_are_pinned(self):
        environment = GitProcessPolicy().child_environment()
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_permitted_explicit_keys_are_added(self):
        environment = GitProcessPolicy().child_environment(
            {"GIT_AUTHOR_NAME": "AI Council", "GIT_AUTHOR_DATE": "0 +0000"}
        )
        self.assertEqual(environment["GIT_AUTHOR_NAME"], "AI Council")
        self.assertEqual(environment["GIT_AUTHOR_DATE"], "0 +0000")

    def test_an_unpermitted_key_is_refused(self):
        for key in ("SSH_AUTH_SOCK", "GIT_SSH_COMMAND", "PATH", "LD_PRELOAD", "HOME"):
            with self.subTest(key=key):
                with self.assertRaises(GitProcessError) as caught:
                    GitProcessPolicy().child_environment({key: "x"})
                self.assertIn(
                    caught.exception.code,
                    {"environment-key-not-permitted", "explicit-key-shadows-base"},
                )

    def test_transport_keys_are_absent_from_the_shipped_default(self):
        # A later, separately reviewed transport policy widens this set.
        self.assertNotIn("SSH_AUTH_SOCK", DEFAULT_EXPLICIT_ENVIRONMENT_KEYS)
        self.assertNotIn("GIT_SSH_COMMAND", DEFAULT_EXPLICIT_ENVIRONMENT_KEYS)

    def test_explicit_keys_may_be_widened_by_construction(self):
        policy = GitProcessPolicy(
            explicit_keys=DEFAULT_EXPLICIT_ENVIRONMENT_KEYS | {"SSH_AUTH_SOCK"}
        )
        self.assertEqual(
            policy.child_environment({"SSH_AUTH_SOCK": "/tmp/agent"})["SSH_AUTH_SOCK"],
            "/tmp/agent",
        )

    def test_explicit_keys_cannot_shadow_the_suppression_set(self):
        for key in ("GIT_CONFIG_GLOBAL", "HOME", "XDG_CONFIG_HOME"):
            with self.subTest(key=key):
                with self.assertRaises(GitProcessError) as caught:
                    GitProcessPolicy(explicit_keys=frozenset({key}))
                self.assertEqual(caught.exception.code, "explicit-key-shadows-base")

    def test_each_call_returns_a_fresh_mapping(self):
        policy = GitProcessPolicy()
        first = policy.child_environment()
        first["GIT_CONFIG_GLOBAL"] = "/etc/hostile"
        self.assertEqual(policy.child_environment()["GIT_CONFIG_GLOBAL"], "/dev/null")


class InvocationTests(unittest.TestCase):
    def test_argv_is_global_options_then_subcommand_then_args(self):
        self.assertEqual(
            invocation().argv(),
            ("--no-replace-objects", "cat-file", "-t", "a" * 40),
        )

    def test_an_empty_subcommand_is_refused(self):
        with self.assertRaises(GitProcessError) as caught:
            invocation(subcommand="")
        self.assertEqual(caught.exception.code, "empty-subcommand")

    def test_non_text_arguments_are_refused(self):
        with self.assertRaises(GitProcessError):
            invocation(global_options=(None,))
        with self.assertRaises(GitProcessError):
            invocation(subcommand_args=(1,))

    def test_nul_in_an_argument_is_refused(self):
        with self.assertRaises(GitProcessError) as caught:
            invocation(subcommand_args=("a\x00b",))
        self.assertEqual(caught.exception.code, "control-character")

    def test_stdin_must_be_bytes_and_bounded(self):
        with self.assertRaises(GitProcessError) as caught:
            invocation(stdin="text")
        self.assertEqual(caught.exception.code, "invalid-type")
        with self.assertRaises(GitProcessError) as caught:
            invocation(stdin=b"x" * (MAX_STDIN_BYTES + 1))
        self.assertEqual(caught.exception.code, "too-long")

    def test_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            invocation().subcommand = "init"  # type: ignore[misc]


class CommandTests(unittest.TestCase):
    def test_executable_is_separate_from_argv(self):
        built = command()
        self.assertEqual(built.executable, GIT)
        self.assertNotIn(GIT, built.argv)

    def test_argv_is_the_complete_post_executable_vector(self):
        self.assertEqual(
            command().argv, ("--no-replace-objects", "cat-file", "-t", "a" * 40)
        )

    def test_a_relative_executable_is_refused(self):
        for value in ("git", "./git", "usr/bin/git", "/usr/bin/"):
            with self.subTest(value=value):
                with self.assertRaises(GitProcessError) as caught:
                    command(executable=value)
                self.assertEqual(caught.exception.code, "executable-not-absolute")

    def test_descriptors_must_be_non_negative_integers(self):
        for value in ((-1,), (True,), ("3",), (None,), (3.0,)):
            with self.subTest(value=repr(value)):
                with self.assertRaises(GitProcessError) as caught:
                    command(inherited_descriptors=value)
                self.assertEqual(caught.exception.code, "invalid-descriptor")

    def test_duplicate_descriptors_are_refused(self):
        with self.assertRaises(GitProcessError) as caught:
            command(inherited_descriptors=(7, 7))
        self.assertEqual(caught.exception.code, "duplicate-descriptor")

    def test_a_descriptor_set_is_accepted_and_ordered_as_given(self):
        self.assertEqual(command(inherited_descriptors=(9, 3)).inherited_descriptors, (9, 3))

    def test_an_over_long_argument_vector_is_refused(self):
        with self.assertRaises(GitProcessError) as caught:
            command(invocation=invocation(subcommand_args=tuple("a" for _ in range(MAX_ARGUMENT_COUNT + 1))))
        self.assertEqual(caught.exception.code, "too-long")

    def test_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            command().executable = "/bin/sh"  # type: ignore[misc]


class ResultTests(unittest.TestCase):
    def test_refuses_boolean_coercion(self):
        # A result read by truthiness is a result read without looking at the
        # exit status, which is how a failed call becomes a silent success.
        for result in (
            GitCommandResult(exit_status=0),
            GitCommandResult(exit_status=1, stderr=b"fatal"),
            GitCommandResult(exit_status=None, local_failure="timeout"),
        ):
            with self.subTest(result=repr(result)[:50]):
                with self.assertRaises(TypeError):
                    bool(result)
                with self.assertRaises(TypeError):
                    if result:  # pragma: no cover - the raise is the assertion
                        pass

    def test_carries_status_streams_and_local_failure(self):
        result = GitCommandResult(exit_status=128, stdout=b"o", stderr=b"e")
        self.assertEqual((result.exit_status, result.stdout, result.stderr), (128, b"o", b"e"))
        self.assertIsNone(result.local_failure)
        self.assertEqual(GitCommandResult(exit_status=None, local_failure="killed").local_failure, "killed")

    def test_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            GitCommandResult(exit_status=0).exit_status = 1  # type: ignore[misc]


class NonAuthorizingTests(unittest.TestCase):
    def test_constructing_a_command_claims_nothing_about_approval(self):
        # Building one is as easy as building any dataclass.  That is the point
        # of recording it: constructibility must never read as authorization.
        self.assertIsInstance(command(), GitCommand)

    def test_executor_obligations_are_documented_and_stable(self):
        self.assertEqual(
            EXECUTOR_OBLIGATIONS,
            (
                "preserve-descriptor-numbers",
                "clear-cloexec-for-allowlist-only",
                "close-other-non-standard-descriptors",
                "never-alias-standard-streams",
            ),
        )


class PurityTests(unittest.TestCase):
    def test_module_exposes_no_spawn_capable_name(self):
        import council_tools.git_process_contract as module

        forbidden = {"subprocess", "os", "pathlib", "shutil", "socket", "signal", "fcntl"}
        present = forbidden.intersection(vars(module))
        self.assertEqual(present, set(), f"module exposes spawn-capable names: {present}")

    def test_errors_are_typed_with_stable_codes_and_fields(self):
        with self.assertRaises(GitProcessError) as caught:
            command(executable="git")
        self.assertTrue(issubclass(GitProcessError, ValueError))
        self.assertEqual(caught.exception.code, "executable-not-absolute")
        self.assertEqual(caught.exception.field, "command.executable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
