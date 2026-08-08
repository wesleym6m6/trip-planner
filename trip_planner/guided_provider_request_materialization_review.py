"""Exact private review before any provider request is materialized.

Phase 5.20 consumes only a fresh ready Phase 5.19 execution-time recheck and
the same exact typed target preimages.  It derives non-executable request
contract candidates and an ephemeral direct-human review of their exact
transmitted values.  Bound provider identifiers remain redacted even there.

The review does not create an executable provider contract or HTTP request,
select a transport endpoint, inject or read credentials, call a provider,
reserve spend, persist data, schedule, render, deploy, confirm, or mutate
canonical state.  A separate exact response gate is always required.
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
    _GuidedProviderExecutionAuthorizationReviewItem,
    assess_guided_provider_execution_authorization_review,
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
    GuidedProviderExecutionTimeRecheckReview,
    GuidedProviderExecutionTimeRecheckStatus,
    assess_guided_provider_execution_time_recheck,
)
from .guided_provider_preflight import (
    GuidedProviderPolicyProfile,
    GuidedProviderPreflight,
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
from .guided_refinement import GuidedRefinementCandidate, GuidedRefinementResponse
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_REQUEST_MATERIALIZATION_REVIEW_VERSION = (
    "guided-provider-request-materialization-review/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ITEMS = 12
_MAX_REQUESTS = 32
_MAX_SOURCE_LINE_REFERENCES = _MAX_ITEMS * 32
_MAX_GOOGLE_LIST_COST_USD_MICROS = _MAX_REQUESTS * 32_000
_MAX_REVIEW_AGE = timedelta(minutes=5)
_NEXT_ACTION = "capture_private_provider_request_materialization_response"
_RESPONSE_OPTIONS = ("prepare_materialization", "request_smaller", "cancel")
_PROMPT = (
    "請再次審閱以下私人 provider request contract candidates、將傳送的"
    "精確值與綁定請求數，並在下一個 exact response gate 選擇 "
    "prepare_materialization、request_smaller 或 cancel。"
)
_DISCLOSURE = (
    "這份 review 仍不會建立 HTTP request、讀取 credential 或呼叫 provider；"
    "prepare_materialization 選項也必須由下一個獨立 typed response gate 擷取，"
    "且只表示準備下一階段，不是立即執行。"
)
_ITEM_TOKEN = object()
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
}


class GuidedProviderRequestMaterializationKind(str, Enum):
    """Provider-neutral materialization surface selected by a typed profile."""

    GOOGLE_PLACES_TEXT_SEARCH = "google_places_text_search"
    GOOGLE_PLACE_DETAILS = "google_place_details"
    GOOGLE_ROUTES_COMPUTE_ROUTES = "google_routes_compute_routes"
    SERPAPI_GOOGLE_HOTELS = "serpapi_google_hotels"


class GuidedProviderRequestMaterializationReviewStatus(str, Enum):
    """A fresh exact candidate review requires a separate typed response."""

    REVIEW_REQUIRED = "review_required"


_MATERIALIZATION_KIND_BY_REQUEST_PROFILE = {
    GuidedProviderRequestProfile.GOOGLE_PLACES_TEXT_SEARCH_PRO: (
        GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH
    ),
    GuidedProviderRequestProfile.GOOGLE_PLACES_PLACE_DETAILS_ENTERPRISE: (
        GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS
    ),
    GuidedProviderRequestProfile.GOOGLE_ROUTES_COMPUTE_ROUTES_ESSENTIALS: (
        GuidedProviderRequestMaterializationKind.GOOGLE_ROUTES_COMPUTE_ROUTES
    ),
    GuidedProviderRequestProfile.SERPAPI_GOOGLE_HOTELS_PLAN_CREDIT: (
        GuidedProviderRequestMaterializationKind.SERPAPI_GOOGLE_HOTELS
    ),
}
_TARGET_KIND_BY_REQUEST_PROFILE = {
    GuidedProviderRequestProfile.GOOGLE_PLACES_TEXT_SEARCH_PRO: (
        GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT
    ),
    GuidedProviderRequestProfile.GOOGLE_PLACES_PLACE_DETAILS_ENTERPRISE: (
        GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT
    ),
    GuidedProviderRequestProfile.GOOGLE_ROUTES_COMPUTE_ROUTES_ESSENTIALS: (
        GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR
    ),
    GuidedProviderRequestProfile.SERPAPI_GOOGLE_HOTELS_PLAN_CREDIT: (
        GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT
    ),
}


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestContractCandidate:
    """One non-executable request shape derived from an exact private target."""

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
    _local_review_context: tuple[tuple[str, object], ...] = field(repr=False)
    _redacted_provider_identifier_fields: tuple[str, ...] = field(repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ITEM_TOKEN:
            raise ValueError("Request contract candidates require trusted preparation")
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
            raise TypeError("Request contract candidate enums must be exact")
        if (
            self.materialization_kind
            is not _MATERIALIZATION_KIND_BY_REQUEST_PROFILE[self.request_profile]
            or self.target_kind
            is not _TARGET_KIND_BY_REQUEST_PROFILE[self.request_profile]
        ):
            raise ValueError("Request contract candidate shape differs")
        if (
            type(self.source_line_reference_count) is not int
            or not 1 <= self.source_line_reference_count <= 32
        ):
            raise ValueError("Request contract source-line count is invalid")
        for value in (
            self.user_stated_source_line_count,
            self.tentative_source_line_count,
            self.ai_candidate_source_line_count,
        ):
            if type(value) is not int or not 0 <= value <= 32:
                raise ValueError("Request contract source-state count is invalid")
        if (
            self.user_stated_source_line_count
            + self.tentative_source_line_count
            + self.ai_candidate_source_line_count
            != self.source_line_reference_count
        ):
            raise ValueError("Request contract source provenance differs")
        transmitted_names = _private_field_names(
            self._provider_transmitted_values,
            "provider_transmitted_values",
        )
        local_names = _private_field_names(
            self._local_review_context,
            "local_review_context",
        )
        if (
            not transmitted_names
            or transmitted_names.intersection(
                self._redacted_provider_identifier_fields
            )
            or not isinstance(self._redacted_provider_identifier_fields, tuple)
            or tuple(sorted(set(self._redacted_provider_identifier_fields)))
            != self._redacted_provider_identifier_fields
            or any(
                type(name) is not str or not name
                for name in self._redacted_provider_identifier_fields
            )
            or transmitted_names.intersection(local_names)
        ):
            raise ValueError("Request contract private fields are invalid")

    @property
    def bound_provider_identifier_count(self) -> int:
        return len(self._redacted_provider_identifier_fields)

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestContractCandidate("
            f"topic={self.topic.value!r}, "
            f"materialization_kind={self.materialization_kind.value!r})"
        )

    def to_safe_dict(self) -> dict[str, object]:
        """Return request shape and field names, never exact private values."""

        transmitted_names = [
            name for name, _ in self._provider_transmitted_values
        ]
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
            "candidate_request_count": 1,
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
            "redacted_bound_provider_identifier_count": (
                self.bound_provider_identifier_count
            ),
            "exact_private_values_included": False,
            "request_contract_candidate": True,
            "request_contract_candidate_is_executable": False,
            "transport_endpoint_selected": False,
            "http_method_selected": False,
            "credential_slot_bound": False,
            "credential_value_included": False,
            "provider_identifier_values_exposed": False,
            "target_fingerprint_exposed": False,
        }

    def to_ephemeral_private_dict(self) -> dict[str, object]:
        """Return exact review values, while keeping provider IDs redacted."""

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
            "exact_private_values_included": True,
            "provider_identifiers_bound_but_redacted": bool(
                self._redacted_provider_identifier_fields
            ),
            "payload_handling": "private_ephemeral_direct_human_review_only",
        }


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestMaterializationReview:
    """Token-gated exact review of non-executable request candidates."""

    status: GuidedProviderRequestMaterializationReviewStatus
    next_action: str
    accepted_scope_item_count: int
    accepted_max_request_count: int
    candidate_count: int
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
    _items: tuple[GuidedProviderRequestContractCandidate, ...] = field(
        default=(), repr=False
    )
    _prepared_at: datetime = field(default=None, repr=False)  # type: ignore[arg-type]
    _expires_at: datetime = field(default=None, repr=False)  # type: ignore[arg-type]
    _context_fingerprint: str = field(default="", repr=False)
    contract_version: str = GUIDED_PROVIDER_REQUEST_MATERIALIZATION_REVIEW_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Request materialization reviews require trusted preparation")
        if (
            type(self.status) is not GuidedProviderRequestMaterializationReviewStatus
            or self.status
            is not GuidedProviderRequestMaterializationReviewStatus.REVIEW_REQUIRED
            or self.next_action != _NEXT_ACTION
        ):
            raise ValueError("Request materialization review status/action differs")
        for name, value, maximum in (
            ("accepted_scope_item_count", self.accepted_scope_item_count, _MAX_ITEMS),
            ("accepted_max_request_count", self.accepted_max_request_count, _MAX_REQUESTS),
            ("candidate_count", self.candidate_count, _MAX_REQUESTS),
            ("bound_request_count", self.bound_request_count, _MAX_REQUESTS),
            ("max_request_count", self.max_request_count, _MAX_REQUESTS),
            (
                "bound_source_line_reference_count",
                self.bound_source_line_reference_count,
                _MAX_SOURCE_LINE_REFERENCES,
            ),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"Request materialization {name} is invalid")
        for name, value in (
            ("bound_google_request_count", self.bound_google_request_count),
            ("bound_serpapi_request_count", self.bound_serpapi_request_count),
            ("serpapi_bound_plan_credit_count", self.serpapi_bound_plan_credit_count),
            ("serpapi_plan_credit_cap", self.serpapi_plan_credit_cap),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_REQUESTS:
                raise ValueError(f"Request materialization {name} is invalid")
        for name, value in (
            ("user_stated_source_line_count", self.user_stated_source_line_count),
            ("tentative_source_line_count", self.tentative_source_line_count),
            ("ai_candidate_source_line_count", self.ai_candidate_source_line_count),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_SOURCE_LINE_REFERENCES:
                raise ValueError(f"Request materialization {name} is invalid")
        if (
            self.accepted_scope_item_count > self.candidate_count
            or self.candidate_count != self.bound_request_count
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
            raise ValueError("Request materialization review counts differ")
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
            raise ValueError("Request materialization data categories are invalid")
        if (
            type(self.estimated_bound_first_paid_tier_google_cost_usd_micros)
            is not int
            or type(self.accepted_max_first_paid_tier_google_cost_usd_micros)
            is not int
            or not 0
            <= self.estimated_bound_first_paid_tier_google_cost_usd_micros
            <= self.accepted_max_first_paid_tier_google_cost_usd_micros
            <= _MAX_GOOGLE_LIST_COST_USD_MICROS
        ):
            raise ValueError("Request materialization cost is invalid")
        if type(self.host_attestation_fresh) is not bool or not self.host_attestation_fresh:
            raise ValueError("Request materialization review requires fresh host state")
        if (
            not isinstance(self._items, tuple)
            or len(self._items) != self.candidate_count
            or any(
                type(item) is not GuidedProviderRequestContractCandidate
                for item in self._items
            )
            or tuple(sorted(self._items, key=_item_sort_key)) != self._items
            or sum(item.source_line_reference_count for item in self._items)
            != self.bound_source_line_reference_count
            or sum(item.user_stated_source_line_count for item in self._items)
            != self.user_stated_source_line_count
            or sum(item.tentative_source_line_count for item in self._items)
            != self.tentative_source_line_count
            or sum(item.ai_candidate_source_line_count for item in self._items)
            != self.ai_candidate_source_line_count
        ):
            raise ValueError("Request materialization candidates differ")
        prepared = _utc_datetime(self._prepared_at, "prepared_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not prepared < expires <= prepared + _MAX_REVIEW_AGE:
            raise ValueError("Request materialization review expiry is invalid")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
            or self.contract_version
            != GUIDED_PROVIDER_REQUEST_MATERIALIZATION_REVIEW_VERSION
        ):
            raise ValueError("Request materialization safe metadata is invalid")
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
            "GuidedProviderRequestMaterializationReview("
            f"status={self.status.value!r}, "
            f"candidate_count={self.candidate_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a safe summary without exact request or target values."""

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
            "provider_request_materialization_review": {
                "fresh_execution_time_recheck_bound": True,
                "same_exact_target_preimages_revalidated": True,
                "must_reassess_before_response_capture": True,
                "exact_review_times_exposed": False,
                "accepted_scope_item_count": self.accepted_scope_item_count,
                "accepted_max_request_count": self.accepted_max_request_count,
                "request_contract_candidate_count": self.candidate_count,
                "eligible_for_materialization_response_count": (
                    self.bound_request_count
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
                "pricing_profile_recheck_bound": True,
                "policy_profile_recheck_bound": True,
                "retention_profile_recheck_bound": True,
                "billing_region_classification_recheck_bound": True,
                "credential_availability_recheck_bound": True,
                "all_credentials_available": True,
                "serpapi_plan_state_recheck_bound": True,
                "serpapi_plan_state_sufficient": True,
                "monthly_free_usage_remaining_checked": False,
                "cost_estimate_is_hard_currency_cap": False,
                "items": [item.to_safe_dict() for item in self._items],
                "exact_private_values_in_safe_output": False,
                "ephemeral_private_review_payload_available": True,
                "raw_target_preimages_retained": False,
                "derived_private_disclosures_retained_process_locally": True,
                "provider_identifier_values_exposed": False,
                "target_fingerprints_exposed": False,
                "credential_values_included": False,
                "request_contract_candidates_are_executable": False,
                "executable_provider_request_contract_count_created_by_review": 0,
                "http_request_count_created_by_review": 0,
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
                "derived_private_disclosures_retained": True,
                "private_disclosures_in_safe_output": False,
                "environment_read": False,
                "vault_accessed": False,
                "credentials_accessed": False,
                "executable_provider_request_contracts_created": False,
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
        """Project exact values only for direct, process-local human review."""

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
            "request_contract_candidates": [
                item.to_ephemeral_private_dict() for item in self._items
            ],
            "must_reassess_before_response_capture": True,
            "provider_identifier_values_exposed": False,
            "target_fingerprints_exposed": False,
            "credentials_exposed": False,
            "request_contract_candidates_are_executable": False,
            "executable_provider_request_contract_count_created_by_review": 0,
            "http_request_count_created_by_review": 0,
            "execution_authority_active": False,
            "provider_call_count_observed": 0,
            "provider_calls_permitted": False,
        }


def prepare_guided_provider_request_materialization_review(
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
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestMaterializationReview:
    """Prepare an exact candidate review from one fresh ready recheck."""

    prepared_at = _utc_datetime(evaluation_at, "evaluation_at")
    recheck_review = assess_guided_provider_execution_time_recheck(
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
        preimages=preimages,
        evaluation_at=prepared_at,
    )
    _require_ready_recheck(recheck_review)
    current_authorization_review = (
        assess_guided_provider_execution_authorization_review(
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
            preimages=preimages,
            evaluation_at=prepared_at,
        )
    )
    candidates = _derive_candidates(current_authorization_review)
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
        authorization_review,
        authorization_response,
        execution_time_recheck,
        recheck_review,
        candidates,
        prepared_at,
        execution_time_recheck._expires_at,
    )


def assess_guided_provider_request_materialization_review(
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
    review: GuidedProviderRequestMaterializationReview,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestMaterializationReview:
    """Rebuild the exact review and recheck all current private gates."""

    if type(review) is not GuidedProviderRequestMaterializationReview:
        raise TypeError("review must be an exact request materialization review")
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < review._prepared_at:
        raise ValueError("evaluation_at cannot precede review preparation")

    captured_recheck_review = assess_guided_provider_execution_time_recheck(
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
        preimages=preimages,
        evaluation_at=review._prepared_at,
    )
    _require_ready_recheck(captured_recheck_review)
    captured_authorization_review = (
        assess_guided_provider_execution_authorization_review(
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
            preimages=preimages,
            evaluation_at=review._prepared_at,
        )
    )
    expected_candidates = _derive_candidates(captured_authorization_review)
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
        authorization_review,
        authorization_response,
        execution_time_recheck,
        captured_recheck_review,
        expected_candidates,
        review._prepared_at,
        review._expires_at,
    )
    if review != expected_review:
        raise ValueError("Request materialization review differs from exact context")
    if evaluated_at >= review._expires_at:
        raise ValueError("Request materialization review is no longer current")

    current_recheck_review = assess_guided_provider_execution_time_recheck(
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
        preimages=preimages,
        evaluation_at=evaluated_at,
    )
    _require_ready_recheck(current_recheck_review)
    current_authorization_review = (
        assess_guided_provider_execution_authorization_review(
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
            preimages=preimages,
            evaluation_at=evaluated_at,
        )
    )
    if _derive_candidates(current_authorization_review) != review._items:
        raise ValueError("Request materialization candidates are no longer current")
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
    authorization_review: GuidedProviderExecutionAuthorizationReview,
    authorization_response: GuidedProviderExecutionAuthorizationResponse,
    execution_time_recheck: GuidedProviderExecutionTimeRecheck,
    recheck_review: GuidedProviderExecutionTimeRecheckReview,
    candidates: tuple[GuidedProviderRequestContractCandidate, ...],
    prepared_at: datetime,
    expires_at: datetime,
) -> GuidedProviderRequestMaterializationReview:
    needs_verification = tuple(
        dict.fromkeys(
            (
                *recheck_review.needs_verification,
                "provider_request_materialization_review",
            )
        )
    )
    return GuidedProviderRequestMaterializationReview(
        status=GuidedProviderRequestMaterializationReviewStatus.REVIEW_REQUIRED,
        next_action=_NEXT_ACTION,
        accepted_scope_item_count=recheck_review.accepted_scope_item_count,
        accepted_max_request_count=recheck_review.accepted_max_request_count,
        candidate_count=len(candidates),
        bound_request_count=recheck_review.bound_request_count,
        max_request_count=recheck_review.max_request_count,
        bound_source_line_reference_count=(
            recheck_review.bound_source_line_reference_count
        ),
        bound_google_request_count=recheck_review.bound_google_request_count,
        bound_serpapi_request_count=recheck_review.bound_serpapi_request_count,
        user_stated_source_line_count=(
            recheck_review.user_stated_source_line_count
        ),
        tentative_source_line_count=recheck_review.tentative_source_line_count,
        ai_candidate_source_line_count=(
            recheck_review.ai_candidate_source_line_count
        ),
        estimated_bound_first_paid_tier_google_cost_usd_micros=(
            recheck_review.estimated_bound_first_paid_tier_google_cost_usd_micros
        ),
        accepted_max_first_paid_tier_google_cost_usd_micros=(
            recheck_review.accepted_max_first_paid_tier_google_cost_usd_micros
        ),
        serpapi_bound_plan_credit_count=(
            recheck_review.serpapi_bound_plan_credit_count
        ),
        serpapi_plan_credit_cap=recheck_review.serpapi_plan_credit_cap,
        bound_request_capability_counts=(
            recheck_review.bound_request_capability_counts
        ),
        data_categories=recheck_review.data_categories,
        host_attestation_fresh=recheck_review.host_attestation_fresh,
        tentative_fields=recheck_review.tentative_fields,
        needs_verification=needs_verification,
        _items=candidates,
        _prepared_at=prepared_at,
        _expires_at=expires_at,
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
            authorization_review,
            authorization_response,
            execution_time_recheck,
            recheck_review,
            candidates,
            prepared_at,
            expires_at,
        ),
        _token=_REVIEW_TOKEN,
    )


def _derive_candidates(
    authorization_review: GuidedProviderExecutionAuthorizationReview,
) -> tuple[GuidedProviderRequestContractCandidate, ...]:
    if type(authorization_review) is not GuidedProviderExecutionAuthorizationReview:
        raise TypeError("authorization_review must be exact")
    candidates = tuple(
        _candidate_from_authorization_item(item)
        for item in authorization_review._items
    )
    return tuple(sorted(candidates, key=_item_sort_key))


def _candidate_from_authorization_item(
    item: _GuidedProviderExecutionAuthorizationReviewItem,
) -> GuidedProviderRequestContractCandidate:
    if type(item) is not _GuidedProviderExecutionAuthorizationReviewItem:
        raise TypeError("authorization review item must be exact")
    return GuidedProviderRequestContractCandidate(
        topic=item.topic,
        capability=item.capability,
        request_profile=item.request_profile,
        materialization_kind=(
            _MATERIALIZATION_KIND_BY_REQUEST_PROFILE[item.request_profile]
        ),
        pricing_profile=item.pricing_profile,
        policy_profile=item.policy_profile,
        retention_profile=item.retention_profile,
        target_kind=item.target_kind,
        source_kind=item.source_kind,
        source_line_reference_count=item.source_line_reference_count,
        user_stated_source_line_count=item.user_stated_source_line_count,
        tentative_source_line_count=item.tentative_source_line_count,
        ai_candidate_source_line_count=item.ai_candidate_source_line_count,
        _provider_transmitted_values=item._provider_transmitted_values,
        _local_review_context=item._local_review_context,
        _redacted_provider_identifier_fields=(
            item._redacted_provider_identifier_fields
        ),
        _token=_ITEM_TOKEN,
    )


def _require_ready_recheck(
    review: GuidedProviderExecutionTimeRecheckReview,
) -> None:
    if (
        review.status
        is not GuidedProviderExecutionTimeRecheckStatus
        .READY_FOR_PRIVATE_PROVIDER_REQUEST_MATERIALIZATION_REVIEW
    ):
        raise ValueError("Request materialization review requires a ready recheck")


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
    authorization_review: GuidedProviderExecutionAuthorizationReview,
    authorization_response: GuidedProviderExecutionAuthorizationResponse,
    execution_time_recheck: GuidedProviderExecutionTimeRecheck,
    recheck_review: GuidedProviderExecutionTimeRecheckReview,
    candidates: tuple[GuidedProviderRequestContractCandidate, ...],
    prepared_at: datetime,
    expires_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": (
                GUIDED_PROVIDER_REQUEST_MATERIALIZATION_REVIEW_VERSION
            ),
            "domain": "guided-provider-request-materialization-review",
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
            "provider_execution_time_recheck_review": _private_context_value(
                recheck_review
            ),
            "request_contract_candidates": _private_context_value(candidates),
            "prepared_at": _private_context_value(prepared_at),
            "expires_at": _private_context_value(expires_at),
        }
    )


def _private_field_names(
    values: tuple[tuple[str, object], ...],
    name: str,
) -> set[str]:
    if (
        not isinstance(values, tuple)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or type(item[0]) is not str
            or not item[0]
            or len(item[0]) > 64
            for item in values
        )
        or tuple(sorted(values, key=lambda item: item[0])) != values
        or len({item[0] for item in values}) != len(values)
    ):
        raise ValueError(f"{name} is invalid")
    return {item[0] for item in values}


def _human_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_human_value(item) for item in value]
    return value


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
        raise ValueError("Request materialization capability counts are invalid")


def _item_sort_key(
    item: GuidedProviderRequestContractCandidate,
) -> tuple[object, ...]:
    return (
        item.topic.value,
        item.capability.value,
        item.request_profile.value,
        item.target_kind.value,
        item.source_line_reference_count,
        item.user_stated_source_line_count,
        item.tentative_source_line_count,
        item.ai_candidate_source_line_count,
        _sha256(
            {
                "provider_transmitted_values": _private_context_value(
                    item._provider_transmitted_values
                ),
                "local_review_context": _private_context_value(
                    item._local_review_context
                ),
                "redacted_provider_identifier_fields": list(
                    item._redacted_provider_identifier_fields
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
    "GUIDED_PROVIDER_REQUEST_MATERIALIZATION_REVIEW_VERSION",
    "GuidedProviderRequestContractCandidate",
    "GuidedProviderRequestMaterializationKind",
    "GuidedProviderRequestMaterializationReview",
    "GuidedProviderRequestMaterializationReviewStatus",
    "assess_guided_provider_request_materialization_review",
    "prepare_guided_provider_request_materialization_review",
]
