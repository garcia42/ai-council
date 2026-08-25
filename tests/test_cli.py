import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import install

from council_tools import capture_runtime, cli
from council_tools import safe_files
from council_tools.artifacts import ArtifactStore
from council_tools.forecasts import append_ledger_row, make_attempt, new_id
from tests.test_forecasts import (
    STRICT_JSON_ADVERSARIAL_PARITY_CASES,
    SUPERSEDE_ADVERSARIAL_PARITY_CASES,
    apply_supersede_adversarial_case,
)


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
            "brief": f"/tmp/council-briefs/brief-{attempt['runId']}.md",
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
        # /tmp is intentionally marked as a Git work tree on this host, while
        # capture artifacts must live outside every repository boundary.
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.temp.name)
        self.log = self.root / "panel.jsonl"
        self.events = self.root / "events.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        arguments = list(args)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "council_tools.cli", *arguments],
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
                            "brief": f"/tmp/council-briefs/brief-{run_id}.md",
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

    def test_attempt_retry_accepts_explicit_related_outcome_id(self):
        spec = self.root / "attempt.json"
        payload = {
            "question": "Retry this council?",
            "expectedSeats": ["code"],
            "sharedOutcome": {
                "claim": "A material retry outcome occurs",
                "resolutionDate": "2026-09-30",
                "resolvedBy": "Inspect the retry evidence",
                "decisionLink": "Retry decision",
                "materiality": "The retry changes the decision",
                "actionIfTrue": "Proceed",
                "actionIfFalse": "Hold",
                "evidenceCutoffAt": "2026-08-22T12:00:00Z",
            },
        }
        spec.write_text(json.dumps(payload), encoding="utf-8")
        first = self.run_cli(
            "attempt", "--log", str(self.log), "--spec", str(spec),
            "--ts", "2026-08-22T12:00:00Z",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        payload["sharedOutcome"]["relatedOutcomeIds"] = [
            json.loads(first.stdout)["outcomeId"]
        ]
        spec.write_text(json.dumps(payload), encoding="utf-8")
        second = self.run_cli(
            "attempt", "--log", str(self.log), "--spec", str(spec),
            "--ts", "2026-08-22T12:01:00Z",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 2)

    def test_malformed_spec_shapes_fail_without_traceback(self):
        attempt_spec = self.root / "bad-attempt.json"
        attempt_spec.write_text(json.dumps({"question": "missing seats"}), encoding="utf-8")
        attempt_result = self.run_cli(
            "attempt", "--log", str(self.log), "--spec", str(attempt_spec)
        )
        self.assertEqual(attempt_result.returncode, 1)
        self.assertIn("expectedSeats must be a list", attempt_result.stderr)
        self.assertNotIn("Traceback", attempt_result.stderr)

        completion_spec = self.root / "bad-completion.json"
        completion_spec.write_text(
            json.dumps(
                {
                    "runId": new_id("run"),
                    "councilFields": {},
                    "seatStates": [],
                    "probabilities": {},
                }
            ),
            encoding="utf-8",
        )
        completion_result = self.run_cli(
            "complete", "--log", str(self.log), "--spec", str(completion_spec)
        )
        self.assertEqual(completion_result.returncode, 1)
        self.assertIn("seatStates must be an object", completion_result.stderr)
        self.assertNotIn("Traceback", completion_result.stderr)

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

    def test_derived_ledger_lock_alias_into_live_tree_requires_authority(self):
        attempt_spec = self.root / "attempt.json"
        attempt_spec.write_text(
            json.dumps(
                {
                    "question": "Reject a redirected derived lock?",
                    "expectedSeats": ["code"],
                    "sharedOutcome": {
                        "claim": "The derived ledger lock remains local",
                        "resolutionDate": "2026-09-30",
                        "resolvedBy": "Inspect the derived lock target",
                        "decisionLink": "Local rehearsal safety",
                        "materiality": "A redirected lock can mutate live state",
                        "actionIfTrue": "Continue local rehearsal",
                        "actionIfFalse": "Repair the lock boundary",
                        "evidenceCutoffAt": "2026-08-22T12:00:00Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        derived = cli.derived_ledger_lock_path(self.log)
        derived.symlink_to(
            cli.LIVE_RUNTIME_STATE_ROOT / "dangling-derived-lock"
        )
        args = SimpleNamespace(
            log=str(self.log),
            spec=str(attempt_spec),
            ts="2026-08-22T12:00:00Z",
            coordination_lock=str(self.root / "coordination.lock"),
        )
        with mock.patch.object(cli.socket, "gethostname", return_value="not-manny"):
            with self.assertRaisesRegex(cli.LedgerError, "authorized only on manny"):
                cli.command_attempt(args)
        self.assertFalse(self.log.exists())

    def test_capture_resolve_authorizes_live_source_ledger_before_any_open(self):
        fake_live_root = self.root / "fake-live"
        fake_live_root.mkdir()
        live_log = fake_live_root / "capture.jsonl"
        live_log_lock = cli.derived_ledger_lock_path(live_log)
        args = SimpleNamespace(
            log=str(live_log),
            events=str(self.events),
            coordination_lock=str(self.root / "external-coordination.lock"),
            outcome_id=new_id("outcome"),
            outcome="true",
            evidence="external evidence",
            resolver="operator",
            method="deterministic",
            reviewer=None,
            void_reason=None,
            supersedes=None,
        )
        with (
            mock.patch.object(cli, "LIVE_WRITE_ROOTS", (fake_live_root.resolve(),)),
            mock.patch.object(cli.socket, "gethostname", return_value="not-manny"),
            mock.patch.object(cli, "append_capture_resolution") as append,
            mock.patch("builtins.print") as output,
        ):
            with self.assertRaisesRegex(cli.LedgerError, "authorized only on manny"):
                cli.command_capture_resolve(args)
        append.assert_not_called()
        output.assert_not_called()
        self.assertFalse(live_log.exists())
        self.assertFalse(live_log_lock.exists())
        self.assertFalse(self.events.exists())
        self.assertFalse(Path(args.coordination_lock).exists())

    def test_capture_resolve_allows_entirely_external_paths_off_authority_host(self):
        args = SimpleNamespace(
            log=str(self.log),
            events=str(self.events),
            coordination_lock=str(self.root / "external-coordination.lock"),
            outcome_id=new_id("outcome"),
            outcome="false",
            evidence="external evidence",
            resolver="operator",
            method="deterministic",
            reviewer=None,
            void_reason=None,
            supersedes=None,
        )
        event = {"resolutionId": new_id("resolution")}
        with (
            mock.patch.object(cli.socket, "gethostname", return_value="not-manny"),
            mock.patch.object(
                cli, "append_capture_resolution", return_value=event
            ) as append,
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(cli.command_capture_resolve(args), 0)
        append.assert_called_once()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["resolutionId"], event["resolutionId"])
        self.assertEqual(payload["transactionEscrows"], [])

    def test_capture_resolve_allows_live_source_ledger_on_authority_host(self):
        fake_live_root = self.root / "fake-live"
        fake_live_root.mkdir()
        args = SimpleNamespace(
            log=str(fake_live_root / "capture.jsonl"),
            events=str(self.events),
            coordination_lock=str(self.root / "external-coordination.lock"),
            outcome_id=new_id("outcome"),
            outcome="true",
            evidence="external evidence",
            resolver="operator",
            method="deterministic",
            reviewer=None,
            void_reason=None,
            supersedes=None,
        )
        event = {"resolutionId": new_id("resolution")}
        with (
            mock.patch.object(cli, "LIVE_WRITE_ROOTS", (fake_live_root.resolve(),)),
            mock.patch.object(cli.socket, "gethostname", return_value="manny"),
            mock.patch.object(
                cli, "append_capture_resolution", return_value=event
            ) as append,
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(cli.command_capture_resolve(args), 0)
        append.assert_called_once()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["resolutionId"], event["resolutionId"])
        self.assertEqual(payload["transactionEscrows"], [])

    def test_capture_resolve_secret_unknown_outcome_is_nonreflective(self):
        secret = "sk-proj-" + "Q" * 40
        events = self.root / "secret-resolution-events.jsonl"

        result = self.run_cli(
            "capture-resolve",
            secret,
            "true",
            "--log",
            str(self.log),
            "--events",
            str(events),
            "--evidence",
            "deterministic fixture",
            "--resolver",
            "test-harness",
            "--method",
            "deterministic",
            "--coordination-lock",
            str(self.root / "secret-resolution.lock"),
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret, result.stderr)
        self.assertIn("secret preflight", result.stderr)
        self.assertFalse(events.exists())

    def test_capture_resolve_canonical_json_aws_secret_is_nonreflective(self):
        secret_value = "B" * 40
        evidence = json.dumps(
            {"AWS_SECRET_ACCESS_KEY": secret_value},
            sort_keys=True,
            separators=(",", ":"),
        )
        events = self.root / "aws-secret-resolution-events.jsonl"
        coordination = self.root / "aws-secret-resolution.lock"

        result = self.run_cli(
            "capture-resolve",
            "outcome-" + "a" * 32,
            "true",
            "--log",
            str(self.log),
            "--events",
            str(events),
            "--evidence",
            evidence,
            "--resolver",
            "test-harness",
            "--method",
            "deterministic",
            "--coordination-lock",
            str(coordination),
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret_value, result.stderr)
        self.assertIn("secret preflight", result.stderr)
        self.assertIn("aws-secret-assignment", result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse(events.exists())
        self.assertFalse(coordination.exists())

    def test_outside_hardlink_to_live_ledger_fails_before_append(self):
        fake_live_root = self.root / "fake-live"
        fake_live_root.mkdir()
        fake_live_ledger = fake_live_root / "panel.jsonl"
        fake_live_ledger.write_bytes(b"")
        os.link(fake_live_ledger, self.log)
        attempt_spec = self.root / "attempt.json"
        attempt_spec.write_text(
            json.dumps(
                {
                    "question": "Reject a live-ledger hardlink?",
                    "expectedSeats": ["code"],
                    "sharedOutcome": {
                        "claim": "The outside hardlink is not appended",
                        "resolutionDate": "2026-09-30",
                        "resolvedBy": "Inspect the simulated live ledger",
                        "decisionLink": "Ledger identity safety",
                        "materiality": "A hardlink bypass mutates live evidence",
                        "actionIfTrue": "Continue local testing",
                        "actionIfFalse": "Repair identity checks",
                        "evidenceCutoffAt": "2026-08-22T12:00:00Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        args = SimpleNamespace(
            log=str(self.log),
            spec=str(attempt_spec),
            ts="2026-08-22T12:00:00Z",
            coordination_lock=str(self.root / "coordination.lock"),
        )
        with (
            mock.patch.object(cli, "LIVE_WRITE_ROOTS", (fake_live_root.resolve(),)),
            mock.patch.object(cli.socket, "gethostname", return_value="not-manny"),
        ):
            with self.assertRaisesRegex(cli.LedgerError, "hardlink alias"):
                cli.command_attempt(args)
        self.assertEqual(fake_live_ledger.read_bytes(), b"")
        self.assertEqual(self.log.read_bytes(), b"")

    def test_activation_parent_swap_to_live_symlink_fails_at_mutation_boundary(self):
        fake_live_root = self.root / "fake-live"
        fake_live_root.mkdir()
        store_parent = self.root / "activation-store"
        store_parent.mkdir()
        displaced_parent = self.root / "activation-store-before-swap"
        log = store_parent / "capture.jsonl"
        activation_spec = self.root / "activation.json"
        activation_spec.write_text(
            json.dumps(
                {
                    "cohortName": "parent-swap-test",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": "a" * 40,
                    "runtimeSourceSha256": "b" * 64,
                    "artifactRootPolicy": "private-content-addressed-v1",
                }
            ),
            encoding="utf-8",
        )
        args = SimpleNamespace(
            log=str(log),
            spec=str(activation_spec),
            approval_manifest_file=None,
            artifact_root=None,
            coordination_lock=str(self.root / "coordination.lock"),
        )

        def swap_then_mutate(path, _spec, *, coordination_lock):
            self.assertEqual(coordination_lock, args.coordination_lock)
            store_parent.rename(displaced_parent)
            store_parent.symlink_to(fake_live_root, target_is_directory=True)
            safe_files.atomic_append_bytes(
                path,
                b'{}\n',
                require_trailing_newline=True,
            )

        with (
            mock.patch.object(
                cli,
                "LIVE_WRITE_ROOTS",
                (*cli.LIVE_WRITE_ROOTS, fake_live_root.resolve()),
            ),
            mock.patch.object(cli.socket, "gethostname", return_value="not-manny"),
            mock.patch.object(
                cli,
                "append_capture_activation",
                side_effect=swap_then_mutate,
            ),
        ):
            with self.assertRaisesRegex(
                safe_files.SafeFileError, "unsafe parent component"
            ):
                cli.command_capture_activate(args)
        self.assertFalse((fake_live_root / "capture.jsonl").exists())

    def test_cli_activation_rejects_plain_live_parent_substitution_before_derived_lock(self):
        fake_live_root = self.root / "fake-live-plain"
        fake_live_root.mkdir()
        store_parent = self.root / "plain-activation-store"
        store_parent.mkdir()
        displaced_parent = self.root / "plain-activation-store-before-swap"
        log = store_parent / "capture.jsonl"
        activation_spec = self.root / "plain-activation.json"
        activation_spec.write_text(
            json.dumps(
                {
                    "cohortName": "plain-parent-swap-test",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": "a" * 40,
                    "runtimeSourceSha256": "b" * 64,
                    "artifactRootPolicy": "private-content-addressed-v1",
                }
            ),
            encoding="utf-8",
        )
        coordination_lock = self.root / "plain-coordination.lock"
        args = SimpleNamespace(
            log=str(log),
            spec=str(activation_spec),
            approval_manifest_file=None,
            artifact_root=None,
            coordination_lock=str(coordination_lock),
        )
        real_classifier = capture_runtime._is_live_activation_target

        def classify_then_swap(path, roots):
            classified = real_classifier(path, roots)
            self.assertFalse(classified)
            store_parent.rename(displaced_parent)
            fake_live_root.rename(store_parent)
            return classified

        with (
            mock.patch.object(
                capture_runtime,
                "_os_account_live_write_roots",
                return_value=(fake_live_root.resolve(),),
            ),
            mock.patch.object(
                capture_runtime,
                "_is_live_activation_target",
                side_effect=classify_then_swap,
            ),
        ):
            with self.assertRaisesRegex(
                capture_runtime.CaptureRuntimeError,
                "direct live capture activation is blocked",
            ):
                cli.command_capture_activate(args)

        self.assertFalse(log.exists())
        self.assertFalse(log.with_name("capture.jsonl.lock").exists())
        self.assertEqual(list(displaced_parent.iterdir()), [])

    def test_omitted_coordination_lock_is_local_for_standalone_store(self):
        attempt_spec = self.root / "attempt.json"
        attempt_spec.write_text(
            json.dumps(
                {
                    "question": "Use a standalone local store?",
                    "expectedSeats": ["code"],
                    "sharedOutcome": {
                        "claim": "The local invocation avoids live runtime state",
                        "resolutionDate": "2026-09-30",
                        "resolvedBy": "Inspect local filesystem paths",
                        "decisionLink": "Off-host rehearsal",
                        "materiality": "Touching the live lock defeats isolation",
                        "actionIfTrue": "Continue rehearsal",
                        "actionIfFalse": "Repair lock derivation",
                        "evidenceCutoffAt": "2026-08-22T12:00:00Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        args = cli.build_parser().parse_args(
            ["attempt", "--log", str(self.log), "--spec", str(attempt_spec)]
        )
        self.assertIsNone(args.coordination_lock)
        cli._resolve_coordination_lock(args)
        expected = self.log.with_name(f"{self.log.name}.evidence.lock")
        self.assertEqual(Path(args.coordination_lock), expected)
        with mock.patch.object(cli.socket, "gethostname", return_value="not-manny"):
            self.assertEqual(cli.command_attempt(args), 0)
        self.assertTrue(expected.is_file())
        self.assertNotEqual(
            Path(cli.DEFAULT_COORDINATION_LOCK).resolve(strict=False),
            expected.resolve(strict=False),
        )

    def test_default_live_store_retains_live_coordination_lock(self):
        args = cli.build_parser().parse_args(
            ["attempt", "--spec", str(self.root / "attempt.json")]
        )
        cli._resolve_coordination_lock(args)
        self.assertEqual(args.coordination_lock, cli.DEFAULT_COORDINATION_LOCK)

    def test_local_resolution_uses_the_ledger_coordination_lock(self):
        args = cli.build_parser().parse_args(
            [
                "resolve",
                "outcome-" + "a" * 32,
                "true",
                "--log",
                str(self.log),
                "--events",
                str(self.events),
                "--evidence",
                "local evidence",
                "--resolver",
                "operator",
                "--method",
                "deterministic",
            ]
        )
        cli._resolve_coordination_lock(args)
        self.assertEqual(
            Path(args.coordination_lock),
            self.log.with_name(f"{self.log.name}.evidence.lock"),
        )

    def test_complete_check_only_neither_authorizes_nor_binds_a_lock(self):
        seeded = make_attempt(
            question="Validate a completion without writing?",
            expected_seats=["code"],
            claim="Check-only makes no filesystem mutation",
            resolution_date="2026-09-30",
            resolved_by="Inspect the local ledger",
            decision_link="Completion validation",
            materiality="Validation must be safe off-host",
            action_if_true="Append separately",
            action_if_false="Repair the completion",
            evidence_cutoff_at="2026-08-22T12:00:00Z",
            ts="2026-08-22T12:00:00Z",
        )
        append_ledger_row(self.log, seeded)
        complete_spec = self.root / "complete.json"
        complete_spec.write_text(
            json.dumps(
                {
                    "runId": seeded["runId"],
                    "councilFields": {
                        "verdicts": {"code": "APPROVE"},
                        "blindSeat": {
                            "role": "SKIPPED",
                            "required": False,
                            "ran": False,
                            "changedDecision": None,
                            "brief": f"/tmp/council-briefs/brief-{seeded['runId']}.md",
                        },
                    },
                    "seatStates": {"code": "submitted"},
                    "probabilities": {"code": 50},
                }
            ),
            encoding="utf-8",
        )
        args = cli.build_parser().parse_args(
            [
                "complete",
                "--log",
                str(self.log),
                "--spec",
                str(complete_spec),
                "--check-only",
            ]
        )
        cli._resolve_coordination_lock(args)
        self.assertIsNone(args.coordination_lock)
        with mock.patch.object(cli, "_require_ledger_write_authority") as authority:
            self.assertEqual(cli.command_complete(args), 0)
        authority.assert_not_called()

    def test_supersede_and_the_installed_reader_agree_on_a_retired_row(self):
        """Append a supersede through the CLI, then read it with the shipped reader.

        The reader is the deployed kill criterion with the install transform applied,
        which is the copy this change ships.  Testing against the pre-activation file
        on disk would only prove the old reader ignores the record; the seam that can
        strand a line is the appender against the reader it ships with.
        """

        criterion = self.installed_kill_criterion()
        seeded = make_attempt(
            question="Does a superseded duplicate leave the denominator alone?",
            expected_seats=["code"],
            claim="The kill criterion stops counting a retired duplicate",
            resolution_date="2026-09-30",
            resolved_by="Run the blind-seat kill criterion",
            decision_link="Supersede appender and reader seam",
            materiality="A double-counted council inflates the kill-criterion denominator",
            action_if_true="Keep the supersede record",
            action_if_false="Stop and repair the reader seam",
            evidence_cutoff_at="2026-07-01T00:00:00Z",
            ts="2026-07-01T00:00:00Z",
        )
        append_ledger_row(self.log, seeded)
        original = completion(seeded)
        original["blindSeat"]["ran"] = True
        original["blindSeat"]["role"] = "allocator"
        original["blindSeat"]["required"] = True
        original["blindSeat"]["changedDecision"] = True
        original["blindSeat"]["blockedReason"] = None
        original["blindSeat"].pop("notRequiredReason", None)
        append_ledger_row(self.log, original)
        duplicate = {
            "kind": "council",
            "runId": seeded["runId"],
            "ts": "2026-07-01T00:10:17Z",
            "question": seeded["question"],
            "verdicts": {"code": "APPROVE"},
            "blindSeat": dict(original["blindSeat"]),
        }
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(duplicate, sort_keys=True, separators=(",", ":")) + "\n")
        raw_lines = self.log.read_bytes().splitlines(keepends=True)
        duplicate_line = len(raw_lines)
        digest = hashlib.sha256(raw_lines[-1]).hexdigest()

        before = self.read_criterion(criterion)
        self.assertEqual(before["completedRuns"], 2)
        self.assertEqual(before["changedDecisionRuns"], 2)
        self.assertEqual(len(before["errors"]), 1)

        refused = self.run_cli(
            "supersede",
            "--log",
            str(self.log),
            "--line",
            str(duplicate_line - 1),
            "--confirm-raw-line-sha256",
            hashlib.sha256(raw_lines[-2]).hexdigest(),
            "--duplicate-of-line",
            str(duplicate_line),
            "--confirm-duplicate-of-raw-line-sha256",
            digest,
            "--reason",
            "Superseding the original is the dangerous direction",
            "--operator",
            "operator",
            "--reference",
            "https://github.com/garcia42/ai-council/issues/25",
            "--check-only",
        )
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("forecastState", refused.stderr)

        recorded = self.run_cli(
            "supersede",
            "--log",
            str(self.log),
            "--line",
            str(duplicate_line),
            "--confirm-raw-line-sha256",
            digest,
            "--duplicate-of-line",
            str(duplicate_line - 1),
            "--confirm-duplicate-of-raw-line-sha256",
            hashlib.sha256(raw_lines[-2]).hexdigest(),
            "--reason",
            "Hand-appended duplicate of the preceding council row",
            "--operator",
            "operator",
            "--reference",
            "https://github.com/garcia42/ai-council/issues/25",
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        recorded_payload = json.loads(recorded.stdout)
        self.assertEqual(recorded_payload["supersedes"]["line"], duplicate_line)
        self.assertEqual(
            recorded_payload["duplicateOf"]["line"], duplicate_line - 1
        )

        after = self.read_criterion(criterion)
        self.assertEqual(after["errors"], [])
        self.assertEqual(after["supersededRows"], 1)
        self.assertEqual(after["completedRuns"], 1)
        self.assertEqual(after["changedDecisionRuns"], 1)
        self.assertEqual(after["nonCouncilRecords"], before["nonCouncilRecords"] + 1)
        self.assertEqual(after["legacyBlindRows"], before["legacyBlindRows"])

    def test_installed_reader_refuses_adversarial_supersedes_without_retirement(self):
        criterion = self.installed_kill_criterion()
        seat = {
            "role": "generic",
            "required": True,
            "ran": True,
            "changedDecision": True,
            "agreedWithPanel": True,
            "blockedReason": None,
        }
        original = {
            "kind": "council",
            "runId": "run-reader-fail-closed",
            "blindSeat": {**seat, "brief": "/tmp/reader-fail-closed.md"},
            "forecastState": {"sealed": True, "seats": {}},
            "predictions": [],
        }
        duplicate = {
            "kind": "council",
            "runId": "run-reader-fail-closed",
            "blindSeat": {**seat, "brief": "/tmp/reader-fail-closed.md"},
        }

        def raw_line(row):
            return (
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")

        original_raw = raw_line(original)
        duplicate_raw = raw_line(duplicate)
        valid_record = {
            "schemaVersion": 1,
            "kind": "council-superseded",
            "ts": "2026-08-24T00:00:00Z",
            "supersedes": {
                "line": 2,
                "rawLineSha256": hashlib.sha256(duplicate_raw).hexdigest(),
            },
            "duplicateOf": {
                "line": 1,
                "rawLineSha256": hashlib.sha256(original_raw).hexdigest(),
            },
            "reason": "hand-appended duplicate",
            "approval": {
                "operator": "operator",
                "approvedAt": "2026-08-24T00:00:00Z",
                "reference": "https://github.com/garcia42/ai-council/issues/25",
            },
        }
        unattributed = {
            "kind": "council-superseded",
            "supersedes": valid_record["supersedes"],
            "duplicateOf": valid_record["duplicateOf"],
        }
        bogus_target = json.loads(json.dumps(valid_record))
        bogus_target["supersedes"]["rawLineSha256"] = "f" * 64
        bogus_retained = json.loads(json.dumps(valid_record))
        bogus_retained["duplicateOf"]["rawLineSha256"] = "f" * 64
        adversarial = [
            ("unattributed", unattributed, "unexpected shape"),
            ("bogus target digest", bogus_target, "does not match line"),
            ("bogus retained digest", bogus_retained, "does not match line"),
        ]
        for case in SUPERSEDE_ADVERSARIAL_PARITY_CASES:
            record = json.loads(json.dumps(valid_record))
            apply_supersede_adversarial_case(record, case)
            expected_error = (
                "schemaVersion" if "schemaVersion" in case[0] else "rawLineSha256"
            )
            adversarial.append((case[0], record, expected_error))

        for name, record, expected_error in adversarial:
            with self.subTest(record=name):
                self.log.write_bytes(original_raw + duplicate_raw + raw_line(record))
                result = self.read_criterion(criterion)
                self.assertEqual(result["supersededRows"], 0)
                self.assertEqual(result["completedRuns"], 2)
                self.assertEqual(result["changedDecisionRuns"], 2)
                self.assertTrue(
                    any(expected_error in error for error in result["errors"]),
                    result["errors"],
                )

    def test_installed_reader_rejects_non_strict_json_before_tally(self):
        criterion = self.installed_kill_criterion()
        prefix = b'{"kind":"council","blindSeat":{"ran":false}}\n'

        for name, payload, expected_error in STRICT_JSON_ADVERSARIAL_PARITY_CASES:
            with self.subTest(case=name):
                ledger = prefix + payload
                self.log.write_bytes(ledger)
                result = subprocess.run(
                    [sys.executable, str(criterion), "--log", str(self.log), "--json"],
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip(), f"line 2: {expected_error}")
                self.assertEqual(self.log.read_bytes(), ledger)

    def installed_kill_criterion(self):
        """Render the deployed kill criterion the way ``install.py`` would render it."""

        deployed = Path(
            "/home/trader/.claude/knowledge/council-eval/blind_seat_kill_criterion.py"
        )
        if not deployed.is_file():
            self.skipTest("deployed blind-seat kill criterion is not present")
        rendered = self.root / "blind_seat_kill_criterion.py"
        rendered.write_text(
            install._with_superseded_reader(
                install._with_attempt_allowlist(
                    deployed.read_text(encoding="utf-8")
                )
            ),
            encoding="utf-8",
        )
        return rendered

    def read_criterion(self, criterion):
        result = subprocess.run(
            [sys.executable, str(criterion), "--log", str(self.log), "--json"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return json.loads(result.stdout)

    def test_live_write_authority_ignores_caller_environment_override(self):
        with (
            mock.patch.object(cli.socket, "gethostname", return_value="attacker-host"),
            mock.patch.dict(
                os.environ,
                {"COUNCIL_LEDGER_AUTHORITY_HOST": "attacker-host"},
            ),
        ):
            with self.assertRaisesRegex(cli.LedgerError, "authorized only on manny"):
                cli._require_write_authority(cli.DEFAULT_LOG)

    def test_live_paths_use_os_account_home_not_caller_home_environment(self):
        env = dict(os.environ)
        env["HOME"] = str(self.root / "forged-home")
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os,pwd; from pathlib import Path; "
                    "from council_tools import cli; "
                    "home=Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(); "
                    "assert cli.ACCOUNT_HOME == home; "
                    "assert Path(cli.DEFAULT_LOG).is_relative_to(home); "
                    "assert str(cli.DEFAULT_LOG).startswith(str(home / '.claude'))"
                ),
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_authority_covers_custom_live_subpaths_and_symlink_aliases(self):
        custom_paths = (
            cli.LIVE_KNOWLEDGE_ROOT / "custom/capture-artifacts",
            cli.LIVE_RUNTIME_STATE_ROOT / "custom/capture-artifacts",
            cli.LIVE_RUNTIME_SOURCE_ROOT / "custom/capture-artifacts",
        )
        outside_alias = self.root / "live-alias"
        outside_alias.symlink_to(cli.LIVE_RUNTIME_STATE_ROOT, target_is_directory=True)
        with mock.patch.object(cli.socket, "gethostname", return_value="not-manny"):
            for path in (*custom_paths, outside_alias / "nested"):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(
                        cli.LedgerError, "authorized only on manny"
                    ):
                        cli._require_write_authority(path)

    def test_non_live_temp_write_paths_do_not_require_authority(self):
        snapshot_args = SimpleNamespace(
            log=str(self.log),
            events=str(self.events),
            control_store=str(self.root / "control"),
            artifact_root=str(self.root / "artifacts"),
            coordination_lock=str(self.root / "coordination.lock"),
            target=str(self.root / "snapshot"),
            repository_root=str(self.root / "repository"),
        )
        restore_args = SimpleNamespace(
            snapshot=str(self.root / "snapshot"),
            target=str(self.root / "restore"),
            repository_root=str(self.root / "repository"),
        )
        with (
            mock.patch.object(cli.socket, "gethostname", return_value="not-manny"),
            mock.patch.object(
                cli, "create_evidence_snapshot", return_value={"status": "created"}
            ) as snapshot,
            mock.patch.object(
                cli, "restore_evidence_snapshot", return_value={"status": "restored"}
            ) as restore,
        ):
            cli._require_write_authority(
                self.root / "artifacts",
                self.root / "snapshot",
                self.root / "restore",
                self.root / "coordination.lock",
            )
            self.assertEqual(cli.command_evidence_snapshot(snapshot_args), 0)
            self.assertEqual(cli.command_evidence_restore(restore_args), 0)
        snapshot.assert_called_once()
        restore.assert_called_once()

    def test_snapshot_and_restore_live_targets_require_authority(self):
        snapshot_args = SimpleNamespace(
            log=str(self.log),
            events=str(self.events),
            control_store=str(self.root / "control"),
            artifact_root=str(self.root / "artifacts"),
            coordination_lock=str(self.root / "coordination.lock"),
            target=str(cli.LIVE_RUNTIME_STATE_ROOT / "custom-snapshot"),
            repository_root=str(self.root / "repository"),
        )
        restore_args = SimpleNamespace(
            snapshot=str(self.root / "snapshot"),
            target=str(cli.LIVE_KNOWLEDGE_ROOT / "custom-restore"),
            repository_root=str(self.root / "repository"),
        )
        with (
            mock.patch.object(cli.socket, "gethostname", return_value="not-manny"),
            mock.patch.object(cli, "create_evidence_snapshot") as snapshot,
            mock.patch.object(cli, "restore_evidence_snapshot") as restore,
        ):
            with self.assertRaisesRegex(cli.LedgerError, "authorized only on manny"):
                cli.command_evidence_snapshot(snapshot_args)
            with self.assertRaisesRegex(cli.LedgerError, "authorized only on manny"):
                cli.command_evidence_restore(restore_args)
        snapshot.assert_not_called()
        restore.assert_not_called()

    def test_custom_live_artifact_subpath_requires_authority(self):
        args = SimpleNamespace(
            run_id=None,
            log=None,
            operator=None,
            evidence_ref=None,
            control_artifact=True,
            artifact_root=str(cli.LIVE_RUNTIME_STATE_ROOT / "custom-artifacts"),
            coordination_lock=str(self.root / "coordination.lock"),
            file=str(self.root / "input.txt"),
            secret_token_file=[],
        )
        with (
            mock.patch.object(cli.socket, "gethostname", return_value="not-manny"),
            mock.patch.object(cli, "ArtifactStore") as store,
        ):
            with self.assertRaisesRegex(cli.LedgerError, "authorized only on manny"):
                cli.command_capture_artifact(args)
        store.assert_not_called()

    def test_live_report_refuses_injected_test_clock(self):
        for log, events in (
            (cli.DEFAULT_LOG, cli.DEFAULT_EVENTS),
            (
                str(cli.LIVE_RUNTIME_STATE_ROOT / "custom-log.jsonl"),
                str(cli.LIVE_RUNTIME_STATE_ROOT / "custom-events.jsonl"),
            ),
        ):
            with self.subTest(log=log):
                args = mock.Mock(
                    today="2026-01-01",
                    log=log,
                    events=events,
                    json=True,
                )
                with self.assertRaisesRegex(cli.LedgerError, "test-only"):
                    cli.command_report(args)

        capture_args = mock.Mock(
            as_of="2026-01-01T00:00:00Z",
            log=str(cli.LIVE_RUNTIME_STATE_ROOT / "capture-log.jsonl"),
            events=str(cli.LIVE_RUNTIME_STATE_ROOT / "capture-events.jsonl"),
            artifact_root=str(self.root / "artifacts"),
            json=True,
        )
        with self.assertRaisesRegex(cli.LedgerError, "test-only"):
            cli.command_capture_report(capture_args)

    def test_resolution_cli_rejects_operator_timestamp_and_uses_system_clock(self):
        outcome_id = new_id("outcome")
        rejected = self.run_cli(
            "resolve",
            outcome_id,
            "true",
            "--log",
            str(self.log),
            "--events",
            str(self.events),
            "--evidence",
            "test evidence",
            "--resolver",
            "operator",
            "--method",
            "deterministic",
            "--resolved-at",
            "2099-01-01T00:00:00Z",
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("unrecognized arguments: --resolved-at", rejected.stderr)

        args = SimpleNamespace(
            events=str(self.events),
            log=str(self.log),
            coordination_lock=str(self.root / "coordination.lock"),
            outcome_id=outcome_id,
            outcome="true",
            void_reason=None,
            evidence="test evidence",
            resolver="operator",
            method="deterministic",
            reviewer=None,
            supersedes=None,
        )
        fingerprint = "a" * 64
        report = {
            "invalidRecords": [],
            "knownOutcomeIds": [outcome_id],
            "outcomeResolutionDates": {outcome_id: "2026-09-30"},
            "outcomeFingerprints": {outcome_id: fingerprint},
        }
        with (
            mock.patch.object(cli, "audit", return_value=report),
            mock.patch.object(cli, "_now", return_value="2026-10-01T12:00:00Z"),
            mock.patch.object(
                cli,
                "append_resolution",
                return_value={"resolutionId": new_id("resolution")},
            ) as append,
        ):
            self.assertEqual(cli.command_resolve(args), 0)
        self.assertEqual(append.call_args.kwargs["resolved_at"], "2026-10-01T12:00:00Z")
        self.assertEqual(append.call_args.kwargs["outcome_fingerprint"], fingerprint)

    def test_strict_spec_duplicate_key_error_never_echoes_secret_shaped_key(self):
        secret_key = "ghp_" + "x" * 40
        spec = self.root / "secret-duplicate.json"
        spec.write_text(
            '{"question":"safe","'
            + secret_key
            + '":1,"'
            + secret_key
            + '":2}',
            encoding="utf-8",
        )

        result = self.run_cli(
            "attempt",
            "--log",
            str(self.log),
            "--spec",
            str(spec),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("attempt spec is not valid strict JSON", result.stderr)
        self.assertNotIn(secret_key, result.stderr)

    def test_strict_spec_excessive_nesting_is_generic_without_traceback(self):
        spec = self.root / "deeply-nested.json"
        spec.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000)

        result = self.run_cli(
            "attempt",
            "--log",
            str(self.log),
            "--spec",
            str(spec),
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "attempt spec is not valid strict JSON")
        self.assertNotIn("recursion", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)

    def test_v1_report_jsonl_parse_error_keeps_context_without_secret_key(self):
        secret_key = "ghp_" + "v" * 40
        self.log.write_text(
            '{"' + secret_key + '":1,"' + secret_key + '":2}\n',
            encoding="utf-8",
        )

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

        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{self.log.name} line 1: invalid JSON", result.stderr)
        self.assertNotIn(secret_key, result.stderr)

    def test_v1_report_excessive_json_nesting_is_one_generic_line_error(self):
        self.log.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000 + b"\n")

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

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr.strip(), f"{self.log.name} line 1: invalid JSON"
        )
        self.assertNotIn("recursion", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)

    def test_v2_report_jsonl_parse_error_keeps_context_without_secret_key(self):
        secret_key = "caller_controlled_duplicate_key_" + "w" * 40
        self.log.write_text(
            '{"' + secret_key + '":1,"' + secret_key + '":2}\n',
            encoding="utf-8",
        )

        result = self.run_cli(
            "capture-report",
            "--log",
            str(self.log),
            "--events",
            str(self.root / "capture-events.jsonl"),
            "--artifact-root",
            str(self.root / "artifacts"),
            "--as-of",
            "2026-08-22T12:00:00Z",
            "--json",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{self.log.name} line 1: invalid JSON", result.stderr)
        self.assertNotIn(secret_key, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_capture_report_raw_secret_v2_line_is_nonreflective(self):
        coordination = self.root / "raw-secret-report.lock"
        capture_runtime.append_capture_activation(
            self.log,
            {
                "cohortName": "raw-secret-report",
                "captureVersion": "capture-v2.0.0",
                "runtimeSourceCommit": "a" * 40,
                "runtimeSourceSha256": "b" * 64,
                "artifactRootPolicy": "private-content-addressed-v1",
            },
            clock=lambda: "2026-08-23T10:00:00Z",
            coordination_lock=coordination,
        )
        secret = "sk-proj-" + "V" * 40
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "kind": "council-v2",
                        "runId": secret,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

        result = self.run_cli(
            "capture-report",
            "--log",
            str(self.log),
            "--events",
            str(self.root / "raw-secret-events.jsonl"),
            "--artifact-root",
            str(self.root / "raw-secret-artifacts"),
            "--as-of",
            "2026-08-27T00:00:00Z",
            "--json",
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret, result.stderr)
        self.assertIn("secret preflight", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_capture_report_raw_secret_sidecar_kind_is_nonreflective(self):
        coordination = self.root / "raw-secret-sidecar-report.lock"
        capture_runtime.append_capture_activation(
            self.log,
            {
                "cohortName": "raw-secret-sidecar-report",
                "captureVersion": "capture-v2.0.0",
                "runtimeSourceCommit": "a" * 40,
                "runtimeSourceSha256": "b" * 64,
                "artifactRootPolicy": "private-content-addressed-v1",
            },
            clock=lambda: "2026-08-23T10:00:00Z",
            coordination_lock=coordination,
        )
        secret = "sk-proj-" + "Z" * 40
        events = self.root / "raw-secret-sidecar-events.jsonl"
        events.write_text(
            json.dumps({"kind": secret}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        ledger_before = self.log.read_bytes()
        events_before = events.read_bytes()

        result = self.run_cli(
            "capture-report",
            "--log",
            str(self.log),
            "--events",
            str(events),
            "--artifact-root",
            str(self.root / "raw-secret-sidecar-artifacts"),
            "--as-of",
            "2026-08-27T00:00:00Z",
            "--json",
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret, result.stderr)
        self.assertIn("secret preflight", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.log.read_bytes(), ledger_before)
        self.assertEqual(events.read_bytes(), events_before)

    def test_capture_report_json_escaped_sidecar_fields_are_nonreflective(self):
        coordination = self.root / "escaped-secret-sidecar-report.lock"
        capture_runtime.append_capture_activation(
            self.log,
            {
                "cohortName": "escaped-secret-sidecar-report",
                "captureVersion": "capture-v2.0.0",
                "runtimeSourceCommit": "a" * 40,
                "runtimeSourceSha256": "b" * 64,
                "artifactRootPolicy": "private-content-addressed-v1",
            },
            clock=lambda: "2026-08-23T10:00:00Z",
            coordination_lock=coordination,
        )
        secret = "sk-proj-" + "J" * 40
        escaped = "sk\\u002dproj\\u002d" + "J" * 40
        cases = {
            "kind": '{"kind":"' + escaped + '"}\n',
            "key": (
                '{"kind":"outcome-resolution","'
                + escaped
                + '":"safe"}\n'
            ),
            "identifier": (
                '{"kind":"outcome-resolution","outcomeId":"'
                + escaped
                + '"}\n'
            ),
            "value": (
                '{"kind":"outcome-resolution","evidence":"'
                + escaped
                + '"}\n'
            ),
        }
        events = self.root / "escaped-secret-sidecar-events.jsonl"
        artifact_root = self.root / "escaped-secret-sidecar-artifacts"
        ledger_before = self.log.read_bytes()
        for field, encoded in cases.items():
            with self.subTest(field=field):
                self.assertNotIn(secret.encode(), encoded.encode())
                events.write_bytes(encoded.encode())
                events_before = events.read_bytes()

                result = self.run_cli(
                    "capture-report",
                    "--log",
                    str(self.log),
                    "--events",
                    str(events),
                    "--artifact-root",
                    str(artifact_root),
                    "--as-of",
                    "2026-08-27T00:00:00Z",
                    "--json",
                )

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(secret, result.stderr)
                self.assertIn("secret preflight", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.log.read_bytes(), ledger_before)
                self.assertEqual(events.read_bytes(), events_before)
                self.assertFalse(artifact_root.exists())

    def test_capture_report_unhashable_enums_are_invalid_v2_without_traceback(self):
        coordination = self.root / "coordination.lock"
        capture_runtime.append_capture_activation(
            self.log,
            {
                "cohortName": "typed-enum-report",
                "captureVersion": "capture-v2.0.0",
                "runtimeSourceCommit": "a" * 40,
                "runtimeSourceSha256": "b" * 64,
                "artifactRootPolicy": "private-content-addressed-v1",
            },
            clock=lambda: "2026-08-23T10:00:00Z",
            coordination_lock=coordination,
        )
        malformed = (
            {
                "schemaVersion": [],
                "kind": "council-attempt-v2",
                "runId": "run-" + "f" * 32,
            },
            {
                "schemaVersion": 2,
                "kind": ["council-attempt-v2"],
                "runId": "run-" + "e" * 32,
            },
        )
        with self.log.open("a", encoding="utf-8") as handle:
            for row in malformed:
                handle.write(json.dumps(row, sort_keys=True) + "\n")

        result = self.run_cli(
            "capture-report",
            "--log",
            str(self.log),
            "--events",
            str(self.root / "capture-events.jsonl"),
            "--artifact-root",
            str(self.root / "artifacts"),
            "--as-of",
            "2026-08-23T12:00:00Z",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["ledger"]["invalidV2RecordCount"], 2)
        self.assertEqual(
            [item["error"] for item in payload["ledger"]["invalidV2Records"]],
            ["invalid V2 record field type"] * 2,
        )
        self.assertEqual(
            [item["kind"] for item in payload["ledger"]["invalidV2Records"]],
            ["council-attempt-v2", "invalid-v2-record"],
        )

    def test_record_check_only_strict_duplicate_question_is_generic_and_nonleaking(self):
        secret_value = "ghp_" + "q" * 40
        row = self.root / "duplicate-question.json"
        row.write_text(
            '{"question":"' + secret_value + '","question":"safe"}',
            encoding="utf-8",
        )

        result = self.run_cli(
            "record",
            "--log",
            str(self.log),
            "--row",
            str(row),
            "--check-only",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("record is not valid strict JSON", result.stderr)
        self.assertNotIn(secret_value, result.stderr)

    def test_capture_cli_owns_boundaries_and_serializes_incomplete_durations(self):
        activation_spec = self.root / "activation.json"
        activation_spec.write_text(
            json.dumps(
                {
                    "cohortName": "first-ten-cli",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": "a" * 40,
                    "runtimeSourceSha256": "b" * 64,
                    "artifactRootPolicy": "private-content-addressed-v1",
                }
            ),
            encoding="utf-8",
        )
        coordination = self.root / "evidence.lock"
        activated = self.run_cli(
            "capture-activate",
            "--log",
            str(self.log),
            "--spec",
            str(activation_spec),
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        activated_payload = json.loads(activated.stdout)
        activation_id = activated_payload["activationId"]
        self.assertEqual(activated_payload["transactionEscrows"], [])
        initiated = self.run_cli(
            "capture-initiate",
            "--log",
            str(self.log),
            "--activation-id",
            activation_id,
            "--idempotency-key",
            "cli-incomplete-run",
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(initiated.returncode, 0, initiated.stderr)
        initiated_payload = json.loads(initiated.stdout)
        self.assertEqual(len(initiated_payload["transactionEscrows"]), 1)
        self.assertTrue(
            Path(initiated_payload["transactionEscrows"][0]).is_file()
        )

        injected = self.run_cli(
            "capture-initiate",
            "--log",
            str(self.log),
            "--activation-id",
            activation_id,
            "--idempotency-key",
            "forbidden-clock",
            "--ts",
            "2026-08-23T00:00:00Z",
        )
        self.assertEqual(injected.returncode, 2)
        self.assertIn("unrecognized arguments: --ts", injected.stderr)

        report = self.run_cli(
            "capture-report",
            "--log",
            str(self.log),
            "--events",
            str(self.events),
            "--artifact-root",
            str(self.root / "artifacts"),
            "--as-of",
            "2026-09-01T00:00:00Z",
            "--json",
        )
        self.assertEqual(report.returncode, 0, report.stderr)
        payload = json.loads(report.stdout)
        self.assertEqual(payload["cohort"]["eligibleInitiationCount"], 1)
        self.assertIsNone(payload["timing"]["activeHandlingSeconds"][0])
        self.assertEqual(payload["transactionEscrows"]["count"], 1)
        self.assertEqual(
            payload["transactionEscrows"]["entries"][0]["path"],
            initiated_payload["transactionEscrows"][0],
        )

        human = self.run_cli(
            "capture-report",
            "--log",
            str(self.log),
            "--events",
            str(self.events),
            "--artifact-root",
            str(self.root / "artifacts"),
            "--as-of",
            "2026-09-01T00:00:00Z",
        )
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("activation=BLOCKED", human.stdout)
        self.assertIn("prospective-audit-not-implemented", human.stdout)
        self.assertIn("durability-evidence-not-supplied", human.stdout)
        self.assertIn("transaction_escrows=1", human.stdout)

        aliased = self.run_cli(
            "capture-report",
            "--log",
            str(self.log),
            "--events",
            str(self.log),
            "--artifact-root",
            str(self.root / "artifacts"),
        )
        self.assertEqual(aliased.returncode, 1)
        self.assertIn("must be separate files", aliased.stderr)

    def test_capture_artifact_requires_incident_context_or_control_mode(self):
        source = self.root / "artifact.txt"
        source.write_text("review input", encoding="utf-8")
        artifact_root = self.root / "artifacts"
        coordination = self.root / "evidence.lock"

        ambiguous = self.run_cli(
            "capture-artifact",
            "--file",
            str(source),
            "--artifact-root",
            str(artifact_root),
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(ambiguous.returncode, 1)
        self.assertIn("or explicit --control-artifact", ambiguous.stderr)
        self.assertFalse(artifact_root.exists())

        control = self.run_cli(
            "capture-artifact",
            "--file",
            str(source),
            "--artifact-root",
            str(artifact_root),
            "--control-artifact",
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertEqual(json.loads(control.stdout)["bytes"], len(b"review input"))

        mixed = self.run_cli(
            "capture-artifact",
            "--file",
            str(source),
            "--artifact-root",
            str(artifact_root),
            "--control-artifact",
            "--run-id",
            new_id("run"),
        )
        self.assertEqual(mixed.returncode, 1)
        self.assertIn("cannot be combined", mixed.stderr)

    def test_run_artifact_requires_a_strict_ledger_and_one_prior_initiation(self):
        source = self.root / "artifact.txt"
        source.write_text("review input", encoding="utf-8")
        artifact_root = self.root / "artifacts"
        coordination = self.root / "evidence.lock"
        run_id = new_id("run")

        missing = self.run_cli(
            "capture-artifact",
            "--file",
            str(source),
            "--artifact-root",
            str(artifact_root),
            "--run-id",
            run_id,
            "--log",
            str(self.log),
            "--operator",
            "test-operator",
            "--evidence-ref",
            "incident:missing-initiation",
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("exactly one prior capture-initiation", missing.stderr)
        self.assertFalse(artifact_root.exists())

        self.log.write_text('{"kind":"broken","kind":"duplicate"}\n')
        invalid = self.run_cli(
            "capture-artifact",
            "--file",
            str(source),
            "--artifact-root",
            str(artifact_root),
            "--run-id",
            run_id,
            "--log",
            str(self.log),
            "--operator",
            "test-operator",
            "--evidence-ref",
            "incident:invalid-ledger",
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("panel.jsonl line 1: invalid JSON", invalid.stderr)
        self.assertFalse(artifact_root.exists())

    def test_secret_rejection_is_nonleaking_but_invalidation_is_not_crash_atomic_without_preflight_intent(self):
        activation_spec = self.root / "activation.json"
        activation_spec.write_text(
            json.dumps(
                {
                    "cohortName": "secret-incident-test",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": "a" * 40,
                    "runtimeSourceSha256": "b" * 64,
                    "artifactRootPolicy": "private-content-addressed-v1",
                }
            ),
            encoding="utf-8",
        )
        coordination = self.root / "evidence.lock"
        activated = self.run_cli(
            "capture-activate",
            "--log",
            str(self.log),
            "--spec",
            str(activation_spec),
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        initiated = self.run_cli(
            "capture-initiate",
            "--log",
            str(self.log),
            "--activation-id",
            json.loads(activated.stdout)["activationId"],
            "--idempotency-key",
            "secret-incident-run",
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(initiated.returncode, 0, initiated.stderr)
        run_id = json.loads(initiated.stdout)["runId"]
        token = "ghp_" + "x" * 40
        source = self.root / "secret.txt"
        source.write_text(token, encoding="utf-8")

        rejected = self.run_cli(
            "capture-artifact",
            "--file",
            str(source),
            "--artifact-root",
            str(self.root / "artifacts"),
            "--run-id",
            run_id,
            "--log",
            str(self.log),
            "--operator",
            "test-operator",
            "--evidence-ref",
            "incident:test-secret-detector",
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(rejected.returncode, 1)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(rows[-1]["kind"], "capture-invalidation")
        self.assertEqual(rows[-1]["runId"], run_id)
        self.assertEqual(rows[-1]["reason"], "secret-detected")
        self.assertNotIn(token, self.log.read_text())
        self.assertNotIn(token, rejected.stderr)

    def test_attempt_duplicate_secret_seat_is_nonleaking_and_invalidated(self):
        activation_spec = self.root / "activation-secret-seat.json"
        activation_spec.write_text(
            json.dumps(
                {
                    "cohortName": "secret-seat-test",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": "a" * 40,
                    "runtimeSourceSha256": "b" * 64,
                    "artifactRootPolicy": "private-content-addressed-v1",
                }
            ),
            encoding="utf-8",
        )
        coordination = self.root / "secret-seat-evidence.lock"
        activated = self.run_cli(
            "capture-activate",
            "--log",
            str(self.log),
            "--spec",
            str(activation_spec),
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        initiated = self.run_cli(
            "capture-initiate",
            "--log",
            str(self.log),
            "--activation-id",
            json.loads(activated.stdout)["activationId"],
            "--idempotency-key",
            "secret-seat-run",
            "--coordination-lock",
            str(coordination),
        )
        self.assertEqual(initiated.returncode, 0, initiated.stderr)

        secret = "sk-proj-" + "D" * 40
        attempt_spec = self.root / "attempt-secret-seat.json"
        attempt_spec.write_text(
            json.dumps(
                {
                    "initiationId": json.loads(initiated.stdout)["initiationId"],
                    "seatInputArtifacts": {},
                    "seatPlan": [
                        {"seatId": secret},
                        {"seatId": secret},
                    ],
                }
            ),
            encoding="utf-8",
        )
        baseline = self.root / "secret-seat-baseline.json"
        baseline.write_text("{}", encoding="utf-8")

        rejected = self.run_cli(
            "capture-attempt",
            "--log",
            str(self.log),
            "--artifact-root",
            str(self.root / "secret-seat-artifacts"),
            "--spec",
            str(attempt_spec),
            "--decision-before-file",
            str(baseline),
            "--coordination-lock",
            str(coordination),
        )

        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(rejected.stdout, "")
        self.assertNotIn(secret, rejected.stderr)
        self.assertNotIn(secret, self.log.read_text())
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(rows[-1]["kind"], "capture-invalidation")
        self.assertEqual(rows[-1]["runId"], json.loads(initiated.stdout)["runId"])
        self.assertEqual(rows[-1]["reason"], "secret-detected")

    def test_secret_invalidation_appends_before_coordination_lock_release(self):
        source = self.root / "secret.txt"
        source.write_text("safe fixture bytes", encoding="utf-8")
        run_id = new_id("run")
        lock_state = {"held": False}

        @contextmanager
        def observed_lock(_path):
            lock_state["held"] = True
            try:
                yield
            finally:
                lock_state["held"] = False

        store = mock.Mock()
        store.capture.side_effect = cli.SecretDetectedError(
            SimpleNamespace(
                code="secret-detected", stage="preflight", recovery_path=None
            )
        )

        def observed_invalidation(_log, _payload, *, coordination_lock):
            self.assertTrue(lock_state["held"])
            self.assertIsNone(coordination_lock)
            return {"kind": "capture-invalidation"}

        args = SimpleNamespace(
            run_id=run_id,
            log=str(self.log),
            operator="test-operator",
            evidence_ref="incident:lock-order",
            control_artifact=False,
            artifact_root=str(self.root / "artifacts"),
            coordination_lock=str(self.root / "coordination.lock"),
            file=str(source),
            secret_token_file=[],
        )
        with (
            mock.patch.object(cli, "evidence_write_lock", side_effect=observed_lock),
            mock.patch.object(cli, "ArtifactStore", return_value=store),
            mock.patch.object(
                cli,
                "validate_capture_ledger",
                return_value=([], [{"kind": "capture-initiation", "runId": run_id}]),
            ),
            mock.patch.object(
                cli,
                "append_capture_invalidation",
                side_effect=observed_invalidation,
            ) as invalidation,
        ):
            with self.assertRaises(cli.SecretDetectedError):
                cli.command_capture_artifact(args)
        invalidation.assert_called_once()
        self.assertFalse(lock_state["held"])

    def test_live_activation_requires_in_process_wrapper_binding_and_remains_blocked(self):
        commit = "b" * 40
        source_sha256 = "c" * 64
        artifact_root = self.root / "artifacts"
        manifest_file = self.root / "approval.json"
        manifest_bytes = b'{"schemaVersion":1}'
        manifest_file.write_bytes(manifest_bytes)
        manifest_ref = ArtifactStore(artifact_root).capture(manifest_bytes)
        activation_spec = self.root / "activation.json"
        activation_spec.write_text(
            json.dumps(
                {
                    "cohortName": "governed-live-test",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": commit,
                    "runtimeSourceSha256": source_sha256,
                    "artifactRootPolicy": "private-content-addressed-v1",
                    "approvalManifest": manifest_ref,
                }
            ),
            encoding="utf-8",
        )
        args = SimpleNamespace(
            log=str(self.log),
            spec=str(activation_spec),
            approval_manifest_file=str(manifest_file),
            artifact_root=str(artifact_root),
            coordination_lock=str(self.root / "evidence.lock"),
        )
        live_path = mock.patch.object(cli, "_is_live_write_path", return_value=True)
        authority = mock.patch.object(cli, "_require_write_authority")

        with live_path, authority:
            with self.assertRaisesRegex(cli.LedgerError, "source-pinned wrapper"):
                cli.command_capture_activate(args)
        self.assertFalse(self.log.exists())

        args._runtime_source_commit = "c" * 40
        args._runtime_source_sha256 = source_sha256
        args._runtime_source_root = Path(cli.__file__).parents[2]
        with (
            mock.patch.object(cli, "_is_live_write_path", return_value=True),
            mock.patch.object(cli, "_require_write_authority"),
        ):
            with self.assertRaisesRegex(cli.LedgerError, "installed runtime pin"):
                cli.command_capture_activate(args)
        self.assertFalse(self.log.exists())

        args._runtime_source_commit = commit
        with (
            mock.patch.object(cli, "_is_live_write_path", return_value=True),
            mock.patch.object(cli, "_require_write_authority"),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "capture activation evidence blocked",
            ):
                cli.command_capture_activate(args)
        self.assertFalse(self.log.exists())

    def test_live_activation_ignores_forgeable_runtime_environment(self):
        commit = "e" * 40
        activation_spec = self.root / "activation.json"
        activation_spec.write_text(
            json.dumps(
                {
                    "cohortName": "environment-forgery-test",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": commit,
                    "runtimeSourceSha256": "f" * 64,
                    "artifactRootPolicy": "private-content-addressed-v1",
                }
            ),
            encoding="utf-8",
        )
        args = SimpleNamespace(
            log=str(self.log),
            spec=str(activation_spec),
            approval_manifest_file=None,
            artifact_root=None,
            coordination_lock=str(self.root / "evidence.lock"),
        )
        with (
            mock.patch.object(cli, "_is_live_write_path", return_value=True),
            mock.patch.object(cli, "_require_write_authority"),
            mock.patch.dict(
                os.environ,
                {"COUNCIL_RUNTIME_EXPECTED_COMMIT": commit},
            ),
        ):
            with self.assertRaisesRegex(cli.LedgerError, "source-pinned wrapper"):
                cli.command_capture_activate(args)
        self.assertFalse(self.log.exists())

    def test_activation_readiness_is_read_only_and_returns_gate_status(self):
        manifest = self.root / "manifest.json"
        manifest.write_bytes(b'{"schemaVersion":2}')
        args = SimpleNamespace(
            manifest_file=str(manifest),
            artifact_root=str(self.root / "artifacts"),
            runtime_source_commit="a" * 40,
            runtime_source_sha256="b" * 64,
            at="2026-08-23T12:00:00Z",
            _runtime_source_commit=None,
            _runtime_source_sha256=None,
        )
        ready = {"appendReady": True, "blockers": []}
        with (
            mock.patch.object(cli, "ArtifactStore") as store,
            mock.patch.object(
                cli, "evaluate_activation_evidence", return_value=ready
            ) as evaluate,
            mock.patch("sys.stdout"),
        ):
            self.assertEqual(cli.command_activation_readiness(args), 0)
        evaluate.assert_called_once_with(
            manifest.read_bytes(),
            reader=store.return_value,
            expected_runtime_commit="a" * 40,
            expected_source_sha256="b" * 64,
            activation_time="2026-08-23T12:00:00Z",
            as_of="2026-08-23T12:00:00Z",
        )

        with (
            mock.patch.object(cli, "ArtifactStore"),
            mock.patch.object(
                cli,
                "evaluate_activation_evidence",
                return_value={"appendReady": False, "blockers": ["stale"]},
            ),
            mock.patch("sys.stdout"),
        ):
            self.assertEqual(cli.command_activation_readiness(args), 1)

    def test_installed_wrapper_passes_authenticated_runtime_binding_in_process(self):
        wrapper = (Path(__file__).parents[1] / "runtime/predictions_report.py").read_text()
        self.assertNotIn("COUNCIL_RUNTIME_EXPECTED_COMMIT", wrapper)
        self.assertIn("runtime_source_commit=EXPECTED_COMMIT", wrapper)
        self.assertIn("runtime_source_sha256=EXPECTED_SOURCE_SHA256", wrapper)
        self.assertIn("runtime_source_root=SOURCE_ROOT", wrapper)
        self.assertIn("pwd.getpwuid(os.getuid()).pw_dir", wrapper)
        self.assertNotIn("Path.home()", wrapper)

    def test_evidence_restore_requires_and_forwards_repository_root(self):
        missing = self.run_cli(
            "evidence-restore",
            str(self.root / "snapshot"),
            str(self.root / "restore"),
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--repository-root", missing.stderr)

        args = SimpleNamespace(
            snapshot=str(self.root / "snapshot"),
            target=str(self.root / "restore"),
            repository_root=str(Path(__file__).parents[1]),
        )
        with mock.patch.object(
            cli, "restore_evidence_snapshot", return_value={"status": "restored"}
        ) as restore:
            self.assertEqual(cli.command_evidence_restore(args), 0)
        restore.assert_called_once_with(
            args.snapshot,
            args.target,
            repository_root=args.repository_root,
        )


TICKET_RUN_ID = "claude-opus-5:adc28853-61ae-4641-9fed-f5fd60da7d07"


def ticket_contract(**overrides):
    contract = {
        "schemaVersion": 1,
        "repository": "garcia42/ai-council",
        "issueNumber": 85,
        "targetBranch": "main",
        "baseCommit": "21357c87d6c958579495552af5aac567ae876a69",
        "workType": "change",
        # Placeholders: phase one never shows a seat either of these.
        "priority": "P1",
        "points": 1,
        "problemStatement": "Expose the two-phase seal on the command line.",
        "acceptanceCriteria": ["Phase one and phase two are commands."],
        "testCommands": ["PYTHONPATH=src:. python3 -m pytest tests/test_cli.py -q"],
        "allowedPaths": [
            {"kind": "file", "path": "src/council_tools/cli.py"},
            {"kind": "file", "path": "tests/test_cli.py"},
        ],
        "outOfScope": ["Any GitHub API call."],
        "dependencies": [],
        "rollbackPlan": "Revert the commit.",
    }
    contract.update(overrides)
    return contract


def ticket_seats(*, codex_days=3, codex_single=True, codex_reasons=()):
    return [
        {
            "seatId": "claude",
            "status": "submitted",
            "engineerDays": 2,
            "singleOutcome": True,
            "splitReasons": [],
            "priority": "P1",
            "confidence": 80,
        },
        {
            "seatId": "codex",
            "status": "submitted",
            "engineerDays": codex_days,
            "singleOutcome": codex_single,
            "splitReasons": list(codex_reasons),
            "priority": "P1",
            "confidence": 90,
        },
    ]


class TicketQualificationCliTest(unittest.TestCase):
    """The two-phase seal, driven the way an operator drives it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, name, value):
        path = self.root / name
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return str(path)

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

    def projection(self, contract=None):
        path = self.write("contract.json", contract or ticket_contract())
        return self.run_cli("ticket-projection", "--contract", path)

    def seal(self, *extra, contract=None, seats=None, run_id=TICKET_RUN_ID):
        contract_path = self.write("contract.json", contract or ticket_contract())
        seats_path = self.write("reviews.json", seats if seats is not None else ticket_seats())
        return self.run_cli(
            "ticket-seal",
            "--contract",
            contract_path,
            "--reviews",
            seats_path,
            "--run-id",
            run_id,
            *extra,
        )

    # -- phase one --------------------------------------------------------

    def test_phase_one_never_shows_a_seat_the_derived_fields(self):
        result = self.projection()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertRegex(payload["sizingProjectionSha256"], r"\A[0-9a-f]{64}\Z")
        for derived in ("points", "priority"):
            with self.subTest(derived=derived):
                self.assertNotIn(derived, payload["projection"])

    def test_phase_one_digest_ignores_every_placeholder(self):
        baseline = json.loads(self.projection().stdout)["sizingProjectionSha256"]
        for points, priority in ((3, "P0"), (2, "P1")):
            with self.subTest(points=points, priority=priority):
                other = self.projection(ticket_contract(points=points, priority=priority))
                self.assertEqual(
                    json.loads(other.stdout)["sizingProjectionSha256"], baseline
                )

    # -- phase two --------------------------------------------------------

    def test_phase_two_records_the_derived_values_and_binds_the_review(self):
        before = json.loads(self.projection().stdout)["sizingProjectionSha256"]
        result = self.seal()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertEqual(payload["contract"]["points"], 3)
        self.assertEqual(payload["contract"]["priority"], "P1")
        # The reviewed content did not move when the derived values landed.
        self.assertEqual(payload["sizingProjectionSha256"], before)
        self.assertEqual(payload["reviewRecord"]["sizingProjectionSha256"], before)
        self.assertEqual(
            payload["reviewRecord"]["contractSha256"], payload["contractSha256"]
        )
        self.assertEqual(
            payload["reviewRef"],
            {"runId": TICKET_RUN_ID, "contractSha256": payload["contractSha256"]},
        )

    def test_rendered_body_parses_and_round_trips(self):
        prose = self.write("prose.md", "## Outcome\n\nDrive the seal from a command.")
        out_body = str(self.root / "body.md")
        result = self.seal("--prose", prose, "--out-body", out_body)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        from council_tools.ticket_policy import parse_ticket_issue_body

        envelope = parse_ticket_issue_body(payload["body"])
        self.assertEqual(envelope.contract.as_dict(), payload["contract"])
        self.assertEqual(envelope.review_ref.as_dict(), payload["reviewRef"])
        self.assertEqual(Path(out_body).read_text(encoding="utf-8"), payload["body"])

    def test_no_body_file_is_written_unless_it_is_named(self):
        prose = self.write("prose.md", "Prose.")
        result = self.seal("--prose", prose)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Only the three inputs exist: the body was printed, never written.
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["contract.json", "prose.md", "reviews.json"],
        )
        self.assertIn("body", json.loads(result.stdout))

    def test_output_is_deterministic(self):
        first = self.seal()
        second = self.seal()
        self.assertEqual(first.stdout, second.stdout)

    def test_inputs_are_not_mutated(self):
        contract_path = self.write("contract.json", ticket_contract())
        original = Path(contract_path).read_text(encoding="utf-8")
        self.seal()
        self.assertEqual(Path(contract_path).read_text(encoding="utf-8"), original)

    # -- refusals ---------------------------------------------------------

    def test_an_ineligible_decision_is_refused(self):
        cases = {
            "estimate-over-three": ticket_seats(codex_days=4),
            "multiple-outcomes": ticket_seats(
                codex_single=False, codex_reasons=("Two outcomes.",)
            ),
        }
        for expected, seats in cases.items():
            with self.subTest(reason=expected):
                result = self.seal(seats=seats)
                self.assertEqual(result.returncode, 1)
                self.assertIn("review-not-eligible", result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_marker_text_in_prose_is_refused(self):
        from council_tools.ticket_policy import TICKET_POLICY_V1

        prose = self.write(
            "prose.md", f"Discussing {TICKET_POLICY_V1.contract_start_marker} inline."
        )
        result = self.seal("--prose", prose)
        self.assertEqual(result.returncode, 1)
        self.assertIn("prose-contains-marker", result.stderr)

    def test_unreadable_and_malformed_inputs_fail_closed(self):
        missing = str(self.root / "absent.json")
        cases = (
            ("missing contract", ("ticket-projection", "--contract", missing)),
            (
                "malformed contract",
                (
                    "ticket-projection",
                    "--contract",
                    self.write("bad.json", "{not json"),
                ),
            ),
        )
        for name, args in cases:
            with self.subTest(case=name):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(result.stdout, "")

    def test_seat_reviews_must_be_a_list(self):
        result = self.seal(seats={"seatId": "claude"})
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.strip())


if __name__ == "__main__":
    unittest.main()
