"""Compose the frozen provider safety chain into one pre-execution facade.

Phase 5.30 consumes one fresh exact Phase 5.29 acceptance, reuses the exact
Phase 5.22 contracts and Phase 5.25 allowlisted transport bindings, asks a
host-owned resolver for each distinct credential slot exactly once, and builds
short-lived private HTTP descriptors.  A final trusted-time assessment runs
after credential resolution and request construction.

Raw target preimages are never retained.  Credentials and prepared requests
are process-local and deliberately non-serializable; their repr and safe view
contain no private values.  This module has no transport, send, provider-call,
trip-write, scheduling, rendering, deployment, or canonical mutation path.
The resulting bundle is only input to the separately bounded Phase 5.31
executor and carries a single-use claim that this module never consumes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlsplit

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
    GuidedProviderRequestContract,
    GuidedProviderRequestContractMaterialization,
)
from .guided_provider_request_credential_binding_response import (
    GuidedProviderRequestCredentialBindingResponse,
)
from .guided_provider_request_credential_binding_review import (
    GuidedProviderRequestCredentialBindingReview,
)
from .guided_provider_request_live_credential_binding_response import (
    GuidedProviderRequestLiveCredentialBindingResponse,
    GuidedProviderRequestLiveCredentialBindingResponseKind,
    GuidedProviderRequestLiveCredentialBindingResponseReview,
    GuidedProviderRequestLiveCredentialBindingResponseStatus,
    assess_guided_provider_request_live_credential_binding_response,
)
from .guided_provider_request_live_credential_binding_review import (
    GuidedProviderRequestLiveCredentialBindingReview,
)
from .guided_provider_request_materialization_response import (
    GuidedProviderRequestMaterializationResponse,
)
from .guided_provider_request_materialization_review import (
    GuidedProviderRequestMaterializationKind,
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
    GuidedProviderRequestHTTPMethod,
    GuidedProviderRequestSendPreparation,
    GuidedProviderRequestTransportBinding,
    GuidedProviderRequestTransportProfile,
    GuidedProviderRequestValuePlacement,
)
from .guided_provider_scope import GuidedProviderScopeProposal
from .guided_provider_scope_response import GuidedProviderScopeResponse
from .guided_refinement import GuidedRefinementCandidate, GuidedRefinementResponse
from .models import DecisionState, EvidenceState


GUIDED_PROVIDER_PRE_EXECUTION_VERSION = "guided-provider-pre-execution/v1"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_REQUESTS = 32
_MAX_CREDENTIAL_BYTES = 4096
_NEXT_ACTION = "execute_bounded_provider_requests"
_CONTEXT_TOKEN = object()
_LEASE_TOKEN = object()
_REQUEST_TOKEN = object()
_PRE_EXECUTION_TOKEN = object()
_REVIEW_TOKEN = object()
_EXECUTION_CLAIM_TOKEN = object()
_CREDENTIAL_ACCESS_TOKEN = object()
_MAX_CONSENT_CLAIMS = 4096
_CONSENT_CLAIM_LOCK = Lock()
_CONSENT_CLAIMS: dict[str, datetime] = {}


class _SealedSlots:
    """Reject ordinary mutation after trusted construction."""

    __slots__ = ("_sealed",)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError(f"{type(self).__name__} is immutable")
        object.__setattr__(self, name, value)

    def _seal(self) -> None:
        object.__setattr__(self, "_sealed", True)


class GuidedProviderCredentialResolver(Protocol):
    """Host seam that resolves one exact public credential slot."""

    def resolve(self, slot: GuidedProviderRequestCredentialSlot) -> str:
        """Return one process-local credential value without logging it."""


class GuidedProviderCredentialBindingError(RuntimeError):
    """Sanitized binding failure that never includes a credential value."""

    def __init__(self, slot: GuidedProviderRequestCredentialSlot) -> None:
        self.slot = slot
        super().__init__(f"Credential binding failed for slot {slot.value!r}.")


class GuidedProviderPreExecutionStatus(str, Enum):
    """A private bundle is ready only for the separately bounded executor."""

    READY_FOR_BOUNDED_PROVIDER_EXECUTION = (
        "ready_for_bounded_provider_execution"
    )


@dataclass(frozen=True, slots=True)
class _ExecutionLimits:
    accepted_max_request_count: int
    bound_google_request_count: int
    bound_serpapi_request_count: int
    estimated_google_cost_usd_micros: int
    accepted_max_google_cost_usd_micros: int
    bound_serpapi_credit_count: int
    serpapi_credit_cap: int

    def __post_init__(self) -> None:
        values = (
            self.accepted_max_request_count,
            self.bound_google_request_count,
            self.bound_serpapi_request_count,
            self.estimated_google_cost_usd_micros,
            self.accepted_max_google_cost_usd_micros,
            self.bound_serpapi_credit_count,
            self.serpapi_credit_cap,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Pre-execution limits must be non-negative integers")
        if not 1 <= self.accepted_max_request_count <= _MAX_REQUESTS:
            raise ValueError("Accepted request cap is invalid")
        if (
            self.bound_google_request_count + self.bound_serpapi_request_count
            > self.accepted_max_request_count
            or self.estimated_google_cost_usd_micros
            > self.accepted_max_google_cost_usd_micros
            or self.bound_serpapi_credit_count
            != self.bound_serpapi_request_count
            or self.bound_serpapi_credit_count > self.serpapi_credit_cap
        ):
            raise ValueError("Pre-execution limits differ from accepted scope")


class GuidedProviderPreExecutionContext(_SealedSlots):
    """One composed process-local reference to the frozen safety chain."""

    __slots__ = (
        "_assessment_args",
        "_response",
        "_materialization",
        "_send_preparation",
        "_limits",
        "_composed_at",
        "_expires_at",
        "_context_fingerprint",
    )

    def __init__(
        self,
        *,
        _assessment_args: tuple[object, ...],
        _response: GuidedProviderRequestLiveCredentialBindingResponse,
        _materialization: GuidedProviderRequestContractMaterialization,
        _send_preparation: GuidedProviderRequestSendPreparation,
        _limits: _ExecutionLimits,
        _composed_at: datetime,
        _expires_at: datetime,
        _context_fingerprint: str = "",
        _token: object | None = None,
    ) -> None:
        if _token is not _CONTEXT_TOKEN:
            raise ValueError("Pre-execution contexts require trusted composition")
        if (
            not isinstance(_assessment_args, tuple)
            or len(_assessment_args) != 26
            or type(_response)
            is not GuidedProviderRequestLiveCredentialBindingResponse
            or type(_materialization)
            is not GuidedProviderRequestContractMaterialization
            or type(_send_preparation)
            is not GuidedProviderRequestSendPreparation
            or type(_limits) is not _ExecutionLimits
        ):
            raise TypeError("Pre-execution context sources must be exact")
        composed = _utc_datetime(_composed_at, "composed_at")
        expires = _utc_datetime(_expires_at, "expires_at")
        if not composed < expires <= composed + timedelta(minutes=5):
            raise ValueError("Pre-execution context expiry is invalid")
        fingerprint = _digest(_context_fingerprint, "context_fingerprint")
        self._assessment_args = _assessment_args
        self._response = _response
        self._materialization = _materialization
        self._send_preparation = _send_preparation
        self._limits = _limits
        self._composed_at = composed
        self._expires_at = expires
        self._context_fingerprint = fingerprint
        self._seal()

    @property
    def request_count(self) -> int:
        return self._send_preparation.binding_count

    def __repr__(self) -> str:
        return (
            "GuidedProviderPreExecutionContext("
            f"request_count={self.request_count!r})"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Pre-execution contexts are process-local")


class _EphemeralCredentialLease(_SealedSlots):
    """Opaque, non-serializable values bound to one exact short-lived bundle."""

    __slots__ = (
        "__values",
        "_bound_at",
        "_expires_at",
        "_context_fingerprint",
    )

    def __init__(
        self,
        values: dict[GuidedProviderRequestCredentialSlot, str],
        *,
        bound_at: datetime,
        expires_at: datetime,
        context_fingerprint: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _LEASE_TOKEN:
            raise ValueError("Credential leases require the host binding seam")
        if (
            type(values) is not dict
            or not values
            or any(
                type(slot) is not GuidedProviderRequestCredentialSlot
                or type(value) is not str
                or not value
                or len(value.encode("utf-8")) > _MAX_CREDENTIAL_BYTES
                or value != value.strip()
                or any(ord(char) < 33 or ord(char) == 127 for char in value)
                for slot, value in values.items()
            )
        ):
            raise ValueError("Credential lease values are invalid")
        bound = _utc_datetime(bound_at, "bound_at")
        expires = _utc_datetime(expires_at, "expires_at")
        if bound >= expires:
            raise ValueError("Credential lease expiry is invalid")
        self.__values = dict(values)
        self._bound_at = bound
        self._expires_at = expires
        self._context_fingerprint = _digest(
            context_fingerprint,
            "context_fingerprint",
        )
        self._seal()

    @property
    def slots(self) -> tuple[GuidedProviderRequestCredentialSlot, ...]:
        return tuple(sorted(self.__values, key=lambda item: item.value))

    def _value(
        self,
        slot: GuidedProviderRequestCredentialSlot,
        *,
        evaluation_at: datetime,
        _token: object | None,
    ) -> str:
        if _token is not _CREDENTIAL_ACCESS_TOKEN:
            raise ValueError("Credential values require the bounded executor")
        if type(slot) is not GuidedProviderRequestCredentialSlot:
            raise TypeError("Credential slot must be exact")
        evaluated = _utc_datetime(evaluation_at, "evaluation_at")
        if evaluated < self._bound_at:
            raise ValueError("Credential lease clock rolled back")
        if evaluated >= self._expires_at:
            raise ValueError("Credential lease expired")
        try:
            return self.__values[slot]
        except KeyError:
            raise ValueError("Credential slot is outside the lease") from None

    def _clear(self, *, _token: object | None) -> None:
        if _token is not _CREDENTIAL_ACCESS_TOKEN:
            raise ValueError("Credential clearing requires the bounded executor")
        for slot in tuple(self.__values):
            self.__values[slot] = ""
        self.__values.clear()

    def __repr__(self) -> str:
        return f"_EphemeralCredentialLease(slot_count={len(self.__values)!r})"

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Credential leases are non-serializable")


class GuidedProviderPreparedRequest(_SealedSlots):
    """Private allowlisted descriptor with no public send method."""

    __slots__ = (
        "transport_profile",
        "materialization_kind",
        "http_method",
        "endpoint_template",
        "credential_slot",
        "credential_provider_field_name",
        "credential_placement",
        "estimated_google_cost_usd_micros",
        "serpapi_credit_count",
        "_request_fingerprint",
        "_endpoint",
        "_headers",
        "_query_parameters",
        "_json_body",
        "_local_result_binding",
        "_binding",
        "_contract",
        "_credential_lease",
    )

    def __init__(
        self,
        *,
        transport_profile: GuidedProviderRequestTransportProfile,
        materialization_kind: GuidedProviderRequestMaterializationKind,
        http_method: GuidedProviderRequestHTTPMethod,
        endpoint_template: str,
        credential_slot: GuidedProviderRequestCredentialSlot,
        credential_provider_field_name: str,
        credential_placement: GuidedProviderRequestValuePlacement,
        estimated_google_cost_usd_micros: int,
        serpapi_credit_count: int,
        _request_fingerprint: str,
        _endpoint: str,
        _headers: tuple[tuple[str, str], ...],
        _query_parameters: tuple[tuple[str, str], ...],
        _json_body: bytes | None,
        _local_result_binding: tuple[tuple[str, object], ...],
        _binding: GuidedProviderRequestTransportBinding,
        _contract: GuidedProviderRequestContract,
        _credential_lease: _EphemeralCredentialLease,
        _token: object | None = None,
    ) -> None:
        if _token is not _REQUEST_TOKEN:
            raise ValueError("Prepared requests require the allowlisted builder")
        self.transport_profile = transport_profile
        self.materialization_kind = materialization_kind
        self.http_method = http_method
        self.endpoint_template = endpoint_template
        self.credential_slot = credential_slot
        self.credential_provider_field_name = credential_provider_field_name
        self.credential_placement = credential_placement
        self.estimated_google_cost_usd_micros = (
            estimated_google_cost_usd_micros
        )
        self.serpapi_credit_count = serpapi_credit_count
        self._request_fingerprint = _request_fingerprint
        self._endpoint = _endpoint
        self._headers = _headers
        self._query_parameters = _query_parameters
        self._json_body = _json_body
        self._local_result_binding = _local_result_binding
        self._binding = _binding
        self._contract = _contract
        self._credential_lease = _credential_lease
        if (
            type(self.transport_profile)
            is not GuidedProviderRequestTransportProfile
            or type(self.materialization_kind)
            is not GuidedProviderRequestMaterializationKind
            or type(self.http_method) is not GuidedProviderRequestHTTPMethod
            or type(self.credential_slot)
            is not GuidedProviderRequestCredentialSlot
            or type(self.credential_placement)
            is not GuidedProviderRequestValuePlacement
            or type(self._binding) is not GuidedProviderRequestTransportBinding
            or type(self._contract) is not GuidedProviderRequestContract
            or type(self._credential_lease) is not _EphemeralCredentialLease
            or self._binding._contract is not self._contract
        ):
            raise TypeError("Prepared request sources must be exact")
        if (
            self.transport_profile is not self._binding.transport_profile
            or self.materialization_kind is not self._binding.materialization_kind
            or self.http_method is not self._binding.http_method
            or self.endpoint_template != self._binding.endpoint_template
            or self.credential_slot is not self._binding.credential_slot
            or self.credential_provider_field_name
            != self._binding.credential_provider_field_name
            or self.credential_placement is not self._binding.credential_placement
        ):
            raise ValueError("Prepared request differs from transport binding")
        expected_cost, expected_credit = _request_cost(binding=self._binding)
        if (
            type(self.estimated_google_cost_usd_micros) is not int
            or type(self.serpapi_credit_count) is not int
            or self.estimated_google_cost_usd_micros != expected_cost
            or self.serpapi_credit_count != expected_credit
        ):
            raise ValueError("Prepared request cost binding differs")
        _validate_private_http_shape(self)
        _digest(self._request_fingerprint, "request_fingerprint")
        self._seal()

    @property
    def topic(self) -> str:
        return self._contract.topic.value

    def __repr__(self) -> str:
        return (
            "GuidedProviderPreparedRequest("
            f"transport_profile={self.transport_profile.value!r})"
        )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "topic": self.topic,
            "materialization_kind": self.materialization_kind.value,
            "transport_profile": self.transport_profile.value,
            "http_method": self.http_method.value,
            "endpoint_template": self.endpoint_template,
            "request_fingerprint_exposed": False,
            "credential_slot": self.credential_slot.value,
            "credential_placement": self.credential_placement.value,
            "estimated_google_cost_usd_micros": (
                self.estimated_google_cost_usd_micros
            ),
            "serpapi_credit_count": self.serpapi_credit_count,
            "credential_value_included": False,
            "header_field_names": [name for name, _ in self._headers],
            "query_parameter_names": [
                name for name, _ in self._query_parameters
            ],
            "json_body_created": self._json_body is not None,
            "local_result_binding_field_names": [
                name for name, _ in self._local_result_binding
            ],
            "private_endpoint_exposed": False,
            "private_request_values_exposed": False,
            "send_method_exposed": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Prepared provider requests are non-serializable")


class _SingleUseExecutionClaim(_SealedSlots):
    __slots__ = ("_claimed", "_lock")

    def __init__(self) -> None:
        self._claimed = False
        self._lock = Lock()
        self._seal()

    @property
    def claimed(self) -> bool:
        with self._lock:
            return self._claimed

    def claim(self, *, _token: object | None) -> None:
        if _token is not _EXECUTION_CLAIM_TOKEN:
            raise ValueError("Execution claims require the bounded executor")
        with self._lock:
            if self._claimed:
                raise RuntimeError("Prepared execution was already claimed")
            object.__setattr__(self, "_claimed", True)

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Execution claims are process-local")


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderPreExecution:
    """Short-lived private descriptors plus a not-yet-consumed claim."""

    _requests: tuple[GuidedProviderPreparedRequest, ...] = field(repr=False)
    _credential_lease: _EphemeralCredentialLease = field(repr=False)
    _claim: _SingleUseExecutionClaim = field(repr=False)
    _limits: _ExecutionLimits = field(repr=False)
    _prepared_at: datetime = field(repr=False)
    _expires_at: datetime = field(repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PRE_EXECUTION_TOKEN:
            raise ValueError("Pre-execution bundles require trusted preparation")
        if (
            not isinstance(self._requests, tuple)
            or not 1 <= len(self._requests) <= _MAX_REQUESTS
            or any(type(item) is not GuidedProviderPreparedRequest for item in self._requests)
            or type(self._credential_lease) is not _EphemeralCredentialLease
            or any(item._credential_lease is not self._credential_lease for item in self._requests)
            or type(self._claim) is not _SingleUseExecutionClaim
            or type(self._limits) is not _ExecutionLimits
            or len(self._requests) > self._limits.accepted_max_request_count
        ):
            raise ValueError("Pre-execution bundle contents are invalid")
        prepared = _utc_datetime(self._prepared_at, "prepared_at")
        expires = _utc_datetime(self._expires_at, "expires_at")
        if not prepared < expires <= prepared + timedelta(minutes=5):
            raise ValueError("Pre-execution bundle expiry is invalid")
        if self._credential_lease._expires_at != expires:
            raise ValueError("Credential lease expiry differs from bundle")
        if self._credential_lease._bound_at != prepared:
            raise ValueError("Credential lease binding time differs from bundle")
        if (
            self._credential_lease._context_fingerprint
            != self._context_fingerprint
        ):
            raise ValueError("Credential lease context differs from bundle")
        expected_slots = tuple(
            sorted(
                {item.credential_slot for item in self._requests},
                key=lambda item: item.value,
            )
        )
        if self._credential_lease.slots != expected_slots:
            raise ValueError("Credential lease slots differ from requests")
        if (
            sum(
                item.estimated_google_cost_usd_micros
                for item in self._requests
            )
            != self._limits.estimated_google_cost_usd_micros
            or sum(item.serpapi_credit_count for item in self._requests)
            != self._limits.bound_serpapi_credit_count
        ):
            raise ValueError("Prepared request costs differ from accepted limits")
        fingerprints = tuple(item._request_fingerprint for item in self._requests)
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("Pre-execution requests must be distinct")
        _digest(self._context_fingerprint, "context_fingerprint")
        object.__setattr__(self, "_prepared_at", prepared)
        object.__setattr__(self, "_expires_at", expires)

    @property
    def request_count(self) -> int:
        return len(self._requests)

    @property
    def credential_slot_count(self) -> int:
        return len(self._credential_lease.slots)

    def __repr__(self) -> str:
        return (
            "GuidedProviderPreExecution("
            f"request_count={self.request_count!r}, "
            f"credential_slot_count={self.credential_slot_count!r})"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Pre-execution bundles are non-serializable")


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderPreExecutionReview:
    """Immutable safe snapshot of one fresh, unclaimed bundle assessment."""

    status: GuidedProviderPreExecutionStatus
    next_action: str
    request_count: int
    _safe_payload_json: str = field(repr=False)
    contract_version: str = GUIDED_PROVIDER_PRE_EXECUTION_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Pre-execution reviews require the assessor")
        if (
            type(self.status) is not GuidedProviderPreExecutionStatus
            or self.status
            is not GuidedProviderPreExecutionStatus.READY_FOR_BOUNDED_PROVIDER_EXECUTION
            or self.next_action != _NEXT_ACTION
            or type(self.request_count) is not int
            or not 1 <= self.request_count <= _MAX_REQUESTS
            or type(self._safe_payload_json) is not str
            or self.contract_version != GUIDED_PROVIDER_PRE_EXECUTION_VERSION
        ):
            raise ValueError("Pre-execution review status or source differs")
        try:
            safe = json.loads(self._safe_payload_json)
        except (TypeError, ValueError):
            raise ValueError("Pre-execution safe snapshot is invalid") from None
        if (
            not isinstance(safe, dict)
            or safe.get("contract_version") != self.contract_version
            or safe.get("status") != self.status.value
            or safe.get("next_action") != self.next_action
            or safe.get("provider_pre_execution", {}).get("request_count")
            != self.request_count
        ):
            raise ValueError("Pre-execution safe snapshot differs")

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderPreExecutionReview("
            f"status={self.status.value!r}, request_count={self.request_count!r}, "
            f"next_action={self.next_action!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._safe_payload_json)

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Pre-execution reviews are non-serializable")


def _safe_pre_execution_review(
    bundle: GuidedProviderPreExecution,
) -> dict[str, Any]:
    """Freeze a safe assessment-time snapshot before claim or lease clear."""

    limits = bundle._limits
    profile_counts = {
        profile.value: sum(
            item.transport_profile is profile for item in bundle._requests
        )
        for profile in GuidedProviderRequestTransportProfile
    }
    return {
        "contract_version": GUIDED_PROVIDER_PRE_EXECUTION_VERSION,
        "status": (
            GuidedProviderPreExecutionStatus
            .READY_FOR_BOUNDED_PROVIDER_EXECUTION.value
        ),
        "next_action": _NEXT_ACTION,
        "requires_user_response": False,
        "requires_user_review": False,
        "requires_user_decision": False,
        "provider_pre_execution": {
            "exact_phase529_consent_revalidated": True,
            "same_exact_target_preimages_revalidated_not_retained": True,
            "same_exact_materialized_contracts_revalidated": True,
            "same_exact_transport_bindings_revalidated": True,
            "final_post_construction_recheck_complete": True,
            "inherited_expiry_preserved": True,
            "request_count": bundle.request_count,
            "accepted_max_request_count": limits.accepted_max_request_count,
            "bound_google_request_count": limits.bound_google_request_count,
            "bound_serpapi_request_count": limits.bound_serpapi_request_count,
            "estimated_google_cost_usd_micros": (
                limits.estimated_google_cost_usd_micros
            ),
            "accepted_max_google_cost_usd_micros": (
                limits.accepted_max_google_cost_usd_micros
            ),
            "bound_serpapi_credit_count": limits.bound_serpapi_credit_count,
            "serpapi_credit_cap": limits.serpapi_credit_cap,
            "credential_slot_count": bundle.credential_slot_count,
            "credential_slots": [
                item.value for item in bundle._credential_lease.slots
            ],
            "credential_values_resolved_once_per_slot": True,
            "credential_values_bound_process_locally": True,
            "credential_values_in_safe_output": False,
            "credential_values_serializable": False,
            "transport_profile_counts": profile_counts,
            "items": [item.to_safe_dict() for item in bundle._requests],
            "allowlisted_http_requests_constructed": True,
            "prepared_bundle_serializable": False,
            "single_use_execution_claim_unconsumed_at_assessment": True,
            "prepared_bundle_is_replayable_authority": False,
            "send_method_exposed": False,
            "send_authority_active": False,
            "execution_authority_active": False,
            "provider_call_count_observed": 0,
            "provider_calls_permitted_by_phase530": False,
            "writes_to_trip": False,
            "decision_state": DecisionState.CANDIDATE.value,
            "evidence_state": EvidenceState.UNVERIFIED.value,
            "supports_authoritative_use": False,
        },
        "tentative_fields": ["provider_execution_results"],
        "needs_verification": ["bounded_provider_execution"],
        "side_effects": {
            "process_local": True,
            "credential_resolver_accessed": True,
            "credential_values_bound": True,
            "private_values_in_safe_output": False,
            "http_requests_created": True,
            "network_accessed": False,
            "provider_calls": False,
            "writes_to_trip": False,
            "scheduled": False,
            "trip_created": False,
            "rendered": False,
            "deployed": False,
        },
    }


def compose_guided_provider_pre_execution_context(
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
    live_credential_binding_review: GuidedProviderRequestLiveCredentialBindingReview,
    response: GuidedProviderRequestLiveCredentialBindingResponse,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderPreExecutionContext:
    """Collapse the exact accepted chain without retaining raw preimages."""

    composed_at = _utc_datetime(evaluation_at, "evaluation_at")
    assessment_args: tuple[object, ...] = (
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
    )
    assessed = assess_guided_provider_request_live_credential_binding_response(
        *assessment_args,
        response,
        preimages=preimages,
        evaluation_at=composed_at,
    )
    _require_accepted_response(assessed)
    if type(materialization) is not GuidedProviderRequestContractMaterialization:
        raise TypeError("materialization must be exact")
    if type(send_preparation) is not GuidedProviderRequestSendPreparation:
        raise TypeError("send_preparation must be exact")
    expires_at = min(
        execution_time_recheck._expires_at,
        materialization._expires_at,
        send_preparation._expires_at,
        live_credential_binding_review._expires_at,
    )
    if composed_at >= expires_at:
        raise ValueError("Accepted provider execution context expired")
    limits = _limits_from_assessment(assessed)
    fingerprint = _sha256(
        {
            "contract_version": GUIDED_PROVIDER_PRE_EXECUTION_VERSION,
            "domain": "guided-provider-pre-execution-context",
            "assessment_args": _private_context_value(assessment_args),
            "response": _private_context_value(response),
            "limits": _private_context_value(limits),
            "composed_at": composed_at,
            "expires_at": expires_at,
        }
    )
    return GuidedProviderPreExecutionContext(
        _assessment_args=assessment_args,
        _response=response,
        _materialization=materialization,
        _send_preparation=send_preparation,
        _limits=limits,
        _composed_at=composed_at,
        _expires_at=expires_at,
        _context_fingerprint=fingerprint,
        _token=_CONTEXT_TOKEN,
    )


def prepare_guided_provider_pre_execution(
    context: GuidedProviderPreExecutionContext,
    credential_resolver: GuidedProviderCredentialResolver,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> GuidedProviderPreExecution:
    """Resolve credentials and construct descriptors, then recheck the chain."""

    if type(context) is not GuidedProviderPreExecutionContext:
        raise TypeError("context must be an exact pre-execution context")
    resolve = getattr(credential_resolver, "resolve", None)
    if not callable(resolve):
        raise TypeError("credential_resolver must expose resolve(slot)")
    started_at = _trusted_clock(clock)
    if started_at < context._composed_at:
        raise ValueError("Trusted clock rolled back before credential binding")
    _assess_context(context, preimages=preimages, evaluation_at=started_at)
    requests_without_lease = tuple(
        _build_private_http_shape(binding)
        for binding in context._send_preparation._bindings
    )
    _claim_phase529_consent(context, evaluation_at=started_at)
    slots = tuple(
        sorted(
            {item.credential_slot for item in context._send_preparation._bindings},
            key=lambda item: item.value,
        )
    )
    values: dict[GuidedProviderRequestCredentialSlot, str] = {}
    lease: _EphemeralCredentialLease | None = None
    completed = False
    try:
        for slot in slots:
            resolve_failed = False
            value: object | None = None
            try:
                value = resolve(slot)
            except Exception:
                resolve_failed = True
            if resolve_failed:
                raise GuidedProviderCredentialBindingError(slot)
            if (
                type(value) is not str
                or not value
                or len(value.encode("utf-8")) > _MAX_CREDENTIAL_BYTES
                or value != value.strip()
                or any(ord(char) < 33 or ord(char) == 127 for char in value)
            ):
                raise GuidedProviderCredentialBindingError(slot)
            values[slot] = value

        finished_at = _trusted_clock(clock)
        if finished_at < started_at:
            raise ValueError(
                "Trusted clock rolled back during request construction"
            )
        _assess_context(context, preimages=preimages, evaluation_at=finished_at)
        if finished_at >= context._expires_at:
            raise ValueError(
                "Provider execution context expired during preparation"
            )
        final_shapes = tuple(
            _build_private_http_shape(binding)
            for binding in context._send_preparation._bindings
        )
        if final_shapes != requests_without_lease:
            raise ValueError(
                "Allowlisted request shape drifted during preparation"
            )
        lease = _EphemeralCredentialLease(
            values,
            bound_at=finished_at,
            expires_at=context._expires_at,
            context_fingerprint=context._context_fingerprint,
            _token=_LEASE_TOKEN,
        )
        prepared_requests = tuple(
            _prepared_request_from_shape(
                binding,
                shape,
                lease,
                context_fingerprint=context._context_fingerprint,
            )
            for binding, shape in zip(
                context._send_preparation._bindings,
                requests_without_lease,
                strict=True,
            )
        )
        bundle = GuidedProviderPreExecution(
            _requests=prepared_requests,
            _credential_lease=lease,
            _claim=_SingleUseExecutionClaim(),
            _limits=context._limits,
            _prepared_at=finished_at,
            _expires_at=context._expires_at,
            _context_fingerprint=context._context_fingerprint,
            _token=_PRE_EXECUTION_TOKEN,
        )
        completed = True
        return bundle
    finally:
        value = None
        for slot in tuple(values):
            values[slot] = ""
        values.clear()
        if lease is not None and not completed:
            lease._clear(_token=_CREDENTIAL_ACCESS_TOKEN)


def assess_guided_provider_pre_execution(
    context: GuidedProviderPreExecutionContext,
    pre_execution: GuidedProviderPreExecution,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderPreExecutionReview:
    """Rebuild and revalidate an unclaimed bundle at trusted current UTC."""

    if type(context) is not GuidedProviderPreExecutionContext:
        raise TypeError("context must be an exact pre-execution context")
    if type(pre_execution) is not GuidedProviderPreExecution:
        raise TypeError("pre_execution must be an exact bundle")
    evaluated_at = _utc_datetime(evaluation_at, "evaluation_at")
    if evaluated_at < pre_execution._prepared_at:
        raise ValueError("evaluation_at cannot precede request preparation")
    if evaluated_at >= pre_execution._expires_at:
        raise ValueError("Pre-execution bundle expired")
    if pre_execution._claim.claimed:
        raise ValueError("Pre-execution bundle was already claimed")
    _validate_runtime_bundle(context, pre_execution)
    _assess_context(
        context,
        preimages=preimages,
        evaluation_at=pre_execution._prepared_at,
    )
    _assess_context(context, preimages=preimages, evaluation_at=evaluated_at)
    if (
        pre_execution._context_fingerprint != context._context_fingerprint
        or pre_execution._expires_at != context._expires_at
        or pre_execution._limits != context._limits
        or len(pre_execution._requests)
        != len(context._send_preparation._bindings)
    ):
        raise ValueError("Pre-execution bundle differs from composed context")
    expected_shapes = tuple(
        _build_private_http_shape(binding)
        for binding in context._send_preparation._bindings
    )
    for actual, binding, shape in zip(
        pre_execution._requests,
        context._send_preparation._bindings,
        expected_shapes,
        strict=True,
    ):
        if (
            actual._binding is not binding
            or actual._contract is not binding._contract
            or _prepared_request_record(actual)
            != _prepared_request_record(
                _prepared_request_from_shape(
                    binding,
                    shape,
                    pre_execution._credential_lease,
                    context_fingerprint=context._context_fingerprint,
                )
            )
        ):
            raise ValueError("Prepared HTTP request differs from exact binding")
    return GuidedProviderPreExecutionReview(
        status=(
            GuidedProviderPreExecutionStatus
            .READY_FOR_BOUNDED_PROVIDER_EXECUTION
        ),
        next_action=_NEXT_ACTION,
        request_count=pre_execution.request_count,
        _safe_payload_json=json.dumps(
            _safe_pre_execution_review(pre_execution),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        _token=_REVIEW_TOKEN,
    )


def _assess_context(
    context: GuidedProviderPreExecutionContext,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderRequestLiveCredentialBindingResponseReview:
    assessed = assess_guided_provider_request_live_credential_binding_response(
        *context._assessment_args,
        context._response,
        preimages=preimages,
        evaluation_at=evaluation_at,
    )
    _require_accepted_response(assessed)
    if _limits_from_assessment(assessed) != context._limits:
        raise ValueError("Accepted provider execution limits drifted")
    return assessed


def _validate_runtime_bundle(
    context: GuidedProviderPreExecutionContext,
    pre_execution: GuidedProviderPreExecution,
) -> None:
    lease = pre_execution._credential_lease
    expected_slots = tuple(
        sorted(
            {item.credential_slot for item in pre_execution._requests},
            key=lambda item: item.value,
        )
    )
    if (
        pre_execution._context_fingerprint != context._context_fingerprint
        or lease._context_fingerprint != context._context_fingerprint
        or lease._bound_at != pre_execution._prepared_at
        or lease._expires_at != pre_execution._expires_at
        or lease.slots != expected_slots
        or any(
            item._credential_lease is not lease
            for item in pre_execution._requests
        )
    ):
        raise ValueError("Credential lease differs from exact prepared bundle")


def _claim_phase529_consent(
    context: GuidedProviderPreExecutionContext,
    *,
    evaluation_at: datetime,
) -> None:
    evaluated = _utc_datetime(evaluation_at, "evaluation_at")
    fingerprint = _digest(
        context._response._context_fingerprint,
        "response_context_fingerprint",
    )
    with _CONSENT_CLAIM_LOCK:
        expired = tuple(
            item
            for item, expires_at in _CONSENT_CLAIMS.items()
            if expires_at <= evaluated
        )
        for item in expired:
            del _CONSENT_CLAIMS[item]
        if fingerprint in _CONSENT_CLAIMS:
            raise ValueError("Exact Phase 5.29 consent was already consumed")
        if len(_CONSENT_CLAIMS) >= _MAX_CONSENT_CLAIMS:
            raise RuntimeError(
                "Process-local consent claim capacity was exhausted"
            )
        _CONSENT_CLAIMS[fingerprint] = context._expires_at


def _require_accepted_response(
    review: GuidedProviderRequestLiveCredentialBindingResponseReview,
) -> None:
    if (
        type(review)
        is not GuidedProviderRequestLiveCredentialBindingResponseReview
        or review.status
        is not GuidedProviderRequestLiveCredentialBindingResponseStatus
        .READY_FOR_PRIVATE_PROVIDER_REQUEST_EPHEMERAL_CREDENTIAL_VALUE_BINDING_GATE
        or review.response_kind
        is not GuidedProviderRequestLiveCredentialBindingResponseKind
        .ACCEPT_LIVE_CREDENTIAL_BINDING
        or review.next_action
        != "prepare_private_provider_request_ephemeral_credential_value_binding_gate"
    ):
        raise ValueError("Pre-execution requires exact fresh Phase 5.29 consent")


def _limits_from_assessment(
    review: GuidedProviderRequestLiveCredentialBindingResponseReview,
) -> _ExecutionLimits:
    send_review = (
        review._live_credential_binding_review
        ._credential_binding_response_review
        ._send_authorization_response_review
    )
    return _ExecutionLimits(
        accepted_max_request_count=send_review.accepted_max_request_count,
        bound_google_request_count=send_review.bound_google_request_count,
        bound_serpapi_request_count=send_review.bound_serpapi_request_count,
        estimated_google_cost_usd_micros=(
            send_review.estimated_bound_first_paid_tier_google_cost_usd_micros
        ),
        accepted_max_google_cost_usd_micros=(
            send_review.accepted_max_first_paid_tier_google_cost_usd_micros
        ),
        bound_serpapi_credit_count=send_review.serpapi_bound_plan_credit_count,
        serpapi_credit_cap=send_review.serpapi_plan_credit_cap,
    )


_PrivateHttpShape = tuple[
    str,
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    bytes | None,
]


def _build_private_http_shape(
    binding: GuidedProviderRequestTransportBinding,
) -> _PrivateHttpShape:
    if type(binding) is not GuidedProviderRequestTransportBinding:
        raise TypeError("Transport binding must be exact")
    contract = binding._contract
    transmitted = dict(contract._provider_transmitted_values)
    identifiers = dict(contract._provider_identifier_values)
    kind = contract.materialization_kind
    if kind is GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH:
        return _build_google_places_text_search(binding, transmitted, identifiers)
    if kind is GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS:
        return _build_google_place_details(binding, transmitted, identifiers)
    if kind is GuidedProviderRequestMaterializationKind.GOOGLE_ROUTES_COMPUTE_ROUTES:
        return _build_google_routes(binding, transmitted, identifiers)
    if kind is GuidedProviderRequestMaterializationKind.SERPAPI_GOOGLE_HOTELS:
        return _build_serpapi_google_hotels(binding, transmitted, identifiers)
    raise ValueError("Unsupported allowlisted transport profile")


def _build_google_places_text_search(
    binding: GuidedProviderRequestTransportBinding,
    transmitted: dict[str, object],
    identifiers: dict[str, str],
) -> _PrivateHttpShape:
    required = {"field_mask", "language_code", "region_code", "text_query"}
    optional = {"latitude", "longitude", "radius_m"}
    if identifiers or not required.issubset(transmitted) or not set(transmitted) <= required | optional:
        raise ValueError("Text Search contract fields differ")
    if bool(set(transmitted) & optional) != optional.issubset(transmitted):
        raise ValueError("Text Search location bias must be complete")
    body: dict[str, object] = {
        "languageCode": _wire_text(transmitted["language_code"]),
        "pageSize": 5,
        "regionCode": _wire_text(transmitted["region_code"]),
        "textQuery": _wire_text(transmitted["text_query"]),
    }
    if optional.issubset(transmitted):
        body["locationBias"] = {
            "circle": {
                "center": {
                    "latitude": _wire_number(transmitted["latitude"]),
                    "longitude": _wire_number(transmitted["longitude"]),
                },
                "radius": _wire_number(transmitted["radius_m"]),
            }
        }
    return (
        _fixed_endpoint(binding, "places.googleapis.com"),
        (("Content-Type", "application/json"), ("X-Goog-FieldMask", _wire_field_mask(transmitted["field_mask"]))),
        (),
        _json_bytes(body),
    )


def _build_google_place_details(
    binding: GuidedProviderRequestTransportBinding,
    transmitted: dict[str, object],
    identifiers: dict[str, str],
) -> _PrivateHttpShape:
    if set(transmitted) != {"field_mask", "language_code", "region_code"} or set(identifiers) != {"provider_place_id"}:
        raise ValueError("Place Details contract fields differ")
    provider_id = _wire_text(identifiers["provider_place_id"])
    endpoint = binding.endpoint_template.replace(
        "{provider_place_id}", quote(provider_id, safe="")
    )
    _validate_https_endpoint(endpoint, "places.googleapis.com")
    return (
        endpoint,
        (("X-Goog-FieldMask", _wire_text(transmitted["field_mask"])),),
        tuple(
            sorted(
                (
                    ("languageCode", _wire_text(transmitted["language_code"])),
                    ("regionCode", _wire_text(transmitted["region_code"])),
                )
            )
        ),
        None,
    )


def _build_google_routes(
    binding: GuidedProviderRequestTransportBinding,
    transmitted: dict[str, object],
    identifiers: dict[str, str],
) -> _PrivateHttpShape:
    if set(transmitted) != {"departure_at", "field_mask", "mode"} or set(identifiers) != {"destination_provider_place_id", "origin_provider_place_id"}:
        raise ValueError("Routes contract fields differ")
    mode = {
        "driving": "DRIVE",
        "walking": "WALK",
        "transit": "TRANSIT",
        "bicycling": "BICYCLE",
        "two_wheeler": "TWO_WHEELER",
    }.get(_wire_text(transmitted["mode"]))
    if mode is None:
        raise ValueError("Routes travel mode is not allowlisted")
    body = {
        "computeAlternativeRoutes": False,
        "departureTime": _wire_text(transmitted["departure_at"]),
        "destination": {
            "placeId": _wire_text(identifiers["destination_provider_place_id"])
        },
        "origin": {
            "placeId": _wire_text(identifiers["origin_provider_place_id"])
        },
        "travelMode": mode,
    }
    return (
        _fixed_endpoint(binding, "routes.googleapis.com"),
        (("Content-Type", "application/json"), ("X-Goog-FieldMask", _wire_text(transmitted["field_mask"]))),
        (),
        _json_bytes(body),
    )


def _build_serpapi_google_hotels(
    binding: GuidedProviderRequestTransportBinding,
    transmitted: dict[str, object],
    identifiers: dict[str, str],
) -> _PrivateHttpShape:
    expected = {
        "adults",
        "check_in",
        "check_out",
        "children",
        "currency",
        "language",
        "query",
        "region",
        "rooms",
    }
    if set(transmitted) != expected or identifiers:
        raise ValueError("SerpAPI hotel contract fields differ")
    names = {
        "adults": "adults",
        "check_in": "check_in_date",
        "check_out": "check_out_date",
        "children": "children",
        "currency": "currency",
        "language": "hl",
        "query": "q",
        "region": "gl",
        "rooms": "rooms",
    }
    query_parameters = [("engine", "google_hotels")]
    query_parameters.extend(
        (provider_name, _wire_text(transmitted[contract_name]))
        for contract_name, provider_name in names.items()
    )
    return (
        _fixed_endpoint(binding, "serpapi.com"),
        (),
        tuple(sorted(query_parameters)),
        None,
    )


def _prepared_request_from_shape(
    binding: GuidedProviderRequestTransportBinding,
    shape: _PrivateHttpShape,
    lease: _EphemeralCredentialLease,
    *,
    context_fingerprint: str,
) -> GuidedProviderPreparedRequest:
    endpoint, headers, query_parameters, body = shape
    contract = binding._contract
    fingerprint = _sha256(
        {
            "contract_version": GUIDED_PROVIDER_PRE_EXECUTION_VERSION,
            "domain": "guided-provider-prepared-request",
            "pre_execution_context_fingerprint": _digest(
                context_fingerprint,
                "context_fingerprint",
            ),
            "binding": _private_context_value(binding),
            "endpoint": endpoint,
            "headers": headers,
            "query_parameters": query_parameters,
            "json_body_digest": (
                hashlib.sha256(body).hexdigest() if body is not None else None
            ),
            "local_result_binding": _private_context_value(
                contract._local_result_binding
            ),
            "credential_slot": binding.credential_slot.value,
        }
    )
    return GuidedProviderPreparedRequest(
        transport_profile=binding.transport_profile,
        materialization_kind=binding.materialization_kind,
        http_method=binding.http_method,
        endpoint_template=binding.endpoint_template,
        credential_slot=binding.credential_slot,
        credential_provider_field_name=binding.credential_provider_field_name,
        credential_placement=binding.credential_placement,
        estimated_google_cost_usd_micros=_request_cost(binding=binding)[0],
        serpapi_credit_count=_request_cost(binding=binding)[1],
        _request_fingerprint=fingerprint,
        _endpoint=endpoint,
        _headers=headers,
        _query_parameters=query_parameters,
        _json_body=body,
        _local_result_binding=contract._local_result_binding,
        _binding=binding,
        _contract=contract,
        _credential_lease=lease,
        _token=_REQUEST_TOKEN,
    )


def _validate_private_http_shape(request: GuidedProviderPreparedRequest) -> None:
    parsed = urlsplit(request._endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Prepared request endpoint is invalid")
    if request.credential_provider_field_name in {
        name for name, _ in (*request._headers, *request._query_parameters)
    }:
        raise ValueError("Credential value entered the public request shape")
    for collection in (request._headers, request._query_parameters):
        if (
            not isinstance(collection, tuple)
            or len(set(name for name, _ in collection)) != len(collection)
            or any(type(name) is not str or type(value) is not str or not name for name, value in collection)
        ):
            raise ValueError("Prepared request fields are invalid")
    if request.http_method is GuidedProviderRequestHTTPMethod.POST:
        if request._json_body is None:
            raise ValueError("POST request requires a JSON body")
    elif request._json_body is not None:
        raise ValueError("GET request cannot carry a JSON body")


def _prepared_request_record(request: GuidedProviderPreparedRequest) -> tuple[object, ...]:
    return (
        request.transport_profile,
        request.materialization_kind,
        request.http_method,
        request.endpoint_template,
        request.credential_slot,
        request.credential_provider_field_name,
        request.credential_placement,
        request.estimated_google_cost_usd_micros,
        request.serpapi_credit_count,
        request._request_fingerprint,
        request._endpoint,
        request._headers,
        request._query_parameters,
        request._json_body,
        request._local_result_binding,
    )


def _fixed_endpoint(
    binding: GuidedProviderRequestTransportBinding,
    hostname: str,
) -> str:
    endpoint = binding.endpoint_template
    if "{" in endpoint or "}" in endpoint:
        raise ValueError("Fixed endpoint contains an unresolved path value")
    _validate_https_endpoint(endpoint, hostname)
    return endpoint


def _request_cost(
    *,
    binding: GuidedProviderRequestTransportBinding,
) -> tuple[int, int]:
    return {
        GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH: (
            32_000,
            0,
        ),
        GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS: (
            20_000,
            0,
        ),
        GuidedProviderRequestMaterializationKind.GOOGLE_ROUTES_COMPUTE_ROUTES: (
            5_000,
            0,
        ),
        GuidedProviderRequestMaterializationKind.SERPAPI_GOOGLE_HOTELS: (
            0,
            1,
        ),
    }[binding.materialization_kind]


def _validate_https_endpoint(endpoint: str, hostname: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != hostname
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Provider endpoint is outside its allowlist")


def _wire_text(value: object) -> str:
    if type(value) is str:
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Provider request text is invalid")
        return value
    if type(value) in {int, float} and not isinstance(value, bool):
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Provider request number is invalid")
        return str(value)
    raise TypeError("Provider request value cannot be encoded as text")


def _wire_field_mask(value: object) -> str:
    if type(value) is str:
        return _wire_text(value)
    if (
        isinstance(value, tuple)
        and value
        and all(type(item) is str and item for item in value)
    ):
        return ",".join(_wire_text(item) for item in value)
    raise TypeError("Provider field mask is invalid")


def _wire_number(value: object) -> int | float:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise TypeError("Provider request number must be exact")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Provider request number must be finite")
    return value


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _trusted_clock(clock: Callable[[], datetime]) -> datetime:
    if not callable(clock):
        raise TypeError("clock must be callable")
    return _utc_datetime(clock(), "clock result")


def _sha256(value: object) -> str:
    encoded = json.dumps(
        _private_context_value(value),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _utc_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{name} must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    if normalized.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must normalize to UTC")
    return normalized


__all__ = [
    "GUIDED_PROVIDER_PRE_EXECUTION_VERSION",
    "GuidedProviderCredentialBindingError",
    "GuidedProviderCredentialResolver",
    "GuidedProviderPreExecution",
    "GuidedProviderPreExecutionContext",
    "GuidedProviderPreExecutionReview",
    "GuidedProviderPreExecutionStatus",
    "GuidedProviderPreparedRequest",
    "assess_guided_provider_pre_execution",
    "compose_guided_provider_pre_execution_context",
    "prepare_guided_provider_pre_execution",
]
