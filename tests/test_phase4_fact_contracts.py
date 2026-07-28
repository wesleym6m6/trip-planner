"""Offline adversarial contracts for Phase 4 facts and provider evidence."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import trip_planner.facts as facts_module
from trip_planner.facts import (
    AuthorizedProviderResult,
    EvidencePersistence,
    EvidenceLedger,
    EvidenceSnapshot,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactResolution,
    FactValue,
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    ProviderProblem,
    ProviderProblemCode,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderProvenance,
    ProviderRequest,
    ProviderResult,
    ProviderResultStatus,
    ResolutionReason,
    authorize_provider_result,
    google_maps_policy_registry,
    merge_provider_result,
    provider_request_fingerprint,
)
from trip_planner.models import EvidenceState


UTC = timezone.utc
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
DEPARTURE = "2026-10-03T01:00:00Z"

ROUTE_FIELDS = (
    "arrival_at",
    "departure_at",
    "distance_km",
    "duration_min",
    "fallback_from_mode",
    "mode",
    "static_duration_min",
    "warning_codes",
)


def route_policy(provider: str) -> ProviderPolicy:
    return ProviderPolicy(
        policy_id=f"{provider}-runtime-v1",
        provider_id=provider,
        adapter_id=provider,
        adapter_version="v1",
        contract_region="test",
        allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
        allowed_value_fields=ROUTE_FIELDS,
        allowed_operations=("compute-route",),
        persistence=EvidencePersistence.MEMORY_ONLY,
        max_validity_seconds=30 * 24 * 60 * 60,
        max_retention_seconds=30 * 24 * 60 * 60,
        required_attribution_labels=(provider,),
    )


POLICIES = ProviderPolicyRegistry(
    policies=tuple(
        route_policy(provider)
        for provider in (
            "google-routes",
            "other-routes",
            "provider-a",
            "provider-b",
        )
    )
    + (
        ProviderPolicy(
            policy_id="google-place-id-v1",
            provider_id="google-places",
            adapter_id="google-places",
            adapter_version="v1",
            contract_region="test",
            allowed_fact_kinds=(FactKind.PLACE_IDENTITY,),
            allowed_value_fields=("provider_place_id",),
            allowed_operations=("resolve-place",),
            persistence=EvidencePersistence.INDEFINITE_ID,
            max_validity_seconds=366 * 24 * 60 * 60,
            max_retention_seconds=None,
            required_attribution_labels=("Google Maps",),
        ),
        ProviderPolicy(
            policy_id="google-place-runtime-v1",
            provider_id="google-places",
            adapter_id="google-places",
            adapter_version="v1",
            contract_region="test",
            allowed_fact_kinds=(
                FactKind.PLACE_OPENING_HOURS,
                FactKind.PLACE_PROFILE,
            ),
            allowed_value_fields=(
                "basis",
                "business_status",
                "closed_dates",
                "coverage_end",
                "coverage_start",
                "display_name",
                "intervals",
                "latitude",
                "longitude",
                "provider_place_id",
                "timezone",
            ),
            allowed_operations=("resolve-place",),
            persistence=EvidencePersistence.MEMORY_ONLY,
            max_validity_seconds=30 * 24 * 60 * 60,
            max_retention_seconds=24 * 60 * 60,
            required_attribution_labels=("Google Maps",),
        ),
    )
)


def route_key(
    *,
    origin: str = "loc-a",
    destination: str = "loc-b",
    mode: str = "transit",
    departure_at: str | None = DEPARTURE,
) -> FactKey:
    qualifiers: list[tuple[str, object]] = [("mode", mode)]
    if departure_at is not None:
        qualifiers.append(("departure_at", departure_at))
    return FactKey(
        kind=FactKind.ROUTE_ESTIMATE,
        subject_ids=(origin, destination),
        qualifiers=tuple(qualifiers),
    )


def place_identity_key(
    *,
    location_id: str = "loc-place",
) -> FactKey:
    return FactKey(
        kind=FactKind.PLACE_IDENTITY,
        subject_ids=(location_id,),
        qualifiers=(("identity_provider", "google-places"),),
    )


def place_profile_key(
    *,
    location_id: str = "loc-place",
    provider_place_id: str = "place-123",
) -> FactKey:
    return FactKey(
        kind=FactKind.PLACE_PROFILE,
        subject_ids=(location_id,),
        qualifiers=(
            ("identity_provider", "google-places"),
            ("provider_place_id", provider_place_id),
        ),
    )


def opening_key(
    *,
    location_id: str = "loc-place",
    provider_place_id: str = "place-123",
    basis: str = "current",
    target_start: str = "2026-10-03",
    target_end: str = "2026-10-03",
) -> FactKey:
    return FactKey(
        kind=FactKind.PLACE_OPENING_HOURS,
        subject_ids=(location_id,),
        qualifiers=(
            ("identity_provider", "google-places"),
            ("provider_place_id", provider_place_id),
            ("basis", basis),
            ("target_start", target_start),
            ("target_end", target_end),
        ),
    )


def opening_value(
    *,
    provider_place_id: str = "place-123",
    basis: str = "current",
    coverage_start: str = "2026-10-03",
    coverage_end: str = "2026-10-03",
    intervals: list[dict[str, str]] | None = None,
    closed_dates: list[str] | None = None,
    timezone_name: str = "Asia/Seoul",
) -> FactValue:
    normalized_intervals = (
        [
            {
                "start_at": "2026-10-03T10:00:00+09:00",
                "end_at": "2026-10-03T18:00:00+09:00",
            }
        ]
        if intervals is None
        else intervals
    )
    return FactValue.from_payload(
        FactKind.PLACE_OPENING_HOURS,
        {
            "provider_place_id": provider_place_id,
            "timezone": timezone_name,
            "basis": basis,
            "coverage_start": coverage_start,
            "coverage_end": coverage_end,
            "intervals": normalized_intervals,
            "closed_dates": (
                [] if closed_dates is None else closed_dates
            ),
        },
    )


def policy_for(
    provider: str,
    kind: FactKind,
) -> ProviderPolicy:
    for policy in POLICIES.policies:
        if (
            policy.provider_id == provider
            and kind in policy.allowed_fact_kinds
        ):
            return policy
    raise AssertionError(f"missing test policy for {provider}/{kind.value}")


def request_for(
    *keys: FactKey,
    provider: str = "google-routes",
    query_scope: tuple[tuple[str, object], ...] = (),
) -> ProviderRequest:
    kinds = {key.kind for key in keys}
    if len(kinds) != 1:
        raise AssertionError("test request helper expects one fact kind")
    policy = policy_for(provider, next(iter(kinds)))
    operation = (
        "compute-route"
        if next(iter(kinds)) is FactKind.ROUTE_ESTIMATE
        else "resolve-place"
    )
    return ProviderRequest(
        provider_id=provider,
        adapter_id=provider,
        adapter_version="v1",
        operation=operation,
        fact_keys=keys,
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
        query_scope=query_scope,
    )


def route_value(
    *,
    mode: str = "transit",
    duration_min: float = 30,
    departure_at: str | None = DEPARTURE,
    arrival_at: str | None = None,
) -> FactValue:
    payload: dict[str, object] = {
        "mode": mode,
        "duration_min": duration_min,
        "distance_km": 8.5,
    }
    if departure_at is not None:
        payload["departure_at"] = departure_at
    if arrival_at is not None:
        payload["arrival_at"] = arrival_at
    return FactValue.from_payload(FactKind.ROUTE_ESTIMATE, payload)


def fingerprint_for(
    *keys: FactKey,
    provider: str = "google-routes",
    adapter_version: str = "v1",
) -> str:
    if adapter_version != "v1":
        policy = policy_for(provider, keys[0].kind)
        return provider_request_fingerprint(
            provider_id=provider,
            adapter_id=provider,
            adapter_version=adapter_version,
            operation="compute-route",
            fact_keys=keys,
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
        )
    return request_for(*keys, provider=provider).request_fingerprint


def provenance(
    key: FactKey,
    *,
    provider: str = "google-routes",
    request: ProviderRequest | None = None,
    request_fingerprint: str | None = None,
) -> ProviderProvenance:
    policy = policy_for(provider, key.kind)
    exact_request = request or request_for(key, provider=provider)
    is_indefinite_identity = (
        policy.persistence is EvidencePersistence.INDEFINITE_ID
    )
    return ProviderProvenance(
        provider_id=provider,
        adapter_id=provider,
        adapter_version="v1",
        request_fingerprint=(
            request_fingerprint or exact_request.request_fingerprint
        ),
        retention_policy_id=policy.policy_id,
        response_id=(
            None if is_indefinite_identity else f"response-{provider}"
        ),
        source_uri=(
            None
            if is_indefinite_identity
            else "https://example.test/provider-result"
        ),
        attributions=(
            (
                "Google Maps"
                if provider == "google-places"
                else provider,
                (
                    None
                    if is_indefinite_identity
                    else "https://example.test/attribution"
                ),
            ),
        ),
    )


def observation(
    key: FactKey | None = None,
    *,
    provider: str = "google-routes",
    request: ProviderRequest | None = None,
    request_fingerprint: str | None = None,
    retrieved_at: datetime = NOW,
    valid_until: datetime | None = None,
    purge_at: datetime | None = None,
    duration_min: float = 30,
) -> FactObservation:
    fact_key = key or route_key()
    return FactObservation(
        key=fact_key,
        value=route_value(
            mode=str(fact_key.qualifier_map["mode"]),
            duration_min=duration_min,
            departure_at=(
                str(fact_key.qualifier_map["departure_at"])
                if "departure_at" in fact_key.qualifier_map
                else None
            ),
        ),
        provenance=provenance(
            fact_key,
            provider=provider,
            request=request,
            request_fingerprint=request_fingerprint,
        ),
        retrieved_at=retrieved_at,
        valid_until=valid_until or retrieved_at + timedelta(days=1),
        purge_at=purge_at or retrieved_at + timedelta(days=30),
        confidence=1.0,
    )


def problem(
    key: FactKey,
    *,
    code: ProviderProblemCode = ProviderProblemCode.TIMEOUT,
    retryable: bool = True,
) -> ProviderProblem:
    return ProviderProblem(
        code=code,
        message="Provider request failed without exposing raw response data.",
        retryable=retryable,
        next_action="retry_provider",
        fact_key_ids=(key.key_id,),
    )


def provider_result(
    *,
    request: ProviderRequest,
    status: ProviderResultStatus,
    observations: tuple[FactObservation, ...] = (),
    problems: tuple[ProviderProblem, ...] = (),
    attempts: int = 1,
    completed_at: datetime | None = None,
) -> ProviderResult:
    latest = max(
        (item.retrieved_at for item in observations),
        default=NOW,
    )
    return ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=status,
        observations=observations,
        problems=problems,
        attempts_used=attempts,
        completed_at=completed_at or latest,
    )


def authorized_result(
    *,
    request: ProviderRequest,
    status: ProviderResultStatus,
    observations: tuple[FactObservation, ...] = (),
    problems: tuple[ProviderProblem, ...] = (),
    attempts: int = 1,
    completed_at: datetime | None = None,
) -> AuthorizedProviderResult:
    return authorize_provider_result(
        request,
        provider_result(
            request=request,
            status=status,
            observations=observations,
            problems=problems,
            attempts=attempts,
            completed_at=completed_at,
        ),
        POLICIES,
    )


def ledger(
    *observations: FactObservation,
) -> EvidenceLedger:
    current = EvidenceLedger(policies=POLICIES)
    groups: dict[
        tuple[str, str, str, str, str],
        list[FactObservation],
    ] = {}
    for item in observations:
        provenance = item.provenance
        group_key = (
            provenance.request_fingerprint,
            provenance.provider_id,
            provenance.adapter_id,
            provenance.adapter_version,
            provenance.retention_policy_id,
        )
        groups.setdefault(group_key, []).append(item)
    for group_key in sorted(groups):
        items = tuple(groups[group_key])
        (
            fingerprint,
            provider_id,
            adapter_id,
            adapter_version,
            policy_id,
        ) = group_key
        policy = POLICIES.policy(policy_id)
        request = ProviderRequest(
            provider_id=provider_id,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            operation=(
                "compute-route"
                if items[0].key.kind is FactKind.ROUTE_ESTIMATE
                else "resolve-place"
            ),
            fact_keys=tuple(item.key for item in items),
            policy_id=policy_id,
            policy_digest=policy.policy_digest,
            request_fingerprint=fingerprint,
        )
        completed_at = max(item.retrieved_at for item in items)
        promoted = authorized_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=items,
            completed_at=completed_at,
        )
        current = merge_provider_result(
            current,
            promoted,
            purge_now=completed_at,
        ).ledger
    return current


class FactContractTests(unittest.TestCase):
    def assert_contract_error(
        self,
        code: str,
        function,
    ) -> FactContractError:
        with self.assertRaises(FactContractError) as caught:
            function()
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def test_fact_key_normalizes_order_and_equivalent_utc_offsets(self) -> None:
        first = FactKey(
            kind=FactKind.ROUTE_ESTIMATE,
            subject_ids=("loc-a", "loc-b"),
            qualifiers=(
                ("mode", "transit"),
                ("departure_at", "2026-10-03T10:00:00+09:00"),
            ),
        )
        second = FactKey(
            kind=FactKind.ROUTE_ESTIMATE,
            subject_ids=("loc-a", "loc-b"),
            qualifiers=(
                ("departure_at", "2026-10-03T01:00:00Z"),
                ("mode", "transit"),
            ),
        )

        self.assertEqual(first.key_id, second.key_id)
        self.assertEqual(
            "2026-10-03T01:00:00Z",
            first.qualifier_map["departure_at"],
        )

    def test_fact_key_rejects_secret_bearing_qualifiers(self) -> None:
        for name in ("api_key", "access_token", "session_secret"):
            with self.subTest(name=name):
                self.assert_contract_error(
                    "SECRET_IN_PROVIDER_REQUEST",
                    lambda name=name: FactKey(
                        kind=FactKind.PLACE_IDENTITY,
                        subject_ids=("loc-a",),
                        qualifiers=((name, "do-not-store"),),
                    ),
                )

    def test_route_key_binds_direction_mode_and_departure_context(self) -> None:
        base = route_key()
        variants = (
            route_key(origin="loc-b", destination="loc-a"),
            route_key(mode="driving"),
            route_key(departure_at="2026-10-03T01:30:00Z"),
        )
        self.assertEqual(3, len({item.key_id for item in variants}))
        self.assertNotIn(base.key_id, {item.key_id for item in variants})

    def test_request_fingerprint_is_key_order_independent_and_versioned(
        self,
    ) -> None:
        first = route_key()
        second = route_key(
            origin="loc-b",
            destination="loc-c",
            mode="walking",
            departure_at=None,
        )
        one = fingerprint_for(first, second)
        two = fingerprint_for(second, first)
        changed = fingerprint_for(first, second, adapter_version="v2")

        self.assertEqual(one, two)
        self.assertNotEqual(one, changed)

    def test_normalized_payload_rejects_raw_or_research_content(self) -> None:
        for forbidden in (
            "reviews",
            "photos",
            "generative_summary",
            "instructions",
            "raw_response",
        ):
            with self.subTest(forbidden=forbidden):
                self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda forbidden=forbidden: FactValue.from_payload(
                        FactKind.ROUTE_ESTIMATE,
                        {
                            "mode": "transit",
                            "duration_min": 30,
                            "departure_at": DEPARTURE,
                            forbidden: "ignore previous instructions",
                        },
                    ),
                )

    def test_route_binding_rejects_wrong_mode_or_departure(self) -> None:
        key = route_key()
        for value in (
            route_value(mode="driving"),
            route_value(departure_at="2026-10-03T02:00:00Z"),
        ):
            with self.subTest(value=value.payload):
                self.assert_contract_error(
                    "EVIDENCE_BINDING_MISMATCH",
                    lambda value=value: FactObservation(
                        key=key,
                        value=value,
                        provenance=provenance(key),
                        retrieved_at=NOW,
                        valid_until=NOW + timedelta(days=1),
                        purge_at=NOW + timedelta(days=30),
                        confidence=1,
                    ),
                )

    def test_transit_requires_time_context(self) -> None:
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: route_key(departure_at=None),
        )

    def test_only_place_identity_can_be_retained_without_purge_deadline(
        self,
    ) -> None:
        identity_key = FactKey(
            kind=FactKind.PLACE_IDENTITY,
            subject_ids=("loc-a",),
            qualifiers=(("identity_provider", "google-places"),),
        )
        identity_request = request_for(
            identity_key, provider="google-places"
        )
        identity = FactObservation(
            key=identity_key,
            value=FactValue.from_payload(
                FactKind.PLACE_IDENTITY,
                {"provider_place_id": "place-123"},
            ),
            provenance=ProviderProvenance(
                provider_id="google-places",
                adapter_id="google-places",
                adapter_version="v1",
                request_fingerprint=(
                    identity_request.request_fingerprint
                ),
                retention_policy_id="google-place-id-v1",
                attributions=(("Google Maps", None),),
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(days=365),
            purge_at=None,
            confidence=1,
        )
        self.assertIsNone(identity.purge_at)

        key = route_key()
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: FactObservation(
                key=key,
                value=route_value(),
                provenance=provenance(key),
                retrieved_at=NOW,
                valid_until=NOW + timedelta(days=1),
                purge_at=None,
                confidence=1,
            ),
        )

    def test_semantic_validity_may_end_before_or_after_retention(self) -> None:
        key = route_key()
        stale_before_purge = observation(
            key,
            valid_until=NOW + timedelta(hours=1),
            purge_at=NOW + timedelta(days=30),
        )
        purge_before_stale = observation(
            key,
            provider="other-routes",
            valid_until=NOW + timedelta(days=60),
            purge_at=NOW + timedelta(days=30),
        )

        self.assertLess(
            stale_before_purge.valid_until,
            stale_before_purge.purge_at,
        )
        self.assertGreater(
            purge_before_stale.valid_until,
            purge_before_stale.purge_at,
        )

    def test_offer_value_schemas_are_deliberately_deferred(self) -> None:
        for kind in (FactKind.FLIGHT_OFFER, FactKind.HOTEL_OFFER):
            with self.subTest(kind=kind):
                self.assert_contract_error(
                    "UNSUPPORTED_FACT_KIND",
                    lambda kind=kind: FactValue.from_payload(kind, {}),
                )

    def test_observation_identity_normalizes_equivalent_timestamps(self) -> None:
        key = route_key()
        first = observation(key)
        second = FactObservation(
            key=key,
            value=route_value(),
            provenance=provenance(key),
            retrieved_at=datetime.fromisoformat(
                "2026-07-28T21:00:00+09:00"
            ),
            valid_until=datetime.fromisoformat(
                "2026-07-29T21:00:00+09:00"
            ),
            purge_at=datetime.fromisoformat(
                "2026-08-27T21:00:00+09:00"
            ),
            confidence=1,
        )
        self.assertEqual(first.observation_id, second.observation_id)

    def test_naive_deadlines_fail_closed(self) -> None:
        key = route_key()
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: FactObservation(
                key=key,
                value=route_value(),
                provenance=provenance(key),
                retrieved_at=NOW.replace(tzinfo=None),
                valid_until=NOW + timedelta(days=1),
                purge_at=NOW + timedelta(days=30),
                confidence=1,
            ),
        )

    def test_provenance_rejects_credential_bearing_uri(self) -> None:
        key = route_key()
        for source_uri in (
            "https://example.test/result?api_key=secret",
            "https://example.test/result?sig=opaque",
            "https://example.test/result?X-Goog-Signature=opaque",
            "https://[::1",
        ):
            with self.subTest(source_uri=source_uri):
                self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda source_uri=source_uri: ProviderProvenance(
                        provider_id="google-routes",
                        adapter_id="google-routes",
                        adapter_version="v1",
                        request_fingerprint=fingerprint_for(key),
                        retention_policy_id="google-routes-runtime-v1",
                        source_uri=source_uri,
                    ),
                )

    def test_provenance_mixed_optional_uris_normalize_without_type_error(
        self,
    ) -> None:
        key = route_key()
        item = ProviderProvenance(
            provider_id="google-routes",
            adapter_id="google-routes",
            adapter_version="v1",
            request_fingerprint=fingerprint_for(key),
            retention_policy_id="google-routes-runtime-v1",
            attributions=(
                ("Provider", "https://example.test/provider"),
                ("Provider", None),
            ),
        )

        self.assertEqual(
            (
                ("Provider", None),
                ("Provider", "https://example.test/provider"),
            ),
            item.attributions,
        )

    def test_provider_problem_rejects_embedded_secret_values(self) -> None:
        key = route_key()
        self.assert_contract_error(
            "SECRET_IN_PROVIDER_RESULT",
            lambda: ProviderProblem(
                code=ProviderProblemCode.AUTH_FAILED,
                message="request failed api_key=do-not-log",
                retryable=False,
                next_action="fix_credentials",
                fact_key_ids=(key.key_id,),
            ),
        )

    def test_generic_empty_success_is_not_a_valid_provider_result(self) -> None:
        key = route_key()
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: provider_result(
                request=request_for(key),
                status=ProviderResultStatus.SUCCESS,
                observations=(),
                problems=(),
            ),
        )

        failed = provider_result(
            request=request_for(key),
            status=ProviderResultStatus.FAILED,
            problems=(problem(key),),
        )
        self.assertEqual(ProviderResultStatus.FAILED, failed.status)

    def test_result_rejects_observation_from_different_request(self) -> None:
        key = route_key()
        item = observation(key)
        other = route_key(origin="loc-x", destination="loc-y")
        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: provider_result(
                request=request_for(other),
                status=ProviderResultStatus.SUCCESS,
                observations=(item,),
            ),
        )

    def test_cache_hit_has_zero_attempts_and_exact_observations(self) -> None:
        key = route_key()
        item = observation(key)
        valid = provider_result(
            request=request_for(key),
            status=ProviderResultStatus.CACHE_HIT,
            observations=(item,),
            attempts=0,
        )
        self.assertEqual(0, valid.attempts_used)

        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: provider_result(
                request=request_for(key),
                status=ProviderResultStatus.CACHE_HIT,
                observations=(item,),
                attempts=1,
            ),
        )

    def test_cache_hit_requires_fresh_retained_single_source(self) -> None:
        key = route_key()
        request = request_for(key)
        stale = observation(
            key,
            request=request,
            valid_until=NOW + timedelta(hours=1),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: provider_result(
                request=request,
                status=ProviderResultStatus.CACHE_HIT,
                observations=(stale,),
                attempts=0,
                completed_at=NOW + timedelta(hours=1),
            ),
        )
        future = observation(
            key,
            request=request,
            retrieved_at=NOW + timedelta(minutes=1),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: provider_result(
                request=request,
                status=ProviderResultStatus.CACHE_HIT,
                observations=(future,),
                attempts=0,
                completed_at=NOW,
            ),
        )
        other_source = observation(
            key,
            provider="other-routes",
            request_fingerprint=request.request_fingerprint,
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: provider_result(
                request=request,
                status=ProviderResultStatus.SUCCESS,
                observations=(observation(key), other_source),
            ),
        )

    def test_success_cannot_promote_stale_content(self) -> None:
        key = route_key()
        request = request_for(key)
        stale = observation(
            key,
            request=request,
            valid_until=NOW + timedelta(hours=1),
        )

        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: provider_result(
                request=request,
                status=ProviderResultStatus.SUCCESS,
                observations=(stale,),
                completed_at=NOW + timedelta(hours=1),
            ),
        )

    def test_outbound_failure_consumes_attempt_but_preflight_need_not(
        self,
    ) -> None:
        key = route_key()
        request = request_for(key)
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: provider_result(
                request=request,
                status=ProviderResultStatus.FAILED,
                problems=(
                    problem(
                        key,
                        code=ProviderProblemCode.TIMEOUT,
                        retryable=False,
                    ),
                ),
                attempts=0,
            ),
        )
        preflight = provider_result(
            request=request,
            status=ProviderResultStatus.FAILED,
            problems=(
                problem(
                    key,
                    code=ProviderProblemCode.UNSUPPORTED_MODE,
                    retryable=False,
                ),
            ),
            attempts=0,
        )

        self.assertEqual(0, preflight.attempts_used)

    def test_provider_request_batch_is_bounded(self) -> None:
        keys = tuple(
            route_key(
                origin=f"loc-{index}",
                destination=f"loc-{index + 1}",
                mode="walking",
                departure_at=None,
            )
            for index in range(257)
        )

        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: request_for(*keys),
        )
        policy = policy_for(
            "google-routes", FactKind.ROUTE_ESTIMATE
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: provider_request_fingerprint(
                provider_id="google-routes",
                adapter_id="google-routes",
                adapter_version="v1",
                operation="compute-route",
                fact_keys=keys,
                policy_id=policy.policy_id,
                policy_digest=policy.policy_digest,
            ),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: request_for(
                route_key(),
                query_scope=tuple(
                    (f"field_{index}", index)
                    for index in range(33)
                ),
            ),
        )

    def test_provider_problem_key_scope_is_bounded(self) -> None:
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: ProviderProblem(
                code=ProviderProblemCode.UNSUPPORTED_MODE,
                message="Unsupported before an outbound request.",
                retryable=False,
                next_action="change_mode",
                fact_key_ids=("0" * 64,) * 257,
            ),
        )

    def test_query_scope_must_be_policy_allowlisted(self) -> None:
        key = route_key()
        request = request_for(
            key,
            query_scope=(("experimental_switch", True),),
        )
        item = observation(key, request=request)
        raw = provider_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(item,),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: authorize_provider_result(request, raw, POLICIES),
        )

        policy = policy_for("google-routes", FactKind.ROUTE_ESTIMATE)
        forbidden_operation = ProviderRequest(
            provider_id="google-routes",
            adapter_id="google-routes",
            adapter_version="v1",
            operation="admin-delete",
            fact_keys=(key,),
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
        )
        forbidden_item = observation(
            key, request=forbidden_operation
        )
        forbidden_raw = provider_result(
            request=forbidden_operation,
            status=ProviderResultStatus.SUCCESS,
            observations=(forbidden_item,),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: authorize_provider_result(
                forbidden_operation, forbidden_raw, POLICIES
            ),
        )

    def test_builtin_google_policy_is_explicit_and_fail_closed(self) -> None:
        policies = google_maps_policy_registry(
            GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
        )
        by_id = {item.policy_id: item for item in policies.policies}

        self.assertEqual(
            EvidencePersistence.INDEFINITE_ID,
            by_id["google-place-id-v1"].persistence,
        )
        for policy_id in (
            "google-place-profile-runtime-v1",
            "google-place-hours-runtime-v1",
            "google-route-runtime-v1",
        ):
            self.assertEqual(
                EvidencePersistence.MEMORY_ONLY,
                by_id[policy_id].persistence,
            )
            self.assertIn(
                "Google Maps",
                by_id[policy_id].required_attribution_labels,
            )
        self.assertFalse(
            any(
                item.persistence is EvidencePersistence.DISK_TTL
                for item in policies.policies
            )
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: google_maps_policy_registry("unknown-region"),
        )

    def test_policy_registry_rejects_overlapping_source_kind_slots(
        self,
    ) -> None:
        first = route_policy("overlap-routes")
        second = ProviderPolicy(
            policy_id="overlap-routes-alternate-v1",
            provider_id="overlap-routes",
            adapter_id="overlap-routes",
            adapter_version="v1",
            contract_region="test",
            allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
            allowed_value_fields=ROUTE_FIELDS,
            allowed_operations=("compute-route-alternate",),
            persistence=EvidencePersistence.DISK_TTL,
            max_validity_seconds=60,
            max_retention_seconds=60,
            required_attribution_labels=("overlap-routes",),
        )

        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: ProviderPolicyRegistry(policies=(first, second)),
        )

    def test_authorization_binds_preimage_keys_not_only_fingerprint(
        self,
    ) -> None:
        requested_key = route_key()
        unrequested_key = route_key(
            origin="loc-x", destination="loc-y"
        )
        request = request_for(requested_key)
        out_of_scope = observation(
            unrequested_key,
            request_fingerprint=request.request_fingerprint,
        )
        raw = provider_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(out_of_scope,),
        )

        self.assert_contract_error(
            "OUT_OF_SCOPE_RESULT",
            lambda: authorize_provider_result(request, raw, POLICIES),
        )

    def test_authorization_rejects_partial_success_failure_overlap(
        self,
    ) -> None:
        first = route_key()
        second = route_key(
            origin="loc-b",
            destination="loc-c",
            mode="walking",
            departure_at=None,
        )
        request = request_for(first, second)
        succeeded = observation(first, request=request)
        raw = provider_result(
            request=request,
            status=ProviderResultStatus.PARTIAL,
            observations=(succeeded,),
            problems=(problem(first), problem(second)),
        )

        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: authorize_provider_result(request, raw, POLICIES),
        )

    def test_unallowlisted_source_and_policy_deadlines_fail_closed(
        self,
    ) -> None:
        key = route_key()
        request = request_for(key)
        untrusted = FactObservation(
            key=key,
            value=route_value(),
            provenance=ProviderProvenance(
                provider_id="research-hint",
                adapter_id="research-hint",
                adapter_version="v1",
                request_fingerprint=request.request_fingerprint,
                retention_policy_id="google-routes-runtime-v1",
                attributions=(("google-routes", None),),
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(days=1),
            purge_at=NOW + timedelta(days=1),
            confidence=1,
        )
        untrusted_raw = provider_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(untrusted,),
        )
        self.assert_contract_error(
            "UNTRUSTED_PROVENANCE",
            lambda: authorize_provider_result(
                request, untrusted_raw, POLICIES
            ),
        )

        invented_long_lived = observation(
            key,
            valid_until=NOW + timedelta(days=3650),
            purge_at=NOW + timedelta(days=3650),
        )
        long_lived_raw = provider_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(invented_long_lived,),
        )
        self.assert_contract_error(
            "UNTRUSTED_PROVENANCE",
            lambda: authorize_provider_result(
                request, long_lived_raw, POLICIES
            ),
        )

    def test_policy_requires_attribution_and_redacts_memory_only_values(
        self,
    ) -> None:
        key = route_key()
        request = request_for(key)
        missing_attribution = FactObservation(
            key=key,
            value=route_value(),
            provenance=ProviderProvenance(
                provider_id="google-routes",
                adapter_id="google-routes",
                adapter_version="v1",
                request_fingerprint=request.request_fingerprint,
                retention_policy_id="google-routes-runtime-v1",
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(days=1),
            purge_at=NOW + timedelta(days=1),
            confidence=1,
        )
        self.assert_contract_error(
            "UNTRUSTED_PROVENANCE",
            lambda: ledger(missing_attribution),
        )

        runtime = observation(key)
        current = ledger(runtime)
        self.assertEqual(
            (), current.durable_observations(purge_now=NOW)
        )
        binding = current.to_dict()["observation_bindings"][0]
        self.assertNotIn("duration_min", repr(current.to_dict()))
        self.assertEqual("memory_only", binding["persistence"])
        self.assertEqual(
            ["google-routes"],
            binding["required_attribution_labels"],
        )
        self.assertTrue(binding["requires_live_attribution"])
        self.assertNotIn("attributions", binding)
        self.assertNotIn("source_uri", binding)

        identity_key = place_identity_key()
        identity_request = request_for(
            identity_key, provider="google-places"
        )
        identity = FactObservation(
            key=identity_key,
            value=FactValue.from_payload(
                FactKind.PLACE_IDENTITY,
                {"provider_place_id": "place-123"},
            ),
            provenance=provenance(
                identity_key,
                provider="google-places",
                request=identity_request,
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(days=365),
            purge_at=None,
            confidence=1,
        )
        self.assertEqual(
            (identity,),
            ledger(identity).durable_observations(purge_now=NOW),
        )

    def test_indefinite_identity_rejects_dynamic_provenance(self) -> None:
        key = place_identity_key()
        request = request_for(key, provider="google-places")
        for provenance_item in (
            ProviderProvenance(
                provider_id="google-places",
                adapter_id="google-places",
                adapter_version="v1",
                request_fingerprint=request.request_fingerprint,
                retention_policy_id="google-place-id-v1",
                source_uri="https://example.test/place/place-123",
                attributions=(("Google Maps", None),),
            ),
            ProviderProvenance(
                provider_id="google-places",
                adapter_id="google-places",
                adapter_version="v1",
                request_fingerprint=request.request_fingerprint,
                retention_policy_id="google-place-id-v1",
                attributions=(
                    ("Google Maps", "https://example.test/attribution"),
                ),
            ),
        ):
            with self.subTest(provenance_item=repr(provenance_item)):
                item = FactObservation(
                    key=key,
                    value=FactValue.from_payload(
                        FactKind.PLACE_IDENTITY,
                        {"provider_place_id": "place-123"},
                    ),
                    provenance=provenance_item,
                    retrieved_at=NOW,
                    valid_until=NOW + timedelta(days=365),
                    purge_at=None,
                    confidence=1,
                )
                raw = provider_result(
                    request=request,
                    status=ProviderResultStatus.SUCCESS,
                    observations=(item,),
                )
                self.assert_contract_error(
                    "UNTRUSTED_PROVENANCE",
                    lambda raw=raw: authorize_provider_result(
                        request, raw, POLICIES
                    ),
                )

    def test_kind_specific_qualifier_typos_are_rejected(self) -> None:
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: FactKey(
                kind=FactKind.ROUTE_ESTIMATE,
                subject_ids=("loc-a", "loc-b"),
                qualifiers=(
                    ("mode", "transit"),
                    ("departue_at", DEPARTURE),
                ),
            ),
        )

    def test_place_values_bind_provider_identity_and_target_coverage(
        self,
    ) -> None:
        profile_key = place_profile_key(provider_place_id="place-a")
        profile_request = request_for(
            profile_key, provider="google-places"
        )
        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: FactObservation(
                key=profile_key,
                value=FactValue.from_payload(
                    FactKind.PLACE_PROFILE,
                    {
                        "provider_place_id": "place-b",
                        "latitude": 35,
                        "longitude": 129,
                        "timezone": "Asia/Seoul",
                    },
                ),
                provenance=provenance(
                    profile_key,
                    provider="google-places",
                    request=profile_request,
                ),
                retrieved_at=NOW,
                valid_until=NOW + timedelta(days=1),
                purge_at=NOW + timedelta(days=1),
                confidence=1,
            ),
        )
        untimed_driving = route_key(
            mode="driving", departure_at=None
        )
        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: FactObservation(
                key=untimed_driving,
                value=route_value(
                    mode="driving",
                    departure_at=DEPARTURE,
                ),
                provenance=provenance(untimed_driving),
                retrieved_at=NOW,
                valid_until=NOW + timedelta(days=1),
                purge_at=NOW + timedelta(days=1),
                confidence=1,
            ),
        )

        hours_key = opening_key()
        hours_request = request_for(
            hours_key, provider="google-places"
        )
        outside = opening_value(
            coverage_start="2027-10-03",
            coverage_end="2027-10-03",
            intervals=[
                {
                    "start_at": "2027-10-03T10:00:00+09:00",
                    "end_at": "2027-10-03T18:00:00+09:00",
                }
            ],
        )
        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: FactObservation(
                key=hours_key,
                value=outside,
                provenance=provenance(
                    hours_key,
                    provider="google-places",
                    request=hours_request,
                ),
                retrieved_at=NOW,
                valid_until=NOW + timedelta(days=1),
                purge_at=NOW + timedelta(days=1),
                confidence=1,
            ),
        )

    def test_opening_hours_require_complete_explicit_local_coverage(
        self,
    ) -> None:
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: opening_value(intervals=[], closed_dates=[]),
        )
        closed = opening_value(
            intervals=[],
            closed_dates=["2026-10-03"],
        )
        self.assertEqual(
            ["2026-10-03"], closed.payload["closed_dates"]
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: opening_value(timezone_name="Mars/Olympus"),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: opening_value(
                intervals=[
                    {
                        "start_at": "2026-10-03T10:00:00+09:00",
                        "end_at": "2026-10-03T14:00:00+09:00",
                    },
                    {
                        "start_at": "2026-10-03T13:00:00+09:00",
                        "end_at": "2026-10-03T18:00:00+09:00",
                    },
                ]
            ),
        )
        key = opening_key()
        request = request_for(key, provider="google-places")
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: FactObservation(
                key=key,
                value=opening_value(),
                provenance=provenance(
                    key,
                    provider="google-places",
                    request=request,
                ),
                retrieved_at=NOW,
                valid_until=NOW + timedelta(hours=12),
                purge_at=NOW + timedelta(hours=12),
                confidence=1,
            ),
        )

    def test_regular_typical_hours_are_verified_but_draft_only(
        self,
    ) -> None:
        key = opening_key(basis="regular_typical")
        request = request_for(key, provider="google-places")
        item = FactObservation(
            key=key,
            value=opening_value(basis="regular_typical"),
            provenance=provenance(
                key,
                provider="google-places",
                request=request,
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(hours=12),
            purge_at=NOW + timedelta(hours=12),
            confidence=1,
        )
        resolved = EvidenceSnapshot.from_ledger(
            ledger(item),
            evaluation_at=NOW,
            purge_now=NOW,
        ).resolve(key)

        self.assertEqual(EvidenceState.VERIFIED, resolved.evidence_state)
        self.assertFalse(resolved.supports_travel_ready_use)

    def test_profile_enums_and_route_timestamps_are_consistent(self) -> None:
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: FactValue.from_payload(
                FactKind.PLACE_PROFILE,
                {
                    "provider_place_id": "place-123",
                    "latitude": 35,
                    "longitude": 129,
                    "business_status": "maybe_open",
                },
            ),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: route_value(
                duration_min=30,
                arrival_at="2026-10-03T03:00:00Z",
            ),
        )
        key = route_key()
        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: FactObservation(
                key=key,
                value=route_value(departure_at=None),
                provenance=provenance(key),
                retrieved_at=NOW,
                valid_until=NOW + timedelta(days=1),
                purge_at=NOW + timedelta(days=1),
                confidence=1,
            ),
        )
        for edge_timestamp in (
            "0001-01-01T00:00:00+14:00",
            "9999-12-31T23:59:59-14:00",
        ):
            with self.subTest(edge_timestamp=edge_timestamp):
                self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda edge_timestamp=edge_timestamp: route_value(
                        departure_at=edge_timestamp
                    ),
                )

    def test_route_timestamp_requires_strict_rfc3339_separator(self) -> None:
        for malformed in (
            "2026-10-03Y01:00:00Z",
            "2026-10-03 01:00:00Z",
            "2026-10-03T01:00:00+0000",
        ):
            with self.subTest(malformed=malformed):
                self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda malformed=malformed: route_value(
                        departure_at=malformed
                    ),
                )

    def test_result_rejects_duplicate_provider_slots(self) -> None:
        key = route_key()
        request = request_for(key)
        first = observation(key, request=request)
        second = observation(
            key,
            request=request,
            retrieved_at=NOW + timedelta(minutes=1),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: provider_result(
                request=request,
                status=ProviderResultStatus.SUCCESS,
                observations=(first, second),
            ),
        )

    def test_all_durable_text_entries_reject_obvious_secrets(self) -> None:
        self.assert_contract_error(
            "SECRET_IN_PROVIDER_REQUEST",
            lambda: route_key(origin="api_key=secret-value"),
        )
        self.assert_contract_error(
            "SECRET_IN_PROVIDER_REQUEST",
            lambda: FactKey(
                kind=FactKind.PLACE_IDENTITY,
                subject_ids=("loc-a",),
                qualifiers=(
                    ("identity_provider", "google-places"),
                    ("apikey", "opaque-value"),
                ),
            ),
        )
        key = route_key()
        for field in ("response_id", "provider_record_id"):
            with self.subTest(field=field):
                values = {
                    "provider_id": "google-routes",
                    "adapter_id": "google-routes",
                    "adapter_version": "v1",
                    "request_fingerprint": fingerprint_for(key),
                    "retention_policy_id": (
                        "google-routes-runtime-v1"
                    ),
                    field: "api_key=secret-value",
                }
                self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda values=values: ProviderProvenance(**values),
                )
        self.assert_contract_error(
            "SECRET_IN_PROVIDER_RESULT",
            lambda: ProviderProblem(
                code=ProviderProblemCode.AUTH_FAILED,
                message="upstream {'api_key': 'secret-value'}",
                retryable=False,
                next_action="fix_credentials",
            ),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: ProviderProvenance(
                provider_id="google-routes",
                adapter_id="google-routes",
                adapter_version="v1",
                request_fingerprint=fingerprint_for(key),
                retention_policy_id="google-routes-runtime-v1",
                attributions=(
                    ("Bearer abcdefghijklmnop", None),
                ),
            ),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: FactValue.from_payload(
                FactKind.PLACE_IDENTITY,
                {"provider_place_id": "api_key=secret-value"},
            ),
        )

    def test_unhashable_enum_values_fail_with_contract_errors(self) -> None:
        invalid_payloads = (
            (
                FactKind.ROUTE_ESTIMATE,
                {"mode": [], "duration_min": 30},
            ),
            (
                FactKind.PLACE_PROFILE,
                {
                    "provider_place_id": "place-123",
                    "latitude": 35,
                    "longitude": 129,
                    "business_status": [],
                },
            ),
            (
                FactKind.PLACE_OPENING_HOURS,
                {
                    "provider_place_id": "place-123",
                    "timezone": "Asia/Seoul",
                    "basis": [],
                    "coverage_start": "2026-10-03",
                    "coverage_end": "2026-10-03",
                    "intervals": [],
                    "closed_dates": ["2026-10-03"],
                },
            ),
            (
                FactKind.ROUTE_ESTIMATE,
                {
                    "mode": "driving",
                    "duration_min": 30,
                    "fallback_from_mode": [],
                },
            ),
        )
        for kind, payload in invalid_payloads:
            with self.subTest(kind=kind, payload=payload):
                self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda kind=kind, payload=payload: FactValue.from_payload(
                        kind, payload
                    ),
                )

    def test_corrupt_json_depth_and_integer_limits_are_typed(self) -> None:
        malformed_payloads = (
            b'{"x":' + b"[" * 10_000 + b"0" + b"]" * 10_000 + b"}",
            b'{"x":' + b"9" * 5_000 + b"}",
        )
        for payload in malformed_payloads:
            with self.subTest(length=len(payload)):
                self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda payload=payload: FactValue(
                        kind=FactKind.ROUTE_ESTIMATE,
                        schema_version="route-estimate/v1",
                        canonical_json=payload,
                    ),
                )

    def test_runtime_provider_content_is_redacted_from_safe_views(self) -> None:
        sentinel = "restricted-provider-sentinel-9f4c"
        key = place_profile_key()
        request = request_for(key, provider="google-places")
        value = FactValue.from_payload(
            FactKind.PLACE_PROFILE,
            {
                "provider_place_id": "place-123",
                "latitude": 35,
                "longitude": 129,
                "display_name": sentinel,
            },
        )
        provenance_item = ProviderProvenance(
            provider_id="google-places",
            adapter_id="google-places",
            adapter_version="v1",
            request_fingerprint=request.request_fingerprint,
            retention_policy_id="google-place-runtime-v1",
            provider_record_id=f"record-{sentinel}",
            response_id=f"response-{sentinel}",
            source_uri=f"https://example.test/{sentinel}",
            attributions=(
                ("Google Maps", f"https://example.test/attr/{sentinel}"),
            ),
        )
        item = FactObservation(
            key=key,
            value=value,
            provenance=provenance_item,
            retrieved_at=NOW,
            valid_until=NOW + timedelta(hours=12),
            purge_at=NOW + timedelta(hours=12),
            confidence=1,
        )
        raw = provider_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(item,),
        )
        authorized = authorize_provider_result(request, raw, POLICIES)
        current = merge_provider_result(
            EvidenceLedger(policies=POLICIES),
            authorized,
            purge_now=NOW,
        ).ledger
        snapshot = EvidenceSnapshot.from_ledger(
            current,
            evaluation_at=NOW,
            purge_now=NOW,
        )
        resolution = snapshot.resolve(key)
        scoped_request = request_for(
            route_key(),
            query_scope=(("units", sentinel),),
        )
        safe_views = (
            repr(value),
            repr(provenance_item),
            repr(item),
            repr(raw),
            repr(authorized),
            repr(current),
            repr(snapshot),
            repr(resolution),
            repr(scoped_request),
            repr(value.to_dict()),
            repr(provenance_item.to_dict()),
            repr(item.to_dict()),
            repr(raw.to_dict()),
            repr(authorized.to_binding_dict()),
            repr(current.to_dict()),
            repr(snapshot.to_dict()),
            repr(scoped_request.to_binding_dict()),
        )

        for safe_view in safe_views:
            self.assertNotIn(sentinel, safe_view)

    def test_numeric_contract_is_typed_and_canonical(self) -> None:
        zero = route_value(duration_min=0.0)
        negative_zero = route_value(duration_min=-0.0)
        self.assertEqual(zero.value_digest, negative_zero.value_digest)
        for value in (True, float("nan"), float("inf"), 10**1000):
            with self.subTest(value=repr(value)[:40]):
                self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda value=value: route_value(duration_min=value),
                )
        key = route_key()
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: FactObservation(
                key=key,
                value=route_value(),
                provenance=provenance(key),
                retrieved_at=NOW,
                valid_until=NOW + timedelta(days=1),
                purge_at=NOW + timedelta(days=1),
                confidence=10**1000,
            ),
        )


class EvidenceMergeTests(unittest.TestCase):
    def test_ledger_rejects_direct_seed_forged_revision_and_bool_generation(
        self,
    ) -> None:
        key = route_key()
        first = observation(key)
        second = observation(
            key,
            retrieved_at=NOW + timedelta(minutes=1),
        )
        for constructor in (
            lambda: EvidenceLedger(
                policies=POLICIES,
                observations=(first, second),
            ),
            lambda: EvidenceLedger(
                policies=POLICIES,
                revision="0" * 64,
            ),
            lambda: EvidenceLedger(
                policies=POLICIES,
                generation=True,
            ),
            lambda: EvidenceLedger(
                policies=POLICIES,
                generation=2**63,
            ),
        ):
            with self.subTest(constructor=constructor):
                with self.assertRaises(FactContractError):
                    constructor()

    def test_failed_refresh_preserves_last_known_good_and_revision(self) -> None:
        key = route_key()
        request = request_for(key)
        current = observation(key)
        current_ledger = ledger(current)
        failed = authorized_result(
            request=request,
            status=ProviderResultStatus.FAILED,
            problems=(problem(key),),
            completed_at=NOW + timedelta(hours=1),
        )

        merged = merge_provider_result(
            current_ledger,
            failed,
            purge_now=NOW + timedelta(hours=1),
        )

        self.assertFalse(merged.changed)
        self.assertIs(current_ledger, merged.ledger)
        self.assertEqual((current,), merged.ledger.observations)

    def test_partial_result_updates_success_and_preserves_failed_slot(
        self,
    ) -> None:
        first_key = route_key()
        second_key = route_key(
            origin="loc-b",
            destination="loc-c",
            mode="walking",
            departure_at=None,
        )
        batch_request = request_for(first_key, second_key)
        first_old = observation(
            first_key,
            request=batch_request,
        )
        second_old = observation(
            second_key,
            request=batch_request,
        )
        first_new = observation(
            first_key,
            request=batch_request,
            retrieved_at=NOW + timedelta(hours=1),
            duration_min=25,
        )
        current_ledger = ledger(first_old, second_old)
        partial = authorized_result(
            request=batch_request,
            status=ProviderResultStatus.PARTIAL,
            observations=(first_new,),
            problems=(problem(second_key),),
            completed_at=NOW + timedelta(hours=1),
        )

        merged = merge_provider_result(
            current_ledger,
            partial,
            purge_now=NOW + timedelta(hours=1),
        )
        by_key = {
            item.key.key_id: item for item in merged.ledger.observations
        }

        self.assertTrue(merged.changed)
        self.assertEqual(first_new, by_key[first_key.key_id])
        self.assertEqual(second_old, by_key[second_key.key_id])
        self.assertEqual(
            (first_new.observation_id,),
            merged.promoted_observation_ids,
        )

    def test_older_same_provider_result_cannot_replace_lkg(self) -> None:
        key = route_key()
        request = request_for(key)
        current = observation(
            key,
            retrieved_at=NOW + timedelta(hours=2),
            duration_min=20,
        )
        older = observation(
            key,
            retrieved_at=NOW + timedelta(hours=1),
            duration_min=40,
        )
        result = authorized_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(older,),
            completed_at=NOW + timedelta(hours=2),
        )

        merged = merge_provider_result(
            ledger(current),
            result,
            purge_now=NOW + timedelta(hours=2),
        )

        self.assertFalse(merged.changed)
        self.assertEqual((current,), merged.ledger.observations)
        self.assertIn(
            ProviderProblemCode.STALE_PROVIDER_RESULT,
            {item.code for item in merged.problems},
        )

    def test_same_provider_time_with_different_value_is_rejected(self) -> None:
        key = route_key()
        request = request_for(key)
        current = observation(key, duration_min=20)
        conflicting = observation(key, duration_min=40)
        result = authorized_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(conflicting,),
        )

        with self.assertRaises(FactContractError) as caught:
            merge_provider_result(
                ledger(current),
                result,
                purge_now=NOW,
            )
        self.assertEqual(
            "AMBIGUOUS_PROVIDER_RESULT",
            caught.exception.code,
        )

    def test_exact_replay_is_idempotent(self) -> None:
        key = route_key()
        request = request_for(key)
        current = observation(key)
        result = authorized_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(current,),
        )
        current_ledger = ledger(current)
        before_generation = current_ledger.generation

        merged = merge_provider_result(
            current_ledger, result, purge_now=NOW
        )

        self.assertFalse(merged.changed)
        self.assertEqual(
            before_generation, merged.ledger.generation
        )
        self.assertEqual(
            current_ledger.revision, merged.ledger.revision
        )

    def test_cache_hit_cannot_seed_missing_lkg(self) -> None:
        key = route_key()
        request = request_for(key)
        current = observation(key)
        result = authorized_result(
            request=request,
            status=ProviderResultStatus.CACHE_HIT,
            observations=(current,),
            attempts=0,
        )

        with self.assertRaises(FactContractError) as caught:
            merge_provider_result(
                ledger(),
                result,
                purge_now=NOW,
            )
        self.assertEqual(
            "INVALID_PROVIDER_RESPONSE",
            caught.exception.code,
        )

    def test_cache_hit_cannot_replace_lkg_and_purge_still_wins(self) -> None:
        key = route_key()
        request = request_for(key)
        current = observation(
            key,
            request=request,
            purge_at=NOW + timedelta(hours=2),
        )
        different = observation(
            key,
            request=request,
            retrieved_at=NOW + timedelta(hours=1),
            purge_at=NOW + timedelta(hours=2),
            duration_min=31,
        )
        mismatch = authorized_result(
            request=request,
            status=ProviderResultStatus.CACHE_HIT,
            observations=(different,),
            attempts=0,
            completed_at=NOW + timedelta(hours=1),
        )
        with self.assertRaises(FactContractError) as caught:
            merge_provider_result(
                ledger(current),
                mismatch,
                purge_now=NOW + timedelta(hours=1),
            )
        self.assertEqual(
            "INVALID_PROVIDER_RESPONSE", caught.exception.code
        )

        exact_hit = authorized_result(
            request=request,
            status=ProviderResultStatus.CACHE_HIT,
            observations=(current,),
            attempts=0,
            completed_at=NOW,
        )
        purged = merge_provider_result(
            ledger(current),
            exact_hit,
            purge_now=NOW + timedelta(hours=2),
        )
        self.assertTrue(purged.changed)
        self.assertEqual((), purged.ledger.observations)

    def test_purge_runs_even_when_refresh_fails(self) -> None:
        key = route_key()
        expired = observation(
            key,
            valid_until=NOW + timedelta(days=20),
            purge_at=NOW + timedelta(days=1),
        )
        failed = authorized_result(
            request=request_for(key),
            status=ProviderResultStatus.FAILED,
            problems=(problem(key),),
            completed_at=NOW + timedelta(days=2),
        )

        merged = merge_provider_result(
            ledger(expired),
            failed,
            purge_now=NOW + timedelta(days=2),
        )

        self.assertTrue(merged.changed)
        self.assertEqual((), merged.ledger.observations)
        self.assertEqual(
            (expired.observation_id,),
            merged.purged_observation_ids,
        )

    def test_incoming_content_already_at_retention_deadline_is_ignored(
        self,
    ) -> None:
        key = route_key()
        request = request_for(key)
        expired = observation(
            key,
            purge_at=NOW + timedelta(hours=1),
        )
        result = authorized_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(expired,),
            completed_at=NOW + timedelta(minutes=30),
        )

        merged = merge_provider_result(
            ledger(),
            result,
            purge_now=NOW + timedelta(hours=1),
        )

        self.assertFalse(merged.changed)
        self.assertEqual((), merged.ledger.observations)
        self.assertIn(
            ProviderProblemCode.RETENTION_EXPIRED,
            {item.code for item in merged.problems},
        )

    def test_merge_reauthorizes_even_a_privately_forged_wrapper(self) -> None:
        requested_key = route_key()
        out_of_scope_key = route_key(
            origin="loc-x",
            destination="loc-y",
        )
        request = request_for(requested_key)
        out_of_scope = observation(
            out_of_scope_key,
            request_fingerprint=request.request_fingerprint,
        )
        raw = provider_result(
            request=request,
            status=ProviderResultStatus.SUCCESS,
            observations=(out_of_scope,),
        )
        forged = AuthorizedProviderResult(
            request=request,
            result=raw,
            policy_registry_revision=POLICIES.revision,
            authorization_id="0" * 64,
            _token=facts_module._AUTHORIZATION_TOKEN,
        )

        with self.assertRaises(FactContractError) as caught:
            merge_provider_result(
                EvidenceLedger(policies=POLICIES),
                forged,
                purge_now=NOW,
            )
        self.assertEqual("OUT_OF_SCOPE_RESULT", caught.exception.code)

    def test_disk_ttl_durable_view_filters_at_purge_deadline(self) -> None:
        policy = ProviderPolicy(
            policy_id="disk-routes-v1",
            provider_id="disk-routes",
            adapter_id="disk-routes",
            adapter_version="v1",
            contract_region="test",
            allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
            allowed_value_fields=ROUTE_FIELDS,
            allowed_operations=("compute-route",),
            persistence=EvidencePersistence.DISK_TTL,
            max_validity_seconds=60 * 60,
            max_retention_seconds=60 * 60,
            required_attribution_labels=("Disk Routes",),
        )
        policies = ProviderPolicyRegistry(policies=(policy,))
        key = route_key()
        request = ProviderRequest(
            provider_id="disk-routes",
            adapter_id="disk-routes",
            adapter_version="v1",
            operation="compute-route",
            fact_keys=(key,),
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
        )
        item = FactObservation(
            key=key,
            value=route_value(),
            provenance=ProviderProvenance(
                provider_id="disk-routes",
                adapter_id="disk-routes",
                adapter_version="v1",
                request_fingerprint=request.request_fingerprint,
                retention_policy_id=policy.policy_id,
                attributions=(("Disk Routes", None),),
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(hours=1),
            purge_at=NOW + timedelta(hours=1),
            confidence=1,
        )
        raw = ProviderResult(
            request_fingerprint=request.request_fingerprint,
            status=ProviderResultStatus.SUCCESS,
            observations=(item,),
            problems=(),
            attempts_used=1,
            completed_at=NOW,
        )
        authorized = authorize_provider_result(request, raw, policies)
        current = merge_provider_result(
            EvidenceLedger(policies=policies),
            authorized,
            purge_now=NOW,
        ).ledger

        self.assertEqual(
            (item,), current.durable_observations(purge_now=NOW)
        )
        self.assertEqual(
            (),
            current.durable_observations(
                purge_now=NOW + timedelta(hours=1)
            ),
        )


class EvidenceSnapshotTests(unittest.TestCase):
    def test_fresh_observation_resolves_verified(self) -> None:
        key = route_key()
        item = observation(key)
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(item),
            evaluation_at=NOW + timedelta(hours=1),
            purge_now=NOW + timedelta(hours=1),
        )

        resolved = snapshot.resolve(key)

        self.assertEqual(EvidenceState.VERIFIED, resolved.evidence_state)
        self.assertEqual(ResolutionReason.FRESH, resolved.reason)
        self.assertEqual(item, resolved.selected)
        self.assertEqual(
            (f"fact:{item.observation_id}",),
            resolved.evidence_refs,
        )

    def test_semantically_expired_but_retained_lkg_is_stale(self) -> None:
        key = route_key()
        item = observation(
            key,
            valid_until=NOW + timedelta(hours=1),
            purge_at=NOW + timedelta(days=30),
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(item),
            evaluation_at=NOW + timedelta(hours=1),
            purge_now=NOW + timedelta(hours=1),
        )

        resolved = snapshot.resolve(key)

        self.assertEqual(EvidenceState.STALE, resolved.evidence_state)
        self.assertEqual(ResolutionReason.STALE, resolved.reason)

    def test_fresh_material_disagreement_is_conflicted_without_winner(
        self,
    ) -> None:
        key = route_key()
        first = observation(key, provider="provider-a", duration_min=20)
        second = observation(key, provider="provider-b", duration_min=40)
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(first, second),
            evaluation_at=NOW,
            purge_now=NOW,
        )

        resolved = snapshot.resolve(key)

        self.assertEqual(
            EvidenceState.CONFLICTED,
            resolved.evidence_state,
        )
        self.assertEqual(ResolutionReason.CONFLICTED, resolved.reason)
        self.assertIsNone(resolved.selected)
        self.assertEqual(2, len(resolved.candidates))

    def test_matching_sources_resolve_verified_deterministically(self) -> None:
        key = route_key()
        first = observation(key, provider="provider-a", duration_min=30)
        second = observation(
            key,
            provider="provider-b",
            retrieved_at=NOW + timedelta(minutes=5),
            duration_min=30,
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(first, second),
            evaluation_at=NOW + timedelta(minutes=5),
            purge_now=NOW + timedelta(minutes=5),
        )

        resolved = snapshot.resolve(key)

        self.assertEqual(EvidenceState.VERIFIED, resolved.evidence_state)
        self.assertEqual(second, resolved.selected)

    def test_fresh_source_supersedes_disagreeing_stale_source_for_resolution(
        self,
    ) -> None:
        key = route_key()
        stale = observation(
            key,
            provider="provider-a",
            valid_until=NOW + timedelta(hours=1),
            duration_min=20,
        )
        fresh = observation(
            key,
            provider="provider-b",
            retrieved_at=NOW + timedelta(hours=2),
            valid_until=NOW + timedelta(days=2),
            duration_min=40,
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(stale, fresh),
            evaluation_at=NOW + timedelta(hours=3),
            purge_now=NOW + timedelta(hours=3),
        )

        resolved = snapshot.resolve(key)

        self.assertEqual(EvidenceState.VERIFIED, resolved.evidence_state)
        self.assertEqual(fresh, resolved.selected)
        self.assertEqual({fresh, stale}, set(resolved.candidates))

    def test_all_stale_disagreement_remains_conflicted(self) -> None:
        key = route_key()
        first = observation(
            key,
            provider="provider-a",
            valid_until=NOW + timedelta(hours=1),
            duration_min=20,
        )
        second = observation(
            key,
            provider="provider-b",
            valid_until=NOW + timedelta(hours=1),
            duration_min=40,
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(first, second),
            evaluation_at=NOW + timedelta(hours=2),
            purge_now=NOW + timedelta(hours=2),
        )

        resolved = snapshot.resolve(key)

        self.assertEqual(
            EvidenceState.CONFLICTED,
            resolved.evidence_state,
        )

    def test_observation_retrieved_after_evaluation_time_is_invisible(
        self,
    ) -> None:
        key = route_key()
        future = observation(
            key,
            retrieved_at=NOW + timedelta(hours=1),
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(future),
            evaluation_at=NOW,
            purge_now=NOW + timedelta(hours=1),
        )

        resolved = snapshot.resolve(key)

        self.assertEqual(EvidenceState.UNVERIFIED, resolved.evidence_state)
        self.assertEqual(ResolutionReason.MISSING, resolved.reason)

    def test_snapshot_rejects_observation_retrieved_after_purge_check(
        self,
    ) -> None:
        key = route_key()
        future = observation(
            key,
            retrieved_at=NOW + timedelta(hours=1),
        )
        with self.assertRaises(FactContractError) as caught:
            EvidenceSnapshot.from_ledger(
                ledger(future),
                evaluation_at=NOW + timedelta(hours=2),
                purge_now=NOW,
            )
        self.assertEqual("CACHE_CORRUPTED", caught.exception.code)

    def test_past_evaluation_cannot_resurrect_content_purged_now(self) -> None:
        key = route_key()
        item = observation(
            key,
            valid_until=NOW + timedelta(days=20),
            purge_at=NOW + timedelta(days=1),
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(item),
            evaluation_at=NOW,
            purge_now=NOW + timedelta(days=2),
        )

        self.assertEqual((), snapshot.observations)
        self.assertEqual(
            EvidenceState.UNVERIFIED,
            snapshot.resolve(key).evidence_state,
        )

    def test_future_evaluation_hides_but_does_not_purge_retained_content(
        self,
    ) -> None:
        key = route_key()
        item = observation(
            key,
            valid_until=NOW + timedelta(days=1),
            purge_at=NOW + timedelta(days=30),
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(item),
            evaluation_at=NOW + timedelta(days=365),
            purge_now=NOW + timedelta(days=2),
        )

        self.assertEqual((item,), snapshot.observations)
        self.assertEqual(
            EvidenceState.UNVERIFIED,
            snapshot.resolve(key).evidence_state,
        )
        self.assertEqual(
            ResolutionReason.MISSING,
            snapshot.resolve(key).reason,
        )

    def test_evaluation_at_exact_purge_deadline_is_missing(self) -> None:
        key = route_key()
        deadline = NOW + timedelta(hours=1)
        item = observation(
            key,
            valid_until=NOW + timedelta(hours=2),
            purge_at=deadline,
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger(item),
            evaluation_at=deadline,
            purge_now=NOW,
        )

        self.assertEqual((item,), snapshot.observations)
        resolved = snapshot.resolve(key)
        self.assertEqual(EvidenceState.UNVERIFIED, resolved.evidence_state)
        self.assertEqual(ResolutionReason.MISSING, resolved.reason)

    def test_snapshot_identity_binds_semantic_evaluation_time(self) -> None:
        key = route_key()
        item = observation(key)
        current_ledger = ledger(item)
        first = EvidenceSnapshot.from_ledger(
            current_ledger,
            evaluation_at=NOW,
            purge_now=NOW,
        )
        second = EvidenceSnapshot.from_ledger(
            current_ledger,
            evaluation_at=NOW + timedelta(minutes=1),
            purge_now=NOW,
        )

        self.assertEqual(
            first.evidence_revision,
            second.evidence_revision,
        )
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)

        different_purge_check = EvidenceSnapshot.from_ledger(
            current_ledger,
            evaluation_at=NOW,
            purge_now=NOW + timedelta(seconds=1),
        )
        self.assertNotEqual(
            first.snapshot_id, different_purge_check.snapshot_id
        )

    def test_resolution_constructor_is_not_a_public_authority_gate(self) -> None:
        key = route_key()
        stale = observation(
            key,
            valid_until=NOW + timedelta(hours=1),
        )
        with self.assertRaises(FactContractError) as caught:
            FactResolution(
                key=key,
                evidence_state=EvidenceState.VERIFIED,
                reason=ResolutionReason.FRESH,
                selected=stale,
                candidates=(stale,),
                evaluation_at=NOW + timedelta(hours=2),
                purge_checked_at=NOW + timedelta(hours=2),
                snapshot_id="0" * 64,
            )
        self.assertEqual("UNTRUSTED_PROVENANCE", caught.exception.code)

    def test_snapshot_constructor_cannot_bypass_trusted_ledger(self) -> None:
        key = route_key()
        item = observation(
            key,
            purge_at=NOW + timedelta(hours=1),
        )
        with self.assertRaises(FactContractError) as caught:
            EvidenceSnapshot(
                policies=POLICIES,
                observations=(item,),
                evaluation_at=NOW,
                purge_checked_at=NOW + timedelta(hours=1),
                store_revision=ledger(item).revision,
            )
        self.assertEqual("UNTRUSTED_PROVENANCE", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
