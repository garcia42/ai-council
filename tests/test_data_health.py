import hashlib
import json
import math
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from council_tools.data_health import (
    RAW_RECORD_SHA256_ANNOTATION,
    DataHealthError,
    analyze_capture_data,
    finding_summary_record_key,
)


AS_OF = "2026-10-01T00:00:00Z"
ACTIVATION_ID = "activation-one"


def artifact(name):
    return {"path": f"sha256/{name}", "sha256": name * 8, "bytes": 10}


def activation():
    return {
        "schemaVersion": 2,
        "kind": "capture-activation",
        "activationId": ACTIVATION_ID,
        "activatedAt": "2026-08-23T04:00:00Z",
        "cohortName": "first-ten",
        "captureVersion": "2",
        "runtimeSourceCommit": "abc123",
        "runtimeSourceSha256": "a" * 64,
        "artifactRootPolicy": "private-content-addressed",
    }


def complete_run(
    number,
    *,
    outcome_class="exogenous",
    probability=60,
    active_seconds=20,
    version="v1",
    came_true=None,
    outcome_fingerprint=None,
    related_outcome_ids=(),
):
    run_id = f"run-{number}"
    initiation_id = f"initiation-{number}"
    outcome_id = f"outcome-{number}"
    base = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    started = base
    launched = base + timedelta(seconds=active_seconds)
    finished = launched + timedelta(seconds=100)
    finalized = finished
    plan = [
        {
            "seatId": "code",
            "role": "voting",
            "agentVersion": version,
            "agentDefinitionDigest": f"digest-{version}",
        }
    ]
    decision_artifact = {**artifact(f"d{number}"), "gitBlob": f"blob-{number}"}
    outcome = {
        "outcomeId": outcome_id,
        "claim": f"Outcome {number}",
        "resolutionDate": "2026-09-01",
        "resolvedBy": "inspect evidence",
        "decisionLink": f"decision-{number}",
        "materiality": "material",
        "actionIfTrue": "keep",
        "actionIfFalse": "repair",
        "evidenceCutoffAt": "2026-08-24T11:59:00Z",
        "relatedOutcomeIds": list(related_outcome_ids),
        "fingerprint": outcome_fingerprint or f"fingerprint-{number}",
    }
    initiation = {
        "schemaVersion": 2,
        "kind": "capture-initiation",
        "initiationId": initiation_id,
        "runId": run_id,
        "activationId": ACTIVATION_ID,
        "idempotencyKey": f"idempotency-{number}",
        "handlingStartedAt": started.isoformat(),
    }
    attempt = {
        "schemaVersion": 2,
        "kind": "council-attempt-v2",
        "runId": run_id,
        "initiationId": initiation_id,
        "activationId": ACTIVATION_ID,
        "seatsLaunchedAt": launched.isoformat(),
        "decisionFamilyId": f"family-{(number - 1) // 2}",
        "question": f"Question {number}",
        "decisionBeforeArtifact": decision_artifact,
        "outcomeClass": outcome_class,
        "outcomeClassRationale": "No path from this review to the measured event",
        "evidenceCutoffAt": "2026-08-24T11:59:00Z",
        "seatPlan": plan,
        "sharedOutcome": outcome,
    }
    seats_finished = {
        "schemaVersion": 2,
        "kind": "council-seats-finished",
        "runId": run_id,
        "initiationId": initiation_id,
        "activationId": ACTIVATION_ID,
        "seatsFinishedAt": finished.isoformat(),
        "seatStates": {"code": "submitted"},
    }
    completion = {
        "schemaVersion": 2,
        "kind": "council-v2",
        "runId": run_id,
        "initiationId": initiation_id,
        "activationId": ACTIVATION_ID,
        "finalizedAt": finalized.isoformat(),
        "decisionFamilyId": attempt["decisionFamilyId"],
        "question": attempt["question"],
        "decisionBeforeArtifact": decision_artifact,
        "outcomeClass": outcome_class,
        "outcomeClassRationale": attempt["outcomeClassRationale"],
        "evidenceCutoffAt": attempt["evidenceCutoffAt"],
        "seatPlan": plan,
        "sharedOutcome": outcome,
        "seatResults": [
            {
                **plan[0],
                "state": "submitted",
                "launcherAttempts": 1,
                "inputArtifact": artifact(f"i{number}"),
                "outputArtifact": artifact(f"o{number}"),
                "modelId": "model",
                "toolPolicy": "read-only",
                "repositoryCommit": "abc123",
            }
        ],
        "findings": [
            {
                "findingId": f"finding-{number}",
                "seatId": "code",
                "group": {"findingGroupId": f"group-{number}"},
                "operatorDisposition": {"kind": "new-acted"},
            }
        ],
        "noFindings": [],
        "predictions": [
            {
                "predictionId": f"prediction-{number}",
                "outcomeId": outcome_id,
                "seat": "code",
                "type": "shared",
                "claim": outcome["claim"],
                "probability": probability,
                "resolutionDate": outcome["resolutionDate"],
                "resolvedBy": outcome["resolvedBy"],
            }
        ],
        "blindSeat": {"required": False},
    }
    result = [initiation, attempt, seats_finished, completion]
    if came_true is not None:
        result.append(
            {
                "schemaVersion": 1,
                "kind": "outcome-resolution",
                "outcomeId": outcome_id,
                "outcomeFingerprint": outcome["fingerprint"],
                "status": "resolved",
                "cameTrue": came_true,
            }
        )
    return result


def accepted_artifacts(_reference):
    assert set(_reference) == {"path", "sha256", "bytes"}
    return dict(_reference)


def with_raw_identity(row, raw_bytes):
    row[RAW_RECORD_SHA256_ANNOTATION] = hashlib.sha256(raw_bytes).hexdigest()
    return row


class DataHealthTest(unittest.TestCase):
    def report(self, rows, **kwargs):
        if "resolution_events" in kwargs:
            resolution_events = kwargs.pop("resolution_events")
            ledger_rows = list(rows)
        else:
            # Test fixtures historically returned ledger and sidecar rows in one
            # list. Keep that terse fixture API while exercising the production
            # interface's explicit provenance split.
            resolution_events = [
                row for row in rows if row.get("kind") == "outcome-resolution"
            ]
            ledger_rows = [
                row for row in rows if row.get("kind") != "outcome-resolution"
            ]
        return analyze_capture_data(
            ledger_rows,
            as_of=kwargs.pop("as_of", AS_OF),
            resolution_events=resolution_events,
            artifact_integrity=kwargs.pop("artifact_integrity", accepted_artifacts),
            **kwargs,
        )

    def test_ledger_origin_resolution_is_diagnostic_only(self):
        rows = [activation(), *complete_run(1, probability=100, came_true=True)]

        report = self.report(rows, resolution_events=[])

        self.assertEqual(report["outcomeCounts"]["exogenousV2"], 1)
        self.assertEqual(report["outcomeCounts"]["resolvedExogenousV2"], 0)
        self.assertEqual(
            report["outcomeCounts"]["resolvedExogenousV2IssuanceCount"], 0
        )
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertIsNone(report["descriptiveForecastAccuracy"]["meanBrier"])
        provenance = report["resolutionProvenanceDiagnostics"]
        self.assertEqual(provenance["sidecarResolutionEventCount"], 0)
        self.assertEqual(provenance["invalidLedgerResolutionEventCount"], 1)
        self.assertEqual(
            provenance["invalidLedgerResolutionEvents"],
            [
                {
                    "lineNumber": 6,
                    "kind": "outcome-resolution",
                    "outcomeId": "outcome-1",
                    "error": "ledger-origin-resolution-not-eligible",
                }
            ],
        )

    def test_sidecar_resolution_must_match_issuance_fingerprint(self):
        run = complete_run(1, probability=100, came_true=True)
        resolution = run.pop()
        resolution["outcomeFingerprint"] = "different-fingerprint"

        report = self.report(
            [activation(), *run], resolution_events=[resolution]
        )

        self.assertEqual(report["outcomeCounts"]["resolvedExogenousV2"], 0)
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertIsNone(report["descriptiveForecastAccuracy"]["meanBrier"])

    def test_requires_exactly_one_capture_activation(self):
        cases = [([], 0), ([activation(), activation()], 2)]
        for rows, count in cases:
            with self.subTest(count=count):
                with self.assertRaisesRegex(
                    DataHealthError, f"exactly one capture-activation, found {count}"
                ):
                    self.report(rows)

    def test_first_ten_denominator_cannot_drop_failures_or_legacy_rows(self):
        rows = [activation()]
        first = complete_run(1)
        with_raw_identity(first[0], b'{"durable":"initiation-1"}\n')
        rows.extend([first[0], deepcopy(first[0]), *first[1:]])
        rows.append(complete_run(2)[0])  # durable pre-attempt crash
        rows.extend(complete_run(3)[:2])  # abandoned after attempt
        rows.append(complete_run(4)[3])  # orphan V2 completion
        rows.append(
            {
                "schemaVersion": 1,
                "kind": "council-attempt",
                "runId": "run-5",
                "sharedOutcome": {"outcomeId": "legacy-outcome-5"},
            }
        )
        rows.append(
            {
                "schemaVersion": 1,
                "kind": "council",
                "runId": "run-6",
                "predictions": [],
            }
        )
        rejected = complete_run(7)
        rejected[3]["question"] = "redefined after review"
        rows.extend(rejected)
        for number in range(8, 13):
            rows.extend(complete_run(number))

        report = self.report(rows)
        cohort = report["cohort"]
        self.assertEqual(cohort["eligibleInitiationCount"], 10)
        self.assertEqual(
            [item["runId"] for item in cohort["runs"]],
            [f"run-{number}" for number in range(1, 11)],
        )
        self.assertEqual(cohort["duplicateIdempotentInitiationRetryCount"], 1)
        self.assertEqual(cohort["completeInitiationCount"], 4)
        self.assertEqual(cohort["captureFraction"], 0.4)
        self.assertEqual(
            report["lifecycleCounts"],
            {
                "preAttemptCrashCount": 1,
                "abandonedAttemptCount": 1,
                "failedAttemptCount": 0,
                "rejectedCompletionCount": 2,
                "orphanAttemptCount": 0,
                "orphanCompletionCount": 2,
                "postActivationV1RunCount": 2,
            },
        )

    def test_distinct_run_with_reused_idempotency_key_still_takes_a_position(self):
        rows = [activation()]
        for number in (1, 2):
            run = complete_run(number)
            run[0]["idempotencyKey"] = "same-key"
            rows.extend(run)
        report = self.report(rows)
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(report["cohort"]["duplicateIdempotentInitiationRetryCount"], 0)

    def test_distinct_invalid_initiation_reusing_run_id_takes_its_own_position(self):
        first = complete_run(1)
        invalid_initiation = deepcopy(first[0])
        invalid_initiation["handlingStartedAt"] = "2026-08-24T12:00:01+00:00"
        invalid_initiation["_captureSchemaError"] = (
            "duplicate capture-initiation runId: run-1"
        )

        report = self.report([activation(), *first, invalid_initiation])

        cohort = report["cohort"]
        self.assertEqual(cohort["eligibleInitiationCount"], 2)
        self.assertEqual(cohort["duplicateIdempotentInitiationRetryCount"], 0)
        self.assertEqual(cohort["completeInitiationCount"], 1)
        self.assertEqual(
            [item["runId"] for item in cohort["runs"]], ["run-1", "run-1"]
        )
        self.assertTrue(cohort["runs"][0]["complete"])
        self.assertFalse(cohort["runs"][1]["complete"])
        self.assertIn(
            "schema-invalid-record:capture-initiation",
            cohort["runs"][1]["incompleteReasons"],
        )
        self.assertEqual(report["lifecycleCounts"]["preAttemptCrashCount"], 1)

    def test_invalid_duplicate_initiation_cannot_capture_valid_later_lineage(self):
        valid = complete_run(1, probability=100, came_true=True)
        invalid_duplicate = deepcopy(valid[0])
        invalid_duplicate["handlingStartedAt"] = "2026-08-24T12:00:01+00:00"
        invalid_duplicate["_captureSchemaError"] = (
            "duplicate capture-initiation runId: run-1"
        )
        with_raw_identity(valid[0], b'{"valid":"initiation"}\n')
        with_raw_identity(invalid_duplicate, b'{"invalid":"duplicate"}\n')

        report = self.report(
            [activation(), valid[0], invalid_duplicate, *valid[1:]]
        )

        cohort = report["cohort"]
        self.assertEqual(cohort["eligibleInitiationCount"], 2)
        self.assertEqual(cohort["completeInitiationCount"], 1)
        self.assertEqual(
            [item["complete"] for item in cohort["runs"]], [True, False]
        )
        self.assertIn(
            "schema-invalid-record:capture-initiation",
            cohort["runs"][1]["incompleteReasons"],
        )
        self.assertEqual(report["outcomeCounts"]["exogenousV2"], 1)
        self.assertEqual(report["outcomeCounts"]["resolvedExogenousV2"], 1)
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 1)
        self.assertEqual(report["descriptiveForecastAccuracy"]["meanBrier"], 0.0)

    def test_report_annotation_does_not_hide_byte_exact_idempotent_retry(self):
        first = complete_run(1)
        with_raw_identity(first[0], b'{"durable":"initiation-1"}\n')
        exact_retry = deepcopy(first[0])
        exact_retry["_captureSchemaError"] = (
            "duplicate capture-initiation runId: run-1"
        )

        report = self.report([activation(), first[0], exact_retry, *first[1:]])

        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 1)
        self.assertEqual(
            report["cohort"]["duplicateIdempotentInitiationRetryCount"], 1
        )
        self.assertTrue(report["cohort"]["runs"][0]["complete"])

    def test_equal_decoded_initiations_without_raw_identity_are_distinct_events(self):
        first = complete_run(1)

        report = self.report(
            [activation(), first[0], deepcopy(first[0]), *first[1:]]
        )

        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(
            report["cohort"]["duplicateIdempotentInitiationRetryCount"], 0
        )

    def test_byte_different_equal_json_initiations_are_distinct_events(self):
        first = complete_run(1)
        second = deepcopy(first[0])
        with_raw_identity(first[0], b'{"a":1,"b":2}\n')
        with_raw_identity(second, b'{ "a": 1, "b": 2 }\n')

        report = self.report([activation(), first[0], second, *first[1:]])

        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(
            report["cohort"]["duplicateIdempotentInitiationRetryCount"], 0
        )

    def test_repeated_v1_attempts_with_same_run_id_take_separate_positions(self):
        attempt = {
            "schemaVersion": 1,
            "kind": "council-attempt",
            "runId": "reused-v1-run",
            "sharedOutcome": {"outcomeId": "legacy-outcome"},
        }

        report = self.report([activation(), attempt, deepcopy(attempt)])

        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(
            [item["runId"] for item in report["cohort"]["runs"]],
            ["reused-v1-run", "reused-v1-run"],
        )
        self.assertEqual(report["lifecycleCounts"]["postActivationV1RunCount"], 2)

    def test_orphan_v2_completions_with_same_identity_take_separate_positions(self):
        completion = complete_run(1)[3]
        duplicate = deepcopy(completion)
        with_raw_identity(completion, b'{"completion":1}\n')
        with_raw_identity(duplicate, b'{ "completion": 1 }\n')

        report = self.report([activation(), completion, duplicate])

        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(report["lifecycleCounts"]["orphanCompletionCount"], 2)

    def test_late_initiation_fills_an_observed_orphan_without_hiding_invalidity(self):
        run = complete_run(1)
        orphan_completion = deepcopy(run[3])
        orphan_completion["_captureSchemaError"] = (
            "council-v2 has no prior attempt: run-1"
        )

        report = self.report(
            [activation(), orphan_completion, run[0], run[1], run[2]]
        )

        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 1)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
        self.assertIn(
            "invalid-lifecycle-order",
            report["cohort"]["runs"][0]["incompleteReasons"],
        )
        self.assertIn(
            "schema-invalid-record:council-v2",
            report["cohort"]["runs"][0]["incompleteReasons"],
        )

    def test_fewer_than_ten_is_pending_before_cutoff_and_false_at_cutoff(self):
        rows = [activation()]
        for number in range(1, 4):
            rows.extend(complete_run(number))
        before = self.report(rows, as_of="2026-10-31T23:59:58-04:00")
        at_cutoff = self.report(rows, as_of="2026-10-31T23:59:59-04:00")
        self.assertIsNone(before["cohort"]["sharedOperationalOutcome"])
        self.assertFalse(before["cohort"]["cutoffReached"])
        self.assertFalse(at_cutoff["cohort"]["sharedOperationalOutcome"])
        self.assertTrue(at_cutoff["cohort"]["cutoffReached"])

    def test_duration_uses_system_boundaries_and_exact_positions_five_and_six(self):
        rows = [activation()]
        for number, seconds in enumerate(range(10, 101, 10), 1):
            rows.extend(complete_run(number, active_seconds=seconds))
        report = self.report(rows)
        self.assertEqual(report["timing"]["validDurationCount"], 10)
        self.assertEqual(report["timing"]["medianActiveHandlingSeconds"], 55.0)
        self.assertEqual(report["timing"]["medianElapsedSeconds"], 155.0)
        self.assertTrue(report["cohort"]["timeGatePassed"])

    def test_invalid_or_future_boundary_is_infinity_and_fails_whole_time_gate(self):
        rows = [activation()]
        for number in range(1, 11):
            run = complete_run(number, active_seconds=number * 10)
            if number == 10:
                run[2]["seatsFinishedAt"] = "2027-01-01T00:00:00Z"
            rows.extend(run)
        report = self.report(rows)
        self.assertEqual(report["timing"]["validDurationCount"], 9)
        self.assertTrue(math.isinf(report["timing"]["activeHandlingSeconds"][-1]))
        self.assertTrue(math.isinf(report["timing"]["elapsedSeconds"][-1]))
        self.assertFalse(report["cohort"]["timeGatePassed"])

    def test_invalidation_permanently_removes_otherwise_complete_run(self):
        rows = [activation(), *complete_run(1, came_true=True)]
        rows.append(
            {
                "schemaVersion": 2,
                "kind": "capture-invalidation",
                "invalidationId": "invalidation-1",
                "runId": "run-1",
                "reason": "disposition-error",
                "operator": "operator",
                "invalidatedAt": "2026-08-25T00:00:00Z",
                "evidenceRef": "ticket-1",
            }
        )
        report = self.report(rows)
        run = report["cohort"]["runs"][0]
        self.assertFalse(run["complete"])
        self.assertEqual(run["invalidationCount"], 1)
        self.assertIn("capture-invalidated", run["incompleteReasons"])
        self.assertEqual(report["findings"]["eligibleRunCount"], 0)
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]["findingCount"], 1
        )
        accuracy = report["descriptiveForecastAccuracy"]
        self.assertEqual(accuracy["predictionCount"], 0)
        self.assertEqual(
            accuracy["excludedOrInvalidStratum"]["predictionCount"], 1
        )

    def test_schema_invalid_invalidation_rows_are_diagnostics_only(self):
        cases = (
            (
                "malformed",
                {
                    "reason": "not-a-valid-reason",
                    "_captureSchemaError": "invalid invalidation reason",
                },
            ),
            (
                "future-as-of",
                {
                    "invalidatedAt": "2027-01-01T00:00:00Z",
                    "_captureSchemaError": "invalidation is after report as-of",
                },
            ),
        )
        for label, mutation in cases:
            with self.subTest(label=label):
                invalidation = {
                    "schemaVersion": 2,
                    "kind": "capture-invalidation",
                    "invalidationId": f"invalidation-{label}",
                    "runId": "run-1",
                    "reason": "disposition-error",
                    "operator": "operator",
                    "invalidatedAt": "2026-08-25T00:00:00Z",
                    "evidenceRef": f"ticket-{label}",
                    **mutation,
                }

                report = self.report(
                    [activation(), *complete_run(1, came_true=True), invalidation]
                )

                cohort = report["cohort"]
                self.assertEqual(cohort["eligibleInitiationCount"], 1)
                self.assertEqual(cohort["completeInitiationCount"], 1)
                run = cohort["runs"][0]
                self.assertTrue(run["complete"])
                self.assertEqual(run["invalidationCount"], 0)
                self.assertNotIn("capture-invalidated", run["incompleteReasons"])

                findings = report["findings"]
                self.assertEqual(findings["eligibleRunCount"], 1)
                self.assertEqual(findings["summarizedRunCount"], 1)
                self.assertEqual(findings["findingCount"], 1)
                self.assertEqual(
                    findings["operatorReportedDispositionMix"],
                    {"new-acted": {"findingCount": 1, "share": 1.0}},
                )
                self.assertEqual(
                    findings["withinRunFindingOverlap"],
                    {
                        "findingGroupCount": 1,
                        "overlapGroupCount": 0,
                        "overlapFraction": 0.0,
                    },
                )
                excluded = findings["excludedOrInvalidStratum"]
                self.assertEqual(excluded["runCount"], 0)
                self.assertEqual(excluded["summarizedRunCount"], 0)
                self.assertEqual(excluded["findingCount"], 0)
                self.assertEqual(excluded["operatorReportedDispositionMix"], {})
                self.assertIsNone(excluded["withinRunFindingOverlap"])

    def test_explicit_unavailable_seat_is_counted_as_failed_and_incomplete(self):
        run = complete_run(1)
        run[2]["seatStates"]["code"] = "unavailable"
        result = run[3]["seatResults"][0]
        result["state"] = "unavailable"
        for field in (
            "outputArtifact",
            "modelId",
            "toolPolicy",
            "repositoryCommit",
        ):
            result.pop(field)
        run[3]["findings"] = []
        run[3]["predictions"] = []
        report = self.report([activation(), *run])
        self.assertEqual(report["lifecycleCounts"]["failedAttemptCount"], 1)
        self.assertEqual(report["lifecycleCounts"]["rejectedCompletionCount"], 0)
        self.assertIn(
            "seat-execution-failure",
            report["cohort"]["runs"][0]["incompleteReasons"],
        )

    def test_artifact_failures_and_finding_summary_are_reported_without_composite(self):
        rows = [activation(), *complete_run(1, came_true=True)]
        completion_raw = b'{"physical":"completion-1"}\n'
        with_raw_identity(rows[4], completion_raw)

        def verifier(reference):
            return not reference["path"].endswith("o1")

        summaries = {
            finding_summary_record_key(5, rows[4]): {
                "submittedSeatCount": 1,
                "findingCount": 2,
                "findingsPerSubmittedSeat": {"code": 2},
                "emptyDeclarationRate": {
                    "declarationCount": 0,
                    "submittedSeatCount": 1,
                    "rate": 0.0,
                },
                "operatorReportedDispositionMix": {
                    "new-acted": {"findingCount": 1, "share": 0.5},
                    "new-rejected": {"findingCount": 1, "share": 0.5},
                },
                "withinRunFindingOverlap": {
                    "findingGroupCount": 2,
                    "overlapGroupCount": 1,
                    "overlapGroups": [],
                },
            }
        }
        report = self.report(
            rows, artifact_integrity=verifier, finding_summaries=summaries
        )
        self.assertEqual(report["artifacts"]["requiredArtifactCount"], 3)
        self.assertEqual(report["artifacts"]["artifactIntegrityFailureCount"], 1)
        self.assertFalse(report["cohort"]["runs"][0]["complete"])
        self.assertIsNone(report["findings"]["findingsPerSubmittedSeat"]["rate"])
        excluded = report["findings"]["excludedOrInvalidStratum"]
        self.assertEqual(excluded["runCount"], 1)
        self.assertEqual(excluded["findingsPerSubmittedSeat"]["rate"], 2.0)
        self.assertEqual(
            excluded["emptyFindingDeclarationRate"]["rate"], 0.0
        )
        self.assertEqual(
            excluded["withinRunFindingOverlap"]["overlapFraction"], 0.5
        )
        self.assertEqual(
            excluded["operatorReportedDispositionMix"]["new-acted"][
                "findingCount"
            ],
            1,
        )
        accuracy = report["descriptiveForecastAccuracy"]
        self.assertEqual(accuracy["predictionCount"], 0)
        self.assertEqual(
            accuracy["excludedOrInvalidStratum"]["predictionCount"], 1
        )
        self.assertNotIn("score", report)

    def test_invalid_duplicate_completion_cannot_inherit_finding_summary(self):
        valid = complete_run(1)
        valid_completion_raw = b'{"physical":"valid-completion"}\n'
        with_raw_identity(valid[3], valid_completion_raw)

        invalid_duplicate = deepcopy(valid[3])
        invalid_duplicate["_captureSchemaError"] = (
            "duplicate council-v2 public run identity"
        )
        # The tolerant reader's schema annotation is report-only.  This models
        # a byte-exact repeated JSONL line, so both occurrences intentionally
        # carry the same raw digest and are distinguishable only by line.
        with_raw_identity(invalid_duplicate, valid_completion_raw)
        summaries = {
            finding_summary_record_key(5, valid[3]): {
                "submittedSeatCount": 1,
                "findingCount": 2,
                "findingsPerSubmittedSeat": {"code": 2},
                "emptyDeclarationRate": {
                    "declarationCount": 0,
                    "submittedSeatCount": 1,
                    "rate": 0.0,
                },
                "operatorReportedDispositionMix": {
                    "new-acted": {"findingCount": 1, "share": 0.5},
                    "new-rejected": {"findingCount": 1, "share": 0.5},
                },
                "withinRunFindingOverlap": {
                    "findingGroupCount": 2,
                    "overlapGroupCount": 1,
                    "overlapGroups": [],
                },
            },
            # A legacy run-scoped entry is deliberately present.  Neither the
            # valid lifecycle nor its invalid duplicate may consume it.
            "run-1": {
                "submittedSeatCount": 1,
                "findingCount": 9,
                "findingsPerSubmittedSeat": {"code": 9},
                "emptyDeclarationRate": {
                    "declarationCount": 0,
                    "submittedSeatCount": 1,
                    "rate": 0.0,
                },
                "operatorReportedDispositionMix": {
                    "inherited-bug": {"findingCount": 9, "share": 1.0}
                },
                "withinRunFindingOverlap": {
                    "findingGroupCount": 9,
                    "overlapGroupCount": 9,
                    "overlapGroups": [],
                },
            },
        }

        report = self.report(
            [activation(), *valid, invalid_duplicate],
            finding_summaries=summaries,
        )

        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 1)
        self.assertEqual(
            [item["runId"] for item in report["cohort"]["runs"]],
            ["run-1", "run-1"],
        )
        self.assertEqual(report["findings"]["findingCount"], 2)
        self.assertEqual(
            report["findings"]["operatorReportedDispositionMix"]["new-acted"][
                "findingCount"
            ],
            1,
        )
        self.assertNotIn(
            "inherited-bug",
            report["findings"]["operatorReportedDispositionMix"],
        )
        excluded = report["findings"]["excludedOrInvalidStratum"]
        self.assertEqual(excluded["runCount"], 1)
        self.assertEqual(excluded["summarizedRunCount"], 1)
        self.assertEqual(excluded["findingCount"], 1)
        self.assertEqual(
            excluded["operatorReportedDispositionMix"],
            {"new-acted": {"findingCount": 1, "share": 1.0}},
        )
        self.assertEqual(
            excluded["withinRunFindingOverlap"]["findingGroupCount"], 1
        )
        self.assertEqual(
            excluded["withinRunFindingOverlap"]["overlapGroupCount"], 0
        )
        self.assertIn(
            "schema-invalid-record:council-v2",
            report["cohort"]["runs"][1]["incompleteReasons"],
        )

    def test_invalid_completion_before_valid_does_not_consume_clean_lineage(self):
        valid = complete_run(1)
        valid_completion_raw = b'{"physical":"later-valid-completion"}\n'
        with_raw_identity(valid[3], valid_completion_raw)

        invalid_duplicate = deepcopy(valid[3])
        invalid_duplicate["findings"] = []
        invalid_duplicate["noFindings"] = [
            {"seatId": "code", "statement": "No findings."}
        ]
        invalid_duplicate["_captureSchemaError"] = (
            "schema-invalid earlier completion occurrence"
        )
        with_raw_identity(
            invalid_duplicate, b'{"physical":"earlier-invalid-completion"}\n'
        )
        summaries = {
            finding_summary_record_key(6, valid[3]): {
                "submittedSeatCount": 1,
                "findingCount": 2,
                "findingsPerSubmittedSeat": {"code": 2},
                "emptyDeclarationRate": {
                    "declarationCount": 0,
                    "submittedSeatCount": 1,
                    "rate": 0.0,
                },
                "operatorReportedDispositionMix": {
                    "new-acted": {"findingCount": 1, "share": 0.5},
                    "new-rejected": {"findingCount": 1, "share": 0.5},
                },
                "withinRunFindingOverlap": {
                    "findingGroupCount": 2,
                    "overlapGroupCount": 1,
                    "overlapGroups": [],
                },
            },
            "run-1": {
                "submittedSeatCount": 1,
                "findingCount": 9,
                "findingsPerSubmittedSeat": {"code": 9},
                "emptyDeclarationRate": {
                    "declarationCount": 0,
                    "submittedSeatCount": 1,
                    "rate": 0.0,
                },
                "operatorReportedDispositionMix": {
                    "inherited-bug": {"findingCount": 9, "share": 1.0}
                },
                "withinRunFindingOverlap": {
                    "findingGroupCount": 9,
                    "overlapGroupCount": 9,
                    "overlapGroups": [],
                },
            },
        }

        report = self.report(
            [activation(), *valid[:3], invalid_duplicate, valid[3]],
            finding_summaries=summaries,
        )

        cohort = report["cohort"]
        self.assertEqual(cohort["eligibleInitiationCount"], 2)
        self.assertEqual(cohort["completeInitiationCount"], 1)
        self.assertEqual(
            [item["runId"] for item in cohort["runs"]], ["run-1", "run-1"]
        )
        self.assertTrue(cohort["runs"][0]["complete"])
        self.assertFalse(cohort["runs"][1]["complete"])
        self.assertIn(
            "schema-invalid-record:council-v2",
            cohort["runs"][1]["incompleteReasons"],
        )

        findings = report["findings"]
        self.assertEqual(findings["eligibleRunCount"], 1)
        self.assertEqual(findings["summarizedRunCount"], 1)
        self.assertEqual(findings["findingCount"], 2)
        self.assertEqual(
            findings["operatorReportedDispositionMix"],
            {
                "new-acted": {"findingCount": 1, "share": 0.5},
                "new-rejected": {"findingCount": 1, "share": 0.5},
            },
        )
        self.assertEqual(
            findings["withinRunFindingOverlap"],
            {
                "findingGroupCount": 2,
                "overlapGroupCount": 1,
                "overlapFraction": 0.5,
            },
        )
        self.assertNotIn(
            "inherited-bug", findings["operatorReportedDispositionMix"]
        )

        excluded = findings["excludedOrInvalidStratum"]
        self.assertEqual(excluded["runCount"], 1)
        self.assertEqual(excluded["summarizedRunCount"], 1)
        self.assertEqual(excluded["findingCount"], 0)
        self.assertEqual(excluded["operatorReportedDispositionMix"], {})
        self.assertEqual(
            excluded["withinRunFindingOverlap"],
            {
                "findingGroupCount": 0,
                "overlapGroupCount": 0,
                "overlapFraction": None,
            },
        )

    def test_tolerated_schema_error_is_denominator_visible_but_headline_excluded(self):
        run = complete_run(1, came_true=True)
        run[3]["_captureSchemaError"] = "completion failed strict schema validation"
        report = self.report([activation(), *run])

        cohort_run = report["cohort"]["runs"][0]
        self.assertFalse(cohort_run["eligibleForHeadlineAnalysis"])
        self.assertIn(
            "schema-invalid-record:council-v2", cohort_run["incompleteReasons"]
        )
        self.assertEqual(report["findings"]["summarizedRunCount"], 0)
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]["summarizedRunCount"],
            1,
        )
        accuracy = report["descriptiveForecastAccuracy"]
        self.assertEqual(accuracy["predictionCount"], 0)
        self.assertEqual(
            accuracy["excludedOrInvalidStratum"]["resolvedOutcomeCount"], 1
        )
        self.assertEqual(report["lifecycleCounts"]["rejectedCompletionCount"], 1)

    def test_tolerated_non_text_outcome_class_is_safely_projected(self):
        invalid_values = ([], {}, True, 7, 1.5)
        for invalid_value in invalid_values:
            with self.subTest(value_type=type(invalid_value).__name__):
                run = complete_run(1)
                run[1]["outcomeClass"] = invalid_value
                run[1]["_captureSchemaError"] = "invalid V2 record field type"
                with_raw_identity(run[1], b'{"integrated":"invalid-attempt"}\n')

                report = self.report([activation(), *run])

                self.assertEqual(report["cohort"]["eligibleInitiationCount"], 1)
                self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
                self.assertEqual(report["outcomeCounts"]["exogenousV2"], 0)
                self.assertEqual(
                    report["outcomeCounts"]["interventionSensitiveV2"], 0
                )
                excluded = report["excludedOrInvalidOutcomeStratum"]
                self.assertEqual(excluded["issuanceCount"], 1)
                self.assertEqual(excluded["byOutcomeClass"], {"invalid-or-missing": 1})
                issuance = excluded["issuances"][0]
                self.assertEqual(issuance["outcomeId"], "outcome-1")
                self.assertEqual(issuance["outcomeFingerprint"], "fingerprint-1")
                self.assertEqual(issuance["outcomeClass"], "invalid-or-missing")
                self.assertIn(
                    "missing-or-invalid-outcome-class",
                    issuance["exclusionReasons"],
                )

    def test_tolerated_nested_invalid_values_never_enter_unsafe_analysis_paths(self):
        cases = (
            (
                "outcome-identity",
                1,
                lambda run: run[1]["sharedOutcome"].update(
                    {"outcomeId": [], "fingerprint": {}}
                ),
            ),
            (
                "seat-plan",
                1,
                lambda run: run[1]["seatPlan"][0].update({"seatId": []}),
            ),
            (
                "seat-state",
                2,
                lambda run: run[2]["seatStates"].update({"code": {}}),
            ),
            (
                "finding",
                3,
                lambda run: run[3]["findings"][0].update(
                    {
                        "seatId": [],
                        "group": {"findingGroupId": {}},
                        "operatorDisposition": {"kind": True},
                    }
                ),
            ),
            (
                "no-finding-declaration",
                3,
                lambda run: run[3].update({"noFindings": [{"seatId": []}]}),
            ),
        )
        for label, row_index, mutate in cases:
            with self.subTest(label=label):
                run = complete_run(1)
                mutate(run)
                run[row_index]["_captureSchemaError"] = "invalid V2 record field type"
                with_raw_identity(
                    run[row_index], f'{{"integrated":"{label}"}}\n'.encode()
                )

                report = self.report([activation(), *run])

                self.assertEqual(report["cohort"]["eligibleInitiationCount"], 1)
                self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
                self.assertEqual(
                    report["excludedOrInvalidOutcomeStratum"]["issuanceCount"],
                    1,
                )

    def test_tolerant_reader_invalid_kind_sentinel_keeps_physical_denominator_row(self):
        invalid = {
            "schemaVersion": 2,
            "kind": "invalid-v2-record",
            "runId": "run-invalid-kind",
            "_captureSchemaError": "invalid V2 record field type",
        }
        with_raw_identity(invalid, b'{"kind":[]}\n')

        report = self.report([activation(), invalid])

        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 1)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
        self.assertEqual(report["cohort"]["runs"][0]["runId"], "run-invalid-kind")
        self.assertEqual(
            report["cohort"]["runs"][0]["eligibilityKind"],
            "invalid-v2-record",
        )
        self.assertIn(
            "schema-invalid-record:invalid-v2-record",
            report["cohort"]["runs"][0]["incompleteReasons"],
        )

    def assert_invalid_kind_between_finished_and_completion_is_independent(
        self, malformed_kind
    ):
        valid = complete_run(1)
        valid_completion_raw = b'{"physical":"valid-after-invalid-kind"}\n'
        with_raw_identity(valid[3], valid_completion_raw)
        malformed = {
            "schemaVersion": 2,
            "kind": malformed_kind,
            "runId": valid[0]["runId"],
            "initiationId": valid[0]["initiationId"],
            "activationId": ACTIVATION_ID,
            "_captureSchemaError": "invalid V2 dispatch kind",
        }
        malformed_raw = json.dumps(
            {"kind": malformed_kind}, separators=(",", ":")
        ).encode() + b"\n"
        with_raw_identity(malformed, malformed_raw)
        summaries = {
            finding_summary_record_key(6, valid[3]): {
                "submittedSeatCount": 1,
                "findingCount": 2,
                "findingsPerSubmittedSeat": {"code": 2},
                "emptyDeclarationRate": {
                    "declarationCount": 0,
                    "submittedSeatCount": 1,
                    "rate": 0.0,
                },
                "operatorReportedDispositionMix": {
                    "new-acted": {"findingCount": 1, "share": 0.5},
                    "new-rejected": {"findingCount": 1, "share": 0.5},
                },
                "withinRunFindingOverlap": {
                    "findingGroupCount": 2,
                    "overlapGroupCount": 1,
                    "overlapGroups": [],
                },
            },
            "run-1": {
                "submittedSeatCount": 1,
                "findingCount": 9,
                "findingsPerSubmittedSeat": {"code": 9},
                "emptyDeclarationRate": {
                    "declarationCount": 0,
                    "submittedSeatCount": 1,
                    "rate": 0.0,
                },
                "operatorReportedDispositionMix": {
                    "inherited-bug": {"findingCount": 9, "share": 1.0}
                },
                "withinRunFindingOverlap": {
                    "findingGroupCount": 9,
                    "overlapGroupCount": 9,
                    "overlapGroups": [],
                },
            },
        }

        report = self.report(
            [activation(), *valid[:3], malformed, valid[3]],
            finding_summaries=summaries,
        )

        cohort = report["cohort"]
        self.assertEqual(cohort["eligibleInitiationCount"], 2)
        self.assertEqual(cohort["completeInitiationCount"], 1)
        self.assertTrue(cohort["runs"][0]["complete"])
        self.assertFalse(cohort["runs"][1]["complete"])
        self.assertEqual(
            cohort["runs"][1]["eligibilityKind"], "invalid-v2-record"
        )
        self.assertIn(
            "schema-invalid-record:invalid-v2-record",
            cohort["runs"][1]["incompleteReasons"],
        )

        findings = report["findings"]
        self.assertEqual(findings["eligibleRunCount"], 1)
        self.assertEqual(findings["summarizedRunCount"], 1)
        self.assertEqual(findings["findingCount"], 2)
        self.assertEqual(
            findings["operatorReportedDispositionMix"],
            {
                "new-acted": {"findingCount": 1, "share": 0.5},
                "new-rejected": {"findingCount": 1, "share": 0.5},
            },
        )
        self.assertEqual(
            findings["withinRunFindingOverlap"],
            {
                "findingGroupCount": 2,
                "overlapGroupCount": 1,
                "overlapFraction": 0.5,
            },
        )
        self.assertNotIn(
            "inherited-bug", findings["operatorReportedDispositionMix"]
        )

        excluded = findings["excludedOrInvalidStratum"]
        self.assertEqual(excluded["runCount"], 1)
        self.assertEqual(excluded["summarizedRunCount"], 0)
        self.assertEqual(excluded["findingCount"], 0)
        self.assertEqual(excluded["submittedSeatCount"], 0)
        self.assertEqual(excluded["operatorReportedDispositionMix"], {})
        self.assertIsNone(excluded["withinRunFindingOverlap"])

    def test_invalid_kind_between_finished_and_completion_is_independent(self):
        for malformed_kind in (["not", "text"], "future-unknown-kind"):
            with self.subTest(kind=malformed_kind):
                self.assert_invalid_kind_between_finished_and_completion_is_independent(
                    malformed_kind
                )

    def test_later_invalid_attempt_cannot_replace_valid_outcome_score(self):
        valid = complete_run(1, probability=100, came_true=True)
        invalid = complete_run(2, probability=0)
        reused_outcome = deepcopy(valid[1]["sharedOutcome"])
        invalid[1]["sharedOutcome"] = reused_outcome
        invalid[3]["sharedOutcome"] = reused_outcome
        invalid[3]["predictions"][0]["outcomeId"] = reused_outcome["outcomeId"]
        invalid[3]["predictions"][0]["claim"] = reused_outcome["claim"]
        invalid[1]["_captureSchemaError"] = (
            "duplicate V2 outcomeId: outcome-1"
        )

        report = self.report([activation(), *valid, *invalid])

        self.assertEqual(report["outcomeCounts"]["exogenousV2"], 1)
        accuracy = report["descriptiveForecastAccuracy"]
        self.assertEqual(accuracy["predictionCount"], 1)
        self.assertEqual(accuracy["meanBrier"], 0.0)
        excluded = accuracy["excludedOrInvalidStratum"]
        self.assertEqual(excluded["resolvedOutcomeCount"], 1)
        self.assertEqual(excluded["predictionCount"], 1)
        self.assertEqual(excluded["runs"][0]["runId"], "run-2")
        self.assertIn(
            "schema-invalid-record:council-attempt-v2",
            excluded["runs"][0]["incompleteReasons"],
        )

    def test_excluded_resolved_outcome_count_deduplicates_reused_id_issuances(self):
        valid = complete_run(1, probability=100, came_true=True)
        invalid_runs = [complete_run(2, probability=0), complete_run(3, probability=0)]
        reused_outcome = deepcopy(valid[1]["sharedOutcome"])
        for invalid in invalid_runs:
            invalid[1]["sharedOutcome"] = deepcopy(reused_outcome)
            invalid[3]["sharedOutcome"] = deepcopy(reused_outcome)
            invalid[3]["predictions"][0]["outcomeId"] = reused_outcome["outcomeId"]
            invalid[3]["predictions"][0]["claim"] = reused_outcome["claim"]
            invalid[1]["_captureSchemaError"] = (
                "duplicate V2 outcomeId: outcome-1"
            )

        report = self.report(
            [activation(), *valid, *invalid_runs[0], *invalid_runs[1]]
        )

        excluded = report["descriptiveForecastAccuracy"][
            "excludedOrInvalidStratum"
        ]
        self.assertEqual(excluded["resolvedIssuanceCount"], 2)
        self.assertEqual(excluded["resolvedOutcomeCount"], 1)
        self.assertEqual(excluded["predictionCount"], 2)
        self.assertEqual(
            {item["outcomeId"] for item in excluded["runs"]}, {"outcome-1"}
        )

    def test_top_level_excluded_counts_intervention_issuances_and_unique_outcomes(self):
        valid = complete_run(
            1,
            outcome_class="intervention-sensitive",
            probability=100,
            came_true=True,
        )
        invalid_runs = [
            complete_run(2, outcome_class="intervention-sensitive"),
            complete_run(3, outcome_class="intervention-sensitive"),
        ]
        reused_outcome = deepcopy(valid[1]["sharedOutcome"])
        for invalid in invalid_runs:
            invalid[1]["sharedOutcome"] = deepcopy(reused_outcome)
            invalid[3]["sharedOutcome"] = deepcopy(reused_outcome)
            invalid[3]["predictions"][0]["outcomeId"] = reused_outcome["outcomeId"]
            invalid[3]["predictions"][0]["claim"] = reused_outcome["claim"]
            invalid[1]["_captureSchemaError"] = (
                "duplicate V2 outcomeId: outcome-1"
            )

        report = self.report(
            [activation(), *valid, *invalid_runs[0], *invalid_runs[1]]
        )

        excluded = report["excludedOrInvalidOutcomeStratum"]
        self.assertEqual(excluded["issuanceCount"], 2)
        self.assertEqual(excluded["resolvedIssuanceCount"], 2)
        self.assertEqual(excluded["resolvedOutcomeCount"], 1)
        self.assertEqual(excluded["knownOutcomeIds"], ["outcome-1"])
        self.assertEqual(excluded["byOutcomeClass"], {"intervention-sensitive": 2})
        self.assertEqual(
            [item["resolutionStatus"] for item in excluded["issuances"]],
            ["resolved", "resolved"],
        )

    def test_linked_repeated_fingerprint_reports_issuances_not_independent_outcomes(self):
        fingerprint = "same-underlying-event"
        first = complete_run(
            1,
            probability=80,
            came_true=True,
            outcome_fingerprint=fingerprint,
        )
        second = complete_run(
            2,
            probability=60,
            came_true=True,
            outcome_fingerprint=fingerprint,
            related_outcome_ids=("outcome-1",),
        )

        report = self.report([activation(), *first, *second])

        counts = report["outcomeCounts"]
        self.assertEqual(counts["eligibleV2IssuanceCount"], 2)
        self.assertEqual(counts["uniqueUnderlyingFingerprintCount"], 1)
        self.assertEqual(counts["exogenousV2IssuanceCount"], 2)
        self.assertEqual(counts["exogenousV2"], 1)
        self.assertEqual(counts["resolvedExogenousV2IssuanceCount"], 2)
        self.assertEqual(counts["resolvedExogenousV2"], 1)

        identity = report["outcomeIdentityDiagnostics"]
        self.assertEqual(identity["eligibleIssuanceCount"], 2)
        self.assertEqual(identity["uniqueUnderlyingFingerprintCount"], 1)
        self.assertEqual(identity["repeatedIssuanceCount"], 1)
        issuances = identity["eligibleIssuances"]
        self.assertFalse(issuances[0]["isRepeatedUnderlyingOutcome"])
        self.assertEqual(issuances[0]["underlyingOutcomeIssuanceOrdinal"], 1)
        self.assertTrue(issuances[1]["isRepeatedUnderlyingOutcome"])
        self.assertEqual(issuances[1]["underlyingOutcomeIssuanceOrdinal"], 2)
        self.assertEqual(
            issuances[1]["priorOutcomeIdsForFingerprint"], ["outcome-1"]
        )

        polarity = report["resolvedExogenousPolarity"]
        self.assertEqual(polarity["resolvedIssuanceCount"], 2)
        self.assertEqual(polarity["resolvedOutcomeCount"], 1)
        self.assertEqual(polarity["trueCount"], 1)
        accuracy = report["descriptiveForecastAccuracy"]
        self.assertEqual(accuracy["predictionCount"], 2)
        self.assertEqual(accuracy["resolvedIssuanceCount"], 2)
        self.assertEqual(accuracy["uniqueUnderlyingFingerprintCount"], 1)

    def test_mixed_classes_taint_entire_fingerprint_intervention_sensitive(self):
        fingerprint = "mixed-class-underlying-event"
        exogenous = complete_run(
            1,
            outcome_class="exogenous",
            probability=100,
            came_true=True,
            outcome_fingerprint=fingerprint,
        )
        sensitive = complete_run(
            2,
            outcome_class="intervention-sensitive",
            probability=0,
            came_true=True,
            outcome_fingerprint=fingerprint,
            related_outcome_ids=("outcome-1",),
        )

        report = self.report([activation(), *exogenous, *sensitive])

        counts = report["outcomeCounts"]
        self.assertEqual(counts["exogenousV2"], 0)
        self.assertEqual(counts["exogenousV2IssuanceCount"], 0)
        self.assertEqual(counts["resolvedExogenousV2"], 0)
        self.assertEqual(counts["resolvedExogenousV2IssuanceCount"], 0)
        self.assertEqual(counts["interventionSensitiveV2"], 1)
        self.assertEqual(counts["interventionSensitiveV2IssuanceCount"], 2)
        self.assertEqual(counts["resolvedInterventionSensitiveV2"], 1)
        self.assertEqual(
            counts["resolvedInterventionSensitiveV2IssuanceCount"], 2
        )
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertEqual(
            report["resolvedExogenousPolarity"]["resolvedOutcomeCount"], 0
        )

        identity = report["outcomeIdentityDiagnostics"]
        self.assertEqual(identity["outcomeClassConflictFingerprintCount"], 1)
        self.assertEqual(
            identity["outcomeClassConflicts"],
            [
                {
                    "outcomeFingerprint": fingerprint,
                    "declaredOutcomeClasses": [
                        "exogenous",
                        "intervention-sensitive",
                    ],
                    "issuanceCount": 2,
                    "outcomeIds": ["outcome-1", "outcome-2"],
                    "effectiveOutcomeClass": "intervention-sensitive",
                }
            ],
        )
        self.assertEqual(
            [
                item["declaredOutcomeClass"]
                for item in identity["eligibleIssuances"]
            ],
            ["exogenous", "intervention-sensitive"],
        )
        self.assertTrue(
            all(
                item["outcomeClass"] == "intervention-sensitive"
                and item["outcomeClassConflict"]
                for item in identity["eligibleIssuances"]
            )
        )

    def test_invalid_relabelled_retry_taints_valid_fingerprint_out_of_brier(self):
        fingerprint = "corrupt-mixed-class-event"
        valid = complete_run(
            1,
            outcome_class="exogenous",
            probability=100,
            came_true=True,
            outcome_fingerprint=fingerprint,
        )
        corrupt = complete_run(
            2,
            outcome_class="intervention-sensitive",
            probability=0,
            came_true=False,
            outcome_fingerprint=fingerprint,
            related_outcome_ids=("outcome-1",),
        )
        corrupt[1]["_captureSchemaError"] = "outcome class conflicts for fingerprint"

        report = self.report([activation(), *valid, *corrupt])

        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertEqual(report["outcomeCounts"]["exogenousV2"], 0)
        self.assertEqual(report["outcomeCounts"]["interventionSensitiveV2"], 1)
        self.assertEqual(
            report["outcomeIdentityDiagnostics"][
                "outcomeClassConflictFingerprintCount"
            ],
            1,
        )
        eligible = report["outcomeIdentityDiagnostics"]["eligibleIssuances"]
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]["declaredOutcomeClass"], "exogenous")
        self.assertEqual(eligible[0]["outcomeClass"], "intervention-sensitive")
        self.assertTrue(eligible[0]["outcomeClassConflict"])
        excluded = report["excludedOrInvalidOutcomeStratum"]["issuances"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["declaredOutcomeClass"], "intervention-sensitive")
        self.assertTrue(excluded[0]["outcomeClassConflict"])

    def test_unresolved_invalid_outcome_reuse_is_explicit_but_not_headline(self):
        valid = complete_run(1)
        invalid = complete_run(2)
        reused_outcome = deepcopy(valid[1]["sharedOutcome"])
        invalid[1]["sharedOutcome"] = reused_outcome
        invalid[3]["sharedOutcome"] = reused_outcome
        invalid[3]["predictions"][0]["outcomeId"] = reused_outcome["outcomeId"]
        invalid[3]["predictions"][0]["claim"] = reused_outcome["claim"]
        invalid[1]["_captureSchemaError"] = "duplicate V2 outcomeId: outcome-1"

        report = self.report([activation(), *valid, *invalid])

        self.assertEqual(report["outcomeCounts"]["exogenousV2"], 1)
        self.assertEqual(report["outcomeCounts"]["resolvedExogenousV2"], 0)
        self.assertEqual(report["resolvedExogenousPolarity"]["resolvedOutcomeCount"], 0)
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        excluded = report["excludedOrInvalidOutcomeStratum"]
        self.assertEqual(excluded["issuanceCount"], 1)
        self.assertEqual(excluded["unresolvedIssuanceCount"], 1)
        self.assertEqual(excluded["knownOutcomeIds"], ["outcome-1"])
        self.assertEqual(excluded["issuances"][0]["runId"], "run-2")
        self.assertIn(
            "schema-invalid-record:council-attempt-v2",
            excluded["issuances"][0]["exclusionReasons"],
        )

    def test_uniquely_invalid_resolved_issuance_cannot_change_headlines(self):
        invalid = complete_run(1, probability=100, came_true=True)
        invalid[1]["_captureSchemaError"] = "attempt failed strict schema validation"

        report = self.report([activation(), *invalid])

        self.assertEqual(
            report["outcomeCounts"],
            {
                "v1OrLegacy": 0,
                "exogenousV2": 0,
                "interventionSensitiveV2": 0,
                "resolvedExogenousV2": 0,
                "resolvedInterventionSensitiveV2": 0,
                "eligibleV2IssuanceCount": 0,
                "uniqueUnderlyingFingerprintCount": 0,
                "exogenousV2IssuanceCount": 0,
                "interventionSensitiveV2IssuanceCount": 0,
                "resolvedExogenousV2IssuanceCount": 0,
                "resolvedInterventionSensitiveV2IssuanceCount": 0,
            },
        )
        self.assertEqual(
            report["resolvedExogenousPolarity"],
            {
                "unit": "unique underlying sharedOutcome fingerprint",
                "resolvedIssuanceCount": 0,
                "resolvedOutcomeCount": 0,
                "trueCount": 0,
                "falseCount": 0,
                "majorityFraction": None,
                "warningAboveEightyPercent": False,
                "conflictingResolutionFingerprintCount": 0,
            },
        )
        accuracy = report["descriptiveForecastAccuracy"]
        self.assertEqual(accuracy["predictionCount"], 0)
        self.assertIsNone(accuracy["meanBrier"])
        self.assertEqual(
            accuracy["excludedOrInvalidStratum"]["resolvedIssuanceCount"], 1
        )
        excluded = report["excludedOrInvalidOutcomeStratum"]
        self.assertEqual(excluded["resolvedIssuanceCount"], 1)
        self.assertEqual(excluded["unresolvedIssuanceCount"], 0)
        self.assertTrue(excluded["issuances"][0]["cameTrue"])

    def test_missing_report_time_artifact_verifier_blocks_headline_analysis(self):
        report = analyze_capture_data(
            [activation(), *complete_run(1, came_true=True)],
            as_of=AS_OF,
            artifact_integrity=None,
        )

        run = report["cohort"]["runs"][0]
        self.assertFalse(run["eligibleForHeadlineAnalysis"])
        self.assertIn("artifact-integrity-not-checked", run["incompleteReasons"])
        self.assertFalse(report["artifacts"]["integrityCheckApplied"])
        self.assertEqual(report["findings"]["findingCount"], 0)
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]["findingCount"], 1
        )
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)

    def test_hindsight_base_rate_uses_same_prediction_weighting_as_mean_brier(self):
        true_run = complete_run(1, probability=100, came_true=True)
        ops_plan = {
            "seatId": "ops",
            "role": "voting",
            "agentVersion": "v1",
            "agentDefinitionDigest": "digest-v1-ops",
        }
        # The attempt and completion share the fixture's plan object, as the real
        # schema requires them to be byte-identical.
        true_run[1]["seatPlan"].append(ops_plan)
        true_run[2]["seatStates"]["ops"] = "submitted"
        true_run[3]["seatResults"].append(
            {
                **ops_plan,
                "state": "submitted",
                "launcherAttempts": 1,
                "inputArtifact": artifact("i1ops"),
                "outputArtifact": artifact("o1ops"),
                "modelId": "model",
                "toolPolicy": "read-only",
                "repositoryCommit": "abc123",
            }
        )
        true_run[3]["findings"].append(
            {
                "findingId": "finding-1-ops",
                "seatId": "ops",
                "group": {"findingGroupId": "group-1-ops"},
                "operatorDisposition": {"kind": "new-acted"},
            }
        )
        true_run[3]["predictions"].append(
            {
                "predictionId": "prediction-1-ops",
                "outcomeId": "outcome-1",
                "seat": "ops",
                "type": "shared",
                "claim": "Outcome 1",
                "probability": 100,
                "resolutionDate": "2026-09-01",
                "resolvedBy": "inspect evidence",
            }
        )
        false_run = complete_run(2, probability=100, came_true=False)

        accuracy = self.report(
            [activation(), *true_run, *false_run]
        )["descriptiveForecastAccuracy"]
        self.assertEqual(accuracy["predictionCount"], 3)
        self.assertAlmostEqual(accuracy["meanBrier"], 1 / 3)
        self.assertAlmostEqual(
            accuracy["predictionWeightedObservedTrueFraction"], 2 / 3
        )
        self.assertAlmostEqual(accuracy["hindsightBaseRateBrierBound"], 2 / 9)
        self.assertEqual(
            accuracy["hindsightBaseRateWeighting"],
            "same prediction rows as meanBrier",
        )

    def test_outcome_strata_and_headline_brier_exclude_legacy_and_sensitive(self):
        rows = [activation()]
        rows.append(
            {
                "schemaVersion": 1,
                "kind": "council-attempt",
                "runId": "legacy-run",
                "sharedOutcome": {"outcomeId": "legacy-outcome"},
            }
        )
        for number in range(1, 6):
            rows.extend(
                complete_run(
                    number,
                    probability=100,
                    version="v1" if number < 5 else "v2",
                    came_true=number < 5,
                )
            )
        rows.extend(
            complete_run(
                20,
                outcome_class="intervention-sensitive",
                probability=0,
                came_true=False,
            )
        )
        report = self.report(rows)
        self.assertEqual(
            report["outcomeCounts"],
            {
                "v1OrLegacy": 1,
                "exogenousV2": 5,
                "interventionSensitiveV2": 1,
                "resolvedExogenousV2": 5,
                "resolvedInterventionSensitiveV2": 1,
                "eligibleV2IssuanceCount": 6,
                "uniqueUnderlyingFingerprintCount": 6,
                "exogenousV2IssuanceCount": 5,
                "interventionSensitiveV2IssuanceCount": 1,
                "resolvedExogenousV2IssuanceCount": 5,
                "resolvedInterventionSensitiveV2IssuanceCount": 1,
            },
        )
        self.assertFalse(
            report["resolvedExogenousPolarity"]["warningAboveEightyPercent"]
        )
        accuracy = report["descriptiveForecastAccuracy"]
        self.assertEqual(accuracy["predictionCount"], 5)
        self.assertAlmostEqual(accuracy["meanBrier"], 0.2)
        self.assertEqual(len(accuracy["seatVersionStrata"]), 2)

        rows.extend(complete_run(6, probability=100, came_true=True))
        warning = self.report(rows)
        self.assertTrue(
            warning["resolvedExogenousPolarity"]["warningAboveEightyPercent"]
        )
        self.assertEqual(warning["descriptiveForecastAccuracy"]["predictionCount"], 6)
        self.assertAlmostEqual(
            warning["descriptiveForecastAccuracy"]["meanBrier"], 1 / 6
        )

    def test_report_is_deterministic_and_uses_only_frozen_non_verdict_labels(self):
        rows = [activation()]
        for number in range(1, 11):
            rows.extend(complete_run(number))
        first = self.report(rows)
        second = self.report(deepcopy(rows))
        self.assertEqual(first, second)
        rendered = json.dumps(first, sort_keys=True).lower()
        forbidden = (
            "calibration proof",
            "redundancy",
            "replaceability",
            "marginal value",
            "decision value",
            "causality",
            "causal",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, rendered)
        self.assertEqual(first["reportLabels"]["seatComparison"], "NO VERDICT")
        self.assertEqual(first["prospectiveAudit"]["status"], "NOT_IMPLEMENTED")
        self.assertTrue(first["prospectiveAudit"]["activationBlocking"])
        self.assertEqual(first["durability"]["status"], "NOT_SUPPLIED")
        self.assertTrue(first["durability"]["activationBlocking"])
        self.assertEqual(first["activationReadiness"]["status"], "BLOCKED")

    def test_verified_activation_evidence_separates_historical_and_current_health(self):
        evidence = {
            "appendReady": True,
            "blockers": [],
            "activationVerdict": {"ready": True, "blockers": []},
            "currentHealth": {"healthy": True, "blockers": []},
        }
        report = self.report([activation()], activation_evidence=evidence)
        self.assertEqual(report["prospectiveAudit"]["status"], "PROTOCOL_READY")
        self.assertFalse(report["prospectiveAudit"]["activationBlocking"])
        self.assertEqual(report["durability"]["status"], "HEALTHY")
        self.assertTrue(report["durability"]["historicallyValidAtActivation"])
        self.assertEqual(report["activationReadiness"]["status"], "READY")


if __name__ == "__main__":
    unittest.main()
