"""Phase 4.5A contracts for natural-language lodging intake."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone

import trip_planner
import trip_planner.lodging as lodging_module
from trip_planner.lodging import (
    IntentAuthority,
    LocationHint,
    LocationHintKind,
    LocationPrecision,
    LodgingCandidate,
    LodgingIntakeStatus,
    LodgingIntentDraft,
    LodgingKind,
    LodgingRequirement,
    PriceBasis,
    ReportedDecisionClaim,
    TransportBoundary,
    TransportBoundaryDraft,
    TransportBoundaryKind,
    assess_lodging_intake,
    bind_lodging_candidate,
    bind_transport_boundary,
)
from trip_planner.models import DecisionState, EvidenceState


UTC = timezone.utc
START = date(2026, 8, 1)
END = date(2026, 8, 4)


def _address(
    value: str = "Private street 99?token=do-not-log",
) -> LocationHint:
    return LocationHint(
        kind=LocationHintKind.ADDRESS,
        label="Private stay",
        input_text=value,
        country_code="jp",
    )


def _area(value: str = "Near Susukino station") -> LocationHint:
    return LocationHint(
        kind=LocationHintKind.AREA,
        label="Approximate area",
        input_text=value,
        country_code="jp",
    )


def _location(location_id: str = "stay-a") -> LocationHint:
    return LocationHint(
        kind=LocationHintKind.LOCATION_ID,
        label="Known stay",
        location_id=location_id,
        country_code="jp",
    )


def _draft(
    *,
    start: date = START,
    end: date = END,
    location: LocationHint | None = None,
    label: str = "Stay option",
    kind: LodgingKind = LodgingKind.UNSPECIFIED,
    decision: DecisionState = DecisionState.CANDIDATE,
    source_ref: str = "user-turn-1",
) -> LodgingIntentDraft:
    reported_decision = (
        None
        if decision is DecisionState.CANDIDATE
        else ReportedDecisionClaim(
            decision_state=decision,
            source_ref=source_ref,
        )
    )
    return LodgingIntentDraft(
        kind=kind,
        label=label,
        location=_area() if location is None else location,
        check_in=start,
        check_out=end,
        reported_decision=reported_decision,
    )


def _candidate(
    *,
    start: date = START,
    end: date = END,
    location: LocationHint | None = None,
    decision: DecisionState = DecisionState.CANDIDATE,
    authority: IntentAuthority = IntentAuthority.USER_STATED,
    label: str = "Stay option",
) -> LodgingCandidate:
    draft = _draft(
        start=start,
        end=end,
        location=location,
        label=label,
        decision=decision,
    )
    return bind_lodging_candidate(draft, authority=authority)


class NaturalLanguageIntakeTests(unittest.TestCase):
    def test_public_api_has_no_decision_promotion_path(self) -> None:
        self.assertTrue(hasattr(trip_planner, "bind_lodging_candidate"))
        self.assertTrue(hasattr(trip_planner, "bind_transport_boundary"))
        self.assertTrue(hasattr(trip_planner, "ReportedDecisionClaim"))
        for unsupported_name in (
            "RuntimeDecisionAuthority",
            "RuntimeDecisionGrant",
            "confirm_lodging_candidate",
            "confirm_transport_boundary",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

    def test_missing_lodging_stays_empty_and_requests_advice(self) -> None:
        result = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
        )

        self.assertEqual(LodgingIntakeStatus.MISSING, result.status)
        self.assertEqual((), result.candidates)
        self.assertEqual(
            (START, START + timedelta(days=1), START + timedelta(days=2)),
            result.option_missing_nights,
        )
        self.assertEqual(result.required_nights, result.undecided_nights)
        self.assertIn("suggest_lodging_options", result.advice)
        self.assertEqual(
            {"LODGING_INPUT_MISSING"},
            {item.code for item in result.issues},
        )

    def test_explicit_no_lodging_is_not_treated_as_missing(self) -> None:
        empty = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            requirement=LodgingRequirement.NOT_REQUIRED,
        )
        self.assertEqual(LodgingIntakeStatus.NOT_REQUIRED, empty.status)
        self.assertEqual(START, empty.stay_start)
        self.assertEqual(END, empty.stay_end)
        self.assertFalse(empty.needs_verification)

        conflict = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            requirement=LodgingRequirement.NOT_REQUIRED,
            candidates=(_candidate(),),
        )
        self.assertEqual(LodgingIntakeStatus.CONFLICTED, conflict.status)
        self.assertIn(
            "LODGING_NOT_REQUIRED_CONFLICT",
            {item.code for item in conflict.issues},
        )

    def test_options_request_does_not_invent_recommendations(self) -> None:
        result = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            requirement=LodgingRequirement.OPTIONS_WANTED,
        )

        self.assertEqual(
            LodgingIntakeStatus.SEEKING_OPTIONS,
            result.status,
        )
        self.assertEqual((), result.candidates)
        self.assertEqual(
            {"LODGING_OPTIONS_REQUESTED"},
            {item.code for item in result.issues},
        )

    def test_candidate_coverage_is_not_a_user_decision(self) -> None:
        option = _candidate(
            authority=IntentAuthority.AI_SUGGESTED,
        )
        result = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            candidates=(option,),
        )

        self.assertEqual(LodgingIntakeStatus.COMPARING, result.status)
        self.assertEqual((), result.option_missing_nights)
        self.assertEqual(result.required_nights, result.undecided_nights)
        self.assertIn(
            "LODGING_DECISION_REQUIRED",
            {item.code for item in result.issues},
        )

    def test_non_overlapping_selected_stays_can_cover_multiple_nights(
        self,
    ) -> None:
        first = _candidate(
            start=START,
            end=START + timedelta(days=1),
            decision=DecisionState.SELECTED,
            location=_location("stay-a"),
        )
        second = _candidate(
            start=START + timedelta(days=1),
            end=END,
            decision=DecisionState.SELECTED,
            location=_location("stay-b"),
        )
        result = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            candidates=(second, first),
        )

        self.assertEqual(
            LodgingIntakeStatus.AWAITING_CONFIRMATION,
            result.status,
        )
        self.assertEqual((), result.undecided_nights)
        self.assertEqual((), result.conflicting_nights)
        self.assertEqual(
            tuple(sorted((first.candidate_id, second.candidate_id))),
            result.needs_verification_candidate_ids,
        )

    def test_overlapping_user_decisions_fail_closed(self) -> None:
        first = _candidate(
            decision=DecisionState.SELECTED,
            location=_location("stay-a"),
        )
        second = _candidate(
            decision=DecisionState.SELECTED,
            location=_location("stay-b"),
        )
        result = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            candidates=(first, second),
        )

        self.assertEqual(LodgingIntakeStatus.CONFLICTED, result.status)
        self.assertEqual(result.required_nights, result.conflicting_nights)
        self.assertIn(
            "LODGING_DECISION_CLAIM_CONFLICT",
            {item.code for item in result.issues},
        )

    def test_partial_decision_lists_exact_uncovered_nights(self) -> None:
        first = _candidate(
            start=START,
            end=START + timedelta(days=1),
            decision=DecisionState.SELECTED,
        )
        result = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            candidates=(first,),
        )

        self.assertEqual(LodgingIntakeStatus.PARTIAL, result.status)
        self.assertEqual(
            (START + timedelta(days=1), START + timedelta(days=2)),
            result.undecided_nights,
        )

    def test_ai_or_provider_cannot_escalate_a_decision(self) -> None:
        draft = _draft(decision=DecisionState.BOOKED)
        for authority in (
            IntentAuthority.AI_SUGGESTED,
            IntentAuthority.PROVIDER_DISCOVERED,
        ):
            with self.subTest(authority=authority):
                with self.assertRaises(ValueError):
                    bind_lodging_candidate(
                        draft,
                        authority=authority,
                    )
        with self.assertRaises(ValueError):
            bind_lodging_candidate(
                draft,
                authority=IntentAuthority.USER_CONFIRMED,
            )

    def test_clear_user_decision_is_a_non_authoritative_claim(self) -> None:
        source_ref = "private-message-42"
        draft = _draft(
            decision=DecisionState.BOOKED,
            source_ref=source_ref,
        )
        candidate = bind_lodging_candidate(draft)
        result = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            candidates=(candidate,),
        )

        self.assertEqual(DecisionState.CANDIDATE, candidate.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, candidate.evidence_state)
        self.assertEqual(
            LodgingIntakeStatus.AWAITING_CONFIRMATION,
            result.status,
        )
        self.assertIn(
            "LODGING_DECISION_CLAIM_REQUIRES_CONFIRMATION",
            {item.code for item in result.issues},
        )
        rendered = repr(draft) + json.dumps(draft.to_dict(), sort_keys=True)
        self.assertNotIn(source_ref, rendered)

    def test_bound_values_require_the_trusted_binder(self) -> None:
        draft = _draft()
        with self.assertRaises(ValueError):
            LodgingCandidate(
                draft=draft,
                decision_state=DecisionState.CANDIDATE,
                evidence_state=EvidenceState.UNVERIFIED,
                authority=IntentAuthority.USER_STATED,
            )
        boundary_draft = TransportBoundaryDraft(
            kind=TransportBoundaryKind.ARRIVAL,
            location=_location("airport"),
            exact_at=datetime(2026, 8, 1, 3, tzinfo=UTC),
        )
        with self.assertRaises(ValueError):
            TransportBoundary(
                draft=boundary_draft,
                decision_state=DecisionState.SELECTED,
                authority=IntentAuthority.USER_STATED,
            )

    def test_direct_module_import_cannot_promote_candidate(self) -> None:
        draft = _draft(decision=DecisionState.BOOKED)
        with self.assertRaises(ValueError):
            LodgingCandidate(
                draft=draft,
                decision_state=DecisionState.BOOKED,
                evidence_state=EvidenceState.UNVERIFIED,
                authority=IntentAuthority.USER_CONFIRMED,
                _token=lodging_module._BINDING_TOKEN,  # type: ignore[attr-defined]
            )
        with self.assertRaises(ValueError):
            LodgingCandidate(
                draft=draft,
                decision_state=DecisionState.CANDIDATE,
                evidence_state=EvidenceState.VERIFIED,
                authority=IntentAuthority.USER_STATED,
                evidence_refs=(f"fact:{'a' * 64}",),
                _token=lodging_module._BINDING_TOKEN,  # type: ignore[attr-defined]
            )

    def test_intake_cannot_self_claim_verified_evidence(self) -> None:
        draft = _draft(location=_location())
        with self.assertRaises(TypeError):
            bind_lodging_candidate(
                draft,
                evidence_state=EvidenceState.VERIFIED,  # type: ignore[call-arg]
            )
        decided = _candidate(
            location=_location(),
            decision=DecisionState.BOOKED,
        )
        result = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            candidates=(decided,),
        )
        self.assertEqual(
            LodgingIntakeStatus.AWAITING_CONFIRMATION,
            result.status,
        )
        self.assertTrue(result.needs_verification)
        self.assertEqual(
            (decided.candidate_id,),
            result.needs_verification_candidate_ids,
        )

    def test_private_location_values_are_redacted(self) -> None:
        secret = (
            "https://airbnb.example/listing?"
            "guest=private&token=do-not-log"
        )
        location = _address(secret)
        draft = _draft(
            location=location,
            kind=LodgingKind.SHORT_TERM_RENTAL,
            label="Secret apartment address",
        )
        candidate = bind_lodging_candidate(
            draft,
            authority=IntentAuthority.USER_STATED,
        )
        rendered = (
            repr(location)
            + repr(draft)
            + repr(candidate)
            + json.dumps(candidate.to_dict(), sort_keys=True)
        )

        for private_value in (
            "airbnb.example",
            "guest=private",
            "do-not-log",
            "Secret apartment address",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertTrue(candidate.to_dict()["draft"]["location"][
            "has_private_text"
        ])

    def test_coordinate_and_area_shapes_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            LocationHint(
                LocationHintKind.COORDINATES,
                "partial",
                latitude=25,
            )
        with self.assertRaises(ValueError):
            LocationHint(
                LocationHintKind.COORDINATES,
                "nan",
                latitude=float("nan"),
                longitude=121,
            )
        with self.assertRaises(ValueError):
            LocationHint(
                LocationHintKind.AREA,
                "missing radius",
                latitude=25,
                longitude=121,
            )
        circle = LocationHint(
            LocationHintKind.AREA,
            "Sapporo center",
            input_text="Sapporo station area",
            latitude=43.068,
            longitude=141.350,
            radius_m=1_500,
        )
        self.assertEqual(LocationPrecision.APPROXIMATE, circle.precision)
        safe = json.dumps(circle.to_dict(), sort_keys=True)
        self.assertNotIn("43.068", safe)
        self.assertNotIn("141.35", safe)

    def test_invalid_dates_and_control_text_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            _draft(
                start=datetime(2026, 8, 1, tzinfo=UTC),  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            _draft(start=END, end=START)
        with self.assertRaises(ValueError):
            LocationHint(
                LocationHintKind.ADDRESS,
                "private\naddress",
                input_text="somewhere",
            )

    def test_optional_price_keeps_unknowns_explicit_and_redacted(self) -> None:
        without_price = _draft()
        self.assertFalse(without_price.to_dict()["has_price"])

        priced = LodgingIntentDraft(
            kind=LodgingKind.HOTEL,
            label="Hotel option",
            location=_location(),
            check_in=START,
            check_out=END,
            price_amount_minor=125_000,
            currency="twd",
            price_basis=PriceBasis.TOTAL,
            price_is_estimate=False,
        )
        safe = json.dumps(priced.to_dict(), sort_keys=True)
        self.assertNotIn("125000", safe)
        self.assertEqual("TWD", priced.currency)

        with self.assertRaises(ValueError):
            LodgingIntentDraft(
                kind=LodgingKind.HOTEL,
                label="partial price",
                location=_location(),
                check_in=START,
                check_out=END,
                price_amount_minor=1,
            )

    def test_transport_supports_exact_or_approximate_time(self) -> None:
        secret_location = _address("Airport pickup token=private")
        exact = TransportBoundaryDraft(
            kind=TransportBoundaryKind.ARRIVAL,
            location=secret_location,
            exact_at=datetime(
                2026,
                8,
                1,
                12,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            buffer_after_min=90,
        )
        window = TransportBoundaryDraft(
            kind=TransportBoundaryKind.DEPARTURE,
            location=_location("airport"),
            window_start=datetime(2026, 8, 4, 1, tzinfo=UTC),
            window_end=datetime(2026, 8, 4, 3, tzinfo=UTC),
            buffer_before_min=150,
        )
        bound = bind_transport_boundary(exact)

        self.assertEqual("exact", exact.time_shape)
        self.assertEqual("window", window.time_shape)
        self.assertEqual(datetime(2026, 8, 1, 3, tzinfo=UTC), exact.exact_at)
        self.assertEqual(DecisionState.CANDIDATE, bound.decision_state)
        rendered = repr(bound) + json.dumps(bound.to_dict(), sort_keys=True)
        self.assertNotIn("token=private", rendered)
        self.assertNotIn("2026-08-01T03", rendered)

    def test_transport_decision_wording_remains_a_claim(self) -> None:
        source_ref = "user-turn-fixed-flight"
        draft = TransportBoundaryDraft(
            kind=TransportBoundaryKind.ARRIVAL,
            location=_location("airport"),
            exact_at=datetime(2026, 8, 1, 3, tzinfo=UTC),
            reported_decision=ReportedDecisionClaim(
                decision_state=DecisionState.FIXED,
                source_ref=source_ref,
            ),
        )
        candidate = bind_transport_boundary(draft)
        self.assertEqual(DecisionState.CANDIDATE, candidate.decision_state)
        self.assertEqual(
            DecisionState.FIXED,
            draft.reported_decision.decision_state,
        )
        rendered = repr(draft) + json.dumps(draft.to_dict(), sort_keys=True)
        self.assertNotIn(source_ref, rendered)
        with self.assertRaises(TypeError):
            bind_transport_boundary(
                draft,
                decision_state=DecisionState.FIXED,  # type: ignore[call-arg]
            )

    def test_reported_claim_rejects_non_decision_states(self) -> None:
        for state in (
            DecisionState.CANDIDATE,
            DecisionState.CANCELLED,
            DecisionState.EXCLUDED,
        ):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    ReportedDecisionClaim(
                        decision_state=state,
                        source_ref="user-turn-1",
                    )

    def test_lodging_span_and_candidate_count_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            _draft(
                start=date(2026, 1, 1),
                end=date(2027, 1, 3),
            )
        with self.assertRaises(ValueError):
            assess_lodging_intake(
                stay_start=date.min,
                stay_end=date.max,
            )
        candidate = _candidate()
        with self.assertRaises(ValueError):
            assess_lodging_intake(
                stay_start=START,
                stay_end=END,
                candidates=(candidate,) * 257,
            )

    def test_assessment_is_deterministic_and_rejects_duplicates(self) -> None:
        first = _candidate(
            start=START,
            end=START + timedelta(days=1),
            location=_location("stay-a"),
        )
        second = _candidate(
            start=START + timedelta(days=1),
            end=END,
            location=_location("stay-b"),
        )
        forward = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            candidates=(first, second),
        )
        reverse = assess_lodging_intake(
            stay_start=START,
            stay_end=END,
            candidates=(second, first),
        )
        self.assertEqual(forward.assessment_id, reverse.assessment_id)
        with self.assertRaises(ValueError):
            assess_lodging_intake(
                stay_start=START,
                stay_end=END,
                candidates=(first, first),
            )


if __name__ == "__main__":
    unittest.main()
