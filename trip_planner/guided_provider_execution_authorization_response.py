"""Exact response to one fresh guided provider-execution review.

Phase 5.18 captures only an unambiguous accept, request-smaller, or cancel
choice for the exact current Phase 5.17 private review.  Capture and assessment
revalidate the full private chain, typed target preimages, host attestations,
endpoint freshness, and review contents at trusted UTC times.

Acceptance permits only preparation of a separate execution-time recheck.  It
does not activate execution authority, create a provider or HTTP request, read
a credential, call a provider, reserve spend, persist data, schedule, render,
deploy, confirm, or mutate canonical state.  Request-smaller changes no target
or cap automatically.  Cancellation closes only the current execution path and
preserves the unverified evidence requirements.
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
from .guided_provider_execution_authorization_review import (
    GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW_VERSION,
    GuidedProviderExecutionAuthorizationReview,
    GuidedProviderExecutionAuthorizationReviewStatus,
    assess_guided_provider_execution_authorization_review,
)
from .guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetBindings,
    GuidedProviderExecutionTargetPreimage,
)
from .guided_provider_execution_targets import GuidedProviderExecutionTargets
from .guided_provider_preflight import GuidedProviderPreflight
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


GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_RESPONSE_VERSION = (
    "guided-provider-execution-authorization-response/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ITEMS = 12
_MAX_REQUESTS = 32
_MAX_SOURCE_LINE_REFERENCES = _MAX_ITEMS * 32
_MAX_GOOGLE_LIST_COST_USD_MICROS = _MAX_REQUESTS * 32_000
_GOOGLE_LIST_COST_USD_MICROS_BY_CAPABILITY = {
    GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: 32_000,
    GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: 20_000,
    GuidedProviderCapability.GOOGLE_ROUTES: 5_000,
    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS: 0,
}
_ACCEPT_ACTION = "prepare_private_provider_execution_time_recheck"
_REFINE_ACTION = "refine_private_provider_execution_targets"
_CANCEL_ACTION = "continue_private_evidence_review"
_RESPONSE_TOKEN = object()
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
}


class GuidedProviderExecutionAuthorizationResponseKind(str, Enum):
    """One clear choice from the exact visible Phase 5.17 review."""

    ACCEPT = "accept"
    REQUEST_SMALLER = "request_smaller"
    CANCEL = "cancel"


class GuidedProviderExecutionAuthorizationResponseStatus(str, Enum):
    """Non-executable handoff after one exact review response."""

    READY_FOR_PRIVATE_PROVIDER_EXECUTION_TIME_RECHECK = (
        "ready_for_private_provider_execution_time_recheck"
    )
    READY_FOR_PRIVATE_PROVIDER_EXECUTION_TARGET_REFINEMENT = (
        "ready_for_private_provider_execution_target_refinement"
    )
    PROVIDER_EXECUTION_CANCELLED = "provider_execution_cancelled"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionAuthorizationResponse:
    """Typed choice bound to one exact fresh process-local review."""

    kind: GuidedProviderExecutionAuthorizationResponseKind
    _captured_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESPONSE_TOKEN:
            raise ValueError(
                "Execution authorization responses require trusted capture"
            )
        if type(self.kind) is not GuidedProviderExecutionAuthorizationResponseKind:
            raise TypeError("Execution authorization response kind must be exact")
        captured_at = _utc_datetime(self._captured_at, "captured_at")
        _digest(self._context_fingerprint, "context_fingerprint")
        object.__setattr__(self, "_captured_at", captured_at)

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionAuthorizationResponse("
            f"kind={self.kind.value!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionAuthorizationResponseReview:
    """Aggregate safe handoff with no immediate execution authority."""

    status: GuidedProviderExecutionAuthorizationResponseStatus
    response_kind: GuidedProviderExecutionAuthorizationResponseKind
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
    host_attestation_fresh: bool = False
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = (
        GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_RESPONSE_VERSION
    )
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Execution authorization response reviews require the assessor"
            )
        if (
            type(self.status)
            is not GuidedProviderExecutionAuthorizationResponseStatus
            or type(self.response_kind)
            is not GuidedProviderExecutionAuthorizationResponseKind
        ):
            raise TypeError("Execution authorization response enums must be exact")
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
                raise ValueError(f"Execution response {name} is invalid")
        for name, value in (
            ("bound_google_request_count", self.bound_google_request_count),
            ("bound_serpapi_request_count", self.bound_serpapi_request_count),
            ("serpapi_bound_plan_credit_count", self.serpapi_bound_plan_credit_count),
            ("serpapi_plan_credit_cap", self.serpapi_plan_credit_cap),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_REQUESTS:
                raise ValueError(f"Execution response {name} is invalid")
        for name, value in (
            ("user_stated_source_line_count", self.user_stated_source_line_count),
            ("tentative_source_line_count", self.tentative_source_line_count),
            ("ai_candidate_source_line_count", self.ai_candidate_source_line_count),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_SOURCE_LINE_REFERENCES:
                raise ValueError(f"Execution response {name} is invalid")
        if (
            self.accepted_scope_item_count != self.target_item_count
            or self.bound_request_count > self.max_request_count
            or self.max_request_count > self.accepted_max_request_count
            or self.bound_request_count
            != self.bound_google_request_count
            + self.bound_serpapi_request_count
            or self.bound_source_line_reference_count
            != self.user_stated_source_line_count
            + self.tentative_source_line_count
            + self.ai_candidate_source_line_count
            or self.serpapi_bound_plan_credit_count
            != self.bound_serpapi_request_count
            or self.serpapi_bound_plan_credit_count > self.serpapi_plan_credit_cap
        ):
            raise ValueError("Execution authorization response counts differ")
        _validate_capability_counts(
            self.bound_request_capability_counts,
            self.bound_request_count,
        )
        capability_counts = dict(self.bound_request_capability_counts)
        if (
            capability_counts[
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
            ]
            != self.bound_serpapi_request_count
            or sum(
                capability_counts[capability]
                for capability in GuidedProviderCapability
                if capability
                is not GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
            )
            != self.bound_google_request_count
        ):
            raise ValueError("Execution response provider counts differ")
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
            raise ValueError("Execution response data categories are invalid")
        if (
            type(self.estimated_bound_first_paid_tier_google_cost_usd_micros)
            is not int
            or type(self.accepted_max_first_paid_tier_google_cost_usd_micros)
            is not int
            or not 0
            <= self.estimated_bound_first_paid_tier_google_cost_usd_micros
            <= self.accepted_max_first_paid_tier_google_cost_usd_micros
            <= _MAX_GOOGLE_LIST_COST_USD_MICROS
            or self.estimated_bound_first_paid_tier_google_cost_usd_micros
            != sum(
                capability_counts[capability]
                * _GOOGLE_LIST_COST_USD_MICROS_BY_CAPABILITY[capability]
                for capability in GuidedProviderCapability
            )
            or type(self.host_attestation_fresh) is not bool
            or not self.host_attestation_fresh
        ):
            raise ValueError("Execution response cost/freshness is invalid")
        expected = {
            GuidedProviderExecutionAuthorizationResponseKind.ACCEPT: (
                GuidedProviderExecutionAuthorizationResponseStatus
                .READY_FOR_PRIVATE_PROVIDER_EXECUTION_TIME_RECHECK,
                _ACCEPT_ACTION,
            ),
            GuidedProviderExecutionAuthorizationResponseKind.REQUEST_SMALLER: (
                GuidedProviderExecutionAuthorizationResponseStatus
                .READY_FOR_PRIVATE_PROVIDER_EXECUTION_TARGET_REFINEMENT,
                _REFINE_ACTION,
            ),
            GuidedProviderExecutionAuthorizationResponseKind.CANCEL: (
                GuidedProviderExecutionAuthorizationResponseStatus
                .PROVIDER_EXECUTION_CANCELLED,
                _CANCEL_ACTION,
            ),
        }[self.response_kind]
        if (self.status, self.next_action) != expected:
            raise ValueError("Execution response status/action conflict")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
            or self.contract_version
            != GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_RESPONSE_VERSION
        ):
            raise ValueError("Execution response safe metadata is invalid")

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionAuthorizationResponseReview("
            f"status={self.status.value!r}, "
            f"response_kind={self.response_kind.value!r}, "
            f"next_action={self.next_action!r}, "
            f"bound_request_count={self.bound_request_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the exact choice and aggregate context without private values."""

        accepted = (
            self.response_kind
            is GuidedProviderExecutionAuthorizationResponseKind.ACCEPT
        )
        smaller = (
            self.response_kind
            is GuidedProviderExecutionAuthorizationResponseKind.REQUEST_SMALLER
        )
        cancelled = (
            self.response_kind
            is GuidedProviderExecutionAuthorizationResponseKind.CANCEL
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_execution_authorization_response": {
                "kind": self.response_kind.value,
                "exact_private_context_bound": True,
                "authorization_response_captured": True,
                "accepted_exact_private_review": accepted,
                "requested_smaller_execution_target_set": smaller,
                "current_external_execution_cancelled": cancelled,
                "accepted_for_execution_time_recheck_preparation": accepted,
                "may_prepare_private_provider_execution_time_recheck": accepted,
                "may_refine_private_provider_execution_targets": smaller,
                "current_external_execution_path_closed": cancelled,
                "new_exact_review_required_after_target_refinement": smaller,
                "new_exact_review_required_before_cancelled_path_reopens": (
                    cancelled
                ),
                "eligible_for_execution_time_recheck_request_count": (
                    self.bound_request_count if accepted else 0
                ),
                "evidence_requirements_preserved": True,
                "provider_scope_preserved": True,
                "preflight_preserved": True,
                "execution_targets_unchanged_by_response": True,
                "authorization_review_unchanged_by_response": True,
                "request_caps_unchanged_by_response": True,
                "response_free_text_retained": False,
                "caller_supplied_authorization_digest_accepted": False,
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
                "monthly_free_usage_remaining_checked": False,
                "cost_estimate_is_hard_currency_cap": False,
                "pricing_policy_retention_attestations_preserved": True,
                "credential_availability_attestation_preserved": True,
                "execution_time_pricing_policy_retention_recheck_required": (
                    accepted
                ),
                "credential_availability_recheck_required": accepted,
                "exact_target_preimages_required_for_recheck": accepted,
                "immediate_provider_execution_authority_granted": False,
                "execution_authority_active": False,
                "partial_execution_authorization_permitted": False,
                "private_target_values_exposed": False,
                "provider_identifier_values_exposed": False,
                "target_fingerprints_exposed": False,
                "provider_request_contract_count_created_by_response": 0,
                "http_request_count_created_by_response": 0,
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


def capture_guided_provider_execution_authorization_response(
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
    kind: GuidedProviderExecutionAuthorizationResponseKind,
    evaluation_at: datetime,
) -> GuidedProviderExecutionAuthorizationResponse:
    """Capture one clear choice for the exact fresh private review."""

    if type(kind) is not GuidedProviderExecutionAuthorizationResponseKind:
        raise TypeError("kind must be an exact execution response kind")
    captured_at = _utc_datetime(evaluation_at, "evaluation_at")
    assessed_review = assess_guided_provider_execution_authorization_review(
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
        review,
        preimages=preimages,
        evaluation_at=captured_at,
    )
    if (
        assessed_review.status
        is not GuidedProviderExecutionAuthorizationReviewStatus.REVIEW_REQUIRED
    ):
        raise ValueError("Execution response requires the current visible review")
    return GuidedProviderExecutionAuthorizationResponse(
        kind=kind,
        _captured_at=captured_at,
        _context_fingerprint=_response_context_fingerprint(
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
            assessed_review,
            kind,
            captured_at,
        ),
        _token=_RESPONSE_TOKEN,
    )


def assess_guided_provider_execution_authorization_response(
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
    response: GuidedProviderExecutionAuthorizationResponse,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderExecutionAuthorizationResponseReview:
    """Revalidate one exact choice and return a non-executable handoff."""

    if type(response) is not GuidedProviderExecutionAuthorizationResponse:
        raise TypeError("response must be an exact execution response")
    current_evaluation = _utc_datetime(evaluation_at, "evaluation_at")
    if current_evaluation < response._captured_at:
        raise ValueError("evaluation_at cannot precede response capture")

    captured_review = assess_guided_provider_execution_authorization_review(
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
        review,
        preimages=preimages,
        evaluation_at=response._captured_at,
    )
    if (
        captured_review.status
        is not GuidedProviderExecutionAuthorizationReviewStatus.REVIEW_REQUIRED
    ):
        raise ValueError("Execution response requires its original visible review")
    expected_fingerprint = _response_context_fingerprint(
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
        captured_review,
        response.kind,
        response._captured_at,
    )
    if response._context_fingerprint != expected_fingerprint:
        raise ValueError("Execution response differs from exact private context")

    current_review = assess_guided_provider_execution_authorization_review(
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
        review,
        preimages=preimages,
        evaluation_at=current_evaluation,
    )
    if (
        current_review.status
        is not GuidedProviderExecutionAuthorizationReviewStatus.REVIEW_REQUIRED
    ):
        raise ValueError("Execution authorization review is no longer current")
    status, next_action = {
        GuidedProviderExecutionAuthorizationResponseKind.ACCEPT: (
            GuidedProviderExecutionAuthorizationResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_TIME_RECHECK,
            _ACCEPT_ACTION,
        ),
        GuidedProviderExecutionAuthorizationResponseKind.REQUEST_SMALLER: (
            GuidedProviderExecutionAuthorizationResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_TARGET_REFINEMENT,
            _REFINE_ACTION,
        ),
        GuidedProviderExecutionAuthorizationResponseKind.CANCEL: (
            GuidedProviderExecutionAuthorizationResponseStatus
            .PROVIDER_EXECUTION_CANCELLED,
            _CANCEL_ACTION,
        ),
    }[response.kind]
    capability_counter = Counter(
        item.capability for item in current_review._items
    )
    capability_counts = tuple(
        (capability, capability_counter[capability])
        for capability in sorted(
            GuidedProviderCapability,
            key=lambda item: item.value,
        )
    )
    source_counts = Counter(
        {
            "user_stated": sum(
                item.user_stated_source_line_count
                for item in current_review._items
            ),
            "tentative": sum(
                item.tentative_source_line_count
                for item in current_review._items
            ),
            "ai_candidate": sum(
                item.ai_candidate_source_line_count
                for item in current_review._items
            ),
        }
    )
    needs_verification = tuple(
        dict.fromkeys(
            (
                *current_review.needs_verification,
                "provider_execution_authorization_response",
            )
        )
    )
    return GuidedProviderExecutionAuthorizationResponseReview(
        status=status,
        response_kind=response.kind,
        next_action=next_action,
        accepted_scope_item_count=current_review.accepted_scope_item_count,
        accepted_max_request_count=current_review.accepted_max_request_count,
        target_item_count=current_review.target_item_count,
        bound_request_count=current_review.bound_request_count,
        max_request_count=current_review.max_request_count,
        bound_source_line_reference_count=(
            current_review.bound_source_line_reference_count
        ),
        bound_google_request_count=current_review.bound_google_request_count,
        bound_serpapi_request_count=current_review.bound_serpapi_request_count,
        user_stated_source_line_count=source_counts["user_stated"],
        tentative_source_line_count=source_counts["tentative"],
        ai_candidate_source_line_count=source_counts["ai_candidate"],
        estimated_bound_first_paid_tier_google_cost_usd_micros=(
            current_review
            .estimated_bound_first_paid_tier_google_cost_usd_micros
        ),
        accepted_max_first_paid_tier_google_cost_usd_micros=(
            current_review
            .accepted_max_first_paid_tier_google_cost_usd_micros
        ),
        serpapi_bound_plan_credit_count=(
            current_review.serpapi_bound_plan_credit_count
        ),
        serpapi_plan_credit_cap=current_review.serpapi_plan_credit_cap,
        bound_request_capability_counts=capability_counts,
        data_categories=current_review.data_categories,
        host_attestation_fresh=current_review.host_attestation_fresh,
        tentative_fields=current_review.tentative_fields,
        needs_verification=needs_verification,
        _token=_REVIEW_TOKEN,
    )


def _response_context_fingerprint(
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
    response_kind: GuidedProviderExecutionAuthorizationResponseKind,
    captured_at: datetime,
) -> str:
    canonical = {
        "contract_version": (
            GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_RESPONSE_VERSION
        ),
        "authorization_review_contract_version": (
            GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW_VERSION
        ),
        "domain": "guided-provider-execution-authorization-response",
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
        "provider_execution_target_bindings": _private_context_value(bindings),
        "provider_execution_authorization_review": _private_context_value(
            review
        ),
        "response_kind": _private_context_value(response_kind),
        "captured_at": _private_context_value(captured_at),
    }
    return _sha256(canonical)


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
        raise ValueError("Execution response capability counts are invalid")


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
    "GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_RESPONSE_VERSION",
    "GuidedProviderExecutionAuthorizationResponse",
    "GuidedProviderExecutionAuthorizationResponseKind",
    "GuidedProviderExecutionAuthorizationResponseReview",
    "GuidedProviderExecutionAuthorizationResponseStatus",
    "assess_guided_provider_execution_authorization_response",
    "capture_guided_provider_execution_authorization_response",
]
