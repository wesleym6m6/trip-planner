from __future__ import annotations

import copy
import math
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from trip_planner import (
    ComposedTripState,
    CheckStatus,
    EvidenceBinding,
    EvidenceLedger,
    EvidencePersistence,
    EvidenceSnapshot,
    EvidenceState,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    LiveAttribution,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderProvenance,
    ProviderRequest,
    ProviderResult,
    ProviderResultStatus,
    TravelEstimate,
    authorize_provider_result,
    compose_trip_state,
    compute_revision,
    evaluate_timeline,
    merge_provider_result,
)
from trip_planner.codec import build_plan, plan_to_trip_state
from trip_planner.scheduling import trip_state_digest


NOW = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
COMPLETED_AT = NOW + timedelta(minutes=1)
EVALUATION_AT = NOW + timedelta(hours=2)
ATTRIBUTION_LABEL = "Maps Fixture"
ATTRIBUTION_URI = (
    "https://example.test/runtime-attribution/restricted-uri-sentinel"
)


def _policies(*providers: str) -> ProviderPolicyRegistry:
    return ProviderPolicyRegistry(
        policies=tuple(
            ProviderPolicy(
                policy_id=f"{provider}-route-runtime-v1",
                provider_id=provider,
                adapter_id=provider,
                adapter_version="v1",
                contract_region="test",
                allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
                allowed_value_fields=(
                    "arrival_at",
                    "departure_at",
                    "distance_km",
                    "duration_min",
                    "fallback_from_mode",
                    "mode",
                    "static_duration_min",
                    "warning_codes",
                ),
                allowed_operations=("compute-route",),
                persistence=EvidencePersistence.MEMORY_ONLY,
                max_validity_seconds=12 * 60 * 60,
                max_retention_seconds=24 * 60 * 60,
                required_attribution_labels=(ATTRIBUTION_LABEL,),
            )
            for provider in providers
        )
    )


def _route_key(
    origin: str,
    destination: str,
    *,
    mode: str = "walking",
    departure_at: datetime | None = None,
) -> FactKey:
    qualifiers: list[tuple[str, object]] = [("mode", mode)]
    if departure_at is not None:
        qualifiers.append(
            ("departure_at", departure_at.isoformat())
        )
    return FactKey(
        kind=FactKind.ROUTE_ESTIMATE,
        subject_ids=(origin, destination),
        qualifiers=tuple(qualifiers),
    )


def _merge_routes(
    ledger: EvidenceLedger,
    *,
    provider: str,
    specs: tuple[tuple[FactKey, float, datetime], ...],
    payload_extras: dict[str, object] | None = None,
) -> tuple[EvidenceLedger, tuple[FactObservation, ...]]:
    policy = next(
        item
        for item in ledger.policies.policies
        if item.provider_id == provider
    )
    request = ProviderRequest(
        provider_id=provider,
        adapter_id=provider,
        adapter_version="v1",
        operation="compute-route",
        fact_keys=tuple(key for key, _duration, _valid_until in specs),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
    )
    observations: list[FactObservation] = []
    for index, (key, duration, valid_until) in enumerate(specs):
        payload: dict[str, object] = {
            "mode": key.qualifier_map["mode"],
            "duration_min": duration,
            "distance_km": duration / 10,
        }
        if "departure_at" in key.qualifier_map:
            payload["departure_at"] = key.qualifier_map[
                "departure_at"
            ]
        if payload_extras is not None:
            payload.update(payload_extras)
        observations.append(
            FactObservation(
                key=key,
                value=FactValue.from_payload(
                    FactKind.ROUTE_ESTIMATE, payload
                ),
                provenance=ProviderProvenance(
                    provider_id=provider,
                    adapter_id=provider,
                    adapter_version="v1",
                    request_fingerprint=request.request_fingerprint,
                    retention_policy_id=policy.policy_id,
                    response_id=f"runtime-response-{index}",
                    source_uri="https://example.test/runtime-source",
                    attributions=(
                        (ATTRIBUTION_LABEL, ATTRIBUTION_URI),
                    ),
                ),
                retrieved_at=NOW,
                valid_until=valid_until,
                purge_at=NOW + timedelta(hours=23),
                confidence=1.0,
            )
        )
    result = ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=ProviderResultStatus.SUCCESS,
        observations=tuple(observations),
        problems=(),
        attempts_used=1,
        completed_at=COMPLETED_AT,
    )
    authorized = authorize_provider_result(
        request, result, ledger.policies
    )
    merged = merge_provider_result(
        ledger,
        authorized,
        purge_now=COMPLETED_AT,
    )
    return merged.ledger, tuple(observations)


def _snapshot(
    ledger: EvidenceLedger,
    *,
    evaluation_at: datetime = EVALUATION_AT,
    purge_now: datetime = COMPLETED_AT,
) -> EvidenceSnapshot:
    return EvidenceSnapshot.from_ledger(
        ledger,
        evaluation_at=evaluation_at,
        purge_now=purge_now,
        store_revision="9" * 64,
    )


def _canonical_plan(*, route_mode: str = "walking") -> dict[str, Any]:
    return build_plan(
        trip_id="composition-trip",
        generation=1,
        state={
            "trip": {
                "slug": "composition-trip",
                "title": "Composition fixture",
                "timezone": "Asia/Taipei",
                "date_range": "2026-10-01 ~ 2026-10-01",
                "cities": ["Fixture City"],
            },
            "itinerary": {
                "available_modes": sorted(
                    {"walking", "transit", route_mode}
                ),
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": "2026-10-01",
                        "timezone": "Asia/Taipei",
                        "available_start": "08:00",
                        "available_end": "20:00",
                        "start_location_id": "loc-a",
                        "end_location_id": "loc-b",
                        "places": [
                            {
                                "activity_id": "activity-a",
                                "title": "Alpha",
                                "location_id": "loc-a",
                                "time": "09:00",
                                "duration_min": 30,
                                "decision_state": "selected",
                                "flexibility": "movable",
                                "evidence_state": "verified",
                            },
                            {
                                "activity_id": "activity-b",
                                "title": "Beta",
                                "location_id": "loc-b",
                                "time": "11:00",
                                "duration_min": 45,
                                "decision_state": "selected",
                                "flexibility": "movable",
                                "evidence_state": "verified",
                            },
                        ],
                        "travel": [
                            {
                                "from_activity_id": "activity-a",
                                "to_activity_id": "activity-b",
                                "source": "canonical-fixture",
                                "recommended_mode": route_mode,
                                "modes": {
                                    route_mode: {
                                        "duration_min": 99,
                                        "buffer_min": 7,
                                        "distance_km": 9.9,
                                        "evidence_state": "unverified",
                                    }
                                },
                            },
                            {
                                "from_activity_id": "activity-b",
                                "to_activity_id": "activity-a",
                                "source": "canonical-fixture",
                                "recommended_mode": route_mode,
                                "modes": {
                                    route_mode: {
                                        "duration_min": 88,
                                        "buffer_min": 5,
                                        "distance_km": 8.8,
                                        "evidence_state": "unverified",
                                    }
                                },
                            },
                        ],
                    }
                ],
            },
        },
    )


class Phase4CompositionTests(unittest.TestCase):
    def test_stable_binding_ignores_purge_clock_and_snapshot_id(self) -> None:
        policies = _policies("route-a")
        key = _route_key("loc-a", "loc-b")
        ledger, _observations = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=((key, 37.25, NOW + timedelta(hours=4)),),
        )
        first_snapshot = _snapshot(
            ledger, purge_now=COMPLETED_AT
        )
        second_snapshot = _snapshot(
            ledger, purge_now=COMPLETED_AT + timedelta(minutes=5)
        )

        first = compose_trip_state(_canonical_plan(), first_snapshot)
        replay = compose_trip_state(_canonical_plan(), first_snapshot)
        second = compose_trip_state(_canonical_plan(), second_snapshot)

        self.assertEqual(first, replay)
        self.assertNotEqual(
            first.evidence.snapshot_id, second.evidence.snapshot_id
        )
        self.assertNotEqual(
            first.evidence.purge_checked_at,
            second.evidence.purge_checked_at,
        )
        self.assertEqual(
            first.evidence.binding_digest,
            second.evidence.binding_digest,
        )
        self.assertEqual(
            first.composed_state_digest,
            second.composed_state_digest,
        )

    def test_redaction_keeps_state_values_and_dynamic_uri_runtime_only(
        self,
    ) -> None:
        policies = _policies("route-a")
        key = _route_key("loc-a", "loc-b")
        ledger, _observations = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=((key, 37.25, NOW + timedelta(hours=4)),),
        )

        composed = compose_trip_state(
            _canonical_plan(), _snapshot(ledger)
        )
        durable = composed.to_dict()
        rendered = repr(composed)
        durable_rendered = repr(durable)

        self.assertNotIn("state", durable)
        self.assertNotIn("live_attributions", durable)
        self.assertNotIn("37.25", rendered)
        self.assertNotIn("37.25", durable_rendered)
        self.assertNotIn("restricted-uri-sentinel", rendered)
        self.assertNotIn(
            "restricted-uri-sentinel", durable_rendered
        )
        self.assertEqual(
            ATTRIBUTION_URI, composed.live_attributions[0].uri
        )
        self.assertNotIn(
            "restricted-uri-sentinel",
            repr(composed.live_attributions[0]),
        )
        for value in (
            composed.plan_revision,
            composed.evidence.policy_registry_revision,
            composed.evidence.store_revision,
            composed.evidence.evidence_revision,
            composed.evidence.snapshot_id,
            composed.evidence.binding_digest,
        ):
            self.assertEqual(64, len(value))
            self.assertEqual(value, value.lower())
            self.assertTrue(set(value).issubset(set("0123456789abcdef")))

    def test_fresh_and_stale_routes_overlay_without_mutating_plan(
        self,
    ) -> None:
        policies = _policies("route-a")
        fresh_key = _route_key("loc-a", "loc-b")
        stale_key = _route_key("loc-b", "loc-a")
        ledger, observations = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=(
                (
                    fresh_key,
                    31,
                    EVALUATION_AT + timedelta(hours=1),
                ),
                (
                    stale_key,
                    42,
                    EVALUATION_AT - timedelta(hours=1),
                ),
            ),
        )
        plan = _canonical_plan()
        before = copy.deepcopy(plan)
        canonical_state = plan_to_trip_state(plan)

        composed = compose_trip_state(plan, _snapshot(ledger))

        self.assertEqual(before, plan)
        self.assertEqual(
            trip_state_digest(canonical_state),
            composed.canonical_state_digest,
        )
        self.assertNotEqual(
            composed.canonical_state_digest,
            composed.composed_state_digest,
        )
        by_edge = {
            (
                item.from_location_id,
                item.to_location_id,
                item.mode,
            ): item
            for item in composed.state.travel_estimates
        }
        fresh = by_edge[("loc-a", "loc-b", "walking")]
        stale = by_edge[("loc-b", "loc-a", "walking")]
        self.assertEqual(31, fresh.duration_min)
        self.assertEqual(7, fresh.buffer_min)
        self.assertTrue(fresh.recommended)
        self.assertEqual(EvidenceState.VERIFIED, fresh.evidence_state)
        self.assertEqual(42, stale.duration_min)
        self.assertEqual(5, stale.buffer_min)
        self.assertTrue(stale.recommended)
        self.assertEqual(EvidenceState.STALE, stale.evidence_state)
        for estimate in (fresh, stale):
            self.assertTrue(estimate.source.startswith("fact:"))
            self.assertEqual(estimate.source, estimate.evidence_ref)
            self.assertIsNotNone(estimate.fresh_until)
        self.assertEqual(
            tuple(
                sorted(item.observation_id for item in observations)
            ),
            composed.evidence.used_observation_ids,
        )
        self.assertTrue(composed.evidence.requires_live_attribution)
        self.assertEqual(
            (ATTRIBUTION_LABEL,),
            composed.evidence.required_attribution_labels,
        )
        self.assertEqual(
            {"route-a"},
            {
                item.provider_id
                for item in composed.live_attributions
            },
        )
        report = evaluate_timeline(
            composed.state, now=EVALUATION_AT
        )
        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assertTrue(
            any(
                issue.code == "TRAVEL_EVIDENCE_UNVERIFIED"
                for issue in composed.state.load_issues
            )
        )

    def test_verified_routes_supersede_only_exact_loader_warning(
        self,
    ) -> None:
        policies = _policies("route-a")
        forward = _route_key("loc-a", "loc-b")
        reverse = _route_key("loc-b", "loc-a")
        ledger, _observations = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=(
                (
                    forward,
                    31,
                    EVALUATION_AT + timedelta(hours=1),
                ),
                (
                    reverse,
                    42,
                    EVALUATION_AT + timedelta(hours=1),
                ),
            ),
        )
        plan = _canonical_plan()
        canonical = plan_to_trip_state(plan)
        before = evaluate_timeline(
            canonical, now=EVALUATION_AT
        )

        composed = compose_trip_state(plan, _snapshot(ledger))
        after = evaluate_timeline(
            composed.state, now=EVALUATION_AT
        )

        self.assertEqual(
            CheckStatus.NEEDS_VERIFICATION, before.status
        )
        self.assertTrue(
            any(
                issue.code == "TRAVEL_EVIDENCE_UNVERIFIED"
                for issue in canonical.load_issues
            )
        )
        self.assertFalse(
            any(
                issue.code == "TRAVEL_EVIDENCE_UNVERIFIED"
                for issue in composed.state.load_issues
            )
        )
        self.assertEqual(CheckStatus.FEASIBLE, after.status)

        malformed = _canonical_plan()
        first_edge = malformed["state"]["itinerary"]["days"][0][
            "travel"
        ][0]
        first_edge["modes"]["transit"] = {
            "evidence_state": "unverified"
        }
        malformed["revision"] = compute_revision(malformed)
        malformed_composed = compose_trip_state(
            malformed, _snapshot(ledger)
        )
        malformed_codes = {
            issue.code
            for issue in malformed_composed.state.load_issues
        }
        self.assertNotIn(
            "TRAVEL_EVIDENCE_UNVERIFIED", malformed_codes
        )
        self.assertIn("MISSING_TRAVEL_ESTIMATE", malformed_codes)
        self.assertEqual(
            CheckStatus.NEEDS_VERIFICATION,
            evaluate_timeline(
                malformed_composed.state, now=EVALUATION_AT
            ).status,
        )

    def test_conflicted_and_missing_routes_are_not_projected(self) -> None:
        key = _route_key("loc-a", "loc-b")
        policies = _policies("route-a", "route-b")
        ledger, _first = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=((key, 30, NOW + timedelta(hours=4)),),
        )
        ledger, _second = _merge_routes(
            ledger,
            provider="route-b",
            specs=((key, 45, NOW + timedelta(hours=4)),),
        )
        conflicted = compose_trip_state(
            _canonical_plan(), _snapshot(ledger)
        )

        estimate = next(
            item
            for item in conflicted.state.travel_estimates
            if item.from_location_id == "loc-a"
            and item.to_location_id == "loc-b"
        )
        self.assertEqual(99, estimate.duration_min)
        self.assertEqual("canonical-fixture", estimate.source)
        self.assertEqual((), conflicted.evidence.used_observation_ids)
        self.assertEqual((), conflicted.live_attributions)
        self.assertFalse(
            conflicted.evidence.requires_live_attribution
        )
        self.assertEqual(
            CheckStatus.NEEDS_VERIFICATION,
            evaluate_timeline(
                conflicted.state, now=EVALUATION_AT
            ).status,
        )
        self.assertTrue(
            any(
                issue.code == "TRAVEL_EVIDENCE_UNVERIFIED"
                for issue in conflicted.state.load_issues
            )
        )

        empty = compose_trip_state(
            _canonical_plan(),
            _snapshot(EvidenceLedger(policies)),
        )
        self.assertEqual(
            empty.canonical_state_digest,
            empty.composed_state_digest,
        )
        self.assertFalse(
            any(
                (item.source or "").startswith("fact:")
                for item in empty.state.travel_estimates
            )
        )
        self.assertEqual(
            CheckStatus.NEEDS_VERIFICATION,
            evaluate_timeline(
                empty.state, now=EVALUATION_AT
            ).status,
        )
        self.assertTrue(
            any(
                issue.code == "TRAVEL_EVIDENCE_UNVERIFIED"
                for issue in empty.state.load_issues
            )
        )

    def test_timed_route_keeps_exact_query_context(self) -> None:
        policies = _policies("route-a")
        departure_at = datetime(
            2026, 10, 1, 1, tzinfo=timezone.utc
        )
        key = _route_key(
            "loc-a",
            "loc-b",
            mode="transit",
            departure_at=departure_at,
        )
        ledger, observations = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=((key, 25, NOW + timedelta(hours=4)),),
        )

        composed = compose_trip_state(
            _canonical_plan(), _snapshot(ledger)
        )
        timed = next(
            item
            for item in composed.state.travel_estimates
            if item.mode == "transit"
        )

        self.assertEqual(departure_at, timed.query_departure_at)
        self.assertIsNone(timed.query_arrival_at)
        self.assertEqual(
            f"fact:{observations[0].observation_id}", timed.source
        )

    def test_route_runtime_metadata_projects_and_discloses_without_blocking(
        self,
    ) -> None:
        policies = _policies("route-a")
        ledger, _observations = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=(
                (
                    _route_key("loc-a", "loc-b", mode="driving"),
                    31,
                    NOW + timedelta(hours=4),
                ),
                (
                    _route_key("loc-b", "loc-a", mode="driving"),
                    42,
                    NOW + timedelta(hours=4),
                ),
            ),
            payload_extras={
                "static_duration_min": 28,
                "fallback_from_mode": "transit",
                "warning_codes": [
                    "beta_route",
                    "route_token_missing",
                ],
            },
        )

        composed = compose_trip_state(
            _canonical_plan(route_mode="driving"), _snapshot(ledger)
        )
        selected = next(
            item
            for item in composed.state.travel_estimates
            if item.from_location_id == "loc-a"
            and item.to_location_id == "loc-b"
            and item.mode == "driving"
        )
        report = evaluate_timeline(
            composed.state, now=EVALUATION_AT
        )

        self.assertEqual(28, selected.static_duration_min)
        self.assertEqual("transit", selected.fallback_from_mode)
        self.assertEqual(
            ("beta_route", "route_token_missing"),
            selected.warning_codes,
        )
        self.assertEqual("driving", selected.mode)
        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        provider_warnings = [
            issue
            for issue in report.issues
            if issue.code == "ROUTE_PROVIDER_WARNING"
        ]
        self.assertEqual(2, len(provider_warnings))
        self.assertEqual(
            {"beta_route", "route_token_missing"},
            {
                dict(issue.details)["warning_code"]
                for issue in provider_warnings
            },
        )
        self.assertTrue(
            all(
                issue.severity.value == "warning"
                and dict(issue.details)["status_effect"] == "none"
                for issue in provider_warnings
            )
        )
        fallback = next(
            issue
            for issue in report.issues
            if issue.code == "TRANSIT_FALLBACK_DISCLOSURE"
        )
        self.assertEqual("driving", dict(fallback.details)["mode"])
        self.assertEqual(
            "transit",
            dict(fallback.details)["fallback_from_mode"],
        )
        self.assertEqual("none", dict(fallback.details)["status_effect"])

    def test_route_runtime_metadata_validation_is_strict(self) -> None:
        kwargs = {
            "from_location_id": "loc-a",
            "to_location_id": "loc-b",
            "mode": "walking",
            "duration_min": 10,
        }
        invalid_overrides = (
            {"static_duration_min": -1},
            {"static_duration_min": math.inf},
            {"static_duration_min": math.nan},
            {"static_duration_min": True},
            {"fallback_from_mode": "walking"},
            {"warning_codes": ["beta_route"]},
            {"warning_codes": ("BetaRoute",)},
            {"warning_codes": ("1_beta_route",)},
            {"warning_codes": ("beta_route", "beta_route")},
        )

        for override in invalid_overrides:
            with self.subTest(override=override):
                with self.assertRaises((TypeError, ValueError)):
                    TravelEstimate(**kwargs, **override)

    def test_missing_required_live_label_fails_closed(self) -> None:
        policies = _policies("route-a")
        key = _route_key("loc-a", "loc-b")
        ledger, _observations = _merge_routes(
            EvidenceLedger(policies),
            provider="route-a",
            specs=((key, 37.25, NOW + timedelta(hours=4)),),
        )
        composed = compose_trip_state(
            _canonical_plan(), _snapshot(ledger)
        )
        wrong = LiveAttribution(
            observation_id=(
                composed.evidence.used_observation_ids[0]
            ),
            provider_id="route-a",
            label="Wrong label",
        )

        with self.assertRaises(ValueError):
            ComposedTripState(
                state=composed.state,
                trip_id=composed.trip_id,
                plan_revision=composed.plan_revision,
                canonical_state_digest=(
                    composed.canonical_state_digest
                ),
                composed_state_digest=(
                    composed.composed_state_digest
                ),
                evidence=EvidenceBinding(
                    policy_registry_revision=(
                        composed.evidence.policy_registry_revision
                    ),
                    store_revision=(
                        composed.evidence.store_revision
                    ),
                    evidence_revision=(
                        composed.evidence.evidence_revision
                    ),
                    evaluation_at=(
                        composed.evidence.evaluation_at
                    ),
                    purge_checked_at=(
                        composed.evidence.purge_checked_at
                    ),
                    snapshot_id=composed.evidence.snapshot_id,
                    used_observation_ids=(
                        composed.evidence.used_observation_ids
                    ),
                    required_attribution_labels=(
                        composed.evidence.required_attribution_labels
                    ),
                    requires_live_attribution=True,
                ),
                live_attributions=(wrong,),
            )


if __name__ == "__main__":
    unittest.main()
