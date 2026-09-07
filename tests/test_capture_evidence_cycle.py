import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operations import capture_evidence_cycle as ops
from council_tools.activation_evidence import evaluate_activation_evidence
from council_tools.artifacts import ArtifactStore
from council_tools.capture_runtime import append_evidence_bound_capture_activation, append_capture_evidence_renewal, capture_report
from council_tools.offhost_durability import StorageConfiguration
from tests.test_offhost_durability import FakeVersionedStore


class EvidenceCycleTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/var/tmp'); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config={'root':str(self.root),'maxCycles':3,'expiresAt':'2099-01-01T00:00:00Z',
            'maxCalls':20,'maxObjects':512,'maxBytes':67108864,'maxSeconds':1800,
            'runtimeRoot':str(Path(__file__).parents[1]),'runtimeCommit':'a'*40,'runtimeSha256':'b'*64,
            'bucket':'council-evidence-test','operator':'test','authority':'test-only'}
        for name in ['log','events','v1Events','evidenceLock']:
            path=self.root/(name+'.jsonl'); path.write_bytes(b''); path.chmod(0o600); self.config[name]=str(path)
        self.configuration=StorageConfiguration(provider='gcs',bucket_uri='gs://council-evidence-test',
            failure_domain='gcs:region:us-east4',versioning_enabled=True,public_access_prevention='enforced',
            uniform_bucket_access=True,retention_seconds=30*86400,retention_locked=False,
            encryption_at_rest='provider-managed',access_posture='service-account access through project IAM; public-access prevention enforced; uniform bucket-level access enabled',automatic_application_deletion=False)

    def invoke(self, config, *args):
        store=ArtifactStore(self.root/'artifacts')
        if args[0]=='activation-readiness':
            data=Path(args[args.index('--manifest-file')+1]).read_bytes()
            result=evaluate_activation_evidence(data,reader=store,expected_runtime_commit=config['runtimeCommit'],
                expected_source_sha256=config['runtimeSha256'],activation_time=ops.now(),as_of=ops.now())
            self.assertTrue(result['appendReady'],result)
            return result
        self.assertEqual(args[0],'capture-renew-evidence')
        spec=json.loads(Path(args[args.index('--spec')+1]).read_text())
        data=Path(args[args.index('--approval-manifest-file')+1]).read_bytes()
        row,evidence=append_capture_evidence_renewal(config['log'],spec,manifest_data=data,artifact_store=store,
            expected_runtime_commit=config['runtimeCommit'],expected_source_sha256=config['runtimeSha256'],coordination_lock=config['evidenceLock'])
        return {'renewalId':row['renewalId'],'activationEvidence':evidence}

    def test_prepare_and_renew_use_real_snapshot_restore_and_preserve_activation(self):
        with mock.patch('council_tools.gcs_durability.GcsVersionedObjectStore',side_effect=lambda **kwargs:FakeVersionedStore(self.configuration)),mock.patch.object(ops,'wrapper',side_effect=self.invoke):
            ops.cycle(self.config,'prepare')
            first=next((self.root/'cycles').iterdir())
            spec=json.loads((first/'activation-spec.json').read_text())
            row,_=append_evidence_bound_capture_activation(self.config['log'],spec,manifest_data=(first/'manifest.json').read_bytes(),
                artifact_store=ArtifactStore(self.root/'artifacts'),expected_runtime_commit='a'*40,expected_source_sha256='b'*64,
                coordination_lock=self.config['evidenceLock'])
            original=Path(self.config['log']).read_bytes()
            ops.save(first/'complete.json',{'activationId':row['activationId']})
            ops.cycle(self.config,'renew')
            self.assertTrue(Path(self.config['log']).read_bytes().startswith(original))
            report=capture_report(self.config['log'],self.config['events'],artifact_store=ArtifactStore(self.root/'artifacts'),as_of=ops.now())
            self.assertTrue(report['activationReadiness']['currentlyHealthy'])
            self.assertEqual(len(Path(self.config['log']).read_text().splitlines()),2)

    def test_incomplete_cycle_is_not_replayed(self):
        path=self.root/'cycles'/'failed'; path.mkdir(parents=True)
        with self.assertRaisesRegex(ops.CycleError,'reconciliation'):
            ops.check_previous(self.root,self.config)

    def test_unready_prepare_retains_failure_without_prepared_marker(self):
        with mock.patch('council_tools.gcs_durability.GcsVersionedObjectStore',side_effect=lambda **kwargs:FakeVersionedStore(self.configuration)),mock.patch.object(ops,'wrapper',return_value={'appendReady':False}):
            with self.assertRaisesRegex(ops.CycleError,'readiness refused'):
                ops.cycle(self.config,'prepare')
        first=next((self.root/'cycles').iterdir())
        self.assertTrue((first/'readiness.json').exists())
        self.assertFalse((first/'prepared.json').exists())
        self.assertFalse((first/'complete.json').exists())

    def test_expiry_and_cycle_count_refuse_before_work(self):
        with self.assertRaisesRegex(ops.CycleError,'expired'):
            ops.check_previous(self.root,{**self.config,'expiresAt':'2000-01-01T00:00:00Z'})
        (self.root/'cycles'/'done').mkdir(parents=True); ops.save(self.root/'cycles'/'done'/'complete.json',{})
        with self.assertRaisesRegex(ops.CycleError,'allowance'):
            ops.check_previous(self.root,{**self.config,'maxCycles':1})

    def test_runner_bounds_bytes_calls_and_disables_storage_retries(self):
        run=mock.Mock(return_value=subprocess.CompletedProcess([],0,b'ok',b''))
        runner=ops.BudgetRunner({**self.config,'maxCalls':1,'maxBytes':3},self.root,run)
        with self.assertRaisesRegex(ops.CycleError,'upload budget'):
            runner(['gcloud','storage','cp','-','gs://b/a'],input=b'1234')
        run.assert_not_called()
        runner(['gcloud','storage','cp','-','gs://b/a'],input=b'123')
        self.assertEqual(run.call_args.kwargs['env']['CLOUDSDK_STORAGE_MAX_RETRIES'],'0')
        self.assertLessEqual(run.call_args.kwargs['timeout'],60)
        with self.assertRaisesRegex(ops.CycleError,'execution budget'):
            runner(['gcloud','storage','cat','gs://b/a#1'])
        self.assertEqual(run.call_count,1)

    def test_timeout_retains_attempt_without_invented_result_or_retry(self):
        run=mock.Mock(side_effect=subprocess.TimeoutExpired('gcloud',60))
        runner=ops.BudgetRunner(self.config,self.root,run)
        with self.assertRaises(subprocess.TimeoutExpired):
            runner(['gcloud','storage','cp','-','gs://b/a'],input=b'1')
        self.assertTrue((self.root/'command-0001-attempt.json').exists())
        self.assertFalse((self.root/'command-0001-result.json').exists())
        self.assertEqual(run.call_count,1)
