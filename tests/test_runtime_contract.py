import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


RUNTIME_ROOT = Path(os.environ.get("COUNCIL_RUNTIME_ROOT", "/home/trader")) / ".claude"


class RuntimeContractTest(unittest.TestCase):
    def test_installed_council_skill_names_forecast_contract(self):
        text = (RUNTIME_ROOT / "skills/council/SKILL.md").read_text(encoding="utf-8")
        for required in (
            "council-attempt",
            "shared outcome",
            "forecastState",
            "complete --spec <completion-spec.json> --check-only",
            "grading debt",
        ):
            self.assertIn(required, text)

    def test_blind_tally_allows_attempt_records(self):
        path = RUNTIME_ROOT / "knowledge/council-eval/blind_seat_kill_criterion.py"
        spec = importlib.util.spec_from_file_location("blind_criterion_runtime", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.tally([(1, {"kind": "council-attempt", "runId": "run-x"})])
        self.assertEqual(result["legacyBlindRows"], 0)
        self.assertEqual(result["nonCouncilRecords"], 1)

    def test_compatibility_wrapper_preserves_explicit_environment_ledgers(self):
        reporter = RUNTIME_ROOT / "knowledge/council-eval/predictions_report.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "separate-log.jsonl"
            events = root / "separate-events.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "ts": "2026-08-18T00:00:00Z",
                        "kind": "phase0",
                        "predictions": [
                            {
                                "seat": "research",
                                "claim": "A separate venture outcome",
                                "probability": 80,
                                "resolutionDate": "2026-08-19",
                                "resolvedBy": "Inspect separate evidence",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            events.write_text(
                json.dumps(
                    {
                        "key": "2026-08-18T00:00:00Z#0",
                        "ts": "2026-08-18T00:00:00Z",
                        "index": 0,
                        "seat": "research",
                        "claim": "A separate venture outcome",
                        "probability": 80,
                        "came_true": True,
                        "note": "separate evidence",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["PANEL_LOG"] = str(log)
            env["PANEL_RESOLVED"] = str(events)
            result = subprocess.run(
                [sys.executable, str(reporter), "--all"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CALIBRATION (1 resolved)", result.stdout)
        self.assertIn("Brier score:", result.stdout)
        self.assertNotIn("councilRows", result.stdout)


if __name__ == "__main__":
    unittest.main()
