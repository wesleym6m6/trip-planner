"""Short-lived trusted-host recheck before provider request materialization.

Phase 5.19 consumes only a fresh accepted Phase 5.18 response, the same exact
typed target preimages, and a new bounded tuple of host attestation items.  It
rechecks pricing, policy, retention, billing classification, boolean credential
availability, SerpApi plan state, and trusted endpoint freshness without
reading a secret or contacting a provider.

A ready result may only prepare a separate exact request-materialization
review.  It does not activate execution authority, create a provider or HTTP
request, call a provider, reserve spend, persist data, schedule, render, deploy,
confirm, or mutate canonical state.  Changed commercial/policy context requires
a new preflight and authorization chain; transient availability failures remain
blocked and retryable.
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

from .guided_draft import TripBriefDraft
from .guided_evidence_plan import GuidedEvidenceRequirementPlan
from .guided_itinerary import GuidedItineraryCandidate, GuidedItineraryResponse
from .guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreference,
    _private_context_value,
)
from .guided_provider_execution_authorization_response import (
    GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_RESPONSE_VERSION,
    GuidedProviderExecutionAuthorizationResponse,
    GuidedProviderExecutionAuthorizationResponseKind,
    GuidedProviderExecutionAuthorizationResponseReview,
    GuidedProviderExecutionAuthorizationResponseStatus,
    assess_guided_provider_execution_authorization_response,
)
from .guided_provider_execution_authorization_review import (
    GuidedProviderExecutionAuthorizationReview,
)
from .guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetBindings,
    GuidedProviderExecutionTargetPreimage,
)
from .guided_provider_execution_targets import GuidedProviderExecutionTargets
from .guided_provider_preflight import (
    GuidedProviderBillingRegion,
    GuidedProviderCredentialStatus,
    GuidedProviderPolicyProfile,
    GuidedProviderPreflight,
    GuidedProviderPreflightItem,
    GuidedProviderPreflightProblemCode,
    GuidedProviderPreflightReview,
    GuidedProviderPreflightStatus,
    GuidedProviderPricingProfile,
    GuidedProviderRequestProfile,
    GuidedProviderRetentionProfile,
    GuidedProviderSerpApiZeroTraceStatus,
    assess_guided_provider_preflight,
    prepare_guided_provider_preflight,
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
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_EXECUTION_TIME_RECHECK_VERSION = (
    "guided-provider-execution-time-recheck/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ITEMS = 12
_MAX_REQUESTS = 32
_MAX_SOURCE_LINE_REFERENCES = _MAX_ITEMS * 32
_MAX_GOOGLE_LIST_COST_USD_MICROS = _MAX_REQUESTS * 32_000
_RECHECK_TTL = timedelta(minutes=5)
_READY_ACTION = "prepare_private_provider_request_materialization_review"
_REAUTHORIZE_ACTION = "prepare_private_provider_preflight_review"
_BLOCKED_ACTION = "refresh_private_provider_execution_time_recheck"
_RECHECK_TOKEN = object()
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
}


class GuidedProviderExecutionTimeRecheckStatus(str, Enum):
    """Outcome of the short-lived execution-time host re-attestation."""

    READY_FOR_PRIVATE_PROVIDER_REQUEST_MATERIALIZATION_REVIEW = (
        "ready_for_private_provider_request_materialization_review"
    )
    NEEDS_NEW_PRIVATE_PROVIDER_PREFLIGHT_REVIEW = (
        "needs_new_private_provider_preflight_review"
    )
    BLOCKED = "blocked"


class GuidedProviderExecutionTimeRecheckProblemCode(str, Enum):
    """Accepted review context that changed and therefore needs reauthorization."""

    PRICING_PROFILE_CHANGED = "pricing_profile_changed"
    POLICY_PROFILE_CHANGED = "policy_profile_changed"
    RETENTION_PROFILE_CHANGED = "retention_profile_changed"
    BILLING_REGION_CHANGED = "billing_region_changed"
    SERPAPI_ZERO_TRACE_STATUS_CHANGED = "serpapi_zero_trace_status_changed"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTimeRecheck:
    """Token-gated short-lived bundle of current host attestations."""

    items: tuple[GuidedProviderPreflightItem, ...] = field(repr=False)
    _checked_at: datetime = field(repr=False)
    _expires_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RECHECK_TOKEN:
            raise ValueError("Execution-time rechecks require trusted preparation")
        if (
            not isinstance(self.items, tuple)
            or not 1 <= len(self.items) <= _MAX_ITEMS
            or any(type(item) is not GuidedProviderPreflightItem for item in self.items)
            or tuple(sorted(self.items, key=_item_sort_key)) != self.items
        ):
            raise ValueError("Execution-time recheck items are invalid")
        checked = _utc_datetime(self._checked_at, "checked_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not checked < expires <= checked + _RECHECK_TTL:
            raise ValueError("Execution-time recheck expiry is invalid")
        _digest(self._context_fingerprint, "context_fingerprint")
        object.__setattr__(self, "_checked_at", checked)
        object.__setattr__(self, "_expires_at", expires)

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTimeRecheck("
            f"item_count={len(self.items)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTimeRecheckReview:
    """Safe result of an exact current host re-attestation."""

    status: GuidedProviderExecutionTimeRecheckStatus
    next_action: str
    accepted_scope_item_count: int
    accepted_max_request_count: int
    target_item_count: int
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
    profile_problem_codes: tuple[
        GuidedProviderExecutionTimeRecheckProblemCode, ...
    ] = ()
    preflight_problem_codes: tuple[GuidedProviderPreflightProblemCode, ...] = ()
    host_attestation_fresh: bool = False
    all_credentials_available: bool = False
    serpapi_plan_state_sufficient: bool = False
    items: tuple[GuidedProviderPreflightItem, ...] = field(
        default=(), repr=False
    )
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_PROVIDER_EXECUTION_TIME_RECHECK_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Execution-time recheck reviews require the assessor")
        if type(self.status) is not GuidedProviderExecutionTimeRecheckStatus:
            raise TypeError("Execution-time recheck status must be exact")
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
                raise ValueError(f"Execution-time recheck {name} is invalid")
        for name, value in (
            ("bound_google_request_count", self.bound_google_request_count),
            ("bound_serpapi_request_count", self.bound_serpapi_request_count),
            ("serpapi_bound_plan_credit_count", self.serpapi_bound_plan_credit_count),
            ("serpapi_plan_credit_cap", self.serpapi_plan_credit_cap),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_REQUESTS:
                raise ValueError(f"Execution-time recheck {name} is invalid")
        for name, value in (
            ("user_stated_source_line_count", self.user_stated_source_line_count),
            ("tentative_source_line_count", self.tentative_source_line_count),
            ("ai_candidate_source_line_count", self.ai_candidate_source_line_count),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_SOURCE_LINE_REFERENCES:
                raise ValueError(f"Execution-time recheck {name} is invalid")
        if (
            self.accepted_scope_item_count != self.target_item_count
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
            raise ValueError("Execution-time recheck counts differ")
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
            raise ValueError("Execution-time recheck data categories are invalid")
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
            raise ValueError("Execution-time recheck cost is invalid")
        if (
            not isinstance(self.profile_problem_codes, tuple)
            or any(
                type(item) is not GuidedProviderExecutionTimeRecheckProblemCode
                for item in self.profile_problem_codes
            )
            or tuple(sorted(set(self.profile_problem_codes), key=lambda item: item.value))
            != self.profile_problem_codes
            or not isinstance(self.preflight_problem_codes, tuple)
            or any(
                type(item) is not GuidedProviderPreflightProblemCode
                for item in self.preflight_problem_codes
            )
            or tuple(sorted(set(self.preflight_problem_codes), key=lambda item: item.value))
            != self.preflight_problem_codes
        ):
            raise ValueError("Execution-time recheck problems are invalid")
        if any(
            type(value) is not bool
            for value in (
                self.host_attestation_fresh,
                self.all_credentials_available,
                self.serpapi_plan_state_sufficient,
            )
        ):
            raise TypeError("Execution-time recheck readiness flags must be bool")
        expected = (
            (
                GuidedProviderExecutionTimeRecheckStatus
                .NEEDS_NEW_PRIVATE_PROVIDER_PREFLIGHT_REVIEW,
                _REAUTHORIZE_ACTION,
            )
            if self.profile_problem_codes
            else (
                (GuidedProviderExecutionTimeRecheckStatus.BLOCKED, _BLOCKED_ACTION)
                if self.preflight_problem_codes
                else (
                    GuidedProviderExecutionTimeRecheckStatus
                    .READY_FOR_PRIVATE_PROVIDER_REQUEST_MATERIALIZATION_REVIEW,
                    _READY_ACTION,
                )
            )
        )
        if (self.status, self.next_action) != expected:
            raise ValueError("Execution-time recheck status/action conflict")
        if self.status is (
            GuidedProviderExecutionTimeRecheckStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_MATERIALIZATION_REVIEW
        ) and not (
            self.host_attestation_fresh
            and self.all_credentials_available
            and self.serpapi_plan_state_sufficient
        ):
            raise ValueError("Ready execution-time recheck lacks fresh attestations")
        if (
            not isinstance(self.items, tuple)
            or len(self.items) != self.target_item_count
            or any(type(item) is not GuidedProviderPreflightItem for item in self.items)
            or tuple(sorted(self.items, key=_item_sort_key)) != self.items
        ):
            raise ValueError("Execution-time recheck review items are invalid")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
            or self.contract_version != GUIDED_PROVIDER_EXECUTION_TIME_RECHECK_VERSION
        ):
            raise ValueError("Execution-time recheck safe metadata is invalid")

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTimeRecheckReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"bound_request_count={self.bound_request_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return safe current-attestation state without private target values."""

        ready = self.status is (
            GuidedProviderExecutionTimeRecheckStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_MATERIALIZATION_REVIEW
        )
        reauthorize = self.status is (
            GuidedProviderExecutionTimeRecheckStatus
            .NEEDS_NEW_PRIVATE_PROVIDER_PREFLIGHT_REVIEW
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "profile_problems": [
                item.value for item in self.profile_problem_codes
            ],
            "preflight_problems": [
                item.value for item in self.preflight_problem_codes
            ],
            "provider_execution_time_recheck": {
                "exact_accept_response_bound": True,
                "same_exact_target_preimages_revalidated": True,
                "short_lived_host_attestation": True,
                "exact_recheck_times_exposed": False,
                "host_attestation_fresh": self.host_attestation_fresh,
                "current_profiles_match_accepted_review": not reauthorize,
                "new_preflight_and_authorization_required": reauthorize,
                "transient_host_state_blocked": (
                    not ready and not reauthorize
                ),
                "all_credentials_available": self.all_credentials_available,
                "credential_values_accessed": False,
                "serpapi_plan_state_sufficient": (
                    self.serpapi_plan_state_sufficient
                ),
                "eligible_for_request_materialization_review_count": (
                    self.bound_request_count if ready else 0
                ),
                "request_materialization_review_required": ready,
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
                "items": [_safe_attestation_item(item) for item in self.items],
                "pricing_profile_rechecked_by_host": True,
                "pricing_verified_by_provider": False,
                "policy_profile_rechecked_by_host": True,
                "policy_verified_by_provider": False,
                "retention_profile_rechecked_by_host": True,
                "retention_verified_by_provider": False,
                "billing_region_classification_rechecked_by_host": True,
                "billing_address_retained": False,
                "credential_availability_rechecked_by_host": True,
                "monthly_free_usage_remaining_checked": False,
                "cost_estimate_is_hard_currency_cap": False,
                "private_target_values_exposed": False,
                "provider_identifier_values_exposed": False,
                "target_fingerprints_exposed": False,
                "execution_authority_active": False,
                "provider_request_contract_count_created_by_recheck": 0,
                "http_request_count_created_by_recheck": 0,
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


def prepare_guided_provider_execution_time_recheck(
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
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    items: tuple[GuidedProviderPreflightItem, ...],
    evaluation_at: datetime,
) -> GuidedProviderExecutionTimeRecheck:
    """Bind new short-lived host attestations to one exact accepted response."""

    checked_at = _utc_datetime(evaluation_at, "evaluation_at")
    response_review = assess_guided_provider_execution_authorization_response(
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
        preimages=preimages,
        evaluation_at=checked_at,
    )
    _require_accepted_response(authorization_response, response_review)
    ordered = _validate_attestation_structure(items, preflight.items)
    expires_at = min(checked_at + _RECHECK_TTL, preflight.expires_at)
    if expires_at <= checked_at:
        raise ValueError("Accepted preflight expires before execution recheck")
    current_preflight = _prepare_current_preflight(
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
        ordered,
        checked_at,
        expires_at,
    )
    current_preflight_review = assess_guided_provider_preflight(
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
        current_preflight,
        evaluation_at=checked_at,
    )
    return GuidedProviderExecutionTimeRecheck(
        items=ordered,
        _checked_at=checked_at,
        _expires_at=expires_at,
        _context_fingerprint=_recheck_context_fingerprint(
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
            response_review,
            current_preflight,
            current_preflight_review,
            checked_at,
            expires_at,
        ),
        _token=_RECHECK_TOKEN,
    )


def assess_guided_provider_execution_time_recheck(
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
    recheck: GuidedProviderExecutionTimeRecheck,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderExecutionTimeRecheckReview:
    """Revalidate the accepted response and current short-lived attestations."""

    if type(recheck) is not GuidedProviderExecutionTimeRecheck:
        raise TypeError("recheck must be an exact execution-time recheck")
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < recheck._checked_at:
        raise ValueError("evaluation_at cannot precede execution-time recheck")

    captured_response_review = (
        assess_guided_provider_execution_authorization_response(
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
            preimages=preimages,
            evaluation_at=recheck._checked_at,
        )
    )
    _require_accepted_response(
        authorization_response,
        captured_response_review,
    )
    ordered = _validate_attestation_structure(recheck.items, preflight.items)
    captured_preflight = _prepare_current_preflight(
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
        ordered,
        recheck._checked_at,
        recheck._expires_at,
    )
    captured_preflight_review = assess_guided_provider_preflight(
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
        captured_preflight,
        evaluation_at=recheck._checked_at,
    )
    expected_fingerprint = _recheck_context_fingerprint(
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
        captured_response_review,
        captured_preflight,
        captured_preflight_review,
        recheck._checked_at,
        recheck._expires_at,
    )
    if recheck._context_fingerprint != expected_fingerprint:
        raise ValueError("Execution-time recheck differs from exact context")

    current_response_review = (
        assess_guided_provider_execution_authorization_response(
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
            preimages=preimages,
            evaluation_at=evaluated_at,
        )
    )
    _require_accepted_response(authorization_response, current_response_review)
    current_preflight_review = assess_guided_provider_preflight(
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
        captured_preflight,
        evaluation_at=evaluated_at,
    )
    profile_problems = _profile_problem_codes(
        preflight.items,
        current_preflight_review.items,
    )
    preflight_problems = current_preflight_review.problem_codes
    status, next_action = (
        (
            GuidedProviderExecutionTimeRecheckStatus
            .NEEDS_NEW_PRIVATE_PROVIDER_PREFLIGHT_REVIEW,
            _REAUTHORIZE_ACTION,
        )
        if profile_problems
        else (
            (GuidedProviderExecutionTimeRecheckStatus.BLOCKED, _BLOCKED_ACTION)
            if preflight_problems
            else (
                GuidedProviderExecutionTimeRecheckStatus
                .READY_FOR_PRIVATE_PROVIDER_REQUEST_MATERIALIZATION_REVIEW,
                _READY_ACTION,
            )
        )
    )
    all_credentials_available = all(
        item.credential_status is GuidedProviderCredentialStatus.AVAILABLE
        for item in current_preflight_review.items
    )
    serpapi_plan_state_sufficient = _serpapi_plan_state_sufficient(
        current_preflight_review.items
    )
    needs_verification = tuple(
        dict.fromkeys(
            (
                *current_response_review.needs_verification,
                "provider_execution_time_recheck",
            )
        )
    )
    return GuidedProviderExecutionTimeRecheckReview(
        status=status,
        next_action=next_action,
        accepted_scope_item_count=(
            current_response_review.accepted_scope_item_count
        ),
        accepted_max_request_count=(
            current_response_review.accepted_max_request_count
        ),
        target_item_count=current_response_review.target_item_count,
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
        serpapi_plan_credit_cap=(
            current_response_review.serpapi_plan_credit_cap
        ),
        bound_request_capability_counts=(
            current_response_review.bound_request_capability_counts
        ),
        data_categories=current_response_review.data_categories,
        profile_problem_codes=profile_problems,
        preflight_problem_codes=preflight_problems,
        host_attestation_fresh=(
            current_preflight_review.host_attestation_fresh
        ),
        all_credentials_available=all_credentials_available,
        serpapi_plan_state_sufficient=serpapi_plan_state_sufficient,
        items=current_preflight_review.items,
        tentative_fields=current_response_review.tentative_fields,
        needs_verification=needs_verification,
        _token=_REVIEW_TOKEN,
    )


def _prepare_current_preflight(
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
    items: tuple[GuidedProviderPreflightItem, ...],
    checked_at: datetime,
    expires_at: datetime,
) -> GuidedProviderPreflight:
    return prepare_guided_provider_preflight(
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
        items=items,
        checked_at=checked_at,
        expires_at=expires_at,
    )


def _require_accepted_response(
    response: GuidedProviderExecutionAuthorizationResponse,
    response_review: GuidedProviderExecutionAuthorizationResponseReview,
) -> None:
    if (
        response.kind
        is not GuidedProviderExecutionAuthorizationResponseKind.ACCEPT
        or response_review.status
        is not GuidedProviderExecutionAuthorizationResponseStatus
        .READY_FOR_PRIVATE_PROVIDER_EXECUTION_TIME_RECHECK
    ):
        raise ValueError("Execution-time recheck requires an exact accept response")


def _validate_attestation_structure(
    current_items: tuple[GuidedProviderPreflightItem, ...],
    accepted_items: tuple[GuidedProviderPreflightItem, ...],
) -> tuple[GuidedProviderPreflightItem, ...]:
    if (
        not isinstance(current_items, tuple)
        or not current_items
        or len(current_items) > _MAX_ITEMS
        or any(
            type(item) is not GuidedProviderPreflightItem
            for item in current_items
        )
    ):
        raise TypeError("items must contain exact current preflight values")
    ordered = tuple(sorted(current_items, key=_item_sort_key))
    accepted_by_topic = {item.topic: item for item in accepted_items}
    counts = Counter(item.topic for item in ordered)
    if (
        set(counts) != set(accepted_by_topic)
        or any(count != 1 for count in counts.values())
    ):
        raise ValueError("Execution-time attestations must cover each topic once")
    for item in ordered:
        accepted = accepted_by_topic[item.topic]
        if (
            item.capability is not accepted.capability
            or item.request_profile is not accepted.request_profile
            or item.max_request_count != accepted.max_request_count
        ):
            raise ValueError(
                "Execution-time attestation request contract differs"
            )
    return ordered


def _profile_problem_codes(
    accepted_items: tuple[GuidedProviderPreflightItem, ...],
    current_items: tuple[GuidedProviderPreflightItem, ...],
) -> tuple[GuidedProviderExecutionTimeRecheckProblemCode, ...]:
    accepted_by_topic = {item.topic: item for item in accepted_items}
    problems: set[GuidedProviderExecutionTimeRecheckProblemCode] = set()
    for current in current_items:
        accepted = accepted_by_topic[current.topic]
        if current.pricing_profile is not accepted.pricing_profile:
            problems.add(
                GuidedProviderExecutionTimeRecheckProblemCode
                .PRICING_PROFILE_CHANGED
            )
        if current.policy_profile is not accepted.policy_profile:
            problems.add(
                GuidedProviderExecutionTimeRecheckProblemCode
                .POLICY_PROFILE_CHANGED
            )
        if current.retention_profile is not accepted.retention_profile:
            problems.add(
                GuidedProviderExecutionTimeRecheckProblemCode
                .RETENTION_PROFILE_CHANGED
            )
        if current.billing_region is not accepted.billing_region:
            problems.add(
                GuidedProviderExecutionTimeRecheckProblemCode
                .BILLING_REGION_CHANGED
            )
        if (
            current.serpapi_zero_trace_status
            is not accepted.serpapi_zero_trace_status
        ):
            problems.add(
                GuidedProviderExecutionTimeRecheckProblemCode
                .SERPAPI_ZERO_TRACE_STATUS_CHANGED
            )
    return tuple(sorted(problems, key=lambda item: item.value))


def _serpapi_plan_state_sufficient(
    items: tuple[GuidedProviderPreflightItem, ...],
) -> bool:
    return all(
        item.capability is not GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
        or (
            item.serpapi_remaining_plan_credits is not None
            and item.serpapi_remaining_plan_credits >= item.max_request_count
            and item.serpapi_automatic_renewal_enabled is False
            and (
                item.retention_profile
                is not GuidedProviderRetentionProfile.SERPAPI_ZERO_TRACE
                or item.serpapi_zero_trace_status
                is GuidedProviderSerpApiZeroTraceStatus.ENTITLED
            )
        )
        for item in items
    )


def _safe_attestation_item(
    item: GuidedProviderPreflightItem,
) -> dict[str, object]:
    return {
        "topic": item.topic.value,
        "capability": item.capability.value,
        "request_profile": item.request_profile.value,
        "pricing_profile": item.pricing_profile.value,
        "policy_profile": item.policy_profile.value,
        "retention_profile": item.retention_profile.value,
        "max_request_count": item.max_request_count,
        "billing_region_classification_attested": True,
        "credential_availability_attested": True,
        "credential_available": (
            item.credential_status is GuidedProviderCredentialStatus.AVAILABLE
        ),
        "serpapi_plan_state_attested": (
            item.capability is GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
        ),
        "serpapi_plan_state_values_exposed": False,
    }


def _recheck_context_fingerprint(
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
    authorization_response_review: (
        GuidedProviderExecutionAuthorizationResponseReview
    ),
    current_preflight: GuidedProviderPreflight,
    current_preflight_review: GuidedProviderPreflightReview,
    checked_at: datetime,
    expires_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": GUIDED_PROVIDER_EXECUTION_TIME_RECHECK_VERSION,
            "authorization_response_contract_version": (
                GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_RESPONSE_VERSION
            ),
            "domain": "guided-provider-execution-time-recheck",
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
            "accepted_provider_preflight": _private_context_value(preflight),
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
            "provider_execution_authorization_response_review": (
                _private_context_value(authorization_response_review)
            ),
            "current_provider_preflight": _private_context_value(
                current_preflight
            ),
            "current_provider_preflight_review": _private_context_value(
                current_preflight_review
            ),
            "checked_at": _private_context_value(checked_at),
            "expires_at": _private_context_value(expires_at),
        }
    )


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
        raise ValueError("Execution-time recheck capability counts are invalid")


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
        item.serpapi_remaining_plan_credits
        if item.serpapi_remaining_plan_credits is not None
        else -1,
        item.serpapi_automatic_renewal_enabled
        if item.serpapi_automatic_renewal_enabled is not None
        else False,
        item.serpapi_zero_trace_status.value,
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
    "GUIDED_PROVIDER_EXECUTION_TIME_RECHECK_VERSION",
    "GuidedProviderExecutionTimeRecheck",
    "GuidedProviderExecutionTimeRecheckProblemCode",
    "GuidedProviderExecutionTimeRecheckReview",
    "GuidedProviderExecutionTimeRecheckStatus",
    "assess_guided_provider_execution_time_recheck",
    "prepare_guided_provider_execution_time_recheck",
]
