import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        legacy_dir = self.root / "truth-and-reconciliation/data"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "forecasts.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-08-18T00:00:00Z",
                    "predictions": [
                        {
                            "seat": "research",
                            "claim": "Resolved rehearsal legacy item",
                            "probability": 80,
                            "resolutionDate": "2026-08-19",
                        },
                        {
                            "seat": "research",
                            "claim": "Unresolved rehearsal legacy item",
                            "probability": 60,
                            "resolutionDate": "2026-09-30",
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (legacy_dir / "resolved.jsonl").write_text(
            json.dumps(
                {
                    "key": "2026-08-18T00:00:00Z#0",
                    "ts": "2026-08-18T00:00:00Z",
                    "index": 0,
                    "probability": 80,
                    "came_true": True,
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
        self.assertTrue(result["liveCoordinationLockUnopened"])
        self.assertTrue(result["stagedInstallClean"])
        self.assertGreater(result["isolatedCoreTests"], 0)
        self.assertEqual(result["runtimeContractTests"], 5)
        self.assertEqual(
            result["runtimeContractIsolation"]["executedProofTest"],
            "test_rehearsal_audit_guard_denies_live_access_and_path_mutators",
        )
        isolation = result["runtimeContractIsolation"]
        self.assertEqual(isolation["stores"], "staged-or-temp-only")
        self.assertTrue(isolation["liveOpenDeniedBeforeSyscall"])
        self.assertTrue(isolation["liveLinkDeniedBeforeSyscall"])
        self.assertEqual(
            isolation["representativeMutationProofs"],
            {
                "chmod": True,
                "rename": True,
                "replace": True,
                "remove": True,
                "unlink": True,
                "symlink": True,
            },
        )
        self.assertTrue(isolation["metadataComparisonDefenseInDepth"])
        self.assertIn("not arbitrary native syscalls", isolation["enforcementScope"])
        self.assertIn("descriptor inherited before the hook", isolation["enforcementScope"])
        self.assertEqual(result["legacyCompatibility"]["realRowsParsed"], 1)
        self.assertEqual(result["legacyCompatibility"]["realResolutionsParsed"], 1)
        self.assertTrue(result["legacyCompatibility"]["copiedResolutionAppended"])
        self.assertTrue(
            result["legacyCompatibility"]["councilPathsDeniedDuringProcess"]
        )
        self.assertTrue(result["failurePath"]["malformedProbabilityRejectedWithoutAppend"])
        self.assertTrue(result["failurePath"]["unavailableSeatSealedWithoutProbability"])
        self.assertTrue(result["captureV2"]["copiedRuntimeLifecycleComplete"])
        self.assertTrue(result["captureV2"]["forecastRequestAndPromptBound"])
        self.assertTrue(result["captureV2"]["canonicalForecastRequestParsed"])
        self.assertTrue(result["captureV2"]["reportTimeForecastRequestVerified"])
        self.assertTrue(result["captureV2"]["structuredEmptyFindingsVerified"])
        self.assertEqual(result["captureV2"]["completeInitiations"], 1)
        self.assertEqual(result["captureV2"]["artifactIntegrityFailures"], 0)
        self.assertEqual(result["captureV2"]["blindCompletedRuns"], 1)
        self.assertEqual(result["captureV2"]["blindNonCouncilRecords"], 4)
        self.assertFalse(result["captureV2"]["liveActivated"])
        self.assertTrue(result["runtimeSourcePin"]["sourceDriftRejectedBeforeAppend"])
        self.assertEqual(
            (
                self.root / ".claude/knowledge/council-eval/predictions_report.py"
            ).read_bytes(),
            original_reporter,
        )

    def test_live_lock_state_observation_does_not_open_its_contents(self):
        sentinel = self.root / "live-evidence.lock"
        sentinel.write_bytes(b"must-not-be-opened")
        with mock.patch.object(
            rehearse, "_digest", side_effect=AssertionError("lock content opened")
        ):
            before = rehearse._path_state(sentinel, hash_content=False)
            after = rehearse._path_state(sentinel, hash_content=False)
        self.assertEqual(before, after)
        self.assertNotIn("sha256", before)
        self.assertIn("nlink", before)
        self.assertIn("ctimeNs", before)

    def test_path_state_detects_hardlink_inode_metadata_change(self):
        source = self.root / "staged-ledger.jsonl"
        alias = self.root / "staged-ledger-alias.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        before = rehearse._path_state(source)
        os.link(source, alias)
        after = rehearse._path_state(source)
        self.assertEqual(after["nlink"], before["nlink"] + 1)
        self.assertNotEqual(before, after)
        self.assertIn("ctimeNs", after)


if __name__ == "__main__":
    unittest.main()
