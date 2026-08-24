import unittest
from dataclasses import FrozenInstanceError, replace

import council_tools.ticket_mutations as ticket_mutations
from council_tools.ticket_contracts import (
    TicketContract,
    contract_sha256,
    validate_ticket_envelope,
)
from council_tools.ticket_mutations import (
    MAX_DIFF_ENTRIES,
    MAX_FINDINGS,
    MAX_MUTATIONS,
    REASON_CODES,
    Finding,
    MutationRequest,
    NormalizedDiffEntry,
    NormalizedMutation,
    evaluate_ticket_mutations,
)


BASE_COMMIT = "34b57e9ad71db76d361e6995a608d9cbed0b0575"


def normalized_contract(*, base_commit=BASE_COMMIT):
    raw_contract = {
        "schemaVersion": 1,
        "repository": "garcia42/ai-council",
        "issueNumber": 8,
        "targetBranch": "main",
        "baseCommit": base_commit,
        "workType": "change",
        "priority": "P0",
        "points": 2,
        "problemStatement": "Decide one pure normalized mutation policy.",
        "acceptanceCriteria": ["Declared mutations match observed entries."],
        "testCommands": [
            "PYTHONPATH=src:. python3 -m unittest tests.test_ticket_mutations -v"
        ],
        "allowedPaths": [
            {"kind": "directory", "path": "pkg"},
            {"kind": "file", "path": "README.md"},
        ],
        "outOfScope": ["Git parsing"],
        "dependencies": [4, 5],
        "rollbackPlan": "Revert the issue commit.",
    }
    digest = contract_sha256(raw_contract)
    return validate_ticket_envelope(
        {
            "contract": raw_contract,
            "reviewRef": {
                "runId": "claude-opus-5:ticket-mutations",
                "contractSha256": digest,
            },
        }
    ).contract


def request(*, mutations=(), diff_entries=(), branch="main", base=BASE_COMMIT):
    return MutationRequest(
        target_branch=branch,
        base_commit=base,
        mutations=tuple(mutations),
        diff_entries=tuple(diff_entries),
    )


def mutation(op, path, source_path=""):
    return NormalizedMutation(op=op, path=path, source_path=source_path)


def diff(status, old_path, new_path, old_kind, new_kind):
    return NormalizedDiffEntry(
        status=status,
        old_path=old_path,
        new_path=new_path,
        old_kind=old_kind,
        new_kind=new_kind,
    )


def create_pair(path="pkg/new.py", *, kind="regular"):
    return (
        mutation("create", path),
        diff("A", None, path, None, kind),
    )


class TicketMutationsTest(unittest.TestCase):
    def assertCodes(self, decision, expected):
        self.assertFalse(decision.allowed)
        self.assertEqual(
            tuple(finding.code for finding in decision.findings),
            tuple(expected),
        )
        return decision

    def test_golden_and_empty_requests_are_structurally_allowed_only(self):
        contract = normalized_contract()
        declared, observed = create_pair()
        decision = evaluate_ticket_mutations(
            contract,
            request(mutations=(declared,), diff_entries=(observed,)),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.findings, ())
        with self.assertRaisesRegex(TypeError, "not authorization"):
            bool(decision)

        empty = evaluate_ticket_mutations(contract, request())
        self.assertTrue(empty.allowed)
        self.assertEqual(empty.findings, ())

    def test_models_and_decisions_are_frozen(self):
        declared, _ = create_pair()
        decision = evaluate_ticket_mutations(normalized_contract(), request())
        with self.assertRaises(FrozenInstanceError):
            declared.path = "pkg/other.py"
        with self.assertRaises(FrozenInstanceError):
            decision.allowed = False

    def test_reason_order_is_frozen_complete_and_unique(self):
        expected = (
            "invalid-contract",
            "invalid-request",
            "target-branch-mismatch",
            "base-commit-malformed",
            "base-commit-mismatch",
            "too-many-mutations",
            "too-many-diff-entries",
            "unknown-diff-status",
            "invalid-diff-fields",
            "unsupported-entry-kind",
            "type-change-forbidden",
            "path-not-allowed",
            "source-equals-destination",
            "duplicate-path-mutation",
            "case-collision",
            "undeclared-diff-entry",
            "unsubstantiated-mutation",
            "findings-truncated",
        )
        self.assertEqual(REASON_CODES, expected)
        self.assertEqual(len(REASON_CODES), len(set(REASON_CODES)))

    def test_every_supported_status_projects_to_one_exact_mutation_key(self):
        cases = (
            (
                mutation("create", "pkg/new.py"),
                diff("A", None, "pkg/new.py", None, "regular"),
            ),
            (
                mutation("modify", "pkg/code.py"),
                diff(
                    "M",
                    "pkg/code.py",
                    "pkg/code.py",
                    "regular",
                    "regular",
                ),
            ),
            (
                mutation("delete", "pkg/old.py"),
                diff("D", "pkg/old.py", None, "regular", None),
            ),
            (
                mutation("rename", "pkg/new.py", "pkg/old.py"),
                diff(
                    "R",
                    "pkg/old.py",
                    "pkg/new.py",
                    "regular",
                    "regular",
                ),
            ),
            (
                mutation("copy", "pkg/copy.py", "pkg/source.py"),
                diff(
                    "C",
                    "pkg/source.py",
                    "pkg/copy.py",
                    "regular",
                    "regular",
                ),
            ),
        )
        for declared, observed in cases:
            with self.subTest(status=observed.status):
                result = evaluate_ticket_mutations(
                    normalized_contract(),
                    request(mutations=(declared,), diff_entries=(observed,)),
                )
                self.assertTrue(result.allowed)

    def test_target_branch_and_full_base_identity_are_exact(self):
        contract = normalized_contract()
        self.assertCodes(
            evaluate_ticket_mutations(contract, request(branch="refs/heads/main")),
            ("target-branch-mismatch",),
        )
        malformed = self.assertCodes(
            evaluate_ticket_mutations(contract, request(base=BASE_COMMIT[:12])),
            ("base-commit-malformed",),
        )
        self.assertNotIn(
            "base-commit-mismatch",
            tuple(finding.code for finding in malformed.findings),
        )
        self.assertCodes(
            evaluate_ticket_mutations(contract, request(base="0" * 40)),
            ("base-commit-mismatch",),
        )

        sha256 = "a" * 64
        contract_64 = normalized_contract(base_commit=sha256)
        self.assertTrue(
            evaluate_ticket_mutations(
                contract_64, request(base=sha256)
            ).allowed
        )

    def test_invalid_field_matrix_entries_do_not_project_or_evaluate_paths(self):
        cases = (
            diff("A", "pkg/old.py", "pkg/new.py", "regular", "regular"),
            diff("D", "pkg/old.py", "pkg/new.py", "regular", None),
            diff("M", "pkg/a.py", "pkg/b.py", "regular", "regular"),
            diff("R", None, "pkg/new.py", None, "regular"),
            diff("C", "pkg/old.py", None, "regular", None),
        )
        original = TicketContract.allows_path

        def poison(_contract, _path):
            raise AssertionError("invalid diff fields must suppress path evaluation")

        TicketContract.allows_path = poison
        try:
            for entry in cases:
                with self.subTest(status=entry.status):
                    result = evaluate_ticket_mutations(
                        normalized_contract(),
                        request(diff_entries=(entry,)),
                    )
                    self.assertCodes(
                        result,
                        ("invalid-diff-fields",),
                    )
        finally:
            TicketContract.allows_path = original

    def test_unknown_and_type_change_statuses_deny_without_path_evaluation(self):
        original = TicketContract.allows_path

        def poison(_contract, _path):
            raise AssertionError("nonprojecting statuses must not inspect paths")

        TicketContract.allows_path = poison
        try:
            unknown = diff("U", "outside", "elsewhere", "future", "future")
            self.assertCodes(
                evaluate_ticket_mutations(
                    normalized_contract(), request(diff_entries=(unknown,))
                ),
                ("unknown-diff-status",),
            )
            changed = diff("T", "outside", "elsewhere", "regular", "regular")
            self.assertCodes(
                evaluate_ticket_mutations(
                    normalized_contract(), request(diff_entries=(changed,))
                ),
                ("type-change-forbidden",),
            )
        finally:
            TicketContract.allows_path = original

    def test_symlink_gitlink_and_unknown_kinds_fail_closed(self):
        for kind in ("symlink", "gitlink", "future"):
            declared, observed = create_pair(kind=kind)
            with self.subTest(kind=kind):
                result = evaluate_ticket_mutations(
                    normalized_contract(),
                    request(mutations=(declared,), diff_entries=(observed,)),
                )
                self.assertCodes(result, ("unsupported-entry-kind",))

    def test_rename_and_copy_require_both_paths_and_distinct_endpoints(self):
        for status, op in (("R", "rename"), ("C", "copy")):
            entry = diff(
                status,
                "outside/source.py",
                "pkg/destination.py",
                "regular",
                "regular",
            )
            declared = mutation(op, "pkg/destination.py", "outside/source.py")
            with self.subTest(status=status, seam="source-scope"):
                result = evaluate_ticket_mutations(
                    normalized_contract(),
                    request(mutations=(declared,), diff_entries=(entry,)),
                )
                self.assertCodes(result, ("path-not-allowed",))
                self.assertEqual(result.findings[0].path, "outside/source.py")

            same = diff(
                status,
                "pkg/same.py",
                "pkg/same.py",
                "regular",
                "regular",
            )
            same_declared = mutation(op, "pkg/same.py", "pkg/same.py")
            with self.subTest(status=status, seam="same-endpoint"):
                self.assertCodes(
                    evaluate_ticket_mutations(
                        normalized_contract(),
                        request(
                            mutations=(same_declared,), diff_entries=(same,)
                        ),
                    ),
                    ("source-equals-destination",),
                )

    def test_contract_path_semantics_are_reused_without_normalization(self):
        paths = (
            "pkg",
            "PKG/file.py",
            "README.MD",
            "pkg/../outside.py",
            "pkg/.GIT/config",
            "pkg\\file.py",
        )
        for path in paths:
            declared, observed = create_pair(path)
            with self.subTest(path=path):
                self.assertCodes(
                    evaluate_ticket_mutations(
                        normalized_contract(),
                        request(mutations=(declared,), diff_entries=(observed,)),
                    ),
                    ("path-not-allowed",),
                )

        declared, observed = create_pair("pkg/child.py")
        self.assertTrue(
            evaluate_ticket_mutations(
                normalized_contract(),
                request(mutations=(declared,), diff_entries=(observed,)),
            ).allowed
        )
        declared, observed = create_pair("README.md")
        self.assertTrue(
            evaluate_ticket_mutations(
                normalized_contract(),
                request(mutations=(declared,), diff_entries=(observed,)),
            ).allowed
        )

    def test_path_scope_is_delegated_once_per_distinct_applicable_diff_path(self):
        declared = mutation("modify", "pkg/code.py")
        observed = diff(
            "M",
            "pkg/code.py",
            "pkg/code.py",
            "regular",
            "regular",
        )
        calls = []
        original = TicketContract.allows_path

        def spy(contract, path):
            calls.append(path)
            return original(contract, path)

        TicketContract.allows_path = spy
        try:
            result = evaluate_ticket_mutations(
                normalized_contract(),
                request(mutations=(declared,), diff_entries=(observed,)),
            )
        finally:
            TicketContract.allows_path = original
        self.assertTrue(result.allowed)
        self.assertEqual(calls, ["pkg/code.py"])

    def test_declared_and_observed_multisets_must_correspond_both_directions(self):
        declared, observed = create_pair()
        self.assertCodes(
            evaluate_ticket_mutations(
                normalized_contract(), request(mutations=(declared,))
            ),
            ("unsubstantiated-mutation",),
        )
        self.assertCodes(
            evaluate_ticket_mutations(
                normalized_contract(), request(diff_entries=(observed,))
            ),
            ("undeclared-diff-entry",),
        )
        self.assertCodes(
            evaluate_ticket_mutations(
                normalized_contract(),
                request(mutations=(declared,), diff_entries=(observed, observed)),
            ),
            ("undeclared-diff-entry",),
        )
        self.assertCodes(
            evaluate_ticket_mutations(
                normalized_contract(),
                request(mutations=(declared, declared), diff_entries=(observed,)),
            ),
            ("duplicate-path-mutation", "unsubstantiated-mutation"),
        )

    def test_invalid_nonprojecting_diff_can_independently_expose_declaration(self):
        declared = mutation("modify", "pkg/code.py")
        changed = diff(
            "T",
            "pkg/code.py",
            "pkg/code.py",
            "regular",
            "regular",
        )
        self.assertCodes(
            evaluate_ticket_mutations(
                normalized_contract(),
                request(mutations=(declared,), diff_entries=(changed,)),
            ),
            ("type-change-forbidden", "unsubstantiated-mutation"),
        )

    def test_duplicate_primary_paths_and_cross_collection_case_collisions_deny(self):
        mutations = (
            mutation("create", "pkg/same.py"),
            mutation("delete", "pkg/same.py"),
        )
        entries = (
            diff("A", None, "pkg/same.py", None, "regular"),
            diff("D", "pkg/same.py", None, "regular", None),
        )
        self.assertCodes(
            evaluate_ticket_mutations(
                normalized_contract(),
                request(mutations=mutations, diff_entries=entries),
            ),
            ("duplicate-path-mutation",),
        )

        declared = mutation("create", "pkg/File.py")
        observed = diff("A", None, "pkg/file.py", None, "regular")
        result = evaluate_ticket_mutations(
            normalized_contract(),
            request(mutations=(declared,), diff_entries=(observed,)),
        )
        self.assertCodes(
            result,
            (
                "case-collision",
                "case-collision",
                "undeclared-diff-entry",
                "unsubstantiated-mutation",
            ),
        )
        self.assertEqual(
            {finding.path for finding in result.findings if finding.code == "case-collision"},
            {"pkg/File.py", "pkg/file.py"},
        )

    def test_over_cap_inputs_deny_without_touching_records(self):
        poison = object()
        too_many_mutations = request(
            mutations=(poison,) * (MAX_MUTATIONS + 1),
            diff_entries=(poison,),
        )
        self.assertCodes(
            evaluate_ticket_mutations(normalized_contract(), too_many_mutations),
            ("too-many-mutations",),
        )

        too_many_diffs = request(
            mutations=(poison,),
            diff_entries=(poison,) * (MAX_DIFF_ENTRIES + 1),
        )
        self.assertCodes(
            evaluate_ticket_mutations(normalized_contract(), too_many_diffs),
            ("too-many-diff-entries",),
        )

        both = request(
            mutations=(poison,) * (MAX_MUTATIONS + 1),
            diff_entries=(poison,) * (MAX_DIFF_ENTRIES + 1),
            branch="other",
        )
        self.assertCodes(
            evaluate_ticket_mutations(normalized_contract(), both),
            (
                "target-branch-mismatch",
                "too-many-mutations",
                "too-many-diff-entries",
            ),
        )

    def test_arbitrary_and_uninitialized_inputs_return_instead_of_raising(self):
        contract = normalized_contract()
        for invalid in (None, {}, [], object(), object.__new__(MutationRequest)):
            with self.subTest(request_type=type(invalid).__name__):
                self.assertCodes(
                    evaluate_ticket_mutations(contract, invalid),
                    ("invalid-request",),
                )

        list_request = MutationRequest("main", BASE_COMMIT, [], ())
        self.assertCodes(
            evaluate_ticket_mutations(contract, list_request),
            ("invalid-request",),
        )
        bad_mutation = NormalizedMutation("create", "pkg/new.py", None)
        self.assertCodes(
            evaluate_ticket_mutations(
                contract, request(mutations=(bad_mutation,))
            ),
            ("invalid-request",),
        )
        self.assertCodes(
            evaluate_ticket_mutations(
                contract,
                request(mutations=(object.__new__(NormalizedMutation),)),
            ),
            ("invalid-request",),
        )
        self.assertCodes(
            evaluate_ticket_mutations(
                contract,
                request(diff_entries=(object.__new__(NormalizedDiffEntry),)),
            ),
            ("invalid-request",),
        )

        for invalid_contract in (
            None,
            object(),
            object.__new__(TicketContract),
            replace(contract, allowed_paths=(object(),)),
        ):
            with self.subTest(contract_type=type(invalid_contract).__name__):
                self.assertCodes(
                    evaluate_ticket_mutations(invalid_contract, request()),
                    ("invalid-contract",),
                )

    def test_unknown_mutation_operations_and_wrong_nested_types_are_malformed(self):
        cases = (
            mutation("move", "pkg/new.py"),
            mutation("create", "pkg/new.py", "pkg/source.py"),
            mutation("rename", "pkg/new.py", ""),
            NormalizedMutation("create", 1, ""),
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertCodes(
                    evaluate_ticket_mutations(
                        normalized_contract(), request(mutations=(value,))
                    ),
                    ("invalid-request",),
                )

        wrong_diff = NormalizedDiffEntry("A", None, 1, None, "regular")
        self.assertCodes(
            evaluate_ticket_mutations(
                normalized_contract(), request(diff_entries=(wrong_diff,))
            ),
            ("invalid-request",),
        )

    def test_findings_are_order_independent_and_non_reflective(self):
        mutations = (
            mutation("create", "pkg/File.py"),
            mutation("delete", "outside.py"),
        )
        entries = (
            diff("D", "outside.py", None, "gitlink", None),
            diff("A", None, "pkg/file.py", None, "symlink"),
        )
        first = evaluate_ticket_mutations(
            normalized_contract(),
            request(mutations=mutations, diff_entries=entries, branch="other"),
        )
        second = evaluate_ticket_mutations(
            normalized_contract(),
            request(
                mutations=tuple(reversed(mutations)),
                diff_entries=tuple(reversed(entries)),
                branch="other",
            ),
        )
        self.assertEqual(first, second)
        self.assertEqual(first.findings, tuple(sorted(
            first.findings,
            key=lambda item: (
                REASON_CODES.index(item.code), item.path, item.source_path
            ),
        )))

    def test_findings_are_capped_at_63_plus_one_truncation_sentinel(self):
        entries = tuple(
            diff(
                "R",
                f"outside/source-{index}.py",
                f"outside/destination-{index}.py",
                "future-old",
                "future-new",
            )
            for index in range(MAX_DIFF_ENTRIES)
        )
        decision = evaluate_ticket_mutations(
            normalized_contract(), request(diff_entries=entries)
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(len(decision.findings), MAX_FINDINGS)
        self.assertEqual(
            decision.findings[-1], Finding("findings-truncated")
        )
        self.assertNotIn(
            "findings-truncated",
            tuple(finding.code for finding in decision.findings[:-1]),
        )

    def test_pathless_findings_use_empty_sort_fields(self):
        decision = evaluate_ticket_mutations(
            normalized_contract(), request(branch="other", base="bad")
        )
        self.assertCodes(
            decision,
            ("target-branch-mismatch", "base-commit-malformed"),
        )
        for finding in decision.findings:
            self.assertEqual(finding.path, "")
            self.assertEqual(finding.source_path, "")

    def test_module_has_no_external_io_dependencies(self):
        for name in (
            "os",
            "pathlib",
            "requests",
            "subprocess",
            "time",
            "urllib",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(ticket_mutations, name))


if __name__ == "__main__":
    unittest.main()
