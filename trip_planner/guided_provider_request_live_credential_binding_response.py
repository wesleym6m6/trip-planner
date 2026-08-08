"""Capture one exact response to a fresh live credential-binding review.

Phase 5.29 accepts only an unambiguous typed accept-live-binding,
request-smaller, or cancel choice for the exact current Phase 5.28 review.
Capture and assessment revalidate the complete private chain, the same typed
target preimages, the same transport-bound contracts, and the same public
slot-level credential availability attestations under inherited freshness.

Acceptance permits only preparation of a separate ephemeral credential-value
binding gate.  It does not read an environment or vault, access or bind a
credential value, expand provider identifiers, construct an HTTP request,
open a network connection, call a provider, reserve spend, persist data,
schedule, render, deploy, confirm, or mutate canonical state.  Reduction and
cancellation also grant no authority.
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
    GuidedProviderRequestContractMaterialization,
)
from .guided_provider_request_credential_binding_response import (
    GuidedProviderRequestCredentialBindingResponse,
)
from .guided_provider_request_credential_binding_review import (
    GuidedProviderRequestCredentialBindingReview,
)
from .guided_provider_request_live_credential_binding_review import (
    GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION,
    GuidedProviderRequestCredentialAvailabilityAttestation,
    GuidedProviderRequestLiveCredentialBindingReview,
    GuidedProviderRequestLiveCredentialBindingReviewStatus,
    assess_guided_provider_request_live_credential_binding_review,
)
from .guided_provider_request_materialization_response import (
    GuidedProviderRequestMaterializationResponse,
)
from .guided_provider_request_materialization_review import (
    GuidedProviderRequestMaterializationReview,
)
from .guided_provider_request_send_authorization_response import (
    GuidedProviderRequestSendAuthorizationResponse,
)
from .guided_provider_request_send_authorization_review import (
    GuidedProviderRequestSendAuthorizationReview,
)
from .guided_provider_request_send_preparation import (
    GuidedProviderRequestSendPreparation,
    GuidedProviderRequestTransportProfile,
)
from .guided_provider_scope import GuidedProviderScopeProposal
from .guided_provider_scope_response import GuidedProviderScopeResponse
from .guided_refinement import GuidedRefinementCandidate, GuidedRefinementResponse
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_RESPONSE_VERSION = (
    "guided-provider-request-live-credential-binding-response/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ACCEPT_ACTION = (
    "prepare_private_provider_request_ephemeral_credential_value_binding_gate"
)
_REFINE_ACTION = "refine_private_provider_execution_targets"
_CANCEL_ACTION = "continue_private_evidence_review"
_RESPONSE_TOKEN = object()
_REVIEW_TOKEN = object()


class GuidedProviderRequestLiveCredentialBindingResponseKind(str, Enum):
    """One exact choice from the visible Phase 5.28 private review."""

    ACCEPT_LIVE_CREDENTIAL_BINDING = "accept_live_credential_binding"
    REQUEST_SMALLER = "request_smaller"
    CANCEL = "cancel"


class GuidedProviderRequestLiveCredentialBindingResponseStatus(str, Enum):
    """Non-executable handoff after one exact live-binding response."""

    READY_FOR_PRIVATE_PROVIDER_REQUEST_EPHEMERAL_CREDENTIAL_VALUE_BINDING_GATE = (
        "ready_for_private_provider_request_ephemeral_credential_value_binding_gate"
    )
    READY_FOR_PRIVATE_PROVIDER_EXECUTION_TARGET_REFINEMENT = (
        "ready_for_private_provider_execution_target_refinement"
    )
    PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_CANCELLED = (
        "provider_request_live_credential_binding_cancelled"
    )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestLiveCredentialBindingResponse:
    """Typed choice bound to one exact fresh process-local live review."""

    kind: GuidedProviderRequestLiveCredentialBindingResponseKind
    _captured_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESPONSE_TOKEN:
            raise ValueError(
                "Live credential-binding responses require trusted capture"
            )
        if (
            type(self.kind)
            is not GuidedProviderRequestLiveCredentialBindingResponseKind
        ):
            raise TypeError("Live credential-binding response kind must be exact")
        captured_at = _utc_datetime(self._captured_at, "captured_at")
        _digest(self._context_fingerprint, "context_fingerprint")
        object.__setattr__(self, "_captured_at", captured_at)

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestLiveCredentialBindingResponse("
            f"kind={self.kind.value!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestLiveCredentialBindingResponseReview:
    """Aggregate safe handoff with no credential-value or send authority."""

    status: GuidedProviderRequestLiveCredentialBindingResponseStatus
    response_kind: GuidedProviderRequestLiveCredentialBindingResponseKind
    next_action: str
    _live_credential_binding_review: (
        GuidedProviderRequestLiveCredentialBindingReview
    ) = field(repr=False)
    contract_version: str = (
        GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_RESPONSE_VERSION
    )
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Live credential-binding response reviews require the assessor"
            )
        if (
            type(self.status)
            is not GuidedProviderRequestLiveCredentialBindingResponseStatus
            or type(self.response_kind)
            is not GuidedProviderRequestLiveCredentialBindingResponseKind
        ):
            raise TypeError(
                "Live credential-binding response enums must be exact"
            )
        expected_status, expected_action = _response_branch(self.response_kind)
        if self.status is not expected_status or self.next_action != expected_action:
            raise ValueError(
                "Live credential-binding response status/action differs from kind"
            )
        _require_review_ready(self._live_credential_binding_review)
        if (
            self.contract_version
            != GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_RESPONSE_VERSION
        ):
            raise ValueError(
                "Live credential-binding response version differs"
            )

    @property
    def accepted_scope_item_count(self) -> int:
        return self._live_credential_binding_review.accepted_scope_item_count

    @property
    def accepted_max_request_count(self) -> int:
        return self._live_credential_binding_review.accepted_max_request_count

    @property
    def contract_count(self) -> int:
        return self._live_credential_binding_review.contract_count

    @property
    def binding_count(self) -> int:
        return self._live_credential_binding_review.binding_count

    @property
    def availability_attestations(
        self,
    ) -> tuple[GuidedProviderRequestCredentialAvailabilityAttestation, ...]:
        return self._live_credential_binding_review.availability_attestations

    @property
    def transport_profile_counts(
        self,
    ) -> tuple[tuple[GuidedProviderRequestTransportProfile, int], ...]:
        return (
            self._live_credential_binding_review
            ._credential_binding_response_review.transport_profile_counts
        )

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    @property
    def tentative_fields(self) -> tuple[str, ...]:
        return self._live_credential_binding_review.tentative_fields

    @property
    def needs_verification(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self._live_credential_binding_review.needs_verification,
                    "provider_request_live_credential_binding_response",
                )
            )
        )

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestLiveCredentialBindingResponseReview("
            f"status={self.status.value!r}, "
            f"response_kind={self.response_kind.value!r}, "
            f"binding_count={self.binding_count!r}, "
            f"next_action={self.next_action!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return safe aggregate branch semantics without credential values."""

        source = self._live_credential_binding_review
        handoff = dict(
            source.to_dict()[
                "provider_request_live_credential_binding_review"
            ]
        )
        accepted = (
            self.response_kind
            is GuidedProviderRequestLiveCredentialBindingResponseKind
            .ACCEPT_LIVE_CREDENTIAL_BINDING
        )
        smaller = (
            self.response_kind
            is GuidedProviderRequestLiveCredentialBindingResponseKind
            .REQUEST_SMALLER
        )
        cancelled = (
            self.response_kind
            is GuidedProviderRequestLiveCredentialBindingResponseKind.CANCEL
        )
        handoff.update(
            {
                "kind": self.response_kind.value,
                "live_credential_binding_response_captured": True,
                "requires_exact_typed_live_credential_binding_response": False,
                "accepted_exact_private_live_credential_binding_review": (
                    accepted
                ),
                "accepted_for_separate_ephemeral_credential_value_binding_gate": (
                    accepted
                ),
                "may_prepare_private_provider_request_ephemeral_credential_value_binding_gate": (
                    accepted
                ),
                "requested_smaller_provider_scope": smaller,
                "may_refine_private_provider_execution_targets": smaller,
                "live_credential_binding_path_cancelled": cancelled,
                "same_exact_target_preimages_revalidated": True,
                "same_exact_transport_bindings_revalidated": True,
                "same_exact_credential_availability_attestations_revalidated": (
                    True
                ),
                "short_lived_execution_recheck_preserved": True,
                "exact_response_times_exposed": False,
                "eligible_for_separate_ephemeral_credential_value_binding_gate_count": (
                    self.binding_count if accepted else 0
                ),
                "generic_continue_is_live_credential_binding_authority": False,
                "credential_availability_attestation_is_credential_value": (
                    False
                ),
                "credential_value_access_permitted": False,
                "credential_values_accessed": False,
                "credential_values_bound": False,
                "credential_values_included": False,
                "environment_read": False,
                "vault_accessed": False,
                "url_path_identifiers_expanded": False,
                "provider_request_contracts_are_executable": False,
                "provider_request_contracts_are_sendable": False,
                "executable_provider_request_contract_count_created_by_response": 0,
                "http_request_count_created_by_response": 0,
                "immediate_credential_binding_authority_granted": False,
                "credential_binding_authority_active": False,
                "immediate_send_authority_granted": False,
                "send_authority_active": False,
                "execution_authority_active": False,
                "provider_call_count_observed": 0,
                "provider_calls_permitted": False,
                "provider_scope_or_cap_modified_by_response": False,
                "targets_or_bindings_modified_by_response": False,
                "materialized_contracts_modified_by_response": False,
                "credential_availability_attestations_modified_by_response": (
                    False
                ),
                "evidence_requirements_preserved": True,
                "is_travel_ready": False,
                "decision_state": DecisionState.CANDIDATE.value,
                "evidence_state": EvidenceState.UNVERIFIED.value,
                "supports_authoritative_use": False,
            }
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_request_live_credential_binding_response": handoff,
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "live_credential_binding_review_read": True,
                "host_boolean_availability_attestation_read": True,
                "private_values_in_safe_output": False,
                "environment_read": False,
                "vault_accessed": False,
                "credentials_accessed": False,
                "credential_values_bound": False,
                "http_requests_created": False,
                "network_accessed": False,
                "send_authority_granted": False,
                "provider_calls": False,
                "writes_to_trip": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }


def capture_guided_provider_request_live_credential_binding_response(
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
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    send_preparation: GuidedProviderRequestSendPreparation,
    credential_binding_review: GuidedProviderRequestCredentialBindingReview,
    credential_binding_response: GuidedProviderRequestCredentialBindingResponse,
    live_credential_binding_review: (
        GuidedProviderRequestLiveCredentialBindingReview
    ),
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    kind: GuidedProviderRequestLiveCredentialBindingResponseKind,
    evaluation_at: datetime,
) -> GuidedProviderRequestLiveCredentialBindingResponse:
    """Capture one clear typed choice for the exact fresh private review."""

    if (
        type(kind)
        is not GuidedProviderRequestLiveCredentialBindingResponseKind
    ):
        raise TypeError(
            "kind must be an exact live credential-binding response kind"
        )
    captured_at = _utc_datetime(evaluation_at, "evaluation_at")
    assessed_review = assess_guided_provider_request_live_credential_binding_review(
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
        send_authorization_review,
        send_authorization_response,
        send_preparation,
        credential_binding_review,
        credential_binding_response,
        live_credential_binding_review,
        preimages=preimages,
        evaluation_at=captured_at,
    )
    _require_review_ready(assessed_review)
    return GuidedProviderRequestLiveCredentialBindingResponse(
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
            authorization_review,
            authorization_response,
            execution_time_recheck,
            materialization_review,
            materialization_response,
            materialization,
            send_authorization_review,
            send_authorization_response,
            send_preparation,
            credential_binding_review,
            credential_binding_response,
            assessed_review,
            kind,
            captured_at,
        ),
        _token=_RESPONSE_TOKEN,
    )


def assess_guided_provider_request_live_credential_binding_response(
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
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    send_preparation: GuidedProviderRequestSendPreparation,
    credential_binding_review: GuidedProviderRequestCredentialBindingReview,
    credential_binding_response: GuidedProviderRequestCredentialBindingResponse,
    live_credential_binding_review: (
        GuidedProviderRequestLiveCredentialBindingReview
    ),
    response: GuidedProviderRequestLiveCredentialBindingResponse,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestLiveCredentialBindingResponseReview:
    """Revalidate one exact choice and return a non-executable handoff."""

    if type(response) is not GuidedProviderRequestLiveCredentialBindingResponse:
        raise TypeError(
            "response must be an exact live credential-binding response"
        )
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < response._captured_at:
        raise ValueError("evaluation_at cannot precede response capture")

    captured_review = assess_guided_provider_request_live_credential_binding_review(
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
        send_authorization_review,
        send_authorization_response,
        send_preparation,
        credential_binding_review,
        credential_binding_response,
        live_credential_binding_review,
        preimages=preimages,
        evaluation_at=response._captured_at,
    )
    _require_review_ready(captured_review)
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
        authorization_review,
        authorization_response,
        execution_time_recheck,
        materialization_review,
        materialization_response,
        materialization,
        send_authorization_review,
        send_authorization_response,
        send_preparation,
        credential_binding_review,
        credential_binding_response,
        captured_review,
        response.kind,
        response._captured_at,
    )
    if response._context_fingerprint != expected_fingerprint:
        raise ValueError(
            "Live credential-binding response differs from exact context"
        )

    current_review = assess_guided_provider_request_live_credential_binding_review(
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
        send_authorization_review,
        send_authorization_response,
        send_preparation,
        credential_binding_review,
        credential_binding_response,
        live_credential_binding_review,
        preimages=preimages,
        evaluation_at=evaluated_at,
    )
    _require_review_ready(current_review)
    status, next_action = _response_branch(response.kind)
    return GuidedProviderRequestLiveCredentialBindingResponseReview(
        status=status,
        response_kind=response.kind,
        next_action=next_action,
        _live_credential_binding_review=current_review,
        _token=_REVIEW_TOKEN,
    )


def _require_review_ready(
    review: GuidedProviderRequestLiveCredentialBindingReview,
) -> None:
    if (
        type(review) is not GuidedProviderRequestLiveCredentialBindingReview
        or review.status
        is not GuidedProviderRequestLiveCredentialBindingReviewStatus
        .REVIEW_REQUIRED
        or review.next_action
        != "capture_private_provider_request_live_credential_binding_response"
        or not review.all_credentials_available
    ):
        raise ValueError(
            "Live credential-binding response requires the current review"
        )


def _response_branch(
    kind: GuidedProviderRequestLiveCredentialBindingResponseKind,
) -> tuple[GuidedProviderRequestLiveCredentialBindingResponseStatus, str]:
    return {
        GuidedProviderRequestLiveCredentialBindingResponseKind
        .ACCEPT_LIVE_CREDENTIAL_BINDING: (
            GuidedProviderRequestLiveCredentialBindingResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_EPHEMERAL_CREDENTIAL_VALUE_BINDING_GATE,
            _ACCEPT_ACTION,
        ),
        GuidedProviderRequestLiveCredentialBindingResponseKind.REQUEST_SMALLER: (
            GuidedProviderRequestLiveCredentialBindingResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_TARGET_REFINEMENT,
            _REFINE_ACTION,
        ),
        GuidedProviderRequestLiveCredentialBindingResponseKind.CANCEL: (
            GuidedProviderRequestLiveCredentialBindingResponseStatus
            .PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_CANCELLED,
            _CANCEL_ACTION,
        ),
    }[kind]


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
    authorization_review: GuidedProviderExecutionAuthorizationReview,
    authorization_response: GuidedProviderExecutionAuthorizationResponse,
    execution_time_recheck: GuidedProviderExecutionTimeRecheck,
    materialization_review: GuidedProviderRequestMaterializationReview,
    materialization_response: GuidedProviderRequestMaterializationResponse,
    materialization: GuidedProviderRequestContractMaterialization,
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    send_preparation: GuidedProviderRequestSendPreparation,
    credential_binding_review: GuidedProviderRequestCredentialBindingReview,
    credential_binding_response: GuidedProviderRequestCredentialBindingResponse,
    live_credential_binding_review: (
        GuidedProviderRequestLiveCredentialBindingReview
    ),
    response_kind: GuidedProviderRequestLiveCredentialBindingResponseKind,
    captured_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": (
                GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_RESPONSE_VERSION
            ),
            "live_credential_binding_review_contract_version": (
                GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION
            ),
            "domain": (
                "guided-provider-request-live-credential-binding-response"
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
            "provider_request_materialization_response": (
                _private_context_value(materialization_response)
            ),
            "provider_request_contract_materialization": (
                _private_context_value(materialization)
            ),
            "provider_request_send_authorization_review": (
                _private_context_value(send_authorization_review)
            ),
            "provider_request_send_authorization_response": (
                _private_context_value(send_authorization_response)
            ),
            "provider_request_send_preparation": (
                _private_context_value(send_preparation)
            ),
            "provider_request_credential_binding_review": (
                _private_context_value(credential_binding_review)
            ),
            "provider_request_credential_binding_response": (
                _private_context_value(credential_binding_response)
            ),
            "provider_request_live_credential_binding_review": (
                _private_context_value(live_credential_binding_review)
            ),
            "credential_availability_attestations": [
                _private_context_value(item)
                for item in (
                    live_credential_binding_review.availability_attestations
                )
            ],
            "response_kind": _private_context_value(response_kind),
            "captured_at": _private_context_value(captured_at),
        }
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
    "GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_RESPONSE_VERSION",
    "GuidedProviderRequestLiveCredentialBindingResponse",
    "GuidedProviderRequestLiveCredentialBindingResponseKind",
    "GuidedProviderRequestLiveCredentialBindingResponseReview",
    "GuidedProviderRequestLiveCredentialBindingResponseStatus",
    "assess_guided_provider_request_live_credential_binding_response",
    "capture_guided_provider_request_live_credential_binding_response",
]
