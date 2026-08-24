import io
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import install


# The kill criterion's body is not carried in this repository, so an install fixture
# cannot be a copy of it without drifting from it. Building the fixture out of the
# transform's own preimages keeps the two in step by construction; that the deployed
# file still matches those preimages is asserted separately, and again by rehearse.py
# against a copy of the real runtime.
CRITERION_FIXTURE = (
    'NON_COUNCIL_RECORD_KINDS = {"pre-mortem-calibration", "council-calibration"}\n'
    + "".join(preimage for preimage, _image in install._SUPERSEDE_READER_REWRITES)
)
DUPLICATE_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures/duplicate-council-row-supersede"
)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".claude/knowledge/council-eval").mkdir(parents=True)
        (self.root / ".claude/skills/council").mkdir(parents=True)
        (self.root / ".claude/knowledge/council-eval/predictions_report.py").write_text(
            "old reporter\n", encoding="utf-8"
        )
        (self.root / ".claude/skills/council/SKILL.md").write_text(
            "# Council\n\n## Steps\n\nOld steps\n", encoding="utf-8"
        )
        (self.root / ".claude/knowledge/council-eval/blind_seat_kill_criterion.py").write_text(
            CRITERION_FIXTURE, encoding="utf-8"
        )
        (self.root / "CLAUDE.md").write_text(
            "# Rules\n\n## SLO/SLI changes require a full council review\n",
            encoding="utf-8",
        )
        self.backups = self.root / "backups"
        self.targets = install._runtime_targets(self.root)
        self.originals = {target: target.read_bytes() for target in self.targets}
        self.original_identities = {
            target: install._identity(target.stat()) for target in self.targets
        }

    def tearDown(self):
        self.temp.cleanup()

    def _assert_committed_runtime_report_failure(self, stage):
        report_name = "RETAINED_ESCROWS.tsv"
        if stage == "create":
            real_operation = install._open_new_report_at

            def fail_operation(parent_fd, name, mode):
                if name == report_name:
                    raise OSError("injected retained report create failure")
                return real_operation(parent_fd, name, mode)

            patched = mock.patch.object(
                install, "_open_new_report_at", side_effect=fail_operation
            )
        elif stage == "write":
            real_operation = install._write_report_descriptor

            def fail_operation(descriptor, content):
                if Path(os.readlink(f"/proc/self/fd/{descriptor}")).name == report_name:
                    raise OSError("injected retained report write failure")
                return real_operation(descriptor, content)

            patched = mock.patch.object(
                install, "_write_report_descriptor", side_effect=fail_operation
            )
        elif stage == "file-fsync":
            real_operation = install._fsync_report_descriptor

            def fail_operation(descriptor):
                if Path(os.readlink(f"/proc/self/fd/{descriptor}")).name == report_name:
                    raise OSError("injected retained report file fsync failure")
                return real_operation(descriptor)

            patched = mock.patch.object(
                install, "_fsync_report_descriptor", side_effect=fail_operation
            )
        elif stage == "directory-fsync":
            patched = mock.patch.object(
                install,
                "_sync_pinned_report_directory",
                side_effect=OSError("injected retained report directory fsync failure"),
            )
        else:
            self.fail(f"unknown report failure stage: {stage}")

        with patched:
            with self.assertRaises(install.InstallError) as raised:
                install.install(self.root, self.backups)

        message = str(raised.exception)
        self.assertIn("runtime publication committed but custody report failed", message)
        self.assertIn("installer publication state=committed", message)
        self.assertIn("backup=", message)
        self.assertIn("retained_entries=", message)
        report_text = message.split("report=", 1)[1].split(";", 1)[0]
        self.assertEqual(Path(report_text).name, report_name)
        for target in self.targets:
            retained = list(
                target.parent.glob(f".{target.name}.council-tools-*.escrow")
            )
            self.assertEqual(len(retained), 1)
            self.assertIn(str(retained[0]), message)
            self.assertNotEqual(target.read_bytes(), self.originals[target])
        return message

    def _apply_rendered_supersede_reader(self, rows):
        namespace = {"hashlib": hashlib, "json": json, "re": re}
        exec(install._SUPERSEDE_READER_REWRITES[1][1], namespace)
        return namespace["_apply_supersedes"](rows)

    def _fixture_identity(self, fixture):
        rows = []
        data = (DUPLICATE_FIXTURE_ROOT / fixture).read_bytes()
        for line_number, raw in enumerate(data.splitlines(keepends=True), 1):
            if raw.strip():
                rows.append(
                    (
                        line_number,
                        json.loads(raw.decode("utf-8")),
                        hashlib.sha256(raw).hexdigest(),
                    )
                )
        return rows

    def test_install_is_backed_up_idempotent_and_checkable(self):
        clean, differences = install.check(self.root)
        self.assertFalse(clean)
        self.assertEqual(len(differences), 4)
        backup = install.install(self.root, self.backups)
        self.assertTrue((backup / "MANIFEST.tsv").exists())
        retained_report = backup / "RETAINED_ESCROWS.tsv"
        self.assertTrue(retained_report.exists())
        retained_rows = retained_report.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(retained_rows), len(self.targets))
        for target, row in zip(self.targets, retained_rows):
            relative, disposition = row.split("\t")
            escrow = self.root / relative
            self.assertEqual(disposition, "operator-review-required")
            self.assertEqual(escrow.read_bytes(), self.originals[target])
            self.assertEqual(
                install._identity(escrow.stat()),
                self.original_identities[target],
            )
        self.assertEqual(
            (self.backups / "LATEST").read_text(encoding="utf-8").strip(),
            str(backup),
        )
        self.assertTrue((backup / ".claude/skills/council/SKILL.md").exists())
        clean, differences = install.check(self.root)
        self.assertTrue(clean, differences)
        text = (self.root / ".claude/skills/council/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(text.count(install.FORECAST_BEGIN), 1)
        criterion = (
            self.root / ".claude/knowledge/council-eval/blind_seat_kill_criterion.py"
        ).read_text(encoding="utf-8")
        for kind in (
            "council-attempt",
            "capture-activation",
            "capture-initiation",
            "council-attempt-v2",
            "council-seats-finished",
            "capture-invalidation",
            "council-superseded",
        ):
            self.assertIn(f'"{kind}"', criterion)
        self.assertIn(install.SUPERSEDE_READER_MARKER, criterion)
        reporter = (
            self.root / ".claude/knowledge/council-eval/predictions_report.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("@@COUNCIL_TOOLS_", reporter)
        self.assertIn(install._repository_identity(install.REPO, require_clean=False)[0], reporter)

    def test_superseded_reader_transform_is_idempotent(self):
        once = install._with_superseded_reader(CRITERION_FIXTURE)
        twice = install._with_superseded_reader(once)

        self.assertNotEqual(once, CRITERION_FIXTURE)
        self.assertEqual(once, twice)
        self.assertIn(install.SUPERSEDE_READER_MARKER, once)

    def test_superseded_reader_transform_fails_closed_on_a_drifted_criterion(self):
        drifted = CRITERION_FIXTURE.replace("def tally(rows):", "def tally(records):")

        with self.assertRaisesRegex(install.InstallError, "preimage is not unique"):
            install._with_superseded_reader(drifted)

    def test_superseded_reader_transform_still_matches_the_deployed_criterion(self):
        deployed = Path(
            "/home/trader/.claude/knowledge/council-eval/blind_seat_kill_criterion.py"
        )
        if not deployed.is_file():
            self.skipTest("deployed blind-seat kill criterion is not present")
        text = deployed.read_text(encoding="utf-8")
        if install.SUPERSEDE_READER_MARKER in text:
            self.skipTest("deployed kill criterion already carries the reader change")

        rendered = install._with_superseded_reader(install._with_attempt_allowlist(text))

        compile(rendered, str(deployed), "exec")
        self.assertIn('"council-superseded"', rendered)

    def test_normative_composition_fixtures_drive_independent_reader_replay(self):
        manifest = json.loads(
            (DUPLICATE_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        for scenario in manifest["scenarios"]:
            if "acceptedSupersedes" not in scenario:
                continue
            with self.subTest(fixture=scenario["fixture"]):
                rows = self._fixture_identity(scenario["fixture"])
                kept, retired_count, errors = self._apply_rendered_supersede_reader(
                    rows
                )
                active_councils = sorted(
                    line_number
                    for line_number, row in kept
                    if row.get("kind") == "council"
                )
                rejected = sorted(
                    int(error.split(":", 1)[0].split()[1]) for error in errors
                )
                self.assertEqual(
                    retired_count, len(scenario["acceptedSupersedes"])
                )
                self.assertEqual(rejected, scenario["rejectedSupersedes"])
                self.assertEqual(active_councils, scenario["activeCouncilLines"])

    def test_live_shape_fixture_has_only_two_reader_provable_duplicates(self):
        manifest = json.loads(
            (DUPLICATE_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        scenario = manifest["scenarios"][0]
        rows = self._fixture_identity(scenario["fixture"])
        by_line = {
            line_number: digest for line_number, _row, digest in rows
        }

        def replay_assertion(target_line, retained_line):
            record = {
                "schemaVersion": 1,
                "kind": "council-superseded",
                "ts": "2030-01-01T00:02:00Z",
                "supersedes": {
                    "line": target_line,
                    "rawLineSha256": by_line[target_line],
                },
                "duplicateOf": {
                    "line": retained_line,
                    "rawLineSha256": by_line[retained_line],
                },
                "reason": "fixture duplicate assertion",
                "approval": {
                    "operator": "fixture-operator",
                    "approvedAt": "2030-01-01T00:02:00Z",
                    "reference": "fixture://issue-32",
                },
            }
            return self._apply_rendered_supersede_reader(
                [*rows, (23, record, "d" * 64)]
            )

        for candidate in scenario["candidateAssertions"]:
            kept, retired_count, errors = replay_assertion(
                candidate["supersedes"], candidate["duplicateOf"]
            )
            self.assertEqual(errors, [])
            self.assertEqual(retired_count, 1)
            self.assertNotIn(candidate["supersedes"], {line for line, _row in kept})

        for target_line in scenario["candidatesWithNoValidDuplicateOf"]:
            _kept, retired_count, errors = replay_assertion(target_line, 1)
            self.assertEqual(retired_count, 0)
            self.assertEqual(len(errors), 1)

    def test_reader_does_not_revise_an_edge_after_a_later_ordinary_collision(self):
        rows = self._fixture_identity("retained-row-later.jsonl")
        later = dict(rows[1][1])
        later["ts"] = "2030-01-01T00:03:00Z"
        rows.append((4, later, "f" * 64))

        kept, retired_count, errors = self._apply_rendered_supersede_reader(rows)

        self.assertEqual(errors, [])
        self.assertEqual(retired_count, 1)
        self.assertEqual(
            [line for line, row in kept if row.get("kind") == "council"],
            [2, 4],
        )

    def test_reader_refuses_a_matching_identifier_with_three_active_owners(self):
        rows = self._fixture_identity("retained-row-later.jsonl")
        target, retained, supersede = rows
        third = dict(retained[1])
        third["ts"] = "2030-01-01T00:01:30Z"
        rows = [target, retained, (3, third, "e" * 64), (4, supersede[1], supersede[2])]

        kept, retired_count, errors = self._apply_rendered_supersede_reader(rows)

        self.assertEqual(retired_count, 0)
        self.assertEqual(
            [line for line, row in kept if row.get("kind") == "council"],
            [1, 2, 3],
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("unique active retained owner", errors[0])

    def test_retained_report_create_failure_reports_committed_custody(self):
        self._assert_committed_runtime_report_failure("create")

    def test_retained_report_write_failure_reports_committed_custody(self):
        self._assert_committed_runtime_report_failure("write")

    def test_retained_report_file_fsync_failure_reports_committed_custody(self):
        self._assert_committed_runtime_report_failure("file-fsync")

    def test_retained_report_directory_fsync_failure_reports_committed_custody(self):
        self._assert_committed_runtime_report_failure("directory-fsync")

    def test_partial_install_failure_restores_every_original_target(self):
        calls = 0

        def fail_on_third(source, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected replacement failure")

        with self.assertRaisesRegex(install.InstallError, "runtime restored") as raised:
            install.install(self.root, self.backups, before_replace=fail_on_third)
        self.assertIn("installer publication state=rolled back", str(raised.exception))
        self.assertIn("retained_entries=", str(raised.exception))
        self.assertIn("backup=", str(raised.exception))
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            self.originals,
        )
        manifests = list(self.backups.glob("*/MANIFEST.tsv"))
        self.assertEqual(len(manifests), 1)

    def test_restore_command_path_verifies_backup_and_preserves_pre_restore_state(self):
        backup = install.install(self.root, self.backups)
        installed = {target: target.read_bytes() for target in self.targets}
        pre_restore = install.restore(self.root, backup, self.backups)
        criterion = self.root / ".claude/knowledge/council-eval/blind_seat_kill_criterion.py"
        for target in self.targets:
            if target == criterion:
                self.assertIn('"council-attempt"', target.read_text(encoding="utf-8"))
            else:
                self.assertEqual(target.read_bytes(), self.originals[target])
        for target, content in installed.items():
            relative = target.relative_to(self.root)
            self.assertEqual((pre_restore / relative).read_bytes(), content)

    def test_restore_report_failure_reports_committed_restore_custody(self):
        backup = install.install(self.root, self.backups)
        installed = {target: target.read_bytes() for target in self.targets}
        with mock.patch.object(
            install,
            "_write_retained_escrow_report",
            side_effect=OSError("injected restore retained report failure"),
        ):
            with self.assertRaises(install.InstallError) as raised:
                install.restore(self.root, backup, self.backups)

        message = str(raised.exception)
        self.assertIn("runtime restore publication committed", message)
        self.assertIn("installer publication state=committed", message)
        self.assertIn("backup=", message)
        self.assertIn("retained_entries=", message)
        changed = [
            target
            for target in self.targets
            if target.read_bytes() != installed[target]
        ]
        self.assertGreaterEqual(len(changed), 3)

    def test_restore_refuses_tampered_backup(self):
        backup = install.install(self.root, self.backups)
        backed_up_skill = backup / ".claude/skills/council/SKILL.md"
        backed_up_skill.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(install.InstallError, "digest mismatch"):
            install.restore(self.root, backup, self.backups)

    def test_install_refuses_backup_inside_source_repository(self):
        with self.assertRaisesRegex(install.InstallError, "outside"):
            install.install(self.root, install.REPO / "runtime/unsafe-backup")

    def test_clean_source_gate_rejects_dirty_commit_before_backup(self):
        source = self.root / "source-copy"
        shutil.copytree(
            install.REPO,
            source,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        readme = source / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\ndirty\n", encoding="utf-8")
        with self.assertRaisesRegex(install.InstallError, "requires a clean"):
            install.install(
                self.root,
                self.backups,
                source_repo=source,
                require_clean_source=True,
            )
        self.assertFalse(self.backups.exists())

    def test_install_rejects_symlinked_runtime_ancestor_without_outside_mutation(self):
        nominal_root = self.root / "nominal-root"
        outside_claude = self.root / "outside-claude"
        nominal_root.mkdir()
        shutil.copytree(self.root / ".claude", outside_claude)
        shutil.copy2(self.root / "CLAUDE.md", nominal_root / "CLAUDE.md")
        (nominal_root / ".claude").symlink_to(outside_claude, target_is_directory=True)
        outside_targets = install._runtime_targets(nominal_root)[:3]
        before = {target.resolve(): target.read_bytes() for target in outside_targets}
        backups = self.root / "symlink-rejection-backups"

        with self.assertRaisesRegex(install.InstallError, "trusted root"):
            install.install(nominal_root, backups)

        self.assertEqual(
            {path: path.read_bytes() for path in before},
            before,
        )
        self.assertFalse(backups.exists())

    def test_live_clean_source_gate_cannot_be_bypassed_by_root_alias(self):
        root_alias = self.root / "root-alias"
        root_alias.symlink_to(self.root, target_is_directory=True)
        source = self.root / "dirty-source-copy"
        shutil.copytree(
            install.REPO,
            source,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        readme = source / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\ndirty\n", encoding="utf-8"
        )
        source_alias = self.root / "dirty-source-alias"
        source_alias.symlink_to(source, target_is_directory=True)
        backups = self.root / "alias-gate-backups"

        with mock.patch.object(install, "LIVE_ROOT", self.root.resolve()):
            with self.assertRaisesRegex(install.InstallError, "requires a clean"):
                install.install(
                    root_alias,
                    backups,
                    source_repo=source_alias,
                    require_clean_source=False,
                )

        self.assertFalse(backups.exists())

    def test_ancestor_swap_at_pre_replace_cannot_touch_outside_or_defeat_rollback(self):
        outside_claude = self.root / "outside-at-mutation"
        shutil.copytree(self.root / ".claude", outside_claude)
        for target in install._runtime_targets(self.root)[:3]:
            relative = target.relative_to(self.root / ".claude")
            sentinel = outside_claude / relative
            sentinel.write_bytes(f"outside sentinel: {relative}\n".encode("utf-8"))
        outside_before = {
            path.relative_to(outside_claude): path.read_bytes()
            for path in outside_claude.rglob("*")
            if path.is_file()
        }
        original_claude = self.root / ".claude-before-swap"
        swapped = False

        def swap_ancestor_before_second_replace(source, target):
            nonlocal swapped
            if target == self.targets[1]:
                (self.root / ".claude").rename(original_claude)
                (self.root / ".claude").symlink_to(
                    outside_claude, target_is_directory=True
                )
                swapped = True

        with self.assertRaisesRegex(install.InstallError, "runtime restored"):
            install.install(
                self.root,
                self.backups,
                before_replace=swap_ancestor_before_second_replace,
            )

        self.assertTrue(swapped)
        self.assertEqual(
            {
                path.relative_to(outside_claude): path.read_bytes()
                for path in outside_claude.rglob("*")
                if path.is_file()
            },
            outside_before,
        )
        for target, original in self.originals.items():
            if target == self.root / "CLAUDE.md":
                restored = target
            else:
                restored = original_claude / target.relative_to(
                    self.root / ".claude"
                )
            self.assertEqual(restored.read_bytes(), original)

    def test_backup_payloads_and_directories_are_synced_before_first_replacement(self):
        authenticated = False
        real_authenticate = install._authenticate_pinned_backup

        def record_authentication(*args, **kwargs):
            nonlocal authenticated
            real_authenticate(*args, **kwargs)
            authenticated = True

        def verify_before_replace(source, target):
            self.assertTrue(authenticated)
            backup = next(path for path in self.backups.iterdir() if path.is_dir())
            for runtime_target in self.targets:
                backup_payload = backup / runtime_target.relative_to(self.root)
                self.assertEqual(
                    backup_payload.read_bytes(),
                    self.originals[runtime_target],
                )
            self.assertTrue((backup / "MANIFEST.tsv").exists())

        with mock.patch.object(
            install,
            "_authenticate_pinned_backup",
            side_effect=record_authentication,
        ):
            install.install(
                self.root,
                self.backups,
                before_replace=verify_before_replace,
            )

    def test_backup_payload_sync_failure_prevents_all_runtime_replacements(self):
        replaced = []

        def record_replace(source, target):
            replaced.append(target)

        with mock.patch.object(
            install,
            "_authenticate_pinned_backup",
            side_effect=OSError("injected backup authentication failure"),
        ):
            with self.assertRaisesRegex(OSError, "backup authentication failure"):
                install.install(
                    self.root,
                    self.backups,
                    before_replace=record_replace,
                )

        self.assertEqual(replaced, [])
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            self.originals,
        )

    def test_backup_copy_uses_pinned_source_when_target_name_is_swapped_and_restored(self):
        attacked = self.targets[0]
        original_stash = attacked.with_name(attacked.name + ".backup-race-original")
        substitute_stash = attacked.with_name(attacked.name + ".backup-race-substitute")
        substitute = b"backup pathname substitute bytes\n"
        replacements = []

        def swap_at_former_copy2_boundary(phase, target):
            if target != attacked:
                return
            if phase == "before-copy":
                target.rename(original_stash)
                target.write_bytes(substitute)
            elif phase == "after-copy":
                target.rename(substitute_stash)
                original_stash.rename(target)

        with self.assertRaisesRegex(
            install.InstallError,
            "changed while copying",
        ):
            install.install(
                self.root,
                self.backups,
                backup_copy_hook=swap_at_former_copy2_boundary,
                before_replace=lambda source, target: replacements.append(target),
            )

        backup = next(path for path in self.backups.iterdir() if path.is_dir())
        backed_up = backup / attacked.relative_to(self.root)
        self.assertEqual(backed_up.read_bytes(), self.originals[attacked])
        self.assertNotEqual(backed_up.read_bytes(), substitute)
        self.assertEqual(substitute_stash.read_bytes(), substitute)
        self.assertFalse((backup / "MANIFEST.tsv").exists())
        self.assertFalse((self.backups / "LATEST").exists())
        self.assertEqual(replacements, [])
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            self.originals,
        )

    def test_restore_prebackup_uses_pinned_source_during_copy_name_swap(self):
        restore_source = install.install(self.root, self.backups)
        installed = {target: target.read_bytes() for target in self.targets}
        existing_backups = {path for path in self.backups.iterdir() if path.is_dir()}
        attacked = self.targets[1]
        original_stash = attacked.with_name(attacked.name + ".restore-race-original")
        substitute_stash = attacked.with_name(attacked.name + ".restore-race-substitute")
        substitute = b"restore prebackup pathname substitute bytes\n"

        def swap_restore_prebackup(phase, target):
            if target != attacked:
                return
            if phase == "before-copy":
                target.rename(original_stash)
                target.write_bytes(substitute)
            elif phase == "after-copy":
                target.rename(substitute_stash)
                original_stash.rename(target)

        with self.assertRaisesRegex(
            install.InstallError,
            "changed while copying",
        ):
            install.restore(
                self.root,
                restore_source,
                self.backups,
                backup_copy_hook=swap_restore_prebackup,
            )

        pre_restore = next(
            path
            for path in self.backups.iterdir()
            if path.is_dir() and path not in existing_backups
        )
        backed_up = pre_restore / attacked.relative_to(self.root)
        self.assertEqual(backed_up.read_bytes(), installed[attacked])
        self.assertNotEqual(backed_up.read_bytes(), substitute)
        self.assertEqual(substitute_stash.read_bytes(), substitute)
        self.assertFalse((pre_restore / "MANIFEST.tsv").exists())
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            installed,
        )

    def test_unrestored_backup_name_swap_fails_before_runtime_publication(self):
        attacked = self.targets[0]
        original_stash = attacked.with_name(attacked.name + ".unrestored-original")
        substitute = b"unrestored backup race bytes\n"
        replacements = []

        def leave_swapped(phase, target):
            if target == attacked and phase == "before-copy":
                target.rename(original_stash)
                target.write_bytes(substitute)

        def record_replacement(source, target):
            replacements.append(target)

        try:
            with self.assertRaisesRegex(
                install.InstallError,
                "changed while copying",
            ):
                install.install(
                    self.root,
                    self.backups,
                    backup_copy_hook=leave_swapped,
                    before_replace=record_replacement,
                )
            self.assertEqual(replacements, [])
            backups = [path for path in self.backups.iterdir() if path.is_dir()]
            self.assertEqual(len(backups), 1)
            backed_up = backups[0] / attacked.relative_to(self.root)
            self.assertEqual(backed_up.read_bytes(), self.originals[attacked])
            self.assertNotEqual(backed_up.read_bytes(), substitute)
            self.assertFalse((self.backups / "LATEST").exists())
        finally:
            if attacked.exists():
                attacked.rename(attacked.with_name(attacked.name + ".unrestored-substitute"))
            if original_stash.exists():
                original_stash.rename(attacked)

    def test_each_successful_replacement_is_followed_by_parent_directory_sync(self):
        events = []
        payloads = {target: b"new\n" for target in self.targets}
        rollback_sources = {target: target for target in self.targets}
        real_exchange_pinned = install._exchange_pinned

        def record_exchange(parent_fd, source_name, target_name):
            events.append(("exchange", target_name))
            real_exchange_pinned(parent_fd, source_name, target_name)

        def record_directory(parent_fd):
            events.append(("directory", parent_fd))

        with mock.patch.object(
            install, "_exchange_pinned", side_effect=record_exchange
        ):
            with mock.patch.object(
                install, "_fsync_pinned_directory", side_effect=record_directory
            ):
                install._replace_all(
                    payloads,
                    rollback_sources=rollback_sources,
                    trusted_root=self.root,
                )

        for target in self.targets:
            replacement = events.index(("exchange", target.name))
            self.assertEqual(events[replacement + 1][0], "directory")

    def test_missing_atomic_exchange_support_fails_before_runtime_mutation(self):
        with mock.patch.object(install, "_RENAMEAT2", None):
            with self.assertRaisesRegex(
                install.InstallError,
                "requires renameat2",
            ) as raised:
                install.install(self.root, self.backups)

        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            self.originals,
        )
        for target in self.targets:
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.council-tools-*.tmp")),
                [],
            )
        self.assertIn("retained_entry=", str(raised.exception))
        retained_path = Path(str(raised.exception).split("retained_entry=", 1)[1])
        self.assertTrue(retained_path.exists())

    def test_substitution_after_last_check_is_reversed_without_losing_substitute(self):
        attacked = self.targets[0]
        displaced_original = attacked.with_name(attacked.name + ".operator-original")
        substitute = b"concurrent operator bytes\n"
        substitute_identity = None

        def substitute_at_publication_boundary(source, target):
            nonlocal substitute_identity
            if target != attacked:
                return
            target.rename(displaced_original)
            target.write_bytes(substitute)
            substitute_identity = install._identity(target.stat())

        with self.assertRaisesRegex(
            install.InstallError,
            "concurrent target substitution detected and reversed",
        ) as raised:
            install.install(
                self.root,
                self.backups,
                before_replace=substitute_at_publication_boundary,
            )

        self.assertEqual(attacked.read_bytes(), substitute)
        self.assertEqual(install._identity(attacked.stat()), substitute_identity)
        self.assertEqual(displaced_original.read_bytes(), self.originals[attacked])
        for target in self.targets[1:]:
            self.assertEqual(target.read_bytes(), self.originals[target])
        retained = list(
            attacked.parent.glob(f".{attacked.name}.council-tools-*.escrow")
        )
        self.assertEqual(len(retained), 1)
        self.assertIn(str(retained[0]), str(raised.exception))

    def test_escrow_substitution_at_former_unlink_boundary_is_retained_and_fails_closed(self):
        target = self.targets[0]
        original_identity = self.original_identities[target]
        rollback = self.root / "rollback-at-retention-boundary.py"
        rollback.write_bytes(self.originals[target])
        replacement = b"installer publication survives\n"
        moved_original = None
        retained_substitute = None
        substitute_identity = None

        def replace_escrow_after_identity_observation(escrow, runtime_target):
            nonlocal moved_original, retained_substitute, substitute_identity
            self.assertEqual(runtime_target, target)
            moved_original = escrow.with_name(escrow.name + ".operator-original")
            escrow.rename(moved_original)
            escrow.write_bytes(b"concurrent escrow bytes\n")
            retained_substitute = escrow
            substitute_identity = install._identity(escrow.stat())

        with self.assertRaisesRegex(
            install.InstallError,
            "escrow retention failed closed",
        ) as raised:
            install._replace_all(
                {target: replacement},
                rollback_sources={target: rollback},
                trusted_root=self.root,
                before_retain=replace_escrow_after_identity_observation,
            )

        self.assertEqual(target.read_bytes(), replacement)
        self.assertIsNotNone(moved_original)
        self.assertEqual(moved_original.read_bytes(), self.originals[target])
        self.assertEqual(install._identity(moved_original.stat()), original_identity)
        self.assertIsNotNone(retained_substitute)
        self.assertEqual(retained_substitute.read_bytes(), b"concurrent escrow bytes\n")
        self.assertEqual(
            install._identity(retained_substitute.stat()),
            substitute_identity,
        )
        self.assertIn(str(retained_substitute), str(raised.exception))
        self.assertIn("retained_entries=", str(raised.exception))

    def test_latest_pointer_exchange_retains_and_reports_previous_pointer(self):
        first_backup = install.install(self.root, self.backups)
        second_backup = install.install(self.root, self.backups)

        self.assertEqual(
            (self.backups / "LATEST").read_text(encoding="utf-8").strip(),
            str(second_backup),
        )
        pointer_report = second_backup / "RETAINED_BACKUP_POINTERS.tsv"
        retained_path_text, disposition = pointer_report.read_text(
            encoding="utf-8"
        ).strip().split("\t")
        retained_pointer = Path(retained_path_text)
        self.assertEqual(disposition, "operator-review-required")
        self.assertEqual(
            retained_pointer.read_text(encoding="utf-8").strip(),
            str(first_backup),
        )

    def test_post_authentication_payload_and_manifest_poison_fails_closed(self):
        attacked = self.targets[0]
        poison = b"post-authentication backup poison\n"
        real_publish_latest = install._publish_latest_pointer
        observed_backup = None
        replacements = []

        def poison_during_latest_publication(backup_root, staged):
            nonlocal observed_backup
            observed_backup = Path(
                staged.read_text(encoding="utf-8").strip()
            )
            backed_up = observed_backup / attacked.relative_to(self.root)
            backed_up.write_bytes(poison)
            poison_digest = hashlib.sha256(poison).hexdigest()
            manifest = observed_backup / "MANIFEST.tsv"
            rows = []
            for row in manifest.read_text(encoding="utf-8").splitlines():
                relative, digest = row.split("\t")
                if Path(relative) == attacked.relative_to(self.root):
                    digest = poison_digest
                rows.append(f"{relative}\t{digest}")
            manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
            return real_publish_latest(backup_root, staged)

        with mock.patch.object(
            install,
            "_publish_latest_pointer",
            side_effect=poison_during_latest_publication,
        ):
            with self.assertRaisesRegex(
                install.InstallError,
                "backup (destination|manifest) authentication failed",
            ):
                install.install(
                    self.root,
                    self.backups,
                    before_replace=lambda source, target: replacements.append(target),
                )

        self.assertIsNotNone(observed_backup)
        self.assertEqual(replacements, [])
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            self.originals,
        )
        self.assertEqual(
            (observed_backup / attacked.relative_to(self.root)).read_bytes(),
            poison,
        )
        with self.assertRaisesRegex(
            install.InstallError,
            "backup manifest seal digest mismatch",
        ):
            install.restore(self.root, observed_backup, self.backups)
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            self.originals,
        )

    def test_restore_source_poison_after_first_publication_rolls_back(self):
        restore_source = install.install(self.root, self.backups)
        installed = {target: target.read_bytes() for target in self.targets}
        installed_identities = {
            target: install._identity(target.stat()) for target in self.targets
        }
        attacked_payload = (
            restore_source / self.targets[0].relative_to(self.root)
        )
        poison = b"restore source poisoned after runtime publication\n"
        real_publish = install._publish_pinned
        publish_calls = 0

        def publish_then_poison_restore_source(pinned, target, expected):
            nonlocal publish_calls
            real_publish(pinned, target, expected)
            publish_calls += 1
            if publish_calls != 1:
                return
            attacked_payload.write_bytes(poison)
            poison_digest = hashlib.sha256(poison).hexdigest()
            manifest = restore_source / "MANIFEST.tsv"
            rows = []
            for row in manifest.read_text(encoding="utf-8").splitlines():
                relative, digest = row.split("\t")
                if Path(relative) == self.targets[0].relative_to(self.root):
                    digest = poison_digest
                rows.append(f"{relative}\t{digest}")
            manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")

        with mock.patch.object(
            install,
            "_publish_pinned",
            side_effect=publish_then_poison_restore_source,
        ):
            with self.assertRaisesRegex(
                install.InstallError,
                "runtime restored.*pre-restore backup=",
            ):
                install.restore(self.root, restore_source, self.backups)

        self.assertEqual(publish_calls, 1)
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            installed,
        )
        self.assertEqual(
            {target: install._identity(target.stat()) for target in self.targets},
            installed_identities,
        )

    def test_install_and_restore_rollback_custody_never_reopens_backup_paths(self):
        with mock.patch.object(
            install,
            "_read_rollback_payload",
            side_effect=AssertionError("mutable rollback path reopened"),
        ):
            restore_source = install.install(self.root, self.backups)
            install.restore(self.root, restore_source, self.backups)

    def test_latest_pointer_report_fsync_failure_reports_unchanged_runtime_custody(self):
        install.install(self.root, self.backups)
        runtime_before = {target: target.read_bytes() for target in self.targets}
        real_fsync_report_file = install._fsync_report_descriptor

        def fail_pointer_report(descriptor):
            if Path(os.readlink(f"/proc/self/fd/{descriptor}")).name == "RETAINED_BACKUP_POINTERS.tsv":
                raise OSError("injected retained pointer report fsync failure")
            return real_fsync_report_file(descriptor)

        with mock.patch.object(
            install,
            "_fsync_report_descriptor",
            side_effect=fail_pointer_report,
        ):
            with self.assertRaises(install.InstallError) as raised:
                install.install(self.root, self.backups)

        message = str(raised.exception)
        self.assertIn("backup pointer publication committed", message)
        self.assertIn("installer publication state=unchanged", message)
        self.assertIn("backup=", message)
        self.assertIn("retained_entries=", message)
        retained_path = Path(
            message.split("retained_entries=", 1)[1].split(";", 1)[0]
        )
        self.assertTrue(retained_path.exists())
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            runtime_before,
        )

    def test_cli_report_fsync_failure_surfaces_committed_state_and_custody(self):
        real_fsync_report_file = install._fsync_report_descriptor

        def fail_runtime_report(descriptor):
            if Path(os.readlink(f"/proc/self/fd/{descriptor}")).name == "RETAINED_ESCROWS.tsv":
                raise OSError("injected CLI retained report fsync failure")
            return real_fsync_report_file(descriptor)

        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "install.py",
            "install",
            "--root",
            str(self.root),
            "--backup-root",
            str(self.backups),
        ]
        with mock.patch.object(sys, "argv", argv):
            with mock.patch.object(
                install,
                "_fsync_report_descriptor",
                side_effect=fail_runtime_report,
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = install.main()

        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        message = stderr.getvalue()
        self.assertIn("installer publication state=committed", message)
        self.assertIn("backup=", message)
        self.assertIn("retained_entries=", message)
        self.assertIn("report=", message)

    def test_source_template_substitution_after_repository_identity_fails_closed(self):
        source = self.root / "source-custody"
        shutil.copytree(
            install.REPO,
            source,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        template = source / "runtime/predictions_report.py"
        original = template.read_bytes()
        original_stash = template.with_name(template.name + ".pinned-original")
        substitute = original.replace(
            b"@@COUNCIL_TOOLS_COMMIT@@",
            b"substitute-after-repository-identity",
            1,
        )
        real_repository_identity = install._repository_identity
        swapped = False

        def substitute_after_identity(*args, **kwargs):
            nonlocal swapped
            result = real_repository_identity(*args, **kwargs)
            if not swapped:
                template.rename(original_stash)
                template.write_bytes(substitute)
                swapped = True
            return result

        with mock.patch.object(
            install,
            "_repository_identity",
            side_effect=substitute_after_identity,
        ):
            with self.assertRaisesRegex(install.InstallError, "source binding changed"):
                install.install(
                    self.root,
                    self.backups,
                    source_repo=source,
                )

        self.assertTrue(swapped)
        self.assertEqual(original_stash.read_bytes(), original)
        self.assertEqual(template.read_bytes(), substitute)
        self.assertFalse(self.backups.exists())
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            self.originals,
        )

    def test_backup_path_substitution_during_nested_creation_stays_on_pinned_inode(self):
        for substitution in ("directory", "symlink"):
            with self.subTest(substitution=substitution):
                backup_root = self.root / f"backups-{substitution}"
                outside = self.root / f"outside-{substitution}"
                outside.mkdir()
                real_mkdirs = install._mkdirs_beneath
                swapped = False
                substitute_path = None

                def swap_backup_path(root_fd, relative):
                    nonlocal swapped, substitute_path
                    if not swapped:
                        backup = next(
                            path for path in backup_root.iterdir() if path.is_dir()
                        )
                        stash = backup.with_name(backup.name + ".retained-original")
                        backup.rename(stash)
                        if substitution == "directory":
                            backup.mkdir()
                            substitute_path = backup
                        else:
                            backup.symlink_to(outside, target_is_directory=True)
                            substitute_path = outside
                        swapped = True
                    return real_mkdirs(root_fd, relative)

                with mock.patch.object(
                    install,
                    "_mkdirs_beneath",
                    side_effect=swap_backup_path,
                ):
                    with self.assertRaisesRegex(
                        install.InstallError,
                        "backup directory identity changed",
                    ):
                        install.install(self.root, backup_root)

                self.assertTrue(swapped)
                self.assertEqual(list(substitute_path.iterdir()), [])
                self.assertEqual(
                    {target: target.read_bytes() for target in self.targets},
                    self.originals,
                )

    def test_custody_report_swap_and_restore_writes_authenticated_pinned_inventory(self):
        outside = self.root / "outside-report-swap"
        outside.mkdir()
        real_write_report = install._write_pinned_report
        swapped = False

        def swap_backup_around_report(backup, relative, content):
            nonlocal swapped
            if relative.name != "RETAINED_ESCROWS.tsv" or swapped:
                return real_write_report(backup, relative, content)
            original_path = backup.path
            stash = original_path.with_name(original_path.name + ".report-original")
            original_path.rename(stash)
            original_path.symlink_to(outside, target_is_directory=True)
            swapped = True
            try:
                return real_write_report(backup, relative, content)
            finally:
                original_path.unlink()
                stash.rename(original_path)

        with mock.patch.object(
            install,
            "_write_pinned_report",
            side_effect=swap_backup_around_report,
        ):
            backup = install.install(self.root, self.backups)

        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        report = backup / "RETAINED_ESCROWS.tsv"
        rows = report.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), len(self.targets))
        reported = {row.split("\t", 1)[0] for row in rows}
        retained = {
            str(escrow.relative_to(self.root))
            for target in self.targets
            for escrow in target.parent.glob(
                f".{target.name}.council-tools-*.escrow"
            )
        }
        self.assertEqual(
            reported,
            retained,
        )
        self.assertTrue(all(row.endswith("\toperator-review-required") for row in rows))

    def test_install_same_inode_staged_mutation_after_hook_fails_closed(self):
        attacked = self.targets[0]
        substitute = b"same-inode staged install substitute\n"
        staged_path = None
        staged_identity = None

        def mutate_staged(source, target):
            nonlocal staged_path, staged_identity
            if target != attacked:
                return
            before = install._identity(source.stat())
            source.write_bytes(substitute)
            after = install._identity(source.stat())
            self.assertEqual(after, before)
            staged_path = source
            staged_identity = after

        with self.assertRaisesRegex(
            install.InstallError,
            "staged runtime authentication failed",
        ) as raised:
            install.install(
                self.root,
                self.backups,
                before_replace=mutate_staged,
            )

        self.assertIn("installer publication state=unchanged", str(raised.exception))
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            self.originals,
        )
        self.assertIsNotNone(staged_path)
        self.assertEqual(staged_path.read_bytes(), substitute)
        self.assertEqual(install._identity(staged_path.stat()), staged_identity)
        self.assertNotIn(substitute, {target.read_bytes() for target in self.targets})

    def test_restore_same_inode_staged_mutation_after_hook_fails_closed(self):
        restore_source = install.install(self.root, self.backups)
        installed = {target: target.read_bytes() for target in self.targets}
        attacked = self.targets[1]
        substitute = b"same-inode staged restore substitute\n"
        staged_path = None
        staged_identity = None

        def mutate_staged(source, target):
            nonlocal staged_path, staged_identity
            if target != attacked:
                return
            before = install._identity(source.stat())
            source.write_bytes(substitute)
            after = install._identity(source.stat())
            self.assertEqual(after, before)
            staged_path = source
            staged_identity = after

        with self.assertRaisesRegex(
            install.InstallError,
            "staged runtime authentication failed",
        ) as raised:
            install.restore(
                self.root,
                restore_source,
                self.backups,
                before_replace=mutate_staged,
            )

        self.assertIn("installer publication state=rolled back", str(raised.exception))
        self.assertEqual(
            {target: target.read_bytes() for target in self.targets},
            installed,
        )
        self.assertIsNotNone(staged_path)
        self.assertEqual(staged_path.read_bytes(), substitute)
        self.assertEqual(install._identity(staged_path.stat()), staged_identity)
        self.assertNotIn(substitute, {target.read_bytes() for target in self.targets})

    def test_later_substitution_rolls_back_prior_publication_and_preserves_substitute(self):
        attacked = self.targets[1]
        displaced_original = attacked.with_name(attacked.name + ".operator-original")
        substitute = b"later concurrent operator bytes\n"
        substitute_identity = None

        def substitute_second_target(source, target):
            nonlocal substitute_identity
            if target != attacked:
                return
            target.rename(displaced_original)
            target.write_bytes(substitute)
            substitute_identity = install._identity(target.stat())

        with self.assertRaisesRegex(
            install.InstallError,
            "concurrent target substitution detected and reversed",
        ):
            install.install(
                self.root,
                self.backups,
                before_replace=substitute_second_target,
            )

        self.assertEqual(self.targets[0].read_bytes(), self.originals[self.targets[0]])
        self.assertEqual(
            install._identity(self.targets[0].stat()),
            self.original_identities[self.targets[0]],
        )
        self.assertEqual(attacked.read_bytes(), substitute)
        self.assertEqual(install._identity(attacked.stat()), substitute_identity)
        self.assertEqual(displaced_original.read_bytes(), self.originals[attacked])
        for target in self.targets[2:]:
            self.assertEqual(target.read_bytes(), self.originals[target])

    def test_rollback_never_overwrites_target_superseded_after_publication(self):
        published = self.targets[0]
        fail_at = self.targets[1]
        moved_installer_inode = published.with_name(
            published.name + ".operator-moved-installer"
        )
        substitute = b"operator superseded installed target\n"
        substitute_identity = None

        def supersede_prior_then_fail(source, target):
            nonlocal substitute_identity
            if target != fail_at:
                return
            published.rename(moved_installer_inode)
            published.write_bytes(substitute)
            substitute_identity = install._identity(published.stat())
            raise OSError("injected failure after external supersession")

        with self.assertRaisesRegex(
            install.InstallError,
            "concurrent targets were preserved",
        ):
            install.install(
                self.root,
                self.backups,
                before_replace=supersede_prior_then_fail,
            )

        self.assertEqual(published.read_bytes(), substitute)
        self.assertEqual(install._identity(published.stat()), substitute_identity)
        self.assertNotEqual(
            moved_installer_inode.read_bytes(), self.originals[published]
        )
        for target in self.targets[1:]:
            self.assertEqual(target.read_bytes(), self.originals[target])

    def test_parent_sync_failure_after_replace_rolls_back_changed_target(self):
        target = self.targets[0]
        rollback = self.root / "rollback-reporter.py"
        rollback.write_bytes(self.originals[target])
        real_fsync_directory = install._fsync_pinned_directory
        calls = 0

        def fail_first_directory_sync(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected directory fsync failure")
            real_fsync_directory(path)

        with mock.patch.object(
            install,
            "_fsync_pinned_directory",
            side_effect=fail_first_directory_sync,
        ):
            with self.assertRaisesRegex(install.InstallError, "runtime restored"):
                install._replace_all(
                    {target: b"replacement\n"},
                    rollback_sources={target: rollback},
                    trusted_root=self.root,
                )

        self.assertEqual(target.read_bytes(), self.originals[target])


if __name__ == "__main__":
    unittest.main()
