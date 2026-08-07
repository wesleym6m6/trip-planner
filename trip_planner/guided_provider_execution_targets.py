"""Provider-execution target requirements for accepted guided preflights.

Phase 5.15 consumes only an exact, fresh Phase 5.14 acceptance and derives the
typed target binding that each accepted capability still needs before an exact
execution-authorization review can exist.  The mapping is deterministic: a
place search needs a private identity intent, current hours need a trusted
Place endpoint produced by identity evidence, routes need two trusted ordered
endpoints, and hotel search needs a private typed search intent.

This module deliberately does not accept or retain any target value, query,
payload, provider resource ID, credential, or provider result.  Every item is
therefore deferred as a target requirement; partial execution authorization is
not allowed.  Preparing or assessing the plan creates no provider request,
performs no provider call, grants no authority, and leaves the itinerary
candidate + unverified.
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
from .guided_provider_preflight import (
    GuidedProviderPreflight,
    GuidedProviderRequestProfile,
)
from .guided_provider_preflight_response import (
    GUIDED_PROVIDER_PREFLIGHT_RESPONSE_VERSION,
    GuidedProviderPreflightResponse,
    GuidedProviderPreflightResponseKind,
    GuidedProviderPreflightResponseReview,
    GuidedProviderPreflightResponseStatus,
    assess_guided_provider_preflight_response,
)
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


GUIDED_PROVIDER_EXECUTION_TARGETS_VERSION = (
    "guided-provider-execution-targets/v1"
)
_CONTEXT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ITEMS = 12
_MAX_REQUESTS = 32
_MAX_GOOGLE_LIST_COST_USD_MICROS = 32 * 32_000
_NEXT_ACTION = "prepare_private_provider_execution_targets"
_PLAN_TOKEN = object()
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
}


class GuidedProviderExecutionTargetKind(str, Enum):
    """The exact private target shape a later binding must provide."""

    PRIVATE_PLACE_IDENTITY_INTENT = "private_place_identity_intent"
    TRUSTED_PLACE_ENDPOINT = "trusted_place_endpoint"
    TRUSTED_ROUTE_ENDPOINT_PAIR = "trusted_route_endpoint_pair"
    PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT = (
        "private_serpapi_hotel_search_intent"
    )


class GuidedProviderExecutionTargetDependency(str, Enum):
    """Why the target cannot be inferred or authorized by this slice."""

    GUIDED_PRIVATE_CONTEXT_BINDING = "guided_private_context_binding"
    TRUSTED_PLACE_IDENTITY_EVIDENCE = "trusted_place_identity_evidence"
    TRUSTED_ROUTE_ENDPOINT_EVIDENCE = "trusted_route_endpoint_evidence"


class GuidedProviderExecutionTargetsStatus(str, Enum):
    """All items need a later exact private target binding."""

    NEEDS_PRIVATE_EXECUTION_TARGETS = "needs_private_execution_targets"


_TARGET_KIND_BY_CAPABILITY = {
    GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: (
        GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT
    ),
    GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: (
        GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT
    ),
    GuidedProviderCapability.GOOGLE_ROUTES: (
        GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR
    ),
    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS: (
        GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT
    ),
}
_CAPABILITY_BY_TOPIC = {
    GuidedEvidenceTopic.PLACE_IDENTITY: (
        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP
    ),
    GuidedEvidenceTopic.CURRENT_OPENING_HOURS: (
        GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS
    ),
    GuidedEvidenceTopic.ROUTE: GuidedProviderCapability.GOOGLE_ROUTES,
    GuidedEvidenceTopic.LODGING: GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
    GuidedEvidenceTopic.AVAILABILITY: (
        GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
    ),
    GuidedEvidenceTopic.PRICE: GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
}
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
_DEPENDENCY_BY_CAPABILITY = {
    GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: (
        GuidedProviderExecutionTargetDependency.GUIDED_PRIVATE_CONTEXT_BINDING
    ),
    GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: (
        GuidedProviderExecutionTargetDependency
        .TRUSTED_PLACE_IDENTITY_EVIDENCE
    ),
    GuidedProviderCapability.GOOGLE_ROUTES: (
        GuidedProviderExecutionTargetDependency
        .TRUSTED_ROUTE_ENDPOINT_EVIDENCE
    ),
    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS: (
        GuidedProviderExecutionTargetDependency.GUIDED_PRIVATE_CONTEXT_BINDING
    ),
}


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTargetItem:
    """One derived requirement; never a target value or provider request."""

    topic: GuidedEvidenceTopic
    capability: GuidedProviderCapability
    request_profile: GuidedProviderRequestProfile
    target_kind: GuidedProviderExecutionTargetKind
    dependency: GuidedProviderExecutionTargetDependency
    max_request_count: int

    def __post_init__(self) -> None:
        exact_enums = (
            ("topic", self.topic, GuidedEvidenceTopic),
            ("capability", self.capability, GuidedProviderCapability),
            (
                "request_profile",
                self.request_profile,
                GuidedProviderRequestProfile,
            ),
            (
                "target_kind",
                self.target_kind,
                GuidedProviderExecutionTargetKind,
            ),
            (
                "dependency",
                self.dependency,
                GuidedProviderExecutionTargetDependency,
            ),
        )
        for name, value, expected_type in exact_enums:
            if type(value) is not expected_type:
                raise TypeError(
                    f"GuidedProviderExecutionTargetItem.{name} must be exact"
                )
        if type(self.max_request_count) is not int or not (
            1 <= self.max_request_count <= _MAX_REQUESTS
        ):
            raise ValueError(
                "GuidedProviderExecutionTargetItem max_request_count is invalid"
            )
        if (
            self.capability is not _CAPABILITY_BY_TOPIC[self.topic]
            or self.request_profile
            is not _REQUEST_PROFILE_BY_CAPABILITY[self.capability]
            or self.target_kind
            is not _TARGET_KIND_BY_CAPABILITY[self.capability]
            or self.dependency
            is not _DEPENDENCY_BY_CAPABILITY[self.capability]
        ):
            raise ValueError(
                "GuidedProviderExecutionTargetItem capability mapping is invalid"
            )

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTargetItem("
            f"topic={self.topic.value!r}, "
            f"capability={self.capability.value!r}, "
            f"target_kind={self.target_kind.value!r}, "
            f"max_request_count={self.max_request_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTargets:
    """Token-gated target requirement plan bound to one accepted preflight."""

    items: tuple[GuidedProviderExecutionTargetItem, ...] = field(repr=False)
    _prepared_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PLAN_TOKEN:
            raise ValueError(
                "Guided provider execution targets require trusted preparation"
            )
        if (
            not isinstance(self.items, tuple)
            or not 1 <= len(self.items) <= _MAX_ITEMS
            or any(
                type(item) is not GuidedProviderExecutionTargetItem
                for item in self.items
            )
            or len({item.topic for item in self.items}) != len(self.items)
            or sum(item.max_request_count for item in self.items)
            > _MAX_REQUESTS
        ):
            raise ValueError("GuidedProviderExecutionTargets.items is invalid")
        prepared_at = _utc_datetime(self._prepared_at, "prepared_at")
        if (
            type(self._context_fingerprint) is not str
            or _CONTEXT_FINGERPRINT_RE.fullmatch(self._context_fingerprint)
            is None
        ):
            raise ValueError(
                "GuidedProviderExecutionTargets context fingerprint is invalid"
            )
        object.__setattr__(
            self,
            "items",
            tuple(sorted(self.items, key=_item_sort_key)),
        )
        object.__setattr__(self, "_prepared_at", prepared_at)

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTargets("
            f"item_count={len(self.items)!r}, "
            f"max_request_count="
            f"{sum(item.max_request_count for item in self.items)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTargetsReview:
    """Redacted assessment of deterministic target requirements."""

    status: GuidedProviderExecutionTargetsStatus
    next_action: str
    accepted_scope_item_count: int
    accepted_max_request_count: int
    preflight_item_count: int
    target_item_count: int
    max_request_count: int
    items: tuple[GuidedProviderExecutionTargetItem, ...] = field(
        default=(),
        repr=False,
    )
    capability_counts: tuple[tuple[GuidedProviderCapability, int], ...] = ()
    target_kind_counts: tuple[
        tuple[GuidedProviderExecutionTargetKind, int], ...
    ] = ()
    dependency_counts: tuple[
        tuple[GuidedProviderExecutionTargetDependency, int], ...
    ] = ()
    data_categories: tuple[GuidedProviderDataCategory, ...] = ()
    estimated_first_paid_tier_google_cost_usd_micros: int = 0
    serpapi_plan_credit_cap: int = 0
    all_provider_costs_have_currency_list_rate_estimates: bool = False
    host_attestation_fresh: bool = False
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_PROVIDER_EXECUTION_TARGETS_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided provider execution target reviews require the assessor"
            )
        if type(self.status) is not GuidedProviderExecutionTargetsStatus:
            raise TypeError(
                "GuidedProviderExecutionTargetsReview.status must be exact"
            )
        if (
            self.status
            is not GuidedProviderExecutionTargetsStatus
            .NEEDS_PRIVATE_EXECUTION_TARGETS
            or self.next_action != _NEXT_ACTION
        ):
            raise ValueError(
                "GuidedProviderExecutionTargetsReview status/action conflict"
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
            ("target_item_count", self.target_item_count, _MAX_ITEMS),
            ("max_request_count", self.max_request_count, _MAX_REQUESTS),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(
                    f"GuidedProviderExecutionTargetsReview.{name} is invalid"
                )
        if (
            self.accepted_scope_item_count != self.preflight_item_count
            or self.preflight_item_count != self.target_item_count
            or self.target_item_count != len(self.items)
            or self.max_request_count
            != sum(item.max_request_count for item in self.items)
            or self.max_request_count > self.accepted_max_request_count
        ):
            raise ValueError(
                "GuidedProviderExecutionTargetsReview counts are inconsistent"
            )
        if (
            not isinstance(self.items, tuple)
            or any(
                type(item) is not GuidedProviderExecutionTargetItem
                for item in self.items
            )
            or tuple(sorted(self.items, key=_item_sort_key)) != self.items
            or len({item.topic for item in self.items}) != len(self.items)
        ):
            raise ValueError(
                "GuidedProviderExecutionTargetsReview items are invalid"
            )
        _validate_complete_counts(
            self.capability_counts,
            GuidedProviderCapability,
            self.target_item_count,
            "capability_counts",
        )
        _validate_complete_counts(
            self.target_kind_counts,
            GuidedProviderExecutionTargetKind,
            self.target_item_count,
            "target_kind_counts",
        )
        _validate_complete_counts(
            self.dependency_counts,
            GuidedProviderExecutionTargetDependency,
            self.target_item_count,
            "dependency_counts",
        )
        if (
            self.capability_counts
            != _complete_counts(
                GuidedProviderCapability,
                Counter(item.capability for item in self.items),
            )
            or self.target_kind_counts
            != _complete_counts(
                GuidedProviderExecutionTargetKind,
                Counter(item.target_kind for item in self.items),
            )
            or self.dependency_counts
            != _complete_counts(
                GuidedProviderExecutionTargetDependency,
                Counter(item.dependency for item in self.items),
            )
        ):
            raise ValueError(
                "GuidedProviderExecutionTargetsReview counts differ from items"
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
                "GuidedProviderExecutionTargetsReview data categories are invalid"
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
                "GuidedProviderExecutionTargetsReview cost/freshness is invalid"
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
            or self.contract_version
            != GUIDED_PROVIDER_EXECUTION_TARGETS_VERSION
        ):
            raise ValueError(
                "GuidedProviderExecutionTargetsReview safe metadata is invalid"
            )

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTargetsReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"target_item_count={self.target_item_count!r}, "
            f"max_request_count={self.max_request_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return target categories and blockers without private target data."""

        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_execution_targets": {
                "exact_private_context_bound": True,
                "host_attestation_fresh": self.host_attestation_fresh,
                "accepted_scope_item_count": self.accepted_scope_item_count,
                "accepted_max_request_count": (
                    self.accepted_max_request_count
                ),
                "preflight_item_count": self.preflight_item_count,
                "target_item_count": self.target_item_count,
                "items": [_safe_item(item) for item in self.items],
                "data_categories": [
                    item.value for item in self.data_categories
                ],
                "max_request_count": self.max_request_count,
                "capability_counts": {
                    item.value: count
                    for item, count in self.capability_counts
                },
                "target_kind_counts": {
                    item.value: count
                    for item, count in self.target_kind_counts
                },
                "dependency_counts": {
                    item.value: count
                    for item, count in self.dependency_counts
                },
                "eligible_for_execution_authorization_item_count": 0,
                "deferred_target_item_count": self.target_item_count,
                "all_execution_targets_bound": False,
                "partial_execution_authorization_permitted": False,
                "provider_result_dependency_auto_authorizes_followup": False,
                "estimated_first_paid_tier_google_cost_usd_micros": (
                    self.estimated_first_paid_tier_google_cost_usd_micros
                ),
                "serpapi_plan_credit_cap": self.serpapi_plan_credit_cap,
                "all_provider_costs_have_currency_list_rate_estimates": (
                    self.all_provider_costs_have_currency_list_rate_estimates
                ),
                "monthly_free_usage_remaining_checked": False,
                "cost_estimate_is_hard_currency_cap": False,
                "execution_time_pricing_policy_retention_recheck_required": (
                    True
                ),
                "credential_availability_recheck_required": True,
                "explicit_execution_authorization_required_before_any_call": (
                    True
                ),
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
        }


def _execution_targets_context_fingerprint(
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
    preflight_response_review: GuidedProviderPreflightResponseReview,
    items: tuple[GuidedProviderExecutionTargetItem, ...],
    prepared_at: datetime,
) -> str:
    canonical = {
        "contract_version": GUIDED_PROVIDER_EXECUTION_TARGETS_VERSION,
        "preflight_response_contract_version": (
            GUIDED_PROVIDER_PREFLIGHT_RESPONSE_VERSION
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
        "provider_preflight_response": _private_context_value(
            preflight_response
        ),
        "provider_preflight_response_review": _private_context_value(
            preflight_response_review
        ),
        "items": [
            _private_context_value(item)
            for item in sorted(items, key=_item_sort_key)
        ],
        "prepared_at": _private_context_value(prepared_at),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_guided_provider_execution_targets(
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
    *,
    evaluation_at: datetime,
) -> GuidedProviderExecutionTargets:
    """Derive target requirements from one exact accepted preflight."""

    prepared_at = _utc_datetime(evaluation_at, "evaluation_at")
    response_review = assess_guided_provider_preflight_response(
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
        evaluation_at=prepared_at,
    )
    if (
        preflight_response.kind
        is not GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
        or response_review.status
        is not GuidedProviderPreflightResponseStatus
        .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION
    ):
        raise ValueError(
            "Execution targets require an exact accepted preflight response"
        )
    items = _derive_items(preflight)
    return GuidedProviderExecutionTargets(
        items=items,
        _prepared_at=prepared_at,
        _context_fingerprint=_execution_targets_context_fingerprint(
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
            response_review,
            items,
            prepared_at,
        ),
        _token=_PLAN_TOKEN,
    )


def assess_guided_provider_execution_targets(
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
    *,
    evaluation_at: datetime,
) -> GuidedProviderExecutionTargetsReview:
    """Revalidate one deterministic target-requirement plan."""

    if type(targets) is not GuidedProviderExecutionTargets:
        raise TypeError(
            "targets must be exact GuidedProviderExecutionTargets"
        )
    current_evaluation = _utc_datetime(evaluation_at, "evaluation_at")
    if current_evaluation < targets._prepared_at:
        raise ValueError("evaluation_at cannot precede target preparation")
    captured_review = assess_guided_provider_preflight_response(
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
        evaluation_at=targets._prepared_at,
    )
    if (
        preflight_response.kind
        is not GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
        or captured_review.status
        is not GuidedProviderPreflightResponseStatus
        .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION
    ):
        raise ValueError(
            "Execution targets require their original accepted preflight"
        )
    expected_items = _derive_items(preflight)
    expected_fingerprint = _execution_targets_context_fingerprint(
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
        captured_review,
        expected_items,
        targets._prepared_at,
    )
    if (
        targets.items != expected_items
        or targets._context_fingerprint != expected_fingerprint
    ):
        raise ValueError(
            "Execution targets do not match the current private context"
        )
    current_review = assess_guided_provider_preflight_response(
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
        evaluation_at=current_evaluation,
    )
    if (
        current_review.status
        is not GuidedProviderPreflightResponseStatus
        .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION
    ):
        raise ValueError("Accepted provider preflight is no longer current")
    target_counter = Counter(item.target_kind for item in targets.items)
    dependency_counter = Counter(item.dependency for item in targets.items)
    needs_verification = tuple(
        dict.fromkeys(
            (*current_review.needs_verification, "provider_execution_targets")
        )
    )
    return GuidedProviderExecutionTargetsReview(
        status=(
            GuidedProviderExecutionTargetsStatus
            .NEEDS_PRIVATE_EXECUTION_TARGETS
        ),
        next_action=_NEXT_ACTION,
        accepted_scope_item_count=current_review.accepted_scope_item_count,
        accepted_max_request_count=current_review.accepted_max_request_count,
        preflight_item_count=current_review.preflight_item_count,
        target_item_count=len(targets.items),
        max_request_count=current_review.max_request_count,
        items=targets.items,
        capability_counts=current_review.capability_counts,
        target_kind_counts=_complete_counts(
            GuidedProviderExecutionTargetKind,
            target_counter,
        ),
        dependency_counts=_complete_counts(
            GuidedProviderExecutionTargetDependency,
            dependency_counter,
        ),
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
        needs_verification=needs_verification,
        _token=_REVIEW_TOKEN,
    )


def _derive_items(
    preflight: GuidedProviderPreflight,
) -> tuple[GuidedProviderExecutionTargetItem, ...]:
    return tuple(
        sorted(
            (
                GuidedProviderExecutionTargetItem(
                    topic=item.topic,
                    capability=item.capability,
                    request_profile=item.request_profile,
                    target_kind=_TARGET_KIND_BY_CAPABILITY[item.capability],
                    dependency=_DEPENDENCY_BY_CAPABILITY[item.capability],
                    max_request_count=item.max_request_count,
                )
                for item in preflight.items
            ),
            key=_item_sort_key,
        )
    )


def _safe_item(item: GuidedProviderExecutionTargetItem) -> dict[str, object]:
    return {
        "topic": item.topic.value,
        "capability": item.capability.value,
        "request_profile": item.request_profile.value,
        "target_kind": item.target_kind.value,
        "dependency": item.dependency.value,
        "max_request_count": item.max_request_count,
        "target_bound": False,
        "eligible_for_execution_authorization": False,
    }


def _complete_counts(
    enum_type: type[Enum],
    counter: Counter[Enum],
) -> tuple[tuple[Enum, int], ...]:
    return tuple(
        (item, counter[item])
        for item in sorted(enum_type, key=lambda value: value.value)
    )


def _validate_complete_counts(
    values: tuple[tuple[Enum, int], ...],
    enum_type: type[Enum],
    expected_total: int,
    name: str,
) -> None:
    expected = tuple(sorted(values, key=lambda item: item[0].value))
    if (
        not isinstance(values, tuple)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or type(item[0]) is not enum_type
            or type(item[1]) is not int
            or not 0 <= item[1] <= _MAX_ITEMS
            for item in values
        )
        or len(values) != len(enum_type)
        or {item[0] for item in values} != set(enum_type)
        or len({item[0] for item in values}) != len(enum_type)
        or values != expected
        or sum(item[1] for item in values) != expected_total
    ):
        raise ValueError(
            f"GuidedProviderExecutionTargetsReview.{name} is invalid"
        )


def _item_sort_key(
    item: GuidedProviderExecutionTargetItem,
) -> tuple[object, ...]:
    return (
        item.topic.value,
        item.capability.value,
        item.request_profile.value,
        item.target_kind.value,
        item.dependency.value,
        item.max_request_count,
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
    "GUIDED_PROVIDER_EXECUTION_TARGETS_VERSION",
    "GuidedProviderExecutionTargetDependency",
    "GuidedProviderExecutionTargetItem",
    "GuidedProviderExecutionTargetKind",
    "GuidedProviderExecutionTargets",
    "GuidedProviderExecutionTargetsReview",
    "GuidedProviderExecutionTargetsStatus",
    "assess_guided_provider_execution_targets",
    "prepare_guided_provider_execution_targets",
]
