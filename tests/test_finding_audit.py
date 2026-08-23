import copy
import hashlib
import json
import unittest

from council_tools.finding_audit import (
    FindingAuditError,
    assign_decision_family,
    audit_case_sha256,
    audit_protocol_sha256,
    build_blinded_audit_case,
    deterministic_family_selection,
    evaluate_adjudicator_agreement,
    make_adjudicator_result,
    make_audit_protocol,
    rehearse_audit_protocol,
    validate_adjudicator_pair,
    validate_adjudicator_result,
    validate_alias_map,
    validate_audit_assignment,
    validate_audit_case_packet,
    validate_audit_protocol,
    validate_protocol_rehearsal_certificate,
)


ACTIVATION = "activation-11111111111111111111111111111111"
RUN_1 = "run-11111111111111111111111111111111"
RUN_2 = "run-22222222222222222222222222222222"
ASSIGNED_AT = "2026-08-23T14:00:00.000000Z"
STARTED_AT = "2026-08-23T14:00:01.000000Z"
CAPTURED_AT = "2026-08-23T14:00:02.000000Z"


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def artifact():
    return {
        "path": "design/2026-08-23-council-activation-controls-amendment-027.md",
        "sha256": "a" * 64,
        "bytes": 3712,
    }


def protocol():
    return make_audit_protocol(frozen_protocol_artifact=artifact())


def family_for(wanted, audit_protocol=None):
    audit_protocol = protocol() if audit_protocol is None else audit_protocol
    for index in range(1000):
        family = f"family-test-{index:04d}"
        selected = deterministic_family_selection(
            audit_protocol,
            activation_id=ACTIVATION,
            decision_family_id=family,
        )["selected"]
        if selected is wanted:
            return family
    raise AssertionError("fixture could not find selection bucket")


def assignment(*, selected=True, audit_protocol=None):
    audit_protocol = protocol() if audit_protocol is None else audit_protocol
    return assign_decision_family(
        audit_protocol,
        activation_id=ACTIVATION,
        decision_family_id=family_for(selected, audit_protocol),
        run_id=RUN_1,
        assigned_at=ASSIGNED_AT,
        attempt_started_at=STARTED_AT,
        existing_assignments=[],
        first_observation=True,
    )


def source_subject(*, self_identification=False):
    answer = (
        "The captured check can pass. "
        "The output digest can be replaced after validation. "
        "The two failures share one custody boundary."
    )
    source_output = json.dumps(
        {"answer": answer, "capture": {"kind": "findings"}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "seatId": "code",
        "role": "voting",
        "modelId": "test-model-v1",
        "agentVersion": "reviewer-2026-08-23",
        "sourceOutputText": source_output,
        "sourceOutputSha256": sha(source_output),
        "visibleAnswer": answer,
        "selfIdentificationRisk": self_identification,
        "capturedFindings": [
            {
                "findingId": "finding-11111111111111111111111111111111",
                "seatId": "code",
                "category": "integrity",
                "claim": "The captured check can pass.",
                "severity": "block",
                "proposedAction": "Bind the check to the source descriptor.",
                "evidenceSummary": "The retained answer identifies a handoff.",
            }
        ],
    }


def case(audit_protocol=None, *, subject=None):
    audit_protocol = protocol() if audit_protocol is None else audit_protocol
    return build_blinded_audit_case(
        audit_protocol,
        assignment=assignment(audit_protocol=audit_protocol),
        case_run_id=RUN_1,
        captured_at=CAPTURED_AT,
        subjects=[source_subject() if subject is None else subject],
        first_capture_complete=True,
    )


def annotations(packet, *, variant="agree"):
    packet_subject = packet["subjects"][0]
    answer = packet_subject["visibleAnswer"]
    captured = "The captured check can pass."
    omitted = "The output digest can be replaced after validation."
    related = "The two failures share one custody boundary."

    def item(text, status, finding_ids, group, material, actionable, wrong=False):
        start = answer.index(text)
        return {
            "subjectAlias": packet_subject["subjectAlias"],
            "start": start,
            "end": start + len(text),
            "captureStatus": status,
            "capturedFindingIds": finding_ids,
            "localGroupId": group,
            "material": material,
            "actionable": actionable,
            "confidentlyWrong": wrong,
        }

    rows = [
        item(
            captured,
            "captured",
            ["finding-11111111111111111111111111111111"],
            "local-group-custody",
            True,
            True,
        ),
        item(
            omitted,
            "omitted",
            [],
            "local-group-custody",
            True,
            True,
        ),
        item(
            related,
            "omitted",
            [],
            "local-group-custody",
            False,
            False,
        ),
    ]
    if variant == "classify-disagree":
        rows[0]["confidentlyWrong"] = True
        rows[1]["confidentlyWrong"] = True
    elif variant == "group-disagree":
        rows[0]["localGroupId"] = "local-group-one"
        rows[1]["localGroupId"] = "local-group-two"
        rows[2]["localGroupId"] = "local-group-three"
    elif variant == "omit-different":
        rows[2]["captureStatus"] = "captured"
        rows[2]["capturedFindingIds"] = [
            "finding-11111111111111111111111111111111"
        ]
    elif variant != "agree":
        raise AssertionError(variant)
    return rows


def adjudication_result(
    audit_protocol,
    packet,
    identity_character,
    *,
    variant="agree",
    timestamp="2026-08-23T14:00:03.000000Z",
):
    return make_adjudicator_result(
        audit_protocol,
        packet=packet,
        adjudicator_identity_sha256=identity_character * 64,
        adjudicated_at=timestamp,
        annotations=annotations(packet, variant=variant),
    )


class ProtocolValidationTest(unittest.TestCase):
    def test_constructor_is_strict_and_content_bound(self):
        first = protocol()
        second = protocol()
        self.assertEqual(first, second)
        self.assertEqual(audit_protocol_sha256(first), audit_protocol_sha256(second))
        self.assertEqual(first["selection"]["modulus"], 5)
        self.assertEqual(first["selection"]["algorithm"], "sha256-domain-v1")
        self.assertEqual(first["adjudication"]["minimumAgreement"], 0.60)
        self.assertFalse(first["prospective"]["backfillAllowed"])
        self.assertEqual(first["prospective"]["denominatorEffect"], "none")

    def test_unknown_protocol_key_and_changed_frozen_rule_fail(self):
        value = protocol()
        value["status"] = "APPROVED"
        with self.assertRaisesRegex(FindingAuditError, "unknown keys.*status"):
            validate_audit_protocol(value)

        value = protocol()
        value["selection"]["modulus"] = 4
        with self.assertRaisesRegex(FindingAuditError, "differs from the frozen rule"):
            validate_audit_protocol(value)

        value = protocol()
        value["adjudication"]["minimumAgreement"] = 0.59
        with self.assertRaisesRegex(FindingAuditError, "minimum agreement"):
            validate_audit_protocol(value)

    def test_frozen_artifact_reference_is_safe_and_exact(self):
        with self.assertRaisesRegex(FindingAuditError, "safe relative path"):
            make_audit_protocol(
                frozen_protocol_artifact={
                    "path": "../amendment.md",
                    "sha256": "a" * 64,
                    "bytes": 1,
                }
            )
        with self.assertRaisesRegex(FindingAuditError, "missing keys"):
            make_audit_protocol(
                frozen_protocol_artifact={"path": "a", "sha256": "a" * 64}
            )


class AssignmentTest(unittest.TestCase):
    def test_selection_is_deterministic_and_domain_bound(self):
        audit_protocol = protocol()
        family = family_for(True, audit_protocol)
        first = deterministic_family_selection(
            audit_protocol, activation_id=ACTIVATION, decision_family_id=family
        )
        second = deterministic_family_selection(
            audit_protocol, activation_id=ACTIVATION, decision_family_id=family
        )
        self.assertEqual(first, second)
        changed_activation = "activation-22222222222222222222222222222222"
        changed = deterministic_family_selection(
            audit_protocol,
            activation_id=changed_activation,
            decision_family_id=family,
        )
        self.assertNotEqual(first["selectionDigest"], changed["selectionDigest"])

    def test_selected_and_non_selected_are_both_persisted_states(self):
        audit_protocol = protocol()
        chosen = assignment(selected=True, audit_protocol=audit_protocol)
        skipped = assignment(selected=False, audit_protocol=audit_protocol)
        self.assertTrue(chosen["selected"])
        self.assertEqual(chosen["assignmentState"], "selected")
        self.assertFalse(skipped["selected"])
        self.assertEqual(skipped["assignmentState"], "not-selected")
        self.assertEqual(skipped["provenance"], "first-observation-prospective")

    def test_retry_inherits_exact_first_assignment(self):
        audit_protocol = protocol()
        original = assignment(selected=False, audit_protocol=audit_protocol)
        inherited = assign_decision_family(
            audit_protocol,
            activation_id=ACTIVATION,
            decision_family_id=original["decisionFamilyId"],
            run_id=RUN_2,
            assigned_at="2026-08-23T15:00:00Z",
            attempt_started_at="2026-08-23T15:00:01Z",
            existing_assignments=[original],
            first_observation=False,
        )
        self.assertEqual(inherited, original)
        self.assertEqual(inherited["firstRunId"], RUN_1)

    def test_missing_prior_on_retry_is_prohibited_backfill(self):
        audit_protocol = protocol()
        with self.assertRaisesRegex(FindingAuditError, "backfill is prohibited"):
            assign_decision_family(
                audit_protocol,
                activation_id=ACTIVATION,
                decision_family_id=family_for(False, audit_protocol),
                run_id=RUN_2,
                assigned_at=ASSIGNED_AT,
                attempt_started_at=STARTED_AT,
                existing_assignments=[],
                first_observation=False,
            )

    def test_assignment_must_strictly_precede_attempt(self):
        audit_protocol = protocol()
        with self.assertRaisesRegex(FindingAuditError, "before the first attempt"):
            assign_decision_family(
                audit_protocol,
                activation_id=ACTIVATION,
                decision_family_id=family_for(True, audit_protocol),
                run_id=RUN_1,
                assigned_at=STARTED_AT,
                attempt_started_at=STARTED_AT,
                existing_assignments=[],
                first_observation=True,
            )

    def test_tampered_selection_fields_and_duplicate_prior_fail(self):
        audit_protocol = protocol()
        original = assignment(selected=True, audit_protocol=audit_protocol)
        changed = copy.deepcopy(original)
        changed["selected"] = False
        with self.assertRaisesRegex(FindingAuditError, "selected value mismatch"):
            validate_audit_assignment(changed, protocol=audit_protocol)
        changed = copy.deepcopy(original)
        changed["assignmentState"] = "not-selected"
        with self.assertRaisesRegex(FindingAuditError, "does not persist"):
            validate_audit_assignment(changed, protocol=audit_protocol)
        with self.assertRaisesRegex(FindingAuditError, "duplicate persisted assignments"):
            assign_decision_family(
                audit_protocol,
                activation_id=ACTIVATION,
                decision_family_id=original["decisionFamilyId"],
                run_id=RUN_2,
                assigned_at=ASSIGNED_AT,
                attempt_started_at=STARTED_AT,
                existing_assignments=[original, original],
                first_observation=False,
            )


class BlindedCaseTest(unittest.TestCase):
    def test_packet_and_alias_map_have_separate_identity_custody(self):
        audit_protocol = protocol()
        packet, alias_map = case(audit_protocol)
        packet_text = json.dumps(packet, sort_keys=True)
        alias_text = json.dumps(alias_map, sort_keys=True)
        for structural_value in ("code", "voting", "test-model-v1", "reviewer-2026-08-23"):
            self.assertNotIn(structural_value, packet_text)
            self.assertIn(structural_value, alias_text)
        for forbidden_key in ("seatId", "role", "modelId", "agentVersion", "mappings"):
            self.assertNotIn(forbidden_key, packet_text)
        self.assertRegex(packet["subjects"][0]["subjectAlias"], r"^subject-[0-9a-f]{12}$")
        self.assertEqual(alias_map["auditCaseSha256"], audit_case_sha256(packet, protocol=audit_protocol))
        self.assertEqual(validate_alias_map(alias_map, packet=packet, protocol=audit_protocol), alias_map)

    def test_packet_preserves_exact_answer_and_normalized_findings(self):
        audit_protocol = protocol()
        source = source_subject()
        packet, _ = case(audit_protocol, subject=source)
        subject = packet["subjects"][0]
        self.assertEqual(subject["visibleAnswer"], source["visibleAnswer"])
        self.assertEqual(subject["sourceOutputSha256"], source["sourceOutputSha256"])
        self.assertEqual(subject["visibleAnswerSha256"], sha(source["visibleAnswer"]))
        self.assertEqual(len(subject["capturedFindings"]), 1)
        self.assertNotIn("seatId", subject["capturedFindings"][0])

    def test_self_identification_is_reported_not_claimed_blind(self):
        audit_protocol = protocol()
        source = source_subject(self_identification=True)
        packet, _ = case(audit_protocol, subject=source)
        self.assertEqual(packet["anonymizationRisk"], "reported")
        self.assertEqual(packet["subjects"][0]["anonymizationRisk"], "reported")

        source = source_subject()
        source["visibleAnswer"] += " I am code."
        source["sourceOutputText"] = json.dumps(
            {"answer": source["visibleAnswer"], "capture": {"kind": "findings"}},
            sort_keys=True,
            separators=(",", ":"),
        )
        source["sourceOutputSha256"] = sha(source["sourceOutputText"])
        packet, _ = case(audit_protocol, subject=source)
        self.assertEqual(packet["anonymizationRisk"], "reported")

    def test_output_digest_and_finding_seat_are_verified_before_blinding(self):
        audit_protocol = protocol()
        source = source_subject()
        source["sourceOutputSha256"] = "f" * 64
        with self.assertRaisesRegex(FindingAuditError, "does not bind sourceOutputText"):
            case(audit_protocol, subject=source)

        source = source_subject()
        source["capturedFindings"][0]["seatId"] = "ops"
        with self.assertRaisesRegex(FindingAuditError, "belongs to another subject"):
            case(audit_protocol, subject=source)

    def test_visible_answer_is_exactly_extracted_from_strict_source_output(self):
        audit_protocol = protocol()
        source = source_subject()
        source["visibleAnswer"] = "substituted answer"
        with self.assertRaisesRegex(FindingAuditError, "not the exact answer"):
            case(audit_protocol, subject=source)

        source = source_subject()
        answer = source["visibleAnswer"]
        source["sourceOutputText"] = (
            '{"answer":' + json.dumps(answer) + ',"answer":"duplicate","capture":{}}'
        )
        source["sourceOutputSha256"] = sha(source["sourceOutputText"])
        with self.assertRaisesRegex(FindingAuditError, "duplicate JSON keys"):
            case(audit_protocol, subject=source)

    def test_not_selected_or_non_first_complete_case_fails(self):
        audit_protocol = protocol()
        with self.assertRaisesRegex(FindingAuditError, "not-selected"):
            build_blinded_audit_case(
                audit_protocol,
                assignment=assignment(selected=False, audit_protocol=audit_protocol),
                case_run_id=RUN_1,
                captured_at=CAPTURED_AT,
                subjects=[source_subject()],
                first_capture_complete=True,
            )
        with self.assertRaisesRegex(FindingAuditError, "first capture-complete"):
            build_blinded_audit_case(
                audit_protocol,
                assignment=assignment(audit_protocol=audit_protocol),
                case_run_id=RUN_2,
                captured_at=CAPTURED_AT,
                subjects=[source_subject()],
                first_capture_complete=False,
            )

    def test_existing_family_case_denies_replacement_or_backfill(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        with self.assertRaisesRegex(FindingAuditError, "already has a case"):
            build_blinded_audit_case(
                audit_protocol,
                assignment=assignment(audit_protocol=audit_protocol),
                case_run_id=RUN_2,
                captured_at="2026-08-23T14:00:03Z",
                subjects=[source_subject()],
                first_capture_complete=True,
                prior_case_packets=[packet],
            )

    def test_packet_and_alias_map_tampering_fail(self):
        audit_protocol = protocol()
        packet, alias_map = case(audit_protocol)
        changed_packet = copy.deepcopy(packet)
        changed_packet["subjects"][0]["visibleAnswer"] += " changed"
        with self.assertRaisesRegex(FindingAuditError, "visibleAnswerSha256 does not bind"):
            validate_audit_case_packet(changed_packet, protocol=audit_protocol)
        changed_map = copy.deepcopy(alias_map)
        changed_map["mappings"][0]["sourceOutputSha256"] = "f" * 64
        with self.assertRaisesRegex(FindingAuditError, "source output digest mismatch"):
            validate_alias_map(changed_map, packet=packet, protocol=audit_protocol)
        changed_map = copy.deepcopy(alias_map)
        changed_map["mappings"][0]["role"] = "shadow"
        with self.assertRaisesRegex(FindingAuditError, "opaque alias does not bind"):
            validate_alias_map(changed_map, packet=packet, protocol=audit_protocol)


class AdjudicatorResultTest(unittest.TestCase):
    def test_result_binds_exact_spans_outputs_and_non_scalar_fields(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        result = adjudication_result(audit_protocol, packet, "b")
        self.assertEqual(validate_adjudicator_result(result, packet=packet, protocol=audit_protocol), result)
        omitted = [
            claim
            for claim in result["claims"]
            if claim["captureStatus"] == "omitted" and claim["actionable"]
        ]
        self.assertEqual(len(omitted), 1)
        self.assertIs(omitted[0]["confidentlyWrong"], False)
        for claim in result["claims"]:
            source = packet["subjects"][0]["visibleAnswer"]
            self.assertEqual(source[claim["start"] : claim["end"]], claim["quotedText"])
            self.assertEqual(sha(claim["quotedText"]), claim["quotedSpanSha256"])

    def test_tampered_span_quote_digest_and_source_output_fail(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        result = adjudication_result(audit_protocol, packet, "b")
        changed = copy.deepcopy(result)
        changed["claims"][0]["quotedText"] = "forged"
        with self.assertRaisesRegex(FindingAuditError, "does not match source span"):
            validate_adjudicator_result(changed, packet=packet, protocol=audit_protocol)
        changed = copy.deepcopy(result)
        changed["claims"][0]["quotedSpanSha256"] = "f" * 64
        with self.assertRaisesRegex(FindingAuditError, "quotedSpanSha256 mismatch"):
            validate_adjudicator_result(changed, packet=packet, protocol=audit_protocol)
        changed = copy.deepcopy(result)
        changed["claims"][0]["sourceOutputSha256"] = "f" * 64
        with self.assertRaisesRegex(FindingAuditError, "sourceOutputSha256 mismatch"):
            validate_adjudicator_result(changed, packet=packet, protocol=audit_protocol)

    def test_capture_status_requires_consistent_finding_references(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        rows = annotations(packet)
        rows[0]["capturedFindingIds"] = []
        with self.assertRaisesRegex(FindingAuditError, "captured claims require"):
            make_adjudicator_result(
                audit_protocol,
                packet=packet,
                adjudicator_identity_sha256="b" * 64,
                adjudicated_at=CAPTURED_AT,
                annotations=rows,
            )
        rows = annotations(packet)
        rows[1]["capturedFindingIds"] = ["finding-11111111111111111111111111111111"]
        with self.assertRaisesRegex(FindingAuditError, "omitted claims cannot"):
            make_adjudicator_result(
                audit_protocol,
                packet=packet,
                adjudicator_identity_sha256="b" * 64,
                adjudicated_at=CAPTURED_AT,
                annotations=rows,
            )

    def test_duplicate_source_spans_and_unknown_finding_fail(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        rows = annotations(packet)
        rows.append(copy.deepcopy(rows[0]))
        with self.assertRaisesRegex(FindingAuditError, "duplicate source spans"):
            make_adjudicator_result(
                audit_protocol,
                packet=packet,
                adjudicator_identity_sha256="b" * 64,
                adjudicated_at=CAPTURED_AT,
                annotations=rows,
            )
        rows = annotations(packet)
        rows[0]["capturedFindingIds"] = ["finding-ffffffffffffffffffffffffffffffff"]
        with self.assertRaisesRegex(FindingAuditError, "outside its subject"):
            make_adjudicator_result(
                audit_protocol,
                packet=packet,
                adjudicator_identity_sha256="b" * 64,
                adjudicated_at=CAPTURED_AT,
                annotations=rows,
            )

    def test_result_id_is_content_bound(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        result = adjudication_result(audit_protocol, packet, "b")
        changed = copy.deepcopy(result)
        changed["claims"][0]["material"] = False
        with self.assertRaisesRegex(FindingAuditError, "claimId mismatch"):
            validate_adjudicator_result(changed, packet=packet, protocol=audit_protocol)
        changed = copy.deepcopy(result)
        changed["auditResultId"] = "audit-result-" + "f" * 32
        with self.assertRaisesRegex(FindingAuditError, "auditResultId mismatch"):
            validate_adjudicator_result(changed, packet=packet, protocol=audit_protocol)


class AgreementTest(unittest.TestCase):
    def test_exactly_two_distinct_adjudicators_are_required(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        first = adjudication_result(audit_protocol, packet, "a")
        second = adjudication_result(audit_protocol, packet, "b")
        with self.assertRaisesRegex(FindingAuditError, "exactly two"):
            validate_adjudicator_pair([first], packet=packet, protocol=audit_protocol)
        duplicate_identity = adjudication_result(
            audit_protocol,
            packet,
            "a",
            timestamp="2026-08-23T14:00:04.000000Z",
        )
        with self.assertRaisesRegex(FindingAuditError, "distinct identity"):
            validate_adjudicator_pair(
                [first, duplicate_identity], packet=packet, protocol=audit_protocol
            )
        self.assertEqual(len(validate_adjudicator_pair([first, second], packet=packet, protocol=audit_protocol)), 2)

    def test_agreement_metrics_are_separate_and_fully_measured(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        first = adjudication_result(audit_protocol, packet, "a")
        second = adjudication_result(audit_protocol, packet, "b")
        evaluation = evaluate_adjudicator_agreement(
            audit_protocol, packet=packet, results=[first, second]
        )
        self.assertEqual(evaluation["gateStatus"], "eligible")
        self.assertEqual(set(evaluation["metrics"]), {
            "classificationAgreement",
            "pairwiseGroupingAgreement",
            "omittedSpanOverlap",
        })
        for metric in evaluation["metrics"].values():
            self.assertEqual(metric["status"], "measured")
            self.assertEqual(metric["value"], 1.0)
        self.assertEqual(evaluation["denominatorEffect"], "none")

    def test_each_applicable_metric_below_sixty_percent_voids_gate(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        first = adjudication_result(audit_protocol, packet, "a")
        cases = (
            ("classify-disagree", "classificationAgreement"),
            ("group-disagree", "pairwiseGroupingAgreement"),
            ("omit-different", "omittedSpanOverlap"),
        )
        for variant, metric_name in cases:
            with self.subTest(metric=metric_name):
                second = adjudication_result(audit_protocol, packet, "b", variant=variant)
                evaluation = evaluate_adjudicator_agreement(
                    audit_protocol, packet=packet, results=[first, second]
                )
                self.assertEqual(evaluation["gateStatus"], "void")
                self.assertLess(evaluation["metrics"][metric_name]["value"], 0.60)
                self.assertTrue(
                    any(reason.startswith(metric_name + ":") for reason in evaluation["gateVoidReasons"])
                )

    def test_unmeasurable_applicable_grouping_voids_gate(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        one_annotation = annotations(packet)[:1]
        first = make_adjudicator_result(
            audit_protocol,
            packet=packet,
            adjudicator_identity_sha256="a" * 64,
            adjudicated_at=CAPTURED_AT,
            annotations=one_annotation,
        )
        second = make_adjudicator_result(
            audit_protocol,
            packet=packet,
            adjudicator_identity_sha256="b" * 64,
            adjudicated_at=CAPTURED_AT,
            annotations=one_annotation,
        )
        evaluation = evaluate_adjudicator_agreement(
            audit_protocol, packet=packet, results=[first, second]
        )
        metric = evaluation["metrics"]["pairwiseGroupingAgreement"]
        self.assertTrue(metric["applicable"])
        self.assertEqual(metric["status"], "unmeasurable")
        self.assertIsNone(metric["value"])
        self.assertEqual(evaluation["gateStatus"], "void")

    def test_no_omitted_claims_makes_overlap_not_applicable(self):
        audit_protocol = protocol()
        packet, _ = case(audit_protocol)
        rows = annotations(packet)
        for row in rows[1:]:
            row["captureStatus"] = "captured"
            row["capturedFindingIds"] = [
                "finding-11111111111111111111111111111111"
            ]
        first = make_adjudicator_result(
            audit_protocol,
            packet=packet,
            adjudicator_identity_sha256="a" * 64,
            adjudicated_at=CAPTURED_AT,
            annotations=rows,
        )
        second = make_adjudicator_result(
            audit_protocol,
            packet=packet,
            adjudicator_identity_sha256="b" * 64,
            adjudicated_at=CAPTURED_AT,
            annotations=rows,
        )
        evaluation = evaluate_adjudicator_agreement(
            audit_protocol, packet=packet, results=[first, second]
        )
        metric = evaluation["metrics"]["omittedSpanOverlap"]
        self.assertFalse(metric["applicable"])
        self.assertEqual(metric["status"], "not-applicable")
        self.assertEqual(evaluation["gateStatus"], "eligible")


class ProtocolRehearsalTest(unittest.TestCase):
    def test_rehearsal_certificate_is_source_protocol_and_artifact_bound(self):
        audit_protocol = protocol()
        certificate = rehearse_audit_protocol(
            audit_protocol,
            runtime_commit="b" * 40,
            source_tree_sha256="c" * 64,
            rehearsed_at="2026-08-23T15:00:00Z",
        )
        self.assertEqual(
            validate_protocol_rehearsal_certificate(certificate, protocol=audit_protocol),
            certificate,
        )
        self.assertEqual(certificate["runtimeCommit"], "b" * 40)
        self.assertEqual(certificate["sourceTreeSha256"], "c" * 64)
        self.assertEqual(certificate["frozenProtocolArtifact"], artifact())
        self.assertEqual(certificate["protocolSha256"], audit_protocol_sha256(audit_protocol))
        self.assertTrue(all(certificate["checks"].values()))
        self.assertEqual(
            set(certificate["checks"]),
            {
                "deterministicSelection",
                "assignedBeforeAttempt",
                "persistedNonSelection",
                "retryInheritance",
                "noBackfill",
                "packetBlinding",
                "aliasMapExcluded",
                "selfIdentificationRiskReported",
                "sourceOutputDigestBound",
                "quotedSpanDigestBound",
                "omittedClaimDetection",
                "localRegroupingRecorded",
                "materialActionableClassified",
                "confidentlyWrongNonScalar",
                "twoAdjudicatorSupport",
                "separateAgreementMetrics",
                "unmeasurableGateVoid",
                "belowThresholdGateVoid",
                "prospectiveCountsRemainZero",
            },
        )
        self.assertRegex(certificate["certificateSha256"], r"^[0-9a-f]{64}$")

    def test_synthetic_rehearsal_never_claims_prospective_observations(self):
        certificate = rehearse_audit_protocol(
            protocol(),
            runtime_commit="b" * 40,
            source_tree_sha256="c" * 64,
            rehearsed_at="2026-08-23T15:00:00Z",
        )
        self.assertGreater(certificate["syntheticRehearsalCounts"]["assignments"], 0)
        self.assertEqual(
            certificate["actualProspectiveCounts"],
            {
                "assignments": 0,
                "selectedFamilies": 0,
                "auditCases": 0,
                "adjudicatorResults": 0,
            },
        )

    def test_certificate_status_cannot_replace_computed_evidence(self):
        audit_protocol = protocol()
        certificate = rehearse_audit_protocol(
            audit_protocol,
            runtime_commit="b" * 40,
            source_tree_sha256="c" * 64,
            rehearsed_at="2026-08-23T15:00:00Z",
        )
        certificate["status"] = "APPROVED"
        with self.assertRaisesRegex(FindingAuditError, "unknown keys.*status"):
            validate_protocol_rehearsal_certificate(certificate, protocol=audit_protocol)

    def test_tampered_binding_check_or_nonzero_counts_fail(self):
        audit_protocol = protocol()
        baseline = rehearse_audit_protocol(
            audit_protocol,
            runtime_commit="b" * 40,
            source_tree_sha256="c" * 64,
            rehearsed_at="2026-08-23T15:00:00Z",
        )
        changed = copy.deepcopy(baseline)
        changed["runtimeCommit"] = "d" * 40
        with self.assertRaisesRegex(FindingAuditError, "content digest mismatch"):
            validate_protocol_rehearsal_certificate(changed, protocol=audit_protocol)
        changed = copy.deepcopy(baseline)
        changed["checks"]["packetBlinding"] = False
        with self.assertRaisesRegex(FindingAuditError, "failed rehearsal check"):
            validate_protocol_rehearsal_certificate(changed, protocol=audit_protocol)
        changed = copy.deepcopy(baseline)
        changed["actualProspectiveCounts"]["assignments"] = 1
        with self.assertRaisesRegex(FindingAuditError, "must remain zero"):
            validate_protocol_rehearsal_certificate(changed, protocol=audit_protocol)


if __name__ == "__main__":
    unittest.main()
