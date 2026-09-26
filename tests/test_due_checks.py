import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from council_tools import cli
from council_tools.due_checks import run_check
from council_tools.forecasts import (
    LedgerError,
    append_ledger_row,
    audit,
    make_attempt,
    outcome_fingerprint,
    validate_attempt,
    validate_check,
)
from tests.test_cli import completion


def check(script, *, cwd="/", timeout=30):
    return {
        "type": "command",
        "argv": ["/bin/sh", "-c", script],
        "cwd": cwd,
        "timeoutSeconds": timeout,
    }


def attempt(claim, resolution_date, **extra):
    return make_attempt(
        question=f"Merge {claim}?",
        expected_seats=["code", "theory", "ops"],
        claim=claim,
        resolution_date=resolution_date,
        resolved_by="Run the check",
        decision_link=f"merge of {claim}",
        materiality="decides whether the change stays",
        action_if_true="keep it",
        action_if_false="revert it",
        evidence_cutoff_at="2026-07-01T00:00:00Z",
        ts="2026-07-01T00:00:00Z",
        **extra,
    )


class FingerprintAndSchemaTest(unittest.TestCase):
    def test_fingerprint_without_check_is_the_pre_existing_digest(self):
        pieces = ("claim", "2026-07-10", "rule", "link")
        legacy = hashlib.sha256("\x1f".join(pieces).encode()).hexdigest()
        self.assertEqual(outcome_fingerprint(*pieces), legacy)
        self.assertEqual(outcome_fingerprint(*pieces, None), legacy)

    def test_check_is_bound_into_the_fingerprint(self):
        pieces = ("claim", "2026-07-10", "rule", "link")
        first = outcome_fingerprint(*pieces, check("exit 0"))
        self.assertNotEqual(first, outcome_fingerprint(*pieces))
        self.assertNotEqual(first, outcome_fingerprint(*pieces, check("exit 1")))

    def test_a_check_swapped_after_issuance_is_refused(self):
        row = attempt("swap", "2026-07-10", check=check("exit 1"))
        row["sharedOutcome"]["check"] = check("exit 0")
        with self.assertRaisesRegex(LedgerError, "fingerprint"):
            validate_attempt(row)

    def test_workstream_is_outside_the_fingerprint_but_must_be_registered(self):
        plain = attempt("ws", "2026-07-10")
        scoped = attempt("ws", "2026-07-10", workstream="tandr")
        self.assertEqual(
            plain["sharedOutcome"]["fingerprint"], scoped["sharedOutcome"]["fingerprint"]
        )
        with self.assertRaisesRegex(LedgerError, "workstream must be one of"):
            attempt("ws", "2026-07-10", workstream="nobody-owns-this")

    def test_malformed_checks_are_refused(self):
        cases = {
            "relative executable": {**check("exit 0"), "argv": ["sh", "-c", "exit 0"]},
            "relative cwd": check("exit 0", cwd="repo"),
            "boolean timeout": check("exit 0", timeout=True),
            "zero timeout": check("exit 0", timeout=0),
            "long timeout": check("exit 0", timeout=601),
            "extra key": {**check("exit 0"), "env": {}},
            "wrong type": {**check("exit 0"), "type": "http"},
            "empty argv": {**check("exit 0"), "argv": []},
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(LedgerError):
                validate_check(value)


class RunCheckTest(unittest.TestCase):
    def test_exit_codes_map_to_verdicts(self):
        self.assertEqual(run_check(check("exit 0"))["verdict"], "true")
        self.assertEqual(run_check(check("exit 1"))["verdict"], "false")
        self.assertEqual(run_check(check("exit 2"))["verdict"], "undetermined")

    def test_timeout_is_undetermined_and_kills_the_session(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "survived"
            result = run_check(
                check(f"(sleep 3; touch {marker}) & sleep 30", timeout=1)
            )
            self.assertEqual(result["verdict"], "undetermined")
            self.assertIsNone(result["exitCode"])
            subprocess.run(["sleep", "4"], check=True)
            self.assertFalse(marker.exists(), "a grandchild outlived the timeout")

    def test_a_command_that_cannot_start_is_undetermined(self):
        missing = {**check("exit 0"), "argv": ["/nonexistent/binary"]}
        self.assertEqual(run_check(missing)["verdict"], "undetermined")

    def test_the_caller_environment_does_not_reach_the_check(self):
        os.environ["COUNCIL_DUE_CHECK_LEAK"] = "1"
        try:
            result = run_check(check('test -z "$COUNCIL_DUE_CHECK_LEAK"'))
        finally:
            del os.environ["COUNCIL_DUE_CHECK_LEAK"]
        self.assertEqual(result["verdict"], "true")


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.temp.name)
        self.log = self.root / "panel.jsonl"
        self.events = self.root / "events.jsonl"
        self.log.touch()
        self.events.touch()

    def tearDown(self):
        self.temp.cleanup()

    def issue(self, claim, resolution_date="2026-07-10", **extra):
        row = attempt(claim, resolution_date, **extra)
        append_ledger_row(self.log, row)
        append_ledger_row(self.log, completion(row))
        return row["sharedOutcome"]["outcomeId"]


class WorkstreamScopeTest(LedgerCase):
    TODAY = date(2026, 9, 1)

    def test_scoped_debt_counts_own_and_unscoped_outcomes_only(self):
        self.issue("a", workstream="tandr")
        self.issue("b", workstream="tandr")
        self.issue("c")
        unscoped = audit(self.log, self.events, today=self.TODAY)
        self.assertEqual(unscoped["gradingDebtState"], "BLOCK_FINALIZATION")
        self.assertEqual(unscoped["scopedOldOverdueOutcomes"], 3)

        tandr = audit(self.log, self.events, today=self.TODAY, workstream="tandr")
        self.assertEqual(tandr["gradingDebtState"], "BLOCK_FINALIZATION")

        other = audit(
            self.log, self.events, today=self.TODAY, workstream="pysystemtrade"
        )
        self.assertEqual(other["scopedOldOverdueOutcomes"], 1)
        self.assertEqual(other["oldOverdueOutcomes"], 3)
        self.assertEqual(other["gradingDebtState"], "WARN")

    def test_map_classifies_legacy_outcomes_but_cannot_contradict_an_attempt(self):
        a = self.issue("a", workstream="tandr")
        b = self.issue("b")
        c = self.issue("c")
        mapped = audit(
            self.log,
            self.events,
            today=self.TODAY,
            workstream="pysystemtrade",
            workstream_map={b: "tandr", c: "tandr"},
        )
        self.assertEqual(mapped["scopedOldOverdueOutcomes"], 0)
        self.assertEqual(mapped["invalidRecords"], [])

        contradicted = audit(
            self.log, self.events, today=self.TODAY, workstream_map={a: "pysystemtrade"}
        )
        self.assertTrue(
            any("contradicts" in item for item in contradicted["invalidRecords"])
        )
        unknown = audit(
            self.log,
            self.events,
            today=self.TODAY,
            workstream_map={"outcome-" + "0" * 32: "tandr"},
        )
        self.assertTrue(any("never issued" in item for item in unknown["invalidRecords"]))

    def test_unregistered_report_workstream_is_refused(self):
        with self.assertRaises(LedgerError):
            audit(self.log, self.events, today=self.TODAY, workstream="nobody")

    def test_workstream_map_file(self):
        self.assertEqual(cli.load_workstream_map(self.root / "absent.json"), {})
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"schemaVersion": 1, "workstreams": {"x": "tandr"}}))
        with self.assertRaises(LedgerError):
            cli.load_workstream_map(bad)
        wrong_slug = self.root / "slug.json"
        wrong_slug.write_text(
            json.dumps({"schemaVersion": 1, "workstreams": {"outcome-1": "nobody"}})
        )
        with self.assertRaises(LedgerError):
            cli.load_workstream_map(wrong_slug)


class ResolveDueCliTest(LedgerCase):
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

    def resolve_due(self, *extra):
        return self.run_cli(
            "resolve-due",
            "--log", str(self.log),
            "--events", str(self.events),
            "--evidence-dir", str(self.root / "evidence"),
            *extra,
        )

    def lines(self, result):
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_dry_run_lists_and_writes_nothing(self):
        marker = self.root / "ran"
        self.issue("t", check=check(f"touch {marker}"))
        result = self.resolve_due()
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.lines(result)
        self.assertEqual(rows[0]["status"], "due")
        self.assertFalse(marker.exists(), "dry run executed the check")
        self.assertEqual(self.events.read_text(), "")
        self.assertFalse((self.root / "evidence").exists())

    def test_apply_records_true_and_false_and_leaves_the_rest(self):
        true_id = self.issue("t", check=check("exit 0"))
        false_id = self.issue("f", check=check("exit 1"))
        odd_id = self.issue("o", check=check("exit 7"))
        manual_id = self.issue("m")
        future_id = self.issue("later", resolution_date="2099-01-01", check=check("exit 0"))

        result = self.resolve_due("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        by_id = {row["outcomeId"]: row for row in self.lines(result) if "outcomeId" in row}
        self.assertEqual(by_id[true_id]["status"], "recorded")
        self.assertEqual(by_id[false_id]["status"], "recorded")
        self.assertEqual(by_id[odd_id]["status"], "left-for-human")
        self.assertNotIn(manual_id, by_id)
        self.assertNotIn(future_id, by_id)

        events = {
            event["outcomeId"]: event
            for event in map(json.loads, self.events.read_text().splitlines())
        }
        self.assertEqual(set(events), {true_id, false_id})
        self.assertIs(events[true_id]["cameTrue"], True)
        self.assertIs(events[false_id]["cameTrue"], False)
        for event in events.values():
            self.assertEqual(event["method"], "deterministic")
            self.assertEqual(event["resolver"], "resolve-due")
            path, digest = event["evidence"].split("#sha256=")
            data = Path(path).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
            self.assertEqual(json.loads(data)["outcomeId"], event["outcomeId"])

        report = audit(self.log, self.events)
        self.assertEqual(report["invalidRecords"], [])
        self.assertIn(true_id, report["gradedOutcomeIds"])

        again = self.resolve_due("--apply")
        self.assertEqual(again.returncode, 0, again.stderr)
        again_ids = {row.get("outcomeId") for row in self.lines(again)}
        self.assertNotIn(true_id, again_ids)
        self.assertIn(odd_id, again_ids)

    def test_apply_refuses_live_paths_off_the_authority_host(self):
        from types import SimpleNamespace
        from unittest import mock

        args = SimpleNamespace(
            apply=True,
            log=cli.DEFAULT_LOG,
            events=cli.DEFAULT_EVENTS,
            evidence_dir=str(Path(cli.DEFAULT_EVENTS).parent / "auto"),
            coordination_lock=cli.DEFAULT_COORDINATION_LOCK,
        )
        with mock.patch.object(cli.socket, "gethostname", return_value="not-manny"), \
                mock.patch.object(cli, "audit") as audit_mock:
            with self.assertRaisesRegex(LedgerError, "authorized only on manny"):
                cli.command_resolve_due(args)
        audit_mock.assert_not_called()

    def test_report_workstream_flag_changes_the_exit_status(self):
        for claim in "abc":
            self.issue(claim, workstream="tandr")
        blocked = self.run_cli(
            "report", "--log", str(self.log), "--events", str(self.events),
            "--today", "2026-09-01", "--workstream", "tandr",
        )
        self.assertEqual(blocked.returncode, 3, blocked.stderr)
        scoped = self.run_cli(
            "report", "--log", str(self.log), "--events", str(self.events),
            "--today", "2026-09-01", "--workstream", "pysystemtrade",
        )
        self.assertEqual(scoped.returncode, 0, scoped.stderr)
        self.assertIn("debt_workstream=pysystemtrade scoped_old_overdue=0", scoped.stdout)


if __name__ == "__main__":
    unittest.main()
