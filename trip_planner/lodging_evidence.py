"""Snapshot-bound lodging evidence and provider-neutral comparison sidecars.

Phase 4.5A lodging candidates deliberately remain ``candidate + unverified``.
This module never rebuilds or promotes those candidates.  Instead, it projects
trusted place identity and route observations into a runtime-only comparison
sidecar.  The projection is bound to one exact :class:`EvidenceSnapshot`, and
its safe serialization contains no raw lodging label, address, coordinates,
provider place ID, route endpoint, departure time, or price amount.

Route observations need an exact request receipt.  A route result necessarily
lands in a snapshot *after* the snapshot that authorized its request, so a
matching route key alone is insufficient.  The receipt lets this module verify
the provider request fingerprint and the exact endpoint observations that were
used.  Missing receipts or changed endpoint evidence fail closed and produce a
fresh, current-snapshot request for an explicitly authorized host to execute.
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
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .facts import (
    EvidenceSnapshot,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactResolution,
)
from .lodging import (
    LocationHintKind,
    LocationPrecision,
    LodgingCandidate,
)
from .models import (
    DecisionState,
    EvidenceState,
    IssueSeverity,
)
from .places_identity import (
    PlaceEndpointIdentity,
    extract_fresh_google_place_endpoint,
)
from .routes import (
    GoogleRouteRequest,
    RouteMode,
    build_google_route_request,
)


LODGING_COMPARISON_VERSION = "lodging-comparison/v1"
_MAX_CANDIDATES = 256
_MAX_ROUTE_PROBES = 256
_MAX_TEXT = 512
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MACHINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_PROCESS_DIGEST_KEY = secrets.token_bytes(32)
_RESULT_TOKEN = object()


class LodgingComparisonCapability(str, Enum):
    """Highest comparison capability supported by trusted evidence."""

    BASIC_ONLY = "basic_only"
    IDENTITY_BOUND = "identity_bound"
    ROUTE_BOUND = "route_bound"
    BLOCKED = "blocked"


class LodgingIdentityDisposition(str, Enum):
    """Why a lodging location is or is not a trusted route endpoint."""

    VERIFIED = "verified"
    MISSING = "missing"
    STALE = "stale"
    CONFLICTED = "conflicted"
    APPROXIMATE_AREA = "approximate_area"
    ADDRESS_NEEDS_IDENTITY = "address_needs_identity"
    COORDINATES_NEED_IDENTITY = "coordinates_need_identity"
    UNREVIEWED_PROVIDER_ID = "unreviewed_provider_id"


class LodgingRouteDirection(str, Enum):
    """Ordered route direction relative to one lodging candidate."""

    FROM_LODGING = "from_lodging"
    TO_LODGING = "to_lodging"


class LodgingRouteDisposition(str, Enum):
    """Why one requested comparison route is or is not usable."""

    VERIFIED = "verified"
    MISSING = "missing"
    STALE = "stale"
    CONFLICTED = "conflicted"
    CANDIDATE_IDENTITY_REQUIRED = "candidate_identity_required"
    ANCHOR_IDENTITY_REQUIRED = "anchor_identity_required"
    BASIS_RECEIPT_REQUIRED = "basis_receipt_required"
    BASIS_CHANGED = "basis_changed"


def _canonical_digest(value: Any, *, prefix: str) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(prefix.encode("utf-8") + b"\n" + encoded).hexdigest()


def _private_digest(value: Any, *, prefix: str) -> str:
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


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, name: str, *, maximum: int = _MAX_TEXT) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must be non-empty bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{name} cannot contain control characters")
    return normalized


def _machine_id(value: object, name: str) -> str:
    normalized = _text(value, name, maximum=256)
    if _MACHINE_ID_RE.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a bounded machine identifier")
    return normalized


def _utc_text(value: object, name: str) -> str:
    normalized = _text(value, name, maximum=128)
    parseable = normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
    try:
        parsed = datetime.fromisoformat(parseable)
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC3339 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _identity_key(location_id: str) -> FactKey:
    return FactKey(
        kind=FactKind.PLACE_IDENTITY,
        subject_ids=(location_id,),
        qualifiers=(("identity_provider", "google-places"),),
    )


def _resolution_observation_ids(
    resolution: FactResolution | None,
) -> tuple[str, ...]:
    if resolution is None:
        return ()
    return tuple(
        sorted(item.observation_id for item in resolution.candidates)
    )


def _attribution_labels(observation: FactObservation) -> tuple[str, ...]:
    return tuple(
        sorted({label for label, _uri in observation.provenance.attributions})
    )


@dataclass(frozen=True, slots=True, repr=False)
class LodgingRouteProbe:
    """One bounded, process-local route comparison request.

    ``basis_request`` is the exact request receipt that produced a route
    observation in a later snapshot.  It is optional while the route is still
    pending.  Raw anchor identity and departure time never enter the safe view.
    """

    candidate_id: str
    anchor_location_id: str = field(repr=False)
    direction: LodgingRouteDirection
    mode: RouteMode
    departure_at: str = field(repr=False)
    basis_request: GoogleRouteRequest | None = field(
        default=None,
        repr=False,
    )
    anchor_ref: str = ""
    probe_id: str = ""

    def __post_init__(self) -> None:
        candidate_id = _digest(
            self.candidate_id,
            "LodgingRouteProbe.candidate_id",
        )
        anchor_location_id = _machine_id(
            self.anchor_location_id,
            "LodgingRouteProbe.anchor_location_id",
        )
        if type(self.direction) is not LodgingRouteDirection:
            raise TypeError("LodgingRouteProbe.direction must be exact")
        if type(self.mode) is not RouteMode:
            raise TypeError("LodgingRouteProbe.mode must be exact")
        departure_at = _utc_text(
            self.departure_at,
            "LodgingRouteProbe.departure_at",
        )
        if (
            self.basis_request is not None
            and type(self.basis_request) is not GoogleRouteRequest
        ):
            raise TypeError(
                "LodgingRouteProbe.basis_request must be an exact "
                "GoogleRouteRequest"
            )
        anchor_ref = _private_digest(
            {"anchor_location_id": anchor_location_id},
            prefix="lodging-route-anchor",
        )
        if self.anchor_ref and self.anchor_ref != anchor_ref:
            raise ValueError(
                "LodgingRouteProbe.anchor_ref does not match its anchor"
            )
        probe_id = _private_digest(
            {
                "candidate_id": candidate_id,
                "anchor_ref": anchor_ref,
                "direction": self.direction.value,
                "mode": self.mode.value,
                "departure_at": departure_at,
            },
            prefix="lodging-route-probe",
        )
        if self.probe_id and self.probe_id != probe_id:
            raise ValueError(
                "LodgingRouteProbe.probe_id does not match its scope"
            )
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(
            self,
            "anchor_location_id",
            anchor_location_id,
        )
        object.__setattr__(self, "departure_at", departure_at)
        object.__setattr__(self, "anchor_ref", anchor_ref)
        object.__setattr__(self, "probe_id", probe_id)

    def __repr__(self) -> str:
        return (
            "LodgingRouteProbe("
            f"probe_id={self.probe_id!r}, "
            f"candidate_id={self.candidate_id!r}, "
            f"direction={self.direction.value!r}, "
            f"mode={self.mode.value!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "candidate_id": self.candidate_id,
            "anchor_ref": self.anchor_ref,
            "direction": self.direction.value,
            "mode": self.mode.value,
            "has_departure_context": True,
            "has_basis_request": self.basis_request is not None,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingIdentityEvidence:
    """Trusted identity resolution for one unchanged 4.5A candidate."""

    candidate: LodgingCandidate = field(repr=False)
    snapshot: EvidenceSnapshot = field(repr=False)
    resolution: FactResolution | None = field(repr=False)
    endpoint: PlaceEndpointIdentity | None = field(
        default=None,
        repr=False,
    )
    identity_id: str = ""
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESULT_TOKEN:
            raise ValueError(
                "Lodging identity evidence must come from the trusted assessor"
            )
        if type(self.candidate) is not LodgingCandidate:
            raise TypeError("candidate must be an exact LodgingCandidate")
        if type(self.snapshot) is not EvidenceSnapshot:
            raise TypeError("snapshot must be an exact EvidenceSnapshot")
        location = self.candidate.draft.location
        if location.kind is LocationHintKind.LOCATION_ID:
            if type(self.resolution) is not FactResolution:
                raise TypeError(
                    "A location ID requires an exact FactResolution"
                )
            if (
                self.resolution.snapshot_id != self.snapshot.snapshot_id
                or self.resolution.key.key_id
                != _identity_key(location.location_id or "").key_id
            ):
                raise FactContractError(
                    "EVIDENCE_BINDING_MISMATCH",
                    "Lodging identity resolution differs from its snapshot.",
                )
            verified = (
                self.resolution.evidence_state is EvidenceState.VERIFIED
            )
            if verified != (type(self.endpoint) is PlaceEndpointIdentity):
                raise FactContractError(
                    "EVIDENCE_BINDING_MISMATCH",
                    "Fresh lodging identity and endpoint availability differ.",
                )
            if self.endpoint is not None and (
                self.endpoint.snapshot_id != self.snapshot.snapshot_id
                or self.resolution.selected is None
                or self.endpoint.observation_id
                != self.resolution.selected.observation_id
                or self.endpoint.value_digest
                != self.resolution.selected.value.value_digest
            ):
                raise FactContractError(
                    "EVIDENCE_BINDING_MISMATCH",
                    "Lodging endpoint differs from its selected identity.",
                )
        elif self.resolution is not None or self.endpoint is not None:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Unreviewed private location hints cannot carry endpoints.",
            )

        expected = _canonical_digest(
            {
                "candidate_id": self.candidate.candidate_id,
                "snapshot_id": self.snapshot.snapshot_id,
                "precision": location.precision.value,
                "disposition": self.disposition.value,
                "evidence_state": self.evidence_state.value,
                "observation_ids": list(self.observation_ids),
                "used_observation_id": self.used_observation_id,
                "used_value_digest": self.used_value_digest,
            },
            prefix="lodging-identity-evidence",
        )
        if self.identity_id and self.identity_id != expected:
            raise ValueError(
                "LodgingIdentityEvidence.identity_id does not match content"
            )
        object.__setattr__(self, "identity_id", expected)

    @property
    def candidate_id(self) -> str:
        return self.candidate.candidate_id

    @property
    def precision(self) -> LocationPrecision:
        return self.candidate.draft.location.precision

    @property
    def evidence_state(self) -> EvidenceState:
        if self.resolution is None:
            return EvidenceState.UNVERIFIED
        return self.resolution.evidence_state

    @property
    def disposition(self) -> LodgingIdentityDisposition:
        location = self.candidate.draft.location
        if location.kind is LocationHintKind.AREA:
            return LodgingIdentityDisposition.APPROXIMATE_AREA
        if location.kind is LocationHintKind.ADDRESS:
            return LodgingIdentityDisposition.ADDRESS_NEEDS_IDENTITY
        if location.kind is LocationHintKind.COORDINATES:
            return LodgingIdentityDisposition.COORDINATES_NEED_IDENTITY
        if location.kind is LocationHintKind.PLACE_ID:
            return LodgingIdentityDisposition.UNREVIEWED_PROVIDER_ID
        assert self.resolution is not None
        return {
            EvidenceState.VERIFIED: LodgingIdentityDisposition.VERIFIED,
            EvidenceState.UNVERIFIED: LodgingIdentityDisposition.MISSING,
            EvidenceState.STALE: LodgingIdentityDisposition.STALE,
            EvidenceState.CONFLICTED: LodgingIdentityDisposition.CONFLICTED,
        }[self.resolution.evidence_state]

    @property
    def observation_ids(self) -> tuple[str, ...]:
        return _resolution_observation_ids(self.resolution)

    @property
    def used_observation_id(self) -> str | None:
        if self.endpoint is None:
            return None
        return self.endpoint.observation_id

    @property
    def used_value_digest(self) -> str | None:
        if self.endpoint is None:
            return None
        return self.endpoint.value_digest

    @property
    def attribution_labels(self) -> tuple[str, ...]:
        if (
            self.resolution is None
            or self.resolution.selected is None
            or self.endpoint is None
        ):
            return ()
        return _attribution_labels(self.resolution.selected)

    def __repr__(self) -> str:
        return (
            "LodgingIdentityEvidence("
            f"identity_id={self.identity_id!r}, "
            f"candidate_id={self.candidate_id!r}, "
            f"disposition={self.disposition.value!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_id": self.identity_id,
            "candidate_id": self.candidate_id,
            "precision": self.precision.value,
            "disposition": self.disposition.value,
            "evidence_state": self.evidence_state.value,
            "observation_ids": list(self.observation_ids),
            "used_observation_id": self.used_observation_id,
            "used_value_digest": self.used_value_digest,
            "has_route_endpoint": self.endpoint is not None,
            "attribution_labels": list(self.attribution_labels),
            "snapshot_id": self.snapshot.snapshot_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingRouteEvidence:
    """One route evidence projection with an optional refresh request."""

    probe: LodgingRouteProbe = field(repr=False)
    snapshot: EvidenceSnapshot = field(repr=False)
    candidate_identity: LodgingIdentityEvidence = field(repr=False)
    anchor_resolution: FactResolution | None = field(repr=False)
    anchor_endpoint: PlaceEndpointIdentity | None = field(
        default=None,
        repr=False,
    )
    resolution: FactResolution | None = field(default=None, repr=False)
    refresh_request: GoogleRouteRequest | None = field(
        default=None,
        repr=False,
    )
    basis_unchanged: bool = False
    route_id: str = ""
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESULT_TOKEN:
            raise ValueError(
                "Lodging route evidence must come from the trusted assessor"
            )
        if type(self.probe) is not LodgingRouteProbe:
            raise TypeError("probe must be an exact LodgingRouteProbe")
        if type(self.snapshot) is not EvidenceSnapshot:
            raise TypeError("snapshot must be an exact EvidenceSnapshot")
        if type(self.candidate_identity) is not LodgingIdentityEvidence:
            raise TypeError(
                "candidate_identity must be exact LodgingIdentityEvidence"
            )
        if (
            self.candidate_identity.snapshot.snapshot_id
            != self.snapshot.snapshot_id
            or self.candidate_identity.candidate_id
            != self.probe.candidate_id
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Route evidence differs from its candidate snapshot.",
            )
        if (
            self.anchor_resolution is not None
            and type(self.anchor_resolution) is not FactResolution
        ):
            raise TypeError("anchor_resolution must be exact or None")
        if (
            self.anchor_endpoint is not None
            and type(self.anchor_endpoint) is not PlaceEndpointIdentity
        ):
            raise TypeError("anchor_endpoint must be exact or None")
        if (
            self.resolution is not None
            and type(self.resolution) is not FactResolution
        ):
            raise TypeError("resolution must be exact or None")
        if (
            self.refresh_request is not None
            and type(self.refresh_request) is not GoogleRouteRequest
        ):
            raise TypeError("refresh_request must be exact or None")
        if type(self.basis_unchanged) is not bool:
            raise TypeError("basis_unchanged must be bool")
        if self.anchor_resolution is not None and (
            self.anchor_resolution.snapshot_id != self.snapshot.snapshot_id
            or self.anchor_resolution.key.key_id
            != _identity_key(self.probe.anchor_location_id).key_id
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Route anchor resolution differs from its snapshot.",
            )
        if self.anchor_endpoint is not None and (
            self.anchor_resolution is None
            or self.anchor_resolution.evidence_state
            is not EvidenceState.VERIFIED
            or self.anchor_endpoint.snapshot_id != self.snapshot.snapshot_id
            or self.anchor_resolution.selected is None
            or self.anchor_endpoint.observation_id
            != self.anchor_resolution.selected.observation_id
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Route anchor endpoint differs from its selected identity.",
            )
        if self.resolution is not None and (
            self.resolution.snapshot_id != self.snapshot.snapshot_id
            or self.probe.basis_request is None
            or self.resolution.key.key_id
            != self.probe.basis_request.provider_request.fact_keys[0].key_id
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Route resolution differs from its exact request receipt.",
            )
        if self.disposition is LodgingRouteDisposition.VERIFIED and (
            self.resolution is None
            or self.resolution.selected is None
            or not self.basis_unchanged
            or self.probe.basis_request is None
            or self.resolution.selected.provenance.request_fingerprint
            != self.probe.basis_request.provider_request.request_fingerprint
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Verified lodging route lacks an exact current basis receipt.",
            )

        expected = _canonical_digest(
            {
                "probe_id": self.probe.probe_id,
                "snapshot_id": self.snapshot.snapshot_id,
                "candidate_identity_id": (
                    self.candidate_identity.identity_id
                ),
                "anchor_evidence_state": self.anchor_evidence_state.value,
                "anchor_observation_ids": list(
                    self.anchor_observation_ids
                ),
                "disposition": self.disposition.value,
                "evidence_state": self.evidence_state.value,
                "route_observation_ids": list(
                    self.route_observation_ids
                ),
                "used_observation_id": self.used_observation_id,
                "used_value_digest": self.used_value_digest,
                "basis_unchanged": self.basis_unchanged,
                "can_request_refresh": self.refresh_request is not None,
            },
            prefix="lodging-route-evidence",
        )
        if self.route_id and self.route_id != expected:
            raise ValueError(
                "LodgingRouteEvidence.route_id does not match content"
            )
        object.__setattr__(self, "route_id", expected)

    @property
    def anchor_evidence_state(self) -> EvidenceState:
        if self.anchor_resolution is None:
            return EvidenceState.UNVERIFIED
        return self.anchor_resolution.evidence_state

    @property
    def disposition(self) -> LodgingRouteDisposition:
        candidate_state = self.candidate_identity.evidence_state
        if self.candidate_identity.endpoint is None:
            if candidate_state is EvidenceState.CONFLICTED:
                return LodgingRouteDisposition.CONFLICTED
            if candidate_state is EvidenceState.STALE:
                return LodgingRouteDisposition.STALE
            return LodgingRouteDisposition.CANDIDATE_IDENTITY_REQUIRED
        if self.anchor_endpoint is None:
            if self.anchor_evidence_state is EvidenceState.CONFLICTED:
                return LodgingRouteDisposition.CONFLICTED
            if self.anchor_evidence_state is EvidenceState.STALE:
                return LodgingRouteDisposition.STALE
            return LodgingRouteDisposition.ANCHOR_IDENTITY_REQUIRED
        if self.probe.basis_request is None:
            return LodgingRouteDisposition.BASIS_RECEIPT_REQUIRED
        if not self.basis_unchanged:
            return LodgingRouteDisposition.BASIS_CHANGED
        if self.resolution is None:
            return LodgingRouteDisposition.MISSING
        if self.resolution.evidence_state is EvidenceState.CONFLICTED:
            return LodgingRouteDisposition.CONFLICTED
        if self.resolution.evidence_state is EvidenceState.STALE:
            return LodgingRouteDisposition.STALE
        if self.resolution.evidence_state is EvidenceState.UNVERIFIED:
            return LodgingRouteDisposition.MISSING
        assert self.resolution.selected is not None
        assert self.probe.basis_request is not None
        if (
            self.resolution.selected.provenance.request_fingerprint
            != self.probe.basis_request.provider_request.request_fingerprint
        ):
            return LodgingRouteDisposition.BASIS_CHANGED
        return LodgingRouteDisposition.VERIFIED

    @property
    def evidence_state(self) -> EvidenceState:
        return {
            LodgingRouteDisposition.VERIFIED: EvidenceState.VERIFIED,
            LodgingRouteDisposition.STALE: EvidenceState.STALE,
            LodgingRouteDisposition.CONFLICTED: EvidenceState.CONFLICTED,
            LodgingRouteDisposition.MISSING: EvidenceState.UNVERIFIED,
            LodgingRouteDisposition.CANDIDATE_IDENTITY_REQUIRED: (
                EvidenceState.UNVERIFIED
            ),
            LodgingRouteDisposition.ANCHOR_IDENTITY_REQUIRED: (
                EvidenceState.UNVERIFIED
            ),
            LodgingRouteDisposition.BASIS_RECEIPT_REQUIRED: (
                EvidenceState.UNVERIFIED
            ),
            LodgingRouteDisposition.BASIS_CHANGED: EvidenceState.UNVERIFIED,
        }[self.disposition]

    @property
    def anchor_observation_ids(self) -> tuple[str, ...]:
        return _resolution_observation_ids(self.anchor_resolution)

    @property
    def route_observation_ids(self) -> tuple[str, ...]:
        return _resolution_observation_ids(self.resolution)

    @property
    def used_observation_id(self) -> str | None:
        if (
            self.disposition is not LodgingRouteDisposition.VERIFIED
            or self.resolution is None
            or self.resolution.selected is None
        ):
            return None
        return self.resolution.selected.observation_id

    @property
    def used_value_digest(self) -> str | None:
        if (
            self.disposition is not LodgingRouteDisposition.VERIFIED
            or self.resolution is None
            or self.resolution.selected is None
        ):
            return None
        return self.resolution.selected.value.value_digest

    @property
    def duration_min(self) -> float | None:
        if (
            self.disposition is not LodgingRouteDisposition.VERIFIED
            or self.resolution is None
            or self.resolution.selected is None
        ):
            return None
        return float(self.resolution.selected.value.payload["duration_min"])

    @property
    def distance_km(self) -> float | None:
        if (
            self.disposition is not LodgingRouteDisposition.VERIFIED
            or self.resolution is None
            or self.resolution.selected is None
        ):
            return None
        value = self.resolution.selected.value.payload.get("distance_km")
        return None if value is None else float(value)

    @property
    def warning_codes(self) -> tuple[str, ...]:
        if (
            self.disposition is not LodgingRouteDisposition.VERIFIED
            or self.resolution is None
            or self.resolution.selected is None
        ):
            return ()
        return tuple(
            self.resolution.selected.value.payload.get("warning_codes", ())
        )

    @property
    def attribution_labels(self) -> tuple[str, ...]:
        if (
            self.disposition is not LodgingRouteDisposition.VERIFIED
            or self.resolution is None
            or self.resolution.selected is None
        ):
            return ()
        return _attribution_labels(self.resolution.selected)

    def __repr__(self) -> str:
        return (
            "LodgingRouteEvidence("
            f"route_id={self.route_id!r}, "
            f"probe_id={self.probe.probe_id!r}, "
            f"disposition={self.disposition.value!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "probe": self.probe.to_dict(),
            "disposition": self.disposition.value,
            "evidence_state": self.evidence_state.value,
            "anchor_evidence_state": self.anchor_evidence_state.value,
            "anchor_observation_ids": list(
                self.anchor_observation_ids
            ),
            "route_observation_ids": list(
                self.route_observation_ids
            ),
            "used_observation_id": self.used_observation_id,
            "used_value_digest": self.used_value_digest,
            "basis_unchanged": self.basis_unchanged,
            "has_duration": self.duration_min is not None,
            "has_distance": self.distance_km is not None,
            "warning_codes": list(self.warning_codes),
            "attribution_labels": list(self.attribution_labels),
            "can_request_refresh": self.refresh_request is not None,
            "snapshot_id": self.snapshot.snapshot_id,
        }


@dataclass(frozen=True, slots=True)
class LodgingEvidenceIssue:
    """Stable, secret-free guidance for the natural-language planning loop."""

    code: str
    severity: IssueSeverity
    message: str
    candidate_ids: tuple[str, ...] = ()
    probe_ids: tuple[str, ...] = ()
    suggested_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        code = _machine_id(self.code, "LodgingEvidenceIssue.code")
        message = _text(
            self.message,
            "LodgingEvidenceIssue.message",
            maximum=512,
        )
        if type(self.severity) is not IssueSeverity:
            raise TypeError("LodgingEvidenceIssue.severity must be exact")
        candidate_ids = _normalized_digests(
            self.candidate_ids,
            "LodgingEvidenceIssue.candidate_ids",
        )
        probe_ids = _normalized_digests(
            self.probe_ids,
            "LodgingEvidenceIssue.probe_ids",
        )
        if (
            not isinstance(self.suggested_actions, tuple)
            or any(
                type(item) is not str
                or _MACHINE_ID_RE.fullmatch(item) is None
                for item in self.suggested_actions
            )
        ):
            raise ValueError(
                "LodgingEvidenceIssue.suggested_actions must be machine IDs"
            )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "candidate_ids", candidate_ids)
        object.__setattr__(self, "probe_ids", probe_ids)
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
            "probe_ids": list(self.probe_ids),
            "suggested_actions": list(self.suggested_actions),
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingComparisonCandidate:
    """Provider-neutral comparison view that cannot promote its candidate."""

    candidate: LodgingCandidate = field(repr=False)
    identity: LodgingIdentityEvidence
    routes: tuple[LodgingRouteEvidence, ...] = field(
        default=(),
        repr=False,
    )
    comparison_id: str = ""
    contract_version: str = LODGING_COMPARISON_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESULT_TOKEN:
            raise ValueError(
                "Comparison candidates must come from the trusted assessor"
            )
        if type(self.candidate) is not LodgingCandidate:
            raise TypeError("candidate must be exact LodgingCandidate")
        if (
            self.candidate.decision_state is not DecisionState.CANDIDATE
            or self.candidate.evidence_state is not EvidenceState.UNVERIFIED
            or self.candidate.evidence_refs != ()
        ):
            raise ValueError(
                "Comparison sidecars require unchanged 4.5A candidates"
            )
        if type(self.identity) is not LodgingIdentityEvidence:
            raise TypeError("identity must be exact LodgingIdentityEvidence")
        if self.identity.candidate_id != self.candidate.candidate_id:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Comparison identity differs from its candidate.",
            )
        if (
            not isinstance(self.routes, tuple)
            or any(type(item) is not LodgingRouteEvidence for item in self.routes)
        ):
            raise TypeError("routes must contain exact route evidence")
        ordered_routes = tuple(
            sorted(self.routes, key=lambda item: item.probe.probe_id)
        )
        if len({item.probe.probe_id for item in ordered_routes}) != len(
            ordered_routes
        ):
            raise ValueError("Comparison candidate contains duplicate routes")
        if any(
            item.probe.candidate_id != self.candidate.candidate_id
            or item.snapshot.snapshot_id
            != self.identity.snapshot.snapshot_id
            for item in ordered_routes
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Comparison routes differ from the candidate evidence basis.",
            )
        object.__setattr__(self, "routes", ordered_routes)
        if self.contract_version != LODGING_COMPARISON_VERSION:
            raise ValueError("Unsupported lodging comparison version")
        expected = _canonical_digest(
            {
                "contract_version": self.contract_version,
                "candidate_id": self.candidate.candidate_id,
                "decision_state": self.candidate.decision_state.value,
                "candidate_evidence_state": (
                    self.candidate.evidence_state.value
                ),
                "identity_id": self.identity.identity_id,
                "route_ids": [item.route_id for item in ordered_routes],
                "capability": self.capability.value,
                "unknown_fields": list(self.unknown_fields),
                "needs_verification": self.needs_verification,
            },
            prefix="lodging-comparison-candidate",
        )
        if self.comparison_id and self.comparison_id != expected:
            raise ValueError(
                "LodgingComparisonCandidate.comparison_id differs from content"
            )
        object.__setattr__(self, "comparison_id", expected)

    @property
    def candidate_id(self) -> str:
        return self.candidate.candidate_id

    @property
    def capability(self) -> LodgingComparisonCapability:
        states = [self.identity.evidence_state] + [
            item.evidence_state for item in self.routes
        ]
        if EvidenceState.CONFLICTED in states:
            return LodgingComparisonCapability.BLOCKED
        if self.identity.endpoint is None:
            return LodgingComparisonCapability.BASIC_ONLY
        if self.routes and all(
            item.disposition is LodgingRouteDisposition.VERIFIED
            for item in self.routes
        ):
            return LodgingComparisonCapability.ROUTE_BOUND
        return LodgingComparisonCapability.IDENTITY_BOUND

    @property
    def unknown_fields(self) -> tuple[str, ...]:
        values: set[str] = set()
        if self.candidate.draft.price_amount_minor is None:
            values.add("price")
        if self.identity.endpoint is None:
            values.add("location_identity")
        if any(
            item.disposition is not LodgingRouteDisposition.VERIFIED
            for item in self.routes
        ):
            values.add("route_evidence")
        return tuple(sorted(values))

    @property
    def needs_verification(self) -> bool:
        return (
            self.identity.endpoint is None
            or any(
                item.disposition is not LodgingRouteDisposition.VERIFIED
                for item in self.routes
            )
            or self.candidate.draft.price_is_estimate is True
        )

    def __repr__(self) -> str:
        return (
            "LodgingComparisonCandidate("
            f"comparison_id={self.comparison_id!r}, "
            f"candidate_id={self.candidate_id!r}, "
            f"capability={self.capability.value!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        draft = self.candidate.draft
        return {
            "comparison_id": self.comparison_id,
            "candidate_id": self.candidate_id,
            "decision_state": self.candidate.decision_state.value,
            "candidate_evidence_state": (
                self.candidate.evidence_state.value
            ),
            "authority": self.candidate.authority.value,
            "capability": self.capability.value,
            "coverage_nights": (draft.check_out - draft.check_in).days,
            "lodging_kind": draft.kind.value,
            "location_precision": draft.location.precision.value,
            "has_price": draft.price_amount_minor is not None,
            "currency": draft.currency,
            "price_basis": (
                draft.price_basis.value if draft.price_basis is not None else None
            ),
            "price_is_estimate": draft.price_is_estimate,
            "reported_decision_pending": draft.reported_decision is not None,
            "identity": self.identity.to_dict(),
            "routes": [item.to_dict() for item in self.routes],
            "unknown_fields": list(self.unknown_fields),
            "needs_verification": self.needs_verification,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingEvidenceBasis:
    """Exact snapshot identity and observations used by one comparison."""

    snapshot: EvidenceSnapshot = field(repr=False)
    used_observation_ids: tuple[str, ...] = ()
    required_attribution_labels: tuple[str, ...] = ()
    basis_id: str = ""
    contract_version: str = LODGING_COMPARISON_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESULT_TOKEN:
            raise ValueError(
                "Lodging evidence basis must come from the trusted assessor"
            )
        if type(self.snapshot) is not EvidenceSnapshot:
            raise TypeError("snapshot must be exact EvidenceSnapshot")
        used_ids = _normalized_digests(
            self.used_observation_ids,
            "LodgingEvidenceBasis.used_observation_ids",
        )
        active_ids = {
            item.observation_id for item in self.snapshot.observations
        }
        if not set(used_ids).issubset(active_ids):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Lodging evidence basis references inactive observations.",
            )
        labels = _normalized_labels(self.required_attribution_labels)
        if self.contract_version != LODGING_COMPARISON_VERSION:
            raise ValueError("Unsupported lodging comparison version")
        object.__setattr__(self, "used_observation_ids", used_ids)
        object.__setattr__(
            self,
            "required_attribution_labels",
            labels,
        )
        expected = _canonical_digest(
            self._identity_payload(),
            prefix="lodging-evidence-basis",
        )
        if self.basis_id and self.basis_id != expected:
            raise ValueError(
                "LodgingEvidenceBasis.basis_id does not match content"
            )
        object.__setattr__(self, "basis_id", expected)

    def _identity_payload(self) -> dict[str, Any]:
        snapshot = self.snapshot
        return {
            "contract_version": self.contract_version,
            "policy_registry_revision": snapshot.policies.revision,
            "store_revision": snapshot.store_revision,
            "evidence_revision": snapshot.evidence_revision,
            "evaluation_at": _utc_iso(snapshot.evaluation_at),
            "purge_checked_at": _utc_iso(snapshot.purge_checked_at),
            "outcome_revision": snapshot.outcome_revision,
            "snapshot_id": snapshot.snapshot_id,
            "used_observation_ids": list(self.used_observation_ids),
            "required_attribution_labels": list(
                self.required_attribution_labels
            ),
        }

    def matches(self, snapshot: EvidenceSnapshot) -> bool:
        if type(snapshot) is not EvidenceSnapshot:
            return False
        current = self.snapshot
        return (
            snapshot.policies.revision == current.policies.revision
            and snapshot.store_revision == current.store_revision
            and snapshot.evidence_revision == current.evidence_revision
            and snapshot.evaluation_at == current.evaluation_at
            and snapshot.purge_checked_at == current.purge_checked_at
            and snapshot.outcome_revision == current.outcome_revision
            and snapshot.snapshot_id == current.snapshot_id
        )

    def __repr__(self) -> str:
        return (
            "LodgingEvidenceBasis("
            f"basis_id={self.basis_id!r}, "
            f"snapshot_id={self.snapshot.snapshot_id!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "basis_id": self.basis_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingComparisonAssessment:
    """Deterministic 4.5B projection over candidates and one snapshot."""

    basis: LodgingEvidenceBasis
    candidates: tuple[LodgingComparisonCandidate, ...] = field(
        default=(),
        repr=False,
    )
    issues: tuple[LodgingEvidenceIssue, ...] = ()
    assessment_id: str = ""
    contract_version: str = LODGING_COMPARISON_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESULT_TOKEN:
            raise ValueError(
                "Lodging comparison must come from the trusted assessor"
            )
        if type(self.basis) is not LodgingEvidenceBasis:
            raise TypeError("basis must be exact LodgingEvidenceBasis")
        if (
            not isinstance(self.candidates, tuple)
            or any(
                type(item) is not LodgingComparisonCandidate
                for item in self.candidates
            )
        ):
            raise TypeError("candidates must contain exact comparison values")
        if (
            not isinstance(self.issues, tuple)
            or any(type(item) is not LodgingEvidenceIssue for item in self.issues)
        ):
            raise TypeError("issues must contain exact lodging evidence issues")
        ordered_candidates = tuple(
            sorted(self.candidates, key=lambda item: item.candidate_id)
        )
        if len({item.candidate_id for item in ordered_candidates}) != len(
            ordered_candidates
        ):
            raise ValueError("Comparison contains duplicate candidates")
        if any(
            item.identity.snapshot.snapshot_id
            != self.basis.snapshot.snapshot_id
            for item in ordered_candidates
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Comparison candidates do not share one evidence basis.",
            )
        ordered_issues = tuple(
            sorted(
                self.issues,
                key=lambda item: (
                    item.code,
                    item.candidate_ids,
                    item.probe_ids,
                    item.message,
                ),
            )
        )
        object.__setattr__(self, "candidates", ordered_candidates)
        object.__setattr__(self, "issues", ordered_issues)
        if self.contract_version != LODGING_COMPARISON_VERSION:
            raise ValueError("Unsupported lodging comparison version")
        used = {
            value
            for item in ordered_candidates
            for value in (
                item.identity.used_observation_id,
                *(
                    route.anchor_endpoint.observation_id
                    if route.anchor_endpoint is not None
                    else None
                    for route in item.routes
                ),
                *(route.used_observation_id for route in item.routes),
            )
            if value is not None
        }
        if tuple(sorted(used)) != self.basis.used_observation_ids:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Comparison used observations differ from its basis.",
            )
        expected = _canonical_digest(
            {
                "contract_version": self.contract_version,
                "basis_id": self.basis.basis_id,
                "comparison_ids": [
                    item.comparison_id for item in ordered_candidates
                ],
                "issues": [item.to_dict() for item in ordered_issues],
            },
            prefix="lodging-comparison-assessment",
        )
        if self.assessment_id and self.assessment_id != expected:
            raise ValueError(
                "LodgingComparisonAssessment.assessment_id differs from content"
            )
        object.__setattr__(self, "assessment_id", expected)

    @property
    def pending_route_requests(self) -> tuple[GoogleRouteRequest, ...]:
        unique: dict[str, GoogleRouteRequest] = {}
        for request in (
            route.refresh_request
            for candidate in self.candidates
            for route in candidate.routes
            if route.refresh_request is not None
        ):
            fingerprint = request.provider_request.request_fingerprint
            previous = unique.get(fingerprint)
            if (
                previous is not None
                and previous.to_binding_dict() != request.to_binding_dict()
            ):
                raise FactContractError(
                    "DIGEST_MISMATCH",
                    "Equivalent route fingerprints have different bindings.",
                )
            unique[fingerprint] = request
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: item.provider_request.request_fingerprint,
            )
        )

    @property
    def needs_verification(self) -> bool:
        return any(item.needs_verification for item in self.candidates)

    def require_snapshot(self, snapshot: EvidenceSnapshot) -> None:
        if not self.basis.matches(snapshot):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                "Lodging comparison evidence snapshot changed.",
            )

    def __repr__(self) -> str:
        return (
            "LodgingComparisonAssessment("
            f"assessment_id={self.assessment_id!r}, "
            f"candidate_count={len(self.candidates)!r}, "
            f"basis_id={self.basis.basis_id!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "basis": self.basis.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "issues": [item.to_dict() for item in self.issues],
            "pending_route_request_count": len(
                self.pending_route_requests
            ),
            "needs_verification": self.needs_verification,
            "contract_version": self.contract_version,
        }


def assess_lodging_evidence(
    *,
    candidates: tuple[LodgingCandidate, ...],
    snapshot: EvidenceSnapshot,
    route_probes: tuple[LodgingRouteProbe, ...] = (),
) -> LodgingComparisonAssessment:
    """Build a safe comparison sidecar without selecting or mutating lodging."""

    if (
        not isinstance(candidates, tuple)
        or any(type(item) is not LodgingCandidate for item in candidates)
    ):
        raise TypeError("candidates must contain exact LodgingCandidate values")
    if len(candidates) > _MAX_CANDIDATES:
        raise ValueError("Lodging comparison exceeds the candidate limit")
    if type(snapshot) is not EvidenceSnapshot:
        raise TypeError("snapshot must be an exact EvidenceSnapshot")
    if (
        not isinstance(route_probes, tuple)
        or any(type(item) is not LodgingRouteProbe for item in route_probes)
    ):
        raise TypeError("route_probes must contain exact LodgingRouteProbe values")
    if len(route_probes) > _MAX_ROUTE_PROBES:
        raise ValueError("Lodging comparison exceeds the route probe limit")

    ordered_candidates = tuple(
        sorted(candidates, key=lambda item: item.candidate_id)
    )
    candidate_ids = [item.candidate_id for item in ordered_candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("Lodging comparison cannot contain duplicates")
    ordered_probes = tuple(
        sorted(route_probes, key=lambda item: item.probe_id)
    )
    if len({item.probe_id for item in ordered_probes}) != len(
        ordered_probes
    ):
        raise ValueError("Lodging comparison cannot contain duplicate probes")
    known_candidate_ids = set(candidate_ids)
    if any(
        item.candidate_id not in known_candidate_ids
        for item in ordered_probes
    ):
        raise ValueError("A route probe references an unknown candidate")

    identities = {
        candidate.candidate_id: _candidate_identity(candidate, snapshot)
        for candidate in ordered_candidates
    }
    anchor_cache: dict[
        str,
        tuple[FactResolution, PlaceEndpointIdentity | None],
    ] = {}
    routes_by_candidate: dict[str, list[LodgingRouteEvidence]] = {
        candidate_id: [] for candidate_id in candidate_ids
    }
    for probe in ordered_probes:
        if probe.anchor_location_id not in anchor_cache:
            anchor_cache[probe.anchor_location_id] = _resolve_endpoint(
                snapshot,
                probe.anchor_location_id,
            )
        anchor_resolution, anchor_endpoint = anchor_cache[
            probe.anchor_location_id
        ]
        route = _route_evidence(
            probe=probe,
            snapshot=snapshot,
            candidate_identity=identities[probe.candidate_id],
            anchor_resolution=anchor_resolution,
            anchor_endpoint=anchor_endpoint,
        )
        routes_by_candidate[probe.candidate_id].append(route)

    comparisons = tuple(
        LodgingComparisonCandidate(
            candidate=candidate,
            identity=identities[candidate.candidate_id],
            routes=tuple(routes_by_candidate[candidate.candidate_id]),
            _token=_RESULT_TOKEN,
        )
        for candidate in ordered_candidates
    )
    issues = _issues(comparisons)
    used_observations = tuple(
        sorted(
            {
                value
                for comparison in comparisons
                for value in (
                    comparison.identity.used_observation_id,
                    *(
                        route.anchor_endpoint.observation_id
                        if route.anchor_endpoint is not None
                        else None
                        for route in comparison.routes
                    ),
                    *(
                        route.used_observation_id
                        for route in comparison.routes
                    ),
                )
                if value is not None
            }
        )
    )
    labels = tuple(
        sorted(
            {
                label
                for comparison in comparisons
                for label in (
                    *comparison.identity.attribution_labels,
                    *(
                        route_label
                        for route in comparison.routes
                        for route_label in route.attribution_labels
                    ),
                    *(
                        anchor_label
                        for route in comparison.routes
                        if route.anchor_resolution is not None
                        and route.anchor_endpoint is not None
                        and route.anchor_resolution.selected is not None
                        for anchor_label in _attribution_labels(
                            route.anchor_resolution.selected
                        )
                    ),
                )
            }
        )
    )
    basis = LodgingEvidenceBasis(
        snapshot=snapshot,
        used_observation_ids=used_observations,
        required_attribution_labels=labels,
        _token=_RESULT_TOKEN,
    )
    return LodgingComparisonAssessment(
        basis=basis,
        candidates=comparisons,
        issues=issues,
        _token=_RESULT_TOKEN,
    )


def _candidate_identity(
    candidate: LodgingCandidate,
    snapshot: EvidenceSnapshot,
) -> LodgingIdentityEvidence:
    location = candidate.draft.location
    if location.kind is not LocationHintKind.LOCATION_ID:
        return LodgingIdentityEvidence(
            candidate=candidate,
            snapshot=snapshot,
            resolution=None,
            endpoint=None,
            _token=_RESULT_TOKEN,
        )
    assert location.location_id is not None
    resolution, endpoint = _resolve_endpoint(snapshot, location.location_id)
    return LodgingIdentityEvidence(
        candidate=candidate,
        snapshot=snapshot,
        resolution=resolution,
        endpoint=endpoint,
        _token=_RESULT_TOKEN,
    )


def _resolve_endpoint(
    snapshot: EvidenceSnapshot,
    location_id: str,
) -> tuple[FactResolution, PlaceEndpointIdentity | None]:
    resolution = snapshot.resolve(_identity_key(location_id))
    endpoint = None
    if resolution.evidence_state is EvidenceState.VERIFIED:
        endpoint = extract_fresh_google_place_endpoint(
            snapshot,
            location_id,
        )
    return resolution, endpoint


def _route_evidence(
    *,
    probe: LodgingRouteProbe,
    snapshot: EvidenceSnapshot,
    candidate_identity: LodgingIdentityEvidence,
    anchor_resolution: FactResolution,
    anchor_endpoint: PlaceEndpointIdentity | None,
) -> LodgingRouteEvidence:
    candidate_endpoint = candidate_identity.endpoint
    current_request: GoogleRouteRequest | None = None
    if candidate_endpoint is not None and anchor_endpoint is not None:
        if candidate_endpoint.location_id == anchor_endpoint.location_id:
            raise ValueError("Lodging route endpoints must be distinct")
        origin, destination = _ordered_endpoints(
            probe,
            candidate_endpoint,
            anchor_endpoint,
        )
        current_request = build_google_route_request(
            snapshot,
            origin,
            destination,
            probe.mode,
            departure_at=probe.departure_at,
        )

    resolution: FactResolution | None = None
    basis_unchanged = False
    receipt = probe.basis_request
    if receipt is not None:
        _validate_route_receipt(
            probe,
            receipt,
        )
        resolution = snapshot.resolve(
            receipt.provider_request.fact_keys[0]
        )
        if candidate_endpoint is not None and anchor_endpoint is not None:
            current_origin, current_destination = _ordered_endpoints(
                probe,
                candidate_endpoint,
                anchor_endpoint,
            )
            if (
                receipt.origin.location_id
                != current_origin.location_id
                or receipt.destination.location_id
                != current_destination.location_id
            ):
                raise FactContractError(
                    "EVIDENCE_BINDING_MISMATCH",
                    "Route receipt endpoints differ from its comparison probe.",
                )
            basis_unchanged = (
                _same_endpoint_basis(receipt.origin, current_origin)
                and _same_endpoint_basis(
                    receipt.destination,
                    current_destination,
                )
            )

    refresh_request: GoogleRouteRequest | None = None
    if current_request is not None:
        usable = (
            receipt is not None
            and basis_unchanged
            and resolution is not None
            and resolution.evidence_state is EvidenceState.VERIFIED
            and resolution.selected is not None
            and resolution.selected.provenance.request_fingerprint
            == receipt.provider_request.request_fingerprint
        )
        if not usable:
            refresh_request = current_request

    return LodgingRouteEvidence(
        probe=probe,
        snapshot=snapshot,
        candidate_identity=candidate_identity,
        anchor_resolution=anchor_resolution,
        anchor_endpoint=anchor_endpoint,
        resolution=resolution,
        refresh_request=refresh_request,
        basis_unchanged=basis_unchanged,
        _token=_RESULT_TOKEN,
    )


def _ordered_endpoints(
    probe: LodgingRouteProbe,
    lodging: PlaceEndpointIdentity,
    anchor: PlaceEndpointIdentity,
) -> tuple[PlaceEndpointIdentity, PlaceEndpointIdentity]:
    if probe.direction is LodgingRouteDirection.FROM_LODGING:
        return lodging, anchor
    return anchor, lodging


def _validate_route_receipt(
    probe: LodgingRouteProbe,
    request: GoogleRouteRequest,
) -> None:
    key = request.provider_request.fact_keys[0]
    qualifiers = key.qualifier_map
    expected_subjects = (
        (request.origin.location_id, request.destination.location_id)
    )
    if (
        key.kind is not FactKind.ROUTE_ESTIMATE
        or key.subject_ids != expected_subjects
        or request.mode is not probe.mode
        or request.departure_at != probe.departure_at
        or qualifiers.get("mode") != probe.mode.value
        or qualifiers.get("departure_at") != probe.departure_at
    ):
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "Route receipt differs from its comparison probe.",
        )
    if probe.direction is LodgingRouteDirection.FROM_LODGING:
        candidate_location = request.origin.location_id
        anchor_location = request.destination.location_id
    else:
        candidate_location = request.destination.location_id
        anchor_location = request.origin.location_id
    if anchor_location != probe.anchor_location_id:
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "Route receipt differs from its comparison anchor.",
        )
    # The candidate's raw location ID is checked against the current endpoint
    # later.  It is deliberately not copied into any safe issue or serializer.
    if not candidate_location:
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "Route receipt has no lodging endpoint.",
        )


def _same_endpoint_basis(
    previous: PlaceEndpointIdentity,
    current: PlaceEndpointIdentity,
) -> bool:
    return (
        previous.location_id == current.location_id
        and previous.provider_id == current.provider_id
        and previous.observation_id == current.observation_id
        and previous.value_digest == current.value_digest
    )


def _issues(
    comparisons: tuple[LodgingComparisonCandidate, ...],
) -> tuple[LodgingEvidenceIssue, ...]:
    issues: list[LodgingEvidenceIssue] = []
    for comparison in comparisons:
        disposition = comparison.identity.disposition
        if disposition is not LodgingIdentityDisposition.VERIFIED:
            conflict = (
                disposition is LodgingIdentityDisposition.CONFLICTED
            )
            issues.append(
                LodgingEvidenceIssue(
                    code=(
                        "LODGING_IDENTITY_CONFLICTED"
                        if conflict
                        else "LODGING_IDENTITY_REQUIRED"
                    ),
                    severity=(
                        IssueSeverity.ERROR
                        if conflict
                        else IssueSeverity.WARNING
                    ),
                    message=(
                        "Lodging location identity is conflicted."
                        if conflict
                        else (
                            "Lodging location needs trusted identity "
                            "verification before route claims."
                        )
                    ),
                    candidate_ids=(comparison.candidate_id,),
                    suggested_actions=("verify_lodging_location",),
                )
            )
        for route in comparison.routes:
            if route.disposition is LodgingRouteDisposition.VERIFIED:
                continue
            conflict = (
                route.disposition is LodgingRouteDisposition.CONFLICTED
            )
            issues.append(
                LodgingEvidenceIssue(
                    code=(
                        "LODGING_ROUTE_CONFLICTED"
                        if conflict
                        else "LODGING_ROUTE_EVIDENCE_REQUIRED"
                    ),
                    severity=(
                        IssueSeverity.ERROR
                        if conflict
                        else IssueSeverity.WARNING
                    ),
                    message=(
                        "Lodging comparison route evidence is conflicted."
                        if conflict
                        else (
                            "Lodging comparison route needs an exact current "
                            "evidence receipt."
                        )
                    ),
                    candidate_ids=(comparison.candidate_id,),
                    probe_ids=(route.probe.probe_id,),
                    suggested_actions=("refresh_lodging_route",),
                )
            )
    return tuple(issues)


def _normalized_digests(
    values: tuple[str, ...],
    name: str,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    normalized = tuple(_digest(item, f"{name} item") for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} cannot contain duplicates")
    return tuple(sorted(normalized))


def _normalized_labels(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError("required_attribution_labels must be a tuple")
    normalized = tuple(
        _text(item, "required attribution label", maximum=256)
        for item in values
    )
    return tuple(sorted(set(normalized)))


__all__ = [
    "LODGING_COMPARISON_VERSION",
    "LodgingComparisonAssessment",
    "LodgingComparisonCandidate",
    "LodgingComparisonCapability",
    "LodgingEvidenceBasis",
    "LodgingEvidenceIssue",
    "LodgingIdentityDisposition",
    "LodgingIdentityEvidence",
    "LodgingRouteDirection",
    "LodgingRouteDisposition",
    "LodgingRouteEvidence",
    "LodgingRouteProbe",
    "assess_lodging_evidence",
]
