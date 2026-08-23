import fcntl
import json
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from council_tools import forecasts
from council_tools.forecasts import (
    LedgerError,
    append_ledger_row,
    append_override,
    append_resolution,
    audit,
    brier_score,
    make_attempt,
    new_id,
    outcome_fingerprint,
    repair_trailing_jsonl,
)


NOW = "2026-08-22T12:00:00Z"


def completion(attempt, probabilities=None, *, run_id=None, prediction_ids=None):
    probabilities = probabilities or {
        "code": 70,
        "theory": 50,
        "ops": 30,
        "blind": 60,
    }
    prediction_ids = prediction_ids or {}
    outcome = attempt["sharedOutcome"]
    predictions = []
    for seat, probability in probabilities.items():
        predictions.append(
            {
                "predictionId": prediction_ids.get(seat, new_id("prediction")),
                "outcomeId": outcome["outcomeId"],
                "seat": seat,
                "type": "shared",
                "claim": outcome["claim"],
                "probability": probability,
                "issuedAt": "2026-08-22T12:05:00Z",
                "resolutionDate": outcome["resolutionDate"],
                "resolvedBy": outcome["resolvedBy"],
            }
        )
    return {
        "schemaVersion": 1,
        "kind": "council",
        "runId": run_id or attempt["runId"],
        "ts": "2026-08-22T12:10:00Z",
        "question": attempt["question"],
        "verdicts": {"code": "APPROVE", "theory": "APPROVE", "ops": "APPROVE"},
        "blindSeat": {
            "role": "generic",
            "required": True,
            "ran": True,
            "changedDecision": False,
        },
        "forecastState": {
            "sealed": True,
            "seats": {seat: "submitted" for seat in probabilities},
        },
        "predictions": predictions,
    }


def attempt(**overrides):
    values = dict(
        question="Should the forecast scorer be activated?",
        expected_seats=["code", "theory", "ops", "blind"],
        claim="The first ten completed council rows have complete forecast sets",
        resolution_date="2026-09-30",
        resolved_by="Audit the first ten post-activation council rows",
        decision_link="Activation of council forecast scoring",
        materiality="Incomplete emission makes calibration results selected and unusable",
        action_if_true="Keep forecast collection active",
        action_if_false="Disable scoring claims and repair emission",
        evidence_cutoff_at=NOW,
        ts=NOW,
    )
    values.update(overrides)
    return make_attempt(**values)


class ForecastLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.log = root / "panel.jsonl"
        self.events = root / "events.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def test_zero_probability_is_not_treated_as_missing(self):
        self.assertEqual(brier_score(0, False), 0.0)
        self.assertEqual(brier_score(0, True), 1.0)

    def test_fifty_percent_is_valid(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        result = audit(self.log, self.events, today=date(2026, 10, 1))
        self.assertEqual(result["invalidRecords"], [])

    def test_unavailable_seat_is_accounted_for_without_a_probability(self):
        a = attempt()
        row = completion(a, probabilities={"theory": 50, "ops": 30, "blind": 60})
        row["forecastState"]["seats"]["code"] = "unavailable"
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, row)
        result = audit(self.log, self.events, today=date(2026, 8, 22))
        self.assertEqual(result["completeForecastRows"], 1)
        self.assertEqual(result["missingForecastSeats"], [])
        self.assertEqual(result["seatEmissionStates"]["code"]["unavailable"], 1)
        self.assertEqual(result["forecastIssuances"], 3)

    def test_malformed_json_fails_closed(self):
        self.log.write_text('{"kind":"council"}\nnot-json\n', encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "line 2"):
            audit(self.log, self.events, today=date(2026, 10, 1))

    def test_outcome_requires_decision_linkage(self):
        with self.assertRaisesRegex(LedgerError, "materiality"):
            attempt(materiality="")

    def test_unknown_seat_fails(self):
        a = attempt()
        row = completion(a)
        row["predictions"][0]["seat"] = "mystery-reviewer"
        append_ledger_row(self.log, a)
        with self.assertRaisesRegex(LedgerError, "unknown seat"):
            append_ledger_row(self.log, row)

    def test_duplicate_prediction_id_is_rejected(self):
        a = attempt()
        duplicate = new_id("prediction")
        row = completion(a, prediction_ids={"code": duplicate, "theory": duplicate})
        append_ledger_row(self.log, a)
        with self.assertRaisesRegex(LedgerError, "duplicate predictionId"):
            append_ledger_row(self.log, row)

    def test_same_second_attempts_get_distinct_ids_and_resolve_independently(self):
        first = attempt(ts=NOW)
        second = attempt(
            ts=NOW,
            claim="No invalid forecast row is appended during the first month",
            resolved_by="Audit post-activation forecast rows after one month",
        )
        self.assertNotEqual(first["runId"], second["runId"])
        self.assertNotEqual(first["sharedOutcome"]["outcomeId"], second["sharedOutcome"]["outcomeId"])
        append_ledger_row(self.log, first)
        append_ledger_row(self.log, completion(first))
        append_ledger_row(self.log, second)
        append_ledger_row(self.log, completion(second))
        append_resolution(
            self.events,
            outcome_id=first["sharedOutcome"]["outcomeId"],
            resolution_date=first["sharedOutcome"]["resolutionDate"],
            came_true=True,
            evidence="audit:first-ten.json",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )
        result = audit(self.log, self.events, today=date(2026, 10, 1))
        self.assertEqual(result["resolvedOutcomes"], 1)
        self.assertEqual(result["unresolvedDueOutcomes"], 1)

    def test_concurrent_attempt_appends_preserve_both_records(self):
        rows = [
            attempt(
                claim=f"Concurrent material outcome {index}",
                resolved_by=f"Inspect concurrent outcome {index}",
            )
            for index in range(2)
        ]
        errors = []

        def write(row):
            try:
                append_ledger_row(self.log, row)
            except Exception as exc:  # captured and asserted in the parent thread
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(row,)) for row in rows]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 2)

    def test_append_times_out_instead_of_hanging_on_held_lock(self):
        lock_path = self.log.with_name(f"{self.log.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as held_lock:
            fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX)
            with mock.patch.object(forecasts, "LOCK_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(LedgerError, "timed out"):
                    append_ledger_row(self.log, attempt())

    def test_resolution_binds_to_outcome_not_prediction_position(self):
        a = attempt()
        row = completion(a)
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, row)
        append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            came_true=False,
            evidence="audit:row-set.json",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )
        result = audit(self.log, self.events, today=date(2026, 10, 1))
        scores = {item["seat"]: item for item in result["seatScores"]}
        self.assertAlmostEqual(scores["code"]["brier"], 0.49)
        self.assertAlmostEqual(scores["theory"]["brier"], 0.25)
        self.assertEqual(scores["code"]["constantFiftyBrier"], 0.25)
        self.assertEqual(scores["code"]["climatologyBrier"], 0.0)

    def test_selective_resolution_marks_scores_incomplete(self):
        first = attempt()
        second = attempt(
            claim="No invalid forecast row is appended during the first month",
            resolved_by="Audit post-activation forecast rows after one month",
        )
        for a in (first, second):
            append_ledger_row(self.log, a)
            append_ledger_row(self.log, completion(a))
        append_resolution(
            self.events,
            outcome_id=first["sharedOutcome"]["outcomeId"],
            resolution_date=first["sharedOutcome"]["resolutionDate"],
            came_true=True,
            evidence="audit:first-ten.json",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )
        result = audit(self.log, self.events, today=date(2026, 10, 20))
        self.assertEqual(result["scoreStatus"], "INCOMPLETE")
        self.assertEqual(result["eligibleDueOutcomes"], 2)
        self.assertEqual(result["resolvedOutcomes"], 1)

    def test_manual_resolution_requires_independent_reviewer(self):
        with self.assertRaisesRegex(LedgerError, "reviewer must differ"):
            append_resolution(
                self.events,
                outcome_id=new_id("outcome"),
                resolution_date="2026-09-30",
                came_true=True,
                evidence="manual observation",
                resolver="operator",
                reviewer="operator",
                resolved_at="2026-10-01T12:00:00Z",
                method="manual-reviewed",
            )

    def test_non_void_resolution_cannot_be_recorded_before_deadline_has_ended(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        with self.assertRaisesRegex(LedgerError, "after resolutionDate"):
            append_resolution(
                self.events,
                outcome_id=a["sharedOutcome"]["outcomeId"],
                resolution_date=a["sharedOutcome"]["resolutionDate"],
                came_true=False,
                evidence="premature absence of evidence",
                resolver="operator",
                resolved_at="2026-09-30T23:59:59-04:00",
                method="deterministic",
            )

    def test_void_reason_is_enumerated_and_excluded_from_score(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        with self.assertRaisesRegex(LedgerError, "voidReason"):
            append_resolution(
                self.events,
                outcome_id=a["sharedOutcome"]["outcomeId"],
                resolution_date=a["sharedOutcome"]["resolutionDate"],
                came_true=None,
                evidence="decision record",
                resolver="operator",
                resolved_at="2026-10-01T12:00:00Z",
                method="deterministic",
                void_reason="conveniently-ungradeable",
            )
        with self.assertRaisesRegex(LedgerError, "manual-reviewed"):
            append_resolution(
                self.events,
                outcome_id=a["sharedOutcome"]["outcomeId"],
                resolution_date=a["sharedOutcome"]["resolutionDate"],
                came_true=None,
                evidence="decision was formally cancelled",
                resolver="operator",
                resolved_at="2026-10-01T12:00:00Z",
                method="deterministic",
                void_reason="cancelled-decision",
            )
        append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            came_true=None,
            evidence="decision was formally cancelled",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="manual-reviewed",
            reviewer="independent-reviewer",
            void_reason="cancelled-decision",
        )
        result = audit(self.log, self.events, today=date(2026, 10, 1))
        self.assertEqual(result["voidOutcomes"], 1)
        self.assertEqual(sum(item["n"] for item in result["seatScores"]), 0)

    def test_resolution_correction_must_supersede_latest(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        first = append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            came_true=False,
            evidence="initial deterministic audit",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )
        with self.assertRaisesRegex(LedgerError, "must supersede"):
            append_resolution(
                self.events,
                outcome_id=a["sharedOutcome"]["outcomeId"],
                resolution_date=a["sharedOutcome"]["resolutionDate"],
                came_true=True,
                evidence="corrected deterministic audit",
                resolver="operator",
                resolved_at="2026-10-01T13:00:00Z",
                method="deterministic",
            )
        append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            came_true=True,
            evidence="corrected deterministic audit",
            resolver="operator",
            resolved_at="2026-10-01T13:00:00Z",
            method="deterministic",
            supersedes_resolution_id=first["resolutionId"],
        )
        result = audit(self.log, self.events, today=date(2026, 10, 1))
        scores = {item["seat"]: item for item in result["seatScores"]}
        self.assertAlmostEqual(scores["code"]["brier"], 0.09)

    def test_completion_rejects_probability_after_resolution_date(self):
        a = attempt(resolution_date="2026-08-23")
        row = completion(a)
        row["ts"] = "2026-08-23T00:01:00Z"
        for prediction in row["predictions"]:
            prediction["issuedAt"] = row["ts"]
        append_ledger_row(self.log, a)
        with self.assertRaisesRegex(LedgerError, "must precede resolutionDate"):
            append_ledger_row(self.log, row)

    def test_exact_open_outcome_reuse_requires_relationship(self):
        first = attempt()
        second = attempt()
        append_ledger_row(self.log, first)
        with self.assertRaisesRegex(LedgerError, "open outcome fingerprint"):
            append_ledger_row(self.log, second)
        second["sharedOutcome"]["relatedOutcomeIds"] = [
            first["sharedOutcome"]["outcomeId"]
        ]
        append_ledger_row(self.log, second)

    def test_three_old_overdue_outcomes_block_finalization_but_not_collection(self):
        for index in range(3):
            a = attempt(
                claim=f"Material outcome {index} occurs",
                resolved_by=f"Inspect material outcome {index}",
                resolution_date="2026-09-01",
            )
            append_ledger_row(self.log, a)
            append_ledger_row(self.log, completion(a))
        result = audit(self.log, self.events, today=date(2026, 10, 1))
        self.assertEqual(result["gradingDebtState"], "BLOCK_FINALIZATION")
        new_attempt = attempt(
            claim="A new council can still be convened",
            resolved_by="Inspect the new council attempt record",
        )
        append_ledger_row(self.log, new_attempt)

    def test_logged_override_preserves_but_overrides_debt_block(self):
        for index in range(3):
            a = attempt(
                claim=f"Material outcome {index} occurs",
                resolved_by=f"Inspect material outcome {index}",
                resolution_date="2026-09-01",
            )
            append_ledger_row(self.log, a)
            append_ledger_row(self.log, completion(a))
        append_override(
            self.events,
            reason="Resolution evidence is unavailable until the scheduled audit",
            operator="principal",
            created_at="2026-10-01T00:00:00Z",
            expires_date="2026-10-02",
        )
        result = audit(self.log, self.events, today=date(2026, 10, 1))
        self.assertEqual(result["gradingDebtState"], "OVERRIDDEN")
        self.assertEqual(result["oldOverdueOutcomes"], 3)

    def test_concurrent_duplicate_override_is_appended_once(self):
        override_id = new_id("override")
        errors = []
        successes = []
        barrier = threading.Barrier(8)

        def write():
            barrier.wait()
            try:
                successes.append(
                    append_override(
                        self.events,
                        reason="Temporary evidence outage",
                        operator="principal",
                        created_at="2026-10-01T00:00:00Z",
                        expires_date="2026-10-02",
                        override_id=override_id,
                    )
                )
            except Exception as exc:  # captured and asserted in the parent thread
                errors.append(exc)

        threads = [threading.Thread(target=write) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(isinstance(item, LedgerError) for item in errors))
        self.assertEqual(len(self.events.read_text(encoding="utf-8").splitlines()), 1)

    def test_repair_tail_quarantines_only_confirmed_invalid_final_line(self):
        first = json.dumps({"kind": "first"}) + "\n"
        original = (first + '{"kind":"truncated"').encode("utf-8")
        self.log.write_bytes(original)
        result = repair_trailing_jsonl(
            self.log,
            expected_line=2,
            backup_dir=self.log.parent / "quarantine",
        )
        self.assertEqual(self.log.read_text(encoding="utf-8"), first)
        self.assertEqual(Path(result["backup"]).read_bytes(), original)
        self.assertEqual(result["removedLine"], 2)

    def test_repair_tail_refuses_mid_file_corruption_without_changes(self):
        original = b'{"kind":"first"}\nnot-json\n{"kind":"last"}\n'
        self.log.write_bytes(original)
        with self.assertRaisesRegex(LedgerError, "not the final nonblank line"):
            repair_trailing_jsonl(
                self.log,
                expected_line=2,
                backup_dir=self.log.parent / "quarantine",
            )
        self.assertEqual(self.log.read_bytes(), original)
        self.assertFalse((self.log.parent / "quarantine").exists())

    def test_repair_tail_requires_exact_operator_confirmed_line(self):
        original = b'{"kind":"first"}\nnot-json\n'
        self.log.write_bytes(original)
        with self.assertRaisesRegex(LedgerError, "does not match"):
            repair_trailing_jsonl(
                self.log,
                expected_line=3,
                backup_dir=self.log.parent / "quarantine",
            )
        self.assertEqual(self.log.read_bytes(), original)

    def test_non_council_predictions_are_excluded_by_default(self):
        self.log.write_text(
            json.dumps(
                {
                    "kind": "pre-mortem",
                    "ts": NOW,
                    "predictions": [
                        {
                            "seat": "ops",
                            "claim": "A pre-mortem claim",
                            "probability": 90,
                            "resolutionDate": "2026-08-23",
                            "resolvedBy": "Inspect something",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = audit(self.log, self.events, today=date(2026, 8, 24))
        self.assertEqual(result["forecastIssuances"], 0)
        self.assertEqual(result["excludedPredictions"], 1)
        self.assertEqual(result["allPredictionStates"]["due"], 1)

    def test_invalid_excluded_prediction_is_classified_legacy_ineligible(self):
        self.log.write_text(
            json.dumps(
                {
                    "kind": "council-calibration",
                    "ts": NOW,
                    "predictions": [
                        {
                            "seat": "pysystemtrade-expert",
                            "claim": "An unresolvable legacy claim",
                            "probability": 70,
                            "resolutionDate": None,
                            "resolvedBy": None,
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = audit(self.log, self.events, today=date(2026, 8, 24))
        self.assertEqual(result["allPredictionStates"]["legacyIneligible"], 1)
        self.assertEqual(result["excludedValidPredictions"], 0)

    def test_fingerprint_is_stable_and_sensitive_to_resolution_rule(self):
        first = outcome_fingerprint(
            "Claim", "2026-09-30", "Inspect A", "Decision A"
        )
        second = outcome_fingerprint(
            "Claim", "2026-09-30", "Inspect B", "Decision A"
        )
        self.assertNotEqual(first, second)

    def test_report_always_preserves_descriptive_only_label(self):
        result = audit(self.log, self.events, today=date(2026, 8, 22))
        self.assertEqual(
            result["label"],
            "DESCRIPTIVE ONLY - normal seat operation has unequal information access; "
            "outcomes may be seat-controlled and are non-independent",
        )


if __name__ == "__main__":
    unittest.main()
