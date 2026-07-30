"""Offline adversarial contracts for the Phase 4.6A readiness seam."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import patch

import trip_planner
from trip_planner import EvidenceLedger, compose_trip_state
from trip_planner.codec import build_plan, plan_to_trip_state
from trip_planner.lodging import (
    LodgingRequirement,
    assess_lodging_intake,
)
from trip_planner.lodging_confirmation import (
    LodgingConfirmationProblem,
    LodgingConfirmationReview,
    LodgingConfirmationState,
)
from trip_planner.place_details import (
    PlaceDetailsAttemptBudget,
    PlaceDetailsKind,
    execute_google_place_details_batch,
)
from trip_planner.readiness import (
    CanonicalLodgingSummary,
    ReadinessAction,
    ReadinessStatus,
    TripReadiness,
    assess_trip_readiness,
)
from trip_planner.scheduling import trip_state_digest
from tests.test_phase4_composition import (
    ATTRIBUTION_LABEL,
    ATTRIBUTION_URI,
    EVALUATION_AT,
    NOW,
    _canonical_plan,
    _merge_routes,
    _policies,
    _route_key,
    _snapshot,
)
from tests.test_phase44_e2e import _plan as phase44_plan
from tests.test_phase44_place_details import (
    NOW as PLACE_NOW,
    _Fixture,
    _regular_body,
    _response,
    _Transport,
)
from tests.test_phase45_lodging_confirmation import _state


PRIVATE_SENTINEL = (
    "private-label-address-37.5000-127.0000-price-token-candidate"
)


def _empty_snapshot():
    return _snapshot(EvidenceLedger(_policies("unused-provider")))


def _not_required(start: date, end: date):
    return assess_lodging_intake(
        stay_start=start,
        stay_end=end,
        requirement=LodgingRequirement.NOT_REQUIRED,
    )


def _route_case(
    *,
    first_valid_until=EVALUATION_AT + timedelta(hours=2),
    second_valid_until=EVALUATION_AT + timedelta(hours=1),
    evaluation_at=EVALUATION_AT,
    purge_at=None,
):
    ledger, _observations = _merge_routes(
        EvidenceLedger(_policies("route-a")),
        provider="route-a",
        specs=(
            (
                _route_key("loc-a", "loc-b"),
                37.25,
                first_valid_until,
            ),
            (
                _route_key("loc-b", "loc-a"),
                40,
                second_valid_until,
            ),
        ),
        purge_at=purge_at,
    )
    snapshot = _snapshot(ledger, evaluation_at=evaluation_at)
    plan = _canonical_plan()
    composed = compose_trip_state(plan, snapshot)
    summary = CanonicalLodgingSummary.from_plan(
        plan,
        composed=composed,
    )
    intake = _not_required(
        date(2026, 10, 1),
        date(2026, 10, 2),
    )
    return plan, snapshot, composed, summary, intake


def _lodging_plan(*, split: bool):
    state = copy.deepcopy(_state(split=split))
    state["trip"].update(
        {
            "slug": "phase45-confirmation",
            "timezone": "Asia/Tokyo",
            "date_range": (
                "2026-12-10 ~ 2026-12-12"
                if split
                else "2026-08-01 ~ 2026-08-03"
            ),
        }
    )
    days = state["itinerary"]["days"]
    location_a = "lodging-location-" + "a" * 64
    if not split:
        state["trip"]["lodgings"] = [
            {
                "lodging_id": "stay-a",
                "location_id": location_a,
                "check_in": "2026-08-01",
                "check_out": "2026-08-04",
                "kind": "hotel",
                "decision_state": "booked",
                "evidence_state": "unverified",
            }
        ]
        for day in days:
            day.update(
                {
                    "start_lodging_id": "stay-a",
                    "start_location_id": location_a,
                    "end_lodging_id": "stay-a",
                    "end_location_id": location_a,
                }
            )
    else:
        location_b = "lodging-location-" + "b" * 64
        state["trip"]["lodgings"] = [
            {
                "lodging_id": "stay-a",
                "location_id": location_a,
                "check_in": "2026-12-10",
                "check_out": "2026-12-11",
                "kind": "hotel",
                "decision_state": "selected",
                "evidence_state": "unverified",
            },
            {
                "lodging_id": "stay-b",
                "location_id": location_b,
                "check_in": "2026-12-11",
                "check_out": "2026-12-13",
                "kind": "ryokan",
                "decision_state": "booked",
                "evidence_state": "unverified",
            },
        ]
        days[0].update(
            {
                "start_lodging_id": "stay-a",
                "start_location_id": location_a,
                "end_lodging_id": "stay-a",
                "end_location_id": location_a,
            }
        )
        days[1].update(
            {
                "start_lodging_id": "stay-a",
                "start_location_id": location_a,
                "end_lodging_id": "stay-b",
                "end_location_id": location_b,
            }
        )
        days[2].update(
            {
                "start_lodging_id": "stay-b",
                "start_location_id": location_b,
                "end_lodging_id": "stay-b",
                "end_location_id": location_b,
            }
        )
    return build_plan(
        trip_id="phase45-confirmation",
        generation=1,
        state=state,
    )


def _pending_review(composed, *, created_at):
    return LodgingConfirmationReview(
        state=LodgingConfirmationState.WAITING_CONFIRMATION,
        request_id="a" * 64,
        trip_id=composed.trip_id,
        base_revision=composed.plan_revision,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=30),
        stay_count=1,
        stay_night_count=1,
        affected_day_ids=("day-1",),
        decision_states=("selected",),
        reviewed_itinerary=True,
        patch_digest="sha256:" + "b" * 64,
        required_lodging_confirmation_scope="sha256:" + "c" * 64,
        expected_state_digest="d" * 64,
        expected_applied_revision="e" * 64,
        check_status="feasible",
    )


class ReadinessTests(unittest.TestCase):
    def test_fresh_exact_evidence_is_travel_ready_with_minimum_deadline(
        self,
    ) -> None:
        plan, snapshot, composed, _summary, intake = _route_case()

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=intake,
        )
        replay = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=intake,
        )

        self.assertEqual(ReadinessStatus.TRAVEL_READY, result.status)
        self.assertEqual(ReadinessAction.NONE, result.next_action)
        self.assertEqual(
            EVALUATION_AT + timedelta(hours=1),
            result.recheck_required_at,
        )
        self.assertEqual(2, result.used_evidence_count)
        self.assertEqual((), result.problems)
        self.assertEqual(result, replay)
        self.assertEqual(result.readiness_id, replay.readiness_id)
        self.assertIn(
            "指定時間前重新確認",
            result.to_dict()["summary_zh"],
        )

    def test_recheck_uses_earliest_retention_or_freshness_deadline(
        self,
    ) -> None:
        retention_deadline = EVALUATION_AT + timedelta(minutes=30)
        plan, snapshot, composed, _summary, intake = _route_case(
            purge_at=retention_deadline,
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=intake,
        )

        self.assertEqual(ReadinessStatus.TRAVEL_READY, result.status)
        self.assertEqual(
            retention_deadline,
            result.recheck_required_at,
        )

    def test_feasible_plan_without_required_external_facts_can_be_ready(
        self,
    ) -> None:
        plan = phase44_plan(
            trip_id="readiness-no-external-facts",
            city="Fixture",
            timezone_name="UTC",
        )
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=_not_required(
                date(2026, 7, 29),
                date(2026, 7, 30),
            ),
        )

        self.assertEqual(ReadinessStatus.TRAVEL_READY, result.status)
        self.assertIsNone(result.recheck_required_at)
        self.assertEqual([], result.to_dict()["problems"])
        self.assertNotIn(
            "指定時間",
            result.to_dict()["summary_zh"],
        )

    def test_stale_evidence_requires_refresh_without_fake_deadline(
        self,
    ) -> None:
        plan, snapshot, composed, _summary, intake = _route_case(
            first_valid_until=EVALUATION_AT,
            second_valid_until=EVALUATION_AT,
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=intake,
        )

        self.assertEqual(ReadinessStatus.REVIEW, result.status)
        self.assertEqual(ReadinessAction.REFRESH_EVIDENCE, result.next_action)
        self.assertIsNone(result.recheck_required_at)
        self.assertIn(
            "RECHECK_REQUIRED",
            {item.code for item in result.problems},
        )

    def test_retention_expired_is_distinct_from_stale(self) -> None:
        evaluation_at = NOW + timedelta(hours=24)
        plan, snapshot, composed, _summary, intake = _route_case(
            first_valid_until=NOW + timedelta(hours=4),
            second_valid_until=NOW + timedelta(hours=4),
            evaluation_at=evaluation_at,
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=intake,
        )
        codes = {item.code for item in result.problems}

        self.assertEqual(ReadinessStatus.REVIEW, result.status)
        self.assertIn("EVIDENCE_RETENTION_EXPIRED", codes)
        self.assertIn("PLAN_NEEDS_VERIFICATION", codes)
        self.assertIsNone(result.recheck_required_at)

    def test_missing_route_evidence_has_a_distinct_code(self) -> None:
        plan = _canonical_plan()
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=_not_required(
                date(2026, 10, 1),
                date(2026, 10, 2),
            ),
        )
        by_code = {item.code: item for item in result.problems}

        self.assertEqual(ReadinessStatus.REVIEW, result.status)
        self.assertEqual(2, by_code["EVIDENCE_MISSING"].affected_count)
        self.assertNotIn("EVIDENCE_CONFLICTED", by_code)
        self.assertNotIn("RECHECK_REQUIRED", by_code)

    def test_conflicted_route_evidence_has_priority_over_refresh(
        self,
    ) -> None:
        policies = _policies("route-a", "route-b")
        ledger, _observations = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=(
                (
                    _route_key("loc-a", "loc-b"),
                    20,
                    EVALUATION_AT + timedelta(hours=2),
                ),
                (
                    _route_key("loc-b", "loc-a"),
                    40,
                    EVALUATION_AT + timedelta(hours=2),
                ),
            ),
        )
        ledger, _observations = _merge_routes(
            ledger,
            provider="route-b",
            specs=(
                (
                    _route_key("loc-a", "loc-b"),
                    80,
                    EVALUATION_AT + timedelta(hours=2),
                ),
            ),
        )
        snapshot = _snapshot(ledger)
        plan = _canonical_plan()
        composed = compose_trip_state(plan, snapshot)

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=_not_required(
                date(2026, 10, 1),
                date(2026, 10, 2),
            ),
        )

        self.assertEqual(ReadinessStatus.REVIEW, result.status)
        self.assertEqual(ReadinessAction.RESOLVE_CONFLICT, result.next_action)
        self.assertEqual(
            "EVIDENCE_CONFLICTED",
            result.problems[0].code,
        )
        self.assertIsNone(result.recheck_required_at)

    def test_snapshot_binding_mismatch_fails_closed(self) -> None:
        plan, snapshot, composed, _summary, intake = _route_case()
        changed_binding = replace(
            composed.evidence,
            snapshot_id="b" * 64,
        )
        changed_composed = replace(
            composed,
            evidence=changed_binding,
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=changed_composed,
            snapshot=snapshot,
            lodging_intake=intake,
        )

        self.assertEqual(ReadinessStatus.DRAFT, result.status)
        self.assertIn(
            "EVIDENCE_BINDING_MISMATCH",
            {item.code for item in result.problems},
        )
        self.assertIsNone(result.recheck_required_at)

    def test_different_composed_view_requests_recomposition_not_plan_fix(
        self,
    ) -> None:
        plan = phase44_plan(
            trip_id="readiness-current-plan",
            city="Fixture",
            timezone_name="UTC",
        )
        other_plan = phase44_plan(
            trip_id="readiness-other-plan",
            city="Fixture",
            timezone_name="UTC",
        )
        snapshot = _empty_snapshot()
        other_composed = compose_trip_state(other_plan, snapshot)

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=other_composed,
            snapshot=snapshot,
            lodging_intake=_not_required(
                date(2026, 7, 29),
                date(2026, 7, 30),
            ),
        )

        self.assertEqual(ReadinessStatus.DRAFT, result.status)
        self.assertEqual(
            ReadinessAction.RECOMPOSE_TRIP_STATE,
            result.next_action,
        )
        self.assertNotEqual(
            ReadinessAction.FIX_INFEASIBLE_PLAN,
            result.next_action,
        )
        self.assertIn(
            "CANONICAL_BINDING_MISMATCH",
            {item.code for item in result.problems},
        )

    def test_missing_live_attribution_fails_closed(self) -> None:
        plan, snapshot, composed, _summary, intake = _route_case()
        forged = copy.copy(composed)
        object.__setattr__(forged, "live_attributions", ())

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=forged,
            snapshot=snapshot,
            lodging_intake=intake,
        )

        self.assertEqual(ReadinessStatus.DRAFT, result.status)
        self.assertIn(
            "LIVE_ATTRIBUTION_MISSING",
            {item.code for item in result.problems},
        )

    def test_stripped_used_evidence_and_attribution_fail_closed(
        self,
    ) -> None:
        plan, snapshot, composed, _summary, intake = _route_case()
        stripped_binding = replace(
            composed.evidence,
            used_observation_ids=(),
            required_attribution_labels=(),
            requires_live_attribution=False,
        )
        stripped = replace(
            composed,
            evidence=stripped_binding,
            live_attributions=(),
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=stripped,
            snapshot=snapshot,
            lodging_intake=intake,
        )

        self.assertEqual(ReadinessStatus.DRAFT, result.status)
        self.assertIn(
            "COMPOSITION_BINDING_MISMATCH",
            {item.code for item in result.problems},
        )
        self.assertEqual(2, result.used_evidence_count)
        self.assertIsNone(result.recheck_required_at)
        self.assertEqual(
            ReadinessAction.RECOMPOSE_TRIP_STATE,
            result.next_action,
        )

    def test_infeasible_plan_is_draft_and_fix_is_the_only_next_action(
        self,
    ) -> None:
        base = phase44_plan(
            trip_id="readiness-infeasible",
            city="Fixture",
            timezone_name="UTC",
        )
        state = copy.deepcopy(base["state"])
        state["itinerary"]["days"][0]["available_end"] = "10:30"
        plan = build_plan(
            trip_id="readiness-infeasible",
            generation=1,
            state=state,
        )
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=_not_required(
                date(2026, 7, 29),
                date(2026, 7, 30),
            ),
        )

        self.assertEqual(ReadinessStatus.DRAFT, result.status)
        self.assertEqual(
            ReadinessAction.FIX_INFEASIBLE_PLAN,
            result.next_action,
        )
        self.assertIn(
            "PLAN_INFEASIBLE",
            {item.code for item in result.problems},
        )

    def test_required_lodging_is_never_invented(self) -> None:
        plan = phase44_plan(
            trip_id="readiness-lodging-required",
            city="Fixture",
            timezone_name="UTC",
        )
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)
        intake = assess_lodging_intake(
            stay_start=date(2026, 7, 29),
            stay_end=date(2026, 7, 30),
            requirement=LodgingRequirement.REQUIRED,
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=intake,
        )

        self.assertEqual(ReadinessStatus.DRAFT, result.status)
        self.assertEqual(ReadinessAction.COMPLETE_LODGING, result.next_action)
        self.assertEqual(0, result.lodging_stay_count)
        self.assertIn(
            "LODGING_INCOMPLETE",
            {item.code for item in result.problems},
        )

    def test_lodging_intake_from_an_unrelated_span_is_rejected(
        self,
    ) -> None:
        plan = phase44_plan(
            trip_id="readiness-lodging-span",
            city="Fixture",
            timezone_name="UTC",
        )
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)
        unrelated = _not_required(
            date(2026, 1, 1),
            date(2026, 1, 2),
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=unrelated,
        )

        self.assertEqual(ReadinessStatus.DRAFT, result.status)
        self.assertIn(
            "LODGING_INTAKE_MISMATCH",
            {item.code for item in result.problems},
        )

    def test_busan_booked_lodging_is_review_not_verified(self) -> None:
        plan = _lodging_plan(split=False)
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)
        summary = CanonicalLodgingSummary.from_plan(
            plan,
            composed=composed,
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
        )

        self.assertEqual(1, summary.stay_count)
        self.assertEqual(3, summary.night_count)
        self.assertEqual(("booked",), summary.decision_states)
        self.assertEqual(("unverified",), summary.evidence_states)
        self.assertEqual(ReadinessStatus.REVIEW, result.status)
        self.assertIn(
            "LODGING_EVIDENCE_UNVERIFIED",
            {item.code for item in result.problems},
        )
        activities = {
            item.activity_id: item.scheduled_start.isoformat()
            for item in composed.state.activities
        }
        self.assertEqual("10:00:00", activities["booked-arrival"])
        self.assertEqual("18:00:00", activities["booked-dinner"])

    def test_hokkaido_split_stay_is_counted_without_selecting_a_winner(
        self,
    ) -> None:
        plan = _lodging_plan(split=True)
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)
        summary = CanonicalLodgingSummary.from_plan(
            plan,
            composed=composed,
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
        )

        self.assertEqual(2, summary.stay_count)
        self.assertEqual(3, summary.night_count)
        self.assertEqual(
            ("booked", "selected"),
            summary.decision_states,
        )
        self.assertEqual(ReadinessStatus.REVIEW, result.status)
        self.assertNotIn("winner", json.dumps(result.to_dict()))
        activity = composed.state.activities[0]
        self.assertEqual("16:00:00", activity.scheduled_start.isoformat())

    def test_pending_review_is_distinct_from_expired_review(self) -> None:
        plan = phase44_plan(
            trip_id="readiness-pending-review",
            city="Fixture",
            timezone_name="UTC",
        )
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)

        pending = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            pending_lodging_review=_pending_review(
                composed,
                created_at=EVALUATION_AT - timedelta(minutes=30),
            ),
        )
        expired = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            pending_lodging_review=_pending_review(
                composed,
                created_at=EVALUATION_AT - timedelta(minutes=31),
            ),
        )
        conflicting = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=_not_required(
                date(2026, 7, 29),
                date(2026, 7, 30),
            ),
            pending_lodging_review=_pending_review(
                composed,
                created_at=EVALUATION_AT - timedelta(minutes=5),
            ),
        )

        self.assertIn(
            "LODGING_CONFIRMATION_PENDING",
            {item.code for item in pending.problems},
        )
        self.assertNotIn(
            "LODGING_REVIEW_EXPIRED",
            {item.code for item in pending.problems},
        )
        self.assertIn(
            "LODGING_REVIEW_EXPIRED",
            {item.code for item in expired.problems},
        )
        self.assertEqual(
            ReadinessAction.CONFIRM_LODGING,
            pending.next_action,
        )
        self.assertEqual(
            EVALUATION_AT,
            pending.recheck_required_at,
        )
        self.assertIsNone(expired.recheck_required_at)
        self.assertEqual(
            ReadinessAction.RESTAGE_LODGING_REVIEW,
            expired.next_action,
        )
        self.assertIn(
            "LODGING_REVIEW_REQUIREMENT_CONFLICT",
            {item.code for item in conflicting.problems},
        )
        self.assertEqual(
            ReadinessAction.RESOLVE_CONFLICT,
            conflicting.next_action,
        )

    def test_rejected_lodging_review_requires_restage(self) -> None:
        plan = phase44_plan(
            trip_id="readiness-rejected-review",
            city="Fixture",
            timezone_name="UTC",
        )
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)
        rejected = replace(
            _pending_review(
                composed,
                created_at=EVALUATION_AT - timedelta(minutes=5),
            ),
            state=LodgingConfirmationState.REJECTED,
            review_id=None,
            problems=(
                LodgingConfirmationProblem(
                    code="REVIEW_REJECTED",
                    message="Fixture rejection.",
                ),
            ),
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            pending_lodging_review=rejected,
        )

        self.assertIn(
            "LODGING_REVIEW_REJECTED",
            {item.code for item in result.problems},
        )
        self.assertEqual(
            ReadinessAction.RESTAGE_LODGING_REVIEW,
            result.next_action,
        )

    def test_mismatched_lodging_review_requires_restage(self) -> None:
        plan = phase44_plan(
            trip_id="readiness-mismatched-review",
            city="Fixture",
            timezone_name="UTC",
        )
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)
        mismatched = replace(
            _pending_review(
                composed,
                created_at=EVALUATION_AT - timedelta(minutes=5),
            ),
            trip_id="different-trip",
            review_id=None,
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            pending_lodging_review=mismatched,
        )

        self.assertIn(
            "LODGING_REVIEW_MISMATCH",
            {item.code for item in result.problems},
        )
        self.assertEqual(
            ReadinessAction.RESTAGE_LODGING_REVIEW,
            result.next_action,
        )

    def test_regular_hours_never_become_travel_ready(self) -> None:
        fixture = _Fixture(place_id="ChIJ-readiness-regular")
        request = fixture.request(
            PlaceDetailsKind.REGULAR_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        batch = execute_google_place_details_batch(
            (request,),
            _Transport(
                _response(
                    _regular_body(
                        [
                            {
                                "open": {"day": 3, "hour": 9},
                                "close": {"day": 3, "hour": 17},
                            }
                        ],
                        place_id=fixture.place_id,
                        timezone_name="Asia/Tokyo",
                    )
                )
            ),
            session=fixture.session,
            attempt_budget=PlaceDetailsAttemptBudget(1),
            clock=lambda: PLACE_NOW,
        )
        plan = phase44_plan(
            trip_id="readiness-regular-hours",
            city="Hokkaido",
            timezone_name="Asia/Tokyo",
        )
        snapshot = batch.current.snapshot(evaluation_at=PLACE_NOW)
        composed = compose_trip_state(
            plan,
            snapshot,
            availability_keys=(request.provider_request.fact_keys[0],),
        )

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            availability_keys=(request.provider_request.fact_keys[0],),
            lodging_intake=_not_required(
                date(2026, 7, 29),
                date(2026, 7, 30),
            ),
        )

        self.assertEqual(ReadinessStatus.REVIEW, result.status)
        self.assertIn(
            "EVIDENCE_NOT_TRAVEL_READY",
            {item.code for item in result.problems},
        )
        self.assertIsNone(result.recheck_required_at)

    def test_factory_binding_rejects_untrusted_or_different_plan(self) -> None:
        plan, snapshot, composed, summary, _intake = _route_case()
        for public_name in (
            "CanonicalLodgingSummary",
            "READINESS_VERSION",
            "ReadinessAction",
            "ReadinessProblem",
            "ReadinessSource",
            "ReadinessStatus",
            "TripReadiness",
            "assess_trip_readiness",
        ):
            self.assertIn(public_name, trip_planner.__all__)
            self.assertTrue(hasattr(trip_planner, public_name))
        with self.assertRaises(ValueError):
            CanonicalLodgingSummary(
                trip_id=composed.trip_id,
                plan_revision=composed.plan_revision,
                canonical_state_digest=composed.canonical_state_digest,
                stay_count=0,
                night_count=0,
                decision_states=(),
                evidence_states=(),
                covered_nights=(),
            )
        with self.assertRaises(ValueError):
            TripReadiness(
                trip_ref="f" * 64,
                plan_revision=composed.plan_revision,
                canonical_state_digest=composed.canonical_state_digest,
                composed_state_digest=composed.composed_state_digest,
                kernel_report_digest="report-" + "0" * 64,
                canonical_lodging_digest=summary.binding_digest,
                evidence_binding_digest=composed.evidence.binding_digest,
                evidence_snapshot_id=snapshot.snapshot_id,
                evaluated_at=EVALUATION_AT,
                status=ReadinessStatus.TRAVEL_READY,
                recheck_required_at=None,
                used_evidence_count=0,
                lodging_stay_count=0,
                lodging_night_count=0,
                next_action=ReadinessAction.NONE,
                problems=(),
            )
        other = phase44_plan(
            trip_id="other-trip",
            city="Fixture",
            timezone_name="UTC",
        )
        other_composed = compose_trip_state(other, snapshot)
        with self.assertRaises(ValueError):
            CanonicalLodgingSummary.from_plan(
                plan,
                composed=other_composed,
            )

    def test_missing_legacy_slug_has_a_stable_canonical_projection(
        self,
    ) -> None:
        state = _state(split=False)
        state["trip"]["trip_id"] = "readiness-stable-fallback"
        plan = build_plan(
            trip_id="readiness-stable-fallback",
            generation=1,
            state=state,
        )
        snapshot = _empty_snapshot()
        first = plan_to_trip_state(plan)
        second = plan_to_trip_state(plan)
        composed = compose_trip_state(plan, snapshot)
        summary = CanonicalLodgingSummary.from_plan(
            plan,
            composed=composed,
        )

        self.assertEqual("readiness-stable-fallback", first.slug)
        self.assertEqual(first, second)
        self.assertEqual(
            trip_state_digest(first),
            summary.canonical_state_digest,
        )
        self.assertEqual(
            composed.canonical_state_digest,
            summary.canonical_state_digest,
        )

    def test_safe_outputs_are_bounded_and_assessment_is_read_only(
        self,
    ) -> None:
        (
            route_plan,
            route_snapshot,
            route_composed,
            route_summary,
            route_intake,
        ) = _route_case()
        lodging_plan = _lodging_plan(split=False)
        lodging_snapshot = _empty_snapshot()
        lodging_composed = compose_trip_state(
            lodging_plan,
            lodging_snapshot,
        )
        lodging_summary = CanonicalLodgingSummary.from_plan(
            lodging_plan,
            composed=lodging_composed,
        )
        plan_before = copy.deepcopy(route_plan)
        snapshot_before = copy.deepcopy(route_snapshot)
        composed_before = copy.deepcopy(route_composed)

        with patch(
            "builtins.open",
            side_effect=AssertionError("readiness must not perform file I/O"),
        ):
            route_result = assess_trip_readiness(
                canonical_plan=route_plan,
                composed=route_composed,
                snapshot=route_snapshot,
                lodging_intake=route_intake,
            )
            lodging_result = assess_trip_readiness(
                canonical_plan=lodging_plan,
                composed=lodging_composed,
                snapshot=lodging_snapshot,
            )

        self.assertEqual(plan_before, route_plan)
        self.assertEqual(snapshot_before, route_snapshot)
        self.assertEqual(composed_before, route_composed)
        serialized = json.dumps(
            {
                "route": route_result.to_dict(),
                "route_lodging": route_summary.to_dict(),
                "lodging": lodging_result.to_dict(),
                "lodging_summary": lodging_summary.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for forbidden in (
            PRIVATE_SENTINEL,
            ATTRIBUTION_LABEL,
            ATTRIBUTION_URI,
            "loc-a",
            "loc-b",
            "lodging-location-",
            "2026-08-01",
            "2026-08-04",
            "https://",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("check_report", serialized)
        self.assertNotIn("observations", serialized)

    def test_safe_output_hashes_a_hostile_canonical_trip_id(self) -> None:
        private_trip_id = (
            "https://secret.example/37.5000-price-999-provider-token"
        )
        plan = phase44_plan(
            trip_id=private_trip_id,
            city="Fixture",
            timezone_name="UTC",
        )
        snapshot = _empty_snapshot()
        composed = compose_trip_state(plan, snapshot)

        result = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=_not_required(
                date(2026, 7, 29),
                date(2026, 7, 30),
            ),
        )
        serialized = json.dumps(result.to_dict(), sort_keys=True)

        self.assertNotIn(private_trip_id, serialized)
        self.assertNotIn("https://", serialized)
        self.assertRegex(result.trip_ref, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
