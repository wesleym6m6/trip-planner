"""Phase 5.15 deterministic provider-execution target requirements."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_execution_targets as targets_module
from tests.test_phase510_guided_evidence_plan import (
    _accepted_response_context,
    _plan,
    _requirement,
)
from tests.test_phase511_guided_provider_scope import _proposal, _scope_item
from tests.test_phase513_guided_provider_preflight import (
    CHECKED_AT,
    EVALUATION_AT,
    EXPIRES_AT,
    _google_item,
    _prepared,
)
from tests.test_phase56_guided_refinement import PRIVATE
from trip_planner.guided_evidence_plan import (
    GuidedEvidenceDisposition,
    GuidedEvidenceTopic,
)
from trip_planner.guided_provider_execution_targets import (
    GuidedProviderExecutionTargetDependency,
    GuidedProviderExecutionTargetItem,
    GuidedProviderExecutionTargetKind,
    GuidedProviderExecutionTargets,
    GuidedProviderExecutionTargetsReview,
    GuidedProviderExecutionTargetsStatus,
    assess_guided_provider_execution_targets,
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
    prepare_guided_provider_preflight,
)
from trip_planner.guided_provider_preflight_response import (
    GuidedProviderPreflightResponseKind,
    capture_guided_provider_preflight_response,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.guided_provider_scope_response import (
    GuidedProviderScopeResponseKind,
    capture_guided_provider_scope_response,
)
from trip_planner.models import DecisionState, EvidenceState


TARGETS_PREPARED_AT = EVALUATION_AT + timedelta(minutes=1)
TARGETS_ASSESSED_AT = TARGETS_PREPARED_AT + timedelta(minutes=1)


def _accepted_preflight_context(
    *,
    context: tuple[object, ...] | None = None,
):
    exact_context = _prepared() if context is None else context
    response = capture_guided_provider_preflight_response(
        *exact_context,
        kind=GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT,
        evaluation_at=EVALUATION_AT,
    )
    return (*exact_context, response)


def _prepared_targets(*, context: tuple[object, ...] | None = None):
    exact_context = (
        _accepted_preflight_context() if context is None else context
    )
    targets = prepare_guided_provider_execution_targets(
        *exact_context,
        evaluation_at=TARGETS_PREPARED_AT,
    )
    return (*exact_context, targets)


def _single_capability_context(
    topic: GuidedEvidenceTopic,
    capability: GuidedProviderCapability,
    item: GuidedProviderPreflightItem,
):
    upstream = _accepted_response_context()
    evidence_plan = _plan(
        _requirement(0, topic),
        _requirement(
            1,
            disposition=(
                GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
            ),
        ),
    )
    provider_scope = _proposal(_scope_item(topic, capability, 1))
    scope_response = capture_guided_provider_scope_response(
        *upstream,
        evidence_plan,
        provider_scope,
        kind=GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE,
    )
    base = (*upstream, evidence_plan, provider_scope, scope_response)
    preflight = prepare_guided_provider_preflight(
        *base,
        items=(item,),
        checked_at=CHECKED_AT,
        expires_at=EXPIRES_AT,
    )
    return _accepted_preflight_context(context=(*base, preflight))


class GuidedProviderExecutionTargetsTests(unittest.TestCase):
    def test_google_targets_are_deferred_and_preserve_exact_aggregates(
        self,
    ) -> None:
        context = _prepared_targets()

        review = assess_guided_provider_execution_targets(
            *context,
            evaluation_at=TARGETS_ASSESSED_AT,
        )
        safe = review.to_dict()

        self.assertEqual(
            GuidedProviderExecutionTargetsStatus
            .NEEDS_PRIVATE_EXECUTION_TARGETS,
            review.status,
        )
        self.assertEqual(
            "prepare_private_provider_execution_targets",
            safe["next_action"],
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        target_plan = safe["provider_execution_targets"]
        self.assertTrue(target_plan["exact_private_context_bound"])
        self.assertTrue(target_plan["host_attestation_fresh"])
        self.assertEqual(2, target_plan["accepted_scope_item_count"])
        self.assertEqual(5, target_plan["accepted_max_request_count"])
        self.assertEqual(2, target_plan["preflight_item_count"])
        self.assertEqual(2, target_plan["target_item_count"])
        self.assertEqual(5, target_plan["max_request_count"])
        self.assertEqual(
            [
                "trusted_place_endpoint",
                "private_place_identity_intent",
            ],
            [item["target_kind"] for item in target_plan["items"]],
        )
        self.assertEqual(
            [
                "trusted_place_identity_evidence",
                "guided_private_context_binding",
            ],
            [item["dependency"] for item in target_plan["items"]],
        )
        self.assertEqual(0, target_plan[
            "eligible_for_execution_authorization_item_count"
        ])
        self.assertEqual(2, target_plan["deferred_target_item_count"])
        self.assertFalse(target_plan["all_execution_targets_bound"])
        self.assertFalse(
            target_plan["partial_execution_authorization_permitted"]
        )
        self.assertFalse(
            target_plan["provider_result_dependency_auto_authorizes_followup"]
        )
        self.assertEqual(136_000, target_plan[
            "estimated_first_paid_tier_google_cost_usd_micros"
        ])
        self.assertEqual(0, target_plan["serpapi_plan_credit_cap"])
        self.assertTrue(target_plan[
            "all_provider_costs_have_currency_list_rate_estimates"
        ])
        self.assertFalse(target_plan["monthly_free_usage_remaining_checked"])
        self.assertFalse(target_plan["cost_estimate_is_hard_currency_cap"])
        self.assertTrue(target_plan[
            "execution_time_pricing_policy_retention_recheck_required"
        ])
        self.assertTrue(target_plan[
            "credential_availability_recheck_required"
        ])
        self.assertTrue(target_plan[
            "explicit_execution_authorization_required_before_any_call"
        ])
        self.assertFalse(target_plan["provider_scope_authorized"])
        self.assertFalse(target_plan["provider_requests_created"])
        self.assertFalse(target_plan["provider_calls_permitted"])
        self.assertFalse(target_plan["is_travel_ready"])
        self.assertEqual("candidate", target_plan["decision_state"])
        self.assertEqual("unverified", target_plan["evidence_state"])
        self.assertFalse(target_plan["supports_authoritative_use"])
        self.assertIn(
            "provider_execution_targets",
            safe["needs_verification"],
        )
        self.assertEqual(
            {
                "process_local": True,
                "target_values_read": False,
                "environment_read": False,
                "vault_accessed": False,
                "credentials_accessed": False,
                "provider_requests_created": False,
                "provider_calls": False,
                "writes_to_trip": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )
        self.assertEqual(DecisionState.CANDIDATE, context[-1].decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, context[-1].evidence_state)

    def test_routes_and_serpapi_require_distinct_typed_targets(self) -> None:
        route_item = _google_item(
            GuidedEvidenceTopic.ROUTE,
            GuidedProviderCapability.GOOGLE_ROUTES,
            1,
        )
        serpapi_item = GuidedProviderPreflightItem(
            topic=GuidedEvidenceTopic.LODGING,
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
        cases = (
            (
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
                route_item,
                "trusted_route_endpoint_pair",
                "trusted_route_endpoint_evidence",
            ),
            (
                GuidedEvidenceTopic.LODGING,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
                serpapi_item,
                "private_serpapi_hotel_search_intent",
                "guided_private_context_binding",
            ),
            (
                GuidedEvidenceTopic.AVAILABILITY,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
                replace(serpapi_item, topic=GuidedEvidenceTopic.AVAILABILITY),
                "private_serpapi_hotel_search_intent",
                "guided_private_context_binding",
            ),
            (
                GuidedEvidenceTopic.PRICE,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
                replace(serpapi_item, topic=GuidedEvidenceTopic.PRICE),
                "private_serpapi_hotel_search_intent",
                "guided_private_context_binding",
            ),
        )
        for topic, capability, item, target_kind, dependency in cases:
            with self.subTest(capability=capability):
                accepted = _single_capability_context(topic, capability, item)
                targets = prepare_guided_provider_execution_targets(
                    *accepted,
                    evaluation_at=TARGETS_PREPARED_AT,
                )
                safe = assess_guided_provider_execution_targets(
                    *accepted,
                    targets,
                    evaluation_at=TARGETS_ASSESSED_AT,
                ).to_dict()["provider_execution_targets"]
                self.assertEqual(target_kind, safe["items"][0]["target_kind"])
                self.assertEqual(dependency, safe["items"][0]["dependency"])
                self.assertFalse(safe["items"][0]["target_bound"])
                self.assertFalse(
                    safe["items"][0][
                        "eligible_for_execution_authorization"
                    ]
                )

    def test_only_accept_response_can_prepare_execution_targets(self) -> None:
        for kind in (
            GuidedProviderPreflightResponseKind
            .REQUEST_SMALLER_PROVIDER_PREFLIGHT,
            GuidedProviderPreflightResponseKind.CANCEL_EXTERNAL_EXECUTION,
        ):
            with self.subTest(kind=kind):
                context = _prepared()
                response = capture_guided_provider_preflight_response(
                    *context,
                    kind=kind,
                    evaluation_at=EVALUATION_AT,
                )
                with self.assertRaises(ValueError):
                    prepare_guided_provider_execution_targets(
                        *context,
                        response,
                        evaluation_at=TARGETS_PREPARED_AT,
                    )

    def test_targets_cannot_replay_after_expiry_or_clock_rollback(self) -> None:
        context = _prepared_targets()
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_targets(
                *context,
                evaluation_at=EXPIRES_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_targets(
                *context,
                evaluation_at=TARGETS_PREPARED_AT - timedelta(microseconds=1),
            )
        with self.assertRaises(ValueError):
            prepare_guided_provider_execution_targets(
                *_accepted_preflight_context(),
                evaluation_at=TARGETS_PREPARED_AT.replace(tzinfo=None),
            )

    def test_targets_bind_every_context_layer_and_allow_card_reordering(
        self,
    ) -> None:
        context = _prepared_targets()
        original = assess_guided_provider_execution_targets(
            *context,
            evaluation_at=TARGETS_ASSESSED_AT,
        ).to_dict()
        reordered_context = (
            context[0],
            tuple(reversed(context[1])),
            *context[2:],
        )
        reordered = assess_guided_provider_execution_targets(
            *reordered_context,
            evaluation_at=TARGETS_ASSESSED_AT,
        ).to_dict()
        self.assertEqual(original, reordered)

        changed = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_targets(
                *changed,
                evaluation_at=TARGETS_ASSESSED_AT,
            )
        duplicate = prepare_guided_provider_execution_targets(
            *_accepted_preflight_context(),
            evaluation_at=TARGETS_PREPARED_AT,
        )
        self.assertEqual(context[-1], duplicate)

    def test_target_plan_and_review_cannot_be_forged_or_replaced(self) -> None:
        context = _prepared_targets()
        targets = context[-1]
        review = assess_guided_provider_execution_targets(
            *context,
            evaluation_at=TARGETS_ASSESSED_AT,
        )
        with self.assertRaises(ValueError):
            GuidedProviderExecutionTargets(
                items=targets.items,
                _prepared_at=TARGETS_PREPARED_AT,
            )
        with self.assertRaises(ValueError):
            replace(targets, items=(targets.items[0],))
        with self.assertRaises(ValueError):
            GuidedProviderExecutionTargetItem(
                topic=GuidedEvidenceTopic.PLACE_IDENTITY,
                capability=(
                    GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP
                ),
                request_profile=(
                    GuidedProviderRequestProfile.GOOGLE_PLACES_TEXT_SEARCH_PRO
                ),
                target_kind=(
                    GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT
                ),
                dependency=(
                    GuidedProviderExecutionTargetDependency
                    .GUIDED_PRIVATE_CONTEXT_BINDING
                ),
                max_request_count=1,
            )
        with self.assertRaises(ValueError):
            GuidedProviderExecutionTargetsReview(
                status=(
                    GuidedProviderExecutionTargetsStatus
                    .NEEDS_PRIVATE_EXECUTION_TARGETS
                ),
                next_action="prepare_private_provider_execution_targets",
                accepted_scope_item_count=2,
                accepted_max_request_count=5,
                preflight_item_count=2,
                target_item_count=2,
                max_request_count=5,
                items=targets.items,
                host_attestation_fresh=True,
            )
        with self.assertRaises(ValueError):
            replace(review, next_action="build_guided_provider_request")

    def test_safe_output_and_schema_exclude_target_values(self) -> None:
        context = _prepared_targets()
        targets = context[-1]
        review = assess_guided_provider_execution_targets(
            *context,
            evaluation_at=TARGETS_ASSESSED_AT,
        )
        rendered = "\n".join(
            (
                repr(targets),
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )
        for private_value in (
            PRIVATE,
            "card-a",
            "card-refined",
            "source_line_index",
            "Private direction A",
            "2026-10-12",
            CHECKED_AT.isoformat(),
            TARGETS_PREPARED_AT.isoformat(),
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {"items", "_prepared_at", "_context_fingerprint"},
            {item.name for item in fields(GuidedProviderExecutionTargets)},
        )
        forbidden_fields = {
            "query",
            "payload",
            "url",
            "api_key",
            "secret",
            "provider_resource_id",
            "place_id",
            "address",
            "coordinates",
            "target_digest",
            "authorization",
            "confirmation",
        }
        self.assertTrue(
            forbidden_fields.isdisjoint(
                {item.name for item in fields(GuidedProviderExecutionTargetItem)}
            )
        )

    def test_public_contract_has_no_target_binding_or_execution_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_EXECUTION_TARGETS_VERSION",
            "GuidedProviderExecutionTargetDependency",
            "GuidedProviderExecutionTargetItem",
            "GuidedProviderExecutionTargetKind",
            "GuidedProviderExecutionTargets",
            "GuidedProviderExecutionTargetsReview",
            "GuidedProviderExecutionTargetsStatus",
            "assess_guided_provider_execution_targets",
            "prepare_guided_provider_execution_targets",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "bind_guided_provider_execution_target",
            "build_guided_provider_request",
            "authorize_guided_provider_execution",
            "execute_guided_provider_request",
            "call_guided_provider",
            "apply_guided_provider_execution_targets",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(targets_module)
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
            (1, "guided_provider_preflight"),
            (1, "guided_provider_preflight_response"),
            (1, "guided_provider_scope"),
            (1, "guided_provider_scope_response"),
            (1, "guided_refinement"),
            (1, "models"),
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
            "requests.",
            "subprocess",
            "Path(",
            "open(",
            "EvidenceStore",
            "ProviderRequest(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
