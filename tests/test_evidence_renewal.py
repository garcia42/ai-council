import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from council_tools import cli, evidence_backup
from council_tools.activation_evidence import AUDIT_CONTROL_KEY
from council_tools.artifacts import ArtifactStore
from council_tools.capture_runtime import (
    CaptureRuntimeError, append_capture_evidence_renewal,
    append_evidence_bound_capture_activation, capture_report,
)
from council_tools.capture_schema import CaptureSchemaError, validate_v2_ledger
from council_tools.evidence_backup import SnapshotIntegrityError, create_evidence_snapshot, restore_evidence_snapshot
from council_tools.finding_audit import rehearse_audit_protocol
from council_tools.forecasts import audit
from tests.test_activation_evidence import EvidenceFixture, COMMIT, SOURCE_SHA, _json


ACTIVATION_ID = "activation-" + "1" * 32


def shifted(value, days):
    if isinstance(value, dict):
        return {k: shifted(v, days) for k, v in value.items()}
    if isinstance(value, list):
        return [shifted(v, days) for v in value]
    if isinstance(value, str) and value.startswith("2026-08-") and "T" in value:
        return (datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return value


class EvidenceRenewalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "ledger.jsonl"
        self.events = self.root / "events.jsonl"
        self.events.write_bytes(b"")
        self.lock = self.root / "evidence.lock"
        self.store = ArtifactStore(self.root / "artifacts")
        self.fixture = EvidenceFixture()
        self.fixture.rebuild()
        for data in self.fixture.reader.content.values():
            self.store.capture(data)
        self.original = copy.deepcopy(self.fixture.manifest)
        self.original["activationId"] = ACTIVATION_ID
        self.original_bytes = _json(self.original)
        self.original_ref = self.store.capture(self.original_bytes)
        self.activation, _ = append_evidence_bound_capture_activation(
            self.log, {"cohortName": "test", "captureVersion": "capture-v2.0.0",
                       "runtimeSourceCommit": COMMIT, "runtimeSourceSha256": SOURCE_SHA,
                       "artifactRootPolicy": "private-content-addressed-v1",
                       "approvalManifest": self.original_ref},
            manifest_data=self.original_bytes, artifact_store=self.store,
            expected_runtime_commit=COMMIT, expected_source_sha256=SOURCE_SHA,
            clock=lambda: "2026-08-23T12:00:00Z", coordination_lock=self.lock)
        self.original_ledger = self.log.read_bytes()

    def renewal(self, days=1, policy_change=None):
        policy = shifted(self.fixture.activation_policy, days)
        policy["policyId"] += f"-renewal-{days}"
        if policy_change:
            policy_change(policy)
        policy_ref = self.store.capture(_json(policy))
        audit = rehearse_audit_protocol(self.fixture.audit_protocol,
            runtime_commit=COMMIT, source_tree_sha256=SOURCE_SHA,
            rehearsed_at=shifted("2026-08-23T10:00:00Z", days))
        durability = shifted(self.fixture.durability, days)
        durability["policySha256"] = policy_ref["sha256"]
        audit_ref = self.store.capture(_json(audit))
        durability_ref = self.store.capture(_json(durability))
        manifest = shifted(self.original, days)
        manifest["policyRef"] = policy_ref
        manifest["controls"] = {k: audit_ref if k == AUDIT_CONTROL_KEY else durability_ref
                                for k in manifest["controls"]}
        data = _json(manifest)
        ref = self.store.capture(data)
        previous = json.loads(self.log.read_text().splitlines()[-1])["approvalManifest"]["sha256"]
        spec = {"activationId": ACTIVATION_ID, "previousManifestSha256": previous,
                "approvalManifest": ref, "operator": "test-operator", "evidenceRef": "test-approval"}
        return spec, data

    def append(self, spec, data, when="2026-08-24T12:00:00Z", **kwargs):
        return append_capture_evidence_renewal(self.log, spec, manifest_data=data,
            artifact_store=self.store, expected_runtime_commit=kwargs.get("commit", COMMIT),
            expected_source_sha256=SOURCE_SHA, clock=lambda: when, coordination_lock=self.lock)

    def report(self, when):
        return capture_report(self.log, self.events, artifact_store=self.store, as_of=when)

    def test_renewal_restores_current_health_preserving_original_and_denominators(self):
        before = self.report("2026-08-24T12:00:00Z")
        v1_before = audit(self.log, self.events, as_of="2026-08-24T12:01:00Z")
        self.assertFalse(before["activationReadiness"]["currentlyHealthy"])
        spec, data = self.renewal()
        row, evidence = self.append(spec, data)
        self.assertTrue(evidence["activationVerdict"]["ready"])
        self.assertTrue(evidence["currentHealth"]["healthy"])
        self.assertTrue(self.log.read_bytes().startswith(self.original_ledger))
        after = self.report("2026-08-24T12:01:00Z")
        self.assertTrue(after["activationReadiness"]["currentlyHealthy"])
        for key in ("cohort", "descriptiveForecastAccuracy", "prospectiveAudit"):
            self.assertEqual(before[key], after[key])
        v1_after = audit(self.log, self.events, as_of="2026-08-24T12:01:00Z")
        # Atomic replacement may retain transaction escrow. Operational custody
        # metadata changes, but the entire forecast/scoring report is stable.
        for key in v1_before.keys() - {'transactionEscrows'}:
            self.assertEqual(v1_before[key], v1_after[key], key)
        self.assertEqual(row["activationId"], self.activation["activationId"])
        self.assertFalse(self.report("2026-08-25T12:00:00Z")["activationReadiness"]["currentlyHealthy"])
        second, second_data = self.renewal(2)
        self.append(second, second_data, "2026-08-25T12:00:00Z")
        self.assertTrue(self.report("2026-08-25T12:01:00Z")["activationReadiness"]["currentlyHealthy"])

    def test_weakening_or_changing_frozen_policy_is_rejected_without_append(self):
        for change in [lambda p: p.update(maxCertificateAgeSeconds=172800),
                       lambda p: p.update(maxClockSkewSeconds=301),
                       lambda p: p.update(expiresAt="2026-08-30T12:00:00Z"),
                       lambda p: p.update(durabilityPolicyRef=p["auditProtocolRef"])]:
            with self.subTest(change=change):
                spec, data = self.renewal(policy_change=change)
                with self.assertRaises(CaptureRuntimeError):
                    self.append(spec, data)
                self.assertEqual(self.original_ledger, self.log.read_bytes())

    def test_wrong_predecessor_source_manifest_and_operator_time_refused(self):
        spec, data = self.renewal()
        for patch in [{"previousManifestSha256": "0"*64}, {"activationId": "activation-"+"2"*32},
                      {"renewedAt": "2026-08-24T12:00:00Z"}]:
            with self.subTest(patch=patch), self.assertRaises((CaptureRuntimeError, CaptureSchemaError)):
                self.append({**spec, **patch}, data)
            self.assertEqual(self.original_ledger, self.log.read_bytes())
        for kwargs in [{"commit": "c"*40}, {"when": "2026-08-23T12:01:00Z"},
                       {"when": "2026-08-26T12:00:00Z"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(CaptureRuntimeError):
                self.append(spec, data, **kwargs)
        with self.assertRaises(CaptureRuntimeError):
            self.append(spec, data+b" ")
        self.assertEqual(self.original_ledger, self.log.read_bytes())

    def test_manifest_window_cannot_expand_and_activation_cannot_change(self):
        spec, data = self.renewal()
        for change in [{"expiresAt":"2026-08-30T12:00:00Z"},
                       {"activationId":"activation-"+"3"*32}, {"runtimeSourceCommit":"c"*40}]:
            manifest = {**json.loads(data), **change}
            altered = _json(manifest)
            with self.subTest(change=change), self.assertRaises(CaptureRuntimeError):
                self.append({**spec, "approvalManifest": self.store.capture(altered)}, altered)
        self.assertEqual(self.original_ledger, self.log.read_bytes())

    def test_replay_conflicting_predecessor_and_duplicate_id_rejected(self):
        spec, data = self.renewal()
        row, _ = self.append(spec, data)
        accepted = self.log.read_bytes()
        with self.assertRaises(CaptureSchemaError):
            self.append(spec, data, "2026-08-24T12:01:00Z")
        second, second_data = self.renewal(2)
        with self.assertRaises(CaptureSchemaError):
            self.append({**second, "previousManifestSha256": self.original_ref["sha256"]}, second_data,
                        "2026-08-25T12:00:00Z")
        self.assertEqual(accepted, self.log.read_bytes())
        with self.assertRaises(CaptureSchemaError):
            validate_v2_ledger([self.activation, row, row])

    def test_missing_accepted_artifact_and_malformed_chain_do_not_fall_back(self):
        spec, data = self.renewal()
        row, _ = self.append(spec, data)
        (self.store.root / spec["approvalManifest"]["path"]).unlink()
        report = self.report("2026-08-24T12:01:00Z")
        self.assertFalse(report["activationReadiness"]["currentlyHealthy"])
        self.assertTrue(report["activationReadiness"]["historicallyReady"])
        self.store.capture(data)
        for patch in [{"previousManifestSha256":"0"*64}, {"renewedAt":"2026-09-01T12:00:00Z"}]:
            self.log.write_bytes(self.original_ledger + _json({**row, **patch}) + b"\n")
            # A malformed non-run boundary makes the report refuse entirely;
            # it cannot become a partial report that skips the bad renewal.
            with self.assertRaises(CaptureSchemaError):
                self.report("2026-08-24T12:01:00Z")

    def test_snapshot_restore_retains_renewal_and_current_health(self):
        spec, data = self.renewal()
        self.append(spec, data)
        controls = self.root / "controls"
        controls.mkdir(mode=0o700)
        repository = self.root / "repository"
        repository.mkdir()
        create_evidence_snapshot(ledger_path=self.log, resolution_store_path=self.events,
            control_store_path=controls, artifact_root=self.store.root, lock_path=self.lock,
            snapshot_target=self.root/'snapshot', repository_root=repository)
        restore_evidence_snapshot(self.root/'snapshot', self.root/'restore', repository_root=repository)
        self.assertEqual(self.log.read_bytes(), (self.root/'restore/ledger').read_bytes())
        restored = capture_report(self.root/'restore/ledger', self.root/'restore/resolution-store',
            artifact_store=ArtifactStore(self.root/'restore/artifact-root'), as_of="2026-08-24T12:01:00Z")
        self.assertTrue(restored['activationReadiness']['currentlyHealthy'])

    def test_snapshot_waits_for_whole_renewal_transaction(self):
        spec, data = self.renewal()
        entered = threading.Event()
        release = threading.Event()
        snapshot_started = threading.Event()
        read = self.store.read_verified

        def pause(ref):
            if ref == spec['approvalManifest'] and not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test did not release renewal')
            return read(ref)

        controls = self.root/'controls'
        controls.mkdir(mode=0o700)
        repository = self.root/'repository'
        repository.mkdir()

        original_shared_lock = evidence_backup._shared_lock

        @contextmanager
        def observed_lock(path):
            snapshot_started.set()  # Policy metadata is already captured.
            with original_shared_lock(path) as held:
                yield held

        def snapshot(target='snapshot'):
            return create_evidence_snapshot(ledger_path=self.log, resolution_store_path=self.events,
                control_store_path=controls, artifact_root=self.store.root, lock_path=self.lock,
                snapshot_target=self.root/target, repository_root=repository)

        with mock.patch.object(self.store, 'read_verified', side_effect=pause), \
                mock.patch.object(evidence_backup, '_shared_lock', side_effect=observed_lock), ThreadPoolExecutor(2) as pool:
            renewal = pool.submit(self.append, spec, data)
            try:
                self.assertTrue(entered.wait(5))
                backup = pool.submit(snapshot)
                self.assertTrue(snapshot_started.wait(5))
                self.assertFalse(backup.done())
            finally:
                release.set()
            renewal.result(timeout=5)
            with self.assertRaisesRegex(SnapshotIntegrityError, 'source-changed'):
                backup.result(timeout=5)
        self.assertFalse((self.root/'snapshot').exists())
        snapshot('snapshot-after-renewal')
        restore_evidence_snapshot(self.root/'snapshot-after-renewal', self.root/'restore', repository_root=repository)
        self.assertEqual(self.log.read_bytes(), (self.root/'restore/ledger').read_bytes())

    def test_cli_renewal_uses_real_process_and_explicit_artifact_path(self):
        # Current-time fixture supports the CLI's system-owned timestamp. This
        # is synthetic evidence, and the ledger and artifact roots are private.
        days = (datetime.now(timezone.utc) - datetime(2026, 8, 23, 12, tzinfo=timezone.utc)).total_seconds()/86400
        spec, data = self.renewal(days)
        spec_path, manifest_path = self.root/'spec.json', self.root/'manifest.json'
        spec_path.write_bytes(_json(spec))
        manifest_path.write_bytes(data)
        result = subprocess.run([sys.executable, '-B', '-m', 'council_tools.cli',
            'capture-renew-evidence','--log',str(self.log),'--spec',str(spec_path),
            '--approval-manifest-file',str(manifest_path),'--artifact-root',str(self.store.root),
            '--coordination-lock',str(self.lock),'--runtime-source-commit',COMMIT,
            '--runtime-source-sha256',SOURCE_SHA], capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1]/'src')}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['activationEvidence']['currentHealth']['healthy'])

    def test_live_cli_without_installed_binding_is_refused_before_append(self):
        spec, data = self.renewal()
        spec_path = self.root/'spec.json'
        spec_path.write_bytes(_json(spec))
        manifest_path = self.root/'manifest.json'
        manifest_path.write_bytes(data)
        argv = ['council', 'capture-renew-evidence','--log',str(self.log),'--spec',str(spec_path),
                '--approval-manifest-file',str(manifest_path),'--artifact-root',str(self.store.root),
                '--coordination-lock',str(self.lock)]
        with mock.patch.object(cli, '_is_live_write_path', return_value=True), \
                mock.patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            result = cli.main()
        self.assertEqual(result, 1)
        self.assertEqual(self.original_ledger, self.log.read_bytes())
