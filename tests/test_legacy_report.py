import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from council_tools import legacy_report


class LegacyReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "forecasts.jsonl"
        self.resolved = self.root / "resolved.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def test_zero_probability_scores_as_zero_for_false_outcome(self):
        self.log.write_text(
            json.dumps(
                {
                    "ts": "2026-08-18T00:00:00Z",
                    "predictions": [
                        {
                            "seat": "research",
                            "claim": "A legacy outcome",
                            "probability": 0,
                            "resolutionDate": "2026-08-19",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.resolved.write_text(
            json.dumps(
                {
                    "key": "2026-08-18T00:00:00Z#0",
                    "probability": 0,
                    "came_true": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = legacy_report.main(
                ["--all"], log_path=self.log, resolved_path=self.resolved
            )
        self.assertEqual(result, 0)
        self.assertIn("Brier score:     0.000", stdout.getvalue())

    def test_malformed_legacy_json_fails_closed(self):
        self.log.write_text("not-json\n", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = legacy_report.main(
                ["--all"], log_path=self.log, resolved_path=self.resolved
            )
        self.assertEqual(result, 1)
        self.assertIn("line 1", stderr.getvalue())

    def test_truncated_legacy_resolution_sidecar_fails_closed(self):
        self.log.write_text("", encoding="utf-8")
        self.resolved.write_text('{"key":', encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = legacy_report.main(
                ["--all"], log_path=self.log, resolved_path=self.resolved
            )
        self.assertEqual(result, 1)
        self.assertIn("resolved.jsonl line 1", stderr.getvalue())

    def test_unknown_or_council_arguments_fail_closed(self):
        self.log.write_text("", encoding="utf-8")
        for arguments in (["report", "--json"], ["--json"], ["--all", "extra"]):
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    result = legacy_report.main(
                        arguments, log_path=self.log, resolved_path=self.resolved
                    )
                self.assertEqual(result, 1)
                self.assertIn("accepts only", stderr.getvalue())

    def test_resolution_requires_exact_boolean_literal(self):
        self.log.write_text(
            json.dumps(
                {
                    "ts": "2026-08-18T00:00:00Z",
                    "predictions": [
                        {"seat": "research", "claim": "A claim", "probability": 60}
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = legacy_report.main(
                ["--resolve", "2026-08-18T00:00:00Z", "0", "ture"],
                log_path=self.log,
                resolved_path=self.resolved,
            )
        self.assertEqual(result, 1)
        self.assertIn("exactly true or false", stderr.getvalue())

    def test_timestamp_index_resolution_identity_is_accepted(self):
        self.log.write_text(
            json.dumps(
                {
                    "ts": "2026-08-18T00:00:00Z",
                    "predictions": [
                        {
                            "seat": "research",
                            "claim": "Already resolved",
                            "probability": 0,
                            "resolutionDate": "2026-08-18",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.resolved.write_text(
            json.dumps(
                {
                    "ts": "2026-08-18T00:00:00Z",
                    "index": 0,
                    "probability": 0,
                    "came_true": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = legacy_report.main(
                ["--all"], log_path=self.log, resolved_path=self.resolved
            )
        self.assertEqual(result, 0)
        self.assertIn("DUE FOR SCORING (0)", stdout.getvalue())
        self.assertIn("CALIBRATION (1 resolved)", stdout.getvalue())

    def test_concurrent_duplicate_legacy_resolution_appends_once(self):
        self.log.write_text(
            json.dumps(
                {
                    "ts": "2026-08-18T00:00:00Z",
                    "predictions": [
                        {
                            "seat": "research",
                            "claim": "A concurrent legacy outcome",
                            "probability": 60,
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        barrier = threading.Barrier(4)
        successes = []
        errors = []

        def write():
            barrier.wait()
            try:
                legacy_report.resolve(
                    self.log,
                    self.resolved,
                    ts="2026-08-18T00:00:00Z",
                    index=0,
                    came_true=True,
                    note="concurrent test",
                )
                successes.append(True)
            except Exception as exc:  # captured and asserted in the parent thread
                errors.append(exc)

        threads = [threading.Thread(target=write) for _ in range(4)]
        with redirect_stdout(io.StringIO()):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(isinstance(item, legacy_report.LegacyReportError) for item in errors))
        self.assertEqual(len(self.resolved.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
