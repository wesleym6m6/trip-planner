"""Bounded Google Routes adapter with no built-in network implementation.

The module owns the exact request/response boundary for one route edge.  Raw
Place IDs, credentials, provider response bytes, and free-form provider
messages remain runtime-only.  Callers must inject a transport; tests can
therefore exercise the complete adapter without contacting Google.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from threading import Lock
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .facts import (
    AuthorizedProviderResult,
    EvidencePersistence,
    EvidenceSnapshot,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderProblem,
    ProviderProblemCode,
    ProviderProvenance,
    ProviderRequest,
    ProviderResult,
    ProviderResultStatus,
    _GOOGLE_ROUTE_AUTHORIZATION_TOKEN,
    _GOOGLE_ROUTE_FIELD_MASK,
    _authorize_google_route_result,
)
from .evidence_session import (
    EvidenceSession,
    EvidenceSessionLoad,
    EvidenceSessionMerge,
)
from .places_identity import PlaceEndpointIdentity


GOOGLE_ROUTES_COMPUTE_URL = (
    "https://routes.googleapis.com/directions/v2:computeRoutes"
)
GOOGLE_ROUTES_FIELD_MASK = _GOOGLE_ROUTE_FIELD_MASK

_PROVIDER_ID = "google-routes"
_ADAPTER_VERSION = "v1"
_POLICY_ID = "google-route-runtime-v1"
_ATTRIBUTION = "Google Maps"
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 512
_MAX_ROUTE_SECONDS = Decimal(366 * 24 * 60 * 60)
_MAX_DISTANCE_METERS = 100_000_000
_MAX_ATTEMPTS_PER_REQUEST = 3
_MAX_RETRY_DELAY_SECONDS = 4.0
_ROUTE_VALIDITY = timedelta(hours=1)
_ROUTE_RETENTION = timedelta(days=1)
_DURATION_RE = re.compile(r"(0|[1-9][0-9]*)(?:\.([0-9]{1,9}))?s")
_WARNING_RE = re.compile(r"[a-z][a-z0-9._/-]{0,127}")


class RouteMode(str, Enum):
    """Supported exact Google Routes travel modes."""

    DRIVING = "driving"
    WALKING = "walking"
    TRANSIT = "transit"
    BICYCLING = "bicycling"
    TWO_WHEELER = "two_wheeler"

    @property
    def google_value(self) -> str:
        return {
            RouteMode.DRIVING: "DRIVE",
            RouteMode.WALKING: "WALK",
            RouteMode.TRANSIT: "TRANSIT",
            RouteMode.BICYCLING: "BICYCLE",
            RouteMode.TWO_WHEELER: "TWO_WHEELER",
        }[self]


class TransitFallbackPolicy(str, Enum):
    """Whether an unavailable transit result may trigger a driving request."""

    NONE = "none"
    DRIVING = "driving"

    # Descriptive aliases keep call sites readable without changing bytes.
    DISABLED = "none"
    DRIVING_ON_UNAVAILABLE = "driving"


_BETA_WARNING_BY_MODE = {
    RouteMode.WALKING: "walking_route_beta",
    RouteMode.BICYCLING: "bicycling_route_beta",
    RouteMode.TWO_WHEELER: "two_wheeler_route_beta",
}
_FALLBACK_TRIGGER_CODES = frozenset(
    {
        ProviderProblemCode.EMPTY_RESPONSE,
        ProviderProblemCode.NOT_FOUND,
        ProviderProblemCode.UNSUPPORTED_MODE,
    }
)


_ROUTE_REQUEST_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class GoogleRouteRequest:
    """Token-gated route request bound to one trusted evidence snapshot."""

    snapshot: EvidenceSnapshot = field(repr=False)
    origin: PlaceEndpointIdentity = field(repr=False)
    destination: PlaceEndpointIdentity = field(repr=False)
    mode: RouteMode
    departure_at: str
    transit_fallback_policy: TransitFallbackPolicy
    fallback_from_mode: RouteMode | None
    provider_request: ProviderRequest
    field_mask: str

    def __init__(
        self,
        *,
        snapshot: EvidenceSnapshot,
        origin: PlaceEndpointIdentity,
        destination: PlaceEndpointIdentity,
        mode: RouteMode,
        departure_at: str,
        transit_fallback_policy: TransitFallbackPolicy,
        fallback_from_mode: RouteMode | None,
        provider_request: ProviderRequest,
        field_mask: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _ROUTE_REQUEST_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Google route requests can only be created by the host factory.",
            )
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "departure_at", departure_at)
        object.__setattr__(
            self,
            "transit_fallback_policy",
            transit_fallback_policy,
        )
        object.__setattr__(self, "fallback_from_mode", fallback_from_mode)
        object.__setattr__(self, "provider_request", provider_request)
        object.__setattr__(self, "field_mask", field_mask)
        self.__post_init__()

    def __post_init__(self) -> None:
        if (
            type(self.snapshot) is not EvidenceSnapshot
            or type(self.origin) is not PlaceEndpointIdentity
            or type(self.destination) is not PlaceEndpointIdentity
            or type(self.provider_request) is not ProviderRequest
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Google route requests require exact trusted contract values.",
            )
        if type(self.mode) is not RouteMode or type(
            self.transit_fallback_policy
        ) is not TransitFallbackPolicy:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Route mode and fallback policy must be exact enum values.",
            )
        if self.field_mask != _GOOGLE_ROUTE_FIELD_MASK:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Google route requests require the fixed minimal field mask.",
            )
        if (
            self.origin.snapshot_id != self.snapshot.snapshot_id
            or self.destination.snapshot_id != self.snapshot.snapshot_id
        ):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                "Route endpoints must come from the exact current snapshot.",
            )
        if (
            self.origin.valid_until <= self.snapshot.evaluation_at
            or self.destination.valid_until <= self.snapshot.evaluation_at
        ):
            raise FactContractError(
                "STALE_EVIDENCE",
                "Route endpoints must be fresh at the snapshot evaluation time.",
            )
        if (
            self.origin.location_id == self.destination.location_id
            or self.origin.endpoint_id == self.destination.endpoint_id
            or self.origin.provider_place_id
            == self.destination.provider_place_id
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Route endpoints must be distinct ordered places.",
            )
        key = self.provider_request.fact_keys[0]
        if (
            len(self.provider_request.fact_keys) != 1
            or key.subject_ids
            != (self.origin.location_id, self.destination.location_id)
            or key.qualifier_map
            != {
                "departure_at": self.departure_at,
                "mode": self.mode.value,
            }
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Route request differs from its exact mode/time fact key.",
            )

    def __repr__(self) -> str:
        return (
            "GoogleRouteRequest("
            f"origin={self.origin.location_id!r}, "
            f"destination={self.destination.location_id!r}, "
            f"mode={self.mode.value!r}, "
            f"request_fingerprint="
            f"{self.provider_request.request_fingerprint!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        """Return a safe binding without Place IDs or credentials."""

        return {
            "origin": self.origin.to_binding_dict(),
            "destination": self.destination.to_binding_dict(),
            "mode": self.mode.value,
            "departure_at": self.departure_at,
            "transit_fallback_policy": self.transit_fallback_policy.value,
            "fallback_from_mode": (
                self.fallback_from_mode.value
                if self.fallback_from_mode is not None
                else None
            ),
            "field_mask": self.field_mask,
            "provider_request": self.provider_request.to_binding_dict(),
        }


def build_google_route_request(
    snapshot: EvidenceSnapshot,
    origin: PlaceEndpointIdentity,
    destination: PlaceEndpointIdentity,
    mode: RouteMode,
    *,
    departure_at: str,
    transit_fallback_policy: TransitFallbackPolicy = (
        TransitFallbackPolicy.NONE
    ),
) -> GoogleRouteRequest:
    """Build one exact route request without performing transport I/O."""

    return _build_google_route_request(
        snapshot,
        origin,
        destination,
        mode,
        departure_at=departure_at,
        transit_fallback_policy=transit_fallback_policy,
        fallback_from_mode=None,
    )


def _build_google_route_request(
    snapshot: EvidenceSnapshot,
    origin: PlaceEndpointIdentity,
    destination: PlaceEndpointIdentity,
    mode: RouteMode,
    *,
    departure_at: str,
    transit_fallback_policy: TransitFallbackPolicy,
    fallback_from_mode: RouteMode | None,
) -> GoogleRouteRequest:
    if type(snapshot) is not EvidenceSnapshot:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "snapshot must be an exact EvidenceSnapshot.",
        )
    if (
        type(origin) is not PlaceEndpointIdentity
        or type(destination) is not PlaceEndpointIdentity
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "origin and destination must be exact PlaceEndpointIdentity values.",
        )
    if type(mode) is not RouteMode:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "mode must be an exact RouteMode.",
        )
    if type(transit_fallback_policy) is not TransitFallbackPolicy:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "transit_fallback_policy must be exact.",
        )
    if (
        transit_fallback_policy is not TransitFallbackPolicy.NONE
        and mode is not RouteMode.TRANSIT
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Only a transit request may declare a fallback policy.",
        )
    if fallback_from_mode is not None and (
        mode is not RouteMode.DRIVING
        or fallback_from_mode is not RouteMode.TRANSIT
        or transit_fallback_policy is not TransitFallbackPolicy.NONE
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Only an exact transit-to-driving fallback is supported.",
        )

    key = FactKey(
        kind=FactKind.ROUTE_ESTIMATE,
        subject_ids=(origin.location_id, destination.location_id),
        qualifiers=(
            ("mode", mode.value),
            ("departure_at", departure_at),
        ),
    )
    normalized_departure = str(key.qualifier_map["departure_at"])
    if mode is RouteMode.TRANSIT:
        requested_at = _parse_rfc3339(normalized_departure)
        earliest = snapshot.evaluation_at - timedelta(days=7)
        latest = snapshot.evaluation_at + timedelta(days=100)
        if requested_at < earliest or requested_at > latest:
            raise FactContractError(
                "OUTSIDE_PROVIDER_HORIZON",
                "Transit departure is outside the inclusive -7/+100 day horizon.",
            )

    policy = snapshot.policies.policy(_POLICY_ID)
    if (
        policy.provider_id != _PROVIDER_ID
        or policy.adapter_id != _PROVIDER_ID
        or policy.adapter_version != _ADAPTER_VERSION
        or policy.persistence is not EvidencePersistence.MEMORY_ONLY
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "The snapshot does not authorize the Google Routes adapter.",
        )

    scope = [
        ("basis_evidence_revision", snapshot.evidence_revision),
        ("basis_snapshot_id", snapshot.snapshot_id),
        ("basis_store_revision", snapshot.store_revision),
        ("destination_endpoint_id", destination.endpoint_id),
        ("destination_observation_id", destination.observation_id),
        ("destination_value_digest", destination.value_digest),
        ("field_mask", _GOOGLE_ROUTE_FIELD_MASK),
        ("origin_endpoint_id", origin.endpoint_id),
        ("origin_observation_id", origin.observation_id),
        ("origin_value_digest", origin.value_digest),
    ]
    if fallback_from_mode is not None:
        scope.append(("fallback_from_mode", fallback_from_mode.value))
    provider_request = ProviderRequest(
        provider_id=_PROVIDER_ID,
        adapter_id=_PROVIDER_ID,
        adapter_version=_ADAPTER_VERSION,
        operation="compute-route",
        fact_keys=(key,),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
        query_scope=tuple(scope),
    )
    return GoogleRouteRequest(
        snapshot=snapshot,
        origin=origin,
        destination=destination,
        mode=mode,
        departure_at=normalized_departure,
        transit_fallback_policy=transit_fallback_policy,
        fallback_from_mode=fallback_from_mode,
        provider_request=provider_request,
        field_mask=_GOOGLE_ROUTE_FIELD_MASK,
        _token=_ROUTE_REQUEST_TOKEN,
    )


@dataclass(frozen=True, slots=True, repr=False)
class GoogleRoutesHttpRequest:
    """Runtime-only HTTP request; its repr and binding are secret-free."""

    url: str
    body: bytes = field(repr=False)
    field_mask: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.url != GOOGLE_ROUTES_COMPUTE_URL:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Google Routes URL must be the fixed trusted endpoint.",
            )
        if not isinstance(self.body, bytes) or not self.body:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Google Routes request body must be non-empty bytes.",
            )
        if self.field_mask != _GOOGLE_ROUTE_FIELD_MASK:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Google Routes HTTP request uses an unexpected field mask.",
            )

    @property
    def headers(self) -> Mapping[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-FieldMask": self.field_mask,
        }

    def __repr__(self) -> str:
        return (
            "GoogleRoutesHttpRequest("
            f"url={self.url!r}, body_bytes={len(self.body)}, "
            f"body_digest={hashlib.sha256(self.body).hexdigest()!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": "POST",
            "field_mask": self.field_mask,
            "body_bytes": len(self.body),
            "body_digest": hashlib.sha256(self.body).hexdigest(),
        }


def build_google_routes_http_request(
    request: GoogleRouteRequest,
) -> GoogleRoutesHttpRequest:
    """Materialize the exact POST body at the last runtime-only boundary."""

    if type(request) is not GoogleRouteRequest:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "request must be an exact GoogleRouteRequest.",
        )
    body = {
        "computeAlternativeRoutes": False,
        "destination": {
            "placeId": request.destination.provider_place_id,
        },
        "origin": {"placeId": request.origin.provider_place_id},
        "travelMode": request.mode.google_value,
        "departureTime": request.departure_at,
    }
    encoded = json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return GoogleRoutesHttpRequest(
        url=GOOGLE_ROUTES_COMPUTE_URL,
        body=encoded,
        field_mask=request.field_mask,
    )


@dataclass(frozen=True, slots=True)
class GoogleRoutesHttpResponse:
    """Bounded transport response; body and headers are runtime-only."""

    status_code: int
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("status_code must be an HTTP status integer")
        if not isinstance(self.body, bytes):
            raise TypeError("Google Routes response body must be bytes")
        raw_headers: Any = self.headers
        if isinstance(raw_headers, Mapping):
            raw_headers = tuple(raw_headers.items())
        if (
            not isinstance(raw_headers, tuple)
            or len(raw_headers) > 32
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                for item in raw_headers
            )
        ):
            raise TypeError("response headers must be bounded text pairs")
        object.__setattr__(
            self,
            "headers",
            tuple((name.lower(), value) for name, value in raw_headers),
        )

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next(
            (value for key, value in self.headers if key == wanted),
            None,
        )


class GoogleRoutesTransportErrorKind(str, Enum):
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    NETWORK = "network"
    TLS = "tls"


class GoogleRoutesTransportError(RuntimeError):
    """Sanitized transport failure; never wrap raw exception text."""

    def __init__(self, kind: GoogleRoutesTransportErrorKind) -> None:
        if type(kind) is not GoogleRoutesTransportErrorKind:
            raise TypeError("kind must be GoogleRoutesTransportErrorKind")
        self.kind = kind
        super().__init__(f"Google Routes transport failed ({kind.value}).")


@runtime_checkable
class GoogleRoutesTransport(Protocol):
    def send(
        self,
        request: GoogleRoutesHttpRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        """Send one request and return bytes without decoding provider data."""


class RouteAttemptBudget:
    """Thread-safe global budget charged immediately before every send."""

    def __init__(self, max_attempts: int) -> None:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 0
        ):
            raise ValueError("max_attempts must be a non-negative integer")
        self._max_attempts = max_attempts
        self._used_attempts = 0
        self._lock = Lock()

    def reserve(self) -> bool:
        with self._lock:
            if self._used_attempts >= self._max_attempts:
                return False
            self._used_attempts += 1
            return True

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def used_attempts(self) -> int:
        with self._lock:
            return self._used_attempts

    @property
    def remaining_attempts(self) -> int:
        with self._lock:
            return self._max_attempts - self._used_attempts


@dataclass(frozen=True, slots=True, repr=False)
class GoogleRouteExecution:
    """Authorized primary result and optional exact driving fallback."""

    request: GoogleRouteRequest
    primary_result: AuthorizedProviderResult
    fallback_request: GoogleRouteRequest | None
    fallback_result: AuthorizedProviderResult | None
    attempts_used: int
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.request) is not GoogleRouteRequest
            or type(self.primary_result) is not AuthorizedProviderResult
        ):
            raise TypeError(
                "GoogleRouteExecution requires exact request and result values"
            )
        if (
            self.primary_result.request.request_fingerprint
            != self.request.provider_request.request_fingerprint
        ):
            raise ValueError(
                "Primary route result differs from its exact request"
            )
        if (self.fallback_request is None) != (
            self.fallback_result is None
        ):
            raise ValueError(
                "Fallback request and result must either both exist or be absent"
            )
        if self.fallback_request is not None:
            if (
                type(self.fallback_request) is not GoogleRouteRequest
                or type(self.fallback_result)
                is not AuthorizedProviderResult
            ):
                raise TypeError(
                    "Fallback request and result must be exact values"
                )
            assert self.fallback_result is not None
            if (
                self.request.mode is not RouteMode.TRANSIT
                or self.request.transit_fallback_policy
                is not TransitFallbackPolicy.DRIVING
                or self.fallback_request.mode is not RouteMode.DRIVING
                or self.fallback_request.fallback_from_mode
                is not RouteMode.TRANSIT
                or self.fallback_result.request.request_fingerprint
                != self.fallback_request.provider_request.request_fingerprint
            ):
                raise ValueError(
                    "Execution fallback is not exact transit-to-driving"
                )
        expected_attempts = self.primary_result.result.attempts_used + (
            self.fallback_result.result.attempts_used
            if self.fallback_result is not None
            else 0
        )
        if (
            isinstance(self.attempts_used, bool)
            or not isinstance(self.attempts_used, int)
            or self.attempts_used != expected_attempts
        ):
            raise ValueError(
                "Execution attempts must equal its exact provider results"
            )
        if (
            not isinstance(self.warnings, tuple)
            or any(
                not isinstance(item, str)
                or _WARNING_RE.fullmatch(item) is None
                for item in self.warnings
            )
            or len(set(self.warnings)) != len(self.warnings)
        ):
            raise ValueError(
                "Execution warnings must be unique lowercase machine names"
            )
        expected_warnings: list[str] = []
        for authorized in (
            self.primary_result,
            *(
                (self.fallback_result,)
                if self.fallback_result is not None
                else ()
            ),
        ):
            for observation in authorized.result.observations:
                expected_warnings.extend(
                    observation.value.payload.get("warning_codes", ())
                )
        if self.fallback_result is not None:
            expected_warnings.append("transit_unavailable")
        normalized = _unique_warnings(tuple(expected_warnings))
        if self.warnings != normalized:
            raise ValueError(
                "Execution warnings differ from its authorized results"
            )

    @property
    def primary(self) -> AuthorizedProviderResult:
        return self.primary_result

    @property
    def fallback(self) -> AuthorizedProviderResult | None:
        return self.fallback_result

    def __repr__(self) -> str:
        return (
            "GoogleRouteExecution("
            f"request_fingerprint="
            f"{self.request.provider_request.request_fingerprint!r}, "
            f"attempts_used={self.attempts_used}, "
            f"has_fallback={self.fallback_result is not None}, "
            f"warnings={self.warnings!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_binding_dict(),
            "primary_result": self.primary_result.to_binding_dict(),
            "fallback_request": (
                self.fallback_request.to_binding_dict()
                if self.fallback_request is not None
                else None
            ),
            "fallback_result": (
                self.fallback_result.to_binding_dict()
                if self.fallback_result is not None
                else None
            ),
            "attempts_used": self.attempts_used,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class _DecodedRoute:
    payload: Mapping[str, Any]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    decoded: _DecodedRoute | None
    problem: ProviderProblem | None
    retry_after_s: float | None = None


def execute_google_route(
    request: GoogleRouteRequest,
    transport: GoogleRoutesTransport,
    *,
    attempt_budget: RouteAttemptBudget,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] | None = None,
    max_attempts: int = 1,
    connect_timeout_s: float = 3.0,
    read_timeout_s: float = 15.0,
) -> GoogleRouteExecution:
    """Execute one request and its narrowly authorized transit fallback."""

    if type(request) is not GoogleRouteRequest:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "request must be an exact GoogleRouteRequest.",
        )
    if type(attempt_budget) is not RouteAttemptBudget:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "attempt_budget must be an exact RouteAttemptBudget.",
        )
    _validate_execution_options(
        max_attempts=max_attempts,
        sleeper=sleeper,
        connect_timeout_s=connect_timeout_s,
        read_timeout_s=read_timeout_s,
    )
    run_clock = _RunClock(
        clock,
        floor=request.snapshot.purge_checked_at,
    )
    primary, primary_warnings, primary_attempts = _execute_exact_request(
        request,
        transport,
        attempt_budget=attempt_budget,
        clock=run_clock.now,
        sleeper=sleeper,
        max_attempts=max_attempts,
        connect_timeout_s=connect_timeout_s,
        read_timeout_s=read_timeout_s,
    )

    fallback_request: GoogleRouteRequest | None = None
    fallback_result: AuthorizedProviderResult | None = None
    fallback_warnings: tuple[str, ...] = ()
    fallback_attempts = 0
    problem_codes = {
        problem.code for problem in primary.result.problems
    }
    should_fallback = (
        request.mode is RouteMode.TRANSIT
        and request.transit_fallback_policy is TransitFallbackPolicy.DRIVING
        and bool(problem_codes.intersection(_FALLBACK_TRIGGER_CODES))
    )
    if should_fallback:
        primary = _with_transit_unavailable_problem(request, primary)
        fallback_request = _build_google_route_request(
            request.snapshot,
            request.origin,
            request.destination,
            RouteMode.DRIVING,
            departure_at=request.departure_at,
            transit_fallback_policy=TransitFallbackPolicy.NONE,
            fallback_from_mode=RouteMode.TRANSIT,
        )
        (
            fallback_result,
            fallback_warnings,
            fallback_attempts,
        ) = _execute_exact_request(
            fallback_request,
            transport,
            attempt_budget=attempt_budget,
            clock=run_clock.now,
            sleeper=sleeper,
            max_attempts=max_attempts,
            connect_timeout_s=connect_timeout_s,
            read_timeout_s=read_timeout_s,
        )
        fallback_warnings = (
            "transit_unavailable",
            *fallback_warnings,
        )

    return GoogleRouteExecution(
        request=request,
        primary_result=primary,
        fallback_request=fallback_request,
        fallback_result=fallback_result,
        attempts_used=primary_attempts + fallback_attempts,
        warnings=_unique_warnings(primary_warnings + fallback_warnings),
    )


execute_google_route_request = execute_google_route


def _with_transit_unavailable_problem(
    request: GoogleRouteRequest,
    authorized: AuthorizedProviderResult,
) -> AuthorizedProviderResult:
    """Bind explicit fallback disclosure to the unavailable transit fact."""

    raw = authorized.result
    unavailable = ProviderProblem(
        code=ProviderProblemCode.TRANSIT_UNAVAILABLE,
        message=(
            "Transit routing was unavailable; a driving result is only an "
            "explicit fallback."
        ),
        retryable=False,
        next_action="review_transit_fallback",
        fact_key_ids=(request.provider_request.fact_keys[0].key_id,),
    )
    revised = ProviderResult(
        request_fingerprint=raw.request_fingerprint,
        status=raw.status,
        observations=raw.observations,
        problems=(*raw.problems, unavailable),
        attempts_used=raw.attempts_used,
        completed_at=raw.completed_at,
    )
    return _authorize_google_route_result(
        request.provider_request,
        revised,
        request.snapshot.policies,
        _token=_GOOGLE_ROUTE_AUTHORIZATION_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class GoogleRouteBatchExecution:
    """Per-request outcomes merged into one run-scoped evidence session."""

    executions: tuple[GoogleRouteExecution, ...]
    merges: tuple[EvidenceSessionMerge, ...] = field(repr=False)
    current: EvidenceSessionLoad
    attempts_used: int
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.executions, tuple)
            or not self.executions
            or any(
                type(item) is not GoogleRouteExecution
                for item in self.executions
            )
        ):
            raise TypeError(
                "Route batches require exact non-empty executions"
            )
        if (
            not isinstance(self.merges, tuple)
            or not self.merges
            or any(
                type(item) is not EvidenceSessionMerge
                for item in self.merges
            )
        ):
            raise TypeError("Route batch merges must contain exact values")
        if type(self.current) is not EvidenceSessionLoad:
            raise TypeError("Route batch current load must be exact")
        expected_attempts = sum(
            item.attempts_used for item in self.executions
        )
        if self.attempts_used != expected_attempts:
            raise ValueError(
                "Route batch attempts differ from its executions"
            )
        expected_warnings = _unique_warnings(
            tuple(
                warning
                for execution in self.executions
                for warning in execution.warnings
            )
        )
        if self.warnings != expected_warnings:
            raise ValueError(
                "Route batch warnings differ from its executions"
            )
        if self.merges[-1].current != self.current:
            raise ValueError(
                "Route batch current load must equal its final merge"
            )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "executions": [
                item.to_binding_dict() for item in self.executions
            ],
            "current": self.current.to_dict(),
            "attempts_used": self.attempts_used,
            "warnings": list(self.warnings),
        }


def execute_google_route_batch(
    requests: tuple[GoogleRouteRequest, ...],
    transport: GoogleRoutesTransport,
    *,
    session: EvidenceSession,
    attempt_budget: RouteAttemptBudget,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] | None = None,
    max_attempts: int = 1,
    connect_timeout_s: float = 3.0,
    read_timeout_s: float = 15.0,
) -> GoogleRouteBatchExecution:
    """Execute a pre-expanded route batch and merge only valid exact slots."""

    if (
        not isinstance(requests, tuple)
        or not requests
        or len(requests) > 256
        or any(type(item) is not GoogleRouteRequest for item in requests)
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Route batch must contain 1 to 256 exact requests.",
        )
    if type(session) is not EvidenceSession:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Route batch requires an exact EvidenceSession.",
        )
    if type(attempt_budget) is not RouteAttemptBudget:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Route batch requires an exact RouteAttemptBudget.",
        )
    _validate_execution_options(
        max_attempts=max_attempts,
        sleeper=sleeper,
        connect_timeout_s=connect_timeout_s,
        read_timeout_s=read_timeout_s,
    )
    fingerprints = [
        item.provider_request.request_fingerprint for item in requests
    ]
    if len(set(fingerprints)) != len(fingerprints):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Route batch cannot contain duplicate exact requests.",
        )
    snapshot_ids = {item.snapshot.snapshot_id for item in requests}
    if len(snapshot_ids) != 1:
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Route batch requests must share one exact evidence snapshot.",
    )
    initial = session.load()
    basis = requests[0].snapshot
    active_ids = {
        item.observation_id for item in initial.ledger.observations
    }
    basis_ids = {
        item.observation_id for item in basis.observations
    }
    if (
        initial.store_revision != basis.store_revision
        or active_ids != basis_ids
        or initial.outcome_revision != basis.outcome_revision
    ):
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Evidence changed before route batch execution.",
        )
    required_endpoint_ids = {
        endpoint.observation_id
        for request in requests
        for endpoint in (request.origin, request.destination)
    }
    if not required_endpoint_ids.issubset(active_ids):
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Route batch endpoint identity is no longer current.",
        )

    executions: list[GoogleRouteExecution] = []
    merges: list[EvidenceSessionMerge] = []
    for request in requests:
        execution = execute_google_route(
            request,
            transport,
            attempt_budget=attempt_budget,
            clock=clock,
            sleeper=sleeper,
            max_attempts=max_attempts,
            connect_timeout_s=connect_timeout_s,
            read_timeout_s=read_timeout_s,
        )
        executions.append(execution)
        merges.append(session.merge(execution.primary_result))
        if execution.fallback_result is not None:
            merges.append(session.merge(execution.fallback_result))

    return GoogleRouteBatchExecution(
        executions=tuple(executions),
        merges=tuple(merges),
        current=merges[-1].current,
        attempts_used=sum(item.attempts_used for item in executions),
        warnings=_unique_warnings(
            tuple(
                warning
                for execution in executions
                for warning in execution.warnings
            )
        ),
    )


def _execute_exact_request(
    request: GoogleRouteRequest,
    transport: GoogleRoutesTransport,
    *,
    attempt_budget: RouteAttemptBudget,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None] | None,
    max_attempts: int,
    connect_timeout_s: float,
    read_timeout_s: float,
) -> tuple[AuthorizedProviderResult, tuple[str, ...], int]:
    started_at = _trusted_clock(clock)
    precondition = _execution_precondition(request, started_at)
    if precondition is not None:
        raw_result, warnings = _provider_result(
            request,
            precondition,
            attempts_used=0,
            completed_at=started_at,
        )
        return (
            _authorize_google_route_result(
                request.provider_request,
                raw_result,
                request.snapshot.policies,
                _token=_GOOGLE_ROUTE_AUTHORIZATION_TOKEN,
            ),
            warnings,
            0,
        )

    http_request = build_google_routes_http_request(request)
    attempts_used = 0
    terminal: _AttemptOutcome | None = None
    while attempts_used < max_attempts:
        if not attempt_budget.reserve():
            terminal = _problem_outcome(
                request,
                ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED,
            )
            break
        attempts_used += 1
        try:
            response = transport.send(
                http_request,
                connect_timeout_s=connect_timeout_s,
                read_timeout_s=read_timeout_s,
            )
            if type(response) is not GoogleRoutesHttpResponse:
                terminal = _problem_outcome(
                    request,
                    ProviderProblemCode.INVALID_PROVIDER_RESPONSE,
                )
            else:
                terminal = _decode_attempt(request, response)
        except GoogleRoutesTransportError as exc:
            code = (
                ProviderProblemCode.TIMEOUT
                if exc.kind
                in {
                    GoogleRoutesTransportErrorKind.CONNECT_TIMEOUT,
                    GoogleRoutesTransportErrorKind.READ_TIMEOUT,
                }
                else ProviderProblemCode.PROVIDER_UNAVAILABLE
            )
            terminal = _problem_outcome(request, code)
        except Exception:
            terminal = _problem_outcome(
                request,
                ProviderProblemCode.PROVIDER_UNAVAILABLE,
            )

        assert terminal is not None
        retryable = bool(
            terminal.problem is not None
            and terminal.problem.retryable
            and attempts_used < max_attempts
        )
        if not retryable:
            break
        assert sleeper is not None
        delay = terminal.retry_after_s
        if delay is None:
            delay = min(
                float(2 ** (attempts_used - 1)),
                _MAX_RETRY_DELAY_SECONDS,
            )
        sleeper(delay)

    if terminal is None:
        terminal = _problem_outcome(
            request,
            ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED,
        )
    completed_at = _trusted_clock(clock)
    raw_result, warnings = _provider_result(
        request,
        terminal,
        attempts_used=attempts_used,
        completed_at=completed_at,
    )
    authorized = _authorize_google_route_result(
        request.provider_request,
        raw_result,
        request.snapshot.policies,
        _token=_GOOGLE_ROUTE_AUTHORIZATION_TOKEN,
    )
    return authorized, warnings, attempts_used


def parse_protobuf_duration_seconds(value: Any) -> Decimal:
    """Parse a non-negative protobuf JSON Duration with nanosecond precision."""

    if not isinstance(value, str):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Google route duration must be protobuf JSON text.",
        )
    matched = _DURATION_RE.fullmatch(value)
    if matched is None:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Google route duration is malformed.",
        )
    try:
        seconds = Decimal(matched.group(1))
        if matched.group(2):
            seconds += Decimal(f"0.{matched.group(2)}")
    except InvalidOperation as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Google route duration is malformed.",
        ) from exc
    if seconds > _MAX_ROUTE_SECONDS:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Google route duration exceeds the bounded adapter range.",
        )
    return seconds


def _decode_attempt(
    request: GoogleRouteRequest,
    response: GoogleRoutesHttpResponse,
) -> _AttemptOutcome:
    if not 200 <= response.status_code <= 299:
        code = _map_http_problem(response)
        return _problem_outcome(
            request,
            code,
            retry_after_s=(
                _retry_after_seconds(response)
                if code is ProviderProblemCode.RATE_LIMITED
                else None
            ),
        )
    if not response.body.strip():
        return _problem_outcome(
            request,
            ProviderProblemCode.EMPTY_RESPONSE,
        )
    try:
        decoded = _strict_json_object(response.body)
        _exact_fields(
            decoded,
            required={"routes"},
            optional={"fallbackInfo"},
        )
        routes = decoded["routes"]
        if not isinstance(routes, list):
            raise ValueError("routes must be a list")
        if not routes:
            return _problem_outcome(
                request,
                ProviderProblemCode.NOT_FOUND,
            )
        if len(routes) != 1 or not isinstance(routes[0], dict):
            raise ValueError("exactly one route is required")
        route = routes[0]
        _exact_fields(
            route,
            required={"duration"},
            optional={"distanceMeters", "staticDuration", "warnings"},
        )
        seconds = parse_protobuf_duration_seconds(route["duration"])
        if seconds <= 0:
            raise ValueError("duration must be positive")
        payload: dict[str, Any] = {
            "mode": request.mode.value,
            "duration_min": float(seconds / Decimal(60)),
            "departure_at": request.departure_at,
        }
        if "distanceMeters" in route:
            distance = route["distanceMeters"]
            if (
                isinstance(distance, bool)
                or not isinstance(distance, int)
                or not 0 <= distance <= _MAX_DISTANCE_METERS
            ):
                raise ValueError("distanceMeters is invalid")
            payload["distance_km"] = distance / 1000
        if "staticDuration" in route:
            static_seconds = parse_protobuf_duration_seconds(
                route["staticDuration"]
            )
            payload["static_duration_min"] = float(
                static_seconds / Decimal(60)
            )
        warnings: list[str] = []
        beta_warning = _BETA_WARNING_BY_MODE.get(request.mode)
        if beta_warning is not None:
            warnings.append(beta_warning)
        if "warnings" in route:
            raw_warnings = route["warnings"]
            if (
                not isinstance(raw_warnings, list)
                or len(raw_warnings) > 16
                or any(
                    not isinstance(item, str) or len(item) > 2048
                    for item in raw_warnings
                )
            ):
                raise ValueError("route warnings are invalid")
            if raw_warnings:
                warnings.append("provider_route_warning")
        if "fallbackInfo" in decoded:
            fallback_info = decoded["fallbackInfo"]
            if not isinstance(fallback_info, dict):
                raise ValueError("fallbackInfo must be an object")
            _exact_fields(
                fallback_info,
                required=set(),
                optional={"routingMode", "reason"},
            )
            if any(
                not isinstance(value, str) or len(value) > 128
                for value in fallback_info.values()
            ):
                raise ValueError("fallbackInfo values are invalid")
            warnings.append("provider_routing_fallback")
        if request.fallback_from_mode is not None:
            payload["fallback_from_mode"] = (
                request.fallback_from_mode.value
            )
        normalized_warnings = _unique_warnings(tuple(warnings))
        if normalized_warnings:
            payload["warning_codes"] = list(normalized_warnings)
        return _AttemptOutcome(
            decoded=_DecodedRoute(
                payload=payload,
                warnings=normalized_warnings,
            ),
            problem=None,
        )
    except (FactContractError, TypeError, ValueError):
        return _problem_outcome(
            request,
            ProviderProblemCode.INVALID_PROVIDER_RESPONSE,
        )


def _provider_result(
    request: GoogleRouteRequest,
    outcome: _AttemptOutcome,
    *,
    attempts_used: int,
    completed_at: datetime,
) -> tuple[ProviderResult, tuple[str, ...]]:
    provider_request = request.provider_request
    if outcome.decoded is None:
        assert outcome.problem is not None
        return (
            ProviderResult(
                request_fingerprint=provider_request.request_fingerprint,
                status=ProviderResultStatus.FAILED,
                observations=(),
                problems=(outcome.problem,),
                attempts_used=attempts_used,
                completed_at=completed_at,
            ),
            (),
        )

    policy = request.snapshot.policies.policy(provider_request.policy_id)
    validity = min(
        _ROUTE_VALIDITY,
        timedelta(seconds=policy.max_validity_seconds),
    )
    if policy.max_retention_seconds is None:
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Google route policy must have bounded memory retention.",
        )
    retention = min(
        _ROUTE_RETENTION,
        timedelta(seconds=policy.max_retention_seconds),
    )
    value = FactValue.from_payload(
        FactKind.ROUTE_ESTIMATE,
        outcome.decoded.payload,
    )
    observation = FactObservation(
        key=provider_request.fact_keys[0],
        value=value,
        provenance=ProviderProvenance(
            provider_id=provider_request.provider_id,
            adapter_id=provider_request.adapter_id,
            adapter_version=provider_request.adapter_version,
            request_fingerprint=provider_request.request_fingerprint,
            retention_policy_id=provider_request.policy_id,
            provider_record_id=None,
            response_id=None,
            source_uri=None,
            attributions=((_ATTRIBUTION, None),),
        ),
        retrieved_at=completed_at,
        valid_until=completed_at + validity,
        purge_at=completed_at + retention,
        confidence=1.0,
    )
    return (
        ProviderResult(
            request_fingerprint=provider_request.request_fingerprint,
            status=ProviderResultStatus.SUCCESS,
            observations=(observation,),
            problems=(),
            attempts_used=attempts_used,
            completed_at=completed_at,
        ),
        outcome.decoded.warnings,
    )


def _problem_outcome(
    request: GoogleRouteRequest,
    code: ProviderProblemCode,
    *,
    retry_after_s: float | None = None,
) -> _AttemptOutcome:
    messages = {
        ProviderProblemCode.INVALID_PROVIDER_REQUEST:
            "Google Routes rejected the exact request.",
        ProviderProblemCode.INVALID_PROVIDER_RESPONSE:
            "Google Routes returned an invalid bounded response.",
        ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED:
            "The Google Routes HTTP-attempt budget is exhausted.",
        ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON:
            "The route departure is outside the supported provider horizon.",
        ProviderProblemCode.STALE_EVIDENCE:
            "A route endpoint identity expired before provider execution.",
        ProviderProblemCode.AUTH_FAILED:
            "Google Routes credentials were rejected.",
        ProviderProblemCode.QUOTA_EXHAUSTED:
            "Google Routes quota is exhausted.",
        ProviderProblemCode.RATE_LIMITED:
            "Google Routes rate-limited the request.",
        ProviderProblemCode.TIMEOUT:
            "Google Routes timed out.",
        ProviderProblemCode.PROVIDER_UNAVAILABLE:
            "Google Routes is temporarily unavailable.",
        ProviderProblemCode.EMPTY_RESPONSE:
            "Google Routes returned an empty response.",
        ProviderProblemCode.NOT_FOUND:
            "Google Routes found no route for the exact request.",
        ProviderProblemCode.UNSUPPORTED_MODE:
            "Google Routes does not support the exact route mode.",
    }
    retryable = code in {
        ProviderProblemCode.RATE_LIMITED,
        ProviderProblemCode.TIMEOUT,
        ProviderProblemCode.PROVIDER_UNAVAILABLE,
    }
    next_actions = {
        ProviderProblemCode.INVALID_PROVIDER_REQUEST: "fix_request",
        ProviderProblemCode.INVALID_PROVIDER_RESPONSE: "review_adapter",
        ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED: "increase_budget",
        ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON: "change_departure_time",
        ProviderProblemCode.STALE_EVIDENCE: "refresh_place_identity",
        ProviderProblemCode.AUTH_FAILED: "fix_credentials",
        ProviderProblemCode.QUOTA_EXHAUSTED: "review_quota",
        ProviderProblemCode.RATE_LIMITED: "retry_provider",
        ProviderProblemCode.TIMEOUT: "retry_provider",
        ProviderProblemCode.PROVIDER_UNAVAILABLE: "retry_provider",
        ProviderProblemCode.EMPTY_RESPONSE: "change_mode",
        ProviderProblemCode.NOT_FOUND: "change_mode",
        ProviderProblemCode.UNSUPPORTED_MODE: "change_mode",
    }
    return _AttemptOutcome(
        decoded=None,
        problem=ProviderProblem(
            code=code,
            message=messages[code],
            retryable=retryable,
            next_action=next_actions[code],
            fact_key_ids=(
                request.provider_request.fact_keys[0].key_id,
            ),
        ),
        retry_after_s=retry_after_s,
    )


def _map_http_problem(
    response: GoogleRoutesHttpResponse,
) -> ProviderProblemCode:
    provider_status: str | None = None
    if response.body.strip() and len(response.body) <= _MAX_RESPONSE_BYTES:
        try:
            decoded = _strict_json_object(response.body)
            error = decoded.get("error")
            if isinstance(error, dict):
                status = error.get("status")
                if isinstance(status, str):
                    provider_status = status
        except (TypeError, ValueError):
            pass
    status_code = response.status_code
    if provider_status == "RESOURCE_EXHAUSTED":
        return ProviderProblemCode.QUOTA_EXHAUSTED
    if status_code in {401, 403} or provider_status in {
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
    }:
        return ProviderProblemCode.AUTH_FAILED
    if status_code == 429:
        return ProviderProblemCode.RATE_LIMITED
    if status_code == 408 or provider_status == "DEADLINE_EXCEEDED":
        return ProviderProblemCode.TIMEOUT
    if status_code == 404 or provider_status == "NOT_FOUND":
        return ProviderProblemCode.NOT_FOUND
    if status_code == 501 or provider_status == "UNIMPLEMENTED":
        return ProviderProblemCode.UNSUPPORTED_MODE
    if status_code == 400 or provider_status in {
        "INVALID_ARGUMENT",
        "FAILED_PRECONDITION",
    }:
        return ProviderProblemCode.INVALID_PROVIDER_REQUEST
    if status_code in {500, 502, 503, 504}:
        return ProviderProblemCode.PROVIDER_UNAVAILABLE
    return ProviderProblemCode.PROVIDER_UNAVAILABLE


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    if not payload or len(payload) > _MAX_RESPONSE_BYTES:
        raise ValueError("response size is invalid")

    def object_pairs(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_raise_invalid_json()),
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("response is not strict UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("response root must be an object")
    nodes = [0]
    _validate_json_bounds(decoded, depth=0, nodes=nodes)
    return decoded


def _validate_json_bounds(
    value: Any,
    *,
    depth: int,
    nodes: list[int],
) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("response nesting is too deep")
    nodes[0] += 1
    if nodes[0] > _MAX_JSON_NODES:
        raise ValueError("response contains too many values")
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError("response object is too wide")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ValueError("response key is invalid")
            _validate_json_bounds(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, list):
        if len(value) > 128:
            raise ValueError("response list is too long")
        for child in value:
            _validate_json_bounds(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, str) and len(value) > 4096:
        raise ValueError("response text is too long")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("response number is non-finite")


def _exact_fields(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
) -> None:
    names = set(value)
    if not required.issubset(names) or not names.issubset(
        required | optional
    ):
        raise ValueError("response fields differ from the fixed field mask")


def _raise_invalid_json() -> None:
    raise ValueError("non-finite JSON number")


def _retry_after_seconds(
    response: GoogleRoutesHttpResponse,
) -> float | None:
    value = response.header("retry-after")
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return min(parsed, _MAX_RETRY_DELAY_SECONDS)


def _validate_execution_options(
    *,
    max_attempts: int,
    sleeper: Callable[[float], None] | None,
    connect_timeout_s: float,
    read_timeout_s: float,
) -> None:
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= _MAX_ATTEMPTS_PER_REQUEST
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "max_attempts must be between 1 and 3.",
        )
    if max_attempts > 1 and sleeper is None:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Bounded retries require an injected sleeper.",
        )
    for value, name in (
        (connect_timeout_s, "connect_timeout_s"),
        (read_timeout_s, "read_timeout_s"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < float(value) <= 60
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                f"{name} must be a finite value in (0, 60].",
            )


def _trusted_clock(clock: Callable[[], datetime]) -> datetime:
    if not callable(clock):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "clock must be callable.",
        )
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "clock must return an aware datetime.",
        )
    return value.astimezone(timezone.utc)


class _RunClock:
    """Clamp one primary/fallback execution to a non-regressing UTC clock."""

    def __init__(
        self,
        source: Callable[[], datetime],
        *,
        floor: datetime,
    ) -> None:
        if not callable(source):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "clock must be callable.",
            )
        self._source = source
        self._last = floor.astimezone(timezone.utc)

    def now(self) -> datetime:
        current = _trusted_clock(self._source)
        if current < self._last:
            current = self._last
        self._last = current
        return current


def _execution_precondition(
    request: GoogleRouteRequest,
    executed_at: datetime,
) -> _AttemptOutcome | None:
    if (
        request.origin.valid_until <= executed_at
        or request.destination.valid_until <= executed_at
    ):
        return _problem_outcome(
            request,
            ProviderProblemCode.STALE_EVIDENCE,
        )
    departure_at = _parse_rfc3339(request.departure_at)
    if request.mode is RouteMode.TRANSIT:
        if not (
            executed_at - timedelta(days=7)
            <= departure_at
            <= executed_at + timedelta(days=100)
        ):
            return _problem_outcome(
                request,
                ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON,
            )
    elif departure_at < executed_at:
        return _problem_outcome(
            request,
            ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON,
        )
    return None


def _parse_rfc3339(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "departure_at must be an RFC 3339 timestamp.",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "departure_at must include a UTC offset.",
        )
    return parsed.astimezone(timezone.utc)


def _unique_warnings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))
