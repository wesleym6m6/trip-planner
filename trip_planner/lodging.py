"""Runtime-only natural-language intake for transport and lodging.

The public drafts are bounded containers for information extracted from a
conversation.  They are not canonical plan entities, provider requests, or
booking records.  Binders create only unverified candidates.  Clear user
wording may be retained as a non-authoritative reported decision claim, but
Phase 4.5A has no decision-promotion path.  The pure assessor reports missing,
unconfirmed, conflicted, and needs-verification lodging nights without
inventing a property.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import unicodedata
from dataclasses import InitVar, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .models import DecisionState, EvidenceState, IssueSeverity


LODGING_INTAKE_VERSION = "lodging-intake/v1"
_MAX_TEXT_LENGTH = 2_048
_MAX_LABEL_LENGTH = 256
_MAX_BUFFER_MINUTES = 1_440
_MAX_TIME_WINDOW = timedelta(days=7)
_MAX_AREA_RADIUS_M = 100_000
_MAX_PRICE_MINOR = 10**15
_MAX_STAY_NIGHTS = 366
_MAX_LODGING_CANDIDATES = 256
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_MACHINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_PROCESS_DIGEST_KEY = secrets.token_bytes(32)
_BINDING_TOKEN = object()
_ASSESSMENT_TOKEN = object()


class IntentAuthority(str, Enum):
    """Who is allowed to claim a decision represented by a bound value."""

    USER_STATED = "user_stated"
    USER_CONFIRMED = "user_confirmed"
    AI_SUGGESTED = "ai_suggested"
    PROVIDER_DISCOVERED = "provider_discovered"


class TransportBoundaryKind(str, Enum):
    """A fixed or tentative edge of usable trip time."""

    ARRIVAL = "arrival"
    DEPARTURE = "departure"
    TRANSFER = "transfer"


class LocationHintKind(str, Enum):
    """How much location information the conversation currently provides."""

    LOCATION_ID = "location_id"
    PLACE_ID = "place_id"
    ADDRESS = "address"
    COORDINATES = "coordinates"
    AREA = "area"


class LocationPrecision(str, Enum):
    """Whether route calculations can use a location as an exact endpoint."""

    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNRESOLVED = "unresolved"


class LodgingKind(str, Enum):
    """Provider-neutral lodging categories used only for comparison."""

    UNSPECIFIED = "unspecified"
    HOTEL = "hotel"
    HOSTEL = "hostel"
    RYOKAN = "ryokan"
    GUESTHOUSE = "guesthouse"
    SHORT_TERM_RENTAL = "short_term_rental"
    APARTMENT = "apartment"
    HOMESTAY = "homestay"
    OTHER = "other"


class PriceBasis(str, Enum):
    """What an optional user-entered price amount describes."""

    NIGHTLY = "nightly"
    TOTAL = "total"


class LodgingRequirement(str, Enum):
    """What the user has said about needing lodging for the date span."""

    UNKNOWN = "unknown"
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    OPTIONS_WANTED = "options_wanted"


class LodgingIntakeStatus(str, Enum):
    """Deterministic state of the current lodging conversation."""

    NOT_REQUIRED = "not_required"
    MISSING = "missing"
    SEEKING_OPTIONS = "seeking_options"
    COMPARING = "comparing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PARTIAL = "partial"
    CONFLICTED = "conflicted"


def _digest(value: Any, *, prefix: str) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        _PROCESS_DIGEST_KEY,
        prefix.encode("utf-8") + b"\n" + encoded,
        hashlib.sha256,
    ).hexdigest()


def _check_digest(value: str, expected: str, name: str) -> None:
    if value and value != expected:
        raise ValueError(f"{name} does not match its normalized content")


def _text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must be non-empty bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{name} cannot contain control characters")
    return normalized


def _optional_text(
    value: object | None,
    name: str,
    *,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum=maximum)


def _machine_id(value: object | None, name: str) -> str | None:
    if value is None:
        return None
    normalized = _text(value, name, maximum=256)
    if _MACHINE_ID_RE.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a bounded machine identifier")
    return normalized


def _exact_date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be an exact date")
    return value


def _utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _coordinate(value: object, name: str, *, limit: float) -> float:
    if type(value) not in {int, float}:
        raise TypeError(f"{name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or not -limit <= normalized <= limit:
        raise ValueError(f"{name} is outside its valid range")
    return normalized


def _buffer(value: object, name: str) -> int:
    if (
        type(value) is not int
        or not 0 <= value <= _MAX_BUFFER_MINUTES
    ):
        raise ValueError(
            f"{name} must be an integer from 0 to {_MAX_BUFFER_MINUTES}"
        )
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ReportedDecisionClaim:
    """Untrusted record of decision wording extracted from one user turn.

    A claim lets the AI remember that the user said "selected", "fixed", or
    "booked" without granting that state to the candidate.  The opaque source
    reference must be checked by a future host-owned confirmation boundary.
    """

    decision_state: DecisionState
    source_ref: str = field(repr=False)
    claim_id: str = ""

    def __post_init__(self) -> None:
        if type(self.decision_state) is not DecisionState:
            raise TypeError(
                "ReportedDecisionClaim.decision_state must be exact"
            )
        if self.decision_state not in {
            DecisionState.SELECTED,
            DecisionState.FIXED,
            DecisionState.BOOKED,
        }:
            raise ValueError(
                "Reported lodging/transport decisions must be selected, "
                "fixed, or booked wording"
            )
        source_ref = _machine_id(
            self.source_ref,
            "ReportedDecisionClaim.source_ref",
        )
        assert source_ref is not None
        expected_id = _digest(
            {
                "decision_state": self.decision_state.value,
                "source_ref": source_ref,
            },
            prefix="reported-decision-claim",
        )
        _check_digest(
            self.claim_id,
            expected_id,
            "ReportedDecisionClaim.claim_id",
        )
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "claim_id", expected_id)

    def __repr__(self) -> str:
        return (
            "ReportedDecisionClaim("
            f"decision_state={self.decision_state.value!r}, "
            f"claim_id={self.claim_id!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_state": self.decision_state.value,
            "claim_id": self.claim_id,
            "has_source_ref": True,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LocationHint:
    """Private, process-local location input with a redacted safe view."""

    kind: LocationHintKind
    label: str = field(repr=False)
    location_id: str | None = None
    provider_place_id: str | None = field(default=None, repr=False)
    input_text: str | None = field(default=None, repr=False)
    latitude: float | None = field(default=None, repr=False)
    longitude: float | None = field(default=None, repr=False)
    radius_m: int | None = None
    country_code: str | None = None
    location_digest: str = ""

    def __post_init__(self) -> None:
        if type(self.kind) is not LocationHintKind:
            raise TypeError("LocationHint.kind must be exact")
        label = _text(
            self.label,
            "LocationHint.label",
            maximum=_MAX_LABEL_LENGTH,
        )
        location_id = _machine_id(
            self.location_id,
            "LocationHint.location_id",
        )
        provider_place_id = _optional_text(
            self.provider_place_id,
            "LocationHint.provider_place_id",
            maximum=_MAX_TEXT_LENGTH,
        )
        input_text = _optional_text(
            self.input_text,
            "LocationHint.input_text",
            maximum=_MAX_TEXT_LENGTH,
        )
        has_latitude = self.latitude is not None
        has_longitude = self.longitude is not None
        if has_latitude != has_longitude:
            raise ValueError(
                "LocationHint coordinates require latitude and longitude"
            )
        latitude = (
            _coordinate(
                self.latitude,
                "LocationHint.latitude",
                limit=90,
            )
            if has_latitude
            else None
        )
        longitude = (
            _coordinate(
                self.longitude,
                "LocationHint.longitude",
                limit=180,
            )
            if has_longitude
            else None
        )
        radius_m = self.radius_m
        if radius_m is not None and (
            type(radius_m) is not int
            or not 1 <= radius_m <= _MAX_AREA_RADIUS_M
        ):
            raise ValueError(
                "LocationHint.radius_m is outside its bounded range"
            )
        country_code = self.country_code
        if country_code is not None:
            country_code = _text(
                country_code,
                "LocationHint.country_code",
                maximum=2,
            ).upper()
            if (
                len(country_code) != 2
                or not country_code.isascii()
                or not country_code.isalpha()
            ):
                raise ValueError(
                    "LocationHint.country_code must be two ASCII letters"
                )

        if self.kind is LocationHintKind.LOCATION_ID:
            valid_shape = (
                location_id is not None
                and provider_place_id is None
                and input_text is None
                and latitude is None
                and radius_m is None
            )
        elif self.kind is LocationHintKind.PLACE_ID:
            valid_shape = (
                provider_place_id is not None
                and location_id is None
                and input_text is None
                and latitude is None
                and radius_m is None
            )
        elif self.kind is LocationHintKind.ADDRESS:
            valid_shape = (
                input_text is not None
                and location_id is None
                and provider_place_id is None
                and latitude is None
                and radius_m is None
            )
        elif self.kind is LocationHintKind.COORDINATES:
            valid_shape = (
                latitude is not None
                and location_id is None
                and provider_place_id is None
                and input_text is None
                and radius_m is None
            )
        else:
            valid_shape = (
                location_id is None
                and provider_place_id is None
                and (input_text is not None or latitude is not None)
                and (
                    (latitude is not None and radius_m is not None)
                    or (latitude is None and radius_m is None)
                )
            )
        if not valid_shape:
            raise ValueError(
                "LocationHint fields do not match the exact location kind"
            )

        expected_digest = _digest(
            {
                "kind": self.kind.value,
                "label": label,
                "location_id": location_id,
                "provider_place_id": provider_place_id,
                "input_text": input_text,
                "latitude": latitude,
                "longitude": longitude,
                "radius_m": radius_m,
                "country_code": country_code,
            },
            prefix="lodging-location-hint",
        )
        _check_digest(
            self.location_digest,
            expected_digest,
            "LocationHint.location_digest",
        )
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "location_id", location_id)
        object.__setattr__(
            self,
            "provider_place_id",
            provider_place_id,
        )
        object.__setattr__(self, "input_text", input_text)
        object.__setattr__(self, "latitude", latitude)
        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(self, "radius_m", radius_m)
        object.__setattr__(self, "country_code", country_code)
        object.__setattr__(
            self,
            "location_digest",
            expected_digest,
        )

    @property
    def precision(self) -> LocationPrecision:
        if self.kind is LocationHintKind.AREA:
            return LocationPrecision.APPROXIMATE
        if self.kind is LocationHintKind.ADDRESS:
            return LocationPrecision.UNRESOLVED
        return LocationPrecision.EXACT

    def __repr__(self) -> str:
        return (
            "LocationHint("
            f"kind={self.kind.value!r}, "
            f"precision={self.precision.value!r}, "
            f"location_digest={self.location_digest!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a diagnostic view that excludes all raw location values."""

        return {
            "kind": self.kind.value,
            "precision": self.precision.value,
            "location_digest": self.location_digest,
            "has_location_id": self.location_id is not None,
            "has_provider_place_id": self.provider_place_id is not None,
            "has_private_text": self.input_text is not None,
            "has_coordinates": self.latitude is not None,
            "radius_m": self.radius_m,
            "country_code": self.country_code,
        }


@dataclass(frozen=True, slots=True, repr=False)
class TransportBoundaryDraft:
    """Untrusted conversational transport input with no decision authority."""

    kind: TransportBoundaryKind
    location: LocationHint = field(repr=False)
    exact_at: datetime | None = field(default=None, repr=False)
    window_start: datetime | None = field(default=None, repr=False)
    window_end: datetime | None = field(default=None, repr=False)
    buffer_before_min: int = 0
    buffer_after_min: int = 0
    reported_decision: ReportedDecisionClaim | None = field(
        default=None,
        repr=False,
    )
    draft_id: str = ""

    def __post_init__(self) -> None:
        if type(self.kind) is not TransportBoundaryKind:
            raise TypeError("TransportBoundaryDraft.kind must be exact")
        if type(self.location) is not LocationHint:
            raise TypeError(
                "TransportBoundaryDraft.location must be exact"
            )
        exact_at = (
            _utc(
                self.exact_at,
                "TransportBoundaryDraft.exact_at",
            )
            if self.exact_at is not None
            else None
        )
        window_start = (
            _utc(
                self.window_start,
                "TransportBoundaryDraft.window_start",
            )
            if self.window_start is not None
            else None
        )
        window_end = (
            _utc(
                self.window_end,
                "TransportBoundaryDraft.window_end",
            )
            if self.window_end is not None
            else None
        )
        if exact_at is not None:
            valid_shape = window_start is None and window_end is None
        else:
            valid_shape = window_start is not None and window_end is not None
        if not valid_shape:
            raise ValueError(
                "Transport boundary requires one exact time or one full window"
            )
        if window_start is not None and window_end is not None:
            if (
                window_end <= window_start
                or window_end - window_start > _MAX_TIME_WINDOW
            ):
                raise ValueError(
                    "Transport boundary window is outside its bounded range"
                )
        buffer_before = _buffer(
            self.buffer_before_min,
            "TransportBoundaryDraft.buffer_before_min",
        )
        buffer_after = _buffer(
            self.buffer_after_min,
            "TransportBoundaryDraft.buffer_after_min",
        )
        reported_decision = self.reported_decision
        if (
            reported_decision is not None
            and type(reported_decision) is not ReportedDecisionClaim
        ):
            raise TypeError(
                "TransportBoundaryDraft.reported_decision must be exact"
            )
        expected_id = _digest(
            {
                "kind": self.kind.value,
                "location_digest": self.location.location_digest,
                "exact_at": (
                    exact_at.isoformat() if exact_at is not None else None
                ),
                "window_start": (
                    window_start.isoformat()
                    if window_start is not None
                    else None
                ),
                "window_end": (
                    window_end.isoformat()
                    if window_end is not None
                    else None
                ),
                "buffer_before_min": buffer_before,
                "buffer_after_min": buffer_after,
                "reported_decision_claim_id": (
                    reported_decision.claim_id
                    if reported_decision is not None
                    else None
                ),
            },
            prefix="transport-boundary-draft",
        )
        _check_digest(
            self.draft_id,
            expected_id,
            "TransportBoundaryDraft.draft_id",
        )
        object.__setattr__(self, "exact_at", exact_at)
        object.__setattr__(self, "window_start", window_start)
        object.__setattr__(self, "window_end", window_end)
        object.__setattr__(
            self,
            "buffer_before_min",
            buffer_before,
        )
        object.__setattr__(
            self,
            "buffer_after_min",
            buffer_after,
        )
        object.__setattr__(self, "draft_id", expected_id)

    @property
    def time_shape(self) -> str:
        return "exact" if self.exact_at is not None else "window"

    def __repr__(self) -> str:
        return (
            "TransportBoundaryDraft("
            f"kind={self.kind.value!r}, "
            f"time_shape={self.time_shape!r}, "
            f"draft_id={self.draft_id!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "location": self.location.to_dict(),
            "time_shape": self.time_shape,
            "buffer_before_min": self.buffer_before_min,
            "buffer_after_min": self.buffer_after_min,
            "reported_decision": (
                self.reported_decision.to_dict()
                if self.reported_decision is not None
                else None
            ),
            "draft_id": self.draft_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class TransportBoundary:
    """An unverified candidate boundary extracted from user wording."""

    draft: TransportBoundaryDraft = field(repr=False)
    decision_state: DecisionState
    authority: IntentAuthority
    boundary_id: str = ""
    contract_version: str = LODGING_INTAKE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _BINDING_TOKEN:
            raise ValueError(
                "Transport boundaries must be created by the trusted binder"
            )
        if type(self.draft) is not TransportBoundaryDraft:
            raise TypeError("TransportBoundary.draft must be exact")
        _validate_decision_authority(
            self.decision_state,
            self.authority,
            name="TransportBoundary",
        )
        if self.contract_version != LODGING_INTAKE_VERSION:
            raise ValueError("Unsupported lodging intake contract version")
        expected_id = _digest(
            {
                "contract_version": self.contract_version,
                "draft_id": self.draft.draft_id,
                "decision_state": self.decision_state.value,
                "authority": self.authority.value,
            },
            prefix="transport-boundary",
        )
        _check_digest(
            self.boundary_id,
            expected_id,
            "TransportBoundary.boundary_id",
        )
        object.__setattr__(self, "boundary_id", expected_id)

    def __repr__(self) -> str:
        return (
            "TransportBoundary("
            f"boundary_id={self.boundary_id!r}, "
            f"kind={self.draft.kind.value!r}, "
            f"decision_state={self.decision_state.value!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary_id": self.boundary_id,
            "decision_state": self.decision_state.value,
            "authority": self.authority.value,
            "draft": self.draft.to_dict(),
            "contract_version": self.contract_version,
        }


def bind_transport_boundary(
    draft: TransportBoundaryDraft,
) -> TransportBoundary:
    """Bind unconfirmed user input as a runtime-only candidate.

    Decision escalation is intentionally unavailable in Phase 4.5A.  A clear
    user statement remains a ``reported_decision`` on the private draft until
    a future host-owned confirmation boundary exists.
    """

    if type(draft) is not TransportBoundaryDraft:
        raise TypeError("draft must be an exact TransportBoundaryDraft")
    return TransportBoundary(
        draft=draft,
        decision_state=DecisionState.CANDIDATE,
        authority=IntentAuthority.USER_STATED,
        _token=_BINDING_TOKEN,
    )


@dataclass(frozen=True, slots=True, repr=False)
class LodgingIntentDraft:
    """One bounded accommodation option extracted from natural language."""

    kind: LodgingKind
    label: str = field(repr=False)
    location: LocationHint = field(repr=False)
    check_in: date
    check_out: date
    price_amount_minor: int | None = field(default=None, repr=False)
    currency: str | None = None
    price_basis: PriceBasis | None = None
    price_is_estimate: bool | None = None
    reported_decision: ReportedDecisionClaim | None = field(
        default=None,
        repr=False,
    )
    draft_id: str = ""

    def __post_init__(self) -> None:
        if type(self.kind) is not LodgingKind:
            raise TypeError("LodgingIntentDraft.kind must be exact")
        label = _text(
            self.label,
            "LodgingIntentDraft.label",
            maximum=_MAX_LABEL_LENGTH,
        )
        if type(self.location) is not LocationHint:
            raise TypeError("LodgingIntentDraft.location must be exact")
        check_in = _exact_date(
            self.check_in,
            "LodgingIntentDraft.check_in",
        )
        check_out = _exact_date(
            self.check_out,
            "LodgingIntentDraft.check_out",
        )
        if check_out <= check_in:
            raise ValueError("Lodging check-out must be after check-in")
        if (check_out - check_in).days > _MAX_STAY_NIGHTS:
            raise ValueError(
                "Lodging date span exceeds the bounded intake limit"
            )
        price_fields = (
            self.price_amount_minor,
            self.currency,
            self.price_basis,
        )
        has_price = any(item is not None for item in price_fields)
        if has_price and any(item is None for item in price_fields):
            raise ValueError(
                "Lodging price requires amount, currency, and basis together"
            )
        amount = self.price_amount_minor
        currency = self.currency
        price_basis = self.price_basis
        if amount is not None:
            if (
                type(amount) is not int
                or not 0 <= amount <= _MAX_PRICE_MINOR
            ):
                raise ValueError(
                    "Lodging price amount is outside its bounded range"
                )
            assert currency is not None
            currency = _text(
                currency,
                "LodgingIntentDraft.currency",
                maximum=3,
            ).upper()
            if _CURRENCY_RE.fullmatch(currency) is None:
                raise ValueError(
                    "Lodging currency must be three ASCII letters"
                )
            if type(price_basis) is not PriceBasis:
                raise TypeError(
                    "Lodging price basis must be exact"
                )
        if (
            self.price_is_estimate is not None
            and type(self.price_is_estimate) is not bool
        ):
            raise TypeError("price_is_estimate must be bool or None")
        if not has_price and self.price_is_estimate is not None:
            raise ValueError(
                "Price certainty cannot exist without a price"
            )
        reported_decision = self.reported_decision
        if (
            reported_decision is not None
            and type(reported_decision) is not ReportedDecisionClaim
        ):
            raise TypeError(
                "LodgingIntentDraft.reported_decision must be exact"
            )
        expected_id = _digest(
            {
                "kind": self.kind.value,
                "label": label,
                "location_digest": self.location.location_digest,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "price_amount_minor": amount,
                "currency": currency,
                "price_basis": (
                    price_basis.value
                    if price_basis is not None
                    else None
                ),
                "price_is_estimate": self.price_is_estimate,
                "reported_decision_claim_id": (
                    reported_decision.claim_id
                    if reported_decision is not None
                    else None
                ),
            },
            prefix="lodging-intent-draft",
        )
        _check_digest(
            self.draft_id,
            expected_id,
            "LodgingIntentDraft.draft_id",
        )
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "check_in", check_in)
        object.__setattr__(self, "check_out", check_out)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "draft_id", expected_id)

    def __repr__(self) -> str:
        return (
            "LodgingIntentDraft("
            f"kind={self.kind.value!r}, "
            f"draft_id={self.draft_id!r}, "
            f"has_price={self.price_amount_minor is not None!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted binding, not private review content."""

        return {
            "kind": self.kind.value,
            "location": self.location.to_dict(),
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "has_price": self.price_amount_minor is not None,
            "currency": self.currency,
            "price_basis": (
                self.price_basis.value
                if self.price_basis is not None
                else None
            ),
            "price_is_estimate": self.price_is_estimate,
            "reported_decision": (
                self.reported_decision.to_dict()
                if self.reported_decision is not None
                else None
            ),
            "draft_id": self.draft_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingCandidate:
    """An unverified candidate binding around one private intent draft."""

    draft: LodgingIntentDraft = field(repr=False)
    decision_state: DecisionState
    evidence_state: EvidenceState
    authority: IntentAuthority
    evidence_refs: tuple[str, ...] = ()
    candidate_id: str = ""
    contract_version: str = LODGING_INTAKE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _BINDING_TOKEN:
            raise ValueError(
                "Lodging candidates must be created by the trusted binder"
            )
        if type(self.draft) is not LodgingIntentDraft:
            raise TypeError("LodgingCandidate.draft must be exact")
        _validate_decision_authority(
            self.decision_state,
            self.authority,
            name="LodgingCandidate",
        )
        if type(self.evidence_state) is not EvidenceState:
            raise TypeError("LodgingCandidate.evidence_state must be exact")
        if self.evidence_state is not EvidenceState.UNVERIFIED:
            raise ValueError(
                "Phase 4.5A candidates cannot claim verified evidence"
            )
        if self.evidence_refs != ():
            raise ValueError(
                "Phase 4.5A candidates cannot claim evidence references"
            )
        if self.contract_version != LODGING_INTAKE_VERSION:
            raise ValueError("Unsupported lodging intake contract version")
        expected_id = _digest(
            {
                "contract_version": self.contract_version,
                "draft_id": self.draft.draft_id,
                "decision_state": self.decision_state.value,
                "evidence_state": self.evidence_state.value,
                "authority": self.authority.value,
                "evidence_refs": [],
            },
            prefix="lodging-candidate",
        )
        _check_digest(
            self.candidate_id,
            expected_id,
            "LodgingCandidate.candidate_id",
        )
        object.__setattr__(self, "candidate_id", expected_id)

    @property
    def check_in(self) -> date:
        return self.draft.check_in

    @property
    def check_out(self) -> date:
        return self.draft.check_out

    def covers(self, night: date) -> bool:
        return self.check_in <= night < self.check_out

    def __repr__(self) -> str:
        return (
            "LodgingCandidate("
            f"candidate_id={self.candidate_id!r}, "
            f"decision_state={self.decision_state.value!r}, "
            f"evidence_state={self.evidence_state.value!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "decision_state": self.decision_state.value,
            "evidence_state": self.evidence_state.value,
            "authority": self.authority.value,
            "evidence_refs": list(self.evidence_refs),
            "draft": self.draft.to_dict(),
            "contract_version": self.contract_version,
        }


def bind_lodging_candidate(
    draft: LodgingIntentDraft,
    *,
    authority: IntentAuthority = IntentAuthority.USER_STATED,
) -> LodgingCandidate:
    """Bind extracted input as an unverified runtime-only candidate."""

    if type(draft) is not LodgingIntentDraft:
        raise TypeError("draft must be an exact LodgingIntentDraft")
    if authority not in {
        IntentAuthority.USER_STATED,
        IntentAuthority.AI_SUGGESTED,
        IntentAuthority.PROVIDER_DISCOVERED,
    }:
        raise ValueError(
            "Confirmed authority is unavailable before the host boundary"
        )
    if (
        draft.reported_decision is not None
        and authority is not IntentAuthority.USER_STATED
    ):
        raise ValueError(
            "Reported decision wording must reference a user-owned turn"
        )
    return LodgingCandidate(
        draft=draft,
        decision_state=DecisionState.CANDIDATE,
        evidence_state=EvidenceState.UNVERIFIED,
        authority=authority,
        evidence_refs=(),
        _token=_BINDING_TOKEN,
    )


def _validate_decision_authority(
    decision_state: DecisionState,
    authority: IntentAuthority,
    *,
    name: str,
) -> None:
    if type(decision_state) is not DecisionState:
        raise TypeError(f"{name}.decision_state must be exact")
    if type(authority) is not IntentAuthority:
        raise TypeError(f"{name}.authority must be exact")
    if decision_state is not DecisionState.CANDIDATE:
        raise ValueError(
            f"{name} cannot promote decisions in Phase 4.5A"
        )
    if authority not in {
        IntentAuthority.USER_STATED,
        IntentAuthority.AI_SUGGESTED,
        IntentAuthority.PROVIDER_DISCOVERED,
    }:
        raise ValueError(
            f"{name} confirmed authority requires the future host boundary"
        )


@dataclass(frozen=True, slots=True)
class LodgingIntakeIssue:
    """One stable, non-private explanation for the AI planning loop."""

    code: str
    severity: IssueSeverity
    message: str
    candidate_ids: tuple[str, ...] = ()
    nights: tuple[date, ...] = ()
    suggested_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        code = _text(
            self.code,
            "LodgingIntakeIssue.code",
            maximum=128,
        )
        message = _text(
            self.message,
            "LodgingIntakeIssue.message",
            maximum=512,
        )
        if type(self.severity) is not IssueSeverity:
            raise TypeError("LodgingIntakeIssue.severity must be exact")
        if (
            not isinstance(self.candidate_ids, tuple)
            or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(char not in "0123456789abcdef" for char in item)
                for item in self.candidate_ids
            )
        ):
            raise ValueError(
                "LodgingIntakeIssue.candidate_ids must contain digests"
            )
        if (
            not isinstance(self.nights, tuple)
            or any(
                not isinstance(item, date)
                or isinstance(item, datetime)
                for item in self.nights
            )
        ):
            raise TypeError(
                "LodgingIntakeIssue.nights must contain exact dates"
            )
        if (
            not isinstance(self.suggested_actions, tuple)
            or any(
                not isinstance(item, str)
                or _MACHINE_ID_RE.fullmatch(item) is None
                for item in self.suggested_actions
            )
        ):
            raise ValueError(
                "LodgingIntakeIssue.suggested_actions must be machine IDs"
            )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(
            self,
            "candidate_ids",
            tuple(sorted(set(self.candidate_ids))),
        )
        object.__setattr__(
            self,
            "nights",
            tuple(sorted(set(self.nights))),
        )
        object.__setattr__(
            self,
            "suggested_actions",
            tuple(sorted(set(self.suggested_actions))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "candidate_ids": list(self.candidate_ids),
            "nights": [item.isoformat() for item in self.nights],
            "suggested_actions": list(self.suggested_actions),
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingIntakeAssessment:
    """Pure result over exact candidate and required-night inputs."""

    status: LodgingIntakeStatus
    requirement: LodgingRequirement
    stay_start: date
    stay_end: date
    candidates: tuple[LodgingCandidate, ...] = field(
        default=(),
        repr=False,
    )
    required_nights: tuple[date, ...] = ()
    option_missing_nights: tuple[date, ...] = ()
    undecided_nights: tuple[date, ...] = ()
    conflicting_nights: tuple[date, ...] = ()
    needs_verification_candidate_ids: tuple[str, ...] = ()
    issues: tuple[LodgingIntakeIssue, ...] = ()
    assessment_id: str = ""
    contract_version: str = LODGING_INTAKE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ASSESSMENT_TOKEN:
            raise ValueError(
                "Lodging assessments must be created by the trusted assessor"
            )
        if type(self.status) is not LodgingIntakeStatus:
            raise TypeError("LodgingIntakeAssessment.status must be exact")
        if type(self.requirement) is not LodgingRequirement:
            raise TypeError(
                "LodgingIntakeAssessment.requirement must be exact"
            )
        stay_start = _exact_date(self.stay_start, "stay_start")
        stay_end = _exact_date(self.stay_end, "stay_end")
        if stay_end <= stay_start:
            raise ValueError("stay_end must be after stay_start")
        if (stay_end - stay_start).days > _MAX_STAY_NIGHTS:
            raise ValueError(
                "Lodging assessment span exceeds the bounded intake limit"
            )
        if (
            not isinstance(self.candidates, tuple)
            or any(
                type(item) is not LodgingCandidate
                for item in self.candidates
            )
        ):
            raise TypeError(
                "LodgingIntakeAssessment.candidates must contain exact values"
            )
        if (
            not isinstance(self.issues, tuple)
            or any(type(item) is not LodgingIntakeIssue for item in self.issues)
        ):
            raise TypeError(
                "LodgingIntakeAssessment.issues must contain exact values"
            )
        if self.contract_version != LODGING_INTAKE_VERSION:
            raise ValueError("Unsupported lodging intake contract version")
        expected_id = _digest(
            {
                "contract_version": self.contract_version,
                "status": self.status.value,
                "requirement": self.requirement.value,
                "stay_start": stay_start.isoformat(),
                "stay_end": stay_end.isoformat(),
                "candidate_ids": [
                    item.candidate_id for item in self.candidates
                ],
                "required_nights": [
                    item.isoformat() for item in self.required_nights
                ],
                "option_missing_nights": [
                    item.isoformat() for item in self.option_missing_nights
                ],
                "undecided_nights": [
                    item.isoformat() for item in self.undecided_nights
                ],
                "conflicting_nights": [
                    item.isoformat() for item in self.conflicting_nights
                ],
                "needs_verification_candidate_ids": list(
                    self.needs_verification_candidate_ids
                ),
                "issues": [item.to_dict() for item in self.issues],
            },
            prefix="lodging-intake-assessment",
        )
        _check_digest(
            self.assessment_id,
            expected_id,
            "LodgingIntakeAssessment.assessment_id",
        )
        object.__setattr__(self, "assessment_id", expected_id)

    @property
    def needs_verification(self) -> bool:
        return bool(
            self.needs_verification_candidate_ids
            or self.status
            not in {
                LodgingIntakeStatus.NOT_REQUIRED,
            }
        )

    @property
    def advice(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    action
                    for issue in self.issues
                    for action in issue.suggested_actions
                }
            )
        )

    def __repr__(self) -> str:
        return (
            "LodgingIntakeAssessment("
            f"assessment_id={self.assessment_id!r}, "
            f"status={self.status.value!r}, "
            f"candidate_count={len(self.candidates)!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "status": self.status.value,
            "requirement": self.requirement.value,
            "stay_start": self.stay_start.isoformat(),
            "stay_end": self.stay_end.isoformat(),
            "candidates": [item.to_dict() for item in self.candidates],
            "required_nights": [
                item.isoformat() for item in self.required_nights
            ],
            "option_missing_nights": [
                item.isoformat() for item in self.option_missing_nights
            ],
            "undecided_nights": [
                item.isoformat() for item in self.undecided_nights
            ],
            "conflicting_nights": [
                item.isoformat() for item in self.conflicting_nights
            ],
            "needs_verification_candidate_ids": list(
                self.needs_verification_candidate_ids
            ),
            "issues": [item.to_dict() for item in self.issues],
            "advice": list(self.advice),
            "needs_verification": self.needs_verification,
            "contract_version": self.contract_version,
        }


def assess_lodging_intake(
    *,
    stay_start: date,
    stay_end: date,
    requirement: LodgingRequirement = LodgingRequirement.UNKNOWN,
    candidates: tuple[LodgingCandidate, ...] = (),
) -> LodgingIntakeAssessment:
    """Assess lodging coverage without selecting or fabricating a candidate."""

    stay_start = _exact_date(stay_start, "stay_start")
    stay_end = _exact_date(stay_end, "stay_end")
    if stay_end <= stay_start:
        raise ValueError("stay_end must be after stay_start")
    stay_nights = (stay_end - stay_start).days
    if stay_nights > _MAX_STAY_NIGHTS:
        raise ValueError(
            "Lodging assessment span exceeds the bounded intake limit"
        )
    if type(requirement) is not LodgingRequirement:
        raise TypeError("requirement must be exact")
    if (
        not isinstance(candidates, tuple)
        or any(type(item) is not LodgingCandidate for item in candidates)
    ):
        raise TypeError("candidates must contain exact LodgingCandidate values")
    if len(candidates) > _MAX_LODGING_CANDIDATES:
        raise ValueError(
            "Lodging assessment has too many candidates"
        )
    candidate_ids = [item.candidate_id for item in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidates cannot contain duplicates")
    ordered_candidates = tuple(
        sorted(candidates, key=lambda item: item.candidate_id)
    )
    all_nights = tuple(
        stay_start + timedelta(days=offset)
        for offset in range(stay_nights)
    )
    active = ordered_candidates

    if requirement is LodgingRequirement.NOT_REQUIRED:
        if active:
            issue = LodgingIntakeIssue(
                code="LODGING_NOT_REQUIRED_CONFLICT",
                severity=IssueSeverity.ERROR,
                message=(
                    "Active lodging candidates conflict with an explicit "
                    "no-lodging requirement."
                ),
                candidate_ids=tuple(
                    item.candidate_id for item in active
                ),
                suggested_actions=("clarify_lodging_requirement",),
            )
            return _assessment(
                status=LodgingIntakeStatus.CONFLICTED,
                requirement=requirement,
                stay_start=stay_start,
                stay_end=stay_end,
                candidates=ordered_candidates,
                issues=(issue,),
            )
        return _assessment(
            status=LodgingIntakeStatus.NOT_REQUIRED,
            requirement=requirement,
            stay_start=stay_start,
            stay_end=stay_end,
            candidates=ordered_candidates,
        )

    options_by_night = {
        night: tuple(item for item in active if item.covers(night))
        for night in all_nights
    }
    reported = tuple(
        item
        for item in active
        if item.draft.reported_decision is not None
    )
    reported_by_night = {
        night: tuple(item for item in reported if item.covers(night))
        for night in all_nights
    }
    option_missing = tuple(
        night for night in all_nights if not options_by_night[night]
    )
    undecided = tuple(
        night for night in all_nights if not reported_by_night[night]
    )
    conflicts = tuple(
        night
        for night in all_nights
        if len(reported_by_night[night]) > 1
    )
    verification_ids = tuple(
        item.candidate_id for item in reported
    )
    issues: list[LodgingIntakeIssue] = []

    if not active:
        if requirement is LodgingRequirement.OPTIONS_WANTED:
            issues.append(
                LodgingIntakeIssue(
                    code="LODGING_OPTIONS_REQUESTED",
                    severity=IssueSeverity.INFO,
                    message=(
                        "The user wants lodging options, but no candidate "
                        "has been added yet."
                    ),
                    nights=all_nights,
                    suggested_actions=("suggest_lodging_options",),
                )
            )
        else:
            issues.append(
                LodgingIntakeIssue(
                    code="LODGING_INPUT_MISSING",
                    severity=IssueSeverity.WARNING,
                    message=(
                        "No lodging was provided; the planner must keep "
                        "the stay unknown."
                    ),
                    nights=all_nights,
                    suggested_actions=(
                        "ask_lodging_preference",
                        "suggest_lodging_options",
                    ),
                )
            )
    elif option_missing:
        issues.append(
            LodgingIntakeIssue(
                code="LODGING_OPTION_COVERAGE_MISSING",
                severity=IssueSeverity.WARNING,
                message=(
                    "No lodging candidate covers one or more required nights."
                ),
                nights=option_missing,
                suggested_actions=("suggest_lodging_options",),
            )
        )

    if active and not reported:
        issues.append(
            LodgingIntakeIssue(
                code="LODGING_DECISION_REQUIRED",
                severity=IssueSeverity.INFO,
                message=(
                    "Lodging candidates exist, but no user decision wording "
                    "has been reported."
                ),
                candidate_ids=tuple(
                    item.candidate_id for item in active
                ),
                nights=all_nights,
                suggested_actions=("compare_lodging_options",),
            )
        )
    elif reported:
        issues.append(
            LodgingIntakeIssue(
                code="LODGING_DECISION_CLAIM_REQUIRES_CONFIRMATION",
                severity=IssueSeverity.INFO,
                message=(
                    "User decision wording was retained as a claim, but "
                    "Phase 4.5A cannot promote it to an authoritative state."
                ),
                candidate_ids=tuple(
                    item.candidate_id for item in reported
                ),
                suggested_actions=("confirm_lodging_decision",),
            )
        )
    if reported and undecided:
        issues.append(
            LodgingIntakeIssue(
                code="LODGING_DECISION_CLAIM_COVERAGE_MISSING",
                severity=IssueSeverity.WARNING,
                message=(
                    "Reported lodging decision wording does not cover every "
                    "required night."
                ),
                candidate_ids=tuple(
                    item.candidate_id for item in reported
                ),
                nights=undecided,
                suggested_actions=("clarify_lodging_nights",),
            )
        )
    if conflicts:
        conflict_ids = tuple(
            sorted(
                {
                    item.candidate_id
                    for night in conflicts
                    for item in reported_by_night[night]
                }
            )
        )
        issues.append(
            LodgingIntakeIssue(
                code="LODGING_DECISION_CLAIM_CONFLICT",
                severity=IssueSeverity.ERROR,
                message=(
                    "Multiple reported lodging decisions cover the same "
                    "night and require clarification."
                ),
                candidate_ids=conflict_ids,
                nights=conflicts,
                suggested_actions=("resolve_lodging_conflict",),
            )
        )
    if verification_ids:
        issues.append(
            LodgingIntakeIssue(
                code="LODGING_NEEDS_VERIFICATION",
                severity=IssueSeverity.WARNING,
                message=(
                    "A lodging decision claim and its location still need "
                    "host confirmation and evidence verification."
                ),
                candidate_ids=verification_ids,
                suggested_actions=("verify_lodging_location",),
            )
        )

    if conflicts:
        status = LodgingIntakeStatus.CONFLICTED
    elif not active:
        status = (
            LodgingIntakeStatus.SEEKING_OPTIONS
            if requirement is LodgingRequirement.OPTIONS_WANTED
            else LodgingIntakeStatus.MISSING
        )
    elif not reported:
        status = LodgingIntakeStatus.COMPARING
    elif undecided:
        status = LodgingIntakeStatus.PARTIAL
    else:
        status = LodgingIntakeStatus.AWAITING_CONFIRMATION

    return _assessment(
        status=status,
        requirement=requirement,
        stay_start=stay_start,
        stay_end=stay_end,
        candidates=ordered_candidates,
        required_nights=all_nights,
        option_missing_nights=option_missing,
        undecided_nights=undecided,
        conflicting_nights=conflicts,
        needs_verification_candidate_ids=verification_ids,
        issues=tuple(issues),
    )


def _assessment(
    *,
    status: LodgingIntakeStatus,
    requirement: LodgingRequirement,
    stay_start: date,
    stay_end: date,
    candidates: tuple[LodgingCandidate, ...],
    required_nights: tuple[date, ...] = (),
    option_missing_nights: tuple[date, ...] = (),
    undecided_nights: tuple[date, ...] = (),
    conflicting_nights: tuple[date, ...] = (),
    needs_verification_candidate_ids: tuple[str, ...] = (),
    issues: tuple[LodgingIntakeIssue, ...] = (),
) -> LodgingIntakeAssessment:
    return LodgingIntakeAssessment(
        status=status,
        requirement=requirement,
        stay_start=stay_start,
        stay_end=stay_end,
        candidates=candidates,
        required_nights=required_nights,
        option_missing_nights=option_missing_nights,
        undecided_nights=undecided_nights,
        conflicting_nights=conflicting_nights,
        needs_verification_candidate_ids=(
            needs_verification_candidate_ids
        ),
        issues=issues,
        _token=_ASSESSMENT_TOKEN,
    )


__all__ = [
    "IntentAuthority",
    "LODGING_INTAKE_VERSION",
    "LocationHint",
    "LocationHintKind",
    "LocationPrecision",
    "LodgingCandidate",
    "LodgingIntakeAssessment",
    "LodgingIntakeIssue",
    "LodgingIntakeStatus",
    "LodgingIntentDraft",
    "LodgingKind",
    "LodgingRequirement",
    "PriceBasis",
    "ReportedDecisionClaim",
    "TransportBoundary",
    "TransportBoundaryDraft",
    "TransportBoundaryKind",
    "assess_lodging_intake",
    "bind_lodging_candidate",
    "bind_transport_boundary",
]
