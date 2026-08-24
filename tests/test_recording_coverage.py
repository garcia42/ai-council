import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from council_tools.recording_coverage import (
    ARRAY_ABBREVIATED,
    ARRAY_EMPTY,
    ARRAY_FULL_SHAS,
    FIELD_ABSENT,
    FIELD_NULL,
    OBJECT_BASE_ONLY,
    OBJECT_PRECOMMIT,
    OTHER,
    RecordingCoverageError,
    classify_shape,
    format_recording_coverage,
    parse_timestamp,
    recording_exit_code,
    report_recording_coverage,
)


EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc)
SHA = "a" * 40
OTHER_SHA = "b" * 40


def stamp(minutes: int) -> datetime:
    return EPOCH + timedelta(minutes=minutes)


def text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ShapeTest(unittest.TestCase):
    def shape(self, commits, present=True):
        row = {"kind": "council", "ts": text(stamp(0)), "question": "q"}
        if present:
            row["commits"] = commits
        return classify_shape(row)

    def test_full_sha_array_is_the_convention(self):
        self.assertEqual(self.shape([SHA, OTHER_SHA]), ARRAY_FULL_SHAS)

    def test_any_abbreviation_leaves_the_convention(self):
        self.assertEqual(self.shape([SHA, OTHER_SHA[:7]]), ARRAY_ABBREVIATED)
        self.assertEqual(self.shape([SHA[:12]]), ARRAY_ABBREVIATED)

    def test_empty_array_is_indistinguishable_from_unset(self):
        self.assertEqual(self.shape([]), ARRAY_EMPTY)

    def test_a_staged_tree_review_is_recognised(self):
        for commits in (
            {"base": SHA, "stagedTree": OTHER_SHA},
            {"base": SHA, "candidate_tree": OTHER_SHA},
            {"base": SHA, "state": "uncommitted-untracked"},
        ):
            with self.subTest(commits=commits):
                self.assertEqual(self.shape(commits), OBJECT_PRECOMMIT)

    def test_a_bare_base_pointer_names_the_branch_point_only(self):
        self.assertEqual(self.shape({"base": SHA}), OBJECT_BASE_ONLY)
        self.assertEqual(
            self.shape({"base": SHA, "candidate": OTHER_SHA}), OBJECT_BASE_ONLY
        )

    def test_null_and_absent_are_distinguished(self):
        self.assertEqual(self.shape(None), FIELD_NULL)
        self.assertEqual(self.shape(None, present=False), FIELD_ABSENT)

    def test_unexpected_shapes_are_other_not_silently_dropped(self):
        for commits in ("a string", 7, [1, 2], [""], [SHA, 3]):
            with self.subTest(commits=commits):
                self.assertEqual(self.shape(commits), OTHER)


class ReportTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.temp.name)
        self.log = self.root / "council.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def council_row(self, ts, commits="__absent__", kind="council"):
        row = {"schemaVersion": 1, "kind": kind, "ts": text(ts), "question": "q"}
        if commits != "__absent__":
            row["commits"] = commits
        return row

    def write_ledger(self, *rows):
        with self.log.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write((row if isinstance(row, str) else json.dumps(row)) + "\n")

    def run_report(self, **overrides):
        arguments = {"log_path": self.log}
        arguments.update(overrides)
        return report_recording_coverage(**arguments)


class AdoptionTest(ReportTestBase):
    def test_adoption_counts_only_rows_that_name_commits(self):
        self.write_ledger(
            self.council_row(stamp(1), [SHA]),
            self.council_row(stamp(2), [OTHER_SHA[:8]]),
            self.council_row(stamp(3), []),
            self.council_row(stamp(4), {"base": SHA}),
        )
        result = self.run_report()
        self.assertTrue(result["determined"], result)
        self.assertEqual(result["councilRows"], 4)
        self.assertEqual(result["adoption"]["rowsNamingCommits"], 2)
        self.assertEqual(result["adoption"]["rowsUnableToNameCommits"], 2)
        self.assertEqual(result["adoption"]["share"], 0.5)
        self.assertEqual(recording_exit_code(result), 0)

    def test_precommit_reviews_are_surfaced_separately(self):
        self.write_ledger(
            self.council_row(stamp(1), {"base": SHA, "stagedTree": OTHER_SHA}),
            self.council_row(stamp(2), [SHA]),
        )
        result = self.run_report()
        # These reviews were real; there was simply no commit to name. They are
        # not a recording failure and must not read as one.
        self.assertEqual(result["precommitReviews"], 1)
        self.assertEqual(result["shapes"][OBJECT_PRECOMMIT]["rows"], 1)

    def test_shares_sum_to_one_and_every_shape_is_reported(self):
        self.write_ledger(
            self.council_row(stamp(1), [SHA]),
            self.council_row(stamp(2), {"base": SHA}),
            self.council_row(stamp(3)),
        )
        result = self.run_report()
        self.assertEqual(set(result["shapes"]), set(
            [ARRAY_FULL_SHAS, ARRAY_ABBREVIATED, ARRAY_EMPTY, OBJECT_PRECOMMIT,
             OBJECT_BASE_ONLY, FIELD_NULL, FIELD_ABSENT, OTHER]))
        self.assertAlmostEqual(
            sum(entry["share"] for entry in result["shapes"].values()), 1.0
        )

    def test_non_council_rows_are_ignored(self):
        self.write_ledger(
            self.council_row(stamp(1), [SHA]),
            self.council_row(stamp(2), [OTHER_SHA], kind="council-attempt"),
            {"kind": "blind-seat", "ts": text(stamp(3))},
        )
        result = self.run_report()
        self.assertEqual(result["councilRows"], 1)


class WindowTest(ReportTestBase):
    def test_the_window_is_half_open_on_row_timestamp(self):
        self.write_ledger(
            self.council_row(stamp(5), [SHA]),
            self.council_row(stamp(10), [SHA]),
            self.council_row(stamp(20), [SHA]),
        )
        result = self.run_report(since=stamp(10), until=stamp(20))
        self.assertEqual(result["councilRows"], 1)

    def test_an_unreadable_timestamp_is_counted_never_dropped(self):
        self.write_ledger(
            self.council_row(stamp(10), [SHA]),
            {"kind": "council", "ts": "not-a-timestamp", "commits": [SHA]},
            {"kind": "council", "commits": [SHA]},
        )
        result = self.run_report(since=stamp(0), until=stamp(100))
        self.assertEqual(result["councilRows"], 1)
        self.assertEqual(result["rowsWithUnreadableTs"], 2)

    def test_timestamps_are_only_needed_when_a_window_is_given(self):
        self.write_ledger({"kind": "council", "commits": [SHA]})
        result = self.run_report()
        self.assertEqual(result["councilRows"], 1)
        self.assertEqual(result["adoption"]["share"], 1.0)

    def test_an_inverted_window_is_rejected(self):
        self.write_ledger(self.council_row(stamp(1), [SHA]))
        with self.assertRaises(RecordingCoverageError):
            self.run_report(since=stamp(10), until=stamp(10))


class RefusalTest(ReportTestBase):
    def assert_refused(self, result, code):
        self.assertFalse(result["determined"], result)
        self.assertEqual([item["code"] for item in result["refusals"]], [code])
        self.assertIsNone(result["adoption"])
        self.assertNotIn("shapes", result)
        self.assertNotIn("councilRows", result)
        self.assertEqual(recording_exit_code(result), 3)
        self.assertIn("adoption=UNAVAILABLE", format_recording_coverage(result))

    def test_a_missing_ledger_refuses(self):
        self.assert_refused(self.run_report(), "ledger-unreadable")

    def test_an_unparseable_line_refuses_rather_than_reporting_a_share(self):
        self.write_ledger(self.council_row(stamp(1), [SHA]), "{not json")
        self.assert_refused(self.run_report(), "ledger-unreadable")

    def test_a_window_with_no_council_rows_has_no_denominator(self):
        self.write_ledger(self.council_row(stamp(1), [SHA]))
        result = self.run_report(since=stamp(100), until=stamp(200))
        self.assert_refused(result, "no-council-rows")
        self.assertIn("unreadable ts", result["refusals"][0]["detail"])

    def test_a_ledger_with_no_council_rows_at_all_refuses(self):
        self.write_ledger({"kind": "council-attempt", "ts": text(stamp(1))})
        self.assert_refused(self.run_report(), "no-council-rows")


class ExitCodeTest(ReportTestBase):
    def test_low_adoption_is_a_finding_not_an_error(self):
        self.write_ledger(*[self.council_row(stamp(i), {"base": SHA}) for i in range(5)])
        result = self.run_report()
        self.assertEqual(result["adoption"]["share"], 0.0)
        # Zero adoption is exactly what this report exists to show. An exit code
        # that went red here would stay red for months and train its reader to
        # ignore it.
        self.assertEqual(recording_exit_code(result), 0)

    def test_there_is_no_exit_one(self):
        self.write_ledger(self.council_row(stamp(1), {"base": SHA}))
        determined = self.run_report()
        self.write_ledger("{not json")
        refused = self.run_report()
        self.assertEqual(
            {recording_exit_code(determined), recording_exit_code(refused)}, {0, 3}
        )


class RenderingTest(ReportTestBase):
    def test_shapes_with_no_rows_are_not_printed(self):
        self.write_ledger(self.council_row(stamp(1), [SHA]))
        rendered = format_recording_coverage(self.run_report())
        self.assertIn(ARRAY_FULL_SHAS, rendered)
        self.assertNotIn(FIELD_NULL, rendered)

    def test_rows_that_name_commits_are_marked(self):
        self.write_ledger(
            self.council_row(stamp(1), [SHA]),
            self.council_row(stamp(2), {"base": SHA}),
        )
        rendered = format_recording_coverage(self.run_report())
        naming = [line for line in rendered.splitlines() if ARRAY_FULL_SHAS in line]
        other = [line for line in rendered.splitlines() if OBJECT_BASE_ONLY in line]
        self.assertIn("names-commits", naming[0])
        self.assertNotIn("names-commits", other[0])


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

    def test_unparseable_text_is_rejected(self):
        for value in ("", "   ", "soon", "2026-13-01"):
            with self.subTest(value=value):
                with self.assertRaises(RecordingCoverageError):
                    parse_timestamp(value, field="since")


class RecordingCliTest(ReportTestBase):
    def run_cli(self, *arguments):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "council_tools.cli", "recording-coverage", *arguments],
            text=True, capture_output=True, env=env, check=False,
        )

    def test_a_determined_report_goes_to_stdout_and_exits_zero(self):
        self.write_ledger(self.council_row(stamp(1), [SHA]))
        result = self.run_cli("--log", str(self.log))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("adoption=1.0000", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_a_refusal_goes_to_stderr_and_exits_three(self):
        self.write_ledger("{not json")
        result = self.run_cli("--log", str(self.log))
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("REFUSED", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_json_stays_parseable_on_a_refusal(self):
        self.write_ledger("{not json")
        result = self.run_cli("--log", str(self.log), "--json")
        self.assertEqual(result.returncode, 3)
        payload = json.loads(result.stderr)
        self.assertFalse(payload["determined"])
        self.assertIsNone(payload["adoption"])

    def test_a_malformed_window_bound_is_a_usage_error(self):
        self.write_ledger(self.council_row(stamp(1), [SHA]))
        result = self.run_cli("--log", str(self.log), "--since", "soon")
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_an_inverted_window_is_a_usage_error_not_a_verdict(self):
        self.write_ledger(self.council_row(stamp(1), [SHA]))
        result = self.run_cli(
            "--log", str(self.log), "--since", text(stamp(10)), "--until", text(stamp(5))
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("--since must be strictly before --until", result.stderr)


if __name__ == "__main__":
    unittest.main()
