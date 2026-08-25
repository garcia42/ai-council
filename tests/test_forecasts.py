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
    load_jsonl_with_raw_identity,
    make_attempt,
    make_supersede,
    new_id,
    normalize_duplicate_brief,
    outcome_fingerprint,
    repair_trailing_jsonl,
    superseded_lines,
    validate_ledger_row,
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
    completion_run_id = run_id or attempt["runId"]
    return {
        "schemaVersion": 1,
        "kind": "council",
        "runId": completion_run_id,
        "ts": "2026-08-22T12:10:00Z",
        "question": attempt["question"],
        "verdicts": {"code": "APPROVE", "theory": "APPROVE", "ops": "APPROVE"},
        "blindSeat": {
            "role": "generic",
            "required": True,
            "ran": True,
            "changedDecision": False,
            "brief": f"/tmp/council-briefs/brief-{completion_run_id}.md",
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


DUPLICATE_BRIEF = "/tmp/council-briefs/duplicate-{run_id}.md"
DUPLICATE_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures/duplicate-council-row-supersede"
)
SUPERSEDE_ADVERSARIAL_PARITY_CASES = (
    ("boolean schemaVersion", ("schemaVersion",), True),
    ("non-integer schemaVersion", ("schemaVersion",), 1.0),
    (
        "spaced target digest",
        ("supersedes", "rawLineSha256"),
        "surround-with-spaces",
    ),
    (
        "spaced witness digest",
        ("duplicateOf", "rawLineSha256"),
        "surround-with-spaces",
    ),
)
STRICT_JSON_ADVERSARIAL_PARITY_CASES = (
    (
        "duplicate top-level key",
        b'{"kind":"council","kind":"council-superseded"}\n',
        "invalid JSON",
    ),
    (
        "duplicate nested key",
        b'{"kind":"council","blindSeat":{"ran":true,"ran":false}}\n',
        "invalid JSON",
    ),
    ("NaN", b'{"kind":"council","cost":NaN}\n', "invalid JSON"),
    ("Infinity", b'{"kind":"council","cost":Infinity}\n', "invalid JSON"),
    ("negative Infinity", b'{"kind":"council","cost":-Infinity}\n', "invalid JSON"),
    ("overflowed float", b'{"kind":"council","cost":1e400}\n', "invalid JSON"),
    ("non-object root", b'[]\n', "record must be an object"),
    ("invalid UTF-8", b'{"kind":"council","value":"\xff"}\n', "invalid JSON"),
    ("malformed JSON", b'{"kind":"council"\n', "invalid JSON"),
    (
        "excessive nesting",
        b'{"kind":"council","value":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n",
        "invalid JSON",
    ),
)


def apply_supersede_adversarial_case(row, case):
    """Apply one shared appender/reader parity mutation to a valid assertion."""

    _name, path, replacement = case
    parent = row
    for component in path[:-1]:
        parent = parent[component]
    field = path[-1]
    if replacement == "surround-with-spaces":
        replacement = f" {parent[field]} "
    parent[field] = replacement


def hand_appended_duplicate(completion_row):
    """The shape a reviewer produces by logging a council row that was already written.

    It is a council row with no ``forecastState`` and no ``predictions``: the forecasts
    live on the row it duplicates.  Nothing in this repository can write it, which is
    the point -- it arrives by hand, and an append-only store then has to carry it.
    """

    return {
        "kind": "council",
        "runId": completion_row["runId"],
        "ts": "2026-08-22T12:10:17Z",
        "question": completion_row["question"],
        "verdicts": {"code": "APPROVE"},
        "blindSeat": dict(completion_row["blindSeat"]),
    }


class SupersedeRecordTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.log = root / "panel.jsonl"
        self.attempt = attempt()
        self.completion = completion(self.attempt)
        append_ledger_row(self.log, self.attempt)
        append_ledger_row(self.log, self.completion)
        self.original_line, _row, self.original_digest = (
            load_jsonl_with_raw_identity(self.log)[-1]
        )

    def tearDown(self):
        self.temp.cleanup()

    def hand_append(self, row):
        """Append bytes the way a reviewer does: straight to the file, no validator."""

        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        loaded = load_jsonl_with_raw_identity(self.log)
        return loaded[-1][0], loaded[-1][2]

    def supersede(self, line, digest, **overrides):
        values = dict(
            line=line,
            raw_line_sha256=digest,
            duplicate_of_line=self.original_line,
            duplicate_of_raw_line_sha256=self.original_digest,
            reason="Hand-appended duplicate of an earlier council row",
            operator="operator",
            approved_at="2026-08-22T13:00:00Z",
            reference="https://github.com/garcia42/ai-council/issues/25",
            ts="2026-08-22T13:00:00Z",
        )
        values.update(overrides)
        return make_supersede(**values)

    def test_supersede_retires_a_hand_appended_duplicate(self):
        line, digest = self.hand_append(hand_appended_duplicate(self.completion))

        append_ledger_row(self.log, self.supersede(line, digest))

        loaded = load_jsonl_with_raw_identity(self.log)
        self.assertEqual(superseded_lines(loaded), {line: line + 1})
        self.assertEqual(loaded[-1][1]["kind"], "council-superseded")
        self.assertEqual(loaded[line - 1][2], digest)

    def test_supersede_refuses_a_row_carrying_sealed_forecasts(self):
        loaded = load_jsonl_with_raw_identity(self.log)
        line, _row, digest = loaded[1]
        self.assertIsNotNone(_row.get("forecastState"))
        retained_line, retained_digest = self.hand_append(
            hand_appended_duplicate(self.completion)
        )

        with self.assertRaisesRegex(LedgerError, "carries a forecastState"):
            append_ledger_row(
                self.log,
                self.supersede(
                    line,
                    digest,
                    duplicate_of_line=retained_line,
                    duplicate_of_raw_line_sha256=retained_digest,
                ),
            )

    def test_supersede_refuses_a_row_carrying_predictions(self):
        row = hand_appended_duplicate(self.completion)
        row["predictions"] = self.completion["predictions"]
        line, digest = self.hand_append(row)

        with self.assertRaisesRegex(LedgerError, "carries predictions"):
            append_ledger_row(self.log, self.supersede(line, digest))

    def test_supersede_accepts_null_forecast_state_and_an_empty_prediction_list(self):
        row = hand_appended_duplicate(self.completion)
        row["forecastState"] = None
        row["predictions"] = []
        line, digest = self.hand_append(row)

        append_ledger_row(self.log, self.supersede(line, digest))

    def test_supersede_refuses_malformed_predictions_as_nonempty_evidence(self):
        for malformed in (None, {}, "not-a-list"):
            with self.subTest(predictions=malformed):
                row = hand_appended_duplicate(self.completion)
                row["predictions"] = malformed
                line, digest = self.hand_append(row)
                with self.assertRaisesRegex(LedgerError, "carries predictions"):
                    append_ledger_row(self.log, self.supersede(line, digest))

    def test_supersede_refuses_a_digest_that_names_a_different_row(self):
        line, _digest = self.hand_append(hand_appended_duplicate(self.completion))
        other = load_jsonl_with_raw_identity(self.log)[0][2]

        with self.assertRaisesRegex(LedgerError, "does not match line"):
            append_ledger_row(self.log, self.supersede(line, other))

    def test_supersede_refuses_a_line_that_holds_no_row(self):
        _line, digest = self.hand_append(hand_appended_duplicate(self.completion))

        with self.assertRaisesRegex(LedgerError, "names no ledger row"):
            append_ledger_row(self.log, self.supersede(99, digest))

    def test_supersede_refuses_a_record_that_is_not_a_council_row(self):
        loaded = load_jsonl_with_raw_identity(self.log)
        line, _row, digest = loaded[0]

        with self.assertRaisesRegex(LedgerError, "only a council row"):
            append_ledger_row(self.log, self.supersede(line, digest))

    def test_a_line_cannot_be_superseded_twice(self):
        line, digest = self.hand_append(hand_appended_duplicate(self.completion))
        append_ledger_row(self.log, self.supersede(line, digest))

        with self.assertRaisesRegex(LedgerError, "already superseded"):
            append_ledger_row(self.log, self.supersede(line, digest))

    def test_a_supersede_record_cannot_itself_be_superseded(self):
        line, digest = self.hand_append(hand_appended_duplicate(self.completion))
        append_ledger_row(self.log, self.supersede(line, digest))
        loaded = load_jsonl_with_raw_identity(self.log)
        record_line, _row, record_digest = loaded[-1]

        with self.assertRaisesRegex(LedgerError, "only a council row"):
            append_ledger_row(self.log, self.supersede(record_line, record_digest))

    def test_supersede_without_raw_line_identity_is_refused(self):
        loaded = load_jsonl_with_raw_identity(self.log)
        line, _row, digest = loaded[1]
        prior = [row for _line, row, _raw in loaded]

        with self.assertRaisesRegex(LedgerError, "raw line identity"):
            validate_ledger_row(self.supersede(line, digest), prior)

    def test_supersede_shape_is_exact(self):
        line, digest = self.hand_append(hand_appended_duplicate(self.completion))
        loaded = load_jsonl_with_raw_identity(self.log)
        prior = [row for _line, row, _raw in loaded]
        cases = {
            "extra key": lambda row: row.update({"note": "extra"}),
            "missing approval": lambda row: row.pop("approval"),
            "missing duplicateOf": lambda row: row.pop("duplicateOf"),
            "empty reason": lambda row: row.update({"reason": "  "}),
            "empty operator": lambda row: row["approval"].update({"operator": ""}),
            "naive approval time": lambda row: row["approval"].update(
                {"approvedAt": "2026-08-22T13:00:00"}
            ),
            "extra approval key": lambda row: row["approval"].update({"host": "manny"}),
            "short digest": lambda row: row["supersedes"].update(
                {"rawLineSha256": "abc"}
            ),
            "uppercase digest": lambda row: row["supersedes"].update(
                {"rawLineSha256": digest.upper()}
            ),
            "extra supersedes key": lambda row: row["supersedes"].update(
                {"runId": self.attempt["runId"]}
            ),
            "short duplicateOf digest": lambda row: row["duplicateOf"].update(
                {"rawLineSha256": "abc"}
            ),
            "uppercase duplicateOf digest": lambda row: row["duplicateOf"].update(
                {"rawLineSha256": self.original_digest.upper()}
            ),
            "extra duplicateOf key": lambda row: row["duplicateOf"].update(
                {"runId": self.attempt["runId"]}
            ),
            "zero line": lambda row: row["supersedes"].update({"line": 0}),
            "boolean line": lambda row: row["supersedes"].update({"line": True}),
            "zero duplicateOf line": lambda row: row["duplicateOf"].update(
                {"line": 0}
            ),
            "boolean duplicateOf line": lambda row: row["duplicateOf"].update(
                {"line": True}
            ),
            "same target and retained row": lambda row: row["duplicateOf"].update(
                {"line": line, "rawLineSha256": digest}
            ),
            "wrong schema version": lambda row: row.update({"schemaVersion": 2}),
        }
        for name, mutate in cases.items():
            with self.subTest(shape=name):
                row = self.supersede(line, digest)
                mutate(row)
                with self.assertRaises(LedgerError):
                    validate_ledger_row(row, prior, prior_identity=loaded)

    def test_adversarial_parity_cases_never_append_or_retire_a_row(self):
        line, digest = self.hand_append(hand_appended_duplicate(self.completion))
        before = self.log.read_bytes()

        for case in SUPERSEDE_ADVERSARIAL_PARITY_CASES:
            with self.subTest(case=case[0]):
                row = self.supersede(line, digest)
                apply_supersede_adversarial_case(row, case)
                with self.assertRaises(LedgerError):
                    append_ledger_row(self.log, row)
                self.assertEqual(self.log.read_bytes(), before)
                self.assertEqual(
                    superseded_lines(load_jsonl_with_raw_identity(self.log)), {}
                )

    def test_superseded_lines_ignores_a_record_whose_target_drifted(self):
        line, digest = self.hand_append(hand_appended_duplicate(self.completion))
        append_ledger_row(self.log, self.supersede(line, digest))
        loaded = load_jsonl_with_raw_identity(self.log)
        forged = [
            (
                number,
                row,
                "0" * 64 if number == line else raw,
            )
            for number, row, raw in loaded
        ]

        self.assertEqual(superseded_lines(forged), {})

    def test_a_retired_duplicate_still_blocks_a_second_completion_of_its_run(self):
        line, digest = self.hand_append(hand_appended_duplicate(self.completion))
        append_ledger_row(self.log, self.supersede(line, digest))

        with self.assertRaisesRegex(LedgerError, "duplicate council completion"):
            append_ledger_row(self.log, completion(self.attempt))

    def test_matching_identifier_with_a_third_active_owner_is_ambiguous(self):
        target_line, target_digest = self.hand_append(
            hand_appended_duplicate(self.completion)
        )
        self.hand_append(hand_appended_duplicate(self.completion))

        with self.assertRaisesRegex(LedgerError, "unique active retained owner"):
            append_ledger_row(
                self.log, self.supersede(target_line, target_digest)
            )

    def test_later_ordinary_collision_does_not_revise_an_accepted_edge(self):
        target_line, target_digest = self.hand_append(
            hand_appended_duplicate(self.completion)
        )
        append_ledger_row(self.log, self.supersede(target_line, target_digest))
        supersede_line = load_jsonl_with_raw_identity(self.log)[-1][0]

        self.hand_append(hand_appended_duplicate(self.completion))

        self.assertEqual(
            superseded_lines(load_jsonl_with_raw_identity(self.log)),
            {target_line: supersede_line},
        )

    def test_duplicate_brief_normalization_is_pure_lexical_posix(self):
        self.assertEqual(
            normalize_duplicate_brief(
                " \t/fixtures//normalization/./segment/../blind.md\r\n"
            ),
            "/fixtures/normalization/blind.md",
        )
        self.assertEqual(
            normalize_duplicate_brief("//fixtures/$ROOT/~/blind.md/"),
            "/fixtures/$ROOT/~/blind.md",
        )
        for invalid in (
            None,
            "",
            "relative/brief.md",
            "/",
            "///",
            "/..",
            "/segment/../..",
            "/segment/\x00brief.md",
            "\u00a0/brief.md",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(normalize_duplicate_brief(invalid))

    def test_normative_composition_fixtures_drive_appender_replay(self):
        manifest = json.loads(
            (DUPLICATE_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        for scenario in manifest["scenarios"]:
            if "acceptedSupersedes" not in scenario:
                continue
            with self.subTest(fixture=scenario["fixture"]):
                loaded = load_jsonl_with_raw_identity(
                    DUPLICATE_FIXTURE_ROOT / scenario["fixture"]
                )
                prefix = []
                accepted = []
                rejected = []
                for entry in loaded:
                    line_number, row, _digest = entry
                    if row.get("kind") == forecasts.SUPERSEDE_KIND:
                        try:
                            forecasts.validate_supersede(row, prefix)
                        except LedgerError:
                            rejected.append(line_number)
                        else:
                            accepted.append(line_number)
                    prefix.append(entry)
                retired = superseded_lines(loaded)
                all_councils = {
                    line_number
                    for line_number, row, _digest in loaded
                    if row.get("kind") == "council"
                }
                self.assertEqual(accepted, scenario["acceptedSupersedes"])
                self.assertEqual(rejected, scenario["rejectedSupersedes"])
                self.assertEqual(
                    sorted(retired), scenario["retiredCouncilLines"]
                )
                self.assertEqual(
                    sorted(all_councils - set(retired)),
                    scenario["activeCouncilLines"],
                )

    def test_live_shape_fixture_accepts_only_the_two_named_duplicates(self):
        manifest = json.loads(
            (DUPLICATE_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        scenario = manifest["scenarios"][0]
        loaded = load_jsonl_with_raw_identity(
            DUPLICATE_FIXTURE_ROOT / scenario["fixture"]
        )
        by_line = {
            line_number: (row, digest)
            for line_number, row, digest in loaded
        }

        def assertion(target_line, retained_line):
            return make_supersede(
                line=target_line,
                raw_line_sha256=by_line[target_line][1],
                duplicate_of_line=retained_line,
                duplicate_of_raw_line_sha256=by_line[retained_line][1],
                reason="fixture duplicate assertion",
                operator="fixture-operator",
                approved_at="2030-01-01T00:02:00Z",
                reference="fixture://issue-32",
                ts="2030-01-01T00:02:00Z",
            )

        for candidate in scenario["candidateAssertions"]:
            self.assertTrue(candidate["valid"])
            forecasts.validate_supersede(
                assertion(candidate["supersedes"], candidate["duplicateOf"]),
                loaded,
            )

        council_lines = sorted(by_line)
        for target_line in scenario["candidatesWithNoValidDuplicateOf"]:
            for retained_line in council_lines:
                if retained_line == target_line:
                    continue
                with self.subTest(
                    target=target_line, retained=retained_line
                ):
                    with self.assertRaises(LedgerError):
                        forecasts.validate_supersede(
                            assertion(target_line, retained_line), loaded
                        )


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

    def test_strict_json_adversarial_parity_corpus_fails_closed(self):
        for name, payload, expected_error in STRICT_JSON_ADVERSARIAL_PARITY_CASES:
            with self.subTest(case=name):
                self.log.write_bytes(b'{}\n' + payload)
                with self.assertRaisesRegex(
                    LedgerError,
                    rf"^{self.log.name} line 2: {expected_error}$",
                ):
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


#: Sentinel meaning "omit this key entirely", distinct from a null value.
_ABSENT = object()


class BlindSeatContractTest(unittest.TestCase):
    """The blind-seat contract, enforced where the row is written.

    The contract is stated in ``council/SKILL.md`` and ``blind-seat/SKILL.md``:
    a **required** non-run records the ``SKIPPED`` sentinel as its role,
    ``ran: false``, ``null`` for agreement and decision-change, and a non-empty
    ``blockedReason``; a seat that ran must not carry the sentinel; a
    ``required: false`` row carries a non-empty ``notRequiredReason`` and a
    ``required: true`` row must not.

    It matters at the *write* boundary specifically, because nothing downstream
    can undo a bad row: the store is append-only, tail repair removes only a
    line that fails to parse, and brief recovery rewrites only the brief path.
    """

    RUN_ID = "run-0123456789abcdef0123456789abcdef"

    def row(self, **blind_seat):
        seat = {
            "role": "generic",
            "required": True,
            "ran": True,
            "agreedWithPanel": True,
            "changedDecision": False,
            "brief": f"/tmp/council-briefs/brief-{self.RUN_ID}.md",
        }
        seat.update(blind_seat)
        for key, value in list(seat.items()):
            if value is _ABSENT:
                del seat[key]
        return {"runId": self.RUN_ID, "blindSeat": seat}

    def assertAccepted(self, **blind_seat):
        forecasts.validate_blind_brief_identity(self.row(**blind_seat), [])

    def assertRefused(self, expected, **blind_seat):
        with self.assertRaises(forecasts.LedgerError) as caught:
            forecasts.validate_blind_brief_identity(self.row(**blind_seat), [])
        self.assertIn(expected, str(caught.exception))

    # -- the shape that actually reached the live ledger -------------------

    def test_the_live_incident_shape_is_refused(self):
        # A genuine non-run recorded with the role the seat *would* have taken.
        self.assertRefused(
            "must be 'SKIPPED'",
            ran=False,
            role="allocator",
            agreedWithPanel=None,
            changedDecision=None,
            blockedReason="launcher failure",
        )

    def test_the_corrected_form_of_that_row_is_accepted(self):
        self.assertAccepted(
            ran=False,
            role="SKIPPED",
            agreedWithPanel=None,
            changedDecision=None,
            blockedReason="launcher failure",
        )

    # -- each rule, with its valid counterpart -----------------------------

    def test_a_seat_that_ran_may_not_carry_the_sentinel(self):
        # The inverse defect. A one-directional correction tool is required
        # later precisely because a symmetric one could write this.
        self.assertRefused("must not be 'SKIPPED'", ran=True, role="SKIPPED")
        self.assertAccepted(ran=True, role="generic")

    def test_a_non_run_must_null_both_judgment_fields(self):
        base = dict(ran=False, role="SKIPPED", blockedReason="timeout")
        for field in ("agreedWithPanel", "changedDecision"):
            with self.subTest(field=field):
                self.assertRefused(
                    f"blindSeat.{field} must be null",
                    **{**base, "agreedWithPanel": None, "changedDecision": None,
                       field: True},
                )
        self.assertAccepted(**base, agreedWithPanel=None, changedDecision=None)

    def test_a_required_non_run_owes_a_blocked_reason(self):
        base = dict(ran=False, role="SKIPPED", required=True,
                    agreedWithPanel=None, changedDecision=None)
        for missing in (_ABSENT, None, "", "   ", 7):
            with self.subTest(blockedReason=missing):
                self.assertRefused("blockedReason", **base, blockedReason=missing)
        self.assertAccepted(**base, blockedReason="provider quota exhausted")

    def test_a_seat_that_was_not_required_owes_no_blocked_reason(self):
        # It did not fail to run; it was not asked to. Demanding both would
        # refuse a legitimate row shape.
        self.assertAccepted(
            ran=False, role="SKIPPED", required=False,
            notRequiredReason="mechanical change with no decision surface",
            agreedWithPanel=None, changedDecision=None,
            blockedReason=_ABSENT,
        )

    def test_a_not_required_seat_owes_a_reason(self):
        base = dict(required=False)
        for missing in (_ABSENT, None, "", "  "):
            with self.subTest(notRequiredReason=missing):
                self.assertRefused(
                    "notRequiredReason", **base, notRequiredReason=missing
                )
        self.assertAccepted(**base, notRequiredReason="mechanical refactor")

    def test_a_required_seat_may_not_carry_an_exemption_reason(self):
        # So stale exemption text cannot coast into a later decision-shaped run.
        self.assertRefused(
            "must be absent", required=True, notRequiredReason="left over"
        )
        self.assertAccepted(required=True, notRequiredReason=None)

    def test_the_ran_flag_must_be_boolean(self):
        for value in ("false", 0, 1, None):
            with self.subTest(ran=value):
                self.assertRefused("blindSeat.ran must be true or false", ran=value)

    def test_the_required_flag_is_checked_only_when_present(self):
        # The rules state what required *means*, not that every row carries it.
        self.assertAccepted(required=_ABSENT)
        self.assertRefused("must be true or false", required="yes")

    # -- what must not change ----------------------------------------------

    def test_a_pre_contract_row_is_left_alone(self):
        # No ran key at all: the kill criterion leaves these alone and so must
        # the write boundary, or the contract is applied retroactively.
        row = {"runId": self.RUN_ID, "blindSeat": {"role": "whatever"}}
        forecasts.validate_blind_brief_identity(row, [])

    def test_an_older_narrower_refusal_still_wins(self):
        # The contract check runs last, so a row with a bad brief path is told
        # about the brief path rather than about the contract.
        row = self.row(ran=False, role="allocator", brief="relative/path.md")
        with self.assertRaises(forecasts.LedgerError) as caught:
            forecasts.validate_blind_brief_identity(row, [])
        self.assertIn("absolute and normalized", str(caught.exception))
