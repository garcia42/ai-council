import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import install


RUNTIME_ROOT = Path(os.environ.get("COUNCIL_RUNTIME_ROOT", "/home/trader")) / ".claude"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPROVAL_QUORUM_TEXT = (
    "at least two of the code, theory, and operations lenses return exactly `APPROVE`"
)
_DENIED_RUNTIME_PATHS = tuple(
    Path(item).expanduser().resolve()
    for item in os.environ.get(
        "COUNCIL_RUNTIME_CONTRACT_DENY_PATHS", ""
    ).split(os.pathsep)
    if item
)

_PATH_MUTATOR_OPERANDS = {
    "os.chmod": ((0, 2),),
    "os.chown": ((0, 3),),
    "os.utime": ((0, 3),),
    "os.truncate": ((0, None),),
    "os.link": ((0, 2), (1, 3)),
    "os.symlink": ((0, None), (1, 2)),
    # CPython emits os.rename for both os.rename and os.replace.
    "os.rename": ((0, 2), (1, 3)),
    # CPython emits os.remove for both os.remove and os.unlink.
    "os.remove": ((0, 1),),
    "os.mkdir": ((0, 2),),
    "os.rmdir": ((0, 1),),
}


def _path_from_audit_operand(raw_path, directory_fd):
    if isinstance(raw_path, int):
        try:
            return Path(os.readlink(f"/proc/self/fd/{raw_path}")).resolve()
        except OSError:
            return None
    if not isinstance(raw_path, (str, bytes, os.PathLike)):
        return None
    candidate = Path(os.fsdecode(raw_path)).expanduser()
    if not candidate.is_absolute():
        if isinstance(directory_fd, int) and directory_fd >= 0:
            try:
                base = Path(os.readlink(f"/proc/self/fd/{directory_fd}"))
            except OSError:
                return None
        else:
            base = Path.cwd()
        candidate = base / candidate
    return candidate.resolve(strict=False)


def _path_identity(path):
    try:
        metadata = path.stat()
    except OSError:
        return None
    return metadata.st_dev, metadata.st_ino


def _paths_overlap(left, right):
    if left == right:
        return True
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _make_runtime_isolation_guard(denied_paths, *, enabled=lambda: True):
    denied = tuple(path.resolve(strict=False) for path in denied_paths)
    denied_inodes = {
        identity for identity in (_path_identity(path) for path in denied) if identity
    }

    def targets_denied(raw_path, directory_fd, *, include_ancestors):
        candidate = _path_from_audit_operand(raw_path, directory_fd)
        if candidate is None:
            return False
        if include_ancestors:
            if any(_paths_overlap(candidate, path) for path in denied):
                return True
        elif candidate in denied:
            return True
        identity = _path_identity(candidate)
        return identity is not None and identity in denied_inodes

    def guard(event, args):
        if not enabled():
            return
        if event == "open" and args:
            operands = ((0, None),)
            # CPython's open audit event omits os.open(dir_fd=...). Blocking
            # ancestor-directory opens prevents this subprocess from acquiring
            # a new descriptor that could address a denied basename indirectly.
            include_ancestors = True
        else:
            operands = _PATH_MUTATOR_OPERANDS.get(event)
            include_ancestors = True
        if operands is None:
            return
        for path_index, dir_fd_index in operands:
            if path_index >= len(args):
                continue
            directory_fd = (
                args[dir_fd_index]
                if dir_fd_index is not None and dir_fd_index < len(args)
                else None
            )
            if targets_denied(
                args[path_index],
                directory_fd,
                include_ancestors=include_ancestors,
            ):
                candidate = _path_from_audit_operand(
                    args[path_index], directory_fd
                )
                raise RuntimeError(
                    f"runtime contract isolation denied {event}: {candidate}"
                )

    return guard


def _install_runtime_isolation_audit_hook():
    """Deny audited live-path opens and mutations before their filesystem call."""

    if not _DENIED_RUNTIME_PATHS:
        return None
    guard = _make_runtime_isolation_guard(_DENIED_RUNTIME_PATHS)
    sys.addaudithook(guard)
    return guard


_install_runtime_isolation_audit_hook()


class RuntimeContractTest(unittest.TestCase):
    def test_installed_council_skill_names_forecast_contract(self):
        for relative_path in (
            "runtime/council-operator-steps.md",
            "runtime/CLAUDE_FORECAST_CONTRACT.md",
        ):
            text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
            normalized = " ".join(text.split())
            self.assertIn(APPROVAL_QUORUM_TEXT, normalized)
            self.assertIn("none returns `BLOCK`", normalized)
            self.assertIn(
                "independent blind-seat requirements remain a separate gate",
                normalized,
            )
        text = (RUNTIME_ROOT / "skills/council/SKILL.md").read_text(encoding="utf-8")
        for required in (
            "council-attempt",
            "shared outcome",
            "forecastState",
            "complete --spec <completion-spec.json> --check-only",
            "grading debt",
        ):
            self.assertIn(required, text)

    def test_documented_council_tool_invocations_name_a_supported_interpreter(self):
        """No documented command may reach a council tool through a PATH-resolved interpreter.

        One rule over two surfaces, because two tests with two rules were incoherent: the
        earlier pair demanded literally ``/usr/bin/python3.11`` in the rendered docs while
        allowing any absolute path elsewhere, so ``/home/trader/pysystemtrade/venv/bin/python``
        -- the exact 3.10 build that caused the incident -- passed one of them on merit.

        The rule enforced is "never PATH-resolved".  PATH resolution is the whole trap: both
        ``python3`` and ``python3.11`` resolve to a 3.10 build on this host, so the property
        that makes an invocation auditable is that it names a path, not that it names a
        blessed one.  Absolute paths are then the operator's problem to keep correct, which
        is what the refusal message is for.

        Two tools are anchored.  ``predictions_report.py`` imports ``council_tools``, so the
        guard turns a wrong interpreter there into an honest exit 2 and the documented
        command is a convenience.  ``blind_seat_kill_criterion.py`` does NOT import it -- and
        cannot be made to, because install.py transforms the already-installed file in place
        rather than rendering it from this repo -- so for that one the documented command is
        the only control there is.

        Every markdown file is scanned rather than a fixed list, and the rendered-doc subset
        is derived from ``install._installed_source_relatives`` rather than restated, so the
        coverage follows the renderer instead of drifting from it.  The match spans a
        trailing backslash so a continuation-line invocation cannot hide, and matches bare
        ``python`` as well as ``python3`` so an unversioned spelling cannot either.
        """
        rendered = {
            relative.as_posix()
            for relative in install._installed_source_relatives(REPOSITORY_ROOT)
            if relative.suffix == ".md"
        }
        self.assertTrue(rendered, "install.py renders no markdown; the derivation broke")

        invocation = re.compile(
            # Exclude the backtick: markdown inline code makes it part of \S, which would
            # otherwise hide an absolute path behind a leading ` and read as PATH-resolved.
            r"(?P<interpreter>[^\s`]*python[\d.]*)"
            # interpreter flags and shell line-continuations may sit between the
            # interpreter and the script; `python3 -B <tool>` and a trailing `\` both occur.
            r"(?:\s+(?:-\w+|\\))*\s+"
            r"(?P<script>\S*(?:blind_seat_kill_criterion|predictions_report)\.py)",
            re.MULTILINE,
        )
        offenders = []
        for path in sorted(REPOSITORY_ROOT.rglob("*.md")):
            if ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            for match in invocation.finditer(text):
                interpreter = match.group("interpreter")
                number = text.count("\n", 0, match.start()) + 1
                if not interpreter.startswith("/"):
                    offenders.append(
                        f"{relative}:{number}: {interpreter} (PATH-resolved)"
                    )
                elif relative in rendered and interpreter != "/usr/bin/python3.11":
                    # Stricter inside the rendered set: those lines become the installed
                    # operator procedure, so they name the one interpreter this host
                    # guarantees rather than merely some absolute path.
                    offenders.append(
                        f"{relative}:{number}: {interpreter} (rendered; want /usr/bin/python3.11)"
                    )
        self.assertEqual(
            offenders,
            [],
            "council tools must never be invoked through a PATH-resolved interpreter, and "
            "rendered docs must name /usr/bin/python3.11. Found: " + "; ".join(offenders),
        )

    def test_runtime_contract_carries_the_proportionality_question(self):
        """The deletion question must survive every install.py rendering.

        install.py rewrites the skill's "## Steps" section and its forecast-contract
        block wholesale from these two files, so a mechanism that is not in them is
        one the next install silently removes.  Assert the sources rather than the
        installed skill: the rendering is verbatim, and the installed file cannot
        carry the mechanism until install.py has run.

        ``QUESTION`` is asserted on a single raw line, case-sensitively, because a
        council's sealed shared outcome resolves it with line-oriented ``grep -F``
        against the installed skill.  Reflowing the paragraph so that string spans
        two lines would break that resolution, so the wrap around it is load-bearing.
        Everything else is whitespace-normalized first, as the sibling test above
        does, so that a semantics-preserving reflow of those paragraphs does not red
        the suite for a reason no sealed outcome depends on.
        """

        QUESTION = "What in this diff would you delete"
        steps = (REPOSITORY_ROOT / "runtime/council-operator-steps.md").read_text(
            encoding="utf-8"
        )
        carrying = [line for line in steps.splitlines() if QUESTION in line]
        self.assertEqual(
            len(carrying), 1, f"{QUESTION!r} must appear on exactly one line"
        )

        # The three parts are ask, report, and reach the prompt checklist.  The
        # report half is what makes the question consequential rather than ritual,
        # so it is asserted as explicitly as the question itself.
        normalized = " ".join(steps.split())
        for required in (
            "not a threshold a change must pass",
            'a bare "nothing" is not an answer',
            "- **Deletion** — one line per lens",
            "never counts toward APPROVE / CONCERN / BLOCK",
            # The report bullet's own scoping clause.  The line above it lives in the
            # ask half, so without this one the bullet could be tightened into
            # "folded into the verdict table and scored" with a green suite.
            "never folded into the verdict table and never scored",
            "say whether it was deleted and, if not, why",
            # The tombstone for the deleted escalation half.  It carries the two
            # council runIds that stop the next reader rebuilding 29 lines and
            # three defects; both lens seats named it the best-value text here.
            "Read those rows before proposing it again",
        ):
            self.assertIn(required, normalized)
        contract = " ".join(
            (REPOSITORY_ROOT / "runtime/council-forecast-contract.md")
            .read_text(encoding="utf-8")
            .split()
        )
        self.assertIn(
            "together with the deletion question the operator steps require of the "
            "three lenses (never the blind seat)",
            contract,
        )

        # Both measurements stay cited by what they measured, not by their dates:
        # "2026-09-12" alone occurs several times, so a bare-date assertion would
        # stay green after deleting the case it is meant to protect.
        for measurement in ("963 production lines became 269", "grew to 94 lines"):
            self.assertIn(measurement, normalized)

    def _installed_criterion(self):
        path = RUNTIME_ROOT / "knowledge/council-eval/blind_seat_kill_criterion.py"
        spec = importlib.util.spec_from_file_location("blind_criterion_runtime", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_blind_tally_allows_non_completion_capture_records(self):
        module = self._installed_criterion()
        kinds = ["council-attempt"]
        if os.environ.get("COUNCIL_RUNTIME_ROOT"):
            kinds.extend(
                (
                    "capture-activation",
                    "capture-initiation",
                    "council-attempt-v2",
                    "council-seats-finished",
                    "capture-invalidation",
                    "council-superseded",
                )
            )
        result = module.tally(
            [(index, {"kind": kind, "runId": f"run-{index}"})
             for index, kind in enumerate(kinds, 1)]
        )
        self.assertEqual(result["legacyBlindRows"], 0)
        self.assertEqual(result["nonCouncilRecords"], len(kinds))

    def test_installed_tally_retires_only_a_verified_superseded_row(self):
        if not os.environ.get("COUNCIL_RUNTIME_ROOT"):
            self.skipTest("installed reader is only asserted against a staged runtime")
        module = self._installed_criterion()
        seat = {
            "role": "generic",
            "required": True,
            "ran": True,
            "changedDecision": True,
            "agreedWithPanel": True,
            "blockedReason": None,
        }
        original = {
            "kind": "council",
            "runId": "run-original",
            "blindSeat": {**seat, "brief": "/tmp/brief-original.md"},
            "forecastState": {"sealed": True, "seats": {"code": "submitted"}},
            "predictions": [{"seat": "code", "probability": 60}],
        }
        duplicate = {
            "kind": "council",
            "runId": "run-original",
            "blindSeat": {**seat, "brief": "/tmp/brief-original.md"},
        }
        record = {
            "schemaVersion": 1,
            "kind": "council-superseded",
            "ts": "2026-08-24T00:00:00Z",
            "supersedes": {"line": 2, "rawLineSha256": "b" * 64},
            "duplicateOf": {"line": 1, "rawLineSha256": "a" * 64},
            "reason": "hand-appended duplicate",
            "approval": {
                "operator": "operator",
                "approvedAt": "2026-08-24T00:00:00Z",
                "reference": "https://github.com/garcia42/ai-council/issues/25",
            },
        }
        rows = [
            (1, original, "a" * 64),
            (2, duplicate, "b" * 64),
            (3, record, "c" * 64),
        ]

        retired = module.tally(rows)

        self.assertEqual(retired["errors"], [])
        self.assertEqual(retired["supersededRows"], 1)
        self.assertEqual(retired["completedRuns"], 1)
        self.assertEqual(retired["changedDecisionRuns"], 1)

        drifted = module.tally(
            [(1, original, "a" * 64), (2, duplicate, "z" * 64), (3, record, "c" * 64)]
        )

        self.assertEqual(drifted["supersededRows"], 0)
        self.assertEqual(drifted["completedRuns"], 2)
        self.assertTrue(
            any("does not match line" in error for error in drifted["errors"]),
            drifted["errors"],
        )

        forged = dict(
            record,
            supersedes={"line": 1, "rawLineSha256": "a" * 64},
            duplicateOf={"line": 2, "rawLineSha256": "b" * 64},
        )
        protected = module.tally(
            [(1, original, "a" * 64), (2, duplicate, "b" * 64), (3, forged, "c" * 64)]
        )

        self.assertEqual(protected["supersededRows"], 0)
        self.assertEqual(protected["completedRuns"], 2)
        self.assertTrue(
            any("forecastState" in error for error in protected["errors"]),
            protected["errors"],
        )

    def test_compatibility_wrapper_preserves_explicit_environment_ledgers(self):
        reporter = RUNTIME_ROOT / "knowledge/council-eval/predictions_report.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "separate-log.jsonl"
            events = root / "separate-events.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "ts": "2026-08-18T00:00:00Z",
                        "kind": "phase0",
                        "predictions": [
                            {
                                "seat": "research",
                                "claim": "A separate venture outcome",
                                "probability": 80,
                                "resolutionDate": "2026-08-19",
                                "resolvedBy": "Inspect separate evidence",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            events.write_text(
                json.dumps(
                    {
                        "key": "2026-08-18T00:00:00Z#0",
                        "ts": "2026-08-18T00:00:00Z",
                        "index": 0,
                        "seat": "research",
                        "claim": "A separate venture outcome",
                        "probability": 80,
                        "came_true": True,
                        "note": "separate evidence",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["PANEL_LOG"] = str(log)
            env["PANEL_RESOLVED"] = str(events)
            result = subprocess.run(
                [sys.executable, str(reporter), "--all"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CALIBRATION (1 resolved)", result.stdout)
        self.assertIn("Brier score:", result.stdout)
        self.assertNotIn("councilRows", result.stdout)

    def test_legacy_mode_rejects_filesystem_aliases_to_council_store(self):
        reporter = RUNTIME_ROOT / "knowledge/council-eval/predictions_report.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            account_home = root / "account-home"
            knowledge = account_home / ".claude/knowledge"
            (knowledge / "council-eval").mkdir(parents=True)
            council_log = knowledge / "futures-panel-log.jsonl"
            council_events = knowledge / "council-eval/predictions_resolved.jsonl"
            council_log.write_text("{}\n", encoding="utf-8")
            council_events.write_text("", encoding="utf-8")
            account = types.SimpleNamespace(pw_dir=str(account_home))
            spec = importlib.util.spec_from_file_location(
                "predictions_report_isolated_alias_test", reporter
            )
            module = importlib.util.module_from_spec(spec)
            with (
                mock.patch("pwd.getpwuid", return_value=account),
                mock.patch("pathlib.Path.home", return_value=account_home),
            ):
                spec.loader.exec_module(module)

            for alias_kind in ("symlink", "hardlink"):
                alias = root / f"{alias_kind}-alias.jsonl"
                if alias_kind == "symlink":
                    alias.symlink_to(council_log)
                else:
                    os.link(council_log, alias)
                events = root / "events.jsonl"
                events.write_text("", encoding="utf-8")
                with self.subTest(alias_kind=alias_kind), self.assertRaisesRegex(
                    SystemExit,
                    "refuses live-knowledge paths or council-store aliases",
                ):
                    module._assert_legacy_store_is_separate(str(alias), str(events))
                alias.unlink()

    def test_rehearsal_audit_guard_denies_live_access_and_path_mutators(self):
        if not _DENIED_RUNTIME_PATHS:
            self.skipTest("runtime isolation paths are supplied by copied-live rehearsal")
        # The actual live path is only opened read-only. If isolation regresses,
        # this assertion fails without mutating live evidence.
        denied = _DENIED_RUNTIME_PATHS[0]
        with self.assertRaisesRegex(RuntimeError, "isolation denied open"):
            denied.open("rb")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "enumerated-live-sentinel"
            sentinel.write_text("must remain unchanged\n", encoding="utf-8")
            # Canary: chmod is viable for this owned file and was allowed by
            # the former open/link-only guard.
            sentinel.chmod(0o640)
            self.assertEqual(sentinel.stat().st_mode & 0o777, 0o640)
            original = sentinel.read_bytes()
            original_mode = sentinel.stat().st_mode
            enabled = True
            guard = _make_runtime_isolation_guard(
                (sentinel,), enabled=lambda: enabled
            )
            sys.addaudithook(guard)
            try:
                checks = (
                    ("open", lambda: sentinel.open("rb")),
                    (
                        "open",
                        lambda: os.open(root, os.O_RDONLY | os.O_DIRECTORY),
                    ),
                    ("os.chmod", lambda: os.chmod(sentinel, 0o777)),
                    (
                        "os.chown",
                        lambda: os.chown(sentinel, os.getuid(), os.getgid()),
                    ),
                    ("os.utime", lambda: os.utime(sentinel, None)),
                    ("os.truncate", lambda: os.truncate(sentinel, 0)),
                    ("os.link", lambda: os.link(sentinel, root / "hardlink")),
                    (
                        "os.symlink",
                        lambda: os.symlink(sentinel, root / "symlink"),
                    ),
                    ("os.rename", lambda: os.rename(sentinel, root / "renamed")),
                    (
                        "os.rename",
                        lambda: os.replace(root / "replacement", sentinel),
                    ),
                    ("os.remove", lambda: os.remove(sentinel)),
                    ("os.remove", lambda: os.unlink(sentinel)),
                    ("os.mkdir", lambda: os.mkdir(sentinel)),
                    ("os.rmdir", lambda: os.rmdir(sentinel)),
                )
                (root / "replacement").write_text(
                    "replacement must survive\n", encoding="utf-8"
                )
                for event, operation in checks:
                    with self.subTest(event=event), self.assertRaisesRegex(
                        RuntimeError, f"isolation denied {event}"
                    ):
                        operation()
                enabled = False
                self.assertEqual(sentinel.read_bytes(), original)
                self.assertEqual(sentinel.stat().st_mode, original_mode)
                self.assertFalse((root / "hardlink").exists())
                self.assertFalse((root / "symlink").exists())
                self.assertFalse((root / "renamed").exists())
                self.assertTrue((root / "replacement").exists())
            finally:
                enabled = False


if __name__ == "__main__":
    unittest.main()
