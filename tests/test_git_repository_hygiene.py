"""Proofs for the repository-hygiene preflight.

Each test builds and removes its own bare repository under a temporary tree, so
nothing outside that tree is read or written.  Real repositories are used rather
than fixtures, because the thing under test is what is actually on disk.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from council_tools.git_repository_hygiene import (
    GitRepositoryHygieneError,
    HygieneReport,
    inspect_repository,
)

GIT = "/usr/bin/git"
CLEAN_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "XDG_CONFIG_HOME": "/nonexistent",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}


def git_available() -> bool:
    return Path(GIT).exists()


class HygieneTestCase(unittest.TestCase):
    def setUp(self):
        if not git_available():  # pragma: no cover - environment guard
            self.skipTest("git is not available")
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.repository = self.root / "claim.git"
        self.git(["init", "--bare", "--template=", "--object-format=sha1", str(self.repository)])

    def git(self, arguments, *, stdin=b"", git_dir=None):
        environment = dict(CLEAN_ENV)
        if git_dir is not None:
            environment["GIT_DIR"] = str(git_dir)
        result = subprocess.run(
            [GIT, *arguments], input=stdin, capture_output=True, env=environment
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()


class CleanRepositoryTests(HygieneTestCase):
    def test_a_fresh_bare_repository_reports_clean(self):
        report = inspect_repository(self.repository)
        self.assertEqual(report, HygieneReport())
        self.assertTrue(report.is_clean)

    def test_a_repository_with_objects_but_no_redirection_is_clean(self):
        self.git(["hash-object", "-w", "--stdin", "--"], stdin=b"payload", git_dir=self.repository)
        self.assertTrue(inspect_repository(self.repository).is_clean)


class AlternatesTests(HygieneTestCase):
    def write_alternates(self, text):
        info = self.repository / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text(text, encoding="utf-8")

    def test_an_alternates_entry_is_found(self):
        self.write_alternates("/somewhere/else/objects\n")
        report = inspect_repository(self.repository)
        self.assertEqual(report.alternates, ("/somewhere/else/objects",))
        self.assertFalse(report.is_clean)

    def test_multiple_entries_are_all_found(self):
        self.write_alternates("/a/objects\n/b/objects\n")
        self.assertEqual(
            inspect_repository(self.repository).alternates, ("/a/objects", "/b/objects")
        )

    def test_blank_lines_and_comments_are_not_entries(self):
        self.write_alternates("\n# a comment\n/a/objects\n\n")
        self.assertEqual(inspect_repository(self.repository).alternates, ("/a/objects",))

    def test_an_empty_alternates_file_is_clean(self):
        self.write_alternates("")
        self.assertTrue(inspect_repository(self.repository).is_clean)

    def test_an_unreadable_alternates_file_is_a_refusal_not_an_absence(self):
        # Reporting clean here would say "no alternates" about a repository
        # whose alternates could not be checked.
        if os.geteuid() == 0:  # pragma: no cover - root ignores the mode
            self.skipTest("root can read a mode-000 file")
        self.write_alternates("/a/objects\n")
        target = self.repository / "objects" / "info" / "alternates"
        target.chmod(0o000)
        self.addCleanup(target.chmod, 0o644)
        with self.assertRaises(GitRepositoryHygieneError) as caught:
            inspect_repository(self.repository)
        self.assertEqual(caught.exception.code, "unreadable")


class ReplacementReferenceTests(HygieneTestCase):
    def make_two_objects(self):
        original = self.git(
            ["hash-object", "-w", "--stdin", "--"], stdin=b"original", git_dir=self.repository
        )
        decoy = self.git(
            ["hash-object", "-w", "--stdin", "--"], stdin=b"decoy", git_dir=self.repository
        )
        return original, decoy

    def test_a_loose_replacement_reference_is_found(self):
        original, decoy = self.make_two_objects()
        self.git(["update-ref", f"refs/replace/{original}", decoy], git_dir=self.repository)
        report = inspect_repository(self.repository)
        self.assertEqual(report.replacement_refs, (f"refs/replace/{original}",))
        self.assertFalse(report.is_clean)

    def test_a_packed_replacement_reference_is_found(self):
        # A replacement can live loose or packed; checking only one would report
        # clean on a repository carrying the other.
        original, decoy = self.make_two_objects()
        self.git(["update-ref", f"refs/replace/{original}", decoy], git_dir=self.repository)
        self.git(["pack-refs", "--all"], git_dir=self.repository)
        loose = self.repository / "refs" / "replace" / original
        self.assertFalse(loose.exists(), "precondition: the ref should now be packed")
        report = inspect_repository(self.repository)
        self.assertEqual(report.replacement_refs, (f"refs/replace/{original}",))

    def test_an_ordinary_packed_reference_is_not_a_replacement(self):
        self.git(["hash-object", "-w", "--stdin", "--"], stdin=b"x", git_dir=self.repository)
        (self.repository / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted \n"
            "0000000000000000000000000000000000000000 refs/heads/main\n",
            encoding="utf-8",
        )
        self.assertTrue(inspect_repository(self.repository).is_clean)

    def test_a_peeled_line_is_not_mistaken_for_a_reference(self):
        (self.repository / "packed-refs").write_text(
            "0000000000000000000000000000000000000000 refs/tags/v1\n"
            "^1111111111111111111111111111111111111111\n",
            encoding="utf-8",
        )
        self.assertTrue(inspect_repository(self.repository).is_clean)


class RefusalTests(HygieneTestCase):
    def test_a_directory_that_is_not_a_bare_repository_is_refused(self):
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaises(GitRepositoryHygieneError) as caught:
            inspect_repository(plain)
        self.assertEqual(caught.exception.code, "not-a-bare-repository")

    def test_a_missing_directory_is_refused(self):
        with self.assertRaises(GitRepositoryHygieneError) as caught:
            inspect_repository(self.root / "absent")
        self.assertEqual(caught.exception.code, "not-a-directory")

    def test_a_file_is_refused(self):
        target = self.root / "a-file"
        target.write_text("x", encoding="utf-8")
        with self.assertRaises(GitRepositoryHygieneError) as caught:
            inspect_repository(target)
        self.assertEqual(caught.exception.code, "not-a-directory")

    def test_a_bytes_path_is_refused(self):
        with self.assertRaises(GitRepositoryHygieneError) as caught:
            inspect_repository(b"/tmp")  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid-path")

    def test_errors_are_typed_with_stable_codes_and_fields(self):
        self.assertTrue(issubclass(GitRepositoryHygieneError, ValueError))
        with self.assertRaises(GitRepositoryHygieneError) as caught:
            inspect_repository(self.root / "absent")
        self.assertEqual(caught.exception.field, "repository")


class NoGitInvocationTests(HygieneTestCase):
    def test_the_module_imports_nothing_that_can_run_a_command(self):
        # Checked against the module's namespace rather than its source text:
        # the docstring legitimately mentions subprocess to say it runs none.
        import council_tools.git_repository_hygiene as module

        for name in ("subprocess", "Popen", "check_output", "RenderedInvocation", "GitCommand"):
            with self.subTest(name=name):
                self.assertNotIn(name, vars(module))

    def test_the_report_is_frozen_and_compares_by_value(self):
        import dataclasses

        report = inspect_repository(self.repository)
        self.assertEqual(report, HygieneReport())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.alternates = ("x",)  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
