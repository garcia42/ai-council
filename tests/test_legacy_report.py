import io
import json
import tempfile
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


if __name__ == "__main__":
    unittest.main()
