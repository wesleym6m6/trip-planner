"""Bounded private provider-scope review for guided evidence needs.

Phase 5.11 consumes the exact ready Phase 5.10 evidence-requirement plan and
lets a host propose a small, auditable set of provider capabilities plus hard
request-count caps.  Capability mappings and disclosed data categories are
fixed by this contract; callers cannot insert provider IDs, queries, payloads,
place references, dates, credentials, or free text.

The resulting review is only a subjective scope-review seam.  It creates no
provider request, performs no provider call, checks no credentials or pricing,
and grants no provider, persistence, scheduling, trip, rendering, deployment,
confirmation, or canonical-mutation authority.  A later exact response and
separate provider-policy/request gate remain mandatory.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import Any

from .guided_draft import TripBriefDraft
from .guided_evidence_plan import (
    GuidedEvidencePlanStatus,
    GuidedEvidenceRequirementPlan,
    GuidedEvidenceTopic,
    assess_guided_evidence_requirement_plan,
)
from .guided_itinerary import GuidedItineraryCandidate, GuidedItineraryResponse
from .guided_proposal import GuidedDirectionCard, GuidedDirectionPreference
from .guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementResponse,
)
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_SCOPE_VERSION = "guided-provider-scope/v1"
_MAX_SCOPE_ITEMS = 12
_MAX_REQUESTS_PER_ITEM = 32
_MAX_TOTAL_REQUESTS = 32
_MAX_VERIFICATION_UNITS = 192
_REFINE_ACTION = "refine_private_provider_scope"
_REVIEW_ACTION = "review_private_provider_scope"
_NO_SCOPE_ACTION = "continue_private_evidence_review"
_REVIEW_TOKEN = object()
_REVIEW_PROMPT = (
    "是否同意依照這個外部查證範圍繼續？你可以接受、縮小範圍或取消。"
)
_REVIEW_DISCLOSURE = (
    "這只是私有查證範圍審閱：可能產生費用，現行價格尚未核對；目前沒有建立請求、"
    "讀取憑證或呼叫 provider，接受範圍也不等於授權執行。"
)
_NO_SCOPE_DISCLOSURE = (
    "目前未識別外部 evidence requirement；這不表示資料已驗證或不需後續確認。"
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
}


class GuidedProviderCapability(str, Enum):
    """Auditable provider capability families, never executable requests."""

    GOOGLE_PLACES_IDENTITY_LOOKUP = "google_places_identity_lookup"
    GOOGLE_PLACES_CURRENT_HOURS = "google_places_current_hours"
    GOOGLE_ROUTES = "google_routes"
    SERPAPI_GOOGLE_HOTELS = "serpapi_google_hotels"


class GuidedProviderDataCategory(str, Enum):
    """Category-only disclosure of values a future request may need."""

    CURRENCY_CONTEXT = "currency_context"
    DESTINATION_CONTEXT = "destination_context"
    LODGING_CRITERIA = "lodging_criteria"
    PARTY_CONTEXT = "party_context"
    PLACE_IDENTITY_CONTEXT = "place_identity_context"
    PLACE_SEARCH_CONTEXT = "place_search_context"
    ROUTE_ENDPOINT_CONTEXT = "route_endpoint_context"
    ROUTE_PREFERENCE_CONTEXT = "route_preference_context"
    TRAVEL_DATE_CONTEXT = "travel_date_context"


class GuidedProviderScopeStatus(str, Enum):
    """Whether a private scope needs repair, review, or no external step."""

    NEEDS_REFINEMENT = "needs_refinement"
    REVIEW_REQUIRED = "review_required"
    NO_PROVIDER_SCOPE_REQUIRED = "no_provider_scope_required"


class GuidedProviderScopeProblemCode(str, Enum):
    """Redacted reasons a provider-scope proposal cannot be reviewed."""

    REQUIRED_TOPIC_NOT_SCOPED = "required_topic_not_scoped"
    UNREQUIRED_TOPIC_SCOPED = "unrequired_topic_scoped"
    TOPIC_SCOPED_MULTIPLE_TIMES = "topic_scoped_multiple_times"
    CAPABILITY_TOPIC_MISMATCH = "capability_topic_mismatch"
    TOTAL_REQUEST_LIMIT_EXCEEDED = "total_request_limit_exceeded"


_CAPABILITY_BY_TOPIC = {
    GuidedEvidenceTopic.PLACE_IDENTITY: (
        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP
    ),
    GuidedEvidenceTopic.CURRENT_OPENING_HOURS: (
        GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS
    ),
    GuidedEvidenceTopic.ROUTE: GuidedProviderCapability.GOOGLE_ROUTES,
    GuidedEvidenceTopic.LODGING: (
        GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
    ),
    GuidedEvidenceTopic.AVAILABILITY: (
        GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
    ),
    GuidedEvidenceTopic.PRICE: (
        GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS
    ),
}
_DATA_CATEGORIES_BY_CAPABILITY = {
    GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: (
        GuidedProviderDataCategory.DESTINATION_CONTEXT,
        GuidedProviderDataCategory.PLACE_SEARCH_CONTEXT,
    ),
    GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: (
        GuidedProviderDataCategory.PLACE_IDENTITY_CONTEXT,
        GuidedProviderDataCategory.TRAVEL_DATE_CONTEXT,
    ),
    GuidedProviderCapability.GOOGLE_ROUTES: (
        GuidedProviderDataCategory.ROUTE_ENDPOINT_CONTEXT,
        GuidedProviderDataCategory.ROUTE_PREFERENCE_CONTEXT,
    ),
    GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS: (
        GuidedProviderDataCategory.CURRENCY_CONTEXT,
        GuidedProviderDataCategory.DESTINATION_CONTEXT,
        GuidedProviderDataCategory.LODGING_CRITERIA,
        GuidedProviderDataCategory.PARTY_CONTEXT,
        GuidedProviderDataCategory.TRAVEL_DATE_CONTEXT,
    ),
}


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderScopeItem:
    """One topic/capability/cap declaration; never a provider request."""

    topic: GuidedEvidenceTopic
    capability: GuidedProviderCapability
    max_request_count: int

    def __post_init__(self) -> None:
        if type(self.topic) is not GuidedEvidenceTopic:
            raise TypeError("GuidedProviderScopeItem.topic must be exact")
        if type(self.capability) is not GuidedProviderCapability:
            raise TypeError("GuidedProviderScopeItem.capability must be exact")
        if type(self.max_request_count) is not int or not (
            1 <= self.max_request_count <= _MAX_REQUESTS_PER_ITEM
        ):
            raise ValueError(
                "GuidedProviderScopeItem.max_request_count is invalid"
            )

    def __repr__(self) -> str:
        return (
            "GuidedProviderScopeItem("
            f"topic={self.topic.value!r}, "
            f"capability={self.capability.value!r}, "
            f"max_request_count={self.max_request_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderScopeProposal:
    """Process-local bounded capability proposal without request content."""

    items: tuple[GuidedProviderScopeItem, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.items, tuple)
            or any(type(item) is not GuidedProviderScopeItem for item in self.items)
        ):
            raise TypeError(
                "GuidedProviderScopeProposal.items must contain exact values"
            )
        if len(self.items) > _MAX_SCOPE_ITEMS:
            raise ValueError("GuidedProviderScopeProposal has too many items")
        object.__setattr__(
            self,
            "items",
            tuple(
                sorted(
                    self.items,
                    key=lambda item: (
                        item.topic.value,
                        item.capability.value,
                        item.max_request_count,
                    ),
                )
            ),
        )

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderScopeProposal("
            f"scope_item_count={len(self.items)!r}, "
            f"max_request_count="
            f"{sum(item.max_request_count for item in self.items)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderScopeReview:
    """Safe review of provider categories and request-count boundaries."""

    status: GuidedProviderScopeStatus
    next_action: str
    required_topic_count: int
    verification_unit_count: int
    scope_item_count: int
    distinct_scoped_topic_count: int
    max_request_count: int
    scope_items: tuple[GuidedProviderScopeItem, ...] = field(
        default=(),
        repr=False,
    )
    data_categories: tuple[GuidedProviderDataCategory, ...] = ()
    problem_codes: tuple[GuidedProviderScopeProblemCode, ...] = ()
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_PROVIDER_SCOPE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided provider scope reviews must be created by the assessor"
            )
        if type(self.status) is not GuidedProviderScopeStatus:
            raise TypeError("GuidedProviderScopeReview.status must be exact")
        for name, value, maximum in (
            ("required_topic_count", self.required_topic_count, 6),
            (
                "verification_unit_count",
                self.verification_unit_count,
                _MAX_VERIFICATION_UNITS,
            ),
            ("scope_item_count", self.scope_item_count, _MAX_SCOPE_ITEMS),
            (
                "distinct_scoped_topic_count",
                self.distinct_scoped_topic_count,
                6,
            ),
            (
                "max_request_count",
                self.max_request_count,
                _MAX_SCOPE_ITEMS * _MAX_REQUESTS_PER_ITEM,
            ),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"GuidedProviderScopeReview.{name} is invalid")
        if (
            self.required_topic_count == 0
            and self.verification_unit_count != 0
        ) or (
            self.required_topic_count > 0
            and self.verification_unit_count < self.required_topic_count
        ):
            raise ValueError(
                "GuidedProviderScopeReview requirement counts are inconsistent"
            )
        if (
            not isinstance(self.scope_items, tuple)
            or any(type(item) is not GuidedProviderScopeItem for item in self.scope_items)
            or len(self.scope_items) != self.scope_item_count
            or len({item.topic for item in self.scope_items})
            != self.distinct_scoped_topic_count
            or sum(item.max_request_count for item in self.scope_items)
            != self.max_request_count
        ):
            raise ValueError("GuidedProviderScopeReview scope counts are invalid")
        expected_categories = tuple(
            sorted(
                {
                    category
                    for item in self.scope_items
                    for category in _DATA_CATEGORIES_BY_CAPABILITY[
                        item.capability
                    ]
                },
                key=lambda item: item.value,
            )
        )
        if (
            not isinstance(self.data_categories, tuple)
            or any(
                type(item) is not GuidedProviderDataCategory
                for item in self.data_categories
            )
            or self.data_categories != expected_categories
        ):
            raise ValueError(
                "GuidedProviderScopeReview data categories are invalid"
            )
        if (
            not isinstance(self.problem_codes, tuple)
            or any(
                type(item) is not GuidedProviderScopeProblemCode
                for item in self.problem_codes
            )
            or tuple(
                sorted(set(self.problem_codes), key=lambda item: item.value)
            )
            != self.problem_codes
        ):
            raise ValueError(
                "GuidedProviderScopeReview problem codes are invalid"
            )
        if self.status is GuidedProviderScopeStatus.NEEDS_REFINEMENT:
            if not self.problem_codes or self.next_action != _REFINE_ACTION:
                raise ValueError(
                    "Needs-refinement provider scope requires repair problems"
                )
        elif self.status is GuidedProviderScopeStatus.REVIEW_REQUIRED:
            if (
                self.problem_codes
                or self.next_action != _REVIEW_ACTION
                or self.required_topic_count < 1
                or self.scope_item_count != self.required_topic_count
                or self.distinct_scoped_topic_count
                != self.required_topic_count
                or not 1 <= self.max_request_count <= _MAX_TOTAL_REQUESTS
                or any(
                    item.capability is not _CAPABILITY_BY_TOPIC[item.topic]
                    for item in self.scope_items
                )
            ):
                raise ValueError(
                    "Review-required provider scope is inconsistent"
                )
        elif (
            self.problem_codes
            or self.next_action != _NO_SCOPE_ACTION
            or self.required_topic_count != 0
            or self.verification_unit_count != 0
            or self.scope_item_count != 0
            or self.distinct_scoped_topic_count != 0
            or self.max_request_count != 0
            or self.data_categories
        ):
            raise ValueError("No-provider-scope result is inconsistent")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
        ):
            raise ValueError(
                "GuidedProviderScopeReview safe topic collections are invalid"
            )
        if self.contract_version != GUIDED_PROVIDER_SCOPE_VERSION:
            raise ValueError("Unsupported guided provider scope contract version")

    @property
    def may_present_provider_scope(self) -> bool:
        return self.status is GuidedProviderScopeStatus.REVIEW_REQUIRED

    @property
    def no_provider_scope_required(self) -> bool:
        return (
            self.status
            is GuidedProviderScopeStatus.NO_PROVIDER_SCOPE_REQUIRED
        )

    @property
    def review_prompt(self) -> str | None:
        return _REVIEW_PROMPT if self.may_present_provider_scope else None

    @property
    def review_disclosure(self) -> str | None:
        return _REVIEW_DISCLOSURE if self.may_present_provider_scope else None

    @property
    def no_scope_disclosure(self) -> str | None:
        return _NO_SCOPE_DISCLOSURE if self.no_provider_scope_required else None

    def __repr__(self) -> str:
        return (
            "GuidedProviderScopeReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"problem_count={len(self.problem_codes)!r}, "
            f"scope_item_count={self.scope_item_count!r}, "
            f"max_request_count={self.max_request_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a reviewable typed scope without private request values."""

        presentable = self.may_present_provider_scope
        no_scope = self.no_provider_scope_required
        scope_items = (
            [
                {
                    "topic": item.topic.value,
                    "capability": item.capability.value,
                    "max_request_count": item.max_request_count,
                }
                for item in self.scope_items
            ]
            if presentable
            else []
        )
        data_categories = (
            [item.value for item in self.data_categories]
            if presentable
            else []
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": presentable,
            "requires_user_review": presentable,
            "requires_user_decision": presentable,
            "may_present_provider_scope": presentable,
            "review_prompt": self.review_prompt,
            "review_disclosure": self.review_disclosure,
            "no_provider_scope_disclosure": self.no_scope_disclosure,
            "provider_scope": {
                "required_topic_count": self.required_topic_count,
                "verification_unit_count": self.verification_unit_count,
                "scope_item_count": self.scope_item_count,
                "distinct_scoped_topic_count": (
                    self.distinct_scoped_topic_count
                ),
                "items": scope_items,
                "data_categories": data_categories,
                "max_request_count": self.max_request_count,
                "hard_request_cap": _MAX_TOTAL_REQUESTS,
                "within_hard_request_cap": (
                    self.max_request_count <= _MAX_TOTAL_REQUESTS
                ),
                "request_cap_is_currency_cost_limit": False,
                "potentially_billable": presentable,
                "pricing_verified": False,
                "must_check_current_pricing_before_call": presentable,
                "scope_plan_retention": "process_local_only",
                "provider_policy_review_required_before_call": presentable,
                "provider_terms_and_retention_may_apply": presentable,
                "host_managed_credentials_required_before_call": presentable,
                "no_provider_scope_required": no_scope,
                "no_external_evidence_identified_means_verified": False,
                "contains_line_indexes": False,
                "contains_private_values": False,
                "contains_provider_resource_identifiers": False,
                "contains_provider_query": False,
                "contains_provider_payload": False,
                "provider_scope_authorized": False,
                "provider_requests_created": False,
                "provider_calls_permitted": False,
                "decision_state": DecisionState.CANDIDATE.value,
                "evidence_state": EvidenceState.UNVERIFIED.value,
                "supports_authoritative_use": False,
            },
            "problems": [item.value for item in self.problem_codes],
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "credentials_accessed": False,
                "pricing_checked": False,
                "writes_to_trip": False,
                "provider_calls": False,
                "provider_requests_created": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }


def assess_guided_provider_scope(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    evidence_plan: GuidedEvidenceRequirementPlan,
    proposal: GuidedProviderScopeProposal,
) -> GuidedProviderScopeReview:
    """Revalidate Phase 5.10 and assess one bounded provider-scope proposal."""

    if type(proposal) is not GuidedProviderScopeProposal:
        raise TypeError("proposal must be an exact GuidedProviderScopeProposal")
    evidence_review = assess_guided_evidence_requirement_plan(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
        evidence_plan,
    )
    if (
        evidence_review.status
        is not GuidedEvidencePlanStatus.READY_FOR_PRIVATE_PROVIDER_SCOPE
    ):
        raise ValueError(
            "A provider scope requires an exact ready evidence requirement plan"
        )

    required_counts: Counter[GuidedEvidenceTopic] = Counter(
        topic
        for declaration in evidence_plan.declarations
        for topic in declaration.topics
    )
    required_topics = set(required_counts)
    scope_counts = Counter(item.topic for item in proposal.items)
    problems: set[GuidedProviderScopeProblemCode] = set()
    if any(scope_counts[topic] == 0 for topic in required_topics):
        problems.add(
            GuidedProviderScopeProblemCode.REQUIRED_TOPIC_NOT_SCOPED
        )
    if any(topic not in required_topics for topic in scope_counts):
        problems.add(GuidedProviderScopeProblemCode.UNREQUIRED_TOPIC_SCOPED)
    if any(count > 1 for count in scope_counts.values()):
        problems.add(
            GuidedProviderScopeProblemCode.TOPIC_SCOPED_MULTIPLE_TIMES
        )
    if any(
        item.capability is not _CAPABILITY_BY_TOPIC[item.topic]
        for item in proposal.items
    ):
        problems.add(
            GuidedProviderScopeProblemCode.CAPABILITY_TOPIC_MISMATCH
        )
    total_request_limit = sum(
        item.max_request_count for item in proposal.items
    )
    if total_request_limit > _MAX_TOTAL_REQUESTS:
        problems.add(
            GuidedProviderScopeProblemCode.TOTAL_REQUEST_LIMIT_EXCEEDED
        )

    problem_codes = tuple(sorted(problems, key=lambda item: item.value))
    if problem_codes:
        status = GuidedProviderScopeStatus.NEEDS_REFINEMENT
        next_action = _REFINE_ACTION
    elif required_topics:
        status = GuidedProviderScopeStatus.REVIEW_REQUIRED
        next_action = _REVIEW_ACTION
    else:
        status = GuidedProviderScopeStatus.NO_PROVIDER_SCOPE_REQUIRED
        next_action = _NO_SCOPE_ACTION
    data_categories = tuple(
        sorted(
            {
                category
                for item in proposal.items
                for category in _DATA_CATEGORIES_BY_CAPABILITY[item.capability]
            },
            key=lambda item: item.value,
        )
    )
    needs_verification = tuple(
        dict.fromkeys(
            (
                *evidence_review.needs_verification,
                *(("provider_scope",) if required_topics else ()),
            )
        )
    )
    return GuidedProviderScopeReview(
        status=status,
        next_action=next_action,
        required_topic_count=len(required_topics),
        verification_unit_count=sum(required_counts.values()),
        scope_item_count=len(proposal.items),
        distinct_scoped_topic_count=len(scope_counts),
        max_request_count=total_request_limit,
        scope_items=proposal.items,
        data_categories=data_categories,
        problem_codes=problem_codes,
        tentative_fields=evidence_review.tentative_fields,
        needs_verification=needs_verification,
        _token=_REVIEW_TOKEN,
    )


__all__ = [
    "GUIDED_PROVIDER_SCOPE_VERSION",
    "GuidedProviderCapability",
    "GuidedProviderDataCategory",
    "GuidedProviderScopeItem",
    "GuidedProviderScopeProblemCode",
    "GuidedProviderScopeProposal",
    "GuidedProviderScopeReview",
    "GuidedProviderScopeStatus",
    "assess_guided_provider_scope",
]
