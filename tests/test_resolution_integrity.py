import unittest
from datetime import datetime, timedelta, timezone

from council_tools.resolution_integrity import (
    ResolutionIntegrityError,
    validate_resolution_event_integrity,
)


OUTCOME_ID = "outcome-123"
FINGERPRINT = "a" * 64
ISSUED_AT = "2026-08-23T10:04:00Z"
AS_OF = "2026-08-24T00:00:00Z"


def outcome():
    return {
        "outcomeId": OUTCOME_ID,
        "resolutionDate": "2026-08-23",
        "fingerprint": FINGERPRINT,
    }


def event():
    return {
        "outcomeId": OUTCOME_ID,
        "resolutionDate": "2026-08-23",
        "outcomeFingerprint": FINGERPRINT,
        "resolvedAt": "2026-08-23T12:00:00-04:00",
    }


class ResolutionIntegrityTest(unittest.TestCase):
    def validate(self, resolution=None, issued_at=ISSUED_AT, as_of=AS_OF, **kwargs):
        return validate_resolution_event_integrity(
            resolution or event(),
            kwargs.pop("canonical_outcome", outcome()),
            issuance_at=issued_at,
            as_of=as_of,
            require_outcome_fingerprint=kwargs.pop(
                "require_outcome_fingerprint", True
            ),
            **kwargs,
        )

    def test_v2_resolution_exactly_binds_identity_date_and_fingerprint(self):
        validated = self.validate()
        self.assertEqual(validated.outcome_id, OUTCOME_ID)
        self.assertEqual(str(validated.resolution_date), "2026-08-23")
        self.assertEqual(validated.outcome_fingerprint, FINGERPRINT)
        self.assertEqual(
            validated.resolved_at,
            datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc),
        )

    def test_identity_date_and_fingerprint_mismatches_fail_closed(self):
        cases = (
            ("outcomeId", "outcome-other", "outcomeId differs"),
            ("resolutionDate", "2026-08-24", "resolutionDate differs"),
            ("outcomeFingerprint", "b" * 64, "outcomeFingerprint differs"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                resolution = event()
                resolution[field] = value
                with self.assertRaisesRegex(ResolutionIntegrityError, message):
                    self.validate(resolution)

    def test_v2_fingerprint_is_required_and_strict(self):
        resolution = event()
        del resolution["outcomeFingerprint"]
        with self.assertRaisesRegex(ResolutionIntegrityError, "must be non-empty"):
            self.validate(resolution)

        resolution = event()
        resolution["outcomeFingerprint"] = "A" * 64
        with self.assertRaisesRegex(ResolutionIntegrityError, "lowercase SHA-256"):
            self.validate(resolution)

        canonical = outcome()
        del canonical["fingerprint"]
        with self.assertRaisesRegex(ResolutionIntegrityError, "must be non-empty"):
            self.validate(canonical_outcome=canonical)

    def test_legacy_event_may_omit_fingerprint_but_cannot_supply_wrong_one(self):
        resolution = event()
        del resolution["outcomeFingerprint"]
        validated = self.validate(
            resolution, require_outcome_fingerprint=False
        )
        self.assertIsNone(validated.outcome_fingerprint)

        resolution["outcomeFingerprint"] = "b" * 64
        with self.assertRaisesRegex(ResolutionIntegrityError, "differs"):
            self.validate(resolution, require_outcome_fingerprint=False)

    def test_resolution_must_fall_within_issuance_and_report_boundaries(self):
        issuance = datetime(2026, 8, 23, 10, 4, tzinfo=timezone.utc)
        for resolved_at, message in (
            (
                (issuance - timedelta(microseconds=1)).isoformat(),
                "precedes issuance/completion",
            ),
            ("2026-08-24T00:00:00.000001Z", "follows report as_of"),
        ):
            with self.subTest(resolved_at=resolved_at):
                resolution = event()
                resolution["resolvedAt"] = resolved_at
                with self.assertRaisesRegex(ResolutionIntegrityError, message):
                    self.validate(resolution, issued_at=issuance)

    def test_equality_at_both_temporal_boundaries_is_allowed(self):
        resolution = event()
        resolution["resolvedAt"] = ISSUED_AT
        validated = self.validate(resolution, as_of=ISSUED_AT)
        self.assertEqual(validated.resolved_at, validated.issuance_at)
        self.assertEqual(validated.resolved_at, validated.report_as_of)

    def test_all_timestamps_must_be_timezone_aware(self):
        resolution = event()
        resolution["resolvedAt"] = "2026-08-23T12:00:00"
        with self.assertRaisesRegex(ResolutionIntegrityError, "include a timezone"):
            self.validate(resolution)
        with self.assertRaisesRegex(ResolutionIntegrityError, "include a timezone"):
            self.validate(issued_at=datetime(2026, 8, 23, 10, 4))
        with self.assertRaisesRegex(ResolutionIntegrityError, "include a timezone"):
            self.validate(as_of="2026-08-24T00:00:00")

    def test_malformed_cross_record_fields_fail_closed(self):
        resolution = event()
        resolution["resolutionDate"] = "2026-8-23"
        with self.assertRaisesRegex(ResolutionIntegrityError, "YYYY-MM-DD"):
            self.validate(resolution)
        with self.assertRaisesRegex(ResolutionIntegrityError, "must be an object"):
            validate_resolution_event_integrity(
                [],
                outcome(),
                issuance_at=ISSUED_AT,
                as_of=AS_OF,
                require_outcome_fingerprint=True,
            )
        with self.assertRaisesRegex(ResolutionIntegrityError, "must be boolean"):
            self.validate(require_outcome_fingerprint="yes")


if __name__ == "__main__":
    unittest.main()
