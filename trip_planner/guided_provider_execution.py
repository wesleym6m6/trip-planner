"""Execute one Phase 5.30 bundle under bounded, injected transport control.

Phase 5.31 is the only guided macro phase allowed to cross the provider-call
boundary.  The module has no built-in HTTP client: a host injects an exact
transport.  Every send reserves request, list-cost/credit, and deadline budget
first.  Transport delivery uncertainty is typed, unexpected exceptions are
sanitized as unknown, and an unknown request may be retried at most once using
the same credential-bearing wire object after every request received its first
attempt.

Responses of every HTTP status remain in a private, non-serializable
quarantine.  This module never constructs or authorizes evidence, merges an
EvidenceStore/EvidenceSession, writes a trip, builds a PlanPatch, schedules,
renders, or deploys.  Raw target preimage wrappers are re-supplied for exact
assessment and matching but are not retained; quarantine keeps only the exact
typed normalization target required by the provider-specific Phase 5.32 seam.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Any, Callable, Protocol
from urllib.parse import urlencode

from .guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetPreimage,
)
from .guided_provider_pre_execution import (
    GUIDED_PROVIDER_PRE_EXECUTION_VERSION,
    GuidedProviderPreExecution,
    GuidedProviderPreExecutionContext,
    GuidedProviderPreparedRequest,
    _CREDENTIAL_ACCESS_TOKEN,
    _EXECUTION_CLAIM_TOKEN,
    _SealedSlots,
    _assess_context,
    assess_guided_provider_pre_execution,
)
from .guided_provider_request_contract_materialization import _request_values
from .guided_provider_request_materialization_review import (
    GuidedProviderRequestMaterializationKind,
)
from .guided_provider_request_send_preparation import (
    GuidedProviderRequestCredentialSlot,
    GuidedProviderRequestHTTPMethod,
    GuidedProviderRequestTransportProfile,
    GuidedProviderRequestValuePlacement,
)
from .facts import (
    EvidenceSnapshot,
    FactContractError,
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
)
from .guided_provider_preflight import GuidedProviderPolicyProfile
from .lodging_discovery import LodgingDiscoveryRequest
from .models import DecisionState, EvidenceState
from .place_details import GooglePlaceDetailsRequest
from .places_identity import (
    PlaceIdentityIntent,
    PlaceIdentityRequest,
    build_google_place_identity_request,
)
from .routes import GoogleRouteRequest, google_route_request_is_executable_at


GUIDED_PROVIDER_EXECUTION_VERSION = "guided-provider-execution/v1"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_REQUESTS = 32
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_RESPONSE_HEADERS = 32
_MAX_HEADER_NAME_BYTES = 256
_MAX_HEADER_VALUE_BYTES = 4096
_MAX_TIMEOUT_SECONDS = 60.0
_QUARANTINE_TOKEN = object()
_WIRE_REQUEST_TOKEN = object()
_EXECUTION_TOKEN = object()


class GuidedProviderDeliveryCertainty(str, Enum):
    """Whether a sanitized transport failure could follow a provider send."""

    KNOWN_NOT_SENT = "known_not_sent"
    OUTCOME_UNKNOWN = "outcome_unknown"


class GuidedProviderTransportError(RuntimeError):
    """Sanitized host transport failure with explicit delivery certainty."""

    def __init__(self, certainty: GuidedProviderDeliveryCertainty) -> None:
        if type(certainty) is not GuidedProviderDeliveryCertainty:
            raise TypeError("delivery certainty must be exact")
        self.certainty = certainty
        super().__init__(f"Provider transport failed ({certainty.value}).")


class GuidedProviderTransportResponse(_SealedSlots):
    """Bounded raw HTTP response; bytes remain private until Phase 5.32."""

    __slots__ = ("status_code", "_body", "_headers")

    def __init__(
        self,
        *,
        status_code: int,
        body: bytes,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if (
            type(status_code) is not int
            or not 100 <= status_code <= 599
            or type(body) is not bytes
            or len(body) > _MAX_RESPONSE_BYTES
        ):
            raise ValueError("Provider transport response is outside bounds")
        raw_headers: object = headers
        if isinstance(raw_headers, dict):
            raw_headers = tuple(raw_headers.items())
        if (
            not isinstance(raw_headers, tuple)
            or len(raw_headers) > _MAX_RESPONSE_HEADERS
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                or not item[0]
                or len(item[0].encode("utf-8")) > _MAX_HEADER_NAME_BYTES
                or len(item[1].encode("utf-8")) > _MAX_HEADER_VALUE_BYTES
                or any(
                    ord(char) < 32 and char != "\t"
                    for value in item
                    for char in value
                )
                for item in raw_headers
            )
        ):
            raise ValueError("Provider transport response headers are invalid")
        self.status_code = status_code
        self._body = body
        self._headers = tuple(
            (name.lower(), value) for name, value in raw_headers
        )
        self._seal()

    def __repr__(self) -> str:
        return (
            "GuidedProviderTransportResponse("
            f"status_code={self.status_code!r}, body_bytes={len(self._body)!r})"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Provider transport responses are non-serializable")


class GuidedProviderTransport(Protocol):
    """Host-injected transport; no default network client is provided."""

    def send(
        self,
        request: GuidedProviderWireRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
        deadline_at: datetime,
        max_response_bytes: int,
        max_response_headers: int,
        max_header_name_bytes: int,
        max_header_value_bytes: int,
    ) -> GuidedProviderTransportResponse:
        """Stream one request under exact time and response-buffer caps."""


class GuidedProviderWireRequest(_SealedSlots):
    """Credential-bearing runtime object reused exactly for an unknown retry."""

    __slots__ = (
        "request_index",
        "transport_profile",
        "http_method",
        "_url",
        "_headers",
        "_body",
        "_cleared",
    )

    def __init__(
        self,
        *,
        request_index: int,
        transport_profile: GuidedProviderRequestTransportProfile,
        http_method: GuidedProviderRequestHTTPMethod,
        url: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes | None,
        _token: object | None = None,
    ) -> None:
        if _token is not _WIRE_REQUEST_TOKEN:
            raise ValueError("Wire requests require the bounded executor")
        if (
            type(request_index) is not int
            or not 0 <= request_index < _MAX_REQUESTS
            or type(transport_profile)
            is not GuidedProviderRequestTransportProfile
            or type(http_method) is not GuidedProviderRequestHTTPMethod
            or type(url) is not str
            or not url.startswith("https://")
            or not isinstance(headers, tuple)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                for item in headers
            )
            or body is not None and type(body) is not bytes
        ):
            raise ValueError("Wire request contents are invalid")
        self.request_index = request_index
        self.transport_profile = transport_profile
        self.http_method = http_method
        self._url = url
        self._headers = headers
        self._body = body
        self._cleared = False
        self._seal()

    @property
    def url(self) -> str:
        self._require_live()
        return self._url

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        self._require_live()
        return self._headers

    @property
    def body(self) -> bytes | None:
        self._require_live()
        return self._body

    def _require_live(self) -> None:
        if self._cleared:
            raise RuntimeError("Wire request was cleared")

    def _clear(self) -> None:
        if self._cleared:
            return
        object.__setattr__(self, "_url", "")
        object.__setattr__(
            self,
            "_headers",
            tuple((name, "") for name, _ in self._headers),
        )
        object.__setattr__(self, "_headers", ())
        if self._body is not None:
            object.__setattr__(self, "_body", bytes(len(self._body)))
        object.__setattr__(self, "_body", None)
        object.__setattr__(self, "_cleared", True)

    def __repr__(self) -> str:
        return (
            "GuidedProviderWireRequest("
            f"request_index={self.request_index!r}, "
            f"transport_profile={self.transport_profile.value!r}, "
            f"http_method={self.http_method.value!r}, "
            f"cleared={self._cleared!r})"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Wire requests are non-serializable")


@dataclass(frozen=True, slots=True)
class GuidedProviderExecutionLimits:
    """Caller-selected caps that may only tighten accepted Phase 5.30 limits."""

    max_attempt_count: int
    max_google_cost_usd_micros: int
    max_serpapi_credit_count: int
    connect_timeout_s: float
    read_timeout_s: float
    deadline_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.max_attempt_count) is not int
            or not 1 <= self.max_attempt_count <= _MAX_REQUESTS
            or type(self.max_google_cost_usd_micros) is not int
            or self.max_google_cost_usd_micros < 0
            or type(self.max_serpapi_credit_count) is not int
            or not 0 <= self.max_serpapi_credit_count <= _MAX_REQUESTS
        ):
            raise ValueError("Provider execution caps are invalid")
        for name, value in (
            ("connect_timeout_s", self.connect_timeout_s),
            ("read_timeout_s", self.read_timeout_s),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 < float(value) <= _MAX_TIMEOUT_SECONDS
            ):
                raise ValueError(f"{name} is invalid")
        object.__setattr__(
            self,
            "deadline_at",
            _utc_datetime(self.deadline_at, "deadline_at"),
        )


class GuidedProviderExecutionProblemCode(str, Enum):
    ATTEMPT_CAP_EXHAUSTED = "attempt_cap_exhausted"
    GOOGLE_COST_CAP_EXHAUSTED = "google_cost_cap_exhausted"
    SERPAPI_CREDIT_CAP_EXHAUSTED = "serpapi_credit_cap_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    TRUSTED_CLOCK_INVALID = "trusted_clock_invalid"
    TRANSPORT_KNOWN_NOT_SENT = "transport_known_not_sent"
    TRANSPORT_OUTCOME_UNKNOWN = "transport_outcome_unknown"
    INVALID_TRANSPORT_RESPONSE = "invalid_transport_response"
    PREPARED_REQUEST_DRIFT = "prepared_request_drift"
    NORMALIZATION_TARGET_DRIFT = "normalization_target_drift"
    NORMALIZATION_TARGET_STALE = "normalization_target_stale"
    UNKNOWN_RETRY_NOT_AVAILABLE = "unknown_retry_not_available"


class GuidedProviderRequestExecutionOutcomeKind(str, Enum):
    RESPONSE_QUARANTINED = "response_quarantined"
    KNOWN_NOT_SENT = "known_not_sent"
    OUTCOME_UNKNOWN = "outcome_unknown"
    NOT_ATTEMPTED = "not_attempted"


class GuidedProviderExecutionStatus(str, Enum):
    ALL_RESPONSES_QUARANTINED = "all_responses_quarantined"
    PARTIAL = "partial"
    OUTCOME_UNKNOWN = "outcome_unknown"
    NO_RESPONSES = "no_responses"


class GuidedProviderQuarantinedResponse(_SealedSlots):
    """Exact private raw response plus provider-specific normalization source."""

    __slots__ = (
        "request_index",
        "transport_profile",
        "materialization_kind",
        "status_code",
        "attempt_number",
        "_sent_at",
        "retrieved_at",
        "_body",
        "_headers",
        "_prepared_request",
        "_normalization_target",
        "_prepared_request_runtime_fingerprint",
        "_response_fingerprint",
        "_target_fingerprint",
        "_source_binding_fingerprint",
        "_bundle_context_fingerprint",
    )

    def __init__(
        self,
        *,
        request_index: int,
        response: GuidedProviderTransportResponse,
        prepared_request: GuidedProviderPreparedRequest,
        transport_profile: GuidedProviderRequestTransportProfile,
        materialization_kind: GuidedProviderRequestMaterializationKind,
        prepared_request_runtime_fingerprint: str,
        source_binding_fingerprint: str,
        normalization_target: object,
        target_fingerprint: str,
        bundle_context_fingerprint: str,
        attempt_number: int,
        sent_at: datetime,
        retrieved_at: datetime,
        _token: object | None = None,
    ) -> None:
        if _token is not _QUARANTINE_TOKEN:
            raise ValueError("Quarantine requires the bounded executor")
        if (
            type(request_index) is not int
            or not 0 <= request_index < _MAX_REQUESTS
            or type(response) is not GuidedProviderTransportResponse
            or type(prepared_request) is not GuidedProviderPreparedRequest
            or type(transport_profile)
            is not GuidedProviderRequestTransportProfile
            or type(materialization_kind)
            is not GuidedProviderRequestMaterializationKind
            or type(attempt_number) is not int
            or not 1 <= attempt_number <= _MAX_REQUESTS
        ):
            raise TypeError("Quarantine sources must be exact")
        self.request_index = request_index
        self.transport_profile = transport_profile
        self.materialization_kind = materialization_kind
        self.status_code = response.status_code
        self.attempt_number = attempt_number
        self._sent_at = _utc_datetime(sent_at, "sent_at")
        self.retrieved_at = _utc_datetime(retrieved_at, "retrieved_at")
        if self.retrieved_at < self._sent_at:
            raise ValueError("Provider response predates its send")
        self._body = response._body
        self._headers = response._headers
        self._prepared_request = prepared_request
        self._normalization_target = normalization_target
        self._prepared_request_runtime_fingerprint = _digest(
            prepared_request_runtime_fingerprint,
            "prepared_request_runtime_fingerprint",
        )
        self._response_fingerprint = _quarantine_response_fingerprint(
            status_code=self.status_code,
            body=self._body,
            headers=self._headers,
            attempt_number=self.attempt_number,
            sent_at=self._sent_at,
            retrieved_at=self.retrieved_at,
        )
        self._target_fingerprint = _digest(
            target_fingerprint,
            "target_fingerprint",
        )
        self._source_binding_fingerprint = _digest(
            source_binding_fingerprint, "source_binding_fingerprint"
        )
        self._bundle_context_fingerprint = _digest(
            bundle_context_fingerprint,
            "bundle_context_fingerprint",
        )
        self._seal()

    def __repr__(self) -> str:
        return (
            "GuidedProviderQuarantinedResponse("
            f"request_index={self.request_index!r}, "
            f"transport_profile={self.transport_profile.value!r}, "
            f"status_code={self.status_code!r}, "
            f"attempt_number={self.attempt_number!r})"
        )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "request_index": self.request_index,
            "transport_profile": self.transport_profile.value,
            "materialization_kind": self.materialization_kind.value,
            "status_code": self.status_code,
            "attempt_number": self.attempt_number,
            "send_time_exposed": False,
            "raw_body_exposed": False,
            "raw_headers_exposed": False,
            "normalization_target_exposed": False,
            "prepared_request_runtime_fingerprint_exposed": False,
            "response_fingerprint_exposed": False,
            "target_fingerprint_exposed": False,
            "source_binding_fingerprint_exposed": False,
            "bundle_context_fingerprint_exposed": False,
            "facts_provider_request_fingerprint_exposed": False,
            "authorized_evidence_created": False,
            "supports_authoritative_use": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Quarantined provider responses are non-serializable")


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProviderRequestExecutionOutcome:
    kind: GuidedProviderRequestExecutionOutcomeKind
    request_index: int
    transport_profile: GuidedProviderRequestTransportProfile
    attempts_used: int
    retry_count: int
    unknown_attempt_count: int
    problem_code: GuidedProviderExecutionProblemCode | None
    _quarantine: GuidedProviderQuarantinedResponse | None = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not GuidedProviderRequestExecutionOutcomeKind
            or type(self.request_index) is not int
            or not 0 <= self.request_index < _MAX_REQUESTS
            or type(self.transport_profile)
            is not GuidedProviderRequestTransportProfile
            or type(self.attempts_used) is not int
            or not 0 <= self.attempts_used <= 2
            or type(self.retry_count) is not int
            or not 0 <= self.retry_count <= 1
            or type(self.unknown_attempt_count) is not int
            or not 0 <= self.unknown_attempt_count <= self.attempts_used
            or self.problem_code is not None
            and type(self.problem_code) is not GuidedProviderExecutionProblemCode
            or self._quarantine is not None
            and type(self._quarantine) is not GuidedProviderQuarantinedResponse
        ):
            raise ValueError("Provider execution outcome is invalid")
        has_quarantine = self._quarantine is not None
        if has_quarantine is not (
            self.kind
            is GuidedProviderRequestExecutionOutcomeKind.RESPONSE_QUARANTINED
        ):
            raise ValueError("Provider execution quarantine differs from kind")
        if self.retry_count > 0 and self.attempts_used < 2:
            raise ValueError("Provider retry count differs from attempts")

    def __repr__(self) -> str:
        return (
            "GuidedProviderRequestExecutionOutcome("
            f"kind={self.kind.value!r}, request_index={self.request_index!r}, "
            f"attempts_used={self.attempts_used!r})"
        )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "request_index": self.request_index,
            "transport_profile": self.transport_profile.value,
            "attempts_used": self.attempts_used,
            "retry_count": self.retry_count,
            "unknown_attempt_count": self.unknown_attempt_count,
            "problem_code": (
                self.problem_code.value if self.problem_code is not None else None
            ),
            "response_quarantined": self._quarantine is not None,
            "quarantine": (
                self._quarantine.to_safe_dict()
                if self._quarantine is not None
                else None
            ),
            "raw_provider_values_exposed": False,
        }


class GuidedProviderExecution(_SealedSlots):
    """Safe aggregate with private quarantine and no evidence/canonical seam."""

    __slots__ = (
        "status",
        "next_action",
        "attempts_used",
        "reserved_google_cost_usd_micros",
        "reserved_serpapi_credit_count",
        "_outcomes",
        "_started_at",
        "_completed_at",
    )

    def __init__(
        self,
        *,
        status: GuidedProviderExecutionStatus,
        next_action: str,
        outcomes: tuple[GuidedProviderRequestExecutionOutcome, ...],
        attempts_used: int,
        reserved_google_cost_usd_micros: int,
        reserved_serpapi_credit_count: int,
        started_at: datetime,
        completed_at: datetime,
        _token: object | None = None,
    ) -> None:
        if _token is not _EXECUTION_TOKEN:
            raise ValueError("Provider executions require the bounded executor")
        if (
            type(status) is not GuidedProviderExecutionStatus
            or type(next_action) is not str
            or not next_action
            or not isinstance(outcomes, tuple)
            or not outcomes
            or any(
                type(item) is not GuidedProviderRequestExecutionOutcome
                for item in outcomes
            )
            or tuple(item.request_index for item in outcomes)
            != tuple(range(len(outcomes)))
            or type(attempts_used) is not int
            or attempts_used != sum(item.attempts_used for item in outcomes)
            or type(reserved_google_cost_usd_micros) is not int
            or reserved_google_cost_usd_micros < 0
            or type(reserved_serpapi_credit_count) is not int
            or reserved_serpapi_credit_count < 0
        ):
            raise ValueError("Provider execution aggregate is invalid")
        started = _utc_datetime(started_at, "started_at")
        completed = _utc_datetime(completed_at, "completed_at")
        if completed < started:
            raise ValueError("Provider execution clock rolled back")
        self.status = status
        self.next_action = next_action
        self.attempts_used = attempts_used
        self.reserved_google_cost_usd_micros = (
            reserved_google_cost_usd_micros
        )
        self.reserved_serpapi_credit_count = reserved_serpapi_credit_count
        self._outcomes = outcomes
        self._started_at = started
        self._completed_at = completed
        self._seal()

    @property
    def request_count(self) -> int:
        return len(self._outcomes)

    @property
    def quarantine_count(self) -> int:
        return sum(item._quarantine is not None for item in self._outcomes)

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedProviderExecution("
            f"status={self.status.value!r}, request_count={self.request_count!r}, "
            f"attempts_used={self.attempts_used!r}, "
            f"quarantine_count={self.quarantine_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        counts = {
            kind.value: sum(item.kind is kind for item in self._outcomes)
            for kind in GuidedProviderRequestExecutionOutcomeKind
        }
        return {
            "contract_version": GUIDED_PROVIDER_EXECUTION_VERSION,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "provider_execution": {
                "phase530_bundle_claim_consumed": True,
                "request_count": self.request_count,
                "attempts_used": self.attempts_used,
                "reserved_google_cost_usd_micros": (
                    self.reserved_google_cost_usd_micros
                ),
                "reserved_serpapi_credit_count": (
                    self.reserved_serpapi_credit_count
                ),
                "quarantine_count": self.quarantine_count,
                "outcome_counts": counts,
                "outcomes": [item.to_safe_dict() for item in self._outcomes],
                "initial_pass_completed_before_unknown_retries": True,
                "unknown_retry_maximum_per_request": 1,
                "unknown_retry_reused_same_wire_object": True,
                "all_attempts_reserved_before_send": True,
                "credentials_cleared_after_execution": True,
                "raw_provider_values_in_safe_output": False,
                "authorized_evidence_created": False,
                "evidence_merged": False,
                "canonical_mutation_created": False,
                "writes_to_trip": False,
                "decision_state": DecisionState.CANDIDATE.value,
                "evidence_state": EvidenceState.UNVERIFIED.value,
                "supports_authoritative_use": False,
            },
            "tentative_fields": ["provider_execution_results"],
            "needs_verification": ["quarantined_provider_results"],
            "side_effects": {
                "process_local": True,
                "network_accessed_by_injected_transport": self.attempts_used > 0,
                "transport_attempts_reserved": self.attempts_used,
                "provider_responses_observed": self.quarantine_count,
                "provider_delivery_count_not_inferred": True,
                "responses_quarantined": self.quarantine_count,
                "credentials_cleared": True,
                "writes_to_evidence": False,
                "writes_to_trip": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Provider executions are non-serializable")


@dataclass(slots=True)
class _MutableOutcome:
    kind: GuidedProviderRequestExecutionOutcomeKind
    attempts_used: int = 0
    retry_count: int = 0
    unknown_attempt_count: int = 0
    problem_code: GuidedProviderExecutionProblemCode | None = None
    quarantine: GuidedProviderQuarantinedResponse | None = None
    retry_eligible: bool = False


class _RunClock:
    __slots__ = ("_clock", "_last")

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        started_at: datetime,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._last = started_at

    @property
    def last(self) -> datetime:
        return self._last

    def now(self) -> datetime:
        try:
            value = self._clock()
            result = _utc_datetime(value, "clock result")
        except Exception:
            raise ValueError("Trusted execution clock failed") from None
        if result < self._last:
            raise ValueError("Trusted execution clock rolled back")
        self._last = result
        return result


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedExecutionRequest:
    """Executor-owned copy of every field that may affect one send."""

    source: GuidedProviderPreparedRequest = field(repr=False)
    runtime_fingerprint: str
    transport_profile: GuidedProviderRequestTransportProfile
    materialization_kind: GuidedProviderRequestMaterializationKind
    http_method: GuidedProviderRequestHTTPMethod
    credential_slot: GuidedProviderRequestCredentialSlot
    credential_provider_field_name: str
    credential_placement: GuidedProviderRequestValuePlacement
    google_cost_usd_micros: int
    serpapi_credit_count: int
    endpoint: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    query_parameters: tuple[tuple[str, str], ...] = field(repr=False)
    json_body: bytes | None = field(repr=False)
    credential_lease: object = field(repr=False)
    source_binding_fingerprint: str


class _ExecutionBudget:
    __slots__ = (
        "_limits",
        "_expires_at",
        "_attempts_used",
        "_google_cost",
        "_serpapi_credits",
        "_lock",
    )

    def __init__(
        self,
        limits: GuidedProviderExecutionLimits,
        *,
        expires_at: datetime,
    ) -> None:
        self._limits = limits
        self._expires_at = expires_at
        self._attempts_used = 0
        self._google_cost = 0
        self._serpapi_credits = 0
        self._lock = Lock()

    @property
    def attempts_used(self) -> int:
        with self._lock:
            return self._attempts_used

    @property
    def google_cost(self) -> int:
        with self._lock:
            return self._google_cost

    @property
    def serpapi_credits(self) -> int:
        with self._lock:
            return self._serpapi_credits

    def reserve(
        self,
        *,
        now: datetime,
        google_cost_usd_micros: int,
        serpapi_credit_count: int,
    ) -> GuidedProviderExecutionProblemCode | None:
        with self._lock:
            timeout_window = timedelta(
                seconds=float(self._limits.connect_timeout_s)
                + float(self._limits.read_timeout_s)
            )
            if (
                now >= self._limits.deadline_at
                or now >= self._expires_at
                or now + timeout_window > self._limits.deadline_at
                or now + timeout_window > self._expires_at
            ):
                return GuidedProviderExecutionProblemCode.DEADLINE_EXCEEDED
            if self._attempts_used >= self._limits.max_attempt_count:
                return GuidedProviderExecutionProblemCode.ATTEMPT_CAP_EXHAUSTED
            if (
                self._google_cost
                + google_cost_usd_micros
                > self._limits.max_google_cost_usd_micros
            ):
                return (
                    GuidedProviderExecutionProblemCode
                    .GOOGLE_COST_CAP_EXHAUSTED
                )
            if (
                self._serpapi_credits + serpapi_credit_count
                > self._limits.max_serpapi_credit_count
            ):
                return (
                    GuidedProviderExecutionProblemCode
                    .SERPAPI_CREDIT_CAP_EXHAUSTED
                )
            self._attempts_used += 1
            self._google_cost += google_cost_usd_micros
            self._serpapi_credits += serpapi_credit_count
            return None

    def completion_problem(
        self,
        *,
        now: datetime,
    ) -> GuidedProviderExecutionProblemCode | None:
        with self._lock:
            if now >= self._limits.deadline_at or now >= self._expires_at:
                return GuidedProviderExecutionProblemCode.DEADLINE_EXCEEDED
            return None


def execute_guided_provider_requests(
    context: GuidedProviderPreExecutionContext,
    pre_execution: GuidedProviderPreExecution,
    transport: GuidedProviderTransport,
    limits: GuidedProviderExecutionLimits,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    identity_snapshot: EvidenceSnapshot | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> GuidedProviderExecution:
    """Consume one bundle and execute a fair bounded initial/retry pass."""

    if type(context) is not GuidedProviderPreExecutionContext:
        raise TypeError("context must be an exact pre-execution context")
    if type(pre_execution) is not GuidedProviderPreExecution:
        raise TypeError("pre_execution must be an exact bundle")
    if type(limits) is not GuidedProviderExecutionLimits:
        raise TypeError("limits must be exact")
    send = getattr(transport, "send", None)
    if not callable(send):
        raise TypeError("transport must expose send(request, timeouts)")
    execution_limits = _copy_execution_limits(limits)
    started_at = _trusted_clock(clock)
    assess_guided_provider_pre_execution(
        context,
        pre_execution,
        preimages=preimages,
        evaluation_at=started_at,
    )
    _validate_limits(pre_execution, execution_limits, started_at=started_at)
    targets = _match_normalization_targets(
        pre_execution,
        preimages=preimages,
    )
    normalization_targets = _bind_normalization_targets(
        pre_execution._requests,
        targets,
        identity_snapshot=identity_snapshot,
        started_at=started_at,
    )
    execution_credential_lease = pre_execution._credential_lease
    prepared_requests = tuple(
        _capture_prepared_execution_request(request)
        for request in pre_execution._requests
    )
    if any(
        prepared.credential_lease is not execution_credential_lease
        for prepared in prepared_requests
    ):
        raise ValueError("Prepared requests differ from the execution lease")
    normalization_target_fingerprints = tuple(
        _target_fingerprint(
            prepared.source,
            target,
            bundle_context_fingerprint=pre_execution._context_fingerprint,
        )
        for prepared, target in zip(
            prepared_requests,
            normalization_targets,
            strict=True,
        )
    )
    if any(
        not _prepared_request_matches(prepared)
        for prepared in prepared_requests
    ):
        raise ValueError("Prepared request drifted before execution")
    claimed = False
    wire_request_list: list[GuidedProviderWireRequest] = []
    try:
        pre_execution._claim.claim(_token=_EXECUTION_CLAIM_TOKEN)
        claimed = True
        run_clock = _RunClock(clock, started_at=started_at)
        budget = _ExecutionBudget(
            execution_limits,
            expires_at=pre_execution._expires_at,
        )
        wire_requests: tuple[GuidedProviderWireRequest, ...] = ()
        states = [
            _MutableOutcome(
                kind=GuidedProviderRequestExecutionOutcomeKind.NOT_ATTEMPTED
            )
            for _ in prepared_requests
        ]
        for index, prepared in enumerate(prepared_requests):
            if not _prepared_request_matches(prepared):
                raise ValueError("Prepared request drifted before execution")
            wire_request_list.append(
                _materialize_wire_request(
                    prepared,
                    request_index=index,
                    evaluation_at=started_at,
                )
            )
        wire_requests = tuple(wire_request_list)

        # Fairness: every exact request gets its initial reservation/send chance
        # before any unknown request enters the one-retry pass.
        for index, (prepared, wire, target) in enumerate(
            zip(
                prepared_requests,
                wire_requests,
                normalization_targets,
                strict=True,
            )
        ):
            _attempt_request(
                index,
                prepared,
                wire,
                target,
                states[index],
                send=send,
                limits=execution_limits,
                budget=budget,
                run_clock=run_clock,
                expected_target_fingerprint=(
                    normalization_target_fingerprints[index]
                ),
                bundle_context_fingerprint=(
                    pre_execution._context_fingerprint
                ),
                is_retry=False,
            )

        for index, state in enumerate(states):
            if (
                state.kind
                is not GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
                or state.unknown_attempt_count != 1
                or state.attempts_used != 1
                or not state.retry_eligible
            ):
                continue
            _attempt_request(
                index,
                prepared_requests[index],
                wire_requests[index],
                normalization_targets[index],
                state,
                send=send,
                limits=execution_limits,
                budget=budget,
                run_clock=run_clock,
                expected_target_fingerprint=(
                    normalization_target_fingerprints[index]
                ),
                bundle_context_fingerprint=(
                    pre_execution._context_fingerprint
                ),
                is_retry=True,
            )
    finally:
        if claimed:
            active_failure = sys.exception()
            wire_clear_failure: BaseException | None = None
            try:
                # This also covers a prefix created before a later failure.
                for item in wire_request_list:
                    try:
                        item._clear()
                    except BaseException as exc:
                        _emergency_clear_wire_request(item)
                        if wire_clear_failure is None:
                            wire_clear_failure = exc
            finally:
                execution_credential_lease._clear(
                    _token=_CREDENTIAL_ACCESS_TOKEN
                )
            if wire_clear_failure is not None and active_failure is None:
                raise RuntimeError("Provider wire cleanup failed") from None

    outcomes = tuple(
        GuidedProviderRequestExecutionOutcome(
            kind=state.kind,
            request_index=index,
            transport_profile=(
                prepared_requests[index].transport_profile
            ),
            attempts_used=state.attempts_used,
            retry_count=state.retry_count,
            unknown_attempt_count=state.unknown_attempt_count,
            problem_code=state.problem_code,
            _quarantine=state.quarantine,
        )
        for index, state in enumerate(states)
    )
    status, next_action = _execution_branch(outcomes)
    return GuidedProviderExecution(
        status=status,
        next_action=next_action,
        outcomes=outcomes,
        attempts_used=budget.attempts_used,
        reserved_google_cost_usd_micros=budget.google_cost,
        reserved_serpapi_credit_count=budget.serpapi_credits,
        started_at=started_at,
        completed_at=run_clock.last,
        _token=_EXECUTION_TOKEN,
    )


def _attempt_request(
    index: int,
    prepared: _PreparedExecutionRequest,
    wire: GuidedProviderWireRequest,
    normalization_target: object,
    state: _MutableOutcome,
    *,
    send: Callable[..., object],
    limits: GuidedProviderExecutionLimits,
    budget: _ExecutionBudget,
    run_clock: _RunClock,
    expected_target_fingerprint: str,
    bundle_context_fingerprint: str,
    is_retry: bool,
) -> None:
    request = prepared.source
    state.retry_eligible = False
    try:
        now = run_clock.now()
    except ValueError:
        if state.unknown_attempt_count:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
            state.problem_code = (
                GuidedProviderExecutionProblemCode.UNKNOWN_RETRY_NOT_AVAILABLE
            )
        else:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.NOT_ATTEMPTED
            state.problem_code = (
                GuidedProviderExecutionProblemCode.TRUSTED_CLOCK_INVALID
            )
        return
    if not _prepared_request_matches(prepared):
        _record_pre_send_drift(
            state,
            problem=GuidedProviderExecutionProblemCode.PREPARED_REQUEST_DRIFT,
        )
        return
    if not _normalization_target_matches(
        request,
        normalization_target,
        expected_target_fingerprint=expected_target_fingerprint,
        bundle_context_fingerprint=bundle_context_fingerprint,
    ):
        state.retry_eligible = False
        if state.unknown_attempt_count:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
        else:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.NOT_ATTEMPTED
        state.problem_code = (
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_DRIFT
        )
        return
    if not _normalization_target_fresh_at(normalization_target, now=now):
        state.retry_eligible = False
        if state.unknown_attempt_count:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
        else:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.NOT_ATTEMPTED
        state.problem_code = (
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_STALE
        )
        return
    reservation_problem = budget.reserve(
        now=now,
        google_cost_usd_micros=prepared.google_cost_usd_micros,
        serpapi_credit_count=prepared.serpapi_credit_count,
    )
    if reservation_problem is not None:
        if state.unknown_attempt_count:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
            state.problem_code = (
                GuidedProviderExecutionProblemCode.UNKNOWN_RETRY_NOT_AVAILABLE
            )
        else:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.NOT_ATTEMPTED
            state.problem_code = reservation_problem
        return

    state.attempts_used += 1
    if is_retry:
        state.retry_count += 1
    response: object | None = None
    certainty: GuidedProviderDeliveryCertainty | None = None
    try:
        response = send(
            wire,
            connect_timeout_s=float(limits.connect_timeout_s),
            read_timeout_s=float(limits.read_timeout_s),
            deadline_at=limits.deadline_at,
            max_response_bytes=_MAX_RESPONSE_BYTES,
            max_response_headers=_MAX_RESPONSE_HEADERS,
            max_header_name_bytes=_MAX_HEADER_NAME_BYTES,
            max_header_value_bytes=_MAX_HEADER_VALUE_BYTES,
        )
    except GuidedProviderTransportError as exc:
        try:
            reported_certainty = exc.certainty
        except Exception:
            reported_certainty = None
        certainty = (
            reported_certainty
            if type(reported_certainty) is GuidedProviderDeliveryCertainty
            else GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN
        )
    except Exception:
        certainty = GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN

    completion_problem: GuidedProviderExecutionProblemCode | None = None
    completion_clock_invalid = False
    try:
        completed_at = run_clock.now()
    except ValueError:
        completed_at = None
        completion_clock_invalid = True
    if completed_at is not None:
        completion_problem = budget.completion_problem(now=completed_at)
    retry_window_valid = (
        not completion_clock_invalid and completion_problem is None
    )
    if not _prepared_request_matches(prepared):
        _record_post_send_drift(
            state,
            certainty=certainty,
            problem=GuidedProviderExecutionProblemCode.PREPARED_REQUEST_DRIFT,
        )
        return
    if not _normalization_target_matches(
        request,
        normalization_target,
        expected_target_fingerprint=expected_target_fingerprint,
        bundle_context_fingerprint=bundle_context_fingerprint,
    ):
        if (
            certainty is GuidedProviderDeliveryCertainty.KNOWN_NOT_SENT
            and not state.unknown_attempt_count
        ):
            state.kind = GuidedProviderRequestExecutionOutcomeKind.KNOWN_NOT_SENT
            state.problem_code = (
                GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_DRIFT
            )
            state.retry_eligible = False
        else:
            _record_unknown_outcome(
                state,
                problem=(
                    GuidedProviderExecutionProblemCode
                    .NORMALIZATION_TARGET_DRIFT
                ),
                retry_eligible=False,
            )
        return

    if certainty is not None:
        if certainty is GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN:
            _record_unknown_outcome(
                state,
                problem=(
                    GuidedProviderExecutionProblemCode
                    .TRANSPORT_OUTCOME_UNKNOWN
                ),
                retry_eligible=retry_window_valid,
            )
        elif state.unknown_attempt_count:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
            state.problem_code = (
                GuidedProviderExecutionProblemCode.TRANSPORT_OUTCOME_UNKNOWN
            )
            state.retry_eligible = False
        else:
            state.kind = GuidedProviderRequestExecutionOutcomeKind.KNOWN_NOT_SENT
            state.problem_code = (
                GuidedProviderExecutionProblemCode.TRANSPORT_KNOWN_NOT_SENT
            )
            state.retry_eligible = False
        return

    response_snapshot = _validated_transport_response_snapshot(response)
    response = None
    if response_snapshot is None:
        # Only a typed KNOWN_NOT_SENT exception can prove no delivery. A buggy
        # transport may send, over-buffer, mutate, or return an invalid object.
        _record_unknown_outcome(
            state,
            problem=(
                GuidedProviderExecutionProblemCode.INVALID_TRANSPORT_RESPONSE
            ),
            retry_eligible=retry_window_valid,
        )
        return
    if completion_clock_invalid:
        _record_unknown_outcome(
            state,
            problem=GuidedProviderExecutionProblemCode.TRUSTED_CLOCK_INVALID,
            retry_eligible=False,
        )
        return
    if completion_problem is not None:
        _record_unknown_outcome(
            state,
            problem=completion_problem,
            retry_eligible=False,
        )
        return
    assert completed_at is not None
    try:
        state.quarantine = GuidedProviderQuarantinedResponse(
            request_index=index,
            response=response_snapshot,
            prepared_request=request,
            transport_profile=prepared.transport_profile,
            materialization_kind=prepared.materialization_kind,
            prepared_request_runtime_fingerprint=(
                prepared.runtime_fingerprint
            ),
            source_binding_fingerprint=(
                prepared.source_binding_fingerprint
            ),
            normalization_target=normalization_target,
            target_fingerprint=expected_target_fingerprint,
            bundle_context_fingerprint=bundle_context_fingerprint,
            attempt_number=state.attempts_used,
            sent_at=now,
            retrieved_at=completed_at,
            _token=_QUARANTINE_TOKEN,
        )
    except Exception:
        _record_unknown_outcome(
            state,
            problem=(
                GuidedProviderExecutionProblemCode.INVALID_TRANSPORT_RESPONSE
            ),
            retry_eligible=False,
        )
        return
    state.kind = GuidedProviderRequestExecutionOutcomeKind.RESPONSE_QUARANTINED
    state.problem_code = None
    state.retry_eligible = False


def _record_unknown_outcome(
    state: _MutableOutcome,
    *,
    problem: GuidedProviderExecutionProblemCode,
    retry_eligible: bool,
) -> None:
    state.kind = GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
    state.unknown_attempt_count += 1
    state.problem_code = problem
    state.retry_eligible = (
        retry_eligible
        and state.attempts_used == 1
        and state.unknown_attempt_count == 1
    )


def _record_pre_send_drift(
    state: _MutableOutcome,
    *,
    problem: GuidedProviderExecutionProblemCode,
) -> None:
    state.retry_eligible = False
    state.kind = (
        GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
        if state.unknown_attempt_count
        else GuidedProviderRequestExecutionOutcomeKind.NOT_ATTEMPTED
    )
    state.problem_code = problem


def _record_post_send_drift(
    state: _MutableOutcome,
    *,
    certainty: GuidedProviderDeliveryCertainty | None,
    problem: GuidedProviderExecutionProblemCode,
) -> None:
    if (
        certainty is GuidedProviderDeliveryCertainty.KNOWN_NOT_SENT
        and not state.unknown_attempt_count
    ):
        state.kind = GuidedProviderRequestExecutionOutcomeKind.KNOWN_NOT_SENT
        state.problem_code = problem
        state.retry_eligible = False
        return
    _record_unknown_outcome(
        state,
        problem=problem,
        retry_eligible=False,
    )


def _normalization_target_fresh_at(
    target: object,
    *,
    now: datetime,
) -> bool:
    if type(target) is GooglePlaceDetailsRequest:
        return target.endpoint.valid_until > now
    if type(target) is GoogleRouteRequest:
        try:
            return google_route_request_is_executable_at(target, now)
        except (FactContractError, TypeError, ValueError):
            return False
    return True


def _normalization_target_matches(
    request: GuidedProviderPreparedRequest,
    target: object,
    *,
    expected_target_fingerprint: str,
    bundle_context_fingerprint: str,
) -> bool:
    try:
        current = _target_fingerprint(
            request,
            target,
            bundle_context_fingerprint=bundle_context_fingerprint,
        )
    except Exception:
        return False
    return current == expected_target_fingerprint


def _validated_transport_response_snapshot(
    response: object,
) -> GuidedProviderTransportResponse | None:
    if type(response) is not GuidedProviderTransportResponse:
        return None
    try:
        return GuidedProviderTransportResponse(
            status_code=response.status_code,
            body=response._body,
            headers=tuple(response._headers),
        )
    except Exception:
        return None


def _quarantine_response_fingerprint(
    *,
    status_code: int,
    body: bytes,
    headers: tuple[tuple[str, str], ...],
    attempt_number: int,
    sent_at: datetime,
    retrieved_at: datetime,
) -> str:
    return _sha256(
        {
            "contract_version": GUIDED_PROVIDER_EXECUTION_VERSION,
            "domain": "guided-provider-quarantined-response",
            "status_code": status_code,
            "body": _private_runtime_value(body),
            "headers": headers,
            "attempt_number": attempt_number,
            "sent_at": sent_at,
            "retrieved_at": retrieved_at,
        }
    )


def revalidate_guided_provider_quarantined_response(
    context: GuidedProviderPreExecutionContext,
    pre_execution: GuidedProviderPreExecution,
    execution: GuidedProviderExecution,
    quarantine: GuidedProviderQuarantinedResponse,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderQuarantinedResponse:
    """Recheck one exact raw quarantine before any Phase 5.32 adapter use."""

    if (
        type(context) is not GuidedProviderPreExecutionContext
        or type(pre_execution) is not GuidedProviderPreExecution
        or type(execution) is not GuidedProviderExecution
        or type(quarantine) is not GuidedProviderQuarantinedResponse
    ):
        raise TypeError("Quarantine revalidation sources must be exact")
    try:
        evaluated = _utc_datetime(evaluation_at, "evaluation_at")
        if evaluated < execution._completed_at:
            raise ValueError
        _assess_context(
            context,
            preimages=preimages,
            evaluation_at=execution._started_at,
        )
        if (
            _pre_execution_context_runtime_fingerprint(context)
            != context._context_fingerprint
            or pre_execution._context_fingerprint
            != context._context_fingerprint
            or quarantine._bundle_context_fingerprint
            != context._context_fingerprint
            or execution.request_count != pre_execution.request_count
            or not pre_execution._claim.claimed
        ):
            raise ValueError
        index = quarantine.request_index
        if not 0 <= index < execution.request_count:
            raise ValueError
        outcome = execution._outcomes[index]
        request = pre_execution._requests[index]
        lease = pre_execution._credential_lease
        if (
            outcome._quarantine is not quarantine
            or outcome.kind
            is not GuidedProviderRequestExecutionOutcomeKind
            .RESPONSE_QUARANTINED
            or outcome.attempts_used != quarantine.attempt_number
            or quarantine._prepared_request is not request
            or quarantine.transport_profile is not request.transport_profile
            or quarantine.materialization_kind
            is not request.materialization_kind
            or request._credential_lease is not lease
            or any(
                item._credential_lease is not lease
                for item in pre_execution._requests
            )
            or lease.slots
            or lease._bound_at != pre_execution._prepared_at
            or lease._expires_at != pre_execution._expires_at
            or lease._context_fingerprint != context._context_fingerprint
        ):
            raise ValueError
        current_request_fingerprint = _prepared_request_runtime_fingerprint(
            request
        )
        if (
            current_request_fingerprint
            != quarantine._prepared_request_runtime_fingerprint
            or request._contract._source_binding_fingerprint
            != quarantine._source_binding_fingerprint
        ):
            raise ValueError
        target = quarantine._normalization_target
        current_target_fingerprint = _target_fingerprint(
            request,
            target,
            bundle_context_fingerprint=context._context_fingerprint,
        )
        if current_target_fingerprint != quarantine._target_fingerprint:
            raise ValueError
        source_targets = _match_normalization_targets(
            pre_execution,
            preimages=preimages,
        )
        source_target = source_targets[index]
        if type(target) is PlaceIdentityRequest:
            if (
                type(source_target) is not PlaceIdentityIntent
                or target.intent is not source_target
            ):
                raise ValueError
        elif target is not source_target:
            raise ValueError
        _validate_normalization_target_policy(
            request,
            target,
            started_at=execution._started_at,
        )
        if not _normalization_target_fresh_at(
            target,
            now=execution._started_at,
        ):
            raise ValueError
        if not (
            execution._started_at
            <= quarantine._sent_at
            <= quarantine.retrieved_at
            <= execution._completed_at
        ):
            raise ValueError
        response = GuidedProviderTransportResponse(
            status_code=quarantine.status_code,
            body=quarantine._body,
            headers=tuple(quarantine._headers),
        )
        if (
            _quarantine_response_fingerprint(
                status_code=response.status_code,
                body=response._body,
                headers=response._headers,
                attempt_number=quarantine.attempt_number,
                sent_at=quarantine._sent_at,
                retrieved_at=quarantine.retrieved_at,
            )
            != quarantine._response_fingerprint
        ):
            raise ValueError
    except Exception:
        raise ValueError(
            "Quarantined provider response no longer matches execution"
        ) from None
    return quarantine


def _pre_execution_context_runtime_fingerprint(
    context: GuidedProviderPreExecutionContext,
) -> str:
    return _sha256(
        {
            "contract_version": GUIDED_PROVIDER_PRE_EXECUTION_VERSION,
            "domain": "guided-provider-pre-execution-context",
            "assessment_args": _private_runtime_value(
                context._assessment_args
            ),
            "response": _private_runtime_value(context._response),
            "limits": _private_runtime_value(context._limits),
            "composed_at": context._composed_at,
            "expires_at": context._expires_at,
        }
    )


def _copy_execution_limits(
    limits: GuidedProviderExecutionLimits,
) -> GuidedProviderExecutionLimits:
    """Detach every cap from the caller before any injected callback runs."""

    return GuidedProviderExecutionLimits(
        max_attempt_count=limits.max_attempt_count,
        max_google_cost_usd_micros=limits.max_google_cost_usd_micros,
        max_serpapi_credit_count=limits.max_serpapi_credit_count,
        connect_timeout_s=limits.connect_timeout_s,
        read_timeout_s=limits.read_timeout_s,
        deadline_at=limits.deadline_at,
    )


def _capture_prepared_execution_request(
    request: GuidedProviderPreparedRequest,
) -> _PreparedExecutionRequest:
    if type(request) is not GuidedProviderPreparedRequest:
        raise TypeError("Prepared execution request must be exact")
    source_binding_fingerprint = _digest(
        request._contract._source_binding_fingerprint,
        "source_binding_fingerprint",
    )
    return _PreparedExecutionRequest(
        source=request,
        runtime_fingerprint=_prepared_request_runtime_fingerprint(request),
        transport_profile=request.transport_profile,
        materialization_kind=request.materialization_kind,
        http_method=request.http_method,
        credential_slot=request.credential_slot,
        credential_provider_field_name=(
            request.credential_provider_field_name
        ),
        credential_placement=request.credential_placement,
        google_cost_usd_micros=(
            request.estimated_google_cost_usd_micros
        ),
        serpapi_credit_count=request.serpapi_credit_count,
        endpoint=request._endpoint,
        headers=tuple(request._headers),
        query_parameters=tuple(request._query_parameters),
        json_body=(
            bytes(request._json_body)
            if request._json_body is not None
            else None
        ),
        credential_lease=request._credential_lease,
        source_binding_fingerprint=source_binding_fingerprint,
    )


def _prepared_request_runtime_fingerprint(
    request: GuidedProviderPreparedRequest,
) -> str:
    return _sha256(
        {
            "contract_version": GUIDED_PROVIDER_EXECUTION_VERSION,
            "domain": "guided-provider-prepared-request-runtime",
            "binding": _prepared_request_runtime_binding(request),
        }
    )


def _prepared_request_runtime_binding(
    request: GuidedProviderPreparedRequest,
) -> dict[str, object]:
    if type(request) is not GuidedProviderPreparedRequest:
        raise TypeError("Prepared request runtime binding must be exact")
    lease = request._credential_lease
    return {
        "request_identity": id(request),
        "transport_profile": request.transport_profile,
        "materialization_kind": request.materialization_kind,
        "http_method": request.http_method,
        "endpoint_template": request.endpoint_template,
        "credential_slot": request.credential_slot,
        "credential_provider_field_name": (
            request.credential_provider_field_name
        ),
        "credential_placement": request.credential_placement,
        "estimated_google_cost_usd_micros": (
            request.estimated_google_cost_usd_micros
        ),
        "serpapi_credit_count": request.serpapi_credit_count,
        "request_fingerprint": request._request_fingerprint,
        "endpoint": request._endpoint,
        "headers": request._headers,
        "query_parameters": request._query_parameters,
        "json_body": _private_runtime_value(request._json_body),
        "local_result_binding": _private_runtime_value(
            request._local_result_binding
        ),
        "transport_binding_identity": id(request._binding),
        "transport_binding": _private_runtime_value(request._binding),
        "contract_identity": id(request._contract),
        "contract": _private_runtime_value(request._contract),
        "credential_lease": {
            "identity": id(lease),
            "bound_at": lease._bound_at,
            "expires_at": lease._expires_at,
            "context_fingerprint": lease._context_fingerprint,
        },
    }


def _prepared_request_matches(
    prepared: _PreparedExecutionRequest,
) -> bool:
    try:
        current = _prepared_request_runtime_fingerprint(prepared.source)
    except Exception:
        return False
    return current == prepared.runtime_fingerprint


def _validate_limits(
    pre_execution: GuidedProviderPreExecution,
    limits: GuidedProviderExecutionLimits,
    *,
    started_at: datetime,
) -> None:
    accepted = pre_execution._limits
    if (
        limits.max_attempt_count < pre_execution.request_count
        or limits.max_attempt_count > accepted.accepted_max_request_count
        or limits.max_google_cost_usd_micros
        < accepted.estimated_google_cost_usd_micros
        or limits.max_google_cost_usd_micros
        > accepted.accepted_max_google_cost_usd_micros
        or limits.max_serpapi_credit_count
        < accepted.bound_serpapi_credit_count
        or limits.max_serpapi_credit_count > accepted.serpapi_credit_cap
    ):
        raise ValueError("Execution limits differ from accepted provider caps")
    if (
        limits.deadline_at <= started_at
        or limits.deadline_at > pre_execution._expires_at
        or timedelta(
            seconds=float(limits.connect_timeout_s)
            + float(limits.read_timeout_s)
        )
        > limits.deadline_at - started_at
    ):
        raise ValueError("Execution deadline differs from the short-lived bundle")


def _materialize_wire_request(
    prepared: _PreparedExecutionRequest,
    *,
    request_index: int,
    evaluation_at: datetime,
) -> GuidedProviderWireRequest:
    credential: str | None = None
    headers: list[tuple[str, str]] = []
    query: list[tuple[str, str]] = []
    wire_headers: tuple[tuple[str, str], ...] = ()
    url: str | None = None
    wire: GuidedProviderWireRequest | None = None
    try:
        credential = prepared.credential_lease._value(
            prepared.credential_slot,
            evaluation_at=evaluation_at,
            _token=_CREDENTIAL_ACCESS_TOKEN,
        )
        headers.extend(prepared.headers)
        query.extend(prepared.query_parameters)
        if (
            prepared.credential_placement
            is GuidedProviderRequestValuePlacement.HEADER
        ):
            headers.append(
                (prepared.credential_provider_field_name, credential)
            )
        elif (
            prepared.credential_placement
            is GuidedProviderRequestValuePlacement.QUERY_PARAMETER
        ):
            query.append(
                (prepared.credential_provider_field_name, credential)
            )
        else:
            raise ValueError(
                "Credential placement is outside the execution allowlist"
            )
        if len({name.lower() for name, _ in headers}) != len(headers):
            raise ValueError("Wire request contains duplicate headers")
        if len({name for name, _ in query}) != len(query):
            raise ValueError("Wire request contains duplicate query parameters")
        url = prepared.endpoint
        if query:
            url = f"{url}?{urlencode(tuple(sorted(query)))}"
        wire_headers = tuple(
            sorted(headers, key=lambda item: item[0].lower())
        )
        wire = GuidedProviderWireRequest(
            request_index=request_index,
            transport_profile=prepared.transport_profile,
            http_method=prepared.http_method,
            url=url,
            headers=wire_headers,
            body=prepared.json_body,
            _token=_WIRE_REQUEST_TOKEN,
        )
        return wire
    finally:
        # Keep credential-bearing temporaries out of retained exception frames.
        credential = None
        headers.clear()
        query.clear()
        wire_headers = ()
        url = None
        wire = None


def _emergency_clear_wire_request(
    request: GuidedProviderWireRequest,
) -> None:
    """Best-effort fallback if the ordinary internal scrub path fails."""

    try:
        object.__setattr__(request, "_url", "")
        object.__setattr__(request, "_headers", ())
        object.__setattr__(request, "_body", None)
        object.__setattr__(request, "_cleared", True)
    except BaseException:
        # The credential lease is independently cleared in the outer finally.
        pass


def _match_normalization_targets(
    pre_execution: GuidedProviderPreExecution,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
) -> tuple[object, ...]:
    if (
        not isinstance(preimages, tuple)
        or len(preimages) != pre_execution.request_count
        or any(
            type(item) is not GuidedProviderExecutionTargetPreimage
            for item in preimages
        )
    ):
        raise ValueError("Exact target preimages are required for execution")
    remaining = list(preimages)
    targets: list[object] = []
    for request in pre_execution._requests:
        contract = request._contract
        matches: list[GuidedProviderExecutionTargetPreimage] = []
        for preimage in remaining:
            transmitted, identifiers, local, target_kind = _request_values(
                preimage.target
            )
            if (
                preimage.topic is contract.topic
                and target_kind is contract.target_kind
                and transmitted == contract._provider_transmitted_values
                and identifiers == contract._provider_identifier_values
                and local == contract._local_result_binding
                and len(preimage.source_line_indexes)
                == contract.source_line_reference_count
            ):
                matches.append(preimage)
        if len(matches) != 1:
            raise ValueError("Target preimages do not uniquely match requests")
        selected = matches[0]
        remaining.remove(selected)
        targets.append(selected.target)
    if remaining:
        raise ValueError("Unmatched target preimages remain")
    return tuple(targets)


def _target_fingerprint(
    request: GuidedProviderPreparedRequest,
    target: object,
    *,
    bundle_context_fingerprint: str,
) -> str:
    return _sha256(
        {
            "contract_version": GUIDED_PROVIDER_EXECUTION_VERSION,
            "domain": "guided-provider-quarantine-normalization-target",
            "bundle_context_fingerprint": _digest(
                bundle_context_fingerprint,
                "bundle_context_fingerprint",
            ),
            "prepared_request_runtime_fingerprint": (
                _prepared_request_runtime_fingerprint(request)
            ),
            "source_binding_fingerprint": (
                request._contract._source_binding_fingerprint
            ),
            "normalization_target": _normalization_target_binding(target),
        }
    )


def _normalization_target_binding(target: object) -> dict[str, object]:
    """Return one fixed adapter request binding, never a generic normalizer."""

    if type(target) is PlaceIdentityRequest:
        return {
            "kind": "google_places_text_search",
            "private_exact_binding": _private_runtime_value(target),
        }
    if type(target) is GooglePlaceDetailsRequest:
        return {
            "kind": "google_place_details",
            "private_exact_binding": _private_runtime_value(target),
        }
    if type(target) is GoogleRouteRequest:
        return {
            "kind": "google_routes_compute_routes",
            "private_exact_binding": _private_runtime_value(target),
        }
    if type(target) is LodgingDiscoveryRequest:
        return {
            "kind": "serpapi_google_hotels",
            "private_exact_binding": _private_runtime_value(target),
        }
    raise TypeError("Unsupported provider-specific normalization target")


def _bind_normalization_targets(
    requests: tuple[GuidedProviderPreparedRequest, ...],
    targets: tuple[object, ...],
    *,
    identity_snapshot: EvidenceSnapshot | None,
    started_at: datetime,
) -> tuple[object, ...]:
    if len(requests) != len(targets):
        raise ValueError("Normalization requests and targets differ")
    identity_count = sum(type(item) is PlaceIdentityIntent for item in targets)
    if identity_count:
        if type(identity_snapshot) is not EvidenceSnapshot:
            raise ValueError(
                "Identity execution requires an exact send-time EvidenceSnapshot"
            )
        if identity_snapshot.evaluation_at > started_at:
            raise ValueError("Identity snapshot cannot come from the future")
        if identity_snapshot.purge_checked_at > started_at:
            raise ValueError("Identity purge check cannot come from the future")
    elif identity_snapshot is not None:
        raise ValueError("Identity snapshot is outside the exact request batch")
    bound: list[object] = []
    for request, target in zip(requests, targets, strict=True):
        normalization_target = (
            build_google_place_identity_request(target, identity_snapshot)
            if type(target) is PlaceIdentityIntent
            else target
        )
        _validate_normalization_target_policy(
            request,
            normalization_target,
            started_at=started_at,
        )
        bound.append(normalization_target)
    return tuple(bound)


def _validate_normalization_target_policy(
    request: GuidedProviderPreparedRequest,
    target: object,
    *,
    started_at: datetime,
) -> None:
    expected_types = {
        GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH: (
            PlaceIdentityRequest
        ),
        GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS: (
            GooglePlaceDetailsRequest
        ),
        GuidedProviderRequestMaterializationKind.GOOGLE_ROUTES_COMPUTE_ROUTES: (
            GoogleRouteRequest
        ),
        GuidedProviderRequestMaterializationKind.SERPAPI_GOOGLE_HOTELS: (
            LodgingDiscoveryRequest
        ),
    }
    expected = expected_types.get(request.materialization_kind)
    if expected is None or type(target) is not expected:
        raise ValueError("Normalization target differs from request profile")
    if type(target) is LodgingDiscoveryRequest:
        if (
            request._contract.policy_profile
            is not GuidedProviderPolicyProfile.SERPAPI_TERMS_2026_04_08
        ):
            raise ValueError("Hotel request policy differs from preflight")
        return

    if (
        request._contract.policy_profile
        is not GuidedProviderPolicyProfile.GOOGLE_MAPS_NON_EEA_2026_06_10
    ):
        raise ValueError("Google request policy differs from preflight")
    snapshot = target.snapshot
    if snapshot.evaluation_at > started_at:
        raise ValueError("Normalization snapshot cannot come from the future")
    if snapshot.purge_checked_at > started_at:
        raise ValueError("Normalization purge check cannot come from the future")
    policy = snapshot.policies.policy(target.provider_request.policy_id)
    if (
        policy.contract_region
        != request._contract.policy_profile.value
        or policy.contract_region != GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
    ):
        raise ValueError("Normalization policy differs from accepted profile")


def _execution_branch(
    outcomes: tuple[GuidedProviderRequestExecutionOutcome, ...],
) -> tuple[GuidedProviderExecutionStatus, str]:
    quarantined = sum(
        item.kind
        is GuidedProviderRequestExecutionOutcomeKind.RESPONSE_QUARANTINED
        for item in outcomes
    )
    unknown = any(
        item.kind is GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN
        for item in outcomes
    )
    if unknown:
        return (
            GuidedProviderExecutionStatus.OUTCOME_UNKNOWN,
            "manually_reconcile_unknown_provider_outcomes",
        )
    if quarantined == len(outcomes):
        return (
            GuidedProviderExecutionStatus.ALL_RESPONSES_QUARANTINED,
            "assess_provider_specific_quarantined_responses",
        )
    if quarantined:
        return (
            GuidedProviderExecutionStatus.PARTIAL,
            "review_quarantine_and_reauthorize_failures",
        )
    return (
        GuidedProviderExecutionStatus.NO_RESPONSES,
        "obtain_fresh_exact_provider_execution_consent",
    )


def _trusted_clock(clock: Callable[[], datetime]) -> datetime:
    if not callable(clock):
        raise TypeError("clock must be callable")
    try:
        value = clock()
        return _utc_datetime(value, "clock result")
    except Exception:
        raise ValueError("Trusted execution clock failed") from None


def _sha256(value: object) -> str:
    encoded = json.dumps(
        _private_runtime_value(value),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _private_runtime_value(value: object) -> Any:
    """Canonical private data for a process-local exact-drift digest only."""

    if isinstance(value, Enum):
        return {
            "enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value.value,
        }
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, bytes):
        return {
            "bytes_length": len(value),
            "bytes_sha256": hashlib.sha256(value).hexdigest(),
        }
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, tuple):
        return [_private_runtime_value(item) for item in value]
    if isinstance(value, list):
        return [_private_runtime_value(item) for item in value]
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise TypeError("Private runtime mapping keys must be text")
        return {
            key: _private_runtime_value(value[key])
            for key in sorted(value)
        }
    if is_dataclass(value):
        return {
            "dataclass": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": [
                [item.name, _private_runtime_value(getattr(value, item.name))]
                for item in fields(value)
            ],
        }
    raise TypeError("Unsupported private provider runtime binding")


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
    "GUIDED_PROVIDER_EXECUTION_VERSION",
    "GuidedProviderDeliveryCertainty",
    "GuidedProviderExecution",
    "GuidedProviderExecutionLimits",
    "GuidedProviderExecutionProblemCode",
    "GuidedProviderExecutionStatus",
    "GuidedProviderQuarantinedResponse",
    "GuidedProviderRequestExecutionOutcome",
    "GuidedProviderRequestExecutionOutcomeKind",
    "GuidedProviderTransport",
    "GuidedProviderTransportError",
    "GuidedProviderTransportResponse",
    "GuidedProviderWireRequest",
    "execute_guided_provider_requests",
    "revalidate_guided_provider_quarantined_response",
]
