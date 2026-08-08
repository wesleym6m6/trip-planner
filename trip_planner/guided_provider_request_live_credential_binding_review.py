"""Prepare one exact live credential-binding review without reading a secret.

Phase 5.28 consumes only a fresh accepted Phase 5.27 response, the same exact
typed target preimages, and one trusted-host boolean-equivalent availability
attestation for every required credential slot.  Missing, duplicate, extra, or
unavailable slot attestations fail closed or remain blocked.

A review-ready result may only enter a separate exact response gate.  It does
not read an environment or vault, access or bind a credential value, expand a
provider identifier, construct an HTTP request, open a network connection,
call a provider, reserve spend, persist data, schedule, render, deploy,
confirm, or mutate canonical state.
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
from .guided_provider_preflight import (
    GuidedProviderCredentialStatus,
    GuidedProviderPreflight,
)
from .guided_provider_preflight_response import GuidedProviderPreflightResponse
from .guided_provider_request_contract_materialization import (
    GuidedProviderRequestContractMaterialization,
)
from .guided_provider_request_credential_binding_response import (
    GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_RESPONSE_VERSION,
    GuidedProviderRequestCredentialBindingResponse,
    GuidedProviderRequestCredentialBindingResponseKind,
    GuidedProviderRequestCredentialBindingResponseReview,
    GuidedProviderRequestCredentialBindingResponseStatus,
    assess_guided_provider_request_credential_binding_response,
)
from .guided_provider_request_credential_binding_review import (
    GuidedProviderRequestCredentialBindingReview,
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
    GuidedProviderRequestCredentialSlot,
    GuidedProviderRequestSendPreparation,
)
from .guided_provider_scope import GuidedProviderScopeProposal
from .guided_provider_scope_response import GuidedProviderScopeResponse
from .guided_refinement import GuidedRefinementCandidate, GuidedRefinementResponse
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION = (
    "guided-provider-request-live-credential-binding-review/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_CREDENTIAL_SLOTS = len(GuidedProviderRequestCredentialSlot)
_REVIEW_ACTION = (
    "capture_private_provider_request_live_credential_binding_response"
)
_BLOCKED_ACTION = (
    "refresh_private_provider_request_credential_availability_attestation"
)
_RESPONSE_OPTIONS = (
    "accept_live_credential_binding",
    "request_smaller",
    "cancel",
)
_REVIEW_TOKEN = object()


class GuidedProviderRequestLiveCredentialBindingReviewStatus(str, Enum):
    """Whether current boolean availability permits an exact live review."""

    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"


class GuidedProviderRequestLiveCredentialBindingReviewProblemCode(str, Enum):
    """Safe reason why the live credential-binding review cannot advance."""

    CREDENTIAL_UNAVAILABLE = "credential_unavailable"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestCredentialAvailabilityAttestation:
    """Boolean-equivalent host state for one public credential slot."""

    credential_slot: GuidedProviderRequestCredentialSlot
    availability_status: GuidedProviderCredentialStatus

    def __post_init__(self) -> None:
        if (
            type(self.credential_slot)
            is not GuidedProviderRequestCredentialSlot
            or type(self.availability_status)
            is not GuidedProviderCredentialStatus
        ):
            raise TypeError("Credential availability attestation enums must be exact")

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestCredentialAvailabilityAttestation("
            f"credential_slot={self.credential_slot.value!r}, "
            f"availability_status={self.availability_status.value!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestLiveCredentialBindingReview:
    """Short-lived current-user review before any credential value access."""

    status: GuidedProviderRequestLiveCredentialBindingReviewStatus
    next_action: str
    availability_attestations: tuple[
        GuidedProviderRequestCredentialAvailabilityAttestation, ...
    ]
    _credential_binding_response_review: (
        GuidedProviderRequestCredentialBindingResponseReview
    ) = field(repr=False)
    _prepared_at: datetime = field(repr=False)
    _expires_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    contract_version: str = (
        GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION
    )
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Live credential-binding reviews require trusted preparation"
            )
        _require_accepted_response_review(
            self._credential_binding_response_review
        )
        ordered_attestations = _validate_attestation_collection(
            self.availability_attestations
        )
        if self.availability_attestations != ordered_attestations:
            raise ValueError(
                "Credential availability attestations must be canonical"
            )
        all_available = all(
            item.availability_status
            is GuidedProviderCredentialStatus.AVAILABLE
            for item in self.availability_attestations
        )
        expected_status, expected_action = (
            (
                GuidedProviderRequestLiveCredentialBindingReviewStatus
                .REVIEW_REQUIRED,
                _REVIEW_ACTION,
            )
            if all_available
            else (
                GuidedProviderRequestLiveCredentialBindingReviewStatus.BLOCKED,
                _BLOCKED_ACTION,
            )
        )
        if (
            type(self.status)
            is not GuidedProviderRequestLiveCredentialBindingReviewStatus
            or self.status is not expected_status
            or self.next_action != expected_action
        ):
            raise ValueError(
                "Live credential-binding review status/action differs"
            )
        prepared = _utc_datetime(self._prepared_at, "prepared_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not prepared < expires <= prepared + timedelta(minutes=5):
            raise ValueError(
                "Live credential-binding review expiry is invalid"
            )
        _digest(self._context_fingerprint, "context_fingerprint")
        if (
            self.contract_version
            != GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION
        ):
            raise ValueError(
                "Live credential-binding review version differs"
            )
        object.__setattr__(self, "_prepared_at", prepared)
        object.__setattr__(self, "_expires_at", expires)

    @property
    def binding_count(self) -> int:
        return self._credential_binding_response_review.binding_count

    @property
    def contract_count(self) -> int:
        return self._credential_binding_response_review.contract_count

    @property
    def accepted_scope_item_count(self) -> int:
        return (
            self._credential_binding_response_review
            .accepted_scope_item_count
        )

    @property
    def accepted_max_request_count(self) -> int:
        return (
            self._credential_binding_response_review
            .accepted_max_request_count
        )

    @property
    def all_credentials_available(self) -> bool:
        return all(
            item.availability_status
            is GuidedProviderCredentialStatus.AVAILABLE
            for item in self.availability_attestations
        )

    @property
    def problem_codes(
        self,
    ) -> tuple[
        GuidedProviderRequestLiveCredentialBindingReviewProblemCode, ...
    ]:
        return (
            ()
            if self.all_credentials_available
            else (
                GuidedProviderRequestLiveCredentialBindingReviewProblemCode
                .CREDENTIAL_UNAVAILABLE,
            )
        )

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    @property
    def tentative_fields(self) -> tuple[str, ...]:
        return self._credential_binding_response_review.tentative_fields

    @property
    def needs_verification(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self._credential_binding_response_review
                    .needs_verification,
                    "provider_request_live_credential_binding_review",
                )
            )
        )

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestLiveCredentialBindingReview("
            f"status={self.status.value!r}, "
            f"credential_slot_count={len(self.availability_attestations)!r}, "
            f"binding_count={self.binding_count!r}, "
            f"next_action={self.next_action!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return slot-level booleans and aggregate state without secrets."""

        source = self._credential_binding_response_review
        handoff = dict(
            source.to_dict()[
                "provider_request_credential_binding_response"
            ]
        )
        review_ready = (
            self.status
            is GuidedProviderRequestLiveCredentialBindingReviewStatus
            .REVIEW_REQUIRED
        )
        handoff.update(
            {
                "exact_accepted_credential_binding_response_bound": True,
                "same_exact_target_preimages_revalidated": True,
                "same_exact_transport_bindings_revalidated": True,
                "short_lived_execution_recheck_preserved": True,
                "host_credential_availability_attestation_fresh": True,
                "exact_attestation_times_exposed": False,
                "credential_slot_count": len(
                    self.availability_attestations
                ),
                "credential_availability_attestations": [
                    {
                        "credential_slot": item.credential_slot.value,
                        "credential_available": (
                            item.availability_status
                            is GuidedProviderCredentialStatus.AVAILABLE
                        ),
                    }
                    for item in self.availability_attestations
                ],
                "all_credentials_available": (
                    self.all_credentials_available
                ),
                "eligible_for_live_credential_binding_response_count": (
                    self.binding_count if review_ready else 0
                ),
                "requires_exact_typed_live_credential_binding_response": (
                    review_ready
                ),
                "generic_continue_is_live_credential_binding_authority": False,
                "live_credential_binding_response_captured": False,
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
                "http_request_count_created_by_review": 0,
                "immediate_credential_binding_authority_granted": False,
                "credential_binding_authority_active": False,
                "immediate_send_authority_granted": False,
                "send_authority_active": False,
                "execution_authority_active": False,
                "provider_call_count_observed": 0,
                "provider_calls_permitted": False,
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
            "requires_user_response": review_ready,
            "requires_user_review": review_ready,
            "requires_user_decision": review_ready,
            "response_options": (
                list(_RESPONSE_OPTIONS) if review_ready else []
            ),
            "provider_request_live_credential_binding_review": handoff,
            "problems": [item.value for item in self.problem_codes],
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "credential_binding_response_review_read": True,
                "host_boolean_availability_attestation_read": True,
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


def prepare_guided_provider_request_live_credential_binding_review(
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
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    availability_attestations: tuple[
        GuidedProviderRequestCredentialAvailabilityAttestation, ...
    ],
    evaluation_at: datetime,
) -> GuidedProviderRequestLiveCredentialBindingReview:
    """Prepare one short-lived review from boolean host availability only."""

    prepared_at = _utc_datetime(evaluation_at, "evaluation_at")
    ordered_attestations = _validate_attestations_for_preparation(
        availability_attestations,
        send_preparation,
    )
    response_review = (
        assess_guided_provider_request_credential_binding_response(
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
            preimages=preimages,
            evaluation_at=prepared_at,
        )
    )
    _require_accepted_response_review(response_review)
    expires_at = credential_binding_review._expires_at
    if prepared_at >= expires_at:
        raise ValueError(
            "Credential-binding response expires before live review"
        )
    all_available = all(
        item.availability_status is GuidedProviderCredentialStatus.AVAILABLE
        for item in ordered_attestations
    )
    status, next_action = (
        (
            GuidedProviderRequestLiveCredentialBindingReviewStatus
            .REVIEW_REQUIRED,
            _REVIEW_ACTION,
        )
        if all_available
        else (
            GuidedProviderRequestLiveCredentialBindingReviewStatus.BLOCKED,
            _BLOCKED_ACTION,
        )
    )
    return GuidedProviderRequestLiveCredentialBindingReview(
        status=status,
        next_action=next_action,
        availability_attestations=ordered_attestations,
        _credential_binding_response_review=response_review,
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
            send_authorization_review,
            send_authorization_response,
            send_preparation,
            credential_binding_review,
            credential_binding_response,
            response_review,
            ordered_attestations,
            prepared_at,
            expires_at,
        ),
        _token=_REVIEW_TOKEN,
    )


def assess_guided_provider_request_live_credential_binding_review(
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
    evaluation_at: datetime,
) -> GuidedProviderRequestLiveCredentialBindingReview:
    """Revalidate original/current context and the short-lived host review."""

    if (
        type(live_credential_binding_review)
        is not GuidedProviderRequestLiveCredentialBindingReview
    ):
        raise TypeError(
            "live_credential_binding_review must be an exact review"
        )
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < live_credential_binding_review._prepared_at:
        raise ValueError("evaluation_at cannot precede live review")
    if evaluated_at >= live_credential_binding_review._expires_at:
        raise ValueError("Live credential-binding review has expired")
    ordered_attestations = _validate_attestations_for_preparation(
        live_credential_binding_review.availability_attestations,
        send_preparation,
    )

    captured_response_review = (
        assess_guided_provider_request_credential_binding_response(
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
            preimages=preimages,
            evaluation_at=live_credential_binding_review._prepared_at,
        )
    )
    _require_accepted_response_review(captured_response_review)
    expected_fingerprint = _review_context_fingerprint(
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
        captured_response_review,
        ordered_attestations,
        live_credential_binding_review._prepared_at,
        live_credential_binding_review._expires_at,
    )
    if (
        live_credential_binding_review._context_fingerprint
        != expected_fingerprint
    ):
        raise ValueError(
            "Live credential-binding review differs from exact context"
        )

    current_response_review = (
        assess_guided_provider_request_credential_binding_response(
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
            preimages=preimages,
            evaluation_at=evaluated_at,
        )
    )
    _require_accepted_response_review(current_response_review)
    return live_credential_binding_review


def _require_accepted_response_review(
    review: GuidedProviderRequestCredentialBindingResponseReview,
) -> None:
    if (
        type(review)
        is not GuidedProviderRequestCredentialBindingResponseReview
        or review.status
        is not GuidedProviderRequestCredentialBindingResponseStatus
        .READY_FOR_PRIVATE_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_GATE
        or review.response_kind
        is not GuidedProviderRequestCredentialBindingResponseKind
        .ACCEPT_CREDENTIAL_BINDING
        or review.next_action
        != "prepare_private_provider_request_live_credential_binding_gate"
    ):
        raise ValueError(
            "Live credential-binding review requires an accepted response"
        )


def _validate_attestations_for_preparation(
    attestations: tuple[
        GuidedProviderRequestCredentialAvailabilityAttestation, ...
    ],
    send_preparation: GuidedProviderRequestSendPreparation,
) -> tuple[GuidedProviderRequestCredentialAvailabilityAttestation, ...]:
    if type(send_preparation) is not GuidedProviderRequestSendPreparation:
        raise TypeError("send_preparation must be exact")
    ordered = _validate_attestation_collection(attestations)
    required_slots = tuple(
        sorted(
            {item.credential_slot for item in send_preparation._bindings},
            key=lambda item: item.value,
        )
    )
    if tuple(item.credential_slot for item in ordered) != required_slots:
        raise ValueError(
            "Credential availability attestations differ from required slots"
        )
    return ordered


def _validate_attestation_collection(
    attestations: tuple[
        GuidedProviderRequestCredentialAvailabilityAttestation, ...
    ],
) -> tuple[GuidedProviderRequestCredentialAvailabilityAttestation, ...]:
    if (
        not isinstance(attestations, tuple)
        or not 1 <= len(attestations) <= _MAX_CREDENTIAL_SLOTS
        or any(
            type(item)
            is not GuidedProviderRequestCredentialAvailabilityAttestation
            for item in attestations
        )
    ):
        raise ValueError("Credential availability attestations are invalid")
    ordered = tuple(
        sorted(attestations, key=lambda item: item.credential_slot.value)
    )
    if len({item.credential_slot for item in ordered}) != len(ordered):
        raise ValueError("Credential availability slots must be unique")
    return ordered


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
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    send_preparation: GuidedProviderRequestSendPreparation,
    credential_binding_review: GuidedProviderRequestCredentialBindingReview,
    credential_binding_response: GuidedProviderRequestCredentialBindingResponse,
    credential_binding_response_review: (
        GuidedProviderRequestCredentialBindingResponseReview
    ),
    availability_attestations: tuple[
        GuidedProviderRequestCredentialAvailabilityAttestation, ...
    ],
    prepared_at: datetime,
    expires_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": (
                GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION
            ),
            "credential_binding_response_contract_version": (
                GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_RESPONSE_VERSION
            ),
            "domain": "guided-provider-request-live-credential-binding-review",
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
            "provider_request_credential_binding_response_review": (
                _private_context_value(credential_binding_response_review)
            ),
            "credential_availability_attestations": [
                _private_context_value(item)
                for item in availability_attestations
            ],
            "prepared_at": _private_context_value(prepared_at),
            "expires_at": _private_context_value(expires_at),
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
    "GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION",
    "GuidedProviderRequestCredentialAvailabilityAttestation",
    "GuidedProviderRequestLiveCredentialBindingReview",
    "GuidedProviderRequestLiveCredentialBindingReviewProblemCode",
    "GuidedProviderRequestLiveCredentialBindingReviewStatus",
    "assess_guided_provider_request_live_credential_binding_review",
    "prepare_guided_provider_request_live_credential_binding_review",
]
