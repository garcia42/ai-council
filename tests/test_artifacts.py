import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from council_tools.artifacts import (
    ArtifactIntegrityError,
    ArtifactPolicyError,
    ArtifactStore,
    ArtifactWriteError,
    SecretDetectedError,
    compute_git_blob_oid,
    secret_detectors,
    validate_artifact_ref,
    verify_git_blob_oid,
)


class ArtifactStoreTest(unittest.TestCase):
    def setUp(self):
        # /tmp on this host is itself a Git work tree.  The custody contract
        # intentionally refuses roots below one, so use the system's persistent
        # scratch directory for an actually external test root.
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.base = Path(self.temp.name)
        self.root = self.base / "private-artifacts"
        self.store = ArtifactStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def artifact_path(self, ref):
        return self.root.joinpath(*ref["path"].split("/"))

    def test_git_blob_oid_matches_canonical_git_vectors(self):
        self.assertEqual(
            compute_git_blob_oid(b""),
            "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        )
        self.assertEqual(
            compute_git_blob_oid(b"hello\n"),
            "ce013625030ba8dba906f756967f9e9ca394464a",
        )
        sha256_oid = hashlib.sha256(b"blob 6\0hello\n").hexdigest()
        self.assertEqual(
            compute_git_blob_oid(b"hello\n", object_format="sha256"), sha256_oid
        )
        self.assertEqual(verify_git_blob_oid(b"hello\n", sha256_oid), sha256_oid)

    def test_git_blob_verification_rejects_mismatch_and_malformed_oid(self):
        expected = compute_git_blob_oid(b"sealed decision")
        with self.assertRaises(ArtifactIntegrityError) as mismatch:
            verify_git_blob_oid(b"changed decision", expected)
        self.assertEqual(mismatch.exception.incident.code, "git-blob-mismatch")
        for invalid in (expected.upper(), expected[:-1], "g" * 40, 123):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ArtifactPolicyError) as malformed:
                    verify_git_blob_oid(b"sealed decision", invalid)
                self.assertEqual(
                    malformed.exception.incident.code, "invalid-git-blob-oid"
                )
        with self.assertRaises(ArtifactPolicyError) as object_format:
            compute_git_blob_oid(b"sealed decision", object_format="md5")
        self.assertEqual(
            object_format.exception.incident.code,
            "unsupported-git-object-format",
        )

    def test_reference_is_deterministic_content_addressed_and_exact(self):
        content = b"exact visible prompt\x00including bytes\n"
        digest = hashlib.sha256(content).hexdigest()
        expected = {
            "path": f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.bin",
            "sha256": digest,
            "bytes": len(content),
        }
        first = self.store.capture(content)
        second = self.store.capture(content)
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(set(first), {"path", "sha256", "bytes"})
        self.assertEqual(self.artifact_path(first).read_bytes(), content)
        self.assertEqual(self.store.verify(first), expected)

    def test_capture_forces_private_modes_despite_restrictive_umask(self):
        previous = os.umask(0o777)
        try:
            ref = self.store.capture(b"mode test")
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        path = self.artifact_path(ref)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for directory in (path.parent, path.parent.parent, path.parent.parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_capture_fsyncs_a_regular_file_and_directories(self):
        synced_types = []
        real_fsync = os.fsync

        def recording_fsync(fd):
            synced_types.append(stat.S_IFMT(os.fstat(fd).st_mode))
            return real_fsync(fd)

        with mock.patch(
            "council_tools.artifacts.os.fsync", side_effect=recording_fsync
        ):
            self.store.capture(b"durability test")
        self.assertIn(stat.S_IFREG, synced_types)
        self.assertIn(stat.S_IFDIR, synced_types)
        self.assertGreaterEqual(synced_types.count(stat.S_IFDIR), 1)
        self.assertEqual(synced_types[-2:], [stat.S_IFREG, stat.S_IFDIR])

    def test_new_root_fsyncs_its_pinned_parent_directory(self):
        synced_directories = []
        real_fsync = os.fsync
        base_info = self.base.stat()

        def recording_fsync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                synced_directories.append((info.st_dev, info.st_ino))
            return real_fsync(fd)

        with mock.patch(
            "council_tools.artifacts.os.fsync", side_effect=recording_fsync
        ):
            self.store.capture(b"root parent durability")
        self.assertIn((base_info.st_dev, base_info.st_ino), synced_directories)

    def test_capture_rejects_root_renamed_and_replaced_immediately_after_open(self):
        detached = self.base / "detached-root"
        original_open_root = self.store._open_root

        def substitute_root(*, create):
            custody = original_open_root(create=create)
            self.root.rename(detached)
            self.root.mkdir(mode=0o700)
            return custody

        with mock.patch.object(
            self.store, "_open_root", side_effect=substitute_root
        ):
            with self.assertRaises(ArtifactPolicyError) as caught:
                self.store.capture(b"must not reference detached content")
        self.assertEqual(caught.exception.incident.code, "artifact-root-detached")
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(any(detached.rglob("*.bin")))

    def test_fsync_failure_retains_exact_recoverable_artifact_escrow(self):
        content = b"authentic bytes survive a durability failure"
        digest = hashlib.sha256(content).hexdigest()
        artifact = self.root / "sha256" / digest[:2] / digest[2:4] / f"{digest}.bin"
        real_fsync = os.fsync
        failed = False

        def fail_first_file_fsync(fd):
            nonlocal failed
            if stat.S_ISREG(os.fstat(fd).st_mode) and not failed:
                failed = True
                raise OSError("injected artifact fsync failure")
            return real_fsync(fd)

        with mock.patch(
            "council_tools.artifacts.os.fsync", side_effect=fail_first_file_fsync
        ):
            with self.assertRaises(ArtifactWriteError) as caught:
                self.store.capture(content)

        recovery = Path(caught.exception.incident.recovery_path)
        self.assertEqual(recovery.read_bytes(), content)
        self.assertEqual(artifact.read_bytes(), content)
        self.assertEqual(recovery.stat().st_ino, artifact.stat().st_ino)
        self.assertEqual(
            caught.exception.incident.as_dict()["recoveryPath"], str(recovery)
        )
        self.assertIn(str(recovery), str(caught.exception))

    def test_fsync_failure_preserves_replacement_and_authentic_escrow(self):
        content = b"authentic created artifact"
        replacement_content = b"concurrent replacement survives"
        digest = hashlib.sha256(content).hexdigest()
        artifact = self.root / "sha256" / digest[:2] / digest[2:4] / f"{digest}.bin"
        replacement = artifact.with_name("replacement.tmp")
        real_fsync = os.fsync
        regular_fsyncs = 0

        def fail_then_substitute_before_former_cleanup(fd):
            nonlocal regular_fsyncs
            if stat.S_ISREG(os.fstat(fd).st_mode):
                regular_fsyncs += 1
                if regular_fsyncs == 1:
                    raise OSError("injected artifact fsync failure")
                if regular_fsyncs == 2:
                    replacement.write_bytes(replacement_content)
                    os.chmod(replacement, 0o600)
                    os.replace(replacement, artifact)
            return real_fsync(fd)

        with mock.patch(
            "council_tools.artifacts.os.fsync",
            side_effect=fail_then_substitute_before_former_cleanup,
        ):
            with self.assertRaises(ArtifactWriteError) as caught:
                self.store.capture(content)

        recovery = Path(caught.exception.incident.recovery_path)
        self.assertEqual(recovery.read_bytes(), content)
        self.assertEqual(artifact.read_bytes(), replacement_content)
        self.assertNotEqual(recovery.stat().st_ino, artifact.stat().st_ino)
        self.assertEqual(recovery.stat().st_nlink, 1)
        self.assertEqual(artifact.stat().st_nlink, 1)

    def test_concurrent_identical_capture_returns_one_deterministic_ref(self):
        content = b"same concurrent exact artifact" * 1000
        barrier = threading.Barrier(12)
        refs = []
        failures = []

        def capture():
            try:
                barrier.wait()
                refs.append(self.store.capture(content))
            except Exception as exc:  # asserted in the parent thread
                failures.append(exc)

        threads = [threading.Thread(target=capture) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(refs), 12)
        self.assertTrue(all(ref == refs[0] for ref in refs))
        self.assertEqual(self.artifact_path(refs[0]).read_bytes(), content)

    def test_concurrent_distinct_capture_preserves_every_artifact(self):
        contents = [f"artifact-{index}".encode() for index in range(20)]
        refs = []
        failures = []

        def capture(content):
            try:
                refs.append(self.store.capture(content))
            except Exception as exc:  # asserted in the parent thread
                failures.append(exc)

        threads = [threading.Thread(target=capture, args=(item,)) for item in contents]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(failures, [])
        self.assertEqual(len({ref["sha256"] for ref in refs}), len(contents))
        for ref in refs:
            self.store.verify(ref)

    def test_secret_preflight_writes_nothing_and_exposes_no_secret(self):
        secret = b"supply-this-secret-token-984725"
        content = b"visible prompt contains " + secret + b" and must be rejected"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SecretDetectedError) as caught:
                self.store.capture(content, secret_tokens=[secret])
        serialized = json.dumps(caught.exception.incident.as_dict(), sort_keys=True)
        self.assertNotIn(secret.decode(), str(caught.exception))
        self.assertNotIn(secret.decode(), serialized)
        self.assertNotIn(secret.decode(), stderr.getvalue())
        self.assertEqual(
            caught.exception.incident.as_dict(),
            {
                "code": "secret-detected",
                "stage": "preflight",
                "detectors": ["caller-token"],
            },
        )
        self.assertFalse(self.root.exists())
        self.assertEqual(list(self.base.rglob("*")), [])

    def test_high_confidence_builtin_secret_is_rejected_before_root_creation(self):
        secret = b"-----BEGIN OPENSSH PRIVATE KEY-----\nnot-even-written"
        with self.assertRaises(SecretDetectedError) as caught:
            self.store.capture(secret)
        self.assertEqual(caught.exception.incident.detectors, ("private-key",))
        self.assertFalse(self.root.exists())

    def test_aws_secret_detector_accepts_shell_json_and_escaped_json_forms(self):
        secret = b"A" * 40
        positives = (
            b"AWS_SECRET_ACCESS_KEY=" + secret,
            b"aws_secret_access_key = " + secret,
            b"SECRET_KEY: " + secret,
            b'{"AWS_SECRET_ACCESS_KEY":"' + secret + b'"}',
            b'{ "AWS_SECRET_ACCESS_KEY"  :  "' + secret + b'" }',
            b'{\\"AWS_SECRET_ACCESS_KEY\\":\\"' + secret + b'\\"}',
            json.dumps(
                {
                    "evidence": json.dumps(
                        {"AWS_SECRET_ACCESS_KEY": secret.decode("ascii")},
                        separators=(",", ":"),
                    )
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        for content in positives:
            with self.subTest(content=content[:45]):
                self.assertEqual(
                    secret_detectors(content),
                    ("aws-secret-assignment",),
                )

    def test_aws_secret_detector_retains_narrow_near_miss_policy(self):
        secret = b"A" * 40
        near_misses = (
            b"AWS_ACCESS_KEY_ID=" + secret,
            b"AWS_SECRET_ACCESS_KEY=" + secret[:-1],
            b"AWS_SECRET_ACCESS_KEY=" + secret + b"A",
            b'{"AWS_SECRET_ACCESS_KEY":"' + secret[:-1] + b'"}',
            b'{"AWS_SECRET_ACCESS_KEY":"' + secret + b'A"}',
            b'{"AWS_SECRET_ACCESS_KEY":' + secret + b"}",
            b'{"NOT_AWS_SECRET_ACCESS_KEY":"' + secret + b'"}',
            b'"AWS_SECRET_ACCESS_KEY" described without a value',
        )
        for content in near_misses:
            with self.subTest(content=content[:45]):
                self.assertEqual(secret_detectors(content), ())

    def test_empty_caller_secret_token_is_rejected_before_any_write(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            self.store.capture(b"ordinary prompt", secret_tokens=[b""])
        self.assertFalse(self.root.exists())

    def test_mutation_is_detected(self):
        ref = self.store.capture(b"original")
        path = self.artifact_path(ref)
        path.write_bytes(b"mutated!")
        os.chmod(path, 0o600)
        with self.assertRaises(ArtifactIntegrityError) as caught:
            self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "digest-mismatch")

    def test_length_change_is_detected(self):
        ref = self.store.capture(b"original")
        path = self.artifact_path(ref)
        path.write_bytes(b"short")
        os.chmod(path, 0o600)
        with self.assertRaises(ArtifactIntegrityError) as caught:
            self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "length-mismatch")

    def test_deletion_is_detected(self):
        ref = self.store.capture(b"delete me")
        self.artifact_path(ref).unlink()
        with self.assertRaises(ArtifactIntegrityError) as caught:
            self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "artifact-missing")

    def test_wrong_file_mode_is_rejected(self):
        ref = self.store.capture(b"mode mutation")
        os.chmod(self.artifact_path(ref), 0o644)
        with self.assertRaises(ArtifactIntegrityError) as caught:
            self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "wrong-mode")

    def test_wrong_existing_root_mode_is_rejected(self):
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o755)
        with self.assertRaises(ArtifactPolicyError) as caught:
            self.store.capture(b"must not enter public root")
        self.assertEqual(caught.exception.incident.code, "unsafe-directory")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_hard_link_alias_is_rejected(self):
        ref = self.store.capture(b"hard linked")
        path = self.artifact_path(ref)
        alias = self.base / "alias.bin"
        os.link(path, alias)
        with self.assertRaises(ArtifactIntegrityError) as caught:
            self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "hard-link-alias")

    def test_symlink_file_substitution_is_rejected(self):
        ref = self.store.capture(b"symlink substitution")
        path = self.artifact_path(ref)
        alternate = self.base / "alternate.bin"
        alternate.write_bytes(path.read_bytes())
        os.chmod(alternate, 0o600)
        path.unlink()
        path.symlink_to(alternate)
        with self.assertRaises(ArtifactIntegrityError) as caught:
            self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "unsafe-artifact")

    def test_symlink_directory_substitution_is_rejected(self):
        ref = self.store.capture(b"directory substitution")
        path = self.artifact_path(ref)
        digest_directory = path.parent
        alternate = self.base / "alternate-dir"
        digest_directory.rename(alternate)
        digest_directory.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(ArtifactPolicyError) as caught:
            self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "unsafe-directory")

    def test_verify_rejects_identical_content_below_a_replacement_root(self):
        ref = self.store.capture(b"root identity matters in addition to bytes")
        detached = self.base / "detached-verified-root"
        original_open_root = self.store._open_root

        def substitute_root(*, create):
            custody = original_open_root(create=create)
            self.root.rename(detached)
            shutil.copytree(detached, self.root)
            return custody

        with mock.patch.object(
            self.store, "_open_root", side_effect=substitute_root
        ):
            with self.assertRaises(ArtifactPolicyError) as caught:
                self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "artifact-root-detached")
        detached_artifact = detached.joinpath(*ref["path"].split("/"))
        self.assertEqual(
            self.artifact_path(ref).read_bytes(), detached_artifact.read_bytes()
        )

    def test_verify_rejects_replacement_of_the_configured_root_parent(self):
        configured_parent = self.base / "configured-parent"
        configured_parent.mkdir(mode=0o700)
        nested_root = configured_parent / "artifacts"
        nested_store = ArtifactStore(nested_root)
        ref = nested_store.capture(b"the full configured namespace is retained")
        detached_parent = self.base / "detached-root-parent"
        original_open_root = nested_store._open_root

        def substitute_root_parent(*, create):
            custody = original_open_root(create=create)
            configured_parent.rename(detached_parent)
            shutil.copytree(detached_parent, configured_parent)
            return custody

        with mock.patch.object(
            nested_store, "_open_root", side_effect=substitute_root_parent
        ):
            with self.assertRaises(ArtifactPolicyError) as caught:
                nested_store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "artifact-root-detached")

    def test_verify_rejects_content_parent_replaced_after_open(self):
        ref = self.store.capture(b"content directory identity is retained")
        artifact = self.artifact_path(ref)
        detached_parent = self.base / "detached-content-parent"
        original_open_parent = self.store._open_content_parent
        substituted = False

        def substitute_parent(root_fd, digest, *, create):
            nonlocal substituted
            parent_fd = original_open_parent(root_fd, digest, create=create)
            if not substituted:
                substituted = True
                artifact.parent.rename(detached_parent)
                shutil.copytree(detached_parent, artifact.parent)
            return parent_fd

        with mock.patch.object(
            self.store, "_open_content_parent", side_effect=substitute_parent
        ):
            with self.assertRaises(ArtifactIntegrityError) as caught:
                self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "artifact-directory-aliased")

    def test_verify_rejects_leaf_replaced_after_content_authentication(self):
        ref = self.store.capture(b"leaf identity remains pinned")
        artifact = self.artifact_path(ref)
        replacement = artifact.with_name("replacement.bin")
        original_verify = self.store._verify_named_file
        substituted = False

        def substitute_leaf(parent_fd, digest, reference, **kwargs):
            nonlocal substituted
            content = original_verify(parent_fd, digest, reference, **kwargs)
            if not substituted:
                substituted = True
                replacement.write_bytes(artifact.read_bytes())
                os.chmod(replacement, 0o600)
                os.replace(replacement, artifact)
            return content

        with mock.patch.object(
            self.store, "_verify_named_file", side_effect=substitute_leaf
        ):
            with self.assertRaises(ArtifactIntegrityError) as caught:
                self.store.verify(ref)
        self.assertEqual(caught.exception.incident.code, "artifact-aliased")

    def test_preexisting_conflicting_target_is_never_overwritten(self):
        self.store.capture(b"initialize store")
        content = b"expected exact bytes"
        digest = hashlib.sha256(content).hexdigest()
        ref = {
            "path": f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.bin",
            "sha256": digest,
            "bytes": len(content),
        }
        target = self.artifact_path(ref)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (target.parent, target.parent.parent):
            os.chmod(directory, 0o700)
        conflicting = b"X" * len(content)
        target.write_bytes(conflicting)
        os.chmod(target, 0o600)
        with self.assertRaises(ArtifactIntegrityError):
            self.store.capture(content)
        self.assertEqual(target.read_bytes(), conflicting)

    def test_reference_rejects_absolute_traversal_and_extra_fields(self):
        digest = "a" * 64
        invalid = [
            {"path": f"/sha256/aa/aa/{digest}.bin", "sha256": digest, "bytes": 1},
            {"path": f"sha256/aa/../{digest}.bin", "sha256": digest, "bytes": 1},
            {
                "path": f"sha256/aa/aa/{digest}.bin",
                "sha256": digest,
                "bytes": 1,
                "root": str(self.root),
            },
        ]
        for ref in invalid:
            with self.subTest(ref=ref):
                with self.assertRaises(ArtifactPolicyError):
                    validate_artifact_ref(ref)

    def test_reference_path_must_be_derived_from_digest(self):
        digest = "a" * 64
        ref = {
            "path": f"sha256/bb/aa/{digest}.bin",
            "sha256": digest,
            "bytes": 1,
        }
        with self.assertRaises(ArtifactPolicyError):
            validate_artifact_ref(ref)

    def test_root_must_be_absolute_and_outside_git(self):
        with self.assertRaises(ArtifactPolicyError):
            ArtifactStore("relative/artifacts")

        repository = self.base / "repository"
        repository.mkdir()
        (repository / ".git").mkdir()
        inside_store = ArtifactStore(repository / "private")
        with self.assertRaises(ArtifactPolicyError) as caught:
            inside_store.capture(b"must stay out of Git")
        self.assertEqual(caught.exception.incident.code, "root-inside-git")
        self.assertFalse((repository / "private").exists())

    def test_symlink_configured_root_is_rejected(self):
        real = self.base / "real-root"
        real.mkdir(mode=0o700)
        linked = self.base / "linked-root"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ArtifactPolicyError) as caught:
            ArtifactStore(linked).capture(b"do not follow")
        self.assertEqual(caught.exception.incident.code, "unsafe-directory")
        self.assertEqual(list(real.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
