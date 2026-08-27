import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from council_tools.git_process_contract import (
    BASE_CHILD_ENVIRONMENT,
    GitCommand,
    GitCommandResult,
)
from council_tools import git_repository_read_runner as read_runner
from council_tools import git_repository_write_runner as write_runner
from council_tools.git_claim_observation import PORCELAIN_TRAILER
from council_tools.git_process_executor import GitSubprocessExecutor
from council_tools.git_repository_lease import (
    BareRepositoryLease,
    GitRepositoryLeaseError,
    LeaseIdentity,
    acquire_lease,
    release_descriptor,
)
from council_tools.git_transport_execution import (
    FORBIDDEN_ARGUMENT_PREFIXES,
    GIT_DIR_OPTION,
    REMOTE_ARGUMENT_INDEX,
    GitTransportExecutionError,
    RemoteOperationResult,
    build_remote_command,
    run_remote_operation,
)
from council_tools.git_transport_identity import (
    GitTransportIdentityError,
    validate_remote_url,
)

GIT = shutil.which("git") or "/usr/bin/git"


def target(url="file:///tmp/claim.git"):
    return validate_remote_url(url)


class RecordingExecutor:
    """Captures the command instead of running it."""

    def __init__(self, result=None):
        self.command = None
        # `or` would read the result by truthiness, which the type refuses.
        self._result = GitCommandResult(exit_status=0) if result is None else result

    def execute(self, command):
        self.command = command
        return self._result


class VectorTest(unittest.TestCase):
    def test_the_remote_is_placed_at_the_known_position(self):
        command = build_remote_command(GIT, target(), "ls-remote")
        self.assertEqual(
            command.invocation.subcommand_args[REMOTE_ARGUMENT_INDEX],
            "file:///tmp/claim.git",
        )

    def test_the_argument_vector_is_exactly_this(self):
        command = build_remote_command(
            GIT, target(), "push", ("--porcelain", "--no-verify")
        )
        self.assertEqual(
            command.invocation.argv(),
            ("push", "file:///tmp/claim.git", "--porcelain", "--no-verify"),
        )

    def test_global_options_precede_the_subcommand(self):
        command = build_remote_command(
            GIT, target(), "ls-remote", global_options=("--no-replace-objects",)
        )
        self.assertEqual(
            command.invocation.argv(),
            ("--no-replace-objects", "ls-remote", "file:///tmp/claim.git"),
        )

    def test_the_placed_url_is_the_validated_one(self):
        validated = target("https://git.example.org/claim.git")
        command = build_remote_command(GIT, validated, "ls-remote")
        self.assertEqual(command.invocation.subcommand_args[0], validated.url)


class TargetTest(unittest.TestCase):
    def test_text_is_refused_so_validation_cannot_be_bypassed(self):
        for value in ("file:///tmp/claim.git", "https://h/x", None, 1):
            with self.subTest(value=value):
                with self.assertRaises(GitTransportExecutionError) as caught:
                    build_remote_command(GIT, value, "ls-remote")
                self.assertEqual(caught.exception.code, "invalid-target")

    def test_a_remote_the_policy_refuses_never_reaches_a_command(self):
        for url in ("https://token@h/x", "git://h/x", "host:path"):
            with self.subTest(url=url):
                with self.assertRaises(GitTransportIdentityError):
                    build_remote_command(GIT, validate_remote_url(url), "ls-remote")

    def test_an_invalid_subcommand_is_refused(self):
        for value in ("", " push", None, 1):
            with self.subTest(value=value):
                with self.assertRaises(GitTransportExecutionError) as caught:
                    build_remote_command(GIT, target(), value)
                self.assertEqual(caught.exception.code, "invalid-subcommand")


class ArgumentTest(unittest.TestCase):
    def test_an_argument_repeating_the_remote_is_refused(self):
        with self.assertRaises(GitTransportExecutionError) as caught:
            build_remote_command(GIT, target(), "push", ("file:///tmp/claim.git",))
        self.assertEqual(caught.exception.code, "argument-repeats-remote")

    def test_every_forbidden_argument_is_refused_bare_and_with_a_value(self):
        for prefix in FORBIDDEN_ARGUMENT_PREFIXES:
            for argument in (prefix, f"{prefix}=value"):
                with self.subTest(argument=argument):
                    with self.assertRaises(GitTransportExecutionError) as caught:
                        build_remote_command(GIT, target(), "push", (argument,))
                    self.assertEqual(caught.exception.code, "forbidden-argument")

    def test_a_forbidden_argument_is_refused_in_global_options_too(self):
        with self.assertRaises(GitTransportExecutionError) as caught:
            build_remote_command(
                GIT, target(), "push", global_options=("-c", "http.proxy=x")
            )
        self.assertEqual(caught.exception.code, "forbidden-argument")

    def test_an_ordinary_argument_that_merely_starts_similarly_is_allowed(self):
        command = build_remote_command(GIT, target(), "push", ("--porcelain",))
        self.assertIn("--porcelain", command.invocation.argv())

    def test_non_text_and_non_sequence_arguments_are_refused(self):
        with self.assertRaises(GitTransportExecutionError) as caught:
            build_remote_command(GIT, target(), "push", ("ok", 1))
        self.assertEqual(caught.exception.code, "invalid-argument")
        for value in ("--porcelain", None, 1):
            with self.subTest(value=value):
                with self.assertRaises(GitTransportExecutionError) as caught:
                    build_remote_command(GIT, target(), "push", value)
                self.assertEqual(caught.exception.code, "invalid-arguments")


class EnvironmentTest(unittest.TestCase):
    def test_the_environment_is_the_policy_environment(self):
        command = build_remote_command(GIT, target(), "ls-remote")
        for key, value in BASE_CHILD_ENVIRONMENT.items():
            with self.subTest(key=key):
                self.assertEqual(command.environment[key], value)

    def test_an_unpermitted_environment_key_is_refused_by_the_policy(self):
        with self.assertRaises(GitTransportIdentityError) as caught:
            build_remote_command(
                GIT, target(), "ls-remote", environment={"GIT_PROXY_COMMAND": "x"}
            )
        self.assertEqual(caught.exception.code, "transport-key-not-permitted")

    def test_a_permitted_transport_key_is_carried(self):
        command = build_remote_command(
            GIT, target(), "ls-remote", environment={"SSH_AUTH_SOCK": "/run/a.sock"}
        )
        self.assertEqual(command.environment["SSH_AUTH_SOCK"], "/run/a.sock")

    def test_the_command_environment_carries_nothing_ambient(self):
        command = build_remote_command(GIT, target(), "ls-remote")
        allowed = set(BASE_CHILD_ENVIRONMENT)
        self.assertEqual(set(command.environment) - allowed, set())


class ResultTest(unittest.TestCase):
    def test_output_is_redacted_before_it_is_returned(self):
        executor = RecordingExecutor(
            GitCommandResult(
                exit_status=1,
                stderr=b"fatal: could not read from https://user:secret@h/x\n",
            )
        )
        result = run_remote_operation(executor, GIT, target(), "ls-remote")
        self.assertNotIn("secret", result.stderr)
        self.assertIn("[redacted]", result.stderr)

    def test_control_characters_from_a_remote_are_stripped(self):
        executor = RecordingExecutor(
            GitCommandResult(exit_status=1, stderr=b"fatal: \x1b[31mred\x1b[0m\n")
        )
        result = run_remote_operation(executor, GIT, target(), "ls-remote")
        self.assertNotIn("\x1b", result.stderr)

    def test_the_result_refuses_truthiness(self):
        executor = RecordingExecutor()
        result = run_remote_operation(executor, GIT, target(), "ls-remote")
        self.assertIsInstance(result, RemoteOperationResult)
        with self.assertRaises(TypeError):
            bool(result)

    def test_a_local_failure_is_carried_through(self):
        executor = RecordingExecutor(
            GitCommandResult(exit_status=None, local_failure="deadline-exceeded")
        )
        result = run_remote_operation(executor, GIT, target(), "ls-remote")
        self.assertEqual(result.local_failure, "deadline-exceeded")
        self.assertIsNone(result.exit_status)

    def test_a_non_result_from_an_executor_is_refused(self):
        class Bad:
            def execute(self, command):
                return "ok"

        with self.assertRaises(GitTransportExecutionError) as caught:
            run_remote_operation(Bad(), GIT, target(), "ls-remote")
        self.assertEqual(caught.exception.code, "invalid-result")

    def test_the_executor_receives_the_built_command(self):
        executor = RecordingExecutor()
        run_remote_operation(executor, GIT, target(), "ls-remote")
        self.assertIsInstance(executor.command, GitCommand)
        self.assertEqual(executor.command.invocation.subcommand, "ls-remote")


class RealChildIsolationTest(unittest.TestCase):
    """The isolation claim, reproduced against a real Git child.

    The spike measured that global, system and XDG configuration must be
    suppressed together, but it measured it against *local* commands and it was
    the spike rather than the suite. These reproduce it for a remote operation,
    and every assertion runs against a real `git` child.

    The probe is a hostile `insteadOf` rewrite in the global configuration. If it
    reaches the child, the remote is redirected somewhere that does not exist and
    the operation fails. `test_the_probe_can_detect_a_leak` proves that is what
    happens when the configuration is *not* suppressed, so a passing isolation
    test cannot be one that observed nothing.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="transport-isolation-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        self.remote = self.root / "claim.git"
        subprocess.run(
            [GIT, "init", "--bare", "-q", "--object-format=sha1", str(self.remote)],
            check=True,
            capture_output=True,
        )

        self.hostile_home = self.root / "home"
        self.hostile_home.mkdir()
        (self.hostile_home / ".gitconfig").write_text(
            "[user]\n\tname = HOSTILE\n\temail = hostile@example.invalid\n"
            f'[url "file:///nonexistent-hostile-redirect/"]\n'
            f"\tinsteadOf = file://{self.root}/\n",
            encoding="utf-8",
        )
        askpass = self.root / "askpass.sh"
        askpass.write_text("#!/bin/sh\necho HOSTILE-SECRET\n", encoding="utf-8")
        askpass.chmod(0o755)

        # Poison the parent exactly as a developer machine would be.
        for key, value in {
            "HOME": str(self.hostile_home),
            "XDG_CONFIG_HOME": str(self.hostile_home),
            "SSH_AUTH_SOCK": "/tmp/hostile-agent.sock",
            "GIT_ASKPASS": str(askpass),
            "SSH_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "HOSTILE",
        }.items():
            self.addCleanup(_restore_environ, key, os.environ.get(key))
            os.environ[key] = value

        self.target = validate_remote_url(f"file://{self.remote}")
        self.executor = GitSubprocessExecutor(deadline_seconds=30)

    def test_the_probe_can_detect_a_leak(self):
        """Without the suppression set, the hostile rewrite redirects the remote."""
        result = subprocess.run(
            [GIT, "ls-remote", f"file://{self.remote}"],
            capture_output=True,
            text=True,
            env={"HOME": str(self.hostile_home), "PATH": "/usr/bin:/bin"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nonexistent-hostile-redirect", result.stdout + result.stderr)

    def test_the_hostile_configuration_does_not_reach_the_child(self):
        result = run_remote_operation(self.executor, GIT, self.target, "ls-remote")
        self.assertIsNone(result.local_failure, result.stderr)
        self.assertEqual(result.exit_status, 0, result.stderr)
        self.assertNotIn("nonexistent-hostile-redirect", result.stderr)

    def test_a_permitted_transport_key_does_reach_the_same_child(self):
        """Otherwise the isolation result could be a child that sees nothing.

        The command never connects: it runs a local script that writes a marker
        and exits, so no network host is contacted and no credential exists.
        """
        marker = self.root / "ssh-marker"
        fake_ssh = self.root / "fake-ssh.sh"
        fake_ssh.write_text(
            f"#!/bin/sh\necho reached > {marker}\nexit 42\n", encoding="utf-8"
        )
        fake_ssh.chmod(0o755)

        result = run_remote_operation(
            self.executor,
            GIT,
            validate_remote_url("ssh://placeholder.invalid/claim.git"),
            "ls-remote",
            environment={"GIT_SSH_COMMAND": str(fake_ssh)},
        )
        self.assertTrue(marker.exists(), "GIT_SSH_COMMAND did not reach the child")
        self.assertEqual(marker.read_text().strip(), "reached")
        self.assertNotEqual(result.exit_status, 0)

    def test_no_ambient_key_is_in_the_command_the_child_receives(self):
        command = build_remote_command(GIT, self.target, "ls-remote")
        for key in (
            "SSH_AUTH_SOCK",
            "SSH_ASKPASS",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
        ):
            with self.subTest(key=key):
                self.assertNotIn(key, command.environment)
        self.assertEqual(command.environment["GIT_ASKPASS"], "")
        self.assertEqual(command.environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(command.environment["HOME"], "/nonexistent")

    def test_a_failing_remote_operation_reports_its_status(self):
        missing = validate_remote_url(f"file://{self.root}/absent.git")
        result = run_remote_operation(self.executor, GIT, missing, "ls-remote")
        self.assertNotEqual(result.exit_status, 0)
        self.assertTrue(result.stderr)


def _restore_environ(key, previous):
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


if __name__ == "__main__":
    unittest.main()


def _source_repository(root, name="source.git"):
    """A leased bare repository holding one commit, ready to push."""

    lease = acquire_lease(root, name)

    def git(*arguments, stdin=b""):
        completed = subprocess.run(
            [GIT, f"--git-dir={lease.selector}", *arguments],
            input=stdin,
            capture_output=True,
            pass_fds=(lease.descriptor,),
            env={
                **BASE_CHILD_ENVIRONMENT,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            },
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr.decode())
        return completed.stdout.decode().strip()

    git("init", "--bare", "-q", "--object-format=sha1")
    blob = git("hash-object", "-w", "--stdin", stdin=b"claim")
    tree = git("mktree", stdin=f"100644 blob {blob}\tclaim.json\n".encode())
    return lease, git("commit-tree", tree, "-m", "claim")


class RepositoryBindingVectorTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="transport-binding-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lease = acquire_lease(self.root, "bound.git")
        self.addCleanup(release_descriptor, self.lease)

    def test_the_binding_is_the_last_global_option(self):
        # Measured: Git resolves the LAST --git-dir, so anything after the
        # module's binding would override it.
        command = build_remote_command(
            GIT,
            target(),
            "push",
            global_options=("--no-replace-objects",),
            repository=self.lease,
        )
        self.assertEqual(
            command.invocation.global_options,
            ("--no-replace-objects", f"--git-dir={self.lease.selector}"),
        )
        self.assertEqual(
            command.invocation.argv(),
            (
                "--no-replace-objects",
                f"--git-dir={self.lease.selector}",
                "push",
                "file:///tmp/claim.git",
            ),
        )

    def test_the_leases_descriptor_is_inherited_with_the_binding(self):
        command = build_remote_command(GIT, target(), "push", repository=self.lease)
        self.assertEqual(command.inherited_descriptors, (self.lease.descriptor,))

    def test_without_a_lease_the_vector_is_exactly_what_it_was(self):
        command = build_remote_command(
            GIT, target(), "ls-remote", global_options=("--no-replace-objects",)
        )
        self.assertEqual(command.invocation.global_options, ("--no-replace-objects",))
        self.assertEqual(command.inherited_descriptors, ())
        self.assertNotIn(
            "--git-dir", " ".join(command.invocation.argv())
        )

    def test_all_three_binding_spellings_agree(self):
        # One spelling in three modules. Pinned together so a change to any of
        # them cannot silently diverge from the others.
        self.assertEqual(GIT_DIR_OPTION, read_runner.GIT_DIR_OPTION)
        self.assertEqual(GIT_DIR_OPTION, write_runner.GIT_DIR_OPTION)


class RepositoryRefusalTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="transport-refusal-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_anything_that_is_not_a_lease_is_refused(self):
        class Impostor:
            selector = "/proc/self/fd/3"
            descriptor = 3

        for value in ("/tmp/claim.git", Path("/tmp/claim.git"), 3, object(), Impostor()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(GitTransportExecutionError) as raised:
                    build_remote_command(GIT, target(), "push", repository=value)
                self.assertEqual(raised.exception.code, "invalid-repository")
                self.assertEqual(raised.exception.field, "repository")

    def test_a_refused_repository_never_reaches_the_executor(self):
        executor = RecordingExecutor()
        with self.assertRaises(GitTransportExecutionError):
            run_remote_operation(
                executor, GIT, target(), "push", repository="/tmp/claim.git"
            )
        self.assertIsNone(executor.command)

    def test_a_caller_supplied_binding_is_still_refused(self):
        lease = acquire_lease(self.root, "bound.git")
        self.addCleanup(release_descriptor, lease)
        for field, kwargs in (
            ("arguments", {"arguments": (f"--git-dir={lease.selector}",)}),
            ("global_options", {"global_options": (f"--git-dir={lease.selector}",)}),
        ):
            with self.subTest(field=field):
                with self.assertRaises(GitTransportExecutionError) as raised:
                    build_remote_command(GIT, target(), "push", **kwargs)
                self.assertEqual(raised.exception.code, "forbidden-argument")
                self.assertEqual(raised.exception.detail, "--git-dir")
                # ...and it is refused whether or not the module is also
                # inserting one of its own.
                with self.assertRaises(GitTransportExecutionError):
                    build_remote_command(
                        GIT, target(), "push", repository=lease, **kwargs
                    )


class RepositoryBorrowTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="transport-borrow-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lease = acquire_lease(self.root, "bound.git")
        self.addCleanup(release_descriptor, self.lease)

    def test_the_lease_is_held_for_the_whole_operation(self):
        observed = []

        class Observer(RecordingExecutor):
            def execute(inner, command):  # noqa: N805 - inner self, outer test
                observed.append(self.lease.borrow_count)
                return super().execute(command)

        self.assertEqual(self.lease.borrow_count, 0)
        run_remote_operation(Observer(), GIT, target(), "push", repository=self.lease)
        self.assertEqual(observed, [1])
        self.assertEqual(self.lease.borrow_count, 0)

    def test_no_borrow_is_taken_when_there_is_no_lease(self):
        run_remote_operation(RecordingExecutor(), GIT, target(), "ls-remote")
        self.assertEqual(self.lease.borrow_count, 0)

    def test_a_lease_whose_identity_drifted_never_reaches_the_executor(self):
        executor = RecordingExecutor()
        drifted = BareRepositoryLease(
            descriptor=self.lease.descriptor,
            path=self.lease.path,
            identity=LeaseIdentity(
                device=self.lease.identity.device + 1,
                inode=self.lease.identity.inode,
                owner_uid=self.lease.identity.owner_uid,
                mode=self.lease.identity.mode,
            ),
        )
        with self.assertRaises(GitRepositoryLeaseError) as raised:
            run_remote_operation(executor, GIT, target(), "push", repository=drifted)
        self.assertEqual(raised.exception.code, "identity-mismatch")
        self.assertIsNone(executor.command)

    def test_the_borrow_is_released_when_the_operation_raises(self):
        class Exploding:
            def execute(self, command):
                raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            run_remote_operation(
                Exploding(), GIT, target(), "push", repository=self.lease
            )
        self.assertEqual(self.lease.borrow_count, 0)


class RealPushBindingTest(unittest.TestCase):
    """The exercising case: an operation that needs local objects to send.

    ``ls-remote`` needs neither a local repository nor a readable answer, which
    is why every earlier test of this module used it and why neither of the two
    defects behind #153 was visible.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="transport-push-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.remote = self.root / "remote.git"
        subprocess.run(
            [GIT, "init", "--bare", "-q", str(self.remote)],
            check=True,
            env=dict(BASE_CHILD_ENVIRONMENT),
        )
        self.target = validate_remote_url(f"file://{self.remote}")
        self.lease, self.commit = _source_repository(self.root)
        self.addCleanup(release_descriptor, self.lease)
        self.executor = GitSubprocessExecutor()
        self.ref = "refs/heads/ai-council/claims/155"

    def push(self, **kwargs):
        return run_remote_operation(
            self.executor,
            GIT,
            self.target,
            "push",
            (
                "--porcelain",
                "--no-verify",
                f"--force-with-lease={self.ref}:",
                f"{self.commit}:{self.ref}",
            ),
            **kwargs,
        )

    def remote_refs(self):
        listed = subprocess.run(
            [GIT, "ls-remote", self.target.url],
            capture_output=True,
            check=True,
            env=dict(BASE_CHILD_ENVIRONMENT),
        )
        return listed.stdout.decode()

    def test_a_bound_push_succeeds_and_the_remote_holds_the_object(self):
        result = self.push(repository=self.lease)
        self.assertEqual(result.exit_status, 0)
        self.assertIsNone(result.local_failure)
        # Read the decision back from the remote rather than inferring it from
        # the exit status: a create and an already-current push both exit 0.
        self.assertIn(f"{self.commit}\t{self.ref}", self.remote_refs())

    def test_an_unbound_push_fails_and_creates_nothing(self):
        result = self.push()
        self.assertNotEqual(result.exit_status, 0)
        self.assertIn("bad object", result.stderr)
        self.assertEqual(self.remote_refs().strip(), "")

    def test_the_unbound_failure_is_reported_as_a_remote_rejection(self):
        # Measured: a fault entirely on this side arrives on stdout carrying the
        # rejection flag. Under #151's rule that flag is honoured whatever
        # summary follows it, so the only thing keeping it from reading as
        # "another session holds the claim" is the absent trailer.
        result = self.push()
        self.assertIn("[remote rejected]", result.stdout)
        self.assertNotIn(PORCELAIN_TRAILER, result.stdout.split("\n"))

    def test_the_selector_alone_binds_nothing(self):
        # The selector and the descriptor travel together; a test that passes
        # only the selector proves nothing about the binding.
        command = build_remote_command(
            GIT, self.target, "push", repository=self.lease
        )
        unbound = GitCommand(
            executable=command.executable,
            invocation=command.invocation,
            environment=command.environment,
            inherited_descriptors=(),
        )
        result = self.executor.execute(unbound)
        self.assertNotEqual(result.exit_status, 0)
        self.assertIn(b"not a git repository", result.stderr)
