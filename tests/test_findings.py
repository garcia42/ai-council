import copy
import json
import unittest

from council_tools.findings import (
    DISPOSITION_ORDER,
    FINDING_KEYS,
    SEAT_OWNED_FINDING_KEYS,
    FindingError,
    new_finding_group_id,
    new_finding_id,
    parse_visible_output_findings,
    seat_owned_finding_projection,
    summarize_findings,
    validate_findings,
    validate_visible_output_findings,
)


RUN_ID = "run-11111111111111111111111111111111"
OTHER_RUN_ID = "run-22222222222222222222222222222222"
GROUP_1 = "finding-group-11111111111111111111111111111111"
GROUP_2 = "finding-group-22222222222222222222222222222222"
GROUP_3 = "finding-group-33333333333333333333333333333333"

BASELINE = {
    "schemaVersion": 3,
    "knownConsiderations": [
        {
            "considerationId": "KC-01",
            "claim": "Forecast accuracy alone cannot measure novel useful findings.",
        },
        {
            "considerationId": "KC-13",
            "claim": "Finding co-occurrence measures overlap, not causal redundancy or replaceability.",
        },
    ],
}


def artifact(seat):
    character = {"code": "a", "ops": "b", "blind": "c"}[seat]
    return {
        "path": f"sha256/{character * 2}/{character * 64}",
        "bytes": 123,
        "sha256": character * 64,
    }


def disposition(kind="new-acted"):
    if kind == "already-known":
        return {
            "kind": kind,
            "considerationId": "KC-01",
            "quotedSubclaim": "Forecast accuracy alone",
        }
    if kind == "new-rejected":
        return {"kind": kind, "reason": "The proposed repair is outside this run."}
    if kind == "new-deferred":
        return {
            "kind": kind,
            "reason": "Requires an off-host rehearsal.",
            "reviewDate": "2026-09-30",
        }
    return {"kind": kind}


def finding(
    *,
    finding_id="finding-11111111111111111111111111111111",
    seat="code",
    group_id=GROUP_1,
    run_id=RUN_ID,
    disposition_kind="new-acted",
):
    return {
        "findingId": finding_id,
        "seatId": seat,
        "category": "integrity",
        "claim": "The report accepts an incomplete capture.",
        "severity": "block",
        "proposedAction": "Fail the report closed.",
        "evidenceSummary": "The fixture omits the output digest.",
        "group": {"findingGroupId": group_id, "runId": run_id},
        "operatorDisposition": disposition(disposition_kind),
    }


def declaration(seat):
    return {"kind": "no-findings", "seatId": seat, "outputArtifact": artifact(seat)}


def seat_projection(row):
    return seat_owned_finding_projection(
        row,
        run_id=RUN_ID,
        seat_id=row["seatId"],
    )


def validate(
    *,
    seats=("code",),
    findings=None,
    no_findings=None,
    baseline=None,
    artifacts=None,
    prior_findings=(),
):
    return validate_findings(
        run_id=RUN_ID,
        submitted_seats=list(seats),
        findings=[finding()] if findings is None else findings,
        no_findings=[] if no_findings is None else no_findings,
        baseline=BASELINE if baseline is None else baseline,
        output_artifacts={seat: artifact(seat) for seat in seats}
        if artifacts is None
        else artifacts,
        prior_findings=prior_findings,
    )


class FindingValidationTest(unittest.TestCase):
    def test_valid_finding_is_accepted(self):
        self.assertIsNone(validate())

    def test_generated_ids_use_stable_contract_shapes(self):
        first = new_finding_id()
        second = new_finding_id()
        group = new_finding_group_id()
        self.assertRegex(first, r"^finding-[0-9a-f]{32}$")
        self.assertRegex(group, r"^finding-group-[0-9a-f]{32}$")
        self.assertNotEqual(first, second)

    def test_finding_requires_exact_keys(self):
        row = finding()
        row["confidence"] = 0.9
        with self.assertRaisesRegex(FindingError, "unknown keys.*confidence"):
            validate(findings=[row])

        row = finding()
        del row["evidenceSummary"]
        with self.assertRaisesRegex(FindingError, "missing keys.*evidenceSummary"):
            validate(findings=[row])

    def test_group_requires_exact_keys_and_same_run(self):
        row = finding()
        row["group"]["operatorLabel"] = "same concern"
        with self.assertRaisesRegex(FindingError, "unknown keys.*operatorLabel"):
            validate(findings=[row])

        row = finding(run_id=OTHER_RUN_ID)
        with self.assertRaisesRegex(FindingError, "different run"):
            validate(findings=[row])

    def test_finding_must_belong_to_a_submitted_seat(self):
        with self.assertRaisesRegex(FindingError, "not a submitted seat"):
            validate(findings=[finding(seat="ops")])

    def test_stable_id_shapes_are_enforced(self):
        with self.assertRaisesRegex(FindingError, "invalid stable id"):
            validate(findings=[finding(finding_id="finding-1")])
        with self.assertRaisesRegex(FindingError, "invalid stable id"):
            validate(findings=[finding(group_id="finding-group-1")])

    def test_duplicate_finding_ids_fail(self):
        duplicate = "finding-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        rows = [
            finding(finding_id=duplicate, seat="code"),
            finding(finding_id=duplicate, seat="ops"),
        ]
        with self.assertRaisesRegex(FindingError, "duplicate findingId"):
            validate(seats=("code", "ops"), findings=rows)

    def test_finding_ids_are_unique_against_prior_runs(self):
        prior = finding()
        with self.assertRaisesRegex(FindingError, "duplicate findingId"):
            validate(findings=[finding()], prior_findings=[prior])

    def test_same_group_can_span_submitted_seats_within_one_run(self):
        rows = [
            finding(
                finding_id="finding-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                seat="code",
                group_id=GROUP_1,
            ),
            finding(
                finding_id="finding-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                seat="ops",
                group_id=GROUP_1,
            ),
        ]
        self.assertIsNone(validate(seats=("code", "ops"), findings=rows))

    def test_group_id_cannot_be_reused_across_runs(self):
        prior = finding(
            finding_id="finding-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            run_id=OTHER_RUN_ID,
        )
        current = finding(finding_id="finding-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        with self.assertRaisesRegex(FindingError, "crosses runs"):
            validate(findings=[current], prior_findings=[prior])

    def test_required_atomic_text_fields_reject_empty_and_non_text_values(self):
        for key in ("category", "claim", "severity", "proposedAction", "evidenceSummary"):
            with self.subTest(key=key):
                row = finding()
                row[key] = "   "
                with self.assertRaisesRegex(FindingError, key):
                    validate(findings=[row])
                row[key] = ["not", "atomic", "text"]
                with self.assertRaisesRegex(FindingError, key):
                    validate(findings=[row])

    def test_disposition_requires_exact_kind_specific_keys(self):
        for kind in DISPOSITION_ORDER:
            with self.subTest(kind=kind):
                row = finding(disposition_kind=kind)
                row["operatorDisposition"]["unexpected"] = True
                with self.assertRaisesRegex(FindingError, "invalid keys|unknown keys"):
                    validate(findings=[row])

        row = finding()
        row["operatorDisposition"] = {"kind": "operator-liked-it"}
        with self.assertRaisesRegex(FindingError, "not an allowed disposition"):
            validate(findings=[row])

    def test_rejected_and_deferred_require_reasons(self):
        for kind in ("new-rejected", "new-deferred"):
            with self.subTest(kind=kind):
                row = finding()
                row["operatorDisposition"] = {"kind": kind}
                with self.assertRaisesRegex(FindingError, "invalid keys"):
                    validate(findings=[row])

                row["operatorDisposition"] = {"kind": kind, "reason": ""}
                with self.assertRaisesRegex(FindingError, "reason"):
                    validate(findings=[row])

    def test_deferred_review_date_is_optional_but_strict(self):
        row = finding(disposition_kind="new-deferred")
        del row["operatorDisposition"]["reviewDate"]
        self.assertIsNone(validate(findings=[row]))

        row["operatorDisposition"]["reviewDate"] = "30 September 2026"
        with self.assertRaisesRegex(FindingError, "YYYY-MM-DD"):
            validate(findings=[row])

    def test_already_known_requires_valid_baseline_consideration(self):
        row = finding(disposition_kind="already-known")
        row["operatorDisposition"]["considerationId"] = "KC-404"
        with self.assertRaisesRegex(FindingError, "not present in the sealed baseline"):
            validate(findings=[row])

    def test_already_known_requires_exact_quoted_subclaim(self):
        row = finding(disposition_kind="already-known")
        row["operatorDisposition"]["quotedSubclaim"] = "forecast accuracy alone"
        with self.assertRaisesRegex(FindingError, "not an exact subclaim"):
            validate(findings=[row])

        row["operatorDisposition"]["quotedSubclaim"] = "Forecast accuracy alone "
        with self.assertRaisesRegex(FindingError, "leading or trailing"):
            validate(findings=[row])

    def test_duplicate_baseline_consideration_ids_fail_closed(self):
        baseline = copy.deepcopy(BASELINE)
        baseline["knownConsiderations"].append(
            {"considerationId": "KC-01", "claim": "A different claim."}
        )
        with self.assertRaisesRegex(FindingError, "duplicate baseline considerationId"):
            validate(baseline=baseline)


class SeatOriginatedFindingBindingTest(unittest.TestCase):
    def setUp(self):
        self.code_finding = finding()
        self.code_projection = seat_projection(self.code_finding)

    def validate_visible(
        self,
        *,
        visible=None,
        completion=None,
        seats=("code",),
        empty_seats=(),
    ):
        if visible is None:
            visible = {"code": [copy.deepcopy(self.code_projection)]}
        if completion is None:
            completion = [copy.deepcopy(self.code_finding)]
        return validate_visible_output_findings(
            run_id=RUN_ID,
            submitted_seats=seats,
            visible_findings_by_seat=visible,
            completion_findings=completion,
            no_findings_seats=empty_seats,
        )

    def test_projection_is_exact_seat_owned_shape_and_excludes_operator_fields(self):
        projection = seat_owned_finding_projection(
            self.code_finding,
            run_id=RUN_ID,
            seat_id="code",
        )
        self.assertEqual(set(projection), SEAT_OWNED_FINDING_KEYS)
        self.assertEqual(len(projection), 7)
        self.assertNotIn("group", projection)
        self.assertNotIn("operatorDisposition", projection)

    def test_exact_visible_finding_list_is_accepted(self):
        checked = self.validate_visible()
        self.assertEqual(checked, {"code": [self.code_projection]})

    def test_operator_cannot_invent_or_delete_findings(self):
        invented = copy.deepcopy(self.code_projection)
        invented["findingId"] = "finding-ffffffffffffffffffffffffffffffff"
        with self.assertRaisesRegex(FindingError, "do not exactly match"):
            self.validate_visible(visible={"code": [self.code_projection, invented]})

        with self.assertRaisesRegex(FindingError, "do not exactly match"):
            self.validate_visible(visible={"code": []})

    def test_operator_cannot_alter_any_seat_owned_field(self):
        mutations = {
            "findingId": "finding-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "category": "availability",
            "claim": "A different claim.",
            "severity": "warn",
            "proposedAction": "Ignore the incomplete capture.",
            "evidenceSummary": "Different evidence.",
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                altered = copy.deepcopy(self.code_projection)
                altered[key] = value
                with self.assertRaisesRegex(FindingError, "do not exactly match"):
                    self.validate_visible(visible={"code": [altered]})

    def test_operator_group_is_separate_from_exact_seat_fields(self):
        completion = copy.deepcopy(self.code_finding)
        completion["group"]["findingGroupId"] = GROUP_2
        checked = self.validate_visible(completion=[completion])
        self.assertEqual(checked, {"code": [self.code_projection]})

    def test_independent_cross_seat_outputs_can_share_one_operator_group(self):
        code = finding(
            finding_id="finding-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            seat="code",
            group_id=GROUP_1,
        )
        ops = finding(
            finding_id="finding-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            seat="ops",
            group_id=GROUP_1,
        )
        ops["category"] = "operations"
        ops["claim"] = "The same incomplete capture can escape the operator path."
        ops["severity"] = "high"
        ops["proposedAction"] = "Stop publication when custody is incomplete."
        ops["evidenceSummary"] = "The operations fixture omits the retained output."
        visible = {
            "code": [seat_projection(code)],
            "ops": [seat_projection(ops)],
        }

        checked = self.validate_visible(
            visible=visible,
            completion=[code, ops],
            seats=("code", "ops"),
        )
        self.assertEqual(checked, visible)
        summary = summarize_findings(
            run_id=RUN_ID,
            submitted_seats=("code", "ops"),
            findings=[code, ops],
            no_findings=[],
            baseline=BASELINE,
            output_artifacts={"code": artifact("code"), "ops": artifact("ops")},
        )
        self.assertEqual(summary["withinRunFindingOverlap"]["overlapGroupCount"], 1)

    def test_duplicate_visible_finding_id_fails(self):
        with self.assertRaisesRegex(FindingError, "duplicate findingId"):
            self.validate_visible(
                visible={
                    "code": [
                        copy.deepcopy(self.code_projection),
                        copy.deepcopy(self.code_projection),
                    ]
                }
            )

    def test_cross_seat_visible_finding_fails(self):
        cross_seat = copy.deepcopy(self.code_projection)
        cross_seat["seatId"] = "ops"
        with self.assertRaisesRegex(FindingError, "different seat"):
            self.validate_visible(visible={"code": [cross_seat]})

    def test_visible_items_require_exact_keys(self):
        with_disposition = copy.deepcopy(self.code_projection)
        with_disposition["operatorDisposition"] = {"kind": "new-acted"}
        with self.assertRaisesRegex(FindingError, "unknown keys.*operatorDisposition"):
            self.validate_visible(visible={"code": [with_disposition]})

        missing_evidence = copy.deepcopy(self.code_projection)
        del missing_evidence["evidenceSummary"]
        with self.assertRaisesRegex(FindingError, "missing keys.*evidenceSummary"):
            self.validate_visible(visible={"code": [missing_evidence]})

    def test_visible_list_requires_canonical_finding_id_order(self):
        first = finding(finding_id="finding-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        second = finding(finding_id="finding-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        with self.assertRaisesRegex(FindingError, "sorted by findingId"):
            self.validate_visible(
                visible={"code": [seat_projection(second), seat_projection(first)]},
                completion=[second, first],
            )

        checked = self.validate_visible(
            visible={"code": [seat_projection(first), seat_projection(second)]},
            completion=[second, first],
        )
        self.assertEqual(
            [row["findingId"] for row in checked["code"]],
            [first["findingId"], second["findingId"]],
        )

    def test_no_findings_preserves_exact_empty_path(self):
        checked = self.validate_visible(
            visible={"code": []},
            completion=[],
            empty_seats=("code",),
        )
        self.assertEqual(checked, {"code": []})

        with self.assertRaisesRegex(FindingError, "has visible findings"):
            self.validate_visible(
                visible={"code": [self.code_projection]},
                completion=[],
                empty_seats=("code",),
            )
        with self.assertRaisesRegex(FindingError, "no completion findings"):
            self.validate_visible(visible={"code": []}, completion=[])

    def test_all_submitted_outputs_need_one_visible_list(self):
        with self.assertRaisesRegex(FindingError, "exactly match submittedSeats"):
            self.validate_visible(
                visible={"code": [self.code_projection]},
                seats=("code", "ops"),
            )

    def test_visible_parser_rejects_non_list_and_operator_group(self):
        with self.assertRaisesRegex(FindingError, "must be a list"):
            parse_visible_output_findings(
                {"finding": self.code_projection},
                run_id=RUN_ID,
                seat_id="code",
            )
        with_group = copy.deepcopy(self.code_projection)
        with_group["group"] = {"findingGroupId": GROUP_1, "runId": OTHER_RUN_ID}
        with self.assertRaisesRegex(FindingError, "unknown keys.*group"):
            parse_visible_output_findings(
                [with_group],
                run_id=RUN_ID,
                seat_id="code",
            )


class EmptyFindingPathTest(unittest.TestCase):
    def test_seat_originated_bound_declaration_is_accepted(self):
        self.assertIsNone(validate(findings=[], no_findings=[declaration("code")]))

    def test_operator_only_empty_arrays_fail(self):
        with self.assertRaisesRegex(FindingError, "need findings or"):
            validate(findings=[], no_findings=[])

    def test_no_submitted_seats_needs_no_artificial_declaration(self):
        result = summarize_findings(
            run_id=RUN_ID,
            submitted_seats=[],
            findings=[],
            no_findings=[],
            baseline=BASELINE,
            output_artifacts={},
        )
        self.assertEqual(result["submittedSeatCount"], 0)
        self.assertEqual(result["emptyDeclarationRate"]["rate"], 0.0)

    def test_every_submitted_seat_needs_exactly_one_path(self):
        rows = [finding(seat="code")]
        with self.assertRaisesRegex(FindingError, r"\['ops'\]"):
            validate(seats=("code", "ops"), findings=rows, no_findings=[])

        with self.assertRaisesRegex(FindingError, "cannot have findings and no-findings"):
            validate(findings=[finding()], no_findings=[declaration("code")])

    def test_declaration_requires_exact_keys_and_kind(self):
        row = declaration("code")
        row["operator"] = "operator"
        with self.assertRaisesRegex(FindingError, "unknown keys.*operator"):
            validate(findings=[], no_findings=[row])

        row = declaration("code")
        row["kind"] = "empty-findings"
        with self.assertRaisesRegex(FindingError, "must be no-findings"):
            validate(findings=[], no_findings=[row])

    def test_declaration_must_bind_exact_submitted_output_artifact(self):
        row = declaration("code")
        row["outputArtifact"]["bytes"] += 1
        with self.assertRaisesRegex(FindingError, "does not match"):
            validate(findings=[], no_findings=[row])

        row = declaration("code")
        row["outputArtifact"]["extra"] = "not exact"
        with self.assertRaisesRegex(FindingError, "unknown keys.*extra"):
            validate(findings=[], no_findings=[row])

    def test_output_artifacts_exactly_match_submitted_seats(self):
        with self.assertRaisesRegex(FindingError, "exactly match"):
            validate(findings=[], no_findings=[declaration("code")], artifacts={})

        with self.assertRaisesRegex(FindingError, "exactly match"):
            validate(
                findings=[],
                no_findings=[declaration("code")],
                artifacts={"code": artifact("code"), "ops": artifact("ops")},
            )

    def test_duplicate_and_non_submitted_declarations_fail(self):
        with self.assertRaisesRegex(FindingError, "duplicate no-findings"):
            validate(
                findings=[],
                no_findings=[declaration("code"), declaration("code")],
            )

        with self.assertRaisesRegex(FindingError, "not a submitted seat"):
            validate(findings=[], no_findings=[declaration("ops")])


class FindingSummaryTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            finding(
                finding_id="finding-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                seat="code",
                group_id=GROUP_1,
                disposition_kind="already-known",
            ),
            finding(
                finding_id="finding-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                seat="ops",
                group_id=GROUP_1,
                disposition_kind="new-acted",
            ),
            finding(
                finding_id="finding-cccccccccccccccccccccccccccccccc",
                seat="code",
                group_id=GROUP_2,
                disposition_kind="new-rejected",
            ),
            finding(
                finding_id="finding-dddddddddddddddddddddddddddddddd",
                seat="ops",
                group_id=GROUP_3,
                disposition_kind="new-deferred",
            ),
        ]

    def summary(self, rows=None, seats=("ops", "code", "blind")):
        rows = self.rows if rows is None else rows
        return summarize_findings(
            run_id=RUN_ID,
            submitted_seats=seats,
            findings=rows,
            no_findings=[declaration("blind")],
            baseline=BASELINE,
            output_artifacts={seat: artifact(seat) for seat in seats},
        )

    def test_overlap_uses_distinct_seats_not_multiple_same_seat_findings(self):
        result = self.summary()
        overlap = result["withinRunFindingOverlap"]
        self.assertEqual(overlap["findingGroupCount"], 3)
        self.assertEqual(overlap["overlapGroupCount"], 1)
        self.assertEqual(
            overlap["overlapGroups"],
            [
                {
                    "findingGroupId": GROUP_1,
                    "findingIds": [
                        "finding-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "finding-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    ],
                    "submittedSeats": ["code", "ops"],
                }
            ],
        )

        same_seat = [
            finding(
                finding_id="finding-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                seat="code",
                group_id=GROUP_2,
            ),
            finding(
                finding_id="finding-ffffffffffffffffffffffffffffffff",
                seat="code",
                group_id=GROUP_2,
            ),
        ]
        result = summarize_findings(
            run_id=RUN_ID,
            submitted_seats=["code"],
            findings=same_seat,
            no_findings=[],
            baseline=BASELINE,
            output_artifacts={"code": artifact("code")},
        )
        self.assertEqual(result["withinRunFindingOverlap"]["overlapGroupCount"], 0)

    def test_unique_finding_coverage_is_explicitly_an_upper_bound(self):
        result = self.summary()
        upper = result["uniqueFindingCoverageUpperBounds"]
        self.assertEqual(
            upper["code"],
            {
                "findingGroupCountUpperBound": 1,
                "findingGroupShareUpperBound": 1 / 3,
                "findingGroupIds": [GROUP_2],
            },
        )
        self.assertEqual(upper["ops"]["findingGroupIds"], [GROUP_3])
        self.assertEqual(upper["blind"]["findingGroupCountUpperBound"], 0)

    def test_counts_empty_rate_and_operator_reported_mix(self):
        result = self.summary()
        self.assertEqual(result["submittedSeatCount"], 3)
        self.assertEqual(result["findingCount"], 4)
        self.assertEqual(
            result["findingsPerSubmittedSeat"], {"blind": 0, "code": 2, "ops": 2}
        )
        self.assertEqual(
            result["emptyDeclarationRate"],
            {"declarationCount": 1, "submittedSeatCount": 3, "rate": 1 / 3},
        )
        expected = {
            kind: {"findingCount": 1, "share": 0.25} for kind in DISPOSITION_ORDER
        }
        self.assertEqual(result["operatorReportedDispositionMix"], expected)

    def test_empty_valid_run_has_zero_safe_rates(self):
        result = summarize_findings(
            run_id=RUN_ID,
            submitted_seats=["code", "ops"],
            findings=[],
            no_findings=[declaration("ops"), declaration("code")],
            baseline=BASELINE,
            output_artifacts={"ops": artifact("ops"), "code": artifact("code")},
        )
        self.assertEqual(result["findingCount"], 0)
        self.assertEqual(result["emptyDeclarationRate"]["rate"], 1.0)
        self.assertEqual(
            {item["share"] for item in result["operatorReportedDispositionMix"].values()},
            {0.0},
        )
        self.assertEqual(
            result["uniqueFindingCoverageUpperBounds"]["code"][
                "findingGroupShareUpperBound"
            ],
            0.0,
        )

    def test_summary_is_deterministic_under_input_reordering(self):
        forward = self.summary()
        reverse = summarize_findings(
            run_id=RUN_ID,
            submitted_seats=["blind", "code", "ops"],
            findings=list(reversed(self.rows)),
            no_findings=[declaration("blind")],
            baseline=BASELINE,
            output_artifacts={
                "blind": artifact("blind"),
                "code": artifact("code"),
                "ops": artifact("ops"),
            },
        )
        self.assertEqual(
            json.dumps(forward, sort_keys=True), json.dumps(reverse, sort_keys=True)
        )

    def test_summary_labels_forbid_causal_and_redundancy_claims(self):
        serialized_keys = []

        def collect_keys(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    serialized_keys.append(key.casefold())
                    collect_keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect_keys(nested)

        collect_keys(self.summary())
        labels = " ".join(serialized_keys)
        for forbidden in (
            "causal",
            "redundancy",
            "replaceability",
            "decisionvalue",
            "marginalvalue",
            "calibrationproof",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, labels.replace("_", "").replace("-", ""))

    def test_seat_ids_are_data_not_report_labels(self):
        causal_seat = "causal-reviewer"
        output = {"path": "sha256/dd/" + "d" * 64, "bytes": 1, "sha256": "d" * 64}
        result = summarize_findings(
            run_id=RUN_ID,
            submitted_seats=[causal_seat],
            findings=[],
            no_findings=[
                {"kind": "no-findings", "seatId": causal_seat, "outputArtifact": output}
            ],
            baseline=BASELINE,
            output_artifacts={causal_seat: output},
        )
        self.assertEqual(result["findingsPerSubmittedSeat"], {causal_seat: 0})

    def test_public_finding_shape_is_frozen(self):
        self.assertEqual(
            FINDING_KEYS,
            {
                "findingId",
                "seatId",
                "category",
                "claim",
                "severity",
                "proposedAction",
                "evidenceSummary",
                "group",
                "operatorDisposition",
            },
        )


if __name__ == "__main__":
    unittest.main()
