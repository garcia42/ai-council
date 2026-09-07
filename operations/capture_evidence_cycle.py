#!/usr/bin/env python3
"""Bounded operator for the existing pinned evidence pipeline; no ledger edits."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def save(path, value):
    data = value if isinstance(value, bytes) else canonical(value)
    with Path(path).open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


class CycleError(ValueError):
    pass


class BudgetRunner:
    def __init__(self, config, directory, run=subprocess.run):
        self.config, self.directory, self.run = config, directory, run
        self.started = time.monotonic()
        self.calls = self.uploaded = self.downloaded = self.objects = 0
        self.sizes = {}

    def __call__(self, argv, **kwargs):
        if self.calls >= self.config['maxCalls'] or time.monotonic()-self.started >= self.config['maxSeconds']:
            raise CycleError('execution budget exhausted')
        content = kwargs.get('input')
        if content is not None:
            if self.objects+1 > self.config['maxObjects'] or self.uploaded+len(content) > self.config['maxBytes']:
                raise CycleError('upload budget exhausted')
            self.objects += 1
            self.uploaded += len(content)
            self.sizes[argv[4]] = len(content)
        if argv[1:3] == ['storage', 'cat']:
            uri = argv[3].split('#')[0]
            if uri not in self.sizes or self.downloaded+self.sizes[uri] > self.config['maxBytes']:
                raise CycleError('readback budget or identity mismatch')
            self.downloaded += self.sizes[uri]
        self.calls += 1
        receipt = {'commandNumber':self.calls,'argv':argv,'inputBytes':len(content or b''),
                   'startedAt':stamp(now()),'state':'ATTEMPTED'}
        save(self.directory/f'command-{self.calls:04d}-attempt.json',receipt)
        result = self.run(argv, **kwargs, timeout=min(60, max(1,self.config['maxSeconds']-(time.monotonic()-self.started))),
            env={**os.environ,'CLOUDSDK_STORAGE_MAX_RETRIES':'0','CLOUDSDK_CORE_HTTP_TIMEOUT':'30',
                 'CLOUDSDK_CORE_DISABLE_PROMPTS':'1'})
        save(self.directory/f'command-{self.calls:04d}-result.json',{
            'exitCode':result.returncode,'stdoutSha256':hashlib.sha256(result.stdout).hexdigest(),
            'stderrSha256':hashlib.sha256(result.stderr).hexdigest(),'finishedAt':stamp(now())})
        if result.returncode == 0 and argv[1:4] == ['storage', 'buckets', 'describe']:
            metadata = json.loads(result.stdout)
            storage_class = metadata.get('default_storage_class', metadata.get('storageClass'))
            if storage_class != 'STANDARD' or metadata.get('location') != 'US':
                raise CycleError('bucket differs from bounded cost assumptions')
        return result


def check_previous(root, config):
    directories = sorted((root/'cycles').glob('*')) if (root/'cycles').exists() else []
    if len(directories) >= config['maxCycles']:
        raise CycleError('cycle allowance exhausted')
    if now() >= datetime.fromisoformat(config['expiresAt'].replace('Z','+00:00')):
        raise CycleError('maintenance authorization expired')
    if any(not (path/'complete.json').is_file() for path in directories):
        raise CycleError('previous cycle requires reconciliation; no automatic replay')


def wrapper(config, *args):
    p = subprocess.run([sys.executable,'-B',config['wrapper'],*args],capture_output=True,timeout=120)
    if p.returncode:
        raise CycleError('installed wrapper refused '+args[0]+' (exit '+str(p.returncode)+')')
    return json.loads(p.stdout)


def load_runtime(config):
    root = Path(config['runtimeRoot'])
    commit = subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
    if commit != config['runtimeCommit']:
        raise CycleError('runtime commit changed')
    digest = hashlib.sha256()
    for path in sorted((root/'src/council_tools').rglob('*.py')):
        digest.update(str(path.relative_to(root)).encode()+b'\0'+path.read_bytes()+b'\0')
    if digest.hexdigest() != config['runtimeSha256']:
        raise CycleError('runtime source changed')
    wrapper(config,'report','--json')
    sys.path.insert(0,str(root/'src'))


def policy_from_document(document):
    from council_tools.offhost_durability import DurabilityPolicy
    fields = {re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower(): value for key, value in document.items()}
    result = DurabilityPolicy(**fields)
    if result.document() != document:
        raise CycleError('durability policy round-trip mismatch')
    return result


def cycle(config, mode):
    from council_tools.artifacts import ArtifactStore
    from council_tools.activation_evidence import AUDIT_CONTROL_KEY, CONTROL_KEYS
    from council_tools.evidence_backup import create_evidence_snapshot
    from council_tools.finding_audit import make_audit_protocol, rehearse_audit_protocol
    from council_tools.forecasts import evidence_write_lock
    from council_tools.gcs_durability import GcsVersionedObjectStore
    from council_tools.offhost_durability import DurabilityPolicy, descriptor_custody_snapshot_exporter, run_offhost_durability_rehearsal

    root = Path(config['root'])
    check_previous(root, config)
    run = root/'cycles'/('cycle-'+now().strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex)
    run.mkdir(parents=True,mode=0o700)
    save(run/'started.json',{'mode':mode,'at':stamp(now()),'authority':config['authority']})
    store = ArtifactStore(root/'artifacts')
    def capture(value):
        data = value if isinstance(value,bytes) else canonical(value)
        with evidence_write_lock(config['evidenceLock']):
            return store.capture(data)
    with Path(config['evidenceLock']).open('rb') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        rows = [json.loads(line) for line in Path(config['log']).read_text().splitlines() if line.strip()]
    activations = [row for row in rows if row.get('kind')=='capture-activation']
    if mode=='prepare':
        if activations:
            raise CycleError('activation already exists')
        protocol = make_audit_protocol(frozen_protocol_artifact=capture(
            (Path(config['runtimeRoot'])/'design/2026-08-23-council-activation-controls-amendment-027.md').read_bytes()),selection_residue=2)
        protocol_ref = capture(protocol)
        policy = DurabilityPolicy(target_uri='gs://'+config['bucket'],retention_seconds=30*86400,
            rpo_seconds=86400,rto_seconds=3600,max_snapshot_age_seconds=86400,max_restore_evidence_age_seconds=86400,
            encryption_access_posture='Google-managed encryption at rest with access controlled by GCP IAM',
            access_posture='service-account access through project IAM; public-access prevention enforced; uniform bucket-level access enabled',
            failure_domain_caveats='Bucket shares stocks-451219 project; separate-project isolation, locked retention, CMEK, and narrower project IAM remain P2 hardening.',
            failure_domain_caveat_acknowledged=True,automatic_application_deletion=False)
        durability_ref = capture(policy.canonical_bytes())
        issued = now()
        activation_policy = {'schemaVersion':1,'kind':'activation-evidence-policy','policyId':'policy-'+uuid.uuid4().hex,
            'runtimeSourceCommit':config['runtimeCommit'],'runtimeSourceSha256':config['runtimeSha256'],
            'issuedAt':stamp(issued),'expiresAt':stamp(issued+timedelta(days=7)),
            'maxClockSkewSeconds':300,'maxCertificateAgeSeconds':86400,'auditProtocolRef':protocol_ref,
            'durabilityPolicyRef':durability_ref,'requiredControls':list(CONTROL_KEYS)}
        manifest = {'schemaVersion':2,'activationId':'activation-'+uuid.uuid4().hex,
            'runtimeSourceCommit':config['runtimeCommit'],'runtimeSourceSha256':config['runtimeSha256'],
            'issuedAt':stamp(issued),'expiresAt':stamp(issued+timedelta(days=1))}
    else:
        if len(activations)!=1:
            raise CycleError('expected one live activation')
        activation = activations[0]
        if (activation['runtimeSourceCommit'],activation['runtimeSourceSha256']) != (config['runtimeCommit'],config['runtimeSha256']):
            raise CycleError('activated source changed')
        previous = [r for r in rows if r.get('kind') in ('capture-activation','capture-evidence-renewal')][-1]
        manifest = json.loads(store.read_verified(previous['approvalManifest']))
        activation_policy = json.loads(store.read_verified(manifest['policyRef']))
        protocol = json.loads(store.read_verified(activation_policy['auditProtocolRef']))
        policy = policy_from_document(json.loads(store.read_verified(activation_policy['durabilityPolicyRef'])))
        for doc in [manifest,activation_policy]:
            duration = datetime.fromisoformat(doc['expiresAt'].replace('Z','+00:00'))-datetime.fromisoformat(doc['issuedAt'].replace('Z','+00:00'))
            issued = now(); doc.update(issuedAt=stamp(issued),expiresAt=stamp(issued+duration))
        activation_policy['policyId']='policy-'+uuid.uuid4().hex
    policy_ref = capture(activation_policy)
    audit_ref = capture(rehearse_audit_protocol(protocol,runtime_commit=config['runtimeCommit'],
        source_tree_sha256=config['runtimeSha256'],rehearsed_at=now()))
    controls = run/'controls'; controls.mkdir(mode=0o700)
    save(controls/'configuration.json',config)
    save(controls/'activation-policy.json',activation_policy)
    save(controls/'operations-contract.md',(Path(__file__).parent/'ACTIVATION_CONTRACT.md').read_bytes())
    events = Path(config['events'])
    def snapshot(target):
        with Path(config['evidenceLock']).open('rb') as lock:
            fcntl.flock(lock,fcntl.LOCK_SH)
            save(controls/'v1-resolutions.jsonl',Path(config['v1Events']).read_bytes())
            for name in ('ACTIVE.json','OWNERSHIP.json','COUNCIL_CAPTURE_RUNBOOK.md'):
                if (root/name).is_file():
                    save(controls/name,(root/name).read_bytes())
            selected_events = events
            if not events.exists():
                if mode!='prepare':
                    raise CycleError('live capture sidecar missing')
                selected_events=run/'empty-preactivation-sidecar.jsonl'; save(selected_events,b'')
            return create_evidence_snapshot(ledger_path=config['log'],resolution_store_path=selected_events,
                control_store_path=controls,artifact_root=store.root,lock_path=config['evidenceLock'],
                snapshot_target=target,repository_root=config['runtimeRoot'])
    exporter = descriptor_custody_snapshot_exporter(snapshot)
    def bounded_export(target):
        exported = exporter(target)
        if len(exported.members)+1 > config['maxObjects'] or sum(m.size for m in exported.members)+1048576 > config['maxBytes']:
            raise CycleError('snapshot exceeds fixed budget before upload')
        return exported
    runner=BudgetRunner(config,run)
    certificate=run_offhost_durability_rehearsal(snapshot_exporter=bounded_export,
        object_store=GcsVersionedObjectStore(bucket=config['bucket'],access_posture=policy.access_posture,runner=runner),
        policy=policy,runtime_commit=config['runtimeCommit'],source_tree_sha256=config['runtimeSha256'],
        activation_policy_sha256=policy_ref['sha256'],workspace_parent='/var/tmp',
        prefix_factory=lambda:'snapshots/'+run.name)
    certificate_ref=capture(certificate.canonical_bytes)
    manifest.update(policyRef=policy_ref,controls={key:audit_ref if key==AUDIT_CONTROL_KEY else certificate_ref for key in CONTROL_KEYS})
    manifest_ref=capture(manifest)
    save(run/'manifest.json',manifest)
    save(run/'durability-certificate.json',certificate.canonical_bytes)
    common=['--artifact-root',str(store.root),'--coordination-lock',config['evidenceLock']]
    readiness=wrapper(config,'activation-readiness','--manifest-file',str(run/'manifest.json'),
        '--artifact-root',str(store.root),'--runtime-source-commit',config['runtimeCommit'],
        '--runtime-source-sha256',config['runtimeSha256'])
    save(run/'readiness.json',readiness)
    if readiness.get('appendReady') is not True:
        raise CycleError('activation readiness refused; retain evidence')
    if mode=='prepare':
        spec={'cohortName':'council-usefulness-prospective-2026','captureVersion':'capture-v2.0.0',
            'runtimeSourceCommit':config['runtimeCommit'],'runtimeSourceSha256':config['runtimeSha256'],
            'artifactRootPolicy':'private-content-addressed-v1','approvalManifest':manifest_ref}
        save(run/'activation-spec.json',spec)
        save(run/'prepared.json',{'manifestRef':manifest_ref,'calls':runner.calls,'objects':runner.objects,
            'uploadedBytes':runner.uploaded,'downloadedBytes':runner.downloaded})
    else:
        spec={'activationId':activation['activationId'],'previousManifestSha256':previous['approvalManifest']['sha256'],
            'approvalManifest':manifest_ref,'operator':config['operator'],'evidenceRef':config['authority']}
        save(run/'renewal-spec.json',spec)
        result=wrapper(config,'capture-renew-evidence','--log',config['log'],'--spec',str(run/'renewal-spec.json'),
            '--approval-manifest-file',str(run/'manifest.json'),*common)
        save(run/'complete.json',result)
    logging.info('evidence cycle %s finished: %s',mode,run)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('mode',choices=['prepare','renew']); parser.add_argument('--config',required=True); parser.add_argument('--config-sha256',required=True)
    args=parser.parse_args(); os.umask(0o077)
    config_bytes=Path(args.config).read_bytes()
    if hashlib.sha256(config_bytes).hexdigest()!=args.config_sha256:
        raise CycleError('configuration changed')
    config=json.loads(config_bytes)
    if socket.gethostname().split('.')[0]!='manny':
        raise CycleError('manny is the only writer')
    clock_state = subprocess.run(['timedatectl','show','-p','NTPSynchronized','--value'],
        capture_output=True,text=True,check=True,timeout=10)
    if clock_state.stdout.strip() != 'yes':
        raise CycleError('host clock is not synchronized')
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=config['driverSha256']:
        raise CycleError('operator driver changed')
    if hashlib.sha256((Path(__file__).parent/'ACTIVATION_CONTRACT.md').read_bytes()).hexdigest()!=config['contractSha256']:
        raise CycleError('operations contract changed')
    load_runtime(config)
    with (Path(config['root'])/'maintenance.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        cycle(config,args.mode)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except (CycleError,OSError,ValueError,subprocess.SubprocessError) as error:
        logging.error('evidence maintenance stopped (%s); retain cycle receipts, do not replay',type(error).__name__)
        sys.exit(1)
