import fcntl
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from council_tools import cli, evidence_backup, forecasts
from council_tools.evidence_backup import (
    COMPLETION_NAME,
    MANIFEST_NAME,
    PAYLOAD_NAME,
    RestoreError,
    SnapshotIntegrityError,
    SnapshotPolicyError,
    create_evidence_snapshot,
    export_verified_evidence_snapshot,
    restore_evidence_snapshot,
    verify_evidence_snapshot,
)


class EvidenceBackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.base = Path(self.temp.name)
        self.repository = self.base / "repository"
        self.repository.mkdir(mode=0o700)
        self.live = self.base / "live"
        self.live.mkdir(mode=0o700)

        self.ledger = self.live / "council.jsonl"
        self.ledger.write_bytes(b'{"kind":"one"}\n')
        os.chmod(self.ledger, 0o600)
        self.resolutions = self.live / "resolutions.jsonl"
        self.resolutions.write_bytes(b'{"resolved":true}\n')
        os.chmod(self.resolutions, 0o640)
        self.controls = self.live / "controls"
        self.controls.mkdir(mode=0o750)
        (self.controls / "overrides.jsonl").write_bytes(b'{"override":false}\n')
        os.chmod(self.controls / "overrides.jsonl", 0o400)
        self.artifacts = self.live / "artifacts"
        (self.artifacts / "sha256" / "aa").mkdir(parents=True, mode=0o700)
        (self.artifacts / "sha256" / "aa" / "answer.bin").write_bytes(
            b"exact visible answer"
        )
        os.chmod(self.artifacts, 0o700)
        os.chmod(self.artifacts / "sha256", 0o700)
        os.chmod(self.artifacts / "sha256" / "aa", 0o700)
        os.chmod(self.artifacts / "sha256" / "aa" / "answer.bin", 0o600)
        self.lock = self.live / "council.jsonl.lock"
        self.lock.touch(mode=0o600)
        os.chmod(self.lock, 0o600)

    def tearDown(self):
        # Some mode tests deliberately create read-only trees.
        for root, directories, files in os.walk(self.base, topdown=False):
            for name in files:
                try:
                    os.chmod(Path(root) / name, 0o600)
                except FileNotFoundError:
                    pass
            for name in directories:
                try:
                    os.chmod(Path(root) / name, 0o700)
                except FileNotFoundError:
                    pass
        self.temp.cleanup()

    def create(self, target=None):
        target = target or self.base / "snapshot"
        return create_evidence_snapshot(
            ledger_path=self.ledger,
            resolution_store_path=self.resolutions,
            control_store_path=self.controls,
            artifact_root=self.artifacts,
            lock_path=self.lock,
            snapshot_target=target,
            repository_root=self.repository,
        )

    def test_snapshot_is_deterministic_content_manifested_and_private(self):
        first_target = self.base / "snapshot-one"
        second_target = self.base / "snapshot-two"
        first = self.create(first_target)
        second = self.create(second_target)

        self.assertEqual(first, second)
        self.assertEqual(
            (first_target / MANIFEST_NAME).read_bytes(),
            (second_target / MANIFEST_NAME).read_bytes(),
        )
        self.assertEqual(
            (first_target / COMPLETION_NAME).read_bytes(),
            (second_target / COMPLETION_NAME).read_bytes(),
        )
        self.assertEqual(first["scope"], "local-filesystem-rehearsal-only")
        self.assertTrue(first["verified"])

        manifest_bytes = (first_target / MANIFEST_NAME).read_bytes()
        manifest = json.loads(manifest_bytes)
        completion = json.loads((first_target / COMPLETION_NAME).read_bytes())
        self.assertEqual(
            completion["manifestSha256"], hashlib.sha256(manifest_bytes).hexdigest()
        )
        self.assertEqual(
            [item["name"] for item in manifest["sources"]],
            ["artifact-root", "control-store", "ledger", "resolution-store"],
        )
        self.assertNotIn(str(self.live), manifest_bytes.decode())
        for entry in manifest["entries"]:
            self.assertEqual(
                set(entry),
                {"mode", "path", "sha256", "size", "source", "sourceMode", "type"},
            )
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")

        self.assertEqual(stat.S_IMODE(first_target.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((first_target / PAYLOAD_NAME).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((first_target / MANIFEST_NAME).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((first_target / COMPLETION_NAME).stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(
                (first_target / PAYLOAD_NAME / "resolution-store").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(
                (first_target / PAYLOAD_NAME / "control-store" / "overrides.jsonl").stat().st_mode
            ),
            0o400,
        )

    def test_verified_export_returns_exact_descriptor_custodied_members(self):
        snapshot = self.base / "snapshot-export"
        created = self.create(snapshot)

        exported = export_verified_evidence_snapshot(snapshot)

        self.assertEqual(dict(exported.verification), created)
        paths = [member.relative_path for member in exported.members]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        by_path = {member.relative_path: member for member in exported.members}
        self.assertEqual(by_path[MANIFEST_NAME].content, (snapshot / MANIFEST_NAME).read_bytes())
        self.assertEqual(
            by_path[f"{PAYLOAD_NAME}/ledger"].content,
            self.ledger.read_bytes(),
        )
        self.assertEqual(
            by_path[f"{PAYLOAD_NAME}/artifact-root/sha256/aa/answer.bin"].content,
            b"exact visible answer",
        )
        self.assertEqual(by_path[PAYLOAD_NAME].kind, "directory")
        self.assertEqual(by_path[PAYLOAD_NAME].content, b"")
        for member in exported.members:
            self.assertEqual(member.size, len(member.content))
            self.assertEqual(
                member.sha256,
                hashlib.sha256(member.content).hexdigest(),
            )

    def test_verified_export_rejects_snapshot_root_replacement(self):
        snapshot = self.base / "snapshot-export-root-swap"
        self.create(snapshot)
        displaced = self.base / "snapshot-export-root-original"
        real_load = evidence_backup._load_verified_snapshot_fd
        swapped = False

        def verify_then_replace_root(root_fd):
            nonlocal swapped
            result = real_load(root_fd)
            if not swapped:
                snapshot.rename(displaced)
                snapshot.mkdir(mode=0o700)
                swapped = True
            return result

        with mock.patch.object(
            evidence_backup,
            "_load_verified_snapshot_fd",
            side_effect=verify_then_replace_root,
        ):
            with self.assertRaises(SnapshotIntegrityError) as caught:
                export_verified_evidence_snapshot(snapshot)

        self.assertTrue(swapped)
        self.assertEqual(caught.exception.code, "snapshot-changed")
        self.assertEqual(list(snapshot.iterdir()), [])
        self.assertTrue((displaced / COMPLETION_NAME).is_file())

    def test_verified_export_rejects_member_name_replacement(self):
        snapshot = self.base / "snapshot-export-member-swap"
        self.create(snapshot)
        target = snapshot / PAYLOAD_NAME / "ledger"
        displaced = target.with_name("ledger.original")
        original = target.read_bytes()
        real_materialize = evidence_backup._materialize_pinned_snapshot_export

        def materialize_then_replace(members):
            result = real_materialize(members)
            target.rename(displaced)
            target.write_bytes(original)
            os.chmod(target, 0o600)
            return result

        with mock.patch.object(
            evidence_backup,
            "_materialize_pinned_snapshot_export",
            side_effect=materialize_then_replace,
        ):
            with self.assertRaises(SnapshotIntegrityError) as caught:
                export_verified_evidence_snapshot(snapshot)

        self.assertEqual(caught.exception.code, "snapshot-changed")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(displaced.read_bytes(), original)
        self.assertNotEqual(target.stat().st_ino, displaced.stat().st_ino)

    def test_verified_export_rejects_same_inode_member_mutation(self):
        snapshot = self.base / "snapshot-export-member-mutation"
        self.create(snapshot)
        target = snapshot / PAYLOAD_NAME / "ledger"
        substitute = b'{"kind":"same-inode-substitute"}\n'
        original_identity = (target.stat().st_dev, target.stat().st_ino)
        real_materialize = evidence_backup._materialize_pinned_snapshot_export

        def materialize_then_mutate(members):
            result = real_materialize(members)
            target.write_bytes(substitute)
            os.chmod(target, 0o600)
            self.assertEqual(
                (target.stat().st_dev, target.stat().st_ino),
                original_identity,
            )
            return result

        with mock.patch.object(
            evidence_backup,
            "_materialize_pinned_snapshot_export",
            side_effect=materialize_then_mutate,
        ):
            with self.assertRaises(SnapshotIntegrityError) as caught:
                export_verified_evidence_snapshot(snapshot)

        self.assertEqual(caught.exception.code, "snapshot-changed")
        self.assertEqual(target.read_bytes(), substitute)

    def test_snapshot_fsyncs_files_and_directories_before_completion(self):
        synced_types = []
        real_fsync = os.fsync

        def recording_fsync(fd):
            synced_types.append(stat.S_IFMT(os.fstat(fd).st_mode))
            return real_fsync(fd)

        with mock.patch("council_tools.evidence_backup.os.fsync", side_effect=recording_fsync):
            self.create()
        self.assertIn(stat.S_IFREG, synced_types)
        self.assertIn(stat.S_IFDIR, synced_types)
        self.assertGreaterEqual(synced_types.count(stat.S_IFREG), 6)
        self.assertGreaterEqual(synced_types.count(stat.S_IFDIR), 6)

    def test_restore_requires_clean_target_and_rehashes_restored_bytes(self):
        snapshot = self.base / "snapshot"
        created = self.create(snapshot)
        restore = self.base / "restore"
        result = restore_evidence_snapshot(snapshot, restore)
        self.assertEqual(result, created)
        self.assertEqual((restore / "ledger").read_bytes(), self.ledger.read_bytes())
        self.assertEqual(
            (restore / "resolution-store").read_bytes(), self.resolutions.read_bytes()
        )
        self.assertEqual(
            (restore / "control-store" / "overrides.jsonl").read_bytes(),
            (self.controls / "overrides.jsonl").read_bytes(),
        )
        self.assertEqual(
            (restore / "artifact-root" / "sha256" / "aa" / "answer.bin").read_bytes(),
            b"exact visible answer",
        )
        self.assertEqual(stat.S_IMODE((restore / "ledger").stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((restore / "control-store" / "overrides.jsonl").stat().st_mode),
            0o400,
        )

        nonempty = self.base / "nonempty"
        nonempty.mkdir()
        sentinel = nonempty / "do-not-overwrite"
        sentinel.write_text("untouched", encoding="utf-8")
        with self.assertRaises(RestoreError) as caught:
            restore_evidence_snapshot(snapshot, nonempty)
        self.assertEqual(caught.exception.code, "target-not-empty")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")

    def test_restore_rejects_target_inside_supplied_repository_root(self):
        snapshot = self.base / "snapshot"
        self.create(snapshot)
        target = self.repository / "restored-evidence"
        with self.assertRaises(RestoreError) as caught:
            restore_evidence_snapshot(
                snapshot,
                target,
                repository_root=self.repository,
            )
        self.assertEqual(caught.exception.code, "target-inside-repository")
        self.assertFalse(target.exists())

    def test_restore_is_transactional_when_copy_fails_midway(self):
        snapshot = self.base / "snapshot"
        self.create(snapshot)
        real_copy = evidence_backup._copy_manifested_file
        copy_count = 0

        def fail_after_one_file(*args, **kwargs):
            nonlocal copy_count
            copy_count += 1
            if copy_count == 2:
                raise OSError("injected mid-restore failure")
            return real_copy(*args, **kwargs)

        missing_target = self.base / "missing-restore"
        with mock.patch.object(
            evidence_backup, "_copy_manifested_file", side_effect=fail_after_one_file
        ):
            with self.assertRaises(RestoreError) as missing:
                restore_evidence_snapshot(snapshot, missing_target)
        self.assertEqual(missing.exception.code, "restore-failed")
        self.assertFalse(missing_target.exists())
        self.assertEqual(
            list(self.base.glob(f".{missing_target.name}.restore-*.tmp")), []
        )

        empty_target = self.base / "empty-restore"
        empty_target.mkdir(mode=0o750)
        copy_count = 0
        with mock.patch.object(
            evidence_backup, "_copy_manifested_file", side_effect=fail_after_one_file
        ):
            with self.assertRaises(RestoreError):
                restore_evidence_snapshot(snapshot, empty_target)
        self.assertTrue(empty_target.is_dir())
        self.assertEqual(list(empty_target.iterdir()), [])
        self.assertEqual(stat.S_IMODE(empty_target.stat().st_mode), 0o750)
        self.assertEqual(
            list(self.base.glob(f".{empty_target.name}.restore-*.tmp")), []
        )

    def test_successful_restore_atomically_replaces_existing_empty_target(self):
        snapshot = self.base / "snapshot"
        self.create(snapshot)
        target = self.base / "empty-target"
        target.mkdir(mode=0o750)
        original_inode = target.stat().st_ino
        result = restore_evidence_snapshot(snapshot, target)
        self.assertTrue(result["verified"])
        self.assertNotEqual(target.stat().st_ino, original_inode)
        self.assertEqual((target / "ledger").read_bytes(), self.ledger.read_bytes())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_restore_plain_directory_parent_swap_never_redirects_custody(self):
        snapshot = self.base / "snapshot"
        self.create(snapshot)
        restore_parent = self.base / "restore-parent"
        restore_parent.mkdir(mode=0o700)
        displaced_parent = self.base / "displaced-parent"
        outside = self.base / "outside"
        outside.mkdir(mode=0o700)
        target = restore_parent / "restore"
        live_before = {
            path.relative_to(self.live).as_posix(): path.read_bytes()
            for path in self.live.rglob("*")
            if path.is_file()
        }
        real_copy = evidence_backup._copy_manifested_file
        swapped = False

        def swap_parent_after_one_copy(*args, **kwargs):
            nonlocal swapped
            result = real_copy(*args, **kwargs)
            if not swapped:
                restore_parent.rename(displaced_parent)
                outside.rename(restore_parent)
                swapped = True
                self.assertEqual(list(restore_parent.iterdir()), [])
                self.assertEqual(list(self.repository.iterdir()), [])
            return result

        try:
            with mock.patch.object(
                evidence_backup,
                "_copy_manifested_file",
                side_effect=swap_parent_after_one_copy,
            ):
                with self.assertRaises(RestoreError) as caught:
                    restore_evidence_snapshot(
                        snapshot,
                        target,
                        repository_root=self.repository,
                    )
            self.assertIn(caught.exception.code, {"target-changed", "unsafe-destination"})
        finally:
            if swapped:
                restore_parent.rename(outside)
                displaced_parent.rename(restore_parent)

        self.assertFalse(target.exists())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(self.repository.iterdir()), [])
        self.assertEqual(
            {
                path.relative_to(self.live).as_posix(): path.read_bytes()
                for path in self.live.rglob("*")
                if path.is_file()
            },
            live_before,
        )
        self.assertEqual(
            [path for path in self.base.rglob("*") if ".restore-" in path.name],
            [],
        )

    def test_restore_symlink_parent_swap_and_late_failure_leave_no_evidence(self):
        snapshot = self.base / "snapshot"
        self.create(snapshot)
        restore_parent = self.base / "restore-parent"
        restore_parent.mkdir(mode=0o700)
        displaced_parent = self.base / "displaced-parent"
        target = restore_parent / "restore"
        live_before = {
            path.relative_to(self.live).as_posix(): path.read_bytes()
            for path in self.live.rglob("*")
            if path.is_file()
        }
        real_staging = evidence_backup._new_restore_staging
        real_copy = evidence_backup._copy_manifested_file
        copy_count = 0
        swapped = False

        def swap_to_repository_before_staging(*args, **kwargs):
            nonlocal swapped
            restore_parent.rename(displaced_parent)
            restore_parent.symlink_to(self.repository, target_is_directory=True)
            swapped = True
            staging = real_staging(*args, **kwargs)
            self.assertEqual(list(self.repository.iterdir()), [])
            return staging

        def swap_to_repository_then_fail(*args, **kwargs):
            nonlocal copy_count
            copy_count += 1
            result = real_copy(*args, **kwargs)
            if copy_count == 2:
                self.assertEqual(list(self.repository.iterdir()), [])
                raise OSError("injected failure after parent symlink swap")
            return result

        try:
            with mock.patch.object(
                evidence_backup,
                "_new_restore_staging",
                side_effect=swap_to_repository_before_staging,
            ):
                with mock.patch.object(
                    evidence_backup,
                    "_copy_manifested_file",
                    side_effect=swap_to_repository_then_fail,
                ):
                    with self.assertRaises(RestoreError) as caught:
                        restore_evidence_snapshot(
                            snapshot,
                            target,
                            repository_root=self.repository,
                        )
            self.assertEqual(caught.exception.code, "restore-failed")
        finally:
            if swapped:
                restore_parent.unlink()
                displaced_parent.rename(restore_parent)

        self.assertFalse(target.exists())
        self.assertEqual(list(self.repository.iterdir()), [])
        self.assertEqual(
            {
                path.relative_to(self.live).as_posix(): path.read_bytes()
                for path in self.live.rglob("*")
                if path.is_file()
            },
            live_before,
        )
        self.assertEqual(
            [path for path in self.base.rglob("*") if ".restore-" in path.name],
            [],
        )

    def test_restore_withdraws_exact_published_inode_on_parent_fsync_failure(self):
        snapshot = self.base / "snapshot"
        self.create(snapshot)
        target = self.base / "restore-with-late-failure"
        real_fsync_directory_fd = evidence_backup._fsync_directory_fd

        def fail_after_publication(fd):
            if target.name in os.listdir(fd):
                raise OSError("injected destination parent fsync failure")
            return real_fsync_directory_fd(fd)

        with mock.patch.object(
            evidence_backup,
            "_fsync_directory_fd",
            side_effect=fail_after_publication,
        ):
            with self.assertRaises(RestoreError) as caught:
                restore_evidence_snapshot(
                    snapshot,
                    target,
                    repository_root=self.repository,
                )
        self.assertEqual(caught.exception.code, "target-fsync-failed")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.repository.iterdir()), [])
        self.assertEqual(
            [path for path in self.base.rglob("*") if ".restore-" in path.name],
            [],
        )

    def test_snapshot_requires_exact_source_root_types(self):
        cases = (
            ("ledger", {"ledger_path": self.controls}),
            ("resolution-store", {"resolution_store_path": self.controls}),
            ("control-store", {"control_store_path": self.ledger}),
            ("artifact-root", {"artifact_root": self.ledger}),
        )
        defaults = {
            "ledger_path": self.ledger,
            "resolution_store_path": self.resolutions,
            "control_store_path": self.controls,
            "artifact_root": self.artifacts,
            "lock_path": self.lock,
            "repository_root": self.repository,
        }
        for index, (stage, override) in enumerate(cases):
            with self.subTest(stage=stage):
                target = self.base / f"wrong-source-type-{index}"
                arguments = {**defaults, **override, "snapshot_target": target}
                with self.assertRaises(SnapshotPolicyError) as caught:
                    create_evidence_snapshot(**arguments)
                self.assertEqual(caught.exception.code, "wrong-source-type")
                self.assertEqual(caught.exception.stage, stage)
                self.assertFalse(target.exists())

    def test_snapshot_and_restore_do_not_mutate_live_evidence(self):
        def state(path):
            item = path.stat()
            content = path.read_bytes() if path.is_file() else None
            return (
                item.st_ino,
                stat.S_IMODE(item.st_mode),
                item.st_size,
                item.st_mtime_ns,
                content,
            )

        live_paths = [
            self.ledger,
            self.resolutions,
            self.controls,
            self.controls / "overrides.jsonl",
            self.artifacts,
            self.artifacts / "sha256" / "aa" / "answer.bin",
            self.lock,
        ]
        before = {path: state(path) for path in live_paths}
        snapshot = self.base / "snapshot"
        self.create(snapshot)
        restore_evidence_snapshot(snapshot, self.base / "restore")
        after = {path: state(path) for path in live_paths}
        self.assertEqual(after, before)

    def test_tamper_partial_mode_and_alias_fail_verification(self):
        cases = (
            "tamper",
            "partial",
            "mode",
            "hard-link",
            "symlink",
            "manifest-traversal",
        )
        for case in cases:
            with self.subTest(case=case):
                snapshot = self.base / f"snapshot-{case}"
                self.create(snapshot)
                payload_file = snapshot / PAYLOAD_NAME / "ledger"
                if case == "tamper":
                    payload_file.write_bytes(b'{"kind":"tampered"}\n')
                    os.chmod(payload_file, 0o600)
                elif case == "partial":
                    (snapshot / COMPLETION_NAME).unlink()
                elif case == "mode":
                    os.chmod(payload_file, 0o644)
                elif case == "hard-link":
                    os.link(payload_file, self.base / "snapshot-alias")
                elif case == "symlink":
                    alternate = self.base / "alternate-ledger"
                    alternate.write_bytes(payload_file.read_bytes())
                    os.chmod(alternate, 0o600)
                    payload_file.unlink()
                    payload_file.symlink_to(alternate)
                else:
                    manifest_path = snapshot / MANIFEST_NAME
                    manifest = json.loads(manifest_path.read_bytes())
                    ledger_root = next(
                        entry
                        for entry in manifest["entries"]
                        if entry["source"] == "ledger" and entry["path"] == "."
                    )
                    ledger_root["path"] = "../escape"
                    manifest_bytes = evidence_backup._canonical_json(manifest)
                    manifest_path.write_bytes(manifest_bytes)
                    os.chmod(manifest_path, 0o600)
                    completion_path = snapshot / COMPLETION_NAME
                    completion = json.loads(completion_path.read_bytes())
                    completion["manifestSha256"] = hashlib.sha256(manifest_bytes).hexdigest()
                    completion["manifestSize"] = len(manifest_bytes)
                    completion_path.write_bytes(evidence_backup._canonical_json(completion))
                    os.chmod(completion_path, 0o600)
                with self.assertRaises(SnapshotIntegrityError):
                    verify_evidence_snapshot(snapshot)

    def test_source_symlink_hardlink_traversal_and_special_files_are_rejected(self):
        alias = self.base / "ledger-alias"
        os.link(self.ledger, alias)
        with self.assertRaises(SnapshotIntegrityError) as hardlink:
            self.create(self.base / "hardlink-snapshot")
        self.assertEqual(hardlink.exception.code, "hard-link-alias")
        alias.unlink()

        real_ledger = self.base / "real-ledger"
        real_ledger.write_bytes(self.ledger.read_bytes())
        self.ledger.unlink()
        self.ledger.symlink_to(real_ledger)
        with self.assertRaises(SnapshotPolicyError) as symlink:
            self.create(self.base / "symlink-snapshot")
        self.assertEqual(symlink.exception.code, "symlink-alias")
        self.ledger.unlink()
        self.ledger.write_bytes(b'{"kind":"one"}\n')
        os.chmod(self.ledger, 0o600)

        fifo = self.live / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(SnapshotPolicyError) as special:
            create_evidence_snapshot(
                ledger_path=fifo,
                resolution_store_path=self.resolutions,
                control_store_path=self.controls,
                artifact_root=self.artifacts,
                lock_path=self.lock,
                snapshot_target=self.base / "fifo-snapshot",
                repository_root=self.repository,
            )
        self.assertEqual(special.exception.code, "special-file")

        traversal = str(self.base / "live" / ".." / "live" / "council.jsonl")
        with self.assertRaises(SnapshotPolicyError) as escaped:
            create_evidence_snapshot(
                ledger_path=traversal,
                resolution_store_path=self.resolutions,
                control_store_path=self.controls,
                artifact_root=self.artifacts,
                lock_path=self.lock,
                snapshot_target=self.base / "traversal-snapshot",
                repository_root=self.repository,
            )
        self.assertEqual(escaped.exception.code, "path-traversal")

    def test_target_must_be_new_external_and_have_no_symlink_parent(self):
        inside_repo = self.repository / "snapshot"
        with self.assertRaises(SnapshotPolicyError) as repository:
            self.create(inside_repo)
        self.assertEqual(repository.exception.code, "target-inside-repository")

        existing = self.base / "existing"
        existing.mkdir()
        with self.assertRaises(SnapshotPolicyError) as exists:
            self.create(existing)
        self.assertEqual(exists.exception.code, "target-exists")

        real_parent = self.base / "real-parent"
        real_parent.mkdir()
        linked_parent = self.base / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(SnapshotPolicyError) as symlink:
            self.create(linked_parent / "snapshot")
        self.assertEqual(symlink.exception.code, "symlink-alias")

    def test_snapshot_destination_parent_swap_never_redirects_bytes(self):
        real_create_directory = evidence_backup._create_pinned_directory
        real_write_metadata = evidence_backup._write_private_file_at

        for replacement_kind in ("plain-directory", "symlink"):
            for fail_late in (False, True):
                with self.subTest(
                    replacement_kind=replacement_kind,
                    fail_late=fail_late,
                ):
                    suffix = f"{replacement_kind}-{int(fail_late)}"
                    approved_parent = self.base / f"approved-{suffix}"
                    approved_parent.mkdir(mode=0o700)
                    displaced_parent = self.base / f"approved-{suffix}-displaced"
                    outside_parent = self.base / f"outside-{suffix}"
                    outside_parent.mkdir(mode=0o700)
                    target = approved_parent / "snapshot"
                    swapped = False

                    def swap_parent_before_target_create(parent_fd, name):
                        nonlocal swapped
                        if not swapped and name == target.name:
                            swapped = True
                            approved_parent.rename(displaced_parent)
                            if replacement_kind == "plain-directory":
                                approved_parent.mkdir(mode=0o700)
                            else:
                                approved_parent.symlink_to(
                                    outside_parent,
                                    target_is_directory=True,
                                )
                        return real_create_directory(parent_fd, name)

                    def fail_after_manifest(parent_fd, name, content, **kwargs):
                        result = real_write_metadata(
                            parent_fd,
                            name,
                            content,
                            **kwargs,
                        )
                        if fail_late and name == MANIFEST_NAME:
                            raise OSError("injected post-manifest failure")
                        return result

                    with (
                        mock.patch.object(
                            evidence_backup,
                            "_create_pinned_directory",
                            side_effect=swap_parent_before_target_create,
                        ),
                        mock.patch.object(
                            evidence_backup,
                            "_write_private_file_at",
                            side_effect=fail_after_manifest,
                        ),
                        redirect_stdout(io.StringIO()),
                    ):
                        args = SimpleNamespace(
                            log=str(self.ledger),
                            events=str(self.resolutions),
                            control_store=str(self.controls),
                            artifact_root=str(self.artifacts),
                            coordination_lock=str(self.lock),
                            target=str(target),
                            repository_root=str(self.repository),
                        )
                        if fail_late:
                            with self.assertRaises(evidence_backup.SnapshotWriteError):
                                cli.command_evidence_snapshot(args)
                        else:
                            self.assertEqual(cli.command_evidence_snapshot(args), 0)

                    self.assertTrue(swapped)
                    self.assertEqual(list(outside_parent.iterdir()), [])
                    if replacement_kind == "plain-directory":
                        self.assertEqual(list(approved_parent.iterdir()), [])
                    else:
                        self.assertFalse((approved_parent / "snapshot").exists())

                    pinned_snapshot = displaced_parent / "snapshot"
                    self.assertTrue(pinned_snapshot.is_dir())
                    self.assertTrue((pinned_snapshot / MANIFEST_NAME).is_file())
                    self.assertEqual(
                        (pinned_snapshot / COMPLETION_NAME).exists(),
                        not fail_late,
                    )

    def test_snapshot_pins_parent_before_repository_and_source_policy(self):
        real_source_paths = evidence_backup._source_paths

        for protected_name in ("repository", "control-store"):
            with self.subTest(protected_name=protected_name):
                approved_parent = self.base / f"prepin-{protected_name}"
                approved_parent.mkdir(mode=0o700)
                target = approved_parent / "snapshot"
                protected = (
                    self.repository
                    if protected_name == "repository"
                    else self.controls
                )
                exchange = self.base / f"exchange-{protected_name}"
                swapped = False

                def validate_then_exchange(*args, **kwargs):
                    nonlocal swapped
                    sources = real_source_paths(*args, **kwargs)
                    approved_parent.rename(exchange)
                    protected.rename(approved_parent)
                    exchange.rename(protected)
                    swapped = True
                    return sources

                try:
                    with mock.patch.object(
                        evidence_backup,
                        "_source_paths",
                        side_effect=validate_then_exchange,
                    ):
                        with self.assertRaises(SnapshotPolicyError) as caught:
                            self.create(target)
                    self.assertTrue(swapped)
                    self.assertIn(
                        caught.exception.code,
                        {"target-inside-repository", "target-overlaps-source"},
                    )
                    self.assertFalse(target.exists())
                    self.assertFalse((target / MANIFEST_NAME).exists())
                    self.assertFalse((target / COMPLETION_NAME).exists())
                    self.assertEqual(
                        [path for path in approved_parent.rglob("*") if path.name == "snapshot"],
                        [],
                    )
                finally:
                    if swapped:
                        protected.rename(exchange)
                        approved_parent.rename(protected)
                        exchange.rename(approved_parent)

    def test_snapshot_metadata_duplicate_keys_are_cli_generic_and_nonleaking(self):
        secret_key = "AWS_SECRET_ACCESS_KEY_SUPER_PRIVATE"
        project_root = Path(__file__).resolve().parents[1]

        for metadata_name in (MANIFEST_NAME, COMPLETION_NAME):
            with self.subTest(metadata_name=metadata_name):
                snapshot = self.base / f"duplicate-{metadata_name}"
                self.create(snapshot)
                metadata_path = snapshot / metadata_name
                original = metadata_path.read_bytes()
                duplicate = (
                    b'{"'
                    + secret_key.encode("ascii")
                    + b'":1,"'
                    + secret_key.encode("ascii")
                    + b'":2,'
                    + original[1:]
                )
                metadata_path.write_bytes(duplicate)
                os.chmod(metadata_path, 0o600)
                if metadata_name == MANIFEST_NAME:
                    completion_path = snapshot / COMPLETION_NAME
                    completion = json.loads(completion_path.read_bytes())
                    completion["manifestSha256"] = hashlib.sha256(duplicate).hexdigest()
                    completion["manifestSize"] = len(duplicate)
                    completion_path.write_bytes(
                        evidence_backup._canonical_json(completion)
                    )
                    os.chmod(completion_path, 0o600)

                process = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "council_tools.cli",
                        "evidence-verify",
                        str(snapshot),
                    ],
                    cwd=project_root,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(project_root / "src"),
                    },
                    check=False,
                    capture_output=True,
                    text=True,
                )
                combined = process.stdout + process.stderr
                self.assertEqual(process.returncode, 1)
                self.assertIn("duplicate-json-key", combined)
                self.assertNotIn(secret_key, combined)

    def test_snapshot_metadata_excessive_depth_is_generic_and_nonleaking(self):
        secret = "AWS_SECRET_ACCESS_KEY_DEEP_PRIVATE"
        snapshot = self.base / "deep-metadata"
        self.create(snapshot)
        completion_path = snapshot / COMPLETION_NAME
        deep = (
            b'{"deep":'
            + (b"[" * 2000)
            + json.dumps(secret).encode("ascii")
            + (b"]" * 2000)
            + b"}\n"
        )
        completion_path.write_bytes(deep)
        os.chmod(completion_path, 0o600)

        with self.assertRaises(SnapshotIntegrityError) as caught:
            verify_evidence_snapshot(snapshot)
        self.assertEqual(caught.exception.code, "invalid-json")
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIsInstance(caught.exception.__cause__, RecursionError)

        project_root = Path(__file__).resolve().parents[1]
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "council_tools.cli",
                "evidence-verify",
                str(snapshot),
            ],
            cwd=project_root,
            env={**os.environ, "PYTHONPATH": str(project_root / "src")},
            check=False,
            capture_output=True,
            text=True,
        )
        combined = process.stdout + process.stderr
        self.assertEqual(process.returncode, 1)
        self.assertIn("invalid-json", combined)
        self.assertNotIn("RecursionError", combined)
        self.assertNotIn(secret, combined)

    def test_snapshot_rejects_symlinked_and_hardlinked_coordination_locks(self):
        self.lock.unlink()
        external = self.live / "external-lock"
        external.write_bytes(b"")
        os.chmod(external, 0o600)

        self.lock.symlink_to(external)
        with self.assertRaises(SnapshotIntegrityError) as symlink:
            self.create(self.base / "snapshot-symlink-lock")
        self.assertEqual(symlink.exception.code, "unsafe-lock")
        self.assertFalse((self.base / "snapshot-symlink-lock").exists())
        self.lock.unlink()

        os.link(external, self.lock)
        with self.assertRaises(SnapshotIntegrityError) as hardlink:
            self.create(self.base / "snapshot-hardlink-lock")
        self.assertEqual(hardlink.exception.code, "unsafe-lock")
        self.assertFalse((self.base / "snapshot-hardlink-lock").exists())

    def test_snapshot_is_wholly_before_or_after_locked_append(self):
        before_snapshot = self.base / "snapshot-before"
        hook_entered = threading.Event()
        release_hook = threading.Event()
        real_copy_source = evidence_backup._copy_source

        def delayed_copy(*args, **kwargs):
            if not hook_entered.is_set():
                hook_entered.set()
                if not release_hook.wait(timeout=10):
                    raise AssertionError("test did not release snapshot copy")
            return real_copy_source(*args, **kwargs)

        snapshot_errors = []

        def take_before_snapshot():
            try:
                with mock.patch.object(evidence_backup, "_copy_source", side_effect=delayed_copy):
                    self.create(before_snapshot)
            except Exception as exc:  # asserted in the parent thread
                snapshot_errors.append(exc)

        snapshot_thread = threading.Thread(target=take_before_snapshot)
        snapshot_thread.start()
        self.assertTrue(hook_entered.wait(timeout=10))
        writer_done = self.base / "writer-before-done"
        writer = self._start_locked_writer(b'{"kind":"two"}\n', done=writer_done)
        time.sleep(0.1)
        self.assertFalse(writer_done.exists(), "exclusive writer passed a shared snapshot lock")
        release_hook.set()
        snapshot_thread.join(timeout=10)
        writer.wait(timeout=10)
        self.assertEqual(snapshot_errors, [])
        self.assertEqual(writer.returncode, 0)
        self.assertEqual(
            (before_snapshot / PAYLOAD_NAME / "ledger").read_bytes(),
            b'{"kind":"one"}\n',
        )

        # Hold the exclusive append lock first.  The snapshot cannot enter until
        # the appended row has been fsynced and the writer releases it.
        after_snapshot = self.base / "snapshot-after"
        writer_locked = self.base / "writer-after-locked"
        writer_go = self.base / "writer-after-go"
        writer = self._start_locked_writer(
            b'{"kind":"three"}\n', locked=writer_locked, go=writer_go
        )
        self._wait_for(writer_locked)
        after_errors = []

        def take_after_snapshot():
            try:
                self.create(after_snapshot)
            except Exception as exc:  # asserted in the parent thread
                after_errors.append(exc)

        snapshot_thread = threading.Thread(target=take_after_snapshot)
        snapshot_thread.start()
        time.sleep(0.1)
        self.assertTrue(snapshot_thread.is_alive())
        writer_go.touch()
        writer.wait(timeout=10)
        snapshot_thread.join(timeout=10)
        self.assertEqual(writer.returncode, 0)
        self.assertEqual(after_errors, [])
        self.assertEqual(
            (after_snapshot / PAYLOAD_NAME / "ledger").read_bytes(),
            b'{"kind":"one"}\n{"kind":"two"}\n{"kind":"three"}\n',
        )

    def test_replaced_coordination_lock_cannot_yield_verified_mixed_snapshot(self):
        snapshot = self.base / "snapshot-replaced-lock"
        retired_lock = self.lock.with_name(f"{self.lock.name}.retired")
        artifact_copied = threading.Event()
        release_snapshot = threading.Event()
        writer_started = threading.Event()
        writer_entered = threading.Event()
        snapshot_errors: list[BaseException] = []
        writer_errors: list[BaseException] = []
        real_copy_source = evidence_backup._copy_source

        def pause_after_artifact(*args, **kwargs):
            result = real_copy_source(*args, **kwargs)
            source_name = args[0] if args else kwargs["source_name"]
            if source_name == "artifact-root":
                artifact_copied.set()
                if not release_snapshot.wait(timeout=5):
                    raise AssertionError("test did not release snapshot copy")
            return result

        def take_snapshot():
            try:
                with mock.patch.object(
                    evidence_backup,
                    "_copy_source",
                    side_effect=pause_after_artifact,
                ):
                    self.create(snapshot)
            except BaseException as exc:
                snapshot_errors.append(exc)

        def mutate_related_evidence():
            writer_started.set()
            try:
                with forecasts.evidence_write_lock(self.lock):
                    writer_entered.set()
                    artifact = self.artifacts / "sha256" / "aa" / "after.bin"
                    artifact.write_bytes(b"after-artifact")
                    os.chmod(artifact, 0o600)
                    with self.ledger.open("ab") as stream:
                        stream.write(b'{"kind":"after"}\n')
                        stream.flush()
                        os.fsync(stream.fileno())
            except BaseException as exc:
                writer_errors.append(exc)

        snapshot_thread = threading.Thread(target=take_snapshot, name="snapshot")
        snapshot_thread.start()
        self.assertTrue(artifact_copied.wait(timeout=5))

        # An uncooperative actor swaps the directory entry.  The snapshot still
        # holds a shared flock on the pinned parent inode, so a cooperating
        # writer cannot enter through the replacement lock file.
        self.lock.rename(retired_lock)
        self.lock.write_bytes(b"")
        os.chmod(self.lock, 0o600)
        writer_thread = threading.Thread(
            target=mutate_related_evidence,
            name="writer-after-lock-replacement",
        )
        writer_thread.start()
        self.assertTrue(writer_started.wait(timeout=5))
        self.assertFalse(writer_entered.wait(timeout=0.1))

        release_snapshot.set()
        snapshot_thread.join(timeout=5)
        writer_thread.join(timeout=5)

        self.assertFalse(snapshot_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(writer_errors, [])
        self.assertTrue(writer_entered.is_set())
        self.assertEqual(len(snapshot_errors), 1)
        self.assertIsInstance(snapshot_errors[0], SnapshotIntegrityError)
        self.assertEqual(snapshot_errors[0].code, "unsafe-lock")
        self.assertFalse((snapshot / COMPLETION_NAME).exists())
        with self.assertRaises(SnapshotIntegrityError):
            verify_evidence_snapshot(snapshot)

        # The aborted payload is an old coherent cut, never the formerly
        # reproducible old-artifact/new-ledger mixture.
        self.assertEqual(
            (snapshot / PAYLOAD_NAME / "ledger").read_bytes(),
            b'{"kind":"one"}\n',
        )
        self.assertFalse(
            (snapshot / PAYLOAD_NAME / "artifact-root" / "sha256" / "aa" / "after.bin").exists()
        )
        self.assertEqual(
            self.ledger.read_bytes(),
            b'{"kind":"one"}\n{"kind":"after"}\n',
        )

    def _start_locked_writer(self, content, *, locked=None, go=None, done=None):
        script = r"""
import fcntl
import os
import sys
import time
from pathlib import Path

lock_path, ledger_path, content_hex, locked_path, go_path, done_path = sys.argv[1:]
with open(lock_path, "a+b") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    if locked_path != "-":
        Path(locked_path).touch()
    if go_path != "-":
        while not Path(go_path).exists():
            time.sleep(0.01)
    with open(ledger_path, "ab") as ledger:
        ledger.write(bytes.fromhex(content_hex))
        ledger.flush()
        os.fsync(ledger.fileno())
    if done_path != "-":
        Path(done_path).touch()
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
"""
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.lock),
                str(self.ledger),
                content.hex(),
                str(locked) if locked is not None else "-",
                str(go) if go is not None else "-",
                str(done) if done is not None else "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _wait_for(self, path):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")


if __name__ == "__main__":
    unittest.main()
