"""Exact private target bindings for guided provider execution.

Phase 5.16 consumes a fresh Phase 5.15 target-requirement plan and exact typed
private preimages.  Each preimage is tied to the source lines whose evidence
requirement it serves.  The binder validates the existing trusted contract,
computes its own domain-separated fingerprint, and retains only private
fingerprints plus policy/snapshot/evidence revisions.

No caller-supplied target digest is accepted.  This module creates neither a
provider request contract nor an HTTP request, performs no provider call, and
grants no execution authority.  A complete binding may only advance to a
separate private execution-authorization review; the itinerary remains
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
from .guided_provider_execution_targets import (
    GUIDED_PROVIDER_EXECUTION_TARGETS_VERSION,
    GuidedProviderExecutionTargetItem,
    GuidedProviderExecutionTargetKind,
    GuidedProviderExecutionTargets,
    GuidedProviderExecutionTargetsReview,
    GuidedProviderExecutionTargetsStatus,
    assess_guided_provider_execution_targets,
)
from .guided_provider_preflight import (
    GuidedProviderPreflight,
    GuidedProviderPreflightItem,
)
from .guided_provider_preflight_response import GuidedProviderPreflightResponse
from .guided_provider_scope import (
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
from .place_details import GooglePlaceDetailsRequest, PlaceDetailsKind
from .places_identity import PlaceIdentityIntent
from .routes import GoogleRouteRequest
from .facts import GOOGLE_MAPS_NON_EEA_POLICY_PROFILE


GUIDED_PROVIDER_EXECUTION_TARGET_BINDINGS_VERSION = (
    "guided-provider-execution-target-bindings/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ITEMS = 12
_MAX_REQUESTS = 32
_MAX_SOURCE_LINE_INDEX = 31
_MAX_GOOGLE_LIST_COST_USD_MICROS = 32 * 32_000
_NEXT_ACTION = "prepare_private_provider_execution_authorization_review"
_BINDINGS_TOKEN = object()
_ITEM_TOKEN = object()
_RECORD_TOKEN = object()
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
}


class GuidedProviderExecutionTargetSourceKind(str, Enum):
    """The validated contract family used as one private target preimage."""

    CANONICAL_PRIVATE_INTENT = "canonical_private_intent"
    TRUSTED_EVIDENCE_REQUEST_CONTRACT = (
        "trusted_evidence_request_contract"
    )


class GuidedProviderExecutionTargetBindingsStatus(str, Enum):
    """A complete binding still needs a separate authorization review."""

    READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW = (
        "ready_for_private_provider_execution_authorization_review"
    )


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
_TARGET_TYPE_BY_TARGET_KIND = {
    GuidedProviderExecutionTargetKind.PRIVATE_PLACE_IDENTITY_INTENT: (
        PlaceIdentityIntent
    ),
    GuidedProviderExecutionTargetKind.TRUSTED_PLACE_ENDPOINT: (
        GooglePlaceDetailsRequest
    ),
    GuidedProviderExecutionTargetKind.TRUSTED_ROUTE_ENDPOINT_PAIR: (
        GoogleRouteRequest
    ),
    GuidedProviderExecutionTargetKind.PRIVATE_SERPAPI_HOTEL_SEARCH_INTENT: (
        LodgingDiscoveryRequest
    ),
}


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTargetPreimage:
    """One exact typed target and the private source lines it serves."""

    topic: GuidedEvidenceTopic
    source_line_indexes: tuple[int, ...] = field(repr=False)
    target: object = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.topic) is not GuidedEvidenceTopic:
            raise TypeError(
                "GuidedProviderExecutionTargetPreimage.topic must be exact"
            )
        if (
            not isinstance(self.source_line_indexes, tuple)
            or not self.source_line_indexes
            or any(
                type(item) is not int
                or not 0 <= item <= _MAX_SOURCE_LINE_INDEX
                for item in self.source_line_indexes
            )
            or tuple(sorted(set(self.source_line_indexes)))
            != self.source_line_indexes
        ):
            raise ValueError(
                "GuidedProviderExecutionTargetPreimage source lines are invalid"
            )
        if type(self.target) not in set(_TARGET_TYPE_BY_TARGET_KIND.values()):
            raise TypeError(
                "Guided provider execution target preimage type is unsupported"
            )

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTargetPreimage("
            f"topic={self.topic.value!r}, "
            f"source_line_count={len(self.source_line_indexes)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _GuidedProviderExecutionTargetRecord:
    """Private derived fingerprint and revision binding for one target."""

    source_kind: GuidedProviderExecutionTargetSourceKind
    source_line_indexes: tuple[int, ...] = field(repr=False)
    target_fingerprint: str = field(repr=False)
    policy_registry_revision: str | None = field(default=None, repr=False)
    snapshot_id: str | None = field(default=None, repr=False)
    store_revision: str | None = field(default=None, repr=False)
    evidence_revision: str | None = field(default=None, repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RECORD_TOKEN:
            raise ValueError("Target records require the trusted binder")
        if type(self.source_kind) is not GuidedProviderExecutionTargetSourceKind:
            raise TypeError("Target record source kind must be exact")
        if (
            not isinstance(self.source_line_indexes, tuple)
            or not self.source_line_indexes
            or tuple(sorted(set(self.source_line_indexes)))
            != self.source_line_indexes
        ):
            raise ValueError("Target record source lines are invalid")
        _digest(self.target_fingerprint, "target_fingerprint")
        revisions = (
            self.policy_registry_revision,
            self.snapshot_id,
            self.store_revision,
            self.evidence_revision,
        )
        if self.source_kind is (
            GuidedProviderExecutionTargetSourceKind
            .TRUSTED_EVIDENCE_REQUEST_CONTRACT
        ):
            if any(value is None for value in revisions):
                raise ValueError(
                    "Trusted target records require exact revision bindings"
                )
            for value in revisions:
                assert value is not None
                _digest(value, "trusted target revision")
        elif any(value is not None for value in revisions):
            raise ValueError(
                "Private intent records cannot claim evidence revisions"
            )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTargetBindingItem:
    """One Phase 5.15 requirement with all exact target preimages bound."""

    requirement: GuidedProviderExecutionTargetItem
    _records: tuple[_GuidedProviderExecutionTargetRecord, ...] = field(
        repr=False
    )
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ITEM_TOKEN:
            raise ValueError("Target binding items require the trusted binder")
        if type(self.requirement) is not GuidedProviderExecutionTargetItem:
            raise TypeError("Target binding requirement must be exact")
        if (
            not isinstance(self._records, tuple)
            or not 1 <= len(self._records) <= self.requirement.max_request_count
            or any(
                type(item) is not _GuidedProviderExecutionTargetRecord
                for item in self._records
            )
            or tuple(sorted(self._records, key=_record_sort_key))
            != self._records
            or len({item.target_fingerprint for item in self._records})
            != len(self._records)
            or any(
                item.source_kind
                is not _SOURCE_KIND_BY_TARGET_KIND[
                    self.requirement.target_kind
                ]
                for item in self._records
            )
        ):
            raise ValueError("Target binding records are invalid")

    @property
    def bound_target_count(self) -> int:
        return len(self._records)

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTargetBindingItem("
            f"topic={self.requirement.topic.value!r}, "
            f"bound_target_count={self.bound_target_count!r}, "
            f"max_request_count={self.requirement.max_request_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTargetBindings:
    """Token-gated exact bindings with no retained raw target preimages."""

    items: tuple[GuidedProviderExecutionTargetBindingItem, ...] = field(
        repr=False
    )
    _bound_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _BINDINGS_TOKEN:
            raise ValueError(
                "Guided provider target bindings require trusted preparation"
            )
        if (
            not isinstance(self.items, tuple)
            or not 1 <= len(self.items) <= _MAX_ITEMS
            or any(
                type(item) is not GuidedProviderExecutionTargetBindingItem
                for item in self.items
            )
            or tuple(sorted(self.items, key=_binding_item_sort_key))
            != self.items
            or len({item.requirement.topic for item in self.items})
            != len(self.items)
            or sum(item.bound_target_count for item in self.items)
            > _MAX_REQUESTS
        ):
            raise ValueError("GuidedProviderExecutionTargetBindings.items is invalid")
        object.__setattr__(
            self,
            "_bound_at",
            _utc_datetime(self._bound_at, "bound_at"),
        )
        _digest(self._context_fingerprint, "context_fingerprint")

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTargetBindings("
            f"item_count={len(self.items)!r}, "
            f"bound_target_count="
            f"{sum(item.bound_target_count for item in self.items)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderExecutionTargetBindingsReview:
    """Aggregate safe handoff after exact private target binding."""

    status: GuidedProviderExecutionTargetBindingsStatus
    next_action: str
    accepted_scope_item_count: int
    accepted_max_request_count: int
    preflight_item_count: int
    target_item_count: int
    binding_item_count: int
    bound_target_count: int
    bound_source_line_reference_count: int
    max_request_count: int
    items: tuple[GuidedProviderExecutionTargetBindingItem, ...] = field(
        default=(), repr=False
    )
    source_kind_counts: tuple[
        tuple[GuidedProviderExecutionTargetSourceKind, int], ...
    ] = ()
    data_categories: tuple[GuidedProviderDataCategory, ...] = ()
    estimated_first_paid_tier_google_cost_usd_micros: int = 0
    serpapi_plan_credit_cap: int = 0
    all_provider_costs_have_currency_list_rate_estimates: bool = False
    host_attestation_fresh: bool = False
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = (
        GUIDED_PROVIDER_EXECUTION_TARGET_BINDINGS_VERSION
    )
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Target binding reviews require the assessor")
        if type(self.status) is not GuidedProviderExecutionTargetBindingsStatus:
            raise TypeError("Target binding review status must be exact")
        if (
            self.status
            is not GuidedProviderExecutionTargetBindingsStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW
            or self.next_action != _NEXT_ACTION
        ):
            raise ValueError("Target binding review status/action conflict")
        for name, value, maximum in (
            ("accepted_scope_item_count", self.accepted_scope_item_count, _MAX_ITEMS),
            (
                "accepted_max_request_count",
                self.accepted_max_request_count,
                _MAX_REQUESTS,
            ),
            ("preflight_item_count", self.preflight_item_count, _MAX_ITEMS),
            ("target_item_count", self.target_item_count, _MAX_ITEMS),
            ("binding_item_count", self.binding_item_count, _MAX_ITEMS),
            ("bound_target_count", self.bound_target_count, _MAX_REQUESTS),
            (
                "bound_source_line_reference_count",
                self.bound_source_line_reference_count,
                _MAX_ITEMS * (_MAX_SOURCE_LINE_INDEX + 1),
            ),
            ("max_request_count", self.max_request_count, _MAX_REQUESTS),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(
                    f"GuidedProviderExecutionTargetBindingsReview.{name} is invalid"
                )
        if (
            self.accepted_scope_item_count != self.preflight_item_count
            or self.preflight_item_count != self.target_item_count
            or self.target_item_count != self.binding_item_count
            or self.binding_item_count != len(self.items)
            or self.bound_target_count
            != sum(item.bound_target_count for item in self.items)
            or self.bound_target_count > self.max_request_count
            or self.max_request_count > self.accepted_max_request_count
            or self.bound_source_line_reference_count
            != sum(
                len(record.source_line_indexes)
                for item in self.items
                for record in item._records
            )
        ):
            raise ValueError("Target binding review counts are inconsistent")
        if (
            not isinstance(self.items, tuple)
            or any(
                type(item) is not GuidedProviderExecutionTargetBindingItem
                for item in self.items
            )
            or tuple(sorted(self.items, key=_binding_item_sort_key))
            != self.items
        ):
            raise ValueError("Target binding review items are invalid")
        _validate_source_kind_counts(
            self.source_kind_counts,
            self.bound_target_count,
        )
        actual_source_counts = Counter(
            record.source_kind
            for item in self.items
            for record in item._records
        )
        if self.source_kind_counts != _complete_source_kind_counts(
            actual_source_counts
        ):
            raise ValueError("Target binding review source counts differ")
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
            raise ValueError("Target binding review data categories are invalid")
        if (
            type(self.estimated_first_paid_tier_google_cost_usd_micros)
            is not int
            or not 0
            <= self.estimated_first_paid_tier_google_cost_usd_micros
            <= _MAX_GOOGLE_LIST_COST_USD_MICROS
            or type(self.serpapi_plan_credit_cap) is not int
            or not 0 <= self.serpapi_plan_credit_cap <= _MAX_REQUESTS
            or self.serpapi_plan_credit_cap > self.max_request_count
            or type(self.all_provider_costs_have_currency_list_rate_estimates)
            is not bool
            or self.all_provider_costs_have_currency_list_rate_estimates
            is not (self.serpapi_plan_credit_cap == 0)
            or type(self.host_attestation_fresh) is not bool
            or not self.host_attestation_fresh
        ):
            raise ValueError("Target binding review cost/freshness is invalid")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
            or self.contract_version
            != GUIDED_PROVIDER_EXECUTION_TARGET_BINDINGS_VERSION
        ):
            raise ValueError("Target binding review safe metadata is invalid")

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecutionTargetBindingsReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"binding_item_count={self.binding_item_count!r}, "
            f"bound_target_count={self.bound_target_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return aggregate binding readiness without private target material."""

        private_intent_count = dict(self.source_kind_counts)[
            GuidedProviderExecutionTargetSourceKind.CANONICAL_PRIVATE_INTENT
        ]
        trusted_request_count = dict(self.source_kind_counts)[
            GuidedProviderExecutionTargetSourceKind
            .TRUSTED_EVIDENCE_REQUEST_CONTRACT
        ]
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_execution_target_bindings": {
                "exact_private_context_bound": True,
                "host_attestation_fresh": self.host_attestation_fresh,
                "accepted_scope_item_count": self.accepted_scope_item_count,
                "accepted_max_request_count": self.accepted_max_request_count,
                "preflight_item_count": self.preflight_item_count,
                "target_item_count": self.target_item_count,
                "binding_item_count": self.binding_item_count,
                "bound_target_count": self.bound_target_count,
                "bound_source_line_reference_count": (
                    self.bound_source_line_reference_count
                ),
                "max_request_count": self.max_request_count,
                "items": [_safe_item(item) for item in self.items],
                "data_categories": [
                    item.value for item in self.data_categories
                ],
                "source_kind_counts": {
                    item.value: count
                    for item, count in self.source_kind_counts
                },
                "private_intent_target_count": private_intent_count,
                "trusted_evidence_request_target_count": trusted_request_count,
                "policy_registry_revision_binding_count": trusted_request_count,
                "snapshot_binding_count": trusted_request_count,
                "store_revision_binding_count": trusted_request_count,
                "evidence_revision_binding_count": trusted_request_count,
                "source_line_coverage_complete": True,
                "exact_private_preimages_required_for_reassessment": True,
                "caller_supplied_target_digest_accepted": False,
                "target_fingerprints_exposed": False,
                "raw_target_preimages_retained": False,
                "all_execution_targets_bound": True,
                "eligible_for_execution_authorization_item_count": (
                    self.binding_item_count
                ),
                "eligible_for_execution_authorization_target_count": (
                    self.bound_target_count
                ),
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
                "execution_time_pricing_policy_retention_recheck_required": True,
                "credential_availability_recheck_required": True,
                "explicit_execution_authorization_required_before_any_call": True,
                "provider_scope_authorized": False,
                "provider_request_contracts_created_by_binding": False,
                "http_requests_created": False,
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
                "target_fingerprints_exposed": False,
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


def bind_guided_provider_execution_targets(
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
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderExecutionTargetBindings:
    """Bind exact typed preimages without creating or authorizing requests."""

    bound_at = _utc_datetime(evaluation_at, "evaluation_at")
    targets_review = assess_guided_provider_execution_targets(
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
        evaluation_at=bound_at,
    )
    if (
        targets_review.status
        is not GuidedProviderExecutionTargetsStatus
        .NEEDS_PRIVATE_EXECUTION_TARGETS
    ):
        raise ValueError("Target binding requires exact Phase 5.15 requirements")
    items = _derive_binding_items(
        brief,
        evidence_plan,
        preflight,
        targets,
        preimages,
        evaluation_at=bound_at,
    )
    return GuidedProviderExecutionTargetBindings(
        items=items,
        _bound_at=bound_at,
        _context_fingerprint=_bindings_context_fingerprint(
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
            targets_review,
            items,
            bound_at,
        ),
        _token=_BINDINGS_TOKEN,
    )


def assess_guided_provider_execution_target_bindings(
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
) -> GuidedProviderExecutionTargetBindingsReview:
    """Revalidate upstream state, preimages, revisions, and freshness."""

    if type(bindings) is not GuidedProviderExecutionTargetBindings:
        raise TypeError(
            "bindings must be exact GuidedProviderExecutionTargetBindings"
        )
    current_evaluation = _utc_datetime(evaluation_at, "evaluation_at")
    if current_evaluation < bindings._bound_at:
        raise ValueError("evaluation_at cannot precede target binding")

    captured_targets_review = assess_guided_provider_execution_targets(
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
        evaluation_at=bindings._bound_at,
    )
    expected_items = _derive_binding_items(
        brief,
        evidence_plan,
        preflight,
        targets,
        preimages,
        evaluation_at=bindings._bound_at,
    )
    expected_fingerprint = _bindings_context_fingerprint(
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
        captured_targets_review,
        expected_items,
        bindings._bound_at,
    )
    if (
        bindings.items != expected_items
        or bindings._context_fingerprint != expected_fingerprint
    ):
        raise ValueError(
            "Target bindings do not match the current private context"
        )

    current_targets_review = assess_guided_provider_execution_targets(
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
        evaluation_at=current_evaluation,
    )
    current_items = _derive_binding_items(
        brief,
        evidence_plan,
        preflight,
        targets,
        preimages,
        evaluation_at=current_evaluation,
    )
    if current_items != bindings.items:
        raise ValueError(
            "Execution target preimages or revisions are no longer current"
        )

    source_counts = Counter(
        record.source_kind
        for item in bindings.items
        for record in item._records
    )
    needs_verification = tuple(
        dict.fromkeys(
            (
                *current_targets_review.needs_verification,
                "provider_execution_target_bindings",
            )
        )
    )
    return GuidedProviderExecutionTargetBindingsReview(
        status=(
            GuidedProviderExecutionTargetBindingsStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW
        ),
        next_action=_NEXT_ACTION,
        accepted_scope_item_count=(
            current_targets_review.accepted_scope_item_count
        ),
        accepted_max_request_count=(
            current_targets_review.accepted_max_request_count
        ),
        preflight_item_count=current_targets_review.preflight_item_count,
        target_item_count=current_targets_review.target_item_count,
        binding_item_count=len(bindings.items),
        bound_target_count=sum(
            item.bound_target_count for item in bindings.items
        ),
        bound_source_line_reference_count=sum(
            len(record.source_line_indexes)
            for item in bindings.items
            for record in item._records
        ),
        max_request_count=current_targets_review.max_request_count,
        items=bindings.items,
        source_kind_counts=_complete_source_kind_counts(source_counts),
        data_categories=current_targets_review.data_categories,
        estimated_first_paid_tier_google_cost_usd_micros=(
            current_targets_review
            .estimated_first_paid_tier_google_cost_usd_micros
        ),
        serpapi_plan_credit_cap=(
            current_targets_review.serpapi_plan_credit_cap
        ),
        all_provider_costs_have_currency_list_rate_estimates=(
            current_targets_review
            .all_provider_costs_have_currency_list_rate_estimates
        ),
        host_attestation_fresh=current_targets_review.host_attestation_fresh,
        tentative_fields=current_targets_review.tentative_fields,
        needs_verification=needs_verification,
        _token=_REVIEW_TOKEN,
    )


def _derive_binding_items(
    brief: TripBriefDraft,
    evidence_plan: GuidedEvidenceRequirementPlan,
    preflight: GuidedProviderPreflight,
    targets: GuidedProviderExecutionTargets,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    *,
    evaluation_at: datetime,
) -> tuple[GuidedProviderExecutionTargetBindingItem, ...]:
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
    grouped: dict[
        GuidedEvidenceTopic,
        list[GuidedProviderExecutionTargetPreimage],
    ] = {}
    for preimage in preimages:
        grouped.setdefault(preimage.topic, []).append(preimage)
    if set(grouped) != set(requirements):
        raise ValueError("Target preimages must cover every exact target topic")

    result: list[GuidedProviderExecutionTargetBindingItem] = []
    for topic, requirement in requirements.items():
        topic_preimages = tuple(grouped[topic])
        if not 1 <= len(topic_preimages) <= requirement.max_request_count:
            raise ValueError("Target preimage count exceeds its accepted cap")
        required_lines = {
            declaration.source_line_index
            for declaration in evidence_plan.declarations
            if topic in declaration.topics
        }
        covered_lines = Counter(
            index
            for preimage in topic_preimages
            for index in preimage.source_line_indexes
        )
        if (
            set(covered_lines) != required_lines
            or any(count != 1 for count in covered_lines.values())
        ):
            raise ValueError(
                "Target preimages must cover each required source line once"
            )
        records = tuple(
            sorted(
                (
                    _record_from_preimage(
                        brief,
                        requirement,
                        preflight_items[topic],
                        preimage,
                        evaluation_at=evaluation_at,
                    )
                    for preimage in topic_preimages
                ),
                key=_record_sort_key,
            )
        )
        result.append(
            GuidedProviderExecutionTargetBindingItem(
                requirement=requirement,
                _records=records,
                _token=_ITEM_TOKEN,
            )
        )
    return tuple(sorted(result, key=_binding_item_sort_key))


def _record_from_preimage(
    brief: TripBriefDraft,
    requirement: GuidedProviderExecutionTargetItem,
    preflight_item: GuidedProviderPreflightItem,
    preimage: GuidedProviderExecutionTargetPreimage,
    *,
    evaluation_at: datetime,
) -> _GuidedProviderExecutionTargetRecord:
    expected_type = _TARGET_TYPE_BY_TARGET_KIND[requirement.target_kind]
    if type(preimage.target) is not expected_type:
        raise TypeError("Target preimage type differs from its exact requirement")
    source_kind = _SOURCE_KIND_BY_TARGET_KIND[requirement.target_kind]
    target = preimage.target
    revisions: tuple[str | None, str | None, str | None, str | None]

    if type(target) is PlaceIdentityIntent:
        canonical_target = _place_identity_intent_preimage(target)
        revisions = (None, None, None, None)
    elif type(target) is LodgingDiscoveryRequest:
        _validate_hotel_dates(brief, target)
        canonical_target = _hotel_search_preimage(target)
        revisions = (None, None, None, None)
    elif type(target) is GooglePlaceDetailsRequest:
        if target.kind is not PlaceDetailsKind.CURRENT_HOURS:
            raise ValueError(
                "Current-hours targets require the exact current-hours contract"
            )
        _validate_target_dates(
            brief,
            target.target_start,
            target.target_end,
            "current-hours",
        )
        _validate_trusted_google_request(
            target.snapshot,
            (target.endpoint,),
            target.provider_request.policy_id,
            evaluation_at=evaluation_at,
        )
        canonical_target = {
            "request": target.to_binding_dict(),
            "snapshot": target.snapshot.to_dict(),
        }
        revisions = _snapshot_revisions(target.snapshot)
    elif type(target) is GoogleRouteRequest:
        _validate_route_date(brief, target.departure_at)
        _validate_trusted_google_request(
            target.snapshot,
            (target.origin, target.destination),
            target.provider_request.policy_id,
            evaluation_at=evaluation_at,
        )
        canonical_target = {
            "request": target.to_binding_dict(),
            "snapshot": target.snapshot.to_dict(),
        }
        revisions = _snapshot_revisions(target.snapshot)
    else:  # pragma: no cover - guarded by the exact type mapping above
        raise TypeError("Unsupported target preimage")

    fingerprint_payload = {
        "contract_version": (
            GUIDED_PROVIDER_EXECUTION_TARGET_BINDINGS_VERSION
        ),
        "domain": "guided-provider-execution-target-preimage",
        "topic": requirement.topic.value,
        "capability": requirement.capability.value,
        "request_profile": requirement.request_profile.value,
        "target_kind": requirement.target_kind.value,
        "dependency": requirement.dependency.value,
        "source_kind": source_kind.value,
        "preflight_item": _private_context_value(preflight_item),
        "target": canonical_target,
    }
    return _GuidedProviderExecutionTargetRecord(
        source_kind=source_kind,
        source_line_indexes=preimage.source_line_indexes,
        target_fingerprint=_sha256(fingerprint_payload),
        policy_registry_revision=revisions[0],
        snapshot_id=revisions[1],
        store_revision=revisions[2],
        evidence_revision=revisions[3],
        _token=_RECORD_TOKEN,
    )


def _bindings_context_fingerprint(
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
    targets_review: GuidedProviderExecutionTargetsReview,
    items: tuple[GuidedProviderExecutionTargetBindingItem, ...],
    bound_at: datetime,
) -> str:
    canonical = {
        "contract_version": GUIDED_PROVIDER_EXECUTION_TARGET_BINDINGS_VERSION,
        "targets_contract_version": GUIDED_PROVIDER_EXECUTION_TARGETS_VERSION,
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
        "provider_execution_targets_review": _private_context_value(
            targets_review
        ),
        "binding_items": _private_context_value(items),
        "bound_at": _private_context_value(bound_at),
    }
    return _sha256(canonical)


def _place_identity_intent_preimage(
    target: PlaceIdentityIntent,
) -> dict[str, object]:
    return {
        "location_id": target.location_id,
        "text_query": target.text_query,
        "expected_name": target.expected_name,
        "region_code": target.region_code,
        "language_code": target.language_code,
        "expected_locality": target.expected_locality,
        "expected_primary_types": list(target.expected_primary_types),
        "latitude": target.latitude,
        "longitude": target.longitude,
        "radius_m": target.radius_m,
        "intent_id": target.intent_id,
    }


def _hotel_search_preimage(
    target: LodgingDiscoveryRequest,
) -> dict[str, object]:
    return {
        "query": target.query,
        "check_in": target.check_in.isoformat(),
        "check_out": target.check_out.isoformat(),
        "adults": target.adults,
        "children": target.children,
        "rooms": target.rooms,
        "currency": target.currency,
        "currency_minor_unit": target.currency_minor_unit,
        "region": target.region,
        "language": target.language,
        "request_id": target.request_id,
    }


def _validate_hotel_dates(
    brief: TripBriefDraft,
    target: LodgingDiscoveryRequest,
) -> None:
    if (
        brief.dates.start is None
        or brief.dates.end is None
        or target.check_in < brief.dates.start
        or target.check_out > brief.dates.end
    ):
        raise ValueError("Hotel-search dates must stay within the exact trip span")


def _validate_target_dates(
    brief: TripBriefDraft,
    start: object,
    end: object,
    name: str,
) -> None:
    if (
        brief.dates.start is None
        or brief.dates.end is None
        or start is None
        or end is None
        or start < brief.dates.start
        or end > brief.dates.end
    ):
        raise ValueError(f"{name} dates must stay within the exact trip span")


def _validate_route_date(brief: TripBriefDraft, departure_at: str) -> None:
    try:
        parsed = datetime.fromisoformat(departure_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Route departure must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Route departure must include a UTC offset")
    if (
        brief.dates.start is None
        or brief.dates.end is None
        or not brief.dates.start <= parsed.date() <= brief.dates.end
    ):
        raise ValueError("Route departure must stay within the exact trip span")


def _validate_trusted_google_request(
    snapshot: object,
    endpoints: tuple[object, ...],
    policy_id: str,
    *,
    evaluation_at: datetime,
) -> None:
    if snapshot.evaluation_at > evaluation_at:
        raise ValueError("Trusted request snapshot cannot come from the future")
    if snapshot.purge_checked_at > evaluation_at:
        raise ValueError("Trusted request purge check cannot come from the future")
    if any(endpoint.valid_until <= evaluation_at for endpoint in endpoints):
        raise ValueError("Trusted request endpoint is no longer fresh")
    policy = snapshot.policies.policy(policy_id)
    if policy.contract_region != GOOGLE_MAPS_NON_EEA_POLICY_PROFILE:
        raise ValueError("Guided Google requests require the non-EEA policy")


def _snapshot_revisions(
    snapshot: object,
) -> tuple[str, str, str, str]:
    return (
        snapshot.policies.revision,
        snapshot.snapshot_id,
        snapshot.store_revision,
        snapshot.evidence_revision,
    )


def _safe_item(
    item: GuidedProviderExecutionTargetBindingItem,
) -> dict[str, object]:
    requirement = item.requirement
    source_counts = Counter(record.source_kind for record in item._records)
    return {
        "topic": requirement.topic.value,
        "capability": requirement.capability.value,
        "request_profile": requirement.request_profile.value,
        "target_kind": requirement.target_kind.value,
        "dependency": requirement.dependency.value,
        "max_request_count": requirement.max_request_count,
        "bound_target_count": item.bound_target_count,
        "bound_source_line_reference_count": sum(
            len(record.source_line_indexes) for record in item._records
        ),
        "source_kind_counts": {
            source_kind.value: source_counts[source_kind]
            for source_kind in sorted(
                GuidedProviderExecutionTargetSourceKind,
                key=lambda value: value.value,
            )
        },
        "target_bound": True,
        "source_line_coverage_complete": True,
        "eligible_for_execution_authorization_review": True,
    }


def _complete_source_kind_counts(
    counter: Counter[GuidedProviderExecutionTargetSourceKind],
) -> tuple[tuple[GuidedProviderExecutionTargetSourceKind, int], ...]:
    return tuple(
        (item, counter[item])
        for item in sorted(
            GuidedProviderExecutionTargetSourceKind,
            key=lambda value: value.value,
        )
    )


def _validate_source_kind_counts(
    values: tuple[tuple[GuidedProviderExecutionTargetSourceKind, int], ...],
    expected_total: int,
) -> None:
    if (
        not isinstance(values, tuple)
        or len(values) != len(GuidedProviderExecutionTargetSourceKind)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or type(item[0]) is not GuidedProviderExecutionTargetSourceKind
            or type(item[1]) is not int
            or not 0 <= item[1] <= _MAX_REQUESTS
            for item in values
        )
        or tuple(sorted(values, key=lambda item: item[0].value)) != values
        or {item[0] for item in values}
        != set(GuidedProviderExecutionTargetSourceKind)
        or sum(item[1] for item in values) != expected_total
    ):
        raise ValueError("Target binding review source kind counts are invalid")


def _record_sort_key(
    item: _GuidedProviderExecutionTargetRecord,
) -> tuple[object, ...]:
    return (
        item.source_kind.value,
        item.source_line_indexes,
        item.target_fingerprint,
        item.policy_registry_revision or "",
        item.snapshot_id or "",
        item.store_revision or "",
        item.evidence_revision or "",
    )


def _binding_item_sort_key(
    item: GuidedProviderExecutionTargetBindingItem,
) -> tuple[object, ...]:
    requirement = item.requirement
    return (
        requirement.topic.value,
        requirement.capability.value,
        requirement.request_profile.value,
        requirement.target_kind.value,
        requirement.dependency.value,
        requirement.max_request_count,
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
    "GUIDED_PROVIDER_EXECUTION_TARGET_BINDINGS_VERSION",
    "GuidedProviderExecutionTargetBindingItem",
    "GuidedProviderExecutionTargetBindings",
    "GuidedProviderExecutionTargetBindingsReview",
    "GuidedProviderExecutionTargetBindingsStatus",
    "GuidedProviderExecutionTargetPreimage",
    "GuidedProviderExecutionTargetSourceKind",
    "assess_guided_provider_execution_target_bindings",
    "bind_guided_provider_execution_targets",
]
