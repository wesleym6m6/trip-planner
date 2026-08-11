"""Synthetic contracts for Phase 6.2A private-delivery preparation."""

from __future__ import annotations

import copy
import gc
import hashlib
import json
import pickle
import unittest
import weakref
from dataclasses import replace
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from unittest.mock import patch

import trip_planner
from trip_planner import compose_trip_state
from trip_planner.canonical_tripctl import MAX_CANONICAL_PLAN_BYTES
from trip_planner.codec import compute_revision, encode_plan
from trip_planner.lodging import (
    LocationHint,
    LocationHintKind,
    LodgingIntentDraft,
    LodgingKind,
    LodgingRequirement,
    assess_lodging_intake,
    bind_lodging_candidate,
)
from trip_planner.lodging_confirmation import (
    LodgingConfirmationProblem,
    LodgingConfirmationState,
)
from trip_planner.private_delivery import (
    PRIVATE_DELIVERY_MANIFEST_VERSION,
    PRIVATE_DELIVERY_RESPONSE_VERSION,
    PRIVATE_DELIVERY_VERSION,
    PrivateDeliveryArtifact,
    PrivateDeliveryError,
    PrivateDeliveryProfile,
    PrivateDeliveryResponse,
    PrivateDeliveryResponseKind,
    PrivateDeliveryReview,
    capture_private_delivery_response,
    prepare_private_delivery_review,
    verify_private_delivery_response,
)
from trip_planner.private_html import project_private_html
from trip_planner.private_ics import project_private_ics
from trip_planner.readiness import ReadinessStatus, assess_trip_readiness
from tests.test_phase46_readiness import (
    _empty_snapshot,
    _not_required,
    _pending_review,
    _route_case,
)
from tests.test_phase4_composition import EVALUATION_AT
from tests.test_phase4_fact_contracts import opening_key
from tests.test_phase61_private_ics import _place, _plan as _calendar_plan


SOURCE_PATH = "/synthetic/private-source/plan.json"
TARGET_PATH = "/synthetic/private-target/generation-a"
PRIVATE_SENTINEL = "PRIVATE-SENTINEL-TITLE-PATH-DIGEST-EVENT"


class _Clock:
    def __init__(self, value=EVALUATION_AT) -> None:
        self.value = value

    def __call__(self):
        return self.value


class _HookClock(_Clock):
    def __init__(self, value=EVALUATION_AT) -> None:
        super().__init__(value)
        self.hook = None

    def __call__(self):
        if self.hook is not None:
            self.hook()
        return self.value


class _MissingOffset(tzinfo):
    def utcoffset(self, value):
        del value
        return None


class _HostileTuple(tuple):
    def __new__(cls, values, calls):
        instance = super().__new__(cls, values)
        instance.calls = calls
        return instance

    def __iter__(self):
        self.calls.append("iterated")
        raise RuntimeError(PRIVATE_SENTINEL)


class _HostileTimezone(tzinfo):
    def __init__(self, calls):
        self.calls = calls

    def utcoffset(self, value):
        del value
        self.calls.append("utcoffset")
        raise RuntimeError(PRIVATE_SENTINEL)


_CLOCKS: dict[int, _Clock] = {}


def _set_clock(review: PrivateDeliveryReview, value) -> None:
    _CLOCKS[id(review)].value = value


def _prepare(
    profile: PrivateDeliveryProfile,
    *,
    target_path: str = TARGET_PATH,
    route_kwargs: dict[str, object] | None = None,
):
    plan, snapshot, composed, _summary, intake = _route_case(
        **(route_kwargs or {})
    )
    kwargs: dict[str, object] = {}
    if profile is PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE:
        kwargs["lodging_intake"] = intake
    clock = _Clock()
    review = prepare_private_delivery_review(
        encode_plan(plan),
        snapshot,
        profile=profile,
        source_path=SOURCE_PATH,
        target_path=target_path,
        clock=clock,
        **kwargs,
    )
    _CLOCKS[id(review)] = clock
    return plan, snapshot, composed, intake, clock, review


def _artifact(review: PrivateDeliveryReview, filename: str):
    return next(
        item
        for item in review.to_ephemeral_private_artifacts()
        if item.filename == filename
    )


def _candidate_lodging_intake():
    candidate = bind_lodging_candidate(
        LodgingIntentDraft(
            kind=LodgingKind.HOTEL,
            label="Synthetic stay",
            location=LocationHint(
                kind=LocationHintKind.AREA,
                label="Synthetic area",
                input_text="Synthetic district",
                country_code="ZZ",
            ),
            check_in=date(2026, 10, 1),
            check_out=date(2026, 10, 2),
        )
    )
    return assess_lodging_intake(
        stay_start=date(2026, 10, 1),
        stay_end=date(2026, 10, 2),
        requirement=LodgingRequirement.REQUIRED,
        candidates=(candidate,),
    )


def _error_code(callable_object) -> str:
    with unittest.TestCase().assertRaises(PrivateDeliveryError) as caught:
        callable_object()
    return caught.exception.code


class Phase62PrivateDeliveryTests(unittest.TestCase):
    def test_html_preview_is_deterministic_closed_and_does_not_assess_readiness(self) -> None:
        route_kwargs = {
            "first_valid_until": EVALUATION_AT,
            "second_valid_until": EVALUATION_AT,
        }
        first = _prepare(
            PrivateDeliveryProfile.HTML_PREVIEW,
            route_kwargs=route_kwargs,
        )[-1]
        second = _prepare(
            PrivateDeliveryProfile.HTML_PREVIEW,
            route_kwargs=route_kwargs,
        )[-1]

        self.assertEqual(
            ["index.html", "manifest.json"],
            [item.filename for item in first.to_ephemeral_private_artifacts()],
        )
        self.assertEqual(
            [item.payload for item in first.to_ephemeral_private_artifacts()],
            [item.payload for item in second.to_ephemeral_private_artifacts()],
        )
        self.assertIsNone(first.readiness)
        self.assertEqual(
            "not_assessed",
            first.to_ephemeral_private_review()["readiness"]["status"],
        )
        self.assertNotIn("calendar.ics", first.to_ephemeral_private_review()["artifact_filenames"])
        safe = first.to_safe_dict()
        self.assertFalse(safe["readiness_required"])
        self.assertIsNone(safe["readiness_satisfied_at_preparation"])
        self.assertFalse(safe["writes_performed"])

    def test_ready_bundle_matches_direct_same_state_projectors(self) -> None:
        _plan, _snapshot, composed, _intake, _clock, review = _prepare(
            PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE
        )
        self.assertEqual(
            ["index.html", "calendar.ics", "manifest.json"],
            [item.filename for item in review.to_ephemeral_private_artifacts()],
        )
        self.assertIsNotNone(review.readiness)
        assert review.readiness is not None
        direct_html = project_private_html(composed.state)
        direct_ics = project_private_ics(
            composed.state,
            uid_namespace=composed.trip_id,
            generated_at=review.readiness.evaluated_at,
        )
        self.assertEqual(direct_html.html_bytes, _artifact(review, "index.html").payload)
        self.assertEqual(direct_ics.calendar_bytes, _artifact(review, "calendar.ics").payload)
        self.assertEqual(EVALUATION_AT + timedelta(minutes=30), review.expires_at)
        private = review.to_ephemeral_private_review()
        self.assertEqual("travel_ready", private["readiness"]["status"])
        self.assertTrue(private["calendar_events"])
        self.assertEqual(
            PrivateDeliveryResponseKind.ACCEPT_HTML_ICS_READY_BUNDLE_CANDIDATE.value,
            private["candidate_response_kind"],
        )

    def test_manifest_is_private_deterministic_provenance_not_authority(self) -> None:
        first = _prepare(
            PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
            target_path="/synthetic/private-target/generation-a",
        )[-1]
        second = _prepare(
            PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
            target_path="/synthetic/private-target/generation-b",
        )[-1]
        first_manifest = _artifact(first, "manifest.json")
        second_manifest = _artifact(second, "manifest.json")
        self.assertEqual(first_manifest.payload, second_manifest.payload)
        self.assertNotEqual(first.review_id, second.review_id)
        manifest = json.loads(first_manifest.payload)
        self.assertEqual(PRIVATE_DELIVERY_MANIFEST_VERSION, manifest["contract_version"])
        self.assertEqual("private", manifest["visibility"])
        self.assertFalse(manifest["safe_to_publish"])
        self.assertEqual("travel_ready", manifest["readiness"]["status"])
        self.assertEqual(
            ["index.html", "calendar.ics"],
            [item["filename"] for item in manifest["artifacts"]],
        )
        encoded = first_manifest.payload.decode("utf-8")
        for forbidden in (
            SOURCE_PATH,
            TARGET_PATH,
            first.review_id,
            "accept_html_ics_ready_bundle_candidate",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertTrue(first_manifest.payload.endswith(b"\n"))

    def test_changed_canonical_bytes_change_binding_when_projection_excludes_value(self) -> None:
        plan, snapshot, composed, _summary, _intake = _route_case()
        baseline = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=_Clock(),
        )
        changed = copy.deepcopy(plan)
        changed["state"]["itinerary"]["days"][0]["places"][0][
            "location_id"
        ] = "synthetic-location-drift"
        changed["revision"] = compute_revision(changed)
        changed_composed = compose_trip_state(changed, snapshot)
        drifted = prepare_private_delivery_review(
            encode_plan(changed),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=_Clock(),
        )
        self.assertEqual(
            _artifact(baseline, "index.html").payload,
            _artifact(drifted, "index.html").payload,
        )
        self.assertNotEqual(
            _artifact(baseline, "manifest.json").payload,
            _artifact(drifted, "manifest.json").payload,
        )
        self.assertNotEqual(baseline.review_id, drifted.review_id)

    def test_review_time_mismatch_fails_closed(self) -> None:
        plan, snapshot, _composed, _summary, _intake = _route_case()
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TIME_MISMATCH",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_PREVIEW,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(EVALUATION_AT + timedelta(seconds=1)),
                )
            ),
        )

    def test_ready_profile_rejects_non_ready_state_but_preview_accepts_it(self) -> None:
        route_kwargs = {
            "first_valid_until": EVALUATION_AT,
            "second_valid_until": EVALUATION_AT,
        }
        plan, snapshot, composed, _summary, intake = _route_case(**route_kwargs)
        preview = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=_Clock(),
        )
        self.assertIsNone(preview.readiness)
        self.assertEqual(
            "PRIVATE_DELIVERY_READINESS_NOT_READY",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    lodging_intake=intake,
                )
            ),
        )

        for expected_status, mutate in (
            (
                ReadinessStatus.REVIEW,
                lambda value: value["state"]["itinerary"]["days"][0][
                    "places"
                ][0].update({"decision_state": "candidate"}),
            ),
            (
                ReadinessStatus.DRAFT,
                lambda value: value["state"]["itinerary"]["days"][0][
                    "places"
                ][1].update({"time": "09:00"}),
            ),
        ):
            changed = copy.deepcopy(_route_case()[0])
            mutate(changed)
            changed["revision"] = compute_revision(changed)
            changed_snapshot = _route_case()[1]
            changed_composed = compose_trip_state(changed, changed_snapshot)
            changed_intake = _route_case()[4]
            readiness = assess_trip_readiness(
                canonical_plan=changed,
                composed=changed_composed,
                snapshot=changed_snapshot,
                lodging_intake=changed_intake,
            )
            self.assertIs(expected_status, readiness.status)
            self.assertEqual(
                "PRIVATE_DELIVERY_READINESS_NOT_READY",
                _error_code(
                    lambda changed=changed,
                    changed_composed=changed_composed,
                    changed_snapshot=changed_snapshot,
                    changed_intake=changed_intake:
                        prepare_private_delivery_review(
                            encode_plan(changed),
                            changed_snapshot,
                            profile=(
                                PrivateDeliveryProfile.
                                HTML_ICS_READY_BUNDLE
                            ),
                            source_path=SOURCE_PATH,
                            target_path=TARGET_PATH,
                            clock=_Clock(),
                            lodging_intake=changed_intake,
                        )
                ),
            )

    def test_ready_profile_reports_ics_specific_refusal(self) -> None:
        plan, snapshot, _composed, _summary, intake = _route_case()
        plan = copy.deepcopy(plan)
        plan["state"]["itinerary"]["days"][0]["places"][0][
            "time"
        ] = "09:00:00.123456"
        plan["revision"] = compute_revision(plan)
        composed = compose_trip_state(plan, snapshot)
        readiness = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
            lodging_intake=intake,
        )
        self.assertIs(ReadinessStatus.TRAVEL_READY, readiness.status)
        self.assertEqual(
            "PRIVATE_DELIVERY_ICS_PROJECTION_FAILED",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=(
                        PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE
                    ),
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    lodging_intake=intake,
                )
            ),
        )

    def test_runtime_context_binds_full_composed_availability_view(self) -> None:
        plan, snapshot, baseline, _summary, _intake = _route_case()
        key = opening_key(
            location_id="loc-a",
            provider_place_id="synthetic-place",
            target_start="2026-10-01",
            target_end="2026-10-01",
        )
        with_availability = compose_trip_state(
            plan,
            snapshot,
            availability_keys=(key,),
        )
        self.assertEqual(
            baseline.composed_state_digest,
            with_availability.composed_state_digest,
        )
        self.assertEqual(
            baseline.evidence.binding_digest,
            with_availability.evidence.binding_digest,
        )
        first = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=_Clock(),
        )
        second = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=_Clock(),
            availability_keys=(key,),
        )
        self.assertNotEqual(
            first.runtime_context_sha256,
            second.runtime_context_sha256,
        )
        self.assertNotEqual(first.review_id, second.review_id)
        self.assertNotEqual(
            _artifact(first, "manifest.json").payload,
            _artifact(second, "manifest.json").payload,
        )

    def test_hostile_typed_inputs_fail_closed(self) -> None:
        plan, snapshot, _composed, _summary, _intake = _route_case()
        key = opening_key(
            location_id="loc-a",
            provider_place_id="synthetic-place",
            target_start="2026-10-01",
            target_end="2026-10-01",
        )
        object.__setattr__(key, "subject_ids", ("loc-b",))
        self.assertEqual(
            "PRIVATE_DELIVERY_AVAILABILITY_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_PREVIEW,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    availability_keys=(key,),
                )
            ),
        )

        object.__setattr__(
            snapshot.observations[0].value,
            "canonical_json",
            b"{}",
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_EVIDENCE_SNAPSHOT_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_PREVIEW,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                )
            ),
        )

        fresh = _route_case()
        hostile_intake = fresh[4]
        object.__setattr__(
            hostile_intake,
            "requirement",
            LodgingRequirement.REQUIRED,
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_LODGING_CONTEXT_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(fresh[0]),
                    fresh[1],
                    profile=(
                        PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE
                    ),
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    lodging_intake=hostile_intake,
                )
            ),
        )

        oversized = _pending_review(
            fresh[2],
            created_at=EVALUATION_AT - timedelta(minutes=5),
        )
        object.__setattr__(oversized, "stay_count", 10**1000)
        self.assertEqual(
            "PRIVATE_DELIVERY_LODGING_CONTEXT_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(fresh[0]),
                    fresh[1],
                    profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    lodging_intake=_not_required(
                        date(2026, 10, 1),
                        date(2026, 10, 2),
                    ),
                    pending_lodging_review=oversized,
                )
            ),
        )

        unnormalized = _pending_review(
            fresh[2],
            created_at=EVALUATION_AT - timedelta(minutes=5),
        )
        self.assertTrue(unnormalized.affected_day_ids)
        object.__setattr__(
            unnormalized,
            "affected_day_ids",
            unnormalized.affected_day_ids
            + (unnormalized.affected_day_ids[0],),
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_LODGING_CONTEXT_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(fresh[0]),
                    fresh[1],
                    profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    lodging_intake=_not_required(
                        date(2026, 10, 1),
                        date(2026, 10, 2),
                    ),
                    pending_lodging_review=unnormalized,
                )
            ),
        )

    def test_nested_evidence_callbacks_are_rejected_without_execution(self) -> None:
        def policy_tuple(snapshot, calls):
            policy = snapshot.policies.policies[0]
            object.__setattr__(
                policy,
                "allowed_operations",
                _HostileTuple(policy.allowed_operations, calls),
            )

        def key_tuple(snapshot, calls):
            key = snapshot.observations[0].key
            object.__setattr__(
                key,
                "subject_ids",
                _HostileTuple(key.subject_ids, calls),
            )

        def attribution_tuple(snapshot, calls):
            provenance = snapshot.observations[0].provenance
            object.__setattr__(
                provenance,
                "attributions",
                _HostileTuple(provenance.attributions, calls),
            )

        def snapshot_time(snapshot, calls):
            hostile = _HostileTimezone(calls)
            object.__setattr__(
                snapshot,
                "evaluation_at",
                datetime(2026, 10, 1, tzinfo=hostile),
            )

        def observation_time(snapshot, calls):
            hostile = _HostileTimezone(calls)
            object.__setattr__(
                snapshot.observations[0],
                "retrieved_at",
                datetime(2026, 10, 1, tzinfo=hostile),
            )

        for mutate in (
            policy_tuple,
            key_tuple,
            attribution_tuple,
            snapshot_time,
            observation_time,
        ):
            plan, snapshot, _composed, _summary, _intake = _route_case()
            calls: list[str] = []
            mutate(snapshot, calls)
            self.assertEqual(
                "PRIVATE_DELIVERY_EVIDENCE_SNAPSHOT_INVALID",
                _error_code(
                    lambda: prepare_private_delivery_review(
                        encode_plan(plan),
                        snapshot,
                        profile=PrivateDeliveryProfile.HTML_PREVIEW,
                        source_path=SOURCE_PATH,
                        target_path=TARGET_PATH,
                        clock=_Clock(),
                    )
                ),
            )
            self.assertEqual([], calls)

    def test_nested_lodging_identity_tamper_fails_closed(self) -> None:
        plan, snapshot, composed, _summary, _intake = _route_case()
        hostile_container = _candidate_lodging_intake()
        calls: list[str] = []
        object.__setattr__(
            hostile_container,
            "candidates",
            _HostileTuple(hostile_container.candidates, calls),
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_LODGING_CONTEXT_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    lodging_intake=hostile_container,
                )
            ),
        )
        self.assertEqual([], calls)

        intake = _candidate_lodging_intake()
        object.__setattr__(
            intake.candidates[0].draft,
            "check_out",
            date(2026, 10, 3),
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_LODGING_CONTEXT_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    lodging_intake=intake,
                )
            ),
        )

        pending = replace(
            _pending_review(
                composed,
                created_at=EVALUATION_AT - timedelta(minutes=5),
            ),
            state=LodgingConfirmationState.REJECTED,
            review_id=None,
            problems=(
                LodgingConfirmationProblem(
                    code="SYNTHETIC_REJECTION",
                    message="Synthetic rejection.",
                ),
            ),
        )
        object.__setattr__(
            pending.problems[0],
            "code",
            "SYNTHETIC_CHANGED_REJECTION",
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_LODGING_CONTEXT_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(),
                    lodging_intake=_not_required(
                        date(2026, 10, 1),
                        date(2026, 10, 2),
                    ),
                    pending_lodging_review=pending,
                )
            ),
        )

    def test_calendar_review_preserves_overnight_and_dst_offsets(self) -> None:
        cases = (
            (
                _calendar_plan(
                    available_start="22:00",
                    available_end="02:00",
                    places=(
                        _place(
                            "activity-after",
                            "After Midnight",
                            "01:00",
                        ),
                    ),
                ),
                date(2026, 10, 1),
                "2026-10-02T01:00:00+09:00",
                "2026-10-02T02:00:00+09:00",
            ),
            (
                _calendar_plan(
                    timezone_name="America/New_York",
                    day_date="2026-11-01",
                    available_start="00:00",
                    available_end="04:00",
                    places=(
                        _place(
                            "activity-dst",
                            "DST Example",
                            "00:30",
                            duration_min=120,
                        ),
                    ),
                ),
                date(2026, 11, 1),
                "2026-11-01T00:30:00-04:00",
                "2026-11-01T01:30:00-05:00",
            ),
        )
        for plan, stay_start, expected_start, expected_end in cases:
            snapshot = _empty_snapshot()
            composed = compose_trip_state(plan, snapshot)
            intake = _not_required(
                stay_start,
                stay_start + timedelta(days=1),
            )
            review = prepare_private_delivery_review(
                encode_plan(plan),
                snapshot,
                profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                source_path=SOURCE_PATH,
                target_path=TARGET_PATH,
                clock=_Clock(),
                lodging_intake=intake,
            )
            event = review.to_ephemeral_private_review()[
                "calendar_events"
            ][0]
            self.assertEqual(expected_start, event["local_start"])
            self.assertEqual(expected_end, event["local_end"])

    def test_paths_are_lexically_bound_without_filesystem_claims(self) -> None:
        _plan, _snapshot, _composed, _intake, _clock, review = _prepare(
            PrivateDeliveryProfile.HTML_PREVIEW
        )
        private = review.to_ephemeral_private_review()
        self.assertEqual(SOURCE_PATH, private["source_path"])
        self.assertEqual(TARGET_PATH, private["target_path"])
        self.assertTrue(private["create_only"])
        self.assertFalse(private["overwrite_allowed"])
        self.assertFalse(private["source_path_verified"])
        self.assertFalse(private["target_filesystem_verified"])
        self.assertFalse(private["write_authorized"])
        self.assertFalse(review.to_safe_dict()["source_path_verified"])
        self.assertFalse(review.to_safe_dict()["target_filesystem_verified"])
        for bad in (
            "relative/output",
            "/synthetic/../escape",
            "/synthetic/private target",
            "//synthetic/private-target",
            "/synthetic/private\u200btarget",
            "/synthetic/\u0085/generation-a",
            "/synthetic/\u202e/generation-a",
            "/synthetic/\ud800/generation-a",
            "/" + "é" * 128 + "/generation-a",
            "/synthetic/" + "é" * 2049,
            "/",
            "/synthetic/" + "x" * 5000,
        ):
            self.assertEqual(
                "PRIVATE_DELIVERY_TARGET_PATH_INVALID",
                _error_code(
                    lambda bad=bad: prepare_private_delivery_review(
                        encode_plan(_plan),
                        _snapshot,
                        profile=PrivateDeliveryProfile.HTML_PREVIEW,
                        source_path=SOURCE_PATH,
                        target_path=bad,
                        clock=_Clock(),
                    )
                ),
            )

    def test_profile_specific_accept_is_one_shot_and_no_write(self) -> None:
        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        _set_clock(review, EVALUATION_AT + timedelta(minutes=1))
        response = capture_private_delivery_response(
            review,
            PrivateDeliveryResponseKind.ACCEPT_HTML_PREVIEW_CANDIDATE,
        )
        self.assertTrue(response.candidate_accepted)
        self.assertEqual(PRIVATE_DELIVERY_RESPONSE_VERSION, response.contract_version)
        safe = response.to_safe_dict()
        self.assertFalse(safe["write_authorized"])
        self.assertFalse(safe["writes_performed"])
        safe_material = json.dumps(safe) + repr(response)
        for private_value in (
            response.review_id,
            response.response_id,
            response.captured_at.isoformat(),
            SOURCE_PATH,
            TARGET_PATH,
            PRIVATE_SENTINEL,
        ):
            self.assertNotIn(private_value, safe_material)
        self.assertEqual(
            "PRIVATE_DELIVERY_RESPONSE_ALREADY_CAPTURED",
            _error_code(
                lambda: capture_private_delivery_response(
                    review,
                    PrivateDeliveryResponseKind.CANCEL,
                )
            ),
        )

    def test_wrong_profile_accept_and_generic_string_fail_closed(self) -> None:
        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        self.assertEqual(
            "PRIVATE_DELIVERY_RESPONSE_PROFILE_MISMATCH",
            _error_code(
                lambda: capture_private_delivery_response(
                    review,
                    PrivateDeliveryResponseKind.ACCEPT_HTML_ICS_READY_BUNDLE_CANDIDATE,
                )
            ),
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_RESPONSE_KIND_INVALID",
            _error_code(
                lambda: capture_private_delivery_response(
                    review,
                    "continue",  # type: ignore[arg-type]
                )
            ),
        )

    def test_cancel_and_request_changes_are_non_accepting_exact_responses(self) -> None:
        for kind in (
            PrivateDeliveryResponseKind.CANCEL,
            PrivateDeliveryResponseKind.REQUEST_CHANGES,
        ):
            review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
            _set_clock(review, EVALUATION_AT + timedelta(minutes=1))
            response = capture_private_delivery_response(
                review,
                kind,
            )
            self.assertFalse(response.candidate_accepted)
            self.assertEqual(kind.value, response.to_safe_dict()["kind"])
            self.assertFalse(response.to_safe_dict()["writes_performed"])

    def test_expiry_boundary_and_clock_rollback_are_half_open(self) -> None:
        rollback = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        _set_clock(rollback, rollback.created_at - timedelta(seconds=1))
        self.assertEqual(
            "PRIVATE_DELIVERY_CLOCK_ROLLBACK",
            _error_code(
                lambda: capture_private_delivery_response(
                    rollback,
                    PrivateDeliveryResponseKind.ACCEPT_HTML_PREVIEW_CANDIDATE,
                )
            ),
        )

    def test_subsecond_review_and_readiness_deadline_are_exact(self) -> None:
        subsecond = EVALUATION_AT + timedelta(microseconds=500)
        plan, snapshot, composed, _summary, _intake = _route_case(
            evaluation_at=subsecond
        )
        preview = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=_Clock(subsecond),
        )
        self.assertEqual(subsecond, preview.created_at)
        self.assertEqual(
            "PRIVATE_DELIVERY_CALENDAR_TIME_SUBSECOND_UNSUPPORTED",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=(
                        PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE
                    ),
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(subsecond),
                    lodging_intake=_route_case(evaluation_at=subsecond)[4],
                )
            ),
        )

        deadline = EVALUATION_AT + timedelta(
            minutes=10,
            microseconds=500,
        )
        route_kwargs = {
            "first_valid_until": deadline,
            "second_valid_until": deadline,
        }
        accepted = _prepare(
            PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
            route_kwargs=route_kwargs,
        )[-1]
        self.assertEqual(deadline, accepted.expires_at)
        _set_clock(accepted, deadline - timedelta(microseconds=1))
        response = capture_private_delivery_response(
            accepted,
            PrivateDeliveryResponseKind.
            ACCEPT_HTML_ICS_READY_BUNDLE_CANDIDATE,
        )
        verify_private_delivery_response(response)

        expired = _prepare(
            PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
            route_kwargs=route_kwargs,
        )[-1]
        _set_clock(expired, deadline)
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_EXPIRED",
            _error_code(
                lambda: capture_private_delivery_response(
                    expired,
                    PrivateDeliveryResponseKind.
                    ACCEPT_HTML_ICS_READY_BUNDLE_CANDIDATE,
                )
            ),
        )

    def test_clock_callback_is_retained_validated_and_value_free(self) -> None:
        plan, snapshot, composed, _summary, _intake = _route_case()

        def hostile_clock():
            raise RuntimeError(PRIVATE_SENTINEL)

        for clock in (
            hostile_clock,
            _Clock(datetime(2026, 1, 1)),
            _Clock(datetime(2026, 1, 1, tzinfo=_MissingOffset())),
        ):
            code = _error_code(
                lambda clock=clock: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_PREVIEW,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=clock,
                )
            )
            self.assertEqual("PRIVATE_DELIVERY_CLOCK_INVALID", code)
            self.assertNotIn(PRIVATE_SENTINEL, code)

        max_plan, max_snapshot, _composed, _summary, _intake = _route_case()
        maximum = datetime.max.replace(tzinfo=EVALUATION_AT.tzinfo)
        object.__setattr__(max_snapshot, "evaluation_at", maximum)
        object.__setattr__(max_snapshot, "purge_checked_at", maximum)
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TIME_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(max_plan),
                    max_snapshot,
                    profile=PrivateDeliveryProfile.HTML_PREVIEW,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=_Clock(maximum),
                )
            ),
        )

    def test_clock_side_effect_cannot_cross_a_review_seal(self) -> None:
        plan, snapshot, composed, _summary, _intake = _route_case()
        hostile_clock = _HookClock()
        hostile_clock.hook = lambda: object.__setattr__(
            snapshot.observations[0].value,
            "canonical_json",
            b"{}",
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_EVIDENCE_SNAPSHOT_INVALID",
            _error_code(
                lambda: prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=PrivateDeliveryProfile.HTML_PREVIEW,
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=hostile_clock,
                )
            ),
        )

        plan, snapshot, composed, _summary, _intake = _route_case()
        clock = _HookClock()
        review = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=clock,
        )
        clock.value = EVALUATION_AT + timedelta(minutes=1)
        clock.hook = lambda: object.__setattr__(
            review,
            "target_path",
            "/synthetic/private-target/clock-forged",
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TAMPERED",
            _error_code(
                lambda: capture_private_delivery_response(
                    review,
                    PrivateDeliveryResponseKind.CANCEL,
                )
            ),
        )

        clock = _HookClock()
        review = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=clock,
        )
        clock.value = EVALUATION_AT + timedelta(minutes=1)
        response = capture_private_delivery_response(
            review,
            PrivateDeliveryResponseKind.CANCEL,
        )
        clock.hook = lambda: object.__setattr__(
            response,
            "candidate_accepted",
            True,
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_RESPONSE_TAMPERED",
            _error_code(
                lambda: verify_private_delivery_response(response)
            ),
        )

    def test_clock_high_water_advances_before_other_refusals(self) -> None:
        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        _set_clock(review, review.expires_at + timedelta(seconds=1))
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_EXPIRED",
            _error_code(
                lambda: capture_private_delivery_response(
                    review,
                    PrivateDeliveryResponseKind.
                    ACCEPT_HTML_ICS_READY_BUNDLE_CANDIDATE,
                )
            ),
        )
        _set_clock(review, review.expires_at - timedelta(seconds=1))
        self.assertEqual(
            "PRIVATE_DELIVERY_CLOCK_ROLLBACK",
            _error_code(
                lambda: capture_private_delivery_response(
                    review,
                    PrivateDeliveryResponseKind.
                    ACCEPT_HTML_PREVIEW_CANDIDATE,
                )
            ),
        )

        clock = _HookClock()
        plan, snapshot, _composed, _summary, _intake = _route_case()
        review = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=clock,
        )
        clock.value = EVALUATION_AT + timedelta(minutes=1)
        response = capture_private_delivery_response(
            review,
            PrivateDeliveryResponseKind.CANCEL,
        )
        original = response.candidate_accepted
        clock.value = review.expires_at + timedelta(seconds=1)
        clock.hook = lambda: object.__setattr__(
            response,
            "candidate_accepted",
            not original,
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_RESPONSE_TAMPERED",
            _error_code(
                lambda: verify_private_delivery_response(response)
            ),
        )
        object.__setattr__(response, "candidate_accepted", original)
        clock.hook = None
        clock.value = review.expires_at - timedelta(seconds=1)
        self.assertEqual(
            "PRIVATE_DELIVERY_CLOCK_ROLLBACK",
            _error_code(
                lambda: verify_private_delivery_response(response)
            ),
        )

    def test_clock_binding_is_sealed_and_cycles_do_not_leak(self) -> None:
        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        object.__setattr__(
            review,
            "_clock",
            _Clock(review.created_at + timedelta(seconds=1)),
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TAMPERED",
            _error_code(
                lambda: capture_private_delivery_response(
                    review,
                    PrivateDeliveryResponseKind.CANCEL,
                )
            ),
        )

        plan, snapshot, _composed, _summary, _intake = _route_case()
        clock = _Clock()
        review = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=clock,
        )
        clock.review = review
        review_ref = weakref.ref(review)
        clock_ref = weakref.ref(clock)
        del review
        del clock
        gc.collect()
        self.assertIsNone(review_ref())
        self.assertIsNone(clock_ref())

    def test_review_artifact_response_and_readiness_tampering_is_detected(self) -> None:
        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        calls: list[str] = []

        class HostileArtifact:
            @property
            def filename(self):
                calls.append("filename")
                raise RuntimeError(PRIVATE_SENTINEL)

        object.__setattr__(
            review,
            "artifacts",
            (HostileArtifact(),) + review.artifacts[1:],
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TAMPERED",
            _error_code(review.to_safe_dict),
        )
        self.assertEqual([], calls)

        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        object.__setattr__(
            review,
            "target_path",
            "/synthetic/private-target/forged",
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TAMPERED",
            _error_code(
                lambda: capture_private_delivery_response(
                    review,
                    PrivateDeliveryResponseKind.CANCEL,
                )
            ),
        )

        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        artifact = review.artifacts[0]
        object.__setattr__(artifact, "payload", b"tampered")
        self.assertEqual(
            "PRIVATE_DELIVERY_ARTIFACT_TAMPERED",
            _error_code(artifact.to_safe_dict),
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TAMPERED",
            _error_code(review.to_safe_dict),
        )

        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        _set_clock(review, EVALUATION_AT + timedelta(minutes=1))
        response = capture_private_delivery_response(
            review,
            PrivateDeliveryResponseKind.ACCEPT_HTML_PREVIEW_CANDIDATE,
        )
        verify_private_delivery_response(response)
        object.__setattr__(response, "candidate_accepted", False)
        self.assertEqual(
            "PRIVATE_DELIVERY_RESPONSE_TAMPERED",
            _error_code(
                lambda: verify_private_delivery_response(response)
            ),
        )

        ready = _prepare(
            PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE
        )[-1]
        assert ready.readiness is not None
        object.__setattr__(
            ready.readiness,
            "used_evidence_count",
            ready.readiness.used_evidence_count + 1,
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TAMPERED",
            _error_code(ready.to_safe_dict),
        )

    def test_registry_does_not_keep_private_objects_alive(self) -> None:
        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        reference = weakref.ref(review)
        _CLOCKS.pop(id(review), None)
        del review
        gc.collect()
        self.assertIsNone(reference())

    def test_prepare_uses_bounded_canonical_bytes_and_no_project_io(self) -> None:
        plan, snapshot, composed, _summary, _intake = _route_case()
        canonical = encode_plan(plan)
        for invalid in (
            bytearray(canonical),
            canonical + b" ",
            b"x" * (MAX_CANONICAL_PLAN_BYTES + 1),
        ):
            self.assertEqual(
                "PRIVATE_DELIVERY_CANONICAL_INVALID",
                _error_code(
                    lambda invalid=invalid: prepare_private_delivery_review(
                        invalid,  # type: ignore[arg-type]
                        snapshot,
                        profile=PrivateDeliveryProfile.HTML_PREVIEW,
                        source_path=SOURCE_PATH,
                        target_path=TARGET_PATH,
                        clock=_Clock(),
                    )
                ),
            )

        with (
            patch("builtins.open", side_effect=AssertionError("unexpected I/O")),
            patch("os.open", side_effect=AssertionError("unexpected I/O")),
            patch.object(Path, "mkdir", side_effect=AssertionError("unexpected I/O")),
            patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected I/O")),
            patch.object(Path, "read_text", side_effect=AssertionError("unexpected I/O")),
            patch.object(Path, "write_bytes", side_effect=AssertionError("unexpected I/O")),
            patch.object(Path, "write_text", side_effect=AssertionError("unexpected I/O")),
            patch(
                "trip_planner.loaders._read_json_object",
                side_effect=AssertionError("unexpected file-backed loader"),
            ),
        ):
            review = prepare_private_delivery_review(
                canonical,
                snapshot,
                profile=PrivateDeliveryProfile.HTML_PREVIEW,
                source_path=SOURCE_PATH,
                target_path=TARGET_PATH,
                clock=_Clock(),
            )
        self.assertFalse(review.to_safe_dict()["writes_performed"])
        expired = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        _set_clock(expired, expired.expires_at)
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_EXPIRED",
            _error_code(
                lambda: capture_private_delivery_response(
                    expired,
                    PrivateDeliveryResponseKind.ACCEPT_HTML_PREVIEW_CANDIDATE,
                )
            ),
        )

    def test_safe_views_repr_and_errors_do_not_echo_private_values(self) -> None:
        plan, snapshot, composed, _summary, _intake = _route_case()
        plan = copy.deepcopy(plan)
        plan["state"]["trip"]["title"] = PRIVATE_SENTINEL
        plan["revision"] = compute_revision(plan)
        composed = compose_trip_state(plan, snapshot)
        review = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_PREVIEW,
            source_path="/synthetic/private-source/" + PRIVATE_SENTINEL,
            target_path="/synthetic/private-target/generation-safe",
            clock=_Clock(),
        )
        safe_material = json.dumps(review.to_safe_dict()) + repr(review)
        for private_value in (
            PRIVATE_SENTINEL,
            review.review_id,
            review.plan_revision,
            review.composed_state_digest,
            review.canonical_source_sha256,
            EVALUATION_AT.isoformat(),
        ):
            self.assertNotIn(private_value, safe_material)
        for artifact in review.to_ephemeral_private_artifacts():
            safe_artifact = json.dumps(artifact.to_safe_dict()) + repr(artifact)
            self.assertNotIn(artifact.sha256, safe_artifact)
            self.assertNotIn(PRIVATE_SENTINEL, safe_artifact)
        code = _error_code(
            lambda: prepare_private_delivery_review(
                encode_plan(plan),
                snapshot,
                profile=PrivateDeliveryProfile.HTML_PREVIEW,
                source_path="/synthetic/\x00" + PRIVATE_SENTINEL,
                target_path=TARGET_PATH,
                clock=_Clock(),
            )
        )
        self.assertEqual("PRIVATE_DELIVERY_SOURCE_PATH_INVALID", code)
        self.assertNotIn(PRIVATE_SENTINEL, code)

    def test_review_response_and_artifact_are_factory_only_nonserializable(self) -> None:
        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        artifact = _artifact(review, "index.html")
        _set_clock(review, EVALUATION_AT + timedelta(minutes=1))
        response = capture_private_delivery_response(
            review,
            PrivateDeliveryResponseKind.CANCEL,
        )
        with self.assertRaisesRegex(ValueError, "must come from preparation"):
            PrivateDeliveryArtifact(
                filename="index.html",
                media_type="text/html; charset=utf-8",
                payload=b"private",
                sha256=hashlib.sha256(b"private").hexdigest(),
            )
        with self.assertRaisesRegex(ValueError, "must come from preparation"):
            replace(review)
        with self.assertRaisesRegex(ValueError, "must come from capture"):
            replace(response)
        with self.assertRaisesRegex(TypeError, "process-local"):
            pickle.dumps(review)
        with self.assertRaisesRegex(TypeError, "process-local"):
            pickle.dumps(response)
        with self.assertRaisesRegex(TypeError, "process-local"):
            pickle.dumps(artifact)
        self.assertEqual("index.html", artifact.filename)

    def test_public_contract_versions_and_safe_surface_are_stable(self) -> None:
        review = _prepare(PrivateDeliveryProfile.HTML_PREVIEW)[-1]
        safe = review.to_safe_dict()
        self.assertEqual(PRIVATE_DELIVERY_VERSION, safe["contract_version"])
        self.assertEqual("html_preview", safe["profile"])
        self.assertEqual(
            ["index.html", "manifest.json"], safe["artifact_filenames"]
        )
        forbidden_keys = {
            "source_path",
            "target_path",
            "created_at",
            "expires_at",
            "review_id",
            "sha256",
            "byte_count",
            "event_count",
            "trip_id",
            "title",
        }
        self.assertFalse(forbidden_keys.intersection(safe))
        self.assertIs(
            trip_planner.verify_private_delivery_response,
            verify_private_delivery_response,
        )


if __name__ == "__main__":
    unittest.main()
