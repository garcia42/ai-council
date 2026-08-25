"""Proof that a claim request validates exactly and renders deterministically."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from council_tools.git_claim_request import (
    CLAIM_PAYLOAD_KEYS,
    CLAIM_PAYLOAD_SCHEMA_VERSION,
    HOLDER_EMAIL_DOMAIN,
    MAX_ISSUE_NUMBER,
    MAX_TEXT_LENGTH,
    ClaimRequest,
    ClaimRequestError,
)
from council_tools.git_local_write_operations import (
    CreateZeroParentCommit,
    WriteCanonicalBlob,
)
from council_tools.git_object_id import Sha1ObjectId

VALID = {
    "repository": "garcia42/ai-council",
    "issue_number": 49,
    "contract_sha256": "c" * 64,
    "holder": "session-a",
    "issued_at": "@0 +0000",
}


def request(**overrides) -> ClaimRequest:
    return ClaimRequest(**{**VALID, **overrides})


class ValidationTests(unittest.TestCase):
    def test_a_valid_request_is_accepted(self) -> None:
        self.assertEqual(request().issue_number, 49)

    def test_every_text_field_rejects_its_invalid_forms(self) -> None:
        cases = {
            "invalid-type": [None, 1, b"x", ["x"]],
            "empty": [""],
            "too-long": ["x" * (MAX_TEXT_LENGTH + 1)],
            "forbidden-character": ["a\x00b", "a\nb", "a\rb", "a<b", "a>b"],
            "surrounding-whitespace": [" a", "a ", "\ta"],
        }
        for field in ("repository", "holder"):
            for code, values in cases.items():
                for value in values:
                    with self.subTest(field=field, code=code, value=value):
                        with self.assertRaises(ClaimRequestError) as caught:
                            request(**{field: value})
                        self.assertEqual(caught.exception.code, code)
                        self.assertEqual(caught.exception.field, field)

    def test_the_issue_number_rejects_non_integers_and_out_of_range(self) -> None:
        for value in (None, "49", 4.0, b"49"):
            with self.subTest(value=value):
                with self.assertRaises(ClaimRequestError) as caught:
                    request(issue_number=value)
                self.assertEqual(caught.exception.code, "invalid-type")
        # bool is an int subclass and must not pass as an issue number.
        with self.assertRaises(ClaimRequestError) as caught:
            request(issue_number=True)
        self.assertEqual(caught.exception.code, "invalid-type")
        for value in (0, -1, MAX_ISSUE_NUMBER + 1):
            with self.subTest(value=value):
                with self.assertRaises(ClaimRequestError) as caught:
                    request(issue_number=value)
                self.assertEqual(caught.exception.code, "out-of-range")

    def test_the_contract_digest_must_be_exact_lowercase_hex(self) -> None:
        for value in ("C" * 64, "c" * 63, "c" * 65, "g" * 64, "c" * 40):
            with self.subTest(value=value[:8]):
                with self.assertRaises(ClaimRequestError) as caught:
                    request(contract_sha256=value)
                self.assertEqual(caught.exception.code, "invalid-digest")

    def test_the_issuance_time_must_carry_the_form_the_builder_accepts(self) -> None:
        # A bare "0 +0000" is rejected by the pinned binary; the leading "@" is
        # not decoration, and validating it here is what stops a request that
        # passes this module failing at the commit builder instead.
        for value in ("0 +0000", "@0", "@0 0000", "@0 +000", "@x +0000", "@0 +00000"):
            with self.subTest(value=value):
                with self.assertRaises(ClaimRequestError) as caught:
                    request(issued_at=value)
                self.assertEqual(caught.exception.code, "invalid-issued-at")
        for value in ("@0 +0000", "@1724540000 -0500", "@99999999999999999999 +1400"):
            with self.subTest(value=value):
                self.assertEqual(request(issued_at=value).issued_at, value)


class DeterminismTests(unittest.TestCase):
    def test_identical_inputs_render_byte_identical_output(self) -> None:
        first, second = request(), request()
        self.assertEqual(first, second)
        self.assertEqual(first.payload_bytes(), second.payload_bytes())
        self.assertEqual(first.message_bytes(), second.message_bytes())
        self.assertEqual(first.commit_identity(), second.commit_identity())

    def test_every_field_changes_the_payload(self) -> None:
        base = request().payload_bytes()
        for field, value in (
            ("repository", "garcia42/other"),
            ("issue_number", 50),
            ("contract_sha256", "d" * 64),
            ("holder", "session-b"),
            ("issued_at", "@1 +0000"),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(request(**{field: value}).payload_bytes(), base)

    def test_the_payload_is_canonical(self) -> None:
        raw = request().payload_bytes()
        # Structural separators only: a value may legitimately contain a space
        # (issuedAt is "@0 +0000"), so asserting no space at all would be
        # asserting something false about the format.
        self.assertNotIn(b", ", raw)
        self.assertNotIn(b": ", raw)
        self.assertFalse(raw.endswith(b"\n"))
        decoded = json.loads(raw)
        self.assertEqual(frozenset(decoded), CLAIM_PAYLOAD_KEYS)
        self.assertEqual(decoded["schemaVersion"], CLAIM_PAYLOAD_SCHEMA_VERSION)
        # Sorted keys, so the ordering does not depend on insertion order.
        self.assertEqual(list(decoded), sorted(decoded))
        self.assertEqual(raw, json.dumps(decoded, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8"))

    def test_a_non_ascii_holder_renders_as_escaped_ascii(self) -> None:
        raw = request(holder="sessión").payload_bytes()
        self.assertEqual(raw, raw.decode("utf-8").encode("ascii"))

    def test_the_payload_key_set_is_declared_not_inferred(self) -> None:
        self.assertEqual(frozenset(request().payload_mapping()), CLAIM_PAYLOAD_KEYS)

    def test_rendering_is_independent_of_the_ambient_environment(self) -> None:
        """Rendered in a fresh interpreter with a hostile environment.

        Determinism claimed within one process only would not be determinism:
        the values that could leak -- a clock, a locale, an author identity, the
        hash seed -- are all process-wide.
        """

        source = (
            "import json,sys\n"
            "from council_tools.git_claim_request import ClaimRequest\n"
            f"r = ClaimRequest(**{VALID!r})\n"
            "sys.stdout.buffer.write(r.payload_bytes() + b'|' + r.message_bytes()"
            " + b'|' + json.dumps(dict(r.commit_identity()), sort_keys=True).encode())\n"
        )
        hostile = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": os.pathsep.join(sys.path),
            "TZ": "Asia/Tokyo",
            "LC_ALL": "tr_TR.UTF-8",
            "LANG": "tr_TR.UTF-8",
            "GIT_AUTHOR_NAME": "HOSTILE",
            "GIT_AUTHOR_EMAIL": "hostile@example.invalid",
            "GIT_AUTHOR_DATE": "@999 +0900",
            "GIT_COMMITTER_DATE": "@999 +0900",
            # A different hash seed reorders unsorted dict iteration.
            "PYTHONHASHSEED": "12345",
        }
        child = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, env=hostile
        )
        self.assertEqual(child.returncode, 0, child.stderr.decode())
        here = request()
        expected = (
            here.payload_bytes()
            + b"|"
            + here.message_bytes()
            + b"|"
            + json.dumps(dict(here.commit_identity()), sort_keys=True).encode()
        )
        self.assertEqual(child.stdout, expected)


class BuilderIntegrationTests(unittest.TestCase):
    """The rendered values must be accepted by the builders unchanged."""

    def test_the_payload_is_accepted_as_blob_content(self) -> None:
        operation = WriteCanonicalBlob(content=request().payload_bytes())
        self.assertEqual(operation.render().stdin, request().payload_bytes())

    def test_the_identity_and_message_are_accepted_by_the_commit_builder(self) -> None:
        claim = request()
        operation = CreateZeroParentCommit(
            tree_id=Sha1ObjectId("a" * 40),
            message=claim.message_bytes(),
            **claim.commit_identity(),
        )
        rendered = operation.render()
        self.assertEqual(rendered.stdin, claim.message_bytes())
        self.assertEqual(rendered.identity["GIT_AUTHOR_DATE"], claim.issued_at)
        self.assertEqual(rendered.identity["GIT_AUTHOR_NAME"], claim.holder)
        self.assertEqual(
            rendered.identity["GIT_COMMITTER_EMAIL"],
            f"{claim.holder}@{HOLDER_EMAIL_DOMAIN}",
        )

    def test_the_commit_identity_has_exactly_the_builder_parameter_names(self) -> None:
        # Passed through as **kwargs above, so a renamed key would be a
        # TypeError rather than a silently dropped value.
        self.assertEqual(
            set(request().commit_identity()),
            {
                "author_name",
                "author_email",
                "author_date",
                "committer_name",
                "committer_email",
                "committer_date",
            },
        )

    def test_author_and_committer_are_the_same_party(self) -> None:
        identity = request().commit_identity()
        self.assertEqual(identity["author_name"], identity["committer_name"])
        self.assertEqual(identity["author_email"], identity["committer_email"])
        self.assertEqual(identity["author_date"], identity["committer_date"])


class NonAuthorizationTests(unittest.TestCase):
    def test_a_request_asserts_nothing_about_ownership(self) -> None:
        claim = request()
        for name in ("is_claimed", "owner", "acquire", "claim", "authorize"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(claim, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
