import copy
import unittest
from dataclasses import FrozenInstanceError

from council_tools.initiative_scope import (
    SCOPE_REASON_CODES,
    InitiativeScopeError,
    canonical_initiative_scope_bytes,
    evaluate_initiative_scope,
    initiative_scope_sha256,
    validate_initiative_progress,
    validate_initiative_scope,
)


def raw_scope():
    return {
        "schemaVersion": 1,
        "initiativeId": "bounded-activation",
        "scopeRevision": 2,
        "objective": "Reach one bounded canary.",
        "canarySuccess": ["One supervised cycle completes and seals."],
        "nonGoals": ["General platform hardening"],
        "maxProductionLinesAdded": 500,
        "maxProductionFilesChanged": 8,
        "maxTickets": 5,
        "maxEngineerDays": 10,
        "allowedNewRuntimeComponents": ["systemd-unit"],
    }


def raw_progress(scope_value=None):
    scope_value = scope_value if scope_value is not None else raw_scope()
    normalized = validate_initiative_scope(scope_value)
    return {
        "initiativeId": scope_value["initiativeId"],
        "scopeRevision": scope_value["scopeRevision"],
        "initiativeScopeSha256": initiative_scope_sha256(normalized),
        "cumulativeTickets": 3,
        "cumulativeEngineerDays": 6,
        "cumulativeProductionLinesAdded": 250,
        "cumulativeProductionFilesChanged": 5,
        "newRuntimeComponents": ["systemd-unit"],
        "blockersOpened": 2,
        "blockersClosed": 2,
        "consecutiveGrowingCheckpoints": 0,
        "findings": [],
    }


def finding(*, severity="P1", classification="DIRECT", disposition="BLOCKS_CANARY"):
    return {
        "findingId": "F-1",
        "severity": severity,
        "classification": classification,
        "observedEvidence": "The bounded path fails deterministically.",
        "canaryFailure": "The supervised cycle cannot complete.",
        "maximumConsequence": "No activation receipt is produced.",
        "existingControlsGap": "The failure bypasses the current preflight.",
        "smallestMitigation": "Repair the failing boundary only.",
        "acceptanceTest": "The bounded cycle completes and seals.",
        "disposition": disposition,
    }


class InitiativeScopeTest(unittest.TestCase):
    def assertScopeError(self, value, validator, code, field):
        with self.assertRaises(InitiativeScopeError) as caught:
            validator(value)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.field, field)

    def test_scope_round_trip_digest_and_immutability(self):
        source = raw_scope()
        scope = validate_initiative_scope(source)
        source["canarySuccess"].append("Changed later.")

        self.assertEqual(scope.as_dict(), raw_scope())
        self.assertEqual(
            canonical_initiative_scope_bytes(scope),
            canonical_initiative_scope_bytes(validate_initiative_scope(raw_scope())),
        )
        self.assertEqual(len(initiative_scope_sha256(scope)), 64)
        with self.assertRaises(FrozenInstanceError):
            scope.max_tickets = 99

    def test_valid_progress_is_inside_the_envelope(self):
        scope = validate_initiative_scope(raw_scope())
        progress = validate_initiative_progress(raw_progress())
        decision = evaluate_initiative_scope(scope, progress)

        self.assertFalse(decision.scope_review_required)
        self.assertEqual(decision.reasons, ())
        with self.assertRaisesRegex(TypeError, "not authorization"):
            bool(decision)

    def test_all_aggregate_boundaries_have_stable_ordered_reasons(self):
        scope = validate_initiative_scope(raw_scope())
        progress_value = raw_progress()
        progress_value.update(
            {
                "initiativeId": "other",
                "scopeRevision": 3,
                "initiativeScopeSha256": "0" * 64,
                "cumulativeTickets": 6,
                "cumulativeEngineerDays": 11,
                "cumulativeProductionLinesAdded": 501,
                "cumulativeProductionFilesChanged": 9,
                "newRuntimeComponents": ["daemon"],
                "consecutiveGrowingCheckpoints": 2,
                "findings": [
                    finding(
                        classification="INDUCED_BY_DESIGN",
                        disposition="REQUIRES_PRINCIPAL_SCOPE_CHANGE",
                    )
                ],
            }
        )
        decision = evaluate_initiative_scope(
            scope, validate_initiative_progress(progress_value)
        )

        self.assertTrue(decision.scope_review_required)
        self.assertEqual(decision.reasons, SCOPE_REASON_CODES)

    def test_budget_values_at_the_boundary_are_allowed(self):
        scope_value = raw_scope()
        progress_value = raw_progress(scope_value)
        progress_value.update(
            {
                "cumulativeTickets": scope_value["maxTickets"],
                "cumulativeEngineerDays": scope_value["maxEngineerDays"],
                "cumulativeProductionLinesAdded": scope_value[
                    "maxProductionLinesAdded"
                ],
                "cumulativeProductionFilesChanged": scope_value[
                    "maxProductionFilesChanged"
                ],
                "consecutiveGrowingCheckpoints": 1,
            }
        )
        decision = evaluate_initiative_scope(
            validate_initiative_scope(scope_value),
            validate_initiative_progress(progress_value),
        )
        self.assertFalse(decision.scope_review_required)

    def test_p2_p3_and_design_induced_findings_cannot_block_canary(self):
        cases = (
            (finding(severity="P2"), "nonblocking-severity-blocks-canary"),
            (finding(severity="P3"), "nonblocking-severity-blocks-canary"),
            (
                finding(classification="INDUCED_BY_DESIGN"),
                "classification-cannot-block-canary",
            ),
            (
                finding(classification="POST_CANARY_HARDENING"),
                "classification-cannot-block-canary",
            ),
        )
        for raw_finding, code in cases:
            progress = raw_progress()
            progress["findings"] = [raw_finding]
            with self.subTest(raw_finding=raw_finding):
                self.assertScopeError(
                    progress,
                    validate_initiative_progress,
                    code,
                    "initiativeScopeEvidence.findings[0].disposition",
                )

    def test_post_canary_hardening_must_be_backlogged_or_rejected(self):
        progress = raw_progress()
        progress["findings"] = [
            finding(
                severity="P2",
                classification="POST_CANARY_HARDENING",
                disposition="ACCEPTED_RESIDUAL_RISK",
            )
        ]
        self.assertScopeError(
            progress,
            validate_initiative_progress,
            "hardening-not-backlogged",
            "initiativeScopeEvidence.findings[0].disposition",
        )

    def test_strict_shapes_and_counter_types_fail_closed(self):
        invalid_scope = raw_scope()
        invalid_scope["extra"] = True
        self.assertScopeError(
            invalid_scope,
            validate_initiative_scope,
            "invalid-keys",
            "initiativeScope",
        )

        for value in (True, -1, 1.5, "1"):
            progress = raw_progress()
            progress["cumulativeTickets"] = value
            with self.subTest(value=value):
                self.assertScopeError(
                    progress,
                    validate_initiative_progress,
                    "invalid-nonnegative-integer",
                    "initiativeScopeEvidence.cumulativeTickets",
                )

        progress = raw_progress()
        progress["blockersClosed"] = progress["blockersOpened"] + 1
        self.assertScopeError(
            progress,
            validate_initiative_progress,
            "closed-blockers-exceed-opened",
            "initiativeScopeEvidence.blockersClosed",
        )

    def test_hostile_mapping_keys_are_a_typed_shape_failure(self):
        class ExplodingKey:
            def __hash__(self):
                return hash("schemaVersion")

            def __eq__(self, other):
                raise RuntimeError("untrusted key equality must not run")

        self.assertScopeError(
            {ExplodingKey(): None},
            validate_initiative_scope,
            "invalid-keys",
            "initiativeScope",
        )

    def test_progress_round_trips_to_plain_detached_data(self):
        source = raw_progress()
        source["findings"] = [
            finding(
                severity="P2",
                classification="POST_CANARY_HARDENING",
                disposition="POST_CANARY_BACKLOG",
            )
        ]
        progress = validate_initiative_progress(source)
        emitted = progress.as_dict()
        source["newRuntimeComponents"].clear()
        source["findings"][0]["findingId"] = "changed"

        self.assertEqual(emitted["newRuntimeComponents"], ["systemd-unit"])
        self.assertEqual(emitted["findings"][0]["findingId"], "F-1")
        self.assertEqual(validate_initiative_progress(emitted), progress)


if __name__ == "__main__":
    unittest.main()
