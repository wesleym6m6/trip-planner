"""Exact response handoff for one fresh private provider-preflight review.

Phase 5.14 captures only an unambiguous accept, reduce, or cancel response to
the exact current Phase 5.13 review.  Capture and assessment both re-run the
offline preflight assessor with a trusted UTC evaluation time.  The response
binds the full private guided context, accepted scope, short-lived host
attestation bundle, freshly derived review, and capture time; card ordering
alone is canonicalized.

Acceptance only permits preparation of a later exact provider-execution
authorization review.  It does not authorize a provider, materialize a
request, read a credential, call a provider, reserve spend, persist data,
schedule an itinerary, create a trip, render, deploy, confirm, or mutate
canonical state.  Reduction never edits the scope automatically.  Cancellation
only closes the current external-execution path and preserves every evidence
need while the itinerary remains candidate + unverified.
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
from .guided_provider_preflight import (
    GUIDED_PROVIDER_PREFLIGHT_VERSION,
    GuidedProviderPreflight,
    GuidedProviderPreflightReview,
    GuidedProviderPreflightStatus,
    assess_guided_provider_preflight,
)
from .guided_provider_scope import (
    GuidedProviderCapability,
    GuidedProviderDataCategory,
    GuidedProviderScopeProposal,
)
from .guided_provider_scope_response import (
    GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION,
    GuidedProviderScopeResponse,
)
from .guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementResponse,
)
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_PREFLIGHT_RESPONSE_VERSION = (
    "guided-provider-preflight-response/v1"
)
_CONTEXT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ITEMS = 12
_MAX_REQUESTS = 32
_MAX_GOOGLE_LIST_COST_USD_MICROS = 32 * 32_000
_ACCEPT_ACTION = "prepare_private_provider_execution_authorization"
_REFINE_ACTION = "refine_private_provider_preflight"
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
}


class GuidedProviderPreflightResponseKind(str, Enum):
    """One clear response to the exact visible preflight review."""

    ACCEPT_PROVIDER_PREFLIGHT = "accept_provider_preflight"
    REQUEST_SMALLER_PROVIDER_PREFLIGHT = "request_smaller_provider_preflight"
    CANCEL_EXTERNAL_EXECUTION = "cancel_external_execution"


class GuidedProviderPreflightResponseStatus(str, Enum):
    """Safe handoff after one exact private preflight response."""

    READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION = (
        "ready_for_private_provider_execution_authorization"
    )
    READY_FOR_PRIVATE_PROVIDER_PREFLIGHT_REFINEMENT = (
        "ready_for_private_provider_preflight_refinement"
    )
    EXTERNAL_EXECUTION_CANCELLED = "external_execution_cancelled"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderPreflightResponse:
    """Typed response bound to one exact fresh process-local preflight review."""

    kind: GuidedProviderPreflightResponseKind
    _reviewed_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESPONSE_TOKEN:
            raise ValueError(
                "Guided provider preflight responses must be captured by the host"
            )
        if type(self.kind) is not GuidedProviderPreflightResponseKind:
            raise TypeError(
                "GuidedProviderPreflightResponse.kind must be exact"
            )
        reviewed_at = _utc_datetime(self._reviewed_at, "reviewed_at")
        if (
            type(self._context_fingerprint) is not str
            or _CONTEXT_FINGERPRINT_RE.fullmatch(self._context_fingerprint)
            is None
        ):
            raise ValueError(
                "GuidedProviderPreflightResponse context fingerprint is invalid"
            )
        object.__setattr__(self, "_reviewed_at", reviewed_at)

    def __repr__(self) -> str:
        return f"GuidedProviderPreflightResponse(kind={self.kind.value!r})"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderPreflightResponseReview:
    """Redacted, non-authoritative handoff from an exact preflight response."""

    status: GuidedProviderPreflightResponseStatus
    response_kind: GuidedProviderPreflightResponseKind
    next_action: str
    accepted_scope_item_count: int
    accepted_max_request_count: int
    preflight_item_count: int
    max_request_count: int
    capability_counts: tuple[tuple[GuidedProviderCapability, int], ...] = ()
    data_categories: tuple[GuidedProviderDataCategory, ...] = ()
    estimated_first_paid_tier_google_cost_usd_micros: int = 0
    serpapi_plan_credit_cap: int = 0
    all_provider_costs_have_currency_list_rate_estimates: bool = False
    host_attestation_fresh: bool = False
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_PROVIDER_PREFLIGHT_RESPONSE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided provider preflight response reviews require the assessor"
            )
        if type(self.status) is not GuidedProviderPreflightResponseStatus:
            raise TypeError(
                "GuidedProviderPreflightResponseReview.status must be exact"
            )
        if type(self.response_kind) is not GuidedProviderPreflightResponseKind:
            raise TypeError(
                "GuidedProviderPreflightResponseReview.response_kind must be exact"
            )
        for name, value, maximum in (
            (
                "accepted_scope_item_count",
                self.accepted_scope_item_count,
                _MAX_ITEMS,
            ),
            (
                "accepted_max_request_count",
                self.accepted_max_request_count,
                _MAX_REQUESTS,
            ),
            ("preflight_item_count", self.preflight_item_count, _MAX_ITEMS),
            ("max_request_count", self.max_request_count, _MAX_REQUESTS),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(
                    f"GuidedProviderPreflightResponseReview.{name} is invalid"
                )
        if (
            self.preflight_item_count != self.accepted_scope_item_count
            or self.max_request_count < self.preflight_item_count
            or self.max_request_count > self.accepted_max_request_count
        ):
            raise ValueError(
                "GuidedProviderPreflightResponseReview counts are inconsistent"
            )
        expected_capability_counts = tuple(
            sorted(self.capability_counts, key=lambda item: item[0].value)
        )
        if (
            not isinstance(self.capability_counts, tuple)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not GuidedProviderCapability
                or type(item[1]) is not int
                or not 0 <= item[1] <= _MAX_ITEMS
                for item in self.capability_counts
            )
            or len(self.capability_counts) != len(GuidedProviderCapability)
            or {item[0] for item in self.capability_counts}
            != set(GuidedProviderCapability)
            or len({item[0] for item in self.capability_counts})
            != len(GuidedProviderCapability)
            or self.capability_counts != expected_capability_counts
            or sum(item[1] for item in self.capability_counts)
            != self.preflight_item_count
        ):
            raise ValueError(
                "GuidedProviderPreflightResponseReview capability counts are invalid"
            )
        if (
            not isinstance(self.data_categories, tuple)
            or not self.data_categories
            or any(
                type(item) is not GuidedProviderDataCategory
                for item in self.data_categories
            )
            or tuple(
                sorted(set(self.data_categories), key=lambda item: item.value)
            )
            != self.data_categories
        ):
            raise ValueError(
                "GuidedProviderPreflightResponseReview data categories are invalid"
            )
        if (
            type(self.estimated_first_paid_tier_google_cost_usd_micros)
            is not int
            or not 0
            <= self.estimated_first_paid_tier_google_cost_usd_micros
            <= _MAX_GOOGLE_LIST_COST_USD_MICROS
            or type(self.serpapi_plan_credit_cap) is not int
            or not 0 <= self.serpapi_plan_credit_cap <= _MAX_REQUESTS
            or self.serpapi_plan_credit_cap > self.max_request_count
            or type(
                self.all_provider_costs_have_currency_list_rate_estimates
            )
            is not bool
            or self.all_provider_costs_have_currency_list_rate_estimates
            is not (self.serpapi_plan_credit_cap == 0)
            or type(self.host_attestation_fresh) is not bool
            or not self.host_attestation_fresh
        ):
            raise ValueError(
                "GuidedProviderPreflightResponseReview cost/freshness is invalid"
            )
        expected = {
            GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT: (
                GuidedProviderPreflightResponseStatus
                .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION,
                _ACCEPT_ACTION,
            ),
            GuidedProviderPreflightResponseKind
            .REQUEST_SMALLER_PROVIDER_PREFLIGHT: (
                GuidedProviderPreflightResponseStatus
                .READY_FOR_PRIVATE_PROVIDER_PREFLIGHT_REFINEMENT,
                _REFINE_ACTION,
            ),
            GuidedProviderPreflightResponseKind.CANCEL_EXTERNAL_EXECUTION: (
                GuidedProviderPreflightResponseStatus
                .EXTERNAL_EXECUTION_CANCELLED,
                _CANCEL_ACTION,
            ),
        }[self.response_kind]
        if (self.status, self.next_action) != expected:
            raise ValueError(
                "GuidedProviderPreflightResponseReview status/action conflict"
            )
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(
                item not in _VERIFICATION_TOPICS
                for item in self.needs_verification
            )
            or len(set(self.needs_verification))
            != len(self.needs_verification)
        ):
            raise ValueError(
                "GuidedProviderPreflightResponseReview safe topics are invalid"
            )
        if self.contract_version != GUIDED_PROVIDER_PREFLIGHT_RESPONSE_VERSION:
            raise ValueError(
                "Unsupported guided provider preflight response version"
            )

    def __repr__(self) -> str:
        return (
            "GuidedProviderPreflightResponseReview("
            f"status={self.status.value!r}, "
            f"response_kind={self.response_kind.value!r}, "
            f"next_action={self.next_action!r}, "
            f"preflight_item_count={self.preflight_item_count!r}, "
            f"max_request_count={self.max_request_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return aggregate response state without private request material."""

        accepted = (
            self.response_kind
            is GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
        )
        smaller = (
            self.response_kind
            is GuidedProviderPreflightResponseKind
            .REQUEST_SMALLER_PROVIDER_PREFLIGHT
        )
        cancelled = (
            self.response_kind
            is GuidedProviderPreflightResponseKind.CANCEL_EXTERNAL_EXECUTION
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_preflight_response": {
                "kind": self.response_kind.value,
                "exact_private_context_bound": True,
                "accepted_for_execution_authorization_preparation": accepted,
                "requested_smaller_provider_preflight": smaller,
                "external_execution_cancelled_for_current_scope": cancelled,
                "may_prepare_private_provider_execution_authorization": accepted,
                "may_refine_private_provider_preflight": smaller,
                "evidence_requirements_preserved": True,
                "provider_scope_preserved": True,
                "preflight_unchanged": True,
                "host_attestation_fresh": self.host_attestation_fresh,
                "accepted_scope_item_count": self.accepted_scope_item_count,
                "accepted_max_request_count": (
                    self.accepted_max_request_count
                ),
                "preflight_item_count": self.preflight_item_count,
                "max_request_count": self.max_request_count,
                "capability_counts": {
                    capability.value: count
                    for capability, count in self.capability_counts
                },
                "data_categories": [
                    item.value for item in self.data_categories
                ],
                "estimated_first_paid_tier_google_cost_usd_micros": (
                    self.estimated_first_paid_tier_google_cost_usd_micros
                ),
                "serpapi_plan_credit_cap": self.serpapi_plan_credit_cap,
                "all_provider_costs_have_currency_list_rate_estimates": (
                    self.all_provider_costs_have_currency_list_rate_estimates
                ),
                "monthly_free_usage_remaining_checked": False,
                "cost_estimate_is_hard_currency_cap": False,
                "host_pricing_profile_attested": True,
                "pricing_verified_by_response_contract": False,
                "host_provider_policy_profile_attested": True,
                "provider_policy_verified_by_response_contract": False,
                "billing_region_attested": True,
                "expected_provider_retention_profile_attested": True,
                "provider_retention_verified_by_response_contract": False,
                "credential_availability_attested": True,
                "credentials_available": True,
                "credentials_accessed": False,
                "credential_availability_recheck_required": True,
                "execution_time_recheck_required": True,
                "explicit_execution_authorization_required_before_any_call": (
                    True
                ),
                "preflight_acceptance_is_provider_authorization": False,
                "provider_scope_authorized": False,
                "provider_requests_created": False,
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


def _preflight_response_context_fingerprint(
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
    preflight_review: GuidedProviderPreflightReview,
    response_kind: GuidedProviderPreflightResponseKind,
    reviewed_at: datetime,
) -> str:
    """Bind a response to the exact private preflight and visible review."""

    canonical = {
        "contract_version": GUIDED_PROVIDER_PREFLIGHT_RESPONSE_VERSION,
        "preflight_contract_version": GUIDED_PROVIDER_PREFLIGHT_VERSION,
        "scope_response_contract_version": (
            GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION
        ),
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
        "provider_preflight_review": _private_context_value(preflight_review),
        "response_kind": _private_context_value(response_kind),
        "reviewed_at": _private_context_value(reviewed_at),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_guided_provider_preflight_response(
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
    *,
    kind: GuidedProviderPreflightResponseKind,
    evaluation_at: datetime,
) -> GuidedProviderPreflightResponse:
    """Capture one clear response to the exact fresh preflight review."""

    if type(kind) is not GuidedProviderPreflightResponseKind:
        raise TypeError(
            "kind must be an exact GuidedProviderPreflightResponseKind"
        )
    reviewed_at = _utc_datetime(evaluation_at, "evaluation_at")
    review = assess_guided_provider_preflight(
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
        evaluation_at=reviewed_at,
    )
    if review.status is not GuidedProviderPreflightStatus.REVIEW_REQUIRED:
        raise ValueError(
            "A preflight response requires a current visible preflight review"
        )
    return GuidedProviderPreflightResponse(
        kind=kind,
        _reviewed_at=reviewed_at,
        _context_fingerprint=_preflight_response_context_fingerprint(
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
            review,
            kind,
            reviewed_at,
        ),
        _token=_RESPONSE_TOKEN,
    )


def assess_guided_provider_preflight_response(
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
    response: GuidedProviderPreflightResponse,
    *,
    evaluation_at: datetime,
) -> GuidedProviderPreflightResponseReview:
    """Revalidate and hand off one exact private preflight response."""

    if type(response) is not GuidedProviderPreflightResponse:
        raise TypeError(
            "response must be an exact GuidedProviderPreflightResponse"
        )
    current_evaluation = _utc_datetime(evaluation_at, "evaluation_at")
    if current_evaluation < response._reviewed_at:
        raise ValueError("evaluation_at cannot precede response capture")
    captured_review = assess_guided_provider_preflight(
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
        evaluation_at=response._reviewed_at,
    )
    if captured_review.status is not GuidedProviderPreflightStatus.REVIEW_REQUIRED:
        raise ValueError(
            "A preflight response requires its original visible review"
        )
    if response._context_fingerprint != _preflight_response_context_fingerprint(
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
        captured_review,
        response.kind,
        response._reviewed_at,
    ):
        raise ValueError(
            "Preflight response does not match the current private context"
        )
    current_review = assess_guided_provider_preflight(
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
        evaluation_at=current_evaluation,
    )
    if current_review.status is not GuidedProviderPreflightStatus.REVIEW_REQUIRED:
        raise ValueError("Provider preflight is no longer current and ready")
    status, next_action = {
        GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT: (
            GuidedProviderPreflightResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION,
            _ACCEPT_ACTION,
        ),
        GuidedProviderPreflightResponseKind.REQUEST_SMALLER_PROVIDER_PREFLIGHT: (
            GuidedProviderPreflightResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_PREFLIGHT_REFINEMENT,
            _REFINE_ACTION,
        ),
        GuidedProviderPreflightResponseKind.CANCEL_EXTERNAL_EXECUTION: (
            GuidedProviderPreflightResponseStatus.EXTERNAL_EXECUTION_CANCELLED,
            _CANCEL_ACTION,
        ),
    }[response.kind]
    capability_counter = Counter(
        item.capability for item in current_review.items
    )
    capability_counts = tuple(
        (capability, capability_counter[capability])
        for capability in sorted(
            GuidedProviderCapability,
            key=lambda item: item.value,
        )
    )
    return GuidedProviderPreflightResponseReview(
        status=status,
        response_kind=response.kind,
        next_action=next_action,
        accepted_scope_item_count=current_review.accepted_scope_item_count,
        accepted_max_request_count=(
            current_review.accepted_max_request_count
        ),
        preflight_item_count=current_review.preflight_item_count,
        max_request_count=current_review.max_request_count,
        capability_counts=capability_counts,
        data_categories=current_review.data_categories,
        estimated_first_paid_tier_google_cost_usd_micros=(
            current_review.estimated_first_paid_tier_google_cost_usd_micros
        ),
        serpapi_plan_credit_cap=current_review.serpapi_plan_credit_cap,
        all_provider_costs_have_currency_list_rate_estimates=(
            current_review.all_provider_costs_have_currency_list_rate_estimates
        ),
        host_attestation_fresh=current_review.host_attestation_fresh,
        tentative_fields=current_review.tentative_fields,
        needs_verification=current_review.needs_verification,
        _token=_REVIEW_TOKEN,
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
    "GUIDED_PROVIDER_PREFLIGHT_RESPONSE_VERSION",
    "GuidedProviderPreflightResponse",
    "GuidedProviderPreflightResponseKind",
    "GuidedProviderPreflightResponseReview",
    "GuidedProviderPreflightResponseStatus",
    "assess_guided_provider_preflight_response",
    "capture_guided_provider_preflight_response",
]
