import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from council_tools import forecasts, safe_files


class CrashAtomicPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.target = self.root / "capture.jsonl"

    def temporary_entries(self):
        return tuple(self.root.glob(f".{self.target.name}.*.tmp"))

    def escrow_entries(self, target=None):
        target = target or self.target
        return tuple(self.root.glob(f".{target.name}.*.tmp.escrow.*"))

    def test_existing_jsonl_append_exchanges_and_reports_recoverable_escrow(self):
        self.target.write_bytes(b'{"first":1}\n')
        self.target.chmod(0o640)

        escrow = safe_files.atomic_append_bytes(
            self.target,
            b'{"second":2}\n',
            require_trailing_newline=True,
        )

        self.assertEqual(
            self.target.read_bytes(),
            b'{"first":1}\n{"second":2}\n',
        )
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)
        self.assertEqual(self.temporary_entries(), ())
        self.assertIsNotNone(escrow)
        assert escrow is not None
        self.assertEqual(escrow.read_bytes(), b'{"first":1}\n')
        self.assertEqual(self.escrow_entries(), (escrow,))

    def test_multi_append_receipts_exactly_match_read_only_inventory(self):
        first = b'{"generation":1}\n'
        second = b'{"generation":2}\n'
        third = b'{"generation":3}\n'

        self.assertIsNone(
            safe_files.atomic_append_bytes(
                self.target, first, require_trailing_newline=True
            )
        )
        first_escrow = safe_files.atomic_append_bytes(
            self.target, second, require_trailing_newline=True
        )
        second_escrow = safe_files.atomic_append_bytes(
            self.target, third, require_trailing_newline=True
        )

        self.assertIsNotNone(first_escrow)
        self.assertIsNotNone(second_escrow)
        inventory = safe_files.inventory_transaction_escrows(self.target)
        self.assertEqual(
            {entry.path for entry in inventory},
            {first_escrow, second_escrow},
        )
        self.assertEqual(
            {entry.path: entry.size for entry in inventory},
            {
                first_escrow: len(first),
                second_escrow: len(first + second),
            },
        )
        self.assertTrue(all(entry.entry_type == "regular" for entry in inventory))
        self.assertEqual(self.target.read_bytes(), first + second + third)

    def test_absent_jsonl_target_uses_no_replace_and_fsyncs_parent(self):
        synchronized = []

        safe_files.atomic_append_bytes(
            self.target,
            b'{"first":1}\n',
            require_trailing_newline=True,
            on_directory_fsync=synchronized.append,
        )

        self.assertEqual(self.target.read_bytes(), b'{"first":1}\n')
        self.assertEqual(synchronized, [self.root])
        self.assertEqual(self.temporary_entries(), ())
        self.assertEqual(self.escrow_entries(), ())

    def test_destination_substitution_at_exchange_is_reversed_without_overwrite(self):
        self.target.write_bytes(b'{"validated":true}\n')
        validated_original = self.root / "validated-original.jsonl"
        concurrent_bytes = b'{"concurrent":true}\n'
        concurrent_identity = None
        real_exchange = safe_files._exchange_names_pinned
        injected = False

        def substitute_then_exchange(parent, source_name, target_name):
            nonlocal concurrent_identity, injected
            if not injected:
                injected = True
                os.rename(
                    target_name,
                    validated_original.name,
                    src_dir_fd=parent.descriptor,
                    dst_dir_fd=parent.descriptor,
                )
                descriptor = os.open(
                    target_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent.descriptor,
                )
                try:
                    safe_files._write_all(descriptor, concurrent_bytes)
                    os.fsync(descriptor)
                    info = os.fstat(descriptor)
                    concurrent_identity = (info.st_dev, info.st_ino)
                finally:
                    os.close(descriptor)
            real_exchange(parent, source_name, target_name)

        with mock.patch.object(
            safe_files,
            "_exchange_names_pinned",
            side_effect=substitute_then_exchange,
        ):
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "concurrent file substitution detected and reversed",
            ):
                safe_files.atomic_append_bytes(
                    self.target,
                    b'{"must_not_publish":true}\n',
                    require_trailing_newline=True,
                )

        self.assertTrue(injected)
        self.assertEqual(self.target.read_bytes(), concurrent_bytes)
        self.assertEqual(
            (self.target.stat().st_dev, self.target.stat().st_ino),
            concurrent_identity,
        )
        self.assertEqual(validated_original.read_bytes(), b'{"validated":true}\n')
        self.assertEqual(self.temporary_entries(), ())
        escrows = self.escrow_entries()
        self.assertEqual(len(escrows), 1)
        self.assertEqual(
            escrows[0].read_bytes(),
            b'{"validated":true}\n{"must_not_publish":true}\n',
        )

    def test_foreign_inode_repopulating_temporary_name_is_retained(self):
        original_bytes = b'{"original":true}\n'
        foreign_bytes = b'{"foreign":true}\n'
        self.target.write_bytes(original_bytes)
        retained_owned = self.root / "retained-owned-escrow.jsonl"
        foreign_path = None
        foreign_identity = None
        real_retain = safe_files._retain_escrow_name
        injected = False

        def repopulate_before_retention(parent, name, expected_identity):
            nonlocal foreign_path, foreign_identity, injected
            if not injected:
                injected = True
                foreign_path = self.root / name
                os.rename(
                    name,
                    retained_owned.name,
                    src_dir_fd=parent.descriptor,
                    dst_dir_fd=parent.descriptor,
                )
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent.descriptor,
                )
                try:
                    safe_files._write_all(descriptor, foreign_bytes)
                    os.fsync(descriptor)
                    info = os.fstat(descriptor)
                    foreign_identity = (info.st_dev, info.st_ino)
                finally:
                    os.close(descriptor)
            return real_retain(parent, name, expected_identity)

        with mock.patch.object(
            safe_files,
            "_retain_escrow_name",
            side_effect=repopulate_before_retention,
        ):
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "temporary escrow identity changed; recoverable entry retained",
            ):
                safe_files.atomic_append_bytes(
                    self.target,
                    b'{"published":true}\n',
                    require_trailing_newline=True,
                )

        self.assertTrue(injected)
        self.assertEqual(
            self.target.read_bytes(),
            original_bytes + b'{"published":true}\n',
        )
        self.assertEqual(retained_owned.read_bytes(), original_bytes)
        self.assertIsNotNone(foreign_path)
        assert foreign_path is not None
        self.assertFalse(foreign_path.exists())
        foreign_escrows = self.escrow_entries()
        self.assertEqual(len(foreign_escrows), 1)
        self.assertEqual(foreign_escrows[0].read_bytes(), foreign_bytes)
        self.assertEqual(
            (foreign_escrows[0].stat().st_dev, foreign_escrows[0].stat().st_ino),
            foreign_identity,
        )

    def test_concurrent_creation_at_absent_target_is_not_overwritten(self):
        concurrent_bytes = b'{"concurrent":true}\n'
        real_noreplace = safe_files._rename_noreplace_pinned
        injected = False

        def create_then_publish(parent, source_name, target_name):
            nonlocal injected
            if not injected:
                injected = True
                descriptor = os.open(
                    target_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent.descriptor,
                )
                try:
                    safe_files._write_all(descriptor, concurrent_bytes)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            real_noreplace(parent, source_name, target_name)

        with mock.patch.object(
            safe_files,
            "_rename_noreplace_pinned",
            side_effect=create_then_publish,
        ):
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "file appeared before atomic publication",
            ):
                safe_files.atomic_append_bytes(
                    self.target,
                    b'{"must_not_publish":true}\n',
                    require_trailing_newline=True,
                )

        self.assertTrue(injected)
        self.assertEqual(self.target.read_bytes(), concurrent_bytes)
        self.assertEqual(self.temporary_entries(), ())
        escrows = self.escrow_entries()
        self.assertEqual(len(escrows), 1)
        self.assertEqual(escrows[0].read_bytes(), b'{"must_not_publish":true}\n')

    def test_directory_fsync_failure_reverses_existing_publication(self):
        original_bytes = b'{"original":true}\n'
        self.target.write_bytes(original_bytes)
        real_fsync = safe_files._fsync_fd
        failed_once = False

        def fail_first_directory_fsync(descriptor):
            nonlocal failed_once
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not failed_once:
                failed_once = True
                raise OSError("injected directory fsync failure")
            real_fsync(descriptor)

        with mock.patch.object(
            safe_files,
            "_fsync_fd",
            side_effect=fail_first_directory_fsync,
        ):
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "durability failed and was reversed",
            ):
                safe_files.atomic_append_bytes(
                    self.target,
                    b'{"must_not_publish":true}\n',
                    require_trailing_newline=True,
                )

        self.assertTrue(failed_once)
        self.assertEqual(self.target.read_bytes(), original_bytes)
        self.assertEqual(self.temporary_entries(), ())
        escrows = self.escrow_entries()
        self.assertEqual(len(escrows), 1)
        self.assertEqual(
            escrows[0].read_bytes(),
            original_bytes + b'{"must_not_publish":true}\n',
        )

    def test_torn_jsonl_tail_still_fails_without_publication(self):
        self.target.write_bytes(b'{"torn":true}')
        original_identity = (self.target.stat().st_dev, self.target.stat().st_ino)

        with self.assertRaisesRegex(safe_files.SafeFileError, "torn trailing record"):
            safe_files.atomic_append_bytes(
                self.target,
                b'{"must_not_publish":true}\n',
                require_trailing_newline=True,
            )

        self.assertEqual(self.target.read_bytes(), b'{"torn":true}')
        self.assertEqual(
            (self.target.stat().st_dev, self.target.stat().st_ino),
            original_identity,
        )
        self.assertEqual(self.temporary_entries(), ())
        self.assertEqual(self.escrow_entries(), ())

    def test_renameat2_unavailable_fails_closed_before_temporary_creation(self):
        self.target.write_bytes(b'{"original":true}\n')

        with mock.patch.object(safe_files, "_RENAMEAT2", None):
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "requires Linux renameat2 support",
            ):
                safe_files.atomic_append_bytes(
                    self.target,
                    b'{"must_not_publish":true}\n',
                    require_trailing_newline=True,
                )

        self.assertEqual(self.target.read_bytes(), b'{"original":true}\n')
        self.assertEqual(self.temporary_entries(), ())
        self.assertEqual(self.escrow_entries(), ())

    def test_exclusive_create_publishes_authenticated_bytes_and_fsyncs_parent(self):
        quarantine = self.root / "capture.quarantine"
        payload = b'{"complete":"original-ledger"}\n'
        synchronized = []

        result = safe_files.create_bytes_exclusive(
            quarantine,
            payload,
            mode=0o640,
            on_directory_fsync=synchronized.append,
        )

        self.assertIsNone(result)
        self.assertEqual(quarantine.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(quarantine.stat().st_mode), 0o640)
        self.assertEqual(synchronized, [self.root])
        self.assertEqual(self.escrow_entries(quarantine), ())

    def test_exclusive_create_never_mutates_existing_quarantine(self):
        quarantine = self.root / "capture.quarantine"
        existing = b"operator-retained-existing-backup"
        quarantine.write_bytes(existing)
        identity = (quarantine.stat().st_dev, quarantine.stat().st_ino)

        with self.assertRaisesRegex(safe_files.SafeFileError, "already exists"):
            safe_files.create_bytes_exclusive(
                quarantine,
                b"must-not-replace",
            )

        self.assertEqual(quarantine.read_bytes(), existing)
        self.assertEqual(
            (quarantine.stat().st_dev, quarantine.stat().st_ino), identity
        )
        self.assertEqual(self.escrow_entries(quarantine), ())

    def test_exclusive_create_substitution_during_old_handoff_never_advertises_foreign(self):
        quarantine = self.root / "capture.quarantine"
        payload = b"authenticated-quarantine-payload"
        foreign = b"foreign-staging-payload"
        authenticated_retained = self.root / "authenticated-retained"
        real_write = safe_files._write_all
        foreign_identity = None
        injected = False

        def substitute_staging_then_write(descriptor, data):
            nonlocal foreign_identity, injected
            if not injected:
                injected = True
                staged = tuple(self.root.glob(f".{quarantine.name}.*.tmp"))
                self.assertEqual(len(staged), 1)
                staged[0].rename(authenticated_retained)
                foreign_descriptor = os.open(
                    staged[0],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    os.write(foreign_descriptor, foreign)
                    os.fsync(foreign_descriptor)
                    info = os.fstat(foreign_descriptor)
                    foreign_identity = (info.st_dev, info.st_ino)
                finally:
                    os.close(foreign_descriptor)
            real_write(descriptor, data)

        with mock.patch.object(
            safe_files,
            "_write_all",
            side_effect=substitute_staging_then_write,
        ):
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "exclusive staging identity changed before publication",
            ):
                safe_files.create_bytes_exclusive(quarantine, payload)

        self.assertTrue(injected)
        self.assertFalse(quarantine.exists())
        self.assertEqual(authenticated_retained.read_bytes(), payload)
        foreign_escrows = self.escrow_entries(quarantine)
        self.assertEqual(len(foreign_escrows), 1)
        self.assertEqual(foreign_escrows[0].read_bytes(), foreign)
        self.assertEqual(
            (foreign_escrows[0].stat().st_dev, foreign_escrows[0].stat().st_ino),
            foreign_identity,
        )

    def test_exclusive_create_concurrent_target_is_preserved_and_stage_escrowed(self):
        quarantine = self.root / "capture.quarantine"
        payload = b"authenticated-quarantine-payload"
        concurrent = b"concurrent-quarantine"
        real_noreplace = safe_files._rename_noreplace_pinned
        injected = False

        def create_target_then_publish(parent, source_name, target_name):
            nonlocal injected
            if target_name == quarantine.name and not injected:
                injected = True
                descriptor = os.open(
                    target_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent.descriptor,
                )
                try:
                    os.write(descriptor, concurrent)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            real_noreplace(parent, source_name, target_name)

        with mock.patch.object(
            safe_files,
            "_rename_noreplace_pinned",
            side_effect=create_target_then_publish,
        ):
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "file appeared before exclusive publication",
            ):
                safe_files.create_bytes_exclusive(quarantine, payload)

        self.assertTrue(injected)
        self.assertEqual(quarantine.read_bytes(), concurrent)
        escrows = self.escrow_entries(quarantine)
        self.assertEqual(len(escrows), 1)
        self.assertEqual(escrows[0].read_bytes(), payload)

    def test_exclusive_create_directory_fsync_failure_rolls_back_to_escrow(self):
        quarantine = self.root / "capture.quarantine"
        payload = b"authenticated-quarantine-payload"
        real_fsync = safe_files._fsync_fd
        failed_once = False

        def fail_first_directory_fsync(descriptor):
            nonlocal failed_once
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not failed_once:
                failed_once = True
                raise OSError("injected exclusive directory fsync failure")
            real_fsync(descriptor)

        with mock.patch.object(
            safe_files,
            "_fsync_fd",
            side_effect=fail_first_directory_fsync,
        ):
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "exclusive publication durability failed and was reversed",
            ):
                safe_files.create_bytes_exclusive(quarantine, payload)

        self.assertTrue(failed_once)
        self.assertFalse(quarantine.exists())
        escrows = self.escrow_entries(quarantine)
        self.assertEqual(len(escrows), 1)
        self.assertEqual(escrows[0].read_bytes(), payload)

    def test_torn_tail_quarantine_refuses_repair_when_stage_name_is_substituted(self):
        ledger = self.root / "torn-ledger.jsonl"
        original = b'{"valid":true}\n{"torn":'
        ledger.write_bytes(original)
        quarantine = self.root / "quarantine"
        authenticated_retained = self.root / "quarantine-authenticated-retained"
        foreign = b"foreign-quarantine-name"
        real_write = safe_files._write_all
        injected = False

        def substitute_quarantine_stage(descriptor, data):
            nonlocal injected
            if not injected:
                injected = True
                staged = tuple(quarantine.glob(".*.tmp"))
                self.assertEqual(len(staged), 1)
                staged[0].rename(authenticated_retained)
                staged[0].write_bytes(foreign)
            real_write(descriptor, data)

        with mock.patch.object(
            safe_files,
            "_write_all",
            side_effect=substitute_quarantine_stage,
        ):
            with self.assertRaisesRegex(
                forecasts.LedgerError,
                "exclusive staging identity changed before publication",
            ):
                forecasts.repair_trailing_jsonl(
                    ledger,
                    expected_line=2,
                    backup_dir=quarantine,
                )

        self.assertTrue(injected)
        self.assertEqual(ledger.read_bytes(), original)
        self.assertEqual(authenticated_retained.read_bytes(), original)
        escrows = tuple(quarantine.glob(".*.tmp.escrow.*"))
        self.assertEqual(len(escrows), 1)
        self.assertEqual(escrows[0].read_bytes(), foreign)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
