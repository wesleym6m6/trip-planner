"""Exact response handoff for one visible private provider-scope review.

Phase 5.12 captures only an unambiguous accept, reduce, or cancel response to
the exact current Phase 5.11 review.  It binds the full private guided context,
evidence-requirement plan, provider-scope proposal, and freshly derived safe
review so that any drift invalidates the response.  Card ordering alone is
canonicalized.

Acceptance is for private preflight review preparation only.  It does not
review pricing or provider policy, access credentials, create a request, call
a provider, persist data, schedule an itinerary, create a trip, render,
deploy, confirm, or authorize any external or canonical action.  Cancellation
only closes the current external-lookup path and never removes evidence needs
or upgrades the itinerary from candidate + unverified.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import InitVar, dataclass, field
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
from .guided_provider_scope import (
    GUIDED_PROVIDER_SCOPE_VERSION,
    GuidedProviderCapability,
    GuidedProviderScopeProposal,
    GuidedProviderScopeReview,
    GuidedProviderScopeStatus,
    assess_guided_provider_scope,
)
from .guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementResponse,
)
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION = "guided-provider-scope-response/v1"
_CONTEXT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_MAX_VERIFICATION_UNITS = 192
_ACCEPT_ACTION = "prepare_private_provider_preflight_review"
_REFINE_ACTION = "refine_private_provider_scope"
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
}


class GuidedProviderScopeResponseKind(str, Enum):
    """One clear response to the exact visible provider-scope review."""

    ACCEPT_PROVIDER_SCOPE = "accept_provider_scope"
    REQUEST_SMALLER_PROVIDER_SCOPE = "request_smaller_provider_scope"
    CANCEL_EXTERNAL_LOOKUP = "cancel_external_lookup"


class GuidedProviderScopeResponseStatus(str, Enum):
    """Safe handoff after one exact private provider-scope response."""

    READY_FOR_PRIVATE_PROVIDER_PREFLIGHT = (
        "ready_for_private_provider_preflight"
    )
    READY_FOR_PRIVATE_PROVIDER_SCOPE_REFINEMENT = (
        "ready_for_private_provider_scope_refinement"
    )
    EXTERNAL_LOOKUP_CANCELLED = "external_lookup_cancelled"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderScopeResponse:
    """Typed response bound to one exact process-local scope review."""

    kind: GuidedProviderScopeResponseKind
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESPONSE_TOKEN:
            raise ValueError(
                "Guided provider scope responses must be captured by the host"
            )
        if type(self.kind) is not GuidedProviderScopeResponseKind:
            raise TypeError("GuidedProviderScopeResponse.kind must be exact")
        if (
            type(self._context_fingerprint) is not str
            or _CONTEXT_FINGERPRINT_RE.fullmatch(self._context_fingerprint)
            is None
        ):
            raise ValueError(
                "GuidedProviderScopeResponse context fingerprint is invalid"
            )

    def __repr__(self) -> str:
        return f"GuidedProviderScopeResponse(kind={self.kind.value!r})"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderScopeResponseReview:
    """Redacted, non-authoritative handoff from an exact scope response."""

    status: GuidedProviderScopeResponseStatus
    response_kind: GuidedProviderScopeResponseKind
    next_action: str
    required_topic_count: int
    verification_unit_count: int
    scope_item_count: int
    max_request_count: int
    capability_counts: tuple[tuple[GuidedProviderCapability, int], ...] = ()
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided provider scope response reviews require the assessor"
            )
        if type(self.status) is not GuidedProviderScopeResponseStatus:
            raise TypeError(
                "GuidedProviderScopeResponseReview.status must be exact"
            )
        if type(self.response_kind) is not GuidedProviderScopeResponseKind:
            raise TypeError(
                "GuidedProviderScopeResponseReview.response_kind must be exact"
            )
        for name, value, minimum, maximum in (
            ("required_topic_count", self.required_topic_count, 1, 6),
            (
                "verification_unit_count",
                self.verification_unit_count,
                1,
                _MAX_VERIFICATION_UNITS,
            ),
            ("scope_item_count", self.scope_item_count, 1, 6),
            ("max_request_count", self.max_request_count, 1, 32),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(
                    f"GuidedProviderScopeResponseReview.{name} is invalid"
                )
        if (
            self.scope_item_count != self.required_topic_count
            or self.verification_unit_count < self.required_topic_count
        ):
            raise ValueError(
                "GuidedProviderScopeResponseReview counts are inconsistent"
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
                or not 0 <= item[1] <= 6
                for item in self.capability_counts
            )
            or len(self.capability_counts) != len(GuidedProviderCapability)
            or {item[0] for item in self.capability_counts}
            != set(GuidedProviderCapability)
            or len({item[0] for item in self.capability_counts})
            != len(GuidedProviderCapability)
            or self.capability_counts != expected_capability_counts
            or sum(item[1] for item in self.capability_counts)
            != self.scope_item_count
        ):
            raise ValueError(
                "GuidedProviderScopeResponseReview capability counts are invalid"
            )
        expected = {
            GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE: (
                GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_PREFLIGHT,
                _ACCEPT_ACTION,
            ),
            GuidedProviderScopeResponseKind.REQUEST_SMALLER_PROVIDER_SCOPE: (
                GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_SCOPE_REFINEMENT,
                _REFINE_ACTION,
            ),
            GuidedProviderScopeResponseKind.CANCEL_EXTERNAL_LOOKUP: (
                GuidedProviderScopeResponseStatus.EXTERNAL_LOOKUP_CANCELLED,
                _CANCEL_ACTION,
            ),
        }[self.response_kind]
        if (self.status, self.next_action) != expected:
            raise ValueError(
                "GuidedProviderScopeResponseReview status/action conflict"
            )
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
        ):
            raise ValueError(
                "GuidedProviderScopeResponseReview safe topics are invalid"
            )
        if self.contract_version != GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION:
            raise ValueError(
                "Unsupported guided provider scope response contract version"
            )

    def __repr__(self) -> str:
        return (
            "GuidedProviderScopeResponseReview("
            f"status={self.status.value!r}, "
            f"response_kind={self.response_kind.value!r}, "
            f"next_action={self.next_action!r}, "
            f"scope_item_count={self.scope_item_count!r}, "
            f"max_request_count={self.max_request_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return aggregate response state without private scope contents."""

        accepted = (
            self.response_kind
            is GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE
        )
        smaller = (
            self.response_kind
            is GuidedProviderScopeResponseKind.REQUEST_SMALLER_PROVIDER_SCOPE
        )
        cancelled = (
            self.response_kind
            is GuidedProviderScopeResponseKind.CANCEL_EXTERNAL_LOOKUP
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_scope_response": {
                "kind": self.response_kind.value,
                "accepted_for_private_preflight_review": accepted,
                "requested_smaller_provider_scope": smaller,
                "external_lookup_cancelled_for_current_scope": cancelled,
                "may_prepare_private_provider_preflight_review": accepted,
                "may_refine_private_provider_scope": smaller,
                "evidence_requirements_preserved": True,
                "required_topic_count": self.required_topic_count,
                "verification_unit_count": self.verification_unit_count,
                "scope_item_count": self.scope_item_count,
                "max_request_count": self.max_request_count,
                "capability_counts": {
                    capability.value: count
                    for capability, count in self.capability_counts
                },
                "scope_acceptance_is_provider_authorization": False,
                "pricing_checked": False,
                "provider_policy_reviewed": False,
                "credentials_accessed": False,
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
                "credentials_accessed": False,
                "pricing_checked": False,
                "provider_policy_reviewed": False,
                "writes_to_trip": False,
                "provider_calls": False,
                "provider_requests_created": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }


def _scope_response_context_fingerprint(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    provider_scope_review: GuidedProviderScopeReview,
) -> str:
    """Bind a response to the exact private scope and fresh derived review."""

    canonical = {
        "contract_version": GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION,
        "provider_scope_contract_version": GUIDED_PROVIDER_SCOPE_VERSION,
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
        "provider_scope_review": _private_context_value(provider_scope_review),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_guided_provider_scope_response(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    provider_scope: GuidedProviderScopeProposal,
    *,
    kind: GuidedProviderScopeResponseKind,
) -> GuidedProviderScopeResponse:
    """Capture one unambiguous response to the exact visible scope review."""

    if type(kind) is not GuidedProviderScopeResponseKind:
        raise TypeError(
            "kind must be an exact GuidedProviderScopeResponseKind"
        )
    review = assess_guided_provider_scope(
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
    if review.status is not GuidedProviderScopeStatus.REVIEW_REQUIRED:
        raise ValueError(
            "A provider scope response requires a current visible scope"
        )
    return GuidedProviderScopeResponse(
        kind=kind,
        _context_fingerprint=_scope_response_context_fingerprint(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            review,
        ),
        _token=_RESPONSE_TOKEN,
    )


def assess_guided_provider_scope_response(
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
) -> GuidedProviderScopeResponseReview:
    """Revalidate and hand off one exact private provider-scope response."""

    if type(response) is not GuidedProviderScopeResponse:
        raise TypeError(
            "response must be an exact GuidedProviderScopeResponse"
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
    if scope_review.status is not GuidedProviderScopeStatus.REVIEW_REQUIRED:
        raise ValueError(
            "A provider scope response requires a current visible scope"
        )
    if response._context_fingerprint != _scope_response_context_fingerprint(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
        provider_scope,
        scope_review,
    ):
        raise ValueError(
            "Provider scope response does not match the current private context"
        )
    status, next_action = {
        GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE: (
            GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_PREFLIGHT,
            _ACCEPT_ACTION,
        ),
        GuidedProviderScopeResponseKind.REQUEST_SMALLER_PROVIDER_SCOPE: (
            GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_SCOPE_REFINEMENT,
            _REFINE_ACTION,
        ),
        GuidedProviderScopeResponseKind.CANCEL_EXTERNAL_LOOKUP: (
            GuidedProviderScopeResponseStatus.EXTERNAL_LOOKUP_CANCELLED,
            _CANCEL_ACTION,
        ),
    }[response.kind]
    capability_counter = Counter(
        item.capability for item in provider_scope.items
    )
    capability_counts = tuple(
        (capability, capability_counter[capability])
        for capability in sorted(
            GuidedProviderCapability,
            key=lambda item: item.value,
        )
    )
    return GuidedProviderScopeResponseReview(
        status=status,
        response_kind=response.kind,
        next_action=next_action,
        required_topic_count=scope_review.required_topic_count,
        verification_unit_count=scope_review.verification_unit_count,
        scope_item_count=scope_review.scope_item_count,
        max_request_count=scope_review.max_request_count,
        capability_counts=capability_counts,
        tentative_fields=scope_review.tentative_fields,
        needs_verification=scope_review.needs_verification,
        _token=_REVIEW_TOKEN,
    )


__all__ = [
    "GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION",
    "GuidedProviderScopeResponse",
    "GuidedProviderScopeResponseKind",
    "GuidedProviderScopeResponseReview",
    "GuidedProviderScopeResponseStatus",
    "assess_guided_provider_scope_response",
    "capture_guided_provider_scope_response",
]
