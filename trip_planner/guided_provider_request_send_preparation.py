"""Bind accepted private provider contracts to allowlisted transport metadata.

Phase 5.25 consumes only one fresh Phase 5.24 ``accept_send`` response, the
same complete private planning chain, the same typed target preimages, and the
same materialized request contracts.  It binds each exact process-local
contract to a fixed transport profile, public endpoint template, HTTP method,
provider field placement, and credential slot name.

The resulting bundle remains deliberately non-sendable.  This module never
reads a credential value or the environment, constructs an HTTP request,
opens a network connection, calls a provider, reserves spend, persists data,
schedules, renders, deploys, confirms, or mutates canonical state.  Credential
binding and any live send require a separate explicit gate.
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
)
from .guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetBindings,
    GuidedProviderExecutionTargetPreimage,
)
from .guided_provider_execution_targets import GuidedProviderExecutionTargets
from .guided_provider_execution_time_recheck import (
    GuidedProviderExecutionTimeRecheck,
)
from .guided_provider_preflight import GuidedProviderPreflight
from .guided_provider_preflight_response import GuidedProviderPreflightResponse
from .guided_provider_request_contract_materialization import (
    GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION,
    GuidedProviderRequestContract,
    GuidedProviderRequestContractMaterialization,
    GuidedProviderRequestContractMaterializationReview,
    assess_guided_provider_request_contract_materialization,
)
from .guided_provider_request_materialization_response import (
    GuidedProviderRequestMaterializationResponse,
)
from .guided_provider_request_materialization_review import (
    GuidedProviderRequestMaterializationKind,
    GuidedProviderRequestMaterializationReview,
)
from .guided_provider_request_send_authorization_response import (
    GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_RESPONSE_VERSION,
    GuidedProviderRequestSendAuthorizationResponse,
    GuidedProviderRequestSendAuthorizationResponseKind,
    GuidedProviderRequestSendAuthorizationResponseReview,
    GuidedProviderRequestSendAuthorizationResponseStatus,
    assess_guided_provider_request_send_authorization_response,
)
from .guided_provider_request_send_authorization_review import (
    GuidedProviderRequestSendAuthorizationReview,
)
from .guided_provider_scope import (
    GuidedProviderCapability,
    GuidedProviderScopeProposal,
)
from .guided_provider_scope_response import GuidedProviderScopeResponse
from .guided_refinement import GuidedRefinementCandidate, GuidedRefinementResponse
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION = (
    "guided-provider-request-send-preparation/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_REQUESTS = 32
_NEXT_ACTION = "prepare_private_provider_request_credential_binding_review"
_BINDING_TOKEN = object()
_PREPARATION_TOKEN = object()
_REVIEW_TOKEN = object()


class GuidedProviderRequestTransportProfile(str, Enum):
    """One versioned, allowlisted provider transport surface."""

    GOOGLE_PLACES_TEXT_SEARCH_V1 = "google_places_text_search_v1"
    GOOGLE_PLACE_DETAILS_V1 = "google_place_details_v1"
    GOOGLE_ROUTES_COMPUTE_ROUTES_V2 = "google_routes_compute_routes_v2"
    SERPAPI_GOOGLE_HOTELS_V1 = "serpapi_google_hotels_v1"


class GuidedProviderRequestHTTPMethod(str, Enum):
    """Allowlisted HTTP method metadata, not an executable request."""

    GET = "GET"
    POST = "POST"


class GuidedProviderRequestValuePlacement(str, Enum):
    """Public destination of a future provider field or credential."""

    HEADER = "header"
    JSON_BODY = "json_body"
    QUERY_PARAMETER = "query_parameter"
    URL_PATH = "url_path"


class GuidedProviderRequestCredentialSlot(str, Enum):
    """Credential identity and placement without a credential value."""

    GOOGLE_MAPS_API_KEY_HEADER = "google_maps_api_key_header"
    SERPAPI_API_KEY_QUERY_PARAMETER = "serpapi_api_key_query_parameter"


class GuidedProviderRequestSendPreparationStatus(str, Enum):
    """A transport-bound bundle may only enter credential-binding review."""

    READY_FOR_PRIVATE_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW = (
        "ready_for_private_provider_request_credential_binding_review"
    )


_FieldBinding = tuple[
    str,
    str,
    GuidedProviderRequestValuePlacement,
]
_FixedParameter = tuple[
    str,
    str,
    GuidedProviderRequestValuePlacement,
]


@dataclass(frozen=True, slots=True)
class _TransportSpec:
    profile: GuidedProviderRequestTransportProfile
    endpoint_template: str
    http_method: GuidedProviderRequestHTTPMethod
    credential_slot: GuidedProviderRequestCredentialSlot
    credential_provider_field_name: str
    credential_placement: GuidedProviderRequestValuePlacement
    transmitted_field_bindings: tuple[_FieldBinding, ...]
    provider_identifier_field_bindings: tuple[_FieldBinding, ...]
    fixed_parameters: tuple[_FixedParameter, ...] = ()


_TRANSPORT_SPECS = {
    GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH: (
        _TransportSpec(
            profile=(
                GuidedProviderRequestTransportProfile
                .GOOGLE_PLACES_TEXT_SEARCH_V1
            ),
            endpoint_template=(
                "https://places.googleapis.com/v1/places:searchText"
            ),
            http_method=GuidedProviderRequestHTTPMethod.POST,
            credential_slot=(
                GuidedProviderRequestCredentialSlot.GOOGLE_MAPS_API_KEY_HEADER
            ),
            credential_provider_field_name="X-Goog-Api-Key",
            credential_placement=GuidedProviderRequestValuePlacement.HEADER,
            transmitted_field_bindings=(
                (
                    "field_mask",
                    "X-Goog-FieldMask",
                    GuidedProviderRequestValuePlacement.HEADER,
                ),
                (
                    "language_code",
                    "languageCode",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
                (
                    "latitude",
                    "locationBias.circle.center.latitude",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
                (
                    "longitude",
                    "locationBias.circle.center.longitude",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
                (
                    "radius_m",
                    "locationBias.circle.radius",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
                (
                    "region_code",
                    "regionCode",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
                (
                    "text_query",
                    "textQuery",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
            ),
            provider_identifier_field_bindings=(),
        )
    ),
    GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS: (
        _TransportSpec(
            profile=GuidedProviderRequestTransportProfile.GOOGLE_PLACE_DETAILS_V1,
            endpoint_template=(
                "https://places.googleapis.com/v1/places/{provider_place_id}"
            ),
            http_method=GuidedProviderRequestHTTPMethod.GET,
            credential_slot=(
                GuidedProviderRequestCredentialSlot.GOOGLE_MAPS_API_KEY_HEADER
            ),
            credential_provider_field_name="X-Goog-Api-Key",
            credential_placement=GuidedProviderRequestValuePlacement.HEADER,
            transmitted_field_bindings=(
                (
                    "field_mask",
                    "X-Goog-FieldMask",
                    GuidedProviderRequestValuePlacement.HEADER,
                ),
                (
                    "language_code",
                    "languageCode",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "region_code",
                    "regionCode",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
            ),
            provider_identifier_field_bindings=(
                (
                    "provider_place_id",
                    "provider_place_id",
                    GuidedProviderRequestValuePlacement.URL_PATH,
                ),
            ),
        )
    ),
    GuidedProviderRequestMaterializationKind.GOOGLE_ROUTES_COMPUTE_ROUTES: (
        _TransportSpec(
            profile=(
                GuidedProviderRequestTransportProfile
                .GOOGLE_ROUTES_COMPUTE_ROUTES_V2
            ),
            endpoint_template=(
                "https://routes.googleapis.com/directions/v2:computeRoutes"
            ),
            http_method=GuidedProviderRequestHTTPMethod.POST,
            credential_slot=(
                GuidedProviderRequestCredentialSlot.GOOGLE_MAPS_API_KEY_HEADER
            ),
            credential_provider_field_name="X-Goog-Api-Key",
            credential_placement=GuidedProviderRequestValuePlacement.HEADER,
            transmitted_field_bindings=(
                (
                    "departure_at",
                    "departureTime",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
                (
                    "field_mask",
                    "X-Goog-FieldMask",
                    GuidedProviderRequestValuePlacement.HEADER,
                ),
                (
                    "mode",
                    "travelMode",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
            ),
            provider_identifier_field_bindings=(
                (
                    "destination_provider_place_id",
                    "destination.placeId",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
                (
                    "origin_provider_place_id",
                    "origin.placeId",
                    GuidedProviderRequestValuePlacement.JSON_BODY,
                ),
            ),
        )
    ),
    GuidedProviderRequestMaterializationKind.SERPAPI_GOOGLE_HOTELS: (
        _TransportSpec(
            profile=GuidedProviderRequestTransportProfile.SERPAPI_GOOGLE_HOTELS_V1,
            endpoint_template="https://serpapi.com/search.json",
            http_method=GuidedProviderRequestHTTPMethod.GET,
            credential_slot=(
                GuidedProviderRequestCredentialSlot
                .SERPAPI_API_KEY_QUERY_PARAMETER
            ),
            credential_provider_field_name="api_key",
            credential_placement=(
                GuidedProviderRequestValuePlacement.QUERY_PARAMETER
            ),
            transmitted_field_bindings=(
                (
                    "adults",
                    "adults",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "check_in",
                    "check_in_date",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "check_out",
                    "check_out_date",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "children",
                    "children",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "currency",
                    "currency",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "language",
                    "hl",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "query",
                    "q",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "region",
                    "gl",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
                (
                    "rooms",
                    "rooms",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
            ),
            provider_identifier_field_bindings=(),
            fixed_parameters=(
                (
                    "engine",
                    "google_hotels",
                    GuidedProviderRequestValuePlacement.QUERY_PARAMETER,
                ),
            ),
        )
    ),
}


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestTransportBinding:
    """One exact contract bound to public transport metadata only."""

    transport_profile: GuidedProviderRequestTransportProfile
    endpoint_template: str
    http_method: GuidedProviderRequestHTTPMethod
    credential_slot: GuidedProviderRequestCredentialSlot
    credential_provider_field_name: str
    credential_placement: GuidedProviderRequestValuePlacement
    _transmitted_field_bindings: tuple[_FieldBinding, ...] = field(repr=False)
    _provider_identifier_field_bindings: tuple[_FieldBinding, ...] = field(
        repr=False
    )
    _fixed_parameters: tuple[_FixedParameter, ...] = field(repr=False)
    _contract: GuidedProviderRequestContract = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _BINDING_TOKEN:
            raise ValueError("Transport bindings require trusted preparation")
        if type(self._contract) is not GuidedProviderRequestContract:
            raise TypeError("Transport binding requires an exact request contract")
        spec = _TRANSPORT_SPECS[self._contract.materialization_kind]
        exact_enums = (
            (self.transport_profile, GuidedProviderRequestTransportProfile),
            (self.http_method, GuidedProviderRequestHTTPMethod),
            (self.credential_slot, GuidedProviderRequestCredentialSlot),
            (self.credential_placement, GuidedProviderRequestValuePlacement),
        )
        if any(type(value) is not expected for value, expected in exact_enums):
            raise TypeError("Transport binding enums must be exact")
        expected_transmitted = _bindings_for_names(
            spec.transmitted_field_bindings,
            {name for name, _ in self._contract._provider_transmitted_values},
            "transmitted",
        )
        expected_identifiers = _bindings_for_names(
            spec.provider_identifier_field_bindings,
            {name for name, _ in self._contract._provider_identifier_values},
            "provider identifier",
        )
        if (
            self.transport_profile is not spec.profile
            or self.endpoint_template != spec.endpoint_template
            or self.http_method is not spec.http_method
            or self.credential_slot is not spec.credential_slot
            or self.credential_provider_field_name
            != spec.credential_provider_field_name
            or self.credential_placement is not spec.credential_placement
            or self._transmitted_field_bindings != expected_transmitted
            or self._provider_identifier_field_bindings != expected_identifiers
            or self._fixed_parameters != spec.fixed_parameters
        ):
            raise ValueError("Transport binding differs from the allowlisted profile")
        _digest(self._context_fingerprint, "context_fingerprint")

    @property
    def topic(self) -> GuidedEvidenceTopic:
        return self._contract.topic

    @property
    def capability(self) -> GuidedProviderCapability:
        return self._contract.capability

    @property
    def materialization_kind(self) -> GuidedProviderRequestMaterializationKind:
        return self._contract.materialization_kind

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestTransportBinding("
            f"topic={self.topic.value!r}, "
            f"transport_profile={self.transport_profile.value!r})"
        )

    def to_safe_dict(self) -> dict[str, object]:
        """Expose public transport shape without any exact contract value."""

        return {
            "topic": self.topic.value,
            "capability": self.capability.value,
            "materialization_kind": self.materialization_kind.value,
            "transport_profile": self.transport_profile.value,
            "endpoint_template": self.endpoint_template,
            "http_method": self.http_method.value,
            "credential": {
                "slot": self.credential_slot.value,
                "provider_field_name": self.credential_provider_field_name,
                "placement": self.credential_placement.value,
                "slot_bound": True,
                "value_bound": False,
                "value_included": False,
            },
            "provider_transmitted_field_bindings": [
                {
                    "contract_field_name": contract_name,
                    "provider_field_name": provider_name,
                    "placement": placement.value,
                }
                for contract_name, provider_name, placement
                in self._transmitted_field_bindings
            ],
            "provider_identifier_field_bindings": [
                {
                    "contract_field_name": contract_name,
                    "provider_field_name": provider_name,
                    "placement": placement.value,
                }
                for contract_name, provider_name, placement
                in self._provider_identifier_field_bindings
            ],
            "fixed_public_parameters": [
                {
                    "provider_field_name": name,
                    "value": value,
                    "placement": placement.value,
                }
                for name, value, placement in self._fixed_parameters
            ],
            "exact_private_contract_retained_process_locally": True,
            "exact_private_values_included": False,
            "provider_identifier_values_included": False,
            "context_fingerprint_exposed": False,
            "transport_endpoint_selected": True,
            "http_method_selected": True,
            "credential_slot_bound": True,
            "credential_value_bound": False,
            "credential_value_included": False,
            "provider_request_contract_is_executable": False,
            "provider_request_contract_is_sendable": False,
            "http_request_created": False,
            "provider_call_permitted": False,
            "decision_state": DecisionState.CANDIDATE.value,
            "evidence_state": EvidenceState.UNVERIFIED.value,
            "supports_authoritative_use": False,
        }


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestSendPreparation:
    """Short-lived process-local transport bindings with no send ability."""

    _bindings: tuple[GuidedProviderRequestTransportBinding, ...] = field(
        repr=False
    )
    _prepared_at: datetime = field(repr=False)
    _expires_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PREPARATION_TOKEN:
            raise ValueError("Send preparations require trusted preparation")
        if (
            not isinstance(self._bindings, tuple)
            or not 1 <= len(self._bindings) <= _MAX_REQUESTS
            or any(
                type(item) is not GuidedProviderRequestTransportBinding
                for item in self._bindings
            )
            or tuple(sorted(self._bindings, key=_binding_sort_key))
            != self._bindings
        ):
            raise ValueError("Send-preparation transport bindings are invalid")
        prepared = _utc_datetime(self._prepared_at, "prepared_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not prepared < expires <= prepared + timedelta(minutes=5):
            raise ValueError("Send-preparation expiry is invalid")
        _digest(self._context_fingerprint, "context_fingerprint")
        object.__setattr__(self, "_prepared_at", prepared)
        object.__setattr__(self, "_expires_at", expires)

    @property
    def binding_count(self) -> int:
        return len(self._bindings)

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestSendPreparation("
            f"binding_count={self.binding_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestSendPreparationReview:
    """Safe assessment of a non-sendable transport-bound bundle."""

    status: GuidedProviderRequestSendPreparationStatus
    next_action: str
    transport_profile_counts: tuple[
        tuple[GuidedProviderRequestTransportProfile, int], ...
    ]
    _send_authorization_response_review: (
        GuidedProviderRequestSendAuthorizationResponseReview
    ) = field(repr=False)
    _bindings: tuple[GuidedProviderRequestTransportBinding, ...] = field(
        repr=False
    )
    contract_version: str = GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Send-preparation reviews require the assessor")
        source = self._send_authorization_response_review
        if (
            type(self.status) is not GuidedProviderRequestSendPreparationStatus
            or self.status
            is not GuidedProviderRequestSendPreparationStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW
            or self.next_action != _NEXT_ACTION
            or type(source)
            is not GuidedProviderRequestSendAuthorizationResponseReview
        ):
            raise ValueError("Send-preparation review status/source differs")
        _require_accept_response(source)
        if (
            not isinstance(self._bindings, tuple)
            or len(self._bindings) != source.contract_count
            or any(
                type(item) is not GuidedProviderRequestTransportBinding
                for item in self._bindings
            )
            or tuple(sorted(self._bindings, key=_binding_sort_key))
            != self._bindings
        ):
            raise ValueError("Send-preparation review bindings differ")
        _validate_profile_counts(
            self.transport_profile_counts,
            source.contract_count,
        )
        derived_counts = tuple(
            (profile, sum(item.transport_profile is profile for item in self._bindings))
            for profile in sorted(
                GuidedProviderRequestTransportProfile,
                key=lambda item: item.value,
            )
        )
        if self.transport_profile_counts != derived_counts:
            raise ValueError("Send-preparation transport profile counts differ")
        if self.contract_version != GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION:
            raise ValueError("Send-preparation contract version differs")

    @property
    def binding_count(self) -> int:
        return len(self._bindings)

    @property
    def contract_count(self) -> int:
        return self._send_authorization_response_review.contract_count

    @property
    def accepted_scope_item_count(self) -> int:
        return (
            self._send_authorization_response_review.accepted_scope_item_count
        )

    @property
    def accepted_max_request_count(self) -> int:
        return (
            self._send_authorization_response_review.accepted_max_request_count
        )

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    @property
    def tentative_fields(self) -> tuple[str, ...]:
        return self._send_authorization_response_review.tentative_fields

    @property
    def needs_verification(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self._send_authorization_response_review.needs_verification,
                    "provider_request_send_preparation",
                )
            )
        )

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestSendPreparationReview("
            f"status={self.status.value!r}, "
            f"binding_count={self.binding_count!r}, "
            f"next_action={self.next_action!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return public transport shapes and aggregate safety state only."""

        source = self._send_authorization_response_review
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_request_send_preparation": {
                "exact_accept_send_response_bound": True,
                "same_exact_target_preimages_revalidated": True,
                "same_exact_materialized_contracts_revalidated": True,
                "short_lived_execution_recheck_preserved": True,
                "exact_preparation_times_exposed": False,
                "accepted_scope_item_count": source.accepted_scope_item_count,
                "accepted_max_request_count": (
                    source.accepted_max_request_count
                ),
                "materialized_request_contract_count": source.contract_count,
                "transport_binding_count": self.binding_count,
                "eligible_for_credential_binding_review_count": (
                    self.binding_count
                ),
                "bound_request_count": source.bound_request_count,
                "max_request_count": source.max_request_count,
                "bound_google_request_count": source.bound_google_request_count,
                "bound_serpapi_request_count": (
                    source.bound_serpapi_request_count
                ),
                "bound_request_capability_counts": {
                    capability.value: count
                    for capability, count
                    in source.bound_request_capability_counts
                },
                "transport_profile_counts": {
                    profile.value: count
                    for profile, count in self.transport_profile_counts
                },
                "http_method_counts": {
                    method.value: sum(
                        item.http_method is method for item in self._bindings
                    )
                    for method in GuidedProviderRequestHTTPMethod
                },
                "source_state_counts": {
                    "user_stated": source.user_stated_source_line_count,
                    "tentative": source.tentative_source_line_count,
                    "ai_candidate": source.ai_candidate_source_line_count,
                },
                "all_source_lines_require_verification": True,
                "source_values_are_authoritative": False,
                "data_categories": [item.value for item in source.data_categories],
                "estimated_bound_first_paid_tier_google_cost_usd_micros": (
                    source.estimated_bound_first_paid_tier_google_cost_usd_micros
                ),
                "accepted_max_first_paid_tier_google_cost_usd_micros": (
                    source.accepted_max_first_paid_tier_google_cost_usd_micros
                ),
                "serpapi_bound_plan_credit_count": (
                    source.serpapi_bound_plan_credit_count
                ),
                "serpapi_plan_credit_cap": source.serpapi_plan_credit_cap,
                "host_attestation_fresh": source.host_attestation_fresh,
                "pricing_policy_retention_recheck_preserved": True,
                "billing_region_recheck_preserved": True,
                "credential_availability_recheck_preserved": True,
                "serpapi_plan_state_recheck_preserved": True,
                "items": [item.to_safe_dict() for item in self._bindings],
                "allowlisted_transport_profiles_bound": True,
                "transport_endpoints_selected": True,
                "http_methods_selected": True,
                "provider_field_placements_selected": True,
                "credential_slots_bound": True,
                "credential_values_bound": False,
                "credential_values_included": False,
                "environment_read": False,
                "vault_accessed": False,
                "network_accessed": False,
                "provider_request_contracts_are_executable": False,
                "provider_request_contracts_are_sendable": False,
                "http_request_count_created_by_preparation": 0,
                "immediate_send_authority_granted": False,
                "send_authority_active": False,
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
                "materialized_private_contracts_read": True,
                "public_transport_metadata_bound": True,
                "private_values_in_safe_output": False,
                "environment_read": False,
                "vault_accessed": False,
                "credentials_accessed": False,
                "credential_values_bound": False,
                "http_requests_created": False,
                "network_accessed": False,
                "send_authority_granted": False,
                "provider_calls": False,
                "writes_to_trip": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }


def prepare_guided_provider_request_send_preparation(
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
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestSendPreparation:
    """Bind one fresh accepted exact contract bundle to transport metadata."""

    prepared_at = _utc_datetime(evaluation_at, "evaluation_at")
    response_review = assess_guided_provider_request_send_authorization_response(
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
        materialization,
        send_authorization_review,
        send_authorization_response,
        preimages=preimages,
        evaluation_at=prepared_at,
    )
    _require_accept_response(response_review)
    contract_review = assess_guided_provider_request_contract_materialization(
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
        materialization,
        preimages=preimages,
        evaluation_at=prepared_at,
    )
    _require_same_source(response_review, contract_review)
    transport_bindings = _derive_bindings(contract_review._contracts)
    expires_at = min(
        materialization._expires_at,
        send_authorization_review._expires_at,
    )
    return _new_preparation(
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
        materialization,
        send_authorization_review,
        send_authorization_response,
        response_review,
        contract_review,
        transport_bindings,
        prepared_at,
        expires_at,
    )


def assess_guided_provider_request_send_preparation(
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
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    preparation: GuidedProviderRequestSendPreparation,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestSendPreparationReview:
    """Rebuild transport bindings and revalidate freshness at current UTC."""

    if type(preparation) is not GuidedProviderRequestSendPreparation:
        raise TypeError("preparation must be an exact send preparation")
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < preparation._prepared_at:
        raise ValueError("evaluation_at cannot precede send preparation")
    if evaluated_at >= preparation._expires_at:
        raise ValueError("Send preparation has expired")

    captured_response_review = (
        assess_guided_provider_request_send_authorization_response(
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
            materialization,
            send_authorization_review,
            send_authorization_response,
            preimages=preimages,
            evaluation_at=preparation._prepared_at,
        )
    )
    _require_accept_response(captured_response_review)
    captured_contract_review = (
        assess_guided_provider_request_contract_materialization(
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
            materialization,
            preimages=preimages,
            evaluation_at=preparation._prepared_at,
        )
    )
    _require_same_source(captured_response_review, captured_contract_review)
    captured_bindings = _derive_bindings(captured_contract_review._contracts)
    expected = _new_preparation(
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
        materialization,
        send_authorization_review,
        send_authorization_response,
        captured_response_review,
        captured_contract_review,
        captured_bindings,
        preparation._prepared_at,
        min(materialization._expires_at, send_authorization_review._expires_at),
    )
    if preparation != expected:
        raise ValueError("Send preparation differs from the exact accepted context")

    current_response_review = (
        assess_guided_provider_request_send_authorization_response(
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
            materialization,
            send_authorization_review,
            send_authorization_response,
            preimages=preimages,
            evaluation_at=evaluated_at,
        )
    )
    _require_accept_response(current_response_review)
    current_contract_review = (
        assess_guided_provider_request_contract_materialization(
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
            materialization,
            preimages=preimages,
            evaluation_at=evaluated_at,
        )
    )
    _require_same_source(current_response_review, current_contract_review)
    current_bindings = _derive_bindings(current_contract_review._contracts)
    if current_bindings != preparation._bindings:
        raise ValueError("Send-preparation transport bindings have drifted")
    profile_counts = tuple(
        (profile, sum(item.transport_profile is profile for item in current_bindings))
        for profile in sorted(
            GuidedProviderRequestTransportProfile,
            key=lambda item: item.value,
        )
    )
    return GuidedProviderRequestSendPreparationReview(
        status=(
            GuidedProviderRequestSendPreparationStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW
        ),
        next_action=_NEXT_ACTION,
        transport_profile_counts=profile_counts,
        _send_authorization_response_review=current_response_review,
        _bindings=current_bindings,
        _token=_REVIEW_TOKEN,
    )


def _new_preparation(
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
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    response_review: GuidedProviderRequestSendAuthorizationResponseReview,
    contract_review: GuidedProviderRequestContractMaterializationReview,
    transport_bindings: tuple[GuidedProviderRequestTransportBinding, ...],
    prepared_at: datetime,
    expires_at: datetime,
) -> GuidedProviderRequestSendPreparation:
    return GuidedProviderRequestSendPreparation(
        _bindings=transport_bindings,
        _prepared_at=prepared_at,
        _expires_at=expires_at,
        _context_fingerprint=_preparation_context_fingerprint(
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
            materialization,
            send_authorization_review,
            send_authorization_response,
            response_review,
            contract_review,
            transport_bindings,
            prepared_at,
            expires_at,
        ),
        _token=_PREPARATION_TOKEN,
    )


def _derive_bindings(
    contracts: tuple[GuidedProviderRequestContract, ...],
) -> tuple[GuidedProviderRequestTransportBinding, ...]:
    if (
        not isinstance(contracts, tuple)
        or not 1 <= len(contracts) <= _MAX_REQUESTS
        or any(type(item) is not GuidedProviderRequestContract for item in contracts)
    ):
        raise ValueError("Exact materialized contracts are required")
    return tuple(
        sorted(
            (_binding_from_contract(contract) for contract in contracts),
            key=_binding_sort_key,
        )
    )


def _binding_from_contract(
    contract: GuidedProviderRequestContract,
) -> GuidedProviderRequestTransportBinding:
    spec = _TRANSPORT_SPECS[contract.materialization_kind]
    transmitted = _bindings_for_names(
        spec.transmitted_field_bindings,
        {name for name, _ in contract._provider_transmitted_values},
        "transmitted",
    )
    identifiers = _bindings_for_names(
        spec.provider_identifier_field_bindings,
        {name for name, _ in contract._provider_identifier_values},
        "provider identifier",
    )
    context_fingerprint = _sha256(
        {
            "contract_version": GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION,
            "domain": "guided-provider-request-transport-binding",
            "request_contract": _private_context_value(contract),
            "transport_profile": spec.profile.value,
            "endpoint_template": spec.endpoint_template,
            "http_method": spec.http_method.value,
            "credential_slot": spec.credential_slot.value,
            "credential_provider_field_name": (
                spec.credential_provider_field_name
            ),
            "credential_placement": spec.credential_placement.value,
            "transmitted_field_bindings": _private_context_value(transmitted),
            "provider_identifier_field_bindings": (
                _private_context_value(identifiers)
            ),
            "fixed_parameters": _private_context_value(spec.fixed_parameters),
        }
    )
    return GuidedProviderRequestTransportBinding(
        transport_profile=spec.profile,
        endpoint_template=spec.endpoint_template,
        http_method=spec.http_method,
        credential_slot=spec.credential_slot,
        credential_provider_field_name=spec.credential_provider_field_name,
        credential_placement=spec.credential_placement,
        _transmitted_field_bindings=transmitted,
        _provider_identifier_field_bindings=identifiers,
        _fixed_parameters=spec.fixed_parameters,
        _contract=contract,
        _context_fingerprint=context_fingerprint,
        _token=_BINDING_TOKEN,
    )


def _bindings_for_names(
    candidates: tuple[_FieldBinding, ...],
    required_names: set[str],
    label: str,
) -> tuple[_FieldBinding, ...]:
    selected = tuple(item for item in candidates if item[0] in required_names)
    if {item[0] for item in selected} != required_names:
        raise ValueError(f"Transport profile does not cover {label} fields")
    return selected


def _require_accept_response(
    review: GuidedProviderRequestSendAuthorizationResponseReview,
) -> None:
    if (
        type(review) is not GuidedProviderRequestSendAuthorizationResponseReview
        or review.status
        is not GuidedProviderRequestSendAuthorizationResponseStatus
        .READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_PREPARATION
        or review.response_kind
        is not GuidedProviderRequestSendAuthorizationResponseKind.ACCEPT_SEND
        or review.next_action
        != "prepare_private_provider_request_send_preparation"
    ):
        raise ValueError("Send preparation requires one exact accept_send response")


def _require_same_source(
    response: GuidedProviderRequestSendAuthorizationResponseReview,
    contracts: GuidedProviderRequestContractMaterializationReview,
) -> None:
    comparable_fields = (
        "accepted_scope_item_count",
        "accepted_max_request_count",
        "contract_count",
        "bound_request_count",
        "max_request_count",
        "bound_source_line_reference_count",
        "bound_google_request_count",
        "bound_serpapi_request_count",
        "user_stated_source_line_count",
        "tentative_source_line_count",
        "ai_candidate_source_line_count",
        "estimated_bound_first_paid_tier_google_cost_usd_micros",
        "accepted_max_first_paid_tier_google_cost_usd_micros",
        "serpapi_bound_plan_credit_count",
        "serpapi_plan_credit_cap",
        "bound_request_capability_counts",
        "data_categories",
        "host_attestation_fresh",
    )
    if any(
        getattr(response, name) != getattr(contracts, name)
        for name in comparable_fields
    ):
        raise ValueError("Accepted response and materialized contracts differ")


def _preparation_context_fingerprint(
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
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    response_review: GuidedProviderRequestSendAuthorizationResponseReview,
    contract_review: GuidedProviderRequestContractMaterializationReview,
    transport_bindings: tuple[GuidedProviderRequestTransportBinding, ...],
    prepared_at: datetime,
    expires_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION,
            "send_authorization_response_contract_version": (
                GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_RESPONSE_VERSION
            ),
            "materialization_contract_version": (
                GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION
            ),
            "domain": "guided-provider-request-send-preparation",
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
            "provider_execution_authorization_review": _private_context_value(
                authorization_review
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
            "provider_request_materialization_response": (
                _private_context_value(materialization_response)
            ),
            "provider_request_contract_materialization": (
                _private_context_value(materialization)
            ),
            "provider_request_send_authorization_review": (
                _private_context_value(send_authorization_review)
            ),
            "provider_request_send_authorization_response": (
                _private_context_value(send_authorization_response)
            ),
            "send_authorization_response_review": (
                _private_context_value(response_review)
            ),
            "request_contract_materialization_review": (
                _private_context_value(contract_review)
            ),
            "transport_bindings": _private_context_value(transport_bindings),
            "prepared_at": _private_context_value(prepared_at),
            "expires_at": _private_context_value(expires_at),
        }
    )


def _binding_sort_key(
    binding: GuidedProviderRequestTransportBinding,
) -> tuple[str, str, str, str]:
    contract = binding._contract
    return (
        contract.topic.value,
        contract.materialization_kind.value,
        contract._source_binding_fingerprint,
        contract._context_fingerprint,
    )


def _validate_profile_counts(
    values: tuple[tuple[GuidedProviderRequestTransportProfile, int], ...],
    expected_total: int,
) -> None:
    if (
        not isinstance(values, tuple)
        or len(values) != len(GuidedProviderRequestTransportProfile)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or type(item[0]) is not GuidedProviderRequestTransportProfile
            or type(item[1]) is not int
            or not 0 <= item[1] <= _MAX_REQUESTS
            for item in values
        )
        or tuple(sorted(values, key=lambda item: item[0].value)) != values
        or {item[0] for item in values}
        != set(GuidedProviderRequestTransportProfile)
        or sum(item[1] for item in values) != expected_total
    ):
        raise ValueError("Send-preparation transport profile counts are invalid")


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
    "GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION",
    "GuidedProviderRequestCredentialSlot",
    "GuidedProviderRequestHTTPMethod",
    "GuidedProviderRequestSendPreparation",
    "GuidedProviderRequestSendPreparationReview",
    "GuidedProviderRequestSendPreparationStatus",
    "GuidedProviderRequestTransportBinding",
    "GuidedProviderRequestTransportProfile",
    "GuidedProviderRequestValuePlacement",
    "assess_guided_provider_request_send_preparation",
    "prepare_guided_provider_request_send_preparation",
]
