import hashlib
import json
import os
import shutil
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
    def test_check_stays_outside_the_fingerprint_so_older_readers_accept_it(self):
        # An older runtime recomputes the digest from the four prose pieces. If
        # the check were bound in, the first check-bearing attempt would be an
        # invalid row to every rollback target, for ever (append-only ledger).
        row = attempt("claim", "2026-07-10", check=check("exit 10"), workstream="tandr")
        outcome = row["sharedOutcome"]
        pieces = [" ".join(outcome[k].split()).casefold()
                  for k in ("claim", "resolutionDate", "resolvedBy", "decisionLink")]
        legacy = hashlib.sha256("\x1f".join(pieces).encode()).hexdigest()
        self.assertEqual(outcome["fingerprint"], legacy)

    def test_a_malformed_check_in_a_row_is_refused(self):
        row = attempt("bad", "2026-07-10", check=check("exit 10"))
        row["sharedOutcome"]["check"] = {**check("exit 10"), "timeoutSeconds": 0}
        with self.assertRaisesRegex(LedgerError, "timeoutSeconds"):
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
            "relative executable": {**check("exit 10"), "argv": ["sh", "-c", "exit 10"]},
            "relative cwd": check("exit 0", cwd="repo"),
            "boolean timeout": check("exit 0", timeout=True),
            "zero timeout": check("exit 0", timeout=0),
            "long timeout": check("exit 0", timeout=601),
            "extra key": {**check("exit 10"), "env": {}},
            "wrong type": {**check("exit 10"), "type": "http"},
            "empty argv": {**check("exit 10"), "argv": []},
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(LedgerError):
                validate_check(value)


class NoCheckReasonRowTest(unittest.TestCase):
    def test_reason_stays_outside_the_fingerprint_and_rows_without_either_stay_valid(self):
        plain = attempt("r", "2026-07-10")
        reasoned = attempt("r", "2026-07-10", no_check_reason="needs a human reading")
        self.assertEqual(
            plain["sharedOutcome"]["fingerprint"], reasoned["sharedOutcome"]["fingerprint"]
        )
        # A row from before the rule carries neither; it must stay readable.
        validate_attempt(plain)

    def test_a_row_cannot_carry_both_or_an_empty_reason(self):
        row = attempt("r", "2026-07-10", check=check("exit 10"))
        row["sharedOutcome"]["noCheckReason"] = "and also a reason"
        with self.assertRaisesRegex(LedgerError, "noCheckReason must be absent"):
            validate_attempt(row)
        with self.assertRaisesRegex(LedgerError, "noCheckReason"):
            attempt("r", "2026-07-10", no_check_reason="   ")


class AttemptCliRuleTest(unittest.TestCase):
    """The rule binds where a NEW attempt is written: ``attempt``, not the reader."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "log.jsonl"
        self.log.touch()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, claim="c", **outcome):
        spec = self.root / "attempt.json"
        spec.write_text(json.dumps({
            "question": "Merge it?",
            "expectedSeats": ["code"],
            "sharedOutcome": {
                "claim": claim,
                "resolutionDate": "2099-01-01",
                "resolvedBy": "r",
                "decisionLink": "d",
                "materiality": "m",
                "actionIfTrue": "t",
                "actionIfFalse": "f",
                **outcome,
            },
        }))
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "council_tools.cli", "attempt", "--log", str(self.log),
             "--spec", str(spec)],
            text=True, capture_output=True, env=env, check=False,
        )

    def test_an_ungradable_attempt_is_refused_and_nothing_is_written(self):
        for outcome, message in (
            ({}, "requires sharedOutcome.workstream"),
            ({"workstream": "tandr"}, "exactly one of"),
            ({"workstream": "tandr", "check": check("exit 10")}, "carry the claim"),
        ):
            with self.subTest(message):
                result = self.write(**outcome)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.log.read_text(), "")

    def test_a_check_or_a_reason_is_recorded(self):
        bound = {**check("exit 10"), "argv": ["/bin/sh", "-c", "exit 10", "sh", "--claim", "c"]}
        for claim, outcome in (
            ("c", {"workstream": "tandr", "check": bound}),
            ("judged", {"workstream": "tandr",
                        "noCheckReason": "production behaviour needs a human read"}),
        ):
            result = self.write(claim, **outcome)
            self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(rows[0]["sharedOutcome"]["check"], bound)
        self.assertEqual(
            rows[1]["sharedOutcome"]["noCheckReason"], "production behaviour needs a human read"
        )


class RunCheckTest(unittest.TestCase):
    def test_exit_codes_map_to_verdicts(self):
        self.assertEqual(run_check(check("exit 10"))["verdict"], "true")
        self.assertEqual(run_check(check("exit 11"))["verdict"], "false")
        for code in (0, 1, 2):
            with self.subTest(code=code):
                self.assertEqual(
                    run_check(check(f"exit {code}"))["verdict"], "undetermined"
                )

    def test_a_crashing_check_is_not_graded_false(self):
        crash = {**check(""), "argv": ["/usr/bin/python3", "-c", "raise RuntimeError('bug')"]}
        self.assertEqual(run_check(crash)["verdict"], "undetermined")
        unmatched = check("grep -q never-there /etc/hostname")
        self.assertEqual(run_check(unmatched)["verdict"], "undetermined")

    def test_evidence_digests_the_script_a_path_names(self):
        with tempfile.TemporaryDirectory() as root:
            script = Path(root) / "check.sh"
            script.write_text("exit 10\n")
            result = run_check(
                {"type": "command", "argv": ["/bin/sh", "check.sh"], "cwd": root,
                 "timeoutSeconds": 30}
            )
            self.assertEqual(result["verdict"], "true")
            self.assertEqual(
                result["argvFileSha256"]["check.sh"],
                hashlib.sha256(b"exit 10\n").hexdigest(),
            )
            self.assertIn("/bin/sh", result["argvFileSha256"])

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
        missing = {**check("exit 10"), "argv": ["/nonexistent/binary"]}
        self.assertEqual(run_check(missing)["verdict"], "undetermined")

    def test_the_caller_environment_does_not_reach_the_check(self):
        os.environ["COUNCIL_DUE_CHECK_LEAK"] = "1"
        try:
            result = run_check(check('test -z "$COUNCIL_DUE_CHECK_LEAK" && exit 10 || exit 11'))
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
        stray = "outcome-" + "0" * 32
        unknown = audit(
            self.log, self.events, today=self.TODAY, workstream_map={stray: "tandr"}
        )
        self.assertEqual(unknown["invalidRecords"], [])
        self.assertEqual(unknown["workstreamMapUnknownIds"], [stray])

    def test_a_change_spanning_workstreams_carries_all_their_debt(self):
        for claim in "ab":
            self.issue(claim, workstream="tandr")
        self.issue("c", workstream="council-tools")
        single = audit(self.log, self.events, today=self.TODAY, workstream="council-tools")
        self.assertEqual(single["scopedOldOverdueOutcomes"], 1)
        both = audit(
            self.log, self.events, today=self.TODAY,
            workstream=["council-tools", "tandr"],
        )
        self.assertEqual(both["scopedOldOverdueOutcomes"], 3)
        self.assertEqual(both["gradingDebtState"], "BLOCK_FINALIZATION")

    def test_ledger_wide_backstop_blocks_every_scope(self):
        from council_tools.forecasts import GLOBAL_DEBT_BACKSTOP

        for index in range(GLOBAL_DEBT_BACKSTOP - 1):
            self.issue(f"dormant {index}", workstream="plaintape")
        below = audit(self.log, self.events, today=self.TODAY, workstream="tandr")
        self.assertEqual(below["gradingDebtState"], "WARN")
        self.issue("one more", workstream="plaintape")
        at = audit(self.log, self.events, today=self.TODAY, workstream="tandr")
        self.assertEqual(at["scopedOldOverdueOutcomes"], 0)
        self.assertEqual(at["gradingDebtState"], "BLOCK_FINALIZATION")

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
        true_id = self.issue("t", check=check("exit 10"))
        false_id = self.issue("f", check=check("exit 11"))
        odd_id = self.issue("o", check=check("exit 7"))
        manual_id = self.issue("m")
        future_id = self.issue("later", resolution_date="2099-01-01", check=check("exit 10"))

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

    def test_evidence_dir_defaults_beside_the_sidecar(self):
        outcome_id = self.issue("t", check=check("exit 10"))
        result = self.run_cli(
            "resolve-due", "--log", str(self.log), "--events", str(self.events), "--apply"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(self.events.read_text())
        self.assertEqual(event["outcomeId"], outcome_id)
        path = Path(event["evidence"].split("#sha256=")[0])
        self.assertEqual(path.parent, self.root / "resolution-evidence" / "auto")
        self.assertTrue(path.is_file())

    def test_budget_defers_checks_instead_of_holding_up_a_council(self):
        marker = self.root / "ran"
        outcome_id = self.issue("t", check=check(f"touch {marker}; exit 10"))
        result = self.resolve_due("--apply", "--budget-seconds", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {row.get("outcomeId"): row for row in self.lines(result)}
        self.assertEqual(rows[outcome_id]["status"], "deferred-budget")
        self.assertFalse(marker.exists())
        self.assertEqual(self.events.read_text(), "")

    def test_an_outcome_due_today_waits_until_the_day_has_ended(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        self.issue("today", resolution_date=today, check=check("exit 10"))
        result = self.resolve_due("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines(result), [
            {"summary": True, "due": 0, "applied": True, "errors": 0}
        ])
        self.assertEqual(self.events.read_text(), "")

    def test_the_machine_resolver_name_is_reserved(self):
        outcome_id = self.issue("t")
        result = self.run_cli(
            "resolve", outcome_id, "false", "--log", str(self.log),
            "--events", str(self.events), "--evidence", "x",
            "--resolver", "resolve-due", "--method", "deterministic",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("reserved", result.stderr)
        self.assertEqual(self.events.read_text(), "")

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
        self.assertIn("entries=0", scoped.stdout)
        spanning = self.run_cli(
            "report", "--log", str(self.log), "--events", str(self.events),
            "--today", "2026-09-01", "--workstream", "tandr",
            "--workstream", "pysystemtrade",
        )
        # Order matters to the test: a last-flag-wins parser would scope to
        # pysystemtrade alone and exit 0.
        self.assertEqual(spanning.returncode, 3, spanning.stderr)
        table = self.root / "outcome-workstreams.json"
        table.write_text(json.dumps({"schemaVersion": 1, "workstreams": {}}))
        shown = self.run_cli(
            "report", "--log", str(self.log), "--events", str(self.events),
            "--today", "2026-09-01", "--workstream", "tandr", "--json",
        )
        disclosed = json.loads(shown.stdout)["workstreamMap"]
        table.write_text("not json")
        unscoped = self.run_cli(
            "report", "--log", str(self.log), "--events", str(self.events),
            "--today", "2026-09-01", "--json",
        )
        self.assertNotIn("workstreamMap", json.loads(unscoped.stdout))
        table.write_text(json.dumps({"schemaVersion": 1, "workstreams": {}}))
        self.assertEqual(disclosed["path"], str(table))
        self.assertEqual(disclosed["sha256"], hashlib.sha256(table.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()


class StagedTimerTest(unittest.TestCase):
    """The staged resolve-due units say what the operator steps say."""

    OPS = Path(__file__).parents[1] / "operations"

    def test_service_grades_the_legacy_study_on_manny_only(self):
        text = (self.OPS / "council-resolve-due.service").read_text()
        exec_line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
        argv = exec_line.split("=", 1)[1].split()
        self.assertEqual(argv[0], "/usr/bin/python3")
        # --study is a global flag: it must come before the subcommand.
        self.assertLess(argv.index("--study"), argv.index("resolve-due"))
        self.assertEqual(argv[argv.index("--study") + 1], "council-legacy")
        self.assertEqual(argv[-1], "--apply")
        self.assertIn("ConditionHost=manny", text.splitlines())
        self.assertIn("OnFailure=onduty-failed@%n.service", text.splitlines())

    def test_timer_fires_after_the_new_york_day_ends(self):
        text = (self.OPS / "council-resolve-due.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 00:40:00 America/New_York", text.splitlines())
        self.assertIn("Unit=council-resolve-due.service", text.splitlines())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_units_verify(self):
        result = subprocess.run(
            ["systemd-analyze", "--user", "verify",
             str(self.OPS / "council-resolve-due.service"),
             str(self.OPS / "council-resolve-due.timer")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
