"""Feasibility spike: does the pinned Git bind a repository through an inherited fd?

Issue #61.  This module is a **scratch experiment**, not an implementation and not
a component.  It imports nothing from ``council_tools``, exports nothing, and
nothing may depend on it.  Its whole purpose is to record what the pinned Git
binary on this host actually does, so the Issue #48 custody and runner contracts
are frozen against measured behaviour rather than assumption.

The design under test binds Git execution to an open directory descriptor and
passes ``--git-dir=/proc/self/fd/N`` into the child, so execution follows the same
inode across spawn rather than an attacker-replaceable pathname.

Every result applies **only** to the exact binary identified by
:func:`git_provenance`, and only on Linux with procfs mounted.  The findings are
written up in ``design/git-fd-binding-spike.md``.

Nothing here touches the network, a remote, production data, or any repository
outside a temporary directory it creates and removes.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


GIT = "/usr/bin/git"
PINNED_VERSION = "git version 2.39.5"

# A minimal child environment built from scratch.  Nothing is copied from
# os.environ: the point is that no user, XDG, or system configuration reaches the
# child, and that identity is deterministic.
BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "spike",
    "GIT_AUTHOR_EMAIL": "spike@example.invalid",
    "GIT_AUTHOR_DATE": "@0 +0000",
    "GIT_COMMITTER_NAME": "spike",
    "GIT_COMMITTER_EMAIL": "spike@example.invalid",
    "GIT_COMMITTER_DATE": "@0 +0000",
}

# The suppression set under test in the config-suppression row.
CONFIG_SUPPRESSION = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "XDG_CONFIG_HOME": "/nonexistent",
}

CLAIM_PAYLOAD = b'{"claim":1}'
CLAIM_ENTRY = "100644 blob {oid}\tclaim.json\n"


def git_provenance():
    """Return the identity of the binary these results describe."""

    with open(GIT, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    version = subprocess.run(
        [GIT, "--version"], capture_output=True, text=True, env=BASE_ENV
    ).stdout.strip()
    return {"path": GIT, "version": version, "sha256": digest}


def _supported():
    if sys.platform != "linux" or not os.path.isdir("/proc/self/fd"):
        return False
    if not os.path.isfile(GIT):
        return False
    return git_provenance()["version"] == PINNED_VERSION


requires_pinned_git = unittest.skipUnless(
    _supported(),
    f"needs Linux with procfs and {PINNED_VERSION} at {GIT}",
)


@requires_pinned_git
class GitDescriptorBindingSpike(unittest.TestCase):
    """One disposable bare repository per test, bound only through its fd."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="git-fd-spike-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo = os.path.join(self.root, "claim.git")
        os.mkdir(self.repo, 0o700)
        self.fd = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(self._close_fd)
        self.selector = f"/proc/self/fd/{self.fd}"

    def _close_fd(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    # -- helpers ---------------------------------------------------------

    def git(self, *args, stdin=None, env=None, suppress_config=True):
        child_env = dict(BASE_ENV)
        if suppress_config:
            child_env.update(CONFIG_SUPPRESSION)
        if env:
            child_env.update(env)
        return subprocess.run(
            [GIT, *args],
            input=stdin,
            capture_output=True,
            pass_fds=(self.fd,),
            env=child_env,
        )

    def bound(self, *args, **kwargs):
        """Run one Git command bound to the leased descriptor."""

        return self.git(f"--git-dir={self.selector}", *args, **kwargs)

    def out(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def build_claim(self):
        self.out(self.bound("init", "--bare", "--quiet", "--object-format=sha1"))
        blob = self.out(self.bound("hash-object", "-w", "--stdin", stdin=CLAIM_PAYLOAD))
        tree = self.out(
            self.bound("mktree", stdin=CLAIM_ENTRY.format(oid=blob).encode())
        )
        commit = self.out(self.bound("commit-tree", tree, "-m", "claim"))
        return blob, tree, commit

    # -- row 1: provenance ----------------------------------------------

    def test_provenance_is_recorded_and_pinned(self):
        provenance = git_provenance()
        self.assertEqual(provenance["version"], PINNED_VERSION)
        self.assertEqual(provenance["path"], GIT)
        self.assertRegex(provenance["sha256"], r"\A[0-9a-f]{64}\Z")

    # -- row 2: descriptor inheritance -----------------------------------

    def test_only_the_leased_descriptor_is_inherited_at_the_same_number(self):
        spare_low = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, spare_low)
        spare_high = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, spare_high)

        seen = self.git(
            "--version"
        )  # warm-up; the readlink probes below are what matter
        self.assertEqual(seen.returncode, 0)

        probe = subprocess.run(
            ["/bin/sh", "-c", f"readlink /proc/self/fd/{self.fd}"],
            capture_output=True,
            text=True,
            pass_fds=(self.fd,),
            env=BASE_ENV,
        )
        self.assertEqual(probe.stdout.strip(), self.repo, "same fd number, same target")

        for spare in (spare_low, spare_high):
            with self.subTest(spare=spare):
                probe = subprocess.run(
                    [
                        "/bin/sh",
                        "-c",
                        f"readlink /proc/self/fd/{spare} 2>/dev/null || echo CLOSED",
                    ],
                    capture_output=True,
                    text=True,
                    pass_fds=(self.fd,),
                    env=BASE_ENV,
                )
                self.assertEqual(probe.stdout.strip(), "CLOSED")

        probe = subprocess.run(
            ["/bin/sh", "-c", "readlink /proc/self/fd/0 /proc/self/fd/1 /proc/self/fd/2"],
            capture_output=True,
            text=True,
            pass_fds=(self.fd,),
            env=BASE_ENV,
        )
        self.assertNotIn(self.repo, probe.stdout, "stdio must not alias the lease")

    # -- rows 3 and 4: binding for every required command -----------------

    def test_every_required_command_binds_through_the_descriptor(self):
        blob, tree, commit = self.build_claim()

        self.assertTrue(os.path.exists(os.path.join(self.repo, "HEAD")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "objects")))
        self.assertRegex(blob, r"\A[0-9a-f]{40}\Z")
        self.assertRegex(tree, r"\A[0-9a-f]{40}\Z")
        self.assertRegex(commit, r"\A[0-9a-f]{40}\Z")

        self.assertEqual(
            self.out(self.bound("rev-parse", "--show-object-format")), "sha1"
        )
        self.assertEqual(
            self.out(self.bound("--no-replace-objects", "cat-file", "-t", commit)),
            "commit",
        )
        self.assertEqual(
            self.out(self.bound("--no-replace-objects", "cat-file", "blob", blob)),
            CLAIM_PAYLOAD.decode(),
        )
        raw_commit = self.out(
            self.bound("--no-replace-objects", "cat-file", "commit", commit)
        )
        self.assertIn(f"tree {tree}", raw_commit)

        listing = self.bound("--no-replace-objects", "ls-tree", "-z", tree)
        self.assertEqual(listing.returncode, 0)
        self.assertEqual(
            listing.stdout.decode(), f"100644 blob {blob}\tclaim.json\x00"
        )

    # -- row 5: substitution ---------------------------------------------

    def test_binding_follows_the_leased_inode_across_a_rename(self):
        _, _, commit = self.build_claim()
        moved = os.path.join(self.root, "moved.git")
        os.rename(self.repo, moved)
        self.assertEqual(
            self.out(self.bound("--no-replace-objects", "cat-file", "-t", commit)),
            "commit",
        )
        self.assertEqual(os.readlink(self.selector), moved)

    def test_a_decoy_planted_at_the_original_path_is_not_reachable(self):
        # This is the property the whole custody design exists for.
        blob, _, _ = self.build_claim()
        moved = os.path.join(self.root, "moved.git")
        os.rename(self.repo, moved)

        os.mkdir(self.repo, 0o700)
        decoy_env = dict(BASE_ENV)
        decoy_env.update(CONFIG_SUPPRESSION)
        subprocess.run(
            [GIT, f"--git-dir={self.repo}", "init", "--bare", "--quiet"],
            capture_output=True,
            env=decoy_env,
        )
        decoy = subprocess.run(
            [GIT, f"--git-dir={self.repo}", "hash-object", "-w", "--stdin"],
            input=b"HOSTILE",
            capture_output=True,
            env=decoy_env,
        ).stdout.decode().strip()

        # The lease still reads its own object ...
        self.assertEqual(
            self.out(self.bound("--no-replace-objects", "cat-file", "blob", blob)),
            CLAIM_PAYLOAD.decode(),
        )
        # ... and cannot see anything the decoy contains.
        refused = self.bound("--no-replace-objects", "cat-file", "-t", decoy)
        self.assertNotEqual(refused.returncode, 0)

    # -- row 6: cleanup while borrowed ------------------------------------

    def test_deleting_the_lease_while_borrowed_fails_closed(self):
        # Recorded behaviour, not an endorsement: a directory descriptor does
        # NOT keep the repository usable once the tree is unlinked, because Git
        # re-resolves /proc/self/fd/N as a path and the entries below it are
        # gone.  Cleanup must therefore wait or refuse while a borrow is live.
        #
        # THE SYNCHRONISATION BELOW IS LOAD-BEARING.  Do not remove it.  An
        # earlier version of this test removed the tree immediately after
        # Popen and asserted the child reported the repository absent.  That
        # is a race by construction -- the assertion holds only if the child
        # got far enough to be affected by the unlink and not so far that it
        # had already opened the repository -- and it failed once during a
        # full run with six other processes active (issue #122).
        #
        # Synchronising does not merely stabilise that assertion; it falsifies
        # it.  A child that has ALREADY opened the repository does not report
        # the repository absent after the unlink.  It answers the next lookup
        # with "<oid> missing" on stdout and exits 0.  Measured on the pinned
        # binary and reproduced 5/5 while re-contracting #122.
        #
        # That distinction is the point, and it is why design/git-fd-binding-
        # spike.md Row 6 no longer claims the failure is always an error.  A
        # verification read already in flight comes back looking like a clean
        # answer of "no such object", which for a claim protocol is the
        # difference between not knowing and concluding nobody holds the claim.
        #
        # How this version was checked, since a flake fix that is only run idle
        # has not been checked at all: 10 consecutive runs of this test under 40
        # CPU-bound processes on a 20-core host, all passing, plus the full
        # module and the full suite.  The behaviour it now asserts was itself
        # measured 5/5 before being written down.
        _, _, commit = self.build_claim()
        child = subprocess.Popen(
            [GIT, f"--git-dir={self.selector}", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(self.fd,),
            env={**BASE_ENV, **CONFIG_SUPPRESSION},
        )
        self.addCleanup(self._reap, child)

        # Phase one: make the child answer, so it is KNOWN to be past startup
        # with the repository open rather than presumed to be.
        child.stdin.write((commit + "\n").encode())
        child.stdin.flush()
        header = child.stdout.readline()
        self.assertRegex(header, rb"^[0-9a-f]{40} commit \d+\n$")
        # Consume the body and its trailing newline so the stream stays aligned.
        child.stdout.read(int(header.split()[2]) + 1)

        # Phase two: unlink while that child is live, then ask it again.
        shutil.rmtree(self.repo)
        self.assertTrue(os.readlink(self.selector).endswith("(deleted)"))

        child.stdin.write((commit + "\n").encode())
        child.stdin.flush()
        child.stdin.close()
        stdout = child.stdout.read()
        stderr = child.stderr.read()
        child.wait(timeout=30)

        # The in-flight read reports the object absent and succeeds.  It does
        # NOT report the repository absent, and it does NOT fail.
        self.assertEqual(child.returncode, 0)
        self.assertEqual(stdout, f"{commit} missing\n".encode())
        self.assertEqual(stderr, b"")
        self.assertNotIn(b"not a git repository", stderr)

        # A command started AFTER the removal does still fail closed, which is
        # the behaviour that makes cleanup ordering mandatory.  This half was
        # never raced and is unchanged.
        after = self.bound("cat-file", "-t", commit)
        self.assertNotEqual(after.returncode, 0)
        self.assertIn(b"not a git repository", after.stderr)

    @staticmethod
    def _reap(child):
        """Leave nothing running if an assertion aborts mid-protocol."""

        if child.poll() is None:
            child.kill()
        for stream in (child.stdin, child.stdout, child.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        child.wait(timeout=30)

    # -- row 7: config suppression ----------------------------------------

    def test_user_config_reaches_the_child_unless_it_is_suppressed(self):
        self.out(self.bound("init", "--bare", "--quiet", "--object-format=sha1"))
        hostile_home = os.path.join(self.root, "home")
        os.makedirs(hostile_home)
        with open(os.path.join(hostile_home, ".gitconfig"), "w") as handle:
            handle.write("[user]\n\tname = HOSTILE\n")

        # Suppression minus the two keys that hide a user config, so the only
        # difference between this child and the next is the suppression itself.
        unsuppressed = {
            key: value
            for key, value in CONFIG_SUPPRESSION.items()
            if key not in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "HOME")
        }
        unsuppressed["HOME"] = hostile_home
        leaked = self.bound(
            "config",
            "--get",
            "user.name",
            env=unsuppressed,
            suppress_config=False,
        )
        # Without suppression the hostile value is visible.
        self.assertEqual(leaked.stdout.decode().strip(), "HOSTILE")

        suppressed = self.bound("config", "--get", "user.name")
        self.assertNotEqual(suppressed.returncode, 0)
        self.assertEqual(suppressed.stdout.decode().strip(), "")

    # -- row 8: replacement objects ----------------------------------------

    def test_replacement_objects_are_honoured_unless_suppressed(self):
        blob, _, _ = self.build_claim()
        decoy = self.out(
            self.bound("hash-object", "-w", "--stdin", stdin=b'{"claim":"REPLACED"}')
        )
        replace_dir = os.path.join(self.repo, "refs", "replace")
        os.makedirs(replace_dir, exist_ok=True)
        with open(os.path.join(replace_dir, blob), "w") as handle:
            handle.write(decoy + "\n")

        self.assertEqual(
            self.out(self.bound("cat-file", "blob", blob)), '{"claim":"REPLACED"}'
        )
        self.assertEqual(
            self.out(self.bound("--no-replace-objects", "cat-file", "blob", blob)),
            CLAIM_PAYLOAD.decode(),
        )

    def test_no_replace_objects_is_accepted_in_the_global_position(self):
        blob, tree, commit = self.build_claim()
        cases = (
            ("hash-object", ("hash-object", "--stdin"), b"x"),
            ("mktree", ("mktree",), CLAIM_ENTRY.format(oid=blob).encode()),
            ("commit-tree", ("commit-tree", tree, "-m", "m"), None),
            ("cat-file", ("cat-file", "-t", commit), None),
            ("ls-tree", ("ls-tree", "-z", tree), None),
            ("rev-parse", ("rev-parse", "--show-object-format"), None),
        )
        for name, args, stdin in cases:
            with self.subTest(command=name):
                result = self.bound("--no-replace-objects", *args, stdin=stdin)
                self.assertEqual(result.returncode, 0, result.stderr.decode())

    # -- row 9: option terminators ------------------------------------------

    def test_option_terminator_support_is_uneven(self):
        blob, tree, commit = self.build_claim()

        supported = (
            ("hash-object trailing --", ("hash-object", "--stdin", "--"), b"x"),
            ("cat-file -t --", ("cat-file", "-t", "--", commit), None),
            ("ls-tree trailing --", ("ls-tree", "-z", tree, "--"), None),
            ("commit-tree trailing --", ("commit-tree", tree, "-m", "m", "--"), None),
        )
        for name, args, stdin in supported:
            with self.subTest(supported=name):
                result = self.bound(*args, stdin=stdin)
                self.assertEqual(result.returncode, 0, result.stderr.decode())

        # These do NOT work on the pinned binary.  Recorded so no contract is
        # written against them.
        unsupported = (
            ("--end-of-options as a Git-global option",
             ("--no-replace-objects", "--end-of-options", "cat-file", "-t", commit)),
            ("cat-file --end-of-options before the type",
             ("cat-file", "--end-of-options", "-t", commit)),
            ("rev-parse --end-of-options before a query",
             ("rev-parse", "--end-of-options", "--show-object-format")),
        )
        for name, args in unsupported:
            with self.subTest(unsupported=name):
                self.assertNotEqual(self.bound(*args).returncode, 0)


if __name__ == "__main__":
    unittest.main()
