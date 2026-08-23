import fcntl
import hashlib
import json
import tempfile
import threading
import unittest
import os
from datetime import date
from pathlib import Path
from unittest import mock

from council_tools import forecasts
from council_tools import safe_files
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

    def test_duplicate_keys_and_nonfinite_json_fail_closed(self):
        for payload in (
            '{"kind":"council","kind":"council-attempt"}\n',
            '{"kind":"council","cost":NaN}\n',
            '{"kind":"council","cost":1e999}\n',
        ):
            with self.subTest(payload=payload):
                self.log.write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(LedgerError, "invalid JSON"):
                    forecasts.load_jsonl(self.log)

    def test_excessively_nested_jsonl_is_one_generic_ledger_parse_error(self):
        deeply_nested = b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"
        self.log.write_bytes(deeply_nested)

        with self.assertRaisesRegex(
            LedgerError, f"^{self.log.name} line 1: invalid JSON$"
        ) as raised:
            forecasts.load_jsonl(self.log)

        self.assertNotIn("recursion", str(raised.exception).lower())

    def test_raw_identity_hashes_exact_physical_record_bytes(self):
        compact = b'{"a":1,"b":2}\n'
        spaced = b'{ "a": 1, "b": 2 }\r\n'
        self.log.write_bytes(compact + spaced)

        loaded = forecasts.load_jsonl_with_raw_identity(self.log)

        self.assertEqual([item[0] for item in loaded], [1, 2])
        self.assertEqual(loaded[0][1], loaded[1][1])
        self.assertNotEqual(loaded[0][2], loaded[1][2])
        self.assertEqual(loaded[0][2], hashlib.sha256(compact).hexdigest())
        self.assertEqual(loaded[1][2], hashlib.sha256(spaced).hexdigest())
        self.assertEqual(
            forecasts.load_jsonl(self.log),
            [(1, {"a": 1, "b": 2}), (2, {"a": 1, "b": 2})],
        )

    def test_nested_nonfinite_metadata_fails_check_only_and_append_without_mutation(self):
        a = attempt()
        append_ledger_row(self.log, a)
        before = self.log.read_bytes()
        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(nonfinite=nonfinite):
                row = completion(a)
                row["verdicts"]["ops"] = {
                    "arbitrary": {"nested": ["otherwise-valid", nonfinite]}
                }
                with self.assertRaisesRegex(LedgerError, "strict JSON"):
                    forecasts.validate_ledger_row(row, [a])
                with self.assertRaisesRegex(LedgerError, "strict JSON"):
                    append_ledger_row(self.log, row)
                self.assertEqual(self.log.read_bytes(), before)

    def test_first_append_fsyncs_each_new_ledger_parent(self):
        nested_log = self.log.parent / "new" / "nested" / "panel.jsonl"
        expected = {nested_log.parent.parent, nested_log.parent}
        fsynced = []
        real_fsync_directory = forecasts._fsync_directory

        def record_fsync(path):
            fsynced.append(Path(path))
            real_fsync_directory(path)

        with mock.patch.object(
            forecasts, "_fsync_directory", side_effect=record_fsync
        ):
            append_ledger_row(nested_log, attempt())

        self.assertTrue(expected.issubset(set(fsynced)))
        self.assertEqual(len(forecasts.load_jsonl(nested_log)), 1)

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
            outcome_fingerprint=first["sharedOutcome"]["fingerprint"],
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

    def test_replaced_lock_after_validation_aborts_first_writer_and_serializes_second(self):
        row = attempt()
        lock_path = forecasts.derived_ledger_lock_path(self.log)
        retired_lock = lock_path.with_name(f"{lock_path.name}.retired")
        first_validated = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_reached_append = threading.Event()
        errors: dict[str, BaseException] = {}
        real_append = forecasts._append_jsonl_line

        def pause_after_validation(transaction, value):
            if threading.current_thread().name == "writer-a":
                first_validated.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("test did not release first writer")
            else:
                second_reached_append.set()
            return real_append(transaction, value)

        def writer(name):
            if name == "b":
                second_started.set()
            try:
                append_ledger_row(self.log, row)
            except BaseException as exc:
                errors[name] = exc

        with mock.patch.object(
            forecasts,
            "_append_jsonl_line",
            side_effect=pause_after_validation,
        ):
            first = threading.Thread(target=writer, args=("a",), name="writer-a")
            first.start()
            self.assertTrue(first_validated.wait(timeout=5))

            lock_path.rename(retired_lock)
            lock_path.write_bytes(b"")
            lock_path.chmod(0o600)

            second = threading.Thread(target=writer, args=("b",), name="writer-b")
            second.start()
            self.assertTrue(second_started.wait(timeout=5))
            self.assertFalse(second_reached_append.wait(timeout=0.1))

            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertRegex(str(errors.get("a")), "transaction lock identity changed")
        self.assertNotIn("b", errors)
        self.assertEqual(
            [item["runId"] for _, item in forecasts.load_jsonl(self.log)],
            [row["runId"]],
        )

    def test_replaced_coordination_lock_cannot_create_overlapping_successful_sections(self):
        coordination = self.log.parent / "evidence.lock"
        retired = coordination.with_name("evidence.lock.retired")
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        successes: list[str] = []
        errors: dict[str, BaseException] = {}

        def writer(name):
            if name == "b":
                second_started.set()
            try:
                with forecasts.evidence_write_lock(coordination):
                    if name == "a":
                        first_entered.set()
                        if not release_first.wait(timeout=5):
                            raise AssertionError("test did not release first writer")
                    else:
                        second_entered.set()
                successes.append(name)
            except BaseException as exc:
                errors[name] = exc

        first = threading.Thread(target=writer, args=("a",), name="coordination-a")
        first.start()
        self.assertTrue(first_entered.wait(timeout=5))

        coordination.rename(retired)
        coordination.write_bytes(b"")
        coordination.chmod(0o600)

        second = threading.Thread(target=writer, args=("b",), name="coordination-b")
        second.start()
        self.assertTrue(second_started.wait(timeout=5))
        self.assertFalse(second_entered.wait(timeout=0.1))

        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(successes, ["b"])
        self.assertRegex(str(errors.get("a")), "transaction lock identity changed")
        self.assertNotIn("b", errors)
        self.assertTrue(second_entered.is_set())

    def test_append_times_out_instead_of_hanging_on_held_lock(self):
        lock_path = self.log.with_name(f"{self.log.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as held_lock:
            fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX)
            with mock.patch.object(forecasts, "LOCK_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(LedgerError, "timed out"):
                    append_ledger_row(self.log, attempt())

    def test_derived_ledger_lock_rejects_symlink_and_dangling_symlink(self):
        lock_path = forecasts.derived_ledger_lock_path(self.log)
        for case, target in (
            ("existing", self.log.parent / "lock-target"),
            ("dangling", self.log.parent / "missing-lock-target"),
        ):
            with self.subTest(case=case):
                if target.name == "lock-target":
                    target.write_text("unchanged", encoding="utf-8")
                lock_path.symlink_to(target)
                try:
                    with self.assertRaisesRegex(LedgerError, "unsafe file identity"):
                        append_ledger_row(self.log, attempt())
                    self.assertFalse(self.log.exists())
                    if target.exists():
                        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
                finally:
                    lock_path.unlink()

    def test_derived_ledger_lock_rejects_special_file_and_hardlink(self):
        lock_path = forecasts.derived_ledger_lock_path(self.log)
        os.mkfifo(lock_path)
        with self.assertRaisesRegex(LedgerError, "unsafe file identity"):
            append_ledger_row(self.log, attempt())
        lock_path.unlink()

        target = self.log.parent / "lock-target"
        target.write_text("unchanged", encoding="utf-8")
        os.link(target, lock_path)
        with self.assertRaisesRegex(LedgerError, "unsafe file identity"):
            append_ledger_row(self.log, attempt())
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_append_rejects_existing_ledger_hardlink_alias(self):
        canonical = self.log.parent / "canonical-live-ledger.jsonl"
        canonical.write_bytes(b"")
        os.link(canonical, self.log)

        with self.assertRaisesRegex(LedgerError, "hardlink alias"):
            append_ledger_row(self.log, attempt())

        self.assertEqual(canonical.read_bytes(), b"")
        self.assertEqual(self.log.read_bytes(), b"")
        self.assertEqual(canonical.stat().st_nlink, 2)

    def test_append_rejects_symlinked_parent_before_ledger_mutation(self):
        live = self.log.parent / "simulated-live"
        live.mkdir()
        outside = self.log.parent / "outside"
        outside.symlink_to(live, target_is_directory=True)
        redirected = outside / "panel.jsonl"

        with self.assertRaisesRegex(LedgerError, "unsafe parent component"):
            append_ledger_row(redirected, attempt())

        self.assertFalse((live / "panel.jsonl").exists())
        self.assertFalse((live / "panel.jsonl.lock").exists())

    def test_parent_rename_after_lock_keeps_validation_and_append_on_pinned_parent(self):
        store = self.log.parent / "store"
        store.mkdir()
        ledger = store / "panel.jsonl"
        displaced = self.log.parent / "store-before-rename"
        a = attempt()
        append_ledger_row(ledger, a)
        real_read = safe_files.PinnedFileTransaction.read_bytes
        swapped = False

        def rename_before_transaction_read(transaction, *, missing_ok=False):
            nonlocal swapped
            if transaction.path.name == ledger.name and not swapped:
                swapped = True
                store.rename(displaced)
                store.mkdir()
            return real_read(transaction, missing_ok=missing_ok)

        with mock.patch.object(
            safe_files.PinnedFileTransaction,
            "read_bytes",
            autospec=True,
            side_effect=rename_before_transaction_read,
        ):
            append_ledger_row(ledger, completion(a))

        self.assertTrue(swapped)
        self.assertEqual(len(forecasts.load_jsonl(displaced / ledger.name)), 2)
        self.assertTrue((displaced / f"{ledger.name}.lock").is_file())
        self.assertFalse((store / ledger.name).exists())
        self.assertFalse((store / f"{ledger.name}.lock").exists())

    def test_pinned_mutation_authority_blocks_live_ancestry_before_lock_or_row(self):
        protected_root = self.log.parent / "protected-live"
        protected_parent = protected_root / "store"
        protected_parent.mkdir(parents=True)
        protected_identity = safe_files.capture_directory_identity(protected_root)
        ledger = protected_parent / "panel.jsonl"

        def reject_protected(target):
            if target.parent_is_within(protected_identity):
                raise safe_files.SafeFileError("protected mutation parent")

        with self.assertRaisesRegex(
            safe_files.SafeFileError,
            "protected mutation parent",
        ):
            with safe_files.locked_file_transaction(
                ledger,
                timeout_seconds=1,
                authorize_mutation=reject_protected,
            ):
                self.fail("protected transaction unexpectedly entered")

        self.assertFalse(ledger.exists())
        self.assertFalse(ledger.with_name(f"{ledger.name}.lock").exists())

    def test_pinned_mutation_authority_rechecks_ancestry_after_parent_rename(self):
        protected_root = self.log.parent / "protected-live"
        protected_root.mkdir()
        protected_identity = safe_files.capture_directory_identity(protected_root)
        initially_safe = self.log.parent / "initially-safe"
        initially_safe.mkdir()
        ledger = initially_safe / "panel.jsonl"
        moved_parent = protected_root / "moved-store"

        def reject_protected(target):
            if target.parent_is_within(protected_identity):
                raise safe_files.SafeFileError("protected mutation parent")

        with safe_files.locked_file_transaction(
            ledger,
            timeout_seconds=1,
            authorize_mutation=reject_protected,
        ) as transaction:
            initially_safe.rename(moved_parent)
            with self.assertRaisesRegex(
                safe_files.SafeFileError,
                "protected mutation parent",
            ):
                transaction.atomic_append_bytes(
                    b'{"kind":"must-not-write"}\n',
                    require_trailing_newline=True,
                )

        self.assertFalse((moved_parent / ledger.name).exists())
        self.assertTrue((moved_parent / f"{ledger.name}.lock").is_file())

    def test_safe_atomic_append_rejects_hardlinks_and_preserves_normal_atomic_append(self):
        target = self.log.parent / "atomic.jsonl"
        safe_files.atomic_append_bytes(
            target,
            b'{"first":1}\n',
            require_trailing_newline=True,
        )
        safe_files.atomic_append_bytes(
            target,
            b'{"second":2}\n',
            require_trailing_newline=True,
        )
        self.assertEqual(
            target.read_bytes(),
            b'{"first":1}\n{"second":2}\n',
        )

        alias = self.log.parent / "atomic-alias.jsonl"
        os.link(target, alias)
        with self.assertRaisesRegex(safe_files.SafeFileError, "hardlink alias"):
            safe_files.atomic_append_bytes(
                alias,
                b'{"third":3}\n',
                require_trailing_newline=True,
            )
        self.assertEqual(
            target.read_bytes(),
            b'{"first":1}\n{"second":2}\n',
        )

    def test_low_level_jsonl_append_returns_every_retained_escrow_receipt(self):
        target = self.log.parent / "receipt-ledger.jsonl"
        receipts = []
        with forecasts.ledger_write_transaction(target) as transaction:
            receipts.append(
                forecasts.atomic_append_transaction_jsonl(
                    transaction, b'{"generation":1}\n'
                )
            )
            receipts.append(
                forecasts.atomic_append_transaction_jsonl(
                    transaction, b'{"generation":2}\n'
                )
            )
            receipts.append(
                forecasts.atomic_append_transaction_jsonl(
                    transaction, b'{"generation":3}\n'
                )
            )

        inventory = forecasts.transaction_escrow_inventory(target)
        self.assertIsNone(receipts[0])
        self.assertEqual(
            {str(item) for item in receipts[1:]},
            {entry["path"] for entry in inventory["entries"]},
        )
        self.assertEqual(inventory["count"], 2)
        self.assertEqual(
            inventory["aggregateBytes"],
            len(b'{"generation":1}\n')
            + len(b'{"generation":1}\n{"generation":2}\n'),
        )

    def test_safe_atomic_replace_rejects_parent_symlink(self):
        destination = self.log.parent / "destination"
        destination.mkdir()
        alias = self.log.parent / "destination-alias"
        alias.symlink_to(destination, target_is_directory=True)

        with self.assertRaisesRegex(safe_files.SafeFileError, "unsafe parent component"):
            safe_files.atomic_append_bytes(
                alias / "capture.jsonl",
                b'{}\n',
                require_trailing_newline=True,
            )
        self.assertFalse((destination / "capture.jsonl").exists())

    def test_pinned_transaction_sibling_reads_and_appends_under_original_lock(self):
        events = self.log.with_name("capture-events.jsonl")
        encoded = b'{"kind":"sibling-test"}\n'

        with forecasts.ledger_write_transaction(self.log) as transaction:
            sibling = transaction.sibling(events.name)
            forecasts.atomic_append_transaction_jsonl(sibling, encoded)
            self.assertEqual(sibling.read_bytes(), encoded)
            for unsafe_name in (".", "..", "nested/events.jsonl"):
                with self.subTest(unsafe_name=unsafe_name):
                    with self.assertRaisesRegex(
                        safe_files.SafeFileError, "one plain path component"
                    ):
                        transaction.sibling(unsafe_name)

        self.assertEqual(events.read_bytes(), encoded)

    def test_pinned_transaction_sibling_rejects_replaced_original_lock(self):
        events = self.log.with_name("capture-events.jsonl")
        lock_path = forecasts.derived_ledger_lock_path(self.log)
        retired = lock_path.with_name(f"{lock_path.name}.retired")

        with forecasts.ledger_write_transaction(self.log) as transaction:
            sibling = transaction.sibling(events.name)
            lock_path.rename(retired)
            lock_path.write_bytes(b"")
            lock_path.chmod(0o600)
            with self.assertRaisesRegex(
                LedgerError, "transaction lock identity changed"
            ):
                forecasts.atomic_append_transaction_jsonl(sibling, b'{}\n')

        self.assertFalse(events.exists())

    def test_append_resolution_accepts_same_parent_caller_owned_transaction(self):
        events = self.log.with_name("capture-events.jsonl")
        outcome_id = new_id("outcome")

        with forecasts.ledger_write_transaction(self.log) as log_transaction:
            events_transaction = log_transaction.sibling(events.name)
            event = append_resolution(
                events,
                outcome_id=outcome_id,
                resolution_date="2026-09-30",
                outcome_fingerprint="a" * 64,
                came_true=True,
                evidence="same-parent transaction test",
                resolver="operator",
                resolved_at="2026-10-01T12:00:00Z",
                method="deterministic",
                coordination_lock=None,
                _transaction=events_transaction,
            )

        self.assertEqual(event["outcomeId"], outcome_id)
        self.assertEqual(
            forecasts.load_jsonl(events)[0][1]["resolutionId"],
            event["resolutionId"],
        )

    def test_resolution_binds_to_outcome_not_prediction_position(self):
        a = attempt()
        row = completion(a)
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, row)
        event = append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
            came_true=False,
            evidence="audit:row-set.json",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )
        self.assertEqual(
            event["outcomeFingerprint"], a["sharedOutcome"]["fingerprint"]
        )
        result = audit(self.log, self.events, today=date(2026, 10, 1))
        self.assertEqual(
            result["outcomeFingerprints"][a["sharedOutcome"]["outcomeId"]],
            a["sharedOutcome"]["fingerprint"],
        )
        scores = {item["seat"]: item for item in result["seatScores"]}
        self.assertAlmostEqual(scores["code"]["brier"], 0.49)
        self.assertAlmostEqual(scores["theory"]["brier"], 0.25)
        self.assertEqual(scores["code"]["constantFiftyBrier"], 0.25)
        self.assertEqual(scores["code"]["inSampleBaseRateBrier"], 0.0)

    def test_resolution_integrity_rejects_wrong_fingerprint(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            outcome_fingerprint="f" * 64,
            came_true=True,
            evidence="syntactically valid but misbound resolution",
            resolver="operator",
            resolved_at="2026-10-02T12:00:00Z",
            method="deterministic",
        )

        result = audit(self.log, self.events, today=date(2026, 10, 1))

        self.assertEqual(result["resolvedOutcomes"], 0)
        self.assertTrue(
            any("outcomeFingerprint differs" in item for item in result["invalidRecords"])
        )

    def test_resolution_integrity_rejects_future_report_time(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
            came_true=True,
            evidence="future event already present in sidecar",
            resolver="operator",
            resolved_at="2026-10-02T12:00:00Z",
            method="deterministic",
        )

        result = audit(self.log, self.events, today=date(2026, 10, 1))

        self.assertEqual(result["resolvedOutcomes"], 0)
        self.assertTrue(
            any("follows report as_of" in item for item in result["invalidRecords"])
        )

    def test_resolution_integrity_rejects_changed_resolution_date(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date="2026-09-29",
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
            came_true=True,
            evidence="event with rewritten deadline",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )

        result = audit(self.log, self.events, today=date(2026, 10, 1))

        self.assertEqual(result["resolvedOutcomes"], 0)
        self.assertTrue(
            any("resolutionDate differs" in item for item in result["invalidRecords"])
        )

    def test_legacy_resolution_without_fingerprint_remains_readable(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        event = append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
            came_true=True,
            evidence="legacy retained event",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )
        event.pop("outcomeFingerprint")
        self.events.write_text(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        result = audit(self.log, self.events, today=date(2026, 10, 1))

        self.assertEqual(result["invalidRecords"], [])
        self.assertEqual(result["resolvedOutcomes"], 1)

    def test_resolution_event_schema_rejects_unknown_fields(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        event = append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
            came_true=True,
            evidence="valid event before schema corruption",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )
        event["operatorTimeTravel"] = True
        self.events.write_text(json.dumps(event) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(LedgerError, "exact resolution schema"):
            audit(self.log, self.events, today=date(2026, 10, 1))

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
            outcome_fingerprint=first["sharedOutcome"]["fingerprint"],
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
                outcome_fingerprint="a" * 64,
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
                outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
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
                outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
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
                outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
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
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
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
        self.assertEqual(result["voidRateOfEligibleOutcomes"], 1.0)
        self.assertEqual(sum(item["n"] for item in result["seatScores"]), 0)

    def test_resolution_closed_fields_reject_non_text_without_appending(self):
        malformed_values = ({"nested": "value"}, ["value"], True, 7)
        for field in ("method", "void_reason"):
            for index, value in enumerate(malformed_values):
                with self.subTest(field=field, value_type=type(value).__name__):
                    target = self.log.parent / f"{field}-{index}.jsonl"
                    target.write_bytes(b"")
                    kwargs = {
                        "outcome_id": new_id("outcome"),
                        "resolution_date": "2026-09-30",
                        "outcome_fingerprint": "a" * 64,
                        "came_true": True,
                        "evidence": "retained evidence",
                        "resolver": "operator",
                        "resolved_at": "2026-10-01T12:00:00Z",
                        "method": "deterministic",
                    }
                    kwargs[field] = value
                    with self.assertRaisesRegex(
                        LedgerError,
                        "method must be non-empty text"
                        if field == "method"
                        else "voidReason must be non-empty text",
                    ):
                        append_resolution(target, **kwargs)
                    self.assertEqual(target.read_bytes(), b"")

    def test_loaded_resolution_closed_fields_reject_non_text_as_domain_errors(self):
        event = append_resolution(
            self.events,
            outcome_id=new_id("outcome"),
            resolution_date="2026-09-30",
            outcome_fingerprint="a" * 64,
            came_true=True,
            evidence="retained evidence",
            resolver="operator",
            resolved_at="2026-10-01T12:00:00Z",
            method="deterministic",
        )
        for field in ("method", "status", "voidReason"):
            for value in ({"nested": "value"}, ["value"], True, 7):
                with self.subTest(field=field, value_type=type(value).__name__):
                    malformed = dict(event)
                    malformed[field] = value
                    self.events.write_text(json.dumps(malformed) + "\n", encoding="utf-8")
                    with self.assertRaisesRegex(
                        LedgerError, f"{field} must be non-empty text"
                    ):
                        audit(self.log, self.events, today=date(2026, 10, 1))

    def test_early_void_rate_uses_void_eligible_denominator(self):
        a = attempt(resolution_date="2026-12-31")
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
            came_true=None,
            evidence="decision cancelled before deadline",
            resolver="operator",
            reviewer="independent-reviewer",
            resolved_at="2026-09-01T12:00:00Z",
            method="manual-reviewed",
            void_reason="cancelled-decision",
        )
        result = audit(self.log, self.events, today=date(2026, 9, 1))
        self.assertEqual(result["eligibleDueOutcomes"], 0)
        self.assertEqual(result["voidOutcomes"], 1)
        self.assertEqual(result["voidRateOfEligibleOutcomes"], 1.0)

    def test_resolution_correction_must_supersede_latest(self):
        a = attempt()
        append_ledger_row(self.log, a)
        append_ledger_row(self.log, completion(a))
        first = append_resolution(
            self.events,
            outcome_id=a["sharedOutcome"]["outcomeId"],
            resolution_date=a["sharedOutcome"]["resolutionDate"],
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
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
                outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
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
            outcome_fingerprint=a["sharedOutcome"]["fingerprint"],
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
        with self.assertRaisesRegex(LedgerError, "outcome fingerprint"):
            append_ledger_row(self.log, second)
        second = attempt(
            related_outcome_ids=[first["sharedOutcome"]["outcomeId"]]
        )
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
            created_at="2026-10-01T23:00:00-04:00",
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
