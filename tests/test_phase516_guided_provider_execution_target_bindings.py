"""Phase 5.16 exact private provider-execution target bindings."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import date, timedelta

import trip_planner
import trip_planner.facts as facts_module
import trip_planner.guided_provider_execution_target_bindings as bindings_module
from tests.test_phase44_place_details import _Fixture as PlaceDetailsFixture
from tests.test_phase4_routes import _identity_observation
from tests.test_phase515_guided_provider_execution_targets import (
    TARGETS_ASSESSED_AT,
    _prepared_targets,
    _single_capability_context,
)
from tests.test_phase513_guided_provider_preflight import (
    EXPIRES_AT,
    _google_item,
)
from tests.test_phase56_guided_refinement import PRIVATE, START
from trip_planner.facts import (
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    EvidenceLedger,
    EvidenceSnapshot,
    google_maps_policy_registry,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetBindingItem,
    GuidedProviderExecutionTargetBindings,
    GuidedProviderExecutionTargetBindingsReview,
    GuidedProviderExecutionTargetBindingsStatus,
    GuidedProviderExecutionTargetPreimage,
    GuidedProviderExecutionTargetSourceKind,
    assess_guided_provider_execution_target_bindings,
    bind_guided_provider_execution_targets,
)
from trip_planner.guided_provider_execution_targets import (
    prepare_guided_provider_execution_targets,
)
from trip_planner.guided_provider_preflight import (
    GuidedProviderBillingRegion,
    GuidedProviderCredentialStatus,
    GuidedProviderPolicyProfile,
    GuidedProviderPreflightItem,
    GuidedProviderPricingProfile,
    GuidedProviderRequestProfile,
    GuidedProviderRetentionProfile,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.lodging_discovery import LodgingDiscoveryRequest
from trip_planner.models import DecisionState, EvidenceState
from trip_planner.place_details import PlaceDetailsKind
from trip_planner.places_identity import (
    PlaceIdentityIntent,
    extract_fresh_google_place_endpoint,
)
from trip_planner.routes import RouteMode, build_google_route_request


BIND_AT = TARGETS_ASSESSED_AT + timedelta(minutes=1)
ASSESS_AT = BIND_AT + timedelta(minutes=1)
PRIVATE_QUERY = "private phase516 target query " + PRIVATE
PRIVATE_HOTEL_QUERY = "private phase516 hotel query " + PRIVATE


def _identity_intent(
    *,
    query: str = PRIVATE_QUERY,
) -> PlaceIdentityIntent:
    return PlaceIdentityIntent(
        location_id="guided-private-location",
        text_query=query,
        expected_name="Private guided place " + PRIVATE,
        region_code="JP",
        language_code="zh-TW",
        expected_locality="Private locality " + PRIVATE,
        expected_primary_types=("tourist_attraction",),
    )


def _place_details_request(*, now=BIND_AT - timedelta(minutes=1)):
    fixture = PlaceDetailsFixture(now=now)
    return fixture.request(
        PlaceDetailsKind.CURRENT_HOURS,
        target_start=START,
        target_end=START,
    )


def _default_preimages(
    *,
    identity: PlaceIdentityIntent | None = None,
    details=None,
) -> tuple[GuidedProviderExecutionTargetPreimage, ...]:
    return (
        GuidedProviderExecutionTargetPreimage(
            topic=GuidedEvidenceTopic.PLACE_IDENTITY,
            source_line_indexes=(0,),
            target=_identity_intent() if identity is None else identity,
        ),
        GuidedProviderExecutionTargetPreimage(
            topic=GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
            source_line_indexes=(0,),
            target=(
                _place_details_request() if details is None else details
            ),
        ),
    )


def _prepared_bindings(
    *,
    context: tuple[object, ...] | None = None,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...] | None = None,
):
    exact_context = _prepared_targets() if context is None else context
    exact_preimages = _default_preimages() if preimages is None else preimages
    bindings = bind_guided_provider_execution_targets(
        *exact_context,
        preimages=exact_preimages,
        evaluation_at=BIND_AT,
    )
    return (*exact_context, bindings), exact_preimages


def _serpapi_item(topic: GuidedEvidenceTopic) -> GuidedProviderPreflightItem:
    return GuidedProviderPreflightItem(
        topic=topic,
        capability=GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
        request_profile=(
            GuidedProviderRequestProfile.SERPAPI_GOOGLE_HOTELS_PLAN_CREDIT
        ),
        pricing_profile=(
            GuidedProviderPricingProfile.SERPAPI_PLAN_CREDIT_2026_08_07
        ),
        policy_profile=GuidedProviderPolicyProfile.SERPAPI_TERMS_2026_04_08,
        retention_profile=(
            GuidedProviderRetentionProfile.SERPAPI_STANDARD_PROVIDER_STORAGE
        ),
        billing_region=GuidedProviderBillingRegion.NOT_APPLICABLE,
        credential_status=GuidedProviderCredentialStatus.AVAILABLE,
        max_request_count=1,
        serpapi_remaining_plan_credits=1,
        serpapi_automatic_renewal_enabled=False,
    )


def _hotel_request(
    *,
    query: str = PRIVATE_HOTEL_QUERY,
    check_in: date = START,
    check_out: date = START + timedelta(days=2),
) -> LodgingDiscoveryRequest:
    return LodgingDiscoveryRequest(
        query=query,
        check_in=check_in,
        check_out=check_out,
        adults=2,
        children=0,
        rooms=1,
        currency="JPY",
        currency_minor_unit=0,
        region="JP",
        language="zh-TW",
    )


def _route_request():
    policies = google_maps_policy_registry(
        GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
    )
    observations = (
        _identity_observation(
            policies=policies,
            location_id="location-origin",
            place_id="ChIJ-phase516-origin-private",
        ),
        _identity_observation(
            policies=policies,
            location_id="location-destination",
            place_id="ChIJ-phase516-destination-private",
        ),
    )
    ledger = EvidenceLedger(
        policies,
        observations,
        generation=1,
        _token=facts_module._LEDGER_TOKEN,
    )
    snapshot = EvidenceSnapshot.from_ledger(
        ledger,
        evaluation_at=BIND_AT - timedelta(minutes=1),
        purge_now=BIND_AT - timedelta(minutes=1),
        store_revision="d" * 64,
    )
    origin = extract_fresh_google_place_endpoint(
        snapshot,
        "location-origin",
    )
    destination = extract_fresh_google_place_endpoint(
        snapshot,
        "location-destination",
    )
    return build_google_route_request(
        snapshot,
        origin,
        destination,
        RouteMode.DRIVING,
        departure_at="2026-10-13T09:00:00+09:00",
    )


class GuidedProviderExecutionTargetBindingsTests(unittest.TestCase):
    def test_exact_bindings_cover_lines_and_reach_only_review_preparation(
        self,
    ) -> None:
        context, preimages = _prepared_bindings()

        review = assess_guided_provider_execution_target_bindings(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_AT,
        )
        safe = review.to_dict()

        self.assertEqual(
            GuidedProviderExecutionTargetBindingsStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW,
            review.status,
        )
        self.assertEqual(
            "prepare_private_provider_execution_authorization_review",
            safe["next_action"],
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        binding = safe["provider_execution_target_bindings"]
        self.assertEqual(2, binding["binding_item_count"])
        self.assertEqual(2, binding["bound_target_count"])
        self.assertEqual(2, binding["bound_source_line_reference_count"])
        self.assertEqual(5, binding["max_request_count"])
        self.assertEqual(
            {
                "canonical_private_intent": 1,
                "trusted_evidence_request_contract": 1,
            },
            binding["source_kind_counts"],
        )
        self.assertEqual(1, binding["private_intent_target_count"])
        self.assertEqual(
            1,
            binding["trusted_evidence_request_target_count"],
        )
        self.assertEqual(1, binding["policy_registry_revision_binding_count"])
        self.assertEqual(1, binding["snapshot_binding_count"])
        self.assertEqual(1, binding["store_revision_binding_count"])
        self.assertEqual(1, binding["evidence_revision_binding_count"])
        self.assertTrue(binding["source_line_coverage_complete"])
        self.assertTrue(
            binding["exact_private_preimages_required_for_reassessment"]
        )
        self.assertFalse(binding["caller_supplied_target_digest_accepted"])
        self.assertFalse(binding["target_fingerprints_exposed"])
        self.assertFalse(binding["raw_target_preimages_retained"])
        self.assertTrue(binding["all_execution_targets_bound"])
        self.assertEqual(
            2,
            binding["eligible_for_execution_authorization_item_count"],
        )
        self.assertEqual(
            2,
            binding["eligible_for_execution_authorization_target_count"],
        )
        self.assertFalse(
            binding["partial_execution_authorization_permitted"]
        )
        self.assertFalse(binding["provider_scope_authorized"])
        self.assertFalse(
            binding["provider_request_contracts_created_by_binding"]
        )
        self.assertFalse(binding["http_requests_created"])
        self.assertFalse(binding["provider_calls_permitted"])
        self.assertEqual("candidate", binding["decision_state"])
        self.assertEqual("unverified", binding["evidence_state"])
        self.assertIn(
            "provider_execution_target_bindings",
            safe["needs_verification"],
        )
        self.assertEqual(DecisionState.CANDIDATE, context[-1].decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, context[-1].evidence_state)

    def test_every_topic_uses_its_exact_existing_contract_type(self) -> None:
        route_item = _google_item(
            GuidedEvidenceTopic.ROUTE,
            GuidedProviderCapability.GOOGLE_ROUTES,
            1,
        )
        cases = [
            (
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
                route_item,
                _route_request(),
                "trusted_evidence_request_contract",
            ),
            *[
                (
                    topic,
                    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
                    _serpapi_item(topic),
                    _hotel_request(),
                    "canonical_private_intent",
                )
                for topic in (
                    GuidedEvidenceTopic.LODGING,
                    GuidedEvidenceTopic.AVAILABILITY,
                    GuidedEvidenceTopic.PRICE,
                )
            ],
        ]
        for topic, capability, preflight_item, target, source_kind in cases:
            with self.subTest(topic=topic):
                accepted = _single_capability_context(
                    topic,
                    capability,
                    preflight_item,
                )
                targets = prepare_guided_provider_execution_targets(
                    *accepted,
                    evaluation_at=TARGETS_ASSESSED_AT,
                )
                context = (*accepted, targets)
                preimages = (
                    GuidedProviderExecutionTargetPreimage(
                        topic=topic,
                        source_line_indexes=(0,),
                        target=target,
                    ),
                )
                bindings = bind_guided_provider_execution_targets(
                    *context,
                    preimages=preimages,
                    evaluation_at=BIND_AT,
                )
                safe = assess_guided_provider_execution_target_bindings(
                    *context,
                    bindings,
                    preimages=preimages,
                    evaluation_at=ASSESS_AT,
                ).to_dict()["provider_execution_target_bindings"]
                self.assertEqual(
                    source_kind,
                    next(
                        key
                        for key, count in safe["source_kind_counts"].items()
                        if count
                    ),
                )
                self.assertTrue(safe["items"][0]["target_bound"])
                self.assertTrue(
                    safe["items"][0]["source_line_coverage_complete"]
                )

    def test_missing_duplicate_extra_and_wrong_type_preimages_fail_closed(
        self,
    ) -> None:
        context = _prepared_targets()
        identity, details = _default_preimages()
        cases = (
            (identity,),
            (identity, identity, details),
            (
                identity,
                details,
                GuidedProviderExecutionTargetPreimage(
                    topic=GuidedEvidenceTopic.ROUTE,
                    source_line_indexes=(0,),
                    target=_route_request(),
                ),
            ),
            (
                GuidedProviderExecutionTargetPreimage(
                    topic=GuidedEvidenceTopic.PLACE_IDENTITY,
                    source_line_indexes=(0,),
                    target=_hotel_request(),
                ),
                details,
            ),
        )
        for preimages in cases:
            with self.subTest(preimages=repr(preimages)):
                with self.assertRaises((TypeError, ValueError)):
                    bind_guided_provider_execution_targets(
                        *context,
                        preimages=preimages,
                        evaluation_at=BIND_AT,
                    )

    def test_source_line_coverage_and_trip_dates_are_exact(self) -> None:
        context = _prepared_targets()
        identity, details = _default_preimages()
        for changed in (
            replace(identity, source_line_indexes=(1,)),
            replace(identity, source_line_indexes=(0, 1)),
        ):
            with self.assertRaises(ValueError):
                bind_guided_provider_execution_targets(
                    *context,
                    preimages=(changed, details),
                    evaluation_at=BIND_AT,
                )

        topic = GuidedEvidenceTopic.LODGING
        accepted = _single_capability_context(
            topic,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(topic),
        )
        targets = prepare_guided_provider_execution_targets(
            *accepted,
            evaluation_at=TARGETS_ASSESSED_AT,
        )
        outside = GuidedProviderExecutionTargetPreimage(
            topic=topic,
            source_line_indexes=(0,),
            target=_hotel_request(
                check_in=START - timedelta(days=1),
                check_out=START + timedelta(days=1),
            ),
        )
        with self.assertRaises(ValueError):
            bind_guided_provider_execution_targets(
                *accepted,
                targets,
                preimages=(outside,),
                evaluation_at=BIND_AT,
            )

    def test_bindings_reject_preimage_context_and_time_replay(self) -> None:
        context, preimages = _prepared_bindings()
        changed_preimages = _default_preimages(
            identity=_identity_intent(query=PRIVATE_QUERY + " changed")
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_target_bindings(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_target_bindings(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_target_bindings(
                *context,
                preimages=preimages,
                evaluation_at=EXPIRES_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_target_bindings(
                *context,
                preimages=preimages,
                evaluation_at=BIND_AT - timedelta(microseconds=1),
            )

        reordered = (
            context[0],
            tuple(reversed(context[1])),
            *context[2:],
        )
        original = assess_guided_provider_execution_target_bindings(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_AT,
        ).to_dict()
        replay = assess_guided_provider_execution_target_bindings(
            *reordered,
            preimages=preimages,
            evaluation_at=ASSESS_AT,
        ).to_dict()
        self.assertEqual(original, replay)

    def test_trusted_target_revision_and_endpoint_freshness_are_rechecked(
        self,
    ) -> None:
        details = _place_details_request(
            now=BIND_AT - timedelta(hours=23, minutes=55)
        )
        preimages = _default_preimages(details=details)
        context, preimages = _prepared_bindings(preimages=preimages)
        changed_details = _place_details_request(now=BIND_AT)
        changed_preimages = _default_preimages(details=changed_details)
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_target_bindings(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_AT,
            )

        stale_at = details.endpoint.valid_until
        self.assertLess(stale_at, EXPIRES_AT)
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_target_bindings(
                *context,
                preimages=preimages,
                evaluation_at=stale_at,
            )

    def test_safe_views_and_schema_do_not_expose_private_preimages(self) -> None:
        context, preimages = _prepared_bindings()
        bindings = context[-1]
        review = assess_guided_provider_execution_target_bindings(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_AT,
        )
        rendered = "\n".join(
            (
                repr(preimages),
                repr(bindings),
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )
        for private_value in (
            PRIVATE,
            PRIVATE_QUERY,
            "guided-private-location",
            "ChIJ-place/private value",
            "source_line_indexes",
            BIND_AT.isoformat(),
            details_fingerprint(preimages[1].target),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {"topic", "source_line_indexes", "target"},
            {item.name for item in fields(GuidedProviderExecutionTargetPreimage)},
        )
        self.assertNotIn(
            "target_digest",
            {item.name for item in fields(GuidedProviderExecutionTargetPreimage)},
        )
        safe = review.to_dict()["side_effects"]
        self.assertEqual(
            {
                "process_local": True,
                "private_target_preimages_read": True,
                "raw_target_preimages_retained": False,
                "target_fingerprints_exposed": False,
                "environment_read": False,
                "vault_accessed": False,
                "credentials_accessed": False,
                "provider_request_contracts_created": False,
                "http_requests_created": False,
                "provider_calls": False,
                "writes_to_trip": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
            safe,
        )

    def test_bindings_and_reviews_cannot_be_forged(self) -> None:
        context, preimages = _prepared_bindings()
        bindings = context[-1]
        review = assess_guided_provider_execution_target_bindings(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_AT,
        )
        with self.assertRaises(ValueError):
            GuidedProviderExecutionTargetBindings(
                items=bindings.items,
                _bound_at=BIND_AT,
                _context_fingerprint="a" * 64,
            )
        with self.assertRaises(ValueError):
            GuidedProviderExecutionTargetBindingItem(
                requirement=bindings.items[0].requirement,
                _records=bindings.items[0]._records,
            )
        with self.assertRaises(ValueError):
            replace(bindings, _context_fingerprint="a" * 64)
        with self.assertRaises(ValueError):
            GuidedProviderExecutionTargetBindingsReview(
                status=(
                    GuidedProviderExecutionTargetBindingsStatus
                    .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW
                ),
                next_action=(
                    "prepare_private_provider_execution_authorization_review"
                ),
                accepted_scope_item_count=2,
                accepted_max_request_count=5,
                preflight_item_count=2,
                target_item_count=2,
                binding_item_count=2,
                bound_target_count=2,
                bound_source_line_reference_count=2,
                max_request_count=5,
                items=bindings.items,
                host_attestation_fresh=True,
            )
        with self.assertRaises(ValueError):
            replace(review, next_action="execute_provider")

    def test_public_contract_has_no_authorization_or_execution_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_EXECUTION_TARGET_BINDINGS_VERSION",
            "GuidedProviderExecutionTargetBindingItem",
            "GuidedProviderExecutionTargetBindings",
            "GuidedProviderExecutionTargetBindingsReview",
            "GuidedProviderExecutionTargetBindingsStatus",
            "GuidedProviderExecutionTargetPreimage",
            "GuidedProviderExecutionTargetSourceKind",
            "assess_guided_provider_execution_target_bindings",
            "bind_guided_provider_execution_targets",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "authorize_guided_provider_execution",
            "capture_guided_provider_execution_authorization",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "call_guided_provider",
            "apply_guided_provider_execution_target_bindings",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(bindings_module)
        tree = ast.parse(source)
        allowed_imports = {"hashlib", "json", "re"}
        allowed_from_imports = {
            (0, "__future__"),
            (0, "collections"),
            (0, "dataclasses"),
            (0, "datetime"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "guided_evidence_plan"),
            (1, "guided_itinerary"),
            (1, "guided_proposal"),
            (1, "guided_provider_execution_targets"),
            (1, "guided_provider_preflight"),
            (1, "guided_provider_preflight_response"),
            (1, "guided_provider_scope"),
            (1, "guided_provider_scope_response"),
            (1, "guided_refinement"),
            (1, "facts"),
            (1, "lodging_discovery"),
            (1, "models"),
            (1, "place_details"),
            (1, "places_identity"),
            (1, "routes"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.level, node.module), allowed_from_imports)
        for forbidden in (
            "os.environ",
            "getenv",
            "urlopen",
            "import requests",
            "subprocess",
            "Path(",
            "open(",
            "EvidenceStore",
            "build_google_place_details_http_request",
            "build_google_routes_http_request",
            "execute_google_",
        ):
            self.assertNotIn(forbidden, source)


def details_fingerprint(value: object) -> str:
    return value.provider_request.request_fingerprint


if __name__ == "__main__":
    unittest.main()
