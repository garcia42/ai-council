import copy
import fcntl
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from council_tools import capture_runtime, forecasts
from council_tools.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    compute_git_blob_oid,
)
from council_tools.capture_schema import (
    blind_brief_identity,
    forecast_request_binding_v2,
    forecast_request_identity_v2,
    parse_forecast_request_binding_v2,
    outcome_fingerprint_v2,
    outcome_id_v2,
)
from council_tools.capture_runtime import (
    CaptureRuntimeError,
    CaptureSecretDetectedError,
    append_capture_activation,
    append_capture_initiation,
    append_capture_invalidation,
    append_capture_resolution,
    append_council_attempt_v2,
    append_council_seats_finished,
    append_council_v2,
    capture_report,
    seat_input_manifest_sha256,
    validate_capture_ledger,
)
from council_tools.evidence_backup import (
    create_evidence_snapshot,
    restore_evidence_snapshot,
)
from council_tools.forecasts import (
    append_ledger_row,
    append_override,
    append_resolution,
    make_attempt,
)


RUNTIME_COMMIT = "a" * 40
AGENT_CODE = "c" * 64
AGENT_BLIND = "d" * 64


def at(value):
    return lambda: value


class CaptureIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.temporary.name)
        self.ledger = self.root / "council.jsonl"
        self.events = self.root / "capture-resolved.jsonl"
        self.lock = self.root / "evidence.lock"
        self.artifact_root = self.root / "artifacts"
        self.store = ArtifactStore(self.artifact_root)

        self.baseline_bytes = json.dumps(
            {
                "schemaVersion": 3,
                "knownConsiderations": [
                    {
                        "considerationId": "KC-01",
                        "claim": "Forecast accuracy alone cannot prove useful novelty.",
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        baseline_ref = self.store.capture(self.baseline_bytes)
        self.baseline_blob = compute_git_blob_oid(self.baseline_bytes)
        self.decision_ref = {**baseline_ref, "gitBlob": self.baseline_blob}
        self.activation = append_capture_activation(
            self.ledger,
            {
                "cohortName": "first-ten-v2",
                "captureVersion": "capture-v2.0.0",
                "runtimeSourceCommit": RUNTIME_COMMIT,
                "artifactRootPolicy": "private-content-addressed-v1",
            },
            clock=at("2026-08-23T10:00:00Z"),
            coordination_lock=self.lock,
        )
        self.initiation, recorded = append_capture_initiation(
            self.ledger,
            {
                "activationId": self.activation["activationId"],
                "idempotencyKey": "decision-001-first-convening",
            },
            clock=at("2026-08-23T10:01:00Z"),
            coordination_lock=self.lock,
        )
        self.assertTrue(recorded)
        self.seat_plan = [
            {
                "seatId": "code",
                "role": "voting",
                "agentVersion": "code-v1",
                "agentDefinitionDigest": AGENT_CODE,
            },
            {
                "seatId": "blind",
                "role": "control",
                "agentVersion": "blind-v1",
                "agentDefinitionDigest": AGENT_BLIND,
            },
        ]
        claim = "The external test event occurs."
        resolution_date = "2026-08-24"
        resolved_by = "Inspect the retained deterministic fixture."
        evidence_cutoff = "2026-08-23T10:00:30Z"
        materiality = "Failure blocks the rollout claim."
        action_if_true = "Continue the capture soak."
        action_if_false = "Repair and repeat."
        placeholder_link = (
            f"commit={RUNTIME_COMMIT};blob={self.baseline_blob};"
            f"sha256={baseline_ref['sha256']};inputManifestSha256={'0' * 64}"
        )
        outcome_id = outcome_id_v2(self.initiation["runId"], claim)
        outcome_fingerprint = outcome_fingerprint_v2(
            claim, resolution_date, resolved_by, placeholder_link
        )
        self.request_identity = forecast_request_identity_v2(
            self.initiation["runId"],
            outcome_id,
            outcome_fingerprint,
            evidence_cutoff,
            claim,
            resolution_date,
            resolved_by,
            materiality,
            action_if_true,
            action_if_false,
        )
        request_binding = forecast_request_binding_v2(
            self.initiation["runId"],
            outcome_id,
            outcome_fingerprint,
            evidence_cutoff,
            claim,
            resolution_date,
            resolved_by,
            materiality,
            action_if_true,
            action_if_false,
        )
        bindings = (
            f"commit={RUNTIME_COMMIT};blob={self.baseline_blob};"
            f"sha256={baseline_ref['sha256']}\n{request_binding}"
        )
        self.inputs = {
            "code": f"code prompt\n{bindings}\n".encode(),
            "blind": f"blind prompt\n{bindings}\n".encode(),
        }
        self.input_refs = {
            seat: self.store.capture(data) for seat, data in self.inputs.items()
        }
        manifest = seat_input_manifest_sha256(self.input_refs)
        decision_link = (
            f"commit={RUNTIME_COMMIT};blob={self.baseline_blob};"
            f"sha256={baseline_ref['sha256']};inputManifestSha256={manifest}"
        )
        self.attempt_payload = {
            "initiationId": self.initiation["initiationId"],
            "decisionFamilyId": "family-capture-integration",
            "question": "Should the integrated capture lifecycle proceed?",
            "decisionBeforeArtifact": self.decision_ref,
            "outcomeClass": "exogenous",
            "outcomeClassRationale": "The measured external event is not controlled by this review.",
            "evidenceCutoffAt": evidence_cutoff,
            "seatPlan": self.seat_plan,
            "sharedOutcome": {
                "claim": claim,
                "resolutionDate": resolution_date,
                "resolvedBy": resolved_by,
                "decisionLink": decision_link,
                "materiality": materiality,
                "actionIfTrue": action_if_true,
                "actionIfFalse": action_if_false,
                "relatedOutcomeIds": [],
            },
        }
        self.outputs = {}
        for seat, probability in (("code", 70), ("blind", 60)):
            self.outputs[seat] = json.dumps(
                {
                    "answer": "approve",
                    "capture": {
                        "kind": "no-findings",
                        "findings": [],
                        **self.request_identity,
                        "seatId": seat,
                        "inputArtifactSha256": self.input_refs[seat]["sha256"],
                        "sharedProbability": probability,
                    },
                }
            ).encode()
        self.output_refs = {
            seat: self.store.capture(data) for seat, data in self.outputs.items()
        }

    def tearDown(self):
        self.temporary.cleanup()

    def append_attempt(self):
        return append_council_attempt_v2(
            self.ledger,
            self.attempt_payload,
            artifact_store=self.store,
            decision_before_bytes=self.baseline_bytes,
            seat_input_artifacts=self.input_refs,
            visible_inputs=self.inputs,
            clock=at("2026-08-23T10:02:00Z"),
            coordination_lock=self.lock,
        )

    def capture_without_secret_preflight(self, data):
        """Model restored/corrupt retained bytes that bypassed current capture."""

        with mock.patch(
            "council_tools.artifacts.secret_detectors", return_value=()
        ):
            return self.store.capture(data)

    def duplicate_key_prompt(self, seat, secret_key):
        request = parse_forecast_request_binding_v2(self.inputs[seat])
        canonical = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        duplicate = (
            canonical[:-1]
            + b',"'
            + secret_key.encode()
            + b'":1,"'
            + secret_key.encode()
            + b'":2}'
        )
        return self.inputs[seat].replace(canonical, duplicate)

    def duplicate_key_output(self, seat, secret_key):
        return self.outputs[seat].replace(
            b'{"answer":',
            b'{"'
            + secret_key.encode()
            + b'":1,"'
            + secret_key.encode()
            + b'":2,"answer":',
            1,
        )

    def rebind_to_baseline(self, baseline_bytes):
        """Rebuild all prospective identities around exact baseline bytes."""

        self.baseline_bytes = baseline_bytes
        baseline_ref = self.store.capture(baseline_bytes)
        self.baseline_blob = compute_git_blob_oid(baseline_bytes)
        self.decision_ref = {**baseline_ref, "gitBlob": self.baseline_blob}
        outcome = self.attempt_payload["sharedOutcome"]
        placeholder_link = (
            f"commit={RUNTIME_COMMIT};blob={self.baseline_blob};"
            f"sha256={baseline_ref['sha256']};inputManifestSha256={'0' * 64}"
        )
        outcome_id = outcome_id_v2(self.initiation["runId"], outcome["claim"])
        outcome_fingerprint = outcome_fingerprint_v2(
            outcome["claim"],
            outcome["resolutionDate"],
            outcome["resolvedBy"],
            placeholder_link,
        )
        self.request_identity = forecast_request_identity_v2(
            self.initiation["runId"],
            outcome_id,
            outcome_fingerprint,
            self.attempt_payload["evidenceCutoffAt"],
            outcome["claim"],
            outcome["resolutionDate"],
            outcome["resolvedBy"],
            outcome["materiality"],
            outcome["actionIfTrue"],
            outcome["actionIfFalse"],
        )
        request_binding = forecast_request_binding_v2(
            self.initiation["runId"],
            outcome_id,
            outcome_fingerprint,
            self.attempt_payload["evidenceCutoffAt"],
            outcome["claim"],
            outcome["resolutionDate"],
            outcome["resolvedBy"],
            outcome["materiality"],
            outcome["actionIfTrue"],
            outcome["actionIfFalse"],
        )
        bindings = (
            f"commit={RUNTIME_COMMIT};blob={self.baseline_blob};"
            f"sha256={baseline_ref['sha256']}\n{request_binding}"
        )
        self.inputs = {
            "code": f"code prompt\n{bindings}\n".encode(),
            "blind": f"blind prompt\n{bindings}\n".encode(),
        }
        self.input_refs = {
            seat: self.store.capture(data) for seat, data in self.inputs.items()
        }
        manifest = seat_input_manifest_sha256(self.input_refs)
        decision_link = (
            f"commit={RUNTIME_COMMIT};blob={self.baseline_blob};"
            f"sha256={baseline_ref['sha256']};inputManifestSha256={manifest}"
        )
        self.attempt_payload = copy.deepcopy(self.attempt_payload)
        self.attempt_payload["decisionBeforeArtifact"] = self.decision_ref
        self.attempt_payload["sharedOutcome"]["decisionLink"] = decision_link
        self.outputs = {}
        for seat, probability in (("code", 70), ("blind", 60)):
            self.outputs[seat] = json.dumps(
                {
                    "answer": "approve",
                    "capture": {
                        "kind": "no-findings",
                        "findings": [],
                        **self.request_identity,
                        "seatId": seat,
                        "inputArtifactSha256": self.input_refs[seat]["sha256"],
                        "sharedProbability": probability,
                    },
                }
            ).encode()
        self.output_refs = {
            seat: self.store.capture(data) for seat, data in self.outputs.items()
        }

    def completion_payload(self):
        return {
            "runId": self.initiation["runId"],
            "seatResults": [
                {
                    **self.seat_plan[0],
                    "state": "submitted",
                    "launcherAttempts": 1,
                    "inputArtifact": self.input_refs["code"],
                    "outputArtifact": self.output_refs["code"],
                    "modelId": "model-code",
                    "toolPolicy": "read-only-v1",
                    "repositoryCommit": RUNTIME_COMMIT,
                    "latencyMs": 1000,
                },
                {
                    **self.seat_plan[1],
                    "state": "submitted",
                    "launcherAttempts": 1,
                    "inputArtifact": self.input_refs["blind"],
                    "outputArtifact": self.output_refs["blind"],
                    "modelId": "model-blind",
                    "toolPolicy": "no-tools-v1",
                    "repositoryCommit": RUNTIME_COMMIT,
                    "latencyMs": 900,
                },
            ],
            "findings": [],
            "noFindings": [
                {
                    "kind": "no-findings",
                    "seatId": seat,
                    "outputArtifact": self.output_refs[seat],
                }
                for seat in ("code", "blind")
            ],
            "probabilities": {"code": 70, "blind": 60},
            "blindSeat": {
                "role": "independent-control",
                "required": True,
                "ran": True,
                "changedDecision": False,
                "brief": blind_brief_identity(
                    self.initiation["runId"], self.input_refs["blind"]["path"]
                ),
            },
        }

    def completion_with_code_finding(self):
        seat_owned = {
            "findingId": "finding-" + "1" * 32,
            "seatId": "code",
            "category": "method",
            "claim": "The rollout claim needs an explicit novelty denominator.",
            "severity": "blocking",
            "proposedAction": "Retain the first-ten initiation denominator.",
            "evidenceSummary": "The sealed baseline names forecast accuracy only.",
        }
        finding = {
            **seat_owned,
            "group": {
                "findingGroupId": "finding-group-" + "2" * 32,
                "runId": self.initiation["runId"],
            },
            "operatorDisposition": {
                "kind": "already-known",
                "considerationId": "KC-01",
                "quotedSubclaim": "Forecast accuracy alone cannot prove useful novelty.",
            },
        }
        parsed_output = json.loads(self.outputs["code"])
        parsed_output["capture"]["kind"] = "findings"
        parsed_output["capture"]["findings"] = [seat_owned]
        output_bytes = json.dumps(
            parsed_output, sort_keys=True, separators=(",", ":")
        ).encode()
        output_ref = self.store.capture(output_bytes)
        payload = self.completion_payload()
        payload["findings"] = [finding]
        payload["noFindings"] = [
            declaration
            for declaration in payload["noFindings"]
            if declaration["seatId"] != "code"
        ]
        payload["seatResults"][0]["outputArtifact"] = output_ref
        outputs = {**self.outputs, "code": output_bytes}
        return payload, outputs, seat_owned

    def finish(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        return append_council_v2(
            self.ledger,
            self.completion_payload(),
            artifact_store=self.store,
            decision_before_bytes=self.baseline_bytes,
            seat_input_artifacts=self.input_refs,
            visible_inputs=self.inputs,
            visible_outputs=self.outputs,
            clock=at("2026-08-23T10:04:00Z"),
            coordination_lock=self.lock,
        )

    def finish_with_code_finding(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload, outputs, _seat_owned = self.completion_with_code_finding()
        return append_council_v2(
            self.ledger,
            payload,
            artifact_store=self.store,
            decision_before_bytes=self.baseline_bytes,
            seat_input_artifacts=self.input_refs,
            visible_inputs=self.inputs,
            visible_outputs=outputs,
            clock=at("2026-08-23T10:04:00Z"),
            coordination_lock=self.lock,
        )

    def finish_without_blind_submission(self, state, *, blind_brief=None):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": state},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload = self.completion_payload()
        payload["seatResults"][1] = {
            **self.seat_plan[1],
            "state": state,
            "launcherAttempts": 1,
            "inputArtifact": self.input_refs["blind"],
        }
        payload["noFindings"] = payload["noFindings"][:1]
        payload["probabilities"] = {"code": 70}
        payload["blindSeat"] = {
            "role": "SKIPPED",
            "required": True,
            "ran": False,
            "changedDecision": None,
            "brief": blind_brief or blind_brief_identity(
                self.initiation["runId"], self.input_refs["blind"]["path"]
            ),
            "blockedReason": f"blind seat {state}",
        }
        return append_council_v2(
            self.ledger,
            payload,
            artifact_store=self.store,
            decision_before_bytes=self.baseline_bytes,
            seat_input_artifacts=self.input_refs,
            visible_inputs=self.inputs,
            visible_outputs={"code": self.outputs["code"]},
            clock=at("2026-08-23T10:04:00Z"),
            coordination_lock=self.lock,
        )

    def test_capture_report_inventories_every_retained_jsonl_escrow_without_parsing(self):
        append_capture_invalidation(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "reason": "timing-invalid",
                "operator": "test-harness",
                "evidenceRef": "fixture:escrow-inventory",
            },
            clock=at("2026-08-23T10:01:30Z"),
            coordination_lock=self.lock,
        )
        opaque_escrow = self.root / (
            f".{self.ledger.name}.{'0' * 32}.tmp.escrow.{'1' * 32}"
        )
        opaque_escrow.write_bytes(b"not-json-and-never-ledger-input")
        expected = forecasts.transaction_escrow_inventory(
            self.ledger, self.events
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-23T12:00:00Z",
        )

        self.assertEqual(report["transactionEscrows"], expected)
        self.assertEqual(
            report["transactionEscrows"]["aggregateBytes"],
            sum(item["bytes"] for item in expected["entries"]),
        )
        self.assertEqual(report["ledger"]["rawRecordCount"], 3)
        self.assertIn(
            str(opaque_escrow),
            {item["path"] for item in report["transactionEscrows"]["entries"]},
        )

    def test_end_to_end_capture_report_and_exogenous_brier(self):
        completion, summary = self.finish()
        self.assertEqual(summary["emptyDeclarationRate"]["declarationCount"], 2)
        with mock.patch.object(
            forecasts,
            "evidence_write_lock",
            wraps=forecasts.evidence_write_lock,
        ) as nested_lock:
            event = append_capture_resolution(
                self.ledger,
                self.events,
                outcome_id=completion["sharedOutcome"]["outcomeId"],
                came_true=True,
                evidence="deterministic retained fixture",
                resolver="test-harness",
                resolved_at="2026-08-26T00:00:00Z",
                method="deterministic",
                coordination_lock=self.lock,
            )
        nested_lock.assert_not_called()
        self.assertEqual(event["outcomeId"], completion["sharedOutcome"]["outcomeId"])
        self.assertEqual(
            event["outcomeFingerprint"],
            completion["sharedOutcome"]["fingerprint"],
        )
        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 1)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 1)
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 2)
        self.assertAlmostEqual(
            report["descriptiveForecastAccuracy"]["meanBrier"],
            ((0.7 - 1.0) ** 2 + (0.6 - 1.0) ** 2) / 2,
        )
        raw, v2 = validate_capture_ledger(self.ledger)
        self.assertEqual(len(raw), 5)
        self.assertEqual(len(v2), 5)

    def test_main_ledger_resolution_without_sidecar_never_grades_v2(self):
        completion, _summary = self.finish()
        outcome = completion["sharedOutcome"]
        append_resolution(
            self.ledger,
            outcome_id=outcome["outcomeId"],
            resolution_date=outcome["resolutionDate"],
            outcome_fingerprint=outcome["fingerprint"],
            came_true=True,
            evidence="plausible legacy-looking ledger fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        self.assertFalse(self.events.exists())

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self.assertEqual(report["outcomeCounts"]["exogenousV2"], 1)
        self.assertEqual(report["outcomeCounts"]["resolvedExogenousV2"], 0)
        self.assertEqual(
            report["outcomeCounts"]["resolvedExogenousV2IssuanceCount"], 0
        )
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertIsNone(report["descriptiveForecastAccuracy"]["meanBrier"])
        self.assertEqual(report["ledger"]["captureResolutionEventCount"], 0)
        self.assertEqual(report["ledger"]["invalidResolutionRecordCount"], 1)
        self.assertEqual(
            report["ledger"]["invalidResolutionRecords"],
            [
                {
                    "lineNumber": 6,
                    "kind": "outcome-resolution",
                    "outcomeId": outcome["outcomeId"],
                    "error": "ledger-origin-resolution-not-eligible",
                }
            ],
        )

    def test_writer_prompt_duplicate_key_has_fixed_nonleaking_category(self):
        secret_key = "caller_secret_prompt_" + "p" * 40
        malformed = self.duplicate_key_prompt("code", secret_key)
        references = {
            **self.input_refs,
            "code": self.capture_without_secret_preflight(malformed),
        }
        payload = copy.deepcopy(self.attempt_payload)
        old_manifest = seat_input_manifest_sha256(self.input_refs)
        new_manifest = seat_input_manifest_sha256(references)
        payload["sharedOutcome"]["decisionLink"] = payload["sharedOutcome"][
            "decisionLink"
        ].replace(old_manifest, new_manifest)

        with self.assertRaisesRegex(
            CaptureRuntimeError, "^retained prompt parse failure$"
        ) as raised:
            append_council_attempt_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=references,
                visible_inputs={**self.inputs, "code": malformed},
                clock=at("2026-08-23T10:02:00Z"),
                coordination_lock=self.lock,
            )
        self.assertNotIn(secret_key, str(raised.exception))

    def test_writer_output_duplicate_key_has_fixed_nonleaking_category(self):
        secret_key = "caller_secret_output_" + "o" * 40
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        malformed = self.duplicate_key_output("code", secret_key)
        malformed_ref = self.capture_without_secret_preflight(malformed)
        payload = self.completion_payload()
        payload["seatResults"][0]["outputArtifact"] = malformed_ref
        payload["noFindings"][0]["outputArtifact"] = malformed_ref

        with self.assertRaisesRegex(
            CaptureRuntimeError, "^retained output parse failure$"
        ) as raised:
            append_council_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                visible_outputs={**self.outputs, "code": malformed},
                clock=at("2026-08-23T10:04:00Z"),
                coordination_lock=self.lock,
            )
        self.assertNotIn(secret_key, str(raised.exception))

    def test_baseline_duplicate_key_has_fixed_nonleaking_parse_category(self):
        secret_key = "caller_secret_baseline_" + "b" * 40
        malformed = (
            b'{"schemaVersion":3,"'
            + secret_key.encode()
            + b'":1,"'
            + secret_key.encode()
            + b'":2}'
        )
        self.rebind_to_baseline(malformed)
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        with self.assertRaisesRegex(
            CaptureRuntimeError, "^retained baseline parse failure$"
        ) as raised:
            append_council_v2(
                self.ledger,
                self.completion_payload(),
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                visible_outputs=self.outputs,
                clock=at("2026-08-23T10:04:00Z"),
                coordination_lock=self.lock,
            )
        self.assertNotIn(secret_key, str(raised.exception))

    def test_writer_excessively_nested_retained_baseline_is_generic(self):
        deeply_nested = b"[" * 2000 + b"0" + b"]" * 2000
        self.rebind_to_baseline(deeply_nested)

        with self.assertRaisesRegex(
            CaptureRuntimeError, "^retained baseline parse failure$"
        ) as raised:
            self.finish()

        self.assertNotIn("recursion", str(raised.exception).lower())
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 4)

    def test_report_baseline_duplicate_key_is_generic_and_nonleaking(self):
        secret_key = "caller_secret_report_baseline_" + "b" * 40
        malformed = (
            b'{"schemaVersion":3,"'
            + secret_key.encode()
            + b'":1,"'
            + secret_key.encode()
            + b'":2}'
        )
        self.rebind_to_baseline(malformed)
        with mock.patch.object(
            capture_runtime,
            "_parse_baseline",
            return_value={"schemaVersion": 3, "knownConsiderations": []},
        ):
            self.finish()

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(
            report["ledger"]["invalidV2Records"][0]["error"],
            "report-time retained baseline parse failure",
        )
        self.assertNotIn(secret_key, json.dumps(report, sort_keys=True))

    def test_report_prompt_duplicate_key_is_generic_and_nonleaking(self):
        self.finish()
        secret_key = "caller_secret_report_prompt_" + "r" * 40
        malformed = self.duplicate_key_prompt("code", secret_key)
        malformed_ref = self.capture_without_secret_preflight(malformed)
        rebound_output = json.loads(self.outputs["code"])
        rebound_output["capture"]["inputArtifactSha256"] = malformed_ref["sha256"]
        rebound_output_bytes = json.dumps(
            rebound_output, sort_keys=True, separators=(",", ":")
        ).encode()
        rebound_output_ref = self.store.capture(rebound_output_bytes)

        rows = [json.loads(line) for line in self.ledger.read_bytes().splitlines()]
        attempt, completion = rows[2], rows[-1]
        references = {**self.input_refs, "code": malformed_ref}
        old_manifest = seat_input_manifest_sha256(self.input_refs)
        new_manifest = seat_input_manifest_sha256(references)
        for lifecycle in (attempt, completion):
            lifecycle["sharedOutcome"]["decisionLink"] = lifecycle[
                "sharedOutcome"
            ]["decisionLink"].replace(old_manifest, new_manifest)
        completion["seatResults"][0]["inputArtifact"] = malformed_ref
        completion["seatResults"][0]["outputArtifact"] = rebound_output_ref
        completion["noFindings"][0]["outputArtifact"] = rebound_output_ref
        self.ledger.write_bytes(
            b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(
            report["ledger"]["invalidV2Records"][0]["error"],
            "report-time retained prompt parse failure",
        )
        self.assertNotIn(secret_key, json.dumps(report, sort_keys=True))

    def test_report_output_duplicate_key_is_generic_and_nonleaking(self):
        self.finish()
        secret_key = "caller_secret_report_output_" + "s" * 40
        malformed = self.duplicate_key_output("code", secret_key)
        malformed_ref = self.capture_without_secret_preflight(malformed)
        rows = [json.loads(line) for line in self.ledger.read_bytes().splitlines()]
        completion = rows[-1]
        completion["seatResults"][0]["outputArtifact"] = malformed_ref
        completion["noFindings"][0]["outputArtifact"] = malformed_ref
        self.ledger.write_bytes(
            b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(
            report["ledger"]["invalidV2Records"][0]["error"],
            "report-time retained output parse failure",
        )
        self.assertNotIn(secret_key, json.dumps(report, sort_keys=True))

    def test_report_excessively_nested_retained_output_is_generic(self):
        self.finish()
        deeply_nested = b"[" * 2000 + b"0" + b"]" * 2000
        malformed_ref = self.capture_without_secret_preflight(deeply_nested)
        rows = [json.loads(line) for line in self.ledger.read_bytes().splitlines()]
        completion = rows[-1]
        completion["seatResults"][0]["outputArtifact"] = malformed_ref
        completion["noFindings"][0]["outputArtifact"] = malformed_ref
        self.ledger.write_bytes(
            b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(
            report["ledger"]["invalidV2Records"][0]["error"],
            "report-time retained output parse failure",
        )
        self.assertNotIn("recursion", json.dumps(report).lower())

    def test_resolution_writer_requires_sealed_issuance_before_sidecar_mutation(self):
        attempt = self.append_attempt()
        self.assertFalse(self.events.exists())
        with self.assertRaisesRegex(CaptureRuntimeError, "sealed council-v2 issuance"):
            append_capture_resolution(
                self.ledger,
                self.events,
                outcome_id=attempt["sharedOutcome"]["outcomeId"],
                came_true=True,
                evidence="must not be appended",
                resolver="test-harness",
                resolved_at="2026-08-26T00:00:00Z",
                method="deterministic",
                coordination_lock=self.lock,
            )
        self.assertFalse(self.events.exists())

    def test_resolution_writer_validates_issuance_time_before_sidecar_mutation(self):
        completion, _summary = self.finish()
        self.assertFalse(self.events.exists())
        with self.assertRaisesRegex(CaptureRuntimeError, "precedes issuance"):
            append_capture_resolution(
                self.ledger,
                self.events,
                outcome_id=completion["sharedOutcome"]["outcomeId"],
                came_true=True,
                evidence="must not be appended",
                resolver="test-harness",
                resolved_at="2026-08-23T10:03:30Z",
                method="deterministic",
                coordination_lock=self.lock,
            )
        self.assertFalse(self.events.exists())

    def test_resolution_writer_rejects_non_text_closed_fields_without_sidecar_bytes(self):
        completion, _summary = self.finish()
        outcome_id = completion["sharedOutcome"]["outcomeId"]
        malformed_values = ({"nested": "value"}, ["value"], True, 7)
        for field in ("method", "void_reason"):
            for value in malformed_values:
                with self.subTest(field=field, value_type=type(value).__name__):
                    kwargs = {
                        "outcome_id": outcome_id,
                        "came_true": True,
                        "evidence": "must not be appended",
                        "resolver": "test-harness",
                        "resolved_at": "2026-08-26T00:00:00Z",
                        "method": "deterministic",
                        "coordination_lock": self.lock,
                    }
                    kwargs[field] = value
                    with self.assertRaisesRegex(
                        CaptureRuntimeError,
                        "method must be non-empty text"
                        if field == "method"
                        else "voidReason must be non-empty text",
                    ):
                        append_capture_resolution(
                            self.ledger,
                            self.events,
                            **kwargs,
                        )
                    self.assertFalse(self.events.exists())
    def test_resolution_holds_log_transaction_through_sidecar_append(self):
        completion, _summary = self.finish()
        observed = []
        real_transaction = capture_runtime._capture_transaction
        real_append_resolution = capture_runtime.append_resolution

        @contextmanager
        def log_transaction(*args, **kwargs):
            observed.append("log-enter")
            try:
                with real_transaction(*args, **kwargs) as transaction:
                    yield transaction
            finally:
                observed.append("log-exit")

        def sidecar_append(*args, **kwargs):
            observed.append("sidecar-append")
            return real_append_resolution(*args, **kwargs)

        with (
            mock.patch.object(
                capture_runtime,
                "_capture_transaction",
                side_effect=log_transaction,
            ),
            mock.patch.object(
                capture_runtime,
                "append_resolution",
                side_effect=sidecar_append,
            ),
        ):
            append_capture_resolution(
                self.ledger,
                self.events,
                outcome_id=completion["sharedOutcome"]["outcomeId"],
                came_true=True,
                evidence="deterministic retained fixture",
                resolver="test-harness",
                resolved_at="2026-08-26T00:00:00Z",
                method="deterministic",
                coordination_lock=None,
            )
        self.assertEqual(observed, ["log-enter", "sidecar-append", "log-exit"])

    def test_resolution_sidecar_cannot_alias_capture_ledger(self):
        completion, _summary = self.finish()
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(CaptureRuntimeError, "must be distinct"):
            append_capture_resolution(
                self.ledger,
                self.ledger,
                outcome_id=completion["sharedOutcome"]["outcomeId"],
                came_true=True,
                evidence="must not be appended",
                resolver="test-harness",
                resolved_at="2026-08-26T00:00:00Z",
                method="deterministic",
                coordination_lock=None,
            )
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_report_rederives_probability_from_retained_output_before_brier(self):
        completion, _summary = self.finish()
        append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=True,
            evidence="deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        rows = [json.loads(line) for line in self.ledger.read_bytes().splitlines()]
        rows[-1]["predictions"][0]["probability"] = 5
        self.ledger.write_bytes(
            b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertIn(
            "report-time forecast provenance failure",
            report["ledger"]["invalidV2Records"][0]["error"],
        )

    def test_report_rejects_output_reuse_and_prompt_reference_substitution(self):
        completion, _summary = self.finish()
        append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=True,
            evidence="deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        reused_output = json.loads(self.outputs["code"])
        reused_output["capture"]["runId"] = "run-" + "f" * 32
        reused_ref = self.store.capture(json.dumps(reused_output).encode())
        replacement_input = self.store.capture(self.inputs["code"] + b"reissued\n")

        rows = [json.loads(line) for line in self.ledger.read_bytes().splitlines()]
        attempt, completion_row = rows[2], rows[-1]
        input_refs = dict(self.input_refs)
        input_refs["code"] = replacement_input
        old_manifest = seat_input_manifest_sha256(self.input_refs)
        new_manifest = seat_input_manifest_sha256(input_refs)
        for lifecycle in (attempt, completion_row):
            lifecycle["sharedOutcome"]["decisionLink"] = lifecycle[
                "sharedOutcome"
            ]["decisionLink"].replace(old_manifest, new_manifest)
        completion_row["seatResults"][0]["inputArtifact"] = replacement_input
        completion_row["seatResults"][0]["outputArtifact"] = reused_ref
        completion_row["noFindings"][0]["outputArtifact"] = reused_ref
        self.ledger.write_bytes(
            b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertIn(
            "report-time forecast provenance failure",
            report["ledger"]["invalidV2Records"][0]["error"],
        )

    def test_report_rejects_retained_contradictory_visible_target(self):
        completion, _summary = self.finish()
        append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=True,
            evidence="deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        expected_request = parse_forecast_request_binding_v2(self.inputs["code"])
        contradictory_request = copy.deepcopy(expected_request)
        contradictory_request["actionIfFalse"] = "Proceed despite failure."
        expected_json = json.dumps(
            expected_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        contradictory_json = json.dumps(
            contradictory_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        contradictory_input_bytes = self.inputs["code"].replace(
            expected_json, contradictory_json
        )
        contradictory_input = self.store.capture(contradictory_input_bytes)
        rebound_output = json.loads(self.outputs["code"])
        rebound_output["capture"]["inputArtifactSha256"] = contradictory_input[
            "sha256"
        ]
        rebound_output_ref = self.store.capture(json.dumps(rebound_output).encode())

        rows = [json.loads(line) for line in self.ledger.read_bytes().splitlines()]
        attempt, completion_row = rows[2], rows[-1]
        input_refs = dict(self.input_refs)
        input_refs["code"] = contradictory_input
        old_manifest = seat_input_manifest_sha256(self.input_refs)
        new_manifest = seat_input_manifest_sha256(input_refs)
        for lifecycle in (attempt, completion_row):
            lifecycle["sharedOutcome"]["decisionLink"] = lifecycle[
                "sharedOutcome"
            ]["decisionLink"].replace(old_manifest, new_manifest)
        completion_row["seatResults"][0]["inputArtifact"] = contradictory_input
        completion_row["seatResults"][0]["outputArtifact"] = rebound_output_ref
        completion_row["noFindings"][0]["outputArtifact"] = rebound_output_ref
        self.ledger.write_bytes(
            b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertIn(
            "forecast request block differs from the sealed shared target",
            report["ledger"]["invalidV2Records"][0]["error"],
        )

    def test_capture_report_rejects_unrelated_sidecar_event_kinds(self):
        self.finish()
        append_override(
            self.events,
            reason="unrelated V1 grading debt control",
            operator="test-harness",
            created_at="2026-08-26T00:00:00Z",
            expires_date="2026-08-27",
            coordination_lock=self.lock,
        )

        with self.assertRaisesRegex(
            CaptureRuntimeError, "unrelated event kinds: grading-debt-override"
        ):
            capture_report(
                self.ledger,
                self.events,
                artifact_store=self.store,
                as_of="2026-08-27T00:00:00Z",
            )

    def test_capture_report_preflights_secret_sidecar_snapshot(self):
        self.finish()
        secret = "sk-proj-" + "Y" * 40
        self.events.write_text(
            json.dumps({"kind": secret}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        ledger_before = self.ledger.read_bytes()
        events_before = self.events.read_bytes()

        with self.assertRaises(CaptureSecretDetectedError) as raised:
            capture_report(
                self.ledger,
                self.events,
                artifact_store=self.store,
                as_of="2026-08-27T00:00:00Z",
            )

        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(self.ledger.read_bytes(), ledger_before)
        self.assertEqual(self.events.read_bytes(), events_before)

    def test_capture_report_preflights_json_escaped_decoded_sidecar_fields(self):
        self.finish()
        secret = "sk-proj-" + "E" * 40
        escaped = "sk\\u002dproj\\u002d" + "E" * 40
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
        ledger_before = self.ledger.read_bytes()
        for field, encoded in cases.items():
            with self.subTest(field=field):
                self.assertNotIn(secret.encode(), encoded.encode())
                self.events.write_bytes(encoded.encode())
                events_before = self.events.read_bytes()

                with self.assertRaises(CaptureSecretDetectedError) as raised:
                    capture_report(
                        self.ledger,
                        self.events,
                        artifact_store=self.store,
                        as_of="2026-08-27T00:00:00Z",
                    )

                self.assertNotIn(secret, str(raised.exception))
                self.assertEqual(self.ledger.read_bytes(), ledger_before)
                self.assertEqual(self.events.read_bytes(), events_before)

    def test_capture_report_rejects_resolution_without_v2_attempt_issuance(self):
        self.finish()
        unknown_outcome = "outcome-" + "f" * 32
        append_resolution(
            self.events,
            outcome_id=unknown_outcome,
            resolution_date="2026-08-24",
            outcome_fingerprint="f" * 64,
            came_true=True,
            evidence="unrelated retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )

        with self.assertRaisesRegex(
            CaptureRuntimeError, "no V2 attempt issuance"
        ):
            capture_report(
                self.ledger,
                self.events,
                artifact_store=self.store,
                as_of="2026-08-27T00:00:00Z",
            )

    def test_capture_report_rejects_resolution_fingerprint_or_observation_time(self):
        completion, _summary = self.finish()
        append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=True,
            evidence="deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        original = json.loads(self.events.read_text())
        cases = (
            ("outcomeFingerprint", "f" * 64, "outcomeFingerprint differs"),
            ("resolvedAt", "2026-08-28T00:00:00Z", "follows report as_of"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                event = copy.deepcopy(original)
                event[field] = value
                self.events.write_text(json.dumps(event) + "\n")
                with self.assertRaisesRegex(CaptureRuntimeError, expected):
                    capture_report(
                        self.ledger,
                        self.events,
                        artifact_store=self.store,
                        as_of="2026-08-27T00:00:00Z",
                    )

    def test_middle_invalid_duplicate_initiation_cannot_steal_valid_lineage(self):
        completion, _summary = self.finish()
        append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=True,
            evidence="deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        lines = self.ledger.read_bytes().splitlines(keepends=True)
        duplicate_value = json.loads(lines[1])
        byte_different_duplicate = (
            json.dumps(duplicate_value, sort_keys=True) + "\n"
        ).encode()
        self.assertNotEqual(lines[1], byte_different_duplicate)
        self.ledger.write_bytes(
            b"".join([lines[0], lines[1], byte_different_duplicate, *lines[2:]])
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 1)
        self.assertEqual(
            [item["complete"] for item in report["cohort"]["runs"]],
            [True, False],
        )
        self.assertIn(
            "schema-invalid-record:capture-initiation",
            report["cohort"]["runs"][1]["incompleteReasons"],
        )
        self.assertEqual(report["outcomeCounts"]["exogenousV2"], 1)
        self.assertEqual(report["outcomeCounts"]["resolvedExogenousV2"], 1)
        self.assertEqual(report["descriptiveForecastAccuracy"]["predictionCount"], 2)
        self.assertAlmostEqual(
            report["descriptiveForecastAccuracy"]["meanBrier"],
            ((0.7 - 1.0) ** 2 + (0.6 - 1.0) ** 2) / 2,
        )

    def test_abstained_blind_input_is_reverified_for_report_and_scoring(self):
        completion, _summary = self.finish_without_blind_submission("abstained")
        append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=True,
            evidence="deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        intact = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(intact["cohort"]["completeInitiationCount"], 1)
        self.assertEqual(intact["artifacts"]["requiredArtifactCount"], 4)
        self.assertEqual(intact["artifacts"]["integrityCheckedCount"], 4)
        self.assertEqual(intact["descriptiveForecastAccuracy"]["predictionCount"], 1)

        blind_path = self.artifact_root / self.input_refs["blind"]["path"]
        blind_path.unlink()
        missing = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self.assertEqual(missing["cohort"]["completeInitiationCount"], 0)
        self.assertEqual(missing["artifacts"]["requiredArtifactCount"], 4)
        self.assertEqual(missing["artifacts"]["integrityCheckedCount"], 4)
        self.assertEqual(missing["artifacts"]["artifactIntegrityFailureCount"], 1)
        self.assertIn(
            "artifact-integrity-failure:blind:input",
            missing["cohort"]["runs"][0]["incompleteReasons"],
        )
        self.assertEqual(missing["descriptiveForecastAccuracy"]["predictionCount"], 0)
        self.assertEqual(
            missing["descriptiveForecastAccuracy"]["excludedOrInvalidStratum"][
                "predictionCount"
            ],
            1,
        )

    def test_prompt_manifest_and_visible_no_findings_fail_before_append(self):
        bad = copy.deepcopy(self.attempt_payload)
        bad["sharedOutcome"]["decisionLink"] = bad["sharedOutcome"][
            "decisionLink"
        ].replace("inputManifestSha256=", "omitted=")
        with self.assertRaisesRegex(CaptureRuntimeError, "inputManifestSha256"):
            append_council_attempt_v2(
                self.ledger,
                bad,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                clock=at("2026-08-23T10:02:00Z"),
                coordination_lock=self.lock,
            )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 2)

        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        altered = dict(self.outputs)
        altered["blind"] = json.dumps({"answer": "approve"}).encode()
        altered_ref = self.store.capture(altered["blind"])
        payload = self.completion_payload()
        payload["seatResults"][1]["outputArtifact"] = altered_ref
        payload["noFindings"][1]["outputArtifact"] = altered_ref
        with self.assertRaisesRegex(CaptureRuntimeError, "capture envelope"):
            append_council_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                visible_outputs=altered,
                clock=at("2026-08-23T10:04:00Z"),
                coordination_lock=self.lock,
            )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 4)

    def test_attempt_rejects_missing_or_wrong_forecast_request_prompt_binding(self):
        expected = forecast_request_binding_v2(
            self.initiation["runId"],
            self.request_identity["outcomeId"],
            self.request_identity["outcomeFingerprint"],
            self.request_identity["evidenceCutoffAt"],
            self.attempt_payload["sharedOutcome"]["claim"],
            self.attempt_payload["sharedOutcome"]["resolutionDate"],
            self.attempt_payload["sharedOutcome"]["resolvedBy"],
            self.attempt_payload["sharedOutcome"]["materiality"],
            self.attempt_payload["sharedOutcome"]["actionIfTrue"],
            self.attempt_payload["sharedOutcome"]["actionIfFalse"],
        ).encode()
        contradictory = json.loads(
            json.dumps(parse_forecast_request_binding_v2(self.inputs["code"]))
        )
        contradictory["actionIfFalse"] = "Proceed despite failure."
        contradictory_block = expected.replace(
            json.dumps(
                parse_forecast_request_binding_v2(self.inputs["code"]),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            json.dumps(
                contradictory,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
        for replacement in (b"", contradictory_block, expected + b"\n" + contradictory_block):
            with self.subTest(replacement=replacement):
                visible = dict(self.inputs)
                visible["code"] = visible["code"].replace(expected, replacement)
                refs = dict(self.input_refs)
                refs["code"] = self.store.capture(visible["code"])
                payload = copy.deepcopy(self.attempt_payload)
                manifest = seat_input_manifest_sha256(refs)
                payload["sharedOutcome"]["decisionLink"] = payload[
                    "sharedOutcome"
                ]["decisionLink"].replace(
                    "inputManifestSha256="
                    + seat_input_manifest_sha256(self.input_refs),
                    "inputManifestSha256=" + manifest,
                )
                with self.assertRaisesRegex(
                    CaptureRuntimeError,
                    "^(retained prompt parse failure|"
                    "visible input for code forecast request block differs from "
                    "the sealed shared target)$",
                ):
                    append_council_attempt_v2(
                        self.ledger,
                        payload,
                        artifact_store=self.store,
                        decision_before_bytes=self.baseline_bytes,
                        seat_input_artifacts=refs,
                        visible_inputs=visible,
                        clock=at("2026-08-23T10:02:00Z"),
                        coordination_lock=self.lock,
                    )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 2)

    def test_operator_cannot_substitute_probability_after_output_capture(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload = self.completion_payload()
        payload["probabilities"]["code"] = 5

        with self.assertRaisesRegex(
            CaptureRuntimeError, "sharedProbability for code differs"
        ):
            append_council_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                visible_outputs=self.outputs,
                clock=at("2026-08-23T10:04:00Z"),
                coordination_lock=self.lock,
            )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 4)

    def test_non_empty_finding_must_be_exactly_seat_originated(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload, outputs, _seat_owned = self.completion_with_code_finding()
        invented = json.loads(outputs["code"])
        invented["capture"]["findings"] = []
        invented_bytes = json.dumps(
            invented, sort_keys=True, separators=(",", ":")
        ).encode()
        invented_ref = self.store.capture(invented_bytes)
        payload["seatResults"][0]["outputArtifact"] = invented_ref

        with self.assertRaisesRegex(
            CaptureRuntimeError,
            "visible output findings differ from the sealed completion findings",
        ):
            append_council_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                visible_outputs={**outputs, "code": invented_bytes},
                clock=at("2026-08-23T10:04:00Z"),
                coordination_lock=self.lock,
            )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 4)

    def test_report_rereads_baseline_and_revalidates_dispositions(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload, outputs, _seat_owned = self.completion_with_code_finding()
        append_council_v2(
            self.ledger,
            payload,
            artifact_store=self.store,
            decision_before_bytes=self.baseline_bytes,
            seat_input_artifacts=self.input_refs,
            visible_inputs=self.inputs,
            visible_outputs=outputs,
            clock=at("2026-08-23T10:04:00Z"),
            coordination_lock=self.lock,
        )

        rows = [json.loads(line) for line in self.ledger.read_bytes().splitlines()]
        rows[-1]["findings"][0]["operatorDisposition"][
            "considerationId"
        ] = "KC-not-in-baseline"
        self.ledger.write_bytes(
            b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
        self.assertEqual(report["findings"]["eligibleRunCount"], 0)
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(
            report["ledger"]["validatedV2RecordCount"]
            + report["ledger"]["invalidV2RecordCount"]
            + report["ledger"]["nonV2RecordCount"],
            report["ledger"]["rawRecordCount"],
        )
        self.assertTrue(report["ledger"]["recordCountReconciles"])

    def test_invalid_duplicate_completion_cannot_inherit_valid_finding_summary(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload, outputs, _seat_owned = self.completion_with_code_finding()
        completion, _summary = append_council_v2(
            self.ledger,
            payload,
            artifact_store=self.store,
            decision_before_bytes=self.baseline_bytes,
            seat_input_artifacts=self.input_refs,
            visible_inputs=self.inputs,
            visible_outputs=outputs,
            clock=at("2026-08-23T10:04:00Z"),
            coordination_lock=self.lock,
        )

        invalid_duplicate = copy.deepcopy(completion)
        invalid_duplicate["predictions"] = []
        invalid_duplicate["findings"] = []
        invalid_duplicate["noFindings"] = [
            {
                "kind": "no-findings",
                "seatId": seat,
                "outputArtifact": invalid_duplicate["seatResults"][index][
                    "outputArtifact"
                ],
            }
            for index, seat in enumerate(("code", "blind"))
        ]
        with self.ledger.open("ab") as handle:
            handle.write(
                (
                    json.dumps(
                        invalid_duplicate,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(report["findings"]["findingCount"], 1)
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]["findingCount"], 0
        )
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]
            ["operatorReportedDispositionMix"],
            {},
        )

    def test_invalid_then_valid_duplicate_preserves_clean_completion_lineage(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload, outputs, _seat_owned = self.completion_with_code_finding()
        completion, _summary = append_council_v2(
            self.ledger,
            payload,
            artifact_store=self.store,
            decision_before_bytes=self.baseline_bytes,
            seat_input_artifacts=self.input_refs,
            visible_inputs=self.inputs,
            visible_outputs=outputs,
            clock=at("2026-08-23T10:04:00Z"),
            coordination_lock=self.lock,
        )

        invalid_duplicate = copy.deepcopy(completion)
        invalid_duplicate["predictions"] = []
        invalid_duplicate["findings"] = []
        invalid_duplicate["noFindings"] = [
            {
                "kind": "no-findings",
                "seatId": seat,
                "outputArtifact": invalid_duplicate["seatResults"][index][
                    "outputArtifact"
                ],
            }
            for index, seat in enumerate(("code", "blind"))
        ]
        lines = self.ledger.read_bytes().splitlines(keepends=True)
        invalid_line = (
            json.dumps(
                invalid_duplicate,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        self.ledger.write_bytes(b"".join([*lines[:-1], invalid_line, lines[-1]]))

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 1)
        self.assertTrue(report["cohort"]["runs"][0]["complete"])
        self.assertIn(
            "schema-invalid-record:council-v2",
            report["cohort"]["runs"][1]["incompleteReasons"],
        )
        self.assertEqual(report["findings"]["findingCount"], 1)
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]["findingCount"], 0
        )
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]
            ["operatorReportedDispositionMix"],
            {},
        )

    def _assert_malformed_dispatch_before_valid_completion(self, malformed_kind):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload, outputs, _seat_owned = self.completion_with_code_finding()
        append_council_v2(
            self.ledger,
            payload,
            artifact_store=self.store,
            decision_before_bytes=self.baseline_bytes,
            seat_input_artifacts=self.input_refs,
            visible_inputs=self.inputs,
            visible_outputs=outputs,
            clock=at("2026-08-23T10:04:00Z"),
            coordination_lock=self.lock,
        )

        malformed_dispatch = {
            "schemaVersion": 2,
            "kind": malformed_kind,
            "runId": self.initiation["runId"],
            "initiationId": self.initiation["initiationId"],
        }
        lines = self.ledger.read_bytes().splitlines(keepends=True)
        malformed_line = (
            json.dumps(malformed_dispatch, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        self.ledger.write_bytes(b"".join([*lines[:-1], malformed_line, lines[-1]]))

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 1)
        self.assertTrue(report["cohort"]["runs"][0]["complete"])
        self.assertFalse(report["cohort"]["runs"][1]["complete"])
        self.assertIn(
            "schema-invalid-record:invalid-v2-record",
            report["cohort"]["runs"][1]["incompleteReasons"],
        )
        self.assertEqual(report["findings"]["findingCount"], 1)
        self.assertEqual(report["findings"]["summarizedRunCount"], 1)
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]["findingCount"], 0
        )
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]["summarizedRunCount"],
            0,
        )
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]
            ["submittedSeatCount"],
            0,
        )
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]
            ["operatorReportedDispositionMix"],
            {},
        )
        self.assertIsNone(
            report["findings"]["excludedOrInvalidStratum"]
            ["withinRunFindingOverlap"]
        )

    def test_non_text_dispatch_before_valid_completion_gets_own_position(self):
        self._assert_malformed_dispatch_before_valid_completion([])

    def test_unknown_text_dispatch_before_valid_completion_gets_own_position(self):
        self._assert_malformed_dispatch_before_valid_completion(
            "future-unknown-council-boundary"
        )

    def _assert_invalid_invalidation_is_diagnostic_only(self, report):
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertEqual(
            report["ledger"]["invalidV2Records"][0]["kind"],
            "capture-invalidation",
        )
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 1)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 1)
        self.assertTrue(report["cohort"]["runs"][0]["complete"])
        self.assertEqual(report["cohort"]["runs"][0]["invalidationCount"], 0)
        self.assertNotIn(
            "capture-invalidated",
            report["cohort"]["runs"][0]["incompleteReasons"],
        )
        self.assertEqual(report["findings"]["eligibleRunCount"], 1)
        self.assertEqual(report["findings"]["findingCount"], 1)
        self.assertEqual(
            report["findings"]["excludedOrInvalidStratum"]["runCount"], 0
        )

    def test_schema_invalid_invalidation_is_report_diagnostic_only(self):
        self.finish_with_code_finding()
        malformed = {
            "schemaVersion": 2,
            "kind": "capture-invalidation",
            "invalidationId": "invalidation-" + "1" * 32,
            "runId": self.initiation["runId"],
            "reason": "not-a-valid-invalidation-reason",
            "operator": "test-harness",
            "invalidatedAt": "2026-08-25T00:00:00Z",
            "evidenceRef": "fixture:schema-invalid-invalidation",
        }
        with self.ledger.open("ab") as handle:
            handle.write(
                (
                    json.dumps(malformed, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode()
            )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self._assert_invalid_invalidation_is_diagnostic_only(report)

    def test_future_as_of_invalidation_is_report_diagnostic_only(self):
        self.finish_with_code_finding()
        append_capture_invalidation(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "reason": "identity-error",
                "operator": "test-harness",
                "evidenceRef": "fixture:future-as-of-invalidation",
            },
            clock=at("2026-08-28T00:00:00Z"),
            coordination_lock=self.lock,
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self._assert_invalid_invalidation_is_diagnostic_only(report)

    def test_report_rejects_operator_invented_finding_absent_from_retained_output(self):
        self.finish()
        _payload, _outputs, seat_owned = self.completion_with_code_finding()
        rows = [json.loads(line) for line in self.ledger.read_bytes().splitlines()]
        completion = rows[-1]
        completion["findings"] = [
            {
                **seat_owned,
                "group": {
                    "findingGroupId": "finding-group-" + "3" * 32,
                    "runId": self.initiation["runId"],
                },
                "operatorDisposition": {"kind": "new-acted"},
            }
        ]
        completion["noFindings"] = [
            item for item in completion["noFindings"] if item["seatId"] != "code"
        ]
        self.ledger.write_bytes(
            b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 1)
        self.assertIn(
            "retained findings",
            report["ledger"]["invalidV2Records"][0]["error"],
        )

    def test_submitted_output_capture_envelope_is_strictly_bound(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        invalid_outputs = (
            b"not-json",
            json.dumps({"answer": "approve"}).encode(),
            json.dumps(
                {
                    "capture": {
                        "kind": "no-findings",
                        "seatId": "ops",
                        "sharedProbability": 70,
                    }
                }
            ).encode(),
            json.dumps(
                {
                    "capture": {
                        "kind": "no-findings",
                        "seatId": "code",
                        "sharedProbability": True,
                    }
                }
            ).encode(),
            json.dumps(
                {
                    "capture": {
                        "kind": "no-findings",
                        "seatId": "code",
                        "sharedProbability": 101,
                    }
                }
            ).encode(),
        )
        for invalid_output in invalid_outputs:
            with self.subTest(invalid_output=invalid_output):
                ref = self.store.capture(invalid_output)
                payload = self.completion_payload()
                payload["seatResults"][0]["outputArtifact"] = ref
                payload["noFindings"][0]["outputArtifact"] = ref
                visible = {**self.outputs, "code": invalid_output}
                with self.assertRaises(CaptureRuntimeError):
                    append_council_v2(
                        self.ledger,
                        payload,
                        artifact_store=self.store,
                        decision_before_bytes=self.baseline_bytes,
                        seat_input_artifacts=self.input_refs,
                        visible_inputs=self.inputs,
                        visible_outputs=visible,
                        clock=at("2026-08-23T10:04:00Z"),
                        coordination_lock=self.lock,
                    )

    def test_output_exactly_binds_request_and_its_input_artifact(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        substitutions = {
            "runId": "run-" + "f" * 32,
            "outcomeId": "outcome-" + "f" * 32,
            "outcomeFingerprint": "f" * 64,
            "evidenceCutoffAt": "2026-08-23T09:59:00Z",
            "forecastRequestSha256": "f" * 64,
            "inputArtifactSha256": "f" * 64,
        }
        for field, value in substitutions.items():
            with self.subTest(field=field):
                parsed = json.loads(self.outputs["code"])
                parsed["capture"][field] = value
                altered = json.dumps(parsed).encode()
                ref = self.store.capture(altered)
                payload = self.completion_payload()
                payload["seatResults"][0]["outputArtifact"] = ref
                payload["noFindings"][0]["outputArtifact"] = ref
                with self.assertRaisesRegex(CaptureRuntimeError, field):
                    append_council_v2(
                        self.ledger,
                        payload,
                        artifact_store=self.store,
                        decision_before_bytes=self.baseline_bytes,
                        seat_input_artifacts=self.input_refs,
                        visible_inputs=self.inputs,
                        visible_outputs={**self.outputs, "code": altered},
                        clock=at("2026-08-23T10:04:00Z"),
                        coordination_lock=self.lock,
                    )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 4)

    def test_unavailable_planned_blind_seat_uses_actual_run_scoped_brief(self):
        completion, summary = self.finish_without_blind_submission("unavailable")
        self.assertEqual(
            completion["blindSeat"]["brief"],
            blind_brief_identity(
                self.initiation["runId"], self.input_refs["blind"]["path"]
            ),
        )
        self.assertEqual(summary["emptyDeclarationRate"]["declarationCount"], 1)

    def test_abstained_planned_blind_seat_uses_actual_run_scoped_brief(self):
        completion, summary = self.finish_without_blind_submission("abstained")
        self.assertEqual(
            completion["blindSeat"]["brief"],
            blind_brief_identity(
                self.initiation["runId"], self.input_refs["blind"]["path"]
            ),
        )
        self.assertEqual(summary["emptyDeclarationRate"]["declarationCount"], 1)

    def test_non_submitted_input_reference_must_match_attempt_preflight(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "abstained"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )
        payload = self.completion_payload()
        payload["seatResults"][1] = {
            **self.seat_plan[1],
            "state": "abstained",
            "launcherAttempts": 1,
            "inputArtifact": self.input_refs["code"],
        }
        payload["noFindings"] = payload["noFindings"][:1]
        payload["probabilities"] = {"code": 70}
        payload["blindSeat"] = {
            "role": "SKIPPED",
            "required": True,
            "ran": False,
            "changedDecision": None,
            "brief": blind_brief_identity(
                self.initiation["runId"], self.input_refs["blind"]["path"]
            ),
            "blockedReason": "blind seat abstained",
        }

        with self.assertRaisesRegex(
            CaptureRuntimeError, "seat input artifacts differ"
        ):
            append_council_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                visible_outputs={"code": self.outputs["code"]},
                clock=at("2026-08-23T10:04:00Z"),
                coordination_lock=self.lock,
            )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 4)

    def test_unavailable_blind_rejects_unrelated_canonical_looking_brief(self):
        unrelated = "sha256/00/00/" + "0" * 64 + ".bin"
        with self.assertRaisesRegex(
            CaptureRuntimeError, "run-scoped canonical blind brief identity"
        ):
            self.finish_without_blind_submission(
                "unavailable",
                blind_brief=blind_brief_identity(
                    self.initiation["runId"], unrelated
                ),
            )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 4)

    def test_abstained_blind_rejects_unrelated_canonical_looking_brief(self):
        unrelated = "sha256/00/00/" + "0" * 64 + ".bin"
        with self.assertRaisesRegex(
            CaptureRuntimeError, "run-scoped canonical blind brief identity"
        ):
            self.finish_without_blind_submission(
                "abstained",
                blind_brief=blind_brief_identity(
                    self.initiation["runId"], unrelated
                ),
            )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 4)

    def test_finalized_at_clock_is_sampled_after_precommit_work(self):
        self.append_attempt()
        append_council_seats_finished(
            self.ledger,
            {
                "runId": self.initiation["runId"],
                "seatStates": {"code": "submitted", "blind": "submitted"},
            },
            clock=at("2026-08-23T10:03:00Z"),
            coordination_lock=self.lock,
        )

        events = []
        real_prepare = capture_runtime.prepare_council_v2
        real_summary = capture_runtime.summarize_findings
        real_append = capture_runtime._append_locked
        real_verify = self.store.verify

        def prepare(*args, **kwargs):
            events.append("prepared-schema")
            return real_prepare(*args, **kwargs)

        def verify(ref):
            events.append("artifact-verify")
            return real_verify(ref)

        def summarize(*args, **kwargs):
            events.append("findings-summary")
            return real_summary(*args, **kwargs)

        def clock():
            events.append("clock")
            return "2026-08-23T10:04:00Z"

        def append(*args, **kwargs):
            events.append("append")
            return real_append(*args, **kwargs)

        with (
            mock.patch.object(
                capture_runtime, "prepare_council_v2", side_effect=prepare
            ),
            mock.patch.object(self.store, "verify", side_effect=verify),
            mock.patch.object(
                capture_runtime, "summarize_findings", side_effect=summarize
            ),
            mock.patch.object(capture_runtime, "_append_locked", side_effect=append),
        ):
            completion, _summary = append_council_v2(
                self.ledger,
                self.completion_payload(),
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                visible_outputs=self.outputs,
                clock=clock,
                coordination_lock=self.lock,
            )

        self.assertEqual(
            completion["finalizedAt"], "2026-08-23T10:04:00.000000Z"
        )
        self.assertEqual(events.count("clock"), 1)
        self.assertLess(events.index("prepared-schema"), events.index("clock"))
        self.assertLess(
            max(index for index, event in enumerate(events) if event == "artifact-verify"),
            events.index("clock"),
        )
        self.assertLess(events.index("findings-summary"), events.index("clock"))
        self.assertEqual(events[-2:], ["clock", "append"])

    def test_concurrent_initiation_is_one_row_and_one_idempotent_replay(self):
        results = []

        def invoke():
            results.append(
                append_capture_initiation(
                    self.ledger,
                    {
                        "activationId": self.activation["activationId"],
                        "idempotencyKey": "decision-002-concurrent",
                    },
                    clock=at("2026-08-23T10:01:30Z"),
                    coordination_lock=self.lock,
                )
            )

        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([recorded for _row, recorded in results].count(True), 1)
        self.assertEqual([recorded for _row, recorded in results].count(False), 1)
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 3)

    def test_direct_activation_api_blocks_live_and_symlinked_live_targets(self):
        fake_live_root = self.root / "account" / ".claude" / "knowledge"
        fake_live_root.mkdir(parents=True)
        direct = fake_live_root / "council-eval" / "capture.jsonl"
        alias = self.root / "outside-alias"
        alias.symlink_to(fake_live_root, target_is_directory=True)
        aliased = alias / "council-eval" / "capture-via-alias.jsonl"

        with mock.patch.object(
            capture_runtime,
            "_os_account_live_write_roots",
            return_value=(fake_live_root.resolve(),),
        ):
            for target in (direct, aliased):
                with self.subTest(target=target):
                    with self.assertRaisesRegex(
                        CaptureRuntimeError, "direct live capture activation is blocked"
                    ):
                        append_capture_activation(
                            target,
                            {
                                "cohortName": "blocked-live",
                                "captureVersion": "capture-v2.0.0",
                                "runtimeSourceCommit": RUNTIME_COMMIT,
                                "artifactRootPolicy": "private-content-addressed-v1",
                            },
                            clock=at("2026-08-23T11:00:00Z"),
                            coordination_lock=self.root / "must-not-open.lock",
                        )
                    self.assertFalse(target.exists())
        self.assertFalse((self.root / "must-not-open.lock").exists())

    def test_direct_activation_rejects_plain_parent_substitution_before_lock(self):
        fake_live_root = self.root / "fake-live"
        fake_live_root.mkdir()
        local_parent = self.root / "local-activation"
        local_parent.mkdir()
        displaced_parent = self.root / "local-activation-before-swap"
        target = local_parent / "capture.jsonl"
        real_classifier = capture_runtime._is_live_activation_target

        def classify_then_swap(path, roots):
            classified = real_classifier(path, roots)
            self.assertFalse(classified)
            local_parent.rename(displaced_parent)
            fake_live_root.rename(local_parent)
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
                CaptureRuntimeError, "direct live capture activation is blocked"
            ):
                append_capture_activation(
                    target,
                    {
                        "cohortName": "plain-parent-substitution",
                        "captureVersion": "capture-v2.0.0",
                        "runtimeSourceCommit": RUNTIME_COMMIT,
                        "artifactRootPolicy": "private-content-addressed-v1",
                    },
                    clock=at("2026-08-23T11:00:00Z"),
                    coordination_lock=None,
                )

        self.assertFalse(target.exists())
        self.assertFalse(target.with_name("capture.jsonl.lock").exists())
        self.assertEqual(list(displaced_parent.iterdir()), [])

    def test_direct_activation_rejects_existing_live_inode_substitution(self):
        fake_live_root = self.root / "fake-live-inode"
        fake_live_root.mkdir()
        live_ledger = fake_live_root / "live.jsonl"
        sentinel = b'{"live":"must-remain-unchanged"}\n'
        live_ledger.write_bytes(sentinel)
        local_parent = self.root / "local-existing-activation"
        local_parent.mkdir()
        target = local_parent / "capture.jsonl"
        target.write_bytes(b"")
        displaced_target = local_parent / "capture-before-swap.jsonl"
        real_classifier = capture_runtime._is_live_activation_target

        def classify_then_swap(path, roots):
            classified = real_classifier(path, roots)
            self.assertFalse(classified)
            target.rename(displaced_target)
            live_ledger.rename(target)
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
                CaptureRuntimeError, "target changed after authorization"
            ):
                append_capture_activation(
                    target,
                    {
                        "cohortName": "live-inode-substitution",
                        "captureVersion": "capture-v2.0.0",
                        "runtimeSourceCommit": RUNTIME_COMMIT,
                        "artifactRootPolicy": "private-content-addressed-v1",
                    },
                    clock=at("2026-08-23T11:00:00Z"),
                    coordination_lock=None,
                )

        self.assertEqual(target.read_bytes(), sentinel)
        self.assertFalse(target.with_name("capture.jsonl.lock").exists())
        self.assertEqual(displaced_target.read_bytes(), b"")

    def test_nested_first_lifecycle_fsyncs_every_new_lock_and_ledger_ancestor(self):
        nested_ledger = self.root / "fresh-ledger" / "nested" / "council.jsonl"
        nested_lock = self.root / "fresh-lock" / "nested" / "evidence.lock"
        expected = {
            self.root / "fresh-ledger",
            nested_ledger.parent,
            self.root / "fresh-lock",
            nested_lock.parent,
        }
        fsynced = []
        real_fsync_directory = forecasts._fsync_directory

        def record_fsync(path):
            fsynced.append(Path(path))
            real_fsync_directory(path)

        with mock.patch.object(
            forecasts, "_fsync_directory", side_effect=record_fsync
        ):
            activation = append_capture_activation(
                nested_ledger,
                {
                    "cohortName": "nested-first-use",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": RUNTIME_COMMIT,
                    "artifactRootPolicy": "private-content-addressed-v1",
                },
                clock=at("2026-08-23T11:00:00Z"),
                coordination_lock=nested_lock,
            )
            _initiation, recorded = append_capture_initiation(
                nested_ledger,
                {
                    "activationId": activation["activationId"],
                    "idempotencyKey": "nested-first-initiation",
                },
                clock=at("2026-08-23T11:01:00Z"),
                coordination_lock=nested_lock,
            )

        self.assertTrue(recorded)
        self.assertTrue(expected.issubset(set(fsynced)))
        self.assertEqual(len(validate_capture_ledger(nested_ledger)[1]), 2)

    def test_report_retains_future_and_orphan_rows_as_incomplete_denominator_members(self):
        append_capture_initiation(
            self.ledger,
            {
                "activationId": self.activation["activationId"],
                "idempotencyKey": "decision-002-future",
            },
            clock=at("2026-08-30T10:01:00Z"),
            coordination_lock=self.lock,
        )
        orphan = {
            "schemaVersion": 2,
            "kind": "council-v2",
            "runId": "run-" + "f" * 32,
        }
        with self.ledger.open("ab") as handle:
            handle.write(
                (json.dumps(orphan, sort_keys=True, separators=(",", ":")) + "\n").encode()
            )
        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 3)
        self.assertEqual(report["cohort"]["completeInitiationCount"], 0)
        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 2)
        self.assertEqual(report["ledger"]["rawRecordCount"], 4)
        self.assertEqual(report["ledger"]["validatedV2RecordCount"], 2)
        self.assertEqual(report["ledger"]["nonV2RecordCount"], 0)
        self.assertTrue(report["ledger"]["recordCountReconciles"])
        self.assertEqual(
            report["ledger"]["validatedV2RecordCount"]
            + report["ledger"]["invalidV2RecordCount"]
            + report["ledger"]["nonV2RecordCount"],
            report["ledger"]["rawRecordCount"],
        )
        self.assertEqual(report["lifecycleCounts"]["orphanCompletionCount"], 1)
        reasons = {
            row["runId"]: row["incompleteReasons"]
            for row in report["cohort"]["runs"]
        }
        self.assertIn(
            "schema-invalid-record:capture-initiation",
            reasons[next(run for run in reasons if run not in {self.initiation["runId"], orphan["runId"]})],
        )
        self.assertIn("schema-invalid-record:council-v2", reasons[orphan["runId"]])

    def test_report_wires_exact_physical_record_identity_into_retry_accounting(self):
        physical_lines = self.ledger.read_bytes().splitlines(keepends=True)
        initiation_line = physical_lines[1]
        initiation = json.loads(initiation_line)
        byte_different_equal_json = (json.dumps(initiation, sort_keys=True) + "\n").encode()
        self.assertNotEqual(initiation_line, byte_different_equal_json)

        with self.ledger.open("ab") as handle:
            handle.write(initiation_line)
            handle.write(byte_different_equal_json)

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        self.assertEqual(report["ledger"]["invalidV2RecordCount"], 2)
        self.assertEqual(report["cohort"]["eligibleInitiationCount"], 2)
        self.assertEqual(
            report["cohort"]["duplicateIdempotentInitiationRetryCount"], 1
        )

    def test_secret_in_canonical_attempt_row_records_safe_invalidation(self):
        payload = copy.deepcopy(self.attempt_payload)
        payload["question"] = "Should token sk-proj-" + "A" * 40 + " be exposed?"
        with self.assertRaises(CaptureSecretDetectedError) as raised:
            append_council_attempt_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                clock=at("2026-08-23T10:02:00Z"),
                coordination_lock=self.lock,
            )
        self.assertNotIn("A" * 40, str(raised.exception))
        _raw, rows = validate_capture_ledger(self.ledger)
        invalidations = [row for row in rows if row["kind"] == "capture-invalidation"]
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(invalidations[0]["reason"], "secret-detected")
        self.assertNotIn("A" * 40, json.dumps(invalidations[0]))

    def test_secret_shaped_duplicate_seat_is_preflighted_before_schema(self):
        secret = "sk-proj-" + "S" * 40
        payload = copy.deepcopy(self.attempt_payload)
        payload["seatPlan"] = [
            {**self.seat_plan[0], "seatId": secret},
            {**self.seat_plan[1], "seatId": secret},
        ]

        with self.assertRaises(CaptureSecretDetectedError) as raised:
            append_council_attempt_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                clock=at("2026-08-23T10:02:00Z"),
                coordination_lock=self.lock,
            )

        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret.encode(), self.ledger.read_bytes())
        _raw, rows = validate_capture_ledger(self.ledger)
        invalidations = [row for row in rows if row["kind"] == "capture-invalidation"]
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(invalidations[0]["runId"], self.initiation["runId"])
        self.assertEqual(invalidations[0]["reason"], "secret-detected")

    def test_secret_in_explicit_invalidation_evidence_gets_safe_fallback(self):
        secret = "sk-proj-" + "I" * 40
        with self.assertRaises(CaptureSecretDetectedError) as raised:
            append_capture_invalidation(
                self.ledger,
                {
                    "runId": self.initiation["runId"],
                    "reason": "identity-error",
                    "operator": "operator",
                    "evidenceRef": f"unsafe incident metadata {secret}",
                },
                clock=at("2026-08-23T10:02:00Z"),
                coordination_lock=self.lock,
            )
        self.assertNotIn(secret, str(raised.exception))
        _raw, rows = validate_capture_ledger(self.ledger)
        invalidations = [row for row in rows if row["kind"] == "capture-invalidation"]
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(invalidations[0]["reason"], "secret-detected")
        self.assertEqual(invalidations[0]["operator"], "capture-runtime")
        self.assertNotIn(secret, json.dumps(invalidations[0]))

    def test_secret_in_explicit_invalidation_operator_gets_safe_fallback(self):
        secret = "sk-proj-" + "O" * 40
        with self.assertRaises(CaptureSecretDetectedError):
            append_capture_invalidation(
                self.ledger,
                {
                    "runId": self.initiation["runId"],
                    "reason": "identity-error",
                    "operator": f"unsafe operator {secret}",
                    "evidenceRef": "incident:operator-metadata",
                },
                clock=at("2026-08-23T10:02:00Z"),
                coordination_lock=self.lock,
            )
        self.assertNotIn(secret.encode(), self.ledger.read_bytes())
        _raw, rows = validate_capture_ledger(self.ledger)
        invalidations = [row for row in rows if row["kind"] == "capture-invalidation"]
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(invalidations[0]["reason"], "secret-detected")

    def test_secret_fallback_is_appended_before_invalidation_locks_exit(self):
        events = []
        real_evidence_lock = capture_runtime.evidence_write_lock
        real_ledger_lock = capture_runtime.ledger_write_transaction
        real_append = capture_runtime._append_locked

        @contextmanager
        def evidence_lock(*args, **kwargs):
            events.append("evidence-enter")
            try:
                with real_evidence_lock(*args, **kwargs):
                    yield
            finally:
                events.append("evidence-exit")

        @contextmanager
        def ledger_lock(*args, **kwargs):
            events.append("ledger-enter")
            try:
                with real_ledger_lock(*args, **kwargs) as transaction:
                    yield transaction
            finally:
                events.append("ledger-exit")

        def append(path, row):
            events.append(f"append:{row['reason']}")
            return real_append(path, row)

        secret = "sk-proj-" + "L" * 40
        with (
            mock.patch.object(
                capture_runtime, "evidence_write_lock", side_effect=evidence_lock
            ),
            mock.patch.object(
                capture_runtime, "ledger_write_transaction", side_effect=ledger_lock
            ),
            mock.patch.object(capture_runtime, "_append_locked", side_effect=append),
        ):
            with self.assertRaises(CaptureSecretDetectedError):
                append_capture_invalidation(
                    self.ledger,
                    {
                        "runId": self.initiation["runId"],
                        "reason": "identity-error",
                        "operator": "operator",
                        "evidenceRef": secret,
                    },
                    clock=at("2026-08-23T10:02:00Z"),
                    coordination_lock=self.lock,
                )

        self.assertEqual(
            events,
            [
                "evidence-enter",
                "ledger-enter",
                "append:secret-detected",
                "ledger-exit",
                "evidence-exit",
            ],
        )

    def test_secret_in_capture_resolution_is_rejected_before_sidecar_append(self):
        completion, _summary = self.finish()
        secret = "sk-proj-" + "R" * 40
        with self.assertRaises(CaptureSecretDetectedError) as raised:
            append_capture_resolution(
                self.ledger,
                self.events,
                outcome_id=completion["sharedOutcome"]["outcomeId"],
                came_true=True,
                evidence=f"unsafe resolution evidence {secret}",
                resolver="test-harness",
                resolved_at="2026-08-26T00:00:00Z",
                method="deterministic",
                coordination_lock=self.lock,
            )
        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(self.events.exists())

    def test_canonical_json_aws_secret_in_resolution_evidence_is_rejected(self):
        completion, _summary = self.finish()
        secret_value = "A" * 40
        evidence = json.dumps(
            {"AWS_SECRET_ACCESS_KEY": secret_value},
            sort_keys=True,
            separators=(",", ":"),
        )
        ledger_before = self.ledger.read_bytes()

        with self.assertRaises(CaptureSecretDetectedError) as raised:
            append_capture_resolution(
                self.ledger,
                self.events,
                outcome_id=completion["sharedOutcome"]["outcomeId"],
                came_true=True,
                evidence=evidence,
                resolver="test-harness",
                resolved_at="2026-08-26T00:00:00Z",
                method="deterministic",
                coordination_lock=self.lock,
            )

        self.assertNotIn(secret_value, str(raised.exception))
        self.assertEqual(raised.exception.detectors, ("aws-secret-assignment",))
        self.assertEqual(self.ledger.read_bytes(), ledger_before)
        self.assertFalse(self.events.exists())

    def test_secret_unknown_resolution_id_is_preflighted_before_lookup(self):
        secret = "sk-proj-" + "U" * 40

        with self.assertRaises(CaptureSecretDetectedError) as raised:
            append_capture_resolution(
                self.ledger,
                self.events,
                outcome_id=secret,
                came_true=True,
                evidence="deterministic fixture",
                resolver="test-harness",
                resolved_at="2026-08-26T00:00:00Z",
                method="deterministic",
                coordination_lock=self.lock,
            )

        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(self.events.exists())

    def test_report_rejects_secret_in_raw_invalid_v2_before_projection(self):
        secret = "sk-proj-" + "W" * 40
        with self.ledger.open("ab") as handle:
            handle.write(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "kind": "council-v2",
                        "runId": secret,
                    },
                    sort_keys=True,
                ).encode()
                + b"\n"
            )

        with self.assertRaises(CaptureSecretDetectedError) as raised:
            capture_report(
                self.ledger,
                self.events,
                artifact_store=self.store,
                as_of="2026-08-27T00:00:00Z",
            )

        self.assertNotIn(secret, str(raised.exception))

    def test_git_blob_binding_is_verified_against_exact_baseline_bytes(self):
        payload = copy.deepcopy(self.attempt_payload)
        payload["decisionBeforeArtifact"]["gitBlob"] = "b" * 40
        payload["sharedOutcome"]["decisionLink"] = payload["sharedOutcome"][
            "decisionLink"
        ].replace(self.baseline_blob, "b" * 40)
        with self.assertRaises(ArtifactIntegrityError):
            append_council_attempt_v2(
                self.ledger,
                payload,
                artifact_store=self.store,
                decision_before_bytes=self.baseline_bytes,
                seat_input_artifacts=self.input_refs,
                visible_inputs=self.inputs,
                clock=at("2026-08-23T10:02:00Z"),
                coordination_lock=self.lock,
            )
        self.assertEqual(len(validate_capture_ledger(self.ledger)[1]), 2)

    def test_atomic_append_replace_failure_preserves_prior_ledger_bytes(self):
        before = self.ledger.read_bytes()
        escrows_before = set(
            self.root.glob(f".{self.ledger.name}.*.tmp.escrow.*")
        )
        with mock.patch(
            "council_tools.safe_files._exchange_names_pinned",
            side_effect=OSError("injected replace failure"),
        ):
            with self.assertRaises(CaptureRuntimeError) as raised:
                append_capture_initiation(
                    self.ledger,
                    {
                        "activationId": self.activation["activationId"],
                        "idempotencyKey": "decision-atomic-failure",
                    },
                    clock=at("2026-08-23T10:01:30Z"),
                    coordination_lock=self.lock,
                )
        self.assertIn("injected replace failure", str(raised.exception))
        self.assertIn(
            "recoverable transaction escrow retained", str(raised.exception)
        )
        self.assertEqual(self.ledger.read_bytes(), before)
        escrows = list(
            set(self.root.glob(f".{self.ledger.name}.*.tmp.escrow.*"))
            - escrows_before
        )
        self.assertEqual(len(escrows), 1)
        candidate = escrows[0].read_bytes()
        self.assertTrue(candidate.startswith(before))
        candidate_rows = [json.loads(line) for line in candidate.splitlines()]
        self.assertEqual(len(candidate_rows), len(before.splitlines()) + 1)
        self.assertEqual(candidate_rows[-1]["kind"], "capture-initiation")
        self.assertEqual(
            candidate_rows[-1]["idempotencyKey"], "decision-atomic-failure"
        )

    def test_capture_append_rejects_a_hardlinked_ledger_identity(self):
        alias = self.root / "ledger-alias.jsonl"
        os.link(self.ledger, alias)
        before = self.ledger.read_bytes()

        with self.assertRaisesRegex(CaptureRuntimeError, "hardlink alias"):
            self.append_attempt()

        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(alias.read_bytes(), before)
        self.assertEqual(list(self.root.glob(f".{self.ledger.name}.*.tmp")), [])

    def test_atomic_resolution_correction_failure_preserves_prior_sidecar_bytes(self):
        completion, _summary = self.finish()
        first = append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=False,
            evidence="initial deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        before = self.events.read_bytes()
        escrows_before = set(
            self.root.glob(f".{self.events.name}.*.tmp.escrow.*")
        )
        with mock.patch(
            "council_tools.safe_files._exchange_names_pinned",
            side_effect=OSError("injected resolution replace failure"),
        ):
            with self.assertRaises(CaptureRuntimeError) as raised:
                append_capture_resolution(
                    self.ledger,
                    self.events,
                    outcome_id=completion["sharedOutcome"]["outcomeId"],
                    came_true=True,
                    evidence="corrected deterministic retained fixture",
                    resolver="test-harness",
                    resolved_at="2026-08-26T01:00:00Z",
                    method="deterministic",
                    supersedes_resolution_id=first["resolutionId"],
                    coordination_lock=self.lock,
                )
        self.assertIn(
            "injected resolution replace failure", str(raised.exception)
        )
        self.assertIn(
            "recoverable transaction escrow retained", str(raised.exception)
        )
        self.assertEqual(self.events.read_bytes(), before)
        self.assertEqual(list(self.root.glob(f".{self.events.name}.*.tmp")), [])
        escrows = list(
            set(self.root.glob(f".{self.events.name}.*.tmp.escrow.*"))
            - escrows_before
        )
        self.assertEqual(len(escrows), 1)
        candidate = escrows[0].read_bytes()
        self.assertTrue(candidate.startswith(before))
        candidate_rows = [json.loads(line) for line in candidate.splitlines()]
        self.assertEqual(len(candidate_rows), len(before.splitlines()) + 1)
        self.assertTrue(candidate_rows[-1]["cameTrue"])
        self.assertEqual(
            candidate_rows[-1]["supersedesResolutionId"], first["resolutionId"]
        )

    def test_capture_report_preserves_normal_resolution_correction_chain(self):
        completion, _summary = self.finish()
        first = append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=False,
            evidence="initial deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T00:00:00Z",
            method="deterministic",
            coordination_lock=self.lock,
        )
        append_capture_resolution(
            self.ledger,
            self.events,
            outcome_id=completion["sharedOutcome"]["outcomeId"],
            came_true=True,
            evidence="corrected deterministic retained fixture",
            resolver="test-harness",
            resolved_at="2026-08-26T01:00:00Z",
            method="deterministic",
            supersedes_resolution_id=first["resolutionId"],
            coordination_lock=self.lock,
        )

        report = capture_report(
            self.ledger,
            self.events,
            artifact_store=self.store,
            as_of="2026-08-27T00:00:00Z",
        )

        accuracy = report["descriptiveForecastAccuracy"]
        self.assertEqual(report["ledger"]["captureResolutionEventCount"], 2)
        self.assertEqual(accuracy["resolvedIssuanceCount"], 1)
        self.assertEqual(accuracy["predictionCount"], 2)
        self.assertAlmostEqual(accuracy["meanBrier"], 0.125)

    def test_shared_lock_coordinates_v1_writer_and_snapshot_restore(self):
        self.finish()
        self.events.touch(mode=0o600)
        controls = self.root / "controls"
        controls.mkdir(mode=0o700)
        (controls / "state.jsonl").write_text("{}\n", encoding="utf-8")
        os.chmod(controls / "state.jsonl", 0o600)
        snapshot = self.root / "snapshot"
        repository = self.root / "repository"
        repository.mkdir()

        lock_fd = os.open(self.lock, os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        v1_log = self.root / "v1.jsonl"
        v1_attempt = make_attempt(
            question="Does the coordinated writer append?",
            expected_seats=["blind"],
            claim="The coordinated writer appends after the shared lock releases.",
            resolution_date="2026-09-30",
            resolved_by="Inspect the fixture.",
            decision_link="integration-test",
            materiality="Exercises coherent backup locking.",
            action_if_true="Keep the shared lock.",
            action_if_false="Repair coordination.",
            evidence_cutoff_at="2026-08-23T10:00:00Z",
            ts="2026-08-23T10:01:00Z",
        )
        writer = threading.Thread(
            target=append_ledger_row,
            args=(v1_log, v1_attempt),
            kwargs={"coordination_lock": self.lock},
        )
        writer.start()
        time.sleep(0.1)
        self.assertTrue(writer.is_alive())
        self.assertFalse(v1_log.exists())
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        writer.join(timeout=2)
        self.assertFalse(writer.is_alive())
        self.assertTrue(v1_log.exists())

        created = create_evidence_snapshot(
            ledger_path=self.ledger,
            resolution_store_path=self.events,
            control_store_path=controls,
            artifact_root=self.artifact_root,
            lock_path=self.lock,
            snapshot_target=snapshot,
            repository_root=repository,
        )
        restored = restore_evidence_snapshot(
            snapshot, self.root / "restore", repository_root=repository
        )
        self.assertEqual(restored, created)
        self.assertEqual(created["scope"], "local-filesystem-rehearsal-only")


if __name__ == "__main__":
    unittest.main()
