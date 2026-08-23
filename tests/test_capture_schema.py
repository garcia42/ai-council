import copy
import json
import re
import threading
import unittest
from datetime import datetime, timezone

from council_tools.artifacts import secret_detectors as artifact_secret_detectors
from council_tools.capture_schema import (
    CaptureSchemaError,
    INVALIDATION_REASONS,
    blind_brief_identity,
    forecast_request_binding_v2,
    forecast_request_identity_v2,
    invalidated_run_ids,
    make_capture_activation,
    make_capture_initiation,
    make_capture_invalidation,
    make_council_attempt_v2,
    make_council_seats_finished,
    make_council_v2,
    outcome_fingerprint_v2,
    outcome_id_v2,
    parse_forecast_request_binding_v2,
    prepare_council_v2,
    raw_payload_secret_detectors,
    seat_input_manifest_sha256,
    strict_json_loads,
    validate_v2_ledger,
    validate_v2_record,
)


RUNTIME_COMMIT = "a" * 40
BASELINE_BLOB = "b" * 40
BASELINE_SHA = "c" * 64
AGENT_CODE_SHA = "d" * 64
AGENT_BLIND_SHA = "e" * 64
DIFF_SHA = "f" * 64


def at(value):
    return lambda: value


def artifact(digit, byte_count=10):
    digest = digit * 64
    return {
        "path": f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.bin",
        "sha256": digest,
        "bytes": byte_count,
    }


class CaptureSchemaTest(unittest.TestCase):
    def setUp(self):
        self.activation = make_capture_activation(
            {
                "cohortName": "first-ten-v2",
                "captureVersion": "capture-v2.0.0",
                "runtimeSourceCommit": RUNTIME_COMMIT,
                "artifactRootPolicy": "private-content-addressed-v1",
            },
            clock=at("2026-08-23T10:00:00Z"),
        )
        self.rows = [self.activation]
        self.initiation = make_capture_initiation(
            {
                "activationId": self.activation["activationId"],
                "idempotencyKey": "decision-001-first-convening",
            },
            prior_rows=self.rows,
            clock=at("2026-08-23T10:01:00Z"),
        )
        self.rows.append(self.initiation)
        self.decision_artifact = {
            **artifact("c", 1234),
            "gitBlob": BASELINE_BLOB,
        }
        self.seat_plan = [
            {
                "seatId": "code",
                "role": "voting",
                "agentVersion": "code-v7",
                "agentDefinitionDigest": AGENT_CODE_SHA,
            },
            {
                "seatId": "blind",
                "role": "control",
                "agentVersion": "blind-v3",
                "agentDefinitionDigest": AGENT_BLIND_SHA,
            },
        ]
        self.code_input = artifact("1", 100)
        self.code_output = artifact("2", 200)
        self.blind_input = artifact("3", 110)
        self.blind_output = artifact("4", 210)
        input_manifest = seat_input_manifest_sha256(
            {"code": self.code_input, "blind": self.blind_input}
        )
        decision_link = (
            f"commit={RUNTIME_COMMIT};blob={BASELINE_BLOB};sha256={BASELINE_SHA};"
            f"inputManifestSha256={input_manifest}"
        )
        self.attempt_payload = {
            "initiationId": self.initiation["initiationId"],
            "decisionFamilyId": "family-capture-usefulness",
            "question": "Should this implementation proceed?",
            "decisionBeforeArtifact": self.decision_artifact,
            "outcomeClass": "intervention-sensitive",
            "outcomeClassRationale": "The council can directly change the implementation.",
            "evidenceCutoffAt": "2026-08-23T10:00:30Z",
            "seatPlan": self.seat_plan,
            "sharedOutcome": {
                "claim": "The capture soak passes its frozen gates.",
                "resolutionDate": "2026-10-31",
                "resolvedBy": "Inspect the frozen first-ten report.",
                "decisionLink": decision_link,
                "materiality": "A failure blocks usefulness claims.",
                "actionIfTrue": "Continue capture.",
                "actionIfFalse": "Reduce scope and repeat.",
                "relatedOutcomeIds": [],
            },
        }
        self.attempt = make_council_attempt_v2(
            self.attempt_payload,
            prior_rows=self.rows,
            clock=at("2026-08-23T10:02:00Z"),
        )
        self.rows.append(self.attempt)
        self.finished = make_council_seats_finished(
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            prior_rows=self.rows,
            clock=at("2026-08-23T10:03:00Z"),
        )
        self.rows.append(self.finished)
        self.seat_results = [
            {
                **self.seat_plan[0],
                "state": "submitted",
                "launcherAttempts": 1,
                "inputArtifact": self.code_input,
                "outputArtifact": self.code_output,
                "modelId": "model-code",
                "toolPolicy": "read-only-v1",
                "repositoryCommit": RUNTIME_COMMIT,
                "diffDigest": DIFF_SHA,
                "latencyMs": 1200,
                "inputTokens": 500,
                "outputTokens": 200,
                "costUsd": 0.12,
            },
            {
                **self.seat_plan[1],
                "state": "submitted",
                "launcherAttempts": 2,
                "inputArtifact": self.blind_input,
                "outputArtifact": self.blind_output,
                "modelId": "model-blind",
                "toolPolicy": "no-tools-v1",
                "repositoryCommit": RUNTIME_COMMIT,
            },
        ]
        self.completion_payload = {
            "runId": self.initiation["runId"],
            "seatResults": self.seat_results,
            "findings": [],
            "noFindings": [
                {
                    "kind": "no-findings",
                    "seatId": "code",
                    "outputArtifact": self.code_output,
                },
                {
                    "kind": "no-findings",
                    "seatId": "blind",
                    "outputArtifact": self.blind_output,
                },
            ],
            "probabilities": {"code": 65, "blind": 45},
            "blindSeat": {
                "role": "independent-control",
                "required": True,
                "ran": True,
                "changedDecision": False,
                "brief": blind_brief_identity(
                    self.initiation["runId"], self.blind_input["path"]
                ),
            },
        }

    def make_completion(self, payload=None, *, when="2026-08-23T10:04:00Z", rows=None):
        return make_council_v2(
            payload or self.completion_payload,
            prior_rows=rows or self.rows,
            clock=at(when),
        )

    def test_forecast_request_identity_is_canonical_without_manifest_cycle(self):
        outcome = self.attempt["sharedOutcome"]
        other_link = outcome["decisionLink"].replace(
            "inputManifestSha256=" + seat_input_manifest_sha256(
                {"code": self.code_input, "blind": self.blind_input}
            ),
            "inputManifestSha256=" + "9" * 64,
        )
        self.assertEqual(
            outcome["fingerprint"],
            outcome_fingerprint_v2(
                outcome["claim"],
                outcome["resolutionDate"],
                outcome["resolvedBy"],
                other_link,
            ),
        )
        self.assertEqual(
            outcome["outcomeId"],
            outcome_id_v2(self.initiation["runId"], outcome["claim"]),
        )
        identity = forecast_request_identity_v2(
            self.initiation["runId"],
            outcome["outcomeId"],
            outcome["fingerprint"],
            self.attempt["evidenceCutoffAt"],
            outcome["claim"],
            outcome["resolutionDate"],
            outcome["resolvedBy"],
            outcome["materiality"],
            outcome["actionIfTrue"],
            outcome["actionIfFalse"],
        )
        binding = forecast_request_binding_v2(
            self.initiation["runId"],
            outcome["outcomeId"],
            outcome["fingerprint"],
            self.attempt["evidenceCutoffAt"],
            outcome["claim"],
            outcome["resolutionDate"],
            outcome["resolvedBy"],
            outcome["materiality"],
            outcome["actionIfTrue"],
            outcome["actionIfFalse"],
        )
        parsed = parse_forecast_request_binding_v2(binding.encode())
        self.assertEqual(
            set(parsed),
            {
                "schemaVersion",
                "runId",
                "outcomeId",
                "outcomeFingerprint",
                "evidenceCutoffAt",
                "claim",
                "resolutionDate",
                "resolvedBy",
                "materiality",
                "actionIfTrue",
                "actionIfFalse",
            },
        )
        self.assertEqual(parsed["schemaVersion"], 1)
        for field in (
            "claim",
            "resolutionDate",
            "resolvedBy",
            "materiality",
            "actionIfTrue",
            "actionIfFalse",
        ):
            self.assertEqual(parsed[field], outcome[field])
        self.assertEqual(len(identity["forecastRequestSha256"]), 64)
        changed_action = forecast_request_identity_v2(
            self.initiation["runId"],
            outcome["outcomeId"],
            outcome["fingerprint"],
            self.attempt["evidenceCutoffAt"],
            outcome["claim"],
            outcome["resolutionDate"],
            outcome["resolvedBy"],
            outcome["materiality"],
            outcome["actionIfTrue"],
            "A contradictory action.",
        )
        self.assertNotEqual(
            changed_action["forecastRequestSha256"],
            identity["forecastRequestSha256"],
        )
        with self.assertRaisesRegex(CaptureSchemaError, "exactly one"):
            parse_forecast_request_binding_v2((binding + "\n" + binding).encode())
        begin, canonical, end = binding.splitlines()
        with self.assertRaisesRegex(CaptureSchemaError, "out of order"):
            parse_forecast_request_binding_v2(
                (end + "\n" + canonical + "\n" + begin).encode()
            )
        with self.assertRaisesRegex(CaptureSchemaError, "not canonical"):
            parse_forecast_request_binding_v2(
                (begin + "\n" + canonical.replace(":", ": ", 1) + "\n" + end).encode()
            )

    def test_complete_lifecycle_validates_and_copies_attempt_bindings(self):
        completion = self.make_completion()
        rows = [*self.rows, completion]
        self.assertEqual(validate_v2_ledger(rows), rows)
        for field in (
            "activationId",
            "initiationId",
            "decisionFamilyId",
            "decisionBeforeArtifact",
            "outcomeClass",
            "outcomeClassRationale",
            "evidenceCutoffAt",
            "seatPlan",
            "sharedOutcome",
        ):
            self.assertEqual(completion[field], self.attempt[field])
        self.assertEqual(
            {prediction["seat"] for prediction in completion["predictions"]},
            {"code", "blind"},
        )

    def test_record_shapes_are_exact_and_kind_specific(self):
        completion = self.make_completion()
        expected = {
            "capture-activation": {
                "schemaVersion", "kind", "activationId", "activatedAt", "cohortName",
                "captureVersion", "runtimeSourceCommit", "artifactRootPolicy",
            },
            "capture-initiation": {
                "schemaVersion", "kind", "initiationId", "runId", "activationId",
                "idempotencyKey", "handlingStartedAt",
            },
            "council-attempt-v2": {
                "schemaVersion", "kind", "runId", "initiationId", "activationId",
                "seatsLaunchedAt", "decisionFamilyId", "question",
                "decisionBeforeArtifact", "outcomeClass", "outcomeClassRationale",
                "evidenceCutoffAt", "seatPlan", "sharedOutcome",
            },
            "council-seats-finished": {
                "schemaVersion", "kind", "runId", "initiationId", "activationId",
                "seatsFinishedAt", "seatStates",
            },
            "council-v2": {
                "schemaVersion", "kind", "runId", "initiationId", "activationId",
                "finalizedAt", "decisionFamilyId", "question", "decisionBeforeArtifact",
                "outcomeClass", "outcomeClassRationale", "evidenceCutoffAt", "seatPlan",
                "sharedOutcome", "seatResults", "findings", "noFindings", "predictions",
                "blindSeat",
            },
        }
        for row in [*self.rows, completion]:
            with self.subTest(kind=row["kind"]):
                self.assertEqual(set(row), expected[row["kind"]])

    def test_all_user_injected_boundary_timestamps_are_rejected(self):
        constructors = [
            (
                make_capture_activation,
                {
                    "cohortName": "other", "captureVersion": "v2", "runtimeSourceCommit": RUNTIME_COMMIT,
                    "artifactRootPolicy": "private", "activatedAt": "2026-08-23T00:00:00Z",
                },
                [],
            ),
            (
                make_capture_initiation,
                {"activationId": self.activation["activationId"], "idempotencyKey": "x", "handlingStartedAt": "2026-08-23T00:00:00Z"},
                [self.activation],
            ),
            (
                make_council_attempt_v2,
                {**self.attempt_payload, "seatsLaunchedAt": "2026-08-23T00:00:00Z"},
                self.rows[:2],
            ),
            (
                make_council_seats_finished,
                {"runId": self.initiation["runId"], "seatStates": self.finished["seatStates"], "seatsFinishedAt": "2026-08-23T00:00:00Z"},
                self.rows[:3],
            ),
            (
                make_council_v2,
                {**self.completion_payload, "finalizedAt": "2026-08-23T00:00:00Z"},
                self.rows,
            ),
            (
                make_capture_invalidation,
                {"runId": self.initiation["runId"], "reason": "identity-error", "operator": "operator", "evidenceRef": "ticket-1", "invalidatedAt": "2026-08-23T00:00:00Z"},
                self.rows,
            ),
        ]
        for constructor, payload, rows in constructors:
            with self.subTest(constructor=constructor.__name__):
                with self.assertRaisesRegex(CaptureSchemaError, "cannot inject"):
                    constructor(payload, prior_rows=rows, clock=at("2026-08-23T10:05:00Z"))

    def test_clock_must_be_timezone_aware(self):
        with self.assertRaisesRegex(CaptureSchemaError, "timezone-aware"):
            make_capture_activation(
                {
                    "cohortName": "other", "captureVersion": "v2", "runtimeSourceCommit": RUNTIME_COMMIT,
                    "artifactRootPolicy": "private",
                },
                clock=lambda: datetime(2026, 8, 23),
            )

    def test_v1_rows_are_ignored_and_not_reinterpreted(self):
        malformed_but_v1 = {
            "schemaVersion": 1,
            "kind": "council",
            "schemaVersion2LookingField": {"nonsense": True},
        }
        legacy = {"kind": "council", "timestamp": "not even a timestamp"}
        self.assertFalse(validate_v2_record(malformed_but_v1))
        self.assertFalse(validate_v2_record(legacy))
        self.assertEqual(
            validate_v2_ledger([malformed_but_v1, legacy, *self.rows]), self.rows
        )

    def test_v2_kind_cannot_masquerade_as_v1(self):
        row = copy.deepcopy(self.activation)
        row["schemaVersion"] = 1
        with self.assertRaisesRegex(CaptureSchemaError, "schemaVersion must be 2"):
            validate_v2_record(row)

    def test_unknown_versions_and_v2_kinds_fail_closed(self):
        with self.assertRaisesRegex(CaptureSchemaError, "unknown capture schemaVersion"):
            validate_v2_record({"schemaVersion": 3, "kind": "future"})
        with self.assertRaisesRegex(CaptureSchemaError, "unknown schemaVersion 2 record kind"):
            validate_v2_record({"schemaVersion": 2, "kind": "mystery"})

    def test_every_closed_enum_rejects_json_non_text_types_as_schema_errors(self):
        malformed_values = (
            strict_json_loads(
                '{"secret-shaped-enum":"must-not-be-used-as-a-key"}'
            ),
            strict_json_loads('["submitted"]'),
            strict_json_loads("true"),
            strict_json_loads("7"),
        )

        def attempt_with(field, value):
            payload = copy.deepcopy(self.attempt_payload)
            payload[field] = value
            make_council_attempt_v2(
                payload,
                prior_rows=self.rows[:2],
                clock=at("2026-08-23T10:02:00Z"),
            )

        def seat_plan_role(value):
            payload = copy.deepcopy(self.attempt_payload)
            payload["seatPlan"][0]["role"] = value
            make_council_attempt_v2(
                payload,
                prior_rows=self.rows[:2],
                clock=at("2026-08-23T10:02:00Z"),
            )

        def finished_state(value):
            make_council_seats_finished(
                {
                    "runId": self.initiation["runId"],
                    "seatStates": {"code": "submitted", "blind": value},
                },
                prior_rows=self.rows[:3],
                clock=at("2026-08-23T10:03:00Z"),
            )

        def completion_with(path, value):
            payload = copy.deepcopy(self.completion_payload)
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            self.make_completion(payload)

        def disposition_kind(value):
            payload = copy.deepcopy(self.completion_payload)
            payload["findings"] = [
                {
                    "findingId": "finding-" + "1" * 32,
                    "seatId": "code",
                    "category": "correctness",
                    "claim": "A defect exists.",
                    "severity": "block",
                    "proposedAction": "Repair it.",
                    "evidenceSummary": "Test evidence.",
                    "group": {
                        "findingGroupId": "finding-group-" + "2" * 32,
                        "runId": self.initiation["runId"],
                    },
                    "operatorDisposition": {"kind": value},
                }
            ]
            payload["noFindings"] = payload["noFindings"][1:]
            self.make_completion(payload)

        def prediction_type(value):
            completion = self.make_completion()
            completion["predictions"][0]["type"] = value
            validate_v2_record(completion, self.rows)

        def invalidation_reason(value):
            make_capture_invalidation(
                {
                    "runId": self.initiation["runId"],
                    "reason": value,
                    "operator": "operator",
                    "evidenceRef": "ticket",
                },
                prior_rows=self.rows,
                clock=at("2026-08-23T10:05:00Z"),
            )

        cases = (
            ("record.kind", lambda value: validate_v2_record({"schemaVersion": 2, "kind": value})),
            ("seatPlan[0].role", seat_plan_role),
            ("seatStates.blind", finished_state),
            ("seatResults[0].state", lambda value: completion_with(("seatResults", 0, "state"), value)),
            ("seatResults[0].role", lambda value: completion_with(("seatResults", 0, "role"), value)),
            ("blindSeat.role", lambda value: completion_with(("blindSeat", "role"), value)),
            ("findings[0].operatorDisposition.kind", disposition_kind),
            ("noFindings[0].kind", lambda value: completion_with(("noFindings", 0, "kind"), value)),
            ("predictions[0].type", prediction_type),
            ("outcomeClass", lambda value: attempt_with("outcomeClass", value)),
            ("capture-invalidation reason", invalidation_reason),
        )
        for field, invoke in cases:
            for value in malformed_values:
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaisesRegex(
                        CaptureSchemaError,
                        re.escape(field) + ".*non-empty text",
                    ):
                        invoke(value)

    def test_strict_json_rejects_duplicate_keys_and_all_nonfinite_forms(self):
        self.assertEqual(strict_json_loads(b'{"outer":{"value":1}}'), {"outer": {"value": 1}})
        with self.assertRaisesRegex(CaptureSchemaError, "^invalid JSON: duplicate key$"):
            strict_json_loads('{"outer":{"value":1,"value":2}}')
        for spelling in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(spelling=spelling):
                with self.assertRaisesRegex(
                    CaptureSchemaError, "^invalid JSON: non-finite number$"
                ):
                    strict_json_loads(f'{{"value":{spelling}}}')

    def test_strict_json_normalizes_excessive_nesting(self):
        deeply_nested = "[" * 2000 + "0" + "]" * 2000

        with self.assertRaisesRegex(CaptureSchemaError, "^invalid JSON$") as raised:
            strict_json_loads(deeply_nested)

        self.assertNotIn("recursion", str(raised.exception).lower())

    def test_programmatic_nonfinite_metrics_fail_strict_writer_validation(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            payload = copy.deepcopy(self.completion_payload)
            payload["seatResults"][0]["costUsd"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(CaptureSchemaError, "non-negative number"):
                    self.make_completion(payload)

    def test_unknown_top_level_and_nested_keys_fail_closed(self):
        row = self.make_completion()
        row["surprise"] = True
        with self.assertRaisesRegex(CaptureSchemaError, "unknown keys"):
            validate_v2_record(row, self.rows)
        row = self.make_completion()
        row["seatPlan"][0]["nickname"] = "helpful"
        with self.assertRaisesRegex(CaptureSchemaError, "unknown keys"):
            validate_v2_record(row, self.rows)

    def test_only_one_activation_is_allowed_globally(self):
        duplicate = copy.deepcopy(self.activation)
        with self.assertRaisesRegex(CaptureSchemaError, "already has an activation"):
            validate_v2_record(duplicate, [self.activation])

        with self.assertRaisesRegex(CaptureSchemaError, "already has an activation"):
            make_capture_activation(
                {
                    "cohortName": "second-cohort",
                    "captureVersion": "capture-v2.0.1",
                    "runtimeSourceCommit": "9" * 40,
                    "artifactRootPolicy": "another-private-root-policy",
                },
                prior_rows=[self.activation],
                clock=at("2026-08-23T11:00:00Z"),
            )

    def test_activation_accepts_only_an_exact_approval_manifest_artifact_ref(self):
        manifest_ref = artifact("9", 321)
        activation = make_capture_activation(
            {
                "cohortName": "approved-first-ten",
                "captureVersion": "capture-v2.0.0",
                "runtimeSourceCommit": RUNTIME_COMMIT,
                "artifactRootPolicy": "private-content-addressed-v1",
                "approvalManifest": manifest_ref,
            },
            clock=at("2026-08-23T10:00:00Z"),
        )
        self.assertEqual(activation["approvalManifest"], manifest_ref)
        validate_v2_record(activation)

        invalid = copy.deepcopy(activation)
        invalid["approvalManifest"]["mediaType"] = "application/json"
        with self.assertRaisesRegex(CaptureSchemaError, "unknown keys"):
            validate_v2_record(invalid)

    def test_idempotent_initiation_returns_original_without_calling_clock(self):
        def forbidden_clock():
            raise AssertionError("idempotent retry must not create a new boundary")

        replay = make_capture_initiation(
            {
                "activationId": self.activation["activationId"],
                "idempotencyKey": self.initiation["idempotencyKey"],
            },
            prior_rows=[self.activation, self.initiation],
            clock=forbidden_clock,
        )
        self.assertEqual(replay, self.initiation)
        self.assertIsNot(replay, self.initiation)

    def test_concurrent_same_key_construction_has_stable_ids_and_second_append_fails(self):
        created = []
        errors = []

        def build():
            try:
                created.append(
                    make_capture_initiation(
                        {"activationId": self.activation["activationId"], "idempotencyKey": "concurrent-key"},
                        prior_rows=[self.activation],
                        clock=at("2026-08-23T10:01:30Z"),
                    )
                )
            except Exception as exc:  # asserted by parent thread
                errors.append(exc)

        threads = [threading.Thread(target=build) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(created[0], created[1])
        with self.assertRaisesRegex(CaptureSchemaError, "duplicate .*initiation"):
            validate_v2_record(created[1], [self.activation, created[0]])

    def test_same_key_with_forged_distinct_ids_still_fails(self):
        duplicate = copy.deepcopy(self.initiation)
        duplicate["initiationId"] = "initiation-" + "8" * 32
        duplicate["runId"] = "run-" + "7" * 32
        with self.assertRaisesRegex(CaptureSchemaError, "idempotencyKey"):
            validate_v2_record(duplicate, [self.activation, self.initiation])

    def test_distinct_key_reconvenes_new_run_in_same_family(self):
        second_init = make_capture_initiation(
            {"activationId": self.activation["activationId"], "idempotencyKey": "decision-001-reconvene"},
            prior_rows=[self.activation, self.initiation],
            clock=at("2026-08-23T10:05:00Z"),
        )
        second_payload = copy.deepcopy(self.attempt_payload)
        second_payload["initiationId"] = second_init["initiationId"]
        second_payload["sharedOutcome"]["relatedOutcomeIds"] = [
            self.attempt["sharedOutcome"]["outcomeId"]
        ]
        second_attempt = make_council_attempt_v2(
            second_payload,
            prior_rows=[self.activation, self.initiation, self.attempt, second_init],
            clock=at("2026-08-23T10:06:00Z"),
        )
        self.assertNotEqual(second_init["runId"], self.initiation["runId"])
        self.assertEqual(second_attempt["decisionFamilyId"], self.attempt["decisionFamilyId"])

    def test_resume_is_allowed_before_attempt_but_not_after_attempt_exists(self):
        resumed = make_council_attempt_v2(
            self.attempt_payload,
            prior_rows=self.rows[:2],
            clock=at("2026-08-23T10:02:00Z"),
        )
        self.assertEqual(resumed, self.attempt)
        with self.assertRaisesRegex(CaptureSchemaError, "duplicate council-attempt-v2"):
            make_council_attempt_v2(
                self.attempt_payload,
                prior_rows=self.rows[:3],
                clock=at("2026-08-23T10:02:30Z"),
            )

    def test_an_initiation_without_attempt_is_valid_and_observable(self):
        self.assertEqual(
            validate_v2_ledger([self.activation, self.initiation]),
            [self.activation, self.initiation],
        )

    def test_orphan_and_forged_references_fail(self):
        with self.assertRaisesRegex(CaptureSchemaError, "no prior initiation"):
            validate_v2_record(self.attempt, [self.activation])
        with self.assertRaisesRegex(CaptureSchemaError, "no prior attempt"):
            validate_v2_record(self.finished, self.rows[:2])
        completion = self.make_completion()
        with self.assertRaisesRegex(CaptureSchemaError, "no prior attempt"):
            validate_v2_record(completion, [self.activation, self.initiation])
        forged = copy.deepcopy(self.finished)
        forged["initiationId"] = "initiation-" + "7" * 32
        with self.assertRaisesRegex(CaptureSchemaError, "identity differs"):
            validate_v2_record(forged, self.rows[:3])

    def test_duplicate_attempt_seats_finished_and_completion_fail(self):
        with self.assertRaisesRegex(CaptureSchemaError, "duplicate council-attempt-v2"):
            validate_v2_record(copy.deepcopy(self.attempt), self.rows[:3])
        with self.assertRaisesRegex(CaptureSchemaError, "duplicate council-seats-finished"):
            validate_v2_record(copy.deepcopy(self.finished), self.rows)
        completion = self.make_completion()
        with self.assertRaisesRegex(CaptureSchemaError, "duplicate council-v2"):
            validate_v2_record(copy.deepcopy(completion), [*self.rows, completion])

    def test_boundary_ordering_is_fail_closed_at_every_stage(self):
        cases = []
        initiation = copy.deepcopy(self.initiation)
        initiation["handlingStartedAt"] = "2026-08-23T09:59:59Z"
        cases.append((initiation, [self.activation], "precedes activation"))
        attempt = copy.deepcopy(self.attempt)
        attempt["seatsLaunchedAt"] = "2026-08-23T10:00:59Z"
        cases.append((attempt, self.rows[:2], "precedes handlingStartedAt"))
        finished = copy.deepcopy(self.finished)
        finished["seatsFinishedAt"] = "2026-08-23T10:01:59Z"
        cases.append((finished, self.rows[:3], "precedes seatsLaunchedAt"))
        completion = self.make_completion()
        completion["finalizedAt"] = "2026-08-23T10:02:59Z"
        cases.append((completion, self.rows, "precedes seatsFinishedAt"))
        for row, prior, message in cases:
            with self.subTest(kind=row["kind"]):
                with self.assertRaisesRegex(CaptureSchemaError, message):
                    validate_v2_record(row, prior)

    def test_finalized_issuance_must_strictly_precede_resolution_date(self):
        for when in (
            "2026-10-31T00:00:00Z",
            "2026-11-01T12:00:00Z",
        ):
            with self.subTest(when=when), self.assertRaisesRegex(
                CaptureSchemaError,
                "finalizedAt must precede sharedOutcome.resolutionDate",
            ):
                self.make_completion(when=when)

        completion = self.make_completion()
        completion["finalizedAt"] = "2026-10-31T00:00:00.000000Z"
        with self.assertRaisesRegex(
            CaptureSchemaError,
            "ledger row 5: finalizedAt must precede",
        ):
            validate_v2_ledger([*self.rows, completion])

    def test_every_planned_blind_state_exactly_binds_persisted_input(self):
        unrelated = artifact("9", 110)
        for state in ("submitted", "abstained", "unavailable"):
            with self.subTest(state=state):
                payload = copy.deepcopy(self.completion_payload)
                rows = self.rows
                if state != "submitted":
                    payload["seatResults"][1] = {
                        **self.seat_plan[1],
                        "state": state,
                        "launcherAttempts": 2,
                        "inputArtifact": self.blind_input,
                    }
                    payload["noFindings"] = payload["noFindings"][:1]
                    payload["probabilities"] = {"code": 65}
                    payload["blindSeat"].update(
                        {
                            "role": "SKIPPED",
                            "ran": False,
                            "changedDecision": None,
                            "blockedReason": f"blind seat {state}",
                        }
                    )
                    prior = self.rows[:-1]
                    finished = make_council_seats_finished(
                        {
                            "runId": self.initiation["runId"],
                            "seatStates": {"code": "submitted", "blind": state},
                        },
                        prior_rows=prior,
                        clock=at("2026-08-23T10:03:00Z"),
                    )
                    rows = [*prior, finished]
                payload["blindSeat"]["brief"] = blind_brief_identity(
                    self.initiation["runId"], unrelated["path"]
                )
                with self.assertRaisesRegex(
                    CaptureSchemaError, "canonical blind brief identity"
                ):
                    prepare_council_v2(payload, prior_rows=rows)
                with self.assertRaisesRegex(
                    CaptureSchemaError, "canonical blind brief identity"
                ):
                    make_council_v2(
                        payload,
                        prior_rows=rows,
                        clock=at("2026-08-23T10:04:00Z"),
                    )

                valid_payload = copy.deepcopy(payload)
                valid_payload["blindSeat"]["brief"] = blind_brief_identity(
                    self.initiation["runId"], self.blind_input["path"]
                )
                completion = make_council_v2(
                    valid_payload,
                    prior_rows=rows,
                    clock=at("2026-08-23T10:04:00Z"),
                )
                completion["blindSeat"]["brief"] = blind_brief_identity(
                    self.initiation["runId"], unrelated["path"]
                )
                with self.assertRaisesRegex(
                    CaptureSchemaError, "canonical blind brief identity"
                ):
                    validate_v2_ledger([*rows, completion])

    def test_completion_inputs_must_match_attempt_manifest(self):
        substituted = artifact("9", 100)
        payload = copy.deepcopy(self.completion_payload)
        payload["seatResults"][0]["inputArtifact"] = substituted
        for constructor in (prepare_council_v2,):
            with self.subTest(constructor=constructor.__name__), self.assertRaisesRegex(
                CaptureSchemaError,
                "seat input artifacts differ from the attempt input manifest",
            ):
                constructor(payload, prior_rows=self.rows)
        with self.assertRaisesRegex(
            CaptureSchemaError,
            "seat input artifacts differ from the attempt input manifest",
        ):
            make_council_v2(
                payload,
                prior_rows=self.rows,
                clock=at("2026-08-23T10:04:00Z"),
            )

        completion = self.make_completion()
        completion["seatResults"][0]["inputArtifact"] = substituted
        with self.assertRaisesRegex(
            CaptureSchemaError,
            "ledger row 5: council-v2 seat input artifacts differ",
        ):
            validate_v2_ledger([*self.rows, completion])

    def test_attempt_input_manifest_binding_is_exact_and_unique(self):
        payload = copy.deepcopy(self.attempt_payload)
        payload["sharedOutcome"]["decisionLink"] += (
            ";inputManifestSha256="
            + seat_input_manifest_sha256(
                {"code": self.code_input, "blind": self.blind_input}
            )
        )
        with self.assertRaisesRegex(CaptureSchemaError, "exactly one"):
            make_council_attempt_v2(
                payload,
                prior_rows=self.rows[:2],
                clock=at("2026-08-23T10:02:00Z"),
            )

    def test_future_boundary_fails_when_observation_time_is_supplied(self):
        with self.assertRaisesRegex(CaptureSchemaError, "future system boundary"):
            validate_v2_ledger(
                [self.activation], now=datetime(2026, 8, 23, 9, 59, tzinfo=timezone.utc)
            )

    def test_evidence_cutoff_cannot_follow_launch(self):
        payload = copy.deepcopy(self.attempt_payload)
        payload["evidenceCutoffAt"] = "2026-08-23T10:02:01Z"
        with self.assertRaisesRegex(CaptureSchemaError, "cannot follow"):
            make_council_attempt_v2(payload, prior_rows=self.rows[:2], clock=at("2026-08-23T10:02:00Z"))

    def test_attempt_requires_outcome_class_rationale_and_uniform_binding(self):
        payload = copy.deepcopy(self.attempt_payload)
        payload["outcomeClassRationale"] = ""
        with self.assertRaisesRegex(CaptureSchemaError, "non-empty"):
            make_council_attempt_v2(payload, prior_rows=self.rows[:2], clock=at("2026-08-23T10:02:00Z"))
        payload = copy.deepcopy(self.attempt_payload)
        payload["sharedOutcome"]["decisionLink"] = "missing the frozen bindings"
        with self.assertRaisesRegex(CaptureSchemaError, "must contain"):
            make_council_attempt_v2(payload, prior_rows=self.rows[:2], clock=at("2026-08-23T10:02:00Z"))

    def test_repeated_outcome_fingerprint_requires_prospective_prior_link(self):
        second_initiation = make_capture_initiation(
            {
                "activationId": self.activation["activationId"],
                "idempotencyKey": "decision-002-retry",
            },
            prior_rows=self.rows,
            clock=at("2026-08-23T10:04:00Z"),
        )
        prior = [*self.rows, second_initiation]
        repeated = copy.deepcopy(self.attempt_payload)
        repeated["initiationId"] = second_initiation["initiationId"]

        with self.assertRaisesRegex(
            CaptureSchemaError,
            "repeated sharedOutcome fingerprint must link a prior matching outcomeId",
        ):
            make_council_attempt_v2(
                repeated,
                prior_rows=prior,
                clock=at("2026-08-23T10:05:00Z"),
            )

        repeated["sharedOutcome"]["relatedOutcomeIds"] = [
            self.attempt["sharedOutcome"]["outcomeId"]
        ]
        linked = make_council_attempt_v2(
            repeated,
            prior_rows=prior,
            clock=at("2026-08-23T10:05:00Z"),
        )
        self.assertEqual(
            linked["sharedOutcome"]["fingerprint"],
            self.attempt["sharedOutcome"]["fingerprint"],
        )
        self.assertNotEqual(
            linked["sharedOutcome"]["outcomeId"],
            self.attempt["sharedOutcome"]["outcomeId"],
        )
        self.assertEqual(
            linked["sharedOutcome"]["relatedOutcomeIds"],
            [self.attempt["sharedOutcome"]["outcomeId"]],
        )

    def test_linked_retry_cannot_relabel_intervention_sensitive_outcome_exogenous(self):
        second_initiation = make_capture_initiation(
            {
                "activationId": self.activation["activationId"],
                "idempotencyKey": "decision-002-reclassified-retry",
            },
            prior_rows=self.rows,
            clock=at("2026-08-23T10:04:00Z"),
        )
        prior = [*self.rows, second_initiation]
        repeated = copy.deepcopy(self.attempt_payload)
        repeated["initiationId"] = second_initiation["initiationId"]
        repeated["outcomeClass"] = "exogenous"
        repeated["outcomeClassRationale"] = "A retry tried to relabel the same outcome."
        repeated["sharedOutcome"]["relatedOutcomeIds"] = [
            self.attempt["sharedOutcome"]["outcomeId"]
        ]

        with self.assertRaisesRegex(
            CaptureSchemaError,
            "repeated sharedOutcome fingerprint must retain one outcomeClass",
        ):
            make_council_attempt_v2(
                repeated,
                prior_rows=prior,
                clock=at("2026-08-23T10:05:00Z"),
            )

    def test_seat_plan_requires_unique_role_and_version_bindings(self):
        payload = copy.deepcopy(self.attempt_payload)
        payload["seatPlan"][1]["seatId"] = "code"
        with self.assertRaisesRegex(CaptureSchemaError, "duplicate seatId"):
            make_council_attempt_v2(payload, prior_rows=self.rows[:2], clock=at("2026-08-23T10:02:00Z"))
        payload = copy.deepcopy(self.attempt_payload)
        payload["seatPlan"][0]["role"] = "observer"
        with self.assertRaisesRegex(CaptureSchemaError, "role"):
            make_council_attempt_v2(payload, prior_rows=self.rows[:2], clock=at("2026-08-23T10:02:00Z"))

    def test_raw_secret_preflight_and_duplicate_seat_errors_never_echo_identifier(self):
        secret = "sk-proj-" + "S" * 40
        payload = copy.deepcopy(self.attempt_payload)
        payload["seatPlan"][0]["seatId"] = secret
        payload["seatPlan"][1]["seatId"] = secret

        self.assertEqual(
            raw_payload_secret_detectors(payload),
            ("openai-api-key",),
        )
        with self.assertRaises(CaptureSchemaError) as raised:
            make_council_attempt_v2(
                payload,
                prior_rows=self.rows[:2],
                clock=at("2026-08-23T10:02:00Z"),
            )
        self.assertEqual(str(raised.exception), "seatPlan contains duplicate seatId")
        self.assertNotIn(secret, str(raised.exception))

    def test_raw_secret_preflight_scans_keys_and_caller_tokens_without_returning_values(self):
        key_secret = "sk-proj-" + "K" * 40
        caller_secret = b"private-runtime-token"
        payload = {key_secret: {"nested": caller_secret.decode("ascii")}}

        detectors = raw_payload_secret_detectors(
            payload,
            secret_tokens=(caller_secret,),
        )

        self.assertEqual(detectors, ("openai-api-key", "caller-token"))
        self.assertNotIn(key_secret, repr(detectors))
        self.assertNotIn(caller_secret.decode("ascii"), repr(detectors))

    def test_raw_secret_preflight_matches_artifact_detector_policy(self):
        caller_secret = b"private-runtime-token"
        aws_secret = b"A" * 40
        samples = (
            b"safe council payload",
            b"-----BEGIN OPENSSH PRIVATE KEY-----",
            b"ghp_" + b"G" * 36,
            b"github_pat_" + b"H" * 22,
            b"sk-svcacct-" + b"O" * 32,
            b"xoxb-" + b"S" * 20,
            b"AWS_SECRET_ACCESS_KEY=" + aws_secret,
            b'{"AWS_SECRET_ACCESS_KEY":"' + aws_secret + b'"}',
            b'{ "AWS_SECRET_ACCESS_KEY" : "' + aws_secret + b'" }',
            b'{"AWS_SECRET_ACCESS_KEY":"' + aws_secret[:-1] + b'"}',
            b'{"AWS_SECRET_ACCESS_KEY":"' + aws_secret + b'A"}',
            b"prefix " + caller_secret + b" suffix",
        )
        for sample in samples:
            payload = {"sample": sample.decode("ascii")}
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=True,
            ).encode("utf-8")
            with self.subTest(sample=sample[:20]):
                self.assertEqual(
                    raw_payload_secret_detectors(
                        payload,
                        secret_tokens=(caller_secret,),
                    ),
                    artifact_secret_detectors(
                        canonical,
                        secret_tokens=(caller_secret,),
                    ),
                )

    def test_raw_secret_preflight_detects_aws_json_after_payload_serialization(self):
        secret = "A" * 40
        positive_evidence = (
            f'AWS_SECRET_ACCESS_KEY={secret}',
            f'{{"AWS_SECRET_ACCESS_KEY":"{secret}"}}',
            f'{{  "AWS_SECRET_ACCESS_KEY" :  "{secret}"  }}',
        )
        for evidence in positive_evidence:
            payload = {"evidence": evidence}
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=True,
            ).encode("utf-8")
            with self.subTest(evidence=evidence[:35]):
                self.assertEqual(
                    raw_payload_secret_detectors(payload),
                    ("aws-secret-assignment",),
                )
                self.assertEqual(
                    raw_payload_secret_detectors(payload),
                    artifact_secret_detectors(canonical),
                )

        near_misses = (
            f'{{"AWS_SECRET_ACCESS_KEY":"{secret[:-1]}"}}',
            f'{{"AWS_SECRET_ACCESS_KEY":"{secret}A"}}',
            f'{{"AWS_SECRET_ACCESS_KEY":{secret}}}',
            f'{{"AWS_ACCESS_KEY_ID":"{secret}"}}',
        )
        for evidence in near_misses:
            with self.subTest(near_miss=evidence[:35]):
                self.assertEqual(raw_payload_secret_detectors({"evidence": evidence}), ())

    def test_unmatched_secret_shaped_lifecycle_identifier_is_not_echoed(self):
        secret = "sk-proj-" + "I" * 40
        payload = copy.deepcopy(self.attempt_payload)
        payload["initiationId"] = secret

        self.assertEqual(raw_payload_secret_detectors(payload), ("openai-api-key",))
        with self.assertRaises(CaptureSchemaError) as raised:
            make_council_attempt_v2(
                payload,
                prior_rows=self.rows[:2],
                clock=at("2026-08-23T10:02:00Z"),
            )
        self.assertEqual(
            str(raised.exception),
            "council-attempt-v2 has no prior initiation",
        )
        self.assertNotIn(secret, str(raised.exception))

    def test_completion_cannot_redefine_family_outcome_or_seat_version(self):
        completion = self.make_completion()
        completion["decisionFamilyId"] = "family-something-else"
        with self.assertRaisesRegex(CaptureSchemaError, "decisionFamilyId differs"):
            validate_v2_record(completion, self.rows)
        completion = self.make_completion()
        completion["seatResults"][0]["agentVersion"] = "quietly-changed"
        with self.assertRaisesRegex(CaptureSchemaError, "agentVersion differs"):
            validate_v2_record(completion, self.rows)

    def test_artifact_reference_shape_digest_and_path_are_strict(self):
        payload = copy.deepcopy(self.attempt_payload)
        payload["decisionBeforeArtifact"]["byteCount"] = payload["decisionBeforeArtifact"].pop("bytes")
        with self.assertRaisesRegex(CaptureSchemaError, "missing keys"):
            make_council_attempt_v2(payload, prior_rows=self.rows[:2], clock=at("2026-08-23T10:02:00Z"))
        payload = copy.deepcopy(self.attempt_payload)
        payload["decisionBeforeArtifact"]["path"] = "sha256/cc/cc/" + "9" * 64 + ".bin"
        with self.assertRaisesRegex(CaptureSchemaError, "must agree"):
            make_council_attempt_v2(payload, prior_rows=self.rows[:2], clock=at("2026-08-23T10:02:00Z"))

    def test_submitted_seats_require_tier1_artifacts_and_exactly_one_probability(self):
        payload = copy.deepcopy(self.completion_payload)
        del payload["seatResults"][0]["inputArtifact"]
        with self.assertRaisesRegex(CaptureSchemaError, "missing keys"):
            self.make_completion(payload)
        payload = copy.deepcopy(self.completion_payload)
        del payload["probabilities"]["blind"]
        with self.assertRaisesRegex(CaptureSchemaError, "exactly match submitted"):
            self.make_completion(payload)
        completion = self.make_completion()
        completion["predictions"].append(copy.deepcopy(completion["predictions"][0]))
        with self.assertRaisesRegex(CaptureSchemaError, "duplicate predictionId"):
            validate_v2_record(completion, self.rows)

    def test_non_submitted_seats_retain_input_but_have_no_output_or_probability(self):
        for state in ("abstained", "unavailable"):
            with self.subTest(state=state):
                payload = copy.deepcopy(self.completion_payload)
                payload["seatResults"][1] = {
                    **self.seat_plan[1],
                    "state": state,
                    "launcherAttempts": 2,
                    "inputArtifact": self.blind_input,
                    "latencyMs": 2000,
                }
                payload["noFindings"] = payload["noFindings"][:1]
                payload["probabilities"] = {"code": 65}
                payload["blindSeat"] = {
                    "role": "SKIPPED",
                    "required": True,
                    "ran": False,
                    "changedDecision": None,
                    "brief": blind_brief_identity(
                        self.initiation["runId"], self.blind_input["path"]
                    ),
                    "blockedReason": f"blind seat {state}",
                }
                rows = self.rows[:-1]
                finished = make_council_seats_finished(
                    {
                        "runId": self.initiation["runId"],
                        "seatStates": {"code": "submitted", "blind": state},
                    },
                    prior_rows=rows,
                    clock=at("2026-08-23T10:03:00Z"),
                )
                completion = self.make_completion(payload, rows=[*rows, finished])
                self.assertEqual(len(completion["predictions"]), 1)
                self.assertEqual(completion["predictions"][0]["seat"], "code")
                self.assertEqual(
                    completion["seatResults"][1]["inputArtifact"], self.blind_input
                )
                missing_input = copy.deepcopy(payload)
                del missing_input["seatResults"][1]["inputArtifact"]
                with self.assertRaisesRegex(CaptureSchemaError, "missing keys"):
                    self.make_completion(missing_input, rows=[*rows, finished])
                invalid = copy.deepcopy(completion)
                invalid["seatResults"][1]["outputArtifact"] = self.blind_output
                with self.assertRaisesRegex(CaptureSchemaError, "unknown keys"):
                    validate_v2_record(invalid, [*rows, finished])

    def test_seats_finished_requires_every_expected_seat_terminal_state(self):
        payload = {"runId": self.initiation["runId"], "seatStates": {"code": "submitted"}}
        with self.assertRaisesRegex(CaptureSchemaError, "exactly match"):
            make_council_seats_finished(payload, prior_rows=self.rows[:3], clock=at("2026-08-23T10:03:00Z"))
        payload["seatStates"]["blind"] = "running"
        with self.assertRaisesRegex(CaptureSchemaError, "invalid terminal state"):
            make_council_seats_finished(payload, prior_rows=self.rows[:3], clock=at("2026-08-23T10:03:00Z"))

    def test_submitted_empty_findings_need_seat_originated_declaration(self):
        payload = copy.deepcopy(self.completion_payload)
        payload["noFindings"] = payload["noFindings"][:1]
        with self.assertRaisesRegex(CaptureSchemaError, "need findings or"):
            self.make_completion(payload)
        payload = copy.deepcopy(self.completion_payload)
        payload["noFindings"][0]["outputArtifact"] = self.blind_output
        with self.assertRaisesRegex(CaptureSchemaError, "does not bind"):
            self.make_completion(payload)

    def test_structurally_valid_atomic_finding_replaces_no_findings(self):
        payload = copy.deepcopy(self.completion_payload)
        payload["findings"] = [
            {
                "findingId": "finding-" + "1" * 32,
                "seatId": "code",
                "category": "correctness",
                "claim": "A retry can duplicate a cohort position.",
                "severity": "block",
                "proposedAction": "Make initiation idempotent.",
                "evidenceSummary": "Two concurrent calls reproduced it.",
                "group": {
                    "findingGroupId": "finding-group-" + "2" * 32,
                    "runId": self.initiation["runId"],
                },
                "operatorDisposition": {"kind": "new-acted"},
            }
        ]
        payload["noFindings"] = payload["noFindings"][1:]
        completion = self.make_completion(payload)
        self.assertEqual(completion["findings"][0]["seatId"], "code")

    def test_finding_ids_are_unique_across_completions_and_groups_cannot_cross_runs(self):
        first_payload = copy.deepcopy(self.completion_payload)
        finding = {
            "findingId": "finding-" + "1" * 32,
            "seatId": "code",
            "category": "correctness",
            "claim": "A defect exists.",
            "severity": "block",
            "proposedAction": "Repair it.",
            "evidenceSummary": "Test evidence.",
            "group": {"findingGroupId": "finding-group-" + "2" * 32, "runId": self.initiation["runId"]},
            "operatorDisposition": {"kind": "new-acted"},
        }
        first_payload["findings"] = [finding]
        first_payload["noFindings"] = first_payload["noFindings"][1:]
        first = self.make_completion(first_payload)
        # Direct validation of a forged second completion exercises ledger-global identity checks.
        second = copy.deepcopy(first)
        second["runId"] = "run-" + "9" * 32
        second["initiationId"] = "initiation-" + "9" * 32
        # There is no valid second lifecycle, so the orphan check fires first by design.
        with self.assertRaisesRegex(CaptureSchemaError, "no prior attempt"):
            validate_v2_record(second, [*self.rows, first])

    def test_invalidation_reasons_are_typed_append_only_and_permanent(self):
        completion = self.make_completion()
        prior = [*self.rows, completion]
        events = []
        for index, reason in enumerate(sorted(INVALIDATION_REASONS)):
            event = make_capture_invalidation(
                {"runId": self.initiation["runId"], "reason": reason, "operator": "operator", "evidenceRef": f"ticket-{index}"},
                prior_rows=[*prior, *events],
                clock=at(f"2026-08-23T10:{10 + index:02d}:00Z"),
            )
            events.append(event)
        all_rows = [*prior, *events]
        self.assertEqual(invalidated_run_ids(all_rows), {self.initiation["runId"]})
        self.assertEqual(validate_v2_ledger(all_rows), all_rows)

    def test_invalidation_rejects_bad_reason_duplicate_event_and_orphan(self):
        payload = {"runId": self.initiation["runId"], "reason": "convenient-exclusion", "operator": "operator", "evidenceRef": "ticket"}
        with self.assertRaisesRegex(CaptureSchemaError, "reason must be one of"):
            make_capture_invalidation(payload, prior_rows=self.rows, clock=at("2026-08-23T10:05:00Z"))
        event = make_capture_invalidation(
            {**payload, "reason": "identity-error"},
            prior_rows=self.rows,
            clock=at("2026-08-23T10:05:00Z"),
        )
        with self.assertRaisesRegex(CaptureSchemaError, "duplicate invalidationId"):
            validate_v2_record(copy.deepcopy(event), [*self.rows, event])
        orphan = copy.deepcopy(event)
        orphan["invalidationId"] = "invalidation-" + "9" * 32
        orphan["runId"] = "run-" + "9" * 32
        with self.assertRaisesRegex(CaptureSchemaError, "no prior initiation"):
            validate_v2_record(orphan, self.rows)

    def test_invalidation_cannot_precede_initiation(self):
        event = make_capture_invalidation(
            {"runId": self.initiation["runId"], "reason": "timing-invalid", "operator": "operator", "evidenceRef": "ticket"},
            prior_rows=self.rows,
            clock=at("2026-08-23T10:05:00Z"),
        )
        event["invalidatedAt"] = "2026-08-23T10:00:59Z"
        with self.assertRaisesRegex(CaptureSchemaError, "precedes handlingStartedAt"):
            validate_v2_record(event, self.rows)

    def test_blind_seat_shape_remains_visible_to_existing_tally(self):
        completion = self.make_completion()
        self.assertEqual(
            completion["blindSeat"],
            {
                "role": "independent-control",
                "required": True,
                "ran": True,
                "changedDecision": False,
                "brief": blind_brief_identity(
                    self.initiation["runId"], self.blind_input["path"]
                ),
            },
        )

    def test_blind_metadata_must_match_submitted_canonical_blind_result(self):
        payload = copy.deepcopy(self.completion_payload)
        payload["blindSeat"]["ran"] = False
        payload["blindSeat"]["changedDecision"] = None
        payload["blindSeat"]["role"] = "SKIPPED"
        payload["blindSeat"]["blockedReason"] = "claimed launcher failure"
        payload["blindSeat"]["brief"] = blind_brief_identity(self.initiation["runId"])
        with self.assertRaisesRegex(CaptureSchemaError, "must agree"):
            self.make_completion(payload)

        payload = copy.deepcopy(self.completion_payload)
        payload["blindSeat"]["brief"] = self.blind_input["path"]
        with self.assertRaisesRegex(CaptureSchemaError, "run-scoped canonical"):
            self.make_completion(payload)

        payload = copy.deepcopy(self.completion_payload)
        payload["seatResults"][1]["role"] = "voting"
        attempt = copy.deepcopy(self.attempt)
        attempt["seatPlan"][1]["role"] = "voting"
        payload["seatResults"][1]["role"] = "voting"
        rows = [self.activation, self.initiation, attempt]
        finished = make_council_seats_finished(
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            prior_rows=rows,
            clock=at("2026-08-23T10:03:00Z"),
        )
        with self.assertRaisesRegex(CaptureSchemaError, "canonical blind.*control"):
            self.make_completion(payload, rows=[*rows, finished])

    def test_unavailable_blind_requires_tally_compatible_skipped_state(self):
        payload = copy.deepcopy(self.completion_payload)
        payload["seatResults"][1] = {
            **self.seat_plan[1],
            "state": "unavailable",
            "launcherAttempts": 2,
            "inputArtifact": self.blind_input,
        }
        payload["noFindings"] = payload["noFindings"][:1]
        payload["probabilities"] = {"code": 65}
        payload["blindSeat"] = {
            "role": "SKIPPED",
            "required": True,
            "ran": False,
            "changedDecision": None,
            "brief": blind_brief_identity(
                self.initiation["runId"], self.blind_input["path"]
            ),
            "blockedReason": "blind launcher unavailable",
        }
        rows = self.rows[:-1]
        finished = make_council_seats_finished(
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "unavailable"},
            },
            prior_rows=rows,
            clock=at("2026-08-23T10:03:00Z"),
        )
        completion = self.make_completion(payload, rows=[*rows, finished])
        self.assertEqual(completion["blindSeat"]["role"], "SKIPPED")

        missing_reason = copy.deepcopy(payload)
        del missing_reason["blindSeat"]["blockedReason"]
        with self.assertRaisesRegex(CaptureSchemaError, "blockedReason"):
            self.make_completion(missing_reason, rows=[*rows, finished])
        wrong_role = copy.deepcopy(payload)
        wrong_role["blindSeat"]["role"] = "independent-control"
        with self.assertRaisesRegex(CaptureSchemaError, "must be SKIPPED"):
            self.make_completion(wrong_role, rows=[*rows, finished])

        missing_captured_brief = copy.deepcopy(payload)
        missing_captured_brief["blindSeat"]["brief"] = blind_brief_identity(
            self.initiation["runId"]
        )
        with self.assertRaisesRegex(CaptureSchemaError, "canonical blind brief identity"):
            self.make_completion(missing_captured_brief, rows=[*rows, finished])

    def test_absent_blind_is_explicitly_not_required_and_not_fabricated(self):
        attempt_payload = copy.deepcopy(self.attempt_payload)
        attempt_payload["seatPlan"] = [copy.deepcopy(self.seat_plan[0])]
        attempt_payload["sharedOutcome"]["decisionLink"] = attempt_payload[
            "sharedOutcome"
        ]["decisionLink"].replace(
            seat_input_manifest_sha256(
                {"code": self.code_input, "blind": self.blind_input}
            ),
            seat_input_manifest_sha256({"code": self.code_input}),
        )
        attempt = make_council_attempt_v2(
            attempt_payload,
            prior_rows=self.rows[:2],
            clock=at("2026-08-23T10:02:00Z"),
        )
        finished = make_council_seats_finished(
            {"runId": self.initiation["runId"], "seatStates": {"code": "submitted"}},
            prior_rows=[self.activation, self.initiation, attempt],
            clock=at("2026-08-23T10:03:00Z"),
        )
        payload = {
            "runId": self.initiation["runId"],
            "seatResults": [copy.deepcopy(self.seat_results[0])],
            "findings": [],
            "noFindings": [copy.deepcopy(self.completion_payload["noFindings"][0])],
            "probabilities": {"code": 65},
            "blindSeat": {
                "role": "SKIPPED",
                "required": False,
                "ran": False,
                "changedDecision": None,
                "brief": blind_brief_identity(self.initiation["runId"]),
                "blockedReason": "blind seat was not planned",
                "notRequiredReason": "mechanical change did not require blind review",
            },
        }
        rows = [self.activation, self.initiation, attempt, finished]
        completion = self.make_completion(payload, rows=rows)
        self.assertNotIn("blind", {item["seatId"] for item in completion["seatPlan"]})

        required = copy.deepcopy(payload)
        required["blindSeat"]["required"] = True
        del required["blindSeat"]["notRequiredReason"]
        with self.assertRaisesRegex(CaptureSchemaError, "required blindSeat"):
            self.make_completion(required, rows=rows)

    def test_reused_blind_input_artifact_gets_distinct_run_scoped_identity(self):
        other_run = "run-" + "9" * 32
        first = blind_brief_identity(self.initiation["runId"], self.blind_input["path"])
        second = blind_brief_identity(other_run, self.blind_input["path"])
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(self.blind_input["path"] + "#run-"))

    def test_table_validator_reports_ledger_line(self):
        broken = copy.deepcopy(self.finished)
        broken["seatStates"].pop("blind")
        with self.assertRaisesRegex(CaptureSchemaError, "ledger row 4"):
            validate_v2_ledger([*self.rows[:3], broken])


if __name__ == "__main__":
    unittest.main()
