#!/usr/bin/env python3
"""Offline Phase 6.3B acceptance for the Busan/Hokkaido goldens.

The walkthrough reuses the exact Phase 5.33 canned semantic fixtures and only
creates temporary canonical stores, durable evidence caches, and private
delivery targets.
It evaluates canned provider-shaped identity responses and promotes synthetic
route cache results through the public typed fact gates; it performs no
network request, credential read, repository-trip read/write, browser action,
calendar import, public-source creation, or deployment.
"""

from __future__ import annotations

import json
import stat
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.phase533_acceptance import (  # noqa: E402
    EVALUATION_AT,
    _busan_plan,
    _execute_case,
    _fixed_times,
    _hokkaido_plan,
    _lodging_intake,
)
from trip_planner import compose_trip_state  # noqa: E402
from trip_planner.evidence_session import EvidenceSession  # noqa: E402
from trip_planner.evidence_store import (  # noqa: E402
    EvidenceStore,
    EvidenceStoreResult,
)
from trip_planner.facts import (  # noqa: E402
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    EvidencePersistence,
    EvidenceSnapshot,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderProvenance,
    ProviderRequest,
    ProviderResult,
    ProviderResultStatus,
    authorize_provider_result,
    google_maps_policy_registry,
)
from trip_planner.models import CheckStatus, IssueSeverity  # noqa: E402
from trip_planner.places_identity import (  # noqa: E402
    PlaceIdentityIntent,
    PlaceIdentityReviewAuthority,
    build_google_place_identity_request,
    evaluate_google_place_identity_candidates,
    finalize_google_place_identity_review,
)
from trip_planner.private_delivery import (  # noqa: E402
    PRIVATE_DELIVERY_MANIFEST_VERSION,
    PrivateDeliveryProfile,
)
from trip_planner.private_delivery_writer import (  # noqa: E402
    PrivateDeliveryWriteError,
    PrivateDeliveryWriteOutcomeKind,
    PrivateDeliveryWriteResponseKind,
    capture_private_delivery_write_response,
    execute_private_delivery_write_response,
    prepare_private_delivery_write_review,
)
from trip_planner.readiness import (  # noqa: E402
    READINESS_VERSION,
    ReadinessStatus,
    assess_trip_readiness,
)
from trip_planner.store import TripStore  # noqa: E402
from trip_planner.timeline import evaluate_composed_timeline  # noqa: E402


ACCEPTANCE_VERSION = "phase63b-product-acceptance/v1"
PHASE63B_EVALUATION_AT = EVALUATION_AT + timedelta(minutes=1)
_ROUTE_POLICY_ID = "phase63b-canned-route-v1"
_ROUTE_PROVIDER_ID = "phase63b-canned-routes"
_ROUTE_ATTRIBUTION = "Phase 6.3B canned route"
_PRIVATE_PLACE_PREFIX = "ChIJ-phase63b-private-place-sentinel"
_PRIVATE_NAME_PREFIX = "Phase 6.3B private stay sentinel"
_PRIVATE_ADDRESS_PREFIX = "Phase 6.3B private address sentinel"


class _FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def __call__(self) -> datetime:
        return self._value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _policy_registry() -> ProviderPolicyRegistry:
    google = google_maps_policy_registry(
        GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
    )
    canned_route = ProviderPolicy(
        policy_id=_ROUTE_POLICY_ID,
        provider_id=_ROUTE_PROVIDER_ID,
        adapter_id=_ROUTE_PROVIDER_ID,
        adapter_version="v1",
        contract_region="synthetic",
        allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
        allowed_value_fields=("duration_min", "mode"),
        allowed_operations=("compute-route",),
        persistence=EvidencePersistence.MEMORY_ONLY,
        max_validity_seconds=60 * 60,
        max_retention_seconds=24 * 60 * 60,
        allowed_query_fields=(),
        required_attribution_labels=(_ROUTE_ATTRIBUTION,),
    )
    return ProviderPolicyRegistry((*google.policies, canned_route))


def _durable_snapshot(result: EvidenceStoreResult) -> EvidenceSnapshot:
    _require(
        result.success
        and result.current_revision is not None
        and result.ledger is not None,
        "canned durable evidence store is unavailable",
    )
    return result.snapshot(evaluation_at=PHASE63B_EVALUATION_AT)


def _address_component(
    *,
    long_text: str,
    short_text: str,
    component_type: str,
) -> dict[str, object]:
    return {
        "longText": long_text,
        "shortText": short_text,
        "types": [component_type, "political"],
        "languageCode": "en",
    }


def _promote_lodging_identities(
    evidence_store: EvidenceStore,
    canonical_plan: dict[str, Any],
    *,
    case_name: str,
    region_code: str,
    locality: str,
) -> tuple[EvidenceSnapshot, int]:
    locations = tuple(
        dict.fromkeys(
            lodging["location_id"]
            for lodging in canonical_plan["state"]["trip"]["lodgings"]
        )
    )
    current_result = evidence_store.load()
    for index, location_id in enumerate(locations):
        current = _durable_snapshot(current_result)
        expected_name = f"{_PRIVATE_NAME_PREFIX} {case_name} {index + 1}"
        intent = PlaceIdentityIntent(
            location_id=location_id,
            text_query=f"{expected_name} {locality}",
            expected_name=expected_name,
            region_code=region_code,
            language_code="en",
            expected_locality=locality,
        )
        request = build_google_place_identity_request(intent, current)
        review = evaluate_google_place_identity_candidates(
            request,
            {
                "places": [
                    {
                        "id": (
                            f"{_PRIVATE_PLACE_PREFIX}-{case_name}-{index}"
                        ),
                        "displayName": {
                            "text": expected_name,
                            "languageCode": "en",
                        },
                        "formattedAddress": (
                            f"{_PRIVATE_ADDRESS_PREFIX} {case_name} {index}"
                        ),
                        "location": {
                            "latitude": 35.0 + index,
                            "longitude": 129.0 + index,
                        },
                        "primaryType": "lodging",
                        "types": ["lodging"],
                        "addressComponents": [
                            _address_component(
                                long_text=locality,
                                short_text=locality,
                                component_type="locality",
                            ),
                            _address_component(
                                long_text=region_code,
                                short_text=region_code,
                                component_type="country",
                            ),
                        ],
                    }
                ]
            },
            completed_at=PHASE63B_EVALUATION_AT,
            attempts_used=1,
        )
        authorized = finalize_google_place_identity_review(
            review,
            current,
            PlaceIdentityReviewAuthority(
                reviewer_id="phase63b-canned-host",
                clock=lambda: PHASE63B_EVALUATION_AT,
            ),
        )
        current_result = evidence_store.merge(
            authorized,
            expected_revision=current_result.current_revision,
        )
        _require(
            current_result.success
            and current_result.changed
            and current_result.promoted_observation_ids
            == (authorized.result.observations[0].observation_id,),
            "canned identity evidence was not durably CAS-promoted",
        )
    return _durable_snapshot(current_result), len(locations)


def _original_route_specs(
    canonical_plan: dict[str, Any],
) -> tuple[tuple[str, str, str, str, float], ...]:
    specs: list[tuple[str, str, str, str, float]] = []
    for day in canonical_plan["state"]["itinerary"]["days"]:
        locations = {
            place["activity_id"]: place["location_id"]
            for place in day["places"]
        }
        for edge in day["travel"]:
            mode = edge["recommended_mode"]
            specs.append(
                (
                    day["day_id"],
                    locations[edge["from_activity_id"]],
                    locations[edge["to_activity_id"]],
                    mode,
                    float(edge["modes"][mode]["duration_min"]),
                )
            )
    return tuple(specs)


def _missing_route_specs(
    canonical_plan: dict[str, Any],
    snapshot: EvidenceSnapshot,
    original_specs: tuple[tuple[str, str, str, str, float], ...],
) -> tuple[tuple[str, str, str, float], ...]:
    composed = compose_trip_state(canonical_plan, snapshot)
    report = evaluate_composed_timeline(
        composed,
        now=PHASE63B_EVALUATION_AT,
    )
    _require(
        report.status is CheckStatus.NEEDS_VERIFICATION,
        "post-apply canned plan did not require route refresh",
    )
    missing: dict[tuple[str, str, str], float] = {}
    for issue in report.issues:
        _require(
            issue.severity is not IssueSeverity.ERROR,
            "post-apply canned plan became infeasible",
        )
        _require(
            issue.code == "MISSING_TRAVEL_ESTIMATE",
            "post-apply canned plan has an unexpected warning",
        )
        details = dict(issue.details)
        matches = tuple(
            item
            for item in original_specs
            if item[0] == details.get("day_id")
            and item[1] == details.get("from_location_id")
            and item[2] == details.get("to_location_id")
        )
        _require(
            len(matches) == 1,
            "missing route did not match one original semantic edge",
        )
        _day_id, origin, destination, mode, duration = matches[0]
        key = (origin, destination, mode)
        previous = missing.setdefault(key, duration)
        _require(
            previous == duration,
            "duplicate semantic route has conflicting duration",
        )
    _require(bool(missing), "post-apply canned plan had no route gap")
    return tuple(
        (*key, missing[key])
        for key in sorted(missing)
    )


def _promote_canned_routes(
    session: EvidenceSession,
    policies: ProviderPolicyRegistry,
    specs: tuple[tuple[str, str, str, float], ...],
    *,
    case_name: str,
) -> EvidenceSnapshot:
    policy = policies.policy(_ROUTE_POLICY_ID)
    current = None
    for index, (origin, destination, mode, duration) in enumerate(specs):
        key = FactKey(
            kind=FactKind.ROUTE_ESTIMATE,
            subject_ids=(origin, destination),
            qualifiers=(("mode", mode),),
        )
        request = ProviderRequest(
            provider_id=policy.provider_id,
            adapter_id=policy.adapter_id,
            adapter_version=policy.adapter_version,
            operation="compute-route",
            fact_keys=(key,),
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
        )
        observation = FactObservation(
            key=key,
            value=FactValue.from_payload(
                FactKind.ROUTE_ESTIMATE,
                {"mode": mode, "duration_min": duration},
            ),
            provenance=ProviderProvenance(
                provider_id=policy.provider_id,
                adapter_id=policy.adapter_id,
                adapter_version=policy.adapter_version,
                request_fingerprint=request.request_fingerprint,
                retention_policy_id=policy.policy_id,
                response_id=f"phase63b-{case_name}-canned-route-{index}",
                source_uri=None,
                attributions=((_ROUTE_ATTRIBUTION, None),),
            ),
            retrieved_at=PHASE63B_EVALUATION_AT,
            valid_until=PHASE63B_EVALUATION_AT + timedelta(hours=1),
            purge_at=PHASE63B_EVALUATION_AT + timedelta(hours=24),
            confidence=1.0,
        )
        result = ProviderResult(
            request_fingerprint=request.request_fingerprint,
            status=ProviderResultStatus.SUCCESS,
            observations=(observation,),
            problems=(),
            attempts_used=1,
            completed_at=PHASE63B_EVALUATION_AT,
        )
        merged = session.merge(
            authorize_provider_result(request, result, policies)
        )
        _require(
            merged.changed
            and merged.promoted_observation_ids
            == (observation.observation_id,),
            "canned route evidence was not session-promoted",
        )
        current = merged.current
    _require(current is not None, "canned route evidence was empty")
    return current.snapshot(evaluation_at=PHASE63B_EVALUATION_AT)


def _lodging_evidence_remains_unverified(
    canonical_plan: dict[str, Any],
) -> bool:
    lodgings = canonical_plan["state"]["trip"]["lodgings"]
    return bool(lodgings) and all(
        item["evidence_state"] == "unverified" for item in lodgings
    )


def _verify_exact_private_tree(review, target: Path) -> bool:
    if stat.S_IMODE(target.stat().st_mode) != 0o700:
        return False
    expected = {
        item.filename: item
        for item in review.to_ephemeral_private_artifacts()
    }
    if {item.name for item in target.iterdir()} != set(expected):
        return False
    for filename, artifact in expected.items():
        path = target / filename
        if (
            stat.S_IMODE(path.stat().st_mode) != 0o600
            or path.read_bytes() != artifact.payload
        ):
            return False
    return True


def _phase533_semantic_invariants(
    canonical_plan: dict[str, Any],
    phase533: dict[str, Any],
    *,
    case_name: str,
    first_day: date,
) -> dict[str, bool]:
    days = canonical_plan["state"]["itinerary"]["days"]
    common = {
        "fixed_activity_times_preserved": phase533[
            "fixed_activity_times_preserved"
        ],
        "schedule_change_applied": phase533["schedule_change_applied"],
        "single_canonical_receipt": phase533["receipt_count"] == 1,
    }
    if case_name == "busan":
        lodging = canonical_plan["state"]["trip"]["lodgings"][0]
        day_1 = {
            item["activity_id"]: item for item in days[0]["places"]
        }
        day_2 = {
            item["activity_id"]: item for item in days[1]["places"]
        }
        arrival = day_1["busan-booked-arrival"]
        dinner = day_2["busan-booked-dinner"]
        migration = canonical_plan["state"]["trip"]["_trip_planner"][
            "migration"
        ]
        return {
            **common,
            "daily_lodging_anchors_preserved": all(
                day.get("end_lodging_id") == lodging["lodging_id"]
                and (
                    index == 0
                    or day.get("start_lodging_id")
                    == lodging["lodging_id"]
                )
                for index, day in enumerate(days)
            ),
            "arrival_boundary_preserved": (
                days[0]["date"] == first_day.isoformat()
                and arrival["time"] == "10:00"
                and arrival["location_id"]
                == days[0]["start_location_id"]
            ),
            "dinner_boundary_preserved": (
                days[1]["date"]
                == (first_day + timedelta(days=1)).isoformat()
                and dinner["time"] == "18:00"
            ),
            "semantic_locations_distinct": len(
                {
                    days[0]["start_location_id"],
                    lodging["location_id"],
                    dinner["location_id"],
                }
            )
            == 3,
            "migration_protection_preserved": migration[
                "protected_activity_ids"
            ]
            == ["schedule-alpha", "schedule-beta"],
            "approval_resume_confirmed": phase533[
                "approval_resume_confirmed"
            ],
        }
    if case_name == "hokkaido":
        lodgings = canonical_plan["state"]["trip"]["lodgings"]
        day_2 = days[1]
        places = {
            item["activity_id"]: item for item in day_2["places"]
        }
        driving = day_2["travel"][0]["modes"]["driving"]
        return {
            **common,
            "split_stays_contiguous": (
                len(lodgings) == 2
                and lodgings[0]["check_in"] < lodgings[0]["check_out"]
                == lodgings[1]["check_in"]
                < lodgings[1]["check_out"]
            ),
            "split_stay_anchor_preserved": (
                day_2["start_lodging_id"] == lodgings[0]["lodging_id"]
                and day_2["end_lodging_id"]
                == lodgings[1]["lodging_id"]
                and day_2["start_location_id"]
                == lodgings[0]["location_id"]
                and day_2["end_location_id"]
                == lodgings[1]["location_id"]
            ),
            "checkin_boundary_preserved": places[
                "hokkaido-booked-checkin"
            ]["time"]
            == "16:00",
            "cross_city_duration_preserved": (
                driving["duration_min"] == 180
            ),
            "winter_buffer_preserved": driving["buffer_min"] >= 45,
            "lost_ack_receipt_reconciled": (
                phase533["lost_ack_injected"]
                and phase533["lost_ack_count"] == 1
                and phase533["receipt_reconciliation_confirmed"]
            ),
        }
    raise RuntimeError("unknown Phase 6.3B canned family")


def _run_case(
    root: Path,
    *,
    case_name: str,
    plan_builder: Callable[[], tuple[dict[str, Any], date]],
    region_code: str,
    locality: str,
    approval_resume: bool,
    inject_lost_ack: bool,
) -> dict[str, Any]:
    original, first_day = plan_builder()
    original_specs = _original_route_specs(original)
    phase533 = _execute_case(
        root,
        slug=original["trip_id"],
        plan=original,
        first_day=first_day,
        fixed_times=_fixed_times(original),
        approval_resume=approval_resume,
        inject_lost_ack=inject_lost_ack,
    )
    for path in (
        root,
        root / original["trip_id"],
        root / original["trip_id"] / "data",
    ):
        path.chmod(0o700)
    (root / original["trip_id"] / "data" / "plan.json").chmod(0o600)
    store = TripStore(root, original["trip_id"])
    current = store.load_plan()
    canonical_before_delivery = store.plan_path.read_bytes()
    lodging_intake = _lodging_intake(first_day)
    semantics = _phase533_semantic_invariants(
        current,
        phase533,
        case_name=case_name,
        first_day=first_day,
    )
    _require(
        all(semantics.values()),
        "Phase 5.33 semantic golden drifted before Phase 6 delivery",
    )

    policies = _policy_registry()
    evidence_store = EvidenceStore(
        root,
        original["trip_id"],
        original["trip_id"],
        policies,
        clock=_FixedClock(PHASE63B_EVALUATION_AT),
    )
    empty = _durable_snapshot(evidence_store.load())
    baseline_composed = compose_trip_state(current, empty)
    baseline = assess_trip_readiness(
        canonical_plan=current,
        composed=baseline_composed,
        snapshot=empty,
        lodging_intake=lodging_intake,
    )
    _require(
        baseline.status is ReadinessStatus.REVIEW,
        "post-apply canned plan did not begin at review",
    )
    baseline_problem_codes = {item.code for item in baseline.problems}
    _require(
        baseline_problem_codes
        == {"LODGING_EVIDENCE_UNVERIFIED", "PLAN_NEEDS_VERIFICATION"},
        "post-apply baseline problems changed",
    )

    identity_only, identity_count = _promote_lodging_identities(
        evidence_store,
        current,
        case_name=case_name,
        region_code=region_code,
        locality=locality,
    )
    route_specs = _missing_route_specs(
        current,
        identity_only,
        original_specs,
    )
    identity_only_composed = compose_trip_state(current, identity_only)
    identity_only_readiness = assess_trip_readiness(
        canonical_plan=current,
        composed=identity_only_composed,
        snapshot=identity_only,
        lodging_intake=lodging_intake,
    )
    _require(
        identity_only_readiness.status is ReadinessStatus.REVIEW,
        "lodging identity incorrectly bypassed the route gate",
    )
    _require(
        {item.code for item in identity_only_readiness.problems}
        == {"PLAN_NEEDS_VERIFICATION"},
        "identity-only readiness did not isolate the route gap",
    )

    private_root = store.trip_dir / "private-delivery"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    identity_session = EvidenceSession(
        evidence_store,
        clock=_FixedClock(PHASE63B_EVALUATION_AT),
    )
    blocked_source = identity_session.private_delivery_source()
    identity_only_from_adapter = blocked_source.read_snapshot(
        evaluation_at=PHASE63B_EVALUATION_AT
    )
    _require(
        identity_only_from_adapter.evidence_revision
        == identity_only.evidence_revision
        and identity_only_from_adapter.store_revision
        == identity_only.store_revision
        and identity_only_from_adapter.outcome_revision is not None,
        "identity-only adapter view differs from its durable source",
    )
    blocked_leaf = f"{case_name}-identity-only"
    blocked_target = private_root / blocked_leaf
    try:
        prepare_private_delivery_write_review(
            store,
            blocked_source,
            profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
            private_root=private_root,
            target_leaf=blocked_leaf,
            clock=_FixedClock(PHASE63B_EVALUATION_AT),
            lodging_intake=lodging_intake,
        )
    except PrivateDeliveryWriteError as error:
        blocked_code = error.code
    else:
        raise RuntimeError("identity-only ready bundle was accepted")
    _require(
        blocked_code == "PRIVATE_DELIVERY_WRITE_REPROJECTION_REFUSED"
        and not blocked_target.exists(),
        "identity-only ready bundle did not fail before target creation",
    )

    promoted_snapshot = _promote_canned_routes(
        identity_session,
        policies,
        route_specs,
        case_name=case_name,
    )
    source = identity_session.private_delivery_source()
    snapshot = source.read_snapshot(
        evaluation_at=PHASE63B_EVALUATION_AT
    )
    _require(
        snapshot.evidence_revision == promoted_snapshot.evidence_revision
        and snapshot.store_revision == promoted_snapshot.store_revision
        and snapshot.outcome_revision == promoted_snapshot.outcome_revision,
        "delivery adapter view differs from the promoted session",
    )
    composed = compose_trip_state(current, snapshot)
    readiness = assess_trip_readiness(
        canonical_plan=current,
        composed=composed,
        snapshot=snapshot,
        lodging_intake=lodging_intake,
    )
    _require(
        readiness.status is ReadinessStatus.TRAVEL_READY
        and not readiness.problems
        and readiness.used_evidence_count
        == identity_count + len(route_specs),
        "canned exact evidence did not reach travel-ready",
    )

    target_leaf = f"{case_name}-generation-{current['generation']}"
    target = private_root / target_leaf
    clock = _FixedClock(PHASE63B_EVALUATION_AT)
    evidence_bytes_before_delivery = evidence_store.cache_path.read_bytes()
    evidence_stat_before_delivery = evidence_store.cache_path.stat()
    durable_before_delivery = evidence_store.read_snapshot(
        evaluation_at=PHASE63B_EVALUATION_AT
    )
    _require(
        len(durable_before_delivery.observations) == identity_count
        and all(
            item.provenance.provider_id != _ROUTE_PROVIDER_ID
            for item in durable_before_delivery.observations
        ),
        "memory-only canned routes crossed the durable evidence boundary",
    )
    snapshot_before_delivery = source.read_snapshot(
        evaluation_at=PHASE63B_EVALUATION_AT
    )
    write_review = prepare_private_delivery_write_review(
        store,
        source,
        profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
        private_root=private_root,
        target_leaf=target_leaf,
        clock=clock,
        lodging_intake=lodging_intake,
    )
    _require(not target.exists(), "write review created its target")
    response = capture_private_delivery_write_response(
        write_review,
        PrivateDeliveryWriteResponseKind.
        AUTHORIZE_HTML_ICS_READY_BUNDLE_CREATE_ONLY_WRITE,
    )
    _require(not target.exists(), "response capture created its target")
    outcome = execute_private_delivery_write_response(response)
    _require(
        outcome.kind is PrivateDeliveryWriteOutcomeKind.CREATED
        and outcome.final_target_published is True,
        "private delivery did not create an exact committed bundle",
    )
    _require(
        store.plan_path.read_bytes() == canonical_before_delivery,
        "private delivery changed canonical bytes",
    )
    snapshot_after_delivery = source.read_snapshot(
        evaluation_at=PHASE63B_EVALUATION_AT
    )
    evidence_stat_after_delivery = evidence_store.cache_path.stat()
    _require(
        evidence_store.cache_path.read_bytes()
        == evidence_bytes_before_delivery
        and evidence_stat_after_delivery.st_ino
        == evidence_stat_before_delivery.st_ino
        and evidence_stat_after_delivery.st_size
        == evidence_stat_before_delivery.st_size
        and evidence_stat_after_delivery.st_mtime_ns
        == evidence_stat_before_delivery.st_mtime_ns
        and snapshot_after_delivery.snapshot_id
        == snapshot_before_delivery.snapshot_id,
        "private delivery mutated its evidence source",
    )
    exact_tree = _verify_exact_private_tree(write_review, target)
    _require(exact_tree, "private delivery tree differs from its review")
    manifest = json.loads((target / "manifest.json").read_bytes())
    _require(
        manifest["contract_version"] == PRIVATE_DELIVERY_MANIFEST_VERSION
        and manifest["readiness"]["status"] == "travel_ready"
        and manifest["readiness"]["readiness_contract_version"]
        == READINESS_VERSION
        and manifest["readiness"]["readiness_id"]
        == readiness.readiness_id,
        "private manifest lost the travel-ready binding",
    )

    return {
        "status_chain": {
            "draft": phase533["status_chain"]["validate"],
            "review": phase533["status_chain"][
                "post_apply_validate"
            ],
            "travel_ready": readiness.status.value,
            "private_delivery": outcome.kind.value,
        },
        "phase533_typed_review_completed": (
            phase533["status_chain"]["review"] == "review_required"
            and phase533["status_chain"]["response"]
            == "response_captured"
            and phase533["canonical_write_performed"] is True
        ),
        "post_apply_review_reconfirmed": (
            baseline.status is ReadinessStatus.REVIEW
        ),
        "lodging_identity_alone_stayed_review": (
            identity_only_readiness.status is ReadinessStatus.REVIEW
        ),
        "identity_only_ready_bundle_rejected_prewrite": (
            blocked_code == "PRIVATE_DELIVERY_WRITE_REPROJECTION_REFUSED"
            and not blocked_target.exists()
        ),
        "identity_evidence_count": identity_count,
        "route_evidence_count": len(route_specs),
        "used_evidence_count": readiness.used_evidence_count,
        "problem_count": len(readiness.problems),
        "recheck_deadline_bound": (
            readiness.recheck_required_at is not None
            and readiness.recheck_required_at > readiness.evaluated_at
        ),
        "persisted_lodging_evidence_unverified": (
            _lodging_evidence_remains_unverified(current)
        ),
        "canonical_bytes_unchanged_by_delivery": True,
        "write_review_reloaded_source": write_review.to_safe_dict()[
            "authoritative_source_reloaded"
        ],
        "capture_performed_zero_writes": True,
        "matching_write_response_consumed": response.to_safe_dict()[
            "writes_performed"
        ],
        "exact_private_tree_confirmed": exact_tree,
        "private_artifact_count": len(write_review.artifacts),
        "manifest_commit_present": (target / "manifest.json").is_file(),
        "durable_evidence_unchanged_by_delivery": True,
        "memory_only_routes_not_persisted": True,
        "phase533_semantics": semantics,
    }


def run_walkthrough() -> dict[str, Any]:
    """Return one redacted deterministic Phase 6.3B transcript."""

    _require(
        PHASE63B_EVALUATION_AT > EVALUATION_AT + timedelta(seconds=4),
        "Phase 6.3B evidence must follow the latest Phase 5.33 apply path",
    )
    with tempfile.TemporaryDirectory(
        prefix="trip-planner-phase63b-"
    ) as temporary:
        temporary_root = Path(temporary)
        busan = _run_case(
            temporary_root,
            case_name="busan",
            plan_builder=_busan_plan,
            region_code="KR",
            locality="Busan",
            approval_resume=True,
            inject_lost_ack=False,
        )
        hokkaido = _run_case(
            temporary_root,
            case_name="hokkaido",
            plan_builder=_hokkaido_plan,
            region_code="JP",
            locality="Hokkaido",
            approval_resume=False,
            inject_lost_ack=True,
        )
        transcript = {
            "contract_version": ACCEPTANCE_VERSION,
            "mode": "offline_temporary_canned_authority",
            "boundaries": {
                "real_provider_calls": 0,
                "credential_reads": 0,
                "repository_trip_reads": 0,
                "repository_trip_writes": 0,
                "temporary_canonical_stores": 2,
                "temporary_durable_evidence_stores": 2,
                "temporary_memory_evidence_sessions": 2,
                "temporary_ready_bundle_projection_sets": 2,
                "temporary_private_bundles": 2,
                "browser_opened": False,
                "calendar_imported": False,
                "served_or_shared": False,
                "public_source_created": False,
                "deployed": False,
                "serialized_authority": False,
            },
            "busan": busan,
            "hokkaido": hokkaido,
        }
    _require(
        not temporary_root.exists(),
        "temporary Phase 6.3B fixtures were not cleaned",
    )
    transcript["boundaries"]["temporary_artifacts_retained"] = False
    rendered = json.dumps(transcript, ensure_ascii=False, sort_keys=True)
    for private_value in (
        _PRIVATE_PLACE_PREFIX,
        _PRIVATE_NAME_PREFIX,
        _PRIVATE_ADDRESS_PREFIX,
        "lodging-location-",
    ):
        _require(private_value not in rendered, "private value leaked")
    return transcript


def main() -> None:
    print(
        json.dumps(
            run_walkthrough(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
