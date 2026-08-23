#!/usr/bin/env python3
"""Deterministic time, iteration, and stuck-loop guard for Codex runs.

The file is deliberately self-contained so an installed plugin does not depend on
the AI Council Python package being installed.  State lives outside the working
tree, under the repository's Git directory by default.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


POLICY_RELATIVE_PATH = Path(".ai-council/run-guard.json")
SCHEMA_VERSION = 1
RENEWAL_RE = re.compile(
    r"^CONTINUE BOUNDED RUN FOR (?P<minutes>[1-9][0-9]*) MINUTES: "
    r"(?P<reason>\S(?:.*\S)?)$"
)
SEVERITIES = ("P0", "P1", "P2", "P3")
BLOCKING_SEVERITIES = ("P0", "P1")
TIME_STOP_REASON = "time lease expired"
TRANSPORT_KEYS = frozenset(
    {
        "chunk_id",
        "max_output_tokens",
        "max_tokens",
        "session_id",
        "wall_time_seconds",
        "yield_time_ms",
    }
)


class RunGuardError(ValueError):
    """Raised when policy or persisted state is invalid."""


@dataclass(frozen=True)
class Policy:
    checkpoint_minutes: int = 90
    hard_stop_minutes: int = 240
    renewal_max_minutes: int = 120
    max_review_rounds: int = 2
    max_agent_starts: int = 8
    max_qualification_runs: int = 2
    exact_repeat_limit: int = 3
    alternating_cycle_repeats: int = 3
    no_progress_rounds: int = 2
    max_replans: int = 1
    blocking_severities: tuple[str, ...] = BLOCKING_SEVERITIES
    qualification_patterns: tuple[str, ...] = (
        r"(?:^|\s)unittest\s+discover(?:\s|$)",
        r"(?:^|\s)pytest(?:\s+[^\n]*?)?\s+tests(?:\s|$)",
        r"(?:^|\s)pytest(?:\s+-[A-Za-z0-9_.=-]+)*\s*$",
    )
    review_patterns: tuple[str, ...] = (
        r"(?:^|[\s\"'])/council(?:$|[\s\"'])",
        r"council[-_ ](?:review|round|seat)",
        r"review[-_ ]?(?:round|seat|agent)",
        r"blind[-_ ]?seat",
    )

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "Policy":
        expected = {
            "schemaVersion",
            "enabled",
            "checkpointMinutes",
            "hardStopMinutes",
            "renewalMaxMinutes",
            "maxReviewRounds",
            "maxAgentStarts",
            "maxQualificationRuns",
            "exactRepeatLimit",
            "alternatingCycleRepeats",
            "noProgressRounds",
            "maxReplans",
            "blockingSeverities",
            "qualificationPatterns",
            "reviewPatterns",
        }
        if set(raw) != expected:
            missing = sorted(expected - set(raw))
            extra = sorted(set(raw) - expected)
            raise RunGuardError(
                f"policy keys do not match schema; missing={missing}, extra={extra}"
            )
        if raw["schemaVersion"] != SCHEMA_VERSION:
            raise RunGuardError(f"schemaVersion must be {SCHEMA_VERSION}")
        if raw["enabled"] is not True:
            raise RunGuardError("enabled must be true for an opted-in repository")

        integer_fields = {
            "checkpointMinutes": "checkpoint_minutes",
            "hardStopMinutes": "hard_stop_minutes",
            "renewalMaxMinutes": "renewal_max_minutes",
            "maxReviewRounds": "max_review_rounds",
            "maxAgentStarts": "max_agent_starts",
            "maxQualificationRuns": "max_qualification_runs",
            "exactRepeatLimit": "exact_repeat_limit",
            "alternatingCycleRepeats": "alternating_cycle_repeats",
            "noProgressRounds": "no_progress_rounds",
            "maxReplans": "max_replans",
        }
        values: dict[str, Any] = {}
        for source, target in integer_fields.items():
            value = raw[source]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RunGuardError(f"{source} must be a positive integer")
            values[target] = value
        if values["checkpoint_minutes"] >= values["hard_stop_minutes"]:
            raise RunGuardError("checkpointMinutes must be less than hardStopMinutes")
        if values["renewal_max_minutes"] > 120:
            raise RunGuardError("renewalMaxMinutes may not exceed 120")

        blocking = raw["blockingSeverities"]
        if blocking != list(BLOCKING_SEVERITIES):
            raise RunGuardError("blockingSeverities must be exactly ['P0', 'P1']")
        values["blocking_severities"] = tuple(blocking)
        for source, target in (
            ("qualificationPatterns", "qualification_patterns"),
            ("reviewPatterns", "review_patterns"),
        ):
            patterns = raw[source]
            if not isinstance(patterns, list) or not patterns:
                raise RunGuardError(f"{source} must be a non-empty list")
            if any(not isinstance(item, str) or not item for item in patterns):
                raise RunGuardError(f"{source} entries must be non-empty strings")
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise RunGuardError(f"invalid {source} regex: {pattern}") from exc
            values[target] = tuple(patterns)
        return cls(**values)


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunGuardError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_strict_object)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_value(item)
            for key, item in sorted(value.items())
            if key not in TRANSPORT_KEYS
        }
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\b\d+(?:\.\d+)?\s+seconds?\b", "<duration>", value)
    return value


def _run_git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except subprocess.TimeoutExpired:
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    return result.stdout.strip()


def repository_root(cwd: str | Path) -> Path | None:
    current = Path(cwd).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(current), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / POLICY_RELATIVE_PATH).is_file():
            return candidate
    return None


def state_directory(root: Path) -> Path:
    override = os.environ.get("AI_COUNCIL_RUN_GUARD_STATE_DIR")
    if override:
        return Path(override).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        git_dir = Path(result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
        return git_dir / "ai-council-run-guard"
    plugin_data = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data).resolve() / "run-guard"
    return root / ".ai-council-run-guard-state"


def _safe_session_id(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:160]
    if not safe:
        raise RunGuardError("session_id must not be empty")
    return safe


def _state_path(root: Path, session_id: str) -> Path:
    return state_directory(root) / "sessions" / f"{_safe_session_id(session_id)}.json"


def load_policy(root: Path) -> Policy:
    path = root / POLICY_RELATIVE_PATH
    try:
        raw = _strict_json_loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunGuardError(f"missing policy: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunGuardError(f"policy is not readable strict JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise RunGuardError("policy must be a JSON object")
    return Policy.from_mapping(raw)


def _repo_fingerprint(root: Path) -> str:
    return _digest(
        {
            "head": _run_git(root, "rev-parse", "HEAD"),
            "status": _run_git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        }
    )


def _new_state(session_id: str, root: Path, policy: Policy, now: float) -> dict[str, Any]:
    run_id = f"run-{int(now)}-{secrets.token_hex(4)}"
    return {
        "schemaVersion": SCHEMA_VERSION,
        "sessionId": session_id,
        "runId": run_id,
        "repositoryRoot": str(root),
        "status": "active",
        "startedAt": _utc(now),
        "startedEpoch": now,
        "checkpointDueAt": _utc(now + policy.checkpoint_minutes * 60),
        "checkpointDueEpoch": now + policy.checkpoint_minutes * 60,
        "leaseExpiresAt": _utc(now + policy.hard_stop_minutes * 60),
        "leaseExpiresEpoch": now + policy.hard_stop_minutes * 60,
        "checkpointPath": None,
        "handoffPath": None,
        "stopReason": None,
        "replansUsed": 0,
        "renewals": [],
        "counters": {"agentStarts": 0, "reviewRounds": 0, "qualificationRuns": 0},
        "reviewKeys": [],
        "reviewProgressGenerations": [],
        "progressGeneration": 0,
        "lastProgressAt": _utc(now),
        "lastProgressSummary": "run initialized",
        "repoFingerprint": _repo_fingerprint(root),
        "pairCounts": {},
        "recentPairs": [],
        "findings": [],
        "processedEvents": {},
        "lastUpdatedAt": _utc(now),
    }


def _validate_state(state: Any, session_id: str) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("schemaVersion") != SCHEMA_VERSION:
        raise RunGuardError("persisted state has an unsupported schema")
    if state.get("sessionId") != session_id:
        raise RunGuardError("persisted state sessionId mismatch")
    return state


def _read_state(path: Path, session_id: str) -> dict[str, Any] | None:
    try:
        data = _strict_json_loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunGuardError(f"state is not readable strict JSON: {path}") from exc
    return _validate_state(data, session_id)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    with temporary.open("x", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _artifact_path(root: Path, state: dict[str, Any], kind: str) -> Path:
    folder = Path(state.get("stateDirectory", state_directory(root))) / f"{kind}s"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder / f"{_safe_session_id(state['sessionId'])}-{state['runId']}-{kind}.md"


def _render_artifact(root: Path, state: dict[str, Any], kind: str, reason: str) -> str:
    counters = state["counters"]
    blocking_severities = state.get("blockingSeverities", BLOCKING_SEVERITIES)
    blocking = [
        item for item in state["findings"] if item["severity"] in blocking_severities
    ]
    backlog = [item for item in state["findings"] if item["severity"] in ("P2", "P3")]

    def findings_text(items: list[dict[str, Any]]) -> str:
        if not items:
            return "- None recorded"
        return "\n".join(f"- {item['severity']}: {item['summary']}" for item in items)

    return (
        f"# Bounded run {kind}\n\n"
        f"- Run: `{state['runId']}`\n"
        f"- Session: `{state['sessionId']}`\n"
        f"- Status: `{state['status']}`\n"
        f"- Reason: {reason}\n"
        f"- Started: {state['startedAt']}\n"
        f"- Lease expires: {state['leaseExpiresAt']}\n"
        f"- Last progress: {state['lastProgressAt']} — {state['lastProgressSummary']}\n"
        f"- Git HEAD: `{_run_git(root, 'rev-parse', 'HEAD')}`\n"
        f"- Agent starts: {counters['agentStarts']}\n"
        f"- Review rounds: {counters['reviewRounds']}\n"
        f"- Qualification runs: {counters['qualificationRuns']}\n"
        f"- Replans used: {state['replansUsed']}\n\n"
        "## Blocking findings (P0/P1)\n\n"
        f"{findings_text(blocking)}\n\n"
        "## Backlog findings (P2/P3)\n\n"
        f"{findings_text(backlog)}\n\n"
        "## Working tree\n\n"
        "```text\n"
        f"{_run_git(root, 'status', '--short') or '(clean)'}\n"
        "```\n"
    )


def _write_artifact(root: Path, state: dict[str, Any], kind: str, reason: str) -> Path:
    path = _artifact_path(root, state, kind)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        handle.write(_render_artifact(root, state, kind, reason))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)
    state[f"{kind}Path"] = str(path)
    return path


def _event_id(payload: dict[str, Any]) -> str:
    event = payload.get("hook_event_name", "unknown")
    if event in ("PreToolUse", "PostToolUse"):
        identity = payload.get("tool_use_id") or _digest(
            [payload.get("turn_id"), payload.get("tool_name"), payload.get("tool_input")]
        )
    elif event == "UserPromptSubmit":
        identity = _digest([payload.get("turn_id"), payload.get("prompt")])
    else:
        identity = _digest([payload.get("turn_id"), payload.get("source"), payload.get("reason")])
    return f"{event}:{identity}"


def _cache_output(state: dict[str, Any], event_id: str, output: dict[str, Any]) -> None:
    processed = state["processedEvents"]
    processed[event_id] = output
    while len(processed) > 256:
        del processed[next(iter(processed))]


def _context_output(event: str, message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        }
    }


def _deny_tool(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _post_feedback(reason: str) -> dict[str, Any]:
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason,
        },
    }


def _checkpoint_if_due(
    root: Path, state: dict[str, Any], policy: Policy, now: float
) -> str | None:
    if now < state["checkpointDueEpoch"] or state.get("checkpointPath"):
        return None
    path = _write_artifact(
        root,
        state,
        "checkpoint",
        f"{policy.checkpoint_minutes}-minute checkpoint reached",
    )
    return (
        f"RUN CHECKPOINT REQUIRED. A machine checkpoint was written to {path}. "
        "State the acceptance contract, completed evidence, next bounded step, and blockers "
        "before broadening the task."
    )


def _stop(root: Path, state: dict[str, Any], reason: str, now: float) -> Path:
    state["status"] = "stopped"
    state["stopReason"] = reason
    state["lastUpdatedAt"] = _utc(now)
    if not state.get("handoffPath"):
        return _write_artifact(root, state, "handoff", reason)
    return Path(state["handoffPath"])


def _classifications(payload: dict[str, Any], policy: Policy) -> tuple[bool, bool, bool]:
    tool_name = str(payload.get("tool_name", ""))
    tool_input = _stable_value(payload.get("tool_input"))
    stable_input = _canonical(tool_input)
    is_agent = tool_name == "Agent"
    is_review = is_agent and any(
        re.search(pattern, stable_input, re.IGNORECASE) for pattern in policy.review_patterns
    )
    if tool_name == "Bash":
        if isinstance(tool_input, dict):
            command = tool_input.get("command") or tool_input.get("cmd") or stable_input
        else:
            command = stable_input
        if not isinstance(command, str):
            command = stable_input
        is_review = is_review or any(
            re.search(pattern, command, re.IGNORECASE) for pattern in policy.review_patterns
        )
        is_qualification = any(
            re.search(pattern, command, re.IGNORECASE)
            for pattern in policy.qualification_patterns
        )
    else:
        is_qualification = False
    return is_agent, is_review, is_qualification


def _record_progress(state: dict[str, Any], now: float, summary: str, fingerprint: str) -> None:
    state["progressGeneration"] += 1
    state["lastProgressAt"] = _utc(now)
    state["lastProgressSummary"] = summary
    state["repoFingerprint"] = fingerprint
    state["pairCounts"] = {}
    state["recentPairs"] = []
    state["reviewProgressGenerations"] = []


def _stuck_signal(
    root: Path, state: dict[str, Any], policy: Policy, now: float, reason: str
) -> dict[str, Any]:
    if state["replansUsed"] < policy.max_replans:
        state["replansUsed"] += 1
        state["pairCounts"] = {}
        state["recentPairs"] = []
        state["reviewProgressGenerations"] = []
        return _post_feedback(
            f"REPLAN REQUIRED: {reason}. One replan is available. Freeze the acceptance "
            "contract, name the failed assumption, choose one materially different action, "
            "and do not repeat the same loop."
        )
    path = _stop(root, state, f"stuck after permitted replan: {reason}", now)
    return _post_feedback(
        f"RUN STOPPED: {reason} repeated after the one permitted replan. "
        f"Do not call more tools. Return a NOT READY handoff using {path}."
    )


def _record_review_round(
    root: Path,
    state: dict[str, Any],
    policy: Policy,
    review_key: str,
    now: float,
) -> dict[str, Any] | None:
    if review_key in state["reviewKeys"]:
        return None
    if state["counters"]["reviewRounds"] >= policy.max_review_rounds:
        return _deny_tool(
            f"review-round limit reached ({policy.max_review_rounds}); P2/P3 findings belong "
            "in the backlog and do not justify another repair loop"
        )
    state["reviewKeys"].append(review_key)
    state["counters"]["reviewRounds"] += 1
    generations = state["reviewProgressGenerations"]
    generations.append(state["progressGeneration"])
    if (
        len(generations) >= policy.no_progress_rounds
        and len(set(generations[-policy.no_progress_rounds :])) == 1
    ):
        return _stuck_signal(
            root,
            state,
            policy,
            now,
            f"{policy.no_progress_rounds} review rounds produced no repository progress",
        )
    return None


def _review_key(payload: dict[str, Any], state: dict[str, Any]) -> str:
    """Group parallel seats on one candidate while allowing explicit round IDs."""

    stable_input = _canonical(_stable_value(payload.get("tool_input")))
    match = re.search(
        r"review[-_ ]?round(?:[-_ :=]+)([A-Za-z0-9_.-]+)",
        stable_input,
        re.IGNORECASE,
    )
    if match:
        return f"explicit:{match.group(1).lower()}"
    if payload.get("tool_name") == "Bash":
        return f"command:{payload.get('tool_use_id', _digest(stable_input))}"
    return f"candidate-generation:{state['progressGeneration']}"


def _pre_tool(
    payload: dict[str, Any], root: Path, state: dict[str, Any], policy: Policy, now: float
) -> dict[str, Any]:
    if state["status"] == "stopped":
        return _deny_tool(
            f"bounded run is stopped ({state['stopReason']}); use the handoff at "
            f"{state.get('handoffPath')} or ask the principal to start new work"
        )
    if now >= state["leaseExpiresEpoch"]:
        path = _stop(root, state, TIME_STOP_REASON, now)
        return _deny_tool(
            f"{policy.hard_stop_minutes}-minute hard stop reached; no more tools are "
            "allowed. Return a NOT READY "
            f"handoff using {path}. Only a real user can renew with: CONTINUE BOUNDED RUN "
            f"FOR {policy.renewal_max_minutes} MINUTES: <reason>"
        )

    checkpoint_message = _checkpoint_if_due(root, state, policy, now)
    is_agent, is_review, is_qualification = _classifications(payload, policy)
    counters = state["counters"]
    if is_agent:
        if counters["agentStarts"] >= policy.max_agent_starts:
            return _deny_tool(f"agent-start limit reached ({policy.max_agent_starts})")
        counters["agentStarts"] += 1
    if is_qualification:
        if counters["qualificationRuns"] >= policy.max_qualification_runs:
            return _deny_tool(
                f"full qualification limit reached ({policy.max_qualification_runs}); run a "
                "focused diagnostic or hand off the remaining failure"
            )
        counters["qualificationRuns"] += 1
    if is_review:
        review_output = _record_review_round(
            root, state, policy, _review_key(payload, state), now
        )
        if review_output:
            return review_output
    if checkpoint_message:
        return _context_output("PreToolUse", checkpoint_message)
    return {}


def _post_tool(
    payload: dict[str, Any], root: Path, state: dict[str, Any], policy: Policy, now: float
) -> dict[str, Any]:
    if state["status"] == "stopped":
        return _post_feedback(
            f"RUN STOPPED. Do not continue tooling; use {state.get('handoffPath')}."
        )
    if now >= state["leaseExpiresEpoch"]:
        path = _stop(root, state, TIME_STOP_REASON, now)
        return _post_feedback(
            f"RUN STOPPED: the {policy.hard_stop_minutes}-minute lease expired while the "
            f"tool was running. Do not call more tools; use {path}."
        )
    current_fingerprint = _repo_fingerprint(root)
    if current_fingerprint != state["repoFingerprint"]:
        _record_progress(state, now, "repository state changed", current_fingerprint)
        return {}

    action = _digest(
        [payload.get("tool_name"), _stable_value(payload.get("tool_input"))]
    )
    outcome = _digest(_stable_value(payload.get("tool_response")))
    pair = _digest([action, outcome])
    pair_counts = state["pairCounts"]
    if pair not in pair_counts and len(pair_counts) >= 128:
        del pair_counts[next(iter(pair_counts))]
    pair_counts[pair] = pair_counts.get(pair, 0) + 1
    recent = state["recentPairs"]
    recent.append(pair)
    del recent[:-12]

    if (
        len(recent) >= policy.exact_repeat_limit
        and len(set(recent[-policy.exact_repeat_limit :])) == 1
    ):
        return _stuck_signal(
            root,
            state,
            policy,
            now,
            f"the same action and unchanged outcome occurred {policy.exact_repeat_limit} times",
        )
    cycle_length = policy.alternating_cycle_repeats * 2
    if len(recent) >= cycle_length:
        window = recent[-cycle_length:]
        if (
            len(set(window)) == 2
            and all(window[index] == window[index % 2] for index in range(len(window)))
        ):
            return _stuck_signal(
                root,
                state,
                policy,
                now,
                "an alternating two-action cycle repeated "
                f"{policy.alternating_cycle_repeats} times",
            )
    return {}


def _renew(
    prompt: str, root: Path, state: dict[str, Any], policy: Policy, now: float
) -> dict[str, Any] | None:
    match = RENEWAL_RE.fullmatch(prompt)
    if not match:
        return None
    minutes = int(match.group("minutes"))
    if minutes > policy.renewal_max_minutes:
        return {
            "decision": "block",
            "reason": f"renewal may not exceed {policy.renewal_max_minutes} minutes",
        }
    if state["status"] == "stopped" and state.get("stopReason") != TIME_STOP_REASON:
        return {
            "decision": "block",
            "reason": "time renewal cannot override a stuck-loop or counter stop",
        }
    base = max(now, state["leaseExpiresEpoch"])
    state["leaseExpiresEpoch"] = base + minutes * 60
    state["leaseExpiresAt"] = _utc(state["leaseExpiresEpoch"])
    state["status"] = "active"
    state["stopReason"] = None
    state["handoffPath"] = None
    state["renewals"].append(
        {
            "at": _utc(now),
            "minutes": minutes,
            "reason": match.group("reason"),
            "source": "UserPromptSubmit",
        }
    )
    return _context_output(
        "UserPromptSubmit",
        f"Principal renewal accepted for {minutes} minutes. New lease expiry: "
        f"{state['leaseExpiresAt']}. Counters and stuck-loop history were not reset.",
    )


def process_event(
    payload: dict[str, Any],
    *,
    root: Path,
    policy: Policy,
    state_dir: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Process one Codex hook payload; exported for deterministic tests."""

    now = time.time() if now is None else now
    session_id = payload.get("session_id")
    event = payload.get("hook_event_name")
    if not isinstance(session_id, str) or not session_id:
        raise RunGuardError("hook input requires a non-empty session_id")
    if not isinstance(event, str) or not event:
        raise RunGuardError("hook input requires hook_event_name")
    path = (
        state_dir / "sessions" / f"{_safe_session_id(session_id)}.json"
        if state_dir is not None
        else _state_path(root, session_id)
    )
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_state(path, session_id)
        if state is None:
            state = _new_state(session_id, root, policy, now)
        state["stateDirectory"] = str(state_dir or state_directory(root))
        state["blockingSeverities"] = list(policy.blocking_severities)
        event_id = _event_id(payload)
        cacheable = event in ("PreToolUse", "PostToolUse", "UserPromptSubmit")
        if cacheable:
            cached = state["processedEvents"].get(event_id)
            if cached is not None:
                return cached

        output: dict[str, Any]
        if event == "SessionStart":
            checkpoint = (
                _checkpoint_if_due(root, state, policy, now)
                if state["status"] == "active"
                else None
            )
            if state["status"] != "active":
                output = _context_output(
                    "SessionStart",
                    f"The prior bounded run is {state['status']} "
                    f"({state.get('stopReason') or 'session ended'}); handoff: "
                    f"{state.get('handoffPath')}. A real user may start a new bounded request.",
                )
            elif now >= state["leaseExpiresEpoch"]:
                handoff = _stop(root, state, TIME_STOP_REASON, now)
                output = {
                    "continue": False,
                    "stopReason": f"bounded run lease expired; handoff: {handoff}",
                }
            else:
                remaining = max(0, int((state["leaseExpiresEpoch"] - now) / 60))
                message = (
                    f"Bounded-run policy active for session {session_id}: checkpoint at "
                    f"{policy.checkpoint_minutes} minutes; hard stop at "
                    f"{policy.hard_stop_minutes} minutes; {policy.max_review_rounds} review "
                    f"rounds; {policy.max_agent_starts} agent starts; "
                    f"{policy.max_qualification_runs} full qualifications; only "
                    f"P0/P1 block. Approximately {remaining} lease minutes remain."
                )
                if checkpoint:
                    message = f"{message} {checkpoint}"
                output = _context_output("SessionStart", message)
        elif event == "UserPromptSubmit":
            prompt = payload.get("prompt")
            if not isinstance(prompt, str):
                raise RunGuardError("UserPromptSubmit requires prompt")
            renewal = _renew(prompt, root, state, policy, now)
            if renewal is not None:
                output = renewal
            elif state["status"] != "active" or now >= state["leaseExpiresEpoch"]:
                state = _new_state(session_id, root, policy, now)
                state["stateDirectory"] = str(state_dir or state_directory(root))
                state["blockingSeverities"] = list(policy.blocking_severities)
                output = _context_output(
                    "UserPromptSubmit",
                    "A real user started a new bounded run. Prior handoff remains on disk; "
                    "all budgets reset for this new request.",
                )
            else:
                output = {}
        elif event == "PreToolUse":
            output = _pre_tool(payload, root, state, policy, now)
        elif event == "PostToolUse":
            output = _post_tool(payload, root, state, policy, now)
        elif event == "PostCompact":
            remaining = max(0, int((state["leaseExpiresEpoch"] - now) / 60))
            output = _context_output(
                "PostCompact",
                f"Bounded-run state survived compaction. {remaining} lease minutes remain; "
                f"counters={state['counters']}; replansUsed={state['replansUsed']}.",
            )
        elif event == "SessionEnd":
            if state["status"] == "active":
                state["status"] = "session-ended"
                _write_artifact(root, state, "handoff", "Codex session ended")
            output = {}
        else:
            output = {}

        state["lastUpdatedAt"] = _utc(now)
        if cacheable:
            _cache_output(state, event_id, output)
        _write_state(path, state)
        return output


def hook_main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise RunGuardError("hook input must be a JSON object")
        root = repository_root(payload.get("cwd", os.getcwd()))
        if root is None or not (root / POLICY_RELATIVE_PATH).is_file():
            return 0
        policy = load_policy(root)
        output = process_event(payload, root=root, policy=policy)
    except (RunGuardError, OSError, json.JSONDecodeError) as exc:
        event = payload.get("hook_event_name") if "payload" in locals() else None
        if event == "PreToolUse":
            output = _deny_tool(f"run guard failed closed: {exc}")
        elif event == "UserPromptSubmit":
            output = {"decision": "block", "reason": f"run guard failed closed: {exc}"}
        else:
            output = {
                "continue": False,
                "stopReason": f"run guard failed closed: {exc}",
                "systemMessage": f"run guard error: {exc}",
            }
    if output:
        json.dump(output, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
    return 0


def _select_state(
    root: Path, session_id: str | None
) -> tuple[Path, dict[str, Any]]:
    if session_id:
        path = _state_path(root, session_id)
        state = _read_state(path, session_id)
        if state is None:
            raise RunGuardError(f"no run-guard state exists for session {session_id}")
        return path, state
    paths = list((state_directory(root) / "sessions").glob("*.json"))
    if not paths:
        raise RunGuardError("no run-guard state exists for this repository")
    if len(paths) > 1:
        sessions = sorted(path.stem for path in paths)
        raise RunGuardError(
            "multiple run-guard sessions exist; pass --session-id with one of: "
            + ", ".join(sessions)
        )
    path = paths[0]
    raw = _strict_json_loads(path.read_text(encoding="utf-8"))
    return path, _validate_state(raw, raw.get("sessionId", ""))


def doctor(root: Path, *, probe: bool) -> dict[str, Any]:
    policy = load_policy(root)
    checks: dict[str, Any] = {
        "policy": "ok",
        "recommendedDefaults": asdict(policy),
    }
    expected_files = {
        "projectHooks": root / ".codex/hooks.json",
        "pluginManifest": root / "plugins/ai-council-run-guard/.codex-plugin/plugin.json",
        "pluginHooks": root / "plugins/ai-council-run-guard/hooks/hooks.json",
        "marketplace": root / ".agents/plugins/marketplace.json",
        "skill": root / "plugins/ai-council-run-guard/skills/bounded-runs/SKILL.md",
    }
    for label, path in expected_files.items():
        if not path.is_file():
            raise RunGuardError(f"doctor missing {label}: {path}")
        checks[label] = "ok"
    for label in ("projectHooks", "pluginManifest", "pluginHooks", "marketplace"):
        path = expected_files[label]
        try:
            _strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunGuardError(f"doctor found invalid JSON in {path}") from exc

    directory = state_directory(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe_file = directory / f".doctor-{os.getpid()}-{secrets.token_hex(3)}"
    probe_file.write_text("ok\n", encoding="utf-8")
    probe_file.unlink()
    checks["stateDirectory"] = str(directory)

    if probe:
        with tempfile.TemporaryDirectory(prefix="run-guard-doctor-") as temporary:
            probe_state = Path(temporary)
            base = {
                "session_id": "doctor-session",
                "cwd": str(root),
                "turn_id": "doctor-turn",
                "permission_mode": "default",
            }
            process_event(
                {**base, "hook_event_name": "SessionStart", "source": "startup"},
                root=root,
                policy=policy,
                state_dir=probe_state,
                now=1_000_000,
            )
            third: dict[str, Any] = {}
            for index in range(policy.exact_repeat_limit):
                payload = {
                    **base,
                    "hook_event_name": "PostToolUse",
                    "tool_name": "doctor_noop",
                    "tool_use_id": f"probe-one-{index}",
                    "tool_input": {"value": "same"},
                    "tool_response": {"value": "unchanged"},
                }
                third = process_event(
                    payload,
                    root=root,
                    policy=policy,
                    state_dir=probe_state,
                    now=1_000_010 + index,
                )
            if "REPLAN REQUIRED" not in third.get("reason", ""):
                raise RunGuardError("live probe did not trigger the replan threshold")
            stopped: dict[str, Any] = {}
            for index in range(policy.exact_repeat_limit):
                payload = {
                    **base,
                    "hook_event_name": "PostToolUse",
                    "tool_name": "doctor_noop",
                    "tool_use_id": f"probe-two-{index}",
                    "tool_input": {"value": "same"},
                    "tool_response": {"value": "unchanged"},
                }
                stopped = process_event(
                    payload,
                    root=root,
                    policy=policy,
                    state_dir=probe_state,
                    now=1_000_020 + index,
                )
            if "RUN STOPPED" not in stopped.get("reason", ""):
                raise RunGuardError("live probe did not stop the repeated post-replan loop")
        checks["liveRouteProbe"] = "ok"
    return checks


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor", help="validate policy and hook routes")
    doctor_parser.add_argument("--root", default=os.getcwd())
    doctor_parser.add_argument("--probe", action="store_true")
    doctor_parser.add_argument("--json", action="store_true")
    status_parser = subparsers.add_parser("status", help="show one run state")
    status_parser.add_argument("--root", default=os.getcwd())
    status_parser.add_argument("--session-id")
    finding_parser = subparsers.add_parser("finding", help="record a severity finding")
    finding_parser.add_argument("severity", choices=SEVERITIES)
    finding_parser.add_argument("summary")
    finding_parser.add_argument("--root", default=os.getcwd())
    finding_parser.add_argument("--session-id")
    progress_parser = subparsers.add_parser("progress", help="record semantic progress")
    progress_parser.add_argument("summary")
    progress_parser.add_argument("--root", default=os.getcwd())
    progress_parser.add_argument("--session-id")
    subparsers.add_parser("hook", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "hook":
        return hook_main()
    root = repository_root(args.root)
    if root is None:
        parser.error("not in a repository")
    try:
        if args.command == "doctor":
            result = doctor(root, probe=args.probe)
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("run-guard doctor: PASS")
                for name, value in result.items():
                    if name != "recommendedDefaults":
                        print(f"  {name}: {value}")
            return 0
        path, state = _select_state(root, args.session_id)
        if args.command == "status":
            print(json.dumps({"statePath": str(path), **state}, indent=2, sort_keys=True))
            return 0
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = _read_state(path, state["sessionId"])
            if state is None:
                raise RunGuardError("run state disappeared while acquiring its lock")
            now = time.time()
            if args.command == "finding":
                state["findings"].append(
                    {
                        "severity": args.severity,
                        "summary": args.summary,
                        "recordedAt": _utc(now),
                    }
                )
            elif args.command == "progress":
                _record_progress(state, now, args.summary, _repo_fingerprint(root))
            state["lastUpdatedAt"] = _utc(now)
            _write_state(path, state)
        print(f"updated {path}")
        return 0
    except (RunGuardError, OSError, json.JSONDecodeError) as exc:
        print(f"run-guard: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
