import fcntl
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from council_tools import study_report as report
from council_tools.capture_runtime import append_capture_activation
from council_tools.data_health import DataHealthError
from council_tools.study_routes import StudyRoute


CRITERION = Path('/home/trader/.claude/knowledge/council-eval/blind_seat_kill_criterion.py')


def rows(*values):
    return report._rows(b''.join(json.dumps(value).encode()+b'\n' for value in values))


def seat(identifier, *, ran=True, required=True, changed=False):
    blind = {'brief': '/fixture/'+identifier, 'required': required, 'ran': ran,
             'changedDecision': changed if ran else None}
    if not ran:
        blind.update(role='SKIPPED', blockedReason='provider unavailable', agreedWithPanel=None)
    if not required:
        blind['notRequiredReason'] = 'mechanical fix with no decision surface'
    return {'kind': 'council', 'runId': identifier, 'blindSeat': blind}


class StudyReportTest(unittest.TestCase):
    def setUp(self):
        if not CRITERION.is_file():
            self.skipTest('installed blind criterion is required for runtime integration')
        self.digest = hashlib.sha256(CRITERION.read_bytes()).hexdigest()
        self.criterion = report._load_criterion(CRITERION, self.digest)
        self.temp = tempfile.TemporaryDirectory(dir='/var/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        lock = self.root/'evidence.lock'
        lock.write_bytes(b'')
        self.routes = {}
        for study, state in [('council-legacy', 'closed'), ('council-fresh-20260910', 'active')]:
            root = self.root/study
            root.mkdir()
            paths = [root/name for name in ('log', 'v1_events', 'v2_events')]
            for path in paths:
                path.write_bytes(b'')
            self.routes[study] = StudyRoute(study, state, *(str(path) for path in paths),
                                            str(root/'artifacts'), str(root/'controls'), str(lock))

    def invoke(self, legacy_digest=None):
        with mock.patch.object(report, 'resolve_study_route', side_effect=self.routes.__getitem__):
            return report.study_operations_report(criterion_path=CRITERION, criterion_sha256=self.digest,
                legacy_ledger_sha256=legacy_digest or hashlib.sha256(b'').hexdigest(),
                as_of=datetime(2026, 9, 10, tzinfo=timezone.utc))

    def test_boundary_failure_streak_survives_empty_fresh_store_and_exemptions(self):
        for fresh in ([], rows(seat('fresh-skip', ran=False, required=False)), rows(seat('fresh-failure', ran=False))):
            with self.subTest(fresh=fresh):
                old, new, combined = report._combine_blind(self.criterion, rows(seat('old-failure', ran=False)), fresh)
                self.assertEqual(combined['consecutiveRequiredBlockedNonRuns'], 1+new['consecutiveRequiredBlockedNonRuns'])
                self.assertEqual(old['completedRuns'], 0)
        combined = report._combine_blind(self.criterion, rows(seat('old-failure', ran=False)), rows(seat('fresh-failure', ran=False)))[2]
        self.assertEqual(combined['operationalState'], 'BLOCKED_DEGRADED')

    def test_success_resets_streak_without_pooling_study_inference(self):
        old = rows(*(seat('old-'+str(i)) for i in range(9)), seat('old-failure', ran=False))
        fresh = rows(seat('fresh-success', changed=True), seat('fresh-failure', ran=False))
        a, b, combined = report._combine_blind(self.criterion, old, fresh)
        self.assertEqual((a['completedRuns'], b['completedRuns']), (9, 1))
        self.assertEqual(combined['completedRuns'], 10)
        self.assertEqual(combined['changedDecisionRuns'], 1)
        self.assertEqual(b['criterion'], 'NOT_YET_EVALUABLE')
        self.assertEqual(combined['criterion'], 'NOT_APPLICABLE_SEPARATE_STUDIES')
        self.assertIsNone(combined['decisionChangingRate'])
        self.assertEqual(combined['consecutiveRequiredBlockedNonRuns'], 1)

    def test_cross_study_identity_reuse_and_invalid_rows_refuse(self):
        for fresh in (rows(seat('old')), rows({**seat('new'), 'runId': 'old'}), rows({'blindSeat': {'ran': False}})):
            with self.subTest(fresh=fresh), self.assertRaises(report.StudyReportError):
                report._combine_blind(self.criterion, rows(seat('old')), fresh)

    def test_criterion_and_issuance_digests_fail_closed(self):
        with self.assertRaisesRegex(report.StudyReportError, 'criterion digest'):
            report._load_criterion(CRITERION, '0'*64)
        Path(self.routes['council-legacy'].log).write_bytes(b'{}\n')
        with self.assertRaisesRegex(report.StudyReportError, 'closure receipt'):
            self.invoke()

    def test_missing_sidecar_is_not_an_empty_study(self):
        Path(self.routes['council-fresh-20260910'].v2_events).unlink()
        with self.assertRaises(FileNotFoundError):
            self.invoke()

    def test_empty_unactivated_study_refuses_without_writing(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaises(DataHealthError):
            self.invoke()
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_real_activated_reports_are_separate_and_read_only(self):
        for route in self.routes.values():
            append_capture_activation(route.log, {
                'cohortName': route.study_id, 'captureVersion': 'capture-v2.0.0',
                'runtimeSourceCommit': 'a'*40, 'runtimeSourceSha256': 'b'*64,
                'artifactRootPolicy': 'private-content-addressed-v1',
            }, clock=lambda: '2026-09-10T00:00:00Z', coordination_lock=route.coordination_lock)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.invoke(hashlib.sha256(Path(self.routes['council-legacy'].log).read_bytes()).hexdigest())
        self.assertEqual(result['exitCode'], 3)
        self.assertFalse(result['freshStudyCurrentlyHealthy'])
        self.assertEqual(set(result['studies']), set(self.routes))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertFalse(Path(self.routes['council-fresh-20260910'].artifact_root).exists())

    def test_old_debt_and_blind_degradation_block_even_when_fresh_healthy(self):
        forecast = {'invalidRecords': [], 'gradingDebtState': 'BLOCK_FINALIZATION', 'oldOverdueOutcomes': 3}
        capture = {'ledger': {'invalidV2RecordCount': 0}, 'activationReadiness': {'currentlyHealthy': True}}
        with mock.patch.object(report, 'audit', side_effect=[forecast, {**forecast, 'gradingDebtState': 'OK', 'oldOverdueOutcomes': 0}]), mock.patch.object(report, 'capture_report', return_value=capture):
            result = self.invoke()
        self.assertTrue(result['gradingDebtBlocksFinalization'])
        self.assertEqual(result['oldOverdueOutcomes'], 3)
        self.assertEqual(result['exitCode'], 3)

    def test_reports_are_read_inside_shared_lock(self):
        original = report.audit
        def audit_locked(*args, **kwargs):
            with Path(self.routes['council-legacy'].coordination_lock).open('rb') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(*args, **kwargs)
        with mock.patch.object(report, 'audit', side_effect=audit_locked):
            with self.assertRaises(DataHealthError):
                self.invoke()

    def test_strict_json_rejects_duplicate_keys_and_non_objects(self):
        for raw in (b'{"kind":1,"kind":2}\n', b'[]\n', b'{"p":NaN}\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                report._rows(raw)
