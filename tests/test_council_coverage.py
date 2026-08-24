import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest import mock

from council_tools import council_coverage
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
        # The instrumentation epoch must be attributable to THIS repository, so
        # the anchor row has to name an object that resolves here. The anchor
        # commit sits before the default window so it never perturbs counts.
        self.anchor_sha = self.repo.commit("instrumentation anchor", at=stamp(-60))

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
        gate, so they need one convention row, naming a resolvable object, at or
        before the window start. Tests that exercise the gate pass
        ``anchor=False``.
        """

        entries = list(rows)
        if anchor:
            entries.insert(0, self.council_row(stamp(-30), [self.anchor_sha]))
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
        self.assertIn("author-exempted", result["rateNote"])
        # Every commit exempted by its own author is not a clean window; it is a
        # window with no eligible population, which is cannot-determine.
        self.assertEqual(coverage_exit_code(result), 3)

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
        # The tripwire for a window measured against the wrong repository.
        self.assertEqual(result["ledger"]["reviewedNamesSeen"], 2)
        self.assertEqual(result["ledger"]["reviewedNamesResolvedInRepo"], 1)

    def test_an_ambiguous_row_without_a_readable_timestamp_stays_ambiguous(self):
        base = self.repo.commit("base", at=stamp(5))
        later = self.repo.commit("descendant", at=stamp(400))
        self.write_ledger(
            {"kind": "council", "ts": "not-a-timestamp", "commits": {"base": base}}
        )
        result = self.run_coverage()
        self.assertEqual(self.states(result)[later], UNKNOWN)


class LedgerShapeTest(CoverageTestBase):
    def test_an_empty_array_is_convention_but_attributes_to_no_repository(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(1), []), anchor=False)
        result = self.run_coverage(since=stamp(1))
        # An empty array proves the writer used the convention, but it names no
        # object, so it cannot prove the convention was in force for THIS repo.
        self.assertFalse(result["determined"], result)
        self.assertEqual(
            result["refusals"][0]["code"], "no-repo-attributable-instrumentation"
        )
        self.assertEqual(result["ledger"]["conventionCouncilRows"], 1)

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
        self.assertEqual(result["ledger"]["conventionCouncilRows"], 1)
        self.assertEqual(result["ledger"]["ambiguousCommitRows"], 1)

    def test_an_abbreviated_array_covers_but_does_not_set_the_convention(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), [sha[:7]]))
        result = self.run_coverage()
        # An array is a statement of what was read, so it covers even when it
        # is spelled short -- it is simply not evidence the convention was in
        # force, and it is never reinterpreted as an ancestry base.
        self.assertEqual(self.states(result), {sha: COVERED})
        self.assertEqual(result["ledger"]["conventionCouncilRows"], 1)
        self.assertEqual(result["ledger"]["councilRowsNamingCommits"], 2)
        self.assertEqual(result["ledger"]["ambiguousCommitRows"], 0)

    def test_evidence_reader_counts_rows_without_a_commits_field(self):
        self.write_ledger(
            {"kind": "council", "ts": text(stamp(1)), "question": "q"},
            self.council_row(stamp(2), []),
            anchor=False,
        )
        evidence = read_ledger_evidence(self.log)
        self.assertEqual(evidence.rows_read, 2)
        self.assertEqual(len(evidence.convention_rows), 1)
        self.assertEqual(evidence.ambiguous_rows, 0)
        self.assertEqual(evidence.council_rows_without_commits, 1)


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
        self.write_ledger(
            self.council_row(stamp(5), [self.anchor_sha]), anchor=False
        )
        result = self.run_coverage(since=stamp(0))
        self.assert_refused(result, "window-predates-commit-instrumentation")
        self.assertIn("unrecoverable", result["refusals"][0]["detail"])
        # A refusal must tell the operator what to run instead.
        self.assertIn(f"--since {text(stamp(5))}", result["refusals"][0]["detail"])

    def test_a_window_starting_exactly_at_the_epoch_is_allowed(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(
            self.council_row(stamp(5), [self.anchor_sha]), anchor=False
        )
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

    def test_a_missing_ledger_reads_as_a_path_problem_not_a_convention_gap(self):
        self.repo.commit("shipped", at=stamp(10))
        result = self.run_coverage()
        self.assert_refused(result, "ledger-unreadable")
        # At 3am "unrecoverable from this ledger" would send the operator
        # hunting for a convention gap instead of a typo.
        self.assertIn("does not exist", result["refusals"][0]["detail"])


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


class CouncilFindingRegressionTest(CoverageTestBase):
    """One test per defect the 2026-08-24 review council found.

    A green suite did not catch any of these, so each is pinned by the exact
    scenario a seat reproduced.
    """

    # --- fail-open: exemption scanned outside the trailer block -------------

    def test_a_message_documenting_the_convention_does_not_exempt_itself(self):
        sha = self.repo.commit(
            "Rewrite the live order sizing path\n"
            "\n"
            "The exemption trailer is spelled:\n"
            "\n"
            "Council-Exempt: mechanical rename\n"
            "\n"
            "and an empty reason fails closed. This commit changes live\n"
            "behaviour and nobody reviewed it.\n",
            at=stamp(10),
        )
        self.write_ledger()
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: UNCOVERED})
        self.assertEqual(coverage_exit_code(result), 1)

    def test_a_placeholder_reason_does_not_exempt(self):
        sha = self.repo.commit("Do a thing\n\nCouncil-Exempt: <reason>", at=stamp(10))
        self.write_ledger()
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: UNCOVERED})

    def test_an_indented_trailer_is_quoted_prose_not_an_exemption(self):
        sha = self.repo.commit(
            "Do a thing\n\n    Council-Exempt: mechanical", at=stamp(10)
        )
        self.write_ledger()
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: UNCOVERED})

    def test_a_real_trailer_in_the_final_block_still_exempts(self):
        sha = self.repo.commit(
            "Fix a typo in a comment\n"
            "\n"
            "No behaviour changes at all.\n"
            "\n"
            "Council-Exempt: comment-only edit\n",
            at=stamp(10),
        )
        self.write_ledger()
        result = self.run_coverage()
        self.assertEqual(self.states(result), {sha: EXEMPT})

    # --- fail-open: git log --since prunes traversal ------------------------

    def test_a_backdated_tip_cannot_empty_the_denominator(self):
        first = self.repo.commit("c1", at=stamp(10))
        second = self.repo.commit("c2", at=stamp(20))
        third = self.repo.commit("c3", at=stamp(30))
        # `git log --since` marks this uninteresting and prunes its parents, so
        # a pre-filtered walk returns nothing at all for the window below.
        self.repo.commit("backdated tip", at=stamp(-600))
        self.write_ledger()
        result = self.run_coverage(since=stamp(5), until=stamp(40))
        self.assertEqual(
            sorted(self.states(result)), sorted([first, second, third])
        )
        self.assertEqual(result["counts"]["uncovered"], 3)
        self.assertEqual(coverage_exit_code(result), 1)

    # --- fail-open: measuring nothing reported as clean ---------------------

    def test_an_empty_window_refuses_rather_than_reporting_clean(self):
        self.repo.commit("outside the window", at=stamp(500))
        self.write_ledger()
        result = self.run_coverage(since=stamp(0), until=stamp(100))
        self.assertFalse(result["determined"], result)
        self.assertEqual(result["refusals"][0]["code"], "empty-window")
        self.assertIn("measuring nothing", result["refusals"][0]["detail"])
        self.assertEqual(coverage_exit_code(result), 3)

    # --- fail-open: a swallowed git failure downgraded UNKNOWN to UNCOVERED --

    def test_a_git_failure_refuses_instead_of_accusing(self):
        base = self.repo.commit("base", at=stamp(5))
        self.repo.commit("descendant", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), {"base": base}))
        real_git = council_coverage._git

        def fail_rev_list(repo, *arguments, **kwargs):
            if arguments and arguments[0] == "rev-list":
                raise council_coverage.GitError("injected rev-list failure")
            return real_git(repo, *arguments, **kwargs)

        with mock.patch.object(council_coverage, "_git", side_effect=fail_rev_list):
            result = self.run_coverage()
        self.assertFalse(result["determined"], result)
        self.assertEqual(result["refusals"][0]["code"], "git-unavailable")
        self.assertEqual(coverage_exit_code(result), 3)

    # --- refusal gaps -------------------------------------------------------

    def test_a_shallow_clone_refuses(self):
        for index in range(4):
            self.repo.commit(f"c{index}", at=stamp(10 + index))
        shallow_path = self.root / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{self.repo_path}",
             str(shallow_path)],
            check=True, capture_output=True, text=True, env=self.repo.env,
        )
        self.write_ledger()
        result = self.run_coverage(repo=shallow_path)
        self.assertFalse(result["determined"], result)
        self.assertEqual(result["refusals"][0]["code"], "shallow-repository")
        self.assertEqual(coverage_exit_code(result), 3)

    def test_a_convention_row_about_another_repository_does_not_open_the_gate(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(1), ["b" * 40]), anchor=False)
        result = self.run_coverage(since=stamp(1))
        self.assertFalse(result["determined"], result)
        self.assertEqual(
            result["refusals"][0]["code"], "no-repo-attributable-instrumentation"
        )
        self.assertIn("shared across repositories", result["refusals"][0]["detail"])

    def test_a_naive_timestamp_on_a_convention_row_is_unreadable(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(
            {
                "kind": "council",
                "ts": "2026-09-01T00:20:00",
                "question": "q",
                "commits": [self.anchor_sha],
            },
            anchor=False,
        )
        result = self.run_coverage()
        self.assertFalse(result["determined"], result)
        self.assertEqual(result["refusals"][0]["code"], "ledger-unreadable")
        self.assertIn("timezone", result["refusals"][0]["detail"])

    # --- identity: patch-id fallback ---------------------------------------

    def test_a_cherry_picked_copy_of_a_reviewed_commit_is_covered(self):
        base = self.repo.commit("base", at=stamp(5))
        self.repo.git("checkout", "-q", "-b", "side", base)
        (self.repo_path / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.repo.git("add", "feature.txt")
        self.repo.git("commit", "-q", "-m", "add the feature", at=stamp(10))
        reviewed = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.git("checkout", "-q", "main")
        self.repo.git("cherry-pick", reviewed, at=stamp(20))
        shipped = self.repo.git("rev-parse", "HEAD").strip()
        self.assertNotEqual(reviewed, shipped)

        self.write_ledger(self.council_row(stamp(15), [reviewed]))
        result = self.run_coverage(ref="main")
        self.assertEqual(self.states(result)[shipped], COVERED)
        self.assertIn(
            "patch-identical",
            [c["reason"] for c in result["commits"] if c["sha"] == shipped][0],
        )

    def test_patch_identity_does_not_cover_different_content(self):
        base = self.repo.commit("base", at=stamp(5))
        self.repo.git("checkout", "-q", "-b", "side", base)
        (self.repo_path / "feature.txt").write_text("one\n", encoding="utf-8")
        self.repo.git("add", "feature.txt")
        self.repo.git("commit", "-q", "-m", "add the feature", at=stamp(10))
        reviewed = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.git("checkout", "-q", "main")
        (self.repo_path / "feature.txt").write_text("something else\n", encoding="utf-8")
        self.repo.git("add", "feature.txt")
        self.repo.git("commit", "-q", "-m", "add the feature", at=stamp(20))
        shipped = self.repo.git("rev-parse", "HEAD").strip()

        self.write_ledger(self.council_row(stamp(15), [reviewed]))
        result = self.run_coverage(ref="main")
        self.assertEqual(self.states(result)[shipped], UNCOVERED)

    # --- visibility ---------------------------------------------------------

    def test_council_rows_that_cannot_name_anything_are_counted(self):
        self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(
            {"kind": "council", "ts": text(stamp(20)), "question": "reviewed a tree"}
        )
        result = self.run_coverage()
        # 19 such rows exist in the live ledger; without this count their false
        # UNCOVERED results look like findings.
        self.assertEqual(result["ledger"]["councilRowsWithoutCommits"], 1)

    def test_a_covered_commit_reports_when_its_row_was_written(self):
        sha = self.repo.commit("shipped", at=stamp(10))
        self.write_ledger(self.council_row(stamp(20), [sha]))
        result = self.run_coverage()
        entry = result["commits"][0]
        self.assertEqual(entry["state"], COVERED)
        self.assertEqual(entry["namedByRowAt"], text(stamp(20)))
        # COVERED does not establish review-before-merge; this makes the lag
        # visible rather than inventing a merge clock.
        self.assertEqual(result["counts"]["coveredWithRowAfterCommit"], 1)

    def test_uncovered_sorts_before_unknown_in_the_rendering(self):
        base = self.repo.commit("base", at=stamp(5))
        self.repo.commit("maybe reviewed", at=stamp(10))
        self.repo.commit("definitely not", at=stamp(40))
        self.write_ledger(self.council_row(stamp(20), {"base": base}))
        rendered = format_coverage(self.run_coverage())
        states = [
            line.split()[0]
            for line in rendered.splitlines()
            if line.startswith(("UNCOVERED", "UNKNOWN"))
        ]
        self.assertEqual(states[: states.count("UNCOVERED")], ["UNCOVERED"] * 2)

    def test_git_error_shares_the_package_value_error_base(self):
        self.assertTrue(issubclass(council_coverage.GitError, CoverageError))
        self.assertTrue(issubclass(council_coverage.GitError, ValueError))


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
