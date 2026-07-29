"""Pure, process-local normalization of legacy hotel discovery results.

This module deliberately has no HTTP client, cache, FactKey, or promotion API.
It converts one bounded provider response into the existing candidate-only
lodging intake model without exposing provider or private query values.  Its
result is deliberately not provider provenance: status and diagnostic
references never authorize evidence, cache, receipts, decisions, or mutation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from .lodging import (
    IntentAuthority,
    LocationHint,
    LocationHintKind,
    LodgingCandidate,
    LodgingIntentDraft,
    LodgingKind,
    PriceBasis,
    bind_lodging_candidate,
)
from .models import DecisionState, EvidenceState


LODGING_DISCOVERY_VERSION = "lodging-discovery/v1"
_MAX_PROPERTIES = 256
_MAX_TEXT = 2_048
_MAX_TOP_LEVEL_FIELDS = 64
_MAX_METADATA_FIELDS = 64
_MAX_PROPERTY_FIELDS = 128
_MAX_NESTED_FIELDS = 64
_MAX_PROVIDER_SEARCH_ID = 512
_DIGEST_KEY = secrets.token_bytes(32)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_PUBLIC_METADATA_STATUSES = frozenset(
    {"success", "cached", "error", "failed", "processing", "queued"}
)


class LodgingDiscoveryStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    PARTIAL = "partial"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"


class LodgingDiscoveryProblemCode(str, Enum):
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"
    EMPTY_SUCCESS = "empty_success"
    PROPERTY_NO_LOCATION = "property_no_location"
    PROPERTY_MALFORMED = "property_malformed"
    PROPERTY_PRICE_UNUSABLE = "property_price_unusable"
    PROPERTY_DUPLICATE = "property_duplicate"
    PROPERTY_LIMIT_EXCEEDED = "property_limit_exceeded"


def _opaque(prefix: str, value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hmac.new(
        _DIGEST_KEY,
        prefix.encode() + b"\n" + encoded,
        hashlib.sha256,
    ).hexdigest()


def _text(value: object, *, maximum: int = _MAX_TEXT) -> str:
    if type(value) is not str:
        raise TypeError("text is required")
    if len(value) > maximum:
        raise ValueError("text is malformed")
    value = unicodedata.normalize("NFC", value).strip()
    if (
        not value
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("text is malformed")
    return value


def _date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be an exact date")
    return value


def _metadata_status(value: object) -> str | None:
    if value is None:
        return None
    try:
        normalized = _text(value, maximum=64).casefold()
    except (TypeError, ValueError):
        return None
    if not normalized.isascii():
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if not normalized:
        return "unknown"
    if normalized not in _PUBLIC_METADATA_STATUSES:
        return "unknown"
    return normalized


def _utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _property_ref(item: object) -> str:
    """Create an opaque, bounded reference without encoding raw payloads."""

    if isinstance(item, Mapping):
        name = item.get("name")
        gps = item.get("gps_coordinates")
        address = item.get("address") or item.get("formatted_address")
        latitude = (
            gps.get("latitude") if isinstance(gps, Mapping) else None
        )
        longitude = (
            gps.get("longitude") if isinstance(gps, Mapping) else None
        )
        scope = {
            "name": (
                unicodedata.normalize("NFC", name[:256]).strip()
                if isinstance(name, str)
                else None
            ),
            "latitude": (
                latitude
                if type(latitude) in {int, float}
                else (
                    latitude[:128]
                    if isinstance(latitude, str)
                    else None
                )
            ),
            "longitude": (
                longitude
                if type(longitude) in {int, float}
                else (
                    longitude[:128]
                    if isinstance(longitude, str)
                    else None
                )
            ),
            "address": (
                unicodedata.normalize("NFC", address[:512]).strip()
                if isinstance(address, str)
                else None
            ),
        }
    else:
        scope = {
            "type": type(item).__name__,
            "scalar": (
                str(item)[:256]
                if isinstance(item, (str, int, float, bool, type(None)))
                else None
            ),
        }
    return _opaque("lodging-discovery-property", scope)


@dataclass(frozen=True, slots=True, repr=False)
class LodgingDiscoveryRequest:
    """Private exact query scope; safe views expose only its opaque digest."""

    query: str = field(repr=False)
    check_in: date
    check_out: date
    adults: int
    children: int
    rooms: int
    currency: str
    currency_minor_unit: int
    region: str
    language: str
    request_id: str = ""

    def __post_init__(self) -> None:
        query = _text(self.query)
        check_in = _date(self.check_in, "check_in")
        check_out = _date(self.check_out, "check_out")
        if check_out <= check_in or (check_out - check_in).days > 366:
            raise ValueError("discovery dates are outside the bounded range")
        for name, value, lower, upper in (
            ("adults", self.adults, 1, 32),
            ("children", self.children, 0, 32),
            ("rooms", self.rooms, 1, 32),
            (
                "currency_minor_unit",
                self.currency_minor_unit,
                0,
                6,
            ),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} is outside its bounded range")
        currency = _text(self.currency, maximum=3).upper()
        region = _text(self.region, maximum=2).upper()
        language = _text(self.language, maximum=16)
        if not (currency.isascii() and currency.isalpha() and len(currency) == 3):
            raise ValueError("currency must be ISO-like")
        if not (region.isascii() and region.isalpha() and len(region) == 2):
            raise ValueError("region must be ISO-like")
        normalized = {
            "query": query,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "adults": self.adults,
            "children": self.children,
            "rooms": self.rooms,
            "currency": currency,
            "currency_minor_unit": self.currency_minor_unit,
            "region": region,
            "language": language,
        }
        request_id = _opaque("lodging-discovery-request", normalized)
        if self.request_id and not hmac.compare_digest(self.request_id, request_id):
            raise ValueError("request_id does not match request scope")
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "request_id", request_id)

    def __repr__(self) -> str:
        return f"LodgingDiscoveryRequest(request_id={self.request_id!r})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "adults": self.adults,
            "children": self.children,
            "rooms": self.rooms,
            "currency": self.currency,
            "currency_minor_unit": self.currency_minor_unit,
            "region": self.region,
            "language": self.language,
            "has_query": True,
        }


@dataclass(frozen=True, slots=True)
class LodgingDiscoveryProblem:
    code: LodgingDiscoveryProblemCode
    property_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not LodgingDiscoveryProblemCode:
            raise TypeError("problem code must be exact")
        if self.property_ref is not None and (
            type(self.property_ref) is not str
            or _DIGEST_RE.fullmatch(self.property_ref) is None
        ):
            raise ValueError("property_ref must be opaque")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "property_ref": self.property_ref}


@dataclass(frozen=True, slots=True, repr=False)
class _LodgingDiscoveryProjection:
    """Validated but non-authoritative view of caller-supplied raw data."""

    request: LodgingDiscoveryRequest = field(repr=False)
    status: LodgingDiscoveryStatus
    completed_at: datetime
    candidates: tuple[LodgingCandidate, ...] = field(default=(), repr=False)
    problems: tuple[LodgingDiscoveryProblem, ...] = ()
    metadata_status: str | None = None
    provider_search_ref: str | None = None
    diagnostic_ref: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.request) is not LodgingDiscoveryRequest
            or type(self.status) is not LodgingDiscoveryStatus
        ):
            raise TypeError("result request/status must be exact")
        completed_at = _utc(
            self.completed_at,
            "LodgingDiscoveryResult.completed_at",
        )
        if not isinstance(self.candidates, tuple) or any(
            type(item) is not LodgingCandidate
            for item in self.candidates
        ):
            raise TypeError("candidates must be exact lodging candidates")
        if (
            len(self.candidates) > _MAX_PROPERTIES
            or not isinstance(self.problems, tuple)
            or any(
                type(item) is not LodgingDiscoveryProblem
                for item in self.problems
            )
        ):
            raise ValueError("result is outside bounded discovery contract")
        metadata_status = _metadata_status(self.metadata_status)
        if self.metadata_status is not None and metadata_status is None:
            raise ValueError("metadata_status must be bounded provider status")
        if self.provider_search_ref is not None and (
            type(self.provider_search_ref) is not str
            or _DIGEST_RE.fullmatch(self.provider_search_ref) is None
        ):
            raise ValueError("provider_search_ref must be opaque")
        ids = tuple(item.candidate_id for item in self.candidates)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("candidates must be sorted and unique")
        if any(
            item.authority is not IntentAuthority.PROVIDER_DISCOVERED
            or item.decision_state is not DecisionState.CANDIDATE
            or item.evidence_state is not EvidenceState.UNVERIFIED
            or item.evidence_refs != ()
            or item.draft.reported_decision is not None
            or item.check_in != self.request.check_in
            or item.check_out != self.request.check_out
            or (
                item.draft.currency is not None
                and item.draft.currency != self.request.currency
            )
            for item in self.candidates
        ):
            raise ValueError(
                "Discovery candidates differ from the exact request contract"
            )
        problem_bindings = tuple(
            problem.to_dict() for problem in self.problems
        )
        if problem_bindings != tuple(
            sorted(
                problem_bindings,
                key=lambda item: (
                    item["code"],
                    item["property_ref"] or "",
                ),
            )
        ) or len(
            {
                (item["code"], item["property_ref"])
                for item in problem_bindings
            }
        ) != len(problem_bindings):
            raise ValueError("Discovery problems must be sorted and unique")
        problem_codes = {item.code for item in self.problems}
        valid_shape = {
            LodgingDiscoveryStatus.SUCCESS: (
                bool(self.candidates) and not self.problems
            ),
            LodgingDiscoveryStatus.PARTIAL: (
                bool(self.candidates) and bool(self.problems)
            ),
            LodgingDiscoveryStatus.EMPTY: (
                not self.candidates
                and problem_codes
                == {LodgingDiscoveryProblemCode.EMPTY_SUCCESS}
                and len(self.problems) == 1
            ),
            LodgingDiscoveryStatus.PROVIDER_ERROR: (
                not self.candidates
                and problem_codes
                == {LodgingDiscoveryProblemCode.PROVIDER_ERROR}
                and len(self.problems) == 1
            ),
            LodgingDiscoveryStatus.INVALID_RESPONSE: (
                not self.candidates
                and bool(self.problems)
                and LodgingDiscoveryProblemCode.PROVIDER_ERROR
                not in problem_codes
                and LodgingDiscoveryProblemCode.EMPTY_SUCCESS
                not in problem_codes
            ),
        }[self.status]
        if not valid_shape:
            raise ValueError(
                "Discovery status, candidates, and problems disagree"
            )
        if self.status in {
            LodgingDiscoveryStatus.SUCCESS,
            LodgingDiscoveryStatus.PARTIAL,
            LodgingDiscoveryStatus.EMPTY,
        } and metadata_status not in {"success", "cached"}:
            raise ValueError(
                "Successful discovery shapes require success metadata"
            )
        expected = _opaque(
            "lodging-discovery-result",
            {
                "request": self.request.request_id,
                "status": self.status.value,
                "completed_at": completed_at.isoformat(),
                "candidates": ids,
                "problems": [
                    item.to_dict() for item in self.problems
                ],
                "metadata_status": metadata_status,
                "search": self.provider_search_ref,
            },
        )
        if self.diagnostic_ref and not hmac.compare_digest(
            self.diagnostic_ref,
            expected,
        ):
            raise ValueError(
                "diagnostic_ref does not match normalized view"
            )
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "metadata_status", metadata_status)
        object.__setattr__(self, "diagnostic_ref", expected)

    def __repr__(self) -> str:
        return (
            "_LodgingDiscoveryProjection("
            f"status={self.status.value!r}, "
            f"candidate_count={len(self.candidates)!r}, "
            f"diagnostic_ref={self.diagnostic_ref!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "status": self.status.value,
            "completed_at": self.completed_at.isoformat(),
            "metadata_status": self.metadata_status,
            "candidates": [
                item.to_dict() for item in self.candidates
            ],
            "problems": [item.to_dict() for item in self.problems],
            "has_provider_search_ref": (
                self.provider_search_ref is not None
            ),
            "provenance": "untrusted_caller_supplied",
            "supports_authoritative_use": False,
            "diagnostic_ref": self.diagnostic_ref,
        }


class LodgingDiscoveryResult:
    """Immutable, non-authoritative view of caller-supplied discovery data.

    Construction always applies the bounded schema normalizer, but neither
    this object nor its process-local diagnostic reference proves provider
    provenance.  Downstream code may use only its candidate tuple, whose
    entries remain candidate + unverified with no evidence references.
    """

    __slots__ = ("_projection",)

    def __init__(
        self,
        request: LodgingDiscoveryRequest,
        raw: Mapping[str, Any],
        *,
        completed_at: datetime,
    ) -> None:
        projection = _normalize_serpapi_hotel_discovery(
            request,
            raw,
            completed_at=completed_at,
        )
        object.__setattr__(self, "_projection", projection)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("LodgingDiscoveryResult is immutable")

    @property
    def request(self) -> LodgingDiscoveryRequest:
        return self._projection.request

    @property
    def status(self) -> LodgingDiscoveryStatus:
        return self._projection.status

    @property
    def completed_at(self) -> datetime:
        return self._projection.completed_at

    @property
    def candidates(self) -> tuple[LodgingCandidate, ...]:
        return self._projection.candidates

    @property
    def problems(self) -> tuple[LodgingDiscoveryProblem, ...]:
        return self._projection.problems

    @property
    def metadata_status(self) -> str | None:
        return self._projection.metadata_status

    @property
    def provider_search_ref(self) -> str | None:
        return self._projection.provider_search_ref

    @property
    def diagnostic_ref(self) -> str:
        """Process-local correlation only; never an auth/cache/evidence key."""

        return self._projection.diagnostic_ref

    @property
    def provenance(self) -> str:
        return "untrusted_caller_supplied"

    @property
    def supports_authoritative_use(self) -> bool:
        return False

    def __repr__(self) -> str:
        return (
            "LodgingDiscoveryResult("
            f"status={self.status.value!r}, "
            f"candidate_count={len(self.candidates)!r}, "
            f"diagnostic_ref={self.diagnostic_ref!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return self._projection.to_dict()


def _problem(
    code: LodgingDiscoveryProblemCode,
    raw: object | None = None,
) -> LodgingDiscoveryProblem:
    return LodgingDiscoveryProblem(
        code,
        None if raw is None else _property_ref(raw),
    )


def _location(
    property_value: Mapping[str, Any],
    label: str,
    region: str,
) -> LocationHint | None:
    latitude = property_value.get("latitude")
    longitude = property_value.get("longitude")
    gps = property_value.get("gps_coordinates")
    if isinstance(gps, Mapping):
        if len(gps) > _MAX_NESTED_FIELDS:
            return None
        latitude = gps.get("latitude")
        longitude = gps.get("longitude")
    if (
        type(latitude) in {int, float}
        and type(longitude) in {int, float}
    ):
        try:
            return LocationHint(
                kind=LocationHintKind.COORDINATES,
                label=label,
                latitude=float(latitude),
                longitude=float(longitude),
                country_code=region,
            )
        except (TypeError, ValueError):
            return None
    address = (
        property_value.get("address")
        or property_value.get("formatted_address")
    )
    if isinstance(address, str):
        if len(address) > _MAX_TEXT:
            return None
        try:
            return LocationHint(
                kind=LocationHintKind.ADDRESS,
                label=label,
                input_text=address,
                country_code=region,
            )
        except (TypeError, ValueError):
            return None
    return None


def _major_to_minor(
    value: object,
    minor_unit: int,
) -> int | None:
    if isinstance(value, bool) or not isinstance(
        value,
        (int, float, str, Decimal),
    ):
        return None
    if isinstance(value, str) and len(value) > 128:
        return None
    if type(value) is int and abs(value) > 10**18:
        return None
    if isinstance(value, Decimal) and len(value.as_tuple().digits) > 128:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    scaled = amount * (Decimal(10) ** minor_unit)
    integral = scaled.to_integral_value()
    if scaled != integral:
        return None
    try:
        result = int(integral)
    except (OverflowError, ValueError):
        return None
    return result if result <= 10**15 else None


def _price(
    property_value: Mapping[str, Any],
    request: LodgingDiscoveryRequest,
) -> tuple[
    int | None,
    PriceBasis | None,
    bool | None,
    bool,
]:
    """Return normalized price fields plus whether unusable price was present."""

    value = property_value.get("price")
    if isinstance(value, Mapping):
        if len(value) > _MAX_NESTED_FIELDS:
            return None, None, None, True
        amount = value.get("amount_minor")
        currency = value.get("currency")
        minor_unit = value.get("minor_unit")
        basis = value.get("basis")
        valid = (
            type(amount) is int
            and type(currency) is str
            and type(minor_unit) is int
            and type(basis) is str
            and len(currency) == 3
            and len(basis) <= 16
            and basis in {"nightly", "total"}
            and currency.upper() == request.currency
            and minor_unit == request.currency_minor_unit
            and 0 <= amount <= 10**15
        )
        if valid:
            return amount, PriceBasis(basis), True, False
        return None, None, None, True

    provider_rates = (
        ("total_rate", PriceBasis.TOTAL),
        ("rate_per_night", PriceBasis.NIGHTLY),
    )
    saw_rate = False
    for field, basis in provider_rates:
        rate = property_value.get(field)
        if rate is None:
            continue
        saw_rate = True
        if not isinstance(rate, Mapping):
            continue
        if len(rate) > _MAX_NESTED_FIELDS:
            continue
        amount = _major_to_minor(
            rate.get("extracted_lowest"),
            request.currency_minor_unit,
        )
        if amount is not None:
            return amount, basis, True, False
    return None, None, None, saw_rate


def _lodging_kind(property_value: Mapping[str, Any]) -> LodgingKind:
    raw = property_value.get("type")
    if not isinstance(raw, str) or len(raw) > 128:
        return LodgingKind.HOTEL
    normalized = unicodedata.normalize("NFKC", raw).casefold()
    mappings = (
        ("vacation rental", LodgingKind.SHORT_TERM_RENTAL),
        ("apartment", LodgingKind.APARTMENT),
        ("guesthouse", LodgingKind.GUESTHOUSE),
        ("guest house", LodgingKind.GUESTHOUSE),
        ("hostel", LodgingKind.HOSTEL),
        ("ryokan", LodgingKind.RYOKAN),
        ("homestay", LodgingKind.HOMESTAY),
    )
    for marker, kind in mappings:
        if marker in normalized:
            return kind
    return LodgingKind.HOTEL


def _normalize_serpapi_hotel_discovery(
    request: LodgingDiscoveryRequest,
    raw: Mapping[str, Any],
    *,
    completed_at: datetime,
) -> _LodgingDiscoveryProjection:
    """Normalize a supplied raw mapping; this function never performs I/O."""
    if type(request) is not LodgingDiscoveryRequest:
        raise TypeError("request must be an exact LodgingDiscoveryRequest")
    completed_at = _utc(completed_at, "completed_at")
    if not isinstance(raw, Mapping):
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.INVALID_RESPONSE,
            completed_at=completed_at,
            problems=(
                _problem(LodgingDiscoveryProblemCode.INVALID_RESPONSE),
            ),
        )
    if len(raw) > _MAX_TOP_LEVEL_FIELDS:
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.INVALID_RESPONSE,
            completed_at=completed_at,
            problems=(
                _problem(LodgingDiscoveryProblemCode.INVALID_RESPONSE),
            ),
        )
    metadata = raw.get("search_metadata")
    metadata_mapping = (
        metadata if isinstance(metadata, Mapping) else None
    )
    if (
        metadata_mapping is not None
        and len(metadata_mapping) > _MAX_METADATA_FIELDS
    ):
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.INVALID_RESPONSE,
            completed_at=completed_at,
            problems=(
                _problem(LodgingDiscoveryProblemCode.INVALID_RESPONSE),
            ),
        )
    status = _metadata_status(
        metadata_mapping.get("status")
        if metadata_mapping is not None
        else None
    )
    search_id = (
        metadata_mapping.get("id")
        if metadata_mapping is not None
        else None
    )
    invalid_search_id = (
        search_id is not None
        and (
            not isinstance(search_id, str)
            or not search_id
            or len(search_id) > _MAX_PROVIDER_SEARCH_ID
        )
    )
    if invalid_search_id:
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.INVALID_RESPONSE,
            completed_at=completed_at,
            problems=(
                _problem(LodgingDiscoveryProblemCode.INVALID_RESPONSE),
            ),
            metadata_status=status,
        )
    search_ref = (
        _opaque("lodging-discovery-search", search_id)
        if isinstance(search_id, str) and search_id
        else None
    )
    error = raw.get("error")
    has_error = (
        isinstance(error, str) and bool(error)
    ) or (error is not None and not isinstance(error, str))
    if has_error or status in {"error", "failed"}:
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.PROVIDER_ERROR,
            completed_at=completed_at,
            problems=(
                _problem(LodgingDiscoveryProblemCode.PROVIDER_ERROR),
            ),
            metadata_status=status,
            provider_search_ref=search_ref,
        )
    if status not in {"success", "cached"}:
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.INVALID_RESPONSE,
            completed_at=completed_at,
            problems=(
                _problem(LodgingDiscoveryProblemCode.INVALID_RESPONSE),
            ),
            metadata_status=status,
            provider_search_ref=search_ref,
        )
    properties = raw.get("properties")
    if not isinstance(properties, list):
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.INVALID_RESPONSE,
            completed_at=completed_at,
            problems=(
                _problem(LodgingDiscoveryProblemCode.INVALID_RESPONSE),
            ),
            metadata_status=status,
            provider_search_ref=search_ref,
        )
    if len(properties) > _MAX_PROPERTIES:
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.INVALID_RESPONSE,
            completed_at=completed_at,
            problems=(
                _problem(
                    LodgingDiscoveryProblemCode.PROPERTY_LIMIT_EXCEEDED
                ),
            ),
            metadata_status=status,
            provider_search_ref=search_ref,
        )
    if not properties:
        return _LodgingDiscoveryProjection(
            request=request,
            status=LodgingDiscoveryStatus.EMPTY,
            completed_at=completed_at,
            problems=(
                _problem(LodgingDiscoveryProblemCode.EMPTY_SUCCESS),
            ),
            metadata_status=status,
            provider_search_ref=search_ref,
        )
    accepted: dict[str, LodgingCandidate] = {}
    problems: list[LodgingDiscoveryProblem] = []
    for item in properties:
        if not isinstance(item, Mapping):
            problems.append(
                _problem(
                    LodgingDiscoveryProblemCode.PROPERTY_MALFORMED,
                    item,
                )
            )
            continue
        if len(item) > _MAX_PROPERTY_FIELDS:
            problems.append(
                _problem(
                    LodgingDiscoveryProblemCode.PROPERTY_MALFORMED,
                    item,
                )
            )
            continue
        if item.get("sponsored") is True:
            continue
        try:
            label = _text(item.get("name"), maximum=256)
            location = _location(item, label, request.region)
        except (TypeError, ValueError):
            problems.append(
                _problem(
                    LodgingDiscoveryProblemCode.PROPERTY_MALFORMED,
                    item,
                )
            )
            continue
        if location is None:
            problems.append(
                _problem(
                    LodgingDiscoveryProblemCode.PROPERTY_NO_LOCATION,
                    item,
                )
            )
            continue
        amount, basis, estimate, price_unusable = _price(item, request)
        if price_unusable:
            problems.append(
                _problem(
                    LodgingDiscoveryProblemCode.PROPERTY_PRICE_UNUSABLE,
                    item,
                )
            )
        draft = LodgingIntentDraft(
            kind=_lodging_kind(item),
            label=label,
            location=location,
            check_in=request.check_in,
            check_out=request.check_out,
            price_amount_minor=amount,
            currency=request.currency if amount is not None else None,
            price_basis=basis,
            price_is_estimate=estimate,
        )
        candidate = bind_lodging_candidate(
            draft,
            authority=IntentAuthority.PROVIDER_DISCOVERED,
        )
        # ``LocationHint.location_digest`` deliberately includes the private
        # label; discovery labels can differ for the same property.  Dedup by
        # the private endpoint value instead, while retaining only an HMAC.
        dedupe_key = _opaque(
            "lodging-discovery-dedupe",
            {
                "kind": location.kind.value,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "address": location.input_text,
                "dates": [
                    request.check_in.isoformat(),
                    request.check_out.isoformat(),
                ],
            },
        )
        if dedupe_key in accepted:
            # Provider list order is not evidence.  Choose a stable winner
            # when duplicate endpoint records carry different display labels.
            if candidate.candidate_id < accepted[dedupe_key].candidate_id:
                accepted[dedupe_key] = candidate
            problems.append(
                LodgingDiscoveryProblem(
                    LodgingDiscoveryProblemCode.PROPERTY_DUPLICATE,
                    _opaque(
                        "lodging-discovery-duplicate",
                        dedupe_key,
                    ),
                )
            )
            continue
        accepted[dedupe_key] = candidate
    candidates = tuple(
        sorted(accepted.values(), key=lambda item: item.candidate_id)
    )
    unique_problems = {
        (item.code.value, item.property_ref): item
        for item in problems
    }
    problems_tuple = tuple(
        unique_problems[key]
        for key in sorted(
            unique_problems,
            key=lambda item: (item[0], item[1] or ""),
        )
    )
    if candidates:
        result_status = (
            LodgingDiscoveryStatus.SUCCESS
            if not problems_tuple
            else LodgingDiscoveryStatus.PARTIAL
        )
    elif problems_tuple:
        result_status = LodgingDiscoveryStatus.INVALID_RESPONSE
    else:
        result_status = LodgingDiscoveryStatus.EMPTY
        problems_tuple = (
            _problem(LodgingDiscoveryProblemCode.EMPTY_SUCCESS),
        )
    return _LodgingDiscoveryProjection(
        request=request,
        status=result_status,
        completed_at=completed_at,
        candidates=candidates,
        problems=problems_tuple,
        metadata_status=status,
        provider_search_ref=search_ref,
    )


def normalize_serpapi_hotel_discovery(
    request: LodgingDiscoveryRequest,
    raw: Mapping[str, Any],
    *,
    completed_at: datetime,
) -> LodgingDiscoveryResult:
    """Normalize untrusted caller data without performing I/O.

    The returned object is a non-provenance DTO.  Its status and diagnostic
    references cannot authorize provider facts, cache entries, receipts,
    decisions, or canonical mutations.
    """

    return LodgingDiscoveryResult(
        request,
        raw,
        completed_at=completed_at,
    )


__all__ = [
    "LODGING_DISCOVERY_VERSION",
    "LodgingDiscoveryProblem",
    "LodgingDiscoveryProblemCode",
    "LodgingDiscoveryRequest",
    "LodgingDiscoveryResult",
    "LodgingDiscoveryStatus",
    "normalize_serpapi_hotel_discovery",
]
