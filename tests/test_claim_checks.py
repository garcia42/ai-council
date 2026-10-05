import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from council_tools import claim_checks
from council_tools.claim_checks import (
    EXIT_FALSE,
    EXIT_TRUE,
    EXIT_UNDETERMINED,
    deadline_end,
)
from council_tools.due_checks import run_check
from council_tools.forecasts import LedgerError, require_gradable_outcome, validate_check

SCRIPT = str(Path(claim_checks.__file__).resolve())
#: In the past, so the reflog entry a check's own fetch writes lands after it.
DEADLINE = "2026-09-20"
#: Either side of the end of 2026-09-20 in New York (04:00Z on the 21st).
BEFORE = "2026-09-20T20:00:00-04:00"
AFTER = "2026-09-21T09:00:00-04:00"


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    env.update(extra)
    return env


def sh(cwd, *args, when=None):
    env = clean_env()
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def check(*argv, env=None):
    result = subprocess.run(
        [sys.executable, SCRIPT, *argv],
        env=env or clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result


class DeadlineTest(unittest.TestCase):
    def test_the_deadline_ends_at_midnight_in_new_york(self):
        self.assertEqual(
            deadline_end(date(2026, 10, 4)),
            datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            deadline_end(date(2026, 12, 4)),
            datetime(2026, 12, 5, 5, 0, tzinfo=timezone.utc),
        )


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        self.clone = root / "clone"
        self.other = root / "other"
        sh(root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        sh(root, "clone", "-q", str(self.remote), str(self.clone))
        sh(self.clone, "commit", "-q", "--allow-empty", "-m", "base", when="2026-09-01T12:00:00Z")
        sh(self.clone, "push", "-q", "origin", "HEAD:main", when="2026-09-01T12:00:00Z")
        sh(root, "clone", "-q", str(self.remote), str(self.other))

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, repo, when):
        # Distinct messages: two empty commits with one parent, date and message
        # are the same commit, which would quietly turn a merge into a no-op.
        # Each commit also carries its own file, so patch identity (git cherry)
        # tells commits apart the way it does real changes.
        self.commits = getattr(self, "commits", 0) + 1
        Path(repo, f"f{self.commits}").write_text(f"{self.commits}\n")
        sh(repo, "add", f"f{self.commits}")
        sh(repo, "commit", "-q", "-m", f"{self.commits} at {when}", when=when)
        return sh(repo, "rev-parse", "HEAD")

    def merged(self, sha, *, env=None, repo=None):
        return check(
            "merged-by", "--claim", f"{sha} is on main", "--deadline", DEADLINE,
            "--repo", str(repo or self.clone), "--sha", sha, "--branch", "main",
            env=env,
        )


class MergedByTest(GitFixture):
    def test_pushed_from_here_before_the_deadline_is_true(self):
        sha = self.commit(self.clone, BEFORE)
        sh(self.clone, "push", "-q", "origin", "HEAD:main", when=BEFORE)
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_TRUE, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith(f"claim: {sha} is on main\n"))

    def test_not_on_the_remote_branch_is_false(self):
        sha = self.commit(self.clone, BEFORE)
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_FALSE, result.stdout + result.stderr)

    def test_committed_after_the_deadline_is_false(self):
        sha = self.commit(self.clone, AFTER)
        sh(self.clone, "push", "-q", "origin", "HEAD:main", when=AFTER)
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_FALSE, result.stdout + result.stderr)

    def test_a_late_fast_forward_of_an_early_commit_is_left_for_a_human(self):
        # The case a commit date gets wrong: written before the deadline,
        # pushed after it, from a machine whose reflog this clone cannot see.
        sha = self.commit(self.other, BEFORE)
        sh(self.other, "push", "-q", "origin", "HEAD:main", when=AFTER)
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_UNDETERMINED, result.stdout + result.stderr)
        self.assertIn("nothing recorded here", result.stdout)

    def test_a_local_merge_before_the_deadline_pushed_after_is_not_true(self):
        # refs/heads/main moved before the deadline, but the remote only got it
        # after: the local branch reflog is not evidence about the remote.
        sha = self.commit(self.clone, BEFORE)
        sh(self.clone, "push", "-q", "origin", "HEAD:main", when=AFTER)
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_UNDETERMINED, result.stdout + result.stderr)

    def test_an_equivalent_patch_under_another_id_is_left_for_a_human(self):
        # The reviewed commit stays on a topic branch in the author's clone; a
        # rebase lands the same patch on main under another id.
        sh(self.clone, "checkout", "-q", "-b", "topic")
        sha = self.commit(self.clone, BEFORE)
        sh(self.clone, "checkout", "-q", "main")
        self.commit(self.clone, BEFORE)
        sh(self.clone, "cherry-pick", sha, when=BEFORE)
        sh(self.clone, "push", "-q", "origin", "HEAD:main", when=BEFORE)
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_UNDETERMINED, result.stdout + result.stderr)
        self.assertIn("equivalent patch", result.stdout)

    def test_a_fetch_seen_before_the_deadline_counts(self):
        sha = self.commit(self.other, BEFORE)
        sh(self.other, "push", "-q", "origin", "HEAD:main", when=BEFORE)
        sh(self.clone, "fetch", "-q", "origin", when=BEFORE)
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_TRUE, result.stdout + result.stderr)

    def test_a_merge_commit_after_the_deadline_is_false(self):
        sh(self.other, "checkout", "-q", "-b", "topic")
        sha = self.commit(self.other, BEFORE)
        sh(self.other, "checkout", "-q", "main")
        self.commit(self.other, BEFORE)
        sh(self.other, "merge", "-q", "--no-ff", "-m", "merge", "topic", when=AFTER)
        sh(self.other, "push", "-q", "origin", "HEAD:main", when=AFTER)
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_FALSE, result.stdout + result.stderr)
        self.assertIn("committed after the deadline", result.stdout)

    def test_a_leaked_git_dir_does_not_redirect_the_check(self):
        # A check launched from inside a git hook inherits GIT_DIR; every child
        # git would then read the wrong repository.
        sha = self.commit(self.clone, BEFORE)
        sh(self.clone, "push", "-q", "origin", "HEAD:main", when=BEFORE)
        decoy = Path(self.tmp.name) / "decoy"
        sh(Path(self.tmp.name), "init", "-q", str(decoy))
        env = clean_env(GIT_DIR=str(decoy / ".git"), GIT_WORK_TREE=str(decoy))
        result = self.merged(sha, env=env)
        self.assertEqual(result.returncode, EXIT_TRUE, result.stdout + result.stderr)

    def test_a_short_sha_in_a_check_is_refused_not_guessed(self):
        sha = self.commit(self.clone, BEFORE)
        sh(self.clone, "push", "-q", "origin", "HEAD:main", when=BEFORE)
        result = self.merged(sha[:10])
        self.assertEqual(result.returncode, EXIT_UNDETERMINED, result.stdout + result.stderr)

    def test_an_unreachable_remote_is_undetermined(self):
        sha = self.commit(self.clone, BEFORE)
        sh(self.clone, "remote", "set-url", "origin", str(Path(self.tmp.name) / "gone.git"))
        result = self.merged(sha)
        self.assertEqual(result.returncode, EXIT_UNDETERMINED, result.stdout + result.stderr)


class ReleaseActiveByTest(GitFixture):
    def pointer(self, name, text, when):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        stamp = datetime.fromisoformat(when).timestamp()
        os.utime(path, (stamp, stamp))
        return str(path)

    def active(self, sha, *pointers):
        argv = ["release-active-by", "--claim", "active", "--deadline", DEADLINE,
                "--repo", str(self.clone), "--sha", sha]
        for item in pointers:
            argv += ["--pointer", item]
        return check(*argv)

    def test_unchanged_pointers_naming_a_descendant_are_true(self):
        sha = self.commit(self.clone, BEFORE)
        later = self.commit(self.clone, BEFORE)
        digest = "ab" * 32  # a SHA-256 digest is not a commit id
        shim = self.pointer("shim.py", f'EXPECTED_COMMIT = "{later}"\nDIGEST = "{digest}"\n', BEFORE)
        unit = self.pointer("unit.service", f"ExecStart=/r/releases/{sha}/bin/x\n", BEFORE)
        result = self.active(sha, shim, unit)
        self.assertEqual(result.returncode, EXIT_TRUE, result.stdout + result.stderr)

    def test_an_unchanged_pointer_without_the_sha_is_false(self):
        old = sh(self.clone, "rev-parse", "HEAD")
        sha = self.commit(self.clone, BEFORE)
        shim = self.pointer("shim.py", f'EXPECTED_COMMIT = "{old}"\n', BEFORE)
        result = self.active(sha, shim)
        self.assertEqual(result.returncode, EXIT_FALSE, result.stdout + result.stderr)

    def test_a_pointer_rewritten_after_the_deadline_is_silent_about_it(self):
        sha = self.commit(self.clone, BEFORE)
        shim = self.pointer("shim.py", f'EXPECTED_COMMIT = "{sha}"\n', AFTER)
        result = self.active(sha, shim)
        self.assertEqual(result.returncode, EXIT_UNDETERMINED, result.stdout + result.stderr)

    def test_a_pointer_naming_no_commit_is_undetermined(self):
        sha = self.commit(self.clone, BEFORE)
        shim = self.pointer("shim.py", f'DIGEST = "{"ab" * 32}"\n', BEFORE)
        result = self.active(sha, shim)
        self.assertEqual(result.returncode, EXIT_UNDETERMINED, result.stdout + result.stderr)

    def test_a_symlink_pointer_is_read_by_its_target(self):
        sha = self.commit(self.clone, BEFORE)
        release = Path(self.tmp.name) / "releases" / sha
        release.mkdir(parents=True)
        link = Path(self.tmp.name) / "current"
        link.symlink_to(release)
        stamp = datetime.fromisoformat(BEFORE).timestamp()
        os.utime(link, (stamp, stamp), follow_symlinks=False)
        result = self.active(sha, str(link))
        self.assertEqual(result.returncode, EXIT_TRUE, result.stdout + result.stderr)


class RoundSealedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "rounds.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, *rows):
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def sealed(self, *extra, deadline=DEADLINE):
        return check(
            "round-sealed", "--claim", "round r1 seals", "--deadline", deadline,
            "--jsonl-file", str(self.ledger), "--match", "round.id=r1",
            "--require-prefix", "status=SEALED", *extra,
        )

    def test_sealed_before_the_deadline_is_true_and_after_is_false(self):
        self.rows({"round": {"id": "r1"}, "status": "SEALED_OK", "at": BEFORE})
        self.assertEqual(self.sealed("--at-field", "at").returncode, EXIT_TRUE)
        self.rows({"round": {"id": "r1"}, "status": "SEALED_OK", "at": AFTER})
        self.assertEqual(self.sealed("--at-field", "at").returncode, EXIT_FALSE)

    def test_not_sealed_is_false(self):
        self.rows({"round": {"id": "r1"}, "status": "OPEN"}, {"round": {"id": "r2"}, "status": "SEALED"})
        self.assertEqual(self.sealed().returncode, EXIT_FALSE)

    def test_a_selector_matching_nothing_is_undetermined_not_false(self):
        self.rows({"round": {"id": "r2"}, "status": "OPEN"})
        self.assertEqual(self.sealed().returncode, EXIT_UNDETERMINED)

    def test_sealed_now_without_a_timestamp_is_never_graded_true(self):
        # "Sealed when checked" says nothing about the deadline, however soon after.
        self.rows({"round": {"id": "r1"}, "status": "SEALED"})
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        self.assertEqual(self.sealed(deadline=yesterday).returncode, EXIT_UNDETERMINED)

    def test_an_unreadable_source_is_undetermined(self):
        self.assertEqual(self.sealed().returncode, EXIT_UNDETERMINED)

    def test_without_a_requirement_it_refuses_to_run(self):
        self.rows({"round": {"id": "r1"}, "status": "SEALED"})
        result = check(
            "round-sealed", "--claim", "c", "--deadline", DEADLINE,
            "--jsonl-file", str(self.ledger), "--match", "round.id=r1",
        )
        self.assertNotIn(result.returncode, (EXIT_TRUE, EXIT_FALSE))

    def test_json_document_items_path(self):
        path = Path(self.tmp.name) / "api.json"
        path.write_text(json.dumps({"rounds": [{"id": "r1", "status": "SEALED", "at": BEFORE}]}))
        result = check(
            "round-sealed", "--claim", "c", "--deadline", DEADLINE, "--json-file", str(path),
            "--items", "rounds", "--match", "id=r1", "--require", "status=SEALED",
            "--at-field", "at",
        )
        self.assertEqual(result.returncode, EXIT_TRUE, result.stdout + result.stderr)


class SpecTest(GitFixture):
    def test_spec_expands_the_sha_and_satisfies_the_attempt_rule(self):
        sha = self.commit(self.clone, BEFORE)
        sh(self.clone, "push", "-q", "origin", "HEAD:main", when=BEFORE)
        claim = f"{sha[:9]} is merged to main by {DEADLINE}"
        made = check(
            "spec", "--python", sys.executable, "--script", SCRIPT, "merged-by",
            "--claim", claim, "--deadline", DEADLINE, "--repo", str(self.clone),
            "--sha", sha[:9], "--branch", "main",
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        spec = json.loads(made.stdout)
        self.assertIn(sha, spec["argv"])
        validate_check(spec)
        require_gradable_outcome({"claim": claim, "workstream": "council-tools",
                                  "resolutionDate": DEADLINE, "check": spec})
        # End to end, the way resolve-due runs it: clean environment, verdict by exit code.
        self.assertEqual(run_check(spec)["verdict"], "true")

    def test_spec_names_the_main_clone_not_a_worktree(self):
        sha = self.commit(self.clone, BEFORE)
        worktree = Path(self.tmp.name) / "session"
        sh(self.clone, "worktree", "add", "-q", "-b", "session", str(worktree))
        made = check(
            "spec", "merged-by", "--claim", "c", "--deadline", DEADLINE,
            "--repo", str(worktree), "--sha", sha, "--branch", "main",
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        argv = json.loads(made.stdout)["argv"]
        self.assertEqual(argv[argv.index("--repo") + 1], str(self.clone.resolve()))

    def test_spec_defaults_name_the_installed_runtime(self):
        made = check(
            "spec", "round-sealed", "--claim", "c", "--deadline", DEADLINE,
            "--jsonl-file", "/x.jsonl", "--match", "a=b", "--require", "s=SEALED",
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        argv = json.loads(made.stdout)["argv"]
        self.assertEqual(argv[:2], [claim_checks.CHECK_PYTHON, claim_checks.INSTALLED_SCRIPT])

    def test_spec_refuses_arguments_the_check_would_reject(self):
        made = check("spec", "merged-by", "--claim", "c", "--deadline", "soon")
        self.assertNotEqual(made.returncode, 0)


class GradableOutcomeRuleTest(unittest.TestCase):
    CHECK = {"type": "command", "argv": ["/bin/true", "--claim", "c"], "cwd": "/", "timeoutSeconds": 5}
    LIBRARY = {**CHECK, "argv": ["/usr/bin/python3", "/x/claim_checks.py", "merged-by",
                                 "--claim", "c", "--deadline", "2026-10-04"]}

    def test_a_library_check_must_use_the_outcome_deadline(self):
        base = {"claim": "c", "workstream": "tandr", "check": self.LIBRARY}
        require_gradable_outcome({**base, "resolutionDate": "2026-10-04"})
        with self.assertRaisesRegex(LedgerError, "--deadline equal to"):
            require_gradable_outcome({**base, "resolutionDate": "2026-10-03"})

    def test_null_is_not_a_value(self):
        # make_attempt drops None, so a null would otherwise write a row with neither.
        for outcome in (
            {"claim": "c", "workstream": None, "noCheckReason": "r"},
            {"claim": "c", "workstream": "tandr", "noCheckReason": None},
            {"claim": "c", "workstream": "tandr", "check": None, "noCheckReason": None},
            {"claim": "c", "workstream": "nobody", "noCheckReason": "r"},
            {"claim": "c", "workstream": "tandr", "noCheckReason": "  "},
        ):
            with self.subTest(outcome), self.assertRaises(LedgerError):
                require_gradable_outcome(outcome)

    def test_the_claim_is_compared_as_written(self):
        with self.assertRaisesRegex(LedgerError, "carry the claim"):
            require_gradable_outcome({"claim": "c ", "workstream": "tandr", "check": self.CHECK})

    def test_accepts_a_check_carrying_the_claim_or_a_reason(self):
        require_gradable_outcome({"claim": "c", "workstream": "tandr", "check": self.CHECK})
        require_gradable_outcome({"claim": "c", "workstream": "tandr", "noCheckReason": "needs judgement"})

    def test_refusals(self):
        cases = {
            "workstream": {"claim": "c", "check": self.CHECK},
            "exactly one": {"claim": "c", "workstream": "tandr"},
            "exactly one ": {"claim": "c", "workstream": "tandr", "check": self.CHECK,
                             "noCheckReason": "both"},
            "carry the claim": {"claim": "other", "workstream": "tandr", "check": self.CHECK},
            "carry the claim ": {"claim": "c", "workstream": "tandr", "check": {
                **self.CHECK, "argv": ["/bin/true", "--claim", "c", "--claim", "c"]}},
            "carry the claim  ": {"claim": "c", "workstream": "tandr", "check": {
                **self.CHECK, "argv": ["/bin/true", "c"]}},
        }
        for message, outcome in cases.items():
            with self.subTest(message), self.assertRaisesRegex(LedgerError, message.strip()):
                require_gradable_outcome(outcome)


if __name__ == "__main__":
    unittest.main()
