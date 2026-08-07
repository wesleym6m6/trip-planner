"""Context-bound, offline provider preflight attestations for guided planning.

Phase 5.13 consumes only an exact Phase 5.12 acceptance.  A trusted host may
prepare a short-lived, typed attestation bundle after checking current pricing,
provider policy, billing region, provider-side retention expectations, plan
credits, and boolean credential availability.  The bundle binds those checks
to the full private guided context and accepted scope without storing queries,
payloads, provider identifiers, credentials, or policy text.

This module is deliberately offline.  Profiles are caller-supplied host
attestations, not provider verification.  Even a review-ready result creates no
request, reads no credential, performs no provider call, and grants no external
or canonical authority.  A fresh, explicit execution-authorization review and
an execution-time credential/policy gate remain mandatory.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .facts import GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
from .guided_draft import TripBriefDraft
from .guided_evidence_plan import (
    GuidedEvidenceRequirementPlan,
    GuidedEvidenceTopic,
)
from .guided_itinerary import GuidedItineraryCandidate, GuidedItineraryResponse
from .guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreference,
    _private_context_value,
)
from .guided_provider_scope import (
    GuidedProviderCapability,
    GuidedProviderDataCategory,
    GuidedProviderScopeProposal,
    assess_guided_provider_scope,
)
from .guided_provider_scope_response import (
    GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION,
    GuidedProviderScopeResponse,
    GuidedProviderScopeResponseKind,
    GuidedProviderScopeResponseStatus,
    assess_guided_provider_scope_response,
)
from .guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementResponse,
)
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_PREFLIGHT_VERSION = "guided-provider-preflight/v1"
_MAX_PREFLIGHT_AGE = timedelta(hours=24)
_MAX_ITEMS = 12
_MAX_REQUESTS_PER_ITEM = 32
_MAX_TOTAL_REQUESTS = 32
_MAX_SERPAPI_REMAINING_PLAN_CREDITS = _MAX_TOTAL_REQUESTS
_CONTEXT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_PREFLIGHT_TOKEN = object()
_REVIEW_TOKEN = object()
_REVIEW_ACTION = "review_private_provider_execution_authorization"
_BLOCKED_ACTION = "resolve_private_provider_preflight_blockers"
_REFINE_ACTION = "refine_private_provider_preflight"
_REVIEW_PROMPT = (
    "是否授權依照這份本輪限定的 provider、資料類別與 request cap，進入下一個"
    "明確執行授權步驟？"
)
_REVIEW_DISCLOSURE = (
    "這是 host 提供的短效 preflight attestation，不是 provider 驗證或費用承諾；"
    "真正執行前仍須重新核對 credential、policy、retention 與 exact request，並取得"
    "當輪明確授權。"
)
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
}


class GuidedProviderRequestProfile(str, Enum):
    """A bounded request shape, never a materialized provider request."""

    GOOGLE_PLACES_TEXT_SEARCH_PRO = "google_places_text_search_pro"
    GOOGLE_PLACES_PLACE_DETAILS_ENTERPRISE = (
        "google_places_place_details_enterprise"
    )
    GOOGLE_ROUTES_COMPUTE_ROUTES_ESSENTIALS = (
        "google_routes_compute_routes_essentials"
    )
    SERPAPI_GOOGLE_HOTELS_PLAN_CREDIT = (
        "serpapi_google_hotels_plan_credit"
    )


class GuidedProviderPricingProfile(str, Enum):
    """Versioned official-pricing snapshot named by the trusted host."""

    GOOGLE_TEXT_SEARCH_PRO_GLOBAL_2026_07_31 = (
        "google-text-search-pro-global-2026-07-31"
    )
    GOOGLE_PLACE_DETAILS_ENTERPRISE_GLOBAL_2026_07_31 = (
        "google-place-details-enterprise-global-2026-07-31"
    )
    GOOGLE_COMPUTE_ROUTES_ESSENTIALS_GLOBAL_2026_07_31 = (
        "google-compute-routes-essentials-global-2026-07-31"
    )
    SERPAPI_PLAN_CREDIT_2026_08_07 = "serpapi-plan-credit-2026-08-07"


class GuidedProviderPolicyProfile(str, Enum):
    """Versioned terms/policy profile attested by the trusted host."""

    GOOGLE_MAPS_NON_EEA_2026_06_10 = GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
    SERPAPI_TERMS_2026_04_08 = "serpapi-terms-2026-04-08"


class GuidedProviderRetentionProfile(str, Enum):
    """Expected provider/local retention mode for the later exact request."""

    GOOGLE_MAPS_PROCESS_LOCAL_ONLY = "google-maps-process-local-only"
    SERPAPI_STANDARD_PROVIDER_STORAGE = "serpapi-standard-provider-storage"
    SERPAPI_ZERO_TRACE = "serpapi-zero-trace"


class GuidedProviderBillingRegion(str, Enum):
    """Opaque billing-region classification; no address is retained."""

    NON_EEA = "non_eea"
    EEA = "eea"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class GuidedProviderCredentialStatus(str, Enum):
    """Boolean-equivalent host attestation without credential material."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class GuidedProviderSerpApiZeroTraceStatus(str, Enum):
    """Opaque host attestation of the Enterprise-only ZeroTrace entitlement."""

    NOT_APPLICABLE = "not_applicable"
    ENTITLED = "entitled"
    NOT_ENTITLED = "not_entitled"
    UNKNOWN = "unknown"


class GuidedProviderPreflightStatus(str, Enum):
    """Whether the attested scope needs repair, unblocking, or review."""

    NEEDS_REFINEMENT = "needs_refinement"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"


class GuidedProviderPreflightProblemCode(str, Enum):
    """Redacted reasons the attested preflight cannot advance."""

    ACCEPTED_SCOPE_ITEM_MISSING = "accepted_scope_item_missing"
    UNACCEPTED_SCOPE_ITEM = "unaccepted_scope_item"
    TOPIC_ATTESTED_MULTIPLE_TIMES = "topic_attested_multiple_times"
    CAPABILITY_MISMATCH = "capability_mismatch"
    REQUEST_PROFILE_MISMATCH = "request_profile_mismatch"
    PRICING_PROFILE_MISMATCH = "pricing_profile_mismatch"
    POLICY_PROFILE_MISMATCH = "policy_profile_mismatch"
    RETENTION_PROFILE_MISMATCH = "retention_profile_mismatch"
    REQUEST_LIMIT_EXCEEDS_ACCEPTED_SCOPE = (
        "request_limit_exceeds_accepted_scope"
    )
    CREDENTIAL_STATUS_INCONSISTENT = "credential_status_inconsistent"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    BILLING_REGION_UNCONFIRMED = "billing_region_unconfirmed"
    PREFLIGHT_ATTESTATION_NOT_CURRENT = "preflight_attestation_not_current"
    SERPAPI_PLAN_STATE_UNCONFIRMED = "serpapi_plan_state_unconfirmed"
    SERPAPI_PLAN_CREDITS_INSUFFICIENT = "serpapi_plan_credits_insufficient"
    SERPAPI_AUTOMATIC_RENEWAL_ENABLED = (
        "serpapi_automatic_renewal_enabled"
    )
    SERPAPI_ZERO_TRACE_ENTITLEMENT_UNCONFIRMED = (
        "serpapi_zero_trace_entitlement_unconfirmed"
    )
    SERPAPI_ZERO_TRACE_STATUS_MISMATCH = (
        "serpapi_zero_trace_status_mismatch"
    )


_REQUEST_PROFILE_BY_CAPABILITY = {
    GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: (
        GuidedProviderRequestProfile.GOOGLE_PLACES_TEXT_SEARCH_PRO
    ),
    GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: (
        GuidedProviderRequestProfile.GOOGLE_PLACES_PLACE_DETAILS_ENTERPRISE
    ),
    GuidedProviderCapability.GOOGLE_ROUTES: (
        GuidedProviderRequestProfile.GOOGLE_ROUTES_COMPUTE_ROUTES_ESSENTIALS
    ),
    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS: (
        GuidedProviderRequestProfile.SERPAPI_GOOGLE_HOTELS_PLAN_CREDIT
    ),
}
_PRICING_PROFILE_BY_CAPABILITY = {
    GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: (
        GuidedProviderPricingProfile.GOOGLE_TEXT_SEARCH_PRO_GLOBAL_2026_07_31
    ),
    GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: (
        GuidedProviderPricingProfile.GOOGLE_PLACE_DETAILS_ENTERPRISE_GLOBAL_2026_07_31
    ),
    GuidedProviderCapability.GOOGLE_ROUTES: (
        GuidedProviderPricingProfile.GOOGLE_COMPUTE_ROUTES_ESSENTIALS_GLOBAL_2026_07_31
    ),
    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS: (
        GuidedProviderPricingProfile.SERPAPI_PLAN_CREDIT_2026_08_07
    ),
}
_POLICY_PROFILE_BY_CAPABILITY = {
    GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: (
        GuidedProviderPolicyProfile.GOOGLE_MAPS_NON_EEA_2026_06_10
    ),
    GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: (
        GuidedProviderPolicyProfile.GOOGLE_MAPS_NON_EEA_2026_06_10
    ),
    GuidedProviderCapability.GOOGLE_ROUTES: (
        GuidedProviderPolicyProfile.GOOGLE_MAPS_NON_EEA_2026_06_10
    ),
    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS: (
        GuidedProviderPolicyProfile.SERPAPI_TERMS_2026_04_08
    ),
}
_GOOGLE_CAPABILITIES = frozenset(
    {
        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
        GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS,
        GuidedProviderCapability.GOOGLE_ROUTES,
    }
)
_ALLOWED_RETENTION_BY_CAPABILITY = {
    capability: (GuidedProviderRetentionProfile.GOOGLE_MAPS_PROCESS_LOCAL_ONLY,)
    for capability in _GOOGLE_CAPABILITIES
}
_ALLOWED_RETENTION_BY_CAPABILITY[
    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
] = (
    GuidedProviderRetentionProfile.SERPAPI_STANDARD_PROVIDER_STORAGE,
    GuidedProviderRetentionProfile.SERPAPI_ZERO_TRACE,
)
_FIRST_PAID_TIER_USD_MICROS_PER_REQUEST = {
    GuidedProviderPricingProfile.GOOGLE_TEXT_SEARCH_PRO_GLOBAL_2026_07_31: 32_000,
    GuidedProviderPricingProfile.GOOGLE_PLACE_DETAILS_ENTERPRISE_GLOBAL_2026_07_31: 20_000,
    GuidedProviderPricingProfile.GOOGLE_COMPUTE_ROUTES_ESSENTIALS_GLOBAL_2026_07_31: 5_000,
}
_MONTHLY_FREE_REQUEST_CAP = {
    GuidedProviderPricingProfile.GOOGLE_TEXT_SEARCH_PRO_GLOBAL_2026_07_31: 5_000,
    GuidedProviderPricingProfile.GOOGLE_PLACE_DETAILS_ENTERPRISE_GLOBAL_2026_07_31: 1_000,
    GuidedProviderPricingProfile.GOOGLE_COMPUTE_ROUTES_ESSENTIALS_GLOBAL_2026_07_31: 10_000,
}
_STRUCTURAL_PROBLEMS = frozenset(
    {
        GuidedProviderPreflightProblemCode.ACCEPTED_SCOPE_ITEM_MISSING,
        GuidedProviderPreflightProblemCode.UNACCEPTED_SCOPE_ITEM,
        GuidedProviderPreflightProblemCode.TOPIC_ATTESTED_MULTIPLE_TIMES,
        GuidedProviderPreflightProblemCode.CAPABILITY_MISMATCH,
        GuidedProviderPreflightProblemCode.REQUEST_PROFILE_MISMATCH,
        GuidedProviderPreflightProblemCode.PRICING_PROFILE_MISMATCH,
        GuidedProviderPreflightProblemCode.POLICY_PROFILE_MISMATCH,
        GuidedProviderPreflightProblemCode.RETENTION_PROFILE_MISMATCH,
        GuidedProviderPreflightProblemCode.REQUEST_LIMIT_EXCEEDS_ACCEPTED_SCOPE,
        GuidedProviderPreflightProblemCode.CREDENTIAL_STATUS_INCONSISTENT,
        GuidedProviderPreflightProblemCode.SERPAPI_ZERO_TRACE_STATUS_MISMATCH,
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderPreflightItem:
    """One typed host attestation for one accepted provider-scope topic."""

    topic: GuidedEvidenceTopic
    capability: GuidedProviderCapability
    request_profile: GuidedProviderRequestProfile
    pricing_profile: GuidedProviderPricingProfile
    policy_profile: GuidedProviderPolicyProfile
    retention_profile: GuidedProviderRetentionProfile
    billing_region: GuidedProviderBillingRegion
    credential_status: GuidedProviderCredentialStatus
    max_request_count: int
    serpapi_remaining_plan_credits: int | None = field(default=None, repr=False)
    serpapi_automatic_renewal_enabled: bool | None = field(
        default=None,
        repr=False,
    )
    serpapi_zero_trace_status: GuidedProviderSerpApiZeroTraceStatus = field(
        default=GuidedProviderSerpApiZeroTraceStatus.NOT_APPLICABLE,
        repr=False,
    )

    def __post_init__(self) -> None:
        exact_enums = (
            ("topic", self.topic, GuidedEvidenceTopic),
            ("capability", self.capability, GuidedProviderCapability),
            ("request_profile", self.request_profile, GuidedProviderRequestProfile),
            ("pricing_profile", self.pricing_profile, GuidedProviderPricingProfile),
            ("policy_profile", self.policy_profile, GuidedProviderPolicyProfile),
            (
                "retention_profile",
                self.retention_profile,
                GuidedProviderRetentionProfile,
            ),
            ("billing_region", self.billing_region, GuidedProviderBillingRegion),
            (
                "credential_status",
                self.credential_status,
                GuidedProviderCredentialStatus,
            ),
            (
                "serpapi_zero_trace_status",
                self.serpapi_zero_trace_status,
                GuidedProviderSerpApiZeroTraceStatus,
            ),
        )
        for name, value, expected_type in exact_enums:
            if type(value) is not expected_type:
                raise TypeError(f"GuidedProviderPreflightItem.{name} must be exact")
        if type(self.max_request_count) is not int or not (
            1 <= self.max_request_count <= _MAX_REQUESTS_PER_ITEM
        ):
            raise ValueError(
                "GuidedProviderPreflightItem.max_request_count is invalid"
            )
        if self.capability is GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS:
            if (
                self.serpapi_remaining_plan_credits is not None
                and (
                    type(self.serpapi_remaining_plan_credits) is not int
                    or not 0
                    <= self.serpapi_remaining_plan_credits
                    <= _MAX_SERPAPI_REMAINING_PLAN_CREDITS
                )
            ) or (
                self.serpapi_automatic_renewal_enabled is not None
                and type(self.serpapi_automatic_renewal_enabled) is not bool
            ):
                raise ValueError(
                    "SerpApi preflight items require bounded plan state"
                )
        elif (
            self.serpapi_remaining_plan_credits is not None
            or self.serpapi_automatic_renewal_enabled is not None
            or self.serpapi_zero_trace_status
            is not GuidedProviderSerpApiZeroTraceStatus.NOT_APPLICABLE
        ):
            raise ValueError(
                "Google preflight items cannot contain SerpApi plan state"
            )

    def __repr__(self) -> str:
        return (
            "GuidedProviderPreflightItem("
            f"topic={self.topic.value!r}, "
            f"capability={self.capability.value!r}, "
            f"request_profile={self.request_profile.value!r}, "
            f"max_request_count={self.max_request_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderPreflight:
    """Short-lived host attestation bound to an exact accepted scope."""

    items: tuple[GuidedProviderPreflightItem, ...] = field(repr=False)
    checked_at: datetime = field(repr=False)
    expires_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PREFLIGHT_TOKEN:
            raise ValueError(
                "Guided provider preflight requires trusted host preparation"
            )
        if (
            not isinstance(self.items, tuple)
            or not 1 <= len(self.items) <= _MAX_ITEMS
            or any(type(item) is not GuidedProviderPreflightItem for item in self.items)
        ):
            raise ValueError("GuidedProviderPreflight.items is invalid")
        checked = _utc_datetime(self.checked_at, "checked_at")
        expires = _utc_datetime(self.expires_at, "expires_at")
        if not checked < expires <= checked + _MAX_PREFLIGHT_AGE:
            raise ValueError("GuidedProviderPreflight expiry is invalid")
        if (
            type(self._context_fingerprint) is not str
            or _CONTEXT_FINGERPRINT_RE.fullmatch(self._context_fingerprint) is None
        ):
            raise ValueError("GuidedProviderPreflight context fingerprint is invalid")
        ordered = tuple(sorted(self.items, key=_item_sort_key))
        object.__setattr__(self, "items", ordered)
        object.__setattr__(self, "checked_at", checked)
        object.__setattr__(self, "expires_at", expires)

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderPreflight("
            f"item_count={len(self.items)!r}, "
            f"max_request_count="
            f"{sum(item.max_request_count for item in self.items)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderPreflightReview:
    """Redacted assessment of one exact, short-lived host attestation."""

    status: GuidedProviderPreflightStatus
    next_action: str
    accepted_scope_item_count: int
    accepted_max_request_count: int
    preflight_item_count: int
    max_request_count: int
    items: tuple[GuidedProviderPreflightItem, ...] = field(default=(), repr=False)
    data_categories: tuple[GuidedProviderDataCategory, ...] = ()
    problem_codes: tuple[GuidedProviderPreflightProblemCode, ...] = ()
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    host_attestation_fresh: bool = False
    contract_version: str = GUIDED_PROVIDER_PREFLIGHT_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided provider preflight reviews require the assessor"
            )
        if type(self.status) is not GuidedProviderPreflightStatus:
            raise TypeError("GuidedProviderPreflightReview.status must be exact")
        for name, value, maximum in (
            ("accepted_scope_item_count", self.accepted_scope_item_count, _MAX_ITEMS),
            (
                "accepted_max_request_count",
                self.accepted_max_request_count,
                _MAX_TOTAL_REQUESTS,
            ),
            ("preflight_item_count", self.preflight_item_count, _MAX_ITEMS),
            (
                "max_request_count",
                self.max_request_count,
                _MAX_ITEMS * _MAX_REQUESTS_PER_ITEM,
            ),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"GuidedProviderPreflightReview.{name} is invalid")
        if (
            self.accepted_scope_item_count < 1
            or self.accepted_max_request_count < self.accepted_scope_item_count
            or self.preflight_item_count != len(self.items)
            or self.max_request_count
            != sum(item.max_request_count for item in self.items)
        ):
            raise ValueError("GuidedProviderPreflightReview counts are inconsistent")
        if (
            not isinstance(self.data_categories, tuple)
            or any(
                type(item) is not GuidedProviderDataCategory
                for item in self.data_categories
            )
            or tuple(sorted(set(self.data_categories), key=lambda item: item.value))
            != self.data_categories
            or not isinstance(self.problem_codes, tuple)
            or any(
                type(item) is not GuidedProviderPreflightProblemCode
                for item in self.problem_codes
            )
            or tuple(sorted(set(self.problem_codes), key=lambda item: item.value))
            != self.problem_codes
        ):
            raise ValueError("GuidedProviderPreflightReview collections are invalid")
        expected_action = {
            GuidedProviderPreflightStatus.NEEDS_REFINEMENT: _REFINE_ACTION,
            GuidedProviderPreflightStatus.BLOCKED: _BLOCKED_ACTION,
            GuidedProviderPreflightStatus.REVIEW_REQUIRED: _REVIEW_ACTION,
        }[self.status]
        if self.next_action != expected_action:
            raise ValueError("GuidedProviderPreflightReview status/action conflict")
        problems = set(self.problem_codes)
        if self.status is GuidedProviderPreflightStatus.REVIEW_REQUIRED:
            if (
                problems
                or not self.host_attestation_fresh
                or self.preflight_item_count != self.accepted_scope_item_count
            ):
                raise ValueError("Review-ready provider preflight is inconsistent")
        elif self.status is GuidedProviderPreflightStatus.NEEDS_REFINEMENT:
            if not problems.intersection(_STRUCTURAL_PROBLEMS):
                raise ValueError("Needs-refinement preflight lacks structural problems")
        elif not problems or problems.intersection(_STRUCTURAL_PROBLEMS):
            raise ValueError("Blocked preflight problems are inconsistent")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
            or self.contract_version != GUIDED_PROVIDER_PREFLIGHT_VERSION
        ):
            raise ValueError("GuidedProviderPreflightReview safe metadata is invalid")

    @property
    def may_present_execution_review(self) -> bool:
        return self.status is GuidedProviderPreflightStatus.REVIEW_REQUIRED

    @property
    def review_prompt(self) -> str | None:
        return _REVIEW_PROMPT if self.may_present_execution_review else None

    @property
    def review_disclosure(self) -> str | None:
        return _REVIEW_DISCLOSURE if self.may_present_execution_review else None

    def __repr__(self) -> str:
        return (
            "GuidedProviderPreflightReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"problem_count={len(self.problem_codes)!r}, "
            f"preflight_item_count={self.preflight_item_count!r}, "
            f"max_request_count={self.max_request_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return safe attestation metadata without private request content."""

        structurally_valid = not set(self.problem_codes).intersection(
            _STRUCTURAL_PROBLEMS
        )
        visible_items = self.items if structurally_valid else ()
        google_cost = sum(_google_list_cost(item) for item in visible_items)
        serpapi_credit_cap = sum(
            item.max_request_count
            for item in visible_items
            if item.capability
            is GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
        )
        credentials_available = bool(visible_items) and all(
            item.credential_status is GuidedProviderCredentialStatus.AVAILABLE
            for item in visible_items
        )
        billing_region_attested = bool(visible_items) and all(
            _billing_region_matches(item) for item in visible_items
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": self.may_present_execution_review,
            "requires_user_review": self.may_present_execution_review,
            "requires_user_decision": self.may_present_execution_review,
            "review_prompt": self.review_prompt,
            "review_disclosure": self.review_disclosure,
            "provider_preflight": {
                "exact_private_context_bound": True,
                "host_attestation_fresh": self.host_attestation_fresh,
                "attestation_max_age_hours": 24,
                "accepted_scope_item_count": self.accepted_scope_item_count,
                "accepted_max_request_count": self.accepted_max_request_count,
                "preflight_item_count": self.preflight_item_count,
                "items": [_safe_item(item) for item in visible_items],
                "data_categories": [
                    item.value for item in self.data_categories
                ] if structurally_valid else [],
                "max_request_count": self.max_request_count,
                "within_accepted_request_cap": (
                    structurally_valid
                    and self.max_request_count <= self.accepted_max_request_count
                ),
                "host_pricing_profile_attested": structurally_valid,
                "pricing_verified_by_offline_contract": False,
                "estimated_first_paid_tier_google_cost_usd_micros": google_cost,
                "serpapi_plan_credit_cap": serpapi_credit_cap,
                "all_provider_costs_have_currency_list_rate_estimates": (
                    serpapi_credit_cap == 0
                ),
                "monthly_free_usage_remaining_checked": False,
                "cost_estimate_is_hard_currency_cap": False,
                "host_provider_policy_profile_attested": structurally_valid,
                "provider_policy_verified_by_offline_contract": False,
                "billing_region_attested": billing_region_attested,
                "expected_provider_retention_profile_attested": structurally_valid,
                "provider_retention_verified_by_offline_contract": False,
                "local_result_retention": "process_local_only",
                "credential_availability_attested": structurally_valid,
                "credentials_available": credentials_available,
                "credentials_accessed": False,
                "attestation_sources": {
                    "pricing": "official_provider_pricing_documentation",
                    "provider_policy": "official_provider_terms_documentation",
                    "billing_region": "host_billing_account_configuration",
                    "credential_availability": "host_secret_store_status",
                },
                "execution_time_recheck_required": True,
                "explicit_execution_authorization_required": True,
                "preflight_is_provider_authorization": False,
                "provider_scope_authorized": False,
                "provider_requests_created": False,
                "provider_calls_permitted": False,
                "is_travel_ready": False,
                "decision_state": DecisionState.CANDIDATE.value,
                "evidence_state": EvidenceState.UNVERIFIED.value,
                "supports_authoritative_use": False,
            },
            "problems": [item.value for item in self.problem_codes],
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "official_sources_fetched_by_contract": False,
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
        }


def _preflight_context_fingerprint(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    response: GuidedProviderScopeResponse,
    response_review: object,
    items: tuple[GuidedProviderPreflightItem, ...],
    checked_at: datetime,
    expires_at: datetime,
) -> str:
    canonical = {
        "contract_version": GUIDED_PROVIDER_PREFLIGHT_VERSION,
        "scope_response_contract_version": GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION,
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
        "provider_scope_response": _private_context_value(response),
        "provider_scope_response_review": _private_context_value(response_review),
        "items": [_private_context_value(item) for item in sorted(items, key=_item_sort_key)],
        "checked_at": _private_context_value(checked_at),
        "expires_at": _private_context_value(expires_at),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_guided_provider_preflight(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    response: GuidedProviderScopeResponse,
    *,
    items: tuple[GuidedProviderPreflightItem, ...],
    checked_at: datetime,
    expires_at: datetime,
) -> GuidedProviderPreflight:
    """Bind trusted-host attestations to one exact Phase 5.12 acceptance."""

    response_review = assess_guided_provider_scope_response(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
        response,
    )
    if (
        response.kind
        is not GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE
        or response_review.status
        is not GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_PREFLIGHT
    ):
        raise ValueError(
            "Provider preflight requires an exact accepted provider scope"
        )
    if (
        not isinstance(items, tuple)
        or not items
        or any(type(item) is not GuidedProviderPreflightItem for item in items)
    ):
        raise TypeError("items must contain exact GuidedProviderPreflightItem values")
    checked = _utc_datetime(checked_at, "checked_at")
    expires = _utc_datetime(expires_at, "expires_at")
    ordered = tuple(sorted(items, key=_item_sort_key))
    return GuidedProviderPreflight(
        items=ordered,
        checked_at=checked,
        expires_at=expires,
        _context_fingerprint=_preflight_context_fingerprint(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            response,
            response_review,
            ordered,
            checked,
            expires,
        ),
        _token=_PREFLIGHT_TOKEN,
    )


def assess_guided_provider_preflight(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    response: GuidedProviderScopeResponse,
    preflight: GuidedProviderPreflight,
    *,
    evaluation_at: datetime,
) -> GuidedProviderPreflightReview:
    """Revalidate and assess one exact offline preflight attestation bundle."""

    if type(preflight) is not GuidedProviderPreflight:
        raise TypeError("preflight must be an exact GuidedProviderPreflight")
    evaluated = _utc_datetime(evaluation_at, "evaluation_at")
    response_review = assess_guided_provider_scope_response(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
        response,
    )
    scope_review = assess_guided_provider_scope(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
    )
    if (
        response.kind
        is not GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE
        or response_review.status
        is not GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_PREFLIGHT
    ):
        raise ValueError(
            "Provider preflight requires an exact accepted provider scope"
        )
    expected_fingerprint = _preflight_context_fingerprint(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
        response,
        response_review,
        preflight.items,
        preflight.checked_at,
        preflight.expires_at,
    )
    if preflight._context_fingerprint != expected_fingerprint:
        raise ValueError(
            "Provider preflight does not match the current private context"
        )

    problems: set[GuidedProviderPreflightProblemCode] = set()
    scope_by_topic = {item.topic: item for item in provider_scope.items}
    counts = Counter(item.topic for item in preflight.items)
    if any(counts[topic] == 0 for topic in scope_by_topic):
        problems.add(
            GuidedProviderPreflightProblemCode.ACCEPTED_SCOPE_ITEM_MISSING
        )
    if any(topic not in scope_by_topic for topic in counts):
        problems.add(GuidedProviderPreflightProblemCode.UNACCEPTED_SCOPE_ITEM)
    if any(count > 1 for count in counts.values()):
        problems.add(
            GuidedProviderPreflightProblemCode.TOPIC_ATTESTED_MULTIPLE_TIMES
        )

    for item in preflight.items:
        accepted = scope_by_topic.get(item.topic)
        if accepted is None:
            continue
        if item.capability is not accepted.capability:
            problems.add(GuidedProviderPreflightProblemCode.CAPABILITY_MISMATCH)
        if item.request_profile is not _REQUEST_PROFILE_BY_CAPABILITY[item.capability]:
            problems.add(
                GuidedProviderPreflightProblemCode.REQUEST_PROFILE_MISMATCH
            )
        if item.pricing_profile is not _PRICING_PROFILE_BY_CAPABILITY[item.capability]:
            problems.add(
                GuidedProviderPreflightProblemCode.PRICING_PROFILE_MISMATCH
            )
        if item.policy_profile is not _POLICY_PROFILE_BY_CAPABILITY[item.capability]:
            problems.add(
                GuidedProviderPreflightProblemCode.POLICY_PROFILE_MISMATCH
            )
        if item.retention_profile not in _ALLOWED_RETENTION_BY_CAPABILITY[
            item.capability
        ]:
            problems.add(
                GuidedProviderPreflightProblemCode.RETENTION_PROFILE_MISMATCH
            )
        if item.capability is GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS:
            if (
                item.retention_profile
                is GuidedProviderRetentionProfile.SERPAPI_STANDARD_PROVIDER_STORAGE
                and item.serpapi_zero_trace_status
                is not GuidedProviderSerpApiZeroTraceStatus.NOT_APPLICABLE
            ):
                problems.add(
                    GuidedProviderPreflightProblemCode
                    .SERPAPI_ZERO_TRACE_STATUS_MISMATCH
                )
            elif (
                item.retention_profile
                is GuidedProviderRetentionProfile.SERPAPI_ZERO_TRACE
                and item.serpapi_zero_trace_status
                is not GuidedProviderSerpApiZeroTraceStatus.ENTITLED
            ):
                problems.add(
                    GuidedProviderPreflightProblemCode
                    .SERPAPI_ZERO_TRACE_ENTITLEMENT_UNCONFIRMED
                )
        if item.max_request_count > accepted.max_request_count:
            problems.add(
                GuidedProviderPreflightProblemCode.REQUEST_LIMIT_EXCEEDS_ACCEPTED_SCOPE
            )
        if item.credential_status is GuidedProviderCredentialStatus.UNAVAILABLE:
            problems.add(
                GuidedProviderPreflightProblemCode.CREDENTIAL_UNAVAILABLE
            )
        if not _billing_region_matches(item):
            problems.add(
                GuidedProviderPreflightProblemCode.BILLING_REGION_UNCONFIRMED
            )
        if item.capability is GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS:
            if (
                item.serpapi_remaining_plan_credits is None
                or item.serpapi_automatic_renewal_enabled is None
            ):
                problems.add(
                    GuidedProviderPreflightProblemCode.SERPAPI_PLAN_STATE_UNCONFIRMED
                )
            elif item.serpapi_remaining_plan_credits < item.max_request_count:
                problems.add(
                    GuidedProviderPreflightProblemCode.SERPAPI_PLAN_CREDITS_INSUFFICIENT
                )
            if item.serpapi_automatic_renewal_enabled is True:
                problems.add(
                    GuidedProviderPreflightProblemCode.SERPAPI_AUTOMATIC_RENEWAL_ENABLED
                )

    statuses_by_provider: dict[str, set[GuidedProviderCredentialStatus]] = {}
    for item in preflight.items:
        provider = _provider_family(item.capability)
        statuses_by_provider.setdefault(provider, set()).add(
            item.credential_status
        )
    if any(len(statuses) != 1 for statuses in statuses_by_provider.values()):
        problems.add(
            GuidedProviderPreflightProblemCode.CREDENTIAL_STATUS_INCONSISTENT
        )

    host_attestation_fresh = (
        preflight.checked_at <= evaluated < preflight.expires_at
        and evaluated - preflight.checked_at <= _MAX_PREFLIGHT_AGE
    )
    if not host_attestation_fresh:
        problems.add(
            GuidedProviderPreflightProblemCode.PREFLIGHT_ATTESTATION_NOT_CURRENT
        )

    problem_codes = tuple(sorted(problems, key=lambda item: item.value))
    if problems.intersection(_STRUCTURAL_PROBLEMS):
        status = GuidedProviderPreflightStatus.NEEDS_REFINEMENT
        next_action = _REFINE_ACTION
    elif problems:
        status = GuidedProviderPreflightStatus.BLOCKED
        next_action = _BLOCKED_ACTION
    else:
        status = GuidedProviderPreflightStatus.REVIEW_REQUIRED
        next_action = _REVIEW_ACTION
    needs_verification = tuple(
        dict.fromkeys((*response_review.needs_verification, "provider_preflight"))
    )
    return GuidedProviderPreflightReview(
        status=status,
        next_action=next_action,
        accepted_scope_item_count=response_review.scope_item_count,
        accepted_max_request_count=response_review.max_request_count,
        preflight_item_count=len(preflight.items),
        max_request_count=sum(
            item.max_request_count for item in preflight.items
        ),
        items=preflight.items,
        data_categories=scope_review.data_categories,
        problem_codes=problem_codes,
        tentative_fields=response_review.tentative_fields,
        needs_verification=needs_verification,
        host_attestation_fresh=host_attestation_fresh,
        _token=_REVIEW_TOKEN,
    )


def _safe_item(item: GuidedProviderPreflightItem) -> dict[str, object]:
    pricing = item.pricing_profile
    is_serpapi = (
        item.capability is GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
    )
    plan_credits_sufficient = (
        (
            item.serpapi_remaining_plan_credits is not None
            and item.serpapi_remaining_plan_credits >= item.max_request_count
        )
        if is_serpapi
        else None
    )
    return {
        "topic": item.topic.value,
        "capability": item.capability.value,
        "request_profile": item.request_profile.value,
        "pricing_profile": pricing.value,
        "policy_profile": item.policy_profile.value,
        "expected_retention_profile": item.retention_profile.value,
        "max_request_count": item.max_request_count,
        "billing_region_attested": _billing_region_matches(item),
        "credential_available": (
            item.credential_status is GuidedProviderCredentialStatus.AVAILABLE
        ),
        "first_paid_tier_usd_micros_per_request": (
            _FIRST_PAID_TIER_USD_MICROS_PER_REQUEST.get(pricing)
        ),
        "published_monthly_free_request_cap": (
            _MONTHLY_FREE_REQUEST_CAP.get(pricing)
        ),
        "serpapi_plan_credit_per_successful_uncached_search": (
            1 if is_serpapi else None
        ),
        "serpapi_plan_credits_sufficient": plan_credits_sufficient,
        "serpapi_automatic_renewal_disabled": (
            item.serpapi_automatic_renewal_enabled is False
            if is_serpapi else None
        ),
        "serpapi_zero_trace_entitlement_attested": (
            item.serpapi_zero_trace_status
            is GuidedProviderSerpApiZeroTraceStatus.ENTITLED
            if item.retention_profile
            is GuidedProviderRetentionProfile.SERPAPI_ZERO_TRACE
            else None
        ),
    }


def _google_list_cost(item: GuidedProviderPreflightItem) -> int:
    unit = _FIRST_PAID_TIER_USD_MICROS_PER_REQUEST.get(item.pricing_profile, 0)
    return unit * item.max_request_count


def _billing_region_matches(item: GuidedProviderPreflightItem) -> bool:
    if item.capability in _GOOGLE_CAPABILITIES:
        return item.billing_region is GuidedProviderBillingRegion.NON_EEA
    return item.billing_region is GuidedProviderBillingRegion.NOT_APPLICABLE


def _provider_family(capability: GuidedProviderCapability) -> str:
    return "google_maps" if capability in _GOOGLE_CAPABILITIES else "serpapi"


def _item_sort_key(item: GuidedProviderPreflightItem) -> tuple[object, ...]:
    return (
        item.topic.value,
        item.capability.value,
        item.request_profile.value,
        item.pricing_profile.value,
        item.policy_profile.value,
        item.retention_profile.value,
        item.billing_region.value,
        item.credential_status.value,
        item.max_request_count,
        item.serpapi_remaining_plan_credits,
        item.serpapi_automatic_renewal_enabled,
        item.serpapi_zero_trace_status.value,
    )


def _utc_datetime(value: datetime, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{name} must be an exact UTC datetime")
    return value


__all__ = [
    "GUIDED_PROVIDER_PREFLIGHT_VERSION",
    "GuidedProviderBillingRegion",
    "GuidedProviderCredentialStatus",
    "GuidedProviderPolicyProfile",
    "GuidedProviderPreflight",
    "GuidedProviderPreflightItem",
    "GuidedProviderPreflightProblemCode",
    "GuidedProviderPreflightReview",
    "GuidedProviderPreflightStatus",
    "GuidedProviderPricingProfile",
    "GuidedProviderRequestProfile",
    "GuidedProviderRetentionProfile",
    "GuidedProviderSerpApiZeroTraceStatus",
    "assess_guided_provider_preflight",
    "prepare_guided_provider_preflight",
]
