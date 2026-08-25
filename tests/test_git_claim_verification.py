"""Proof that verification checks every part of what a claim object says.

The positive cases build a real claim through the merged write path and verify
it, so the two halves are shown to agree rather than assumed to. The negative
cases construct the hostile object with plain Git against the same repository:
a mismatch has to be a real object for the verifier to be genuinely reading it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from council_tools.git_claim_materialization import materialize_claim_object
from council_tools.git_claim_request import ClaimRequest
from council_tools.git_claim_verification import (
    CHECK_COMMIT_IDENTITY,
    CHECK_COMMIT_MESSAGE,
    CHECK_COMMIT_SHAPE,
    CHECK_OBJECT_TYPE,
    CHECK_PAYLOAD,
    CHECK_TREE_SHAPE,
    ClaimVerificationError,
    verify_claim_object,
)
from council_tools.git_object_id import Sha1ObjectId
from council_tools.git_process_contract import GitCommandResult
from council_tools.git_process_executor import GitSubprocessExecutor
from council_tools.git_repository_lease import acquire_lease, remove_lease

GIT = "/usr/bin/git"

REQUEST = ClaimRequest(
    repository="garcia42/ai-council",
    issue_number=59,
    contract_sha256="a" * 64,
    holder="session-a",
    issued_at="@0 +0000",
)

requires_git = unittest.skipUnless(
    os.path.isfile(GIT) and os.path.isdir("/proc/self/fd"),
    "needs Linux with procfs and git",
)


@requires_git
class VerificationTestCase(unittest.TestCase):
    """One real repository per test, written through the real write path."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="claim-verify-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lease = acquire_lease(self.root, "claim.git")
        self.addCleanup(self._drop)
        self.executor = GitSubprocessExecutor(deadline_seconds=30)
        self.commit = materialize_claim_object(REQUEST, self.lease, self.executor)
        self.repository = os.path.join(self.root, "claim.git")

    def _drop(self):
        try:
            remove_lease(self.lease)
        except Exception:
            pass

    def git(self, *args, stdin=None):
        """Plain Git against the same repository, for building hostile objects."""

        result = subprocess.run(
            [GIT, f"--git-dir={self.repository}", *args],
            input=stdin,
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_NAME": "other",
                "GIT_AUTHOR_EMAIL": "other@claims.invalid",
                "GIT_AUTHOR_DATE": "@0 +0000",
                "GIT_COMMITTER_NAME": "other",
                "GIT_COMMITTER_EMAIL": "other@claims.invalid",
                "GIT_COMMITTER_DATE": "@0 +0000",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def verify(self, commit=None, request=None):
        return verify_claim_object(
            commit or self.commit, request or REQUEST, self.lease, self.executor
        )

    def assertRefused(self, code, check, commit=None, request=None):
        with self.assertRaises(ClaimVerificationError) as caught:
            self.verify(commit, request)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.check, check)


class PositiveTests(VerificationTestCase):
    def test_an_object_written_by_the_write_path_verifies(self) -> None:
        self.assertIs(self.verify(), True)

    def test_it_verifies_for_several_distinct_requests(self) -> None:
        for issued in ("@0 +0000", "@1724540000 -0500", "@1724540000 +1400"):
            with self.subTest(issued=issued):
                root = tempfile.mkdtemp(prefix="claim-verify-alt-")
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                lease = acquire_lease(root, "claim.git")
                try:
                    request = ClaimRequest(
                        repository="garcia42/other",
                        issue_number=7,
                        contract_sha256="b" * 64,
                        holder="session-b",
                        issued_at=issued,
                    )
                    commit = materialize_claim_object(request, lease, self.executor)
                    self.assertIs(
                        verify_claim_object(commit, request, lease, self.executor), True
                    )
                finally:
                    remove_lease(lease)


class ObjectShapeTests(VerificationTestCase):
    def test_a_non_commit_object_is_refused_before_being_read_as_one(self) -> None:
        blob = Sha1ObjectId(self.git("hash-object", "-w", "--stdin", stdin=b"x"))
        self.assertRefused("not-a-commit", CHECK_OBJECT_TYPE, commit=blob)

    def test_a_commit_with_a_parent_is_refused(self) -> None:
        tree = self.git("rev-parse", f"{self.commit.wire_text}^{{tree}}")
        child = Sha1ObjectId(
            self.git("commit-tree", tree, "-p", self.commit.wire_text,
                     stdin=REQUEST.message_bytes())
        )
        self.assertRefused("commit-has-parent", CHECK_COMMIT_SHAPE, commit=child)

    def test_a_different_message_is_refused(self) -> None:
        tree = self.git("rev-parse", f"{self.commit.wire_text}^{{tree}}")
        other = Sha1ObjectId(self.git("commit-tree", tree, stdin=b"different\n"))
        # Verified against the holder plain Git wrote, so the identity check
        # passes and this reaches the message check rather than stopping short.
        self.assertRefused(
            "message-mismatch",
            CHECK_COMMIT_MESSAGE,
            commit=other,
            request=_other_holder_request(),
        )


class DuplicateHeaderTests(VerificationTestCase):
    """A repeated header must not be collapsed away.

    Git writes a commit whose headers are read back verbatim, so an object can
    carry two ``tree`` or two ``author`` lines. A parser keeping one value per
    key would silently drop the second and report a shape it never saw.
    """

    def _raw_commit(self, body: bytes) -> Sha1ObjectId:
        return Sha1ObjectId(
            self.git("hash-object", "-t", "commit", "-w", "--stdin", stdin=body)
        )

    def test_a_commit_with_two_trees_is_refused(self) -> None:
        tree = self.git("rev-parse", f"{self.commit.wire_text}^{{tree}}")
        body = (
            f"tree {tree}\ntree {tree}\n"
            f"author session-a <session-a@claims.invalid> 0 +0000\n"
            f"committer session-a <session-a@claims.invalid> 0 +0000\n\n"
        ).encode() + REQUEST.message_bytes()
        self.assertRefused(
            "not-exactly-one-tree", CHECK_COMMIT_SHAPE, commit=self._raw_commit(body)
        )

    def test_a_commit_with_two_authors_is_refused(self) -> None:
        tree = self.git("rev-parse", f"{self.commit.wire_text}^{{tree}}")
        author = "author session-a <session-a@claims.invalid> 0 +0000"
        body = (
            f"tree {tree}\n{author}\n{author}\n"
            f"committer session-a <session-a@claims.invalid> 0 +0000\n\n"
        ).encode() + REQUEST.message_bytes()
        self.assertRefused(
            "not-exactly-one-author",
            CHECK_COMMIT_IDENTITY,
            commit=self._raw_commit(body),
        )


class IdentityTests(VerificationTestCase):
    def test_a_different_holder_is_refused(self) -> None:
        # Written by plain Git, whose identity env names "other".
        tree = self.git("rev-parse", f"{self.commit.wire_text}^{{tree}}")
        other = Sha1ObjectId(
            self.git("commit-tree", tree, stdin=REQUEST.message_bytes())
        )
        self.assertRefused("author-mismatch", CHECK_COMMIT_IDENTITY, commit=other)

    def test_a_request_naming_a_different_holder_is_refused(self) -> None:
        self.assertRefused(
            "author-mismatch",
            CHECK_COMMIT_IDENTITY,
            request=ClaimRequest(**{**_fields(REQUEST), "holder": "session-z"}),
        )

    def test_a_request_naming_a_different_issuance_time_is_refused(self) -> None:
        self.assertRefused(
            "author-mismatch",
            CHECK_COMMIT_IDENTITY,
            request=ClaimRequest(**{**_fields(REQUEST), "issued_at": "@1 +0000"}),
        )


class TreeTests(VerificationTestCase):
    def _commit_with_tree(self, entries: bytes) -> Sha1ObjectId:
        tree = self.git("mktree", stdin=entries)
        return Sha1ObjectId(
            self.git("commit-tree", tree, stdin=REQUEST.message_bytes())
        )

    def _payload_blob(self) -> str:
        return self.git("rev-parse", f"{self.commit.wire_text}:claim.json")

    def test_an_extra_entry_is_refused(self) -> None:
        blob = self._payload_blob()
        second = self.git("hash-object", "-w", "--stdin", stdin=b"extra")
        commit = self._commit_with_tree(
            f"100644 blob {blob}\tclaim.json\n100644 blob {second}\tother.json\n".encode()
        )
        # Identity differs too (plain Git wrote it), so assert the tree check is
        # reached by using a commit this test builds with the right identity.
        with self.assertRaises(ClaimVerificationError) as caught:
            self.verify(commit=commit, request=_other_holder_request())
        self.assertEqual(caught.exception.code, "not-exactly-one-entry")
        self.assertEqual(caught.exception.check, CHECK_TREE_SHAPE)

    def test_a_wrong_entry_name_is_refused(self) -> None:
        blob = self._payload_blob()
        commit = self._commit_with_tree(f"100644 blob {blob}\tother.json\n".encode())
        with self.assertRaises(ClaimVerificationError) as caught:
            self.verify(commit=commit, request=_other_holder_request())
        self.assertEqual(caught.exception.code, "entry-name-mismatch")

    def test_a_wrong_entry_mode_is_refused(self) -> None:
        blob = self._payload_blob()
        commit = self._commit_with_tree(f"100755 blob {blob}\tclaim.json\n".encode())
        with self.assertRaises(ClaimVerificationError) as caught:
            self.verify(commit=commit, request=_other_holder_request())
        self.assertEqual(caught.exception.code, "entry-mode-mismatch")

    def test_a_subtree_entry_is_refused(self) -> None:
        """A subtree under the claim name is refused at the tree check.

        It reports ``entry-mode-mismatch`` rather than ``entry-type-mismatch``,
        and that is not a gap: Git ties the two, so a real ``tree`` entry
        necessarily carries mode ``040000`` and the mode check is reached first.
        There is no object this test could build that isolates the type check,
        and asserting the code a real object produces is more honest than
        reordering the checks to make a nicer-looking assertion true.
        """

        inner = self.git("mktree", stdin=b"")
        commit = self._commit_with_tree(f"040000 tree {inner}\tclaim.json\n".encode())
        with self.assertRaises(ClaimVerificationError) as caught:
            self.verify(commit=commit, request=_other_holder_request())
        self.assertEqual(caught.exception.code, "entry-mode-mismatch")
        self.assertEqual(caught.exception.check, CHECK_TREE_SHAPE)

    def test_the_entry_type_check_refuses_a_type_it_can_be_shown(self) -> None:
        """The type check itself, exercised where a real object cannot reach it."""

        from council_tools.git_claim_verification import _verify_tree

        with self.assertRaises(ClaimVerificationError) as caught:
            _verify_tree(b"100644 tree " + b"a" * 40 + b"\tclaim.json\x00")
        self.assertEqual(caught.exception.code, "entry-type-mismatch")


class PayloadTests(VerificationTestCase):
    def test_a_byte_differing_payload_is_refused(self) -> None:
        """Parses equal to the verified request; differs only in bytes.

        Built from the *same* request the verification runs against, so it is
        genuinely parse-equal. A verifier comparing parsed fields would accept
        it, and it is the bytes that were hashed.
        """

        import json

        request = _other_holder_request()
        parsed = json.loads(request.payload_bytes())
        # Reversed key order and padded separators: different bytes, same value.
        reordered = json.dumps(
            {key: parsed[key] for key in reversed(list(parsed))},
            separators=(", ", ": "),
        ).encode("utf-8")
        self.assertEqual(json.loads(reordered), parsed)
        self.assertNotEqual(reordered, request.payload_bytes())

        blob = self.git("hash-object", "-w", "--stdin", stdin=reordered)
        tree = self.git("mktree", stdin=f"100644 blob {blob}\tclaim.json\n".encode())
        commit = Sha1ObjectId(
            self.git("commit-tree", tree, stdin=request.message_bytes())
        )
        with self.assertRaises(ClaimVerificationError) as caught:
            self.verify(commit=commit, request=request)
        self.assertEqual(caught.exception.code, "payload-mismatch")
        self.assertEqual(caught.exception.check, CHECK_PAYLOAD)

    def test_a_different_request_is_refused(self) -> None:
        other = ClaimRequest(**{**_fields(REQUEST), "issue_number": 60})
        with self.assertRaises(ClaimVerificationError):
            self.verify(request=other)


class ReadFailureTests(VerificationTestCase):
    class _Failing:
        def __init__(self, at, result):
            self.at = at
            self.result = result
            self.calls = 0
            self.real = GitSubprocessExecutor(deadline_seconds=30)

        def execute(self, command):
            index = self.calls
            self.calls += 1
            if index == self.at:
                return self.result
            return self.real.execute(command)

    def test_a_local_failure_on_any_read_is_refused(self) -> None:
        for at in range(4):
            with self.subTest(read=at):
                executor = self._Failing(
                    at, GitCommandResult(exit_status=None, local_failure="deadline-expired")
                )
                with self.assertRaises(ClaimVerificationError) as caught:
                    verify_claim_object(self.commit, REQUEST, self.lease, executor)
                self.assertEqual(caught.exception.code, "local-failure")

    def test_a_nonzero_exit_on_any_read_is_refused(self) -> None:
        for at in range(4):
            with self.subTest(read=at):
                executor = self._Failing(at, GitCommandResult(exit_status=128))
                with self.assertRaises(ClaimVerificationError) as caught:
                    verify_claim_object(self.commit, REQUEST, self.lease, executor)
                self.assertEqual(caught.exception.code, "nonzero-exit")


class ArgumentRefusalTests(VerificationTestCase):
    def test_wrong_typed_arguments_are_refused_before_anything_runs(self) -> None:
        cases = [
            (("not-an-id", REQUEST, self.lease, self.executor), "not-an-object-id"),
            ((self.commit, "not-a-request", self.lease, self.executor), "not-a-claim-request"),
            ((self.commit, REQUEST, "not-a-lease", self.executor), "not-a-lease"),
            ((self.commit, REQUEST, self.lease, object()), "not-an-executor"),
        ]
        for args, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ClaimVerificationError) as caught:
                    verify_claim_object(*args)
                self.assertEqual(caught.exception.code, code)


class NonOwnershipTests(VerificationTestCase):
    def test_no_ref_is_ever_read(self) -> None:
        seen = []
        real = self.executor

        class Recording:
            def execute(self, command):
                seen.append(command.invocation.subcommand)
                return real.execute(command)

        verify_claim_object(self.commit, REQUEST, self.lease, Recording())
        for subcommand in seen:
            self.assertNotIn(
                subcommand,
                {"show-ref", "for-each-ref", "rev-list", "update-ref", "symbolic-ref"},
            )


def _fields(request: ClaimRequest) -> dict:
    return {
        "repository": request.repository,
        "issue_number": request.issue_number,
        "contract_sha256": request.contract_sha256,
        "holder": request.holder,
        "issued_at": request.issued_at,
    }


def _other_holder_request() -> ClaimRequest:
    """A request matching the identity plain Git writes in these tests.

    Lets a tree- or payload-shaped test reach its own check rather than being
    stopped earlier by the identity check, which has its own tests.
    """

    return ClaimRequest(**{**_fields(REQUEST), "holder": "other"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
