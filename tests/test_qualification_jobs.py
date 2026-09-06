import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

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
