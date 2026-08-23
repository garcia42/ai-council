import json
import tempfile
import unittest
from pathlib import Path

import rehearse


class RehearsalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".claude/knowledge/council-eval").mkdir(parents=True)
        (self.root / ".claude/skills/council").mkdir(parents=True)
        (self.root / ".claude/knowledge/council-eval/predictions_report.py").write_text(
            "old reporter\n", encoding="utf-8"
        )
        (self.root / ".claude/skills/council/SKILL.md").write_text(
            "# Council\n\n## Steps\n\nOld steps\n", encoding="utf-8"
        )
        criterion_source = Path(
            "/home/trader/.claude/knowledge/council-eval/blind_seat_kill_criterion.py"
        )
        (self.root / ".claude/knowledge/council-eval/blind_seat_kill_criterion.py").write_bytes(
            criterion_source.read_bytes()
        )
        (self.root / "CLAUDE.md").write_text(
            "# Rules\n\n## SLO/SLI changes require a full council review\n",
            encoding="utf-8",
        )
        ledger = self.root / ".claude/knowledge/futures-panel-log.jsonl"
        ledger.write_text(
            json.dumps(
                {
                    "kind": "council",
                    "ts": "2026-08-22T00:00:00Z",
                    "question": "A rehearsal row",
                    "blindSeat": {
                        "role": "generic",
                        "brief": "/tmp/unique-rehearsal-brief.md",
                        "required": True,
                        "ran": True,
                        "notRequiredReason": None,
                        "changedDecision": False,
                        "agreedWithPanel": True,
                        "blockedReason": None,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_rehearsal_installs_only_to_stage_and_preserves_source_hashes(self):
        original_reporter = (
            self.root / ".claude/knowledge/council-eval/predictions_report.py"
        ).read_bytes()
        result = rehearse.rehearse(self.root, today="2026-08-22")
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["liveFilesUnchanged"])
        self.assertTrue(result["stagedInstallClean"])
        self.assertEqual(result["runtimeContractTests"], 3)
        self.assertTrue(result["failurePath"]["malformedProbabilityRejectedWithoutAppend"])
        self.assertTrue(result["failurePath"]["unavailableSeatSealedWithoutProbability"])
        self.assertEqual(
            (
                self.root / ".claude/knowledge/council-eval/predictions_report.py"
            ).read_bytes(),
            original_reporter,
        )


if __name__ == "__main__":
    unittest.main()
