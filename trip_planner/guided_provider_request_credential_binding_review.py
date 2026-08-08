"""Review one fresh transport-bound provider bundle before credential access.

Phase 5.26 consumes only a fresh Phase 5.25 send preparation, the same full
private planning context, and the same typed target preimages.  It produces a
short-lived current-user review of the exact transport profiles, public
endpoint templates, HTTP methods, provider-field placements, and credential
slot names.  The response options are typed and belong to a later response
gate.

This module does not read an environment or vault, access or bind a credential
value, expand provider identifiers into URLs or payloads, construct an HTTP
request, open a network connection, call a provider, reserve spend, persist
data, schedule, render, deploy, confirm, or mutate canonical state.
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
    GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION,
    GuidedProviderRequestSendPreparation,
    GuidedProviderRequestSendPreparationReview,
    GuidedProviderRequestSendPreparationStatus,
    GuidedProviderRequestTransportBinding,
    assess_guided_provider_request_send_preparation,
)
from .guided_provider_scope import GuidedProviderScopeProposal
from .guided_provider_scope_response import GuidedProviderScopeResponse
from .guided_refinement import GuidedRefinementCandidate, GuidedRefinementResponse
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW_VERSION = (
    "guided-provider-request-credential-binding-review/v1"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_NEXT_ACTION = "capture_private_provider_request_credential_binding_response"
_RESPONSE_OPTIONS = (
    "accept_credential_binding",
    "request_smaller",
    "cancel",
)
_REVIEW_TOKEN = object()


class GuidedProviderRequestCredentialBindingReviewStatus(str, Enum):
    """A fresh transport-bound bundle requires one typed response."""

    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestCredentialBindingReview:
    """Exact private review before any credential value can be accessed."""

    status: GuidedProviderRequestCredentialBindingReviewStatus
    next_action: str
    _preparation_review: GuidedProviderRequestSendPreparationReview = field(
        repr=False
    )
    _prepared_at: datetime = field(repr=False)
    _expires_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    contract_version: str = (
        GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW_VERSION
    )
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Credential-binding reviews require trusted preparation"
            )
        if (
            type(self.status)
            is not GuidedProviderRequestCredentialBindingReviewStatus
            or self.status
            is not GuidedProviderRequestCredentialBindingReviewStatus
            .REVIEW_REQUIRED
            or self.next_action != _NEXT_ACTION
        ):
            raise ValueError("Credential-binding review status/action differs")
        source = self._preparation_review
        if (
            type(source) is not GuidedProviderRequestSendPreparationReview
            or source.status
            is not GuidedProviderRequestSendPreparationStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW
            or source.next_action
            != "prepare_private_provider_request_credential_binding_review"
        ):
            raise ValueError(
                "Credential-binding review requires a ready send preparation"
            )
        prepared = _utc_datetime(self._prepared_at, "prepared_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not prepared < expires <= prepared + timedelta(minutes=5):
            raise ValueError("Credential-binding review expiry is invalid")
        _digest(self._context_fingerprint, "context_fingerprint")
        if (
            self.contract_version
            != GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW_VERSION
        ):
            raise ValueError("Credential-binding review version differs")
        object.__setattr__(self, "_prepared_at", prepared)
        object.__setattr__(self, "_expires_at", expires)

    @property
    def binding_count(self) -> int:
        return self._preparation_review.binding_count

    @property
    def contract_count(self) -> int:
        return self._preparation_review.contract_count

    @property
    def accepted_scope_item_count(self) -> int:
        return self._preparation_review.accepted_scope_item_count

    @property
    def accepted_max_request_count(self) -> int:
        return self._preparation_review.accepted_max_request_count

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    @property
    def tentative_fields(self) -> tuple[str, ...]:
        return self._preparation_review.tentative_fields

    @property
    def needs_verification(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self._preparation_review.needs_verification,
                    "provider_request_credential_binding_review",
                )
            )
        )

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestCredentialBindingReview("
            f"status={self.status.value!r}, "
            f"binding_count={self.binding_count!r}, "
            f"next_action={self.next_action!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return public transport shape and aggregate state only."""

        source = self._preparation_review
        preparation_safe = source.to_dict()["provider_request_send_preparation"]
        handoff = dict(preparation_safe)
        handoff.update(
            {
                "exact_send_preparation_bound": True,
                "same_exact_target_preimages_revalidated": True,
                "short_lived_execution_recheck_preserved": True,
                "must_reassess_before_response_capture": True,
                "exact_review_times_exposed": False,
                "reviewed_transport_binding_count": self.binding_count,
                "eligible_for_credential_binding_response_count": (
                    self.binding_count
                ),
                "items": [
                    _safe_review_item(item) for item in source._bindings
                ],
                "private_review_payload_available": True,
                "exact_private_values_in_safe_output": False,
                "provider_identifier_values_exposed": False,
                "source_binding_fingerprints_exposed": False,
                "context_fingerprints_exposed": False,
                "credential_binding_response_captured": False,
                "credential_binding_authority_active": False,
                "credential_value_access_permitted": False,
                "credential_values_accessed": False,
                "credential_values_bound": False,
                "credential_values_included": False,
                "environment_read": False,
                "vault_accessed": False,
                "network_accessed": False,
                "http_request_count_created_by_review": 0,
                "provider_call_count_observed": 0,
                "provider_calls_permitted": False,
                "send_authority_active": False,
                "execution_authority_active": False,
                "decision_state": DecisionState.CANDIDATE.value,
                "evidence_state": EvidenceState.UNVERIFIED.value,
                "supports_authoritative_use": False,
            }
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": True,
            "requires_user_review": True,
            "requires_user_decision": True,
            "response_options": list(_RESPONSE_OPTIONS),
            "provider_request_credential_binding_review": handoff,
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "transport_bound_private_contracts_read": True,
                "private_review_payload_generated_by_safe_view": False,
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

    def to_ephemeral_private_review_payload(self) -> dict[str, Any]:
        """Return exact non-identifier values for direct current-user review."""

        source = self._preparation_review
        return {
            "contract_version": self.contract_version,
            "payload_handling": "private_ephemeral_direct_human_review_only",
            "response_options": list(_RESPONSE_OPTIONS),
            "transport_bindings": [
                _private_review_item(item) for item in source._bindings
            ],
            "transport_binding_count": self.binding_count,
            "materialized_request_contract_count": self.contract_count,
            "accepted_max_request_count": self.accepted_max_request_count,
            "exact_non_identifier_provider_transmitted_values_included": True,
            "provider_identifier_values_exposed": False,
            "source_binding_fingerprints_exposed": False,
            "context_fingerprints_exposed": False,
            "credential_slot_names_included": True,
            "credential_values_accessed": False,
            "credential_values_exposed": False,
            "credential_values_bound": False,
            "url_path_identifiers_expanded": False,
            "http_requests_created": False,
            "credential_binding_response_captured": False,
            "credential_binding_authority_active": False,
            "send_authority_active": False,
            "execution_authority_active": False,
            "provider_calls_permitted": False,
            "decision_state": DecisionState.CANDIDATE.value,
            "evidence_state": EvidenceState.UNVERIFIED.value,
            "supports_authoritative_use": False,
        }


def prepare_guided_provider_request_credential_binding_review(
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
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestCredentialBindingReview:
    """Prepare one exact private review from a fresh send preparation."""

    prepared_at = _utc_datetime(evaluation_at, "evaluation_at")
    current_preparation_review = (
        assess_guided_provider_request_send_preparation(
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
            preimages=preimages,
            evaluation_at=prepared_at,
        )
    )
    _require_ready_preparation(current_preparation_review)
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
        send_authorization_review,
        send_authorization_response,
        send_preparation,
        current_preparation_review,
        prepared_at,
        send_preparation._expires_at,
    )


def assess_guided_provider_request_credential_binding_review(
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
    review: GuidedProviderRequestCredentialBindingReview,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestCredentialBindingReview:
    """Rebuild the exact review and revalidate the current private chain."""

    if type(review) is not GuidedProviderRequestCredentialBindingReview:
        raise TypeError("review must be an exact credential-binding review")
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < review._prepared_at:
        raise ValueError(
            "evaluation_at cannot precede credential-binding review preparation"
        )

    captured_preparation_review = (
        assess_guided_provider_request_send_preparation(
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
            preimages=preimages,
            evaluation_at=review._prepared_at,
        )
    )
    _require_ready_preparation(captured_preparation_review)
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
        send_authorization_review,
        send_authorization_response,
        send_preparation,
        captured_preparation_review,
        review._prepared_at,
        send_preparation._expires_at,
    )
    if review != expected:
        raise ValueError(
            "Credential-binding review differs from the exact context"
        )
    if evaluated_at >= review._expires_at:
        raise ValueError("Credential-binding review is no longer current")

    current_preparation_review = (
        assess_guided_provider_request_send_preparation(
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
            preimages=preimages,
            evaluation_at=evaluated_at,
        )
    )
    _require_ready_preparation(current_preparation_review)
    if current_preparation_review != review._preparation_review:
        raise ValueError("Credential-binding review source has drifted")
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
    send_authorization_review: GuidedProviderRequestSendAuthorizationReview,
    send_authorization_response: GuidedProviderRequestSendAuthorizationResponse,
    send_preparation: GuidedProviderRequestSendPreparation,
    preparation_review: GuidedProviderRequestSendPreparationReview,
    prepared_at: datetime,
    expires_at: datetime,
) -> GuidedProviderRequestCredentialBindingReview:
    return GuidedProviderRequestCredentialBindingReview(
        status=(
            GuidedProviderRequestCredentialBindingReviewStatus.REVIEW_REQUIRED
        ),
        next_action=_NEXT_ACTION,
        _preparation_review=preparation_review,
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
            preparation_review,
            prepared_at,
            expires_at,
        ),
        _token=_REVIEW_TOKEN,
    )


def _safe_review_item(
    binding: GuidedProviderRequestTransportBinding,
) -> dict[str, object]:
    item = binding.to_safe_dict()
    item.update(
        {
            "credential_binding_reviewed": True,
            "credential_binding_response_captured": False,
            "credential_value_access_permitted": False,
            "credential_value_accessed": False,
            "credential_value_bound": False,
            "http_request_created": False,
            "provider_call_permitted": False,
        }
    )
    return item


def _private_review_item(
    binding: GuidedProviderRequestTransportBinding,
) -> dict[str, object]:
    contract = binding._contract
    transmitted_values = dict(contract._provider_transmitted_values)
    return {
        "topic": contract.topic.value,
        "capability": contract.capability.value,
        "materialization_kind": contract.materialization_kind.value,
        "transport_profile": binding.transport_profile.value,
        "endpoint_template": binding.endpoint_template,
        "http_method": binding.http_method.value,
        "credential": {
            "slot": binding.credential_slot.value,
            "provider_field_name": binding.credential_provider_field_name,
            "placement": binding.credential_placement.value,
            "value_accessed": False,
            "value_bound": False,
            "value_included": False,
        },
        "provider_transmitted_fields": [
            {
                "contract_field_name": contract_name,
                "provider_field_name": provider_name,
                "placement": placement.value,
                "value": _human_value(transmitted_values[contract_name]),
            }
            for contract_name, provider_name, placement
            in binding._transmitted_field_bindings
        ],
        "provider_identifier_fields": [
            {
                "contract_field_name": contract_name,
                "provider_field_name": provider_name,
                "placement": placement.value,
                "value_exposed": False,
            }
            for contract_name, provider_name, placement
            in binding._provider_identifier_field_bindings
        ],
        "fixed_public_parameters": [
            {
                "provider_field_name": name,
                "value": value,
                "placement": placement.value,
            }
            for name, value, placement in binding._fixed_parameters
        ],
        "local_result_binding_field_names": [
            name for name, _ in contract._local_result_binding
        ],
        "exact_non_identifier_provider_transmitted_values_included": True,
        "provider_identifier_values_exposed": False,
        "local_result_binding_values_exposed": False,
        "credential_value_accessed": False,
        "credential_value_bound": False,
        "url_path_identifier_expanded": False,
        "http_request_created": False,
        "provider_call_permitted": False,
        "decision_state": DecisionState.CANDIDATE.value,
        "evidence_state": EvidenceState.UNVERIFIED.value,
        "supports_authoritative_use": False,
    }


def _require_ready_preparation(
    review: GuidedProviderRequestSendPreparationReview,
) -> None:
    if (
        type(review) is not GuidedProviderRequestSendPreparationReview
        or review.status
        is not GuidedProviderRequestSendPreparationStatus
        .READY_FOR_PRIVATE_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW
        or review.next_action
        != "prepare_private_provider_request_credential_binding_review"
    ):
        raise ValueError(
            "Credential-binding review requires a fresh send preparation"
        )


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
    preparation_review: GuidedProviderRequestSendPreparationReview,
    prepared_at: datetime,
    expires_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": (
                GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW_VERSION
            ),
            "send_preparation_contract_version": (
                GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION
            ),
            "domain": "guided-provider-request-credential-binding-review",
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
            "provider_request_send_preparation_review": (
                _private_context_value(preparation_review)
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
    "GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW_VERSION",
    "GuidedProviderRequestCredentialBindingReview",
    "GuidedProviderRequestCredentialBindingReviewStatus",
    "assess_guided_provider_request_credential_binding_review",
    "prepare_guided_provider_request_credential_binding_review",
]
