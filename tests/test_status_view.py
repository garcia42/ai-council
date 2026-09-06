import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from council_tools.status_view import build_view


class StatusViewTests(unittest.TestCase):
    def test_contract_text_and_observation_remain_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def artifact(name, value):
                payload = json.dumps(value).encode()
                (root / name).write_bytes(payload)
                return dict(path=name, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
            contract = artifact("contract.json", {"status": "PROPOSED", "dashboard": {"status": "NOT_INSTALLED"}})
            evidence = artifact("evidence.json", {"runtimeStatus": "INSTALLED"})
            row = dict(contractPointer="/dashboard/status", artifact=evidence,
                       evidencePointer="/runtimeStatus", disposition="RECONCILE", reason="New runtime receipt")
            spec = dict(schemaVersion=1, contract=contract, observations=[row])
            report = build_view(spec, evidence_root=root)
            self.assertEqual(report["unobservedAxes"], 1)
            self.assertEqual(report["unresolvedDifferences"], 1)
            self.assertFalse(report["runtimeReadinessInferred"])
            row["disposition"] = "EVIDENCE_NEWER"
            report = build_view(spec, evidence_root=root)
            self.assertEqual(report["differences"], 1)
            self.assertEqual(report["unresolvedDifferences"], 0)
            self.assertFalse(report["authorizationEffect"])
            spec["observations"].append(row)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                build_view(spec, evidence_root=root)
            spec["observations"].pop()
            (root / "evidence.json").write_text('{"runtimeStatus":"ACTIVE"}')
            with self.assertRaisesRegex(ValueError, "mismatch"):
                build_view(spec, evidence_root=root)
