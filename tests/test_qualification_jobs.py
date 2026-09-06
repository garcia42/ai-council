import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from council_tools import qualification_jobs as jobs


class QualificationJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / "test-command.py"
        self.script.write_text("import sys,time\ntime.sleep(float(sys.argv[1]))\nsys.exit(int(sys.argv[2]))\n")
        jobs.initialize(self.root, {"cpu": 2, "fixture": 1})
        self.addCleanup(self.stop_remaining)

    def stop_remaining(self):
        for row in jobs.report(self.root)["jobs"]:
            for identity in [row["child"], row["supervisor"], *row.get("remainingChildren", [])]:
                if identity and jobs._process_identity(identity["pid"]) == identity:
                    try:
                        os.kill(identity["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        time.sleep(0.05)

    def request(self, delay=0, code=0, resources=None):
        return dict(programId="activation-efficiency", cwd=str(self.root),
                    argv=[sys.executable, str(self.script), str(delay), str(code)],
                    inputs=[str(self.script)], environment={}, resources=resources or {"cpu": 1})

    def wait_state(self, job_id, state):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = next(r for r in jobs.report(self.root)["jobs"] if r["jobId"] == job_id)
            if row["state"] == state:
                return row
            if row["state"] not in ("QUEUED", "RUNNING"):
                self.fail(str(row))
            time.sleep(0.05)
        self.fail(f"job {job_id} did not reach {state}")

    def test_native_failure_survives_launcher_exit_and_counts_once(self):
        spec = self.root / "spec.json"
        spec.write_text(json.dumps(self.request(0.2, 7)))
        launched = subprocess.run([sys.executable, "-B", jobs.__file__, "submit", "--root",
                                   str(self.root), "--spec", str(spec)], capture_output=True, check=True)
        job_id = json.loads(launched.stdout)["jobId"]
        row = self.wait_state(job_id, "FINISHED")
        self.assertEqual(row["nativeExit"], 7)
        self.assertLessEqual(row["queuedAt"], row["startedAt"])
        self.assertLessEqual(row["startedAt"], row["endedAt"])
        for _ in range(2):
            result = jobs.report(self.root)
            self.assertEqual(result["programs"]["activation-efficiency"]["failedNativeExits"], 1)
            self.assertEqual(result["counts"], {"FINISHED": 1})
            self.assertFalse(result["qualificationEffect"])

    def test_unrelated_resources_run_while_fixture_is_reserved(self):
        first = jobs.submit(self.root, self.request(1.5, resources={"fixture": 1}))
        self.wait_state(first["jobId"], "RUNNING")
        second = jobs.submit(self.root, self.request(resources={"fixture": 1}))
        independent = jobs.submit(self.root, self.request(resources={"cpu": 1}))
        self.wait_state(independent["jobId"], "FINISHED")
        queued = next(r for r in jobs.report(self.root)["jobs"] if r["jobId"] == second["jobId"])
        self.assertEqual(queued["state"], "QUEUED")
        self.wait_state(second["jobId"], "FINISHED")

    def test_dead_supervisor_keeps_capacity_and_no_exit_is_invented(self):
        first = jobs.submit(self.root, self.request(5, resources={"fixture": 1}))
        running = self.wait_state(first["jobId"], "RUNNING")
        os.kill(running["supervisor"]["pid"], signal.SIGKILL)
        time.sleep(0.1)
        second = jobs.submit(self.root, self.request(resources={"fixture": 1}))
        time.sleep(0.2)
        rows = {r["jobId"]: r for r in jobs.report(self.root)["jobs"]}
        self.assertEqual(rows[first["jobId"]]["observedState"], "UNKNOWN_SUPERVISOR_LOST")
        self.assertIsNone(rows[first["jobId"]]["nativeExit"])
        self.assertEqual(rows[second["jobId"]]["state"], "QUEUED")

    def test_drift_while_queued_refuses_child_start(self):
        first = jobs.submit(self.root, self.request(0.6, resources={"fixture": 1}))
        self.wait_state(first["jobId"], "RUNNING")
        second = jobs.submit(self.root, self.request(resources={"fixture": 1}))
        self.script.write_text("raise SystemExit(0)\n")
        result = self.wait_state(second["jobId"], "REFUSED_INPUT_DRIFT")
        self.assertIsNone(result["child"])
        self.assertIsNone(result["nativeExit"])

    def test_unknown_execution_reserves_claim_and_excess_request_refused(self):
        row = {"state": "UNKNOWN_EXECUTION", "request": {"resources": {"fixture": 1}}}
        self.assertFalse(jobs._available(row, [row], {"fixture": 1}))
        with self.assertRaisesRegex(jobs.JobRefused, "exceeds"):
            jobs.submit(self.root, self.request(resources={"cpu": 3}))
        self.assertEqual(jobs.report(self.root)["counts"], {})

    def test_adopted_zombie_is_reaped_before_main_child_exits(self):
        orphan = self.root / "orphan.py"
        orphan.write_text(
            "import os,time,pathlib\n"
            "if os.fork() == 0:\n"
            " pathlib.Path('orphan.pid').write_text(str(os.getpid()))\n"
            " time.sleep(.15)\n"
            " os._exit(0)\n")
        self.script.write_text(
            "import os,pathlib,subprocess,sys,time\n"
            "subprocess.run([sys.executable,'orphan.py'],check=True)\n"
            "deadline=time.monotonic()+3\n"
            "while not pathlib.Path('orphan.pid').exists() and time.monotonic()<deadline: time.sleep(.02)\n"
            "pid=int(pathlib.Path('orphan.pid').read_text())\n"
            "while time.monotonic()<deadline:\n"
            " try: os.kill(pid,0)\n"
            " except ProcessLookupError: sys.exit(7)\n"
            " time.sleep(.02)\n"
            "sys.exit(8)\n")
        request = self.request()
        request["inputs"].append(str(orphan))
        row = self.wait_state(jobs.submit(self.root, request)["jobId"], "FINISHED")
        self.assertEqual(row["nativeExit"], 7)
        self.assertEqual(row["remainingChildren"], [])

    def test_deleted_queued_input_is_explicitly_refused(self):
        first = jobs.submit(self.root, self.request(.6, resources={"fixture": 1}))
        self.wait_state(first["jobId"], "RUNNING")
        second = jobs.submit(self.root, self.request(resources={"fixture": 1}))
        self.script.unlink()
        row = self.wait_state(second["jobId"], "REFUSED_INPUT_DRIFT")
        self.assertIsNone(row["nativeExit"])
        self.assertIsNone(row["child"])

    def test_only_same_host_reboot_reconciles_without_rewriting_exit_or_evidence(self):
        job_id = "job-" + "a" * 32
        host = jobs._host_identity()
        identity = dict(pid=999999, startTicks="1", bootId=host["bootId"])
        row = dict(jobId=job_id, state="UNKNOWN_DESCENDANTS", binding={"host": host},
                   supervisor=identity, child=identity, remainingChildren=[identity],
                   nativeExit=7, outputSha256="retained-evidence",
                   request={"programId": "test", "resources": {"fixture": 1}})
        path = self.root / (job_id + ".json")
        jobs._save(path, row, new=True)
        for observed in (host, dict(machineSha256="other", bootId="new-boot")):
            with mock.patch.object(jobs, "_host_identity", return_value=observed), self.assertRaises(jobs.JobRefused):
                jobs.reconcile_after_reboot(self.root, job_id, "examined retained evidence")
            self.assertEqual(jobs._load(path), row)
        with mock.patch.object(jobs, "_host_identity", return_value=dict(host, bootId="new-boot")):
            reconciled = jobs.reconcile_after_reboot(self.root, job_id, "examined retained evidence")
            with self.assertRaises(jobs.JobRefused):
                jobs.reconcile_after_reboot(self.root, job_id, "repeat")
        self.assertEqual(reconciled["nativeExit"], 7)
        self.assertEqual(reconciled["outputSha256"], "retained-evidence")
        self.assertEqual(reconciled["remainingChildren"], [identity])
        self.assertEqual(reconciled["reconciliation"]["previousState"], "UNKNOWN_DESCENDANTS")
        self.assertFalse(reconciled["reconciliation"]["qualificationEffect"])
        self.assertTrue(jobs._available(row, [reconciled], {"fixture": 1}))

    def test_pending_write_is_reported_and_preserved(self):
        pending = self.root / "job-interrupted.json.pending"
        pending.write_text("uncommitted evidence")
        self.assertEqual(jobs.report(self.root)["pendingWrites"], [pending.name])
        self.assertEqual(pending.read_text(), "uncommitted evidence")

    def test_successful_parent_cannot_free_capacity_with_a_detached_child_alive(self):
        self.script.write_text(
            "import subprocess,sys\n"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)'],start_new_session=True)\n")
        first = jobs.submit(self.root, self.request(resources={"fixture": 1}))
        row = self.wait_state(first["jobId"], "UNKNOWN_DESCENDANTS")
        self.assertEqual(row["nativeExit"], 0)
        self.assertTrue(row["remainingChildren"])
        second = jobs.submit(self.root, self.request(resources={"fixture": 1}))
        time.sleep(0.1)
        other = next(r for r in jobs.report(self.root)["jobs"] if r["jobId"] == second["jobId"])
        self.assertEqual(other["state"], "QUEUED")
