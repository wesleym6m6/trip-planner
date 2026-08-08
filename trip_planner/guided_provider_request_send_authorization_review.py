"""Prepare an exact private review before any provider request can be sent.

Phase 5.23 consumes a fresh Phase 5.22 non-sendable contract materialization
and the same exact typed preimages.  It exposes a deliberately ephemeral
private review payload containing the exact non-identifier values a provider
would receive, while keeping provider identifiers redacted and binding them in
the review fingerprint.

This module only prepares and reassesses that review.  It does not capture a
response, choose transport endpoints or HTTP methods, bind or read credentials,
construct an HTTP request, grant send or execution authority, call a provider,
persist data, schedule, render, deploy, confirm, or mutate canonical state.
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
from .guided_evidence_plan import GuidedEvidenceRequirementPlan
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
)
from .guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetBindings,
    GuidedProviderExecutionTargetPreimage,
)
from .guided_provider_execution_targets import GuidedProviderExecutionTargets
from .guided_provider_execution_time_recheck import (
    GuidedProviderExecutionTimeRecheck,
)
from .guided_provider_preflight import GuidedProviderPreflight
from .guided_provider_preflight_response import GuidedProviderPreflightResponse
from .guided_provider_request_contract_materialization import (
    GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION,
    GuidedProviderRequestContract,
    GuidedProviderRequestContractMaterialization,
    GuidedProviderRequestContractMaterializationReview,
    GuidedProviderRequestContractMaterializationStatus,
    assess_guided_provider_request_contract_materialization,
)
from .guided_provider_request_materialization_response import (
    GuidedProviderRequestMaterializationResponse,
)
from .guided_provider_request_materialization_review import (
    GuidedProviderRequestMaterializationReview,
)
from .guided_provider_scope import GuidedProviderScopeProposal
from .guided_provider_scope_response import GuidedProviderScopeResponse
from .guided_refinement import GuidedRefinementCandidate, GuidedRefinementResponse
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW_VERSION = (
    "guided-provider-request-send-authorization-review/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_NEXT_ACTION = "capture_private_provider_request_send_authorization_response"
_RESPONSE_OPTIONS = ("accept_send", "request_smaller", "cancel")
_REVIEW_TOKEN = object()


class GuidedProviderRequestSendAuthorizationReviewStatus(str, Enum):
    """A fresh materialized request bundle requires a typed response."""

    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestSendAuthorizationReview:
    """Token-gated exact review of materialized non-sendable contracts."""

    status: GuidedProviderRequestSendAuthorizationReviewStatus
    next_action: str
    _materialization_review: GuidedProviderRequestContractMaterializationReview = (
        field(repr=False)
    )
    _prepared_at: datetime = field(repr=False)
    _expires_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    contract_version: str = (
        GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW_VERSION
    )
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Send-authorization reviews require trusted preparation")
        if (
            type(self.status)
            is not GuidedProviderRequestSendAuthorizationReviewStatus
            or self.status
            is not GuidedProviderRequestSendAuthorizationReviewStatus.REVIEW_REQUIRED
            or self.next_action != _NEXT_ACTION
        ):
            raise ValueError("Send-authorization review status/action differs")
        if (
            type(self._materialization_review)
            is not GuidedProviderRequestContractMaterializationReview
            or self._materialization_review.status
            is not GuidedProviderRequestContractMaterializationStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW
            or self._materialization_review.next_action
            != "prepare_private_provider_request_send_authorization_review"
        ):
            raise ValueError("Send review requires a ready contract materialization")
        prepared = _utc_datetime(self._prepared_at, "prepared_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not prepared < expires <= prepared + timedelta(minutes=5):
            raise ValueError("Send-authorization review expiry is invalid")
        _digest(self._context_fingerprint, "context_fingerprint")
        if (
            self.contract_version
            != GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW_VERSION
        ):
            raise ValueError("Send-authorization review version differs")
        object.__setattr__(self, "_prepared_at", prepared)
        object.__setattr__(self, "_expires_at", expires)

    @property
    def contract_count(self) -> int:
        return self._materialization_review.contract_count

    @property
    def accepted_scope_item_count(self) -> int:
        return self._materialization_review.accepted_scope_item_count

    @property
    def accepted_max_request_count(self) -> int:
        return self._materialization_review.accepted_max_request_count

    @property
    def bound_request_count(self) -> int:
        return self._materialization_review.bound_request_count

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    @property
    def tentative_fields(self) -> tuple[str, ...]:
        return self._materialization_review.tentative_fields

    @property
    def needs_verification(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self._materialization_review.needs_verification,
                    "provider_request_send_authorization_review",
                )
            )
        )

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestSendAuthorizationReview("
            f"status={self.status.value!r}, "
            f"contract_count={self.contract_count!r}, "
            f"next_action={self.next_action!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return safe aggregates and shapes, never exact private values."""

        source = self._materialization_review
        contracts = source._contracts
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": True,
            "requires_user_review": True,
            "requires_user_decision": True,
            "response_options": list(_RESPONSE_OPTIONS),
            "provider_request_send_authorization_review": {
                "exact_request_contract_materialization_bound": True,
                "same_exact_target_preimages_revalidated": True,
                "short_lived_execution_recheck_preserved": True,
                "must_reassess_before_response_capture": True,
                "exact_review_times_exposed": False,
                "accepted_scope_item_count": source.accepted_scope_item_count,
                "accepted_max_request_count": source.accepted_max_request_count,
                "materialized_request_contract_count": source.contract_count,
                "reviewed_request_contract_count": source.contract_count,
                "eligible_for_send_response_count": source.contract_count,
                "bound_request_count": source.bound_request_count,
                "max_request_count": source.max_request_count,
                "bound_source_line_reference_count": (
                    source.bound_source_line_reference_count
                ),
                "bound_google_request_count": source.bound_google_request_count,
                "bound_serpapi_request_count": source.bound_serpapi_request_count,
                "bound_request_capability_counts": {
                    capability.value: count
                    for capability, count in source.bound_request_capability_counts
                },
                "source_state_counts": {
                    "user_stated": source.user_stated_source_line_count,
                    "tentative": source.tentative_source_line_count,
                    "ai_candidate": source.ai_candidate_source_line_count,
                },
                "all_source_lines_require_verification": True,
                "source_values_are_authoritative": False,
                "data_categories": [item.value for item in source.data_categories],
                "estimated_bound_first_paid_tier_google_cost_usd_micros": (
                    source.estimated_bound_first_paid_tier_google_cost_usd_micros
                ),
                "accepted_max_first_paid_tier_google_cost_usd_micros": (
                    source.accepted_max_first_paid_tier_google_cost_usd_micros
                ),
                "serpapi_bound_plan_credit_count": (
                    source.serpapi_bound_plan_credit_count
                ),
                "serpapi_plan_credit_cap": source.serpapi_plan_credit_cap,
                "all_bound_provider_costs_have_currency_list_rate_estimates": (
                    source.serpapi_bound_plan_credit_count == 0
                ),
                "host_attestation_fresh": source.host_attestation_fresh,
                "pricing_policy_retention_recheck_preserved": True,
                "billing_region_recheck_preserved": True,
                "credential_availability_recheck_preserved": True,
                "serpapi_plan_state_recheck_preserved": True,
                "items": [_safe_review_item(item) for item in contracts],
                "private_review_payload_available": True,
                "exact_private_values_in_safe_output": False,
                "provider_identifier_values_exposed": False,
                "source_binding_fingerprints_exposed": False,
                "context_fingerprints_exposed": False,
                "raw_target_preimages_retained": False,
                "provider_request_contracts_are_executable": False,
                "provider_request_contracts_are_sendable": False,
                "transport_endpoints_selected": False,
                "http_methods_selected": False,
                "credential_slots_bound": False,
                "credential_values_included": False,
                "http_request_count_created_by_review": 0,
                "send_response_captured": False,
                "send_authorization_active": False,
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
                "materialized_private_contracts_read": True,
                "private_review_payload_generated_by_safe_view": False,
                "environment_read": False,
                "vault_accessed": False,
                "credentials_accessed": False,
                "transport_selected": False,
                "http_requests_created": False,
                "send_authority_granted": False,
                "provider_calls": False,
                "writes_to_trip": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }

    def to_ephemeral_private_review_payload(self) -> dict[str, Any]:
        """Return exact non-identifier values for direct current-user review."""

        source = self._materialization_review
        return {
            "contract_version": self.contract_version,
            "payload_handling": "private_ephemeral_direct_human_review_only",
            "response_options": list(_RESPONSE_OPTIONS),
            "request_contracts": [
                _private_review_item(item) for item in source._contracts
            ],
            "materialized_request_contract_count": source.contract_count,
            "bound_request_count": source.bound_request_count,
            "accepted_max_request_count": source.accepted_max_request_count,
            "exact_non_identifier_provider_transmitted_values_included": True,
            "exact_local_result_binding_values_included": True,
            "provider_identifier_values_exposed": False,
            "source_binding_fingerprints_exposed": False,
            "context_fingerprints_exposed": False,
            "credentials_exposed": False,
            "transport_endpoints_selected": False,
            "http_methods_selected": False,
            "http_requests_created": False,
            "send_response_captured": False,
            "send_authorization_active": False,
            "execution_authority_active": False,
            "provider_calls_permitted": False,
            "decision_state": DecisionState.CANDIDATE.value,
            "evidence_state": EvidenceState.UNVERIFIED.value,
            "supports_authoritative_use": False,
        }


def prepare_guided_provider_request_send_authorization_review(
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
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    materialization: GuidedProviderRequestContractMaterialization,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestSendAuthorizationReview:
    """Prepare one exact private review from a fresh contract bundle."""

    prepared_at = _utc_datetime(evaluation_at, "evaluation_at")
    current_materialization_review = (
        assess_guided_provider_request_contract_materialization(
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
            materialization_review,
            materialization_response,
            materialization,
            preimages=preimages,
            evaluation_at=prepared_at,
        )
    )
    _require_ready_materialization(current_materialization_review)
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
        materialization_review,
        materialization_response,
        materialization,
        current_materialization_review,
        prepared_at,
        materialization._expires_at,
    )


def assess_guided_provider_request_send_authorization_review(
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
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    materialization: GuidedProviderRequestContractMaterialization,
    review: GuidedProviderRequestSendAuthorizationReview,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestSendAuthorizationReview:
    """Rebuild the exact review and revalidate the current private chain."""

    if type(review) is not GuidedProviderRequestSendAuthorizationReview:
        raise TypeError("review must be an exact send-authorization review")
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < review._prepared_at:
        raise ValueError("evaluation_at cannot precede send-review preparation")

    captured_materialization_review = (
        assess_guided_provider_request_contract_materialization(
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
            materialization_review,
            materialization_response,
            materialization,
            preimages=preimages,
            evaluation_at=review._prepared_at,
        )
    )
    _require_ready_materialization(captured_materialization_review)
    expected = _new_review(
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
        materialization_review,
        materialization_response,
        materialization,
        captured_materialization_review,
        review._prepared_at,
        review._expires_at,
    )
    if review != expected:
        raise ValueError("Send-authorization review differs from exact context")
    if evaluated_at >= review._expires_at:
        raise ValueError("Send-authorization review is no longer current")

    current_materialization_review = (
        assess_guided_provider_request_contract_materialization(
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
            materialization_review,
            materialization_response,
            materialization,
            preimages=preimages,
            evaluation_at=evaluated_at,
        )
    )
    _require_ready_materialization(current_materialization_review)
    if current_materialization_review != review._materialization_review:
        raise ValueError("Materialized request contracts are no longer current")
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
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    materialization: GuidedProviderRequestContractMaterialization,
    current_materialization_review: (
        GuidedProviderRequestContractMaterializationReview
    ),
    prepared_at: datetime,
    expires_at: datetime,
) -> GuidedProviderRequestSendAuthorizationReview:
    return GuidedProviderRequestSendAuthorizationReview(
        status=GuidedProviderRequestSendAuthorizationReviewStatus.REVIEW_REQUIRED,
        next_action=_NEXT_ACTION,
        _materialization_review=current_materialization_review,
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
            materialization_review,
            materialization_response,
            materialization,
            current_materialization_review,
            prepared_at,
            expires_at,
        ),
        _token=_REVIEW_TOKEN,
    )


def _require_ready_materialization(
    review: GuidedProviderRequestContractMaterializationReview,
) -> None:
    if (
        review.status
        is not GuidedProviderRequestContractMaterializationStatus
        .READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW
    ):
        raise ValueError("Send review requires a ready contract materialization")


def _safe_review_item(
    contract: GuidedProviderRequestContract,
) -> dict[str, object]:
    return {
        **contract.to_safe_dict(),
        "eligible_for_send_response_review": True,
        "exact_non_identifier_provider_transmitted_values_available_in_ephemeral_review": True,
        "exact_local_result_binding_values_available_in_ephemeral_review": True,
        "provider_identifier_values_exposed": False,
        "send_response_captured": False,
        "send_authorization_active": False,
    }


def _private_review_item(
    contract: GuidedProviderRequestContract,
) -> dict[str, object]:
    return {
        **_safe_review_item(contract),
        "provider_transmitted_values": {
            name: _human_value(value)
            for name, value in contract._provider_transmitted_values
        },
        "local_result_binding": {
            name: _human_value(value)
            for name, value in contract._local_result_binding
        },
        "redacted_bound_provider_identifier_fields": [
            name for name, _ in contract._provider_identifier_values
        ],
        "exact_non_identifier_provider_transmitted_values_included": True,
        "exact_local_result_binding_values_included": True,
        "provider_identifiers_bound_but_redacted": bool(
            contract._provider_identifier_values
        ),
        "payload_handling": "private_ephemeral_direct_human_review_only",
    }


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
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    materialization: GuidedProviderRequestContractMaterialization,
    current_materialization_review: (
        GuidedProviderRequestContractMaterializationReview
    ),
    prepared_at: datetime,
    expires_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": (
                GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW_VERSION
            ),
            "materialization_contract_version": (
                GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION
            ),
            "domain": "guided-provider-request-send-authorization-review",
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
            "provider_execution_authorization_review": _private_context_value(
                authorization_review
            ),
            "provider_execution_authorization_response": (
                _private_context_value(authorization_response)
            ),
            "provider_execution_time_recheck": _private_context_value(
                execution_time_recheck
            ),
            "provider_request_materialization_review": _private_context_value(
                materialization_review
            ),
            "provider_request_materialization_response": _private_context_value(
                materialization_response
            ),
            "provider_request_contract_materialization": _private_context_value(
                materialization
            ),
            "provider_request_contract_materialization_review": (
                _private_context_value(current_materialization_review)
            ),
            "prepared_at": _private_context_value(prepared_at),
            "expires_at": _private_context_value(expires_at),
        }
    )


def _human_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_human_value(item) for item in value]
    return value


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
    "GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW_VERSION",
    "GuidedProviderRequestSendAuthorizationReview",
    "GuidedProviderRequestSendAuthorizationReviewStatus",
    "assess_guided_provider_request_send_authorization_review",
    "prepare_guided_provider_request_send_authorization_review",
]
