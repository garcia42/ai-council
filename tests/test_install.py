import os
import shutil
import tempfile
import unittest
from pathlib import Path

import install


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
            'NON_COUNCIL_RECORD_KINDS = {"pre-mortem-calibration", "council-calibration"}\n',
            encoding="utf-8",
        )
        (self.root / "CLAUDE.md").write_text(
            "# Rules\n\n## SLO/SLI changes require a full council review\n",
            encoding="utf-8",
        )
        self.backups = self.root / "backups"
        self.targets = install._runtime_targets(self.root)
        self.originals = {target: target.read_bytes() for target in self.targets}

    def tearDown(self):
        self.temp.cleanup()

    def test_install_is_backed_up_idempotent_and_checkable(self):
        clean, differences = install.check(self.root)
        self.assertFalse(clean)
        self.assertEqual(len(differences), 4)
        backup = install.install(self.root, self.backups)
        self.assertTrue((backup / "MANIFEST.tsv").exists())
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
        self.assertIn('"council-attempt"', (
            self.root / ".claude/knowledge/council-eval/blind_seat_kill_criterion.py"
        ).read_text(encoding="utf-8"))
        reporter = (
            self.root / ".claude/knowledge/council-eval/predictions_report.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("@@COUNCIL_TOOLS_", reporter)
        self.assertIn(install._repository_identity(install.REPO, require_clean=False)[0], reporter)

    def test_partial_install_failure_restores_every_original_target(self):
        calls = 0

        def fail_on_third(source, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected replacement failure")
            os.replace(source, target)

        with self.assertRaisesRegex(install.InstallError, "runtime restored"):
            install.install(self.root, self.backups, replace_func=fail_on_third)
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


if __name__ == "__main__":
    unittest.main()
