"""Bounded Google Place Details adapter for runtime-only facts.

Credentials belong to the injected transport.  Raw response bytes, raw Place
IDs and provider text never enter safe request bindings or durable storage.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .evidence_session import (
    EvidenceSession,
    EvidenceSessionLoad,
    EvidenceSessionMerge,
)
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
    _GOOGLE_PLACE_CURRENT_HOURS_FIELD_MASK,
    _GOOGLE_PLACE_DETAILS_AUTHORIZATION_TOKEN,
    _GOOGLE_PLACE_PROFILE_FIELD_MASK,
    _GOOGLE_PLACE_REGULAR_HOURS_FIELD_MASK,
    _authorize_google_place_details_result,
)
from .places_identity import PlaceEndpointIdentity


GOOGLE_PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places"
GOOGLE_PLACE_PROFILE_FIELD_MASK = _GOOGLE_PLACE_PROFILE_FIELD_MASK
GOOGLE_PLACE_CURRENT_HOURS_FIELD_MASK = (
    _GOOGLE_PLACE_CURRENT_HOURS_FIELD_MASK
)
GOOGLE_PLACE_REGULAR_HOURS_FIELD_MASK = (
    _GOOGLE_PLACE_REGULAR_HOURS_FIELD_MASK
)

_PROVIDER_ID = "google-places"
_ADAPTER_VERSION = "v1"
_GOOGLE_MAPS_ATTRIBUTION = "Google Maps"
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 512
_MAX_ATTEMPTS_PER_REQUEST = 3
_MAX_RETRY_DELAY_SECONDS = 30.0
_MAX_ATTRIBUTIONS = 16
_PROFILE_VALIDITY = timedelta(hours=12)
_HOURS_VALIDITY = timedelta(hours=12)
_MEMORY_RETENTION = timedelta(days=1)
_REQUEST_TOKEN = object()
_HTTP_REQUEST_TOKEN = object()
_LANGUAGE_CODE_RE = re.compile(
    r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*"
)


class PlaceDetailsKind(str, Enum):
    """The three separately policy-bound Place Details requests."""

    PROFILE = "profile"
    CURRENT_HOURS = "current_hours"
    REGULAR_HOURS = "regular_hours"

    @property
    def fact_kind(self) -> FactKind:
        if self is PlaceDetailsKind.PROFILE:
            return FactKind.PLACE_PROFILE
        return FactKind.PLACE_OPENING_HOURS

    @property
    def policy_id(self) -> str:
        if self is PlaceDetailsKind.PROFILE:
            return "google-place-profile-runtime-v1"
        return "google-place-hours-runtime-v1"

    @property
    def operation(self) -> str:
        if self is PlaceDetailsKind.PROFILE:
            return "fetch-place-profile"
        return "fetch-opening-hours"

    @property
    def field_mask(self) -> str:
        return {
            PlaceDetailsKind.PROFILE:
                GOOGLE_PLACE_PROFILE_FIELD_MASK,
            PlaceDetailsKind.CURRENT_HOURS:
                GOOGLE_PLACE_CURRENT_HOURS_FIELD_MASK,
            PlaceDetailsKind.REGULAR_HOURS:
                GOOGLE_PLACE_REGULAR_HOURS_FIELD_MASK,
        }[self]

    @property
    def basis(self) -> str | None:
        return {
            PlaceDetailsKind.CURRENT_HOURS: "current",
            PlaceDetailsKind.REGULAR_HOURS: "regular_typical",
        }.get(self)


@dataclass(frozen=True, slots=True, init=False, repr=False)
class GooglePlaceDetailsRequest:
    """Token-gated request bound to one snapshot and fresh Place endpoint."""

    snapshot: EvidenceSnapshot = field(repr=False)
    endpoint: PlaceEndpointIdentity = field(repr=False)
    kind: PlaceDetailsKind
    language_code: str
    region_code: str
    target_start: date | None
    target_end: date | None
    provider_request: ProviderRequest
    field_mask: str

    def __init__(
        self,
        *,
        snapshot: EvidenceSnapshot,
        endpoint: PlaceEndpointIdentity,
        kind: PlaceDetailsKind,
        language_code: str,
        region_code: str,
        target_start: date | None,
        target_end: date | None,
        provider_request: ProviderRequest,
        field_mask: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _REQUEST_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                (
                    "Google Place Details requests can only be created by "
                    "the trusted host factory."
                ),
            )
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "language_code", language_code)
        object.__setattr__(self, "region_code", region_code)
        object.__setattr__(self, "target_start", target_start)
        object.__setattr__(self, "target_end", target_end)
        object.__setattr__(self, "provider_request", provider_request)
        object.__setattr__(self, "field_mask", field_mask)
        self.__post_init__()

    def __post_init__(self) -> None:
        if (
            type(self.snapshot) is not EvidenceSnapshot
            or type(self.endpoint) is not PlaceEndpointIdentity
            or type(self.kind) is not PlaceDetailsKind
            or type(self.provider_request) is not ProviderRequest
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Place Details requires exact trusted contract values.",
            )
        if self.endpoint.snapshot_id != self.snapshot.snapshot_id:
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                (
                    "Place Details endpoint must come from the exact "
                    "request snapshot."
                ),
            )
        if self.endpoint.valid_until <= self.snapshot.evaluation_at:
            raise FactContractError(
                "STALE_EVIDENCE",
                "Place Details endpoint must be fresh in its request snapshot.",
            )
        identity = next(
            (
                item
                for item in self.snapshot.observations
                if item.observation_id == self.endpoint.observation_id
            ),
            None,
        )
        if (
            self.endpoint.provider_id != _PROVIDER_ID
            or identity is None
            or identity.key.kind is not FactKind.PLACE_IDENTITY
            or identity.key.subject_ids != (self.endpoint.location_id,)
            or identity.provenance.provider_id != _PROVIDER_ID
            or identity.value.payload.get("provider_place_id")
            != self.endpoint.provider_place_id
            or identity.value.value_digest != self.endpoint.value_digest
            or identity.valid_until != self.endpoint.valid_until
            or not identity.fresh_at(self.snapshot.evaluation_at)
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                (
                    "Place Details endpoint differs from the active Google "
                    "Place identity in its exact snapshot."
                ),
            )
        if (
            not isinstance(self.language_code, str)
            or _LANGUAGE_CODE_RE.fullmatch(self.language_code) is None
            or len(self.language_code) > 35
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "language_code must be a bounded BCP-47 tag.",
            )
        if (
            not isinstance(self.region_code, str)
            or re.fullmatch(r"[A-Z]{2}", self.region_code) is None
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "region_code must be an uppercase two-letter region.",
            )
        if self.field_mask != self.kind.field_mask:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Place Details requires its fixed minimal field mask.",
            )

        request = self.provider_request
        policy = self.snapshot.policies.policy(self.kind.policy_id)
        if (
            request.provider_id != _PROVIDER_ID
            or request.adapter_id != _PROVIDER_ID
            or request.adapter_version != _ADAPTER_VERSION
            or request.operation != self.kind.operation
            or request.policy_id != policy.policy_id
            or request.policy_digest != policy.policy_digest
            or policy.persistence is not EvidencePersistence.MEMORY_ONLY
            or len(request.fact_keys) != 1
        ):
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Place Details request differs from its static memory policy.",
            )

        key = request.fact_keys[0]
        qualifiers = key.qualifier_map
        if (
            key.kind is not self.kind.fact_kind
            or key.subject_ids != (self.endpoint.location_id,)
            or qualifiers.get("identity_provider") != _PROVIDER_ID
            or qualifiers.get("provider_place_id")
            != self.endpoint.provider_place_id
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Place Details fact key differs from its endpoint identity.",
            )

        expected_scope: dict[str, object] = {
            "basis_evidence_revision": self.snapshot.evidence_revision,
            "basis_snapshot_id": self.snapshot.snapshot_id,
            "basis_store_revision": self.snapshot.store_revision,
            "field_mask": self.field_mask,
            "identity_endpoint_id": self.endpoint.endpoint_id,
            "identity_observation_id": self.endpoint.observation_id,
            "identity_value_digest": self.endpoint.value_digest,
            "language_code": self.language_code,
            "region_code": self.region_code,
        }
        if self.kind is PlaceDetailsKind.PROFILE:
            if self.target_start is not None or self.target_end is not None:
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    "Place profiles cannot declare an hours target.",
                )
        else:
            if (
                type(self.target_start) is not date
                or type(self.target_end) is not date
                or self.target_end < self.target_start
                or (self.target_end - self.target_start).days > 30
            ):
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    (
                        "Opening-hours target must be an ordered range of "
                        "at most 31 dates."
                    ),
                )
            if (
                self.kind is PlaceDetailsKind.CURRENT_HOURS
                and (self.target_end - self.target_start).days > 6
            ):
                raise FactContractError(
                    "OUTSIDE_PROVIDER_HORIZON",
                    "Current opening-hours requests cannot exceed seven dates.",
                )
            expected_scope.update(
                {
                    "basis": self.kind.basis,
                    "target_start": self.target_start.isoformat(),
                    "target_end": self.target_end.isoformat(),
                }
            )
            if (
                qualifiers.get("basis") != self.kind.basis
                or qualifiers.get("target_start")
                != self.target_start.isoformat()
                or qualifiers.get("target_end")
                != self.target_end.isoformat()
            ):
                raise FactContractError(
                    "EVIDENCE_BINDING_MISMATCH",
                    (
                        "Opening-hours fact key differs from its exact "
                        "basis/date request."
                    ),
                )
        if dict(request.query_scope) != expected_scope:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                (
                    "Place Details query scope differs from its exact "
                    "snapshot, endpoint, locale or target binding."
                ),
            )

    def __repr__(self) -> str:
        return (
            "GooglePlaceDetailsRequest("
            f"kind={self.kind.value!r}, "
            "request_fingerprint="
            f"{self.provider_request.request_fingerprint!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        """Return a secret-free binding without raw Place ID."""

        return {
            "endpoint": self.endpoint.to_binding_dict(),
            "kind": self.kind.value,
            "language_code": self.language_code,
            "region_code": self.region_code,
            "target_start": (
                self.target_start.isoformat()
                if self.target_start is not None
                else None
            ),
            "target_end": (
                self.target_end.isoformat()
                if self.target_end is not None
                else None
            ),
            "field_mask": self.field_mask,
            "provider_request": self.provider_request.to_binding_dict(),
        }


def build_google_place_details_request(
    snapshot: EvidenceSnapshot,
    endpoint: PlaceEndpointIdentity,
    kind: PlaceDetailsKind,
    *,
    language_code: str,
    region_code: str,
    target_start: date | None = None,
    target_end: date | None = None,
) -> GooglePlaceDetailsRequest:
    """Build one exact request without performing transport I/O."""

    if (
        type(snapshot) is not EvidenceSnapshot
        or type(endpoint) is not PlaceEndpointIdentity
        or type(kind) is not PlaceDetailsKind
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Place Details factory requires exact trusted values.",
        )
    qualifiers: list[tuple[str, object]] = [
        ("identity_provider", _PROVIDER_ID),
        ("provider_place_id", endpoint.provider_place_id),
    ]
    query_scope: list[tuple[str, object]] = [
        ("basis_evidence_revision", snapshot.evidence_revision),
        ("basis_snapshot_id", snapshot.snapshot_id),
        ("basis_store_revision", snapshot.store_revision),
        ("field_mask", kind.field_mask),
        ("identity_endpoint_id", endpoint.endpoint_id),
        ("identity_observation_id", endpoint.observation_id),
        ("identity_value_digest", endpoint.value_digest),
        ("language_code", language_code),
        ("region_code", region_code),
    ]
    if kind is not PlaceDetailsKind.PROFILE:
        if type(target_start) is not date or type(target_end) is not date:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Opening-hours requests require exact date values.",
            )
        qualifiers.extend(
            (
                ("basis", kind.basis),
                ("target_start", target_start.isoformat()),
                ("target_end", target_end.isoformat()),
            )
        )
        query_scope.extend(
            (
                ("basis", kind.basis),
                ("target_start", target_start.isoformat()),
                ("target_end", target_end.isoformat()),
            )
        )

    key = FactKey(
        kind=kind.fact_kind,
        subject_ids=(endpoint.location_id,),
        qualifiers=tuple(qualifiers),
    )
    policy = snapshot.policies.policy(kind.policy_id)
    provider_request = ProviderRequest(
        provider_id=_PROVIDER_ID,
        adapter_id=_PROVIDER_ID,
        adapter_version=_ADAPTER_VERSION,
        operation=kind.operation,
        fact_keys=(key,),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
        query_scope=tuple(query_scope),
    )
    return GooglePlaceDetailsRequest(
        snapshot=snapshot,
        endpoint=endpoint,
        kind=kind,
        language_code=language_code,
        region_code=region_code,
        target_start=target_start,
        target_end=target_end,
        provider_request=provider_request,
        field_mask=kind.field_mask,
        _token=_REQUEST_TOKEN,
    )


@dataclass(frozen=True, slots=True, init=False, repr=False)
class GooglePlaceDetailsHttpRequest:
    """Fixed GET request descriptor; the raw URL is runtime-only."""

    url: str = field(repr=False)
    field_mask: str = field(repr=False)

    def __init__(
        self,
        *,
        url: str,
        field_mask: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _HTTP_REQUEST_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Place Details HTTP requests require the trusted factory.",
            )
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "field_mask", field_mask)
        self.__post_init__()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.url, str)
            or not self.url.startswith(f"{GOOGLE_PLACE_DETAILS_URL}/")
            or self.field_mask
            not in {
                GOOGLE_PLACE_PROFILE_FIELD_MASK,
                GOOGLE_PLACE_CURRENT_HOURS_FIELD_MASK,
                GOOGLE_PLACE_REGULAR_HOURS_FIELD_MASK,
            }
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Place Details HTTP descriptor differs from its fixed boundary.",
            )

    @property
    def headers(self) -> Mapping[str, str]:
        return {"X-Goog-FieldMask": self.field_mask}

    def __repr__(self) -> str:
        return (
            "GooglePlaceDetailsHttpRequest("
            f"url_digest={self.url_digest!r})"
        )

    @property
    def url_digest(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "method": "GET",
            "url_digest": self.url_digest,
            "query_field_names": ["languageCode", "regionCode"],
            "field_mask": self.field_mask,
        }


def build_google_place_details_http_request(
    request: GooglePlaceDetailsRequest,
) -> GooglePlaceDetailsHttpRequest:
    if type(request) is not GooglePlaceDetailsRequest:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "request must be an exact GooglePlaceDetailsRequest.",
        )
    query = urlencode(
        {
            "languageCode": request.language_code,
            "regionCode": request.region_code,
        }
    )
    place_path = quote(request.endpoint.provider_place_id, safe="")
    return GooglePlaceDetailsHttpRequest(
        url=f"{GOOGLE_PLACE_DETAILS_URL}/{place_path}?{query}",
        field_mask=request.field_mask,
        _token=_HTTP_REQUEST_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class GooglePlaceDetailsHttpResponse:
    """Bounded transport response; body and headers remain runtime-only."""

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
            raise TypeError("Place Details response body must be bytes")
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
                or not 1 <= len(item[0]) <= 256
                or len(item[1]) > 4096
                or any(
                    ord(char) < 32 and char != "\t"
                    for value in item
                    for char in value
                )
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


class GooglePlaceDetailsTransportErrorKind(str, Enum):
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    NETWORK = "network"
    TLS = "tls"


class GooglePlaceDetailsTransportError(RuntimeError):
    """Sanitized transport failure; never wrap the raw exception text."""

    def __init__(
        self,
        kind: GooglePlaceDetailsTransportErrorKind,
    ) -> None:
        if type(kind) is not GooglePlaceDetailsTransportErrorKind:
            raise TypeError(
                "kind must be GooglePlaceDetailsTransportErrorKind"
            )
        self.kind = kind
        super().__init__(
            f"Google Place Details transport failed ({kind.value})."
        )


@runtime_checkable
class GooglePlaceDetailsTransport(Protocol):
    def send(
        self,
        request: GooglePlaceDetailsHttpRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GooglePlaceDetailsHttpResponse:
        """Send one request without exposing credentials to the adapter."""


class PlaceDetailsAttemptBudget:
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


@dataclass(frozen=True, slots=True)
class GooglePlaceDetailsExecution:
    """One authorized Place Details outcome."""

    request: GooglePlaceDetailsRequest
    result: AuthorizedProviderResult = field(repr=False)
    attempts_used: int

    def __post_init__(self) -> None:
        if (
            type(self.request) is not GooglePlaceDetailsRequest
            or type(self.result) is not AuthorizedProviderResult
        ):
            raise TypeError(
                "Place Details execution requires exact request/result values"
            )
        if (
            self.result.request.request_fingerprint
            != self.request.provider_request.request_fingerprint
            or self.attempts_used != self.result.result.attempts_used
        ):
            raise ValueError(
                "Place Details execution differs from its provider result"
            )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_binding_dict(),
            "result": self.result.to_binding_dict(),
            "attempts_used": self.attempts_used,
        }


@dataclass(frozen=True, slots=True)
class GooglePlaceDetailsBatchExecution:
    """Per-request outcomes merged into one run-scoped evidence session."""

    executions: tuple[GooglePlaceDetailsExecution, ...]
    merges: tuple[EvidenceSessionMerge, ...] = field(repr=False)
    current: EvidenceSessionLoad
    attempts_used: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.executions, tuple)
            or not self.executions
            or any(
                type(item) is not GooglePlaceDetailsExecution
                for item in self.executions
            )
        ):
            raise TypeError(
                "Place Details batches require exact non-empty executions"
            )
        if (
            not isinstance(self.merges, tuple)
            or len(self.merges) != len(self.executions)
            or any(
                type(item) is not EvidenceSessionMerge
                for item in self.merges
            )
        ):
            raise TypeError(
                "Place Details batch merges must match its executions"
            )
        if (
            type(self.current) is not EvidenceSessionLoad
            or self.current != self.merges[-1].current
        ):
            raise ValueError(
                "Place Details batch current load must equal its final merge"
            )
        expected_attempts = sum(
            item.attempts_used for item in self.executions
        )
        if self.attempts_used != expected_attempts:
            raise ValueError(
                "Place Details batch attempts differ from its executions"
            )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "executions": [
                item.to_binding_dict() for item in self.executions
            ],
            "current": self.current.to_dict(),
            "attempts_used": self.attempts_used,
        }


@dataclass(frozen=True, slots=True)
class _DecodedPlace:
    payload: dict[str, Any] = field(repr=False)
    attributions: tuple[tuple[str, str | None], ...] = field(
        repr=False
    )


@dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    decoded: _DecodedPlace | None = field(default=None, repr=False)
    problem: ProviderProblem | None = None
    retry_after_s: float | None = None

    def __post_init__(self) -> None:
        if (self.decoded is None) == (self.problem is None):
            raise ValueError(
                "Attempt outcome requires exactly one value or problem"
            )


class _DecodeFailure(ValueError):
    def __init__(self, code: ProviderProblemCode) -> None:
        self.code = code
        super().__init__(code.value)


class _RunClock:
    """Trusted UTC clock that cannot move backwards within one execution."""

    def __init__(self, source: Callable[[], datetime]) -> None:
        if not callable(source):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "clock must be callable.",
            )
        self._source = source
        self._last: datetime | None = None

    def now(self) -> datetime:
        value = _trusted_clock(self._source)
        if self._last is not None and value < self._last:
            value = self._last
        self._last = value
        return value


def execute_google_place_details(
    request: GooglePlaceDetailsRequest,
    transport: GooglePlaceDetailsTransport,
    *,
    attempt_budget: PlaceDetailsAttemptBudget,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] | None = None,
    max_attempts: int = 1,
    connect_timeout_s: float = 3.0,
    read_timeout_s: float = 15.0,
) -> GooglePlaceDetailsExecution:
    """Execute one exact request with bounded retry and no built-in client."""

    if type(request) is not GooglePlaceDetailsRequest:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "request must be an exact GooglePlaceDetailsRequest.",
        )
    if type(attempt_budget) is not PlaceDetailsAttemptBudget:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "attempt_budget must be an exact PlaceDetailsAttemptBudget.",
        )
    _validate_execution_options(
        max_attempts=max_attempts,
        sleeper=sleeper,
        connect_timeout_s=connect_timeout_s,
        read_timeout_s=read_timeout_s,
    )
    run_clock = _RunClock(clock)
    started_at = run_clock.now()
    if request.endpoint.valid_until <= started_at:
        problem = _problem_outcome(
            request,
            ProviderProblemCode.STALE_EVIDENCE,
        )
        raw = _provider_result(
            request,
            problem,
            attempts_used=0,
            completed_at=started_at,
        )
        return GooglePlaceDetailsExecution(
            request=request,
            result=_authorize_result(request, raw),
            attempts_used=0,
        )

    http_request = build_google_place_details_http_request(request)
    attempts_used = 0
    terminal: _AttemptOutcome | None = None
    completed_at = started_at
    retrieved_at: datetime | None = None
    while attempts_used < max_attempts:
        sent_at = run_clock.now()
        if request.endpoint.valid_until <= sent_at:
            terminal = _problem_outcome(
                request,
                ProviderProblemCode.STALE_EVIDENCE,
            )
            break
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
        except GooglePlaceDetailsTransportError as exc:
            code = (
                ProviderProblemCode.TIMEOUT
                if exc.kind
                in {
                    GooglePlaceDetailsTransportErrorKind.CONNECT_TIMEOUT,
                    GooglePlaceDetailsTransportErrorKind.READ_TIMEOUT,
                }
                else ProviderProblemCode.PROVIDER_UNAVAILABLE
            )
            terminal = _problem_outcome(request, code)
        except Exception:
            terminal = _problem_outcome(
                request,
                ProviderProblemCode.PROVIDER_UNAVAILABLE,
            )
        else:
            completed_at = run_clock.now()
            if type(response) is not GooglePlaceDetailsHttpResponse:
                terminal = _problem_outcome(
                    request,
                    ProviderProblemCode.INVALID_PROVIDER_RESPONSE,
                )
            else:
                terminal = _decode_attempt(
                    request,
                    response,
                    retrieved_at=sent_at,
                )
                if terminal.decoded is not None:
                    retrieved_at = sent_at

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
        completed_at = run_clock.now()

    if terminal is None:
        terminal = _problem_outcome(
            request,
            ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED,
        )
    completed_at = run_clock.now()
    raw_result = _provider_result(
        request,
        terminal,
        attempts_used=attempts_used,
        completed_at=completed_at,
        retrieved_at=retrieved_at,
    )
    return GooglePlaceDetailsExecution(
        request=request,
        result=_authorize_result(request, raw_result),
        attempts_used=attempts_used,
    )


def execute_google_place_details_batch(
    requests: tuple[GooglePlaceDetailsRequest, ...],
    transport: GooglePlaceDetailsTransport,
    *,
    session: EvidenceSession,
    attempt_budget: PlaceDetailsAttemptBudget,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] | None = None,
    max_attempts: int = 1,
    connect_timeout_s: float = 3.0,
    read_timeout_s: float = 15.0,
) -> GooglePlaceDetailsBatchExecution:
    """Execute 1..256 exact requests and merge only into one memory session."""

    if (
        not isinstance(requests, tuple)
        or not 1 <= len(requests) <= 256
        or any(
            type(item) is not GooglePlaceDetailsRequest
            for item in requests
        )
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Place Details batch must contain 1 to 256 exact requests.",
        )
    if type(session) is not EvidenceSession:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Place Details batch requires an exact EvidenceSession.",
        )
    if type(attempt_budget) is not PlaceDetailsAttemptBudget:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Place Details batch requires an exact attempt budget.",
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
            "Place Details batch cannot contain duplicate exact requests.",
        )
    if len({item.snapshot.snapshot_id for item in requests}) != 1:
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Place Details batch requests must share one exact snapshot.",
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
            "Evidence changed before Place Details batch execution.",
        )
    endpoint_ids = {
        request.endpoint.observation_id for request in requests
    }
    if not endpoint_ids.issubset(active_ids):
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Place Details endpoint identity is no longer current.",
        )

    executions: list[GooglePlaceDetailsExecution] = []
    merges: list[EvidenceSessionMerge] = []
    for request in requests:
        execution = execute_google_place_details(
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
        merges.append(session.merge(execution.result))
    return GooglePlaceDetailsBatchExecution(
        executions=tuple(executions),
        merges=tuple(merges),
        current=merges[-1].current,
        attempts_used=sum(
            item.attempts_used for item in executions
        ),
    )


def _authorize_result(
    request: GooglePlaceDetailsRequest,
    result: ProviderResult,
) -> AuthorizedProviderResult:
    return _authorize_google_place_details_result(
        request.provider_request,
        result,
        request.snapshot.policies,
        _token=_GOOGLE_PLACE_DETAILS_AUTHORIZATION_TOKEN,
    )


def _decode_attempt(
    request: GooglePlaceDetailsRequest,
    response: GooglePlaceDetailsHttpResponse,
    *,
    retrieved_at: datetime,
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
        raw = _strict_json_object(response.body)
        decoded = _decode_place_payload(
            request,
            raw,
            retrieved_at=retrieved_at,
        )
    except _DecodeFailure as exc:
        return _problem_outcome(request, exc.code)
    except (FactContractError, TypeError, ValueError):
        return _problem_outcome(
            request,
            ProviderProblemCode.INVALID_PROVIDER_RESPONSE,
        )
    return _AttemptOutcome(decoded=decoded)


def _decode_place_payload(
    request: GooglePlaceDetailsRequest,
    raw: dict[str, Any],
    *,
    retrieved_at: datetime,
) -> _DecodedPlace:
    if request.kind is PlaceDetailsKind.PROFILE:
        return _decode_profile(request, raw)
    return _decode_hours(
        request,
        raw,
        retrieved_at=retrieved_at,
    )


def _decode_profile(
    request: GooglePlaceDetailsRequest,
    raw: dict[str, Any],
) -> _DecodedPlace:
    _exact_fields(
        raw,
        required={"id", "location"},
        optional={
            "attributions",
            "businessStatus",
            "displayName",
            "timeZone",
        },
    )
    _validate_returned_place_id(request, raw["id"])
    location = raw["location"]
    if not isinstance(location, dict):
        raise ValueError("location must be an object")
    _exact_fields(
        location,
        required={"latitude", "longitude"},
        optional=set(),
    )
    payload: dict[str, Any] = {
        "provider_place_id": request.endpoint.provider_place_id,
        "latitude": location["latitude"],
        "longitude": location["longitude"],
    }
    if "displayName" in raw:
        display_name = raw["displayName"]
        if not isinstance(display_name, dict):
            raise ValueError("displayName must be an object")
        _exact_fields(
            display_name,
            required={"text"},
            optional=set(),
        )
        payload["display_name"] = display_name["text"]
    if "businessStatus" in raw:
        status = raw["businessStatus"]
        if not isinstance(status, str):
            raise ValueError("businessStatus must be text")
        payload["business_status"] = status.lower()
    if "timeZone" in raw:
        timezone_name, _zone = _decode_timezone(raw["timeZone"])
        payload["timezone"] = timezone_name
    return _DecodedPlace(
        payload=payload,
        attributions=_decode_attributions(raw.get("attributions", [])),
    )


def _decode_hours(
    request: GooglePlaceDetailsRequest,
    raw: dict[str, Any],
    *,
    retrieved_at: datetime,
) -> _DecodedPlace:
    field_name = (
        "currentOpeningHours"
        if request.kind is PlaceDetailsKind.CURRENT_HOURS
        else "regularOpeningHours"
    )
    _exact_fields(
        raw,
        required={"id", "timeZone"},
        optional={"attributions", field_name},
    )
    _validate_returned_place_id(request, raw["id"])
    timezone_name, zone = _decode_timezone(raw["timeZone"])
    hours = raw.get(field_name)
    if hours is None:
        raise _DecodeFailure(ProviderProblemCode.EMPTY_RESPONSE)
    if not isinstance(hours, dict):
        raise ValueError("opening hours must be an object")
    _exact_fields(
        hours,
        required=set(),
        optional={"periods", "specialDays"},
    )
    if "periods" not in hours:
        # Absent means unknown; an explicitly empty list means never open.
        raise _DecodeFailure(ProviderProblemCode.EMPTY_RESPONSE)
    periods = hours["periods"]
    if not isinstance(periods, list):
        raise ValueError("periods must be a list")
    if len(periods) > 128:
        raise ValueError("periods exceeds the bounded adapter limit")

    assert request.target_start is not None
    assert request.target_end is not None
    if request.kind is PlaceDetailsKind.CURRENT_HOURS:
        coverage_start = retrieved_at.astimezone(zone).date()
        coverage_end = coverage_start + timedelta(days=6)
        if (
            request.target_start < coverage_start
            or request.target_end > coverage_end
        ):
            raise _DecodeFailure(
                ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON
            )
        _validate_special_days(
            hours.get("specialDays", []),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )
        intervals = _decode_current_periods(
            periods,
            zone=zone,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )
        basis = "current"
    else:
        if "specialDays" in hours:
            raise ValueError(
                "regular opening hours cannot contain specialDays"
            )
        coverage_start = request.target_start
        coverage_end = request.target_end
        intervals = _decode_regular_periods(
            periods,
            zone=zone,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )
        basis = "regular_typical"

    closed_dates = _closed_dates(
        intervals,
        zone=zone,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    payload = {
        "provider_place_id": request.endpoint.provider_place_id,
        "timezone": timezone_name,
        "basis": basis,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "intervals": [
            {
                "start_at": _utc_iso(start_at),
                "end_at": _utc_iso(end_at),
            }
            for start_at, end_at in intervals
        ],
        "closed_dates": [
            item.isoformat() for item in closed_dates
        ],
    }
    return _DecodedPlace(
        payload=payload,
        attributions=_decode_attributions(raw.get("attributions", [])),
    )


def _decode_current_periods(
    periods: list[Any],
    *,
    zone: ZoneInfo,
    coverage_start: date,
    coverage_end: date,
) -> tuple[tuple[datetime, datetime], ...]:
    coverage_start_at = _localize_exact(
        coverage_start,
        time.min,
        zone,
    )
    coverage_end_at = _localize_exact(
        coverage_end + timedelta(days=1),
        time.min,
        zone,
    )
    if len(periods) == 1:
        only = periods[0]
        if (
            isinstance(only, dict)
            and set(only) == {"open"}
            and _current_open_is_coverage_start(
                only["open"],
                zone=zone,
                coverage_start=coverage_start,
            )
        ):
            return _split_at_local_midnights(
                coverage_start_at,
                coverage_end_at,
                zone=zone,
            )

    intervals: list[tuple[datetime, datetime]] = []
    for period in periods:
        if not isinstance(period, dict):
            raise ValueError("period must be an object")
        _exact_fields(
            period,
            required={"open", "close"},
            optional=set(),
        )
        start_at, open_truncated = _decode_current_point(
            period["open"],
            zone=zone,
        )
        end_at, close_truncated = _decode_current_point(
            period["close"],
            zone=zone,
        )
        if end_at <= start_at:
            raise ValueError("opening period must have positive duration")
        if start_at < coverage_start_at or end_at > coverage_end_at:
            raise ValueError("current period exceeds its seven-day coverage")
        if open_truncated and start_at != coverage_start_at:
            raise ValueError(
                "truncated open point must equal the coverage boundary"
            )
        if close_truncated and end_at != coverage_end_at:
            raise ValueError(
                "truncated close point must equal the coverage boundary"
            )
        intervals.extend(
            _split_at_local_midnights(start_at, end_at, zone=zone)
        )
    return _normalize_intervals(intervals)


def _decode_regular_periods(
    periods: list[Any],
    *,
    zone: ZoneInfo,
    coverage_start: date,
    coverage_end: date,
) -> tuple[tuple[datetime, datetime], ...]:
    coverage_start_at = _localize_exact(
        coverage_start,
        time.min,
        zone,
    )
    coverage_end_at = _localize_exact(
        coverage_end + timedelta(days=1),
        time.min,
        zone,
    )
    if len(periods) == 1 and _is_regular_always_open(periods[0]):
        return _split_at_local_midnights(
            coverage_start_at,
            coverage_end_at,
            zone=zone,
        )

    intervals: list[tuple[datetime, datetime]] = []
    candidate_start = coverage_start - timedelta(days=1)
    candidate_end = coverage_end
    for period in periods:
        if not isinstance(period, dict):
            raise ValueError("period must be an object")
        _exact_fields(
            period,
            required={"open", "close"},
            optional=set(),
        )
        open_day, open_clock = _decode_regular_point(period["open"])
        close_day, close_clock = _decode_regular_point(period["close"])
        day_delta = (close_day - open_day) % 7
        if day_delta not in {0, 1}:
            raise ValueError(
                "regular period cannot span more than the next local day"
            )
        for offset in range(
            (candidate_end - candidate_start).days + 1
        ):
            local_date = candidate_start + timedelta(days=offset)
            if _google_weekday(local_date) != open_day:
                continue
            start_at = _localize_exact(local_date, open_clock, zone)
            end_date = local_date + timedelta(days=day_delta)
            end_at = _localize_exact(end_date, close_clock, zone)
            if end_at <= start_at:
                raise ValueError(
                    "regular period must have positive duration"
                )
            clipped_start = max(start_at, coverage_start_at)
            clipped_end = min(end_at, coverage_end_at)
            if clipped_end <= clipped_start:
                continue
            intervals.extend(
                _split_at_local_midnights(
                    clipped_start,
                    clipped_end,
                    zone=zone,
                )
            )
    return _normalize_intervals(intervals)


def _decode_current_point(
    raw: Any,
    *,
    zone: ZoneInfo,
) -> tuple[datetime, bool]:
    if not isinstance(raw, dict):
        raise ValueError("Point must be an object")
    _exact_fields(
        raw,
        required={"date", "day"},
        optional={"hour", "minute", "truncated"},
    )
    point_date = _decode_date(raw["date"])
    day = _bounded_int(raw["day"], "Point.day", minimum=0, maximum=6)
    if day != _google_weekday(point_date):
        raise ValueError("Point day does not match its local date")
    hour = _bounded_int(
        raw.get("hour", 0),
        "Point.hour",
        minimum=0,
        maximum=23,
    )
    minute = _bounded_int(
        raw.get("minute", 0),
        "Point.minute",
        minimum=0,
        maximum=59,
    )
    truncated = raw.get("truncated", False)
    if not isinstance(truncated, bool):
        raise ValueError("Point.truncated must be bool")
    return (
        _localize_exact(
            point_date,
            time(hour, minute),
            zone,
        ),
        truncated,
    )


def _decode_regular_point(raw: Any) -> tuple[int, time]:
    if not isinstance(raw, dict):
        raise ValueError("Point must be an object")
    _exact_fields(
        raw,
        required={"day"},
        optional={"hour", "minute", "truncated"},
    )
    if raw.get("truncated", False) is not False:
        raise ValueError("regular Point cannot be truncated")
    day = _bounded_int(raw["day"], "Point.day", minimum=0, maximum=6)
    hour = _bounded_int(
        raw.get("hour", 0),
        "Point.hour",
        minimum=0,
        maximum=23,
    )
    minute = _bounded_int(
        raw.get("minute", 0),
        "Point.minute",
        minimum=0,
        maximum=59,
    )
    return day, time(hour, minute)


def _current_open_is_coverage_start(
    raw: Any,
    *,
    zone: ZoneInfo,
    coverage_start: date,
) -> bool:
    try:
        start_at, _truncated = _decode_current_point(raw, zone=zone)
    except (TypeError, ValueError):
        return False
    expected = _localize_exact(coverage_start, time.min, zone)
    return start_at == expected


def _is_regular_always_open(raw: Any) -> bool:
    if not isinstance(raw, dict) or set(raw) != {"open"}:
        return False
    try:
        day, local_time = _decode_regular_point(raw["open"])
    except (TypeError, ValueError):
        return False
    return day == 0 and local_time == time.min


def _split_at_local_midnights(
    start_at: datetime,
    end_at: datetime,
    *,
    zone: ZoneInfo,
) -> tuple[tuple[datetime, datetime], ...]:
    if end_at <= start_at:
        raise ValueError("opening interval must have positive duration")
    result: list[tuple[datetime, datetime]] = []
    cursor = start_at
    while cursor < end_at:
        local_cursor = cursor.astimezone(zone)
        next_midnight = _localize_exact(
            local_cursor.date() + timedelta(days=1),
            time.min,
            zone,
        )
        boundary = min(end_at, next_midnight)
        if boundary <= cursor:
            raise ValueError("opening interval cannot advance")
        result.append((cursor, boundary))
        cursor = boundary
    return tuple(result)


def _normalize_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> tuple[tuple[datetime, datetime], ...]:
    ordered = sorted(intervals)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError("opening intervals cannot overlap")
    return tuple(ordered)


def _closed_dates(
    intervals: tuple[tuple[datetime, datetime], ...],
    *,
    zone: ZoneInfo,
    coverage_start: date,
    coverage_end: date,
) -> tuple[date, ...]:
    open_dates: set[date] = set()
    for offset in range((coverage_end - coverage_start).days + 1):
        local_date = coverage_start + timedelta(days=offset)
        day_start = _localize_exact(local_date, time.min, zone)
        day_end = _localize_exact(
            local_date + timedelta(days=1),
            time.min,
            zone,
        )
        if any(
            start_at < day_end and end_at > day_start
            for start_at, end_at in intervals
        ):
            open_dates.add(local_date)
    return tuple(
        coverage_start + timedelta(days=offset)
        for offset in range((coverage_end - coverage_start).days + 1)
        if coverage_start + timedelta(days=offset) not in open_dates
    )


def _validate_special_days(
    raw: Any,
    *,
    coverage_start: date,
    coverage_end: date,
) -> None:
    if not isinstance(raw, list) or len(raw) > 7:
        raise ValueError("specialDays must be a bounded list")
    seen: set[date] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("special day must be an object")
        _exact_fields(
            item,
            required={"date"},
            optional=set(),
        )
        special_date = _decode_date(item["date"])
        if (
            special_date < coverage_start
            or special_date > coverage_end
            or special_date in seen
        ):
            raise ValueError(
                "special day must be unique and inside current coverage"
            )
        seen.add(special_date)


def _decode_timezone(raw: Any) -> tuple[str, ZoneInfo]:
    if not isinstance(raw, dict):
        raise ValueError("timeZone must be an object")
    _exact_fields(
        raw,
        required={"id"},
        optional={"version"},
    )
    timezone_name = raw["id"]
    if (
        not isinstance(timezone_name, str)
        or not timezone_name
        or len(timezone_name) > 128
    ):
        raise ValueError("timeZone.id must be bounded text")
    version = raw.get("version")
    if version is not None and (
        not isinstance(version, str) or len(version) > 64
    ):
        raise ValueError("timeZone.version must be bounded text")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timeZone.id must be a known IANA zone") from exc
    return timezone_name, zone


def _decode_attributions(
    raw: Any,
) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(raw, list) or len(raw) > _MAX_ATTRIBUTIONS:
        raise ValueError("attributions must be a bounded list")
    result: list[tuple[str, str | None]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("attribution must be an object")
        _exact_fields(
            item,
            required={"provider"},
            optional={"providerUri"},
        )
        provider = item["provider"]
        uri = item.get("providerUri")
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or provider != provider.strip()
            or len(provider) > 256
            or (uri is not None and not isinstance(uri, str))
        ):
            raise ValueError("attribution fields are invalid")
        result.append((provider, uri))
    result.append((_GOOGLE_MAPS_ATTRIBUTION, None))
    return tuple(
        sorted(
            set(result),
            key=lambda item: (item[0], item[1] or ""),
        )
    )


def _validate_returned_place_id(
    request: GooglePlaceDetailsRequest,
    returned: Any,
) -> None:
    if returned != request.endpoint.provider_place_id:
        raise _DecodeFailure(ProviderProblemCode.OUT_OF_SCOPE_RESULT)


def _decode_date(raw: Any) -> date:
    if not isinstance(raw, dict):
        raise ValueError("date must be an object")
    _exact_fields(
        raw,
        required={"day", "month", "year"},
        optional=set(),
    )
    year = _bounded_int(
        raw["year"],
        "date.year",
        minimum=1,
        maximum=9999,
    )
    month = _bounded_int(
        raw["month"],
        "date.month",
        minimum=1,
        maximum=12,
    )
    day = _bounded_int(
        raw["day"],
        "date.day",
        minimum=1,
        maximum=31,
    )
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError("date is not a calendar date") from exc


def _bounded_int(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} is outside its bounded integer range")
    return value


def _localize_exact(
    local_date: date,
    local_time: time,
    zone: ZoneInfo,
) -> datetime:
    naive = datetime.combine(local_date, local_time)
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = (
            candidate.astimezone(timezone.utc)
            .astimezone(zone)
            .replace(tzinfo=None)
        )
        if round_trip == naive:
            candidates.append(candidate.astimezone(timezone.utc))
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise ValueError(
            "provider local time is ambiguous or nonexistent"
        )
    return unique[0]


def _google_weekday(value: date) -> int:
    return (value.weekday() + 1) % 7


def _provider_result(
    request: GooglePlaceDetailsRequest,
    outcome: _AttemptOutcome,
    *,
    attempts_used: int,
    completed_at: datetime,
    retrieved_at: datetime | None = None,
) -> ProviderResult:
    provider_request = request.provider_request
    if outcome.decoded is None:
        assert outcome.problem is not None
        return ProviderResult(
            request_fingerprint=provider_request.request_fingerprint,
            status=ProviderResultStatus.FAILED,
            observations=(),
            problems=(outcome.problem,),
            attempts_used=attempts_used,
            completed_at=completed_at,
        )

    policy = request.snapshot.policies.policy(provider_request.policy_id)
    if (
        policy.persistence is not EvidencePersistence.MEMORY_ONLY
        or policy.max_retention_seconds is None
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Place Details policy must use bounded memory-only retention.",
        )
    requested_validity = (
        _PROFILE_VALIDITY
        if request.kind is PlaceDetailsKind.PROFILE
        else _HOURS_VALIDITY
    )
    validity = min(
        requested_validity,
        timedelta(seconds=policy.max_validity_seconds),
    )
    observation_at = (
        completed_at if retrieved_at is None else retrieved_at
    )
    valid_until = observation_at + validity
    if request.kind is PlaceDetailsKind.CURRENT_HOURS:
        payload = outcome.decoded.payload
        zone = ZoneInfo(payload["timezone"])
        coverage_deadline = _localize_exact(
            date.fromisoformat(payload["coverage_end"])
            + timedelta(days=1),
            time.min,
            zone,
        )
        valid_until = min(valid_until, coverage_deadline)
    retention = min(
        _MEMORY_RETENTION,
        timedelta(seconds=policy.max_retention_seconds),
    )
    purge_at = observation_at + retention
    if valid_until <= completed_at or purge_at <= completed_at:
        return _provider_result(
            request,
            _problem_outcome(
                request,
                ProviderProblemCode.STALE_EVIDENCE,
            ),
            attempts_used=attempts_used,
            completed_at=completed_at,
        )
    observation = FactObservation(
        key=provider_request.fact_keys[0],
        value=FactValue.from_payload(
            request.kind.fact_kind,
            outcome.decoded.payload,
        ),
        provenance=ProviderProvenance(
            provider_id=provider_request.provider_id,
            adapter_id=provider_request.adapter_id,
            adapter_version=provider_request.adapter_version,
            request_fingerprint=provider_request.request_fingerprint,
            retention_policy_id=provider_request.policy_id,
            provider_record_id=request.endpoint.provider_place_id,
            attributions=outcome.decoded.attributions,
        ),
        retrieved_at=observation_at,
        valid_until=valid_until,
        purge_at=purge_at,
        confidence=1.0,
    )
    return ProviderResult(
        request_fingerprint=provider_request.request_fingerprint,
        status=ProviderResultStatus.SUCCESS,
        observations=(observation,),
        problems=(),
        attempts_used=attempts_used,
        completed_at=completed_at,
    )


def _problem_outcome(
    request: GooglePlaceDetailsRequest,
    code: ProviderProblemCode,
    *,
    retry_after_s: float | None = None,
) -> _AttemptOutcome:
    messages = {
        ProviderProblemCode.AUTH_FAILED:
            "Google Place Details authentication failed.",
        ProviderProblemCode.EMPTY_RESPONSE:
            "Google Place Details returned no usable value.",
        ProviderProblemCode.INVALID_PROVIDER_REQUEST:
            "Google Place Details rejected the exact request.",
        ProviderProblemCode.INVALID_PROVIDER_RESPONSE:
            "Google Place Details returned an invalid bounded response.",
        ProviderProblemCode.NOT_FOUND:
            "Google Place Details did not find the exact Place ID.",
        ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON:
            "Current opening hours do not cover the requested dates.",
        ProviderProblemCode.OUT_OF_SCOPE_RESULT:
            "Google Place Details returned a different Place ID.",
        ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED:
            "The Place Details HTTP-attempt budget is exhausted.",
        ProviderProblemCode.PROVIDER_UNAVAILABLE:
            "Google Place Details is temporarily unavailable.",
        ProviderProblemCode.QUOTA_EXHAUSTED:
            "The Google Place Details quota is exhausted.",
        ProviderProblemCode.RATE_LIMITED:
            "Google Place Details rate limited the request.",
        ProviderProblemCode.STALE_EVIDENCE:
            "The Place Details endpoint identity is no longer fresh.",
        ProviderProblemCode.TIMEOUT:
            "The Google Place Details request timed out.",
    }
    next_actions = {
        ProviderProblemCode.AUTH_FAILED: "repair_provider_auth",
        ProviderProblemCode.EMPTY_RESPONSE: "verify_place_hours",
        ProviderProblemCode.INVALID_PROVIDER_REQUEST:
            "review_provider_request",
        ProviderProblemCode.INVALID_PROVIDER_RESPONSE:
            "review_provider_response",
        ProviderProblemCode.NOT_FOUND: "refresh_place_identity",
        ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON:
            "retry_with_current_dates",
        ProviderProblemCode.OUT_OF_SCOPE_RESULT:
            "refresh_place_identity",
        ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED:
            "increase_provider_budget",
        ProviderProblemCode.PROVIDER_UNAVAILABLE:
            "retry_with_budget",
        ProviderProblemCode.QUOTA_EXHAUSTED:
            "review_provider_quota",
        ProviderProblemCode.RATE_LIMITED: "retry_with_budget",
        ProviderProblemCode.STALE_EVIDENCE:
            "refresh_place_identity",
        ProviderProblemCode.TIMEOUT: "retry_with_budget",
    }
    if code not in messages:
        raise ValueError(f"Unsupported Place Details problem code {code!r}")
    return _AttemptOutcome(
        problem=ProviderProblem(
            code=code,
            message=messages[code],
            retryable=code
            in {
                ProviderProblemCode.PROVIDER_UNAVAILABLE,
                ProviderProblemCode.RATE_LIMITED,
                ProviderProblemCode.TIMEOUT,
            },
            next_action=next_actions[code],
            fact_key_ids=(
                request.provider_request.fact_keys[0].key_id,
            ),
        ),
        retry_after_s=retry_after_s,
    )


def _map_http_problem(
    response: GooglePlaceDetailsHttpResponse,
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
    if status_code == 400 or provider_status in {
        "FAILED_PRECONDITION",
        "INVALID_ARGUMENT",
    }:
        return ProviderProblemCode.INVALID_PROVIDER_REQUEST
    if status_code in {500, 502, 503, 504}:
        return ProviderProblemCode.PROVIDER_UNAVAILABLE
    return ProviderProblemCode.PROVIDER_UNAVAILABLE


def _retry_after_seconds(
    response: GooglePlaceDetailsHttpResponse,
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
            parse_constant=lambda _value: _raise_invalid_json(),
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
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "clock must return a timezone-aware datetime.",
        )
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


__all__ = [
    "GOOGLE_PLACE_CURRENT_HOURS_FIELD_MASK",
    "GOOGLE_PLACE_DETAILS_URL",
    "GOOGLE_PLACE_PROFILE_FIELD_MASK",
    "GOOGLE_PLACE_REGULAR_HOURS_FIELD_MASK",
    "GooglePlaceDetailsBatchExecution",
    "GooglePlaceDetailsExecution",
    "GooglePlaceDetailsHttpRequest",
    "GooglePlaceDetailsHttpResponse",
    "GooglePlaceDetailsRequest",
    "GooglePlaceDetailsTransport",
    "GooglePlaceDetailsTransportError",
    "GooglePlaceDetailsTransportErrorKind",
    "PlaceDetailsAttemptBudget",
    "PlaceDetailsKind",
    "build_google_place_details_http_request",
    "build_google_place_details_request",
    "execute_google_place_details",
    "execute_google_place_details_batch",
]
