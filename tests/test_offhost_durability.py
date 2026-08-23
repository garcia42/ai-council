import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from council_tools import activation_evidence
from council_tools.evidence_backup import create_evidence_snapshot
from council_tools.gcs_durability import GcsVersionedObjectStore
from council_tools.offhost_durability import (
    DurabilityPolicy,
    ObjectVersion,
    OffHostDurabilityError,
    OffHostIntegrityError,
    OffHostPolicyError,
    OffHostTransportError,
    SnapshotExport,
    SnapshotMember,
    StorageConfiguration,
    run_offhost_durability_rehearsal,
    snapshot_export_from_directory,
    verify_durability_certificate,
)


RUNTIME_COMMIT = "a" * 40
SOURCE_TREE_SHA256 = "b" * 64
ACTIVATION_POLICY_SHA256 = "c" * 64
BUCKET_URI = "gs://council-evidence-test"
ACCESS_POSTURE = "dedicated create-read service identity; no delete permission"
NOW = datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)


class FakeVersionedStore:
    def __init__(self, configuration=None):
        self._configuration = configuration or StorageConfiguration(
            provider="gcs",
            bucket_uri=BUCKET_URI,
            failure_domain="gcs:region:us-east4",
            versioning_enabled=True,
            public_access_prevention="enforced",
            uniform_bucket_access=True,
            retention_seconds=30 * 86400,
            retention_locked=False,
            encryption_at_rest="provider-managed",
            access_posture=ACCESS_POSTURE,
            automatic_application_deletion=False,
        )
        self.generations = {}
        self.create_calls = []
        self.read_calls = []
        self.next_generation = 1000
        self.read_mutator = None

    def configuration(self):
        return self._configuration

    def create_if_absent(self, object_name, content):
        self.create_calls.append(object_name)
        if object_name in self.generations:
            raise OffHostTransportError("already-exists", "upload")
        generation = str(self.next_generation)
        self.next_generation += 1
        self.generations[object_name] = {generation: bytes(content)}
        return ObjectVersion(
            object_name,
            generation,
            len(content),
            hashlib.sha256(content).hexdigest(),
        )

    def add_later_generation(self, object_name, content):
        generation = str(self.next_generation)
        self.next_generation += 1
        self.generations[object_name][generation] = bytes(content)
        return generation

    def read_generation(self, object_name, generation):
        self.read_calls.append((object_name, generation))
        content = self.generations[object_name][generation]
        if self.read_mutator is not None:
            content = self.read_mutator(object_name, generation, content)
        return content


class OffHostDurabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.base = Path(self.temp.name)
        self.repository = self.base / "repository"
        self.repository.mkdir(mode=0o700)
        self.live = self.base / "live"
        self.live.mkdir(mode=0o700)
        self.ledger = self.live / "council.jsonl"
        self.ledger.write_bytes(b'{"kind":"capture-completion"}\n')
        os.chmod(self.ledger, 0o600)
        self.resolutions = self.live / "resolutions.jsonl"
        self.resolutions.write_bytes(b'{"outcome":"unresolved"}\n')
        os.chmod(self.resolutions, 0o600)
        self.controls = self.live / "controls"
        self.controls.mkdir(mode=0o700)
        (self.controls / "activation.jsonl").write_bytes(b"")
        os.chmod(self.controls / "activation.jsonl", 0o600)
        self.artifacts = self.live / "artifacts"
        (self.artifacts / "sha256" / "aa").mkdir(parents=True, mode=0o700)
        (self.artifacts / "sha256" / "aa" / "answer.bin").write_bytes(b"answer")
        os.chmod(self.artifacts / "sha256" / "aa" / "answer.bin", 0o600)
        self.lock = self.live / "evidence.lock"
        self.lock.touch(mode=0o600)

    def tearDown(self):
        for root, directories, files in os.walk(self.base, topdown=False):
            for name in files:
                try:
                    os.chmod(Path(root) / name, 0o600)
                except FileNotFoundError:
                    pass
            for name in directories:
                try:
                    os.chmod(Path(root) / name, 0o700)
                except FileNotFoundError:
                    pass
        self.temp.cleanup()

    def policy(self, **changes):
        values = {
            "target_uri": BUCKET_URI,
            "retention_seconds": 30 * 86400,
            "rpo_seconds": 86400,
            "rto_seconds": 3600,
            "max_snapshot_age_seconds": 86400,
            "max_restore_evidence_age_seconds": 7 * 86400,
            "encryption_access_posture": "provider-managed encryption at rest",
            "access_posture": ACCESS_POSTURE,
            "failure_domain_caveats": "same provider control plane remains a shared risk",
            "failure_domain_caveat_acknowledged": True,
            "automatic_application_deletion": False,
        }
        values.update(changes)
        return DurabilityPolicy(**values)

    def exporter(self, *, cut_at=NOW):
        def export(target):
            create_evidence_snapshot(
                ledger_path=self.ledger,
                resolution_store_path=self.resolutions,
                control_store_path=self.controls,
                artifact_root=self.artifacts,
                lock_path=self.lock,
                snapshot_target=target,
                repository_root=self.repository,
            )
            return snapshot_export_from_directory(target, cut_at=cut_at)

        return export

    def rehearse(self, store=None, exporter=None, policy=None, **changes):
        clock_counter = iter(range(20))
        monotonic_values = iter((100.0, 100.5))
        return run_offhost_durability_rehearsal(
            snapshot_exporter=exporter or self.exporter(),
            object_store=store or FakeVersionedStore(),
            policy=policy or self.policy(),
            runtime_commit=RUNTIME_COMMIT,
            source_tree_sha256=SOURCE_TREE_SHA256,
            activation_policy_sha256=ACTIVATION_POLICY_SHA256,
            workspace_parent=self.base,
            clock=lambda: NOW + timedelta(seconds=next(clock_counter)),
            monotonic=lambda: next(monotonic_values),
            prefix_factory=lambda: "snapshots/rehearsal-027",
            **changes,
        )

    def test_uploads_members_index_last_reads_exact_generations_and_restores(self):
        store = FakeVersionedStore()
        certificate = self.rehearse(store=store)
        document = certificate.document

        self.assertEqual(document["runtimeSourceCommit"], RUNTIME_COMMIT)
        self.assertEqual(document["runtimeSourceSha256"], SOURCE_TREE_SHA256)
        self.assertEqual(document["policySha256"], ACTIVATION_POLICY_SHA256)
        self.assertEqual(document["prefix"], "snapshots/rehearsal-027")
        self.assertEqual(store.create_calls[-1], "snapshots/rehearsal-027/index.json")
        self.assertEqual(
            len(document["remoteObjects"]),
            len(store.create_calls) - 1,
        )
        self.assertGreater(document["snapshot"]["bytes"], 0)
        self.assertEqual(
            document["upload"]["uploadedBytes"],
            document["readback"]["downloadedBytes"],
        )
        self.assertTrue(document["restore"]["restoredEvidenceValidated"])
        self.assertEqual(certificate.sha256, hashlib.sha256(certificate.canonical_bytes).hexdigest())
        self.assertEqual(json.loads(certificate.canonical_bytes), document)
        for record in document["remoteObjects"]:
            self.assertIn((record["name"], record["generation"]), store.read_calls)
        index = document["indexObject"]
        self.assertIn((index["name"], index["generation"]), store.read_calls)

        verified = verify_durability_certificate(
            certificate.canonical_bytes,
            expected_runtime_commit=RUNTIME_COMMIT,
            expected_source_tree_sha256=SOURCE_TREE_SHA256,
        )
        self.assertEqual(verified.sha256, certificate.sha256)

    def test_later_remote_generation_cannot_redirect_exact_readback(self):
        class LaterGenerationStore(FakeVersionedStore):
            def read_generation(inner_self, object_name, generation):
                if len(inner_self.generations[object_name]) == 1:
                    inner_self.add_later_generation(object_name, b"later unrelated bytes")
                return super(LaterGenerationStore, inner_self).read_generation(
                    object_name, generation
                )

        store = LaterGenerationStore()
        certificate = self.rehearse(store=store)
        for record in certificate.document["remoteObjects"]:
            self.assertNotEqual(
                record["generation"], max(store.generations[record["name"]], key=int)
            )

    def test_tampered_exact_generation_fails_before_certificate(self):
        store = FakeVersionedStore()

        def tamper(name, generation, content):
            if "/members/" in name and content:
                return content + b"tampered"
            return content

        store.read_mutator = tamper
        with self.assertRaises(OffHostIntegrityError) as caught:
            self.rehearse(store=store)
        self.assertEqual(caught.exception.code, "readback-mismatch")

    def test_tampered_index_fails_before_member_readback(self):
        store = FakeVersionedStore()

        def tamper(name, generation, content):
            if name.endswith("index.json"):
                return b"{}"
            return content

        store.read_mutator = tamper
        with self.assertRaises(OffHostIntegrityError) as caught:
            self.rehearse(store=store)
        self.assertEqual(caught.exception.code, "index-readback-mismatch")
        self.assertFalse(any("/members/" in name for name, _generation in store.read_calls))

    def test_provider_controls_and_frozen_policy_fail_closed_before_upload(self):
        base = FakeVersionedStore()._configuration
        cases = (
            ("versioning_enabled", False, "versioning-disabled"),
            ("public_access_prevention", "inherited", "public-access-prevention-not-enforced"),
            ("uniform_bucket_access", False, "uniform-access-disabled"),
            ("retention_seconds", 0, "invalid-policy"),
            ("encryption_at_rest", "unspecified", "insufficient-encryption"),
            ("automatic_application_deletion", True, "automatic-deletion-enabled"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                values = dict(base.__dict__)
                values[field] = value
                store = FakeVersionedStore(StorageConfiguration(**values))
                with self.assertRaises(OffHostDurabilityError) as caught:
                    self.rehearse(store=store)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(store.create_calls, [])

    def test_target_retention_and_access_mismatch_fail_before_snapshot(self):
        cases = (
            (self.policy(target_uri="gs://another-private-bucket"), "target-mismatch"),
            (self.policy(retention_seconds=365 * 86400), "retention-policy-mismatch"),
            (self.policy(access_posture="different identity"), "access-posture-mismatch"),
        )
        for policy, code in cases:
            with self.subTest(code=code):
                exported = False

                def should_not_export(_target):
                    nonlocal exported
                    exported = True
                    raise AssertionError("snapshot exporter should not run")

                store = FakeVersionedStore()
                with self.assertRaises(OffHostPolicyError) as caught:
                    self.rehearse(store=store, exporter=should_not_export, policy=policy)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(exported)
                self.assertEqual(store.create_calls, [])

    def test_future_stale_and_rto_violations_fail_closed(self):
        with self.assertRaises(OffHostPolicyError) as future:
            self.rehearse(exporter=self.exporter(cut_at=NOW + timedelta(minutes=1)))
        self.assertEqual(future.exception.code, "future-snapshot-cut")

        with self.assertRaises(OffHostPolicyError) as stale:
            self.rehearse(
                exporter=self.exporter(cut_at=NOW - timedelta(days=2)),
                policy=self.policy(max_snapshot_age_seconds=86400),
            )
        self.assertEqual(stale.exception.code, "snapshot-too-old")

        clock_counter = iter(range(20))
        monotonic_values = iter((1.0, 3.1))
        with self.assertRaises(OffHostPolicyError) as rto:
            run_offhost_durability_rehearsal(
                snapshot_exporter=self.exporter(),
                object_store=FakeVersionedStore(),
                policy=self.policy(rto_seconds=1),
                runtime_commit=RUNTIME_COMMIT,
                source_tree_sha256=SOURCE_TREE_SHA256,
                activation_policy_sha256=ACTIVATION_POLICY_SHA256,
                workspace_parent=self.base,
                clock=lambda: NOW + timedelta(seconds=next(clock_counter)),
                monotonic=lambda: next(monotonic_values),
                prefix_factory=lambda: "snapshots/rto-test",
            )
        self.assertEqual(rto.exception.code, "rto-exceeded")

    def test_invalid_member_graph_and_restore_validator_fail_closed(self):
        real_exporter = self.exporter()

        def invalid_exporter(target):
            exported = real_exporter(target)
            return SnapshotExport(
                exported.verification,
                exported.members + (SnapshotMember("missing/child", "file", 0o600, b"x"),),
                exported.cut_at,
            )

        with self.assertRaises(OffHostIntegrityError) as invalid:
            self.rehearse(exporter=invalid_exporter)
        self.assertIn(invalid.exception.code, {"invalid-member-order", "missing-member-parent"})

        with self.assertRaises(OffHostIntegrityError) as restored:
            self.rehearse(
                restored_state_validator=lambda _path: {
                    "sourceMembershipMatches": False
                }
            )
        self.assertEqual(restored.exception.code, "restored-state-invalid")

    def test_certificate_verifier_rejects_duplicate_noncanonical_and_wrong_binding(self):
        certificate = self.rehearse()
        with self.assertRaises(OffHostIntegrityError) as wrong_commit:
            verify_durability_certificate(
                certificate.canonical_bytes,
                expected_runtime_commit="c" * 40,
                expected_source_tree_sha256=SOURCE_TREE_SHA256,
            )
        self.assertEqual(wrong_commit.exception.code, "runtime-commit-mismatch")

        with self.assertRaises(OffHostIntegrityError) as noncanonical:
            verify_durability_certificate(
                json.dumps(json.loads(certificate.canonical_bytes), indent=2).encode(),
                expected_runtime_commit=RUNTIME_COMMIT,
                expected_source_tree_sha256=SOURCE_TREE_SHA256,
            )
        self.assertEqual(noncanonical.exception.code, "noncanonical-certificate")

        duplicate = b'{"schemaVersion":1,"schemaVersion":1}'
        with self.assertRaises(OffHostIntegrityError) as duplicated:
            verify_durability_certificate(
                duplicate,
                expected_runtime_commit=RUNTIME_COMMIT,
                expected_source_tree_sha256=SOURCE_TREE_SHA256,
            )
        self.assertEqual(duplicated.exception.code, "invalid-certificate")

    def test_certificate_is_exactly_accepted_by_activation_evaluator_schema(self):
        certificate = self.rehearse()
        normalized = activation_evidence._validate_durability_certificate(
            certificate.document,
            commit=RUNTIME_COMMIT,
            source_sha=SOURCE_TREE_SHA256,
            policy={
                "retentionDays": 30,
                "rtoSeconds": 3600,
            },
            policy_sha=ACTIVATION_POLICY_SHA256,
        )
        self.assertEqual(normalized["snapshotAt"], NOW)
        self.assertEqual(normalized["elapsedSeconds"], 5.0)


class FakeGcloudRunner:
    def __init__(self, *, bucket_metadata=None):
        self.bucket_metadata = bucket_metadata or {
            "versioning": {"enabled": True},
            "iamConfiguration": {
                "publicAccessPrevention": "enforced",
                "uniformBucketLevelAccess": {"enabled": True},
            },
            "retentionPolicy": {"retentionPeriod": "7776000", "isLocked": False},
            "encryption": {"defaultKmsKeyName": "projects/hidden/keys/never-record-this"},
            "location": "US-EAST4",
            "locationType": "region",
        }
        self.calls = []
        self.objects = {}
        self.generation = 2000
        self.failure = None

    def __call__(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        if self.failure is not None:
            return subprocess.CompletedProcess(
                arguments,
                1,
                stdout=b"credential ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
                stderr=self.failure,
            )
        if arguments[2:4] == ["buckets", "describe"]:
            return subprocess.CompletedProcess(
                arguments, 0, stdout=json.dumps(self.bucket_metadata).encode(), stderr=b""
            )
        if arguments[2] == "cp":
            content = kwargs["input"]
            uri = arguments[4]
            generation = str(self.generation)
            self.generation += 1
            self.objects[(uri, generation)] = content
            return subprocess.CompletedProcess(
                arguments, 0, stdout=f"Created: {uri}#{generation}\n".encode(), stderr=b""
            )
        if arguments[2] == "cat":
            versioned_uri = arguments[3]
            uri, generation = versioned_uri.rsplit("#", 1)
            return subprocess.CompletedProcess(
                arguments, 0, stdout=self.objects[(uri, generation)], stderr=b""
            )
        raise AssertionError(arguments)


class GcsDurabilityAdapterTest(unittest.TestCase):
    def store(self, runner):
        return GcsVersionedObjectStore(
            bucket="council-evidence-test",
            access_posture=ACCESS_POSTURE,
            runner=runner,
        )

    def test_configuration_requires_and_records_controls_without_key_identity(self):
        runner = FakeGcloudRunner()
        configuration = self.store(runner).configuration()
        document = configuration.document()
        self.assertEqual(document["provider"], "gcs")
        self.assertEqual(document["encryptionAtRest"], "customer-managed")
        self.assertEqual(document["failureDomain"], "gcs:region:US-EAST4")
        self.assertTrue(document["versioningEnabled"])
        self.assertEqual(document["publicAccessPrevention"], "enforced")
        self.assertTrue(document["uniformBucketAccess"])
        self.assertNotIn("never-record-this", json.dumps(document))
        self.assertEqual(runner.calls[0][0][0:4], ["gcloud", "storage", "buckets", "describe"])

    def test_create_is_conditional_and_read_is_generation_pinned(self):
        runner = FakeGcloudRunner()
        store = self.store(runner)
        content = b"manifested evidence bytes"
        version = store.create_if_absent("snapshots/run/member-0001", content)
        self.assertEqual(version.generation, "2000")
        upload_args, upload_kwargs = runner.calls[0]
        self.assertEqual(upload_args[0:5], [
            "gcloud",
            "storage",
            "cp",
            "-",
            "gs://council-evidence-test/snapshots/run/member-0001",
        ])
        self.assertIn("--if-generation-match=0", upload_args)
        self.assertIn("--print-created-message", upload_args)
        self.assertEqual(upload_kwargs["input"], content)
        self.assertEqual(store.read_generation(version.object_name, version.generation), content)
        read_args = runner.calls[1][0]
        self.assertEqual(
            read_args[3],
            "gs://council-evidence-test/snapshots/run/member-0001#2000",
        )

    def test_provider_failure_is_nonreflective(self):
        runner = FakeGcloudRunner()
        runner.failure = b"AWS_SECRET_ACCESS_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        with self.assertRaises(OffHostTransportError) as caught:
            self.store(runner).configuration()
        rendered = str(caught.exception)
        self.assertEqual(caught.exception.code, "command-failed")
        self.assertNotIn("AWS_SECRET", rendered)
        self.assertNotIn("ghp_", rendered)
        self.assertNotIn("AAAAAAAA", rendered)

    def test_missing_created_generation_and_unsafe_names_fail_closed(self):
        class MissingGeneration(FakeGcloudRunner):
            def __call__(self, arguments, **kwargs):
                result = super().__call__(arguments, **kwargs)
                if arguments[2] == "cp":
                    return subprocess.CompletedProcess(arguments, 0, stdout=b"uploaded", stderr=b"")
                return result

        store = self.store(MissingGeneration())
        with self.assertRaises(OffHostTransportError) as missing:
            store.create_if_absent("snapshots/run/member", b"x")
        self.assertEqual(missing.exception.code, "missing-created-generation")
        with self.assertRaises(OffHostTransportError):
            store.create_if_absent("snapshots/../escape", b"x")
        with self.assertRaises(OffHostTransportError):
            store.read_generation("snapshots/run/member", "latest")

    def test_inadequate_bucket_metadata_is_rejected(self):
        base = FakeGcloudRunner().bucket_metadata
        cases = (
            ({**base, "versioning": {"enabled": False}}, "versioning-disabled"),
            (
                {
                    **base,
                    "iamConfiguration": {
                        **base["iamConfiguration"],
                        "publicAccessPrevention": "inherited",
                    },
                },
                "public-access-prevention-not-enforced",
            ),
            (
                {
                    **base,
                    "iamConfiguration": {
                        **base["iamConfiguration"],
                        "uniformBucketLevelAccess": {"enabled": False},
                    },
                },
                "uniform-access-disabled",
            ),
            ({**base, "retentionPolicy": {}}, "invalid-retention"),
        )
        for metadata, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(OffHostPolicyError) as caught:
                    self.store(FakeGcloudRunner(bucket_metadata=metadata)).configuration()
                self.assertEqual(caught.exception.code, code)

    def test_constructor_rejects_bucket_and_delete_capability(self):
        with self.assertRaises(OffHostPolicyError):
            GcsVersionedObjectStore(
                bucket="gs://not-a-bucket",
                access_posture=ACCESS_POSTURE,
                runner=FakeGcloudRunner(),
            )
        with self.assertRaises(OffHostPolicyError) as deletion:
            GcsVersionedObjectStore(
                bucket="council-evidence-test",
                access_posture=ACCESS_POSTURE,
                automatic_application_deletion=True,
                runner=FakeGcloudRunner(),
            )
        self.assertEqual(deletion.exception.code, "automatic-deletion-enabled")


if __name__ == "__main__":
    unittest.main()
