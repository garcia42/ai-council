import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from council_tools.council_coverage import (
    COVERED,
    EXEMPT,
    UNCOVERED,
    UNKNOWN,
    CoverageError,
    coverage_exit_code,
    format_coverage,
    parse_timestamp,
    read_ledger_evidence,
    reconcile_coverage,
)


EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc)


def stamp(minutes: int) -> datetime:
    return EPOCH + timedelta(minutes=minutes)


def text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Repository:
    """A throwaway git repository with fully deterministic commit times."""

    def __init__(self, root: Path):
        self.root = root
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(root.parent),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_AUTHOR_NAME": "Coverage Test",
                "GIT_AUTHOR_EMAIL": "coverage@example.invalid",
                "GIT_COMMITTER_NAME": "Coverage Test",
                "GIT_COMMITTER_EMAIL": "coverage@example.invalid",
            }
        )
        self.git("init", "-q", "-b", "main", ".")

    def git(self, *arguments: str, at: datetime | None = None) -> str:
        env = dict(self.env)
        if at is not None:
            env["GIT_AUTHOR_DATE"] = text(at)
            env["GIT_COMMITTER_DATE"] = text(at)
        completed = subprocess.run(
            ["git", "-C", str(self.root), "-c", "commit.gpgsign=false", *arguments],
            text=True,
            capture_output=True,
            check=True,
            env=env,
        )
        return completed.stdout

    def commit(self, message: str, *, at: datetime) -> str:
        self.git("commit", "-q", "--allow-empty", "-m", message, at=at)
        return self.git("rev-parse", "HEAD").strip()


class CoverageTestBase(unittest.TestCase):
    def setUp(self):
        # /tmp is marked as a Git work tree on this host, which would make a
        # fixture repository's boundary ambiguous.
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.temp.name)
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.repo = Repository(self.repo_path)
        self.log = self.root / "council.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def council_row(self, ts: datetime, commits) -> dict:
        return {
            "schemaVersion": 1,
            "kind": "council",
            "ts": text(ts),
            "question": "test",
            "commits": commits,
        }

    def write_ledger(self, *rows, anchor: bool = True) -> None:
        """Write a ledger, by default opening it with an instrumentation anchor.

        Most fixtures are about classification, not about the instrumentation
        gate, so they need one conforming row at or before the window start.
        Tests that exercise the gate itself pass ``anchor=False``.
        """

        entries = list(rows)
        if anchor:
            entries.insert(0, self.council_row(stamp(0), []))
        with self.log.open("w", encoding="utf-8") as handle:
            for row in entries:
                if isinstance(row, str):
                    handle.write(row + "\n")
                else:
                    handle.write(json.dumps(row) + "\n")

    def run_coverage(self, **overrides) -> dict:
        arguments = {
            "repo": self.repo_path,
            "log_path": self.log,
            "since": stamp(0),
            "until": stamp(600),
        }
        arguments.update(overrides)
        return reconcile_coverage(**arguments)

    def states(self, result: dict) -> dict[str, str]:
        return {item["sha"]: item["state"] for item in result["commits"]}


class ClassificationTest(CoverageTestBase):
    def test_commit_named_in_a_conforming_row_is_covered(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), [sha]))
        result = self.run_coverage()
        self.assertTrue(result["determined"], result)
        self.assertEqual(self.states(result), {sha: COVERED})
        self.assertEqual(result["counts"]["covered"], 1)
        self.assertEqual(coverage_exit_code(result), 0)

    def test_commit_no_row_names_is_uncovered_and_exits_one(self):
        covered = self.repo.commit("reviewed", at=stamp(10))
        skipped = self.repo.commit("shipped without review", at=stamp(30))
        self.write_ledger(self.council_row(stamp(20), [covered]))
        result = self.run_coverage()
        self.assertEqual(self.states(result), {covered: COVERED, skipped: UNCOVERED})
        self.assertEqual(coverage_exit_code(result), 1)

    def test_explicit_exempt_trailer_leaves_the_denominator(self):
        exempt = self.repo.commit(
            "fix typo\n\nCouncil-Exempt: comment-only edit", at=stamp(10)
        )
        self.write_ledger()
        result = self.run_coverage()
        self.assertEqual(self.states(result), {exempt: EXEMPT})
        self.assertEqual(result["counts"]["exempt"], 1)
        self.assertEqual(result["counts"]["eligible"], 0)
        self.assertIsNone(result["rate"])
        self.assertEqual(result["rateNote"], "no eligible commits in window")
        self.assertEqual(coverage_exit_code(result), 0)

    def test_exempt_trailer_without_a_reason_fails_closed(self):
        sha = self.repo.commit("fix typo\n\nCouncil-Exempt:   ", at=stamp(10))
        self.write_ledger()
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: UNCOVERED})
        self.assertEqual(result["counts"]["exempt"], 0)
        self.assertEqual(coverage_exit_code(result), 1)

    def test_exemption_never_overrides_a_recorded_review(self):
        sha = self.repo.commit(
            "reviewed anyway\n\nCouncil-Exempt: mechanical", at=stamp(10)
        )
        self.write_ledger(self.council_row(stamp(20), [sha]))
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: COVERED})


class AmbiguityTest(CoverageTestBase):
    def test_pre_convention_base_row_makes_later_descendants_unknown(self):
        base = self.repo.commit("base", at=stamp(5))
        later = self.repo.commit("on top of the reviewed base", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), {"base": base}))
        result = self.run_coverage()
        self.assertEqual(self.states(result)[later], UNKNOWN)
        self.assertEqual(result["counts"]["unknown"], 1)
        # The base itself is in-window and unreviewed, so this window is still
        # a positive finding rather than only an ambiguous one.
        self.assertEqual(coverage_exit_code(result), 1)

    def test_an_only_ambiguous_window_cannot_be_determined(self):
        base = self.repo.commit("base", at=stamp(5))
        later = self.repo.commit("on top of the reviewed base", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), {"base": base}))
        result = self.run_coverage(since=stamp(6))
        self.assertEqual(self.states(result), {later: UNKNOWN})
        self.assertEqual(result["counts"]["uncovered"], 0)
        self.assertEqual(coverage_exit_code(result), 3)

    def test_a_commit_made_after_the_row_is_not_ambiguous(self):
        base = self.repo.commit("base", at=stamp(5))
        later = self.repo.commit("appended after the council ran", at=stamp(40))
        self.write_ledger(self.council_row(stamp(20), {"base": base}))
        result = self.run_coverage()
        self.assertEqual(self.states(result)[later], UNCOVERED)
        self.assertEqual(result["counts"]["unknown"], 0)
        self.assertEqual(coverage_exit_code(result), 1)

    def test_the_named_base_itself_is_not_made_unknown_by_its_own_row(self):
        base = self.repo.commit("base", at=stamp(5))
        self.write_ledger(self.council_row(stamp(20), {"base": base}))
        result = self.run_coverage()
        self.assertEqual(self.states(result)[base], UNCOVERED)

    def test_abbreviated_base_resolves_and_free_text_does_not(self):
        base = self.repo.commit("base", at=stamp(5))
        later = self.repo.commit("descendant", at=stamp(10))
        self.write_ledger(
            self.council_row(
                stamp(20),
                {"base": base[:7], "state": "uncommitted-untracked", "branch": "main"},
            )
        )
        result = self.run_coverage()
        self.assertEqual(self.states(result)[later], UNKNOWN)
        self.assertEqual(result["ledger"]["ambiguousBasesResolvedInRepo"], 1)

    def test_a_base_from_another_repository_is_ignored(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        foreign = "0" * 39 + "1"
        self.write_ledger(self.council_row(stamp(20), {"base": foreign}))
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: UNCOVERED})
        self.assertEqual(result["ledger"]["ambiguousBasesResolvedInRepo"], 0)

    def test_a_reviewed_sha_from_another_repository_covers_nothing(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), ["a" * 40]))
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: UNCOVERED})
        self.assertEqual(result["ledger"]["reviewedShas"], 1)

    def test_an_ambiguous_row_without_a_readable_timestamp_stays_ambiguous(self):
        base = self.repo.commit("base", at=stamp(5))
        later = self.repo.commit("descendant", at=stamp(400))
        self.write_ledger(
            {"kind": "council", "ts": "not-a-timestamp", "commits": {"base": base}}
        )
        result = self.run_coverage()
        self.assertEqual(self.states(result)[later], UNKNOWN)


class LedgerShapeTest(CoverageTestBase):
    def test_an_empty_array_establishes_instrumentation_but_covers_nothing(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(1), []), anchor=False)
        result = self.run_coverage(since=stamp(1))
        self.assertTrue(result["determined"], result)
        self.assertEqual(result["instrumentation"]["epoch"], text(stamp(1)))
        self.assertEqual(self.states(result), {sha: UNCOVERED})

    def test_an_attempt_row_never_counts_as_a_review(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(
            {
                "schemaVersion": 1,
                "kind": "council-attempt",
                "ts": text(stamp(20)),
                "question": "test",
                "commits": [sha],
            }
        )
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: UNCOVERED})
        self.assertEqual(result["ledger"]["conformingCouncilRows"], 1)
        self.assertEqual(result["ledger"]["ambiguousCommitRows"], 1)

    def test_an_abbreviated_sha_in_an_array_is_not_the_convention(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), [sha[:7]]))
        result = self.run_coverage()
        self.assertEqual(result["ledger"]["conformingCouncilRows"], 1)
        self.assertEqual(result["ledger"]["ambiguousCommitRows"], 1)
        self.assertEqual(result["ledger"]["reviewedShas"], 0)

    def test_evidence_reader_counts_rows_without_a_commits_field(self):
        self.write_ledger(
            {"kind": "council", "ts": text(stamp(1)), "question": "q"},
            self.council_row(stamp(2), []),
            anchor=False,
        )
        evidence = read_ledger_evidence(self.log)
        self.assertEqual(evidence.rows_read, 2)
        self.assertEqual(evidence.conforming_rows, 1)
        self.assertEqual(evidence.ambiguous_rows, 0)


class RefusalTest(CoverageTestBase):
    def assert_refused(self, result: dict, code: str) -> None:
        self.assertFalse(result["determined"], result)
        self.assertEqual([item["code"] for item in result["refusals"]], [code])
        self.assertIsNone(result["rate"])
        self.assertNotIn("counts", result)
        self.assertNotIn("commits", result)
        self.assertEqual(coverage_exit_code(result), 3)
        self.assertIn("rate=UNAVAILABLE", format_coverage(result))

    def test_an_unparseable_ledger_line_refuses_the_rate(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger("{not json")
        self.assert_refused(self.run_coverage(), "ledger-unreadable")

    def test_a_conforming_row_without_a_readable_ts_refuses_the_rate(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(
            {"kind": "council", "question": "q", "commits": []}, anchor=False
        )
        self.assert_refused(self.run_coverage(), "ledger-unreadable")

    def test_a_ledger_with_no_array_row_has_no_denominator(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(
            self.council_row(stamp(1), {"base": "a" * 40}), anchor=False
        )
        result = self.run_coverage()
        self.assert_refused(result, "no-commit-instrumentation")
        self.assertIn("unrecoverable", result["refusals"][0]["detail"])

    def test_a_window_before_the_convention_is_unrecoverable(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(5), []), anchor=False)
        result = self.run_coverage(since=stamp(0))
        self.assert_refused(result, "window-predates-commit-instrumentation")
        self.assertIn("unrecoverable", result["refusals"][0]["detail"])

    def test_a_window_starting_exactly_at_the_epoch_is_allowed(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(5), []), anchor=False)
        result = self.run_coverage(since=stamp(5))
        self.assertTrue(result["determined"], result)

    def test_an_explicit_epoch_overrides_the_derived_one(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(5), [sha]), anchor=False)
        result = self.run_coverage(since=stamp(0), instrumented_since=stamp(0))
        self.assertTrue(result["determined"], result)
        self.assertEqual(result["instrumentation"]["source"], "explicit")
        self.assertEqual(result["instrumentation"]["epoch"], text(stamp(0)))

    def test_a_directory_that_is_not_a_repository_refuses(self):
        outside = self.root / "not-a-repo"
        outside.mkdir()
        self.write_ledger()
        self.assert_refused(self.run_coverage(repo=outside), "git-unavailable")

    def test_an_unresolvable_ref_refuses(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger()
        self.assert_refused(
            self.run_coverage(ref="refs/heads/does-not-exist"), "git-unavailable"
        )

    def test_a_missing_ledger_has_no_instrumentation_rather_than_a_rate(self):
        self.repo.commit("shipped", at=stamp(10))
        self.assert_refused(self.run_coverage(), "no-commit-instrumentation")


class RateTest(CoverageTestBase):
    def test_the_rate_is_exact_when_nothing_is_unknown(self):
        first = self.repo.commit("reviewed", at=stamp(10))
        self.repo.commit("not reviewed", at=stamp(20))
        self.write_ledger(self.council_row(stamp(30), [first]))
        result = self.run_coverage()
        self.assertTrue(result["rate"]["exact"])
        self.assertEqual(result["rate"]["lower"], 0.5)
        self.assertEqual(result["rate"]["upper"], 0.5)
        self.assertIn("covered_rate=0.5000", format_coverage(result))

    def test_unknown_commits_widen_the_rate_into_a_band(self):
        base = self.repo.commit("base", at=stamp(5))
        self.repo.commit("maybe reviewed", at=stamp(10))
        self.repo.commit("definitely not", at=stamp(40))
        self.write_ledger(self.council_row(stamp(20), {"base": base}))
        result = self.run_coverage()
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertFalse(result["rate"]["exact"])
        self.assertEqual(result["rate"]["lower"], 0.0)
        self.assertAlmostEqual(result["rate"]["upper"], 1 / 3)
        self.assertIn("covered_rate_band=", format_coverage(result))

    def test_uncovered_outranks_unknown_in_the_exit_code(self):
        base = self.repo.commit("base", at=stamp(5))
        self.repo.commit("maybe reviewed", at=stamp(10))
        self.repo.commit("definitely not", at=stamp(40))
        self.write_ledger(self.council_row(stamp(20), {"base": base}))
        result = self.run_coverage()
        self.assertEqual(result["counts"]["uncovered"], 2)
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertEqual(coverage_exit_code(result), 1)


class WindowTest(CoverageTestBase):
    def test_the_window_is_half_open_on_committer_date(self):
        before = self.repo.commit("before", at=stamp(9))
        inside = self.repo.commit("inside", at=stamp(10))
        boundary = self.repo.commit("at until", at=stamp(20))
        self.write_ledger(self.council_row(stamp(1), [before, inside, boundary]))
        result = self.run_coverage(since=stamp(10), until=stamp(20))
        self.assertEqual(list(self.states(result)), [inside])

    def test_merge_commits_are_excluded_by_default_and_counted(self):
        base = self.repo.commit("base", at=stamp(5))
        self.repo.git("checkout", "-q", "-b", "side", base)
        side = self.repo.commit("side work", at=stamp(10))
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("main work", at=stamp(11))
        self.repo.git("merge", "-q", "--no-ff", "-m", "merge side", side, at=stamp(12))
        self.write_ledger()
        excluded = self.run_coverage()
        self.assertEqual(excluded["counts"]["mergeCommitsExcluded"], 1)
        self.assertEqual(excluded["counts"]["total"], 3)
        self.assertFalse(excluded["window"]["includesMerges"])

        included = self.run_coverage(include_merges=True)
        self.assertEqual(included["counts"]["mergeCommitsExcluded"], 0)
        self.assertEqual(included["counts"]["total"], 4)

    def test_a_root_commit_with_no_parents_parses(self):
        root = self.repo.commit("root", at=stamp(10))
        self.write_ledger(self.council_row(stamp(11), [root]))
        result = self.run_coverage()
        self.assertEqual(self.states(result), {root: COVERED})

    def test_a_multi_line_message_reports_only_its_subject(self):
        sha = self.repo.commit(
            "subject line\n\nbody paragraph\n\nCo-Authored-By: nobody", at=stamp(10)
        )
        self.write_ledger()
        result = self.run_coverage()
        self.assertEqual(result["commits"][0]["subject"], "subject line")
        self.assertEqual(result["commits"][0]["sha"], sha)

    def test_an_inverted_window_is_rejected(self):
        self.write_ledger()
        with self.assertRaises(CoverageError):
            self.run_coverage(since=stamp(10), until=stamp(10))


class TimestampTest(unittest.TestCase):
    def test_a_bare_date_is_read_as_utc_midnight(self):
        self.assertEqual(
            parse_timestamp("2026-09-01", field="since"),
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

    def test_a_trailing_z_and_an_explicit_offset_agree(self):
        self.assertEqual(
            parse_timestamp("2026-09-01T12:00:00Z", field="since"),
            parse_timestamp("2026-09-01T08:00:00-04:00", field="since"),
        )

    def test_a_naive_timestamp_is_read_as_utc(self):
        self.assertEqual(
            parse_timestamp("2026-09-01T12:00:00", field="since"),
            datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        )

    def test_unparseable_text_is_rejected(self):
        for value in ("", "   ", "yesterday", "2026-13-01"):
            with self.subTest(value=value):
                with self.assertRaises(CoverageError):
                    parse_timestamp(value, field="since")


class ParserRegistrationTest(unittest.TestCase):
    def test_coverage_is_a_subcommand_of_the_shared_cli(self):
        from council_tools.cli import build_parser, command_coverage

        parsed = build_parser().parse_args(
            ["coverage", "--repo", "/tmp/example", "--since", "2026-09-01"]
        )
        self.assertIs(parsed.func, command_coverage)
        self.assertEqual(parsed.ref, "HEAD")
        self.assertIsNone(parsed.until)
        self.assertFalse(parsed.include_merges)
        self.assertEqual(parsed.since, datetime(2026, 9, 1, tzinfo=timezone.utc))


class CoverageCliTest(CoverageTestBase):
    def run_cli(self, *arguments):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "council_tools.cli", "coverage", *arguments],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def base_arguments(self, *extra):
        return (
            "--repo",
            str(self.repo_path),
            "--log",
            str(self.log),
            "--since",
            text(stamp(0)),
            "--until",
            text(stamp(600)),
            *extra,
        )

    def test_clean_window_exits_zero(self):
        sha = self.repo.commit("reviewed", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), [sha]))
        result = self.run_cli(*self.base_arguments())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("covered_rate=1.0000", result.stdout)

    def test_unreviewed_commit_exits_one(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger()
        result = self.run_cli(*self.base_arguments())
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("UNCOVERED", result.stdout)

    def test_refusal_exits_three_and_json_omits_the_rate(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger("{not json")
        result = self.run_cli(*self.base_arguments("--json"))
        self.assertEqual(result.returncode, 3, result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["determined"])
        self.assertIsNone(payload["rate"])
        self.assertNotIn("counts", payload)

    def test_a_malformed_window_bound_is_a_usage_error(self):
        self.write_ledger()
        result = self.run_cli(
            "--repo", str(self.repo_path), "--log", str(self.log), "--since", "soon"
        )
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_an_inverted_window_is_a_usage_error_not_a_verdict(self):
        self.write_ledger()
        result = self.run_cli(*self.base_arguments("--until", text(stamp(0))))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("--since must be strictly before --until", result.stderr)

    def test_repo_is_required(self):
        result = self.run_cli("--since", text(stamp(0)))
        self.assertEqual(result.returncode, 2, result.stdout)


if __name__ == "__main__":
    unittest.main()
