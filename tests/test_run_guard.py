from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins"
    / "ai-council-run-guard"
    / "scripts"
    / "run_guard.py"
)
SPEC = importlib.util.spec_from_file_location("ai_council_run_guard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
run_guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = run_guard
SPEC.loader.exec_module(run_guard)


class RunGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="run-guard-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / ".ai-council").mkdir()
        source_policy = ROOT / ".ai-council" / "run-guard.json"
        (self.root / ".ai-council" / "run-guard.json").write_bytes(
            source_policy.read_bytes()
        )
        self.state_dir = self.root / "state"
        self.policy = run_guard.load_policy(self.root)
        self.base = {
            "session_id": "session-1",
            "cwd": str(self.root),
            "turn_id": "turn-1",
            "permission_mode": "default",
        }
        self._event("SessionStart", now=1_000_000, source="startup")

    def _event(self, event: str, *, now: float, **fields):
        return run_guard.process_event(
            {**self.base, "hook_event_name": event, **fields},
            root=self.root,
            policy=self.policy,
            state_dir=self.state_dir,
            now=now,
        )

    def _state(self):
        path = self.state_dir / "sessions" / "session-1.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _repeat(self, prefix: str, start: float, count: int, *, value="same"):
        output = {}
        for index in range(count):
            output = self._event(
                "PostToolUse",
                now=start + index,
                tool_name="diagnostic",
                tool_use_id=f"{prefix}-{index}",
                tool_input={"value": value},
                tool_response={"result": "unchanged"},
            )
        return output

    def test_checked_in_policy_uses_recommended_limits(self):
        self.assertEqual(self.policy.checkpoint_minutes, 90)
        self.assertEqual(self.policy.hard_stop_minutes, 240)
        self.assertEqual(self.policy.max_review_rounds, 2)
        self.assertEqual(self.policy.max_agent_starts, 8)
        self.assertEqual(self.policy.max_qualification_runs, 2)
        self.assertEqual(self.policy.blocking_severities, ("P0", "P1"))

    def test_checkpoint_is_machine_written_without_blocking(self):
        output = self._event(
            "PreToolUse",
            now=1_000_000 + 90 * 60,
            tool_name="Bash",
            tool_use_id="checkpoint-tool",
            tool_input={"command": "git status"},
        )
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("RUN CHECKPOINT REQUIRED", context)
        path = Path(self._state()["checkpointPath"])
        self.assertTrue(path.is_file())
        self.assertIn("90-minute checkpoint reached", path.read_text(encoding="utf-8"))

    def test_hard_stop_denies_next_tool_and_writes_handoff(self):
        output = self._event(
            "PreToolUse",
            now=1_000_000 + 240 * 60,
            tool_name="Bash",
            tool_use_id="expired-tool",
            tool_input={"command": "git status"},
        )
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertIn("240-minute hard stop", specific["permissionDecisionReason"])
        handoff = Path(self._state()["handoffPath"])
        self.assertTrue(handoff.is_file())
        self.assertIn("Status: `stopped`", handoff.read_text(encoding="utf-8"))

    def test_tool_that_finishes_after_expiry_stops_at_post_tool_boundary(self):
        output = self._event(
            "PostToolUse",
            now=1_000_000 + 240 * 60,
            tool_name="Bash",
            tool_use_id="long-tool",
            tool_input={"command": "long-running-command"},
            tool_response={"result": "finished"},
        )
        self.assertIn("RUN STOPPED", output["reason"])
        self.assertEqual(self._state()["status"], "stopped")

    def test_handoff_separates_blockers_from_backlog(self):
        path = self.state_dir / "sessions" / "session-1.json"
        state = self._state()
        state["findings"] = [
            {"severity": "P1", "summary": "unsafe accepted path"},
            {"severity": "P2", "summary": "copy could be clearer"},
        ]
        run_guard._write_state(path, state)
        self._event(
            "PreToolUse",
            now=1_000_000 + 240 * 60,
            tool_name="Bash",
            tool_use_id="handoff-tool",
            tool_input={"command": "git status"},
        )
        handoff = Path(self._state()["handoffPath"]).read_text(encoding="utf-8")
        blocking, backlog = handoff.split("## Backlog findings (P2/P3)")
        self.assertIn("P1: unsafe accepted path", blocking)
        self.assertNotIn("P2: copy could be clearer", blocking)
        self.assertIn("P2: copy could be clearer", backlog)

    def test_same_action_and_outcome_replans_once_then_stops(self):
        first = self._repeat("first", 1_000_010, 3)
        self.assertIn("REPLAN REQUIRED", first["reason"])
        second = self._repeat("second", 1_000_020, 3)
        self.assertIn("RUN STOPPED", second["reason"])
        state = self._state()
        self.assertEqual(state["replansUsed"], 1)
        self.assertEqual(state["status"], "stopped")

    def test_duplicate_hook_delivery_does_not_double_count(self):
        fields = {
            "tool_name": "Agent",
            "tool_use_id": "agent-1",
            "tool_input": {"task_name": "implementation"},
        }
        self._event("PreToolUse", now=1_000_010, **fields)
        self._event("PreToolUse", now=1_000_011, **fields)
        self.assertEqual(self._state()["counters"]["agentStarts"], 1)

    def test_agent_start_limit_is_enforced_before_ninth_start(self):
        for index in range(8):
            output = self._event(
                "PreToolUse",
                now=1_000_010 + index,
                tool_name="Agent",
                tool_use_id=f"agent-{index}",
                tool_input={"task_name": "implementation"},
            )
            self.assertEqual(output, {})
        denied = self._event(
            "PreToolUse",
            now=1_000_020,
            tool_name="Agent",
            tool_use_id="agent-9",
            tool_input={"task_name": "implementation"},
        )
        self.assertEqual(
            denied["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn("agent-start limit", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_two_full_qualifications_allowed_and_third_denied(self):
        for index in range(2):
            output = self._event(
                "PreToolUse",
                now=1_000_010 + index,
                tool_name="Bash",
                tool_use_id=f"qualification-{index}",
                tool_input={"command": "python -m unittest discover -s tests -v"},
            )
            self.assertEqual(output, {})
        denied = self._event(
            "PreToolUse",
            now=1_000_020,
            tool_name="Bash",
            tool_use_id="qualification-3",
            tool_input={"command": "python -m unittest discover -s tests -v"},
        )
        self.assertIn(
            "qualification limit",
            denied["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_pytest_without_a_path_counts_as_full_qualification(self):
        self._event(
            "PreToolUse",
            now=1_000_010,
            tool_name="Bash",
            tool_use_id="pytest-full",
            tool_input={"cmd": "python -m pytest -q"},
        )
        self.assertEqual(self._state()["counters"]["qualificationRuns"], 1)

    def test_focused_pytest_file_does_not_count_as_full_qualification(self):
        self._event(
            "PreToolUse",
            now=1_000_010,
            tool_name="Bash",
            tool_use_id="pytest-focused",
            tool_input={"command": "pytest tests/test_run_guard.py -q"},
        )
        self.assertEqual(self._state()["counters"]["qualificationRuns"], 0)

    def test_two_no_progress_review_rounds_trigger_replan(self):
        first = self._event(
            "PreToolUse",
            now=1_000_010,
            turn_id="review-turn-1",
            tool_name="Agent",
            tool_use_id="review-1",
            tool_input={"task_name": "council-review-round-1"},
        )
        self.assertEqual(first, {})
        second = self._event(
            "PreToolUse",
            now=1_000_020,
            turn_id="review-turn-2",
            tool_name="Agent",
            tool_use_id="review-2",
            tool_input={"task_name": "council-review-round-2"},
        )
        self.assertIn("REPLAN REQUIRED", second["reason"])

    def test_parallel_review_seats_on_one_candidate_are_one_round(self):
        for index in range(4):
            output = self._event(
                "PreToolUse",
                now=1_000_010 + index,
                tool_name="Agent",
                tool_use_id=f"seat-{index}",
                tool_input={"task_name": f"council-seat-{index}"},
            )
            self.assertEqual(output, {})
        self.assertEqual(self._state()["counters"]["reviewRounds"], 1)

    def test_alternating_cycle_triggers_replan(self):
        output = {}
        for index, value in enumerate(("A", "B", "A", "B", "A", "B")):
            output = self._event(
                "PostToolUse",
                now=1_000_010 + index,
                tool_name="diagnostic",
                tool_use_id=f"cycle-{index}",
                tool_input={"value": value},
                tool_response={"result": value},
            )
        self.assertIn("alternating two-action cycle", output["reason"])

    def test_repository_progress_resets_repeat_detector(self):
        fingerprints = iter(["same", "same", "changed"])
        with mock.patch.object(
            run_guard, "_repo_fingerprint", side_effect=lambda _root: next(fingerprints)
        ):
            state = self._state()
            state["repoFingerprint"] = "same"
            path = self.state_dir / "sessions" / "session-1.json"
            run_guard._write_state(path, state)
            output = self._repeat("progress", 1_000_010, 3)
        state = self._state()
        self.assertEqual(output, {})
        self.assertEqual(state["progressGeneration"], 1)
        self.assertEqual(state["pairCounts"], {})

    def test_real_user_can_renew_time_but_not_stuck_stop(self):
        self._event(
            "PreToolUse",
            now=1_000_000 + 240 * 60,
            tool_name="Bash",
            tool_use_id="expired",
            tool_input={"command": "git status"},
        )
        renewed = self._event(
            "UserPromptSubmit",
            now=1_000_000 + 240 * 60 + 1,
            prompt="CONTINUE BOUNDED RUN FOR 120 MINUTES: finish the accepted contract",
        )
        self.assertIn("renewal accepted", renewed["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self._state()["renewals"][0]["minutes"], 120)

        self._repeat("stuck-one", 1_020_000, 3)
        self._repeat("stuck-two", 1_020_010, 3)
        blocked = self._event(
            "UserPromptSubmit",
            now=1_020_020,
            turn_id="renew-after-stuck",
            prompt="CONTINUE BOUNDED RUN FOR 120 MINUTES: try the same loop again",
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("cannot override", blocked["reason"])

    def test_nonrenewal_user_prompt_starts_new_run_after_expiry(self):
        original_run = self._state()["runId"]
        output = self._event(
            "UserPromptSubmit",
            now=1_000_000 + 240 * 60,
            prompt="Start a different bounded task",
        )
        self.assertIn(
            "new bounded run", output["hookSpecificOutput"]["additionalContext"]
        )
        state = self._state()
        self.assertNotEqual(state["runId"], original_run)
        self.assertEqual(state["counters"]["agentStarts"], 0)

    def test_policy_rejects_non_p0_p1_blockers(self):
        raw = json.loads((self.root / ".ai-council/run-guard.json").read_text())
        raw["blockingSeverities"].append("P2")
        with self.assertRaisesRegex(run_guard.RunGuardError, "exactly"):
            run_guard.Policy.from_mapping(raw)

    def test_cli_state_selection_fails_closed_with_multiple_sessions(self):
        self._event(
            "SessionStart",
            now=1_000_010,
            session_id="session-2",
            source="startup",
        )
        with mock.patch.dict(
            run_guard.os.environ,
            {"AI_COUNCIL_RUN_GUARD_STATE_DIR": str(self.state_dir)},
        ):
            with self.assertRaisesRegex(run_guard.RunGuardError, "multiple"):
                run_guard._select_state(self.root, None)
            path, state = run_guard._select_state(self.root, "session-1")
        self.assertEqual(path.name, "session-1.json")
        self.assertEqual(state["sessionId"], "session-1")


if __name__ == "__main__":
    unittest.main()
