import copy
import hashlib
import json
import unittest
from datetime import timedelta

from council_tools.activation_evidence import (
    AUDIT_CONTROL_KEY,
    CONTROL_KEYS,
    DURABILITY_CONTROL_KEYS,
    evaluate_activation_evidence,
    parse_activation_manifest_v2,
)
from council_tools.finding_audit import make_audit_protocol, rehearse_audit_protocol
from council_tools.offhost_durability import DurabilityPolicy


COMMIT = "a" * 40
SOURCE_SHA = "b" * 64
ACTIVATION_TIME = "2026-08-23T12:00:00Z"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _ref(data):
    digest = hashlib.sha256(data).hexdigest()
    return {
        "path": f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.bin",
        "sha256": digest,
        "bytes": len(data),
    }


def _reseal_audit(certificate):
    body = {key: value for key, value in certificate.items() if key != "certificateSha256"}
    certificate["certificateSha256"] = hashlib.sha256(_json(body)).hexdigest()


class MemoryReader:
    def __init__(self):
        self.content = {}
        self.reads = []

    def add(self, data):
        ref = _ref(data)
        self.content[ref["path"]] = data
        return ref

    def read_verified(self, ref):
        self.reads.append(ref["path"])
        return self.content[ref["path"]]


class EvidenceFixture:
    """Produce the native finding-audit and aligned durability documents."""

    def __init__(self):
        self.frozen_protocol = b"frozen amendment 027 finding-audit contract\n"
        self.audit_protocol = None
        self.audit = None
        self.durability_policy = DurabilityPolicy(
            target_uri="gs://council-evidence-private",
            retention_seconds=90 * 86400,
            rpo_seconds=86400,
            rto_seconds=600,
            max_snapshot_age_seconds=86400,
            max_restore_evidence_age_seconds=86400,
            encryption_access_posture="runtime writer cannot read credentials or delete",
            access_posture="create-only writer and separate verifier",
            failure_domain_caveats="separate GCS failure domain under the same principal",
            failure_domain_caveat_acknowledged=True,
        ).document()
        self.activation_policy = None
        self.max_certificate_age_seconds = 86400
        self.durability = None
        self.manifest = None
        self.reader = None

    def _durability_certificate(self, activation_policy_sha):
        prefix = "snapshots/snapshot-027"
        return {
            "bucket": {
                "automaticApplicationDeletion": False,
                "encryptionAtRest": "provider-managed",
                "private": True,
                "publicAccessPrevention": "enforced",
                "retentionPolicy": {"configured": True, "days": 90, "locked": False},
                "uniformBucketAccess": True,
                "uri": "gs://council-evidence-private",
                "versioning": True,
            },
            "certificateId": "durability-3333333333333333-103",
            "elapsedSeconds": 300,
            "expiresAt": "2026-08-24T11:00:00.000000Z",
            "failureDomainCaveatAcknowledged": True,
            "indexObject": {
                "bytes": 50,
                "generation": "103",
                "name": prefix + "/index.json",
                "sha256": "5" * 64,
            },
            "issuedAt": "2026-08-23T11:07:00.000000Z",
            "kind": "off-host-durability-certificate",
            "policySha256": activation_policy_sha,
            "prefix": prefix,
            "provider": "gcs",
            "readback": {
                "completedAt": "2026-08-23T11:03:00.000000Z",
                "downloadedBytes": 1050,
                "generationPinned": True,
                "indexGeneration": "103",
                "manifestVerified": True,
                "startedAt": "2026-08-23T11:02:00.000000Z",
            },
            "remoteObjects": [
                {
                    "bytes": 100,
                    "generation": "101",
                    "name": prefix + "/members/00000000-manifest",
                    "sha256": "3" * 64,
                },
                {
                    "bytes": 900,
                    "generation": "102",
                    "name": prefix + "/members/00000001-ledger",
                    "sha256": "4" * 64,
                },
            ],
            "restore": {
                "cleanTarget": True,
                "completedAt": "2026-08-23T11:06:00.000000Z",
                "restoredBytes": 1000,
                "restoredEvidenceValidated": True,
                "snapshotVerified": True,
                "startedAt": "2026-08-23T11:04:00.000000Z",
            },
            "runtimeSourceCommit": COMMIT,
            "runtimeSourceSha256": SOURCE_SHA,
            "schemaVersion": 1,
            "snapshot": {
                "bytes": 1000,
                "cutAt": "2026-08-23T11:00:00.000000Z",
                "manifestObjectName": prefix + "/members/00000000-manifest",
                "manifestSha256": "3" * 64,
            },
            "upload": {
                "completedAt": "2026-08-23T11:02:00.000000Z",
                "startedAt": "2026-08-23T11:01:00.000000Z",
                "uploadedBytes": 1050,
            },
        }

    def rebuild(self):
        reader = MemoryReader()
        frozen_ref = reader.add(self.frozen_protocol)
        if self.audit_protocol is None:
            self.audit_protocol = make_audit_protocol(
                frozen_protocol_artifact=frozen_ref,
                selection_residue=2,
            )
        else:
            self.audit_protocol["frozenProtocolArtifact"] = frozen_ref
        audit_protocol_ref = reader.add(_json(self.audit_protocol))
        durability_policy_ref = reader.add(_json(self.durability_policy))
        self.activation_policy = {
            "schemaVersion": 1,
            "kind": "activation-evidence-policy",
            "policyId": "policy-027",
            "runtimeSourceCommit": COMMIT,
            "runtimeSourceSha256": SOURCE_SHA,
            "issuedAt": "2026-08-23T08:00:00Z",
            "expiresAt": "2026-08-24T12:00:00Z",
            "maxClockSkewSeconds": 300,
            "maxCertificateAgeSeconds": self.max_certificate_age_seconds,
            "auditProtocolRef": audit_protocol_ref,
            "durabilityPolicyRef": durability_policy_ref,
            "requiredControls": list(CONTROL_KEYS),
        }
        activation_policy_ref = reader.add(_json(self.activation_policy))
        if self.audit is None:
            self.audit = rehearse_audit_protocol(
                self.audit_protocol,
                runtime_commit=COMMIT,
                source_tree_sha256=SOURCE_SHA,
                rehearsed_at="2026-08-23T10:00:00.000000Z",
            )
        if self.durability is None:
            self.durability = self._durability_certificate(
                activation_policy_ref["sha256"]
            )
        audit_ref = reader.add(_json(self.audit))
        durability_ref = reader.add(_json(self.durability))
        self.manifest = {
            "schemaVersion": 2,
            "activationId": "activation-027",
            "runtimeSourceCommit": COMMIT,
            "runtimeSourceSha256": SOURCE_SHA,
            "issuedAt": "2026-08-23T11:10:00Z",
            "expiresAt": "2026-08-24T12:00:00Z",
            "policyRef": activation_policy_ref,
            "controls": {
                key: audit_ref if key == AUDIT_CONTROL_KEY else durability_ref
                for key in CONTROL_KEYS
            },
        }
        self.reader = reader
        return _json(self.manifest), reader


class ActivationEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = EvidenceFixture()

    def evaluate(self, manifest=None, reader=None, **kwargs):
        if manifest is None or reader is None:
            built_manifest, built_reader = self.fixture.rebuild()
            manifest = built_manifest if manifest is None else manifest
            reader = built_reader if reader is None else reader
        return evaluate_activation_evidence(
            manifest,
            reader=reader,
            expected_runtime_commit=kwargs.pop("expected_runtime_commit", COMMIT),
            expected_source_sha256=kwargs.pop("expected_source_sha256", SOURCE_SHA),
            activation_time=kwargs.pop("activation_time", ACTIVATION_TIME),
            **kwargs,
        )

    def evaluate_fixture(self, fixture, **kwargs):
        manifest, reader = fixture.rebuild()
        return evaluate_activation_evidence(
            manifest,
            reader=reader,
            expected_runtime_commit=COMMIT,
            expected_source_sha256=SOURCE_SHA,
            activation_time=ACTIVATION_TIME,
            **kwargs,
        )

    def assertBlocked(self, result, code):
        self.assertFalse(result["appendReady"])
        self.assertFalse(result["activationVerdict"]["ready"])
        self.assertFalse(result["currentHealth"]["healthy"])
        self.assertIn(code, result["blockers"])

    def test_native_certificates_are_accepted_without_status_labels(self):
        result = self.evaluate()
        self.assertTrue(result["appendReady"])
        self.assertTrue(result["activationVerdict"]["ready"])
        self.assertTrue(result["currentHealth"]["healthy"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["activationId"], "activation-027")
        self.assertEqual(len(self.fixture.reader.reads), 6)
        self.assertNotIn("status", self.fixture.audit)
        self.assertNotIn("status", self.fixture.durability)

    def test_manifest_duplicate_unknown_and_control_shapes_fail_closed(self):
        manifest, reader = self.fixture.rebuild()
        duplicate = manifest.replace(b'{"activationId"', b'{"schemaVersion":2,"activationId"')
        self.assertBlocked(self.evaluate(duplicate, reader), "manifest-invalid-json")

        decoded = json.loads(manifest)
        decoded["status"] = "APPROVED"
        self.assertBlocked(self.evaluate(_json(decoded), reader), "manifest-invalid-schema")

        decoded = json.loads(manifest)
        decoded["controls"].pop("retention")
        self.assertBlocked(self.evaluate(_json(decoded), reader), "manifest-invalid-controls")

        decoded = json.loads(manifest)
        decoded["controls"]["retention"] = decoded["controls"][AUDIT_CONTROL_KEY]
        self.assertBlocked(
            self.evaluate(_json(decoded), reader),
            "manifest-durability-reference-mismatch",
        )

    def test_parse_returns_normalized_refs_without_reading(self):
        manifest, _reader = self.fixture.rebuild()
        parsed = parse_activation_manifest_v2(manifest)
        self.assertEqual(parsed["activationId"], "activation-027")
        self.assertEqual(set(parsed["controls"]), set(CONTROL_KEYS))
        self.assertEqual(
            parsed["issuedAt"].tzinfo.utcoffset(parsed["issuedAt"]), timedelta(0)
        )

    def test_altered_reference_and_altered_bytes_are_rejected(self):
        manifest, reader = self.fixture.rebuild()
        decoded = json.loads(manifest)
        decoded["policyRef"]["sha256"] = "9" * 64
        self.assertBlocked(self.evaluate(_json(decoded), reader), "manifest-invalid-reference")

        manifest, reader = self.fixture.rebuild()
        policy_path = self.fixture.manifest["policyRef"]["path"]
        reader.content[policy_path] += b" "
        self.assertBlocked(self.evaluate(manifest, reader), "policy-artifact-integrity")

    def test_duplicate_and_unknown_native_certificates_are_rejected(self):
        manifest, reader = self.fixture.rebuild()
        audit_ref = self.fixture.manifest["controls"][AUDIT_CONTROL_KEY]
        raw = reader.content[audit_ref["path"]]
        duplicate = raw.replace(b'{"actualProspectiveCounts"', b'{"schemaVersion":1,"actualProspectiveCounts"')
        duplicate_ref = reader.add(duplicate)
        decoded = json.loads(manifest)
        decoded["controls"][AUDIT_CONTROL_KEY] = duplicate_ref
        self.assertBlocked(
            self.evaluate(_json(decoded), reader), "audit-artifact-invalid-json"
        )

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.audit["status"] = "VERIFIED"
        _reseal_audit(fixture.audit)
        self.assertBlocked(self.evaluate_fixture(fixture), "audit-certificate-invalid")

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.durability["status"] = "VERIFIED"
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-invalid-schema"
        )

    def test_runtime_source_binding_is_required_on_manifest_and_native_certs(self):
        manifest, reader = self.fixture.rebuild()
        decoded = json.loads(manifest)
        decoded["runtimeSourceSha256"] = "c" * 64
        result = self.evaluate(_json(decoded), reader)
        self.assertBlocked(result, "manifest-runtime-source-mismatch")
        self.assertEqual(reader.reads, [])

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.audit["runtimeCommit"] = "c" * 40
        _reseal_audit(fixture.audit)
        self.assertBlocked(
            self.evaluate_fixture(fixture), "audit-runtime-source-mismatch"
        )

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.durability["runtimeSourceSha256"] = "c" * 64
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-runtime-source-mismatch"
        )

    def test_native_audit_protocol_digest_and_frozen_artifact_are_dereferenced(self):
        manifest, reader = self.fixture.rebuild()
        frozen_path = self.fixture.audit_protocol["frozenProtocolArtifact"]["path"]
        reader.content[frozen_path] += b"altered"
        self.assertBlocked(
            self.evaluate(manifest, reader), "audit-frozen-protocol-artifact-integrity"
        )

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.audit["protocolSha256"] = "6" * 64
        _reseal_audit(fixture.audit)
        self.assertBlocked(
            self.evaluate_fixture(fixture), "audit-certificate-invalid"
        )

    def test_native_audit_requires_zero_actual_counts_and_rehearsed_checks(self):
        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.audit["actualProspectiveCounts"]["assignments"] = 1
        _reseal_audit(fixture.audit)
        self.assertBlocked(
            self.evaluate_fixture(fixture), "audit-certificate-invalid"
        )

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.audit["checks"]["omittedClaimDetection"] = False
        _reseal_audit(fixture.audit)
        self.assertBlocked(
            self.evaluate_fixture(fixture), "audit-certificate-invalid"
        )

    def test_policy_artifacts_are_bound_and_must_match_certificates(self):
        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.durability["policySha256"] = "6" * 64
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-policy-mismatch"
        )

        fixture = EvidenceFixture()
        fixture.durability_policy["targetUri"] = "gs://different-private-bucket"
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-provider-policy-mismatch"
        )

    def test_durability_requires_posture_generations_readback_and_restore(self):
        cases = (
            (("bucket", "private"), False, "durability-encryption-access-not-proven"),
            (("bucket", "versioning"), False, "durability-off-host-custody-not-proven"),
            (("bucket", "automaticApplicationDeletion"), True, "durability-retention-not-proven"),
            (("readback", "generationPinned"), False, "durability-readback-not-proven"),
            (("restore", "cleanTarget"), False, "durability-restore-not-proven"),
        )
        for path, value, blocker in cases:
            with self.subTest(path=path):
                fixture = EvidenceFixture()
                fixture.rebuild()
                fixture.durability[path[0]][path[1]] = value
                self.assertBlocked(self.evaluate_fixture(fixture), blocker)

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.durability["remoteObjects"][0]["generation"] = "latest"
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-object-chain-invalid"
        )

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.durability["readback"]["indexGeneration"] = "999"
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-object-chain-invalid"
        )

    def test_durability_rejects_bad_order_byte_chain_elapsed_rto_and_expiry(self):
        cases = (
            (lambda doc: doc["restore"].update(completedAt="2026-08-23T11:03:30Z"), "durability-invalid-event-order"),
            (lambda doc: doc["restore"].update(restoredBytes=999), "durability-object-chain-invalid"),
            (lambda doc: doc.update(elapsedSeconds=299), "durability-elapsed-mismatch"),
            (lambda doc: doc.update(expiresAt="2026-08-24T10:00:00Z"), "durability-expiry-mismatch"),
        )
        for mutate, blocker in cases:
            with self.subTest(blocker=blocker):
                fixture = EvidenceFixture()
                fixture.rebuild()
                mutate(fixture.durability)
                self.assertBlocked(self.evaluate_fixture(fixture), blocker)

        fixture = EvidenceFixture()
        fixture.durability_policy["rtoSeconds"] = 299
        fixture.rebuild()
        fixture.durability["elapsedSeconds"] = 300
        self.assertBlocked(self.evaluate_fixture(fixture), "durability-rto-exceeded")

    def test_future_expired_and_stale_evidence_block(self):
        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.audit["rehearsedAt"] = "2026-08-23T12:10:00.000000Z"
        _reseal_audit(fixture.audit)
        result = self.evaluate_fixture(fixture)
        self.assertBlocked(result, "audit-issued-in-future")
        self.assertIn("audit-evidence-in-future", result["blockers"])

        fixture = EvidenceFixture()
        fixture.rebuild()
        fixture.durability["expiresAt"] = ACTIVATION_TIME
        self.assertBlocked(self.evaluate_fixture(fixture), "durability-expiry-mismatch")

        fixture = EvidenceFixture()
        fixture.max_certificate_age_seconds = 60
        result = self.evaluate_fixture(fixture)
        self.assertBlocked(result, "manifest-stale")
        self.assertIn("policy-stale", result["blockers"])

    def test_rpo_snapshot_and_restore_freshness_are_computed(self):
        fixture = EvidenceFixture()
        fixture.durability_policy["rpoSeconds"] = 3000
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-rpo-exceeded"
        )

        fixture = EvidenceFixture()
        fixture.durability_policy["maxSnapshotAgeSeconds"] = 3000
        fixture.rebuild()
        fixture.durability["expiresAt"] = "2026-08-23T11:50:00.000000Z"
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-snapshot-stale"
        )

        fixture = EvidenceFixture()
        fixture.durability_policy["maxRestoreEvidenceAgeSeconds"] = 3000
        fixture.rebuild()
        fixture.durability["expiresAt"] = "2026-08-23T11:56:00.000000Z"
        self.assertBlocked(
            self.evaluate_fixture(fixture), "durability-restore-evidence-stale"
        )

    def test_historical_verdict_is_separate_from_current_health(self):
        result = self.evaluate(as_of="2026-08-25T00:00:00Z")
        self.assertTrue(result["activationVerdict"]["ready"])
        self.assertEqual(result["activationVerdict"]["blockers"], [])
        self.assertFalse(result["currentHealth"]["healthy"])
        self.assertIn("manifest-expired", result["currentHealth"]["blockers"])
        self.assertFalse(result["appendReady"])

    def test_invalid_evidence_never_claims_append_readiness(self):
        for data in (b"not json", b"{}", b"[]"):
            with self.subTest(data=data):
                result = self.evaluate(data, MemoryReader())
                self.assertFalse(result["appendReady"])
                self.assertFalse(result["activationVerdict"]["ready"])
                self.assertFalse(result["currentHealth"]["healthy"])
                self.assertTrue(result["blockers"])

    def test_bad_trusted_bindings_and_clocks_raise(self):
        manifest, reader = self.fixture.rebuild()
        with self.assertRaises(ValueError):
            evaluate_activation_evidence(
                manifest,
                reader=reader,
                expected_runtime_commit="not-a-commit",
                expected_source_sha256=SOURCE_SHA,
                activation_time=ACTIVATION_TIME,
            )
        with self.assertRaises(ValueError):
            evaluate_activation_evidence(
                manifest,
                reader=reader,
                expected_runtime_commit=COMMIT,
                expected_source_sha256=SOURCE_SHA,
                activation_time=ACTIVATION_TIME,
                as_of="2026-08-22T00:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
