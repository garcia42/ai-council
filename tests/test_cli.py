import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from council_tools import cli
from council_tools.forecasts import append_ledger_row, make_attempt, new_id


def completion(attempt):
    outcome = attempt["sharedOutcome"]
    seats = attempt["expectedSeats"]
    return {
        "schemaVersion": 1,
        "kind": "council",
        "runId": attempt["runId"],
        "ts": "2026-07-01T00:10:00Z",
        "question": attempt["question"],
        "verdicts": {"code": "APPROVE", "theory": "APPROVE", "ops": "APPROVE"},
        "blindSeat": {
            "role": "SKIPPED",
            "required": False,
            "ran": False,
            "changedDecision": None,
        },
        "forecastState": {
            "sealed": True,
            "seats": {seat: "submitted" for seat in seats},
        },
        "predictions": [
            {
                "predictionId": new_id("prediction"),
                "outcomeId": outcome["outcomeId"],
                "seat": seat,
                "type": "shared",
                "claim": outcome["claim"],
                "probability": 60,
                "issuedAt": "2026-07-01T00:05:00Z",
                "resolutionDate": outcome["resolutionDate"],
                "resolvedBy": outcome["resolvedBy"],
            }
            for seat in seats
        ],
    }


class ForecastCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "panel.jsonl"
        self.events = self.root / "events.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "council_tools.cli", *args],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_report_json_uses_injected_paths_and_clock(self):
        result = self.run_cli(
            "report",
            "--log",
            str(self.log),
            "--events",
            str(self.events),
            "--today",
            "2026-08-22",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["forecastIssuances"], 0)
        self.assertIn("DESCRIPTIVE ONLY", payload["label"])

        human = self.run_cli(
            "report",
            "--log",
            str(self.log),
            "--events",
            str(self.events),
            "--today",
            "2026-08-22",
        )
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn(payload["label"], human.stdout)

    def test_invalid_ledger_exits_one(self):
        self.log.write_text("not-json\n", encoding="utf-8")
        result = self.run_cli(
            "report", "--log", str(self.log), "--events", str(self.events)
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("line 1", result.stderr)

    def test_attempt_and_complete_seal_one_shared_set(self):
        attempt_spec = self.root / "attempt.json"
        attempt_spec.write_text(
            json.dumps(
                {
                    "question": "Activate the scorer?",
                    "expectedSeats": ["code", "theory", "ops", "blind"],
                    "sharedOutcome": {
                        "claim": "The first ten rows have complete forecasts",
                        "resolutionDate": "2026-09-30",
                        "resolvedBy": "Audit the first ten rows",
                        "decisionLink": "Scorer activation",
                        "materiality": "Missing forecasts invalidate calibration",
                        "actionIfTrue": "Keep collecting",
                        "actionIfFalse": "Repair emission",
                        "evidenceCutoffAt": "2026-08-22T12:00:00Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        started = self.run_cli(
            "attempt",
            "--log",
            str(self.log),
            "--spec",
            str(attempt_spec),
            "--ts",
            "2026-08-22T12:00:00Z",
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["runId"]
        complete_spec = self.root / "complete.json"
        complete_spec.write_text(
            json.dumps(
                {
                    "runId": run_id,
                    "councilFields": {
                        "verdicts": {
                            "code": "APPROVE",
                            "theory": "APPROVE",
                            "ops": "APPROVE",
                        },
                        "blindSeat": {
                            "role": "generic",
                            "required": True,
                            "ran": True,
                            "changedDecision": False,
                        },
                    },
                    "seatStates": {
                        "code": "submitted",
                        "theory": "submitted",
                        "ops": "submitted",
                        "blind": "submitted",
                    },
                    "probabilities": {
                        "code": 60,
                        "theory": 50,
                        "ops": 40,
                        "blind": 70,
                    },
                }
            ),
            encoding="utf-8",
        )
        checked = self.run_cli(
            "complete",
            "--log",
            str(self.log),
            "--spec",
            str(complete_spec),
            "--ts",
            "2026-08-22T12:10:00Z",
            "--check-only",
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 1)
        completed = self.run_cli(
            "complete",
            "--log",
            str(self.log),
            "--spec",
            str(complete_spec),
            "--ts",
            "2026-08-22T12:10:00Z",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = self.run_cli(
            "report",
            "--log",
            str(self.log),
            "--events",
            str(self.events),
            "--today",
            "2026-08-22",
            "--json",
        )
        payload = json.loads(report.stdout)
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(payload["completeForecastRows"], 1)
        self.assertEqual(payload["forecastIssuances"], 4)

    def test_debt_block_exits_three_but_record_check_still_runs(self):
        for index in range(3):
            row = make_attempt(
                question=f"Question {index}",
                expected_seats=["code", "theory", "ops"],
                claim=f"Outcome {index}",
                resolution_date="2026-08-01",
                resolved_by=f"Inspect outcome {index}",
                decision_link=f"Decision {index}",
                materiality="Changes whether the decision is finalized",
                action_if_true="Finalize",
                action_if_false="Hold",
                evidence_cutoff_at="2026-07-01T00:00:00Z",
                ts="2026-07-01T00:00:00Z",
            )
            append_ledger_row(self.log, row)
            append_ledger_row(self.log, completion(row))
        result = self.run_cli(
            "report",
            "--log",
            str(self.log),
            "--events",
            str(self.events),
            "--today",
            "2026-08-22",
        )
        self.assertEqual(result.returncode, 3)

    def test_resolve_rejects_unknown_outcome_before_sidecar_write(self):
        result = self.run_cli(
            "resolve",
            new_id("outcome"),
            "true",
            "--log",
            str(self.log),
            "--events",
            str(self.events),
            "--evidence",
            "audit evidence",
            "--resolver",
            "operator",
            "--method",
            "deterministic",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown council outcomeId", result.stderr)
        self.assertFalse(self.events.exists())

    def test_repair_tail_requires_confirmation_and_reports_backup(self):
        self.log.write_text('{"kind":"valid"}\n{"kind":', encoding="utf-8")
        quarantine = self.root / "quarantine"
        result = self.run_cli(
            "repair-tail",
            "--path",
            str(self.log),
            "--confirm-final-line",
            "2",
            "--backup-dir",
            str(quarantine),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["removedLine"], 2)
        self.assertTrue(Path(payload["backup"]).exists())
        self.assertEqual(self.log.read_text(encoding="utf-8"), '{"kind":"valid"}\n')

    def test_live_writes_refuse_non_authority_host(self):
        with mock.patch.object(cli.socket, "gethostname", return_value="not-manny"):
            with self.assertRaisesRegex(cli.LedgerError, "authorized only on manny"):
                cli._require_write_authority(cli.DEFAULT_LOG)

    def test_live_report_refuses_injected_test_clock(self):
        args = mock.Mock(
            today="2026-01-01",
            log=cli.DEFAULT_LOG,
            events=cli.DEFAULT_EVENTS,
            json=True,
        )
        with self.assertRaisesRegex(cli.LedgerError, "test-only"):
            cli.command_report(args)


if __name__ == "__main__":
    unittest.main()
