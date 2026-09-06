import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from council_tools.closure import ClosureError, validate_worksheet


class ClosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        finding = dict(findingId="finding-" + "1" * 32, seatId="code", category="integrity",
                       claim="A missing input is accepted", severity="block",
                       proposedAction="Refuse missing inputs", evidenceSummary="Fixture accepts it")
        source = dict(runId="run-" + "2" * 32, candidateCommit="a" * 40, findings=[finding])
        self.row = dict(familyId="operator.input-admission", source=self.artifact("source.json", source),
                        sourcePointers=dict(runId="/runId", candidateCommit="/candidateCommit", finding="/findings/0"),
                        runId=source["runId"], candidateCommit=source["candidateCommit"], finding=finding,
                        repairedRange=dict(base="a" * 40, head="b" * 40),
                        reproduction=self.artifact("before.json", {"operatorObservation": "defect reproduced"}),
                        positiveControl=self.artifact("after.json", {"operatorObservation": "repair passed"}),
                        adjacentPaths=["read", "recovery"], disposition="OPERATOR_CLOSED", reason="Both controls retained")

    def artifact(self, name, value):
        payload = json.dumps(value).encode()
        (self.root / name).write_bytes(payload)
        return dict(path=name, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())

    def validate(self, rows=None):
        return validate_worksheet(dict(schemaVersion=1, entries=rows or [self.row]), evidence_root=self.root)

    def test_linking_does_not_change_source_or_grant_authority(self):
        before = (self.root / "source.json").read_bytes()
        result = self.validate()
        self.assertEqual(result["operatorDispositionCounts"], {"OPERATOR_CLOSED": 1})
        self.assertFalse(result["authorizationEffect"])
        self.assertFalse(result["technicalClosureVerified"])
        self.assertEqual(before, (self.root / "source.json").read_bytes())

    def test_every_original_field_is_bound_to_original_artifact(self):
        original = copy.deepcopy(self.row)
        for field in ("runId", "candidateCommit", "finding"):
            with self.subTest(field=field):
                self.row = copy.deepcopy(original)
                if field == "finding":
                    self.row[field]["claim"] = "Operator rewrote the seat"
                else:
                    self.row[field] = self.row[field].replace("2", "3").replace("a", "b")
                with self.assertRaises(ClosureError):
                    self.validate()

    def test_missing_controls_cannot_close(self):
        for key in ("reproduction", "positiveControl", "repairedRange"):
            with self.subTest(key=key):
                row = copy.deepcopy(self.row)
                row[key] = None
                with self.assertRaisesRegex(ClosureError, "both controls"):
                    self.validate([row])
                row["disposition"] = "OPEN"
                self.assertEqual(self.validate([row])["operatorDispositionCounts"], {"OPEN": 1})

    def test_digest_tampering_and_aliases_refused(self):
        (self.root / "before.json").write_text("different evidence")
        with self.assertRaisesRegex(ClosureError, "mismatch"):
            self.validate()
        self.row["reproduction"] = None
        self.row["disposition"] = "OPEN"
        (self.root / "alias.json").symlink_to(self.root / "source.json")
        self.row["source"]["path"] = "alias.json"
        with self.assertRaisesRegex(ClosureError, "aliases"):
            self.validate()

    def test_duplicate_finding_and_seat_fields_added_by_operator_refused(self):
        with self.assertRaisesRegex(ClosureError, "duplicate original"):
            self.validate([self.row, self.row])
        self.row["finding"]["operatorVerdict"] = "APPROVE"
        with self.assertRaisesRegex(ClosureError, "seat-owned"):
            self.validate()
