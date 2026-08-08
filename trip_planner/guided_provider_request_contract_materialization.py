"""Materialize exact process-local provider request contracts without transport.

Phase 5.22 consumes only a fresh Phase 5.21 prepare-materialization response
and the same exact typed target preimages.  It converts the previously reviewed
request candidates into token-gated process-local contracts, binding exact
transmitted values, provider identifiers, local result context, and trusted
source revisions without retaining raw preimage objects.

These contracts are deliberately non-sendable.  This module does not choose an
HTTP method or endpoint, construct an HTTP request, inject or read credentials,
call a provider, reserve spend, persist data, schedule, render, deploy, confirm,
or mutate canonical state.  A separate private send-authorization review is
required next.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .guided_draft import TripBriefDraft
from .guided_evidence_plan import GuidedEvidenceRequirementPlan, GuidedEvidenceTopic
from .guided_itinerary import GuidedItineraryCandidate, GuidedItineraryResponse
from .guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreference,
    _private_context_value,
)
from .guided_provider_execution_authorization_response import (
    GuidedProviderExecutionAuthorizationResponse,
)
from .guided_provider_execution_authorization_review import (
    GuidedProviderExecutionAuthorizationReview,
    _source_state_counts,
)
from .guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetBindings,
    GuidedProviderExecutionTargetPreimage,
    GuidedProviderExecutionTargetSourceKind,
)
from .guided_provider_execution_targets import (
    GuidedProviderExecutionTargetKind,
    GuidedProviderExecutionTargets,
)
from .guided_provider_execution_time_recheck import (
    GuidedProviderExecutionTimeRecheck,
)
from .guided_provider_preflight import (
    GuidedProviderPolicyProfile,
    GuidedProviderPreflight,
    GuidedProviderPricingProfile,
    GuidedProviderRequestProfile,
    GuidedProviderRetentionProfile,
)
from .guided_provider_preflight_response import GuidedProviderPreflightResponse
from .guided_provider_request_materialization_response import (
    GUIDED_PROVIDER_REQUEST_MATERIALIZATION_RESPONSE_VERSION,
    GuidedProviderRequestMaterializationResponse,
    GuidedProviderRequestMaterializationResponseReview,
    GuidedProviderRequestMaterializationResponseStatus,
    assess_guided_provider_request_materialization_response,
)
from .guided_provider_request_materialization_review import (
    GuidedProviderRequestContractCandidate,
    GuidedProviderRequestMaterializationKind,
    GuidedProviderRequestMaterializationReview,
)
from .guided_provider_scope import (
    GuidedProviderCapability,
    GuidedProviderDataCategory,
    GuidedProviderScopeProposal,
)
from .guided_provider_scope_response import GuidedProviderScopeResponse
from .guided_refinement import GuidedRefinementCandidate, GuidedRefinementResponse
from .lodging_discovery import LodgingDiscoveryRequest
from .models import DecisionState, EvidenceState
from .place_details import GooglePlaceDetailsRequest
from .places_identity import GOOGLE_PLACE_IDENTITY_FIELD_MASK, PlaceIdentityIntent
from .routes import GoogleRouteRequest


GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION = (
    "guided-provider-request-contract-materialization/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ITEMS = 12
_MAX_REQUESTS = 32
_MAX_SOURCE_LINE_REFERENCES = _MAX_ITEMS * 32
_MAX_GOOGLE_LIST_COST_USD_MICROS = _MAX_REQUESTS * 32_000
_NEXT_ACTION = "prepare_private_provider_request_send_authorization_review"
_CONTRACT_TOKEN = object()
_MATERIALIZATION_TOKEN = object()
_REVIEW_TOKEN = object()
_TENTATIVE_FIELDS = {
    "destination",
    "dates",
    "party",
    "budget",
    "pace",
    "must_do",
    "constraints",
}
_VERIFICATION_TOPICS = {
    "destination_location",
    "transport_boundaries",
    "lodging",
    "proposal_candidates",
    "itinerary_candidate",
    "evidence_requirements",
    "provider_scope",
    "provider_preflight",
    "provider_execution_targets",
    "provider_execution_target_bindings",
    "provider_execution_authorization_review",
    "provider_execution_authorization_response",
    "provider_execution_time_recheck",
    "provider_request_materialization_review",
    "provider_request_materialization_response",
    "provider_request_contract_materialization",
}
_EXPECTED_TRANSMITTED_FIELDS = {
    GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH: {
        "field_mask",
        "language_code",
        "region_code",
        "text_query",
    },
    GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS: {
        "field_mask",
        "language_code",
        "region_code",
    },
    GuidedProviderRequestMaterializationKind.GOOGLE_ROUTES_COMPUTE_ROUTES: {
        "departure_at",
        "field_mask",
        "mode",
    },
    GuidedProviderRequestMaterializationKind.SERPAPI_GOOGLE_HOTELS: {
        "adults",
        "check_in",
        "check_out",
        "children",
        "currency",
        "language",
        "query",
        "region",
        "rooms",
    },
}
_OPTIONAL_IDENTITY_BIAS_FIELDS = {"latitude", "longitude", "radius_m"}
_EXPECTED_LOCAL_FIELDS = {
    GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT: {
        "expected_locality",
        "expected_name",
        "expected_primary_types",
        "stable_local_location_id",
    },
    GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT: {
        "kind",
        "stable_local_location_id",
        "target_end",
        "target_start",
    },
    GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR: {
        "destination_stable_local_location_id",
        "fallback_from_mode",
        "origin_stable_local_location_id",
        "transit_fallback_policy",
    },
    GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT: {
        "currency_minor_unit",
    },
}
_EXPECTED_PROVIDER_IDENTIFIER_FIELDS = {
    GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT: set(),
    GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT: {
        "provider_place_id"
    },
    GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR: {
        "destination_provider_place_id",
        "origin_provider_place_id",
    },
    GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT: set(),
}


class GuidedProviderRequestContractMaterializationStatus(str, Enum):
    """A materialized contract bundle may only enter send review."""

    READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW = (
        "ready_for_private_provider_request_send_authorization_review"
    )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestContract:
    """Exact process-local request contract with no transport or send ability."""

    topic: GuidedEvidenceTopic
    capability: GuidedProviderCapability
    request_profile: GuidedProviderRequestProfile
    materialization_kind: GuidedProviderRequestMaterializationKind
    pricing_profile: GuidedProviderPricingProfile
    policy_profile: GuidedProviderPolicyProfile
    retention_profile: GuidedProviderRetentionProfile
    target_kind: GuidedProviderExecutionTargetKind
    source_kind: GuidedProviderExecutionTargetSourceKind
    source_line_reference_count: int
    user_stated_source_line_count: int
    tentative_source_line_count: int
    ai_candidate_source_line_count: int
    _provider_transmitted_values: tuple[tuple[str, object], ...] = field(
        repr=False
    )
    _provider_identifier_values: tuple[tuple[str, str], ...] = field(repr=False)
    _local_result_binding: tuple[tuple[str, object], ...] = field(repr=False)
    _source_binding_fingerprint: str = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _CONTRACT_TOKEN:
            raise ValueError("Provider request contracts require trusted materialization")
        exact_enums = (
            (self.topic, GuidedEvidenceTopic),
            (self.capability, GuidedProviderCapability),
            (self.request_profile, GuidedProviderRequestProfile),
            (self.materialization_kind, GuidedProviderRequestMaterializationKind),
            (self.pricing_profile, GuidedProviderPricingProfile),
            (self.policy_profile, GuidedProviderPolicyProfile),
            (self.retention_profile, GuidedProviderRetentionProfile),
            (self.target_kind, GuidedProviderExecutionTargetKind),
            (self.source_kind, GuidedProviderExecutionTargetSourceKind),
        )
        if any(type(value) is not expected for value, expected in exact_enums):
            raise TypeError("Provider request contract enums must be exact")
        if (
            type(self.source_line_reference_count) is not int
            or not 1 <= self.source_line_reference_count <= 32
        ):
            raise ValueError("Provider request contract source count is invalid")
        for value in (
            self.user_stated_source_line_count,
            self.tentative_source_line_count,
            self.ai_candidate_source_line_count,
        ):
            if type(value) is not int or not 0 <= value <= 32:
                raise ValueError("Provider request contract provenance is invalid")
        if (
            self.user_stated_source_line_count
            + self.tentative_source_line_count
            + self.ai_candidate_source_line_count
            != self.source_line_reference_count
        ):
            raise ValueError("Provider request contract provenance differs")
        transmitted_names = _private_field_names(
            self._provider_transmitted_values,
            "provider_transmitted_values",
        )
        required_transmitted = _EXPECTED_TRANSMITTED_FIELDS[
            self.materialization_kind
        ]
        if self.materialization_kind is (
            GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH
        ):
            if not (
                transmitted_names == required_transmitted
                or transmitted_names
                == required_transmitted | _OPTIONAL_IDENTITY_BIAS_FIELDS
            ):
                raise ValueError("Identity request contract fields differ")
        elif transmitted_names != required_transmitted:
            raise ValueError("Provider request contract fields differ")
        identifier_names = _private_field_names(
            self._provider_identifier_values,
            "provider_identifier_values",
            require_string_values=True,
        )
        if identifier_names != _EXPECTED_PROVIDER_IDENTIFIER_FIELDS[self.target_kind]:
            raise ValueError("Provider request identifier fields differ")
        local_names = _private_field_names(
            self._local_result_binding,
            "local_result_binding",
        )
        if local_names != _EXPECTED_LOCAL_FIELDS[self.target_kind]:
            raise ValueError("Provider request local-result binding differs")
        if transmitted_names.intersection(identifier_names | local_names):
            raise ValueError("Provider request private fields overlap")
        _digest(self._source_binding_fingerprint, "source_binding_fingerprint")
        _digest(self._context_fingerprint, "context_fingerprint")

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    @property
    def bound_provider_identifier_count(self) -> int:
        return len(self._provider_identifier_values)

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestContract("
            f"topic={self.topic.value!r}, "
            f"materialization_kind={self.materialization_kind.value!r})"
        )

    def to_safe_dict(self) -> dict[str, object]:
        """Return contract shape without any exact private value."""

        return {
            "topic": self.topic.value,
            "capability": self.capability.value,
            "request_profile": self.request_profile.value,
            "materialization_kind": self.materialization_kind.value,
            "pricing_profile": self.pricing_profile.value,
            "policy_profile": self.policy_profile.value,
            "retention_profile": self.retention_profile.value,
            "target_kind": self.target_kind.value,
            "source_kind": self.source_kind.value,
            "materialized_contract_count": 1,
            "source_line_reference_count": self.source_line_reference_count,
            "source_state_counts": {
                "user_stated": self.user_stated_source_line_count,
                "tentative": self.tentative_source_line_count,
                "ai_candidate": self.ai_candidate_source_line_count,
            },
            "all_source_lines_require_verification": True,
            "source_values_are_authoritative": False,
            "provider_transmitted_field_names": [
                name for name, _ in self._provider_transmitted_values
            ],
            "provider_identifier_field_names": [
                name for name, _ in self._provider_identifier_values
            ],
            "local_result_binding_field_names": [
                name for name, _ in self._local_result_binding
            ],
            "redacted_bound_provider_identifier_count": (
                self.bound_provider_identifier_count
            ),
            "exact_provider_transmitted_values_retained_process_locally": True,
            "provider_identifier_values_retained_process_locally": bool(
                self._provider_identifier_values
            ),
            "local_result_binding_retained_process_locally": True,
            "exact_private_values_included": False,
            "source_binding_fingerprint_exposed": False,
            "context_fingerprint_exposed": False,
            "provider_request_contract_materialized": True,
            "provider_request_contract_is_executable": False,
            "provider_request_contract_is_sendable": False,
            "transport_endpoint_selected": False,
            "http_method_selected": False,
            "credential_slot_bound": False,
            "credential_value_included": False,
            "http_request_created": False,
            "provider_call_permitted": False,
            "decision_state": DecisionState.CANDIDATE.value,
            "evidence_state": EvidenceState.UNVERIFIED.value,
            "supports_authoritative_use": False,
        }


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestContractMaterialization:
    """Token-gated bundle of exact non-sendable request contracts."""

    _contracts: tuple[GuidedProviderRequestContract, ...] = field(repr=False)
    _materialized_at: datetime = field(repr=False)
    _expires_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _MATERIALIZATION_TOKEN:
            raise ValueError("Request contract bundles require trusted materialization")
        if (
            not isinstance(self._contracts, tuple)
            or not 1 <= len(self._contracts) <= _MAX_REQUESTS
            or any(
                type(item) is not GuidedProviderRequestContract
                for item in self._contracts
            )
            or tuple(sorted(self._contracts, key=_contract_sort_key))
            != self._contracts
        ):
            raise ValueError("Materialized request contracts are invalid")
        materialized = _utc_datetime(self._materialized_at, "materialized_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not materialized < expires <= materialized + timedelta(minutes=5):
            raise ValueError("Request contract materialization expiry is invalid")
        _digest(self._context_fingerprint, "context_fingerprint")
        object.__setattr__(self, "_materialized_at", materialized)
        object.__setattr__(self, "_expires_at", expires)

    @property
    def contract_count(self) -> int:
        return len(self._contracts)

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestContractMaterialization("
            f"contract_count={self.contract_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestContractMaterializationReview:
    """Safe assessment of exact non-sendable request contracts."""

    status: GuidedProviderRequestContractMaterializationStatus
    next_action: str
    accepted_scope_item_count: int
    accepted_max_request_count: int
    candidate_count: int
    contract_count: int
    bound_request_count: int
    max_request_count: int
    bound_source_line_reference_count: int
    bound_google_request_count: int
    bound_serpapi_request_count: int
    user_stated_source_line_count: int
    tentative_source_line_count: int
    ai_candidate_source_line_count: int
    estimated_bound_first_paid_tier_google_cost_usd_micros: int
    accepted_max_first_paid_tier_google_cost_usd_micros: int
    serpapi_bound_plan_credit_count: int
    serpapi_plan_credit_cap: int
    bound_request_capability_counts: tuple[
        tuple[GuidedProviderCapability, int], ...
    ] = ()
    data_categories: tuple[GuidedProviderDataCategory, ...] = ()
    host_attestation_fresh: bool = False
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    _contracts: tuple[GuidedProviderRequestContract, ...] = field(
        default=(), repr=False
    )
    contract_version: str = GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Request contract reviews require the assessor")
        if (
            type(self.status) is not GuidedProviderRequestContractMaterializationStatus
            or self.status
            is not GuidedProviderRequestContractMaterializationStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW
            or self.next_action != _NEXT_ACTION
        ):
            raise ValueError("Request contract review status/action differs")
        for name, value, maximum in (
            ("accepted_scope_item_count", self.accepted_scope_item_count, _MAX_ITEMS),
            ("accepted_max_request_count", self.accepted_max_request_count, _MAX_REQUESTS),
            ("candidate_count", self.candidate_count, _MAX_REQUESTS),
            ("contract_count", self.contract_count, _MAX_REQUESTS),
            ("bound_request_count", self.bound_request_count, _MAX_REQUESTS),
            ("max_request_count", self.max_request_count, _MAX_REQUESTS),
            (
                "bound_source_line_reference_count",
                self.bound_source_line_reference_count,
                _MAX_SOURCE_LINE_REFERENCES,
            ),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"Request contract {name} is invalid")
        for name, value in (
            ("bound_google_request_count", self.bound_google_request_count),
            ("bound_serpapi_request_count", self.bound_serpapi_request_count),
            ("serpapi_bound_plan_credit_count", self.serpapi_bound_plan_credit_count),
            ("serpapi_plan_credit_cap", self.serpapi_plan_credit_cap),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_REQUESTS:
                raise ValueError(f"Request contract {name} is invalid")
        for name, value in (
            ("user_stated_source_line_count", self.user_stated_source_line_count),
            ("tentative_source_line_count", self.tentative_source_line_count),
            ("ai_candidate_source_line_count", self.ai_candidate_source_line_count),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_SOURCE_LINE_REFERENCES:
                raise ValueError(f"Request contract {name} is invalid")
        if (
            self.accepted_scope_item_count > self.candidate_count
            or self.candidate_count != self.contract_count
            or self.contract_count != self.bound_request_count
            or self.bound_request_count > self.max_request_count
            or self.max_request_count > self.accepted_max_request_count
            or self.bound_request_count
            != self.bound_google_request_count + self.bound_serpapi_request_count
            or self.bound_source_line_reference_count
            != self.user_stated_source_line_count
            + self.tentative_source_line_count
            + self.ai_candidate_source_line_count
            or self.serpapi_bound_plan_credit_count
            != self.bound_serpapi_request_count
            or self.serpapi_bound_plan_credit_count > self.serpapi_plan_credit_cap
        ):
            raise ValueError("Request contract materialization counts differ")
        _validate_capability_counts(
            self.bound_request_capability_counts,
            self.bound_request_count,
        )
        if (
            not isinstance(self.data_categories, tuple)
            or not self.data_categories
            or any(
                type(item) is not GuidedProviderDataCategory
                for item in self.data_categories
            )
            or tuple(sorted(set(self.data_categories), key=lambda item: item.value))
            != self.data_categories
        ):
            raise ValueError("Request contract data categories are invalid")
        if (
            type(self.estimated_bound_first_paid_tier_google_cost_usd_micros)
            is not int
            or type(self.accepted_max_first_paid_tier_google_cost_usd_micros)
            is not int
            or not 0
            <= self.estimated_bound_first_paid_tier_google_cost_usd_micros
            <= self.accepted_max_first_paid_tier_google_cost_usd_micros
            <= _MAX_GOOGLE_LIST_COST_USD_MICROS
            or type(self.host_attestation_fresh) is not bool
            or not self.host_attestation_fresh
        ):
            raise ValueError("Request contract cost/freshness is invalid")
        if (
            not isinstance(self._contracts, tuple)
            or len(self._contracts) != self.contract_count
            or any(
                type(item) is not GuidedProviderRequestContract
                for item in self._contracts
            )
            or tuple(sorted(self._contracts, key=_contract_sort_key))
            != self._contracts
            or sum(item.source_line_reference_count for item in self._contracts)
            != self.bound_source_line_reference_count
            or sum(item.user_stated_source_line_count for item in self._contracts)
            != self.user_stated_source_line_count
            or sum(item.tentative_source_line_count for item in self._contracts)
            != self.tentative_source_line_count
            or sum(item.ai_candidate_source_line_count for item in self._contracts)
            != self.ai_candidate_source_line_count
        ):
            raise ValueError("Request contract materialization items differ")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
            or self.contract_version
            != GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION
        ):
            raise ValueError("Request contract materialization metadata is invalid")

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestContractMaterializationReview("
            f"status={self.status.value!r}, "
            f"contract_count={self.contract_count!r}, "
            f"next_action={self.next_action!r})"
        )

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        """Return only safe shapes and aggregate non-sendable contract state."""

        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_request_contract_materialization": {
                "exact_prepare_materialization_response_bound": True,
                "same_exact_target_preimages_revalidated": True,
                "short_lived_execution_recheck_preserved": True,
                "exact_materialization_times_exposed": False,
                "accepted_scope_item_count": self.accepted_scope_item_count,
                "accepted_max_request_count": self.accepted_max_request_count,
                "request_contract_candidate_count": self.candidate_count,
                "materialized_request_contract_count": self.contract_count,
                "eligible_for_send_authorization_review_count": (
                    self.contract_count
                ),
                "bound_request_count": self.bound_request_count,
                "max_request_count": self.max_request_count,
                "bound_source_line_reference_count": (
                    self.bound_source_line_reference_count
                ),
                "bound_google_request_count": self.bound_google_request_count,
                "bound_serpapi_request_count": self.bound_serpapi_request_count,
                "bound_request_capability_counts": {
                    capability.value: count
                    for capability, count in self.bound_request_capability_counts
                },
                "source_state_counts": {
                    "user_stated": self.user_stated_source_line_count,
                    "tentative": self.tentative_source_line_count,
                    "ai_candidate": self.ai_candidate_source_line_count,
                },
                "all_source_lines_require_verification": True,
                "source_values_are_authoritative": False,
                "data_categories": [item.value for item in self.data_categories],
                "estimated_bound_first_paid_tier_google_cost_usd_micros": (
                    self.estimated_bound_first_paid_tier_google_cost_usd_micros
                ),
                "accepted_max_first_paid_tier_google_cost_usd_micros": (
                    self.accepted_max_first_paid_tier_google_cost_usd_micros
                ),
                "serpapi_bound_plan_credit_count": (
                    self.serpapi_bound_plan_credit_count
                ),
                "serpapi_plan_credit_cap": self.serpapi_plan_credit_cap,
                "all_bound_provider_costs_have_currency_list_rate_estimates": (
                    self.serpapi_bound_plan_credit_count == 0
                ),
                "host_attestation_fresh": self.host_attestation_fresh,
                "pricing_policy_retention_recheck_preserved": True,
                "billing_region_recheck_preserved": True,
                "credential_availability_recheck_preserved": True,
                "serpapi_plan_state_recheck_preserved": True,
                "items": [item.to_safe_dict() for item in self._contracts],
                "raw_target_preimages_retained": False,
                "exact_private_values_in_safe_output": False,
                "provider_identifier_values_exposed": False,
                "source_binding_fingerprints_exposed": False,
                "context_fingerprints_exposed": False,
                "provider_request_contracts_are_executable": False,
                "provider_request_contracts_are_sendable": False,
                "transport_endpoints_selected": False,
                "http_methods_selected": False,
                "credential_slots_bound": False,
                "credential_values_included": False,
                "http_request_count_created_by_materialization": 0,
                "send_authorization_active": False,
                "execution_authority_active": False,
                "provider_call_count_observed": 0,
                "provider_calls_permitted": False,
                "is_travel_ready": False,
                "decision_state": DecisionState.CANDIDATE.value,
                "evidence_state": EvidenceState.UNVERIFIED.value,
                "supports_authoritative_use": False,
            },
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "private_target_preimages_read": True,
                "raw_target_preimages_retained": False,
                "derived_private_request_values_retained": True,
                "private_values_in_safe_output": False,
                "environment_read": False,
                "vault_accessed": False,
                "credentials_accessed": False,
                "non_executable_provider_request_contracts_created": True,
                "http_requests_created": False,
                "provider_calls": False,
                "writes_to_trip": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }


def materialize_guided_provider_request_contracts(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    scope_response: GuidedProviderScopeResponse,
    preflight: GuidedProviderPreflight,
    preflight_response: GuidedProviderPreflightResponse,
    targets: GuidedProviderExecutionTargets,
    bindings: GuidedProviderExecutionTargetBindings,
    authorization_review: GuidedProviderExecutionAuthorizationReview,
    authorization_response: GuidedProviderExecutionAuthorizationResponse,
    execution_time_recheck: GuidedProviderExecutionTimeRecheck,
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestContractMaterialization:
    """Create exact non-sendable contracts from one fresh prepare response."""

    materialized_at = _utc_datetime(evaluation_at, "evaluation_at")
    response_review = assess_guided_provider_request_materialization_response(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
        scope_response,
        preflight,
        preflight_response,
        targets,
        bindings,
        authorization_review,
        authorization_response,
        execution_time_recheck,
        materialization_review,
        materialization_response,
        preimages=preimages,
        evaluation_at=materialized_at,
    )
    _require_prepare_response(response_review)
    contracts = _derive_contracts(
        refinement,
        evidence_plan,
        materialization_review,
        preimages,
    )
    return _new_materialization(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
        scope_response,
        preflight,
        preflight_response,
        targets,
        bindings,
        authorization_review,
        authorization_response,
        execution_time_recheck,
        materialization_review,
        materialization_response,
        response_review,
        contracts,
        materialized_at,
        execution_time_recheck._expires_at,
    )


def assess_guided_provider_request_contract_materialization(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    scope_response: GuidedProviderScopeResponse,
    preflight: GuidedProviderPreflight,
    preflight_response: GuidedProviderPreflightResponse,
    targets: GuidedProviderExecutionTargets,
    bindings: GuidedProviderExecutionTargetBindings,
    authorization_review: GuidedProviderExecutionAuthorizationReview,
    authorization_response: GuidedProviderExecutionAuthorizationResponse,
    execution_time_recheck: GuidedProviderExecutionTimeRecheck,
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    materialization: GuidedProviderRequestContractMaterialization,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestContractMaterializationReview:
    """Rebuild exact contracts and revalidate freshness at current UTC."""

    if type(materialization) is not GuidedProviderRequestContractMaterialization:
        raise TypeError("materialization must be an exact request contract bundle")
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < materialization._materialized_at:
        raise ValueError("evaluation_at cannot precede contract materialization")

    captured_response_review = (
        assess_guided_provider_request_materialization_response(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            scope_response,
            preflight,
            preflight_response,
            targets,
            bindings,
            authorization_review,
            authorization_response,
            execution_time_recheck,
            materialization_review,
            materialization_response,
            preimages=preimages,
            evaluation_at=materialization._materialized_at,
        )
    )
    _require_prepare_response(captured_response_review)
    expected_contracts = _derive_contracts(
        refinement,
        evidence_plan,
        materialization_review,
        preimages,
    )
    expected = _new_materialization(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
        scope_response,
        preflight,
        preflight_response,
        targets,
        bindings,
        authorization_review,
        authorization_response,
        execution_time_recheck,
        materialization_review,
        materialization_response,
        captured_response_review,
        expected_contracts,
        materialization._materialized_at,
        materialization._expires_at,
    )
    if materialization != expected:
        raise ValueError("Request contract materialization differs from exact context")
    if evaluated_at >= materialization._expires_at:
        raise ValueError("Request contract materialization is no longer current")

    current_response_review = assess_guided_provider_request_materialization_response(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
        scope_response,
        preflight,
        preflight_response,
        targets,
        bindings,
        authorization_review,
        authorization_response,
        execution_time_recheck,
        materialization_review,
        materialization_response,
        preimages=preimages,
        evaluation_at=evaluated_at,
    )
    _require_prepare_response(current_response_review)
    current_contracts = _derive_contracts(
        refinement,
        evidence_plan,
        materialization_review,
        preimages,
    )
    if current_contracts != materialization._contracts:
        raise ValueError("Materialized request contracts are no longer current")
    needs_verification = tuple(
        dict.fromkeys(
            (
                *current_response_review.needs_verification,
                "provider_request_contract_materialization",
            )
        )
    )
    return GuidedProviderRequestContractMaterializationReview(
        status=(
            GuidedProviderRequestContractMaterializationStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW
        ),
        next_action=_NEXT_ACTION,
        accepted_scope_item_count=(
            current_response_review.accepted_scope_item_count
        ),
        accepted_max_request_count=(
            current_response_review.accepted_max_request_count
        ),
        candidate_count=current_response_review.candidate_count,
        contract_count=len(current_contracts),
        bound_request_count=current_response_review.bound_request_count,
        max_request_count=current_response_review.max_request_count,
        bound_source_line_reference_count=(
            current_response_review.bound_source_line_reference_count
        ),
        bound_google_request_count=(
            current_response_review.bound_google_request_count
        ),
        bound_serpapi_request_count=(
            current_response_review.bound_serpapi_request_count
        ),
        user_stated_source_line_count=(
            current_response_review.user_stated_source_line_count
        ),
        tentative_source_line_count=(
            current_response_review.tentative_source_line_count
        ),
        ai_candidate_source_line_count=(
            current_response_review.ai_candidate_source_line_count
        ),
        estimated_bound_first_paid_tier_google_cost_usd_micros=(
            current_response_review
            .estimated_bound_first_paid_tier_google_cost_usd_micros
        ),
        accepted_max_first_paid_tier_google_cost_usd_micros=(
            current_response_review
            .accepted_max_first_paid_tier_google_cost_usd_micros
        ),
        serpapi_bound_plan_credit_count=(
            current_response_review.serpapi_bound_plan_credit_count
        ),
        serpapi_plan_credit_cap=current_response_review.serpapi_plan_credit_cap,
        bound_request_capability_counts=(
            current_response_review.bound_request_capability_counts
        ),
        data_categories=current_response_review.data_categories,
        host_attestation_fresh=current_response_review.host_attestation_fresh,
        tentative_fields=current_response_review.tentative_fields,
        needs_verification=needs_verification,
        _contracts=current_contracts,
        _token=_REVIEW_TOKEN,
    )


def _new_materialization(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    scope_response: GuidedProviderScopeResponse,
    preflight: GuidedProviderPreflight,
    preflight_response: GuidedProviderPreflightResponse,
    targets: GuidedProviderExecutionTargets,
    bindings: GuidedProviderExecutionTargetBindings,
    authorization_review: GuidedProviderExecutionAuthorizationReview,
    authorization_response: GuidedProviderExecutionAuthorizationResponse,
    execution_time_recheck: GuidedProviderExecutionTimeRecheck,
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    response_review: GuidedProviderRequestMaterializationResponseReview,
    contracts: tuple[GuidedProviderRequestContract, ...],
    materialized_at: datetime,
    expires_at: datetime,
) -> GuidedProviderRequestContractMaterialization:
    return GuidedProviderRequestContractMaterialization(
        _contracts=contracts,
        _materialized_at=materialized_at,
        _expires_at=expires_at,
        _context_fingerprint=_materialization_context_fingerprint(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            scope_response,
            preflight,
            preflight_response,
            targets,
            bindings,
            authorization_review,
            authorization_response,
            execution_time_recheck,
            materialization_review,
            materialization_response,
            response_review,
            contracts,
            materialized_at,
            expires_at,
        ),
        _token=_MATERIALIZATION_TOKEN,
    )


def _derive_contracts(
    refinement: GuidedRefinementCandidate,
    evidence_plan: GuidedEvidenceRequirementPlan,
    review: GuidedProviderRequestMaterializationReview,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
) -> tuple[GuidedProviderRequestContract, ...]:
    if (
        not isinstance(preimages, tuple)
        or not preimages
        or len(preimages) != review.candidate_count
        or any(
            type(item) is not GuidedProviderExecutionTargetPreimage
            for item in preimages
        )
    ):
        raise ValueError("Contract materialization requires every exact preimage")
    declarations = {
        item.source_line_index: item for item in evidence_plan.declarations
    }
    remaining = list(review._items)
    derived: list[GuidedProviderRequestContract] = []
    records = []
    for preimage in preimages:
        transmitted, identifiers, local, target_kind = _request_values(
            preimage.target
        )
        source_counts = _source_state_counts(
            refinement,
            declarations,
            preimage,
        )
        records.append(
            (
                preimage,
                transmitted,
                identifiers,
                local,
                target_kind,
                source_counts,
                _sha256(
                    {
                        "target": _target_binding_context(preimage.target),
                        "source_line_indexes": list(preimage.source_line_indexes),
                    }
                ),
            )
        )
    records.sort(key=_derived_record_sort_key)
    for (
        preimage,
        transmitted,
        identifiers,
        local,
        target_kind,
        source_counts,
        source_binding_fingerprint,
    ) in records:
        candidate_index = next(
            (
                index
                for index, candidate in enumerate(remaining)
                if _candidate_matches(
                    candidate,
                    preimage.topic,
                    target_kind,
                    transmitted,
                    identifiers,
                    local,
                    source_counts,
                )
            ),
            None,
        )
        if candidate_index is None:
            raise ValueError("Exact preimage differs from reviewed request candidate")
        candidate = remaining.pop(candidate_index)
        derived.append(
            _contract_from_candidate(
                candidate,
                transmitted,
                identifiers,
                local,
                source_binding_fingerprint,
            )
        )
    if remaining:
        raise ValueError("Reviewed request candidate lacks an exact preimage")
    return tuple(sorted(derived, key=_contract_sort_key))


def _candidate_matches(
    candidate: GuidedProviderRequestContractCandidate,
    topic: GuidedEvidenceTopic,
    target_kind: GuidedProviderExecutionTargetKind,
    transmitted: tuple[tuple[str, object], ...],
    identifiers: tuple[tuple[str, str], ...],
    local: tuple[tuple[str, object], ...],
    source_counts: tuple[int, int, int],
) -> bool:
    return (
        candidate.topic is topic
        and candidate.target_kind is target_kind
        and candidate._provider_transmitted_values == transmitted
        and candidate._local_review_context == local
        and candidate._redacted_provider_identifier_fields
        == tuple(name for name, _ in identifiers)
        and candidate.user_stated_source_line_count == source_counts[0]
        and candidate.tentative_source_line_count == source_counts[1]
        and candidate.ai_candidate_source_line_count == source_counts[2]
    )


def _contract_from_candidate(
    candidate: GuidedProviderRequestContractCandidate,
    transmitted: tuple[tuple[str, object], ...],
    identifiers: tuple[tuple[str, str], ...],
    local: tuple[tuple[str, object], ...],
    source_binding_fingerprint: str,
) -> GuidedProviderRequestContract:
    context_fingerprint = _sha256(
        {
            "contract_version": (
                GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION
            ),
            "domain": "guided-provider-request-contract",
            "candidate": _private_context_value(candidate),
            "provider_transmitted_values": _private_context_value(transmitted),
            "provider_identifier_values": _private_context_value(identifiers),
            "local_result_binding": _private_context_value(local),
            "source_binding_fingerprint": source_binding_fingerprint,
        }
    )
    return GuidedProviderRequestContract(
        topic=candidate.topic,
        capability=candidate.capability,
        request_profile=candidate.request_profile,
        materialization_kind=candidate.materialization_kind,
        pricing_profile=candidate.pricing_profile,
        policy_profile=candidate.policy_profile,
        retention_profile=candidate.retention_profile,
        target_kind=candidate.target_kind,
        source_kind=candidate.source_kind,
        source_line_reference_count=candidate.source_line_reference_count,
        user_stated_source_line_count=candidate.user_stated_source_line_count,
        tentative_source_line_count=candidate.tentative_source_line_count,
        ai_candidate_source_line_count=candidate.ai_candidate_source_line_count,
        _provider_transmitted_values=transmitted,
        _provider_identifier_values=identifiers,
        _local_result_binding=local,
        _source_binding_fingerprint=source_binding_fingerprint,
        _context_fingerprint=context_fingerprint,
        _token=_CONTRACT_TOKEN,
    )


def _request_values(
    target: object,
) -> tuple[
    tuple[tuple[str, object], ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, object], ...],
    GuidedProviderExecutionTargetKind,
]:
    transmitted: dict[str, object]
    identifiers: dict[str, str]
    local: dict[str, object]
    target_kind: GuidedProviderExecutionTargetKind
    if type(target) is PlaceIdentityIntent:
        transmitted = {
            "field_mask": GOOGLE_PLACE_IDENTITY_FIELD_MASK,
            "language_code": target.language_code,
            "region_code": target.region_code,
            "text_query": target.text_query,
        }
        if target.radius_m is not None:
            transmitted.update(
                {
                    "latitude": target.latitude,
                    "longitude": target.longitude,
                    "radius_m": target.radius_m,
                }
            )
        identifiers = {}
        local = {
            "expected_locality": target.expected_locality,
            "expected_name": target.expected_name,
            "expected_primary_types": target.expected_primary_types,
            "stable_local_location_id": target.location_id,
        }
        target_kind = (
            GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT
        )
    elif type(target) is GooglePlaceDetailsRequest:
        transmitted = {
            "field_mask": target.field_mask,
            "language_code": target.language_code,
            "region_code": target.region_code,
        }
        identifiers = {"provider_place_id": target.endpoint.provider_place_id}
        local = {
            "kind": target.kind.value,
            "stable_local_location_id": target.endpoint.location_id,
            "target_end": (
                target.target_end.isoformat()
                if target.target_end is not None
                else None
            ),
            "target_start": (
                target.target_start.isoformat()
                if target.target_start is not None
                else None
            ),
        }
        target_kind = GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT
    elif type(target) is GoogleRouteRequest:
        transmitted = {
            "departure_at": target.departure_at,
            "field_mask": target.field_mask,
            "mode": target.mode.value,
        }
        identifiers = {
            "destination_provider_place_id": (
                target.destination.provider_place_id
            ),
            "origin_provider_place_id": target.origin.provider_place_id,
        }
        local = {
            "destination_stable_local_location_id": (
                target.destination.location_id
            ),
            "fallback_from_mode": (
                target.fallback_from_mode.value
                if target.fallback_from_mode is not None
                else None
            ),
            "origin_stable_local_location_id": target.origin.location_id,
            "transit_fallback_policy": target.transit_fallback_policy.value,
        }
        target_kind = (
            GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR
        )
    elif type(target) is LodgingDiscoveryRequest:
        transmitted = {
            "adults": target.adults,
            "check_in": target.check_in.isoformat(),
            "check_out": target.check_out.isoformat(),
            "children": target.children,
            "currency": target.currency,
            "language": target.language,
            "query": target.query,
            "region": target.region,
            "rooms": target.rooms,
        }
        identifiers = {}
        local = {"currency_minor_unit": target.currency_minor_unit}
        target_kind = (
            GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT
        )
    else:
        raise TypeError("Unsupported provider request contract target")
    return (
        tuple(sorted(transmitted.items())),
        tuple(sorted(identifiers.items())),
        tuple(sorted(local.items())),
        target_kind,
    )


def _target_binding_context(target: object) -> dict[str, object]:
    if type(target) is PlaceIdentityIntent:
        return {
            "type": "place_identity_intent",
            "location_id": target.location_id,
            "text_query": target.text_query,
            "expected_name": target.expected_name,
            "region_code": target.region_code,
            "language_code": target.language_code,
            "expected_locality": target.expected_locality,
            "expected_primary_types": list(target.expected_primary_types),
            "latitude": target.latitude,
            "longitude": target.longitude,
            "radius_m": target.radius_m,
            "intent_id": target.intent_id,
        }
    if type(target) is LodgingDiscoveryRequest:
        return {
            "type": "lodging_discovery_request",
            "query": target.query,
            "check_in": target.check_in.isoformat(),
            "check_out": target.check_out.isoformat(),
            "adults": target.adults,
            "children": target.children,
            "rooms": target.rooms,
            "currency": target.currency,
            "currency_minor_unit": target.currency_minor_unit,
            "region": target.region,
            "language": target.language,
            "request_id": target.request_id,
        }
    if type(target) in {GooglePlaceDetailsRequest, GoogleRouteRequest}:
        return {
            "type": type(target).__name__,
            "request": target.to_binding_dict(),
            "snapshot": target.snapshot.to_dict(),
        }
    raise TypeError("Unsupported provider request contract target")


def _require_prepare_response(
    review: GuidedProviderRequestMaterializationResponseReview,
) -> None:
    if (
        review.status
        is not GuidedProviderRequestMaterializationResponseStatus
        .READY_FOR_PRIVATE_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION
    ):
        raise ValueError("Request contracts require a fresh prepare response")


def _materialization_context_fingerprint(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    scope_response: GuidedProviderScopeResponse,
    preflight: GuidedProviderPreflight,
    preflight_response: GuidedProviderPreflightResponse,
    targets: GuidedProviderExecutionTargets,
    bindings: GuidedProviderExecutionTargetBindings,
    authorization_review: GuidedProviderExecutionAuthorizationReview,
    authorization_response: GuidedProviderExecutionAuthorizationResponse,
    execution_time_recheck: GuidedProviderExecutionTimeRecheck,
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    response_review: GuidedProviderRequestMaterializationResponseReview,
    contracts: tuple[GuidedProviderRequestContract, ...],
    materialized_at: datetime,
    expires_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": (
                GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION
            ),
            "materialization_response_contract_version": (
                GUIDED_PROVIDER_REQUEST_MATERIALIZATION_RESPONSE_VERSION
            ),
            "domain": "guided-provider-request-contract-materialization",
            "brief": _private_context_value(brief),
            "cards": [
                _private_context_value(card)
                for card in sorted(cards, key=lambda item: item.card_ref)
            ],
            "preference": _private_context_value(preference),
            "refinement": _private_context_value(refinement),
            "refinement_response": _private_context_value(refinement_response),
            "itinerary_candidate": _private_context_value(itinerary_candidate),
            "itinerary_response": _private_context_value(itinerary_response),
            "evidence_plan": _private_context_value(evidence_plan),
            "provider_scope": _private_context_value(provider_scope),
            "provider_scope_response": _private_context_value(scope_response),
            "provider_preflight": _private_context_value(preflight),
            "provider_preflight_response": _private_context_value(
                preflight_response
            ),
            "provider_execution_targets": _private_context_value(targets),
            "provider_execution_target_bindings": _private_context_value(
                bindings
            ),
            "provider_execution_authorization_review": (
                _private_context_value(authorization_review)
            ),
            "provider_execution_authorization_response": (
                _private_context_value(authorization_response)
            ),
            "provider_execution_time_recheck": _private_context_value(
                execution_time_recheck
            ),
            "provider_request_materialization_review": _private_context_value(
                materialization_review
            ),
            "provider_request_materialization_response": _private_context_value(
                materialization_response
            ),
            "provider_request_materialization_response_review": (
                _private_context_value(response_review)
            ),
            "provider_request_contracts": _private_context_value(contracts),
            "materialized_at": _private_context_value(materialized_at),
            "expires_at": _private_context_value(expires_at),
        }
    )


def _derived_record_sort_key(record: tuple[object, ...]) -> tuple[str, str]:
    preimage = record[0]
    return (preimage.topic.value, record[6])


def _contract_sort_key(
    contract: GuidedProviderRequestContract,
) -> tuple[str, str, str, str]:
    return (
        contract.topic.value,
        contract.capability.value,
        contract.request_profile.value,
        contract._context_fingerprint,
    )


def _private_field_names(
    values: tuple[tuple[str, object], ...],
    name: str,
    *,
    require_string_values: bool = False,
) -> set[str]:
    if (
        not isinstance(values, tuple)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or type(item[0]) is not str
            or not item[0]
            or len(item[0]) > 64
            or (
                require_string_values
                and (type(item[1]) is not str or not item[1] or len(item[1]) > 512)
            )
            for item in values
        )
        or tuple(sorted(values, key=lambda item: item[0])) != values
        or len({item[0] for item in values}) != len(values)
    ):
        raise ValueError(f"{name} is invalid")
    return {item[0] for item in values}


def _validate_capability_counts(
    values: tuple[tuple[GuidedProviderCapability, int], ...],
    expected_total: int,
) -> None:
    if (
        not isinstance(values, tuple)
        or len(values) != len(GuidedProviderCapability)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or type(item[0]) is not GuidedProviderCapability
            or type(item[1]) is not int
            or not 0 <= item[1] <= _MAX_REQUESTS
            for item in values
        )
        or tuple(sorted(values, key=lambda item: item[0].value)) != values
        or {item[0] for item in values} != set(GuidedProviderCapability)
        or sum(item[1] for item in values) != expected_total
    ):
        raise ValueError("Request contract capability counts are invalid")


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _utc_datetime(value: datetime, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{name} must be an exact UTC datetime")
    return value


__all__ = [
    "GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION",
    "GuidedProviderRequestContract",
    "GuidedProviderRequestContractMaterialization",
    "GuidedProviderRequestContractMaterializationReview",
    "GuidedProviderRequestContractMaterializationStatus",
    "assess_guided_provider_request_contract_materialization",
    "materialize_guided_provider_request_contracts",
]
