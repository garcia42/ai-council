import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from council_tools import brief_recovery
from council_tools.brief_recovery import (
    LedgerError,
    plan_blind_brief_recovery,
    prepare_blind_brief,
    recover_blind_brief,
)
from council_tools.forecasts import validate_blind_brief_identity


LEDGER = "/home/trader/.claude/knowledge/futures-panel-log.jsonl"
BRIEFS = "/home/trader/.claude/knowledge/council-eval/briefs"
SHARED_BRIEF = f"{BRIEFS}/2026-08-23-shared.md"
DDA = "run-dda640ff78404cacadc35136f1b1da2e"
E8 = "run-e8b31abd9b91405190862e3d64a79a90"


class Crash(RuntimeError):
    pass


def crash_at(name):
    def checkpoint(reached):
        if reached == name:
            raise Crash(name)

    return checkpoint


def council_row(run_id, brief, **extra):
    row = {
        "schemaVersion": 1,
        "kind": "council",
        "runId": run_id,
        "ts": "2026-08-23T17:56:00Z",
        "question": f"question for {run_id}",
        "blindSeat": {
            "role": "generic",
            "required": True,
            "ran": True,
            "changedDecision": False,
            "brief": brief,
        },
    }
    row.update(extra)
    return row


def canonical(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"


class BriefRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ledger = self._mirror(LEDGER)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self._mirror(BRIEFS).mkdir(parents=True, exist_ok=True)
        rows = [
            {"schemaVersion": 1, "kind": "council-attempt", "runId": DDA},
            council_row(DDA, SHARED_BRIEF),
            council_row(E8, SHARED_BRIEF),
        ]
        self.ledger.write_text("".join(canonical(row) for row in rows), encoding="utf-8")
        self.ledger.chmod(0o600)
        self.source = self.root / "recovery-source.md"
        self.source.write_text("the brief the E8 blind seat actually read\n", encoding="utf-8")
        self.artifact_parent = self._mirror(f"/home/trader/.local/state/council-tools")
        self.artifact_parent.mkdir(parents=True, exist_ok=True)

    def _mirror(self, logical):
        return self.root / Path(logical).relative_to("/")

    def plan(self, **overrides):
        values = dict(
            ledger_path=LEDGER,
            target_line=3,
            replacement_source=str(
                Path("/") / self.source.relative_to(self.root)
            ),
            operator="principal",
            approval_reference="test approval",
            approval_reason="restore unique brief ownership",
            rehearsal_root=self.root,
        )
        values.update(overrides)
        return plan_blind_brief_recovery(**values)

    def recover(self, spec, **kwargs):
        kwargs.setdefault("operator_confirmed", True)
        kwargs.setdefault("rehearsal_root", self.root)
        return recover_blind_brief(spec, **kwargs)

    def artifact_dir(self, spec):
        return self._mirror(spec["artifactDir"])

    def audit_records(self, spec):
        audit = self.artifact_dir(spec) / f"council-brief-recovery-{E8}.audit.jsonl"
        return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]

    def assert_repaired(self, spec):
        after = self.ledger.read_bytes()
        self.assertEqual(brief_recovery._digest(after), spec["expectedAfter"]["ledgerSha256"])
        rows = [json.loads(line) for line in after.decode("utf-8").splitlines()]
        self.assertEqual(rows[1]["blindSeat"]["brief"], SHARED_BRIEF)
        self.assertEqual(
            rows[2]["blindSeat"]["brief"], spec["replacementBrief"]["destinationPath"]
        )
        destination = self._mirror(spec["replacementBrief"]["destinationPath"])
        self.assertEqual(destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o444)
        backup = self.artifact_dir(spec) / f"{self.ledger.name}.{spec['ledger']['expectedSha256']}.backup"
        self.assertEqual(
            brief_recovery._digest(backup.read_bytes()), spec["ledger"]["expectedSha256"]
        )
        records = self.audit_records(spec)
        self.assertEqual([r["status"] for r in records], ["prepared", "completed"])
        self.assertEqual(len({r["recoveryId"] for r in records}), 1)
        self.assertEqual(stat.S_IMODE(self.ledger.stat().st_mode), 0o600)

    # ---- planning -----------------------------------------------------

    def test_plan_derives_the_incident_and_recovery_repairs_it(self):
        spec = self.plan()
        self.assertEqual(spec["target"]["runId"], E8)
        self.assertEqual(spec["target"]["conflictingLine"], 2)
        self.assertEqual(spec["target"]["conflictingRunId"], DDA)
        self.assertIn(E8, Path(spec["replacementBrief"]["destinationPath"]).name)
        result = self.recover(spec)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["resumed"])
        self.assertEqual(result["reusedSteps"], [])
        self.assert_repaired(spec)

    def test_plan_refuses_a_line_that_does_not_share_its_brief(self):
        rows = [
            {"schemaVersion": 1, "kind": "council-attempt", "runId": DDA},
            council_row(DDA, f"{BRIEFS}/only-mine.md"),
            council_row(E8, SHARED_BRIEF),
        ]
        self.ledger.write_text("".join(canonical(row) for row in rows), encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "exactly one other"):
            self.plan()

    def test_plan_refuses_a_non_council_line(self):
        with self.assertRaisesRegex(LedgerError, "not a council completion"):
            self.plan(target_line=1)

    def test_plan_refuses_a_destination_without_the_run_id(self):
        with self.assertRaisesRegex(LedgerError, "exact target runId"):
            self.plan(destination_path=f"{BRIEFS}/no-run-id.md")

    # ---- fail-closed preconditions ------------------------------------

    def test_ledger_drift_refuses_and_writes_nothing(self):
        spec = self.plan()
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(canonical({"schemaVersion": 1, "kind": "council-attempt", "runId": "run-" + "c" * 32}))
        with self.assertRaisesRegex(LedgerError, "ledger SHA-256 drift"):
            self.recover(spec)
        self.assertFalse(self.artifact_dir(spec).exists())
        self.assertFalse(self._mirror(spec["replacementBrief"]["destinationPath"]).exists())

    def test_target_raw_line_drift_refuses(self):
        spec = self.plan()
        spec["target"]["rawLineSha256"] = "0" * 64
        with self.assertRaisesRegex(LedgerError, "target raw-line"):
            self.recover(spec)

    def test_wrong_expected_after_refuses(self):
        spec = self.plan()
        spec["expectedAfter"]["targetRawLineSha256"] = "1" * 64
        with self.assertRaisesRegex(LedgerError, "repaired target raw-line"):
            self.recover(spec)
        self.assertFalse(self.artifact_dir(spec).exists())

    def test_ledger_mode_drift_refuses(self):
        spec = self.plan()
        self.ledger.chmod(0o644)
        with self.assertRaisesRegex(LedgerError, "ledger mode drift"):
            self.recover(spec)

    def test_replacement_source_drift_refuses(self):
        spec = self.plan()
        self.source.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "replacement brief SHA-256 drift"):
            self.recover(spec)

    def test_existing_destination_refuses_without_resume(self):
        spec = self.plan()
        self._mirror(spec["replacementBrief"]["destinationPath"]).write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "destination already exists"):
            self.recover(spec)

    def test_existing_artifact_dir_refuses_without_resume(self):
        spec = self.plan()
        self.artifact_dir(spec).mkdir(parents=True)
        with self.assertRaisesRegex(LedgerError, "artifact directory already exists"):
            self.recover(spec)

    def test_missing_operator_confirmation_refuses(self):
        spec = self.plan()
        with self.assertRaisesRegex(LedgerError, "operator approval"):
            self.recover(spec, operator_confirmed=False)

    def test_unknown_spec_key_refuses(self):
        spec = self.plan()
        spec["extra"] = True
        with self.assertRaisesRegex(LedgerError, "unexpected shape"):
            self.recover(spec)

    def test_live_knowledge_recovery_is_authorized_only_on_manny(self):
        live = Path(LEDGER)
        with mock.patch.object(brief_recovery.socket, "gethostname", return_value="plaintape-prod-gcp"):
            with self.assertRaisesRegex(LedgerError, "authorized only on manny"):
                brief_recovery._assert_live_authority(live)
        with mock.patch.object(brief_recovery.socket, "gethostname", return_value="manny"):
            brief_recovery._assert_live_authority(live)

    def test_rehearsal_root_may_not_be_live_knowledge(self):
        spec = self.plan()
        with self.assertRaisesRegex(LedgerError, "outside live knowledge"):
            self.recover(spec, rehearsal_root=Path("/home/trader/.claude/knowledge"))

    # ---- crash injection and resume ------------------------------------

    def test_every_checkpoint_crash_is_exactly_resumable(self):
        names = [
            "validated",
            "backup-durable",
            "intent-durable",
            "brief-durable",
            "before-ledger-replace",
            "ledger-durable",
            "complete-audit-durable",
        ]
        for name in names:
            with self.subTest(checkpoint=name):
                self.setUp()
                spec = self.plan()
                with self.assertRaises(Crash):
                    self.recover(spec, checkpoint=crash_at(name))
                if name == "validated":
                    # nothing durable yet; a plain re-run is the correct move
                    self.assertFalse(self.artifact_dir(spec).exists())
                    result = self.recover(spec)
                    self.assertEqual(result["status"], "completed")
                else:
                    result = self.recover(spec, resume=True)
                    self.assertIn(result["status"], {"completed", "already-complete"})
                    self.assertTrue(result["resumed"])
                self.assert_repaired(spec)
                # a second resume is a no-op, not a second repair
                again = self.recover(spec, resume=True)
                self.assertEqual(again["status"], "already-complete")
                self.assert_repaired(spec)

    def test_resume_without_a_prior_attempt_refuses(self):
        spec = self.plan()
        with self.assertRaisesRegex(LedgerError, "artifact directory does not exist"):
            self.recover(spec, resume=True)

    def test_resume_refuses_a_tampered_prepared_record(self):
        spec = self.plan()
        with self.assertRaises(Crash):
            self.recover(spec, checkpoint=crash_at("brief-durable"))
        audit = self.artifact_dir(spec) / f"council-brief-recovery-{E8}.audit.jsonl"
        records = self.audit_records(spec)
        records[0]["oldBriefPath"] = f"{BRIEFS}/something-else.md"
        audit.chmod(0o600)
        audit.write_text(
            "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(LedgerError, "disagrees with this spec on oldBriefPath"):
            self.recover(spec, resume=True)

    def test_resume_refuses_a_tampered_backup(self):
        spec = self.plan()
        with self.assertRaises(Crash):
            self.recover(spec, checkpoint=crash_at("intent-durable"))
        backup = self.artifact_dir(spec) / f"{self.ledger.name}.{spec['ledger']['expectedSha256']}.backup"
        backup.chmod(0o600)
        backup.write_text("not the before image\n", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "recovery backup exists with unexpected content"):
            self.recover(spec, resume=True)

    def test_resume_refuses_a_ledger_at_neither_image(self):
        spec = self.plan()
        with self.assertRaises(Crash):
            self.recover(spec, checkpoint=crash_at("intent-durable"))
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(canonical({"schemaVersion": 1, "kind": "council-attempt", "runId": "run-" + "d" * 32}))
        with self.assertRaisesRegex(LedgerError, "ledger SHA-256 drift"):
            self.recover(spec, resume=True)

    def test_resume_clears_a_leaked_temporary(self):
        spec = self.plan()
        with self.assertRaises(Crash):
            self.recover(spec, checkpoint=crash_at("intent-durable"))
        leaked = self.ledger.with_name(f".{self.ledger.name}.brief-recovery-deadbeef.tmp")
        leaked.write_bytes(b"partial\n")
        result = self.recover(spec, resume=True)
        self.assertFalse(leaked.exists())
        self.assertEqual(result["cleanedTemporaries"], [str(Path("/") / leaked.relative_to(self.root))])
        self.assert_repaired(spec)

    def test_resume_survives_a_peer_append_after_the_ledger_swap(self):
        spec = self.plan()
        with self.assertRaises(Crash):
            self.recover(spec, checkpoint=crash_at("ledger-durable"))
        # a legitimate council appends while the operator is asleep
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(canonical({"schemaVersion": 1, "kind": "council-attempt",
                                    "runId": "run-" + "e" * 32}))
        result = self.recover(spec, resume=True)
        self.assertEqual(result["status"], "completed")
        self.assertIn("ledger", result["reusedSteps"])
        rows = [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2]["blindSeat"]["brief"], spec["replacementBrief"]["destinationPath"])
        self.assertEqual([r["status"] for r in self.audit_records(spec)], ["prepared", "completed"])

    def test_resume_refuses_a_completed_audit_over_an_unrepaired_ledger(self):
        spec = self.plan()
        self.recover(spec)
        backup = self.artifact_dir(spec) / f"{self.ledger.name}.{spec['ledger']['expectedSha256']}.backup"
        self.ledger.write_bytes(backup.read_bytes())
        destination = self._mirror(spec["replacementBrief"]["destinationPath"])
        destination.chmod(0o600)
        destination.unlink()
        with self.assertRaisesRegex(LedgerError, "records a completion but the ledger is at the before image"):
            self.recover(spec, resume=True)

    # ---- prepare-brief -------------------------------------------------

    def test_prepare_brief_creates_one_immutable_run_scoped_file(self):
        destination = f"{BRIEFS}/2026-08-23-topic-{E8}.md"
        result = prepare_blind_brief(
            run_id=E8,
            source_path=self.source,
            destination_path=self._mirror(destination),
            expected_sha256=brief_recovery._digest(self.source.read_bytes()),
        )
        self.assertEqual(result["status"], "created")
        created = self._mirror(destination)
        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o444)
        with self.assertRaisesRegex(LedgerError, "destination already exists"):
            prepare_blind_brief(
                run_id=E8,
                source_path=self.source,
                destination_path=self._mirror(destination),
                expected_sha256=brief_recovery._digest(self.source.read_bytes()),
            )

    def test_prepare_brief_refuses_a_path_without_the_run_id(self):
        with self.assertRaisesRegex(LedgerError, "exact runId"):
            prepare_blind_brief(
                run_id=E8,
                source_path=self.source,
                destination_path=self._mirror(f"{BRIEFS}/2026-08-23-topic.md"),
                expected_sha256=brief_recovery._digest(self.source.read_bytes()),
            )

    def test_prepare_brief_refuses_source_drift(self):
        with self.assertRaisesRegex(LedgerError, "SHA-256 drift"):
            prepare_blind_brief(
                run_id=E8,
                source_path=self.source,
                destination_path=self._mirror(f"{BRIEFS}/2026-08-23-topic-{E8}.md"),
                expected_sha256="0" * 64,
            )


class BlindBriefIdentityGuardTest(unittest.TestCase):
    def row(self, run_id, seat):
        return {"schemaVersion": 1, "kind": "council", "runId": run_id, "blindSeat": seat}

    def test_a_seat_that_ran_must_name_its_brief(self):
        row = self.row(E8, {"ran": True, "required": True})
        with self.assertRaisesRegex(Exception, "requires a blind brief path"):
            validate_blind_brief_identity(row, [])

    def test_a_skipped_seat_must_still_name_the_brief_it_was_given(self):
        # The deployed kill criterion requires a brief on every explicit-run-state
        # row. Accepting a briefless one here would append a line it then rejects.
        row = self.row(E8, {"ran": False, "required": False, "role": "SKIPPED"})
        with self.assertRaisesRegex(Exception, "requires a blind brief path"):
            validate_blind_brief_identity(row, [])

    def test_a_pre_contract_row_without_a_ran_key_is_left_alone(self):
        row = {"schemaVersion": 1, "kind": "council", "runId": E8, "blindSeat": {"role": "generic"}}
        validate_blind_brief_identity(row, [])

    def test_a_malformed_run_id_raises_a_typed_error_not_a_key_error(self):
        row = {"schemaVersion": 1, "kind": "council", "blindSeat": {"ran": True, "brief": "/x.md"}}
        with self.assertRaisesRegex(Exception, "well-formed runId"):
            validate_blind_brief_identity(row, [])

    def test_a_skipped_seat_that_names_a_brief_is_still_validated(self):
        row = self.row(E8, {"ran": False, "required": False, "brief": f"{BRIEFS}/x-{DDA}.md"})
        with self.assertRaisesRegex(Exception, "must contain its exact runId"):
            validate_blind_brief_identity(row, [])

    def test_a_path_another_council_already_owns_is_rejected(self):
        owned = f"{BRIEFS}/x-{E8}.md"
        prior = self.row(DDA, {"ran": True, "brief": owned})
        row = self.row(E8, {"ran": True, "brief": owned})
        with self.assertRaisesRegex(Exception, "already belongs to another council"):
            validate_blind_brief_identity(row, [prior])

    def test_the_incident_shape_is_now_rejected_at_append(self):
        prior = self.row(DDA, {"ran": True, "brief": SHARED_BRIEF})
        row = self.row(E8, {"ran": True, "brief": SHARED_BRIEF})
        with self.assertRaisesRegex(Exception, "must contain its exact runId"):
            validate_blind_brief_identity(row, [prior])

    def test_a_run_scoped_unique_brief_is_accepted(self):
        prior = self.row(DDA, {"ran": True, "brief": f"{BRIEFS}/x-{DDA}.md"})
        row = self.row(E8, {"ran": True, "brief": f"{BRIEFS}/x-{E8}.md"})
        validate_blind_brief_identity(row, [prior])

    def test_a_relative_brief_path_is_rejected(self):
        row = self.row(E8, {"ran": True, "brief": f"~/knowledge/x-{E8}.md"})
        with self.assertRaisesRegex(Exception, "absolute and normalized"):
            validate_blind_brief_identity(row, [])


class ValidatorAgreementTest(unittest.TestCase):
    """The append rule must not admit a row the deployed kill criterion rejects.

    The 2026-08-23 outage was one ledger line that the reader refused, halting
    every later council. A row this validator accepts and that one rejects is
    the same outage through a different door.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "ledger.jsonl"

    def criterion_errors(self, rows):
        self.log.write_text(
            "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
            encoding="utf-8",
        )
        out = __import__("subprocess").run(
            [
                "/usr/bin/python3.11",
                "/home/trader/.claude/knowledge/council-eval/blind_seat_kill_criterion.py",
                "--log",
                str(self.log),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        return json.loads(out.stdout)["errors"]

    def seat_shapes(self):
        base = {"required": True, "changedDecision": False, "role": "generic"}
        return [
            ("ran-true-with-brief", {**base, "ran": True, "brief": f"{BRIEFS}/a-{E8}.md"}),
            ("ran-false-with-brief", {**base, "ran": False, "changedDecision": None,
                                      "role": "SKIPPED", "blockedReason": "launcher failed",
                                      "brief": f"{BRIEFS}/b-{E8}.md"}),
            ("ran-false-no-brief", {**base, "ran": False, "changedDecision": None,
                                    "role": "SKIPPED", "blockedReason": "launcher failed"}),
            ("ran-true-no-brief", {**base, "ran": True}),
        ]

    def test_neither_validator_admits_what_the_other_refuses(self):
        for name, seat in self.seat_shapes():
            with self.subTest(shape=name):
                row = {"schemaVersion": 1, "kind": "council", "runId": E8,
                       "ts": "2026-08-23T18:00:00Z", "blindSeat": seat}
                try:
                    validate_blind_brief_identity(row, [])
                    appended = True
                except Exception:
                    appended = False
                accepted_by_criterion = not self.criterion_errors([row])
                self.assertEqual(
                    appended,
                    accepted_by_criterion,
                    f"{name}: append-rule={appended} kill-criterion={accepted_by_criterion}",
                )


if __name__ == "__main__":
    unittest.main()
