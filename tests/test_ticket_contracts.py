import copy
import json
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from enum import IntEnum

import council_tools.ticket_contracts as ticket_contracts
from council_tools.ticket_contracts import (
    AllowedPath,
    MAX_TICKET_JSON_BYTES,
    TicketContractError,
    canonical_contract_bytes,
    contract_sha256,
    load_ticket_envelope_json,
    validate_ticket_envelope,
)


GOLDEN_SHA256 = "c5d7c05babaf89936a8048dca1cf753ca22a2fa3ad03de69ae7b07520ae2fa95"


def golden_contract():
    return {
        "schemaVersion": 1,
        "repository": "garcia42/ai-council",
        "issueNumber": 2,
        "targetBranch": "main",
        "baseCommit": "befbbfaea22f4c6f69fa42a8bc5bff21e5a189dd",
        "workType": "change",
        "priority": "P0",
        "points": 2,
        "problemStatement": "Add a strict ticket contract for José.",
        "acceptanceCriteria": ["Valid envelopes normalize deterministically."],
        "testCommands": [
            "PYTHONPATH=src:. python3 -m unittest tests.test_ticket_contracts -v"
        ],
        "allowedPaths": [
            {
                "kind": "file",
                "path": "src/council_tools/ticket_contracts.py",
            },
            {"kind": "file", "path": "tests/test_ticket_contracts.py"},
        ],
        "outOfScope": ["GitHub API integration"],
        "dependencies": [],
        "rollbackPlan": "Revert the issue commit.",
    }


def envelope(contract=None, *, run_id="premortem-f4d66860"):
    contract = copy.deepcopy(contract if contract is not None else golden_contract())
    return {
        "contract": contract,
        "reviewRef": {
            "runId": run_id,
            "contractSha256": contract_sha256(contract),
        },
    }


def refresh_digest(payload):
    payload["reviewRef"]["contractSha256"] = contract_sha256(payload["contract"])
    return payload


class IntegerSubclass(int):
    pass


class IntegerEnum(IntEnum):
    TWO = 2


class TextSubclass(str):
    pass


class BytesSubclass(bytes):
    pass


class BytearraySubclass(bytearray):
    pass


class TicketContractTest(unittest.TestCase):
    def assertContractError(self, payload, code, field):
        with self.assertRaises(TicketContractError) as caught:
            validate_ticket_envelope(payload)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.field, field)
        self.assertEqual(
            str(caught.exception), f"ticket contract {code} at {field}"
        )

    def assertJsonError(self, document, code):
        with self.assertRaises(TicketContractError) as caught:
            load_ticket_envelope_json(document)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.field, "$")
        self.assertEqual(str(caught.exception), f"ticket contract {code} at $")

    def test_golden_non_ascii_contract_pins_canonical_bytes_and_digest(self):
        contract = golden_contract()
        expected = (
            '{"acceptanceCriteria":["Valid envelopes normalize deterministically."],'
            '"allowedPaths":[{"kind":"file","path":"src/council_tools/'
            'ticket_contracts.py"},{"kind":"file","path":"tests/'
            'test_ticket_contracts.py"}],"baseCommit":"befbbfaea22f4c6f69fa42a8bc5bff21e5a189dd",'
            '"dependencies":[],"issueNumber":2,'
            '"outOfScope":["GitHub API integration"],"points":2,'
            '"priority":"P0","problemStatement":"Add a strict ticket contract '
            'for José.","repository":"garcia42/ai-council","rollbackPlan":'
            '"Revert the issue commit.","schemaVersion":1,"targetBranch":"main",'
            '"testCommands":["PYTHONPATH=src:. python3 -m unittest '
            'tests.test_ticket_contracts -v"],"workType":"change"}'
        ).encode("utf-8")
        self.assertEqual(canonical_contract_bytes(contract), expected)
        self.assertEqual(contract_sha256(contract), GOLDEN_SHA256)

        payload = envelope(contract)
        payload["reviewRef"]["contractSha256"] = GOLDEN_SHA256
        normalized = validate_ticket_envelope(payload)
        self.assertEqual(normalized.contract.problem_statement, contract["problemStatement"])
        self.assertEqual(normalized.review_ref.contract_sha256, GOLDEN_SHA256)

    def test_contract_mapping_order_does_not_change_digest_or_result(self):
        original = golden_contract()
        reordered = dict(reversed(list(original.items())))
        reordered["allowedPaths"] = [
            dict(reversed(list(item.items()))) for item in reordered["allowedPaths"]
        ]
        self.assertEqual(contract_sha256(original), contract_sha256(reordered))
        self.assertEqual(
            validate_ticket_envelope(envelope(original)),
            validate_ticket_envelope(envelope(reordered)),
        )

    def test_all_supported_work_types_and_priorities_validate(self):
        for work_type in ("bug", "change", "investigation"):
            for priority in ("P0", "P1"):
                contract = golden_contract()
                contract["workType"] = work_type
                contract["priority"] = priority
                with self.subTest(work_type=work_type, priority=priority):
                    normalized = validate_ticket_envelope(envelope(contract))
                    self.assertEqual(normalized.contract.work_type, work_type)
                    self.assertEqual(normalized.contract.priority, priority)

        for points in (1, 2, 3):
            contract = golden_contract()
            contract["points"] = points
            with self.subTest(points=points):
                self.assertEqual(
                    validate_ticket_envelope(envelope(contract)).contract.points,
                    points,
                )

    def test_normalized_value_round_trips_idempotently(self):
        first = validate_ticket_envelope(envelope())
        second = validate_ticket_envelope(first.as_dict())
        self.assertEqual(first, second)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_normalized_value_is_deeply_immutable_and_detached_from_input(self):
        payload = envelope()
        normalized = validate_ticket_envelope(payload)
        payload["contract"]["acceptanceCriteria"].append("Widened later.")
        payload["contract"]["allowedPaths"][0]["path"] = "everything"
        payload["reviewRef"]["runId"] = "changed"

        self.assertEqual(
            normalized.contract.acceptance_criteria,
            ("Valid envelopes normalize deterministically.",),
        )
        self.assertEqual(
            normalized.contract.allowed_paths[0].path,
            "src/council_tools/ticket_contracts.py",
        )
        self.assertEqual(normalized.review_ref.run_id, "premortem-f4d66860")
        with self.assertRaises(FrozenInstanceError):
            normalized.contract.points = 3

    def test_review_reference_binds_contract_only_and_is_not_authorization(self):
        payload = envelope()
        payload["reviewRef"]["runId"] = "different-run-reference"
        normalized = validate_ticket_envelope(payload)
        self.assertEqual(normalized.review_ref.run_id, "different-run-reference")
        self.assertIn("not authorization", ticket_contracts.__doc__.lower())

    def test_unknown_or_missing_keys_fail_closed_at_every_level(self):
        cases = []
        missing_envelope = envelope()
        missing_envelope.pop("reviewRef")
        cases.append((missing_envelope, "invalid-envelope-keys", "$"))
        extra_envelope = envelope()
        extra_envelope["future"] = True
        cases.append((extra_envelope, "invalid-envelope-keys", "$"))
        extra_contract = envelope()
        extra_contract["contract"]["severity"] = "high"
        cases.append((extra_contract, "invalid-contract-keys", "contract"))
        missing_contract = envelope()
        missing_contract["contract"].pop("rollbackPlan")
        cases.append((missing_contract, "invalid-contract-keys", "contract"))
        extra_review = envelope()
        extra_review["reviewRef"]["approved"] = True
        cases.append((extra_review, "invalid-review-ref-keys", "reviewRef"))

        for payload, code, field in cases:
            with self.subTest(code=code, field=field):
                self.assertContractError(payload, code, field)

    def test_schema_repository_and_enum_fields_are_strict(self):
        cases = (
            ("schemaVersion", 2, "unsupported-schema-version"),
            ("schemaVersion", True, "unsupported-schema-version"),
            ("repository", "no-slash", "invalid-repository"),
            ("repository", "/repo", "invalid-repository"),
            ("repository", "owner/", "invalid-repository"),
            ("workType", "feature", "invalid-work-type"),
            ("priority", "P2", "invalid-priority"),
        )
        for field, value, code in cases:
            payload = envelope()
            payload["contract"][field] = value
            with self.subTest(field=field, value=value):
                self.assertContractError(payload, code, f"contract.{field}")

    def test_schema_version_dispatch_precedes_exact_v1_key_validation(self):
        payload = envelope()
        payload["contract"]["schemaVersion"] = 2
        payload["contract"]["futureV2Field"] = True
        self.assertContractError(
            payload, "unsupported-schema-version", "contract.schemaVersion"
        )

    def test_target_branch_and_base_commit_are_strict(self):
        for branch in (
            "",
            "/main",
            "-main",
            "main/",
            "feature//x",
            ".hidden",
            "feature/.hidden",
            "release.lock",
            "a..b",
            "feature x",
            "feature@{upstream}",
            "feature\\x",
        ):
            payload = envelope()
            payload["contract"]["targetBranch"] = branch
            with self.subTest(branch=repr(branch)):
                self.assertContractError(
                    payload, "invalid-target-branch", "contract.targetBranch"
                )

        for commit in ("", "A" * 40, "a" * 39, "g" * 40, "a" * 41):
            payload = envelope()
            payload["contract"]["baseCommit"] = commit
            with self.subTest(commit=repr(commit)):
                self.assertContractError(
                    payload, "invalid-base-commit", "contract.baseCommit"
                )

        payload = envelope()
        payload["contract"]["targetBranch"] = "feature/ticket-contract-v1"
        payload["contract"]["baseCommit"] = "a" * 64
        refresh_digest(payload)
        normalized = validate_ticket_envelope(payload)
        self.assertEqual(normalized.contract.target_branch, "feature/ticket-contract-v1")
        self.assertEqual(normalized.contract.base_commit, "a" * 64)

    def test_numeric_fields_reject_bool_coercions_and_non_exact_ints(self):
        invalid_numbers = (True, False, 2.0, "2", Decimal("2"), IntegerSubclass(2), IntegerEnum.TWO)
        for field in ("issueNumber", "points"):
            for value in invalid_numbers:
                payload = envelope()
                payload["contract"][field] = value
                code = "invalid-issue-number" if field == "issueNumber" else "invalid-points"
                with self.subTest(field=field, value=value):
                    self.assertContractError(payload, code, f"contract.{field}")

        for value in (0, 2**63):
            payload = envelope()
            payload["contract"]["issueNumber"] = value
            self.assertContractError(
                payload, "invalid-issue-number", "contract.issueNumber"
            )
        for value in (0, 4, -1):
            payload = envelope()
            payload["contract"]["points"] = value
            self.assertContractError(payload, "invalid-points", "contract.points")

    def test_text_fields_reject_blank_padding_nul_and_non_nfc(self):
        fields = (
            "problemStatement",
            "rollbackPlan",
        )
        invalid_values = (
            "",
            " ",
            " padded",
            "padded ",
            "a\x00b",
            "Jose\u0301",
            "\ud800",
        )
        for field in fields:
            for value in invalid_values:
                payload = envelope()
                payload["contract"][field] = value
                code = (
                    "invalid-problem-statement"
                    if field == "problemStatement"
                    else "invalid-rollback-plan"
                )
                with self.subTest(field=field, value=repr(value)):
                    self.assertContractError(payload, code, f"contract.{field}")

    def test_text_lists_are_non_empty_unique_and_canonical(self):
        fields = (
            ("acceptanceCriteria", "invalid-acceptance-criteria"),
            ("testCommands", "invalid-test-commands"),
            ("outOfScope", "invalid-out-of-scope"),
        )
        for field, code in fields:
            for value in ([], [""], ["one", "one"], "one", [" padded"]):
                payload = envelope()
                payload["contract"][field] = value
                with self.subTest(field=field, value=value):
                    self.assertContractError(payload, code, f"contract.{field}")

    def test_path_scope_accepts_github_and_rejects_ambiguous_or_git_paths(self):
        valid = (
            {"kind": "file", "path": ".github/workflows/check.yml"},
            {"kind": "directory", "path": ".github/workflows"},
            {"kind": "directory", "path": "src/council_tools"},
            {"kind": "file", "path": "README.md"},
        )
        for item in valid:
            payload = envelope()
            payload["contract"]["allowedPaths"] = [item]
            refresh_digest(payload)
            with self.subTest(valid=item):
                normalized = validate_ticket_envelope(payload)
                self.assertEqual(normalized.contract.allowed_paths[0].path, item["path"])

        invalid_paths = (
            "",
            "/abs",
            "./x",
            "../x",
            "a/../b",
            "a/./b",
            "a//b",
            "a\\b",
            "a\x00b",
            ".git",
            ".git/config",
            "x/.git/y",
            ".GIT/config",
            "x/.Git/y",
            "a\nb",
            "src/",
            " src",
            "src ",
        )
        for path in invalid_paths:
            payload = envelope()
            payload["contract"]["allowedPaths"] = [
                {"kind": "directory", "path": path}
            ]
            with self.subTest(path=repr(path)):
                self.assertContractError(
                    payload, "invalid-allowed-path", "contract.allowedPaths[0].path"
                )

    def test_path_scope_shape_kind_and_duplicates_are_strict(self):
        cases = []
        wrong_container = envelope()
        wrong_container["contract"]["allowedPaths"] = "src"
        cases.append((wrong_container, "invalid-allowed-paths", "contract.allowedPaths"))
        empty = envelope()
        empty["contract"]["allowedPaths"] = []
        cases.append((empty, "invalid-allowed-paths", "contract.allowedPaths"))
        wrong_keys = envelope()
        wrong_keys["contract"]["allowedPaths"] = [
            {"kind": "file", "path": "README.md", "recursive": False}
        ]
        cases.append((wrong_keys, "invalid-allowed-path-keys", "contract.allowedPaths[0]"))
        wrong_kind = envelope()
        wrong_kind["contract"]["allowedPaths"] = [
            {"kind": "glob", "path": "src/*"}
        ]
        cases.append((wrong_kind, "invalid-allowed-path-kind", "contract.allowedPaths[0].kind"))
        duplicate = envelope()
        duplicate["contract"]["allowedPaths"] = [
            {"kind": "file", "path": "README.md"},
            {"kind": "file", "path": "README.md"},
        ]
        cases.append((duplicate, "duplicate-allowed-path", "contract.allowedPaths[1]"))

        for payload, code, field in cases:
            with self.subTest(code=code):
                self.assertContractError(payload, code, field)

    def test_segment_safe_case_sensitive_path_matching_is_defined_once(self):
        payload = envelope()
        payload["contract"]["allowedPaths"] = [
            {"kind": "file", "path": "README.md"},
            {"kind": "directory", "path": "src/council"},
        ]
        refresh_digest(payload)
        contract = validate_ticket_envelope(payload).contract
        self.assertTrue(contract.allows_path("README.md"))
        self.assertFalse(contract.allows_path("readme.md"))
        self.assertTrue(contract.allows_path("src/council/review.py"))
        self.assertFalse(contract.allows_path("src/council_tools/review.py"))
        self.assertFalse(contract.allows_path("src/council"))
        for invalid in ("/src/council/x", "src/council/../x", "src\\council\\x"):
            with self.subTest(invalid=invalid):
                self.assertFalse(contract.allows_path(invalid))

    def test_manually_constructed_unknown_path_kind_fails_closed(self):
        scope = AllowedPath(kind="glob", path="src")
        self.assertFalse(scope.allows("src/review.py"))
        self.assertFalse(scope.allows("src"))

    def test_dependencies_are_exact_positive_unique_and_not_self_referential(self):
        payload = envelope()
        payload["contract"]["dependencies"] = [1, 3]
        refresh_digest(payload)
        normalized = validate_ticket_envelope(payload)
        self.assertEqual(normalized.contract.dependencies, (1, 3))

        for dependencies, code, field in (
            ([2], "self-dependency", "contract.dependencies[0]"),
            ([1, 1], "duplicate-dependency", "contract.dependencies[1]"),
            ([True], "invalid-dependency", "contract.dependencies[0]"),
            ([0], "invalid-dependency", "contract.dependencies[0]"),
            ([2.0], "invalid-dependency", "contract.dependencies[0]"),
            ("1", "invalid-dependencies", "contract.dependencies"),
        ):
            candidate = envelope()
            candidate["contract"]["dependencies"] = dependencies
            with self.subTest(dependencies=dependencies):
                self.assertContractError(candidate, code, field)

    def test_review_reference_shape_digest_and_run_id_are_strict(self):
        for digest, code in (
            ("A" * 64, "invalid-contract-sha256"),
            ("a" * 63, "invalid-contract-sha256"),
            ("g" * 64, "invalid-contract-sha256"),
            ("0" * 64, "contract-sha256-mismatch"),
        ):
            payload = envelope()
            payload["reviewRef"]["contractSha256"] = digest
            with self.subTest(digest=digest):
                self.assertContractError(
                    payload, code, "reviewRef.contractSha256"
                )

        for run_id in (
            "",
            " padded",
            "padded ",
            "a\x00b",
            "run\u0301",
            "\ud800",
            "x" * (ticket_contracts.MAX_RUN_ID_LENGTH + 1),
        ):
            payload = envelope()
            payload["reviewRef"]["runId"] = run_id
            with self.subTest(run_id=repr(run_id)):
                self.assertContractError(
                    payload, "invalid-run-id", "reviewRef.runId"
                )

    def test_contract_collection_text_and_payload_bounds_fail_closed(self):
        cases = []

        long_problem = envelope()
        long_problem["contract"]["problemStatement"] = (
            "x" * (ticket_contracts.MAX_TEXT_LENGTH + 1)
        )
        cases.append(
            (long_problem, "invalid-problem-statement", "contract.problemStatement")
        )

        long_command = envelope()
        long_command["contract"]["testCommands"] = [
            "x" * (ticket_contracts.MAX_TEST_COMMAND_LENGTH + 1)
        ]
        cases.append((long_command, "invalid-test-commands", "contract.testCommands"))

        too_many_paths = envelope()
        too_many_paths["contract"]["allowedPaths"] = [
            {"kind": "file", "path": f"src/file-{index}.py"}
            for index in range(ticket_contracts.MAX_LIST_ITEMS + 1)
        ]
        cases.append(
            (too_many_paths, "invalid-allowed-paths", "contract.allowedPaths")
        )

        too_many_dependencies = envelope()
        too_many_dependencies["contract"]["dependencies"] = list(
            range(100, 100 + ticket_contracts.MAX_LIST_ITEMS + 1)
        )
        cases.append(
            (too_many_dependencies, "invalid-dependencies", "contract.dependencies")
        )

        long_path = envelope()
        long_path["contract"]["allowedPaths"] = [
            {
                "kind": "file",
                "path": "x" * (ticket_contracts.MAX_PATH_LENGTH + 1),
            }
        ]
        cases.append(
            (long_path, "invalid-allowed-path", "contract.allowedPaths[0].path")
        )

        for payload, code, field in cases:
            with self.subTest(code=code, field=field):
                self.assertContractError(payload, code, field)

        with self.assertRaises(TicketContractError) as caught:
            canonical_contract_bytes(
                {"value": "x" * (ticket_contracts.MAX_CONTRACT_BYTES + 1)}
            )
        self.assertEqual(caught.exception.code, "contract-too-large")
        self.assertEqual(caught.exception.field, "contract")

    def test_rejection_is_deterministic_across_mapping_order(self):
        first = envelope()
        first["contract"]["issueNumber"] = True
        first["contract"]["points"] = 4
        second = {
            "reviewRef": dict(reversed(list(first["reviewRef"].items()))),
            "contract": dict(reversed(list(first["contract"].items()))),
        }
        errors = []
        for payload in (first, second):
            with self.assertRaises(TicketContractError) as caught:
                validate_ticket_envelope(payload)
            errors.append((caught.exception.code, caught.exception.field, str(caught.exception)))
        self.assertEqual(errors[0], errors[1])

    def test_canonical_json_rejects_non_json_and_non_finite_values(self):
        for contract in (
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": Decimal("1")},
        ):
            with self.subTest(contract=contract):
                with self.assertRaises(TicketContractError) as caught:
                    canonical_contract_bytes(contract)
                self.assertEqual(caught.exception.code, "non-canonical-json")
                self.assertEqual(caught.exception.field, "contract")

    def test_strict_json_loader_accepts_golden_text_bytes_and_bytearray(self):
        payload = envelope()
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        expected = validate_ticket_envelope(payload)

        for document in (
            compact,
            compact.encode("utf-8"),
            bytearray(compact, "utf-8"),
            pretty,
        ):
            with self.subTest(document_type=type(document).__name__):
                self.assertEqual(load_ticket_envelope_json(document), expected)

        expanded = None
        for indent in range(128, 2_049, 128):
            candidate = json.dumps(payload, ensure_ascii=False, indent=indent)
            size = len(candidate.encode("utf-8"))
            if ticket_contracts.MAX_CONTRACT_BYTES < size <= MAX_TICKET_JSON_BYTES:
                expanded = candidate
                break
        self.assertIsNotNone(expanded)
        self.assertEqual(load_ticket_envelope_json(expanded), expected)

    def test_strict_json_loader_calls_the_v1_validator_once_without_bypass(self):
        payload = envelope()
        document = json.dumps(payload, ensure_ascii=False)
        sentinel = object()
        original = ticket_contracts.validate_ticket_envelope
        calls = []

        def spy(candidate):
            calls.append(candidate)
            return sentinel

        ticket_contracts.validate_ticket_envelope = spy
        try:
            self.assertIs(load_ticket_envelope_json(document), sentinel)
        finally:
            ticket_contracts.validate_ticket_envelope = original

        self.assertEqual(len(calls), 1)
        self.assertIs(type(calls[0]), dict)

    def test_strict_json_loader_rejects_non_exact_input_types(self):
        for document in (
            None,
            1,
            {},
            memoryview(b"{}"),
            TextSubclass("{}"),
            BytesSubclass(b"{}"),
            BytearraySubclass(b"{}"),
        ):
            with self.subTest(document_type=type(document).__name__):
                self.assertJsonError(document, "invalid-json-type")

    def test_strict_json_loader_enforces_plain_strict_utf8(self):
        for document in (
            b"\xed\xa0\x80",
            b"\xc0\x80",
            "{}".encode("utf-16"),
            "\ud800",
        ):
            with self.subTest(document=repr(document)):
                self.assertJsonError(document, "invalid-json-encoding")

        valid = json.dumps(envelope(), ensure_ascii=False)
        for document in ("\ufeff" + valid, b"\xef\xbb\xbf" + valid.encode("utf-8")):
            with self.subTest(document_type=type(document).__name__):
                self.assertJsonError(document, "invalid-json")

    def test_strict_json_loader_rejects_duplicate_keys_at_every_depth(self):
        payload = envelope()
        contract_text = json.dumps(
            payload["contract"], ensure_ascii=False, separators=(",", ":")
        )
        review_text = json.dumps(
            payload["reviewRef"], ensure_ascii=False, separators=(",", ":")
        )
        path_text = json.dumps(
            payload["contract"]["allowedPaths"][0],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        duplicate_contract = '{"issueNumber":99,' + contract_text[1:]
        duplicate_review = '{"runId":"first",' + review_text[1:]
        escaped_duplicate_path = path_text.replace(
            '"path":', '"\\u006bind":"directory","path":', 1
        )
        duplicate_path_contract = contract_text.replace(
            path_text, escaped_duplicate_path, 1
        )
        cases = (
            (
                '{"contract":'
                + contract_text
                + ',"contract":'
                + contract_text
                + ',"reviewRef":'
                + review_text
                + "}"
            ),
            (
                '{"contract":'
                + duplicate_contract
                + ',"reviewRef":'
                + review_text
                + "}"
            ),
            (
                '{"contract":'
                + contract_text
                + ',"reviewRef":'
                + duplicate_review
                + "}"
            ),
            (
                '{"contract":'
                + duplicate_path_contract
                + ',"reviewRef":'
                + review_text
                + "}"
            ),
            '[{"nested":{"a":1,"a":2}}]',
        )

        for document in cases:
            with self.subTest(document=document[:80]):
                self.assertJsonError(document, "duplicate-json-key")

    def test_strict_json_loader_rejects_constants_and_malformed_json(self):
        compact = json.dumps(envelope(), ensure_ascii=False, separators=(",", ":"))
        for constant in ("NaN", "Infinity", "-Infinity"):
            for document in (
                constant,
                compact.replace('"issueNumber":2', f'"issueNumber":{constant}', 1),
            ):
                with self.subTest(constant=constant, root=document == constant):
                    self.assertJsonError(document, "non-finite-json-number")

        overflow = compact.replace('"issueNumber":2', '"issueNumber":1e1000', 1)
        with self.assertRaises(TicketContractError) as caught:
            load_ticket_envelope_json(overflow)
        self.assertEqual(caught.exception.code, "invalid-issue-number")
        self.assertEqual(caught.exception.field, "contract.issueNumber")

        for document in ("", " ", "{", '{"a":1} {"b":2}', "{'a':1}"):
            with self.subTest(document=repr(document)):
                self.assertJsonError(document, "invalid-json")

    def test_strict_json_loader_rejects_non_object_roots_after_parse_hooks(self):
        for document in ("[]", "1", "true", "null", '"text"'):
            with self.subTest(document=document):
                self.assertJsonError(document, "invalid-json-top-level")

        self.assertJsonError('[{"a":1,"a":2}]', "duplicate-json-key")

    def test_strict_json_loader_bounds_bytes_and_recursion(self):
        self.assertJsonError(
            "x" * (MAX_TICKET_JSON_BYTES + 1), "ticket-json-too-large"
        )
        multibyte = "é" * (MAX_TICKET_JSON_BYTES // 2 + 1)
        self.assertLess(len(multibyte), MAX_TICKET_JSON_BYTES)
        self.assertJsonError(multibyte, "ticket-json-too-large")

        nested = "[" * 1_100 + "0" + "]" * 1_100
        self.assertJsonError(nested, "ticket-json-too-deep")

        self.assertJsonError("9" * 5_000, "invalid-json")

    def test_validator_module_has_no_external_io_dependencies(self):
        for forbidden in (
            "os",
            "pathlib",
            "socket",
            "subprocess",
            "time",
            "urllib",
            "requests",
        ):
            self.assertFalse(hasattr(ticket_contracts, forbidden), forbidden)
        self.assertIn("load_ticket_envelope_json", ticket_contracts.__all__)
        self.assertIn("MAX_TICKET_JSON_BYTES", ticket_contracts.__all__)


if __name__ == "__main__":
    unittest.main()
