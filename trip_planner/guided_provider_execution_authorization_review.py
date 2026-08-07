"""Private human review for exact guided provider-execution targets.

Phase 5.17 revalidates the complete Phase 5.16 binding chain and projects a
bounded, human-readable review of the exact targets, intended data transfer,
exact bound request count, and current host attestations.  No request has yet
been created or sent.  The ordinary ``to_dict``
view remains safe for logs.  Exact private values are available only through
an explicitly named, process-local ephemeral projection.

This module captures no authorization response, creates no provider or HTTP
request, reads no credential, performs no provider call, and grants no
execution authority.  The itinerary remains candidate + unverified.
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
from .guided_evidence_plan import (
    GuidedEvidenceDisposition,
    GuidedEvidenceRequirementPlan,
    GuidedEvidenceTopic,
)
from .guided_itinerary import GuidedItineraryCandidate, GuidedItineraryResponse
from .guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreference,
    _private_context_value,
)
from .guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetBindings,
    GuidedProviderExecutionTargetBindingsReview,
    GuidedProviderExecutionTargetPreimage,
    GuidedProviderExecutionTargetSourceKind,
    assess_guided_provider_execution_target_bindings,
)
from .guided_provider_execution_targets import (
    GuidedProviderExecutionTargetItem,
    GuidedProviderExecutionTargetKind,
    GuidedProviderExecutionTargets,
)
from .guided_provider_preflight import (
    GuidedProviderBillingRegion,
    GuidedProviderCredentialStatus,
    GuidedProviderPolicyProfile,
    GuidedProviderPreflight,
    GuidedProviderPreflightItem,
    GuidedProviderPricingProfile,
    GuidedProviderRequestProfile,
    GuidedProviderRetentionProfile,
)
from .guided_provider_preflight_response import GuidedProviderPreflightResponse
from .guided_provider_scope import (
    GuidedProviderCapability,
    GuidedProviderDataCategory,
    GuidedProviderScopeProposal,
)
from .guided_provider_scope_response import GuidedProviderScopeResponse
from .guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementResponse,
)
from .lodging_discovery import LodgingDiscoveryRequest
from .models import DecisionState, EvidenceState
from .place_details import GooglePlaceDetailsRequest
from .places_identity import GOOGLE_PLACE_IDENTITY_FIELD_MASK, PlaceIdentityIntent
from .routes import GoogleRouteRequest


GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW_VERSION = (
    "guided-provider-execution-authorization-review/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ITEMS = 12
_MAX_REQUESTS = 32
_MAX_SOURCE_LINE_REFERENCES = _MAX_ITEMS * 32
_MAX_GOOGLE_LIST_COST_USD_MICROS = _MAX_REQUESTS * 32_000
_NEXT_ACTION = "capture_private_provider_execution_authorization_response"
_RESPONSE_OPTIONS = ("accept", "request_smaller", "cancel")
_PROMPT = (
    "請審閱以下私人 provider 查詢目標、將傳送的資料、若接受時的精確綁定"
    "請求數與核准上限；"
    "接受、縮小或取消都必須在下一個 exact response gate 明確回覆。"
)
_DISCLOSURE = (
    "這份審閱只準備授權選擇，不等於授權執行；價格是第一付費級距試算，"
    "實際帳單、月免費額度、政策、留存與憑證仍會在執行前重查。"
)
_REVIEW_TOKEN = object()
_ITEM_TOKEN = object()
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
}
_FIRST_PAID_TIER_USD_MICROS_PER_REQUEST = {
    GuidedProviderPricingProfile.GOOGLE_TEXT_SEARCH_PRO_GLOBAL_2026_07_31: 32_000,
    GuidedProviderPricingProfile.GOOGLE_PLACE_DETAILS_ENTERPRISE_GLOBAL_2026_07_31: 20_000,
    GuidedProviderPricingProfile.GOOGLE_COMPUTE_ROUTES_ESSENTIALS_GLOBAL_2026_07_31: 5_000,
}
_SOURCE_KIND_BY_TARGET_KIND = {
    GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT: (
        GuidedProviderExecutionTargetSourceKind.CANONICAL_PRIVATE_INTENT
    ),
    GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT: (
        GuidedProviderExecutionTargetSourceKind
        .TRUSTED_EVIDENCE_REQUEST_CONTRACT
    ),
    GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR: (
        GuidedProviderExecutionTargetSourceKind
        .TRUSTED_EVIDENCE_REQUEST_CONTRACT
    ),
    GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT: (
        GuidedProviderExecutionTargetSourceKind.CANONICAL_PRIVATE_INTENT
    ),
}
_ALLOWED_TRANSMITTED_FIELD_NAMES = {
    GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT: {
        "field_mask",
        "language_code",
        "latitude",
        "longitude",
        "radius_m",
        "region_code",
        "text_query",
    },
    GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT: {
        "field_mask",
        "language_code",
        "region_code",
    },
    GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR: {
        "departure_at",
        "field_mask",
        "mode",
    },
    GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT: {
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
_REQUIRED_TRANSMITTED_FIELD_NAMES = {
    GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT: {
        "field_mask",
        "language_code",
        "region_code",
        "text_query",
    },
    GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT: {
        "field_mask",
        "language_code",
        "region_code",
    },
    GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR: {
        "departure_at",
        "field_mask",
        "mode",
    },
    GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT: {
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
_ALLOWED_LOCAL_FIELD_NAMES = {
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
_REDACTED_PROVIDER_IDENTIFIER_FIELDS = {
    GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT: (),
    GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT: (
        "provider_place_id",
    ),
    GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR: (
        "destination_provider_place_id",
        "origin_provider_place_id",
    ),
    GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT: (),
}


class GuidedProviderExecutionAuthorizationReviewStatus(str, Enum):
    """A fresh exact review requires a separately captured response."""

    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True, repr=False)
class _GuidedProviderExecutionAuthorizationReviewItem:
    """One derived private disclosure; no raw target object is retained."""

    topic: GuidedEvidenceTopic
    capability: GuidedProviderCapability
    request_profile: GuidedProviderRequestProfile
    pricing_profile: GuidedProviderPricingProfile
    policy_profile: GuidedProviderPolicyProfile
    retention_profile: GuidedProviderRetentionProfile
    billing_region: GuidedProviderBillingRegion = field(repr=False)
    credential_status: GuidedProviderCredentialStatus = field(repr=False)
    target_kind: GuidedProviderExecutionTargetKind
    source_kind: GuidedProviderExecutionTargetSourceKind
    source_line_reference_count: int
    user_stated_source_line_count: int
    tentative_source_line_count: int
    ai_candidate_source_line_count: int
    _provider_transmitted_values: tuple[tuple[str, object], ...] = field(
        repr=False
    )
    _local_review_context: tuple[tuple[str, object], ...] = field(repr=False)
    _redacted_provider_identifier_fields: tuple[str, ...] = field(repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ITEM_TOKEN:
            raise ValueError("Authorization review items require trusted preparation")
        exact_enums = (
            (self.topic, GuidedEvidenceTopic),
            (self.capability, GuidedProviderCapability),
            (self.request_profile, GuidedProviderRequestProfile),
            (self.pricing_profile, GuidedProviderPricingProfile),
            (self.policy_profile, GuidedProviderPolicyProfile),
            (self.retention_profile, GuidedProviderRetentionProfile),
            (self.billing_region, GuidedProviderBillingRegion),
            (self.credential_status, GuidedProviderCredentialStatus),
            (self.target_kind, GuidedProviderExecutionTargetKind),
            (self.source_kind, GuidedProviderExecutionTargetSourceKind),
        )
        if any(type(value) is not expected for value, expected in exact_enums):
            raise TypeError("Authorization review item enums must be exact")
        if self.source_kind is not _SOURCE_KIND_BY_TARGET_KIND[self.target_kind]:
            raise ValueError("Authorization review item source kind differs")
        if (
            type(self.source_line_reference_count) is not int
            or not 1 <= self.source_line_reference_count <= 32
        ):
            raise ValueError("Authorization review source-line count is invalid")
        for name, value in (
            ("user_stated", self.user_stated_source_line_count),
            ("tentative", self.tentative_source_line_count),
            ("ai_candidate", self.ai_candidate_source_line_count),
        ):
            if type(value) is not int or not 0 <= value <= 32:
                raise ValueError(
                    f"Authorization review {name} source-line count is invalid"
                )
        if (
            self.user_stated_source_line_count
            + self.tentative_source_line_count
            + self.ai_candidate_source_line_count
            != self.source_line_reference_count
        ):
            raise ValueError("Authorization review source provenance differs")
        transmitted_names = _validate_private_fields(
            self._provider_transmitted_values,
            "provider_transmitted_values",
        )
        local_names = _validate_private_fields(
            self._local_review_context,
            "local_review_context",
        )
        allowed_transmitted = _ALLOWED_TRANSMITTED_FIELD_NAMES[self.target_kind]
        required_transmitted = _REQUIRED_TRANSMITTED_FIELD_NAMES[self.target_kind]
        if not required_transmitted <= transmitted_names <= allowed_transmitted:
            raise ValueError("Authorization review transmitted fields differ")
        if local_names != _ALLOWED_LOCAL_FIELD_NAMES[self.target_kind]:
            raise ValueError("Authorization review local fields differ")
        if (
            self._redacted_provider_identifier_fields
            != _REDACTED_PROVIDER_IDENTIFIER_FIELDS[self.target_kind]
        ):
            raise ValueError("Authorization review identifier fields differ")
        coordinate_names = {"latitude", "longitude", "radius_m"}
        if (
            self.target_kind
            is GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT
            and bool(transmitted_names & coordinate_names)
            is not coordinate_names <= transmitted_names
        ):
            raise ValueError("Authorization review location bias is incomplete")
        if self.credential_status is not GuidedProviderCredentialStatus.AVAILABLE:
            raise ValueError("Authorization review requires available credentials")

    @property
    def first_paid_tier_google_cost_usd_micros(self) -> int:
        return _FIRST_PAID_TIER_USD_MICROS_PER_REQUEST.get(
            self.pricing_profile,
            0,
        )

    @property
    def uses_serpapi_plan_credit(self) -> bool:
        return (
            self.pricing_profile
            is GuidedProviderPricingProfile.SERPAPI_PLAN_CREDIT_2026_08_07
        )

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionAuthorizationReviewItem("
            f"topic={self.topic.value!r}, "
            f"target_kind={self.target_kind.value!r})"
        )

    def to_safe_dict(self) -> dict[str, object]:
        """Return field names and profiles without exact private values."""

        transmitted_names = tuple(
            name for name, _ in self._provider_transmitted_values
        )
        return {
            "topic": self.topic.value,
            "capability": self.capability.value,
            "request_profile": self.request_profile.value,
            "pricing_profile": self.pricing_profile.value,
            "policy_profile": self.policy_profile.value,
            "retention_profile": self.retention_profile.value,
            "target_kind": self.target_kind.value,
            "source_kind": self.source_kind.value,
            "bound_request_count": 1,
            "source_line_reference_count": self.source_line_reference_count,
            "source_state_counts": {
                "user_stated": self.user_stated_source_line_count,
                "tentative": self.tentative_source_line_count,
                "ai_candidate": self.ai_candidate_source_line_count,
            },
            "all_source_lines_require_verification": True,
            "source_values_are_authoritative": False,
            "provider_transmitted_field_names": [
                *transmitted_names,
                *self._redacted_provider_identifier_fields,
            ],
            "local_review_context_field_names": [
                name for name, _ in self._local_review_context
            ],
            "redacted_bound_provider_identifier_count": len(
                self._redacted_provider_identifier_fields
            ),
            "exact_private_values_included": False,
            "target_fingerprint_exposed": False,
            "billing_region_classification_attested": True,
            "credential_available_attested": True,
            "stable_local_review_identifiers_are_provider_place_ids": False,
        }

    def to_ephemeral_private_dict(self) -> dict[str, object]:
        """Return minimum exact values for direct, private human review."""

        return {
            **self.to_safe_dict(),
            "provider_transmitted_values": {
                name: _human_value(value)
                for name, value in self._provider_transmitted_values
            },
            "local_review_context": {
                name: _human_value(value)
                for name, value in self._local_review_context
            },
            "redacted_bound_provider_identifier_fields": list(
                self._redacted_provider_identifier_fields
            ),
            "stable_local_identifiers_are_private_review_labels": True,
            "exact_private_values_included": True,
        }


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionAuthorizationReview:
    """Token-gated exact review with a safe default serialization."""

    status: GuidedProviderExecutionAuthorizationReviewStatus
    next_action: str
    accepted_scope_item_count: int
    accepted_max_request_count: int
    target_item_count: int
    bound_request_count: int
    max_request_count: int
    bound_source_line_reference_count: int
    bound_google_request_count: int
    bound_serpapi_request_count: int
    estimated_bound_first_paid_tier_google_cost_usd_micros: int
    accepted_max_first_paid_tier_google_cost_usd_micros: int
    serpapi_bound_plan_credit_count: int
    serpapi_plan_credit_cap: int
    data_categories: tuple[GuidedProviderDataCategory, ...] = ()
    host_attestation_fresh: bool = False
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = (
        GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW_VERSION
    )
    _items: tuple[_GuidedProviderExecutionAuthorizationReviewItem, ...] = field(
        default=(), repr=False
    )
    _prepared_at: datetime = field(
        default=datetime.min.replace(tzinfo=None), repr=False
    )
    _expires_at: datetime = field(
        default=datetime.min.replace(tzinfo=None), repr=False
    )
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Authorization reviews require trusted preparation")
        if (
            type(self.status)
            is not GuidedProviderExecutionAuthorizationReviewStatus
            or self.status
            is not GuidedProviderExecutionAuthorizationReviewStatus.REVIEW_REQUIRED
            or self.next_action != _NEXT_ACTION
        ):
            raise ValueError("Authorization review status/action conflict")
        for name, value, maximum in (
            ("accepted_scope_item_count", self.accepted_scope_item_count, _MAX_ITEMS),
            ("accepted_max_request_count", self.accepted_max_request_count, _MAX_REQUESTS),
            ("target_item_count", self.target_item_count, _MAX_ITEMS),
            ("bound_request_count", self.bound_request_count, _MAX_REQUESTS),
            ("max_request_count", self.max_request_count, _MAX_REQUESTS),
            (
                "bound_source_line_reference_count",
                self.bound_source_line_reference_count,
                _MAX_SOURCE_LINE_REFERENCES,
            ),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"Authorization review {name} is invalid")
        for name, value in (
            ("bound_google_request_count", self.bound_google_request_count),
            ("bound_serpapi_request_count", self.bound_serpapi_request_count),
            ("serpapi_bound_plan_credit_count", self.serpapi_bound_plan_credit_count),
            ("serpapi_plan_credit_cap", self.serpapi_plan_credit_cap),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_REQUESTS:
                raise ValueError(f"Authorization review {name} is invalid")
        if (
            not isinstance(self._items, tuple)
            or not self._items
            or any(
                type(item) is not _GuidedProviderExecutionAuthorizationReviewItem
                for item in self._items
            )
            or tuple(sorted(self._items, key=_item_sort_key)) != self._items
            or self.bound_request_count != len(self._items)
            or self.target_item_count != len({item.topic for item in self._items})
            or self.accepted_scope_item_count != self.target_item_count
            or self.bound_request_count > self.max_request_count
            or self.max_request_count > self.accepted_max_request_count
            or self.bound_source_line_reference_count
            != sum(item.source_line_reference_count for item in self._items)
            or self.bound_google_request_count
            != sum(not item.uses_serpapi_plan_credit for item in self._items)
            or self.bound_serpapi_request_count
            != sum(item.uses_serpapi_plan_credit for item in self._items)
            or self.bound_request_count
            != self.bound_google_request_count
            + self.bound_serpapi_request_count
            or self.serpapi_bound_plan_credit_count
            != self.bound_serpapi_request_count
            or self.serpapi_bound_plan_credit_count > self.serpapi_plan_credit_cap
        ):
            raise ValueError("Authorization review counts are inconsistent")
        expected_bound_cost = sum(
            item.first_paid_tier_google_cost_usd_micros for item in self._items
        )
        if (
            type(self.estimated_bound_first_paid_tier_google_cost_usd_micros)
            is not int
            or self.estimated_bound_first_paid_tier_google_cost_usd_micros
            != expected_bound_cost
            or not 0
            <= expected_bound_cost
            <= self.accepted_max_first_paid_tier_google_cost_usd_micros
            <= _MAX_GOOGLE_LIST_COST_USD_MICROS
        ):
            raise ValueError("Authorization review cost estimate is invalid")
        if (
            not isinstance(self.data_categories, tuple)
            or not self.data_categories
            or any(
                type(item) is not GuidedProviderDataCategory
                for item in self.data_categories
            )
            or tuple(sorted(set(self.data_categories), key=lambda item: item.value))
            != self.data_categories
            or type(self.host_attestation_fresh) is not bool
            or not self.host_attestation_fresh
        ):
            raise ValueError("Authorization review attestations are invalid")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
            or self.contract_version
            != GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW_VERSION
        ):
            raise ValueError("Authorization review safe metadata is invalid")
        prepared = _utc_datetime(self._prepared_at, "prepared_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not prepared < expires:
            raise ValueError("Authorization review expiry is invalid")
        _digest(self._context_fingerprint, "context_fingerprint")
        object.__setattr__(self, "_prepared_at", prepared)
        object.__setattr__(self, "_expires_at", expires)

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionAuthorizationReview("
            f"status={self.status.value!r}, "
            f"bound_request_count={self.bound_request_count!r}, "
            f"next_action={self.next_action!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a log-safe review summary without exact private values."""

        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": True,
            "requires_user_review": True,
            "requires_user_decision": True,
            "review_prompt": _PROMPT,
            "review_disclosure": _DISCLOSURE,
            "response_options": list(_RESPONSE_OPTIONS),
            "provider_execution_authorization_review": {
                "exact_private_context_bound": True,
                "host_attestation_fresh": self.host_attestation_fresh,
                "accepted_scope_item_count": self.accepted_scope_item_count,
                "accepted_max_request_count": self.accepted_max_request_count,
                "target_item_count": self.target_item_count,
                "bound_request_count": self.bound_request_count,
                "max_request_count": self.max_request_count,
                "bound_source_line_reference_count": (
                    self.bound_source_line_reference_count
                ),
                "bound_google_request_count": self.bound_google_request_count,
                "bound_serpapi_request_count": self.bound_serpapi_request_count,
                "requests_bounded_by_accepted_caps": True,
                "items": [item.to_safe_dict() for item in self._items],
                "data_categories": [
                    item.value for item in self.data_categories
                ],
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
                "monthly_free_usage_remaining_checked": False,
                "cost_estimate_is_hard_currency_cap": False,
                "pricing_profile_attested": True,
                "policy_profile_attested": True,
                "retention_profile_attested": True,
                "credential_availability_attested": True,
                "billing_region_classification_attested": True,
                "billing_address_retained": False,
                "exact_private_values_in_safe_output": False,
                "ephemeral_private_review_payload_available": True,
                "raw_target_preimages_retained": False,
                "derived_private_disclosures_retained_process_locally": True,
                "provider_identifier_values_exposed": False,
                "target_fingerprints_exposed": False,
                "authorization_response_captured": False,
                "partial_execution_authorization_permitted": False,
                "execution_time_pricing_policy_retention_recheck_required": True,
                "credential_availability_recheck_required": True,
                "provider_scope_authorized": False,
                "provider_request_contracts_created_by_review": False,
                "provider_request_contract_count_created_by_review": 0,
                "http_requests_created": False,
                "http_request_count_created_by_review": 0,
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
                "derived_private_disclosures_retained": True,
                "private_disclosures_in_safe_output": False,
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
        }

    def to_ephemeral_private_review_payload(self) -> dict[str, Any]:
        """Project exact values for direct review; callers must not persist it."""

        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "payload_handling": "private_ephemeral_direct_human_review_only",
            "review_prompt": _PROMPT,
            "review_disclosure": _DISCLOSURE,
            "response_options": list(_RESPONSE_OPTIONS),
            "prepared_at": self._prepared_at.isoformat(),
            "expires_at": self._expires_at.isoformat(),
            "bound_request_count": self.bound_request_count,
            "accepted_max_request_count": self.accepted_max_request_count,
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
            "targets": [
                item.to_ephemeral_private_dict() for item in self._items
            ],
            "authorization_response_captured": False,
            "must_reassess_before_response_capture": True,
            "provider_identifier_values_exposed": False,
            "target_fingerprints_exposed": False,
            "credentials_exposed": False,
            "provider_request_contract_count_created_by_review": 0,
            "http_request_count_created_by_review": 0,
            "provider_call_count_observed": 0,
            "provider_calls_permitted": False,
        }


def prepare_guided_provider_execution_authorization_review(
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
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderExecutionAuthorizationReview:
    """Prepare a private human review without capturing a response."""

    prepared_at = _utc_datetime(evaluation_at, "evaluation_at")
    bindings_review = assess_guided_provider_execution_target_bindings(
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
        preimages=preimages,
        evaluation_at=prepared_at,
    )
    items = _derive_review_items(
        refinement,
        evidence_plan,
        preflight,
        targets,
        preimages,
    )
    return _new_review(
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
        bindings_review,
        items,
        prepared_at,
    )


def assess_guided_provider_execution_authorization_review(
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
    review: GuidedProviderExecutionAuthorizationReview,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderExecutionAuthorizationReview:
    """Rebuild the exact review and recheck all current freshness gates."""

    if type(review) is not GuidedProviderExecutionAuthorizationReview:
        raise TypeError("review must be exact authorization review")
    current_evaluation = _utc_datetime(evaluation_at, "evaluation_at")
    if current_evaluation < review._prepared_at:
        raise ValueError("evaluation_at cannot precede review preparation")

    captured_bindings_review = assess_guided_provider_execution_target_bindings(
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
        preimages=preimages,
        evaluation_at=review._prepared_at,
    )
    expected_items = _derive_review_items(
        refinement,
        evidence_plan,
        preflight,
        targets,
        preimages,
    )
    expected_review = _new_review(
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
        captured_bindings_review,
        expected_items,
        review._prepared_at,
    )
    if review != expected_review:
        raise ValueError("Authorization review differs from exact private context")

    assess_guided_provider_execution_target_bindings(
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
        preimages=preimages,
        evaluation_at=current_evaluation,
    )
    if _derive_review_items(
        refinement,
        evidence_plan,
        preflight,
        targets,
        preimages,
    ) != review._items:
        raise ValueError("Authorization review target disclosure is no longer current")
    return review


def _new_review(
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
    bindings_review: GuidedProviderExecutionTargetBindingsReview,
    items: tuple[_GuidedProviderExecutionAuthorizationReviewItem, ...],
    prepared_at: datetime,
) -> GuidedProviderExecutionAuthorizationReview:
    needs_verification = tuple(
        dict.fromkeys(
            (
                *bindings_review.needs_verification,
                "provider_execution_authorization_review",
            )
        )
    )
    google_count = sum(not item.uses_serpapi_plan_credit for item in items)
    serpapi_count = sum(item.uses_serpapi_plan_credit for item in items)
    return GuidedProviderExecutionAuthorizationReview(
        status=GuidedProviderExecutionAuthorizationReviewStatus.REVIEW_REQUIRED,
        next_action=_NEXT_ACTION,
        accepted_scope_item_count=bindings_review.accepted_scope_item_count,
        accepted_max_request_count=(
            bindings_review.accepted_max_request_count
        ),
        target_item_count=bindings_review.target_item_count,
        bound_request_count=len(items),
        max_request_count=bindings_review.max_request_count,
        bound_source_line_reference_count=(
            bindings_review.bound_source_line_reference_count
        ),
        bound_google_request_count=google_count,
        bound_serpapi_request_count=serpapi_count,
        estimated_bound_first_paid_tier_google_cost_usd_micros=sum(
            item.first_paid_tier_google_cost_usd_micros for item in items
        ),
        accepted_max_first_paid_tier_google_cost_usd_micros=(
            bindings_review.estimated_first_paid_tier_google_cost_usd_micros
        ),
        serpapi_bound_plan_credit_count=serpapi_count,
        serpapi_plan_credit_cap=bindings_review.serpapi_plan_credit_cap,
        data_categories=bindings_review.data_categories,
        host_attestation_fresh=bindings_review.host_attestation_fresh,
        tentative_fields=bindings_review.tentative_fields,
        needs_verification=needs_verification,
        _items=items,
        _prepared_at=prepared_at,
        _expires_at=preflight.expires_at,
        _context_fingerprint=_review_context_fingerprint(
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
            bindings_review,
            items,
            prepared_at,
        ),
        _token=_REVIEW_TOKEN,
    )


def _derive_review_items(
    refinement: GuidedRefinementCandidate,
    evidence_plan: GuidedEvidenceRequirementPlan,
    preflight: GuidedProviderPreflight,
    targets: GuidedProviderExecutionTargets,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
) -> tuple[_GuidedProviderExecutionAuthorizationReviewItem, ...]:
    if (
        not isinstance(preimages, tuple)
        or not preimages
        or len(preimages) > _MAX_REQUESTS
        or any(
            type(item) is not GuidedProviderExecutionTargetPreimage
            for item in preimages
        )
    ):
        raise ValueError("preimages must be a bounded tuple of exact values")
    requirements = {item.topic: item for item in targets.items}
    preflight_items = {item.topic: item for item in preflight.items}
    declarations = {
        item.source_line_index: item for item in evidence_plan.declarations
    }
    if {item.topic for item in preimages} != set(requirements):
        raise ValueError("Authorization review requires every target topic")
    items = tuple(
        _review_item_from_preimage(
            requirements[preimage.topic],
            preflight_items[preimage.topic],
            preimage,
            _source_state_counts(refinement, declarations, preimage),
        )
        for preimage in preimages
    )
    return tuple(sorted(items, key=_item_sort_key))


def _review_item_from_preimage(
    requirement: GuidedProviderExecutionTargetItem,
    preflight_item: GuidedProviderPreflightItem,
    preimage: GuidedProviderExecutionTargetPreimage,
    source_state_counts: tuple[int, int, int],
) -> _GuidedProviderExecutionAuthorizationReviewItem:
    target = preimage.target
    transmitted: dict[str, object]
    local: dict[str, object]
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
        local = {
            "expected_locality": target.expected_locality,
            "expected_name": target.expected_name,
            "expected_primary_types": target.expected_primary_types,
            "stable_local_location_id": target.location_id,
        }
    elif type(target) is GooglePlaceDetailsRequest:
        transmitted = {
            "field_mask": target.field_mask,
            "language_code": target.language_code,
            "region_code": target.region_code,
        }
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
    elif type(target) is GoogleRouteRequest:
        transmitted = {
            "departure_at": target.departure_at,
            "field_mask": target.field_mask,
            "mode": target.mode.value,
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
        local = {"currency_minor_unit": target.currency_minor_unit}
    else:  # pragma: no cover - Phase 5.16 already rejects unsupported types
        raise TypeError("Unsupported authorization review target")

    return _GuidedProviderExecutionAuthorizationReviewItem(
        topic=requirement.topic,
        capability=requirement.capability,
        request_profile=requirement.request_profile,
        pricing_profile=preflight_item.pricing_profile,
        policy_profile=preflight_item.policy_profile,
        retention_profile=preflight_item.retention_profile,
        billing_region=preflight_item.billing_region,
        credential_status=preflight_item.credential_status,
        target_kind=requirement.target_kind,
        source_kind=_SOURCE_KIND_BY_TARGET_KIND[requirement.target_kind],
        source_line_reference_count=len(preimage.source_line_indexes),
        user_stated_source_line_count=source_state_counts[0],
        tentative_source_line_count=source_state_counts[1],
        ai_candidate_source_line_count=source_state_counts[2],
        _provider_transmitted_values=tuple(sorted(transmitted.items())),
        _local_review_context=tuple(sorted(local.items())),
        _redacted_provider_identifier_fields=(
            _REDACTED_PROVIDER_IDENTIFIER_FIELDS[requirement.target_kind]
        ),
        _token=_ITEM_TOKEN,
    )


def _source_state_counts(
    refinement: GuidedRefinementCandidate,
    declarations: dict[int, Any],
    preimage: GuidedProviderExecutionTargetPreimage,
) -> tuple[int, int, int]:
    """Summarize source certainty without retaining text or line indexes."""

    counts = {"user_stated": 0, "tentative": 0, "ai_candidate": 0}
    for index in preimage.source_line_indexes:
        declaration = declarations.get(index)
        if (
            declaration is None
            or declaration.disposition
            is not GuidedEvidenceDisposition.REQUIRES_VERIFICATION
            or preimage.topic not in declaration.topics
            or index >= len(refinement.direction.lines)
        ):
            raise ValueError(
                "Authorization review source provenance is not exact"
            )
        presentation_source = (
            refinement.direction.lines[index].presentation_source
        )
        if presentation_source not in counts:
            raise ValueError(
                "Authorization review source state is unsupported"
            )
        counts[presentation_source] += 1
    return (
        counts["user_stated"],
        counts["tentative"],
        counts["ai_candidate"],
    )


def _review_context_fingerprint(
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
    bindings_review: GuidedProviderExecutionTargetBindingsReview,
    items: tuple[_GuidedProviderExecutionAuthorizationReviewItem, ...],
    prepared_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": (
                GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW_VERSION
            ),
            "domain": "guided-provider-execution-authorization-review",
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
            "provider_execution_target_bindings_review": (
                _private_context_value(bindings_review)
            ),
            "derived_private_review_items": _private_context_value(items),
            "prepared_at": _private_context_value(prepared_at),
            "expires_at": _private_context_value(preflight.expires_at),
        }
    )


def _validate_private_fields(
    values: tuple[tuple[str, object], ...],
    name: str,
) -> set[str]:
    if (
        not isinstance(values, tuple)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or type(item[0]) is not str
            or not _valid_private_value(item[1])
            for item in values
        )
        or tuple(sorted(values, key=lambda item: item[0])) != values
        or len({item[0] for item in values}) != len(values)
    ):
        raise ValueError(f"Authorization review {name} is invalid")
    return {item[0] for item in values}


def _valid_private_value(value: object) -> bool:
    return (
        value is None
        or type(value) in {bool, int, float, str}
        or (
            isinstance(value, tuple)
            and all(type(item) in {bool, int, float, str} for item in value)
        )
    )


def _human_value(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def _item_sort_key(
    item: _GuidedProviderExecutionAuthorizationReviewItem,
) -> tuple[object, ...]:
    return (
        item.topic.value,
        item.capability.value,
        item.request_profile.value,
        item.target_kind.value,
        _sha256(
            {
                "provider_transmitted_values": _private_context_value(
                    item._provider_transmitted_values
                ),
                "local_review_context": _private_context_value(
                    item._local_review_context
                ),
            }
        ),
    )


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
    "GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW_VERSION",
    "GuidedProviderExecutionAuthorizationReview",
    "GuidedProviderExecutionAuthorizationReviewStatus",
    "assess_guided_provider_execution_authorization_review",
    "prepare_guided_provider_execution_authorization_review",
]
