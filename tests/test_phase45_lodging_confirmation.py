"""Offline contracts for Phase 4.5D canonical lodging confirmation."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from trip_planner.codec import PlanCodecError, build_plan, encode_plan
from trip_planner.facts import FactContractError
from trip_planner.lodging import LodgingKind
from trip_planner.lodging_itinerary import (
    LodgingItineraryStatus,
    LodgingOptionDisposition,
)
from trip_planner.lodging_confirmation import (
    LodgingConfirmationAuthority,
    LodgingConfirmationRequest,
    LodgingConfirmationStager,
    LodgingConfirmationState,
    LodgingSelectionAnchor,
    LodgingSelectionSegment,
    canonical_lodging_location_id,
    lodging_confirmation_request_from_option,
)
from trip_planner.models import DecisionState
from trip_planner.mutations import (
    AddActivity,
    ApprovalGrant,
    ConfirmedLodgingStay,
    LodgingConfirmationGrant,
    PlanPatch,
    UpdateActivity,
    UpdateDay,
    _mint_lodging_confirmation_grant,
)
from trip_planner.store import TripStore

from tests.test_phase45_lodging_itinerary import (
    EVALUATED,
    _assess,
    _busan_fixture,
    _hokkaido_fixture,
)


UTC = timezone.utc
PRIVATE = "private-label-address-37.5000-127.0000-price-token-candidate"


def _state(*, split: bool = False) -> dict:
    first = date(2026, 12, 10) if split else date(2026, 8, 1)
    days = []
    for offset in range(3):
        places = []
        if not split and offset == 0:
            places.append(
                {
                    "activity_id": "booked-arrival",
                    "title": "arrival",
                    "location_id": "busan-arrival-terminal",
                    "time": "10:00",
                    "duration_min": 30,
                    "decision_state": "booked",
                    "flexibility": "fixed_time",
                    "evidence_state": "verified",
                }
            )
        if not split and offset == 1:
            places.append(
                {
                    "activity_id": "booked-dinner",
                    "title": "dinner",
                    "location_id": "busan-booked-dinner",
                    "time": "18:00",
                    "duration_min": 30,
                    "decision_state": "booked",
                    "flexibility": "fixed_time",
                    "evidence_state": "verified",
                }
            )
        if split and offset == 1:
            places.append(
                {
                    "activity_id": "booked-ryokan-checkin",
                    "title": "ryokan check-in",
                    "location_id": "hokkaido-ryokan-b",
                    "time": "16:00",
                    "duration_min": 30,
                    "decision_state": "booked",
                    "flexibility": "fixed_time",
                    "evidence_state": "verified",
                }
            )
        days.append(
            {
                "day_id": f"day-{offset + 1}",
                "date": (first + timedelta(days=offset)).isoformat(),
                "timezone": "Asia/Tokyo",
                "available_start": "08:00",
                "available_end": "22:00",
                "places": places,
                "travel": [],
            }
        )
    return {
        "trip": {"trip_id": "phase45-confirmation", "title": "fixture"},
        "itinerary": {"days": days},
    }


class Phase45LodgingConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "phase45-confirmation" / "data"
        self.data.mkdir(parents=True)
        self.signing_key = b"phase45d-test-host-key"
        self.authority = LodgingConfirmationAuthority(
            confirmed_by="test-host",
            issuer_id="phase45d-test-issuer",
            signer=self._sign,
            clock=lambda: EVALUATED,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _sign(self, payload: bytes) -> str:
        return hmac.new(
            self.signing_key,
            payload,
            hashlib.sha256,
        ).hexdigest()

    def _verify(self, grant: LodgingConfirmationGrant) -> bool:
        return (
            grant.issuer_id == "phase45d-test-issuer"
            and grant.confirmed_at <= EVALUATED <= grant.expires_at
            and hmac.compare_digest(
                grant.signature,
                self._sign(grant.verification_payload()),
            )
        )

    def _store(
        self,
        *,
        split: bool = False,
        fault_hook=None,
    ) -> TripStore:
        plan = build_plan(
            trip_id="phase45-confirmation", generation=1, state=_state(split=split)
        )
        (self.data / "plan.json").write_bytes(encode_plan(plan))
        return TripStore(
            self.root,
            "phase45-confirmation",
            fault_hook=fault_hook,
            lodging_confirmation_verifier=self._verify,
        )

    def _request(
        self,
        store: TripStore,
        *,
        split: bool = False,
        private_location: bool = False,
    ) -> LodgingConfirmationRequest:
        plan = store.load_plan()
        if split:
            first = LodgingSelectionSegment(
                location_id="hokkaido-hotel-a", check_in=date(2026, 12, 10),
                check_out=date(2026, 12, 11), kind=LodgingKind.HOTEL,
                decision_state=DecisionState.SELECTED,
            )
            second = LodgingSelectionSegment(
                location_id="hokkaido-ryokan-b", check_in=date(2026, 12, 11),
                check_out=date(2026, 12, 13), kind=LodgingKind.RYOKAN,
                decision_state=DecisionState.BOOKED,
            )
            segments = (first, second)
            anchors = (
                LodgingSelectionAnchor("day-1", end_lodging_id=first.lodging_id),
                LodgingSelectionAnchor("day-2", first.lodging_id, second.lodging_id),
                LodgingSelectionAnchor("day-3", second.lodging_id, second.lodging_id),
            )
        else:
            stay = LodgingSelectionSegment(
                location_id=(
                    PRIVATE if private_location else "busan-hotel-a"
                ),
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 4), kind=LodgingKind.HOTEL,
                decision_state=DecisionState.SELECTED,
            )
            segments = (stay,)
            anchors = (
                LodgingSelectionAnchor("day-1", end_lodging_id=stay.lodging_id),
                LodgingSelectionAnchor("day-2", stay.lodging_id, stay.lodging_id),
                LodgingSelectionAnchor("day-3", stay.lodging_id, stay.lodging_id),
            )
        return LodgingConfirmationRequest(
            trip_id="phase45-confirmation", base_revision=plan["revision"],
            idempotency_key="phase45-confirmation-request", segments=segments,
            anchors=anchors, evaluation_at=EVALUATED,
        )

    def _stager(self, store: TripStore) -> LodgingConfirmationStager:
        return LodgingConfirmationStager(store, clock=lambda: EVALUATED)

    def _grant(self, review):
        return self.authority.issue_grant(review)

    def _approval(self, scope: str) -> ApprovalGrant:
        return ApprovalGrant(
            "phase45d-protected-approval",
            scope,
            "test-host",
            EVALUATED.isoformat(),
        )

    def test_busan_stage_grant_apply_and_receipt_replay_preserve_booked_events(self):
        store = self._store()
        before = store.load_plan()
        stager = self._stager(store)
        request = self._request(store)
        review = stager.stage(request)
        self.assertEqual(LodgingConfirmationState.WAITING_CONFIRMATION, review.state)
        applied = stager.commit(review.review_id, confirmation=self._grant(review))
        self.assertEqual(LodgingConfirmationState.APPLIED, applied.state)
        after = store.load_plan()
        self.assertEqual(
            before["state"]["itinerary"]["days"][0]["places"],
            after["state"]["itinerary"]["days"][0]["places"],
        )
        self.assertEqual(
            before["state"]["itinerary"]["days"][1]["places"],
            after["state"]["itinerary"]["days"][1]["places"],
        )
        self.assertEqual(
            "10:00",
            after["state"]["itinerary"]["days"][0]["places"][0]["time"],
        )
        self.assertEqual(
            "18:00",
            after["state"]["itinerary"]["days"][1]["places"][0]["time"],
        )
        replay = self._stager(store).stage(request)
        self.assertEqual(LodgingConfirmationState.REPLAY_CONFIRMED, replay.state)

    def test_hokkaido_split_stay_anchors_preserve_booked_1600_checkin(self):
        store = self._store(split=True)
        stager = self._stager(store)
        review = stager.stage(self._request(store, split=True))
        applied = stager.commit(review.review_id, confirmation=self._grant(review))
        self.assertEqual(LodgingConfirmationState.APPLIED, applied.state)
        plan = store.load_plan()
        days = plan["state"]["itinerary"]["days"]
        self.assertNotEqual(days[1]["start_lodging_id"], days[1]["end_lodging_id"])
        self.assertEqual("16:00", days[1]["places"][0]["time"])
        self.assertEqual("booked", days[1]["places"][0]["decision_state"])

    def test_missing_foreign_and_generic_only_grants_fail_closed(self):
        store = self._store()
        stager = self._stager(store)
        request = self._request(store)
        review = stager.stage(request)
        missing = stager.commit(review.review_id, confirmation=None)
        self.assertEqual(
            LodgingConfirmationState.WAITING_CONFIRMATION,
            missing.state,
        )
        with self.assertRaisesRegex(ValueError, "host-minted"):
            replace(
                self._grant(review),
                scope_digest="sha256:" + "0" * 64,
            )
        foreign_request = replace(
            request,
            request_id="",
            idempotency_key="foreign-lodging-confirmation",
        )
        foreign_review = self._stager(store).stage(foreign_request)
        foreign_grant = self._grant(foreign_review)
        rejected = stager.commit(review.review_id, confirmation=foreign_grant)
        self.assertEqual(
            LodgingConfirmationState.WAITING_CONFIRMATION,
            rejected.state,
        )
        assert review.required_store_approval_scope is not None
        generic_only = store.apply_patch(
            request.to_patch(),
            approvals=(
                self._approval(review.required_store_approval_scope),
            ),
            evaluation_at=EVALUATED,
        )
        self.assertFalse(generic_only.success)
        self.assertIn(
            "LODGING_CONFIRMATION_REQUIRED",
            {problem.code for problem in generic_only.problems},
        )
        self.assertNotIn(
            "lodgings",
            store.load_plan()["state"]["trip"],
        )

    def test_private_mint_helper_cannot_bypass_host_signature_verifier(self):
        store = self._store()
        request = self._request(store)
        review = self._stager(store).stage(request)
        assert review.review_id is not None
        assert review.patch_digest is not None
        assert review.required_lodging_confirmation_scope is not None
        assert review.required_store_approval_scope is not None
        forged = _mint_lodging_confirmation_grant(
            review_id=review.review_id,
            trip_id=review.trip_id,
            base_revision=review.base_revision,
            request_digest=review.patch_digest,
            scope_digest=review.required_lodging_confirmation_scope,
            confirmed_by="forged-host",
            confirmed_at=EVALUATED,
            expires_at=review.expires_at,
            issuer_id="phase45d-test-issuer",
            signature="forged-signature",
        )

        result = store.apply_patch(
            request.to_patch(),
            approvals=(
                self._approval(review.required_store_approval_scope),
            ),
            lodging_confirmations=(forged,),
            evaluation_at=EVALUATED,
        )

        self.assertFalse(result.success)
        self.assertEqual(
            {"UNTRUSTED_LODGING_CONFIRMATION"},
            {problem.code for problem in result.problems},
        )
        self.assertNotIn(
            "lodgings",
            store.load_plan()["state"]["trip"],
        )

    def test_mixed_patch_is_atomic_and_lodging_activity_aliases_are_blocked(
        self,
    ):
        store = self._store()
        before = store.load_plan()
        request = self._request(store)
        review = self._stager(store).stage(request)
        mixed = PlanPatch(
            trip_id=request.trip_id,
            base_revision=request.base_revision,
            idempotency_key="mixed-lodging-and-activity",
            operations=(
                *request.to_patch().operations,
                AddActivity(
                    "add-unrelated",
                    "unrelated-activity",
                    "day-1",
                    {
                        "title": "unrelated",
                        "location_id": "unrelated-location",
                        "decision_state": "tentative",
                        "flexibility": "movable",
                        "evidence_state": "unverified",
                    },
                ),
            ),
        )
        mixed_preview = store.preview_patch(
            mixed,
            evaluation_at=EVALUATED,
        )
        assert mixed_preview.required_approval_scope is not None
        mixed_result = store.apply_patch(
            mixed,
            approvals=(
                self._approval(mixed_preview.required_approval_scope),
            ),
            lodging_confirmations=(self._grant(review),),
            evaluation_at=EVALUATED,
        )
        self.assertFalse(mixed_result.success)
        self.assertIn(
            "LODGING_CONFIRMATION_MISMATCH",
            {problem.code for problem in mixed_result.problems},
        )
        self.assertEqual(before, store.load_plan())

        for operation in (
            AddActivity(
                "add-airbnb",
                "airbnb-activity",
                "day-1",
                {
                    "title": "private stay",
                    "location_id": "private-stay",
                    "type": "AirBnB",
                },
            ),
            UpdateActivity(
                "turn-activity-into-hotel",
                "booked-arrival",
                {"type": "HOTEL"},
            ),
            UpdateDay(
                "generic-lodging-anchor",
                "day-1",
                {"end_location_id": "bypass-location"},
            ),
        ):
            patch = PlanPatch(
                trip_id=request.trip_id,
                base_revision=request.base_revision,
                idempotency_key=f"blocked-{operation.op_id}",
                operations=(operation,),
            )
            result = store.preview_patch(
                patch,
                evaluation_at=EVALUATED,
            )
            self.assertIn(
                "LODGING_OPERATION_REQUIRED",
                {problem.code for problem in result.problems},
            )

    def test_lost_ack_retains_review_and_exact_retry_replays(self):
        def fault_hook(stage: str) -> None:
            if stage == "after_replace":
                raise RuntimeError("simulated lost acknowledgement")

        store = self._store(fault_hook=fault_hook)
        stager = self._stager(store)
        review = stager.stage(self._request(store))
        grant = self._grant(review)

        uncertain = stager.commit(
            review.review_id,
            confirmation=grant,
        )
        self.assertEqual(
            LodgingConfirmationState.OUTCOME_UNKNOWN,
            uncertain.state,
        )
        self.assertTrue(uncertain.pending_review_retained)

        replay = stager.commit(
            review.review_id,
            confirmation=grant,
        )
        self.assertEqual(
            LodgingConfirmationState.REPLAY_CONFIRMED,
            replay.state,
        )
        self.assertTrue(replay.applied)

    def test_invalid_stay_shapes_and_anchor_coverage_are_rejected(self):
        store = self._store()
        plan = store.load_plan()
        first = LodgingSelectionSegment(
            "a-hotel",
            date(2026, 8, 1),
            date(2026, 8, 2),
            LodgingKind.HOTEL,
            DecisionState.SELECTED,
        )
        second = LodgingSelectionSegment(
            "b-hotel",
            date(2026, 8, 3),
            date(2026, 8, 4),
            LodgingKind.HOTEL,
            DecisionState.SELECTED,
        )
        with self.assertRaisesRegex(ValueError, "gaps or overlaps"):
            LodgingConfirmationRequest(
                "phase45-confirmation",
                plan["revision"],
                "gap",
                (first, second),
                (
                    LodgingSelectionAnchor(
                        "day-1",
                        end_lodging_id=first.lodging_id,
                    ),
                ),
                EVALUATED,
            )
        request = self._request(store)
        bad = replace(
            request,
            request_id="",
            selection_binding_digest="",
            anchors=(
                LodgingSelectionAnchor(
                    "unknown-day",
                    end_lodging_id=request.segments[0].lodging_id,
                ),
            ),
        )
        self.assertEqual(LodgingConfirmationState.REJECTED, self._stager(store).stage(bad).state)
        incomplete = replace(
            request,
            request_id="",
            selection_binding_digest="",
            anchors=(request.anchors[0], request.anchors[1]),
        )
        self.assertEqual(
            LodgingConfirmationState.REJECTED,
            self._stager(store).stage(incomplete).state,
        )
        stager = self._stager(store)
        review = stager.stage(request)
        self.assertEqual(
            LodgingConfirmationState.APPLIED,
            stager.commit(
                review.review_id,
                confirmation=self._grant(review),
            ).state,
        )
        malformed = store.load_plan()
        malformed["state"]["itinerary"]["days"][1]["date"] = "20260802"
        with self.assertRaisesRegex(
            PlanCodecError,
            "exact ISO itinerary dates",
        ):
            encode_plan(malformed)

    def test_option_projection_requires_grant_but_allows_reported_only_warning(self):
        fixture = _busan_fixture()
        assessment = _assess(fixture)
        request = lodging_confirmation_request_from_option(
            assessment=assessment, option=fixture.options[0], comparison=fixture.comparison,
            snapshot=fixture.snapshot,
            decision_states={fixture.candidates[0].candidate_id: DecisionState.SELECTED},
            idempotency_key="from-ranked-option",
        )
        self.assertTrue(request.reviewed_itinerary)
        reported_fixture = _busan_fixture(reported_first=True)
        reported = _assess(reported_fixture)
        reported_request = lodging_confirmation_request_from_option(
            assessment=reported,
            option=reported_fixture.options[0],
            comparison=reported_fixture.comparison,
            snapshot=reported_fixture.snapshot,
            decision_states={reported_fixture.candidates[0].candidate_id: DecisionState.SELECTED},
            idempotency_key="reported-only",
        )
        self.assertTrue(reported_request.reviewed_itinerary)
        tied_fixture = _busan_fixture(durations=(12, 12))
        tied = _assess(tied_fixture)
        self.assertIsNone(tied.priority_review_option_id)
        chosen_tie = tied_fixture.options[0]
        tie_request = lodging_confirmation_request_from_option(
            assessment=tied,
            option=chosen_tie,
            comparison=tied_fixture.comparison,
            snapshot=tied_fixture.snapshot,
            decision_states={
                candidate_id: DecisionState.SELECTED
                for candidate_id in chosen_tie.candidate_ids
            },
            idempotency_key="human-chosen-tie",
        )
        self.assertTrue(tie_request.reviewed_itinerary)
        self.assertTrue(tie_request.selection_binding_digest)

        blocked_options = tuple(
            replace(
                item,
                disposition=LodgingOptionDisposition.NEEDS_VERIFICATION,
                issue_codes=("LODGING_ROUTE_EVIDENCE_REQUIRED",),
                schedule_candidate_id=None,
                score=None,
                assessment_id="",
            )
            if item.option_id == fixture.options[0].option_id
            else item
            for item in assessment.options
        )
        unresolved = replace(
            assessment,
            status=LodgingItineraryStatus.NEEDS_VERIFICATION,
            options=blocked_options,
            review_order=(),
            priority_review_option_id=None,
            assessment_id="",
        )
        with self.assertRaises(FactContractError):
            lodging_confirmation_request_from_option(
                assessment=unresolved,
                option=fixture.options[0],
                comparison=fixture.comparison,
                snapshot=fixture.snapshot,
                decision_states={
                    fixture.candidates[0].candidate_id: (
                        DecisionState.SELECTED
                    )
                },
                idempotency_key="unresolved-evidence",
            )

    def test_safe_objects_and_store_artifacts_exclude_private_runtime_values(self):
        store = self._store()
        opaque_one = canonical_lodging_location_id(PRIVATE)
        opaque_two = canonical_lodging_location_id(PRIVATE)
        self.assertNotEqual(opaque_one, opaque_two)
        self.assertNotEqual(
            "lodging-location-"
            + hashlib.sha256(PRIVATE.encode("utf-8")).hexdigest(),
            opaque_one,
        )
        with self.assertRaisesRegex(ValueError, "privacy-safe"):
            ConfirmedLodgingStay(
                lodging_id="unsafe-stay",
                location_id=PRIVATE,
                check_in="2026-08-01",
                check_out="2026-08-04",
                kind="hotel",
                decision_state="selected",
            )
        stager = self._stager(store)
        request = self._request(store, private_location=True)
        self.assertNotEqual(
            PRIVATE,
            request.segments[0].location_id,
        )
        self.assertTrue(
            request.segments[0].location_id.startswith(
                "lodging-location-"
            )
        )
        review = stager.stage(request)
        result = stager.commit(review.review_id, confirmation=self._grant(review))
        rendered = json.dumps(
            [
                request.to_dict(),
                review.to_dict(),
                result.to_dict(),
                repr(review),
                repr(request),
                store.load_plan(),
            ],
            sort_keys=True,
        )
        self.assertNotIn(PRIVATE, rendered)
        self.assertNotIn("candidate_id", rendered)
        plan = store.load_plan()
        receipts = json.dumps(plan["receipts"], sort_keys=True)
        history = list((self.data / ".trip-planner-history").glob("*.json"))
        self.assertTrue(history)
        self.assertNotIn(PRIVATE, receipts + "".join(item.read_text() for item in history))


if __name__ == "__main__":
    unittest.main()
