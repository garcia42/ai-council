"""Acquisition and identity proofs for the descriptor lease.

The central claim -- that binding follows the inode rather than the name -- is
proved directly by renaming the leased directory and by planting a decoy at the
original pathname, because that is the property the whole custody design exists
for and asserting it indirectly would prove nothing.

Every test builds and removes its own tree under a temporary directory.
"""

from __future__ import annotations

import dataclasses
import os
import stat
import tempfile
import unittest
from pathlib import Path

from council_tools.git_repository_lease import (
    PRIVATE_DIRECTORY_MODE,
    BareRepositoryLease,
    GitRepositoryLeaseError,
    LeaseIdentity,
    acquire_lease,
    release_descriptor,
)


class LeaseTestCase(unittest.TestCase):
    def setUp(self):
        if not Path("/proc/self/fd").is_dir():  # pragma: no cover - environment guard
            self.skipTest("procfs is required")
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def lease(self, name="claim.git"):
        held = acquire_lease(self.root, name)
        self.addCleanup(self._close_quietly, held)
        return held

    @staticmethod
    def _close_quietly(held):
        try:
            os.close(held.descriptor)
        except OSError:
            pass


class AcquisitionTests(LeaseTestCase):
    def test_creates_the_directory_owner_only(self):
        held = self.lease()
        mode = stat.S_IMODE(os.stat(held.path).st_mode)
        self.assertEqual(mode, PRIVATE_DIRECTORY_MODE)
        self.assertEqual(mode & 0o077, 0, "group and other must have no access")

    def test_the_directory_is_created_empty(self):
        held = self.lease()
        self.assertEqual(os.listdir(held.path), [])

    def test_records_identity_matching_the_created_directory(self):
        held = self.lease()
        info = os.stat(held.path)
        self.assertEqual(held.identity.device, info.st_dev)
        self.assertEqual(held.identity.inode, info.st_ino)
        self.assertEqual(held.identity.owner_uid, os.geteuid())

    def test_the_selector_names_the_descriptor(self):
        held = self.lease()
        self.assertEqual(held.selector, f"/proc/self/fd/{held.descriptor}")
        self.assertTrue(Path(held.selector).is_dir())

    def test_the_descriptor_resolves_to_the_leased_directory(self):
        held = self.lease()
        self.assertEqual(
            os.stat(held.selector).st_ino, os.stat(held.path).st_ino
        )

    def test_two_leases_are_distinct(self):
        first, second = self.lease("a.git"), self.lease("b.git")
        self.assertNotEqual(first.descriptor, second.descriptor)
        self.assertNotEqual(first.identity.inode, second.identity.inode)


class RefusalTests(LeaseTestCase):
    def test_an_existing_target_is_refused(self):
        # A lease must never be a handle on a directory the caller did not
        # create, because the descriptor would then bind faithfully to the
        # wrong repository.
        (self.root / "claim.git").mkdir()
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            acquire_lease(self.root, "claim.git")
        self.assertEqual(caught.exception.code, "target-exists")

    def test_an_existing_file_at_the_target_is_refused(self):
        (self.root / "claim.git").write_text("x", encoding="utf-8")
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            acquire_lease(self.root, "claim.git")
        self.assertEqual(caught.exception.code, "target-exists")

    def test_a_symlink_at_the_target_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.root / "claim.git")
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            acquire_lease(self.root, "claim.git")
        # mkdir refuses first; either way the symlink is never followed.
        self.assertIn(caught.exception.code, {"target-exists", "symlinked-target"})
        self.assertEqual(os.listdir(elsewhere), [], "the symlink target must be untouched")

    def test_a_missing_parent_is_refused(self):
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            acquire_lease(self.root / "absent", "claim.git")
        self.assertEqual(caught.exception.code, "parent-not-a-directory")

    def test_a_name_with_a_separator_or_dot_segment_is_refused(self):
        for name in ("a/b", "..", ".", "", "/abs"):
            with self.subTest(name=repr(name)):
                with self.assertRaises(GitRepositoryLeaseError) as caught:
                    acquire_lease(self.root, name)
                self.assertEqual(caught.exception.code, "invalid-name")

    def test_a_bytes_path_or_name_is_refused(self):
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            acquire_lease(b"/tmp", "claim.git")  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid-path")

    def test_a_failed_acquisition_leaves_nothing_behind(self):
        before = set(os.listdir(self.root))
        with self.assertRaises(GitRepositoryLeaseError):
            acquire_lease(self.root, "a/b")
        self.assertEqual(set(os.listdir(self.root)), before)


class IdentityBindingTests(LeaseTestCase):
    def test_the_descriptor_follows_the_directory_across_a_rename(self):
        # This is the property the whole custody design exists for.
        held = self.lease()
        moved = self.root / "renamed.git"
        os.rename(held.path, moved)
        held.revalidate()
        self.assertEqual(os.stat(held.selector).st_ino, os.stat(moved).st_ino)

    def test_a_decoy_planted_at_the_original_pathname_is_not_seen(self):
        held = self.lease()
        original = Path(held.path)
        os.rename(original, self.root / "renamed.git")
        decoy = original
        decoy.mkdir()
        (decoy / "PLANTED").write_text("decoy", encoding="utf-8")
        # The lease still resolves to its own inode, and the decoy's contents
        # are invisible through it.
        held.revalidate()
        self.assertNotEqual(os.stat(held.selector).st_ino, os.stat(decoy).st_ino)
        self.assertEqual(os.listdir(held.selector), [])

    def test_revalidate_accepts_an_unchanged_lease(self):
        held = self.lease()
        held.revalidate()
        held.revalidate()

    def test_revalidate_reports_a_substituted_identity(self):
        held = self.lease()
        substituted = dataclasses.replace(
            held, identity=dataclasses.replace(held.identity, inode=held.identity.inode + 1)
        )
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            substituted.revalidate()
        self.assertEqual(caught.exception.code, "identity-mismatch")

    def test_revalidate_reports_a_closed_descriptor(self):
        held = self.lease()
        os.close(held.descriptor)
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            held.revalidate()
        self.assertEqual(caught.exception.code, "descriptor-unusable")


class ReleaseTests(LeaseTestCase):
    def test_release_closes_the_descriptor_and_leaves_the_directory(self):
        # Removal is a separate outcome: the spike measured that unlinking a
        # tree while a bound child is live makes that child fail, so removal
        # must be ordered against borrows rather than done here.
        held = acquire_lease(self.root, "claim.git")
        path = held.path
        release_descriptor(held)
        self.assertTrue(Path(path).is_dir())
        with self.assertRaises(OSError):
            os.fstat(held.descriptor)

    def test_releasing_twice_is_reported(self):
        held = acquire_lease(self.root, "claim.git")
        release_descriptor(held)
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            release_descriptor(held)
        self.assertEqual(caught.exception.code, "descriptor-unusable")


class ValueSemanticsTests(LeaseTestCase):
    def test_the_lease_and_its_identity_are_frozen(self):
        held = self.lease()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            held.descriptor = 3  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            held.identity.inode = 3  # type: ignore[misc]

    def test_identity_compares_by_value(self):
        held = self.lease()
        self.assertEqual(LeaseIdentity.of_descriptor(held.descriptor), held.identity)

    def test_errors_are_typed_with_stable_codes_and_fields(self):
        self.assertTrue(issubclass(GitRepositoryLeaseError, ValueError))
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            acquire_lease(self.root, "")
        self.assertEqual(caught.exception.code, "invalid-name")
        self.assertEqual(caught.exception.field, "name")


class DocumentedLimitsTests(LeaseTestCase):
    def test_the_module_states_what_it_does_not_promise(self):
        # These limits are load-bearing: a reader who assumes a lease survives a
        # process boundary, or defends a same-user attacker, would be wrong.
        import council_tools.git_repository_lease as module

        text = module.__doc__ or ""
        self.assertIn("process boundary is unmeasured", text)
        self.assertIn("same-effective-user", text)

    def test_no_git_command_is_constructed_here(self):
        import council_tools.git_repository_lease as module

        for name in ("subprocess", "RenderedInvocation", "GitCommand"):
            with self.subTest(name=name):
                self.assertNotIn(name, vars(module))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class BorrowTests(LeaseTestCase):
    def test_a_fresh_lease_is_not_borrowed(self):
        held = self.lease()
        self.assertEqual(held.borrow_count, 0)
        self.assertFalse(held.is_borrowed)

    def test_a_borrow_is_visible_while_held_and_gone_afterwards(self):
        # A removal path needs something exact to observe, not a convention.
        held = self.lease()
        with held.borrow():
            self.assertEqual(held.borrow_count, 1)
            self.assertTrue(held.is_borrowed)
        self.assertEqual(held.borrow_count, 0)
        self.assertFalse(held.is_borrowed)

    def test_a_borrow_blocks_closing_the_descriptor(self):
        # Closing mid-operation is the same failure as removing the tree: the
        # bound child loses its repository.
        held = self.lease()
        with held.borrow():
            with self.assertRaises(GitRepositoryLeaseError) as caught:
                release_descriptor(held)
            self.assertEqual(caught.exception.code, "lease-is-borrowed")
        release_descriptor(held)

    def test_nested_borrows_count_and_only_the_last_release_frees_the_lease(self):
        held = self.lease()
        with held.borrow():
            with held.borrow():
                self.assertEqual(held.borrow_count, 2)
                with self.assertRaises(GitRepositoryLeaseError):
                    release_descriptor(held)
            self.assertEqual(held.borrow_count, 1)
            self.assertTrue(held.is_borrowed)
        self.assertEqual(held.borrow_count, 0)

    def test_an_exception_inside_a_borrow_still_releases_it(self):
        # A failed operation must not pin a lease forever, because cleanup
        # waits on this count.
        held = self.lease()

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with held.borrow():
                raise Boom
        self.assertEqual(held.borrow_count, 0)
        release_descriptor(held)

    def test_taking_a_borrow_revalidates_identity_first(self):
        # An operation must never begin against a descriptor whose identity has
        # already drifted.
        held = self.lease()
        drifted = dataclasses.replace(
            held, identity=dataclasses.replace(held.identity, inode=held.identity.inode + 1)
        )
        with self.assertRaises(GitRepositoryLeaseError) as caught:
            with drifted.borrow():
                pass  # pragma: no cover - the borrow must not be entered
        self.assertEqual(caught.exception.code, "identity-mismatch")
        self.assertEqual(drifted.borrow_count, 0)

    def test_a_borrow_yields_the_lease_itself(self):
        held = self.lease()
        with held.borrow() as borrowed:
            self.assertIs(borrowed, held)

    def test_borrow_state_does_not_affect_lease_equality(self):
        # The count is operational state, not part of the lease's identity.
        held = self.lease()
        snapshot = dataclasses.replace(held)
        with held.borrow():
            self.assertEqual(held, snapshot)

    def test_the_module_records_that_a_descriptor_does_not_protect_an_operation(self):
        import council_tools.git_repository_lease as module

        text = module.__doc__ or ""
        self.assertIn("does not by itself protect an in-flight operation", text)
        self.assertIn("process-local", text)
